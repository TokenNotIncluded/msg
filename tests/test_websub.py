from dataclasses import replace
from datetime import timedelta
from xml.etree import ElementTree as ET

import httpx
import pytest
from test_service import call, register

from msg.config import load_settings
from msg.core.errors import Failure
from msg.transports.http import create_app
from msg.workers.effects import EffectWorker

HUBS = ('https://one.example.org/hub', 'https://two.example.org/hub')


def test_hubs_are_validated_and_deduplicated(tmp_path):
    config = tmp_path / 'msgd.toml'
    base = '[server]\nservice_url="https://msg.example.org"\n[storage]\npostgres_dsn="service=msgd"\n[websub]\n'
    config.write_text(base + 'hubs=["' + '","'.join((*HUBS, HUBS[0])) + '"]\n')
    assert load_settings(tmp_path).websub_hubs == HUBS
    for value in ('["http://one.example.org"]', '["https://127.0.0.1/"]', '"hub"'):
        config.write_text(base + 'hubs=' + value)
        with pytest.raises(Failure):
            load_settings(tmp_path)


@pytest.mark.asyncio
async def test_public_feed_multiple_hubs_independent_retry_and_read_only_discovery(installed):
    app, _ = installed
    key, subject, cert = await register(app, 'websub-author')
    app.settings = replace(app.settings, websub_hubs=HUBS)

    async def write(op, args, **kw):
        result = await call(app, op, args, key=key, subject=subject, certs=(cert,), **kw)
        assert result.status == 'ok', result.error
        return result

    post = await write('content.post_create', {'parent': '/main', 'body': 'public'})
    rid = post.resources[0].id
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='websub'")[0] == 2
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        for path in ('/rss', '/rss.xml', '/-/rss'):
            response = await http.get(path)
            links = ET.fromstring(response.content).findall(
                'channel/{http://www.w3.org/2005/Atom}link'
            )
            assert [(link.get('rel'), link.get('href')) for link in links] == [
                ('self', app.settings.service_url + '/rss.xml'),
                *(('hub', hub) for hub in HUBS),
            ]
            assert all(f'<{hub}>; rel="hub"' in response.headers['link'] for hub in HUBS)
            assert (await http.head(path)).headers['link'] == response.headers['link']
        assert 'rel="hub"' not in (await http.get('/rss.xml?limit=1')).headers.get('link', '')
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='websub'")[0] == 2

    class Sender:
        def __init__(self):
            self.calls = []

        async def send(self, hub, topic):
            self.calls.append((hub, topic))
            return 'retry' if hub == HUBS[0] else 'delivered'

    sender = Sender()
    worker = EffectWorker(app, websub_sender=sender)
    for _ in range(20):
        if len(sender.calls) == 2:
            break
        await worker.run_once()
    assert {hub for hub, topic in sender.calls} == set(HUBS)
    assert all(topic == app.settings.service_url + '/rss.xml' for hub, topic in sender.calls)
    async with app.metadata.transaction(write=False) as tx:
        states = dict(
            tx.rows("SELECT body::json->'arguments'->>'hub',state FROM jobs WHERE kind='websub'")
        )
        assert states == {HUBS[0]: 'pending', HUBS[1]: 'done'}
        retry_id = tx.one("SELECT id FROM jobs WHERE kind='websub' AND state='pending'")[0]
    # Publish is an idempotent hint, so an expired execution lease is retryable.
    async with app.metadata.transaction(write=True) as tx:
        job = await tx.job(retry_id)
        await tx.save_job(
            replace(job, state='running', lease_until=app.clock() - timedelta(seconds=1))
        )
    await worker.run_once()
    async with app.metadata.transaction(write=False) as tx:
        retry = await tx.job(retry_id)
        assert retry.state == 'pending'
        assert retry.next_attempt_at > app.clock()
    # Unrelated events on an unchanged public feed do not cause fanout.
    await write('discussion.like', {'id': rid})
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='websub'")[0] == 2
    await write(
        'content.post_edit',
        {'id': rid, 'expected_revision': post.resources[0].revision, 'body': 'updated'},
        expected=((rid, post.data['generation']),),
    )
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='websub'")[0] == 4
    # Removal from the public feed also notifies hubs, without any private data.
    await write('content.chmod', {'id': rid, 'mode': '0600'}, expected=((rid, 2),))
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='websub'")[0] == 6
    # Worker crashes must not bypass the eight-attempt bound.
    async with app.metadata.transaction(write=True) as tx:
        retry = await tx.job(retry_id)
        await tx.save_job(
            replace(
                retry,
                state='running',
                attempts=8,
                lease_until=app.clock() - timedelta(seconds=1),
            )
        )
    calls_before = list(sender.calls)
    await worker.run_once()
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.job(retry_id)).state == 'failed'
        assert tx.setting('job_status:' + retry_id)['code'] == 'websub_attempts_exhausted'
    assert sender.calls == calls_before
    await write(
        'content.post_edit',
        {
            'id': rid,
            'expected_revision': (
                await call(
                    app, 'discovery.get', {'id': rid}, key=key, subject=subject, certs=(cert,)
                )
            ).data['revision'],
            'body': 'private updated',
        },
        expected=((rid, 3),),
    )
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='websub'")[0] == 6
