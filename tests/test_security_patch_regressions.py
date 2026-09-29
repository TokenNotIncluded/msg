"""Real authenticated regressions for security PRs 121--128 on current main."""

import asyncio
import os
from datetime import timedelta

import pytest
from test_achievements import advance
from test_custodial_upgrade import begin, finish
from test_service import NOW, call, register
from test_store_foundation import listing_args

from msg.core.codec import b64, decode, digest, unb64, wire
from msg.core.models import BlobRef
from msg.core.requests import request_for
from msg.plugins.achievements import CEREMONY_TTL_SECONDS
from msg.security.age_keys import generate_age_key
from msg.security.crypto import Ed25519Signer
from msg.workers import maintenance


async def fresh_call(
    app,
    operation,
    arguments,
    *,
    key=None,
    subject=None,
    token=None,
    rid=None,
    expected=(),
    return_fields=(),
):
    packet = request_for(
        operation,
        arguments,
        app.settings.service_url,
        signer=key,
        subject=subject,
        token=token,
        request_id=rid,
        expected=expected,
        return_fields=return_fields,
        expires_at=app.clock() + timedelta(seconds=120),
    )
    return await app.executor.execute(packet)


def set_clock(app, now):
    # Authentication, certificate validity, business TTL and maintenance use the
    # same instant; a valid fresh envelope must not conceal a stale clock.
    app.clock = app.executor.clock = app.authenticator.clock = app.certificates.clock = lambda: now


@pytest.mark.asyncio
async def test_package_projection_failure_does_not_leak_pins(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'security-deposit')
    listing = await call(app, 'store.listing_create', listing_args(), key=key, subject=subject)
    source = await call(
        app,
        'content.file_put',
        {
            'parent': '/@security-deposit/files',
            'name': 'secret.bin',
            'media_type': 'application/octet-stream',
            'data': b64(b'private'),
        },
        key=key,
        subject=subject,
    )
    assert listing.status == source.status == 'ok'
    args = {
        'listing_id': listing.resources[0].id,
        'listing_revision': listing.resources[0].revision,
        'manifest': {},
        'payload_refs': [wire(source.resources[0])],
    }
    before = {p for p in (app.contents.path / 'pins').glob('*/*') if p.is_file()}
    for _ in range(2):
        result = await fresh_call(
            app,
            'store.package_deposit',
            args,
            key=key,
            subject=subject,
            rid='failed-deposit',
            return_fields=('not_a_field',),
        )
        assert result.error.code == 'unknown_projection_field', wire(result)
        assert {p for p in (app.contents.path / 'pins').glob('*/*') if p.is_file()} == before
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM store_packages')[0] == 0
    accepted = await fresh_call(
        app, 'store.package_deposit', args, key=key, subject=subject, rid='valid-deposit'
    )
    assert accepted.status == 'ok', wire(accepted)
    replay = await fresh_call(
        app, 'store.package_deposit', args, key=key, subject=subject, rid='valid-deposit'
    )
    assert replay.status == 'ok' and replay.replayed
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM store_packages')[0] == 1
        revision = await tx.revision(source.resources[0])
    assert await app.contents.pinned(revision.content, accepted.data['package']['revision'])


@pytest.mark.asyncio
async def test_achievement_concurrent_start_and_expired_replacement(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'security-achievement')
    results = await asyncio.gather(
        *(
            fresh_call(app, 'achievement.start', {}, key=key, subject=subject, rid=f'start-{i}')
            for i in range(2)
        )
    )
    successful = [r for r in results if r.status == 'ok']
    denied = [r for r in results if r.status == 'error']
    assert len(successful) == len(denied) == 1, [wire(r) for r in results]
    assert denied[0].error.code == 'achievement_ceremony_active'
    replay = await fresh_call(
        app, 'achievement.start', {}, key=key, subject=subject, rid=successful[0].request_id
    )
    assert replay.status == 'ok' and replay.replayed
    set_clock(app, NOW + timedelta(seconds=CEREMONY_TTL_SECONDS + 1))
    replaced = await fresh_call(app, 'achievement.start', {}, key=key, subject=subject)
    assert replaced.status == 'ok', wire(replaced)
    assert replaced.data['challenge_id'] != successful[0].data['challenge_id']
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one('SELECT COUNT(*) FROM achievement_ceremonies WHERE subject=?', (subject,))[0]
            == 1
        )


@pytest.mark.asyncio
async def test_achievement_failed_round_can_restart(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'security-ach-failed')
    first = await call(app, 'achievement.start', {}, key=key, subject=subject)
    failed = await advance(app, key, subject, first.data, answer='n')
    assert failed.status == 'ok' and failed.data['status'] == 'failed'
    replacement = await call(app, 'achievement.start', {}, key=key, subject=subject)
    assert replacement.status == 'ok'
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one('SELECT COUNT(*) FROM achievement_ceremonies WHERE subject=?', (subject,))[0]
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['achievement.start', 'hosting.preview'])
async def test_new_persistent_operations_obey_write_pause(installed, operation):
    app, _ = installed
    key, subject, _ = await register(app, 'security-pause')
    arguments, expected = {}, ()
    if operation == 'hosting.preview':
        website = await call(
            app,
            'hosting.create',
            {'parent': '/@security-pause', 'name': 'site'},
            key=key,
            subject=subject,
        )
        source = await call(
            app,
            'content.file_put',
            {
                'parent': '/@security-pause/files',
                'name': 'page.html',
                'media_type': 'text/html',
                'data': b64(b'hello'),
            },
            key=key,
            subject=subject,
        )
        assert website.status == source.status == 'ok'
        arguments = {
            'id': website.resources[0].id,
            'entries': [{'path': 'index.html', 'source': wire(source.resources[0])}],
        }
        expected = ((website.resources[0].id, website.data['generation']),)
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('runtime_config', {'accept_writes': False, 'cleanup_enabled': True})
        before = tuple(
            tx.one(f'SELECT COUNT(*) FROM {table}')[0]
            for table in ('resources', 'revisions', 'achievement_ceremonies')
        )
    result = await fresh_call(
        app, operation, arguments, key=key, subject=subject, expected=expected
    )
    assert result.status == 'error' and result.error.code == 'writes_paused', wire(result)
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tuple(
                tx.one(f'SELECT COUNT(*) FROM {table}')[0]
                for table in ('resources', 'revisions', 'achievement_ceremonies')
            )
            == before
        )


@pytest.mark.asyncio
async def test_hosting_preview_rejects_129_entries_before_storage(installed, monkeypatch):
    app, _ = installed
    key, subject, _ = await register(app, 'security-preview-limit')
    website = await call(
        app,
        'hosting.create',
        {'parent': '/@security-preview-limit', 'name': 'site'},
        key=key,
        subject=subject,
    )
    source = await call(
        app,
        'content.file_put',
        {
            'parent': '/@security-preview-limit/files',
            'name': 'page.html',
            'media_type': 'text/html',
            'data': b64(b'hello'),
        },
        key=key,
        subject=subject,
    )
    assert website.status == source.status == 'ok'
    arguments = {
        'id': website.resources[0].id,
        'entries': [{'path': f'{i}.html', 'source': wire(source.resources[0])} for i in range(129)],
    }
    expected = ((website.resources[0].id, website.data['generation']),)
    # A published wire schema is immutable. The runtime work bound is checked
    # after current authorization but before any source read or creation.
    spec = app.registry.operation('hosting.preview', 1)
    assert 'maxItems' not in app.registry.schema(spec.input_schema)['properties']['entries']
    app.registry.validate(spec.input_schema, arguments)
    async with app.metadata.transaction(write=False) as tx:
        before = tuple(
            tx.one(f'SELECT COUNT(*) FROM {table}')[0] for table in ('resources', 'revisions')
        )

    async def no_materialization(*args, **kwargs):
        pytest.fail('oversized preview touched the content store')

    with monkeypatch.context() as patch:
        patch.setattr(app.contents, 'read_bytes', no_materialization)
        patch.setattr(app.contents, 'put_bytes', no_materialization)
        denied = await fresh_call(
            app, 'hosting.preview', arguments, key=key, subject=subject, expected=expected
        )
    assert denied.status == 'error' and denied.error.code == 'too_many_preview_entries', wire(
        denied
    )
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tuple(
                tx.one(f'SELECT COUNT(*) FROM {table}')[0] for table in ('resources', 'revisions')
            )
            == before
        )
    # An actual authorized 128-entry preview remains valid, not just its schema.
    allowed = await fresh_call(
        app,
        'hosting.preview',
        {**arguments, 'entries': arguments['entries'][:128]},
        key=key,
        subject=subject,
        expected=expected,
    )
    assert allowed.status == 'ok' and allowed.data['files'] == 128, wire(allowed)


async def custody(app, handle):
    created = await call(
        app,
        'identity.custodial_create',
        {'handle': handle, 'nonce': b64(os.urandom(32)), 'recovery_secret': b64(os.urandom(32))},
        contract_version=2,
    )
    assert created.status == 'ok', wire(created)
    return created.data['subject_id'], (created.data['credential_id'], unb64(created.data['token']))


@pytest.mark.asyncio
@pytest.mark.parametrize('complete', [False, True])
async def test_custodial_cleanup_preserves_accepted_upgrade_facts(installed, complete):
    app, _ = installed
    subject, token = await custody(app, 'security-custody')
    signer = Ed25519Signer.generate()
    private, recipient = generate_age_key()
    challenge = await begin(app, subject, token, signer, recipient)
    assert challenge.status == 'ok', wire(challenge)
    result = await finish(
        app, subject, token, signer, private, challenge.data, external_migrated=complete
    )
    assert result.status == 'ok', wire(result)
    assert result.data['status'] == ('completed' if complete else 'pending_rewrap')
    async with app.metadata.transaction(write=False) as tx:
        before = tx.one(
            'SELECT * FROM custodial_upgrades WHERE id=?', (challenge.data['challenge_id'],)
        )
    set_clock(app, NOW + timedelta(seconds=301))
    await maintenance.run_maintenance(app, 'cleanup_expired', scheduled=True)
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one('SELECT * FROM custodial_upgrades WHERE id=?', (challenge.data['challenge_id'],))
            == before
        )
    if complete:
        recovered = await fresh_call(
            app,
            'identity.custodial_upgrade_result',
            {'challenge_id': challenge.data['challenge_id']},
            key=signer,
            subject=subject,
        )
        assert recovered.status == 'ok' and recovered.data['status'] == 'completed', wire(recovered)


@pytest.mark.asyncio
async def test_expired_unaccepted_custodial_challenges_are_reclaimed(installed):
    app, _ = installed
    subject, token = await custody(app, 'security-abandoned')
    first = await begin(app, subject, token, Ed25519Signer.generate(), generate_age_key()[1])
    assert first.status == 'ok', wire(first)
    set_clock(app, NOW + timedelta(seconds=301))
    await maintenance.run_maintenance(app, 'cleanup_expired', scheduled=True)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM custodial_upgrades WHERE subject=?', (subject,))[0] == 0
        assert (await tx.subject(subject)).kind == 'custodial'
        assert (
            tx.one('SELECT status FROM custodial_vault WHERE subject=?', (subject,))[0] == 'active'
        )


@pytest.mark.asyncio
async def test_todo_sweep_has_bounded_content_reads_and_fair_cursor(installed, monkeypatch):
    app, _ = installed
    key, subject, _ = await register(app, 'security-todos')
    monkeypatch.setattr(maintenance, 'TODO_DELIVERY_BATCH_SIZE', 3, raising=False)
    for i in range(7):
        created = await call(
            app,
            'identity.todo_put',
            {'name': f'item-{i}', 'title': f'Todo {i}', 'due_at': wire(NOW - timedelta(hours=1))},
            key=key,
            subject=subject,
        )
        assert created.status == 'ok', wire(created)
    reads = 0
    original = app.contents.read_bytes

    async def observed_read(*args, **kwargs):
        nonlocal reads
        reads += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(app.contents, 'read_bytes', observed_read)
    for _ in range(3):
        reads = 0
        await maintenance.run_maintenance(app, 'deliver_due_todos', scheduled=True)
        assert reads <= 3
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM messages WHERE recipient=?', (subject,))[0] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize('sealed', [False, True])
async def test_expired_transfer_releases_only_its_pins(installed, sealed):
    app, _ = installed
    key, subject, _ = await register(app, 'security-upload')
    data = b'abandoned or published upload'
    opened = await call(
        app, 'transfer.open', {'direction': 'upload', 'size': len(data)}, key=key, subject=subject
    )
    assert opened.status == 'ok', wire(opened)
    tid = opened.data['transfer_id']
    uploaded = await call(
        app,
        'transfer.part_put',
        {'transfer_id': tid, 'offset': 0, 'data': b64(data), 'digest': digest(data)},
        key=key,
        subject=subject,
    )
    assert uploaded.status == 'ok', wire(uploaded)
    blob = decode(BlobRef, uploaded.data['chunk']['content'])
    assert await app.contents.pinned(blob, tid + ':0')
    if sealed:
        result = await call(
            app,
            'transfer.seal',
            {'transfer_id': tid, 'final_size': len(data), 'final_digest': digest(data)},
            key=key,
            subject=subject,
        )
        assert result.status == 'ok', wire(result)
        async with app.metadata.transaction(write=False) as tx:
            revision = await tx.revision(result.resources[0])
        assert await app.contents.pinned(revision.content, tid + ':sealed')
    set_clock(app, NOW + timedelta(seconds=app.settings.transfer_ttl + 1))
    await maintenance.run_maintenance(app, 'cleanup_expired', scheduled=True)
    assert not await app.contents.pinned(blob, tid + ':0')
    if sealed:
        assert not await app.contents.pinned(revision.content, tid + ':sealed')
        assert await app.contents.pinned(revision.content, revision.id)
        assert await app.contents.read_bytes(revision.content) == data
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM chunks WHERE transfer_id=?', (tid,))[0] == 0
