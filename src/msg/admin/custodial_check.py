"""Read-only lifecycle inspection and an isolated real-age history selftest."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx

from msg.core.codec import digest, loads, wire
from msg.core.errors import require


async def inspect_custodial_history(tx):
    from msg.security.custodial_migration import snapshot
    constraint = tx.one("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conrelid='custodial_vault'::regclass AND conname='custodial_vault_lifecycle_check'""")
    require(constraint is not None and 'decrypt_only' in constraint[0], 'custodial_lifecycle_schema_missing')
    states = []
    for subject, status, challenge, body in tx.rows(
            'SELECT subject,status,challenge,body FROM custodial_upgrades ORDER BY id'):
        if status == 'failed':
            continue
        view = (await snapshot(tx, subject, status, loads(challenge), loads(body))).data
        states.append({'subject': subject, 'challenge_id': view['challenge_id'], 'phase': view['phase'],
            'unresolved_count': len(view['unresolved_revisions']), 'inventory_changed': view['inventory_changed'],
            'retirement': view['retirement']})
    return {'migrations': states, 'backup_retirement_verified': False,
            'verification_scope': 'online_state_only'}


async def check_custodial_history(app, now):
    """Call only in the existing selftest disposable Test Root database."""
    from msg.client import ClientState, MsgClient
    from msg.client_custodial import migrate, transition
    from msg.client_recovery import _age
    from msg.core.models import Resource, Revision
    from msg.transports.client import HTTPTransport
    from msg.transports.http import create_app
    require(app.selftest_run_id is not None, 'isolated_selftest_required')
    with TemporaryDirectory(prefix='msg-history-selftest-') as directory:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app))) as http:
            client = MsgClient(ClientState(Path(directory), server=app.settings.service_url),
                HTTPTransport(app.settings.service_url, http=http), clock=lambda: now)
            created = client.checked(await client.custodial('history-' + uuid4().hex[:12]))
            subject = client.state.subject
            ciphertext = _age('--encrypt', '--recipient', created.data['encryption_recipient'],
                              input_data=b'isolated historical plaintext')
            # Explicit fixture fact, not a new privileged network operation. The
            # normal custodial credential ceiling does not grant keystore writes.
            async with app.metadata.transaction(write=True) as tx:
                folder = tx.one("SELECT id FROM resources WHERE parent=? AND name='keystore'", (subject,))[0]
                resource = Resource(id='ks_' + uuid4().hex, type='keystore', type_version=1,
                    name='history.age', parent=folder, owner=subject, group='g_public', mode=0o600,
                    generation=0, revision=None, state='active', created_at=now, created_by=subject,
                    modified_at=now, modified_by=subject)
                await tx.insert(resource)
                blob = await app.contents.put_bytes(ciphertext, 'application/octet-stream')
                revision = Revision(format_version=1, id='v_' + uuid4().hex, resource_id=resource.id,
                    parents=(), content=blob, relations=(), actor=subject, subject=subject, author=subject,
                    created_at=now, manifest_digest='')
                revision = replace(revision, manifest_digest=digest({k: v for k, v in wire(revision).items()
                                    if k not in {'manifest_digest', 'signature'}}))
                await app.contents.pin(blob, revision.id)
                await app.contents.commit_revision(folder, revision)
                await tx.append_revision(revision)
                await tx.replace(replace(resource, generation=1, revision=revision.id), 0)
                tx.set_setting('keystore_format:' + revision.id, 'age')
            pending = client.checked(await client.upgrade_custodial('history-self-' + uuid4().hex[:12],
                                      external_ciphertexts_migrated=True))
            if pending.data['status'] != 'pending_rewrap':
                return False
            result = await migrate(client)
            if not result['inventory']['history_recoverable']:
                return False
            done = client.checked(await transition(client, 'finalize', external_ciphertexts_migrated=True))
            return (done.data['history_recoverable'] and done.data['retirement']['online_encryption_key_deleted']
                    and not done.data['retirement']['backup_retired'] and client.state.token is None)
