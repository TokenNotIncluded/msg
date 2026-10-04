"""有预算且按当前权限过滤的回复数量投影。"""

from __future__ import annotations

import time

from msg.core.codec import wire
from msg.core.errors import Failure, require
from msg.core.models import HandlerOutput, ResourceRef
from msg.plugins.common import check_access, resolve_read
from msg.plugins.discovery import visible
from msg.plugins.schemas import IDENTIFIER, obj
from msg.storage.post_view_migration import view_count

MAX_IDS = 20
MAX_CANDIDATES = 2048
TIME_BUDGET_SECONDS = 0.25
MAX_PREVIEW_CHARS = 160


def _candidates(tx, buckets, deadline):
    quota = max(1, MAX_CANDIDATES // len(buckets) - 1)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {}, set(buckets)
    placeholders = ','.join('(?,?)' for _ in buckets)
    parameters = tuple(value for bucket in buckets for value in bucket)
    # 每桶保留一个哨兵，防止大量不可读回复把截断后的零误报为精确零。
    query = f"""WITH requested(target_id,kind) AS (VALUES {placeholders})
        SELECT requested.target_id,requested.kind,candidate.id,candidate.revision
        FROM requested CROSS JOIN LATERAL (
            SELECT DISTINCT r.id,r.revision,r.created_at
            FROM relations rel JOIN resources r
                ON r.id=rel.source_id AND r.revision=rel.revision_id
            WHERE rel.target_id=requested.target_id AND rel.type=requested.kind
                AND r.type='post' AND r.state='active' AND r.id<>rel.target_id
            ORDER BY r.created_at DESC,r.id DESC LIMIT ?
        ) candidate ORDER BY requested.target_id,requested.kind,
            candidate.created_at DESC,candidate.id DESC"""
    tx.execute('SAVEPOINT reply_status_candidates')
    try:
        timeout, configured_ms = tx.one(
            "SELECT current_setting('statement_timeout'),setting::integer "
            "FROM pg_settings WHERE name='statement_timeout'"
        )
        timeout_ms = max(1, int(remaining * 1000))
        if configured_ms:
            timeout_ms = min(timeout_ms, configured_ms)
        tx.execute(
            "SELECT set_config('statement_timeout',?,true)",
            (str(timeout_ms) + 'ms',),
        )
        rows = tx.rows(query, (*parameters, quota + 1))
        tx.execute("SELECT set_config('statement_timeout',?,true)", (timeout,))
        tx.execute('RELEASE SAVEPOINT reply_status_candidates')
    except Failure as exc:
        tx.execute('ROLLBACK TO SAVEPOINT reply_status_candidates')
        tx.execute('RELEASE SAVEPOINT reply_status_candidates')
        if exc.code != 'server_busy':
            raise
        return {}, set(buckets)
    grouped = {bucket: [] for bucket in buckets}
    for target, kind, rid, revision in rows:
        grouped[target, kind].append((rid, revision))
    incomplete = {bucket for bucket, rows in grouped.items() if len(rows) > quota}
    return {bucket: rows[:quota] for bucket, rows in grouped.items()}, incomplete


async def reply_status(app, ctx, request, tx):
    deadline = min(ctx.deadline_monotonic, time.monotonic() + TIME_BUDGET_SECONDS)
    include_thread = request.arguments.get('include_thread', False)
    recent_limit = request.arguments.get('recent_limit', 0)
    inputs = []
    checked = {}
    for value in request.arguments['ids']:
        rid = await resolve_read(tx, value)
        if rid in checked:
            continue
        await check_access(app, ctx, request, tx, rid, 'read')
        resource = await tx.resource(rid)
        require(resource.type == 'post' and resource.state == 'active', 'not_a_post')
        checked[rid] = resource
        inputs.append(rid)

    roots = {}
    allowed = dict.fromkeys(inputs, True)
    for rid in inputs if include_thread else ():
        revision = await tx.revision(ResourceRef(id=rid))
        root_id = next(
            (
                relation.target.id
                for relation in revision.relations
                if relation.type == 'thread_root'
            ),
            rid,
        )
        if root_id not in allowed:
            allowed[root_id] = await visible(app, ctx, request, tx, root_id)
        if not allowed[root_id]:
            roots[rid] = None
            continue
        root = checked.get(root_id) or await tx.resource(root_id)
        roots[rid] = (
            ResourceRef(id=root_id, revision=root.revision)
            if root.type == 'post' and root.state == 'active' and root.revision
            else None
        )

    buckets = sorted(
        {(rid, 'reply_to') for rid in inputs}
        | {(root.id, 'thread_root') for root in roots.values() if root}
    )
    candidates, incomplete = _candidates(tx, buckets, deadline)
    counts = dict.fromkeys(buckets, 0)
    recent = {rid: [] for rid in inputs}
    # 此缓存只存在于当前只读事务内，不复用此前请求的授权结果。
    for bucket in buckets:
        for rid, revision_id in candidates.get(bucket, ()):
            if time.monotonic() >= deadline:
                incomplete.add(bucket)
                break
            if rid not in allowed:
                allowed[rid] = await visible(app, ctx, request, tx, rid)
            if not allowed[rid]:
                continue
            counts[bucket] += 1
            target, kind = bucket
            if kind == 'reply_to' and len(recent[target]) < recent_limit:
                preview = {'ref': {'id': rid, 'revision': revision_id}}
                revision = await tx.revision(ResourceRef(id=rid, revision=revision_id))
                if revision.summary:
                    preview['summary'] = revision.summary[:MAX_PREVIEW_CHARS]
                recent[target].append(preview)

    items = []
    for rid in inputs:
        item = {
            'id': rid,
            'reply_count': counts[rid, 'reply_to'],
            'count_complete': (rid, 'reply_to') not in incomplete,
            'view_count': view_count(tx, rid),
        }
        if include_thread:
            root = roots[rid]
            item.update(
                thread_root=wire(root) if root else None,
                thread_count=counts[root.id, 'thread_root'] if root else None,
                thread_complete=bool(root and (root.id, 'thread_root') not in incomplete),
            )
        if recent_limit:
            item['recent'] = recent[rid]
        items.append(item)
    return HandlerOutput(data={'items': items})


def install(app, op):
    @op(
        'discussion.reply_status',
        obj(
            {
                'ids': {'type': 'array', 'items': IDENTIFIER, 'minItems': 1, 'maxItems': MAX_IDS},
                'include_thread': {'type': 'boolean', 'default': False},
                'recent_limit': {'type': 'integer', 'minimum': 0, 'maximum': 3, 'default': 0},
            },
            ('ids',),
        ),
        effect='read',
    )
    async def status(ctx, request, tx):
        return await reply_status(app, ctx, request, tx)
