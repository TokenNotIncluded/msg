"""Readable mailbox labels and bounded conversation bodies, after ACL checks."""

import time

from msg.core.codec import wire
from msg.core.errors import require
from msg.core.models import ResourceRef
from msg.core.post_preview import post_preview


async def contact(app, ctx, request, tx, subject):
    from msg.plugins.discovery import visible

    if await visible(app, ctx, request, tx, subject):
        resource = await tx.resource(subject)
        return {'name': resource.name, 'path': await tx.path(subject)}
    return {'name': 'Private account'}


async def message_preview(app, ctx, request, tx, rid):
    from msg.plugins.discovery import visible

    require(time.monotonic() < ctx.deadline_monotonic, 'query_cost_exceeded')
    if not await visible(app, ctx, request, tx, rid):
        return None
    resource = await tx.resource(rid)
    if resource.type != 'post' or not resource.revision or resource.state != 'active':
        return None
    revision = await tx.revision(ResourceRef(id=rid, revision=resource.revision))
    raw = b''.join([
        chunk
        async for chunk in app.contents.read(
            revision.content, (0, min(revision.content.size, 16384))
        )
    ])
    body = raw.decode('utf-8', errors='replace')
    author = await contact(app, ctx, request, tx, revision.author)
    return {
        'id': rid,
        'path': await tx.path(rid),
        'author': author,
        'created_at': wire(resource.created_at),
        'body': body,
        'truncated': revision.content.size > 16384,
        **post_preview(resource.name, body),
    }


async def conversation_view(app, ctx, request, tx, rid, items):
    row = tx.one(
        'SELECT participant_a,participant_b,state FROM dm_conversations WHERE resource_id=?', (rid,)
    )
    if row is None:
        return None
    first, second, state = row
    require(ctx.principal.subject in {first, second}, 'permission_denied')
    other = second if ctx.principal.subject == first else first
    rows = tx.rows(
        "SELECT id FROM resources WHERE parent=? AND type='post' AND state='active' AND created_at<=? ORDER BY created_at DESC,id DESC LIMIT 51",
        (rid, wire(ctx.now)),
    )
    messages = []
    for (post_id,) in rows[:50]:
        preview = await message_preview(app, ctx, request, tx, post_id)
        if preview is not None:
            messages.append(preview)
    messages.sort(key=lambda item: (item['created_at'], item['id']))
    return {
        'contact': await contact(app, ctx, request, tx, other),
        'state': state,
        'messages': messages,
        'older_messages': len(rows) > 50,
    }
