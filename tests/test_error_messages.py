"""New failures have safe messages without changing historical signed results."""
from datetime import timedelta

import httpx
import pytest
from test_service import NOW

from msg.core.codec import canonical, wire
from msg.core.errors import Failure
from msg.core.models import OperationResult
from msg.core.packet import decode_result
from msg.core.requests import receipt_bytes, request_for
from msg.security.crypto import Ed25519Signer, verify
from msg.transports.client import (
    GraphQLTransport,
    HTTPTransport,
    MCPHTTPTransport,
    PathGETTransport,
)
from msg.transports.http import create_app


@pytest.mark.parametrize('message', [None, 'An explicitly stored message.'])
def test_error_result_preserves_exact_canonical_bytes_and_receipt(message):
    result = OperationResult(request_id='old-result', operation='discovery.get', status='error',
                             actor=None, subject=None)
    legacy = wire(result)
    legacy['error'] = {'code': 'permission_denied', 'retryable': False,
                       'field_path': None, 'retry_after_seconds': None}
    if message is not None:
        legacy['error']['message'] = message
    signing = {key: value for key, value in legacy.items()
               if key not in {'receipt', 'replayed', 'prefer_cli', 'cli_url'}}
    original_receipt_payload = canonical(signing)
    signer = Ed25519Signer.generate()
    legacy['receipt'] = wire(signer.sign(original_receipt_payload, purpose='receipt'))
    decoded = decode_result(legacy)
    assert decoded.error.message == message
    assert canonical(decoded) == canonical(legacy)
    assert receipt_bytes(decoded) == original_receipt_payload
    verify(signer.public_key, receipt_bytes(decoded), decoded.receipt, purpose='receipt')


@pytest.mark.asyncio
async def test_real_operation_errors_keep_safe_message_across_network_adapters(installed, monkeypatch):
    app, _ = installed
    packet = request_for('discovery.get', {'id': 'missing-private-name'}, app.settings.service_url,
                         expires_at=NOW + timedelta(seconds=60))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        for kind in (HTTPTransport, PathGETTransport, GraphQLTransport, MCPHTTPTransport):
            result = await kind(app.settings.service_url, http=http).call(packet)
            assert result.error.code == 'not_found'
            assert result.error.message == 'The requested resource was not found.'
            assert 'missing-private-name' not in result.error.message
        async def unexpected(*args, **kwargs):
            raise RuntimeError('token=never-disclose-this')
        monkeypatch.setattr(app.authenticator, 'authenticate', unexpected)
        result = await HTTPTransport(app.settings.service_url, http=http).call(packet)
        assert result.error.code == 'internal_error'
        assert result.error.message == 'The service could not complete the operation.'
        assert 'never-disclose-this' not in canonical(result).decode()


def test_pre_executor_failure_uses_fixed_fallback_without_interpolating_input():
    result = Failure('future_code', details={'unused': 'secret-value'}).as_dict()
    assert result['message'] == 'The operation could not be completed.'
    assert 'future_code' not in result['message'] and 'secret-value' not in result['message']
