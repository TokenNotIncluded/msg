"""绑定浏览器的短期 OAuth 事务；GET 回调只保存暂态，不修改账号。"""

import hmac
import os
from datetime import timedelta

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from msg.core.codec import b64, unb64
from msg.core.errors import Failure, require
from msg.login_config import OAUTH_PROVIDERS
from msg.security.oauth import get, put, save, secret, state_id

TTL = 600


def _seal(app, state, value):
    nonce = os.urandom(12)
    aad = ('msg-login-flow-v1:' + state).encode()
    return b64(nonce + AESGCM(app._vault_key).encrypt(nonce, value.encode(), aad))


def _open(app, state, value):
    try:
        raw = unb64(value, limit=8192)
        return (
            AESGCM(app._vault_key)
            .decrypt(raw[:12], raw[12:], ('msg-login-flow-v1:' + state).encode())
            .decode()
        )
    except InvalidTag, UnicodeError, ValueError, Failure:
        raise Failure('invalid_login_transaction') from None


def start_flow(app, tx, provider, browser, *, mode='login', source=None, handle=None):
    require(provider in OAUTH_PROVIDERS, 'login_provider_disabled')
    require(mode in {'login', 'register', 'bind'}, 'invalid_login_mode')
    require(mode != 'register' or provider == 'google', 'login_registration_forbidden')
    require(mode != 'bind' or source is not None, 'authentication_required')
    state, verifier, nonce = secret(), secret(), secret()
    body = {
        'provider': provider,
        'browser': state_id('browser', browser),
        'mode': mode,
        'source': source,
        'handle': handle,
        'verifier': _seal(app, state, verifier),
        'nonce': nonce,
        'phase': 'pending',
    }
    put(tx, state_id('login-flow', state), 'login-flow', app.clock() + timedelta(seconds=TTL), body)
    return state, verifier, nonce


def flow(app, tx, state, browser, *, provider=None):
    require(type(state) is str and len(state) == 43, 'invalid_login_transaction')
    require(type(browser) is str and len(browser) >= 20, 'invalid_login_transaction')
    _, body = get(tx, state_id('login-flow', state), app.clock())
    require(
        hmac.compare_digest(body['browser'], state_id('browser', browser))
        and (provider is None or provider == body['provider']),
        'invalid_login_transaction',
    )
    return body


def receive_callback(app, tx, state, browser, provider, code):
    body = flow(app, tx, state, browser, provider=provider)
    require(body['phase'] == 'pending', 'invalid_login_transaction')
    require(type(code) is str and 1 <= len(code) <= 4096, 'invalid_login_transaction')
    body.update(phase='returned', code=_seal(app, state, code))
    save(tx, state_id('login-flow', state), body)


def consume_flow(app, tx, state, browser):
    body = flow(app, tx, state, browser)
    require(body['phase'] == 'returned', 'invalid_login_transaction')
    snapshot = dict(
        body, code=_open(app, state, body['code']), verifier=_open(app, state, body['verifier'])
    )
    # 先消耗事务再访问外部提供方；故障后重开流程，不重放外部授权码。
    body.update(phase='consumed')
    body.pop('code', None)
    body.pop('verifier', None)
    save(tx, state_id('login-flow', state), body)
    return snapshot
