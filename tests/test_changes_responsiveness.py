"""Large committed histories cannot monopolize the HTTP event loop."""

import asyncio
import json
from dataclasses import replace
from time import perf_counter
from types import SimpleNamespace

import pytest
from test_client_subagents_remote import client_for
from test_service import NOW

from msg.cli import parser
from msg.client_agent_cli import run_remote
from msg.client_subagents_remote import RemoteAgents
from msg.core.codec import b64, canonical
from msg.core.models import Event, ResourceRef
from msg.plugins import communication
from msg.storage.postgres import PostgresSession


async def make_file(client, username, name):
    result = client.checked(
        await client.call(
            'file.create',
            {
                'parent': '/@' + username + '/files',
                'name': name,
                'data': b64(b'private content'),
            },
        )
    )
    return result.resources[0].id


async def append_history(app, actor, resource, count, prefix):
    async with app.metadata.transaction(write=True) as tx:
        for number in range(count):
            await tx.append_event(
                Event(
                    id=prefix + str(number),
                    type='file.create',
                    time=NOW,
                    request_id=prefix + str(number),
                    actor=actor,
                    subject=actor,
                    resources=(ResourceRef(id=resource),) if resource else (),
                    data={'operation': 'file.create'},
                )
            )


async def wait_for_scan_phase(signal, pending):
    """A later phase can start after real ACL work, within the 30s operation budget."""
    phase = asyncio.create_task(signal.wait())
    try:
        done, _ = await asyncio.wait(
            (phase, pending), timeout=35, return_when=asyncio.FIRST_COMPLETED
        )
        if phase in done:
            return
        if pending in done:
            result = await pending
            error = result.error.code if result.error is not None else result.status
            pytest.fail(f'change scan exited before the observed phase: {error}')
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        pytest.fail('change scan did not reach the observed phase within its operation budget')
    finally:
        phase.cancel()
        await asyncio.gather(phase, return_exceptions=True)


@pytest.mark.asyncio
async def test_sparse_history_is_bounded_and_keeps_health_and_signed_read_responsive(
    installed,
    tmp_path,
    monkeypatch,
):
    app, _ = installed
    owner, http = await client_for(app, tmp_path / 'owner', 'fair-owner')
    stranger, other_http = await client_for(app, tmp_path / 'other', 'fair-other')
    try:
        own_file = await make_file(owner, 'fair-owner', 'visible.txt')
        hidden_file = await make_file(stranger, 'fair-other', 'hidden.txt')
        baseline = owner.checked(await owner.call('communication.changes', {})).data
        initial = baseline['tail_cursor']
        await append_history(app, stranger.state.subject, hidden_file, 1024, 'hidden-')
        await append_history(app, owner.state.subject, own_file, 3, 'visible-')
        started = asyncio.Event()
        original_visible = communication.visible
        authorization_seconds = []

        async def observe(*args):
            started.set()
            start = perf_counter()
            result = await original_visible(*args)
            authorization_seconds.append(perf_counter() - start)
            return result

        monkeypatch.setattr(communication, 'visible', observe)
        begin = perf_counter()
        pending = asyncio.create_task(
            owner.call(
                'communication.changes',
                {
                    'cursor': initial,
                    'limit': 200,
                },
            )
        )
        await asyncio.wait_for(started.wait(), 5)
        assert not pending.done()
        health_begin = perf_counter()
        health = await http.get('/healthz')
        health_seconds = perf_counter() - health_begin
        assert health.status_code == 200
        checked_at_health = len(authorization_seconds)
        assert checked_at_health <= 64
        read_begin = perf_counter()
        owner.checked(
            await owner.call(
                'discovery.get',
                {
                    'id': owner.state.subject,
                    'fields': ['id', 'name'],
                },
            )
        )
        read_seconds = perf_counter() - read_begin
        first = owner.checked(await pending).data
        elapsed = perf_counter() - begin
        assert not first['items'] and first['has_more']
        position = app.cursors.decode(first['sync_cursor'], 'sync', owner.state.subject)['seq']
        origin = app.cursors.decode(initial, 'sync', owner.state.subject)['seq']
        assert 0 < position - origin <= 64
        assert hidden_file not in repr(first)
        assert 'hidden.txt' not in repr(first)
        # Shared-runner load affects wall time. The operation must still return
        # after bounded raw-event work rather than draining the hidden history.
        assert len(authorization_seconds) <= 64
        authorizer_200 = sum(authorization_seconds)
        authorization_seconds.clear()
        default_begin = perf_counter()
        default_page = owner.checked(
            await owner.call('communication.changes', {'cursor': initial, 'limit': 50})
        ).data
        default_seconds = perf_counter() - default_begin
        authorizer_50 = sum(authorization_seconds)
        assert not default_page['items'] and default_page['has_more']
        assert 0 < len(authorization_seconds) <= 64

        cursor, last_empty, pages = first['sync_cursor'], first['sync_cursor'], 1
        while True:
            page = owner.checked(
                await owner.call(
                    'communication.changes',
                    {
                        'cursor': cursor,
                        'limit': 1,
                    },
                )
            ).data
            pages += 1
            if page['items']:
                assert page['items'][0]['id'] == 'visible-0'
                break
            assert page['has_more'] and page['sync_cursor'] != cursor
            last_empty = cursor = page['sync_cursor']
            assert pages <= 1026
        # Two independent readers can resume the same protected cursor. Equal
        # timestamps and an insertion between their pages do not lose events.
        cursors, found = [last_empty, last_empty], [[], []]
        inserted = False
        for _ in range(10):
            for reader in range(2):
                page = owner.checked(
                    await owner.call(
                        'communication.changes',
                        {
                            'cursor': cursors[reader],
                            'limit': 1,
                        },
                    )
                ).data
                cursors[reader] = page['sync_cursor']
                found[reader].extend(event['id'] for event in page['items'])
            if found[0] and not inserted:
                await append_history(app, owner.state.subject, own_file, 1, 'late-')
                inserted = True
            if all(len(events) == 4 for events in found):
                break
        assert found == [['visible-0', 'visible-1', 'visible-2', 'late-0']] * 2
        print({
            'history_events': 1024,
            'first_page_seconds': round(elapsed, 3),
            'limit_50_seconds': round(default_seconds, 3),
            'limit_200_authorization_seconds': round(authorizer_200, 3),
            'limit_50_authorization_seconds': round(authorizer_50, 3),
            'health_seconds': round(health_seconds, 3),
            'authorization_checks_at_health': checked_at_health,
            'signed_read_seconds': round(read_seconds, 3),
            'first_page_scanned': position - origin,
            'pages_to_visible': pages,
        })
    finally:
        await http.aclose()
        await other_http.aclose()


@pytest.mark.asyncio
async def test_remote_inbox_stops_after_eight_sparse_pages_and_resumes(installed, tmp_path):
    app, _ = installed
    owner, http = await client_for(app, tmp_path / 'owner', 'fair-inbox')
    stranger, other_http = await client_for(app, tmp_path / 'other', 'fair-inbox-other')
    agents = RemoteAgents(owner)
    try:
        await agents.create('sender')
        await agents.create('recipient')
        tail = await agents.inbox('recipient', tail=True)
        # Resource-free events belonging to a different signed account still
        # consume the raw history budget, even though none can be returned.
        await append_history(app, stranger.state.subject, None, 512, 'sparse-inbox-')
        await agents.send(
            'sender', 'recipient', 'after the sparse pages', message_id='after-sparse'
        )
        original = owner.transport.call
        changes = []

        async def counted(packet):
            if packet.operation == 'communication.changes':
                changes.append(packet)
            return await original(packet)

        owner.transport.call = counted
        first = await agents.inbox('recipient', cursor=tail['cursor'], limit=1)
        assert first['items'] == [] and first['has_more']
        assert first['cursor'] != tail['cursor'] and len(changes) == 8
        cursor = first['cursor']
        for _ in range(70):
            changes.clear()
            page = await agents.inbox('recipient', cursor=cursor, limit=1)
            assert len(changes) <= 8
            assert page['cursor'] != cursor
            cursor = page['cursor']
            if page['items']:
                assert [item['id'] for item in page['items']] == ['after-sparse']
                break
            assert page['has_more']
        else:
            pytest.fail('bounded inbox did not resume to the late mailbox message')
    finally:
        await http.aclose()
        await other_http.aclose()


@pytest.mark.asyncio
async def test_account_listener_once_and_from_now_follow_sparse_pages(installed, tmp_path, capsys):
    app, _ = installed
    owner, http = await client_for(app, tmp_path / 'owner', 'fair-listener')
    stranger, other_http = await client_for(app, tmp_path / 'other', 'fair-listener-other')
    try:
        own_file = await make_file(owner, 'fair-listener', 'visible.txt')
        initial = owner.checked(await owner.call('communication.changes', {})).data['tail_cursor']
        await append_history(app, stranger.state.subject, None, 130, 'listener-hidden-')
        await append_history(app, owner.state.subject, own_file, 1, 'listener-visible-')
        args = parser().parse_args([
            'listen',
            '--once',
            '--cursor',
            initial,
            '--cursor-file',
            str(tmp_path / 'once.cursor.json'),
            '--event',
            'file.create',
        ])
        assert await run_remote(owner, args) == 0
        output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert [event['id'] for event in output] == ['listener-visible-0']
        original, changes = owner.transport.call, []

        async def counted(packet):
            if packet.operation == 'communication.changes':
                changes.append(packet)
            return await original(packet)

        owner.transport.call = counted
        args = parser().parse_args([
            'listen',
            '--once',
            '--from-now',
            '--cursor-file',
            str(tmp_path / 'now.cursor.json'),
        ])
        assert await run_remote(owner, args) == 0
        assert capsys.readouterr().out == '' and len(changes) == 2
    finally:
        await http.aclose()
        await other_http.aclose()


@pytest.mark.asyncio
async def test_changes_highwater_defers_rows_committed_between_queries(
    installed, tmp_path, monkeypatch
):
    app, _ = installed
    owner, http = await client_for(app, tmp_path / 'owner', 'fair-highwater')
    try:
        own_file = await make_file(owner, 'fair-highwater', 'visible.txt')
        baseline = owner.checked(await owner.call('communication.changes', {})).data
        initial = baseline['tail_cursor']
        await append_history(app, owner.state.subject, own_file, 1, 'before-max-')
        original_rows = PostgresSession.rows
        inserted = False

        def insert_after_max(tx, sql, parameters=()):
            nonlocal inserted
            if not inserted and sql.startswith('SELECT seq,body FROM events WHERE seq>'):
                inserted = True
                event = Event(
                    id='after-max',
                    type='file.create',
                    time=NOW,
                    request_id='after-max',
                    actor=owner.state.subject,
                    subject=owner.state.subject,
                    resources=(ResourceRef(id=own_file),),
                    data={'operation': 'file.create'},
                )
                # A second PostgreSQL connection commits in the READ COMMITTED
                # gap between MAX(seq) and the scan query; ACLs remain real.
                with app.metadata._connect() as connection:
                    connection.execute(
                        'INSERT INTO events(id,body) VALUES(%s,%s)',
                        (event.id, canonical(event).decode()),
                    )
            return original_rows(tx, sql, parameters)

        monkeypatch.setattr(PostgresSession, 'rows', insert_after_max)
        first = owner.checked(
            await owner.call('communication.changes', {'cursor': initial, 'limit': 200})
        ).data
        assert inserted and [event['id'] for event in first['items']] == ['before-max-0']
        assert not first['has_more']
        assert (
            app.cursors.decode(first['sync_cursor'], 'sync', owner.state.subject)['seq']
            == (app.cursors.decode(first['tail_cursor'], 'sync', owner.state.subject)['seq'])
        )
        second = owner.checked(
            await owner.call('communication.changes', {'cursor': first['sync_cursor']})
        ).data
        assert [event['id'] for event in second['items']] == ['after-max']
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_multi_ref_relevance_pass_also_yields(installed, tmp_path, monkeypatch):
    app, _ = installed
    owner, http = await client_for(app, tmp_path / 'owner', 'fair-relevance')
    stranger, other_http = await client_for(app, tmp_path / 'other', 'fair-relevance-other')
    try:
        own_file = await make_file(owner, 'fair-relevance', 'visible.txt')
        baseline = owner.checked(await owner.call('communication.changes', {})).data
        cursor = baseline['tail_cursor']
        async with app.metadata.transaction(write=True) as tx:
            await tx.append_event(
                Event(
                    id='irrelevant-multi-ref',
                    type='file.create',
                    time=NOW,
                    request_id='irrelevant-multi-ref',
                    actor=stranger.state.subject,
                    subject=stranger.state.subject,
                    resources=(ResourceRef(id=own_file),) * 32,
                    data={'operation': 'file.create'},
                )
            )
        inside, seen = asyncio.Event(), 0
        original_direct = communication.direct_ancestor

        async def observe(*args):
            nonlocal seen
            seen += 1
            if seen == 2:
                inside.set()
            return await original_direct(*args)

        monkeypatch.setattr(communication, 'direct_ancestor', observe)
        pending = asyncio.create_task(owner.call('communication.changes', {'cursor': cursor}))
        await wait_for_scan_phase(inside, pending)
        assert not pending.done()
        assert (await http.get('/healthz')).status_code == 200
        assert not pending.done()
        refs_at_health = seen
        assert 2 <= refs_at_health < 32
        start = perf_counter()
        owner.checked(await owner.call('discovery.get', {'id': own_file, 'fields': ['id']}))
        read_seconds = perf_counter() - start
        refs_at_read = seen
        assert not pending.done() and refs_at_health <= refs_at_read < 32
        result = owner.checked(await pending).data
        assert seen == 32 and not result['items'] and not result['has_more']
        assert result['sync_cursor'] != cursor
        print({
            'relevance_refs': seen,
            'relevance_refs_at_health': refs_at_health,
            'relevance_refs_at_signed_read': refs_at_read,
            'parallel_signed_read_seconds': round(read_seconds, 3),
        })
    finally:
        await http.aclose()
        await other_http.aclose()


@pytest.mark.asyncio
async def test_multi_ref_event_yields_and_cancellation_deadline_leave_http_usable(
    installed,
    tmp_path,
    monkeypatch,
):
    app, _ = installed
    owner, http = await client_for(app, tmp_path / 'owner', 'fair-multi')
    # Scheduling, cancellation and the deadline are separate properties. Enough
    # real ACL checks must remain during parallel reads/writes; a successful
    # 128-ref scan cannot be assumed to fit every runner's 30-second budget.
    reference_count = 32
    try:
        own_file = await make_file(owner, 'fair-multi', 'visible.txt')
        baseline = owner.checked(await owner.call('communication.changes', {})).data
        cursor = baseline['tail_cursor']
        async with app.metadata.transaction(write=True) as tx:
            await tx.append_event(
                Event(
                    id='big-event',
                    type='file.create',
                    time=NOW,
                    request_id='big-event',
                    actor=owner.state.subject,
                    subject=owner.state.subject,
                    resources=(ResourceRef(id=own_file),) * reference_count,
                    data={'operation': 'file.create'},
                )
            )
        inside = asyncio.Event()
        seen = 0
        original_visible = communication.visible

        async def observe(*args):
            nonlocal seen
            seen += 1
            if seen == 2:
                inside.set()
            return await original_visible(*args)

        monkeypatch.setattr(communication, 'visible', observe)
        pending = asyncio.create_task(owner.call('communication.changes', {'cursor': cursor}))
        await asyncio.wait_for(inside.wait(), 5)
        assert not pending.done()
        start = perf_counter()
        assert (await http.get('/healthz')).status_code == 200
        owner.checked(
            await owner.call(
                'discovery.get',
                {
                    'id': owner.state.subject,
                    'fields': ['id', 'name'],
                },
            )
        )
        parallel_seconds = perf_counter() - start
        refs_at_read = seen
        # Completion before all ACL checks proves actual scheduling progress;
        # an absolute latency threshold measures the CI runner's speed instead.
        assert not pending.done() and 2 <= refs_at_read < reference_count
        write_start = perf_counter()
        await make_file(owner, 'fair-multi', 'created-during-scan.txt')
        write_seconds = perf_counter() - write_start
        refs_at_write = seen
        assert not pending.done() and refs_at_read <= refs_at_write < reference_count
        page = owner.checked(await pending).data
        assert len(page['items']) == 1
        assert len(page['items'][0]['resources']) == reference_count
        assert page['items'][0]['id'] == 'big-event'
        assert seen == reference_count

        seen = 0
        inside.clear()
        pending = asyncio.create_task(owner.call('communication.changes', {'cursor': cursor}))
        await asyncio.wait_for(inside.wait(), 5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert (await http.get('/healthz')).status_code == 200
        owner.checked(
            await owner.call(
                'discovery.get',
                {
                    'id': owner.state.subject,
                    'fields': ['id'],
                },
            )
        )
        tail = owner.checked(
            await owner.call('communication.changes', {'cursor': page['sync_cursor']})
        ).data['tail_cursor']
        async with app.metadata.transaction(write=True) as tx:
            await tx.append_event(
                Event(
                    id='deadline-event',
                    type='file.create',
                    time=NOW,
                    request_id='deadline-event',
                    actor=owner.state.subject,
                    subject=owner.state.subject,
                    resources=(ResourceRef(id=own_file),) * 128,
                    data={'operation': 'file.create'},
                )
            )
        spec = app.registry.operation('communication.changes')

        async def expired(ctx, request, tx):
            return await spec.handler(
                replace(ctx, deadline_monotonic=perf_counter() - 1), request, tx
            )

        app.registry._operations[(spec.name, spec.version)] = replace(spec, handler=expired)
        failure = await owner.call('communication.changes', {'cursor': cursor})
        assert failure.error.code == 'query_cost_exceeded'
        assert (await http.get('/healthz')).status_code == 200

        # Keep the large event and real ACL checks, but expire the clock after
        # two checks so deadline enforcement does not depend on runner speed.
        scan_clock = SimpleNamespace(value=90.0)
        monkeypatch.setattr(
            communication, 'time', SimpleNamespace(monotonic=lambda: scan_clock.value)
        )
        checked_refs = 0

        async def expire_during_scan(*args):
            nonlocal checked_refs
            result = await original_visible(*args)
            checked_refs += 1
            if checked_refs == 2:
                scan_clock.value = 101.0
            return result

        async def finite_budget(ctx, request, tx):
            return await spec.handler(replace(ctx, deadline_monotonic=100.0), request, tx)

        monkeypatch.setattr(communication, 'visible', expire_during_scan)
        app.registry._operations[(spec.name, spec.version)] = replace(spec, handler=finite_budget)
        interrupted = await owner.call('communication.changes', {'cursor': tail})
        assert interrupted.error.code == 'query_cost_exceeded'
        assert interrupted.data is None and checked_refs == 2
        assert (await http.get('/healthz')).status_code == 200
        owner.checked(await owner.call('discovery.get', {'id': own_file, 'fields': ['id']}))
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one("SELECT COUNT(*) FROM events WHERE id='big-event'")[0] == 1
            assert tx.one("SELECT COUNT(*) FROM events WHERE id='deadline-event'")[0] == 1
        print({
            'multi_refs': reference_count,
            'refs_at_signed_read': refs_at_read,
            'refs_at_signed_write': refs_at_write,
            'parallel_health_and_read_seconds': round(parallel_seconds, 3),
            'parallel_signed_write_seconds': round(write_seconds, 3),
        })
    finally:
        await http.aclose()
