"""Disposable complete-state proofs; never real console approval or external freshness."""

from copy import deepcopy

import pytest

from msg.admin.recovery_proof import IndependentRecoveryPin, capture, promote
from msg.core.codec import canonical, digest
from msg.core.errors import Failure
from msg.security.quarantine import SETTING, active


@pytest.fixture
async def complete_state(installed):
    app, root = installed
    packet = await capture(app, root, source_backup_sha256='a' * 64, sequence=1)
    pin = IndependentRecoveryPin(
        service=app.settings.service_url,
        public_key=root.public_key,
        digest=digest(packet['state']),
        sequence=1,
        source_backup_sha256='a' * 64,
    )
    gate = {
        'format': 'msg-recovery-quarantine-v1',
        'source_backup_sha256': 'a' * 64,
        'outbound_enabled': False,
        'authority': 'health_only',
    }
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting(SETTING, gate)
        runtime = dict(tx.setting('runtime_config', {}), accept_writes=False, cleanup_enabled=False)
        tx.set_setting('runtime_config', runtime)
    (app.settings.config_dir / 'recovery-drill.json').write_bytes(canonical(gate))
    (app.settings.config_dir / 'recovery-drill.json').chmod(0o600)
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
    assert not (app.settings.config_dir / 'recovery-drill.json').exists()
    assert app.executor.recovery_drill_active()  # Existing runtime stays closed.


@pytest.mark.parametrize(
    'change',
    [
        'extra_setting',
        'extra_table',
        'extra_column',
        'oauth_state',
        'money_visibility',
        'wrong_pin',
        'partial',
    ],
)
async def test_complete_proof_rejects_unaccounted_state(complete_state, change):
    app, root, packet, pin = complete_state
    if change in {
        'extra_setting',
        'extra_table',
        'extra_column',
        'oauth_state',
        'money_visibility',
    }:
        async with app.metadata.transaction(write=True) as tx:
            if change == 'extra_setting':
                tx.set_setting('recovery_promotion_attacker', {'authority': True})
            elif change == 'extra_table':
                tx.execute('CREATE TABLE hidden_authority (id TEXT)', write=True)
            elif change == 'extra_column':
                tx.execute('ALTER TABLE resources ADD COLUMN hidden_authority TEXT', write=True)
            elif change == 'money_visibility':
                tx.execute(
                    'INSERT INTO money_visibility VALUES (?,?,?)',
                    ('u_root', 'public', '2026-09-27T00:00:00Z'),
                    write=True,
                )
            else:
                tx.execute(
                    'INSERT INTO oauth_states VALUES (?,?,?,?)',
                    (
                        'unaccounted-oauth-session',
                        'session',
                        '2999-01-01T00:00:00Z',
                        canonical({'subject': 's_unaccounted'}).decode(),
                    ),
                    write=True,
                )
    elif change == 'wrong_pin':
        from dataclasses import replace

        pin = replace(pin, digest='sha256:' + '0' * 64)
    else:
        packet = deepcopy(packet)
        packet['state']['metadata']['tables'].pop('credentials')
    with pytest.raises(Failure):
        await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture')
    async with app.metadata.transaction(write=False) as tx:
        assert active(tx) and tx.setting('recovery_promotion') is None
    assert (app.settings.config_dir / 'recovery-drill.json').exists()


async def test_marker_failure_requires_full_reverification(complete_state, monkeypatch):
    from pathlib import Path

    app, root, packet, pin = complete_state
    marker = app.settings.config_dir / 'recovery-drill.json'
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
        assert (
            list(tx.one('SELECT last_value,is_called FROM audit_seq_seq'))
            == packet['state']['metadata']['sequences']['audit_seq_seq']
        )
    assert (await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture'))[
        'status'
    ] == 'recovery_promoted'


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
    body = await draft(app, public_key=root.public_key, source_backup_sha256='a' * 64, sequence=1)
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('new_authority', 'changed')
    with pytest.raises(Failure, match='recovery_complete_state_mismatch'):
        await seal(app, body, root)


@pytest.mark.parametrize('action', ['sign', 'promote'])
def test_proof_cli_refuses_nonconsole_before_reading_files(action, tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    args = (
        ['a' * 64, '1', str(tmp_path / 'output.json')]
        if action == 'sign'
        else [str(tmp_path / 'missing.json'), str(tmp_path / 'trust.json')]
    )
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    for key in ('SSH_CONNECTION', 'SSH_CLIENT', 'SSH_TTY'):
        env.pop(key, None)
    result = subprocess.run(
        [
            str(Path(sys.executable).with_name('msgd')),
            '--config-dir',
            str(tmp_path),
            'root',
            'recovery-proof',
            action,
            *args,
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    expected = 'local_os_administrator_required' if os.geteuid() else 'local_console_required'
    assert result.returncode == 1 and json.loads(result.stderr) == {
        'status': 'error',
        'error': {'code': expected},
    }
    assert not result.stdout and not (tmp_path / 'output.json').exists()


async def test_complete_proof_from_real_backup_restores_exact_inventory(
    installed, tmp_path, pg_dsn
):
    from test_service import call, register

    from msg.admin.backups import backup, restore
    from msg.admin.recovery_proof import open_for_proof

    app, root = installed
    key, subject, _ = await register(app, 'complete-backup')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'committed recovery bytes'},
        key=key,
        subject=subject,
    )
    assert created.status == 'ok'
    published = await call(
        app,
        'money.visibility_set',
        {'visibility': 'public'},
        key=key,
        subject=subject,
    )
    assert published.status == 'ok'
    _, followed_subject, _ = await register(app, 'complete-followed')
    followed = await call(
        app, 'communication.follow', {'id': followed_subject}, key=key, subject=subject
    )
    assert followed.status == 'ok'
    async with app.metadata.transaction(write=False) as tx:
        follow_rows = tx.rows('SELECT follower,target,created_at FROM agent_follows')
    assert len(follow_rows) == 1 and follow_rows[0][:2] == (subject, followed_subject)
    archive = tmp_path / 'complete.zip'
    saved = await backup(app, archive)
    packet = await capture(app, root, source_backup_sha256=saved['sha256'], sequence=3)
    pin = IndependentRecoveryPin(
        service=app.settings.service_url,
        public_key=root.public_key,
        digest=digest(packet['state']),
        sequence=3,
        source_backup_sha256=saved['sha256'],
    )
    restore(archive, tmp_path / 'complete-etc', tmp_path / 'complete-data', postgres_dsn=pg_dsn)
    restored = await open_for_proof(tmp_path / 'complete-etc')
    restored.clock = app.clock
    try:
        async with restored.metadata.transaction(write=False) as tx:
            assert (
                tx.one('SELECT visibility FROM money_visibility WHERE subject_id=?', (subject,))[0]
                == 'public'
            )
            assert tx.rows('SELECT follower,target,created_at FROM agent_follows') == follow_rows
        assert (await promote(restored, packet, pin=pin, signer=root, operator='isolated-fixture'))[
            'status'
        ] == 'recovery_promoted'
    finally:
        await restored.close()


@pytest.mark.parametrize(
    'ddl',
    [
        'CREATE SCHEMA pga_hidden',
        'CREATE VIEW hidden_view AS SELECT id FROM resources',
        'CREATE INDEX hidden_idx ON resources(owner)',
        'ALTER TABLE resources ENABLE ROW LEVEL SECURITY',
        'CREATE POLICY hidden_policy ON resources USING (true)',
        'CREATE RULE hidden_rule AS ON DELETE TO resources DO INSTEAD NOTHING',
        'ALTER TABLE resources DISABLE TRIGGER ALL',
    ],
)
async def test_unknown_schema_objects_reject_promotion(complete_state, ddl):
    app, root, packet, pin = complete_state
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(ddl, write=True)
    with pytest.raises(Failure, match='recovery_schema_'):
        await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture')
    assert (app.settings.config_dir / 'recovery-drill.json').exists()


async def test_directory_sync_failure_keeps_database_closed_and_can_resume(
    complete_state, monkeypatch
):
    import os

    app, root, packet, pin = complete_state

    def fail_sync(fd):
        raise OSError('fixture directory sync failure')

    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', fail_sync)
        with pytest.raises(Failure, match='recovery_promotion_finish_required'):
            await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture')
    assert not (app.settings.config_dir / 'recovery-drill.json').exists()
    async with app.metadata.transaction(write=False) as tx:
        assert active(tx) and tx.setting('recovery_promotion') is not None
    result = await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture')
    assert result['status'] == 'recovery_promoted'
    async with app.metadata.transaction(write=False) as tx:
        assert not active(tx)


async def test_configuration_policy_drift_cannot_bypass_full_database_proof(complete_state):
    app, root, packet, pin = complete_state
    config = app.settings.config_dir / 'msgd.toml'
    content = config.read_text()
    assert '[server]' in content
    config.write_text(content.replace('[server]', '[server]\ntransfer_ttl = 12345', 1))
    with pytest.raises(Failure, match='recovery_cached_configuration_mismatch'):
        await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture')
    async with app.metadata.transaction(write=False) as tx:
        assert active(tx)


async def test_resume_requires_committed_generation_fence(complete_state, monkeypatch):
    import os

    app, root, packet, pin = complete_state
    with monkeypatch.context() as patch:

        def fail_sync(fd):
            raise OSError('fixture interrupted finalization')

        patch.setattr(os, 'fsync', fail_sync)
        with pytest.raises(Failure, match='recovery_promotion_finish_required'):
            await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture')
    async with app.metadata.transaction(write=True) as tx:
        tx.execute("DELETE FROM settings WHERE key='recovery_runtime_generation'", write=True)
    with pytest.raises(Failure, match='recovery_promotion_receipt_mismatch'):
        await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture')
    async with app.metadata.transaction(write=False) as tx:
        assert active(tx)


async def test_subsequent_complete_recovery_does_not_resume_historical_receipt(complete_state):
    app, root, packet, pin = complete_state
    await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture')
    packet = await capture(app, root, source_backup_sha256='b' * 64, sequence=2)
    pin = IndependentRecoveryPin(
        service=app.settings.service_url,
        public_key=root.public_key,
        digest=digest(packet['state']),
        sequence=2,
        source_backup_sha256='b' * 64,
    )
    gate = {
        'format': 'msg-recovery-quarantine-v1',
        'source_backup_sha256': 'b' * 64,
        'outbound_enabled': False,
        'authority': 'health_only',
    }
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting(SETTING, gate)
        tx.set_setting(
            'runtime_config',
            dict(tx.setting('runtime_config', {}), accept_writes=False, cleanup_enabled=False),
        )
    marker = app.settings.config_dir / 'recovery-drill.json'
    marker.write_bytes(canonical(gate))
    marker.chmod(0o600)
    assert (await promote(app, packet, pin=pin, signer=root, operator='isolated-fixture'))[
        'status'
    ] == 'recovery_promoted'


def test_schema_vocabulary_preserves_nullability_across_pg_catalog_versions():
    from msg.admin.recovery_state import shape

    pg16 = {
        'tables': {'sample': [['owner', 'text', True, '', None]]},
        'constraints': [],
        'indexes': [],
        'triggers': [],
        'functions': [],
        'sequences': [],
    }
    pg18 = deepcopy(pg16)
    pg18['constraints'] = [['sample', 'sample_owner_not_null', 'n', 'NOT NULL owner', True]]
    assert shape(pg16) == shape(pg18)
    pg18['tables']['sample'][0][2] = False
    assert shape(pg16) != shape(pg18)
