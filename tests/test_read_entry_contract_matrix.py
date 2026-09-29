"""A signed query and its bounded continuation retain meaning across dispatchers."""
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from test_read_projection_cache_transports import forbid_read_effects
from test_route_effect_matrix import database_snapshot
from test_service import NOW, call, register

from msg.core.codec import canonical, wire
from msg.core.requests import request_for
from msg.transports.client import TRANSPORTS
from msg.transports.http import create_app


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,version', [
    ('discovery.read_query', 1), ('discovery.read_query', 3),
    ('discovery.lexical_search', 5), ('communication.sync', 1),
])
async def test_signed_filter_sort_page_and_budget_across_four_entries(
        installed, monkeypatch, operation, version):
    app, _ = installed
    key, subject, _ = await register(app, 'entry-matrix')
    ids = []
    bodies = ['matrixneedle alpha', 'matrixneedle beta', 'unmatched']
    if operation == 'discovery.lexical_search':
        bodies += ['matrixneedle alpha unmatched', 'alpha without phrase']
    for body in bodies:
        result = await call(app, 'content.post_create', {'parent': '/main', 'body': body},
                            key=key, subject=subject)
        assert result.status == 'ok', wire(result)
        ids.append(result.resources[0].id)
    if operation == 'discovery.read_query':
        arguments = {'parent': '/main', 'type': 'post', 'author': subject,
                     'query': 'matrixneedle', 'sort': 'id', 'direction': 'desc',
                     'fields': ['id'], 'limit': 1}
        if version == 3:
            arguments['query_version'] = 3
    elif operation == 'discovery.lexical_search':
        arguments = {'scope': {'resource_refs': [{'id': 't_main'}]},
                     'terms': 'alpha beta', 'mode': 'any', 'exact': 'matrixneedle',
                     'not_terms': 'unmatched', 'type': 'post', 'author': subject,
                     'order': 'created', 'fields': ['id'], 'limit': 1}
    else:
        # Sync has event order and a limit, not user-defined sort/filter.
        arguments = {'limit': 1}
    before = await database_snapshot(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        adapters = [adapter(app.settings.service_url, http=http)
                    for adapter in TRANSPORTS.values()]

        async def equivalent(args):
            packet = request_for(operation, args, app.settings.service_url,
                signer=key, subject=subject, contract_version=version,
                expires_at=NOW + timedelta(seconds=90))
            signed_bytes = canonical(packet)
            results = [await adapter.call(packet) for adapter in adapters]
            assert canonical(packet) == signed_bytes  # No resigning/argument normalization.
            assert all(wire(result) == wire(results[0]) for result in results[1:])
            return results[0]

        with forbid_read_effects(app, monkeypatch):
            first = await equivalent(arguments)
            assert first.status == 'ok', wire(first)
            assert len(first.data['items']) == 1
            cursor = first.data.get('cursor') or first.data.get('sync_cursor')
            assert cursor
            second = await equivalent({'cursor': cursor})
            assert second.status == 'ok', wire(second)
            if operation != 'communication.sync':
                assert len(second.data['items']) == 1
                items = first.data['items'] + second.data['items']
                found = [item.get('id') or item['ref']['id'] for item in items]
                assert set(found) == set(ids[:2])
                if operation == 'discovery.read_query':
                    assert found == sorted(ids[:2], reverse=True)
            else:
                assert first.data['sync_cursor'] != second.data['sync_cursor']
            denied = await equivalent({**arguments, 'limit': 101})
            assert denied.status == 'error', wire(denied)
            assert denied.error.code == ('query_cost_exceeded' if
                operation == 'discovery.read_query' and version == 1 else 'schema_validation')
            if operation == 'discovery.read_query' and version == 3:
                monkeypatch.setattr(app, 'settings', replace(app.settings,
                    server=replace(app.settings.server, limits=replace(
                        app.settings.server.limits, max_response_bytes=1500))))
                oversized = await equivalent(arguments)
                assert oversized.status == 'error', wire(oversized)
                assert oversized.error.code == 'response_too_large'
            if operation == 'communication.sync':
                unsupported = await equivalent({'limit': 1, 'sort': 'id', 'type': 'post'})
                assert unsupported.status == 'error', wire(unsupported)
                assert unsupported.error.code == 'schema_validation'
    assert await database_snapshot(app) == before
