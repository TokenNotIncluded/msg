"""Retired identity protocols must never authenticate, even on restored data."""

import importlib.util
from dataclasses import replace

import pytest
from test_browser_sessions import signed_browser as signed_browser
from test_service import call

from msg.browser_config import BrowserConfig, load_browser
from msg.core.errors import Failure
from msg.security.policy import operations_cover
from msg.security.sessions import require_binding


@pytest.mark.parametrize(
    'path',
    [
        '/oauth',
        '/oauth/login',
        '/oauth/device_authorization',
        '/oauth/token',
        '/oauth/authorize',
        '/oauth/userinfo',
        '/oauth/revoke',
        '/oauth/jwks',
        '/oauth/logout',
        '/.well-known/openid-configuration',
        '/.well-known/oauth-authorization-server',
        '/.well-known/oauth-protected-resource',
        '/account/login-methods',
        '/login/google',
        '/login/github',
        '/login/chatgpt',
        '/login/agentid',
        '/login/email',
        '/login/passkey',
        '/-/login/complete',
    ],
)
@pytest.mark.parametrize('method', ['GET', 'POST'])
async def test_retired_endpoints_fail_closed_without_third_party_calls(
    signed_browser, path, method
):
    _, _, _, http = signed_browser
    response = await http.request(method, path)
    assert response.status_code == 410, response.text
    assert response.json()['error'] == 'identity_protocol_removed'
    assert 'location' not in response.headers and 'set-cookie' not in response.headers
    assert 'www-authenticate' not in response.headers


@pytest.mark.parametrize(
    'module',
    [
        'msg.oauth_config',
        'msg.login_config',
        'msg.client_oauth',
        'msg.security.oauth',
        'msg.security.login',
        'msg.security.email_login',
        'msg.security.passkey',
        'msg.workers.sequenzy',
        'msg.transports.oauth_http',
        'msg.transports.account_login',
        'msg.transports.login_providers',
    ],
)
def test_deleted_identity_implementations_are_not_importable(module):
    assert importlib.util.find_spec(module) is None


@pytest.mark.parametrize(
    'name',
    [
        'identity.oauth_request',
        'identity.oauth_approve',
        'identity.login_complete',
        'identity.login_bind',
        'identity.login_remove',
    ],
)
async def test_retired_certificate_declarations_cannot_be_invoked(signed_browser, name):
    app, key, subject, _ = signed_browser
    result = await call(app, name, {}, key=key, subject=subject)
    assert result.status == 'error' and result.error.code == 'operation_disabled'
    assert not app.registry.operation(name, 1).enabled


@pytest.mark.parametrize('prefix', ['t_oauth_', 't_browser_'])
async def test_restored_legacy_token_never_falls_back_to_native_auth(signed_browser, prefix):
    app, key, _, _ = signed_browser
    async with app.metadata.transaction(write=False) as tx:
        parent = await tx.credential(key.key_id)
        with pytest.raises(Failure, match='invalid_grant'):
            await require_binding(
                tx,
                replace(parent, id=prefix + 'restored'),
                app.clock(),
                app.settings.browser,
                custodial_ceiling=app.temporary_ceiling(),
            )


@pytest.mark.parametrize('path', ['/-/p/discovery.get', '/-/mcp', '/@key-owner/in'])
async def test_bearer_header_never_upgrades_or_silently_downgrades_identity(signed_browser, path):
    _, _, _, http = signed_browser
    response = await http.post(path, headers={'Authorization': 'Bearer old-token'})
    assert response.status_code == 401
    assert response.json()['error'] == 'signature_required'
    assert 'www-authenticate' not in response.headers


def test_finite_operation_rename_does_not_grant_other_permissions():
    assert operations_cover({'identity.oauth_approve@1'}, {'identity.session_approve@1'})
    assert not operations_cover(set(), {'identity.session_approve@1'})
    assert not operations_cover({'identity.login_bind@1'}, {'identity.session_approve@1'})
    assert not operations_cover({'identity.oauth_approve@1'}, {'identity.key_add@1'})
    assert not operations_cover({'identity.oauth_approve@2'}, {'identity.session_approve@1'})


@pytest.mark.parametrize(
    'data',
    [
        {'enabled': 'yes'},
        {'session_ttl': True},
        {'session_ttl': 59},
        {'session_ttl': 86401},
        {'provider': 'google'},
        {'client_secret': 'unused'},
    ],
)
def test_browser_config_rejects_unsupported_auth_settings(data):
    with pytest.raises(Failure):
        load_browser(data, 'https://msg.test')


def test_browser_config_requires_a_secure_origin():
    with pytest.raises(Failure):
        load_browser({}, 'http://msg.test')
    assert load_browser({'enabled': False}, 'http://msg.test') == BrowserConfig(enabled=False)


@pytest.mark.parametrize('path', ['/-/p/discovery.get', '/-/mcp', '/@key-owner/in'])
async def test_git_basic_header_does_not_authenticate_other_routes(signed_browser, path):
    _, _, _, http = signed_browser
    response = await http.get(path, headers={'Authorization': 'Basic bmF0aXZlOmNyZWRlbnRpYWw='})
    assert response.status_code == 401
    assert response.json()['error'] == 'signature_required'


async def test_git_exception_rejects_bearer_and_duplicate_authorization(signed_browser):
    _, _, _, http = signed_browser
    path = '/-/git/any/info/refs?service=git-receive-pack'
    for headers in [
        [('Authorization', 'Bearer retired')],
        [('Authorization', 'Basic YTpi'), ('Authorization', 'Basic Yzpk')],
    ]:
        response = await http.get(path, headers=headers)
        assert response.status_code == 401
        assert response.json()['error'] == 'signature_required'
