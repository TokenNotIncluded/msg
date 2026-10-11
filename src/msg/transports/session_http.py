"""Native key-approved browser sessions. Retired identity providers fail closed."""

import hashlib
import hmac
import re
from base64 import b64encode
from datetime import timedelta
from html import escape
from urllib.parse import parse_qsl, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from msg.core.codec import b64, loads, wire
from msg.core.errors import Failure, require
from msg.core.requests import request_for
from msg.security.sessions import BrowserSessionService, get, save, state_id
from msg.transports.browser_login import LOGIN_POLL_SCRIPT
from msg.transports.browser_navigation import HASH as NAVIGATION_HASH, TAG as NAVIGATION_TAG
from msg.transports.browser_style import BRAND_LINK, PREFERENCES, SKIP_LINK, THEME_CSS
from msg.transports.http_common import body_bytes
from msg.transports.url_safety import require_matching_host, require_safe_request_target
from msg.transports.webmcp import WEBMCP_HASH, WEBMCP_TAG

HEADERS = {
    'Cache-Control': 'no-store',
    'Pragma': 'no-cache',
    'Referrer-Policy': 'no-referrer',
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; "
    f"script-src 'sha256-{WEBMCP_HASH}' 'sha256-{NAVIGATION_HASH}'; connect-src 'self'; img-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'",
}


def json(value, status=200):
    return JSONResponse(value, status_code=status, headers=HEADERS)


def page(title, body, *, script=None):
    """Render trusted form markup; authorize only the explicitly supplied script."""
    headers = {**HEADERS, 'Referrer-Policy': 'strict-origin'}
    script_tag = ''
    if script is not None:
        pinned = b64encode(hashlib.sha256(script.encode()).digest()).decode()
        headers['Content-Security-Policy'] = headers['Content-Security-Policy'].replace(
            'script-src ', f"script-src 'sha256-{pinned}' "
        )
        script_tag = '<script>' + script + '</script>'
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>' + escape(title) + '</title>'
        '<link rel="icon" href="/favicon.png">'
        '<style>'
        + THEME_CSS
        + '</style></head><body class="page-auth">'
        + SKIP_LINK
        + '<header class="site-header">'
        + BRAND_LINK
        + '<nav aria-label="Account"><a href="/" data-i18n="home">Home</a>'
        '<a href="/register" data-i18n="register">Register</a></nav></header>'
        '<main><div class="toolbar">'
        + PREFERENCES
        + '<a class="raw-link" href="?format=raw">raw</a></div>'
        '<div id="content" tabindex="-1"><h1>'
        + escape(title)
        + '</h1>'
        + body
        + '</div></main>'
        + WEBMCP_TAG
        + NAVIGATION_TAG
        + script_tag
        + '</body></html>',
        # Form POSTs need a non-opaque Origin for the same-origin CSRF fence.
        # no-referrer makes navigation POST origins null in Chromium.
        headers=headers,
    )


def fields(items):
    return ''.join(
        '<input type="hidden" name="'
        + escape(k, quote=True)
        + '" value="'
        + escape(v, quote=True)
        + '">'
        for k, v in items.items()
    )


def csrf(cookie):
    return b64(hmac.digest(cookie.encode(), b'msg-key-browser-csrf-v1', 'sha256'))


def unique(pairs):
    require(len({k for k, _ in pairs}) == len(pairs), 'invalid_request')
    return dict(pairs)


async def form(request):
    raw = await body_bytes(request, 16384)
    content = request.headers.get('content-type', '').split(';')[0]
    if content == 'application/json':
        value = loads(raw)
        require(isinstance(value, dict), 'invalid_request')
    else:
        require(content == 'application/x-www-form-urlencoded', 'invalid_request')
        try:
            value = unique(parse_qsl(raw.decode(), keep_blank_values=True, max_num_fields=20))
        except UnicodeError, ValueError:
            raise Failure('invalid_request') from None
    require(
        all(isinstance(k, str) and isinstance(v, str) and len(v) <= 4096 for k, v in value.items()),
        'invalid_request',
    )
    return value


class BrowserSessionBoundary:
    def __init__(self, app, *, service):
        self.app, self.service = app, service
        self.sessions = BrowserSessionService(service)
        self.secure = urlsplit(service.settings.service_url).scheme == 'https'
        self.session_cookie = '__Host-msg_session' if self.secure else 'msg_session'
        self.login_cookie = '__Host-msg_login' if self.secure else 'msg_login'

    def cookie(self, response, name, value, ttl):
        response.set_cookie(
            name, value, max_age=ttl, secure=self.secure, httponly=True, samesite='lax', path='/'
        )

    def require_csrf(self, request, args, name):
        value = request.cookies.get(name, '')
        supplied = args.get('csrf', '')
        require(
            isinstance(supplied, str)
            and supplied.isascii()
            and value
            and request.headers.get('origin') == self.service.settings.service_url
            and hmac.compare_digest(supplied, csrf(value)),
            'invalid_request',
        )
        return value

    def require_origin(self, request):
        require_safe_request_target(
            request.scope.get('raw_path') or request.url.path.encode(),
            request.scope.get('query_string', b''),
            maximum=self.service.settings.server.limits.max_path_bytes,
        )
        require_matching_host(
            request.headers.getlist('host'), urlsplit(self.service.settings.service_url)
        )
        require(
            'x-http-method-override' not in request.headers
            and 'x-method-override' not in request.headers,
            'method_not_allowed',
        )
        require(
            request.headers.get('origin') in {None, self.service.settings.service_url},
            'forbidden_origin',
        )

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        path = request.url.path
        # No fallback from a retired identity endpoint or bearer to an anonymous
        # operation. This also invalidates old clients after a database restore.
        retired = path in {
            '/oauth',
            '/account/login-methods',
            '/.well-known/openid-configuration',
        } or path.startswith(('/oauth/', '/-/login/', '/login/', '/.well-known/oauth-'))
        if retired:
            await json({'error': 'identity_protocol_removed'}, 410)(scope, receive, send)
            return
        authorization = request.headers.getlist('authorization')
        if authorization:
            # Standard Git clients carry native, key-issued MSG credentials in
            # Basic auth. The Git adapter still validates their current scope,
            # signing source, revocation and request id; this is not OAuth.
            native_git = re.fullmatch(
                r'/-/git/[A-Za-z0-9_-]{1,128}(?:\.git)?/(?:info/refs|git-receive-pack|info/lfs/objects(?:/batch|/[0-9a-f]{64}/[0-9]+))'
                r'|/[@&][^/]+/[^/]+\.git/(?:info/refs|git-upload-pack|HEAD|info/lfs/objects(?:/batch|/[0-9a-f]{64}))',
                path,
            )
            if len(authorization) == 1 and authorization[0].startswith('Basic ') and native_git:
                await self.app(scope, receive, send)
                return
            await json({'error': 'signature_required'}, 401)(scope, receive, send)
            return
        session_path = path == '/login' or path.startswith('/session/')
        cookie = request.cookies.get(self.session_cookie, '')
        browser_read = (
            cookie
            and request.method in {'GET', 'HEAD'}
            and self.sessions.config.enabled
            and not path.startswith('/-/')
            and not request.headers.get('x-msg-request')
        )
        if not session_path:
            if browser_read:
                try:
                    self.require_origin(request)
                    async with self.service.metadata.transaction(write=False) as tx:
                        credentials = await self.sessions.browser_credentials(tx, cookie)
                    scope.setdefault('state', {})['msg_browser_credentials'] = credentials
                except Failure as exc:
                    if exc.code not in {'invalid_grant', 'credential_not_found'}:
                        status = (
                            503
                            if exc.code in {'recovery_quarantined', 'recovery_runtime_stale'}
                            else 400
                        )
                        await json({'error': exc.code}, status)(scope, receive, send)
                        return
                    scope.setdefault('state', {})['msg_browser_expired'] = True

                async def private_send(message):
                    if message['type'] == 'http.response.start':
                        headers = [
                            (k, v) for k, v in message['headers'] if k.lower() != b'cache-control'
                        ]
                        message = dict(
                            message,
                            headers=headers
                            + [(b'cache-control', b'private, no-store'), (b'vary', b'Cookie')],
                        )
                    await send(message)

                await self.app(scope, receive, private_send)
            else:
                await self.app(scope, receive, send)
            return
        try:
            self.require_origin(request)
            require(not request.headers.get('x-msg-request'), 'ambiguous_credentials')
            if (
                path == '/login'
                and request.method in {'GET', 'HEAD'}
                and request.query_params.get('format') == 'raw'
            ):
                require(
                    request.query_params.multi_items() == [('format', 'raw')], 'invalid_request'
                )
                response = PlainTextResponse(
                    '# Sign in with your key\n\nOpen /login. Inspect its code with `msg auth request CODE`, '
                    'then use `msg auth approve CODE` on your own device. Never upload a private key. '
                    'New identities: /register.\n',
                    headers=HEADERS,
                )
                if request.method == 'HEAD':
                    response.body = b''
            else:
                require(not request.query_params, 'invalid_request')
                require(self.sessions.config.enabled, 'browser_sessions_disabled')
                response = await self.dispatch(request)
        except Failure as exc:
            status = (
                503
                if exc.code in {'recovery_quarantined', 'recovery_runtime_stale'}
                else (405 if exc.code == 'method_not_allowed' else 400)
            )
            response = json({'error': exc.code}, status)
        except Exception:
            response = json({'error': 'server_error'}, 500)
        await response(scope, receive, send)

    async def dispatch(self, request):
        path, now = request.url.path, self.service.clock()
        if path == '/session/post-action':
            from msg.security.browser_actions import BROWSER_POST_WRITES

            require(request.method == 'POST', 'method_not_allowed')
            require(
                not request.query_params
                and not request.headers.get('authorization')
                and not request.headers.get('x-msg-request'),
                'ambiguous_credentials',
            )
            args = loads(await body_bytes(request, 65536))
            require(
                isinstance(args, dict)
                and set(args)
                <= {
                    'csrf',
                    'operation',
                    'id',
                    'body',
                    'revision',
                    'request_id',
                    'kind',
                    'note',
                    'generation',
                    'name',
                    'svg',
                    'text',
                },
                'invalid_request',
            )
            cookie = self.require_csrf(request, args, self.session_cookie)
            operation = args.get('operation')
            require(
                isinstance(operation, str) and operation in BROWSER_POST_WRITES, 'permission_denied'
            )
            require(
                isinstance(args.get('request_id'), str) and 0 < len(args['request_id']) <= 128,
                'invalid_request',
            )
            async with self.service.metadata.transaction(write=False) as tx:
                subject, credential, token = await self.sessions.browser_credentials(tx, cookie)
            arguments = {'id': args.get('id')}
            expected = ()
            if operation == 'content.public_board_update':
                arguments = {k: args[k] for k in ('generation', 'svg', 'text') if k in args}
            if operation in {'content.post_create', 'content.post_edit'}:
                from msg.core.wiki import in_wiki
                from msg.plugins.common import resolve

                require(isinstance(args.get('body'), str), 'invalid_request')
                async with self.service.metadata.transaction(write=False) as tx:
                    resource = await tx.resource(await resolve(tx, args.get('id')))
                    require(await in_wiki(tx, resource), 'wiki_only')
                if operation == 'content.post_create':
                    require(resource.type == 'topic', 'not_a_topic')
                    arguments = {'parent': resource.id, 'body': args['body']}
                    if 'name' in args:
                        arguments['name'] = args['name']
                else:
                    require(type(args.get('generation')) is int, 'expected_generation_required')
                    require(isinstance(args.get('revision'), str), 'revision_required')
                    arguments = {
                        'id': resource.id,
                        'body': args['body'],
                        'expected_revision': args['revision'],
                    }
                    expected = ((resource.id, args['generation']),)
            if operation in {'discussion.reply', 'discussion.fork'}:
                require(
                    isinstance(args.get('body'), str) and bool(args['body'].strip()),
                    'invalid_request',
                )
                arguments = {
                    'target': {'id': args.get('id'), 'revision': args.get('revision')},
                    'body': args['body'],
                }
            if operation == 'discussion.prove':
                require(isinstance(args.get('revision'), str), 'proof_revision_required')
                # Resolve only the displayed revision, never silently attest the latest.
                from msg.core.models import ResourceRef

                async with self.service.metadata.transaction(write=False) as tx:
                    revision = await tx.revision(
                        ResourceRef(id=args.get('id'), revision=args['revision'])
                    )
                arguments = {
                    'target': {'id': args.get('id'), 'revision': args['revision']},
                    'digest': revision.content.digest,
                    'kind': args.get('kind'),
                    'note': args.get('note', ''),
                }
            packet = request_for(
                operation,
                arguments,
                self.service.settings.service_url,
                subject=subject,
                token=(credential, token),
                source='manual',
                expected=expected,
                request_id=args['request_id'],
                expires_at=now + timedelta(minutes=3),
            )
            result = await self.service.executor.execute(packet, entry='network')
            return json(wire(result), 200 if result.error is None else 403)
        if request.method == 'POST' or (path == '/login' and request.method == 'GET'):
            async with self.service.metadata.transaction(write=True) as tx:
                self.sessions.rate(tx, request.client.host if request.client else '')
        if path == '/login':
            require(request.method == 'GET', 'method_not_allowed')
            async with self.service.metadata.transaction(write=True) as tx:
                value, code, _ = self.sessions.pending(tx)
            response = page(
                'Sign in with your key',
                '<p>Your public key is your identity. Your private key stays on your device.</p>'
                '<p>Inspect this request with <code>msg auth request ' + escape(code) + '</code>. '
                'Then approve only the browser sign-in that you started:</p>'
                '<code class="approval-code">msg auth approve ' + escape(code) + '</code>'
                '<p>This temporary session can read your content and use permitted post actions. '
                'It cannot change your identity, keys, or server permissions.</p>'
                '<p><a href="/register">Create an identity with local keys</a></p>'
                '<p id="status" role="status" aria-live="polite" data-i18n="approval_wait" '
                'data-csrf="' + escape(csrf(value), quote=True) + '">Waiting for approval…</p>'
                '<noscript><p>JavaScript is needed to complete this browser sign-in. '
                'The <a href="/register">CLI</a> works without it.</p></noscript>',
                script=LOGIN_POLL_SCRIPT,
            )
            self.cookie(response, self.login_cookie, value, 600)
            return response
        if path == '/session/poll':
            require(request.method == 'POST', 'method_not_allowed')
            args = await form(request)
            require(set(args) == {'csrf'}, 'invalid_request')
            value = self.require_csrf(request, args, self.login_cookie)
            async with self.service.metadata.transaction(write=True) as tx:
                self.sessions.fence(tx)
                logged, error = await self.sessions.pending_result(tx, value)
            response = json({'error': error}, 400) if error else json({'logged_in': True})
            if not error:
                self.cookie(
                    response,
                    self.session_cookie,
                    logged['cookie'],
                    int((logged['expires'] - now).total_seconds()),
                )
                response.delete_cookie(
                    self.login_cookie, path='/', secure=self.secure, httponly=True
                )
            return response
        if path == '/session/logout':
            if request.method == 'GET':
                cookie = request.cookies.get(self.session_cookie, '')
                async with self.service.metadata.transaction(write=False) as tx:
                    await self.sessions.session(tx, cookie)
                return page(
                    'Sign out of MSG',
                    '<form method="post" action="/session/logout">'
                    + fields({'csrf': csrf(cookie)})
                    + '<button>Confirm sign-out</button></form>',
                )
            require(request.method == 'POST', 'method_not_allowed')
            args = await form(request)
            require(set(args) == {'csrf'}, 'invalid_request')
            cookie = self.require_csrf(request, args, self.session_cookie)
            async with self.service.metadata.transaction(write=True) as tx:
                self.sessions.fence(tx)
                _, body = get(tx, state_id('session', cookie), now)
                body['revoked'] = True
                save(tx, state_id('session', cookie), body)
            response = json({'logged_out': True})
            response.delete_cookie(self.session_cookie, path='/', secure=self.secure, httponly=True)
            return response
        raise Failure('invalid_request')
