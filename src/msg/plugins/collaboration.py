"""Explicit handoff and expiring work leases; neither grants authority."""

from __future__ import annotations

import time
from datetime import timedelta

from msg.core.codec import canonical, loads, parse_time, wire
from msg.core.errors import Failure, require
from msg.core.models import HandlerOutput
from msg.plugins.common import check_access, new_id, operation_id, resolve
from msg.plugins.schemas import IDENTIFIER, STRING, obj

MAX_LEASE_TTL = timedelta(days=7)
MAX_HANDOFF_REFS = 16


async def _self(app, ctx, request, tx):
    subject = ctx.principal.subject
    require(
        subject is not None and ctx.principal.actor == subject, 'collaboration_subject_required'
    )
    await app.authorizer.require_base(ctx.principal, operation_id(request), subject, tx)
    return subject


async def _safe_ref(app, ctx, request, tx, value):
    rid = await resolve(tx, value)
    resource = await tx.resource(rid)
    chain = (*await tx.ancestors(rid), resource)
    require(
        resource.state == 'active'
        and not any(
            item.id in {'r_agents', 'r_rules', 't_last_will'}
            or item.type
            in {'dm_conversation', 'credential', 'certificate', 'csr', 'legacy_directive', 'tool'}
            for item in chain
        ),
        'collaboration_ref_forbidden',
    )
    require(
        not any(
            parent.type == 'user' and child.name in {'keystore', 'SOUL.md', 'AGENTS.md', 'todos'}
            for parent, child in zip(chain, chain[1:], strict=False)
        ),
        'collaboration_ref_forbidden',
    )
    require(
        tx.one(
            'SELECT 1 FROM dm_conversations WHERE resource_id IN ('
            + ','.join('?' for _ in chain)
            + ') LIMIT 1',
            tuple(item.id for item in chain),
        )
        is None,
        'collaboration_ref_forbidden',
    )
    require(
        not any(
            tx.setting('hosting_preview:' + item.id)
            or tx.setting('hosting_preview_file:' + item.id)
            for item in chain
        ),
        'collaboration_ref_forbidden',
    )
    await check_access(app, ctx, request, tx, rid, 'read')
    return rid


async def _visible_refs(app, ctx, request, tx, refs):
    output = []
    for rid in refs:
        try:
            await _safe_ref(app, ctx, request, tx, rid)
        except Failure as exc:
            if exc.code in {
                'permission_denied',
                'credential_ceiling',
                'certificate_gate',
                'delegation_scope',
                'ancestor_inactive',
                'not_found',
                'collaboration_ref_forbidden',
            }:
                continue
            raise
        output.append(rid)
    return output


def _notice(tx, *, sender, recipient, handoff_id, status, now):
    # The resource is the recipient's own user resource: Inbox can filter it by
    # current ACL. No handoff message, target name, or private ref is copied.
    record = {
        'id': new_id('message'),
        'sender': sender,
        'recipient': recipient,
        'resource': {'id': recipient},
        'source': 'handoff',
        'handoff_id': handoff_id,
        'status': status,
        'time': wire(now),
    }
    tx.execute(
        'INSERT INTO messages (id,sender,recipient,resource,event_id,body) VALUES (?,?,?,?,?,?)',
        (record['id'], sender, recipient, recipient, None, canonical(record).decode()),
        write=True,
    )


def _summary(record):
    return {key: record[key] for key in ('id', 'status', 'generation')}


def install(app, op):
    @op(
        'communication.handoff_create',
        obj(
            {
                'to_subject': IDENTIFIER,
                'resource_refs': {
                    'type': 'array',
                    'items': IDENTIFIER,
                    'maxItems': MAX_HANDOFF_REFS,
                    'uniqueItems': True,
                },
                'message': {'type': 'string', 'maxLength': 4096},
                'next_action': {'type': 'string', 'maxLength': 1024},
            },
            ('to_subject', 'resource_refs'),
        ),
    )
    async def handoff_create(ctx, request, tx):
        sender = await _self(app, ctx, request, tx)
        recipient = await resolve(tx, request.arguments['to_subject'])
        target_subject = await tx.subject(recipient)
        require(not target_subject.local_only, 'invalid_handoff_recipient')
        require(recipient != sender, 'invalid_handoff_recipient')
        refs = [
            await _safe_ref(app, ctx, request, tx, value)
            for value in request.arguments['resource_refs']
        ]
        record = {
            'id': new_id('handoff'),
            'from_subject': sender,
            'to_subject': recipient,
            'resource_refs': refs,
            'message': request.arguments.get('message', ''),
            'next_action': request.arguments.get('next_action'),
            'created_at': wire(ctx.now),
            'updated_at': wire(ctx.now),
            'status': 'pending',
            'generation': 1,
        }
        tx.execute(
            'INSERT INTO handoffs VALUES (?,?,?,?,?,?)',
            (record['id'], sender, recipient, 'pending', 1, canonical(record).decode()),
            write=True,
        )
        _notice(
            tx,
            sender=sender,
            recipient=recipient,
            handoff_id=record['id'],
            status='pending',
            now=ctx.now,
        )
        # Mutation results are replayed by the executor without running this
        # handler again. Keep target refs only in ACL-checked read views.
        return HandlerOutput(data={'handoff': _summary(record)})

    @op('communication.handoff_get', obj({'id': IDENTIFIER}, ('id',)), effect='read')
    async def handoff_get(ctx, request, tx):
        subject = await _self(app, ctx, request, tx)
        row = tx.one(
            'SELECT from_subject,to_subject,body FROM handoffs WHERE id=?',
            (request.arguments['id'],),
        )
        require(row is not None and subject in row[:2], 'handoff_not_found')
        record = loads(row[2])
        record['resource_refs'] = await _visible_refs(
            app, ctx, request, tx, record['resource_refs']
        )
        # Handoff text is authored for its participant, but it cannot carry a
        # private target's body via our own projection.
        return HandlerOutput(data={'handoff': record})

    @op(
        'communication.handoff_list',
        obj({'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}, 'after': IDENTIFIER}),
        effect='read',
    )
    async def handoff_list(ctx, request, tx):
        subject = await _self(app, ctx, request, tx)
        limit = request.arguments.get('limit', 50)
        rows = tx.rows(
            'SELECT id,body FROM handoffs WHERE '
            '(from_subject=? OR to_subject=?) AND id>? ORDER BY id LIMIT ?',
            (subject, subject, request.arguments.get('after', ''), limit + 1),
        )
        records = []
        for _, raw in rows[:limit]:
            record = loads(raw)
            record['resource_refs'] = await _visible_refs(
                app, ctx, request, tx, record['resource_refs']
            )
            records.append(record)
        return HandlerOutput(
            data={'items': records, 'next_after': rows[limit - 1][0] if len(rows) > limit else None}
        )

    @op(
        'communication.handoff_decide',
        obj(
            {
                'id': IDENTIFIER,
                'decision': {'enum': ['accept', 'reject', 'cancel']},
                'expected_generation': {'type': 'integer', 'minimum': 1},
            },
            ('id', 'decision', 'expected_generation'),
        ),
    )
    async def handoff_decide(ctx, request, tx):
        subject = await _self(app, ctx, request, tx)
        row = tx.one(
            'SELECT from_subject,to_subject,status,generation,body FROM handoffs WHERE id=?',
            (request.arguments['id'],),
        )
        require(row is not None and subject in row[:2], 'handoff_not_found')
        sender, recipient, state, generation, raw = row
        decision = request.arguments['decision']
        require(state == 'pending', 'handoff_not_pending')
        require(
            (decision == 'cancel' and subject == sender)
            or (decision in {'accept', 'reject'} and subject == recipient),
            'handoff_decision_forbidden',
        )
        require(generation == request.arguments['expected_generation'], 'generation_conflict')
        status = {'accept': 'accepted', 'reject': 'rejected', 'cancel': 'cancelled'}[decision]
        record = loads(raw)
        record.update(status=status, generation=generation + 1, updated_at=wire(ctx.now))
        updated = tx.execute(
            'UPDATE handoffs SET status=?,generation=?,body=? '
            'WHERE id=? AND status=? AND generation=?',
            (
                status,
                generation + 1,
                canonical(record).decode(),
                request.arguments['id'],
                'pending',
                generation,
            ),
            write=True,
        )
        require(updated.rowcount == 1, 'generation_conflict')
        _notice(
            tx,
            sender=subject,
            recipient=recipient if subject == sender else sender,
            handoff_id=record['id'],
            status=status,
            now=ctx.now,
        )
        return HandlerOutput(data={'handoff': _summary(record)})

    @op(
        'communication.lease_acquire',
        obj(
            {
                'target': IDENTIFIER,
                'purpose': {'type': 'string', 'minLength': 1, 'maxLength': 1024},
                'expires_at': STRING,
            },
            ('target', 'purpose', 'expires_at'),
        ),
    )
    async def lease_acquire(ctx, request, tx):
        holder = await _self(app, ctx, request, tx)
        target = await _safe_ref(app, ctx, request, tx, request.arguments['target'])
        expires = parse_time(request.arguments['expires_at'])
        require(ctx.now < expires <= ctx.now + MAX_LEASE_TTL, 'invalid_lease_expiry')
        record = {
            'id': new_id('lease'),
            'holder': holder,
            'target': target,
            'purpose': request.arguments['purpose'],
            'acquired_at': wire(ctx.now),
            'expires_at': wire(expires),
            'status': 'active',
            'generation': 1,
        }
        tx.execute(
            'INSERT INTO collaboration_leases VALUES (?,?,?,?,?,?,?)',
            (record['id'], holder, target, 'active', 1, wire(expires), canonical(record).decode()),
            write=True,
        )
        return HandlerOutput(data={'lease': _summary(record)})

    async def _lease(ctx, request, tx, *, mutation=False):
        holder = await _self(app, ctx, request, tx)
        row = tx.one(
            'SELECT holder,target,status,generation,expires_at,body '
            'FROM collaboration_leases WHERE id=?',
            (request.arguments['id'],),
        )
        require(row is not None and row[0] == holder, 'lease_not_found')
        # A lease cannot keep a target visible after permission is revoked.
        await _safe_ref(app, ctx, request, tx, row[1])
        return row, loads(row[5])

    @op('communication.lease_get', obj({'id': IDENTIFIER}, ('id',)), effect='read')
    async def lease_get(ctx, request, tx):
        row, record = await _lease(ctx, request, tx)
        record['effective_status'] = (
            'expired' if row[2] == 'active' and parse_time(row[4]) <= ctx.now else row[2]
        )
        return HandlerOutput(data={'lease': record})

    @op(
        'communication.lease_list',
        obj({'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}, 'after': IDENTIFIER}),
        effect='read',
    )
    async def lease_list(ctx, request, tx):
        holder = await _self(app, ctx, request, tx)
        limit = request.arguments.get('limit', 50)
        position = request.arguments.get('after', '')
        records = []
        more = False
        scanned = 0
        while not more:
            require(time.monotonic() < ctx.deadline_monotonic, 'query_cost_exceeded')
            rows = tx.rows(
                'SELECT id,target,status,expires_at,body FROM collaboration_leases '
                'WHERE holder=? AND id>? ORDER BY id LIMIT 128',
                (holder, position),
            )
            for lease_id, target, status, expires, raw in rows:
                scanned += 1
                require(
                    scanned <= 4096 and time.monotonic() < ctx.deadline_monotonic,
                    'query_cost_exceeded',
                )
                position = lease_id
                try:
                    await _safe_ref(app, ctx, request, tx, target)
                except Failure as exc:
                    if exc.code in {
                        'permission_denied',
                        'credential_ceiling',
                        'certificate_gate',
                        'delegation_scope',
                        'ancestor_inactive',
                        'not_found',
                        'collaboration_ref_forbidden',
                    }:
                        continue
                    raise
                if len(records) == limit:
                    more = True
                    break
                record = loads(raw)
                record['effective_status'] = (
                    'expired' if status == 'active' and parse_time(expires) <= ctx.now else status
                )
                records.append(record)
            if len(rows) < 128:
                break
        return HandlerOutput(
            data={'items': records, 'next_after': records[-1]['id'] if more else None}
        )

    @op(
        'communication.lease_renew',
        obj(
            {
                'id': IDENTIFIER,
                'expires_at': STRING,
                'expected_generation': {'type': 'integer', 'minimum': 1},
            },
            ('id', 'expires_at', 'expected_generation'),
        ),
    )
    async def lease_renew(ctx, request, tx):
        row, record = await _lease(ctx, request, tx, mutation=True)
        require(row[2] == 'active' and parse_time(row[4]) > ctx.now, 'lease_inactive')
        require(row[3] == request.arguments['expected_generation'], 'generation_conflict')
        expires = parse_time(request.arguments['expires_at'])
        require(
            ctx.now < expires <= ctx.now + MAX_LEASE_TTL and expires > parse_time(row[4]),
            'invalid_lease_expiry',
        )
        record.update(expires_at=wire(expires), generation=row[3] + 1)
        updated = tx.execute(
            'UPDATE collaboration_leases SET generation=?,expires_at=?,body=? '
            'WHERE id=? AND generation=? AND status=? AND expires_at=?',
            (
                row[3] + 1,
                wire(expires),
                canonical(record).decode(),
                request.arguments['id'],
                row[3],
                'active',
                row[4],
            ),
            write=True,
        )
        require(updated.rowcount == 1, 'generation_conflict')
        return HandlerOutput(data={'lease': _summary(record)})

    @op(
        'communication.lease_release',
        obj(
            {
                'id': IDENTIFIER,
                'expected_generation': {'type': 'integer', 'minimum': 1},
            },
            ('id', 'expected_generation'),
        ),
    )
    async def lease_release(ctx, request, tx):
        row, record = await _lease(ctx, request, tx, mutation=True)
        require(row[2] == 'active' and parse_time(row[4]) > ctx.now, 'lease_inactive')
        require(row[3] == request.arguments['expected_generation'], 'generation_conflict')
        record.update(status='released', generation=row[3] + 1)
        updated = tx.execute(
            'UPDATE collaboration_leases SET status=?,generation=?,body=? '
            'WHERE id=? AND status=? AND generation=?',
            (
                'released',
                row[3] + 1,
                canonical(record).decode(),
                request.arguments['id'],
                'active',
                row[3],
            ),
            write=True,
        )
        require(updated.rowcount == 1, 'generation_conflict')
        return HandlerOutput(data={'lease': _summary(record)})
