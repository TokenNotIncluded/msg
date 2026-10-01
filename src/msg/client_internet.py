"""Cross-service discovery and signed delivery without sharing local credentials."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx

from msg.core.codec import canonical, loads, unb64, wire
from msg.core.errors import Failure, require
from msg.core.internet_address import (
    DELIVERY_REL,
    KEYS_REL,
    MAX_BODY_BYTES,
    PROFILE_REL,
    PURPOSE,
    SUBJECT_PROPERTY,
    account_address,
    address_parts,
    delivery_request_id,
    envelope_bytes,
)
from msg.core.packet import decode_result
from msg.core.requests import request_for
from msg.security.crypto import key_id


async def json_request(http, method, url, *, body=None):
    try:
        async with http.stream(
            method,
            url,
            content=canonical(body) if body is not None else None,
            headers={
                'Accept': 'application/jrd+json, application/json',
                'Content-Type': 'application/json',
            },
            follow_redirects=False,
        ) as response:
            require(not response.is_redirect, 'redirect_not_allowed')
            data = bytearray()
            async for chunk in response.aiter_bytes():
                require(len(data) + len(chunk) <= 131072, 'response_too_large')
                data.extend(chunk)
            value = loads(bytes(data))
            require(isinstance(value, dict), 'invalid_server_response')
            if response.status_code >= 400:
                error = value.get('error')
                code = error.get('code') if isinstance(error, dict) else None
                raise Failure(
                    code if isinstance(code, str) else 'internet_remote_error',
                    retryable=response.status_code >= 500,
                )
            return value
    except httpx.HTTPError as exc:
        raise Failure('internet_transport_uncertain', retryable=True) from exc


async def discover(http, address, *, allow_http=False):
    handle, authority = address_parts(address)
    address = handle + '@' + authority
    origin = ('http' if allow_http else 'https') + '://' + authority
    value = await json_request(
        http, 'GET', origin + '/.well-known/webfinger?' + urlencode({'resource': 'acct:' + address})
    )
    require(value.get('subject') == 'acct:' + address, 'internet_discovery_mismatch')
    require(isinstance(value.get('properties'), dict), 'internet_discovery_mismatch')
    subject = value['properties'].get(SUBJECT_PROPERTY)
    require(isinstance(subject, str) and subject.startswith('u_'), 'internet_discovery_mismatch')
    links = value.get('links')
    require(isinstance(links, list) and len(links) <= 32, 'internet_discovery_mismatch')

    def link(rel, path):
        matches = [
            item.get('href') for item in links if isinstance(item, dict) and item.get('rel') == rel
        ]
        require(matches == [origin + path], 'internet_discovery_mismatch')
        return matches[0]

    profile = link(PROFILE_REL, '/@' + handle)
    keys_url = link(KEYS_REL, '/@' + handle + '/k')
    delivery = link(DELIVERY_REL, '/-/p/communication.internet_receive')
    keys = await json_request(http, 'GET', keys_url)
    require(
        keys.get('subject_id') == subject
        and isinstance(keys.get('keys'), list)
        and all(isinstance(k, dict) for k in keys['keys']),
        'internet_discovery_mismatch',
    )
    active = [
        k
        for k in keys['keys']
        if k.get('primary')
        and k.get('retired_at') is None
        and k.get('algorithm') == 'ed25519'
        and k.get('purpose') == 'identity'
    ]
    require(len(active) == 1, 'internet_identity_key_unavailable')
    key = active[0]
    require(
        isinstance(key.get('public_key'), str) and isinstance(key.get('key_id'), str),
        'internet_discovery_mismatch',
    )
    public = unb64(key['public_key'], limit=32)
    require(len(public) == 32 and key['key_id'] == key_id(public), 'internet_discovery_mismatch')
    return {
        'address': address,
        'subject_id': subject,
        'public_key': key['public_key'],
        'key_id': key['key_id'],
        'profile': profile,
        'delivery': delivery,
        'service': origin,
    }


def make_envelope(sender, recipient, body, signer, *, message_id=None, now=None):
    require(
        isinstance(body, str) and 0 < len(body.encode('utf-8')) <= MAX_BODY_BYTES,
        'internet_body_too_large',
    )
    require(
        signer is not None and signer.key_id == sender['key_id'], 'internet_identity_key_required'
    )
    now = now or datetime.now(UTC)
    envelope = {
        'version': 1,
        'message_id': message_id or uuid4().hex,
        'sender': sender['address'],
        'sender_subject': sender['subject_id'],
        'recipient': recipient['address'],
        'recipient_subject': recipient['subject_id'],
        'issued_at': wire(now),
        'expires_at': wire(now + timedelta(minutes=10)),
        'body': body,
    }
    envelope['signature'] = wire(signer.sign(envelope_bytes(envelope), purpose=PURPOSE))
    return envelope


async def deliver(http, recipient, envelope, *, now=None):
    require(
        recipient['delivery'] == recipient['service'] + '/-/p/communication.internet_receive'
        and urlsplit(recipient['service']).netloc == address_parts(envelope['recipient'])[1]
        and recipient['subject_id'] == envelope['recipient_subject'],
        'internet_discovery_mismatch',
    )
    packet = request_for(
        'communication.internet_receive',
        {'envelope': envelope},
        recipient['service'],
        request_id=delivery_request_id(envelope),
        expires_at=(now or datetime.now(UTC)) + timedelta(minutes=3),
    )
    # The signed envelope is stable across retries; the outer anonymous request may change.
    result = await json_request(http, 'POST', recipient['delivery'], body=wire(packet))
    result = decode_result(result)
    require(
        result.operation == 'communication.internet_receive'
        and isinstance(result.data, Mapping)
        and result.status == 'ok'
        and result.data.get('message_id') == envelope['message_id']
        and result.data.get('delivered') is True,
        'internet_delivery_failed',
    )
    return result


async def run_command(client, args):
    if args.action in {'allow', 'send', 'revoke', 'delete'}:
        require(client.state.signer is not None, 'internet_identity_key_required')
    if args.action == 'inbox':
        return await client.call(
            'communication.internet_inbox', {'limit': args.limit, 'offset': args.offset}
        )
    if args.action == 'revoke':
        return await client.call(
            'communication.internet_revoke', {'address': args.address}, signer=client.state.signer
        )
    if args.action == 'delete':
        return await client.call(
            'communication.internet_delete', {'id': args.id}, signer=client.state.signer
        )
    async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False) as http:
        if args.action == 'retry':
            from msg.client import private_client_json

            require(
                len(args.message_id) == 32
                and all(c in '0123456789abcdef' for c in args.message_id),
                'invalid_message_id',
            )
            pending = private_client_json(
                client.state.file('internet-send-' + args.message_id + '.json')
            )
            require(pending is not None, 'internet_pending_message_not_found')
            envelope = pending['envelope']
            require(envelope['message_id'] == args.message_id, 'internet_discovery_mismatch')
            remote = await discover(http, envelope['recipient'], allow_http=args.allow_http)
            require(
                remote['subject_id'] == envelope['recipient_subject'], 'internet_discovery_mismatch'
            )
            return await deliver(http, remote, envelope)
        if args.action in {'resolve', 'allow'}:
            remote = await discover(http, args.address, allow_http=args.allow_http)
            if args.action == 'resolve':
                return remote
            return await client.call(
                'communication.internet_allow',
                {
                    'address': remote['address'],
                    'subject_id': remote['subject_id'],
                    'public_key': remote['public_key'],
                },
                signer=client.state.signer,
            )
        identity = client.checked(
            await client.call(
                'discovery.get',
                {
                    'id': client.state.subject,
                    'fields': ['id', 'name'],
                },
            )
        )
        origin = urlsplit(client.state.server)
        sender = await discover(
            http,
            account_address(identity.data['name'], client.state.server),
            allow_http=origin.scheme == 'http' and args.allow_http,
        )
        require(sender['subject_id'] == client.state.subject, 'internet_discovery_mismatch')
        recipient = await discover(http, args.address, allow_http=args.allow_http)
        envelope = make_envelope(sender, recipient, args.body, client.state.signer)
        # Save the exact signed envelope before transport so a lost response is retryable.
        from msg.atomic_file import durable_write

        filename = 'internet-send-' + envelope['message_id'] + '.json'
        durable_write(
            client.state.file(filename),
            canonical({'recipient': recipient, 'envelope': envelope}),
            mode=0o600,
        )
        try:
            return await deliver(http, recipient, envelope)
        except Failure as exc:
            raise Failure(
                exc.code,
                retryable=exc.retryable,
                details={
                    'message_id': envelope['message_id'],
                    'pending_file': str(client.state.file(filename)),
                    'allow_http': args.allow_http,
                },
            ) from exc
