"""A post-import filesystem failure cannot expose restored snapshot authority."""
from pathlib import Path

import psycopg
import pytest

from msg.admin import backups
from msg.core.codec import loads


@pytest.mark.asyncio
async def test_restore_move_failure_keeps_committed_database_quarantined(installed, tmp_path, pg_dsn, monkeypatch):
    app, _ = installed
    archive = tmp_path / 'physical-failure.zip'
    await backups.backup(app, archive)
    config = tmp_path / 'restore-etc'
    data = tmp_path / 'restore-data'
    original_move = backups.shutil.move
    moves = []
    def interrupted_move(source, destination, *args, **kwargs):
        moves.append(Path(source).name)
        if Path(source).name == 'blobs':
            raise OSError('injected restore blob move failure')
        return original_move(source, destination, *args, **kwargs)
    monkeypatch.setattr(backups.shutil, 'move', interrupted_move)
    with pytest.raises(OSError, match='injected restore blob move failure'):
        backups.restore(archive, config, data, postgres_dsn=pg_dsn)
    assert moves == ['content', 'repositories', 'blobs']
    assert (data / 'git' / 'content' / 'private.git').is_dir()
    assert not (data / 'blobs' / 'sha256').exists()
    marker = loads((config / 'recovery-drill.json').read_bytes())
    assert marker['outbound_enabled'] is False
    assert marker['revocation_replay'] == 'required'
    # The import has genuinely committed, yet its independent database gate is
    # already active. Losing the config marker cannot promote this partial restore.
    with psycopg.connect(pg_dsn) as connection:
        assert connection.execute('SELECT COUNT(*) FROM resources').fetchone()[0] > 0
        values = dict(connection.execute(
            "SELECT key,value FROM settings WHERE key IN ('recovery_quarantine','runtime_config')"))
    quarantine = loads(values['recovery_quarantine'])
    runtime = loads(values['runtime_config'])
    assert quarantine['authority'] == 'health_only'
    assert quarantine['outbound_enabled'] is False
    assert quarantine['revocation_replay'] == 'required'
    assert runtime['accept_writes'] is False and runtime['cleanup_enabled'] is False
    assert not (config / 'root').exists()
