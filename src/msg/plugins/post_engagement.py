"""Private account bookmarks and ACL-checked post engagement projections."""

from msg.core.codec import canonical, wire
from msg.core.errors import require
from msg.core.models import HandlerOutput
from msg.plugins.common import check_access, operation_id, resolve
from msg.plugins.discovery import metadata, visible
from msg.plugins.schemas import IDENTIFIER, STRING, obj


def install(app, op):
    async def bookmark(ctx, request, tx):
        require(ctx.principal.subject is not None, 'authentication_required')
        rid = await resolve(tx, request.arguments['id'])
        resource = await tx.resource(rid)
        require(resource.type == 'post' and resource.state == 'active', 'not_a_post')
        await check_access(app, ctx, request, tx, rid, 'read')
        from msg.plugins.discussion import gates

        await gates(app, ctx, request, tx, rid)
        enabled = request.operation == 'discussion.bookmark'
        if enabled:
            from msg.storage.capacity import require_reaction_capacity

            if not tx.one(
                "SELECT 1 FROM reactions WHERE subject=? AND resource=? AND kind='bookmark'",
                (ctx.principal.subject, rid),
            ):
                require_reaction_capacity(tx)
            tx.execute(
                'INSERT INTO reactions VALUES (?,?,?,?,?) ON CONFLICT(subject,resource,kind,revision) DO NOTHING',
                (
                    ctx.principal.subject,
                    rid,
                    'bookmark',
                    '',
                    canonical({'time': wire(ctx.now)}).decode(),
                ),
                write=True,
            )
        else:
            tx.execute(
                "DELETE FROM reactions WHERE subject=? AND resource=? AND kind='bookmark'",
                (ctx.principal.subject, rid),
                write=True,
            )
        return HandlerOutput(data={'bookmarked': enabled})

    for name in ('discussion.bookmark', 'discussion.unbookmark'):
        op(name, obj({'id': IDENTIFIER}, ('id',)))(bookmark)

    @op('discussion.state', obj({'id': IDENTIFIER, 'revision': IDENTIFIER}, ('id',)), effect='read')
    async def state(ctx, request, tx):
        rid = await resolve(tx, request.arguments['id'])
        resource = await tx.resource(rid)
        require(resource.type == 'post' and resource.state == 'active', 'not_a_post')
        await check_access(app, ctx, request, tx, rid, 'read')
        subject = ctx.principal.subject
        mine = (
            set()
            if not subject
            else {
                r[0]
                for r in tx.rows(
                    'SELECT kind FROM reactions WHERE subject=? AND resource=?', (subject, rid)
                )
            }
        )
        following = bool(
            subject
            and tx.one(
                'SELECT 1 FROM agent_follows WHERE follower=? AND target=?',
                (subject, resource.owner),
            )
        )
        from msg.core.models import ResourceRef
        from msg.plugins.post_proofs import projection

        revision = await tx.revision(
            ResourceRef(id=rid, revision=request.arguments.get('revision'))
        )
        proofs = await projection(tx, rid, revision.id, subject)
        return HandlerOutput(
            data={
                **proofs,
                'likes': tx.one(
                    "SELECT COUNT(*) FROM reactions WHERE resource=? AND kind='like'", (rid,)
                )[0],
                'liked': 'like' in mine,
                'bookmarked': 'bookmark' in mine,
                'following': following,
            }
        )

    @op('discussion.bookmarks', obj({'after': STRING}), effect='read')
    async def bookmarks(ctx, request, tx):
        require(ctx.principal.subject is not None, 'authentication_required')
        await app.authorizer.require_base(
            ctx.principal, operation_id(request), ctx.principal.subject, tx
        )
        after = request.arguments.get('after', '')
        rows = tx.rows(
            "SELECT resource FROM reactions WHERE subject=? AND kind='bookmark' AND resource>? ORDER BY resource LIMIT 101",
            (ctx.principal.subject, after),
        )
        items = []
        for (rid,) in rows[:100]:
            resource = await tx.resource(rid)
            if resource.state == 'active' and await visible(app, ctx, request, tx, rid):
                info = await metadata(tx, resource)
                items.append({key: info[key] for key in ('id', 'name', 'path')})
        return HandlerOutput(
            data={'items': items, 'after': rows[99][0] if len(rows) > 100 else None}
        )
