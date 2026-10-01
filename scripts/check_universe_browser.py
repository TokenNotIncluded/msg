"""Exercise the packaged page under its real CSP using isolated HTTP fixtures.

Requires Playwright/Chromium only in the dedicated browser CI job. This is not
production data, a deployment script, or a replacement for backend ACL tests.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

from msg.bootstrap import ROOT_WEB_SAMPLE
from msg.core.codec import b64, decode, digest
from msg.core.models import OperationRequest
from msg.core.requests import payload_fields, signing_bytes
from msg.extensions.hosting import hosted_headers
from msg.security.crypto import Ed25519Signer, verify

OUTPUT = Path('artifacts/universe')
OUTPUT.mkdir(parents=True, exist_ok=True)
KEY = Ed25519Signer.generate()
NAMES = [
    'lightjunction',
    'ada',
    'kei',
    'orbit',
    'lin',
    'atlas',
    'echo',
    'nova',
    'sol',
    'mira',
    'ion',
    'sage',
    'pico',
    'pixel',
    'alba',
    'lyra',
    'vega',
    'rem',
    'aiko',
    'ember',
    'quill',
    'neon',
    'kira',
    'rune',
    'flux',
    'noor',
    'cleo',
    'cass',
    'nero',
    'ash',
    'odin',
    'luna',
    'iris',
    'aster',
    'sora',
    'cosmo',
]
USERS = [{'id': 'u_' + digest(name)[7:39], 'name': name, 'path': '/@' + name} for name in NAMES]
POSTS = [
    {
        'id': 'p_' + str(i + 1).zfill(32),
        'path': f'/main/post{i}.md',
        'title': [
            'What survives between conversations?',
            'A small map of somewhere',
            'The geometry of a good question',
            'Building for quiet discovery',
            'Notes from the edge',
        ][i % 5],
        'excerpt': 'An isolated browser verification fixture, not live activity.',
        'created_at': '2026-10-01T13:00:00.000000Z',
        'author': USERS[i % len(USERS)],
        'reply_to': {'id': 'p_' + str(i).zfill(32), 'path': f'/main/post{i - 1}.md', 'name': 'post'}
        if i
        else None,
    }
    for i in range(24)
]
CONVERSATION = {
    'conversation_id': 't_private_fixture',
    'other_subject': USERS[1]['id'],
    'contact': {'name': 'ada', 'path': '/@ada'},
    'path': '/@lightjunction/messages/private-fixture',
    'state': 'active',
    'initiator': USERS[0]['id'],
}
PRIVATE_BODY = 'PRIVATE FIXTURE — must disappear when this view closes.'
PUBLIC_BODY = '<img src=x onerror="window.__injected=true">\nA fixture post, not executable markup.'
SENT = []
REQUESTS = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def respond(self, data, *, html=False):
        body = data if html else json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html' if html else 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'private, no-store')
        if html:
            for key, value in hosted_headers(
                'w_root_web', 'index.html', digest(ROOT_WEB_SAMPLE)
            ).items():
                if key.lower() != 'cache-control':
                    self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        REQUESTS.append(('GET', parsed.path, self.headers.get('Cookie')))
        if parsed.path == '/@root/web/':
            self.respond(ROOT_WEB_SAMPLE, html=True)
            return
        data = {}
        if parsed.path == '/_universe':
            kind = query.get('kind', ['users'])[0]
            data = {
                'version': 1,
                'kind': kind,
                'items': USERS
                if kind == 'users'
                else [
                    post
                    for post in POSTS
                    if not query.get('author') or post['author']['id'] == query['author'][0]
                ],
                'author': query.get('author', [None])[0],
                'cursor': None,
                'service': SERVICE,
            }
        elif parsed.path == '/_universe/me':
            logged_in = 'fixture_session=owner' in self.headers.get('Cookie', '')
            data = {
                'version': 1,
                'account': USERS[0] if logged_in else None,
                'conversations': [CONVERSATION] if logged_in else [],
                'service': SERVICE,
            }
        elif parsed.path == CONVERSATION['path'] + '/json':
            data = {
                'conversation': {
                    'state': 'active',
                    'messages': [
                        {
                            'id': 'p_private_message',
                            'path': '/private-message.md',
                            'title': 'A private satellite',
                            'body': PRIVATE_BODY,
                            'author': {'name': 'ada'},
                            'created_at': '2026-10-01T13:00:00.000000Z',
                            'truncated': False,
                        }
                    ],
                    'older_messages': False,
                }
            }
        elif parsed.path.startswith('/_r/'):
            data = {'content': PUBLIC_BODY}
        elif parsed.path == '/private-message.md/json':
            data = {'content': PRIVATE_BODY}
        elif parsed.path == '/@lightjunction/pk':
            data = {
                'subject_id': USERS[0]['id'],
                'public_key': b64(KEY.public_key),
                'key_id': KEY.key_id,
                'retired_at': None,
            }
        elif parsed.path == '/@lightjunction/cert/json':
            data = {'certificates': []}
        self.respond(data)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        packet = decode(OperationRequest, json.loads(raw))
        assert not self.headers.get('Cookie')
        assert packet.target_service == SERVICE and packet.subject == USERS[0]['id']
        assert packet.payload_digest == digest(payload_fields(packet))
        verify(KEY.public_key, signing_bytes(packet), packet.proof.signature, purpose='request')
        SENT.append(packet)
        self.respond({'status': 'ok', 'data': {}, 'resources': []})


SERVER = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
SERVICE = f'http://127.0.0.1:{SERVER.server_port}'
threading.Thread(target=SERVER.serve_forever, daemon=True).start()
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, args=['--enable-unsafe-swiftshader', '--use-angle=swiftshader']
        )
        context = browser.new_context(
            viewport={'width': 1440, 'height': 900}, reduced_motion='reduce'
        )
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(SERVICE + '/@root/web/')
        expect(page.locator('#counts')).to_contain_text('36 STARS')
        assert page.locator('#space').get_attribute('data-renderer') == 'webgl'
        page.screenshot(path=str(OUTPUT / 'desktop-fixture.png'))
        before = page.locator('#space').screenshot()
        page.locator('#space').hover(position={'x': 900, 'y': 500})
        page.mouse.down()
        page.mouse.move(1100, 550, steps=8)
        page.mouse.up()
        assert before != page.locator('#space').screenshot()
        page.click('#catalog-toggle')
        page.locator('#catalog-items button').first.click()
        assert page.locator('#progress-label').inner_text().startswith('1 / 6')
        page.screenshot(path=str(OUTPUT / 'star-fixture.png'))
        page.click('#detail-close')
        page.click('#catalog-toggle')
        page.locator('#catalog-items button').nth(37).click()
        expect(page.locator('#detail-body')).to_contain_text('onerror')
        assert page.evaluate('window.__injected === undefined')
        assert page.locator('#detail-body img').count() == 0
        page.get_by_role('button', name='Follow the reply').click()
        expect(page.locator('#progress-label')).to_contain_text('4 / 6')
        page.click('#detail-close')
        page.click('#private-tab')
        expect(page.locator('#status')).to_contain_text('Sign in')
        assert PRIVATE_BODY not in page.content()
        context.add_cookies([{'name': 'fixture_session', 'value': 'owner', 'url': SERVICE}])
        page.click('#private-tab')
        expect(page.locator('#counts')).to_contain_text('YOUR CONVERSATIONS')
        page.click('#catalog-toggle')
        page.locator('#catalog-items button').nth(1).click()
        expect(page.locator('#detail-body')).to_contain_text('PRIVATE FIXTURE')
        page.screenshot(path=str(OUTPUT / 'private-fixture.png'))
        page.click('#public-tab')
        expect(page.locator('#counts')).to_contain_text('STARS')
        assert PRIVATE_BODY not in page.content()
        assert not [cookie for method, path, cookie in REQUESTS if path == '/_universe' and cookie]
        page.click('#compose-open')
        page.fill('#message', 'A signed browser signal: 宇宙 🌟')
        page.click('#connect-open')
        page.fill('#handle', '@lightjunction')
        page.locator('#key-file').set_input_files({
            'name': 'identity.key',
            'mimeType': 'application/octet-stream',
            'buffer': KEY.private_bytes(),
        })
        page.click('#connect')
        expect(page.locator('#key-dialog')).not_to_be_visible()
        assert page.locator('#key-file').input_value() == ''
        page.click('#send')
        expect(page.locator('#composer')).not_to_be_visible()
        assert len(SENT) == 1 and SENT[0].arguments['body'] == 'A signed browser signal: 宇宙 🌟'
        page.set_viewport_size({'width': 390, 'height': 844})
        page.click('#home')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(OUTPUT / 'mobile-fixture.png'))
        page.click('#catalog-toggle')
        page.locator('#catalog-items button').first.focus()
        page.keyboard.press('Enter')
        assert page.locator('#inspector').is_visible()
        assert not errors, errors
        context.close()
        browser.close()
    print(
        'Universe: WebGL, CSP, orbiting, keyboard/mobile, text safety, private separation and signed writes passed.'
    )
finally:
    SERVER.shutdown()
