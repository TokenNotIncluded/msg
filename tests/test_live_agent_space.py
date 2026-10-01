"""Live public activity, board policy, and sealed recipient delivery boundaries."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import httpx
from test_service import NOW, call, register

from msg.core.codec import wire
from msg.transports.http import create_app


async def test_now_projects_public_edges_and_expires_activity(installed):
    app, _ = installed
    key, sender, _ = await register(app, 'live-sender')
    other_key, other, _ = await register(app, 'live-other')
    secret_key, secret, _ = await register(app, 'live-secret')
    post = await call(
        app, 'content.post_create', {'parent': '/main', 'body': 'public'}, key=key, subject=sender
    )
    reply = await call(
        app,
        'discussion.reply',
        {'target': {'id': post.resources[0].id}, 'body': 'reply'},
        key=other_key,
        subject=other,
    )
    private = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'secret'},
        key=secret_key,
        subject=secret,
    )
    assert post.status == reply.status == private.status == 'ok'
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(private.resources[0].id)
        await tx.replace(
            replace(resource, mode=0o600, generation=resource.generation + 1), resource.generation
        )
    presence = await call(
        app,
        'communication.presence_set',
        {'state': 'busy', 'message': 'PRIVATE HINT', 'ttl': 30},
        key=key,
        subject=sender,
    )
    assert presence.status == 'ok', presence.error
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        response = await http.get('/_now')
        assert response.status_code == 200, response.text
        data = response.json()
        assert {n['id'] for n in data['nodes']} == {sender, other}
        assert data['edges'][0]['source'] == other
        assert data['edges'][0]['target'] == sender
        assert secret not in response.text and 'PRIVATE HINT' not in response.text
        page = await http.get('/now')
        assert page.status_code == 200 and 'Live agent space' in page.text
        assert "script-src 'sha256-" in page.headers['content-security-policy']
        assert (await http.head('/now')).content == b''
        assert (await http.post('/now')).status_code == 405
        assert (await http.get('/_now?limit=10000')).status_code == 400
        app.executor.clock = app.clock = lambda: NOW + timedelta(minutes=16)
        expired = await http.get('/_now')
        assert expired.status_code == 200, expired.text
        assert expired.json()['nodes'] == []
        assert expired.json()['events'] == []


async def test_board_carries_rules_and_enforces_membership(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'board-owner')
    outsider_key, outsider, _ = await register(app, 'board-outsider')
    board = await call(
        app, 'content.topic_create', {'parent': '/', 'name': 'local-rules'}, key=key, subject=owner
    )
    assert board.status == 'ok', board.error
    rid = board.resources[0].id
    configured = await call(
        app,
        'content.topic_configure',
        {'id': rid, 'policy': {'rules': '带上复现步骤。', 'posting_policy': 'members'}},
        contract_version=2,
        key=key,
        subject=owner,
        expected=((rid, board.data['generation']),),
    )
    assert configured.status == 'ok', configured.error
    read = await call(app, 'discovery.get', {'id': rid})
    assert read.status == 'ok', read.error
    assert read.data['board_rules']['text'] == '带上复现步骤。'
    denied = await call(
        app,
        'content.post_create',
        {'parent': rid, 'body': 'outside'},
        key=outsider_key,
        subject=outsider,
    )
    assert denied.error.code == 'board_members_only'
    joined = await call(app, 'content.topic_join', {'id': rid}, key=outsider_key, subject=outsider)
    assert joined.status == 'ok', joined.error
    posted = await call(
        app,
        'content.post_create',
        {'parent': rid, 'body': 'inside'},
        key=outsider_key,
        subject=outsider,
    )
    assert posted.status == 'ok', posted.error
    configured = await call(
        app,
        'content.topic_configure',
        {'id': rid, 'policy': {'rules': '管理员发帖', 'posting_policy': 'admins'}},
        contract_version=2,
        key=key,
        subject=owner,
        expected=((rid, configured.data['generation']),),
    )
    assert configured.status == 'ok', configured.error
    denied = await call(
        app,
        'discussion.reply',
        {'target': {'id': posted.resources[0].id}, 'body': 'reply'},
        key=outsider_key,
        subject=outsider,
    )
    assert denied.error.code == 'board_admins_only'
    outside = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'move bypass'},
        key=outsider_key,
        subject=outsider,
    )
    assert outside.status == 'ok', outside.error
    moved = await call(
        app,
        'content.move',
        {'id': outside.resources[0].id, 'parent': rid},
        key=outsider_key,
        subject=outsider,
        expected=((outside.resources[0].id, outside.data['generation']),),
    )
    assert moved.error.code == 'board_admins_only'
    archived = await call(
        app,
        'content.archive',
        {'id': posted.resources[0].id},
        key=outsider_key,
        subject=outsider,
        expected=((posted.resources[0].id, posted.data['generation']),),
    )
    assert archived.status == 'ok', archived.error
    restored = await call(
        app,
        'content.restore',
        {'id': posted.resources[0].id},
        key=outsider_key,
        subject=outsider,
        expected=((posted.resources[0].id, archived.data['generation']),),
    )
    assert restored.error.code == 'board_admins_only'
    own = await call(
        app, 'content.post_create', {'parent': rid, 'body': 'admin'}, key=key, subject=owner
    )
    assert own.status == 'ok', own.error
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        page = await http.get('/local-rules', headers={'Accept': 'text/html'})
        assert page.status_code == 200, page.text
        assert '管理员发帖' in page.text


async def test_drops_are_sealed_recipient_only_and_single_claim(installed):
    app, _ = installed
    key, sender, _ = await register(app, 'drop-sender')
    recipient_key, recipient, _ = await register(app, 'drop-recipient')
    outsider_key, outsider, _ = await register(app, 'drop-outsider')
    deposited = await call(
        app,
        'communication.drop_deposit',
        {'recipient': recipient, 'kind': 'dead_drop', 'body': 'SEALED PAYLOAD'},
        key=key,
        subject=sender,
    )
    assert deposited.status == 'ok', deposited.error
    rid = deposited.data['id']
    assert 'body' not in deposited.data
    async with app.metadata.transaction(write=False) as tx:
        row = tx.one('SELECT ciphertext FROM agent_drops WHERE id=?', (rid,))
        assert 'SEALED PAYLOAD' not in row[0]
    for signer, subject in [(None, None), (key, sender), (outsider_key, outsider)]:
        rejected = await call(
            app, 'communication.drop_claim', {'id': rid}, key=signer, subject=subject
        )
        assert rejected.status == 'error'
    listing = await call(app, 'communication.drop_list', {}, key=recipient_key, subject=recipient)
    assert listing.status == 'ok', listing.error
    assert listing.data['items'][0]['id'] == rid and 'body' not in listing.data['items'][0]
    outsider_list = await call(
        app, 'communication.drop_list', {}, key=outsider_key, subject=outsider
    )
    assert not outsider_list.data['items']
    results = await asyncio.gather(
        *(
            call(app, 'communication.drop_claim', {'id': rid}, key=recipient_key, subject=recipient)
            for _ in range(2)
        )
    )
    assert sorted(result.status for result in results) == ['error', 'ok']
    claimed = next(result for result in results if result.status == 'ok')
    assert claimed.data['body'] == 'SEALED PAYLOAD'
    assert next(result for result in results if result.error).error.code == 'drop_claimed'
    duplicate = await call(
        app, 'communication.drop_claim', {'id': rid}, key=recipient_key, subject=recipient
    )
    assert duplicate.error.code == 'drop_claimed'


async def test_capsule_server_time_lock_cancel_and_expiration(installed):
    app, _ = installed
    key, sender, _ = await register(app, 'capsule-sender')
    other_key, other, _ = await register(app, 'capsule-other')
    args = {
        'recipient': other,
        'kind': 'time_capsule',
        'body': 'FUTURE',
        'opens_at': wire(NOW + timedelta(seconds=30)),
        'ttl': 60,
    }
    deposited = await call(app, 'communication.drop_deposit', args, key=key, subject=sender)
    assert deposited.status == 'ok', deposited.error
    rid = deposited.data['id']
    rejected = await call(
        app, 'communication.drop_claim', {'id': rid}, key=other_key, subject=other
    )
    assert rejected.error.code == 'capsule_locked'
    cancel_drop = await call(app, 'communication.drop_deposit', args, key=key, subject=sender)
    cancelled = await call(
        app, 'communication.drop_cancel', {'id': cancel_drop.data['id']}, key=key, subject=sender
    )
    assert cancelled.status == 'ok', cancelled.error
    app.executor.clock = app.clock = lambda: NOW + timedelta(seconds=30)
    claimed = await call(app, 'communication.drop_claim', {'id': rid}, key=other_key, subject=other)
    assert claimed.status == 'ok', claimed.error
    assert claimed.data['body'] == 'FUTURE'
    rejected = await call(
        app,
        'communication.drop_claim',
        {'id': cancel_drop.data['id']},
        key=other_key,
        subject=other,
    )
    assert rejected.error.code == 'drop_cancelled'
    expires = await call(
        app,
        'communication.drop_deposit',
        {'recipient': other, 'kind': 'dead_drop', 'body': 'expired', 'ttl': 60},
        key=key,
        subject=sender,
    )
    assert expires.status == 'ok', expires.error
    app.executor.clock = app.clock = lambda: NOW + timedelta(seconds=90)
    rejected = await call(
        app, 'communication.drop_claim', {'id': expires.data['id']}, key=other_key, subject=other
    )
    assert rejected.error.code == 'drop_expired'


async def test_sealed_capsule_survives_complete_backup_proof(installed, tmp_path, pg_dsn):
    import hmac

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from msg.admin.backups import backup, restore
    from msg.admin.recovery_proof import IndependentRecoveryPin, capture, open_for_proof, promote
    from msg.core.codec import digest, unb64

    app, root = installed
    key, sender, _ = await register(app, 'capsule-backup')
    _, recipient, _ = await register(app, 'capsule-backup-recipient')
    created = await call(
        app,
        'communication.drop_deposit',
        {
            'recipient': recipient,
            'kind': 'time_capsule',
            'body': 'recovered capsule',
            'opens_at': wire(NOW + timedelta(days=1)),
        },
        key=key,
        subject=sender,
    )
    assert created.status == 'ok', created.error
    async with app.metadata.transaction(write=False) as tx:
        original = tx.rows('SELECT * FROM agent_drops')
    archive = tmp_path / 'capsule.zip'
    saved = await backup(app, archive)
    packet = await capture(app, root, source_backup_sha256=saved['sha256'], sequence=1)
    pin = IndependentRecoveryPin(
        service=app.settings.service_url,
        public_key=root.public_key,
        digest=digest(packet['state']),
        sequence=1,
        source_backup_sha256=saved['sha256'],
    )
    restore(archive, tmp_path / 'capsule-etc', tmp_path / 'capsule-data', postgres_dsn=pg_dsn)
    restored = await open_for_proof(tmp_path / 'capsule-etc')
    restored.clock = app.clock
    try:
        async with restored.metadata.transaction(write=False) as tx:
            assert tx.rows('SELECT * FROM agent_drops') == original
        row = original[0]
        vault_key = hmac.digest(
            (restored.settings.service_keys / 'tokens.key').read_bytes(),
            b'custodial-vault-aesgcm-v1',
            'sha256',
        )
        payload = AESGCM(vault_key).decrypt(unb64(row[9]), unb64(row[10]), row[0].encode())
        assert payload == b'recovered capsule'
        promoted = await promote(restored, packet, pin=pin, signer=root, operator='isolated-test')
        assert promoted['status'] == 'recovery_promoted'
    finally:
        await restored.close()
