"""短期邮箱验证码；明文只在发送前留于当前请求内存。"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from msg.core.codec import wire
from msg.core.email_address import validate_address
from msg.core.errors import Failure, require
from msg.security.oauth import get, put, save, state_id

TTL = 600
MAX_ATTEMPTS = 5


def _digest(app, challenge, code):
    return hmac.new(
        app._token_secret, ('msg-email-login-v1:' + challenge + ':' + code).encode(), hashlib.sha256
    ).hexdigest()


def begin_email(app, tx, address, browser, metadata):
    mailbox = validate_address(address)
    now = app.clock()
    require(type(browser) is str and 20 <= len(browser) <= 200, 'invalid_login_browser')
    rate_id = state_id('login-email-rate', mailbox.addr_spec)
    row = tx.one('SELECT expires,body FROM oauth_states WHERE id=?', (rate_id,))
    if row:
        from msg.core.codec import parse_time

        require(parse_time(row[0]) <= now, 'slow_down')
    put(tx, rate_id, 'login-email-rate', now + timedelta(seconds=60), {})
    challenge = secrets.token_urlsafe(32)
    code = ''.join(secrets.choice('0123456789') for _ in range(12))
    body = {
        'browser': state_id('browser', browser),
        'address': mailbox.addr_spec,
        'sub': mailbox.username + '@' + mailbox.domain.lower(),
        'digest': _digest(app, challenge, code),
        'attempts': 0,
        'consumed': False,
        'metadata': metadata,
    }
    put(tx, state_id('login-email', challenge), 'login-email', now + timedelta(seconds=TTL), body)
    return challenge, code


def verify_email(app, tx, challenge, browser, code):
    """失败次数也须提交；调用方在事务退出后才抛出返回的错误。"""
    require(type(challenge) is str and len(challenge) == 43, 'invalid_login_challenge')
    require(type(browser) is str and 20 <= len(browser) <= 200, 'invalid_login_browser')
    id = state_id('login-email', challenge)
    try:
        _, body = get(tx, id, app.clock())
    except Failure:
        return None, 'invalid_login_challenge'
    if (
        body.get('browser') != state_id('browser', browser)
        or body.get('consumed')
        or body.get('attempts', MAX_ATTEMPTS) >= MAX_ATTEMPTS
    ):
        return None, 'invalid_login_challenge'
    body['attempts'] += 1
    valid = (
        type(code) is str
        and len(code) == 12
        and code.isascii()
        and code.isdigit()
        and hmac.compare_digest(body['digest'], _digest(app, challenge, code))
    )
    if valid:
        body['consumed'] = True
        body['consumed_at'] = wire(app.clock())
    save(tx, id, body)
    return (body, None) if valid else (None, 'invalid_login_code')
