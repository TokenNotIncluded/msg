"""业务参数表单接入统一执行器；提供方凭据只留在服务端。"""

import re
from dataclasses import replace
from html import escape
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

from starlette.requests import Request
from starlette.responses import RedirectResponse

from msg.core.errors import Failure, require
from msg.security.email_login import begin_email, verify_email
from msg.security.login import VerifiedLogin, bind_login, complete_login, remove_login
from msg.security.login_flow import consume_flow, flow, receive_callback, start_flow
from msg.security.oauth import OAuthService, get, put, secret, state_id
from msg.transports.account_login_page import (
    approval_page,
    email_code_page,
    finish_page,
    login_page,
    methods_page,
)
from msg.transports.login_providers import authorization_url, exchange_identity
from msg.transports.oauth_http import HEADERS, csrf, form, json, page
from msg.transports.url_safety import require_matching_host, require_safe_request_target


class AccountLoginBoundary:
    def __init__(self, app, *, service):
        self.app, self.service = app, service
        self.oauth = OAuthService(service)
        self.secure = urlsplit(service.settings.service_url).scheme == 'https'
        self.flow_cookie = '__Host-msg_account_login' if self.secure else 'msg_account_login'
        self.session_cookie = '__Host-msg_session' if self.secure else 'msg_session'

    @property
    def config(self):
        return self.service.settings.login

    def cookie(self, response, name, value, ttl):
        response.set_cookie(
            name, value, max_age=ttl, secure=self.secure, httponly=True, samesite='lax', path='/'
        )

    def provider(self, name):
        provider = self.config.provider(name)
        require(provider is not None and provider.enabled, 'login_provider_disabled')
        return provider

    def providers(self):
        return tuple(
            p.name
            for p in self.config.providers
            if p.enabled and p.name in {'google', 'github', 'chatgpt'}
        )

    def enabled(self, name):
        item = self.config.provider(name)
        return item is not None and item.enabled

    def check_csrf(self, request, args):
        browser, supplied = request.cookies.get(self.flow_cookie, ''), args.get('csrf', '')
        import hmac

        require(
            len(browser) >= 20
            and supplied.isascii()
            and hmac.compare_digest(supplied, csrf(browser)),
            'forbidden_origin',
        )
        return browser

    async def source(self, request):
        cookie = request.cookies.get(self.session_cookie, '')
        require(cookie, 'authentication_required')
        async with self.service.metadata.transaction(write=False) as tx:
            _, source = await self.oauth.session(tx, cookie)
        return source

    async def args(self, request, allowed):
        require(request.method == 'POST', 'method_not_allowed')
        require(not request.query_params, 'invalid_request')
        args = await form(request)
        require(set(args) <= allowed, 'invalid_request')
        return args, self.check_csrf(request, args)

    def mode(self, args):
        mode = args.get('mode', 'login')
        require(mode in {'login', 'register', 'bind'}, 'invalid_login_mode')
        handle = None
        if mode == 'register':
            handle = args.get('handle', '')
            require(args.get('custody') == 'yes', 'custodial_consent_required')
            require(re.fullmatch(r'[a-z][a-z0-9-]{1,40}', handle) is not None, 'invalid_handle')
        else:
            require(not args.get('handle') and not args.get('custody'), 'invalid_request')
        return mode, handle

    def success(self, completed, *, bound=False):
        require(
            completed.result.status == 'ok',
            completed.result.error.code if completed.result.error else 'login_failed',
        )
        response = RedirectResponse(
            '/account/login-methods' if bound else '/', 303, headers=HEADERS
        )
        if completed.cookie:
            self.cookie(
                response,
                self.session_cookie,
                completed.cookie,
                int((completed.expires - self.service.clock()).total_seconds()),
            )
        response.delete_cookie(
            self.flow_cookie, secure=self.secure, httponly=True, samesite='lax', path='/'
        )
        return response

    async def finish_identity(self, request, verified, body, request_id):
        if body['mode'] == 'bind':
            return await self.management(
                request, verified, body['source']['subject'], request_id=request_id
            )
        completed = await complete_login(
            self.service, verified, request_id=request_id, handle=body.get('handle')
        )
        return self.success(completed)

    async def management(self, request, verified, owner, *, request_id, binding_id=None):
        from msg.security.login import request_login_approval

        action = 'remove' if binding_id is not None else 'bind'
        requested = await request_login_approval(
            self.service,
            verified,
            owner,
            action=action,
            binding_id=binding_id,
            request_id=request_id,
        )
        pending = secret()
        body = {
            'browser': state_id('browser', request.cookies.get(self.flow_cookie, '')),
            'owner': owner,
            'intent_id': requested.intent_id,
            'user_code': requested.user_code,
            'request_id': request_id,
            'action': action,
            'verified': {'provider': verified.provider, 'sub': verified.sub} if verified else None,
        }
        async with self.service.metadata.transaction(write=True) as tx:
            self.oauth.fence(tx)
            body['provider'] = (
                verified.provider
                if verified
                else tx.one(
                    'SELECT provider FROM login_bindings WHERE id=? AND subject=?',
                    (binding_id, owner),
                )[0]
            )
            put(tx, state_id('login-approval', pending), 'login-approval', requested.expires, body)
        return await self.finish_pending(request, pending, body)

    async def finish_pending(self, request, pending, body):
        app = self.service
        source = await self.source(request)
        require(source['subject'] == body['owner'], 'login_owner_required')
        from msg.security import login

        async with app.metadata.transaction(write=True) as tx:
            self.oauth.fence(tx)
            _, intent = get(tx, body['intent_id'], app.clock())
            if intent['status'] == 'pending':
                approve = getattr(login, 'approve_browser_login_intent', None)
                if approve is not None:
                    await approve(
                        app, tx, request.cookies.get(self.session_cookie, ''), body['intent_id']
                    )
                _, intent = get(tx, body['intent_id'], app.clock())
            require(intent['status'] in {'pending', 'approved'}, 'invalid_grant')
        if intent['status'] == 'pending':
            return approval_page(
                csrf(request.cookies.get(self.flow_cookie, '')),
                pending,
                body['user_code'],
                removing=body['action'] == 'remove',
                provider=body['provider'],
            )
        if body['action'] == 'bind':
            verified = VerifiedLogin(**body['verified'])
            completed = await bind_login(
                app, verified, request_id=body['request_id'], intent_id=body['intent_id']
            )
            return self.success(completed, bound=True)
        completed = await remove_login(
            app, request_id=body['request_id'], intent_id=body['intent_id']
        )
        require(
            completed.status == 'ok', completed.error.code if completed.error else 'login_failed'
        )
        try:
            await self.source(request)
        except Failure:
            response = RedirectResponse('/login', 303, headers=HEADERS)
            response.delete_cookie(
                self.session_cookie, secure=self.secure, httponly=True, samesite='lax', path='/'
            )
        else:
            response = RedirectResponse('/account/login-methods', 303, headers=HEADERS)
        response.delete_cookie(
            self.flow_cookie, secure=self.secure, httponly=True, samesite='lax', path='/'
        )
        return response

    async def dispatch(self, request):
        path = request.url.path
        app = self.service
        if path == '/oauth/signup':
            require(request.method in {'GET', 'HEAD'}, 'login_registration_forbidden')
            return RedirectResponse('/register', 303, headers=HEADERS)
        if path in {'/login', '/register', '/account/login-methods'}:
            require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
            browser = secret()
            token = csrf(browser)
            if path == '/account/login-methods':
                source = await self.source(request)
                async with app.metadata.transaction(write=False) as tx:
                    bindings = [
                        {'binding_id': row[0], 'provider': row[1]}
                        for row in tx.rows(
                            'SELECT id,provider FROM login_bindings WHERE subject=? AND revoked_at IS NULL ORDER BY provider,id',
                            (source['subject'],),
                        )
                    ]
                    subject = await tx.subject(source['subject'])
                from msg.transports.passkey_login import PASSKEY_SCRIPT, passkey_control

                response = methods_page(
                    token,
                    self.providers(),
                    bindings,
                    email=self.enabled('email'),
                    kind=subject.kind,
                    passkey_markup=passkey_control('bind', token)
                    if self.enabled('passkey')
                    else '',
                    script=PASSKEY_SCRIPT if self.enabled('passkey') else '',
                )
            else:
                from msg.transports.passkey_login import PASSKEY_SCRIPT, passkey_control

                response = login_page(
                    token,
                    self.providers(),
                    email=self.enabled('email'),
                    register=path == '/register',
                    passkey=self.enabled('passkey'),
                    passkey_markup=passkey_control('login', token),
                    script=PASSKEY_SCRIPT
                    if self.enabled('passkey') and path != '/register'
                    else '',
                )
            self.cookie(response, self.flow_cookie, browser, 600)
            return response
        if path == '/login/finish':
            require(request.method == 'GET', 'method_not_allowed')
            require(set(request.query_params) == {'state'}, 'invalid_request')
            state = request.query_params['state']
            browser = request.cookies.get(self.flow_cookie, '')
            async with app.metadata.transaction(write=False) as tx:
                body = flow(app, tx, state, browser)
                require(body['phase'] == 'returned', 'invalid_login_transaction')
            return finish_page(csrf(browser), state)
        if path == '/-/login/start':
            args, browser = await self.args(
                request, {'csrf', 'provider', 'mode', 'handle', 'custody'}
            )
            provider = self.provider(args.get('provider'))
            require(provider.name in {'google', 'github', 'chatgpt'}, 'login_provider_disabled')
            mode, handle = self.mode(args)
            source = await self.source(request) if mode == 'bind' else None
            async with app.metadata.transaction(write=True) as tx:
                self.oauth.rate(tx, request.client.host if request.client else '')
                state, verifier, nonce = start_flow(
                    app, tx, provider.name, browser, mode=mode, source=source, handle=handle
                )
            callback = app.settings.service_url + '/-/login/callback/' + provider.name
            url = authorization_url(provider.name, provider, state, verifier, nonce, callback)
            return RedirectResponse(url, 303, headers=HEADERS)
        if path.startswith('/-/login/callback/'):
            require(request.method == 'GET', 'method_not_allowed')
            provider = self.provider(path.rsplit('/', 1)[-1])
            require(provider.name in {'google', 'github', 'chatgpt'}, 'login_provider_disabled')
            require(
                set(request.query_params)
                <= {
                    'state',
                    'code',
                    'error',
                    'error_description',
                    'scope',
                    'authuser',
                    'prompt',
                    'iss',
                },
                'invalid_request',
            )
            require('error' not in request.query_params, 'login_provider_denied')
            state = request.query_params.get('state', '')
            async with app.metadata.transaction(write=True) as tx:
                self.oauth.fence(tx)
                receive_callback(
                    app,
                    tx,
                    state,
                    request.cookies.get(self.flow_cookie, ''),
                    provider.name,
                    request.query_params.get('code'),
                )
            return RedirectResponse(
                '/login/finish?state=' + quote(state, safe=''), 303, headers=HEADERS
            )
        if path == '/-/login/complete':
            args, browser = await self.args(request, {'csrf', 'state'})
            state = args.get('state', '')
            async with app.metadata.transaction(write=True) as tx:
                self.oauth.rate(tx, request.client.host if request.client else '')
                body = consume_flow(app, tx, state, browser)
            provider = self.provider(body['provider'])
            callback = app.settings.service_url + '/-/login/callback/' + provider.name
            identity = await exchange_identity(
                provider.name,
                provider,
                body['code'],
                body['verifier'],
                body['nonce'],
                callback,
                app.clock(),
            )
            return await self.finish_identity(
                request,
                VerifiedLogin(provider.name, identity.stable_id),
                body,
                state_id('login-complete', state),
            )
        if path == '/-/login/email/start':
            args, browser = await self.args(request, {'csrf', 'email', 'mode', 'handle', 'custody'})
            provider = self.provider('email')
            mode, handle = self.mode(args)
            source = await self.source(request) if mode == 'bind' else None
            async with app.metadata.transaction(write=True) as tx:
                self.oauth.rate(tx, request.client.host if request.client else '')
                challenge, code = begin_email(
                    app,
                    tx,
                    args.get('email'),
                    browser,
                    {'mode': mode, 'handle': handle, 'source': source},
                )
            sender = getattr(app, '_login_mail_sender', None)
            if sender is None:
                if provider.transport == 'smtp':
                    from msg.workers.mail import SmtpSender

                    config = app.settings.server.mail
                    require(config is not None and config.enabled, 'login_mail_not_configured')
                    if provider.sender:
                        config = replace(config, sender=provider.sender)
                    try:
                        status = await SmtpSender(config).send(
                            SimpleNamespace(
                                id=state_id('login-mail', challenge),
                                arguments={
                                    'recipient': args['email'],
                                    'subject': 'MSG 登录验证码 / Sign-in code',
                                    'text': 'MSG 登录验证码：'
                                    + code
                                    + '\n10 分钟内有效，只能使用一次。',
                                },
                            )
                        )
                    except Failure:
                        raise Failure('login_mail_send_failed') from None
                    require(status == 'sent', 'login_mail_delivery_unknown')
                else:
                    require(provider.transport == 'sequenzy', 'login_mail_not_configured')
                    from msg.workers.sequenzy import SequenzySender

                    sender = SequenzySender(
                        provider.credential_file, sender=provider.sender or None
                    )
            if sender is not None:
                await sender.send_code(args['email'], code)
            return email_code_page(csrf(browser), challenge)
        if path == '/-/login/email/complete':
            args, browser = await self.args(request, {'csrf', 'challenge', 'code'})
            self.provider('email')
            challenge = args.get('challenge', '')
            async with app.metadata.transaction(write=True) as tx:
                self.oauth.rate(tx, request.client.host if request.client else '')
                body, error = verify_email(app, tx, challenge, browser, args.get('code', ''))
            require(error is None, error or 'invalid_login_code')
            return await self.finish_identity(
                request,
                VerifiedLogin('email', body['sub']),
                body['metadata'],
                state_id('email-complete', challenge),
            )
        if path == '/-/login/remove':
            args, _ = await self.args(request, {'csrf', 'binding_id'})
            source = await self.source(request)
            return await self.management(
                request,
                None,
                source['subject'],
                binding_id=args.get('binding_id', ''),
                request_id=secret(),
            )
        if path == '/-/login/approval':
            args, browser = await self.args(request, {'csrf', 'pending'})
            pending = args.get('pending', '')
            require(len(pending) == 43, 'invalid_request')
            async with app.metadata.transaction(write=False) as tx:
                _, body = get(tx, state_id('login-approval', pending), app.clock())
                require(body['browser'] == state_id('browser', browser), 'invalid_login_browser')
            return await self.finish_pending(request, pending, body)
        if path.startswith('/-/login/passkey/'):
            from msg.transports.passkey_login import dispatch

            browser = request.cookies.get(self.flow_cookie, '')
            return await dispatch(
                request,
                app=app,
                source=lambda: self.source(request),
                browser=browser,
                csrf=csrf(browser),
                session_cookie=request.cookies.get(self.session_cookie, ''),
            )
        raise Failure('not_found')

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        enabled = getattr(getattr(self.service.settings, 'login', None), 'enabled', False)
        routes = {'/login', '/register', '/login/finish', '/account/login-methods'}
        if self.config.registration == 'provider_only':
            routes.add('/oauth/signup')
        if (
            scope['type'] != 'http'
            or not enabled
            or (path not in routes and not path.startswith('/-/login/'))
        ):
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        try:
            require(
                len(request.query_params.multi_items()) == len(request.query_params),
                'invalid_request',
            )
            require_safe_request_target(
                scope.get('raw_path') or path.encode(),
                scope.get('query_string', b''),
                maximum=self.service.settings.server.limits.max_path_bytes,
            )
            require_matching_host(
                request.headers.getlist('host'), urlsplit(self.service.settings.service_url)
            )
            require(
                not request.headers.get('authorization')
                and not request.headers.get('x-msg-request'),
                'ambiguous_credentials',
            )
            origin = request.headers.get('origin')
            require(
                origin in {None, self.service.settings.service_url}
                and (request.method != 'POST' or origin == self.service.settings.service_url),
                'forbidden_origin',
            )
            response = await self.dispatch(request)
        except Failure as exc:
            status = (
                404
                if exc.code == 'not_found'
                else 403
                if exc.code
                in {'authentication_required', 'forbidden_origin', 'ambiguous_credentials'}
                else 400
            )
            if request.headers.get('accept', '').startswith('application/json'):
                response = json({'error': exc.code, 'retryable': exc.retryable}, status)
            else:
                response = page(
                    '登录未完成 / Sign-in incomplete',
                    '<p>'
                    + escape(exc.code)
                    + '</p><p><a href="/login">重新登录 / Try again</a></p>',
                )
                response.status_code = status
        await response(scope, receive, send)
