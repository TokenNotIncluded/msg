"""Local visual regressions for the token field, with explicitly synthetic data.

Run: python scripts/check_nebula_browser.py
Requires Playwright and Chromium. MSG_BROWSER_PATH may select an installed
browser; MSG_NEBULA_OUTPUT selects the evidence directory. No live site is used. Set MSG_NEBULA_OFFLINE_DOM=1 for policy-restricted
local DOM testing, and MSG_NEBULA_EXPECT_RENDERER=canvas-3d to explicitly
require the software renderer. These modes do not count as HTTP/WebGL tests.
This fixture tests the actual packaged scripts under the release CSP shape, not
the Python/SQL authorization path (covered by test_star_projection.py).
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import threading
from copy import deepcopy
from io import BytesIO
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'src/msg/data'
OUTPUT = Path(os.environ.get('MSG_NEBULA_OUTPUT', 'artifacts/token-nebula'))
OUTPUT.mkdir(parents=True, exist_ok=True)
NOW = datetime.now(UTC)
OFFLINE_DOM = os.environ.get('MSG_NEBULA_OFFLINE_DOM') == '1'


def stamp(seconds=0):
    return (NOW + timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')


def assemble():
    page = (DATA / 'root-web.html').read_text()
    css = (DATA / 'root-web.css').read_text()
    for token, name in (
        ('__SANS_FONT__', 'root-web-sans.woff2'),
        ('__SANS_BOLD_FONT__', 'root-web-sans-bold.woff2'),
    ):
        css = css.replace(token, base64.b64encode((DATA / name).read_bytes()).decode())
    scripts = '\n'.join(
        (DATA / ('root-web-' + name + '.js')).read_text()
        for name in ('model', 'renderer', 'app')
    )
    return (
        page.replace('__LOGO__', (DATA / 'logo.svg').read_text())
        .replace('__UNIVERSE_STYLE__', css)
        .replace('__UNIVERSE_SCRIPT__', scripts)
        .encode()
    )


PAGE = assemble()
SCRIPT = re.search(rb'<script>(.*?)</script>', PAGE, re.DOTALL).group(1)
PIN = base64.b64encode(hashlib.sha256(SCRIPT).digest()).decode()
CSP = (
    "sandbox allow-scripts allow-same-origin; default-src 'none'; script-src 'sha256-"
    + PIN
    + "'; style-src 'unsafe-inline'; font-src data:; img-src data:; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
NAMES = [
    'root', 'lightjunction', 'ada', 'kei', 'orbit', 'lin', 'atlas', 'echo', 'nova',
    'sol', 'mira', 'ion', 'sage', 'pico', 'pixel', 'alba', 'lyra', 'vega', 'rem',
    'aiko', 'ember', 'quill', 'neon', 'kira', 'rune', 'flux', 'noor', 'cleo',
    'cass', 'nero', 'ash', 'odin', 'luna', 'iris', 'aster', 'sora', 'cosmo',
]


def facts(i):
    return {
        'role': 'root' if i == 0 else 'user',
        'checked_at': stamp(),
        'certificate': {
            'state': 'valid' if i % 4 == 1 else 'none',
            'id': 'cert_fixture_' + str(i),
            'path': '/fixture-cert-' + str(i),
            'expires_at': stamp(86400),
            'kind': 'access',
        },
        'presence': {
            'state': ['available', 'unknown', 'busy', 'away'][i % 4],
            'self_reported': True,
            'updated_at': stamp(-5),
            'expires_at': stamp(300),
        },
        'last_public_post_at': stamp(-i * 60000),
        'balance': {
            'visibility': 'public',
            'amount_minor': str(i * 12400000),
            'scale': 6,
            'code': 'MSG',
        } if i % 3 != 2 else {'visibility': 'private'},
    }


USERS = [
    {'id': 'u_' + name, 'name': name, 'path': '/@' + name, 'star': facts(i)}
    for i, name in enumerate(NAMES)
]
POSTS = [
    {
        'id': 'p_fixture_' + str(i),
        'path': '/main/fixture-' + str(i) + '.md',
        'title': ['Between two thoughts', 'A small map of somewhere', 'Geometry of a question'][i % 3],
        'excerpt': 'Isolated local fixture. Not live user activity.',
        'author': {k: USERS[1 + i % (len(USERS) - 1)][k] for k in ('id', 'name', 'path')},
        'created_at': stamp(-i * 300),
        'reply_to': {'id': 'p_fixture_' + str(i - 1), 'path': '/main/fixture-' + str(i - 1) + '.md', 'name': 'Fixture'} if i else None,
    }
    for i in range(24)
]
REQUESTS = []
CONVERSATION = {
    'conversation_id': 't_private_fixture',
    'other_subject': 'u_ada',
    'contact': {'name': 'ada', 'path': '/@ada'},
    'path': '/@lightjunction/messages/fixture',
    'state': 'active',
    'initiator': 'u_lightjunction',
}
PRIVATE_BODY = 'PRIVATE FIXTURE — owner only.'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = urlsplit(self.path)
        query = parse_qs(path.query)
        REQUESTS.append((path.path, self.headers.get('Cookie')))
        html = path.path == '/@root/web/'
        data = {}
        if html:
            body = PAGE
        else:
            if path.path == '/_universe':
                kind = query.get('kind', ['users'])[0]
                items = USERS[1:] if kind == 'users' else POSTS
                if 'ids' in query:
                    items = [u for u in USERS if u['id'] in query['ids'][0].split(',')]
                if 'author' in query:
                    items = [p for p in POSTS if p['author']['id'] == query['author'][0]]
                data = {
                    'version': 1, 'kind': kind, 'items': items, 'cursor': None,
                    'service': SERVICE, 'generated_at': stamp(),
                    'author': query.get('author', [None])[0],
                    **({'anchor': USERS[0]} if kind == 'users' and 'ids' not in query else {}),
                }
            elif path.path == '/_universe/me':
                account = None
                if 'fixture_session=owner' in self.headers.get('Cookie', ''):
                    account = deepcopy(USERS[1])
                    account['star']['balance'] = {
                        'visibility': 'self', 'amount_minor': '9223372036854775807',
                        'scale': 6, 'code': 'MSG',
                    }
                data = {
                    'version': 1, 'account': account,
                    'conversations': [CONVERSATION] if account else [], 'service': SERVICE,
                }
            elif path.path == CONVERSATION['path'] + '/json':
                data = {'conversation': {
                    'state': 'active', 'messages': [{
                        'id': 'p_private_fixture', 'path': '/private-fixture.md',
                        'title': 'Private satellite', 'body': PRIVATE_BODY,
                        'author': {'name': 'ada'}, 'created_at': stamp(),
                    }], 'older_messages': False,
                }}
            elif path.path.startswith('/_r/'):
                data = {'content': '<img src=x onerror="window.injected=true"> Not executable.'}
            body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'private, no-store')
        if html:
            self.send_header('Content-Security-Policy', CSP)
        self.end_headers()
        self.wfile.write(body)


def fixture_request(payload):
    # Exercise the same fixture handler without network navigation. The local
    # browser policy may disallow loopback; do not weaken that policy.
    if payload.get('method', 'GET') != 'GET':
        raise AssertionError('Exploration must not perform writes')
    handler = object.__new__(Handler)
    handler.path = payload['path']
    handler.headers = {'Cookie': 'fixture_session=owner' if payload.get('owner') else ''}
    handler.wfile = BytesIO()
    handler.send_response = lambda *_args: None
    handler.send_header = lambda *_args: None
    handler.end_headers = lambda: None
    handler.do_GET()
    return handler.wfile.getvalue().decode()


def navigate(page):
    if not OFFLINE_DOM:
        page.goto(SERVICE + '/@root/web/')
        return
    page.expose_function('nebulaFixtureFetch', fixture_request)
    page.evaluate("""() => {
      window.fixtureOwner = false;
      window.fetch = async (path, options = {}) => {
        if (window.fixtureOffline) throw new TypeError('Fixture outage');
        const text = await window.nebulaFixtureFetch({path: String(path),
          owner: window.fixtureOwner && options.credentials === 'same-origin',
          method: options.method || 'GET'});
        return new Response(text, {status: 200, headers: {'Content-Type': 'application/json'}});
      };
    }""")
    # sandbox and frame-ancestors require response headers and cannot be tested
    # by set_content. Keep the script pin, styles and all network restrictions.
    policy = CSP.split('; ', 1)[1].replace("; frame-ancestors 'none'", '')
    content = PAGE.decode().replace('<head>', '<head><meta http-equiv="Content-Security-Policy" content="' + html.escape(policy, quote=True) + '">', 1)
    page.set_content(content)


def open_page(browser, *, mobile=False, reduced=True):
    context = browser.new_context(
        viewport={'width': 390, 'height': 844} if mobile else {'width': 1440, 'height': 900},
        reduced_motion='reduce' if reduced else 'no-preference',
    )
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda err: errors.append(str(err)))
    navigate(page)
    expect(page.locator('#counts')).to_contain_text('37 STARS')
    return context, page, errors


def run():
    checks = []
    with sync_playwright() as tool:
        args = ['--no-sandbox', '--enable-unsafe-swiftshader', '--use-angle=swiftshader']
        launch = {'headless': True, 'args': args}
        executable = os.environ.get('MSG_BROWSER_PATH')
        if executable:
            launch['executable_path'] = executable
        browser = tool.chromium.launch(**launch)
        context, page, errors = open_page(browser)
        renderer = page.locator('#space').get_attribute('data-renderer')
        assert renderer == os.environ.get('MSG_NEBULA_EXPECT_RENDERER', 'webgl'), renderer
        assert page.evaluate("getComputedStyle(document.querySelector('#universe')).backgroundColor") == 'rgb(0, 0, 0)'
        assert page.evaluate("getComputedStyle(document.querySelector('.coordinates')).opacity") == '1'
        page.screenshot(path=str(OUTPUT / '01-entry.png'))
        page.mouse.move(650, 400)
        page.mouse.move(720, 420)
        expect(page.locator('body')).to_have_class('exploring')
        expect(page.locator('.coordinates')).to_have_attribute('aria-hidden', 'true')
        assert page.evaluate("document.querySelector('.coordinates').inert")
        assert page.evaluate("getComputedStyle(document.querySelector('.coordinates')).opacity") == '0'
        page.screenshot(path=str(OUTPUT / '02-field.png'))
        checks.append('black background, token overlay, intro dismissal/inertness')
        page.click('#help-toggle')
        expect(page.locator('#help-dialog')).to_be_visible()
        expect(page.locator('#help-dialog')).to_contain_text('not a reputation score')
        page.click('#help-close')
        page.keyboard.press('?')
        expect(page.locator('#help-dialog')).to_be_visible()
        page.keyboard.press('Escape')
        checks.append('recoverable help, keyboard, Escape')
        page.click('#origin')
        expect(page.locator('#detail-title')).to_have_text('@root')
        expect(page.locator('.identity-status')).to_contain_text('TRUST ANCHOR')
        page.screenshot(path=str(OUTPUT / '03-root.png'))
        page.click('#detail-close')
        page.click('#catalog-toggle')
        page.locator('#catalog-items button').filter(has_text=re.compile(r'^@lightjunction')).click()
        expect(page.locator('.identity-status')).to_contain_text('CERTIFICATE ACTIVE')
        expect(page.locator('.identity-facts')).to_contain_text('12.4 MSG')
        page.screenshot(path=str(OUTPUT / '04-certificate.png'))
        checks.append('root anchor, certificate badge, precise published reserve')
        page.click('#detail-close')
        page.click('#catalog-toggle')
        page.locator('#catalog-items button').filter(has_text=re.compile(r'^@ada')).click()
        expect(page.locator('.identity-facts')).to_contain_text('Not public')
        assert page.locator('.reserve-meter').count() == 0
        page.click('#detail-close')
        page.click('#private-tab')
        expect(page.locator('#status')).to_contain_text('Sign in')
        if OFFLINE_DOM:
            page.evaluate('window.fixtureOwner = true')
        else:
            context.add_cookies([{'name': 'fixture_session', 'value': 'owner', 'url': SERVICE}])
        page.click('#private-tab')
        expect(page.locator('#counts')).to_contain_text('YOUR CONVERSATIONS')
        page.click('#catalog-toggle')
        page.locator('#catalog-items button').first.click()
        expect(page.locator('.identity-facts')).to_contain_text('9,223,372,036,854.775807 MSG')
        expect(page.locator('.identity-facts')).to_contain_text('Only visible in your orbit')
        page.screenshot(path=str(OUTPUT / '05-own-reserve.png'))
        page.click('#public-tab')
        expect(page.locator('#counts')).to_contain_text('37 STARS')
        assert '9,223,372,036,854.775807' not in page.content()
        assert not [c for p, c in REQUESTS if p == '/_universe' and c]
        checks.append('undisclosed versus zero, owner reserve, privacy purge, credential omission')
        # Test time-based transitions using a controllable browser wall clock.
        page.clock.install(time=NOW)
        page.click('#catalog-toggle')
        page.locator('#catalog-items button').filter(has_text=re.compile(r'^@lightjunction')).click()
        expect(page.locator('.identity-status')).to_contain_text('CERTIFICATE ACTIVE')
        # Prevent refresh; an outage must not leave an eternal certificate/presence badge.
        if OFFLINE_DOM:
            page.evaluate('window.fixtureOffline = true')
        else:
            page.route('**/_universe?*', lambda route: route.abort())
        page.clock.fast_forward(100_000)
        expect(page.locator('.identity-status')).not_to_contain_text('CERTIFICATE ACTIVE')
        expect(page.locator('.identity-facts')).to_contain_text('Certificate status unknown')
        checks.append('stale observations remove certification without fabricating offline')
        assert not errors, errors
        context.close()
        mobile, phone, phone_errors = open_page(browser, mobile=True)
        phone.click('#origin')
        expect(phone.locator('#detail-title')).to_have_text('@root')
        assert phone.evaluate('document.documentElement.scrollWidth <= innerWidth')
        root_label = phone.locator('.root-label').bounding_box()
        sheet = phone.locator('#inspector').bounding_box()
        assert root_label and sheet and root_label['y'] < sheet['y']
        phone.screenshot(path=str(OUTPUT / '06-mobile.png'))
        phone.click('#detail-close')
        phone.click('#catalog-toggle')
        phone.locator('#catalog-items button').first.focus()
        phone.keyboard.press('Enter')
        expect(phone.locator('#inspector')).to_be_visible()
        assert not phone_errors, phone_errors
        checks.append('390px layout, keyboard catalog')
        mobile.close()
        # An independent context forces only WebGL to fail; Canvas is still real.
        fallback = browser.new_context(viewport={'width': 1440, 'height': 900}, reduced_motion='reduce')
        fallback_script = """(() => {
          const original = HTMLCanvasElement.prototype.getContext;
          HTMLCanvasElement.prototype.getContext = function(type, ...args) {
            return type.startsWith('webgl') || type === 'experimental-webgl' ? null : original.call(this, type, ...args);
          };
        })();"""
        fallback.add_init_script(fallback_script)
        view = fallback.new_page()
        if OFFLINE_DOM:
            view.evaluate(fallback_script)
        navigate(view)
        expect(view.locator('#counts')).to_contain_text('37 STARS')
        assert view.locator('#space').get_attribute('data-renderer') == 'canvas-3d'
        view.click('#origin')
        expect(view.locator('#detail-title')).to_have_text('@root')
        view.screenshot(path=str(OUTPUT / '07-canvas-fallback.png'))
        checks.append('Canvas fallback preserves root, seals and reserve geometry')
        fallback.close()
        animated, moving, moving_errors = open_page(browser, reduced=False)
        moving.mouse.move(650, 400)
        moving.mouse.move(720, 420)
        moving.wait_for_timeout(250)
        opacity = float(moving.locator('.coordinates').evaluate('(el) => getComputedStyle(el).opacity'))
        assert 0 < opacity < 1, opacity
        moving.wait_for_timeout(1000)
        expect(moving.locator('.coordinates')).to_have_css('opacity', '0')
        assert not moving_errors, moving_errors
        checks.append('real fade transition; reduced-motion immediate dismissal')
        animated.close()
        browser.close()
    (OUTPUT / 'checks.json').write_text(json.dumps({'checks': checks, 'data': 'synthetic, local only', 'renderer': renderer, 'mode': 'offline DOM with fetch fixtures; no HTTP sandbox validation' if OFFLINE_DOM else 'local HTTP fixture and response CSP'}, indent=2))
    print('PASSED:', '; '.join(checks))


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    SERVICE = f'http://127.0.0.1:{server.server_port}'
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        run()
    finally:
        server.shutdown()
