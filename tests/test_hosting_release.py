"""One reviewed hosting bundle executes without inheriting service authority."""

import asyncio
import base64
import hashlib
import os
import shutil
import socket
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
import uvicorn
from test_service import NOW, call, register

from msg.client import ClientState, MsgClient
from msg.core.codec import b64, canonical, digest, loads, wire
from msg.core.errors import Failure
from msg.core.models import ResourceRef
from msg.core.requests import request_for
from msg.extensions import hosting
from msg.transports import hosted_release
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app

APPROVED = b"""<!doctype html><meta charset="utf-8"><button id="move">Move</button>
<output id="position">0</output><script>
const output = document.querySelector('#position');
let position = 0;
document.querySelector('#move').addEventListener('click', () => output.textContent = ++position);
document.addEventListener('keydown', event => {
  if (event.key === 'ArrowRight') output.textContent = ++position;
});
document.documentElement.dataset.booted = 'yes';
</script>"""


@pytest.fixture
def approved_ascii(tmp_path, monkeypatch):
    asset = tmp_path / hosted_release.ASCII_ASSET
    asset.write_bytes(APPROVED)
    monkeypatch.setattr(hosted_release, 'ASCII_RELEASE_DIGEST', digest(APPROVED))
    monkeypatch.setattr(hosted_release, 'files', lambda package: tmp_path)
    hosted_release.ascii_release.cache_clear()
    try:
        yield asset
    finally:
        hosted_release.ascii_release.cache_clear()


def assert_inert(response):
    for name, value in hosting.HOSTED_HEADERS.items():
        assert response.headers[name] == value
    assert 'set-cookie' not in response.headers
    assert 'access-control-allow-origin' not in response.headers


def test_release_pin_is_bound_to_packaged_bytes_site_and_path(approved_ascii):
    approved = hosting.hosted_headers(hosted_release.ASCII_SITE_ID, 'index.html', digest(APPROVED))
    csp = approved['Content-Security-Policy']
    script = APPROVED.split(b'<script>')[1].split(b'</script>')[0]
    script_hash = base64.b64encode(hashlib.sha256(script).digest()).decode('ascii')
    assert csp.startswith("sandbox allow-scripts allow-pointer-lock; default-src 'none';")
    assert "script-src 'sha256-" + script_hash + "'" in csp
    assert "script-src-attr 'none'" in csp
    for directive in ('connect-src', 'frame-src', 'object-src', 'worker-src', 'form-action'):
        assert directive + " 'none'" in csp
    assert 'allow-same-origin' not in csp
    assert 'allow-popups' not in csp
    assert 'allow-top-navigation' not in csp
    assert "script-src 'unsafe-inline'" not in csp
    approved_ascii.write_bytes(APPROVED + b' ')
    # A running release caches only the known approved bytes. Replacing a
    # packaged file cannot authorize its new content after that cache warms.
    assert (
        hosting.hosted_headers(hosted_release.ASCII_SITE_ID, 'index.html', digest(APPROVED + b' '))
        == hosting.HOSTED_HEADERS
    )
    for site, path, body in (
        ('r_other_site', 'index.html', APPROVED),
        (hosted_release.ASCII_SITE_ID, 'other.html', APPROVED),
        (hosted_release.ASCII_SITE_ID, 'index.html', APPROVED + b' '),
    ):
        assert hosting.hosted_headers(site, path, digest(body)) == hosting.HOSTED_HEADERS
    for body in (APPROVED + b' ', None):
        hosted_release.ascii_release.cache_clear()
        if body is None:
            approved_ascii.unlink()
        else:
            approved_ascii.write_bytes(body)
        assert (
            hosting.hosted_headers(hosted_release.ASCII_SITE_ID, 'index.html', digest(APPROVED))
            == hosting.HOSTED_HEADERS
        )


async def publish_fixture(app, monkeypatch):
    key, user, _ = await register(app, 'ascii-release-owner')
    original_create = hosting.create_resource

    async def fixed_site(*args, **kwargs):
        if kwargs['type'] == 'website' and kwargs['name'] == 'web':
            kwargs['resource_id'] = hosted_release.ASCII_SITE_ID
        return await original_create(*args, **kwargs)

    monkeypatch.setattr(hosting, 'create_resource', fixed_site)
    site = await call(
        app,
        'hosting.create',
        {'parent': '/@ascii-release-owner', 'name': 'web'},
        key=key,
        subject=user,
    )
    other = await call(
        app,
        'hosting.create',
        {'parent': '/@ascii-release-owner', 'name': 'ordinary'},
        key=key,
        subject=user,
    )
    source = await call(
        app,
        'content.file_put',
        {
            'parent': '/@ascii-release-owner/files',
            'name': 'candidate.html',
            'data': b64(APPROVED),
            'media_type': 'text/html',
        },
        key=key,
        subject=user,
    )
    tampered = await call(
        app,
        'content.file_put',
        {
            'parent': '/@ascii-release-owner/files',
            'name': 'tampered.html',
            'data': b64(APPROVED + b' '),
            'media_type': 'text/html',
        },
        key=key,
        subject=user,
    )
    assert site.status == other.status == source.status == tampered.status == 'ok'
    entries = [
        {'path': name, 'source': wire(source.resources[0])} for name in ('index.html', 'other.html')
    ]
    entries.append({'path': 'tampered.html', 'source': wire(tampered.resources[0])})
    deployed = []
    for website in (site, other):
        result = await call(
            app,
            'hosting.deploy',
            {'id': website.resources[0].id, 'entries': entries},
            key=key,
            subject=user,
            expected=((website.resources[0].id, website.data['generation']),),
        )
        assert result.status == 'ok', wire(result)
        deployed.append(result)
    return key, user, source, deployed[0], tampered


@pytest.mark.asyncio
async def test_pg_public_bundle_preview_and_unapproved_routes(
    installed, approved_ascii, monkeypatch, tmp_path
):
    app, _ = installed
    key, user, source, deployed, tampered = await publish_fixture(app, monkeypatch)
    site = hosted_release.ASCII_SITE_ID
    candidate = await call(
        app,
        'hosting.preview',
        {'id': site, 'entries': [{'path': 'index.html', 'source': wire(source.resources[0])}]},
        key=key,
        subject=user,
        expected=((site, deployed.data['generation']),),
    )
    assert candidate.status == 'ok', wire(candidate)
    async with app.metadata.transaction(write=False) as tx:
        revision = await tx.revision(ResourceRef(id=site))
        manifest = loads(await app.contents.read_bytes(revision.content))
        published_id = manifest['entries']['index.html']['id']
    packet = request_for(
        'discovery.raw',
        {'id': candidate.resources[0].id},
        app.settings.service_url,
        signer=key,
        subject=user,
        expires_at=NOW + timedelta(seconds=120),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        path = '/@ascii-release-owner/web/'
        approved = await http.get(
            path,
            headers={
                'Origin': 'null',
                'Cookie': 'session=must-not-leak',
                'Authorization': 'Bearer must-not-leak',
            },
        )
        assert approved.status_code == 200 and approved.content == APPROVED
        assert approved.headers['content-security-policy'].startswith(
            'sandbox allow-scripts allow-pointer-lock;'
        )
        assert 'allow-same-origin' not in approved.headers['content-security-policy']
        assert 'must-not-leak' not in approved.text
        assert 'set-cookie' not in approved.headers
        assert 'access-control-allow-origin' not in approved.headers
        for method, headers in (
            ('HEAD', {}),
            ('GET', {'If-None-Match': approved.headers['etag']}),
            ('GET', {'Range': 'bytes=0-14'}),
        ):
            response = await http.request(method, path, headers=headers)
            assert (
                response.headers['content-security-policy']
                == approved.headers['content-security-policy']
            )
        historical = await http.get(path + '_rev/' + deployed.resources[0].revision + '/index.html')
        assert historical.status_code == 200 and historical.content == APPROVED
        assert (
            historical.headers['content-security-policy']
            == approved.headers['content-security-policy']
        )
        for unapproved in (
            path + 'other.html',
            path + 'tampered.html',
            '/@ascii-release-owner/ordinary/index.html',
            f'/_read/{published_id}/raw',
        ):
            response = await http.get(unapproved)
            assert response.status_code == 200, unapproved
            assert 'allow-scripts' not in response.headers['content-security-policy']
        preview = await http.get(
            path + '_preview/' + candidate.resources[0].id + '/index.html',
            headers={'X-Msg-Request': b64(canonical(wire(packet)))},
        )
        assert preview.status_code == 200 and preview.content == APPROVED
        assert_inert(preview)
        preview_path = path + '_preview/' + candidate.resources[0].id + '/index.html'
        proof = {'X-Msg-Request': b64(canonical(wire(packet)))}
        for method, extra in (
            ('HEAD', {}),
            ('GET', {'If-None-Match': preview.headers['etag']}),
            ('GET', {'Range': 'bytes=0-14'}),
        ):
            assert_inert(await http.request(method, preview_path, headers={**proof, **extra}))
        state = ClientState(tmp_path / 'preview-client', server=app.settings.service_url)
        state.signer = key
        state.data['subject_id'] = user
        client = MsgClient(state, HTTPTransport(state.server, http=http), clock=lambda: NOW)
        data, _ = await client.hosting_preview_get(
            '/@ascii-release-owner/web', candidate.resources[0].id
        )
        assert data == APPROVED
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    content=APPROVED,
                    headers=hosting.hosted_headers(site, 'index.html', digest(APPROVED)),
                )
            )
        ) as unsafe_http:
            unsafe_client = MsgClient(
                state, HTTPTransport(state.server, http=unsafe_http), clock=lambda: NOW
            )
            with pytest.raises(Failure, match='unsafe_preview_response'):
                await unsafe_client.hosting_preview_get(
                    '/@ascii-release-owner/web', candidate.resources[0].id
                )
        assert (await http.post(path, content=b'overwrite')).status_code == 405
        changed = await call(
            app,
            'hosting.deploy',
            {
                'id': site,
                'entries': [{'path': 'index.html', 'source': wire(tampered.resources[0])}],
            },
            key=key,
            subject=user,
            expected=((site, deployed.data['generation']),),
        )
        assert changed.status == 'ok', wire(changed)
        replaced = await http.get(path)
        assert replaced.status_code == 200 and replaced.content == APPROVED + b' '
        assert_inert(replaced)


@asynccontextmanager
async def browser_server(app):
    router = create_app(app)
    requests = []

    async def host_bridge(scope, receive, send):
        if scope['type'] == 'http':
            requests.append((scope['method'], scope['path']))
            scope = {
                **scope,
                'headers': [
                    (name, b'testserver' if name == b'host' else value)
                    for name, value in scope['headers']
                ],
            }
        await router(scope, receive, send)

    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.setblocking(False)
    server = uvicorn.Server(
        uvicorn.Config(host_bridge, lifespan='off', log_level='error', access_log=False, ws='none')
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield 'http://127.0.0.1:' + str(listener.getsockname()[1]), requests
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        listener.close()


@pytest.mark.asyncio
async def test_real_chromium_executes_local_interaction_with_opaque_origin(
    installed, approved_ascii, monkeypatch
):
    from playwright.async_api import async_playwright

    app, _ = installed
    await publish_fixture(app, monkeypatch)
    async with browser_server(app) as (url, requests), async_playwright() as tool:
        browser = await tool.chromium.launch(
            executable_path=os.environ.get('MSG_BROWSER_PATH') or shutil.which('chromium')
        )
        try:
            context = await browser.new_context()
            page = await context.new_page()
            response = await page.goto(url + '/@ascii-release-owner/web/index.html')
            assert response.status == 200
            await page.wait_for_function("document.documentElement.dataset.booted === 'yes'")
            await page.locator('#move').click()
            await page.keyboard.press('ArrowRight')
            assert await page.locator('#position').text_content() == '2'
            assert await page.evaluate('window.origin') == 'null'
            for storage in ('local', 'session', 'cookie'):
                assert await page.evaluate(
                    'kind => { try { if (kind === "local") localStorage.length; '
                    'else if (kind === "session") sessionStorage.length; else document.cookie; '
                    'return false; } '
                    'catch (error) { return error.name === "SecurityError"; } }',
                    storage,
                )
            before = len(requests)
            blocked = await page.evaluate("""async () => {
              window.violations = [];
              document.addEventListener('securitypolicyviolation', event =>
                window.violations.push([event.effectiveDirective, event.blockedURI]));
              const attempts = [];
              for (const url of ['/credential-probe', 'https://example.invalid/credential-probe']) {
                try { await fetch(url, {credentials: 'include'}); attempts.push(false); }
                catch (error) { attempts.push(error.name === 'TypeError'); }
              }
              return attempts;
            }""")
            assert blocked == [True, True]
            await page.wait_for_function(
                'window.violations.filter(item => item[0] === "connect-src").length === 2'
            )
            assert await page.evaluate(
                'window.violations.filter(item => item[0] === "connect-src").map(item => item[1]).sort()'
            ) == sorted([url + '/credential-probe', 'https://example.invalid/credential-probe'])
            await page.evaluate("""() => {
              const frame = document.createElement('iframe');
              frame.src = '/frame-probe'; document.body.append(frame);
              const form = document.createElement('form');
              form.action = '/form-probe'; form.method = 'post';
              document.body.append(form); form.submit();
            }""")
            await page.wait_for_function('window.violations.some(item => item[0] === "frame-src")')
            assert len(requests) == before
            assert not await page.evaluate("Boolean(window.open('/popup-probe'))")
            assert not await context.cookies()
            for path in ('/web/other.html', '/web/tampered.html', '/ordinary/index.html'):
                await page.goto(url + '/@ascii-release-owner' + path)
                assert await page.evaluate('document.documentElement.dataset.booted') is None
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_real_pointer_lock_requires_exact_pin_and_allows_relative_mouse(
    installed, approved_ascii, monkeypatch
):
    from playwright.async_api import async_playwright

    body = b"""<!doctype html><style>#world{width:320px;height:240px}</style>
    <div id="world">Click to look</div><script>
    const world = document.querySelector('#world');
    world.dataset.yaw = '0';
    world.addEventListener('click', async () => {
      try { await world.requestPointerLock(); }
      catch (error) { world.dataset.error = error.name; }
    });
    document.addEventListener('mousemove', event => {
      if (document.pointerLockElement === world)
        world.dataset.yaw = String(Number(world.dataset.yaw) + event.movementX);
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') document.exitPointerLock();
    });
    </script>"""
    monkeypatch.setitem(globals(), 'APPROVED', body)
    approved_ascii.write_bytes(body)
    monkeypatch.setattr(hosted_release, 'ASCII_RELEASE_DIGEST', digest(body))
    hosted_release.ascii_release.cache_clear()
    app, _ = installed
    await publish_fixture(app, monkeypatch)
    async with browser_server(app) as (url, _requests), async_playwright() as tool:
        browser = await tool.chromium.launch(
            executable_path=os.environ.get('MSG_BROWSER_PATH') or shutil.which('chromium')
        )
        try:
            page = await browser.new_page()

            async def wait_state(predicate):
                # Read native state through the debugger without Playwright's
                # string predicate eval inside this deliberately strict CSP.
                async with asyncio.timeout(5):
                    while not await page.evaluate(predicate):
                        await asyncio.sleep(0.02)

            response = await page.goto(url + '/@ascii-release-owner/web/index.html')
            assert 'allow-pointer-lock' in response.headers['content-security-policy']
            assert await page.evaluate('window.origin') == 'null'
            await page.locator('#world').click()
            await wait_state(
                '() => document.pointerLockElement === document.querySelector("#world")'
            )
            await page.mouse.move(30, 20)
            await wait_state('() => document.querySelector("#world").dataset.yaw !== "0"')
            await page.keyboard.press('Escape')
            await wait_state('() => document.pointerLockElement === null')
            for path in ('/ordinary/index.html', '/web/other.html', '/web/tampered.html'):
                response = await page.goto(url + '/@ascii-release-owner' + path)
                assert 'allow-pointer-lock' not in response.headers['content-security-policy']
                # Unapproved page callbacks are inert. After a real click,
                # request the native API through the debugger to verify that
                # its distinct sandbox flag also rejects Pointer Lock.
                await page.locator('#world').click()
                assert (
                    await page.evaluate("""async () => {
                  try { await document.querySelector('#world').requestPointerLock();
                        return 'allowed'; }
                  catch (error) { return error.name; }
                }""")
                    == 'SecurityError'
                )
                assert await page.evaluate('document.pointerLockElement') is None
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_packaged_ascii_executes_through_real_pg_route(installed, monkeypatch):
    """The actual package asset must run with the production pin, not a mock grant."""
    from playwright.async_api import async_playwright

    body = hosted_release.ascii_release()
    assert body is not None, 'The reviewed release HTML must be packaged and pinned.'
    assert digest(body) == hosted_release.ASCII_RELEASE_DIGEST
    monkeypatch.setitem(globals(), 'APPROVED', body)
    app, _ = installed
    await publish_fixture(app, monkeypatch)
    async with browser_server(app) as (url, requests), async_playwright() as tool:
        browser = await tool.chromium.launch(
            executable_path=os.environ.get('MSG_BROWSER_PATH') or shutil.which('chromium')
        )
        try:
            page = await browser.new_page()

            async def wait_state(predicate, *, timeout=5):
                # Poll from the debugger instead of invoking an eval loop in
                # the page, which intentionally has no unsafe-eval grant.
                async with asyncio.timeout(timeout):
                    while not await page.evaluate(predicate):
                        await asyncio.sleep(0.02)

            response = await page.goto(url + '/@ascii-release-owner/web/index.html')
            assert response.status == 200
            assert await response.body() == body
            headers = await response.all_headers()
            expected = hosting.hosted_headers(
                hosted_release.ASCII_SITE_ID, 'index.html', digest(body)
            )
            assert headers['content-security-policy'] == expected['Content-Security-Policy']
            assert 'allow-same-origin' not in headers['content-security-policy']
            assert headers['content-security-policy'].split(';', 1)[0].split() == [
                'sandbox',
                'allow-scripts',
                'allow-pointer-lock',
            ]
            assert 'set-cookie' not in headers and 'access-control-allow-origin' not in headers
            assert await page.evaluate('window.origin') == 'null'
            assert await page.locator('a[href], form, iframe, [src]').count() == 0
            await wait_state(
                '() => Number(document.querySelector("#world").dataset.time) >= 8.5',
                timeout=15,
            )
            assert len(await page.locator('#world').text_content()) > 1000
            before = len(requests)
            await page.locator('#coffee').click()
            await wait_state('() => document.querySelector("#world").dataset.sipping === "true"')
            await page.locator('#stance').click()
            await wait_state('() => document.querySelector("#world").dataset.stance === "standing"')
            position = await page.locator('#world').get_attribute('data-position')
            await page.keyboard.down('w')
            try:
                async with asyncio.timeout(5):
                    while await page.locator('#world').get_attribute('data-position') == position:
                        await asyncio.sleep(0.02)
            finally:
                await page.keyboard.up('w')
            await page.locator('#world').click()
            await wait_state(
                '() => document.pointerLockElement === document.querySelector("#world")'
            )
            yaw = await page.locator('#world').get_attribute('data-yaw')
            await page.mouse.move(30, 20)
            async with asyncio.timeout(5):
                while await page.locator('#world').get_attribute('data-yaw') == yaw:
                    await asyncio.sleep(0.02)
            await page.keyboard.press('Escape')
            await wait_state('() => document.pointerLockElement === null')
            assert '继续' in await page.locator('#pause').text_content()
            assert len(requests) == before
        finally:
            await browser.close()
