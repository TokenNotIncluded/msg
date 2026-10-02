"""Offline browser acceptance for profile artwork and the owner editor.

Uses production HTML/CSP/art generators with intercepted read fixtures. It
verifies UI behavior, not PostgreSQL authorization or public deployment. Run
the real profile tests separately. No accounts or server writes are created.
MSG_BROWSER_PATH may select an installed Chromium executable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from msg.core.profile_art import generate_svg, still_svg
from msg.transports.home_page import HOME_BROWSER_HEADERS, document_html

ORIGIN = 'http://127.0.0.1:8999'
ARTIFACTS = Path(os.environ.get('MSG_PROFILE_ART_ARTIFACTS', 'artifacts/profile-art'))
CUSTOM = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 320"><text x="30" y="160" fill="#73864b">CUSTOM SVG</text></svg>'
PROFILES = {
    'owner': ('u_fixture_owner', ''),
    'other': ('u_fixture_other', ''),
    'root': ('u_root', 'root'),
    'online-ca': ('u_online_ca', 'online_ca'),
}


def page_html(handle, *, authenticated=True):
    uid, role = PROFILES[handle]
    return document_html(
        '',
        title='@' + handle,
        account={'name': '@owner'} if authenticated else None,
        resource={
            'id': uid,
            'name': '@' + handle,
            'type': 'user',
            'kind': 'system' if role else 'registered',
            'created_at': '2026-10-02T00:00:00Z',
            'profile': {'post_count': 0, 'latest_posts': [], 'bio': 'Profile artwork fixture.'},
        },
        raw_path='/@' + handle,
    )


def run():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get('MSG_BROWSER_PATH', '/usr/bin/chromium'),
            args=['--no-sandbox'],
        )
        for width, height in ((1040, 850), (390, 844)):
            context = browser.new_context(viewport={'width': width, 'height': height})
            page = context.new_page()
            errors, requests = [], []
            page.on('pageerror', lambda error, errors=errors: errors.append(str(error)))
            state = {'authenticated': True, 'existing': False, 'custom': False}

            def route(request_route, state=state, requests=requests):
                request = request_route.request
                requests.append((request.method, request.url))
                url = urlsplit(request.url)
                assert request.method == 'GET', (request.method, request.url)
                if url.path == '/@owner/AVATAR.svg/meta':
                    body = (
                        {
                            'id': 'r_fixture',
                            'revision': 'v_fixture',
                            'generation': 3,
                            'type': 'file',
                        }
                        if state['existing']
                        else {'error': {'code': 'not_found'}}
                    )
                    request_route.fulfill(
                        status=200 if state['existing'] else 404,
                        content_type='application/json',
                        body=json.dumps(body),
                    )
                elif '/art/' in url.path:
                    handle = url.path.split('/')[1][1:]
                    uid, role = PROFILES[handle]
                    kind = url.path.rsplit('/', 1)[1].split('.')[0]
                    svg = (
                        CUSTOM
                        if state['custom'] and kind == 'avatar'
                        else generate_svg(uid, kind, role)
                    )
                    if url.query == 'still=1':
                        svg = still_svg(svg)
                    request_route.fulfill(
                        status=200,
                        content_type='image/svg+xml',
                        headers={
                            'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; sandbox"
                        },
                        body=svg,
                    )
                elif url.path[2:] in PROFILES:
                    request_route.fulfill(
                        status=200,
                        content_type='text/html',
                        headers=HOME_BROWSER_HEADERS,
                        body=page_html(url.path[2:], authenticated=state['authenticated']),
                    )
                else:
                    request_route.fulfill(status=404, body='fixture read not found')

            page.route('**/*', route)
            for handle in PROFILES:
                page.goto(ORIGIN + '/@' + handle)
                page.locator('.profile-avatar').wait_for()
                page.wait_for_function(
                    "[...document.querySelectorAll('[data-motion-src]')].every(i=>i.complete&&i.naturalWidth>0)"
                )
                assert page.locator('.profile-edit-link').is_visible() == (handle == 'owner')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), handle
                page.screenshot(path=str(ARTIFACTS / f'{handle}-{width}.png'), full_page=True)

            page.goto(ORIGIN + '/@owner')
            page.locator('.profile-edit-link').click()
            upload = page.locator('input[type=file]')
            upload.set_input_files({
                'name': 'avatar.svg',
                'mimeType': 'image/svg+xml',
                'buffer': CUSTOM.encode(),
            })
            page.wait_for_function("document.querySelector('.profile-art-prepared').hidden===false")
            assert (
                'file.create @msg-profile-avatar.json --request-id profile_art_'
                in page.locator('.profile-art-prepared code').inner_text()
            )
            assert 'not saved yet' in page.locator('[role=status]').inner_text()
            assert page.locator('.profile-art-preview').is_visible()
            with page.expect_download() as download_info:
                page.locator('.profile-art-editor a[download]').click()
            request_file = Path(download_info.value.path())
            packet = json.loads(request_file.read_text())
            assert packet['parent'] == '/@owner' and packet['name'] == 'AVATAR.svg'
            assert packet['media_type'] == 'image/svg+xml'
            page.screenshot(path=str(ARTIFACTS / f'owner-edit-{width}.png'), full_page=True)
            state['existing'] = True
            upload.set_input_files({
                'name': 'avatar.svg',
                'mimeType': 'image/svg+xml',
                'buffer': CUSTOM.encode(),
            })
            page.wait_for_function("document.querySelector('.profile-art-prepared').hidden===false")
            assert (
                'file.write @msg-profile-avatar.json --request-id profile_art_'
                in page.locator('.profile-art-prepared code').inner_text()
            )
            write_command = page.locator('.profile-art-prepared code').inner_text()
            assert write_command.endswith(' --expect r_fixture=3')
            with page.expect_download() as download_info:
                page.locator('.profile-art-editor a[download]').click()
            packet = json.loads(Path(download_info.value.path()).read_text())
            assert packet['id'] == 'r_fixture' and packet['base_revision'] == 'v_fixture'
            assert page.locator('.profile-art-prepared code').inner_text() == write_command
            before = len(requests)
            upload.set_input_files({
                'name': 'unsafe.svg',
                'mimeType': 'image/svg+xml',
                'buffer': b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            })
            page.wait_for_function(
                "document.querySelector('[role=status]').textContent.includes('Unsupported SVG')"
            )
            assert page.locator('.profile-art-prepared').is_hidden()
            assert page.locator('.profile-art-preview').is_hidden() and len(requests) == before
            state['authenticated'] = False
            page.reload()
            assert (
                page.locator('.profile-edit-link').is_hidden()
                and page.locator('.profile-art-editor').is_hidden()
            )
            state['custom'] = True
            page.reload()
            page.wait_for_function(
                "document.querySelector('.profile-avatar').complete&&document.querySelector('.profile-avatar').naturalWidth>0"
            )
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.emulate_media(reduced_motion='reduce')
            page.wait_for_function(
                "[...document.querySelectorAll('[data-motion-src]')].every(i=>i.getAttribute('src').endsWith('?still=1'))"
            )
            assert not errors, errors
            results.append({
                'width': width,
                'owner_create_and_update': True,
                'unsafe_rejected': True,
                'owner_only': True,
                'overflow': False,
                'reduced_motion': True,
                'writes': 0,
                'page_errors': errors,
            })
            context.close()
        browser.close()
    print(
        json.dumps(
            {
                'scope': 'offline production-HTML UI fixture',
                'results': results,
                'artifacts': str(ARTIFACTS),
            },
            indent=2,
        )
    )


if __name__ == '__main__':
    run()
