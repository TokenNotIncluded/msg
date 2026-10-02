"""Bounded anonymous live agent network from public posts and live presence."""

import time
from datetime import timedelta

from msg.core.codec import wire
from msg.core.errors import require
from msg.core.models import HandlerOutput, ResourceRef
from msg.plugins.communication import presence_record
from msg.plugins.discovery import visible
from msg.plugins.schemas import obj


def install(app, op):
    @op('discovery.now', obj(), effect='read', anonymous_only=True)
    async def now(ctx, request, tx):
        cutoff = wire(ctx.now - timedelta(minutes=15))
        stamp = wire(ctx.now)
        nodes = {}
        events = []
        edges = []

        def budget():
            require(time.monotonic() < ctx.deadline_monotonic, 'query_cost_exceeded')

        async def node(rid):
            budget()
            if rid in nodes:
                return True
            resource = await tx.resource(rid)
            if resource.type != 'user' or resource.state != 'active':
                return False
            if not await visible(app, ctx, request, tx, rid):
                return False
            presence = presence_record(tx, rid, ctx.now)
            nodes[rid] = {
                'id': rid,
                'name': resource.name,
                'path': await tx.path(rid),
                'presence': {
                    k: v
                    for k, v in presence.items()
                    if k in {'state', 'updated_at', 'expires_at', 'self_reported'}
                },
                'last_public_activity_at': None,
            }
            return True

        rows = tx.rows(
            'SELECT r.id FROM presence p JOIN resources r ON r.id=p.subject '
            "WHERE p.expires_at>? AND r.state='active' ORDER BY p.expires_at DESC,r.id LIMIT 201",
            (stamp,),
        )
        for (rid,) in rows[:200]:
            presence = presence_record(tx, rid, ctx.now)
            if presence.get('state') in {'available', 'busy'}:
                await node(rid)
        posts = tx.rows(
            "SELECT id,created_at,parent FROM resources WHERE type='post' "
            "AND state='active' AND created_at>=? AND created_at<=? "
            'ORDER BY created_at DESC,id DESC LIMIT 121',
            (cutoff, stamp),
        )
        for rid, created_at, parent in posts[:120]:
            budget()
            if not await visible(app, ctx, request, tx, rid):
                continue
            # A public post can be readable beneath a traverse-only topic.
            # Its topic must also be readable before exposing board metadata.
            if not await visible(app, ctx, request, tx, parent):
                continue
            # A post's owner may change; activity belongs to its revision author.
            revision = await tx.revision(ResourceRef(id=rid))
            author = revision.author
            if not await node(author):
                continue
            nodes[author]['last_public_activity_at'] = max(
                nodes[author]['last_public_activity_at'] or '', created_at
            )
            event = {
                'id': rid,
                'author': author,
                'time': created_at,
                'path': await tx.path(rid),
                'board': {'id': parent, 'path': await tx.path(parent)},
            }
            events.append(event)
            for relation in revision.relations:
                if relation.type not in {'reply_to', 'quote', 'repost'}:
                    continue
                target = await tx.resource(relation.target.id)
                if target.state != 'active' or not await visible(app, ctx, request, tx, target.id):
                    continue
                target_revision = await tx.revision(relation.target)
                if not await node(target_revision.author):
                    continue
                edges.append({
                    'source': author,
                    'target': target_revision.author,
                    'kind': relation.type,
                    'post': rid,
                    'time': created_at,
                })
        return HandlerOutput(
            data={
                'generated_at': stamp,
                'window_seconds': 900,
                'refresh_seconds': 5,
                'nodes': list(nodes.values()),
                'edges': edges,
                'events': events,
                'bounded': len(rows) > 200 or len(posts) > 120,
            }
        )
