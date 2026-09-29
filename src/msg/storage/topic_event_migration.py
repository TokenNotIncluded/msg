"""Backfill a rebuildable topic index once, without duplicating signed bodies."""

MIGRATION_KEY = 'migration:topic_event_projection:v1'
EVENT_TYPES = (
    'topic.create',
    'topic.member.join',
    'topic.member.request',
    'topic.member.leave',
    'topic.member.invite',
    'topic.member.approve',
    'topic.member.remove',
    'topic.member.promote',
    'topic.member.demote',
    'topic.member.ban',
    'topic.member.unban',
    'topic.policy.change',
    'topic.archive',
    'topic.restore',
    'topic.move',
    'topic.chmod',
    'topic.chgrp',
    'topic.chown',
    'topic.configure',
)


def migrate_topic_events(connection, *, postgres=False):
    """Caller holds the schema writer lock; commit index and marker together."""
    parameter = '%s' if postgres else '?'
    if connection.execute(
        f'SELECT 1 FROM settings WHERE key={parameter}', (MIGRATION_KEY,)
    ).fetchone():
        return
    topic = (
        "body::jsonb->'data'->>'topic_id'" if postgres else "json_extract(body,'$.data.topic_id')"
    )
    kind = "body::jsonb->>'type'" if postgres else "json_extract(body,'$.type')"
    placeholders = ','.join([parameter] * len(EVENT_TYPES))
    connection.execute(
        f'INSERT INTO topic_event_projection (seq,topic) '
        f'SELECT seq,{topic} FROM events WHERE {kind} IN ({placeholders}) '
        f'AND {topic} IS NOT NULL ON CONFLICT DO NOTHING',
        EVENT_TYPES,
    )
    connection.execute(
        f'INSERT INTO settings (key,value) VALUES ({parameter},{parameter})',
        (MIGRATION_KEY, 'true'),
    )
