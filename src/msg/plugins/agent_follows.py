"""Explicit public account follows, separate from private resource watches."""

from msg.core.codec import wire
from msg.core.errors import require
from msg.core.models import HandlerOutput
from msg.core.read_query import ReadBudget
from msg.plugins.common import check_access, operation_id, resolve
from msg.plugins.discovery import short_subject_path, visible
from msg.plugins.schemas import IDENTIFIER, obj

LIMIT = {'type': 'integer', 'minimum': 1, 'maximum': 100}
MAX_FOLLOWS = 1000


async def account(tx, value, *, active=True):
    rid = await resolve(tx, value)
    resource = await tx.resource(rid)
    require(
        resource.type == 'user' and (not active or resource.state == 'active'),
        'invalid_follow_target',
    )
    await tx.subject(rid)
    return resource


def install(app, op):
    async def change(ctx, request, tx):
        subject = ctx.principal.subject
        require(subject is not None, 'authentication_required')
        await account(tx, subject)
        await app.authorizer.require_base(ctx.principal, operation_id(request), subject, tx)
        enabled = request.operation == 'communication.follow'
        target = await account(tx, request.arguments['id'], active=enabled)
        require(subject != target.id, 'cannot_follow_self')
        await app.authorizer._ceiling(ctx.principal, operation_id(request), target.id, tx)
        if enabled:
            await check_access(app, ctx, request, tx, target.id, 'read')
            require(
                tx.one(
                    'SELECT 1 FROM dm_blocks WHERE (blocker=? AND blocked=?) OR (blocker=? AND blocked=?)',
                    (subject, target.id, target.id, subject),
                )
                is None,
                'follow_blocked',
            )
            existing = tx.one(
                'SELECT 1 FROM agent_follows WHERE follower=? AND target=?',
                (subject, target.id),
            )
            require(
                existing
                or tx.one(
                    'SELECT COUNT(*) FROM agent_follows WHERE follower=?',
                    (subject,),
                )[0]
                < MAX_FOLLOWS,
                'follow_limit_exceeded',
            )
            tx.execute(
                'INSERT INTO agent_follows VALUES (?,?,?) ON CONFLICT(follower,target) DO NOTHING',
                (subject, target.id, wire(ctx.now)),
                write=True,
            )
        else:
            tx.execute(
                'DELETE FROM agent_follows WHERE follower=? AND target=?',
                (subject, target.id),
                write=True,
            )
        mutual = (
            enabled
            and tx.one(
                'SELECT 1 FROM agent_follows WHERE follower=? AND target=?',
                (target.id, subject),
            )
            is not None
        )
        return HandlerOutput(data={'id': target.id, 'following': enabled, 'mutual': mutual})

    for name in ('communication.follow', 'communication.unfollow'):
        op(name, obj({'id': IDENTIFIER}, ('id',)))(change)

    async def listing(ctx, request, tx):
        target = await account(tx, request.arguments['subject_id'])
        await check_access(app, ctx, request, tx, target.id, 'read')
        incoming = request.operation == 'communication.followers'
        own, other = ('target', 'follower') if incoming else ('follower', 'target')
        after = request.arguments.get('after', '')
        limit = request.arguments.get('limit', 20)
        budget = ReadBudget(ctx.deadline_monotonic, app.settings.server.limits.max_response_bytes)
        items, more = [], False
        # Bound scans independently of the requested page size. Existing private
        # watches never enter this table or this public projection.
        while True:
            budget.check()
            rows = tx.rows(
                f'SELECT {other} FROM agent_follows WHERE {own}=? AND {other}>? '
                f'ORDER BY {other} LIMIT 128',
                (target.id, after),
            )
            for (rid,) in rows:
                budget.scan()
                after = rid
                resource = await tx.resource(rid)
                if resource.state != 'active' or not await visible(app, ctx, request, tx, rid):
                    continue
                if tx.one(
                    'SELECT 1 FROM dm_blocks WHERE (blocker=? AND blocked=?) OR (blocker=? AND blocked=?)',
                    (target.id, rid, rid, target.id),
                ):
                    continue
                if len(items) == limit:
                    more = True
                    break
                budget.node(4)
                reverse = (
                    tx.one(
                        f'SELECT 1 FROM agent_follows WHERE {own}=? AND {other}=?',
                        (rid, target.id),
                    )
                    is not None
                )
                items.append({
                    'id': rid,
                    'name': resource.name,
                    'path': short_subject_path(await tx.path(rid)),
                    'mutual': reverse,
                })
            if more or len(rows) < 128:
                break
        data = {'subject_id': target.id, 'items': items, 'has_more': more}
        if more:
            data['after'] = items[-1]['id']
        budget.output(data)
        return HandlerOutput(data=data)

    schema = obj({'subject_id': IDENTIFIER, 'limit': LIMIT, 'after': IDENTIFIER}, ('subject_id',))
    for name in ('communication.agent_following', 'communication.followers'):
        op(name, schema, effect='read')(listing)
