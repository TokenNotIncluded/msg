"""真实 PostgreSQL 验证登录暂态的单次、浏览器绑定和失败次数。"""

from datetime import timedelta

import pytest

from msg.core.errors import Failure
from msg.security.email_login import begin_email, verify_email
from msg.security.login_flow import consume_flow, receive_callback, start_flow
from msg.security.oauth import secret


async def test_oauth_callback_does_not_create_account_and_encrypts_code(installed):
    app, _ = installed
    browser, code = secret(), secret()
    async with app.metadata.transaction(write=True) as tx:
        before = {
            table: tx.one('SELECT COUNT(*) FROM ' + table)[0]
            for table in (
                'identities',
                'credentials',
                'identity_keys',
                'encryption_subkeys',
                'audit',
            )
        }
        state, challenge, nonce = start_flow(app, tx, 'google', browser)
        assert len(challenge) == len(nonce) == 43
        receive_callback(app, tx, state, browser, 'google', code)
        rows = tx.rows("SELECT body FROM oauth_states WHERE kind='login-flow'")
        assert len(rows) == 1 and code not in rows[0][0]
        after = {table: tx.one('SELECT COUNT(*) FROM ' + table)[0] for table in before}
        assert after == before
    async with app.metadata.transaction(write=True) as tx:
        returned = consume_flow(app, tx, state, browser)
        assert returned['code'] == code
        assert returned['provider'] == 'google'
    async with app.metadata.transaction(write=True) as tx:
        with pytest.raises(Failure, match='invalid_login_transaction'):
            consume_flow(app, tx, state, browser)


async def test_oauth_state_rejects_wrong_browser_provider_and_replayed_callback(installed):
    app, _ = installed
    browser = secret()
    async with app.metadata.transaction(write=True) as tx:
        state, _, _ = start_flow(app, tx, 'github', browser)
        with pytest.raises(Failure, match='invalid_login_transaction'):
            receive_callback(app, tx, state, secret(), 'github', 'code')
        with pytest.raises(Failure, match='invalid_login_transaction'):
            receive_callback(app, tx, state, browser, 'google', 'code')
        receive_callback(app, tx, state, browser, 'github', 'code')
        with pytest.raises(Failure, match='invalid_login_transaction'):
            receive_callback(app, tx, state, browser, 'github', 'code')


async def test_only_google_can_start_oauth_registration(installed):
    app, _ = installed
    async with app.metadata.transaction(write=True) as tx:
        for provider in ('github', 'chatgpt'):
            with pytest.raises(Failure, match='login_registration_forbidden'):
                start_flow(app, tx, provider, secret(), mode='register')
        with pytest.raises(Failure, match='authentication_required'):
            start_flow(app, tx, 'google', secret(), mode='bind')


async def test_email_code_single_use_and_browser_bound(installed):
    app, _ = installed
    browser = secret()
    async with app.metadata.transaction(write=True) as tx:
        challenge, code = begin_email(app, tx, 'Alice@example.org', browser, {'mode': 'login'})
        rows = tx.rows("SELECT body FROM oauth_states WHERE kind='login-email'")
        assert code not in rows[0][0]
    async with app.metadata.transaction(write=True) as tx:
        assert verify_email(app, tx, challenge, secret(), code)[1] == 'invalid_login_challenge'
        body, error = verify_email(app, tx, challenge, browser, code)
        assert error is None and body['sub'] == 'Alice@example.org'
    async with app.metadata.transaction(write=True) as tx:
        assert verify_email(app, tx, challenge, browser, code)[1] == 'invalid_login_challenge'


async def test_email_five_bad_guesses_remain_blocked_after_commit(installed):
    app, _ = installed
    browser = secret()
    async with app.metadata.transaction(write=True) as tx:
        challenge, code = begin_email(app, tx, 'alice@example.org', browser, {})
    for _ in range(5):
        async with app.metadata.transaction(write=True) as tx:
            assert verify_email(app, tx, challenge, browser, 'wrong')[1] == 'invalid_login_code'
    async with app.metadata.transaction(write=True) as tx:
        assert verify_email(app, tx, challenge, browser, code)[1] == 'invalid_login_challenge'
        with pytest.raises(Failure, match='slow_down'):
            begin_email(app, tx, 'alice@example.org', browser, {})


async def test_email_and_oauth_expire(installed):
    app, _ = installed
    browser = secret()
    async with app.metadata.transaction(write=True) as tx:
        challenge, code = begin_email(app, tx, 'alice@example.org', browser, {})
        state, _, _ = start_flow(app, tx, 'google', browser)
    now = app.clock()
    app.clock = lambda: now + timedelta(seconds=601)
    async with app.metadata.transaction(write=True) as tx:
        assert verify_email(app, tx, challenge, browser, code)[1] == 'invalid_login_challenge'
        with pytest.raises(Failure, match='invalid_grant'):
            receive_callback(app, tx, state, browser, 'google', 'code')
