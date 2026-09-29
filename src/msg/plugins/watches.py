"""Resource-backed target watches, projected by the existing Event transaction."""
import time
from datetime import timedelta

from msg.core.codec import canonical, decode, loads, parse_time, wire
from msg.core.errors import Failure, require
from msg.core.models import ExecutionContext, HandlerOutput, Principal, ResourceRef
from msg.core.requests import request_for
from msg.plugins.common import check_access, create_resource, new_id, operation_id, resolve, revise_resource
from msg.plugins.schemas import IDENTIFIER, STRING, obj

EVENT_TYPES = ('content.post_create', 'content.post_edit', 'discussion.reply', 'content.archive')


async def record(app, tx, rid):
    resource = await tx.resource(rid)
    require(resource.type == 'watch', 'watch_not_found')
    revision = await tx.revision(ResourceRef(id=rid))
    return resource, loads(await app.contents.read_bytes(revision.content))


async def create(app, ctx, request, tx, target, *, legacy=False):
    subject = ctx.principal.subject
    require(subject is not None, 'authentication_required')
    await app.authorizer.require_base(ctx.principal, operation_id(request), subject, tx)
    await check_access(app, ctx, request, tx, target, 'read')
    await app.authorizer._ceiling(ctx.principal, operation_id(request), target, tx)
    expires = request.arguments.get('expires_at')
    if expires:
        require(ctx.now < parse_time(expires) <= ctx.now + timedelta(days=365), 'watch_expiry_invalid')
    if legacy:
        for (rid,) in tx.rows("SELECT id FROM resources WHERE type='watch' AND owner=? AND state='active'", (subject,)):
            resource, saved = await record(app, tx, rid)
            if saved.get('legacy') and saved['target'] == target and saved['status'] == 'active':
                return resource, saved
    rid = new_id('watch')
    saved = {'id': rid, 'subject': subject, 'target': target, 'query_ref': None,
             'event_types': list(EVENT_TYPES) if legacy else request.arguments['event_types'],
             'delivery': 'inbox', 'created_at': wire(ctx.now), 'expires_at': expires,
             'status': 'active', 'legacy': legacy, 'principal': wire(ctx.principal),
             'operation': operation_id(request)}
    resource = await create_resource(app, ctx, request, tx, parent=subject, type='watch',
                                    name=rid, resource_id=rid, mode=0o600,
                                    body=canonical(saved), media_type='application/json')
    return resource, saved


async def cancel(app, ctx, request, tx, resource, saved):
    await revise_resource(app, ctx, request, tx, resource, canonical({**saved, 'status': 'cancelled'}), 'application/json')
    if saved.get('legacy'):
        tx.execute('DELETE FROM watches WHERE subject=? AND resource=?', (saved['subject'], saved['target']), write=True)


async def legacy(app, ctx, request, tx, target, enabled):
    if enabled:
        await create(app, ctx, request, tx, target, legacy=True)
    else:
        for (rid,) in tx.rows("SELECT id FROM resources WHERE type='watch' AND owner=? AND state='active'", (ctx.principal.subject,)):
            resource, saved = await record(app, tx, rid)
            if saved.get('legacy') and saved['target'] == target and saved['status'] == 'active':
                await cancel(app, ctx, request, tx, resource, saved)


async def enqueue(app, tx, event):
    from msg.security.quarantine import active
    from msg.workers.effects import current_principal
    if active(tx) or event.type not in EVENT_TYPES:
        return
    for (rid,) in tx.rows("SELECT id FROM resources WHERE type='watch' AND state='active' ORDER BY id"):
        resource, saved = await record(app, tx, rid)
        if saved['status'] != 'active' or event.type not in saved['event_types']:
            continue
        if saved['expires_at'] and app.clock() >= parse_time(saved['expires_at']):
            continue
        try:
            principal = await current_principal(app, decode(Principal, saved['principal']), tx)
            operation, version = saved['operation'].rsplit('@', 1)
            request = request_for(operation, {}, subject=principal.subject, service=app.settings.service_url, contract_version=int(version))
            ctx = ExecutionContext(request_id=event.id, principal=principal, entry='worker',
                                   now=app.clock(), deadline_monotonic=time.monotonic()+30)
            await check_access(app, ctx, request, tx, saved['target'], 'read')
            for ref in event.resources:
                ancestors = await tx.ancestors(ref.id)
                if ref.id != saved['target'] and not any(a.id == saved['target'] for a in ancestors):
                    continue
                await check_access(app, ctx, request, tx, ref.id, 'read')
                if tx.one('SELECT 1 FROM messages WHERE event_id=? AND recipient=? AND resource=?', (event.id, saved['subject'], ref.id)):
                    continue
                mid = new_id('message')
                body = {'id': mid, 'sender': event.subject, 'recipient': saved['subject'],
                        'resource': {'id': ref.id}, 'source': 'follow' if saved['legacy'] else 'watch', 'time': wire(event.time)}
                tx.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',
                           (mid, event.subject, saved['subject'], ref.id, event.id, canonical(body).decode()), write=True)
        except Failure:
            continue


def install(app, op):
    @op('communication.watch_create', obj({'target': IDENTIFIER, 'query_ref': STRING,
        'event_types': {'type': 'array', 'items': {'enum': list(EVENT_TYPES)}, 'minItems': 1,
                        'maxItems': len(EVENT_TYPES), 'uniqueItems': True},
        'delivery': {'enum': ['inbox']}, 'expires_at': STRING}, ('event_types', 'delivery')))
    async def watch_create(ctx, request, tx):
        require('query_ref' not in request.arguments, 'watch_query_unsupported')
        require('target' in request.arguments, 'watch_target_required')
        target = await resolve(tx, request.arguments['target'])
        resource, saved = await create(app, ctx, request, tx, target)
        return HandlerOutput(data={'id': resource.id, 'status': saved['status']})

    async def owned(ctx, request, tx, rid):
        resource, saved = await record(app, tx, await resolve(tx, rid))
        require(ctx.principal.subject == resource.owner == saved['subject'], 'permission_denied')
        await app.authorizer.require_base(ctx.principal, operation_id(request), resource.id, tx)
        return resource, saved

    async def public(ctx, request, tx, saved):
        result = {k: v for k, v in saved.items() if k not in {'principal', 'operation', 'legacy'}}
        try:
            await check_access(app, ctx, request, tx, saved['target'], 'read')
        except Failure:
            result.pop('target', None)
        if saved['expires_at'] and ctx.now >= parse_time(saved['expires_at']) and saved['status'] == 'active':
            result['status'] = 'expired'
        return result

    @op('communication.watch_get', obj({'id': IDENTIFIER}, ('id',)), effect='read')
    async def watch_get(ctx, request, tx):
        _, saved = await owned(ctx, request, tx, request.arguments['id'])
        return HandlerOutput(data=await public(ctx, request, tx, saved))

    @op('communication.watch_cancel', obj({'id': IDENTIFIER}, ('id',)))
    async def watch_cancel(ctx, request, tx):
        resource, saved = await owned(ctx, request, tx, request.arguments['id'])
        if saved['status'] != 'cancelled':
            await cancel(app, ctx, request, tx, resource, saved)
        return HandlerOutput(data={'id': resource.id, 'status': 'cancelled'})

    @op('communication.watch_list', obj(), effect='read')
    async def watch_list(ctx, request, tx):
        require(ctx.principal.subject is not None, 'authentication_required')
        await app.authorizer.require_base(ctx.principal, operation_id(request), ctx.principal.subject, tx)
        rows = tx.rows("SELECT id FROM resources WHERE type='watch' AND owner=? AND state='active' ORDER BY id LIMIT 101", (ctx.principal.subject,))
        require(len(rows) <= 100, 'watch_list_limit')
        items = []
        for (rid,) in rows:
            _, saved = await owned(ctx, request, tx, rid)
            items.append(await public(ctx, request, tx, saved))
        return HandlerOutput(data={'items': items})
