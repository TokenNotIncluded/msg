"""WebAuthn controls for the unified login page; no account creation UI."""

import hmac
from html import escape
from inspect import isawaitable
from urllib.parse import urlsplit

from msg.core.codec import b64, loads, unb64
from msg.core.errors import require
from msg.security.oauth import OAuthService, get, save, state_id
from msg.security.passkey import (
    MAX_RESPONSE_BYTES,
    PasskeyServer,
    begin_challenge,
    read_challenge,
    trusted_passkey,
)
from msg.transports.http_common import body_bytes
from msg.transports.oauth_http import json

PASSKEY_SCRIPT = r"""(() => {
  const bytes = value => {
    const padded = value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4);
    return Uint8Array.from(atob(padded), character => character.charCodeAt(0));
  };
  const encoded = value => {
    if (value === null || value === undefined) return null;
    const data = new Uint8Array(value);
    let result = '';
    for (let start = 0; start < data.length; start += 4096) result += String.fromCharCode(...data.subarray(start, start + 4096));
    return btoa(result).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  };
  const options = (value, binding) => {
    if (binding && PublicKeyCredential.parseCreationOptionsFromJSON) return PublicKeyCredential.parseCreationOptionsFromJSON(value);
    if (!binding && PublicKeyCredential.parseRequestOptionsFromJSON) return PublicKeyCredential.parseRequestOptionsFromJSON(value);
    const result = {...value, challenge: bytes(value.challenge)};
    if (binding) result.user = {...value.user, id: bytes(value.user.id)};
    for (const name of ['excludeCredentials', 'allowCredentials']) {
      if (value[name]) result[name] = value[name].map(credential => ({...credential, id: bytes(credential.id)}));
    }
    return result;
  };
  const serialized = (credential, binding) => {
    const response = {clientDataJSON: encoded(credential.response.clientDataJSON)};
    if (binding) {
      response.attestationObject = encoded(credential.response.attestationObject);
      if (credential.response.getTransports) response.transports = credential.response.getTransports();
    } else {
      response.authenticatorData = encoded(credential.response.authenticatorData);
      response.signature = encoded(credential.response.signature);
      response.userHandle = encoded(credential.response.userHandle);
    }
    return {id: credential.id, rawId: encoded(credential.rawId), type: credential.type, response,
      clientExtensionResults: credential.getClientExtensionResults()};
  };
  const send = async (url, body) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(url, {method: 'POST', mode: 'same-origin', credentials: 'same-origin', redirect: 'error',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), signal: controller.signal});
      const data = await response.json();
      if (!response.ok || data.error || data.status === 'error') throw new Error('verification_failed');
      return data;
    } finally { clearTimeout(timer); }
  };
  for (const button of document.querySelectorAll('[data-passkey-action]')) {
    const status = document.getElementById(button.dataset.status);
    if (!status) continue;
    const text = (en, zh) => globalThis.msgText?.(en, zh) ?? (document.documentElement.lang.startsWith('zh') ? zh : en);
    const show = (en, zh) => {status.textContent = text(en, zh);};
    if (!globalThis.PublicKeyCredential || !navigator.credentials || !globalThis.isSecureContext) {
      button.disabled = true;
      show('Passkeys are unavailable in this browser.', '此浏览器无法使用 Passkey。');
      continue;
    }
    button.addEventListener('click', async () => {
      if (button.disabled) return;
      button.disabled = true;
      const binding = button.dataset.passkeyAction === 'bind';
      show('Confirm using your passkey.', '请使用 Passkey 确认。');
      try {
        const prefix = binding ? '/-/login/passkey/bind' : '/-/login/passkey';
        const started = await send(prefix + '/options', {csrf: button.dataset.csrf});
        const publicKey = options(started.options.publicKey, binding);
        const credential = binding ? await navigator.credentials.create({publicKey}) : await navigator.credentials.get({publicKey});
        if (!credential) throw new Error('canceled');
        let completed = await send(prefix + '/complete', {csrf: button.dataset.csrf, challenge_id: started.challenge_id,
          credential: serialized(credential, binding)});
        const deadline = Date.now() + 180000;
        while (completed.approval_required) {
          status.textContent = text('Approve this passkey binding: ', '确认绑定此 Passkey：');
          const command = document.createElement('code');
          command.textContent = 'msg auth approve ' + completed.user_code;
          status.append(command);
          if (Date.now() >= deadline) throw new Error('approval_expired');
          await new Promise(resolve => setTimeout(resolve, 2000));
          completed = await send(prefix + '/complete', {csrf: button.dataset.csrf, challenge_id: started.challenge_id});
        }
        show(binding ? 'Passkey added.' : 'Signed in.', binding ? '已添加 Passkey。' : '已登录。');
        const link = document.createElement('a');
        link.href = binding ? '/account/login-methods' : '/';
        link.textContent = text(binding ? 'Account settings' : 'Back to home', binding ? '账户设置' : '返回首页');
        status.append(' ', link);
      } catch (error) {
        if (error.name === 'NotAllowedError' || error.message === 'canceled') show('Passkey action canceled. Try again.', 'Passkey 操作已取消，请重试。');
        else show('Passkey verification failed. Try again or use another sign-in method.', 'Passkey 验证失败，请重试或换一种登录方式。');
      } finally {button.disabled = false;}
    });
  }
})();"""


def passkey_control(action, csrf):
    if action not in {'bind', 'login'}:
        raise ValueError('invalid_passkey_action')
    status = 'passkey-' + action + '-status'
    label = 'Add a passkey' if action == 'bind' else 'Sign in with a passkey'
    return (
        '<button type="button" data-passkey-action="'
        + action
        + '" data-csrf="'
        + escape(csrf, quote=True)
        + '" data-status="'
        + status
        + '">'
        + label
        + '</button>'
        + '<p id="'
        + status
        + '" role="status" aria-live="polite"></p>'
    )


async def _source(value):
    result = value() if callable(value) else value
    if isawaitable(result):
        result = await result
    require(
        isinstance(result, dict) and isinstance(result.get('subject'), str),
        'authentication_required',
    )
    return result


async def dispatch(request, *, app, source, browser, csrf, session_cookie=None):
    """Only explicit same-origin JSON writes; core owns all account/session commits."""
    from msg.security.login import (
        VerifiedLogin,
        bind_login,
        complete_login,
        lookup_binding,
        request_login_approval,
    )

    provider = app.settings.login.provider('passkey')
    require(
        app.settings.login.enabled and provider is not None and provider.enabled,
        'login_provider_disabled',
    )
    require(request.method == 'POST' and not request.query_params, 'invalid_request')
    require(request.headers.get('origin') == app.settings.service_url, 'forbidden_origin')
    require(
        request.headers.get('content-type', '').split(';')[0] == 'application/json',
        'invalid_request',
    )
    args = loads(await body_bytes(request, MAX_RESPONSE_BYTES + 4096))
    require(isinstance(args, dict) and isinstance(args.get('csrf'), str), 'invalid_request')
    require(isinstance(browser, str) and 32 <= len(browser) <= 512, 'invalid_request')
    require(args['csrf'].isascii() and hmac.compare_digest(args['csrf'], csrf), 'forbidden_origin')
    path = request.url.path
    require(
        path
        in {
            '/-/login/passkey/bind/options',
            '/-/login/passkey/bind/complete',
            '/-/login/passkey/options',
            '/-/login/passkey/complete',
        },
        'not_found',
    )
    registration = '/bind/' in path
    owner = await _source(source) if registration else None
    server, oauth, now = PasskeyServer(app.settings.service_url), OAuthService(app), app.clock()
    if path.endswith('/options'):
        require(set(args) == {'csrf'}, 'invalid_request')
        async with app.metadata.transaction(write=True) as tx:
            oauth.fence(tx)
            oauth.rate(tx, request.client.host if request.client else '')
            if registration:
                subject = await tx.subject(owner['subject'])
                require(subject is not None, 'authentication_required')
                existing = tx.rows(
                    'SELECT provider_data FROM login_bindings WHERE provider=? AND subject=? AND revoked_at IS NULL',
                    ('passkey', subject.resource_id),
                )
                options, state = server.registration_options(
                    subject.resource_id, 'MSG account', [loads(row[0]) for row in existing]
                )
            else:
                options, state = server.authentication_options()
            challenge = begin_challenge(
                tx,
                state,
                mode='registration' if registration else 'authentication',
                now=now,
                flow=browser,
                subject=owner['subject'] if registration else None,
            )
        return json({'options': options, 'challenge_id': challenge})
    require(
        set(args) <= {'csrf', 'challenge_id', 'credential'} and 'challenge_id' in args,
        'invalid_request',
    )
    challenge, mode = args['challenge_id'], 'registration' if registration else 'authentication'
    async with app.metadata.transaction(write=True) as tx:
        oauth.fence(tx)
        oauth.rate(tx, request.client.host if request.client else '')
        body = read_challenge(
            tx,
            challenge,
            mode=mode,
            now=now,
            flow=browser,
            subject=owner['subject'] if registration else None,
        )
        if registration:
            # Public registration evidence is retained privately for the short approval window.
            # A browser read session selects the owner; signed approval authorizes the factor.
            approval = body.get('approval')
            if approval is None:
                require('credential' in args, 'invalid_request')
                proof = server.verify_registration(
                    body['state'], args['credential'], owner['subject']
                )
                verified = VerifiedLogin('passkey', proof.credential_id)
                requested = await request_login_approval(
                    app,
                    verified,
                    owner['subject'],
                    action='bind',
                    request_id=state_id('passkey-bind', challenge),
                )
                approval = {
                    'intent_id': requested.intent_id,
                    'user_code': requested.user_code,
                    'credential': args['credential'],
                }
                body = dict(body, approval=approval)
                save(tx, state_id('passkey', challenge), body)
            else:
                require('credential' not in args, 'invalid_request')
            _, intent = get(tx, approval['intent_id'], now)
            if session_cookie and intent['status'] == 'pending':
                from msg.security.login import approve_browser_login_intent

                await approve_browser_login_intent(app, tx, session_cookie, approval['intent_id'])
                _, intent = get(tx, approval['intent_id'], now)
            if intent['status'] == 'pending':
                return json({'approval_required': True, 'user_code': approval['user_code']})
            require(intent['status'] == 'approved', 'invalid_passkey_challenge')
            proof = server.verify_registration(
                body['state'], approval['credential'], owner['subject']
            )
            verified = VerifiedLogin('passkey', proof.credential_id)
        else:
            require(isinstance(args.get('credential'), dict), 'invalid_request')
            credential_id = b64(unb64(args['credential'].get('rawId'), limit=1023))
            binding = lookup_binding(tx, 'passkey', credential_id)
            require(binding is not None, 'login_binding_required')
            proof = server.verify_authentication(
                body['state'], args['credential'], binding.subject, binding.provider_data
            )
            verified = VerifiedLogin('passkey', proof.credential_id)
        with trusted_passkey(proof, challenge_id=challenge, flow=browser):
            completed = (
                await bind_login(
                    app,
                    verified,
                    request_id=state_id('passkey-bind', challenge),
                    intent_id=approval['intent_id'],
                )
                if registration
                else await complete_login(
                    app, verified, request_id=state_id('passkey-login', challenge)
                )
            )
        require(
            completed.result.status == 'ok',
            completed.result.error.code if completed.result.error else 'login_failed',
        )
    response = json({'ok': True, 'bound': registration})
    secure = urlsplit(app.settings.service_url).scheme == 'https'
    response.set_cookie(
        '__Host-msg_session' if secure else 'msg_session',
        completed.cookie,
        max_age=int((completed.expires - now).total_seconds()),
        secure=secure,
        httponly=True,
        samesite='lax',
        path='/',
    )
    return response
