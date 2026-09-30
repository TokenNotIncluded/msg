from dataclasses import replace

import pytest

from msg.core.errors import Failure
from msg.core.models import NetworkPolicy
from msg.security.network import validate_url


@pytest.fixture
def policy():
    return NetworkPolicy(
        schemes=frozenset({'http', 'https'}),
        hosts=(),
        ports=frozenset({80, 443}),
        methods=frozenset({'GET'}),
        allow_private=False,
        timeout_ms=1000,
        max_response_bytes=10000,
        max_redirects=3,
    )


@pytest.mark.parametrize('scheme', ['http', 'https'])
def test_explicit_zero_port_does_not_inherit_scheme_default(scheme, policy):
    with pytest.raises(Failure, match='network_port_forbidden'):
        validate_url(f'{scheme}://example.org:0/path', 'GET', policy)


@pytest.mark.parametrize('port', ['-1', '65536', 'invalid'])
def test_invalid_explicit_port_is_a_network_url_failure(port, policy):
    with pytest.raises(Failure, match='invalid_network_url'):
        validate_url(f'https://example.org:{port}/path', 'GET', policy)


@pytest.mark.parametrize(('scheme', 'port'), [('http', 80), ('https', 443)])
def test_default_port_and_explicit_port_obey_same_allowlist(scheme, port, policy):
    implicit = f'{scheme}://example.org/path'
    explicit = f'{scheme}://example.org:{port}/path'
    assert validate_url(implicit, 'GET', policy).port is None
    assert validate_url(explicit, 'GET', policy).port == port
    denied = replace(policy, ports=frozenset({8443}))
    for url in (implicit, explicit):
        with pytest.raises(Failure, match='network_port_forbidden'):
            validate_url(url, 'GET', denied)
