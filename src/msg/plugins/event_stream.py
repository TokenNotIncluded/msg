"""One durable, authorized event stream over the committed event log."""

import asyncio
import time
from datetime import timedelta

from msg.core.addressing import parse_address
from msg.core.codec import decode, digest, loads, parse_time, wire
from msg.core.errors import Failure, require
from msg.core.events import event_envelope
from msg.core.models import Event, HandlerOutput
from msg.plugins.common import check_access, operation_id, resolve_read
from msg.plugins.discovery import next_link, visible
from msg.plugins.schemas import STRING, obj


async def readable(app, ctx, request, tx, rid):
    if rid is None:
        return False
    try:
        resource = await tx.resource(rid)
        return resource.state != 'purged' and await visible(app, ctx, request, tx, rid)
    except Failure as exc:
        if exc.code in {'not_found', 'resource_purged', 'ancestor_inactive'}:
            return False
        raise


def install(app, op):
    @op(
        'communication.event',
        obj({'id': {'type': 'string', 'pattern': '^[A-Za-z0-9_.:-]{1,160}$'}}, ('id',)),
        effect='read',
    )
    async def event_get(ctx, request, tx):
        subject = ctx.principal.subject
        require(subject is not None, 'authentication_required')
        await app.authorizer.require_base(ctx.principal, operation_id(request), subject, tx)
        row = tx.one('SELECT seq,body FROM events WHERE id=?', (request.arguments['id'],))
        require(row is not None, 'not_found')
        event = decode(Event, loads(row[1]))
        refs = tuple([
            ref for ref in event.resources if await readable(app, ctx, request, tx, ref.id)
        ])
        require(refs or (not event.resources and event.subject == subject), 'not_found')
        return HandlerOutput(
            data={
                'seq': row[0],
                **event_envelope(
                    event,
                    app.settings.service_url,
                    refs,
                    actor=event.actor
                    if await readable(app, ctx, request, tx, event.actor)
                    else None,
                    subject=event.subject
                    if await readable(app, ctx, request, tx, event.subject)
                    else None,
                ),
            }
        )

    @op(
        'communication.events',
        obj({
            'resource': {'type': 'string', 'minLength': 1, 'maxLength': 2048},
            'cursor': STRING,
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200},
        }),
        effect='read',
    )
    async def events(ctx, request, tx):
        subject = ctx.principal.subject
        require(subject is not None, 'authentication_required')
        await app.authorizer.require_base(ctx.principal, operation_id(request), subject, tx)
        scope = request.arguments.get('resource')
        if scope is not None:
            target, _ = parse_address(scope, app.settings.service_url)
            scope = await resolve_read(tx, target)
            await check_access(app, ctx, request, tx, scope, 'read')
        watches = {
            r[0] for r in tx.rows('SELECT resource FROM watches WHERE subject=?', (subject,))
        }
        memberships = tx.rows(
            "SELECT topic,role FROM topic_memberships WHERE subject=? AND status='active' ORDER BY topic",
            (subject,),
        )
        topics = {row[0] for row in memberships}
        subscription = request.contract_version == 2
        binding = {
            'actor': ctx.principal.actor,
            'subject': subject,
            'credential_id': ctx.principal.credential_id,
            'certificates': sorted(ctx.principal.certificates),
            'resource': scope,
        }
        previous_binding = dict(binding)
        if subscription:
            binding.pop('credential_id')
            binding.pop('certificates')
        authority = digest({
            'epoch': tx.setting('authorization_epoch', 0),
            'watches': sorted(watches),
            'topics': [tuple(row) for row in memberships],
            **(
                {
                    'credential_id': ctx.principal.credential_id,
                    'certificates': sorted(ctx.principal.certificates),
                }
                if subscription
                else {}
            ),
        })
        floor = tx.setting('sync_floor', 0)
        position = floor
        expires = ctx.now + timedelta(minutes=15)
        cursor = request.arguments.get('cursor')
        authority_changed = False
        if cursor:
            if subscription:
                inspected = app.cursors.inspect(cursor)
                require(inspected.get('kind') in {'events-v1', 'events-v2'}, 'cursor_kind_mismatch')
                query = inspected.get('query')
                require(isinstance(query, dict), 'invalid_cursor')
                stable = {key: query.get(key) for key in binding}
                require(stable == binding, 'cursor_query_mismatch')
                saved = inspected.get('position')
                require(isinstance(saved, dict), 'invalid_cursor')
                # A signed position is not authority. Recheck today's credential,
                # scope and each reference, including after a credential rotation.
                authority_changed = saved.get('authority') != authority
            else:
                saved = app.cursors.decode(cursor, 'events-v1', previous_binding)
                require(isinstance(saved, dict), 'invalid_cursor')
                require(ctx.now < parse_time(saved['expires_at']), 'cursor_expired')
                require(saved.get('authority') == authority, 'resync_required')
                expires = parse_time(saved['expires_at'])
            position = saved.get('seq')
            require(type(position) is int and position >= floor, 'resync_required')

        tail = max(position, tx.one('SELECT COALESCE(MAX(seq),0) FROM events')[0])
        if subscription and request.arguments.get('tail'):
            require(cursor is None, 'listener_start_position_conflict')
            position = tail

        def resume(sequence):
            state = {'seq': sequence, 'authority': authority}
            if not subscription:
                state['expires_at'] = wire(expires)
            return app.cursors.encode('events-v2' if subscription else 'events-v1', binding, state)

        items = []
        limit = request.arguments.get('limit', 50)
        budget = 64 if subscription else 500
        rows = tx.rows(
            'SELECT seq,body FROM events WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?',
            (position, tail, budget),
        )
        for seq, raw in rows:
            require(time.monotonic() < ctx.deadline_monotonic, 'query_cost_exceeded')
            before = position
            position = seq
            event = decode(Event, loads(raw))
            permitted = []
            for ref in event.resources:
                if subscription:
                    await asyncio.sleep(0)
                if not await readable(app, ctx, request, tx, ref.id):
                    continue
                ancestors = {r.id for r in await tx.ancestors(ref.id)}
                if scope is not None:
                    relevant = ref.id == scope or scope in ancestors
                else:
                    relevant = (
                        event.subject == subject
                        or ref.id in watches | topics
                        or bool(ancestors & (watches | topics))
                    )
                    if not relevant:
                        from msg.plugins.communication import direct_ancestor

                        direct = await direct_ancestor(tx, ref.id)
                        pair = (
                            tx.one(
                                'SELECT participant_a,participant_b FROM dm_conversations WHERE resource_id=?',
                                (direct,),
                            )
                            if direct is not None
                            else None
                        )
                        relevant = pair is not None and subject in pair
                if relevant:
                    permitted.append(ref)
            own_empty = scope is None and not event.resources and event.subject == subject
            if permitted or own_empty:
                envelope = event_envelope(
                    event,
                    app.settings.service_url,
                    permitted,
                    actor=event.actor
                    if await readable(app, ctx, request, tx, event.actor)
                    else None,
                    subject=event.subject
                    if await readable(app, ctx, request, tx, event.subject)
                    else None,
                )
                item = {'seq': seq, **envelope}
                if subscription:
                    item.update(before_cursor=resume(before), resume_cursor=resume(seq))
                items.append(item)
                if len(items) >= limit:
                    break
        token = resume(position)
        args = {**request.arguments, 'cursor': token}
        return HandlerOutput(
            data={
                'items': items,
                'cursor': token,
                **({'next': next_link(app, request.operation, args)} if not subscription else {}),
                'next_requires_auth': True,
                **(
                    {
                        'has_more': position < tail,
                        'authority_changed': authority_changed,
                        'wait_supported': getattr(app.metadata, 'event_signal', None) is not None,
                    }
                    if subscription
                    else {}
                ),
            }
        )

    op(
        'communication.events',
        obj({
            'resource': {'type': 'string', 'minLength': 1, 'maxLength': 2048},
            'cursor': STRING,
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200},
            'tail': {'type': 'boolean'},
            'wait_ms': {'type': 'integer', 'minimum': 0, 'maximum': 20000},
        }),
        effect='read',
        version=2,
    )(events)
