"""Account removal revokes access atomically without erasing resources or ledger history."""

import pytest
from test_service import call, register

from msg.admin.accounts import archive_account, archive_preview
from msg.core.codec import digest
from msg.core.errors import Failure
from msg.security.crypto import Ed25519Signer


@pytest.mark.asyncio
async def test_archive_blocks_existing_credentials_and_preserves_history(installed):
    app, root = installed
    key, uid, cert = await register(app, 'archive-me')
    async with app.metadata.transaction(write=False) as tx:
        preview = await archive_preview(tx, uid)
        before = {
            table: tx.one(f'SELECT COUNT(*) FROM {table}')[0]
            for table in ('resources', 'identities', 'money_ledger')
        }
    result = await archive_account(
        app, uid, root, expected_digest=digest(preview), operator='isolated-test-console'
    )
    assert result['archived'] and result['history_preserved']
    for operation, arguments in [
        ('discovery.get', {'id': '/main'}),
        ('identity.certificate_renew', {}),
    ]:
        denied = await call(app, operation, arguments, key=key, subject=uid, certs=(cert,))
        assert denied.error.code == 'account_archived'
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource(uid)).state == 'archived'
        assert before == {table: tx.one(f'SELECT COUNT(*) FROM {table}')[0] for table in before}
        for row in tx.rows('SELECT id FROM credentials WHERE subject=?', (uid,)):
            assert (await tx.credential(row[0])).revoked_at is not None
        assert tx.setting('identity_archived:' + uid)['handle'] == '@archive-me'
    with pytest.raises(Failure, match='account_already_archived'):
        await archive_account(app, uid, root, expected_digest=digest(preview), operator='test')


@pytest.mark.asyncio
async def test_archive_rejects_wrong_signer_and_changed_preview_without_side_effects(installed):
    app, root = installed
    _, uid, _ = await register(app, 'archive-guard')
    async with app.metadata.transaction(write=False) as tx:
        preview = await archive_preview(tx, uid)
    with pytest.raises(Failure, match='root_key_mismatch'):
        await archive_account(
            app, uid, Ed25519Signer.generate(), expected_digest=digest(preview), operator='test'
        )
    with pytest.raises(Failure, match='account_archive_preview_changed'):
        await archive_account(app, uid, root, expected_digest='sha256:' + '0' * 64, operator='test')
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource(uid)).state == 'active'
        assert not tx.setting('identity_archived:' + uid)


@pytest.mark.asyncio
async def test_root_is_not_archivable(installed):
    app, _ = installed
    async with app.metadata.transaction(write=False) as tx:
        with pytest.raises(Failure, match='account_not_archivable'):
            await archive_preview(tx, 'u_root')
