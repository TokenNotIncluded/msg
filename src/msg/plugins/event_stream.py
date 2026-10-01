"""One durable, authorized event stream over the committed event log."""

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


def install(app, op):
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
        binding = {
            'actor': ctx.principal.actor,
            'subject': subject,
            'credential_id': ctx.principal.credential_id,
            'certificates': sorted(ctx.principal.certificates),
            'resource': scope,
        }
        authority = digest({
            'epoch': tx.setting('authorization_epoch', 0),
            'watches': sorted(watches),
            'topics': [tuple(row) for row in memberships],
        })
        floor = tx.setting('sync_floor', 0)
        position = floor
        expires = ctx.now + timedelta(minutes=15)
        cursor = request.arguments.get('cursor')
        if cursor:
            saved = app.cursors.decode(cursor, 'events-v1', binding)
            require(isinstance(saved, dict), 'invalid_cursor')
            require(ctx.now < parse_time(saved['expires_at']), 'cursor_expired')
            require(saved.get('authority') == authority, 'resync_required')
            position = saved.get('seq')
            require(type(position) is int and position >= floor, 'resync_required')
            expires = parse_time(saved['expires_at'])

        async def readable(rid):
            if rid is None:
                return False
            try:
                resource = await tx.resource(rid)
                return resource.state != 'purged' and await visible(app, ctx, request, tx, rid)
            except Failure as exc:
                if exc.code in {'not_found', 'resource_purged', 'ancestor_inactive'}:
                    return False
                raise

        items = []
        limit = request.arguments.get('limit', 50)
        rows = tx.rows(
            'SELECT seq,body FROM events WHERE seq>? ORDER BY seq LIMIT 500', (position,)
        )
        for seq, raw in rows:
            require(time.monotonic() < ctx.deadline_monotonic, 'query_cost_exceeded')
            position = seq
            event = decode(Event, loads(raw))
            permitted = []
            for ref in event.resources:
                if not await readable(ref.id):
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
                    actor=event.actor if await readable(event.actor) else None,
                    subject=event.subject if await readable(event.subject) else None,
                )
                items.append({'seq': seq, **envelope})
                if len(items) >= limit:
                    break
        token = app.cursors.encode(
            'events-v1',
            binding,
            {'seq': position, 'authority': authority, 'expires_at': wire(expires)},
        )
        args = {**request.arguments, 'cursor': token}
        return HandlerOutput(
            data={
                'items': items,
                'cursor': token,
                'next': next_link(app, request.operation, args),
                'next_requires_auth': True,
            }
        )
