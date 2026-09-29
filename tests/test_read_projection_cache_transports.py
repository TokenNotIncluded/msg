"""Actual clients may reuse default compact projections only under live authority."""

import hashlib
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_route_effect_matrix import database_snapshot
from test_share_transport_matrix import live_adapters, ok

from msg.core.codec import canonical, digest, wire
from msg.workers.mail import SmtpSender
from msg.workers.sandbox import BubblewrapRunner
from msg.workers.webhook import WebhookSender


def file_snapshot(directory):
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in Path(directory).rglob('*')
        if path.is_file()
    }


@contextmanager
def forbid_read_effects(app, monkeypatch):
    """Keep real read adapters; fail on content writes, job wakes or external sends."""
    spies = []
    with monkeypatch.context() as patch:
        for name in ('put_bytes', 'pin', 'commit_revision'):
            spy = AsyncMock(side_effect=AssertionError('read attempted content mutation'))
            patch.setattr(app.contents, name, spy)
            spies.append(spy)
        for owner, name in (
            (SmtpSender, 'send'),
            (WebhookSender, 'send'),
            (BubblewrapRunner, '__call__'),
        ):
            spy = AsyncMock(side_effect=AssertionError('read attempted external effect'))
            patch.setattr(owner, name, spy)
            spies.append(spy)
        signal = AsyncMock(side_effect=AssertionError('read attempted job notification'))
        patch.setattr(app.metadata, 'signal', SimpleNamespace(publish_pending=signal))
        spies.append(signal)
        yield
        for spy in spies:
            spy.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['http', 'path_get', 'graphql', 'mcp_http', 'cli', 'mcp_stdio'])
async def test_default_compact_cache_is_reusable_but_never_authorizes_a_read(
    tmp_path, pg_dsn, monkeypatch, mode
):
    async with live_adapters(tmp_path, pg_dsn, mode) as (app, peers, invoke):
        owner, _, reader = peers
        marker = 'cache-private-' + uuid4().hex
        post = ok(await owner.call('content.post_create', {'parent': '/main', 'body': marker}))
        rid, original = post['resources'][0]['id'], post['resources'][0]['revision']
        private = ok(
            await owner.call(
                'content.chmod',
                {'id': rid, 'mode': '0600'},
                expected=((rid, post['data']['generation']),),
            )
        )
        ok(
            await owner.call(
                'content.post_edit',
                {'id': rid, 'expected_revision': original, 'body': marker + '\nsecond revision'},
                expected=((rid, private['data']['generation']),),
            )
        )
        grant = ok(
            await owner.call(
                'sharing.grant',
                {
                    'resource': rid,
                    'operations': ['read'],
                    'grantee': reader.state.subject,
                    'grantee_kind': 'user',
                    'expires_at': wire(app.clock() + timedelta(minutes=30)),
                },
                contract_version=2,
            )
        )
        grant_id = grant['data']['grant']['id']
        before = await database_snapshot(app)
        files = file_snapshot(tmp_path / 'data')
        queries = (
            {'id': rid},
            {'id': rid, 'revision': original},
            {'id': rid, 'view': 'meta'},
            {'id': rid, 'view': 'history'},
        )
        cached = []
        with forbid_read_effects(app, monkeypatch):
            for query in queries:
                first = ok(await invoke('discovery.get', query))
                validator = digest(first['data'])
                conditional = {**query, 'known_digest': validator}
                second = ok(await invoke('discovery.get', conditional))
                assert second['data'] == {'not_modified': True, 'digest': validator}
                # A wrong validator must produce the exact original projection.
                stale = ok(
                    await invoke('discovery.get', {**query, 'known_digest': digest('stale')})
                )
                assert stale['data'] == first['data']
                cached.append(conditional)
        assert await database_snapshot(app) == before
        assert file_snapshot(tmp_path / 'data') == files

        ok(await owner.call('sharing.revoke', {'grant_id': grant_id}, contract_version=2))
        before = await database_snapshot(app)
        files = file_snapshot(tmp_path / 'data')
        with forbid_read_effects(app, monkeypatch):
            for query in (*queries, *cached):
                denied = await invoke('discovery.get', query)
                assert denied['status'] == 'error', denied
                assert denied['error']['code'] == 'permission_denied', denied
                assert marker not in canonical(denied).decode()
                assert 'not_modified' not in denied.get('data', {})
        assert await database_snapshot(app) == before
        assert file_snapshot(tmp_path / 'data') == files
