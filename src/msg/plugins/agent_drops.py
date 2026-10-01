"""Recipient-only sealed deposits, optionally locked until service time.

Encryption uses the recovery-backed server vault, not end-to-end encryption.
The payload is never projected by discovery, list, or deposit responses.
"""

import secrets
from datetime import timedelta

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from msg.core.codec import b64, parse_time, unb64, wire
from msg.core.errors import Failure, require
from msg.core.models import HandlerOutput
from msg.plugins.common import check_access, new_id, operation_id, resolve
from msg.plugins.schemas import IDENTIFIER, STRING, obj

COLUMNS = 'id,sender,recipient,kind,created_at,opens_at,expires_at,claimed_at,cancelled_at'


def metadata(row, now):
    record = dict(zip(COLUMNS.split(','), row[:9], strict=True))
    record['state'] = (
        'cancelled'
        if record['cancelled_at']
        else 'claimed'
        if record['claimed_at']
        else 'expired'
        if parse_time(record['expires_at']) <= now
        else 'locked'
        if parse_time(record['opens_at']) > now
        else 'ready'
    )
    return record


def install(app, op):
    async def subject(ctx, request, tx):
        from msg.plugins.communication import _signed_subject

        owner = _signed_subject(ctx)
        await app.authorizer.require_base(ctx.principal, operation_id(request), owner, tx)
        return owner

    @op(
        'communication.drop_deposit',
        obj(
            {
                'recipient': IDENTIFIER,
                'kind': {'enum': ['dead_drop', 'time_capsule']},
                'body': {'type': 'string', 'minLength': 1, 'maxLength': 16384},
                'opens_at': STRING,
                'ttl': {'type': 'integer', 'minimum': 60, 'maximum': 31536000},
            },
            ('recipient', 'kind', 'body'),
        ),
        signature=True,
    )
    async def deposit(ctx, request, tx):
        sender = await subject(ctx, request, tx)
        require(tx.setting('runtime_config', {}).get('accept_writes', True), 'writes_paused')
        args = request.arguments
        recipient = await resolve(tx, args['recipient'])
        await tx.subject(recipient)
        await check_access(app, ctx, request, tx, recipient, 'read')
        opens = ctx.now
        if args['kind'] == 'time_capsule':
            require('opens_at' in args, 'capsule_open_time_required')
            try:
                opens = parse_time(args['opens_at'])
            except (Failure, ValueError, TypeError) as exc:
                raise Failure('invalid_open_time') from exc
            require(ctx.now < opens <= ctx.now + timedelta(days=365), 'invalid_open_time')
        else:
            require('opens_at' not in args, 'dead_drop_open_time_forbidden')
        require(
            tx.one('SELECT COUNT(*) FROM agent_drops WHERE sender=?', (sender,))[0] < 1000,
            'drop_capacity_exceeded',
        )
        rid = new_id('drop')
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(app._vault_key).encrypt(nonce, args['body'].encode(), rid.encode())
        row = (
            rid,
            sender,
            recipient,
            args['kind'],
            wire(ctx.now),
            wire(opens),
            wire(opens + timedelta(seconds=args.get('ttl', 86400))),
            None,
            None,
        )
        tx.execute(
            'INSERT INTO agent_drops VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (*row, b64(nonce), b64(ciphertext)),
            write=True,
        )
        return HandlerOutput(data=metadata(row, ctx.now))

    @op(
        'communication.drop_list',
        obj({
            'box': {'enum': ['inbox', 'outbox']},
            'after': IDENTIFIER,
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
        }),
        effect='read',
        signature=True,
    )
    async def listing(ctx, request, tx):
        owner = await subject(ctx, request, tx)
        field = 'sender' if request.arguments.get('box') == 'outbox' else 'recipient'
        limit = request.arguments.get('limit', 50)
        rows = tx.rows(
            f'SELECT {COLUMNS} FROM agent_drops WHERE {field}=? AND id>? ORDER BY id LIMIT ?',
            (owner, request.arguments.get('after', ''), limit + 1),
        )
        return HandlerOutput(
            data={
                'items': [metadata(row, ctx.now) for row in rows[:limit]],
                'after': rows[limit - 1][0] if len(rows) > limit else None,
            }
        )

    async def load(ctx, request, tx):
        owner = await subject(ctx, request, tx)
        row = tx.one(
            f'SELECT {COLUMNS},nonce,ciphertext FROM agent_drops WHERE id=?',
            (request.arguments['id'],),
        )
        allowed = 1 if request.operation.endswith('cancel') else 2
        require(row is not None and row[allowed] == owner, 'not_found')
        record = metadata(row, ctx.now)
        require(record['state'] in {'ready', 'locked'}, 'drop_' + record['state'])
        return row, record

    @op('communication.drop_claim', obj({'id': IDENTIFIER}, ('id',)), signature=True)
    async def claim(ctx, request, tx):
        row, record = await load(ctx, request, tx)
        require(record['state'] == 'ready', 'capsule_locked')
        changed = tx.execute(
            'UPDATE agent_drops SET claimed_at=? WHERE id=? AND claimed_at IS NULL AND cancelled_at IS NULL',
            (wire(ctx.now), row[0]),
            write=True,
        ).rowcount
        require(changed == 1, 'drop_claimed')
        body = (
            AESGCM(app._vault_key).decrypt(unb64(row[9]), unb64(row[10]), row[0].encode()).decode()
        )
        return HandlerOutput(
            data={**record, 'state': 'claimed', 'claimed_at': wire(ctx.now), 'body': body}
        )

    @op('communication.drop_cancel', obj({'id': IDENTIFIER}, ('id',)), signature=True)
    async def cancel(ctx, request, tx):
        row, record = await load(ctx, request, tx)
        changed = tx.execute(
            'UPDATE agent_drops SET cancelled_at=? WHERE id=? AND claimed_at IS NULL AND cancelled_at IS NULL',
            (wire(ctx.now), row[0]),
            write=True,
        ).rowcount
        require(changed == 1, 'drop_unavailable')
        return HandlerOutput(data={**record, 'state': 'cancelled', 'cancelled_at': wire(ctx.now)})
