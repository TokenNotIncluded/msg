"""Use real isolated PostgreSQL; local use cases do not simulate a physical console."""
from copy import deepcopy

import pytest

from msg.admin.root import root_envelope
from msg.admin.rotation import prepare, complete, journal_path
from msg.application import Application
from msg.core.codec import canonical, loads
from msg.core.errors import Failure
from msg.security.crypto import Ed25519Signer, seal_private_key, open_private_key
from msg.storage.git import durable_write
from test_service import NOW

OLD_PIN = 'old-protected-envelope-test-passphrase'
NEW_PIN = 'new-protected-envelope-test-passphrase'


def old_envelope(app, root):
    path = root_envelope(app.settings.config_dir)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    durable_write(path, canonical(seal_private_key(root.private_bytes(), OLD_PIN)))
    return path, path.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_at', ['key', 'trust'])
async def test_rotation_resumes_without_archiving_new_key_as_old(installed, monkeypatch, failure_at):
    from msg.admin import rotation
    app, root = installed
    path, original_key = old_envelope(app, root)
    successor = Ed25519Signer.generate()
    journal = prepare(app, successor, NEW_PIN, old_signer=root, operator='isolated-test')
    pending = journal_path(app)
    assert journal['version'] == 2 and pending.parent.stat().st_mode & 0o777 == 0o700
    fail_path = path if failure_at == 'key' else app.settings.trust_file
    real_write = rotation.durable_write
    def interrupted(target, data, **kwargs):
        if target == fail_path:
            raise OSError('injected protected-file interruption')
        return real_write(target, data, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(rotation, 'durable_write', interrupted)
        with pytest.raises(OSError, match='injected'):
            await complete(app, journal, pin=NEW_PIN)
    assert pending.exists()
    async with app.metadata.transaction(write=False) as tx:
        assert tx.setting('active_root_certificate') == journal['new_trust']['certificate']['resource_id']
        assert await tx.certificate_revoked(journal['old_certificate']['resource_id'])
        before = tx.one('SELECT COUNT(*) FROM audit')[0]
    # Resume must work before Application.load can validate partially switched trust files.
    fresh = Application(app.settings, clock=lambda:NOW)
    try:
        result = await complete(fresh, loads(pending.read_bytes()), pin=NEW_PIN)
        assert result['restart_required'] and not pending.exists()
        assert root_envelope(app.settings.config_dir) == path
        assert open_private_key(loads(path.read_bytes()), NEW_PIN) == successor.private_bytes()
        history = path.parent/'history'/journal['old_certificate']['resource_id']/'key.json'
        assert history.read_bytes() == original_key
        assert open_private_key(loads(history.read_bytes()), OLD_PIN) == root.private_bytes()
        assert path.stat().st_mode & 0o777 == history.stat().st_mode & 0o777 == 0o600
        assert history.parent.stat().st_mode & 0o777 == 0o700
        assert not (app.settings.config_dir/'root').exists()
        async with fresh.metadata.transaction(write=False) as tx:
            assert tx.one('SELECT COUNT(*) FROM audit')[0] == before
        await fresh.load()
        assert fresh.certificates.root_public_key == successor.public_key
    finally:
        await fresh.close()


@pytest.mark.asyncio
async def test_replacement_csr_is_rejected_before_any_commit(installed):
    app, root = installed
    path, original = old_envelope(app, root)
    journal = prepare(app, Ed25519Signer.generate(), NEW_PIN, old_signer=root, operator='isolated-test')
    changed = deepcopy(journal)
    changed['online_csr']['resource_id'] = 'csr_replaced_after_confirmation'
    with pytest.raises(Failure, match='invalid_signature'):
        await complete(app, changed, pin=NEW_PIN)
    assert path.read_bytes() == original and journal_path(app).exists()
    async with app.metadata.transaction(write=False) as tx:
        assert tx.setting('active_root_certificate') == journal['old_certificate']['resource_id']
        assert not await tx.certificate_revoked(journal['old_certificate']['resource_id'])


@pytest.mark.asyncio
async def test_explicit_lost_root_key_rotation_uses_bound_journal(installed):
    app, root = installed
    path, original = old_envelope(app, root)
    successor = Ed25519Signer.generate()
    journal = prepare(app, successor, NEW_PIN, old_signer=None, operator='isolated-lost-key-test')
    assert journal['statement']['previous_key_proved'] is False and journal['old_signature'] is None
    await complete(app, journal, pin=NEW_PIN)
    assert open_private_key(loads(path.read_bytes()), NEW_PIN) == successor.private_bytes()
    assert (path.parent/'history'/journal['old_certificate']['resource_id']/'key.json').read_bytes() == original
