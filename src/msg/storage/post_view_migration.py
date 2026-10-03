"""Add derived post heat without changing Resource or Revision generations."""

MIGRATION_KEY = 'migration:post_views:v1'


def migrate_post_views(connection, *, postgres=False):
    connection.execute(
        'CREATE TABLE IF NOT EXISTS post_view_days ('
        'resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE, '
        'day TEXT NOT NULL, visitor_digest TEXT NOT NULL, '
        'PRIMARY KEY(resource_id,day,visitor_digest))'
    )
    connection.execute('CREATE INDEX IF NOT EXISTS post_view_days_day ON post_view_days(day)')
    parameter = '%s' if postgres else '?'
    connection.execute(
        f'INSERT INTO settings (key,value) VALUES ({parameter},{parameter}) ON CONFLICT DO NOTHING',
        (MIGRATION_KEY, 'true'),
    )


def view_count(tx, resource_id):
    # Scalar JSON values use the existing reader grant; no new hosting access.
    return tx.setting('post_view_count:' + resource_id, 0)


def record_view(tx, resource_id, day, visitor_digest, browser_digest=None):
    # Both backends serialize writers. Remember the browser as well as the
    # account so signing in or out cannot turn a refresh into another view.
    tx.execute('DELETE FROM post_view_days WHERE day<?', (day,), write=True)
    digests = tuple(dict.fromkeys((visitor_digest, browser_digest or visitor_digest)))
    seen = tx.one(
        'SELECT 1 FROM post_view_days WHERE resource_id=? AND day=? AND visitor_digest IN (?,?)',
        (resource_id, day, digests[0], digests[-1]),
    )
    for value in digests:
        tx.execute(
            'INSERT INTO post_view_days VALUES (?,?,?) ON CONFLICT DO NOTHING',
            (resource_id, day, value),
            write=True,
        )
    if not seen:
        tx.set_setting('post_view_count:' + resource_id, view_count(tx, resource_id) + 1)
    return view_count(tx, resource_id)
