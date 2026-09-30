"""Browser consent, persistent HttpOnly sessions and standard OAuth endpoints."""

import hashlib
import hmac
import os
from dataclasses import replace
from datetime import timedelta
from html import escape
from urllib.parse import parse_qsl, urlencode, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from msg.core.codec import b64, canonical, loads
from msg.core.errors import Failure, require
from msg.core.models import TokenProof
from msg.core.requests import request_for
from msg.security.oauth import DEVICE_GRANT, OAuthService, get, save, secret, state_id
from msg.transports.http_common import body_bytes
from msg.transports.packet import decode_packet
from msg.transports.url_safety import require_matching_host, require_safe_request_target

HEADERS = {
    'Cache-Control': 'no-store',
    'Pragma': 'no-cache',
    'Referrer-Policy': 'no-referrer',
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; "
    "script-src 'nonce-msg'; connect-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'",
}


def json(value, status=200):
    return JSONResponse(value, status_code=status, headers=HEADERS)


def page(title, body):
    return HTMLResponse(
        '<!doctype html><html lang="zh"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>' + escape(title) + '</title><style>'
        'body{font:17px system-ui;background:#faf9f6;color:#252525;margin:0;}'
        'main{max-width:540px;margin:12vh auto;padding:32px;line-height:1.7;}'
        'h1{font-size:30px;}code{font-size:21px;word-break:break-all;}'
        'button,input,textarea{font:inherit;padding:10px 16px;margin:8px 0;}'
        'button{cursor:pointer;}a{color:inherit;}</style><main><h1>'
        + escape(title)
        + '</h1>'
        + body
        + '</main></html>',
        headers=HEADERS,
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
        require(
            value
            and request.headers.get('origin') == self.service.settings.service_url
            and hmac.compare_digest(args.get('csrf', ''), csrf(value)),
            'invalid_request',
        )
        return value

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        path = request.url.path
        oauth_path = path.startswith('/oauth/') or path in {
            '/.well-known/oauth-authorization-server',
            '/.well-known/openid-configuration',
        }
        bearer = request.headers.get('authorization')
        if not oauth_path and not (bearer and path.startswith('/-/p/')):
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

                await self.app(scope, bound_receive, send)
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
                response.headers['WWW-Authenticate'] = 'Bearer error="invalid_token"'
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
        if path.startswith('/.well-known/') or path == '/oauth/jwks':
            require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
            base = self.service.settings.service_url
            if path == '/oauth/jwks':
                return json({'keys': [self.oauth.jwk()]})
            return json({
                'issuer': base,
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
                'scopes_supported': [
                    'openid',
                    'profile',
                    'offline_access',
                    'msg.read',
                    'msg.write',
                ],
            })
        # Rate limits commit independently, including rejected code guesses.
        # Userinfo is a read: do not mutate retained OAuth state during GET or
        # POST reads. Authentication still checks the current source/family.
        if path != '/oauth/userinfo':
            async with self.service.metadata.transaction(write=True) as tx:
                self.oauth.rate(tx, request.client.host if request.client else '')
        if path == '/oauth/login':
            require(request.method == 'GET', 'method_not_allowed')
            async with self.service.metadata.transaction(write=True) as tx:
                self.oauth.fence(tx)
                value, code, _ = self.oauth.pending(tx, kind='login')
            response = page(
                '登录 MSG',
                '<p>在已登录的 CLI 中确认这次浏览器登录：</p><code>msg auth approve '
                + escape(code)
                + '</code><p id="status">等待确认…</p><p><a href="/oauth/signup">'
                '没有身份？申请托管 key</a></p><script nonce="msg">'
                'const c=' + canonical(csrf(value)).decode() + ';'
                'const poll=async()=>{const r=await fetch("/oauth/login/poll",{method:"POST",'
                'headers:{"Content-Type":"application/json"},body:JSON.stringify({csrf:c})});'
                'const d=await r.json();if(d.logged_in){document.getElementById("status").textContent='
                '"登录完成。返回原授权页面继续。";return;}if(["authorization_pending","slow_down"].includes(d.error))'
                '{setTimeout(poll,d.error==="slow_down"?15000:5000);}else{document.getElementById("status").textContent='
                '"登录未完成，请重新打开本页。";}};setTimeout(poll,5000);</script>',
            )
            self.cookie(response, self.login_cookie, value, 600)
            return response
        if path == '/oauth/signup':
            if request.method == 'GET':
                binder = secret()
                response = page(
                    '申请托管 key',
                    '<p>服务器会保管签名和加密密钥。之后可升级为自行保管。</p>'
                    '<form method="post">'
                    + fields({'csrf': csrf(binder)})
                    + '<input name="handle" placeholder="身份名称" required pattern="[a-z][a-z0-9-]{1,40}">'
                    '<br><button>创建身份并登录</button></form>',
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
                '登录完成',
                '<p>托管身份已创建。返回原授权页面继续。请保存登录态，退出后需用已授权 CLI 登录。</p>',
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
                    },
                    'invalid_request',
                )
                result, error = await self.oauth.exchange(tx, args)
                return json({'error': error}, 400) if error else json(result)
            if path == '/oauth/userinfo':
                credential = await self.oauth.bearer(tx, self.bearer_value(request))
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
                    credential = await self.oauth.bearer(tx, args['token'])
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
                        '设备登录',
                        '<form method="get"><input name="user_code" placeholder="验证码" required>'
                        '<button>继续</button></form>',
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
                    '登录后继续',
                    '<p>' + escape(client.name) + ' 请求使用你的 MSG 身份。</p>'
                    '<p><a href="/oauth/login" target="_blank" rel="noopener">打开登录页面</a></p>'
                    '<p>登录完成后，<a href="'
                    + escape(str(request.url), quote=True)
                    + '">刷新继续</a>。</p>',
                )
            if request.method == 'GET':
                return page(
                    '授权给 ' + client.name,
                    '<p>身份：'
                    + escape(source['subject'])
                    + '</p><p>权限：'
                    + escape(' '.join(scopes))
                    + '</p><form method="post">'
                    + fields(dict(auth_args, csrf=csrf(cookie)))
                    + '<button name="decision" value="approve">同意</button> '
                    '<button name="decision" value="deny">拒绝</button></form>',
                )
            self.require_csrf(request, args, self.session_cookie)
            require(args.get('decision') in {'approve', 'deny'}, 'invalid_request')
            if device:
                id = state_id('user', code)
                if args['decision'] == 'approve':
                    pending.update(source)
                pending['status'] = 'approved' if args['decision'] == 'approve' else 'denied'
                save(tx, id, pending)
                return page('已确认', '<p>可以返回 CLI 继续。</p>')
            redirect = {'state': auth_args['state']}
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
