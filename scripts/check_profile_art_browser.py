"""Offline browser acceptance for profile artwork and the owner editor.

Uses production HTML/CSP/art generators with intercepted read fixtures. It
verifies UI behavior, not PostgreSQL authorization or public deployment. Run
the real profile tests separately. No accounts or server writes are created.
MSG_BROWSER_PATH may select an installed Chromium executable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from msg.core.codec import unb64
from msg.core.profile_art import generate_svg, still_svg
from msg.transports.home_page import HOME_BROWSER_HEADERS, document_html

ORIGIN = 'http://127.0.0.1:8999'
ARTIFACTS = Path(os.environ.get('MSG_PROFILE_ART_ARTIFACTS', 'artifacts/profile-art'))
CUSTOM = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 320"><text x="30" y="160" fill="#73864b">CUSTOM SVG · 头像 🚀 / +</text></svg>'
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

            def route(request_route, _request, state=state, requests=requests):
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
                    "() => [...document.querySelectorAll('[data-motion-src]')].every(i=>i.complete&&i.naturalWidth>0)"
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
            page.wait_for_function(
                "() => document.querySelector('.profile-art-prepared').hidden===false"
            )
            command = page.locator('.profile-art-command').text_content()
            assert 'file.create - --request-id profile_art_' in command.splitlines()[0]
            assert command.splitlines()[0].endswith(" <<'MSG_PROFILE_REQUEST'")
            assert command.endswith('\nMSG_PROFILE_REQUEST\n')
            command_packet = json.loads('\n'.join(command.splitlines()[1:-1]))
            assert unb64(command_packet['data']) == CUSTOM.encode()
            assert 'not saved yet' in page.locator('.profile-art-editor [role=status]').inner_text()
            assert page.locator('.profile-art-preview').is_visible()
            page.evaluate(
                '() => {navigator.clipboard.writeText=async text=>{window.copiedSaveCommand=text}}'
            )
            page.locator('.profile-art-editor button').click()
            assert page.evaluate('window.copiedSaveCommand') == command
            assert 'not saved yet' in page.locator('.profile-art-editor [role=status]').inner_text()
            page.locator('.profile-art-download summary').click()
            with page.expect_download() as download_info:
                page.locator('.profile-art-editor a[download]').click()
            request_file = Path(download_info.value.path())
            packet = json.loads(request_file.read_text())
            assert packet['parent'] == '/@owner' and packet['name'] == 'AVATAR.svg'
            assert packet['media_type'] == 'image/svg+xml'
            assert packet == command_packet and unb64(packet['data']) == CUSTOM.encode()
            assert (
                'file.create @msg-profile-avatar.json --request-id profile_art_'
                in page.locator('.profile-art-file-command').inner_text()
            )
            (ARTIFACTS / f'owner-create-{width}.json').write_text(json.dumps(packet))
            (ARTIFACTS / f'owner-create-{width}.sh').write_text(command)
            page.screenshot(path=str(ARTIFACTS / f'owner-edit-{width}.png'), full_page=True)
            state['existing'] = True
            upload.set_input_files({
                'name': 'avatar.svg',
                'mimeType': 'image/svg+xml',
                'buffer': CUSTOM.encode(),
            })
            page.wait_for_function(
                "() => document.querySelector('.profile-art-prepared').hidden===false"
            )
            write_command = page.locator('.profile-art-command').text_content()
            assert 'file.write - --request-id profile_art_' in write_command.splitlines()[0]
            assert " --expect r_fixture=3 <<'MSG_PROFILE_REQUEST'" in write_command.splitlines()[0]
            write_packet = json.loads('\n'.join(write_command.splitlines()[1:-1]))
            assert unb64(write_packet['data']) == CUSTOM.encode()
            with page.expect_download() as download_info:
                page.locator('.profile-art-editor a[download]').click()
            packet = json.loads(Path(download_info.value.path()).read_text())
            assert packet['id'] == 'r_fixture' and packet['base_revision'] == 'v_fixture'
            assert packet == write_packet and unb64(packet['data']) == CUSTOM.encode()
            assert page.locator('.profile-art-command').text_content() == write_command
            (ARTIFACTS / f'owner-write-{width}.json').write_text(json.dumps(packet))
            (ARTIFACTS / f'owner-write-{width}.sh').write_text(write_command)
            page.evaluate(
                "() => {navigator.clipboard.writeText=async()=>{throw Error('clipboard blocked')}}"
            )
            page.locator('.profile-art-editor button').click()
            assert page.locator('.profile-art-command-view').get_attribute('open') is not None
            assert (
                'full command below'
                in page.locator('.profile-art-editor [role=status]').inner_text()
            )
            before = len(requests)
            upload.set_input_files({
                'name': 'unsafe.svg',
                'mimeType': 'image/svg+xml',
                'buffer': b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            })
            page.wait_for_function(
                "() => document.querySelector('.profile-art-editor [role=status]').textContent.includes('Unsupported SVG')"
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
                "() => document.querySelector('.profile-avatar').complete&&document.querySelector('.profile-avatar').naturalWidth>0"
            )
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.emulate_media(reduced_motion='reduce')
            page.wait_for_function(
                "() => [...document.querySelectorAll('[data-motion-src]')].every(i=>i.getAttribute('src').endsWith('?still=1'))"
            )
            assert not errors, errors
            results.append({
                'width': width,
                'owner_create_and_update': True,
                'ui_data_decoded': True,
                'stdin_and_download_requests_match': True,
                'copied_command_stays_unsaved': True,
                'clipboard_fallback': True,
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


@contextmanager
def backup_download_server(cipher, digest):
    """Serve ciphertext over localhost for Chromium's native download path."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert not self.headers.get('Cookie')
            allowed = {
                f'/@owner/BACKUP-{digest[:16]}.age/raw',
                f'/@owner/BACKUP-{digest[:16]}.gpg/raw',
            }
            if self.path not in allowed:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(cipher)))
            self.end_headers()
            self.wfile.write(cipher)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def run_backup():
    """Public backup discovery matrix; no repeated artwork-editor acceptance."""
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    cipher = b'age-encryption.org/v1\nsynthetic browser download fixture\n'
    digest = hashlib.sha256(cipher).hexdigest()
    manifest = {
        'schema': 'msg.identity-backup/1',
        'server': '',
        'subject_id': PROFILES['owner'][0],
        'key_id': 'k_fixture_old_signing_key',
        'created_at': '2026-10-02T14:00:00.000000Z',
        'encryption': {'format': 'age'},
        'archive_format': 'msg.account-backup/1',
        'file': {
            'path': '/@owner/BACKUP-' + digest[:16] + '.age',
            'sha256': digest,
            'size': len(cipher),
        },
    }
    results = []
    with backup_download_server(cipher, digest) as origin, sync_playwright() as playwright:
        manifest['server'] = origin
        browser = playwright.chromium.launch(
            executable_path=os.environ.get('MSG_BROWSER_PATH', '/usr/bin/chromium'),
            args=['--no-sandbox'],
        )
        for width, height in ((1040, 850), (390, 844)):
            context = browser.new_context(viewport={'width': width, 'height': height})
            page = context.new_page()
            errors, requests, cases = [], [], []
            page.on('pageerror', lambda error, errors=errors: errors.append(str(error)))
            state = {'mode': 'missing', 'value': manifest, 'authenticated': False}

            def route(request_route, _request, state=state, requests=requests):
                request = request_route.request
                requests.append((request.method, request.url))
                assert request.method == 'GET' and request.url.startswith(origin + '/'), requests[
                    -1
                ]
                path = urlsplit(request.url).path
                if path == '/@owner/BACKUP.json/raw':
                    assert 'cookie' not in request.headers
                    if state['mode'] == 'missing':
                        request_route.fulfill(status=404, body='not found')
                    elif state['mode'] == 'failure':
                        request_route.fulfill(status=503, body='unavailable')
                    elif state['mode'] == 'forbidden':
                        request_route.fulfill(status=403, body='forbidden')
                    elif state['mode'] == 'redirect':
                        request_route.fulfill(
                            status=302, headers={'location': 'https://evil.invalid/backup'}
                        )
                    elif state['mode'] == 'oversized':
                        request_route.fulfill(status=200, body=' ' * 16385)
                    elif state['mode'] == 'invalid_json':
                        request_route.fulfill(status=200, body='{broken')
                    else:
                        request_route.fulfill(
                            status=200,
                            content_type='application/json',
                            body=json.dumps(state['value']),
                        )
                elif path == '/@owner':
                    request_route.fulfill(
                        status=200,
                        content_type='text/html',
                        headers=HOME_BROWSER_HEADERS,
                        body=page_html('owner', authenticated=state['authenticated']),
                    )
                elif '/art/' in path:
                    request_route.fulfill(
                        status=200,
                        content_type='image/svg+xml',
                        body='<svg xmlns="http://www.w3.org/2000/svg"><text y="20">fixture</text></svg>',
                    )
                elif path in {
                    manifest['file']['path'] + '/raw',
                    manifest['file']['path'].removesuffix('.age') + '.gpg/raw',
                }:
                    request_route.continue_()
                else:
                    request_route.fulfill(status=404, body='fixture read not found')

            page.route('**/*', route)

            def visit(mode, value=None, *, state=state, page=page, cases=cases):
                state.update(mode=mode, value=value or manifest)
                page.goto(origin + '/@owner')
                page.wait_for_function(
                    "() => document.querySelector('.profile-backup').getAttribute('aria-busy')==='false'"
                )
                assert page.locator('.profile-backup').is_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                cases.append(mode)

            def failed(page=page):
                assert (
                    'Could not read backup information'
                    in page.locator('.profile-backup-status').inner_text()
                )
                assert page.locator('.profile-backup-record').is_hidden()
                assert page.locator('.profile-backup-download').get_attribute('href') is None
                assert page.locator('.profile-backup-retry').is_visible()

            visit('missing')
            assert 'No backup registered' in page.locator('.profile-backup-status').inner_text()
            assert page.locator('.profile-backup-record').is_hidden()
            assert page.locator('.profile-art-editor').is_hidden()
            assert page.locator('.profile-backup-owner').is_hidden()

            visit('valid')
            assert page.locator('.profile-backup-record').is_visible()
            assert page.locator('[data-backup-field=sha256]').inner_text() == digest
            assert page.locator('[data-backup-field=format]').inner_text() == 'age'
            assert 'UTC' in page.locator('[data-backup-field=created_at]').inner_text()
            assert str(len(cipher)) in page.locator('[data-backup-field=size]').inner_text()
            command = page.locator('.profile-backup-command').text_content()
            assert command == (
                "msg --server '" + origin + "' account restore restored-account "
                "--from '@owner' --identity '/path/to/identity.txt'"
            )
            page.evaluate(
                '() => {navigator.clipboard.writeText=async value=>{window.backupCopied=value}}'
            )
            page.locator('.profile-backup-copy').click()
            assert page.evaluate('window.backupCopied') == command
            assert 'Restore command copied' in page.locator('.profile-backup-status').inner_text()
            with page.expect_download() as info:
                page.locator('.profile-backup-download').click()
            assert Path(info.value.path()).read_bytes() == cipher
            assert info.value.suggested_filename == manifest['file']['path'].split('/')[-1]
            page.screenshot(path=str(ARTIFACTS / f'backup-valid-{width}.png'), full_page=True)
            page.locator('.profile-backup').screenshot(
                path=str(ARTIFACTS / f'backup-panel-{width}.png')
            )
            page.evaluate(
                "() => {navigator.clipboard.writeText=async()=>{throw Error('clipboard blocked')}}"
            )
            page.locator('.profile-backup-copy').click()
            assert page.locator('.profile-backup-command-view').get_attribute('open') is not None
            assert 'Select and copy' in page.locator('.profile-backup-status').inner_text()

            gpg = json.loads(json.dumps(manifest))
            gpg['encryption']['format'] = 'gpg'
            gpg['file']['path'] = gpg['file']['path'].removesuffix('.age') + '.gpg'
            visit('gpg', gpg)
            assert page.locator('.profile-backup-record').is_visible()
            assert page.locator('.profile-backup-copy').is_hidden()
            assert page.locator('.profile-backup-command').text_content() == ''
            external = {**manifest, 'archive_format': 'external'}
            visit('external', external)
            assert page.locator('.profile-backup-copy').is_hidden()

            malicious_hint = {
                **manifest,
                'recovery_hint': '<img src="https://evil.invalid/key" onerror="window.manifestExecuted=true"> $(echo unsafe) javascript:unsafe',
            }
            visit('malicious_hint', malicious_hint)
            assert page.locator('.profile-backup-record').is_visible()
            assert (
                malicious_hint['recovery_hint'] in page.locator('.profile-backup-hint').inner_text()
            )
            assert page.locator('.profile-backup-hint img').count() == 0
            assert page.evaluate('window.manifestExecuted') is None
            assert page.locator('.profile-backup-command').text_content() == command

            unicode_hint = {**manifest, 'recovery_hint': '\U0001f510' * 500}
            visit('unicode_hint', unicode_hint)
            assert page.locator('.profile-backup-record').is_visible()

            invalid = [
                (
                    'foreign_path',
                    {'file': {**manifest['file'], 'path': 'https://evil.invalid/backup.age'}},
                ),
                (
                    'private_files_path',
                    {
                        'file': {
                            **manifest['file'],
                            'path': manifest['file']['path'].replace('/@owner/', '/@owner/files/'),
                        }
                    },
                ),
                (
                    'encoded_path',
                    {'file': {**manifest['file'], 'path': manifest['file']['path'] + '%2f..'}},
                ),
                ('foreign_subject', {'subject_id': 'u_fixture_other'}),
                ('foreign_server', {'server': 'https://evil.invalid'}),
                ('wrong_schema', {'schema': 'msg.identity-backup/999'}),
                ('zero_year', {'created_at': '0000-10-02T14:00:00Z'}),
                ('wrong_hash', {'file': {**manifest['file'], 'sha256': 'not-a-hash'}}),
                ('wrong_size', {'file': {**manifest['file'], 'size': 8388609}}),
                ('newline_hint', {'recovery_hint': 'one\ntwo'}),
                ('tab_hint', {'recovery_hint': 'one\ttwo'}),
                ('long_hint', {'recovery_hint': '\U0001f510' * 501}),
                (
                    'manifest_command',
                    {'restore_command': 'javascript:window.manifestExecuted=true'},
                ),
            ]
            for mode, patch in invalid:
                visit(mode, {**manifest, **patch})
                failed()
            for mode in ('failure', 'forbidden', 'redirect', 'oversized', 'invalid_json'):
                visit(mode)
                failed()
            state.update(mode='valid', value=manifest, authenticated=True)
            page.locator('.profile-backup-retry').click()
            page.wait_for_function(
                "() => document.querySelector('.profile-backup-record').hidden===false"
            )
            assert (
                'Encrypted backup registered' in page.locator('.profile-backup-status').inner_text()
            )
            visit('owner', manifest)
            assert page.locator('.profile-backup-owner').is_visible()
            assert not errors, errors
            results.append({
                'width': width,
                'cases': cases,
                'writes': 0,
                'overflow': False,
                'page_errors': errors,
            })
            context.close()
        browser.close()
    print(
        json.dumps(
            {
                'scope': 'offline public backup UI fixture; ciphertext is synthetic',
                'results': results,
                'artifacts': str(ARTIFACTS),
            },
            indent=2,
        )
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--backup-only',
        action='store_true',
        help='Run only the new public backup discovery acceptance.',
    )
    if parser.parse_args().backup_only:
        run_backup()
    else:
        run()
