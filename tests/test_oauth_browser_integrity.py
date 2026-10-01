"""Authentication-page behavior and CSRF failure paths without a database."""

from base64 import b64encode
from hashlib import sha256
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from msg.core.errors import Failure
from msg.transports.oauth_http import HEADERS, OAuthBoundary, csrf, page
from msg.transports.webmcp import WEBMCP_HASH


def test_oauth_csp_never_authorizes_a_fixed_nonce():
    assert 'nonce-msg' not in HEADERS['Content-Security-Policy']
    response = page('Consent', '<p>Read-only permission</p>')
    assert 'nonce-msg' not in response.headers['content-security-policy']
    assert "'sha256-" + WEBMCP_HASH + "'" in response.headers['content-security-policy']


def test_optional_page_script_is_pinned_to_exact_bytes():
    script = 'document.documentElement.dataset.test = "ok";'
    response = page('Test', '<p>Body</p>', script=script)
    expected = b64encode(sha256(script.encode()).digest()).decode()
    assert f"'sha256-{expected}'" in response.headers['content-security-policy']
    assert f'<script>{script}</script>' in response.body.decode()
    # The extra authorization must not leak into later responses or the base policy.
    assert expected not in HEADERS['Content-Security-Policy']
    assert expected not in page('Other', '').headers['content-security-policy']


@pytest.mark.parametrize('token', ['非ASCII', '', 'wrong', None, ['wrong']])
def test_invalid_csrf_values_fail_with_a_controlled_error(token):
    boundary = OAuthBoundary.__new__(OAuthBoundary)
    boundary.service = SimpleNamespace(settings=SimpleNamespace(service_url='https://msg.test'))
    request = Request({'type': 'http', 'headers': [
        (b'cookie', b'msg_login=opaque-cookie'), (b'origin', b'https://msg.test'),
    ]})
    with pytest.raises(Failure, match='invalid_request'):
        boundary.require_csrf(request, {'csrf': token}, 'msg_login')


def test_valid_csrf_still_requires_the_origin_and_cookie():
    boundary = OAuthBoundary.__new__(OAuthBoundary)
    boundary.service = SimpleNamespace(settings=SimpleNamespace(service_url='https://msg.test'))
    for origin, good in [(b'https://msg.test', True), (b'https://other.test', False)]:
        request = Request({'type': 'http', 'headers': [
            (b'cookie', b'msg_login=opaque-cookie'), (b'origin', origin),
        ]})
        if good:
            assert boundary.require_csrf(request, {'csrf': csrf('opaque-cookie')}, 'msg_login') == 'opaque-cookie'
        else:
            with pytest.raises(Failure, match='invalid_request'):
                boundary.require_csrf(request, {'csrf': csrf('opaque-cookie')}, 'msg_login')
