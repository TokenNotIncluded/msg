"""Listener restart must not turn a cached page into an authority bypass."""

import asyncio
import io
import json
import os
import time
from pathlib import Path

import httpx
import pytest
from test_client_link import args, linked, run_command
from test_client_subagents_remote import client_for

from msg.client_listener import listen
from msg.client_subagents_remote import RemoteAgents
from msg.core.errors import Failure
from msg.transports.http import create_app


def context(client, label):
    return {'server': client.state.server, 'subject': client.state.subject,
            'source': 'remote', 'agent': label}


async def test_same_account_lesser_authority_and_revocation_cannot_replay_cache(installed, tmp_path):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url,
    ) as http:
        owner, helper, _, _ = await linked(app, tmp_path, http)
        full, limited = RemoteAgents(owner), RemoteAgents(helper)
        assert owner.state.subject == helper.state.subject
        await full.create('private')
        start = await full.inbox('private', tail=True)
        for number in range(2):
            await full.send('private', 'private', 'owner-only-result', message_id=f'private-{number}')
        checkpoint = tmp_path / 'owner.json'

        async def owner_fetch(cursor, tail=False):
            return await full.inbox('private', cursor=cursor, tail=tail)

        await listen(owner_fetch, context=context(owner, 'private'), cursor_only=True,
                     cursor=start['cursor'], cursor_file=checkpoint, max_events=1,
                     output=io.StringIO())
        assert 'owner-only-result' not in checkpoint.read_text()

        async def limited_fetch(cursor, tail=False):
            return await limited.inbox('private', cursor=cursor, tail=tail)

        output = io.StringIO()
        with pytest.raises(Failure, match='credential_ceiling'):
            await listen(limited_fetch, context=context(helper, 'private'), cursor_only=True,
                         cursor_file=checkpoint, once=True, output=output)
        assert output.getvalue() == ''

        start = await limited.inbox('reviewer', tail=True)
        for number in range(2):
            await full.send('reviewer-lead', 'reviewer', 'task-result', message_id=f'task-{number}')
        checkpoint = tmp_path / 'helper.json'

        async def helper_fetch(cursor, tail=False):
            return await limited.inbox('reviewer', cursor=cursor, tail=tail)

        await listen(helper_fetch, context=context(helper, 'reviewer'), cursor_only=True,
                     cursor=start['cursor'], cursor_file=checkpoint, max_events=1,
                     output=io.StringIO())
        assert 'task-result' not in checkpoint.read_text()
        await run_command(owner, args('revoke', name='reviewer'))
        with pytest.raises(Failure, match='authority_source_inactive'):
            await listen(helper_fetch, context=context(helper, 'reviewer'), cursor_only=True,
                         cursor_file=checkpoint, once=True, output=output)
        assert output.getvalue() == ''


async def test_remote_idle_cost_records_actual_http_requests(installed, tmp_path):
    """ASGI HTTP + real PostgreSQL, not a wide-area-network latency benchmark."""
    app, _ = installed
    client, http = await client_for(app, tmp_path / 'idle', 'idle-bench')
    store = RemoteAgents(client)
    rows = []
    try:
        await store.create('worker')
        initial = await store.inbox('worker', tail=True)
        counts = {'requests': 0}

        async def count_request(request):
            counts['requests'] += 1

        http.event_hooks['request'].append(count_request)
        for mode, maximum in [('fixed_poll', 0.05), ('adaptive_poll', 0.4)]:
            ready = asyncio.Event()
            reads = 0

            async def fetch(cursor, tail=False):
                nonlocal reads
                result = await store.inbox('worker', cursor=cursor, tail=tail)
                reads += 1
                ready.set()
                return result

            output = io.StringIO()
            task = asyncio.create_task(listen(
                fetch, context=context(client, 'worker'), cursor=initial['cursor'],
                cursor_only=True, interval=0.05, max_idle_interval=maximum, output=output,
            ))
            try:
                await asyncio.wait_for(ready.wait(), 10)
                requests_before, reads_before = counts['requests'], reads
                started, cpu = time.perf_counter(), time.process_time()
                await asyncio.sleep(2)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            row = {
                'mode': mode, 'status': 'measured', 'transport': 'in_process_http',
                'database': 'real_postgresql', 'poll_interval_seconds': 0.05,
                'max_idle_interval_seconds': maximum,
                'idle_seconds': time.perf_counter() - started,
                'idle_cpu_seconds_client_and_server': time.process_time() - cpu,
                'idle_http_requests_started': counts['requests'] - requests_before,
                'idle_source_reads_completed': reads - reads_before,
            }
            assert row['idle_http_requests_started'] > 0 and output.getvalue() == ''
            rows.append(row)
    finally:
        await http.aclose()
    target = Path(os.environ.get('MSG_BENCHMARK_OUTPUT', tmp_path))
    target.mkdir(parents=True, exist_ok=True)
    (target / 'remote-idle.json').write_text(json.dumps({
        'results': rows,
        'warning': 'Two-second idle windows with shortened intervals. No WAN or harness ranking.',
        'remote_events': {'status': 'not_implemented'},
    }, indent=2) + '\n')
