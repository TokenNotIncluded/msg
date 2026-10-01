"""Recipient-approved cross-service mail with pinned signing keys and durable replay fences.

No network access occurs in a transaction. Discovery and transport belong to the
client; receiving requires an explicit local recipient grant and an inner signature.
"""

from datetime import timedelta

from msg.core.codec import digest, parse_time, unb64, wire
from msg.core.errors import require
from msg.core.internet_address import (
    MAX_BODY_BYTES,
    account_address,
    address_parts,
    delivery_request_id,
    verify_envelope,
)
from msg.core.models import HandlerOutput
from msg.plugins.common import operation_id, resolve
from msg.plugins.schemas import IDENTIFIER, SIGNATURE, STRING, obj
from msg.security.crypto import key_id

ENVELOPE_FIELDS = {
    'version': {'const': 1},
    'message_id': {'type': 'string', 'pattern': '^[a-f0-9]{32}$'},
    'sender': STRING,
    'sender_subject': IDENTIFIER,
    'recipient': STRING,
    'recipient_subject': IDENTIFIER,
    'issued_at': {'type': 'string', 'format': 'date-time'},
    'expires_at': {'type': 'string', 'format': 'date-time'},
    'body': {'type': 'string', 'minLength': 1, 'maxLength': MAX_BODY_BYTES},
    'signature': SIGNATURE,
}
ENVELOPE = obj(ENVELOPE_FIELDS, tuple(ENVELOPE_FIELDS))
MAX_CONTACTS = 100
MAX_MESSAGES = 64
MAX_SEEN = 1024


def state_key(subject):
    return 'internet-mail:' + subject


def mail_state(tx, subject):
    return tx.setting(state_key(subject), {'contacts': {}, 'messages': [], 'seen': {}})


def install(app, op):
    async def owner(ctx, request, tx):
        subject = ctx.principal.subject
        require(subject is not None and ctx.principal.actor == subject, 'authentication_required')
        await app.authorizer.require_base(ctx.principal, operation_id(request), subject, tx)
        require((await tx.resource(subject)).state == 'active', 'not_found')
        return subject

    @op(
        'communication.internet_allow',
        obj(
            {'address': STRING, 'subject_id': IDENTIFIER, 'public_key': STRING},
            ('address', 'subject_id', 'public_key'),
        ),
        signature=True,
    )
    async def allow(ctx, request, tx):
        subject = await owner(ctx, request, tx)
        a = request.arguments
        handle, authority = address_parts(a['address'])
        address = handle + '@' + authority
        public = unb64(a['public_key'], limit=32)
        require(len(public) == 32, 'invalid_public_key')
        state = mail_state(tx, subject)
        require(
            address in state['contacts'] or len(state['contacts']) < MAX_CONTACTS,
            'internet_contacts_full',
        )
        state['contacts'][address] = {
            'subject_id': a['subject_id'],
            'public_key': a['public_key'],
            'key_id': key_id(public),
        }
        tx.set_setting(state_key(subject), state)
        return HandlerOutput(data={'address': address, 'key_id': key_id(public), 'allowed': True})

    @op('communication.internet_revoke', obj({'address': STRING}, ('address',)), signature=True)
    async def revoke(ctx, request, tx):
        subject = await owner(ctx, request, tx)
        handle, authority = address_parts(request.arguments['address'])
        address = handle + '@' + authority
        state = mail_state(tx, subject)
        state['contacts'].pop(address, None)
        tx.set_setting(state_key(subject), state)
        return HandlerOutput(data={'address': address, 'allowed': False})

    @op(
        'communication.internet_receive',
        obj({'envelope': ENVELOPE}, ('envelope',)),
        anonymous_only=True,
    )
    async def receive(ctx, request, tx):
        require(ctx.principal.method == 'anonymous', 'anonymous_only')
        require(tx.setting('runtime_config', {}).get('accept_writes', True), 'writes_paused')
        envelope = request.arguments['envelope']
        sender_handle, sender_authority = address_parts(envelope['sender'])
        require(
            envelope['sender'] == sender_handle + '@' + sender_authority, 'invalid_internet_address'
        )
        recipient_handle, recipient_authority = address_parts(envelope['recipient'])
        require(
            recipient_authority
            == address_parts(account_address('@aa', app.settings.service_url))[1],
            'internet_wrong_recipient',
        )
        subject = await resolve(tx, '/@' + recipient_handle)
        recipient = await tx.resource(subject)
        require(
            recipient.type == 'user'
            and recipient.state == 'active'
            and not tx.setting('identity_archived:' + subject)
            and subject == envelope['recipient_subject'],
            'internet_wrong_recipient',
        )
        state = mail_state(tx, subject)
        contact = state['contacts'].get(envelope['sender'])
        require(
            contact is not None and contact['subject_id'] == envelope['sender_subject'],
            'internet_sender_not_allowed',
        )
        verify_envelope(envelope, unb64(contact['public_key'], limit=32))
        issued, expires = parse_time(envelope['issued_at']), parse_time(envelope['expires_at'])
        require(
            issued <= ctx.now + timedelta(seconds=30)
            and issued < expires
            and expires <= issued + timedelta(minutes=10)
            and ctx.now < expires,
            'internet_message_expired',
        )
        require(len(envelope['body'].encode('utf-8')) <= MAX_BODY_BYTES, 'internet_body_too_large')
        require(request.request_id == delivery_request_id(envelope), 'internet_request_id_mismatch')
        state['seen'] = {
            k: v for k, v in state['seen'].items() if parse_time(v['expires_at']) > ctx.now
        }
        marker = digest([envelope['sender_subject'], envelope['sender'], envelope['message_id']])
        content_digest = digest(envelope)
        previous = state['seen'].get(marker)
        if previous is not None:
            require(previous['digest'] == content_digest, 'internet_message_conflict')
            return HandlerOutput(
                data={'message_id': envelope['message_id'], 'delivered': True, 'duplicate': True}
            )
        require(
            len(state['messages']) < MAX_MESSAGES and len(state['seen']) < MAX_SEEN,
            'internet_inbox_full',
        )
        state['messages'].append({'envelope': envelope, 'received_at': wire(ctx.now), 'id': marker})
        state['seen'][marker] = {'digest': content_digest, 'expires_at': envelope['expires_at']}
        tx.set_setting(state_key(subject), state)
        return HandlerOutput(
            data={'message_id': envelope['message_id'], 'delivered': True, 'duplicate': False}
        )

    @op(
        'communication.internet_inbox',
        obj({
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20},
            'offset': {'type': 'integer', 'minimum': 0, 'maximum': MAX_MESSAGES},
        }),
        effect='read',
    )
    async def inbox(ctx, request, tx):
        subject = await owner(ctx, request, tx)
        state = mail_state(tx, subject)
        offset, limit = request.arguments.get('offset', 0), request.arguments.get('limit', 20)
        messages = state['messages'][offset : offset + limit]
        next_offset = offset + limit if offset + limit < len(state['messages']) else None
        return HandlerOutput(
            data={
                'contacts': state['contacts'],
                'messages': messages,
                'total': len(state['messages']),
                'next_offset': next_offset,
            }
        )

    @op('communication.internet_delete', obj({'id': STRING}, ('id',)), signature=True)
    async def delete(ctx, request, tx):
        subject = await owner(ctx, request, tx)
        state = mail_state(tx, subject)
        state['messages'] = [m for m in state['messages'] if m['id'] != request.arguments['id']]
        tx.set_setting(state_key(subject), state)
        return HandlerOutput(data={'id': request.arguments['id'], 'deleted': True})
