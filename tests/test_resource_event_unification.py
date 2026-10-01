"""Stable, server-qualified addressing and committed event delivery boundaries."""

import httpx
import pytest
from test_service import NOW, call, register

from msg.core.addressing import parse_address, resource_address, service_origin
from msg.core.codec import wire
from msg.core.errors import Failure
from msg.core.models import Event, ResourceRef
from msg.transports.http import create_app


@pytest.mark.parametrize(
    'service,expected',
    [
        ('https://EXAMPLE.org:443/', 'https://example.org'),
        ('http://localhost:8080', 'http://localhost:8080'),
        ('https://[::1]:444', 'https://[::1]:444'),
    ],
)
def test_origin_normalization(service, expected):
    assert service_origin(service) == expected


@pytest.mark.parametrize(
    'address',
    [
        'https://example.org/_r/r_x/json?token=secret',
        'https://example.org/_r/r_x/json#secret',
        'https://user:secret@example.org/_r/r_x/json',
        'https://example.org/_r/%72_x/json',
        'https://example.org/_r/r_x/rev/',
        'https://example.org/@alice',
        'https://example.org\\evil/_r/r_x/json',
    ],
)
def test_address_rejects_credentials_and_alternate_spelling(address):
    with pytest.raises(Failure):
        parse_address(address, 'https://example.org')


def test_address_remote_never_becomes_local():
    with pytest.raises(Failure, match='remote_resource_address'):
        parse_address('https://other.example/_r/r_x/json', 'https://example.org')
    ref = ResourceRef(id='r_x', revision='v_old')
    address = resource_address('https://example.org', ref)
    assert parse_address(address['url'], 'https://example.org') == ('r_x', 'v_old')


async def new_post(app, key, subject, cert, *, body='version one', request_id=None):
    result = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': body},
        key=key,
        subject=subject,
        certs=(cert,),
        rid=request_id,
    )
    assert result.status == 'ok', wire(result)
    return result


async def test_address_round_trip_move_revision_http_and_read_purity(installed):
    app, _ = installed
    key, subject, cert = await register(app, 'address-owner')
    post = await new_post(app, key, subject, cert)
    ref = post.resources[0]
    async with app.metadata.transaction(write=False) as tx:
        path = await tx.path(ref.id)
    pinned = resource_address(app.settings.service_url, ref)
    for value in (path, ref.id, pinned['url'], '/*' + ref.id):
        resolved = await call(app, 'discovery.resolve', {'address': value})
        assert resolved.status == 'ok', wire(resolved)
        assert resolved.data['ref']['id'] == ref.id
        assert resolved.data['current']['ref']['revision'] == ref.revision
    moved = await call(
        app,
        'content.move',
        {'id': ref.id, 'parent': '/intro', 'name': 'address-renamed.md'},
        key=key,
        subject=subject,
        certs=(cert,),
        expected=((ref.id, post.data['generation']),),
    )
    assert moved.status == 'ok', wire(moved)
    async with app.metadata.transaction(write=False) as tx:
        count = tx.one('SELECT COUNT(*) FROM events')[0]
    resolved = await call(app, 'discovery.resolve', {'address': pinned['url']})
    assert resolved.status == 'ok', wire(resolved)
    assert resolved.data['url'] == pinned['url']
    assert resolved.data['path'] == '/intro/address-renamed.md'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url='http://testserver',
    ) as http:
        response = await http.get(resolved.data['url'])
        assert response.status_code == 200
        assert 'version one' in response.text
    mismatch = await call(
        app, 'discovery.resolve', {'address': pinned['url'], 'revision': 'v_other'}
    )
    assert mismatch.error.code == 'revision_mismatch'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM events')[0] == count


async def test_address_current_acl_and_foreign_revision(installed):
    app, _ = installed
    key, subject, cert = await register(app, 'address-private')
    post = await new_post(app, key, subject, cert)
    other = await new_post(app, key, subject, cert, body='other')
    bad = await call(
        app,
        'discovery.resolve',
        {
            'address': post.resources[0].id,
            'revision': other.resources[0].revision,
        },
    )
    assert bad.error.code == 'revision_not_found'
    changed = await call(
        app,
        'content.chmod',
        {'id': post.resources[0].id, 'mode': '0600'},
        key=key,
        subject=subject,
        certs=(cert,),
        expected=((post.resources[0].id, post.data['generation']),),
    )
    assert changed.status == 'ok', wire(changed)
    denied = await call(
        app,
        'discovery.resolve',
        {
            'address': resource_address(app.settings.service_url, post.resources[0])['url'],
        },
    )
    assert denied.status == 'error'
    assert denied.error.code == 'permission_denied'


async def test_events_resume_replay_scope_and_safe_projection(installed):
    app, _ = installed
    key, subject, cert = await register(app, 'event-owner')
    args = {'resource': '/main', 'limit': 1}
    initial = await call(app, 'communication.events', args, key=key, subject=subject, certs=(cert,))
    assert initial.status == 'ok', wire(initial)
    first = await new_post(app, key, subject, cert, request_id='unified-event-once')
    replay = await new_post(app, key, subject, cert, request_id='unified-event-once')
    assert replay.replayed
    second = await new_post(app, key, subject, cert, body='version two')
    cursor = initial.data['cursor']
    found = []
    for _ in range(10):
        page = await call(
            app,
            'communication.events',
            {**args, 'cursor': cursor},
            key=key,
            subject=subject,
            certs=(cert,),
        )
        assert page.status == 'ok', wire(page)
        found.extend(page.data['items'])
        cursor = page.data['cursor']
        if not page.data['items']:
            break
    created = [event for event in found if event['operation'] == 'content.post_create']
    assert len(created) == 2
    assert len({event['id'] for event in created}) == 2
    assert [e['resources'][0]['ref']['id'] for e in created] == [
        first.resources[0].id,
        second.resources[0].id,
    ]
    for event in created:
        assert event['version'] == 1 and event['type'] == 'resource.created'
        assert 'data' not in event
        assert event['resources'][0]['url'].startswith('http://testserver/_r/')
    wrong_scope = await call(
        app,
        'communication.events',
        {'resource': '/intro', 'cursor': cursor},
        key=key,
        subject=subject,
        certs=(cert,),
    )
    assert wrong_scope.error.code == 'cursor_query_mismatch'
    stranger_key, stranger, stranger_cert = await register(app, 'event-stranger')
    wrong_user = await call(
        app,
        'communication.events',
        {**args, 'cursor': cursor},
        key=stranger_key,
        subject=stranger,
        certs=(stranger_cert,),
    )
    assert wrong_user.error.code == 'cursor_query_mismatch'
    anon = await call(app, 'communication.events', args)
    assert anon.error.code == 'authentication_required'


async def test_events_current_permissions_no_audit_data_and_resync(installed):
    app, _ = installed
    key, subject, cert = await register(app, 'event-private')
    other_key, other, other_cert = await register(app, 'event-reader')
    post = await new_post(app, key, subject, cert)
    initial = await call(
        app,
        'communication.events',
        {'resource': '/main'},
        key=other_key,
        subject=other,
        certs=(other_cert,),
    )
    assert initial.status == 'ok', wire(initial)
    changed = await call(
        app,
        'content.chmod',
        {'id': post.resources[0].id, 'mode': '0600'},
        key=key,
        subject=subject,
        certs=(cert,),
        expected=((post.resources[0].id, post.data['generation']),),
    )
    assert changed.status == 'ok', wire(changed)
    stale = await call(
        app,
        'communication.events',
        {
            'resource': '/main',
            'cursor': initial.data['cursor'],
        },
        key=other_key,
        subject=other,
        certs=(other_cert,),
    )
    assert stale.error.code == 'resync_required'
    current = await call(
        app,
        'communication.events',
        {'resource': '/main'},
        key=other_key,
        subject=other,
        certs=(other_cert,),
    )
    assert current.status == 'ok', wire(current)
    assert all(
        ref['ref']['id'] != post.resources[0].id
        for event in current.data['items']
        for ref in event['resources']
    )
    # Anonymous committed facts and arbitrary audit fields are safe to scan.
    async with app.metadata.transaction(write=True) as tx:
        await tx.append_event(
            Event(
                id='e_anonymous',
                type='internet.receive',
                time=NOW,
                request_id='anonymous',
                actor=None,
                subject=None,
                resources=(),
                data={'secret': 'audit-only'},
            )
        )
        own = Event(
            id='e_private_audit',
            type='topic.policy.change',
            time=NOW,
            request_id='audit',
            actor=subject,
            subject=subject,
            resources=(post.resources[0],),
            data={'reason': 'audit-only', 'target_subject': other},
        )
        await tx.append_event(own)
    owner_events = await call(
        app, 'communication.events', {}, key=key, subject=subject, certs=(cert,)
    )
    assert owner_events.status == 'ok', wire(owner_events)
    assert 'audit-only' not in str(owner_events.data)
    assert any(e['id'] == 'e_private_audit' for e in owner_events.data['items'])
    saved = app.cursors.inspect(owner_events.data['cursor'])
    saved['position']['expires_at'] = wire(NOW)
    expired_cursor = app.cursors.encode(saved['kind'], saved['query'], saved['position'])
    expired = await call(
        app,
        'communication.events',
        {
            'cursor': expired_cursor,
        },
        key=key,
        subject=subject,
        certs=(cert,),
    )
    assert expired.error.code == 'cursor_expired'


async def test_cli_resolve_read_pinned_url_and_events(installed, tmp_path, monkeypatch, capsys):
    from msg import cli
    from msg.client import ClientState, MsgClient
    from msg.core.codec import loads
    from msg.transports.client import HTTPTransport

    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url=app.settings.service_url,
    ) as http:
        directory = tmp_path / 'unified-client'
        client = MsgClient(
            ClientState(directory, server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await client.register('cli-unified')).status == 'ok'
        post = await client.call('content.post_create', {'parent': '/main', 'body': 'pinned CLI'})
        assert post.status == 'ok', wire(post)
        monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: HTTPTransport(server, http=http))
        monkeypatch.setattr(
            cli,
            'MsgClient',
            lambda state, transport: MsgClient(
                state,
                transport,
                clock=lambda: NOW,
            ),
        )

        async def invoke(*command):
            args = cli.parser().parse_args([
                '--config-dir',
                str(directory),
                '--server',
                app.settings.service_url,
                '--format',
                'json',
                *command,
            ])
            status = await cli.run(args)
            return status, loads(capsys.readouterr().out.encode())

        status, resolved = await invoke('resolve', post.resources[0].id)
        assert status == 0 and resolved['status'] == 'ok'
        url = resolved['data']['current']['url']
        status, reading = await invoke('read', url)
        assert status == 0 and reading['data']['content'] == 'pinned CLI'
        assert reading['data']['revision'] == post.resources[0].revision
        status, events = await invoke('events', '--resource', resolved['data']['url'])
        assert status == 0 and events['status'] == 'ok'
        assert any(event['operation'] == 'content.post_create' for event in events['data']['items'])
        status, resumed = await invoke(
            'events', '--resource', resolved['data']['url'], '--cursor', events['data']['cursor']
        )
        assert status == 0 and resumed['data']['items'] == []


async def test_events_empty_page_advances_past_bounded_hidden_history(installed):
    app, _ = installed
    key, subject, cert = await register(app, 'event-bounded')
    post = await new_post(app, key, subject, cert)
    initial = await call(
        app,
        'communication.events',
        {'resource': post.resources[0].id},
        key=key,
        subject=subject,
        certs=(cert,),
    )
    assert initial.status == 'ok', wire(initial)
    async with app.metadata.transaction(write=True) as tx:
        for index in range(501):
            await tx.append_event(
                Event(
                    id=f'e_hidden_{index}',
                    type='internet.receive',
                    time=NOW,
                    request_id=f'hidden_{index}',
                    actor=None,
                    subject=None,
                    resources=(),
                    data={},
                )
            )
        await tx.append_event(
            Event(
                id='e_after_hidden',
                type='content.post_edit',
                time=NOW,
                request_id='after-hidden',
                actor=subject,
                subject=subject,
                resources=post.resources,
                data={},
            )
        )
        event_count = tx.one('SELECT COUNT(*) FROM events')[0]
        job_count = tx.one('SELECT COUNT(*) FROM jobs')[0]
    empty = await call(
        app,
        'communication.events',
        {
            'resource': post.resources[0].id,
            'cursor': initial.data['cursor'],
        },
        key=key,
        subject=subject,
        certs=(cert,),
    )
    assert empty.status == 'ok' and not empty.data['items'], wire(empty)
    assert empty.data['cursor'] != initial.data['cursor']
    following = await call(
        app,
        'communication.events',
        {
            'resource': post.resources[0].id,
            'cursor': empty.data['cursor'],
        },
        key=key,
        subject=subject,
        certs=(cert,),
    )
    assert following.status == 'ok', wire(following)
    assert [event['id'] for event in following.data['items']] == ['e_after_hidden']
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM events')[0] == event_count
        assert tx.one('SELECT COUNT(*) FROM jobs')[0] == job_count


@pytest.mark.parametrize(
    'path', ['/main/space name.md', '/main/hash#name.md', '/main/percent%name.md']
)
def test_plain_paths_preserve_existing_names(path):
    assert parse_address(path, 'https://example.org') == (path, None)
