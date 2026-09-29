from copy import deepcopy

import pytest
from test_legacy_resource_import import setup

from msg.admin import legacy_approval as module
from msg.core.errors import Failure


@pytest.mark.asyncio
async def test_review_requires_real_holder_and_explicit_mapping(installed, tmp_path):
    app, _root, _snapshot, approval, _key, _cert = await setup(installed, tmp_path)
    approval['identities'] = {'old-id': None}
    review = await module.validate_and_review(app, approval)
    assert review['real_holder']['subject'] == approval['operator']
    assert review['unmapped_count'] == 1
    assert review['explicit_new_subject_associations'] == {}
    bad = deepcopy(approval)
    bad['identities'] = {'old-id': 'u_nonexistent'}
    with pytest.raises(Failure):
        await module.validate_and_review(app, bad)
    bad = deepcopy(approval)
    bad['extra'] = True
    with pytest.raises(Failure, match='legacy_approval_invalid'):
        module.recognized_payload(bad)
    with pytest.raises(Failure, match='unsupported_legacy_approval_purpose'):
        module.recognized_payload({'format': 'certificate'})


def test_console_gate_precedes_files_and_pin(tmp_path, monkeypatch):
    def denied(*args):
        raise Failure('local_only')

    monkeypatch.setattr(module, 'require_local_console', denied)
    monkeypatch.setattr(module.getpass, 'getpass', lambda *_: pytest.fail('PIN requested'))
    with pytest.raises(Failure, match='local_only'):
        module.sign_approval(tmp_path, tmp_path / 'absent', tmp_path / 'out')
    assert not (tmp_path / 'out').exists()


def test_private_approval_output_is_exclusive(tmp_path):
    target = tmp_path / 'draft'
    module.write_new(target, {'format': 'example'})
    assert target.stat().st_mode & 0o777 == 0o600
    assert module.read_json(target) == {'format': 'example'}
    with pytest.raises(Failure, match='approval_destination_exists'):
        module.write_new(target, {})


def test_console_signs_only_reviewed_legacy_payload(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from msg.core.codec import canonical, decode, digest
    from msg.core.models import Signature
    from msg.security.crypto import Ed25519Signer, seal_private_key, verify
    from msg.storage.legacy_resource_import import PURPOSE, approval_payload

    root = Ed25519Signer.generate()
    approval = approval_payload(
        sha256='a' * 64,
        service='https://candidate.invalid',
        parent='r_parent',
        parent_generation=1,
        operator='u_holder',
        identities={'old': None},
        expires_at='2026-09-29T12:00:00Z',
    )
    draft, envelope, output = (tmp_path / name for name in ('draft', 'key', 'signed'))
    module.write_new(draft, approval)
    module.write_new(envelope, seal_private_key(root.private_bytes(), 'synthetic-test-pin'))

    async def inspect(*_):
        return {'approval_digest': digest(approval)}, root.public_key

    monkeypatch.setattr(module, 'require_local_console', lambda *_: 'test-console')
    monkeypatch.setattr(module, 'inspect_approval', inspect)
    monkeypatch.setattr(
        module, 'load_settings', lambda *_: SimpleNamespace(root_private_dir=tmp_path)
    )
    monkeypatch.setattr(module, 'rotation_lock', lambda *_: nullcontext())
    monkeypatch.setattr(module, 'root_envelope', lambda *_: envelope)
    monkeypatch.setattr(
        module, 'input', lambda *_: 'APPROVE LEGACY IMPORT ' + digest(approval), raising=False
    )
    monkeypatch.setattr(module.getpass, 'getpass', lambda *_: 'synthetic-test-pin')
    module.sign_approval(tmp_path, draft, output)
    result = module.read_json(output)
    verify(
        root.public_key,
        canonical(approval),
        decode(Signature, result['signature']),
        purpose=PURPOSE,
    )
    assert result['approval'] == approval
    monkeypatch.setattr(module, 'input', lambda *_: 'cancel', raising=False)
    monkeypatch.setattr(
        module.getpass, 'getpass', lambda *_: pytest.fail('PIN requested after cancel')
    )
    with pytest.raises(Failure, match='approval_cancelled'):
        module.sign_approval(tmp_path, draft, tmp_path / 'cancelled')
    assert not (tmp_path / 'cancelled').exists()


@pytest.mark.asyncio
async def test_runbook_holder_registers_through_local_http(installed, tmp_path):
    import httpx

    from msg.client import ClientState, MsgClient
    from msg.transports.client import HTTPTransport
    from msg.transports.http import create_app

    app, _ = installed
    state = ClientState(tmp_path / 'new-real-holder', server=app.settings.service_url)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        client = MsgClient(state, HTTPTransport(state.server, http=http), clock=app.clock)
        result = await client.register('migration-holder')
        assert result.status == 'ok', result.error
        for name in ('legacy-content', 'legacy-git', 'legacy-identities'):
            result = await client.call('content.topic_create', {'parent': '/main', 'name': name})
            assert result.status == 'ok', result.error
            ref = result.resources[0]
            async with app.metadata.transaction(write=False) as tx:
                generation = (await tx.resource(ref.id)).generation
            result = await client.call(
                'content.chmod', {'id': ref.id, 'mode': '0700'}, expected=((ref.id, generation),)
            )
            assert result.status == 'ok', result.error
            async with app.metadata.transaction(write=False) as tx:
                parent = await tx.resource(ref.id)
                assert parent.owner == state.subject and parent.mode == 0o700
    assert state.key_path.exists()
    assert ClientState(state.directory).subject == state.subject


@pytest.mark.asyncio
async def test_reviewed_visibility_restores_live_content_but_not_provenance(installed, tmp_path):
    from test_service import call

    from msg.core.codec import canonical, wire
    from msg.storage.legacy_resource_import import PURPOSE, import_content

    app, root, snapshot, approval, key, cert = await setup(installed, tmp_path)
    report = await import_content(
        app, snapshot, approval, wire(root.sign(canonical(approval), purpose=PURPOSE))
    )
    post_id = report['url_map']['/main/17']
    async with app.metadata.transaction(write=False) as tx:
        provenance_id = tx.setting('legacy-provenance:' + post_id)['resource']
    candidates = set(report['url_map'].values())
    candidates.discard(approval['parent'])
    for rid in [*sorted(candidates), approval['parent']]:
        async with app.metadata.transaction(write=False) as tx:
            resource = await tx.resource(rid)
        result = await call(
            app,
            'content.chmod',
            {'id': rid, 'mode': '0755' if resource.type == 'topic' else '0644'},
            key=key,
            subject=approval['operator'],
            certs=(cert,),
            expected=((rid, resource.generation),),
        )
        assert result.status == 'ok', result.error
    assert (await call(app, 'discovery.get', {'id': post_id})).status == 'ok'
    assert (await call(app, 'discovery.get', {'id': report['url_map']['/file/1']})).status == 'ok'
    assert (await call(app, 'discovery.get', {'id': provenance_id})).status == 'error'
