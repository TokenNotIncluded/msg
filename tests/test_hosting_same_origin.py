"""Hosted executable bytes stay sandboxed on the service origin."""

from datetime import timedelta

import httpx
import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, loads, wire
from msg.core.models import ResourceRef
from msg.core.requests import request_for
from msg.transports.http import create_app


def isolated(response):
    csp = response.headers['content-security-policy'].lower()
    assert 'sandbox' in csp
    assert 'allow-same-origin' not in csp
    assert 'report-only' not in response.headers
    assert 'set-cookie' not in response.headers
    assert response.headers['x-content-type-options'] == 'nosniff'


@pytest.mark.asyncio
async def test_same_domain_hosted_html_head_304_range_raw_and_no_write_route(installed):
    app, _ = installed
    key, user, _ = await register(app, 'web-owner')
    site = await call(
        app, 'hosting.create', {'parent': '/@web-owner', 'name': 'web'}, key=key, subject=user
    )
    assert site.status == 'ok', wire(site)
    assert site.data['url'] == 'http://testserver/@web-owner/web/'
    html = b'<!doctype html><script>fetch("/_read/private/json")</script><p>hosted</p>'
    source = await call(
        app,
        'content.file_put',
        {
            'parent': '/@web-owner/files',
            'name': 'page.html',
            'data': b64(html),
            'media_type': 'text/html',
        },
        key=key,
        subject=user,
    )
    svg = await call(
        app,
        'content.file_put',
        {
            'parent': '/@web-owner/files',
            'name': 'drawing.svg',
            'data': b64(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
            'media_type': 'image/svg+xml',
        },
        key=key,
        subject=user,
    )
    deploy = await call(
        app,
        'hosting.deploy',
        {
            'id': site.resources[0].id,
            'entries': [
                {'path': 'index.html', 'source': wire(source.resources[0])},
                {'path': 'drawing.svg', 'source': wire(svg.resources[0])},
            ],
        },
        key=key,
        subject=user,
        expected=((site.resources[0].id, site.data['generation']),),
    )
    assert deploy.status == 'ok', wire(deploy)
    async with app.metadata.transaction(write=False) as tx:
        revision = await tx.revision(ResourceRef(id=site.resources[0].id))
        manifest = loads(await app.contents.read_bytes(revision.content))
        published = manifest['entries']['index.html']['id']
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        page = await http.get('/@web-owner/web/index.html')
        assert page.status_code == 200 and page.content == html
        isolated(page)
        head = await http.head('/@web-owner/web/index.html')
        assert head.status_code == 200 and head.content == b''
        isolated(head)
        cached = await http.get(
            '/@web-owner/web/index.html', headers={'If-None-Match': page.headers['etag']}
        )
        assert cached.status_code == 304
        isolated(cached)
        partial = await http.get('/@web-owner/web/index.html', headers={'Range': 'bytes=0-14'})
        assert partial.status_code == 206 and partial.content == html[:15]
        isolated(partial)
        raw = await http.get(f'/_read/{published}/raw')
        assert raw.status_code == 200
        assert 'attachment' in raw.headers['content-disposition']
        isolated(raw)
        vector = await http.get('/@web-owner/web/drawing.svg')
        assert vector.status_code == 200
        assert 'attachment' in vector.headers['content-disposition']
        isolated(vector)
        missing = await http.get('/@web-owner/web/missing.html')
        assert missing.status_code == 404
        isolated(missing)
        for encoded in ('%69ndex.html', '%2e%2e/index.html', '%2Findex.html'):
            ambiguous = await http.get('/@web-owner/web/' + encoded)
            assert ambiguous.status_code == 404
            isolated(ambiguous)
        platform = await http.get(
            '/_read/t_private/json',
            headers={
                'Origin': 'null',
                'Cookie': 'session=should-not-appear',
                'Authorization': 'Bearer should-not-appear',
            },
        )
        assert platform.status_code == 403
        assert platform.json()['error']['code'] == 'forbidden_origin'
        forbidden = await http.post('/@web-owner/web/index.html', content=b'overwrite')
        assert forbidden.status_code == 405


@pytest.mark.asyncio
async def test_deploy_entry_bound_matches_preview_before_any_materialization(
    installed, monkeypatch
):
    app, _ = installed
    key, user, _ = await register(app, 'web-bounded')
    site = await call(
        app, 'hosting.create', {'parent': '/@web-bounded', 'name': 'web'}, key=key, subject=user
    )
    source = await call(
        app,
        'content.file_put',
        {
            'parent': '/@web-bounded/files',
            'name': 'page.html',
            'data': b64(b'<p>bounded</p>'),
            'media_type': 'text/html',
        },
        key=key,
        subject=user,
    )
    assert site.status == source.status == 'ok'
    site_id = site.resources[0].id
    arguments = {
        'id': site_id,
        'entries': [{'path': f'{i}.html', 'source': wire(source.resources[0])} for i in range(129)],
    }
    spec = app.registry.operation('hosting.deploy', 1)
    assert 'maxItems' not in app.registry.schema(spec.input_schema)['properties']['entries']
    app.registry.validate(spec.input_schema, arguments)
    async with app.metadata.transaction(write=False) as tx:
        before = tuple(
            tx.one(f'SELECT COUNT(*) FROM {table}')[0]
            for table in ('resources', 'revisions', 'settings')
        )

    async def no_materialization(*args, **kwargs):
        pytest.fail('oversized deploy touched the content store')

    with monkeypatch.context() as patch:
        patch.setattr(app.contents, 'read_bytes', no_materialization)
        patch.setattr(app.contents, 'put_bytes', no_materialization)
        denied = await call(
            app,
            'hosting.deploy',
            arguments,
            key=key,
            subject=user,
            expected=((site_id, site.data['generation']),),
        )
    assert denied.status == 'error' and denied.error.code == 'too_many_hosting_entries', wire(
        denied
    )
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tuple(
                tx.one(f'SELECT COUNT(*) FROM {table}')[0]
                for table in ('resources', 'revisions', 'settings')
            )
            == before
        )
        assert (await tx.resource(site_id)).revision is None
    allowed = await call(
        app,
        'hosting.deploy',
        {**arguments, 'entries': arguments['entries'][:128]},
        key=key,
        subject=user,
        expected=((site_id, site.data['generation']),),
    )
    assert allowed.status == 'ok' and allowed.data['files'] == 128, wire(allowed)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        last = await http.get('/@web-bounded/web/127.html')
        assert last.status_code == 200 and last.content == b'<p>bounded</p>'
        isolated(last)
        assert (await http.get('/@web-bounded/web/128.html')).status_code == 404


@pytest.mark.asyncio
async def test_preview_history_and_atomic_activation_share_same_csp(installed):
    app, _ = installed
    key, user, _ = await register(app, 'web-versions')
    site = await call(
        app, 'hosting.create', {'parent': '/@web-versions', 'name': 'web'}, key=key, subject=user
    )
    first = await call(
        app,
        'content.file_put',
        {
            'parent': '/@web-versions/files',
            'name': 'v1.html',
            'data': b64(b'<h1>one</h1>'),
            'media_type': 'text/html',
        },
        key=key,
        subject=user,
    )
    deployed1 = await call(
        app,
        'hosting.deploy',
        {
            'id': site.resources[0].id,
            'entries': [{'path': 'index.html', 'source': wire(first.resources[0])}],
        },
        key=key,
        subject=user,
        expected=((site.resources[0].id, site.data['generation']),),
    )
    assert deployed1.status == 'ok', wire(deployed1)
    old_revision = deployed1.resources[0].revision
    second = await call(
        app,
        'content.file_put',
        {
            'parent': '/@web-versions/files',
            'name': 'v2.html',
            'data': b64(b'<h1>two</h1>'),
            'media_type': 'text/html',
        },
        key=key,
        subject=user,
    )
    deployed2 = await call(
        app,
        'hosting.deploy',
        {
            'id': site.resources[0].id,
            'entries': [{'path': 'index.html', 'source': wire(second.resources[0])}],
        },
        key=key,
        subject=user,
        expected=((site.resources[0].id, deployed1.data['generation']),),
    )
    assert deployed2.status == 'ok', wire(deployed2)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        current = await http.get('/@web-versions/web/index.html')
        historical = await http.get(f'/@web-versions/web/_rev/{old_revision}/index.html')
        assert current.content == b'<h1>two</h1>' and historical.content == b'<h1>one</h1>'
        isolated(current)
        isolated(historical)
    rollback = await call(
        app,
        'hosting.activate',
        {'id': site.resources[0].id, 'revision': old_revision},
        key=key,
        subject=user,
        expected=((site.resources[0].id, deployed2.data['generation']),),
    )
    assert rollback.status == 'ok', wire(rollback)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        rolled_back = await http.get('/@web-versions/web/index.html')
        assert rolled_back.content == b'<h1>one</h1>'
        isolated(rolled_back)


@pytest.mark.asyncio
async def test_root_sample_is_public_sandboxed_and_never_uses_root_credentials(installed):
    app, _ = installed
    async with app.metadata.transaction(write=False) as tx:
        website = await tx.resource(await tx.resolve('/@root/web'))
        assert website.type == 'website' and website.revision is not None
        revision = await tx.revision(ResourceRef(id=website.id))
        manifest = loads(await app.contents.read_bytes(revision.content))
        assert (await tx.resource(manifest['deployment'])).parent == website.id
        sample = await tx.resource(manifest['entries']['index.html']['id'])
        assert (
            sample.type == 'file'
            and sample.revision == manifest['entries']['index.html']['revision']
        )
        generation = website.generation
    from msg.bootstrap import bootstrap

    await bootstrap(app.metadata, app.contents, app.registry, app.clock())
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource(website.id)).generation == generation
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        sample = await http.get(
            '/@root/web/index.html',
            headers={
                'Origin': 'null',
                'Authorization': 'Bearer should-not-appear',
                'Cookie': 'session=should-not-appear',
            },
        )
        assert sample.status_code == 200
        isolated(sample)
        assert b'should-not-appear' not in sample.content


@pytest.mark.asyncio
async def test_private_preview_readback_then_deploy_and_rollback(installed):
    app, _ = installed
    key, user, _ = await register(app, 'web-preview')
    site = await call(
        app, 'hosting.create', {'parent': '/@web-preview', 'name': 'web'}, key=key, subject=user
    )
    old = await call(
        app,
        'content.file_put',
        {
            'parent': '/@web-preview/files',
            'name': 'old.html',
            'data': b64(b'<h1>old</h1>'),
            'media_type': 'text/html',
        },
        key=key,
        subject=user,
    )
    first = await call(
        app,
        'hosting.deploy',
        {
            'id': site.resources[0].id,
            'entries': [{'path': 'index.html', 'source': wire(old.resources[0])}],
        },
        key=key,
        subject=user,
        expected=((site.resources[0].id, site.data['generation']),),
    )
    assert first.status == 'ok', wire(first)
    changed = await call(
        app,
        'content.file_put',
        {
            'parent': '/@web-preview/files',
            'name': 'changed.html',
            'data': b64(b'<h1>candidate</h1>'),
            'media_type': 'text/html',
        },
        key=key,
        subject=user,
    )
    candidate = await call(
        app,
        'hosting.preview',
        {
            'id': site.resources[0].id,
            'entries': [{'path': 'index.html', 'source': wire(changed.resources[0])}],
        },
        key=key,
        subject=user,
        expected=((site.resources[0].id, first.data['generation']),),
    )
    assert candidate.status == 'ok', wire(candidate)
    candidate_id = candidate.resources[0].id
    packet = request_for(
        'discovery.raw',
        {'id': candidate_id},
        app.settings.service_url,
        signer=key,
        subject=user,
        expires_at=NOW + timedelta(seconds=120),
    )
    header = b64(canonical(wire(packet)))
    path = f'/@web-preview/web/_preview/{candidate_id}/index.html'
    async with app.metadata.transaction(write=False) as tx:
        before = (
            tx.one('SELECT COUNT(*) FROM events')[0],
            tx.one('SELECT COUNT(*) FROM revisions')[0],
            tx.one('SELECT COUNT(*) FROM resources')[0],
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        live = await http.get('/@web-preview/web/')
        assert live.content == b'<h1>old</h1>'
        denied = await http.get(path)
        assert denied.status_code == 403
        preview = await http.get(path, headers={'X-Msg-Request': header})
        assert preview.status_code == 200 and preview.content == b'<h1>candidate</h1>', preview.text
        isolated(preview)
        head = await http.head(path, headers={'X-Msg-Request': header})
        assert head.status_code == 200 and not head.content
        cached = await http.get(
            path, headers={'X-Msg-Request': header, 'If-None-Match': preview.headers['etag']}
        )
        assert cached.status_code == 304
        partial = await http.get(path, headers={'X-Msg-Request': header, 'Range': 'bytes=0-5'})
        assert partial.status_code == 206 and partial.content == b'<h1>ca'
        disallowed = await http.post(path, content=b'overwrite', headers={'X-Msg-Request': header})
        assert disallowed.status_code == 405
    async with app.metadata.transaction(write=False) as tx:
        assert before == (
            tx.one('SELECT COUNT(*) FROM events')[0],
            tx.one('SELECT COUNT(*) FROM revisions')[0],
            tx.one('SELECT COUNT(*) FROM resources')[0],
        )
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource(site.resources[0].id)).revision == first.resources[0].revision
        manifest = loads(
            await app.contents.read_bytes((await tx.revision(ResourceRef(id=candidate_id))).content)
        )
        preview_file = manifest['entries']['index.html']
    other_key, other_user, _ = await register(app, 'preview-stranger')
    other_packet = request_for(
        'discovery.raw',
        {'id': candidate_id},
        app.settings.service_url,
        signer=other_key,
        subject=other_user,
        expires_at=NOW + timedelta(seconds=120),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        outsider = await http.get(
            path, headers={'X-Msg-Request': b64(canonical(wire(other_packet)))}
        )
        assert outsider.status_code == 403
    publish = await call(
        app,
        'hosting.deploy',
        {'id': site.resources[0].id, 'entries': [{'path': 'index.html', 'source': preview_file}]},
        key=key,
        subject=user,
        expected=((site.resources[0].id, first.data['generation']),),
    )
    assert publish.status == 'ok', wire(publish)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        assert (await http.get('/@web-preview/web/')).content == b'<h1>candidate</h1>'
    rollback = await call(
        app,
        'hosting.activate',
        {'id': site.resources[0].id, 'revision': first.resources[0].revision},
        key=key,
        subject=user,
        expected=((site.resources[0].id, publish.data['generation']),),
    )
    assert rollback.status == 'ok', wire(rollback)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        assert (await http.get('/@web-preview/web/')).content == b'<h1>old</h1>'
    opened_topic = await call(
        app,
        'content.chmod',
        {'id': candidate_id, 'mode': '0755'},
        key=key,
        subject=user,
        expected=((candidate_id, candidate.data['generation']),),
    )
    assert opened_topic.status == 'ok', wire(opened_topic)
    opened_file = await call(
        app,
        'content.chmod',
        {'id': preview_file['id'], 'mode': '0644'},
        key=key,
        subject=user,
        expected=((preview_file['id'], 1),),
    )
    assert opened_file.status == 'ok', wire(opened_file)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        anonymous = await http.get('/_read/' + preview_file['id'] + '/raw')
        assert anonymous.status_code == 403, anonymous.text
    revoked = await call(
        app,
        'content.chmod',
        {'id': candidate_id, 'mode': '0000'},
        key=key,
        subject=user,
        expected=((candidate_id, opened_topic.data['generation']),),
    )
    assert revoked.status == 'ok', wire(revoked)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        inaccessible = await http.get(path, headers={'X-Msg-Request': header})
        assert inaccessible.status_code == 403
