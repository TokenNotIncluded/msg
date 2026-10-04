"""One-paste Agent Link rendezvous.

The owner opens an invitation. The helper, which still has no account, claims
it with a possession proof of a new key. That claim is an event on the owner's
private change stream. After the owner issues the delegated identity, the same
proof collects the grant sealed to the helper's encryption key.
"""

import re
from datetime import timedelta

from msg.constants import ROOT_SUBJECT
from msg.core.codec import b64, canonical, decode, loads, parse_time, unb64, wire
from msg.core.errors import require
from msg.core.models import Event, HandlerOutput, Signature
from msg.plugins.common import new_id, operation_id
from msg.plugins.delegated_identity import possession_body
from msg.plugins.schemas import BYTES, SIGNATURE, STRING, obj
from msg.security.age_keys import public_from_recipient
from msg.security.crypto import verify
from msg.security.sealed_box import validate_envelope

COLUMNS = (
    'invite_id,owner,name,minutes,created_at,expires_at,claim,claimed_at,'
    'grant_box,released_at,revoked_at'
)
NAME = r'[A-Za-z0-9][A-Za-z0-9_-]{0,58}'


def link_name(value):
    require(
        isinstance(value, str) and re.fullmatch(NAME, value) and not value.endswith('-lead'),
        'invalid_link_name',
    )
    return value


def invite_id(value):
    require(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value), 'invalid_link_code')
    return value


def row_state(row, now):
    if row[10] is not None:
        return 'revoked'
    if parse_time(row[5]) <= now and row[8] is None:
        return 'expired'
    if row[8] is not None:
        return 'released'
    if row[6] is not None:
        return 'claimed'
    return 'open'


def load_row(tx, ident):
    return tx.one(f'SELECT {COLUMNS} FROM link_invitations WHERE invite_id=?', (ident,))


def claim_body(row):
    return loads(row[6]) if row[6] is not None else None


def install(app, op):
    def verify_claim(arguments):
        public = unb64(arguments['public_key'], limit=32)
        require(len(public) == 32, 'invalid_public_key')
        recipient = arguments['encryption_recipient']
        require(public != public_from_recipient(recipient), 'encryption_key_must_be_independent')
        verify(
            public,
            possession_body(
                app.settings.service_url,
                arguments['public_key'],
                recipient,
                arguments['grantor'],
            ),
            decode(Signature, arguments['possession_proof']),
            purpose='delegated-identity-v1',
        )
        return {
            'target_service': app.settings.service_url,
            'grantor': arguments['grantor'],
            'public_key': arguments['public_key'],
            'encryption_recipient': recipient,
            'possession_proof': arguments['possession_proof'],
        }

    claim_schema = obj(
        {
            'invite_id': STRING,
            'grantor': STRING,
            'public_key': BYTES,
            'encryption_recipient': STRING,
            'possession_proof': SIGNATURE,
        },
        ('invite_id', 'grantor', 'public_key', 'encryption_recipient', 'possession_proof'),
    )

    async def owner_subject(ctx, request, tx):
        principal = ctx.principal
        require(
            principal.subject is not None
            and principal.actor == principal.subject
            and principal.subject != ROOT_SUBJECT,
            'link_owner_required',
        )
        await app.authorizer.require_base(principal, operation_id(request), principal.subject, tx)
        return await tx.resource(principal.subject)

    @op(
        'identity.link_open',
        obj(
            {
                'invite_id': STRING,
                'name': STRING,
                'minutes': {'type': 'integer', 'minimum': 1, 'maximum': 1440},
            },
            ('invite_id', 'name', 'minutes'),
        ),
        signature=True,
    )
    async def open_invite(ctx, request, tx):
        owner = await owner_subject(ctx, request, tx)
        args = request.arguments
        ident, name = invite_id(args['invite_id']), link_name(args['name'])
        require(tx.setting('runtime_config', {}).get('accept_writes', True), 'writes_paused')
        existing = tx.one(
            f'SELECT {COLUMNS} FROM link_invitations WHERE owner=? AND name=?',
            (owner.id, name),
        )
        if existing is not None:
            require(existing[0] == ident and existing[3] == args['minutes'], 'link_exists')
            return HandlerOutput(data={'invite_id': ident, 'name': name, 'expires_at': existing[5]})
        expires = wire(ctx.now + timedelta(minutes=args['minutes']))
        tx.execute(
            'INSERT INTO link_invitations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (
                ident,
                owner.id,
                name,
                args['minutes'],
                wire(ctx.now),
                expires,
                None,
                None,
                None,
                None,
                None,
            ),
            write=True,
        )
        return HandlerOutput(data={'invite_id': ident, 'name': name, 'expires_at': expires})

    @op('identity.link_claim', claim_schema, anonymous_only=True)
    async def claim(ctx, request, tx):
        require(tx.setting('runtime_config', {}).get('accept_writes', True), 'writes_paused')
        args = request.arguments
        ident = invite_id(args['invite_id'])
        body = verify_claim(args)
        row = load_row(tx, ident)
        require(row is not None, 'not_found')
        owner = await tx.resource(row[1])
        handle = owner.name if str(owner.name).startswith('@') else '@' + owner.name
        require(body['grantor'] in {owner.id, owner.name, handle}, 'delegated_grantor_mismatch')
        require(row[10] is None, 'link_revoked')
        encoded = canonical(body).decode()
        if row[6] is not None:
            require(row[6] == encoded, 'link_claim_taken')
            return HandlerOutput(data={'invite_id': ident, 'name': row[2], 'state': 'claimed'})
        require(parse_time(row[5]) > ctx.now, 'link_invite_expired')
        changed = tx.execute(
            """UPDATE link_invitations SET claim=?, claimed_at=?
            WHERE invite_id=? AND claim IS NULL AND revoked_at IS NULL""",
            (encoded, wire(ctx.now), ident),
            write=True,
        ).rowcount
        require(changed == 1, 'link_claim_taken')
        await tx.append_event(
            Event(
                id=new_id('ev'),
                type='identity.link_claim',
                time=ctx.now,
                request_id=request.request_id,
                actor=None,
                subject=owner.id,
                resources=(),
                data={'name': row[2], 'invite_id': ident, 'request': body},
            )
        )
        return HandlerOutput(data={'invite_id': ident, 'name': row[2], 'state': 'claimed'})

    @op('identity.link_pending', obj(), effect='read', signature=True)
    async def pending(ctx, request, tx):
        owner = await owner_subject(ctx, request, tx)
        items = []
        for row in tx.rows(
            f'SELECT {COLUMNS} FROM link_invitations WHERE owner=? ORDER BY name',
            (owner.id,),
        ):
            state = row_state(row, ctx.now)
            if state not in {'claimed', 'released'}:
                continue
            items.append({
                'invite_id': row[0],
                'name': row[2],
                'minutes': row[3],
                'state': state,
                'request': claim_body(row),
            })
        return HandlerOutput(data={'items': items})

    @op(
        'identity.link_release',
        obj(
            {
                'invite_id': STRING,
                'grant_box': {'type': 'string', 'minLength': 1, 'maxLength': 65536},
            },
            ('invite_id', 'grant_box'),
        ),
        signature=True,
    )
    async def release(ctx, request, tx):
        owner = await owner_subject(ctx, request, tx)
        require(tx.setting('runtime_config', {}).get('accept_writes', True), 'writes_paused')
        ident = invite_id(request.arguments['invite_id'])
        row = load_row(tx, ident)
        require(row is not None and row[1] == owner.id, 'not_found')
        require(row[10] is None, 'link_revoked')
        require(row[6] is not None, 'link_not_claimed')
        envelope = validate_envelope(request.arguments['grant_box'].encode())
        recipient = public_from_recipient(claim_body(row)['encryption_recipient'])
        require(envelope['recipient'] == b64(recipient), 'wrong_decryption_recipient')
        tx.execute(
            """UPDATE link_invitations SET grant_box=?, released_at=?
            WHERE invite_id=? AND revoked_at IS NULL""",
            (request.arguments['grant_box'], wire(ctx.now), ident),
            write=True,
        )
        return HandlerOutput(data={'invite_id': ident, 'name': row[2], 'state': 'released'})

    @op('identity.link_collect', claim_schema, effect='read', anonymous_only=True)
    async def collect(ctx, request, tx):
        ident = invite_id(request.arguments['invite_id'])
        body = verify_claim(request.arguments)
        row = load_row(tx, ident)
        require(row is not None and row[6] == canonical(body).decode(), 'not_found')
        require(row[10] is None, 'link_revoked')
        data = {'invite_id': ident, 'name': row[2], 'state': row_state(row, ctx.now)}
        if row[8] is not None:
            data['grant_box'] = row[8]
            data['state'] = 'released'
        return HandlerOutput(data=data)

    @op('identity.link_close', obj({'invite_id': STRING}, ('invite_id',)), signature=True)
    async def close(ctx, request, tx):
        owner = await owner_subject(ctx, request, tx)
        ident = invite_id(request.arguments['invite_id'])
        row = load_row(tx, ident)
        require(row is not None and row[1] == owner.id, 'not_found')
        if row[10] is None:
            tx.execute(
                'UPDATE link_invitations SET revoked_at=? WHERE invite_id=? AND revoked_at IS NULL',
                (wire(ctx.now), ident),
                write=True,
            )
        return HandlerOutput(data={'invite_id': ident, 'name': row[2], 'state': 'revoked'})
