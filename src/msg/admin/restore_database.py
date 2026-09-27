"""Import an offline dump and its quarantine gate in the same PostgreSQL commit."""
from pathlib import Path
import os
import subprocess

from msg.core.codec import canonical
from msg.security.quarantine import SETTING


def restore_dump(dump, *, safe_dsn, env, quarantine):
    """Stream through a protected SQL file; COPY data requires psql, not execute().

    pg_restore does not connect to PostgreSQL here. psql's single transaction
    contains both the dump and the mandatory gate. An interruption before commit
    leaves neither an imported snapshot nor a runnable ungated database.
    """
    script = Path(dump).parent / 'quarantined-restore.sql'
    script.touch(mode=0o600, exist_ok=False)
    try:
        subprocess.run(['pg_restore', '--exit-on-error', '--no-owner', '--no-acl',
                        '--file', str(script), str(dump)],
                       env=env, check=True, capture_output=True)
        # Values are fixed recovery metadata, not secrets. Escape as a SQL
        # literal (including backslashes) independently of session string flags.
        value = canonical(quarantine).decode().replace('\\', '\\\\').replace("'", "''")
        gate = (f"\nINSERT INTO settings(key,value) VALUES('{SETTING}',E'{value}') "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value;\n")
        with script.open('ab') as stream:
            stream.write(gate.encode())
            stream.flush()
            os.fsync(stream.fileno())
        subprocess.run(['psql', '--no-psqlrc', '--quiet', '--single-transaction',
                        '--set', 'ON_ERROR_STOP=1', '--dbname', safe_dsn, '--file', str(script)],
                       env=env, check=True, capture_output=True)
    finally:
        script.unlink(missing_ok=True)
