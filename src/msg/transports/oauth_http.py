"""Browser consent, persistent HttpOnly sessions and standard OAuth endpoints."""

import hashlib
import hmac
import os
from base64 import b64encode
from dataclasses import replace
from datetime import timedelta
from html import escape
from urllib.parse import parse_qsl, urlencode, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from msg.core.codec import b64, canonical, loads, wire
from msg.core.errors import Failure, require
from msg.core.models import TokenProof
from msg.core.requests import request_for
from msg.oauth_config import SCOPES
from msg.security.oauth import (
    DEVICE_GRANT,
    OAuthService,
    get,
    resource_execution,
    save,
    secret,
    state_id,
)
from msg.transports.browser_login import LOGIN_POLL_SCRIPT
from msg.transports.browser_style import BRAND_LINK, PREFERENCES, SKIP_LINK, THEME_CSS
from msg.transports.http_common import body_bytes
from msg.transports.mcp_auth import (
    MCP_PATHS,
    PROTECTED_RESOURCE_PATHS,
    authenticate_mcp,
    challenge,
    protected_resource_metadata,
    require_unmixed_mcp,
    resource_uri,
)
from msg.transports.packet import decode_packet
from msg.transports.url_safety import require_matching_host, require_safe_request_target
from msg.transports.webmcp import WEBMCP_HASH, WEBMCP_TAG

HEADERS = {
    'Cache-Control': 'no-store',
    'Pragma': 'no-cache',
    'Referrer-Policy': 'no-referrer',
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; "
    f"script-src 'sha256-{WEBMCP_HASH}'; connect-src 'self'; img-src 'self'; form-action 'self'; "
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
    return b64(hmac.digest(cookie.encode(), b'msg-oauth-browser-csrf-v1', 'sha256'))


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


class OAuthBoundary:
    def __init__(self, app, *, service):
        self.app, self.service = app, service
        self.oauth = OAuthService(service)
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

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        path = request.url.path
        oauth_path = (
            path == '/login'
            or path.startswith('/oauth/')
            or path
            in {
                '/.well-known/oauth-authorization-server',
                '/.well-known/openid-configuration',
            }
            or path in PROTECTED_RESOURCE_PATHS
        )
        bearer = request.headers.get('authorization')
        mcp_path = path in MCP_PATHS
        if mcp_path:
            scope.setdefault('state', {})['msg_mcp_identity'] = None
            if bearer is None:
                # Keep signed/anonymous MCP on the shared HTTP ingress policy,
                # including its explicit aliases and denial status. No cookie fallback.
                await self.app(scope, receive, send)
                return
        if not oauth_path and not mcp_path and not (bearer and path.startswith('/-/p/')):
            cookie = request.cookies.get(self.session_cookie, '')
            browser_read = (
                cookie
                and request.method in {'GET', 'HEAD'}
                and self.oauth.config.enabled
                and not path.startswith('/-/')
                and not request.headers.get('x-msg-request')
                and not bearer
            )
            if browser_read:
                try:
                    require_safe_request_target(
                        scope.get('raw_path') or path.encode(),
                        scope.get('query_string', b''),
                        maximum=self.service.settings.server.limits.max_path_bytes,
                    )
                    require_matching_host(
                        request.headers.getlist('host'), urlsplit(self.service.settings.service_url)
                    )
                    require(
                        request.headers.get('origin') in {None, self.service.settings.service_url},
                        'forbidden_origin',
                    )
                    async with self.service.metadata.transaction(write=False) as tx:
                        credentials = await self.oauth.browser_credentials(tx, cookie)
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
                            + [
                                (b'cache-control', b'private, no-store'),
                                (b'vary', b'Cookie'),
                            ],
                        )
                    await send(message)

                await self.app(scope, receive, private_send)
            else:
                await self.app(scope, receive, send)
            return
        try:
            raw = scope.get('raw_path') or path.encode()
            require_safe_request_target(
                raw,
                scope.get('query_string', b''),
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
            origin = request.headers.get('origin')
            require(
                origin is None or origin == self.service.settings.service_url, 'forbidden_origin'
            )
            if mcp_path:
                require(request.method == 'POST', 'method_not_allowed')
                require(
                    len(request.headers.getlist('authorization')) == 1
                    and not request.headers.get('x-msg-request'),
                    'ambiguous_credentials',
                )
                identity = await authenticate_mcp(self.service, bearer)
                raw_body = await body_bytes(
                    request, self.service.settings.server.limits.max_request_bytes
                )
                require_unmixed_mcp(loads(raw_body))
                scope['state']['msg_mcp_identity'] = identity
                sent = False

                async def mcp_receive():
                    nonlocal sent
                    if sent:
                        return await receive()
                    sent = True
                    return {'type': 'http.request', 'body': raw_body, 'more_body': False}

                rebound_scope = dict(
                    scope,
                    headers=[
                        (k, v)
                        for k, v in scope['headers']
                        if k.lower()
                        not in {b'content-length', b'content-encoding', b'transfer-encoding'}
                    ]
                    + [(b'content-length', str(len(raw_body)).encode())],
                )
                with resource_execution(resource_uri(self.service)):
                    await self.app(rebound_scope, mcp_receive, send)
                return
            if not oauth_path:
                require(request.method == 'POST', 'method_not_allowed')
                packet = decode_packet(
                    await body_bytes(
                        request, self.service.settings.server.limits.max_request_bytes
                    ),
                    self.service.settings.server.limits.max_request_bytes,
                )
                require(packet.proof is None, 'ambiguous_credentials')
                async with self.service.metadata.transaction(write=False) as tx:
                    self.service.runtime_generation.require_current(tx)
                    credential = await self.oauth.bearer(tx, self.bearer_value(request))
                require(packet.subject == credential.subject_id, 'subject_mismatch')
                from msg.core.codec import unb64

                packet = replace(
                    packet,
                    proof=TokenProof(
                        credential_id=credential.id,
                        token=unb64(self.bearer_value(request).partition('.')[2], limit=32),
                    ),
                )
                data = canonical(packet)
                sent = False

                async def bound_receive():
                    nonlocal sent
                    if sent:
                        return await receive()
                    sent = True
                    return {'type': 'http.request', 'body': data, 'more_body': False}

                # The canonical packet now contains a proof and is no longer gzip.
                # Forward matching framing, without mutating the incoming scope.
                rebound_scope = dict(
                    scope,
                    headers=[
                        (k, v)
                        for k, v in scope['headers']
                        if k.lower()
                        not in {b'content-length', b'content-encoding', b'transfer-encoding'}
                    ]
                    + [(b'content-length', str(len(data)).encode())],
                )
                await self.app(rebound_scope, bound_receive, send)
                return
            if (path.startswith('/oauth/') or path == '/login') and (
                request.method in {'GET', 'HEAD'} and request.query_params.get('format') == 'raw'
            ):
                from starlette.responses import PlainTextResponse

                require(
                    request.query_params.multi_items() == [('format', 'raw')], 'invalid_request'
                )
                response = PlainTextResponse(
                    '# Sign in to MSG\n\nOpen /login, then confirm its code with `msg auth approve XXXXXXXX`.\n\nUse your registered CLI profile. New users: /register.\n',
                    headers=HEADERS,
                )
                if request.method == 'HEAD':
                    response.body = b''
                await response(scope, receive, send)
                return
            require(self.oauth.config.enabled, 'oauth_disabled')
            response = await self.dispatch(request)
        except Failure as exc:
            status = (
                503
                if exc.code in {'recovery_quarantined', 'recovery_runtime_stale'}
                else (405 if exc.code == 'method_not_allowed' else 400)
            )
            response = json({'error': exc.code}, status)
            if exc.code == 'invalid_token':
                response.status_code = 401
                response.headers['WWW-Authenticate'] = (
                    challenge(self.service, 'invalid_token', ('msg.mcp.read',))
                    if mcp_path
                    else 'Bearer error="invalid_token"'
                )
        except Exception:
            response = json({'error': 'server_error'}, 500)
        await response(scope, receive, send)

    @staticmethod
    def bearer_value(request):
        value = request.headers.get('authorization', '')
        require(value.startswith('Bearer ') and len(value) <= 300, 'invalid_token')
        return value[7:]

    async def dispatch(self, request):
        path, now = request.url.path, self.service.clock()
        if path == '/oauth/post-action':
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
                subject, credential, token = await self.oauth.browser_credentials(tx, cookie)
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
        if path.startswith('/.well-known/') or path == '/oauth/jwks':
            require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
            base = self.service.settings.service_url
            if path in PROTECTED_RESOURCE_PATHS:
                return json(protected_resource_metadata(self.service))
            if path == '/oauth/jwks':
                return json({'keys': [self.oauth.jwk()]})
            return json({
                'issuer': base,
                'authorization_response_iss_parameter_supported': True,
                'authorization_endpoint': base + '/oauth/authorize',
                'token_endpoint': base + '/oauth/token',
                'userinfo_endpoint': base + '/oauth/userinfo',
                'jwks_uri': base + '/oauth/jwks',
                'revocation_endpoint': base + '/oauth/revoke',
                'device_authorization_endpoint': base + '/oauth/device_authorization',
                'response_types_supported': ['code'],
                'subject_types_supported': ['public'],
                'id_token_signing_alg_values_supported': ['EdDSA'],
                'grant_types_supported': ['authorization_code', 'refresh_token', DEVICE_GRANT],
                'token_endpoint_auth_methods_supported': ['none'],
                'code_challenge_methods_supported': ['S256'],
                'scopes_supported': sorted(SCOPES),
            })
        # Rate limits commit independently, including rejected code guesses.
        # Userinfo is a read: do not mutate retained OAuth state during GET or
        # POST reads. Authentication still checks the current source/family.
        if path != '/oauth/userinfo':
            async with self.service.metadata.transaction(write=True) as tx:
                self.oauth.rate(tx, request.client.host if request.client else '')
        if path in {'/login', '/oauth/login'}:
            require(request.method == 'GET', 'method_not_allowed')
            async with self.service.metadata.transaction(write=True) as tx:
                self.oauth.fence(tx)
                value, code, _ = self.oauth.pending(
                    tx, kind='login', scope='openid profile msg.read msg.write'
                )
            response = page(
                'Sign in to MSG',
                '<p>Approve this browser sign-in from a signed-in CLI:</p>'
                '<code class="approval-code">msg auth approve ' + escape(code) + '</code>'
                '<p>This browser can read content, your inbox and direct messages, and explicitly like, bookmark, follow authors, comment on posts, or create and edit shared wiki articles within your permissions. It cannot administer your identity or the server. Only approve if you opened this page.</p>'
                '<p><a href="/register">Register using the CLI</a></p>'
                '<p id="status" role="status" aria-live="polite" data-i18n="approval_wait" '
                'data-csrf="' + escape(csrf(value), quote=True) + '">Waiting for approval…</p>'
                '<p><a href="/oauth/signup">No identity? Create a hosted identity</a></p>'
                '<noscript><p>JavaScript is needed to complete this browser sign-in. '
                'The <a href="/register">CLI</a> works without it.</p></noscript>',
                script=LOGIN_POLL_SCRIPT,
            )
            self.cookie(response, self.login_cookie, value, 600)
            return response
        if path == '/oauth/signup':
            if request.method == 'GET':
                binder = secret()
                response = page(
                    'Create a hosted identity',
                    '<p>The server will store your signing and encryption keys. You can switch to managing your own keys later.</p>'
                    '<form method="post">'
                    + fields({'csrf': csrf(binder)})
                    + '<label for="handle">Identity handle</label>'
                    '<input id="handle" name="handle" autocomplete="username" autocapitalize="none" spellcheck="false" minlength="2" maxlength="41" required pattern="[a-z][a-z0-9-]{1,40}">'
                    '<br><button>Create identity and sign in</button></form>',
                )
                self.cookie(response, self.login_cookie, binder, 600)
                return response
            require(request.method == 'POST', 'method_not_allowed')
            args = await form(request)
            self.require_csrf(request, args, self.login_cookie)
            packet = request_for(
                'identity.custodial_create',
                {
                    'handle': args.get('handle', ''),
                    'nonce': b64(os.urandom(32)),
                    'recovery_secret': b64(os.urandom(32)),
                },
                self.service.settings.service_url,
                contract_version=2,
                expires_at=now + timedelta(seconds=180),
            )
            result = await self.service.executor.execute(packet, entry='network')
            require(result.status == 'ok', result.error.code if result.error else 'server_error')
            from msg.core.codec import unb64
            from msg.core.models import Principal

            token = unb64(result.data['token'])
            async with self.service.metadata.transaction(write=True) as tx:
                self.oauth.fence(tx)
                value, code, _ = self.oauth.pending(tx, kind='login')
                credential = await tx.credential(result.data['credential_id'])
                require(
                    hmac.compare_digest(credential.verifier, hashlib.sha256(token).digest()),
                    'invalid_token',
                )
                principal = Principal(
                    actor=result.subject,
                    subject=result.subject,
                    credential_id=credential.id,
                    method='token',
                    certificates=(),
                    ceiling=credential.ceiling,
                )
                await self.oauth.approve(tx, principal, code, 'approve')
                logged, error = await self.oauth.pending_result(tx, value, 'msg-cli', 'login')
                require(error is None, 'server_error')
            response = page(
                'Signed in',
                '<p>Your hosted identity has been created. Return to the authorization page to continue. Keep this browser session; after signing out, you will need a signed-in CLI to sign in again.</p>',
            )
            self.cookie(
                response,
                self.session_cookie,
                logged['cookie'],
                int((logged['expires'] - now).total_seconds()),
            )
            return response
        if path == '/oauth/login/poll':
            require(request.method == 'POST', 'method_not_allowed')
            value = self.require_csrf(request, await form(request), self.login_cookie)
            async with self.service.metadata.transaction(write=True) as tx:
                self.oauth.fence(tx)
                logged, error = await self.oauth.pending_result(tx, value, 'msg-cli', 'login')
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
        if path in {'/oauth/authorize', '/oauth/device'}:
            return await self.consent(request)
        if path == '/oauth/logout' and request.method == 'GET':
            cookie = request.cookies.get(self.session_cookie, '')
            async with self.service.metadata.transaction(write=False) as tx:
                await self.oauth.session(tx, cookie)
            return page(
                'Sign out of MSG',
                '<form method="post" action="/oauth/logout">'
                + fields({'csrf': csrf(cookie)})
                + '<button>Confirm sign-out</button></form>',
            )
        require(
            request.method == 'POST' or (path == '/oauth/userinfo' and request.method == 'GET'),
            'method_not_allowed',
        )
        args = await form(request) if request.method == 'POST' else {}
        async with self.service.metadata.transaction(write=path != '/oauth/userinfo') as tx:
            self.oauth.fence(tx)
            if path == '/oauth/device_authorization':
                require(set(args) <= {'client_id', 'scope'}, 'invalid_request')
                value, code, _ = self.oauth.pending(
                    tx,
                    kind='device',
                    client_id=args.get('client_id'),
                    scope=args.get('scope', 'openid profile msg.read offline_access'),
                )
                uri = self.service.settings.service_url + '/oauth/device'
                return json({
                    'device_code': value,
                    'user_code': code,
                    'verification_uri': uri,
                    'verification_uri_complete': uri + '?' + urlencode({'user_code': code}),
                    'expires_in': 600,
                    'interval': 5,
                })
            if path == '/oauth/token':
                require(
                    set(args)
                    <= {
                        'client_id',
                        'grant_type',
                        'code',
                        'redirect_uri',
                        'code_verifier',
                        'device_code',
                        'refresh_token',
                        'resource',
                        'scope',
                    },
                    'invalid_request',
                )
                result, error = await self.oauth.exchange(tx, args)
                return json({'error': error}, 400) if error else json(result)
            if path == '/oauth/userinfo':
                credential = await self.oauth.bearer(
                    tx, self.bearer_value(request), check_resource=False
                )
                _, binding = get(tx, 'access:' + credential.id, now)
                _, family = get(tx, binding['family'], now)
                require('openid' in family['scopes'], 'invalid_scope')
                user = await tx.subject(credential.subject_id)
                resource = await tx.resource(user.resource_id)
                return json({
                    'sub': user.resource_id,
                    **(
                        {'preferred_username': resource.name.removeprefix('@')}
                        if 'profile' in family['scopes']
                        else {}
                    ),
                })
            if path == '/oauth/revoke':
                from msg.security.oauth import client_for

                client_for(self.oauth.config, args.get('client_id'))
                require(set(args) <= {'client_id', 'token', 'token_type_hint'}, 'invalid_request')
                id = state_id('refresh', args.get('token', ''))
                if args.get('token', '').startswith('t_oauth_'):
                    credential = await self.oauth.bearer(tx, args['token'], check_resource=False)
                    id = 'access:' + credential.id
                row = tx.one('SELECT body FROM oauth_states WHERE id=?', (id,))
                if row:
                    binding = loads(row[0])
                    _, family = get(tx, binding['family'], now)
                    if family['client_id'] == args['client_id']:
                        family['revoked'] = True
                        save(tx, binding['family'], family)
                return json({})
            if path == '/oauth/logout':
                cookie = self.require_csrf(request, args, self.session_cookie)
                _, body = get(tx, state_id('session', cookie), now)
                body['revoked'] = True
                save(tx, state_id('session', cookie), body)
                response = json({'logged_out': True})
                response.delete_cookie(
                    self.session_cookie, path='/', secure=self.secure, httponly=True
                )
                return response
        raise Failure('invalid_request')

    async def consent(self, request):
        require(request.method in {'GET', 'POST'}, 'method_not_allowed')
        args = (
            unique(list(request.query_params.multi_items()))
            if request.method == 'GET'
            else await form(request)
        )
        device = request.url.path == '/oauth/device'
        auth_args = {k: v for k, v in args.items() if k not in {'csrf', 'decision'}}
        async with self.service.metadata.transaction(write=True) as tx:
            self.oauth.fence(tx)
            if device:
                require(set(auth_args) <= {'user_code'}, 'invalid_request')
                code = auth_args.get('user_code', '').upper().replace('-', '')
                if not code:
                    return page(
                        'Device sign-in',
                        '<form method="get"><input name="user_code" placeholder="Approval code" required>'
                        '<button>Continue</button></form>',
                    )
                _, pending = get(tx, state_id('user', code), self.service.clock())
                require(
                    pending['kind'] == 'device' and pending['status'] == 'pending', 'invalid_grant'
                )
                from msg.security.oauth import client_for

                client = client_for(self.oauth.config, pending['client_id'])
                scopes = pending['scopes']
            else:
                client = self.oauth.authorization(auth_args)
                scopes = auth_args.get('scope', 'openid profile').split()
            cookie = request.cookies.get(self.session_cookie, '')
            try:
                _, source = await self.oauth.session(tx, cookie)
            except Failure as exc:
                if exc.code != 'invalid_grant':
                    raise
                require(request.method == 'GET', 'invalid_grant')
                return page(
                    'Sign in to continue',
                    '<p>' + escape(client.name) + ' requests access to your MSG identity.</p>'
                    '<p><a href="/oauth/login" target="_blank" rel="noopener">Open sign-in page</a></p>'
                    '<p>After signing in, <a href="'
                    + escape(str(request.url), quote=True)
                    + '">refresh to continue</a>.</p>',
                )
            if request.method == 'GET':
                return page(
                    'Authorize ' + client.name,
                    '<p>Identity: '
                    + escape(source['subject'])
                    + '</p><p>Permissions: '
                    + escape(' '.join(scopes))
                    + '</p><form method="post">'
                    + fields(dict(auth_args, csrf=csrf(cookie)))
                    + '<button name="decision" value="approve">Approve</button> '
                    '<button name="decision" value="deny">Deny</button></form>',
                )
            self.require_csrf(request, args, self.session_cookie)
            require(args.get('decision') in {'approve', 'deny'}, 'invalid_request')
            if device:
                id = state_id('user', code)
                if args['decision'] == 'approve':
                    pending.update(source)
                pending['status'] = 'approved' if args['decision'] == 'approve' else 'denied'
                save(tx, id, pending)
                return page('Confirmed', '<p>You can return to the CLI to continue.</p>')
            redirect = {'state': auth_args['state'], 'iss': self.service.settings.service_url}
            if args['decision'] == 'approve':
                auth_args.setdefault('scope', 'openid profile')
                redirect['code'] = await self.oauth.authorize(tx, cookie, auth_args)
            else:
                redirect['error'] = 'access_denied'
            return RedirectResponse(
                auth_args['redirect_uri'] + '?' + urlencode(redirect),
                status_code=303,
                headers=HEADERS,
            )
