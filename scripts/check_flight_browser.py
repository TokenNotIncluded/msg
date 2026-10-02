"""Actual-app Inspect regression plus offline renderer integration fixtures.

Run: python scripts/check_flight_browser.py
Requires Playwright and Chromium. MSG_BROWSER_PATH overrides the browser path.
Only a disposable loopback HTTP fixture is used. No external requests, signing
key, real account, WebSocket game session or database is used.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'src/msg/data'
ARTIFACTS = ROOT / 'artifacts/flight'
FIXTURE = r"""
(() => {
  const M = globalThis.MSGUniverse;
  const now = Date.parse('2026-10-01T21:52:15Z');
  const star = (id, name, certified = false) => ({ id, name, kind: 'user', position: M.position(id), star: {
    checked_at: new Date(now).toISOString(),
    certificate: { state: certified ? 'valid' : 'none', expires_at: new Date(now + 3600000).toISOString() },
    presence: { state: 'available', self_reported: true, updated_at: new Date(now - 1000).toISOString(), expires_at: new Date(now + 3600000).toISOString() },
    balance: { visibility: 'public', amount_minor: '12345', scale: 2, code: 'USD' }
  }});
  const nodes = [star('u_root', 'root', true), ...Array.from({length: 45}, (_, i) => star('u_fixture_' + i, 'fixture-' + i, i % 3 === 0))];
  const renderer = new MSGUniverseRenderer(document.getElementById('space'), document.getElementById('labels'), {
    now: () => now,
    select: node => { window.__selected = node.id; renderer.focus(node); },
    overview: () => { renderer.setFocus(null); },
    fallback: reason => { window.__fallback = reason; }
  });
  renderer.setGraph({ nodes, links: [] });
  window.__renderer = renderer;
  const byId = id => document.getElementById(id);
  const list = (container, matches) => {
    container.replaceChildren();
    for (const node of matches) {
      const button = document.createElement('button'); button.className = 'catalog-item';
      button.textContent = '@' + node.name + ' — synthetic fixture';
      button.onclick = () => { renderer.focus(node); byId('catalog').hidden = true; byId('search-box').hidden = true; };
      container.append(button);
    }
  };
  byId('catalog-toggle').onclick = () => {
    list(byId('catalog-items'), nodes); byId('catalog').hidden = false; byId('catalog').focus();
  };
  byId('catalog-close').onclick = () => { byId('catalog').hidden = true; };
  byId('search-toggle').onclick = () => { byId('search-box').hidden = false; byId('search').focus(); };
  byId('search-close').onclick = () => { byId('search-box').hidden = true; };
  byId('search').oninput = () => list(byId('search-results'), nodes.filter(n => n.name.includes(byId('search').value)));
  byId('drift').onclick = () => renderer.focus(nodes[1 + Math.floor(Math.random() * (nodes.length - 1))]);
  for (const id of ['private-tab', 'compose-open', 'more', 'shuffle', 'retry']) {
    byId(id).disabled = true; byId(id).title = 'Offline renderer fixture only; no account or server';
  }
  byId('account-link').removeAttribute('href'); byId('account-link').textContent = 'Offline demo';
  document.getElementById('home').onclick = () => renderer.home();
  document.getElementById('origin').onclick = () => renderer.focus(nodes[0]);
  document.getElementById('pause').onclick = () => renderer.pause(!renderer.paused);
  document.getElementById('help-toggle').onclick = () => document.getElementById('help-dialog').showModal();
  document.getElementById('help-close').onclick = () => document.getElementById('help-dialog').close();
  document.getElementById('counts').textContent = '46 SYNTHETIC FIXTURE IDENTITIES';
  document.getElementById('scope-label').textContent = 'OFFLINE RENDERER FIXTURE';
  document.getElementById('status').textContent = '';
  for (const event of ['pointerdown', 'keydown']) document.addEventListener(event, () => document.body.classList.add('exploring'), {once:true});
  const badge = document.createElement('div');
  badge.textContent = 'RENDERER FIXTURE / SYNTHETIC DATA / NO NETWORK';
  badge.style.cssText = 'position:fixed;right:12px;bottom:5px;color:#777;font:8px monospace;z-index:12;pointer-events:none';
  document.body.append(badge);
})();
"""


def assemble() -> str:
    page = (DATA / 'root-web.html').read_text()
    css = (DATA / 'root-web.css').read_text()
    # Font binaries are unnecessary for renderer contracts; use the CSS fallbacks.
    css = re.sub(r'@font-face\s*\{[^}]+\}', '', css)
    code = (
        '\n'.join(
            (DATA / name).read_text() for name in ('root-web-model.js', 'root-web-renderer.js')
        )
        + FIXTURE
    )
    pin = base64.b64encode(hashlib.sha256(code.encode()).digest()).decode()
    policy = f"default-src 'none'; script-src 'sha256-{pin}'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'"
    page = page.replace('__UNIVERSE_STYLE__', css).replace('__UNIVERSE_SCRIPT__', code)
    page = page.replace('__LOGO__', "<span aria-hidden='true'>✦</span>")
    return page.replace(
        '<head>', f'<head><meta http-equiv="Content-Security-Policy" content="{policy}">'
    )


def check_actual_app_inspect(browser) -> None:
    """Use the real app callback and native pointer focus/click ordering."""
    from msg.bootstrap import ROOT_WEB_SAMPLE
    from msg.core.codec import digest
    from msg.extensions.hosting import hosted_headers

    root = {'id': 'u_root', 'name': 'root', 'path': '/@root'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == '/':
                body = ROOT_WEB_SAMPLE
                headers = hosted_headers('w_root_web', 'index.html', digest(body))
                content_type = 'text/html'
            else:
                kind = parse_qs(parsed.query).get('kind', ['users'])[0]
                body = json.dumps({
                    'version': 1,
                    'kind': kind,
                    'items': [root] if kind == 'users' else [],
                    'cursor': None,
                    'service': {'url': f'http://127.0.0.1:{self.server.server_port}'},
                }).encode()
                headers = {}
                content_type = 'application/json'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for viewport in ({'width': 1280, 'height': 800}, {'width': 390, 'height': 844}):
            context = browser.new_context(viewport=viewport)
            # Expose the actual renderer instance, retaining all real callbacks.
            context.add_init_script("""Object.defineProperty(window, 'MSGUniverseRenderer', {
              configurable: true, set(Renderer) {
                Object.defineProperty(window, 'MSGUniverseRenderer', {configurable:true,
                  value:class extends Renderer {constructor(...args) {super(...args); window.__renderer=this;}}
                });
              }
            });""")
            page = context.new_page()
            page.goto(f'http://127.0.0.1:{server.server_port}/')
            page.wait_for_function("window.__renderer?.graph.nodes.some(n => n.id === 'u_root')")
            page.locator('#pilot-toggle').click()
            page.evaluate("""() => {
              __renderer.flight.position = [0, 0, 16]; __renderer.flight.yaw = 0;
              __renderer.flight.pitch = 0; __renderer.updateFlight(0); __renderer.wake();
            }""")
            page.wait_for_function("!document.getElementById('pilot-inspect').disabled")
            page.locator('#pilot-inspect').click()
            page.wait_for_function("!document.getElementById('inspector').hidden")
            assert page.locator('#detail-title').inner_text() == '@root'
            assert page.evaluate('__renderer.flight === null')
            assert page.locator('#detail-body .identity-facts').count() == 1
            context.close()
    finally:
        server.shutdown()
        server.server_close()


def run() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    checks = []
    with sync_playwright() as pw:
        options = {'headless': True}
        executable = os.environ.get('MSG_BROWSER_PATH')
        if executable:
            options['executable_path'] = executable
        browser = pw.chromium.launch(**options)
        check_actual_app_inspect(browser)
        for label, width, height, reduced, software in (
            ('desktop', 1440, 900, False, False),
            ('mobile', 390, 844, False, False),
            ('reduced', 1280, 800, True, False),
            ('mobile-reduced', 390, 844, True, False),
            ('mobile-landscape', 844, 390, False, False),
            ('canvas', 1280, 800, False, True),
        ):
            context = browser.new_context(
                viewport={'width': width, 'height': height},
                has_touch=label.startswith('mobile'),
                is_mobile=label.startswith('mobile'),
                reduced_motion='reduce' if reduced else 'no-preference',
            )
            print(f'Checking {label}', flush=True)
            page = context.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on('pageerror', lambda error, found=errors: found.append(str(error)))
            if software:
                page.evaluate("""() => { const get = HTMLCanvasElement.prototype.getContext;
                    HTMLCanvasElement.prototype.getContext = function(kind, ...args) {
                      return kind.startsWith('webgl') ? null : get.call(this, kind, ...args);
                    }; }""")
            page.set_content(assemble(), wait_until='load')
            page.wait_for_function('window.__renderer?.available')
            renderer_kind = page.locator('#space').get_attribute('data-renderer')
            page.wait_for_timeout(150)
            page.screenshot(path=str(ARTIFACTS / f'{label}-overview.png'))
            page.locator('#pilot-toggle').click()
            assert page.locator('#pilot-toggle').get_attribute('aria-pressed') == 'true'
            start = page.evaluate('__renderer.flight.position.slice()')
            page.keyboard.down('w')
            page.wait_for_timeout(650)
            end = page.evaluate('__renderer.flight.position.slice()')
            assert sum((a - b) ** 2 for a, b in zip(start, end, strict=True)) > 0.05
            assert page.evaluate('__renderer.flight.speed') > 0
            page.keyboard.up('w')
            page.screenshot(path=str(ARTIFACTS / f'{label}-flight.png'))
            page.keyboard.down(' ')
            page.wait_for_timeout(1000)
            page.keyboard.up(' ')
            assert page.evaluate('__renderer.flight.speed') < 0.01
            # An input losing canvas focus must halt keys and inertia, not keep thrusting.
            page.keyboard.down('w')
            page.wait_for_timeout(120)
            page.evaluate("document.getElementById('search-box').hidden = false")
            page.locator('#search').focus()
            page.keyboard.up('w')
            assert page.evaluate('__renderer.flight.speed') == 0
            assert page.evaluate('__renderer.keys.size') == 0
            page.evaluate("document.getElementById('search-box').hidden = true")
            page.locator('#space').focus()
            page.keyboard.press('Escape')
            assert page.evaluate('__renderer.flight === null')
            assert page.locator('#pilot-hud').is_hidden()
            # Lifecycle handlers are exercised with synthetic browser events.
            # This is not a real OS backgrounding / GPU context restoration test.
            page.locator('#pilot-toggle').click()
            for event in ('blur', 'pagehide', 'visibilitychange'):
                page.locator('#space').focus()
                page.keyboard.down('w')
                page.wait_for_timeout(100)
                page.evaluate(
                    """event => {
                    (event === 'visibilitychange' ? document : window).dispatchEvent(new Event(event));
                }""",
                    event,
                )
                page.keyboard.up('w')
                assert page.evaluate('__renderer.flight.speed') == 0
                assert page.evaluate('__renderer.keys.size') == 0
                assert page.evaluate('__renderer.flightControls.size') == 0
            page.evaluate('__renderer.setPilot(false)')
            # Nearby inspection resolves only a current graph identity, through the read callback.
            page.locator('#pilot-toggle').click()
            page.evaluate("""() => {
                __renderer.flight.position = [0, 0, 16]; __renderer.flight.yaw = 0; __renderer.flight.pitch = 0;
                __renderer.updateFlight(0); __renderer.wake();
            }""")
            page.wait_for_timeout(150)
            assert page.locator('#pilot-inspect').is_enabled()
            page.keyboard.press('Enter')
            assert page.evaluate('__selected') == 'u_root'
            assert page.evaluate('__renderer.flight === null')
            page.wait_for_timeout(700 if not reduced else 30)
            page.screenshot(path=str(ARTIFACTS / f'{label}-root.png'))
            # Losing the graph cannot retain a readable private/nearby label.
            page.locator('#pilot-toggle').click()
            page.evaluate('__renderer.setGraph({nodes: [], links: []})')
            assert page.locator('#pilot-inspect').is_disabled()
            assert page.locator('#pilot-inspect').inner_text() == 'Approach a star'
            page.locator('#space').focus()
            page.keyboard.press('h')
            assert page.evaluate('__renderer.flight === null')
            assert page.evaluate('__renderer.destination?.target || __renderer.camera.target') == [
                0,
                0,
                0,
            ]
            if reduced:
                page.locator('#pilot-toggle').click()
                page.keyboard.down('w')
                page.keyboard.down('Shift')
                page.keyboard.down('ArrowRight')
                page.wait_for_timeout(200)
                assert page.evaluate('__renderer.flight.bank') == 0
                assert page.evaluate('__renderer.flight.trail.length') == 0
                assert page.evaluate('__renderer.flight.boost') is False
                page.keyboard.up('w')
                page.keyboard.up('Shift')
                page.keyboard.up('ArrowRight')
            if label.startswith('mobile'):
                page.evaluate('__renderer.setPilot(true); __renderer.stopFlightInput()')
                cdp = context.new_cdp_session(page)
                forward = page.locator('[data-flight-key="w"]').bounding_box()
                right = page.locator('[data-flight-key="arrowright"]').bounding_box()
                points = [
                    {
                        'x': box['x'] + box['width'] / 2,
                        'y': box['y'] + box['height'] / 2,
                        'id': i + 1,
                        'radiusX': 3,
                        'radiusY': 3,
                    }
                    for i, box in enumerate((forward, right))
                ]
                yaw = page.evaluate('__renderer.flight.yaw')
                cdp.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': points})
                page.wait_for_timeout(300)
                assert page.evaluate('__renderer.flightControls.size') == 2
                assert page.evaluate('__renderer.flight.speed') > 0
                assert page.evaluate('__renderer.flight.yaw') != yaw
                cdp.send(
                    'Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': points[1:]}
                )
                assert page.evaluate('[...__renderer.flightControls.values()]') == ['w']
                cdp.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
                assert page.evaluate('__renderer.flightControls.size') == 0
                assert page.locator('#catalog-toggle').is_visible()
                assert page.locator('#compose-open').is_visible()
                for selector in (
                    '#pilot-toggle',
                    '#catalog-toggle',
                    '#compose-open',
                    '[data-flight-key="w"]',
                    '[data-flight-key="arrowleft"]',
                ):
                    box = page.locator(selector).bounding_box()
                    assert box and box['x'] >= 0 and box['x'] + box['width'] <= width + 1
                    assert box['y'] >= 0 and box['y'] + box['height'] <= height + 1
            assert not errors, errors
            checks.append({
                'case': label,
                'renderer': renderer_kind,
                'passed': True,
                'scope': 'actual model/renderer/HTML/CSS; synthetic callback host; no HTTP/backend',
            })
            context.close()
        browser.close()
    (ARTIFACTS / 'results.json').write_text(json.dumps(checks, indent=2) + '\n')
    print(json.dumps(checks, indent=2))


if __name__ == '__main__':
    run()
