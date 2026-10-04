import json
from types import SimpleNamespace

import pytest

from msg.transports.safe_diagnostics import LOGGER, SafeDiagnostics


@pytest.mark.asyncio
async def test_early_rejection_trace_never_logs_private_url_or_body(monkeypatch):
    records, messages = [], []
    monkeypatch.setattr(LOGGER, 'info', lambda value: records.append(json.loads(value)))
    registry = SimpleNamespace(operation=lambda name: SimpleNamespace(name='identity.link_claim'))

    async def reject(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 403, 'headers': []})
        await send({
            'type': 'http.response.body',
            'body': b'{"status":"error","error":{"code":"passive_client_forbidden"},"private":"secret-response"}',
        })

    async def receive():
        raise AssertionError('Diagnostic must not read request data')

    async def send(message):
        messages.append(message)

    app = SafeDiagnostics(reject, service=SimpleNamespace(registry=registry))
    await app(
        {
            'type': 'http',
            'path': '/-/g/identity.link_claim/j/private-proof',
            'query_string': b'token=secret-token',
        },
        receive,
        send,
    )
    assert len(records) == 1
    row = records[0]
    assert row['http_status'] == 403 and row['error_code'] == 'passive_client_forbidden'
    assert row['operation'] == 'identity.link_claim'
    assert len(row['trace_id']) == 32
    assert set(row) == {'stage', 'operation', 'http_status', 'error_code', 'elapsed_ms', 'trace_id'}
    assert (b'x-msg-trace-id', row['trace_id'].encode()) in messages[0]['headers']
    assert 'secret' not in json.dumps(records) and 'private-proof' not in json.dumps(records)
