"""Passive denial and secret rejection must both hold before business dispatch."""

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from msg.core.errors import Failure
from msg.transports.http import create_app


class BoundaryRegistry:
    def operation(self, name, *_args):
        effects = {
            'content.post_create': 'transaction',
            'tool.run': 'external',
            'discovery.get': 'read',
        }
        if name not in effects:
            raise Failure('unknown_operation')
        return SimpleNamespace(name=name, effect=effects[name], entries=('network',))


def service():
    # There is deliberately no executor/storage: a test cannot silently execute
    # a business request on an invented authenticated principal.
    return SimpleNamespace(
        _loaded=True,
        registry=BoundaryRegistry(),
        settings=SimpleNamespace(
            service_url='http://testserver',
            server=SimpleNamespace(
                limits=SimpleNamespace(
                    max_path_bytes=8192, max_request_bytes=1048576, max_response_bytes=1048576
                )
            ),
        ),
    )


@pytest.mark.parametrize(
    'headers',
    [
        {'User-Agent': 'Discordbot/2.0'},
        {'User-Agent': 'Mozilla/5.0'},
        {'User-Agent': 'AgentRuntime/1', 'Purpose': 'prefetch'},
        {'User-Agent': 'AgentRuntime/1', 'Sec-Purpose': 'prerender'},
    ],
)
@pytest.mark.parametrize('operation', ['content.post_create', 'tool.run'])
@pytest.mark.parametrize(
    'target',
    [
        '/-/g/{op}/token/{secret}',
        '/-/g/{op}/%2574oken/{secret}',
        '/-/g/{op}/j/invalid?recovery_secret={secret}',
    ],
)
async def test_passive_write_get_preserves_403_without_echo_or_execution(
    headers, operation, target
):
    secret = 'nonlive_' + uuid4().hex
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service())), base_url='http://testserver'
    ) as http:
        response = await http.get(target.format(op=operation, secret=secret), headers=headers)
    assert response.status_code == 403
    assert response.json()['error']['code'] == 'passive_client_forbidden'
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert response.headers['x-robots-tag'] == 'noindex, nofollow'
    assert secret not in response.text + str(response.headers)


@pytest.mark.parametrize('method', ['GET', 'HEAD', 'POST'])
async def test_nonpassive_secret_url_still_fails_the_strict_boundary(method):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service())), base_url='http://testserver'
    ) as http:
        response = await http.request(
            method,
            '/-/g/content.post_create/token/nonlive',
            headers={'User-Agent': 'AgentRuntime/1'},
        )
    assert response.status_code == 400
    if method != 'HEAD':
        assert response.json()['error']['code'] == 'secure_channel_required'


async def test_read_only_operation_does_not_get_a_write_guard():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service())), base_url='http://testserver'
    ) as http:
        read = await http.get(
            '/-/g/discovery.get/token/nonlive', headers={'User-Agent': 'Discordbot/2'}
        )
        health = await http.get('/healthz', headers={'User-Agent': 'Discordbot/2'})
        oversized = await http.get(
            '/-/g/content.post_create/j/' + 'x' * 8192, headers={'User-Agent': 'Discordbot/2'}
        )
    assert read.status_code == 400
    assert read.json()['error']['code'] == 'secure_channel_required'
    assert health.status_code == 200
    assert oversized.status_code == 413
    assert oversized.json()['error']['code'] == 'path_too_large'
