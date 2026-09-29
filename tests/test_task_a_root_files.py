"""Real disposable Root storage; these tests do not claim physical-console evidence."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from test_root_rotation_resume import old_envelope

from msg.admin.rotation import complete, journal_path, prepare
from msg.core.codec import canonical, loads
from msg.core.errors import Failure
from msg.security.crypto import Ed25519Signer

PIN = 'independent-task-a-test-root-passphrase'


@pytest.mark.asyncio
async def test_prepare_refuses_insecure_root_envelope_without_pending_journal(installed):
    app, root = installed
    path, _ = old_envelope(app, root)
    original = path.read_bytes()
    path.chmod(0o644)
    with pytest.raises(Failure, match='^unsafe_root_private_file$'):
        prepare(app, Ed25519Signer.generate(), PIN, old_signer=root, operator='isolated-test')
    assert path.read_bytes() == original and not journal_path(app).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('unsafe', ['permissions', 'hardlink', 'symlink'])
async def test_history_validation_precedes_database_root_transition(installed, tmp_path, unsafe):
    app, root = installed
    path, _ = old_envelope(app, root)
    old_id = app.certificates.root_certificate.resource_id
    journal = prepare(app, Ed25519Signer.generate(), PIN, old_signer=root, operator='isolated-test')
    history = path.parent / 'history' / old_id / 'key.json'
    history.parent.mkdir(parents=True, mode=0o700)
    history.write_bytes(path.read_bytes())
    history.chmod(0o600)
    if unsafe == 'permissions':
        history.chmod(0o644)
    elif unsafe == 'hardlink':
        import os

        os.link(history, tmp_path / 'duplicate-key')
    else:
        history.unlink()
        history.symlink_to(path)
    async with app.metadata.transaction(write=False) as tx:
        before = tx.one('SELECT COUNT(*) FROM audit')[0]
    expected = 'unsafe_root_private_path' if unsafe == 'symlink' else 'unsafe_root_private_file'
    with pytest.raises(Failure, match='^' + expected + '$'):
        await complete(app, journal, pin=PIN)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.setting('active_root_certificate') == old_id
        assert not await tx.certificate_revoked(old_id)
        assert tx.one('SELECT COUNT(*) FROM audit')[0] == before
    assert journal_path(app).exists()


@pytest.mark.asyncio
async def test_concurrent_prepare_does_not_replace_an_approved_journal(installed):
    app, root = installed
    old_envelope(app, root)

    def attempt(_):
        try:
            return prepare(
                app,
                Ed25519Signer.generate(),
                PIN,
                old_signer=root,
                operator='isolated-concurrent-test',
            )
        except Failure as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    journals = [result for result in results if isinstance(result, dict)]
    assert len(journals) == 1
    assert next(result for result in results if isinstance(result, str)) in {
        'root_rotation_busy',
        'root_rotation_pending',
    }
    assert canonical(loads(journal_path(app).read_bytes())) == canonical(journals[0])
