"""Credential delivery has one owner, separate from service composition."""

import ast
import asyncio
import hashlib
import hmac
import importlib
import importlib.util
import inspect
import subprocess
import sys
from pathlib import Path

import pytest
from test_service import NOW, temporary_v3_args

from msg.core.codec import b64, canonical, loads, unb64, wire
from msg.core.errors import Failure
from msg.core.requests import receipt_bytes, request_for
from msg.security.crypto import verify


def delivery_module():
    assert importlib.util.find_spec('msg.security.token_delivery') is not None
    return importlib.import_module('msg.security.token_delivery')


def test_composition_only_delegates_the_existing_delivery_api():
    from msg.application import Application

    module = delivery_module()
    tree = ast.parse(inspect.getsource(Application))
    methods = {
        node.name: node
        for node in tree.body[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in ('issued_token', 'record_token_delivery', '_secrets_for_caller'):
        method = methods[name]
        assert len(method.body) == 1 and isinstance(method.body[0], ast.Return)
        assert 'token_delivery' in ast.unparse(method)
    assert Application.recovery_verifier is module.recovery_verifier
    assert module.TokenDelivery.recovery_verifier is module.recovery_verifier


def test_domain_import_and_constructor_need_no_application_or_plugins():
    module = delivery_module()
    assert set(inspect.signature(module.TokenDelivery).parameters) == {
        'metadata',
        'token_secret',
        'clock',
        'recovery_window',
    }
    code = """
import importlib.abc
import sys
class NoApplication(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('msg.application', 'msg.plugins', 'msg.transports', 'msg.storage')):
            raise AssertionError('delivery imports composition/adapter: ' + fullname)
sys.meta_path.insert(0, NoApplication())
from msg.security.token_delivery import TokenDelivery
instance = TokenDelivery(metadata=None, token_secret=b'x'*32,
                         clock=lambda: None, recovery_window=lambda: 900)
assert not hasattr(instance, 'application')
"""
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_derivation_and_recovery_verifier_keep_the_published_bytes():
    module = delivery_module()
    instance = module.TokenDelivery(
        metadata=None, token_secret=b'k' * 32, clock=lambda: NOW, recovery_window=lambda: 900
    )
    request = request_for(
        'identity.token_create',
        {'nonce': b64(b'n' * 32)},
        'https://example.invalid',
        request_id='stable-delivery',
        contract_version=2,
    )
    expected = hmac.digest(
        b'k' * 32,
        b'issued-token-v1\0'
        + canonical({
            'subject': 'u_subject',
            'request_id': 'stable-delivery',
            'nonce': b64(b'n' * 32),
        }),
        'sha256',
    )
    assert instance.issued_token(request, 'u_subject') == expected
    assert instance.issued_token(request, 'u_other') != expected
    assert (
        module.recovery_verifier(b64(b'r' * 32))
        == hashlib.sha256(b'token-recovery-v1\0' + b'r' * 32).hexdigest()
    )
    with pytest.raises(Failure, match='invalid_recovery_secret'):
        module.recovery_verifier(b64(b'r' * 31))


def test_authentication_uses_the_same_verifier_owner():
    from msg.security import authentication

    module = delivery_module()
    assert authentication.recovery_verifier is module.recovery_verifier
    source = Path(authentication.__file__).read_text()
    assert 'token-recovery-v1' not in source


async def test_real_service_binds_one_delivery_instance_for_all_executors(installed):
    app, _ = installed
    module = delivery_module()
    assert isinstance(app.token_delivery, module.TokenDelivery)
    assert app.executor.response_hook == app.token_delivery.release
    assert app.new_executor().response_hook == app.token_delivery.release


async def test_standalone_delivery_claim_commits_once_without_persisting_secrets(installed):
    app, _ = installed
    module = delivery_module()
    args, rid, _, _ = temporary_v3_args(request_id='domain-claim-once')
    request = request_for(
        'identity.temporary',
        args,
        app.settings.service_url,
        request_id=rid,
        contract_version=3,
        expires_at=NOW.replace(minute=1),
    )
    app.executor.response_hook = None
    committed = await app.executor.execute(request)
    assert committed.status == 'ok' and 'token' not in committed.data, wire(committed)
    delivery = module.TokenDelivery(
        metadata=app.metadata,
        token_secret=app._token_secret,
        clock=lambda: NOW,
        recovery_window=lambda: app.settings.credential_delivery_recovery_window,
    )
    answers = await asyncio.gather(
        delivery.release(request, committed),
        delivery.release(request, committed),
        return_exceptions=True,
    )
    successes = [value for value in answers if not isinstance(value, BaseException)]
    failures = [value for value in answers if isinstance(value, Failure)]
    assert len(successes) == len(failures) == 1
    assert failures[0].code == 'token_delivery_unavailable'
    released = successes[0]
    assert unb64(released.data['token']) == app.issued_token(request, committed.subject)
    assert released.receipt == committed.receipt
    verify(
        app.receipt_signer.public_key, receipt_bytes(released), released.receipt, purpose='receipt'
    )
    async with app.metadata.transaction(write=False) as tx:
        saved = await tx.request_result(committed.subject, rid, request.payload_digest)
        assert saved.receipt == committed.receipt and receipt_bytes(saved) == receipt_bytes(
            committed
        )
        assert saved.data == committed.data and 'token' not in saved.data
        row = tx.one(
            'SELECT claimed_at,recovery_verifier FROM token_deliveries WHERE credential_id=?',
            (committed.data['credential_id'],),
        )
        assert row[0] is not None and row[1] == module.recovery_verifier(args['recovery_secret'])
        assert released.data['token'] not in str(row) and args['recovery_secret'] not in str(row)
        # Event fields are stored in canonical JSON, not separate SQL columns.
        events = [loads(row[0]) for row in tx.rows('SELECT body FROM events')]
        matching = [event for event in events if event.get('request_id') == rid]
        assert len(matching) == 1
        assert matching[0]['subject'] == committed.subject
        assert released.data['token'] not in canonical(matching[0]).decode()
        assert args['recovery_secret'] not in canonical(matching[0]).decode()


async def test_delivery_failure_returns_no_new_secret_or_claim(installed):
    app, _ = installed
    module = delivery_module()
    args, rid, _, _ = temporary_v3_args(request_id='domain-expired-claim')
    request = request_for(
        'identity.temporary',
        args,
        app.settings.service_url,
        request_id=rid,
        contract_version=3,
        expires_at=NOW.replace(minute=1),
    )
    app.executor.response_hook = None
    committed = await app.executor.execute(request)
    assert committed.status == 'ok', wire(committed)
    async with app.metadata.transaction(write=False) as tx:
        credential = await tx.credential(committed.data['credential_id'])
    delivery = module.TokenDelivery(
        metadata=app.metadata,
        token_secret=app._token_secret,
        clock=lambda: credential.expires_at,
        recovery_window=lambda: 900,
    )
    with pytest.raises(Failure, match='credential_expired'):
        await delivery.release(request, committed)
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one(
                'SELECT claimed_at FROM token_deliveries WHERE credential_id=?', (credential.id,)
            )[0]
            is None
        )
        saved = await tx.request_result(committed.subject, rid, request.payload_digest)
        assert saved.receipt == committed.receipt and receipt_bytes(saved) == receipt_bytes(
            committed
        )
        assert saved.data == committed.data and 'token' not in saved.data
