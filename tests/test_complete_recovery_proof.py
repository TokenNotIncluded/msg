"""Disposable complete-state proofs; never real console approval or external freshness."""
from copy import deepcopy

import pytest

from msg.admin.recovery_proof import capture, promote, IndependentRecoveryPin, verify_proof
from msg.core.codec import canonical, digest
from msg.core.errors import Failure
from msg.security.quarantine import SETTING, active


@pytest.fixture
async def complete_state(installed):
    app, root = installed
    packet = await capture(app, root, source_backup_sha256='a'*64, sequence=1)
    pin = IndependentRecoveryPin(service=app.settings.service_url, public_key=root.public_key,
        digest=digest(packet['state']), sequence=1, source_backup_sha256='a'*64)
    gate = {'format': 'msg-recovery-quarantine-v1', 'source_backup_sha256': 'a'*64,
            'outbound_enabled': False, 'authority': 'health_only'}
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting(SETTING, gate)
        runtime = dict(tx.setting('runtime_config', {}), accept_writes=False, cleanup_enabled=False)
        tx.set_setting('runtime_config', runtime)
    (app.settings.config_dir/'recovery-drill.json').write_bytes(canonical(gate))
    (app.settings.config_dir/'recovery-drill.json').chmod(0o600)
    app.executor.recovery_quarantined = True
    return app, root, packet, pin


async def test_complete_proof_accepts_exact_disposable_state(complete_state):
    app, root, packet, pin = complete_state
    result = await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture')
    assert result['status'] == 'recovery_promoted' and result['requires_runtime_restart']
    async with app.metadata.transaction(write=False) as tx:
        assert not active(tx)
        assert tx.setting('recovery_runtime_generation') == result['runtime_generation']
        assert tx.setting('recovery_promotion')['receipt']['manifest_digest'] == pin.digest
    assert not (app.settings.config_dir/'recovery-drill.json').exists()
    assert app.executor.recovery_drill_active()  # Existing runtime stays closed.


@pytest.mark.parametrize('change', ['extra_setting', 'extra_table', 'extra_column', 'wrong_pin', 'partial'])
async def test_complete_proof_rejects_unaccounted_state(complete_state, change):
    app, root, packet, pin = complete_state
    if change in {'extra_setting', 'extra_table', 'extra_column'}:
        async with app.metadata.transaction(write=True) as tx:
            if change == 'extra_setting':
                tx.set_setting('recovery_promotion_attacker', {'authority': True})
            elif change == 'extra_table':
                tx.execute('CREATE TABLE hidden_authority (id TEXT)', write=True)
            else:
                tx.execute('ALTER TABLE resources ADD COLUMN hidden_authority TEXT', write=True)
    elif change == 'wrong_pin':
        from dataclasses import replace
        pin = replace(pin, digest='sha256:'+'0'*64)
    else:
        packet = deepcopy(packet)
        packet['state']['metadata']['tables'].pop('credentials')
    with pytest.raises(Failure):
        await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture')
    async with app.metadata.transaction(write=False) as tx:
        assert active(tx) and tx.setting('recovery_promotion') is None
    assert (app.settings.config_dir/'recovery-drill.json').exists()


async def test_marker_failure_requires_full_reverification(complete_state, monkeypatch):
    from pathlib import Path
    app, root, packet, pin = complete_state
    marker = app.settings.config_dir/'recovery-drill.json'
    unlink = Path.unlink
    def fail_marker(path, *args, **kwargs):
        if path == marker:
            raise OSError('fixture interrupted finalization')
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail_marker)
        with pytest.raises(Failure, match='recovery_promotion_finish_required'):
            await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture')
    assert marker.exists() and app.executor.recovery_drill_active()
    async with app.metadata.transaction(write=True) as tx:
        assert active(tx)
        tx.set_setting('extra_after_promotion', True)
    with pytest.raises(Failure, match='recovery_complete_state_mismatch'):
        await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture')
    assert marker.exists()
    async with app.metadata.transaction(write=True) as tx:
        tx.execute("DELETE FROM settings WHERE key='extra_after_promotion'", write=True)
    result = await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture')
    assert result['status'] == 'recovery_promoted' and not marker.exists()


async def test_transaction_failure_does_not_consume_audit_sequence(complete_state, monkeypatch):
    from msg.storage.session import RelationalSession
    app, root, packet, pin = complete_state
    original = RelationalSession.set_setting
    def fail_receipt(tx, key, value):
        if key == 'recovery_promotion':
            raise RuntimeError('fixture transaction failure')
        return original(tx, key, value)
    with monkeypatch.context() as patch:
        patch.setattr(RelationalSession, 'set_setting', fail_receipt)
        with pytest.raises(RuntimeError, match='fixture transaction failure'):
            await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture')
    async with app.metadata.transaction(write=False) as tx:
        assert active(tx) and tx.setting('recovery_promotion') is None
        assert list(tx.one('SELECT last_value,is_called FROM audit_seq_seq')) == packet['state']['metadata']['sequences']['audit_seq_seq']
    assert (await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture'))['status'] == 'recovery_promoted'


async def test_valid_signature_cannot_omit_authority_inventory(complete_state):
    from dataclasses import replace
    from msg.admin.recovery_proof import PURPOSE
    from msg.core.codec import wire
    app, root, packet, pin = complete_state
    packet = deepcopy(packet)
    packet['state']['metadata']['tables'].pop('credentials')
    packet['signature'] = wire(root.sign(canonical(packet['state']), purpose=PURPOSE))
    pin = replace(pin, digest=digest(packet['state']))
    with pytest.raises(Failure, match='recovery_complete_state_mismatch'):
        await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture')


async def test_source_changes_after_confirmation_refuse_seal(installed):
    from msg.admin.recovery_proof import draft, seal
    app, root = installed
    body = await draft(app, public_key=root.public_key, source_backup_sha256='a'*64, sequence=1)
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('new_authority', 'changed')
    with pytest.raises(Failure, match='recovery_complete_state_mismatch'):
        await seal(app, body, root)


@pytest.mark.parametrize('action', ['sign', 'promote'])
def test_proof_cli_refuses_nonconsole_before_reading_files(action, tmp_path):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    args = ['a'*64, '1', str(tmp_path/'output.json')] if action == 'sign' else [str(tmp_path/'missing.json'), str(tmp_path/'trust.json')]
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
    for key in ('SSH_CONNECTION', 'SSH_CLIENT', 'SSH_TTY'):
        env.pop(key, None)
    result = subprocess.run([str(Path(sys.executable).with_name('msgd')), '--config-dir', str(tmp_path), 'root', 'recovery-proof', action, *args], env=env, capture_output=True, text=True)
    expected = 'local_os_administrator_required' if os.geteuid() else 'local_console_required'
    assert result.returncode == 1 and json.loads(result.stderr) == {'status': 'error', 'error': {'code': expected}}
    assert not result.stdout and not (tmp_path/'output.json').exists()


async def test_complete_proof_from_real_backup_restores_exact_inventory(installed, tmp_path, pg_dsn):
    from msg.admin.backups import backup, restore
    from msg.admin.recovery_proof import open_for_proof
    from test_service import call, register
    app, root = installed
    key, subject, _ = await register(app, 'complete-backup')
    created = await call(app, 'content.post_create', {'parent': '/main', 'body': 'committed recovery bytes'}, key=key, subject=subject)
    assert created.status == 'ok'
    archive = tmp_path/'complete.zip'
    saved = await backup(app, archive)
    packet = await capture(app, root, source_backup_sha256=saved['sha256'], sequence=3)
    pin = IndependentRecoveryPin(service=app.settings.service_url, public_key=root.public_key,
        digest=digest(packet['state']), sequence=3, source_backup_sha256=saved['sha256'])
    restore(archive, tmp_path/'complete-etc', tmp_path/'complete-data', postgres_dsn=pg_dsn)
    restored = await open_for_proof(tmp_path/'complete-etc')
    restored.clock = app.clock
    try:
        assert (await promote(restored, packet, pin=pin, signer=root, operator='isolated-fixture'))['status'] == 'recovery_promoted'
    finally:
        await restored.close()


@pytest.mark.parametrize('ddl', [
    'CREATE SCHEMA pga_hidden',
    'CREATE VIEW hidden_view AS SELECT id FROM resources',
    'CREATE INDEX hidden_idx ON resources(owner)',
])
async def test_unknown_schema_objects_reject_promotion(complete_state, ddl):
    app, root, packet, pin = complete_state
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(ddl, write=True)
    with pytest.raises(Failure, match='recovery_schema_'):
        await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture')
    assert (app.settings.config_dir/'recovery-drill.json').exists()


async def test_directory_sync_failure_keeps_database_closed_and_can_resume(complete_state, monkeypatch):
    import os
    app, root, packet, pin = complete_state
    def fail_sync(fd):
        raise OSError('fixture directory sync failure')
    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', fail_sync)
        with pytest.raises(Failure, match='recovery_promotion_finish_required'):
            await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture')
    assert not (app.settings.config_dir/'recovery-drill.json').exists()
    async with app.metadata.transaction(write=False) as tx:
        assert active(tx) and tx.setting('recovery_promotion') is not None
    result = await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture')
    assert result['status'] == 'recovery_promoted'
    async with app.metadata.transaction(write=False) as tx:
        assert not active(tx)
