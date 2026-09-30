"""Device login and durable origin-bound sessions for browserless agents."""

import asyncio
import os
import stat
import sys
import time
import webbrowser
from datetime import timedelta

from msg.atomic_file import durable_write
from msg.client_upgrade import locked_state
from msg.core.codec import canonical, loads, parse_time, unb64, wire
from msg.core.errors import Failure, require
from msg.security.oauth import DEVICE_GRANT
from msg.transports.client import GraphQLTransport, HTTPTransport, MCPHTTPTransport


def read_session(state):
    path = state.file('oauth-session.json')
    if not path.exists() and not path.is_symlink():
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise Failure('unsafe_oauth_session') from None
    try:
        info = os.fstat(fd)
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_nlink == 1
            and info.st_mode & 0o077 == 0
            and info.st_size <= 16384,
            'unsafe_oauth_session',
        )
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            value = loads(stream.read(16385))
    finally:
        os.close(fd)
    require(
        isinstance(value, dict)
        and value.get('server') == state.server
        and value.get('client_id') == 'msg-cli',
        'oauth_session_server_mismatch',
    )
    return value


def save_session(client, value):
    require(
        value.get('token_type') == 'Bearer'
        and isinstance(value.get('expires_in'), int)
        and value['expires_in'] > 0
        and isinstance(value.get('subject_id'), str),
        'invalid_oauth_response',
    )
    require(
        client.state.subject is None or client.state.subject == value['subject_id'],
        'oauth_identity_mismatch',
    )
    token = value.get('access_token', '')
    id, sep, encoded = token.partition('.')
    require(
        sep and id.startswith('t_oauth_') and len(unb64(encoded, limit=32)) == 32,
        'invalid_oauth_response',
    )
    saved = dict(
        value,
        server=client.state.server,
        client_id='msg-cli',
        expires_at=wire(client.clock() + timedelta(seconds=value['expires_in'])),
    )
    durable_write(client.state.file('oauth-session.json'), canonical(saved), mode=0o600)
    client.state.data['subject_id'] = value['subject_id']
    client.state._save()
    return saved


def secure_transport(client):
    require(
        type(client.transport) in {HTTPTransport, GraphQLTransport, MCPHTTPTransport},
        'secure_channel_required',
    )
    from urllib.parse import urlsplit

    parsed = urlsplit(client.state.server)
    require(
        parsed.scheme == 'https'
        or parsed.hostname in {'localhost', '127.0.0.1', '::1', 'testserver'},
        'secure_channel_required',
    )


async def endpoint(client, path, data):
    secure_transport(client)
    try:
        return await client.transport._json('POST', path, body=data)
    except Failure as exc:
        # OAuth errors are standard string-valued errors, unlike MSG envelopes.
        raise exc


async def start_login(
    client, *, scope='openid profile msg.read msg.write offline_access', browser=False
):
    value = await endpoint(
        client, '/oauth/device_authorization', {'client_id': 'msg-cli', 'scope': scope}
    )
    require(
        all(
            isinstance(value.get(k), str)
            for k in ('device_code', 'user_code', 'verification_uri', 'verification_uri_complete')
        ),
        'invalid_oauth_response',
    )
    require(
        value['verification_uri'] == client.state.server + '/oauth/device'
        and value['verification_uri_complete'].startswith(value['verification_uri'] + '?'),
        'oauth_response_origin_mismatch',
    )
    if browser:
        webbrowser.open(value['verification_uri_complete'])
    return value


async def finish_login(client, pending):
    secure_transport(client)
    deadline = time.monotonic() + min(pending.get('expires_in', 600), 600)
    interval = max(5, pending.get('interval', 5))
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        try:
            value = await endpoint(
                client,
                '/oauth/token',
                {
                    'client_id': 'msg-cli',
                    'grant_type': DEVICE_GRANT,
                    'device_code': pending['device_code'],
                },
            )
        except Failure as exc:
            if exc.code == 'authorization_pending':
                continue
            if exc.code == 'slow_down':
                interval += 5
                continue
            raise
        with locked_state(client.state):
            save_session(client, value)
        return {'logged_in': True, 'subject_id': value['subject_id'], 'scope': value['scope']}
    raise Failure('expired_token')


async def refresh(client):
    secure_transport(client)
    with locked_state(client.state):
        current = read_session(client.state)
        require(current is not None, 'oauth_session_missing')
        if parse_time(current['expires_at']) > client.clock() + timedelta(seconds=30):
            return current
        require(current.get('refresh_token'), 'oauth_login_required')
        # A failed/lost response is ambiguous after rotation. Keep the local key,
        # require a fresh login, and never replay a consumed refresh token.
        if current.get('refresh_pending'):
            raise Failure('oauth_login_required')
        current['refresh_pending'] = True
        durable_write(client.state.file('oauth-session.json'), canonical(current), mode=0o600)
        value = await endpoint(
            client,
            '/oauth/token',
            {
                'client_id': 'msg-cli',
                'grant_type': 'refresh_token',
                'refresh_token': current['refresh_token'],
            },
        )
        return save_session(client, value)


async def logout(client):
    secure_transport(client)
    with locked_state(client.state):
        current = read_session(client.state)
        if current is not None:
            await endpoint(
                client,
                '/oauth/revoke',
                {
                    'client_id': 'msg-cli',
                    'token': current.get('refresh_token') or current['access_token'],
                },
            )
            from msg.client_tokens import remove_journal

            remove_journal(client.state.file('oauth-session.json'))
    return {'logged_out': True}


async def run_command(client, args):
    if args.command == 'login':
        pending = await start_login(client, scope=args.scope, browser=not args.no_browser)
        print(
            '打开 ' + pending['verification_uri'] + '，输入 ' + pending['user_code'],
            file=sys.stderr,
            flush=True,
        )
        return await finish_login(client, pending)
    if args.command == 'logout':
        return await logout(client)
    if args.action == 'status':
        current = read_session(client.state)
        return {
            'logged_in': current is not None and not current.get('refresh_pending'),
            'subject_id': current.get('subject_id') if current else None,
            'expires_at': current.get('expires_at') if current else None,
            'scope': current.get('scope') if current else None,
        }
    if args.action == 'request':
        return await client.call(
            'identity.oauth_request', {'user_code': args.user_code}, anonymous=True
        )
    return await client.call(
        'identity.oauth_approve',
        {'user_code': args.user_code, 'decision': 'deny' if args.action == 'deny' else 'approve'},
        signer=client.state.signer,
    )
