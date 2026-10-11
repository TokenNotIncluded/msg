"""Discard retired login grants while preserving native API source checks."""


def migrate_key_identity(conn, *, postgres=False):
    exists = (
        conn.execute("SELECT to_regclass('public.oauth_states')").fetchone()[0] is not None
        if postgres
        else conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='oauth_states'"
        ).fetchone()
        is not None
    )
    if exists:
        conn.execute(
            'INSERT INTO credential_states (id,kind,expires,body) '
            "SELECT id,kind,expires,body FROM oauth_states WHERE kind='api' "
            'ON CONFLICT(id) DO NOTHING'
        )
        conn.execute('DROP TABLE oauth_states')
    conn.execute('DROP TABLE IF EXISTS login_bindings')
