"""Exercise the real home renderer and CSP with isolated, synthetic account data."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from check_home_first_read_browser import DATA, POSTS
from playwright.sync_api import expect, sync_playwright

from msg.plugins.public_board import default
from msg.transports.home_page import HOME_BROWSER_HEADERS, home_html

OUTPUT = Path('artifacts/ascii-home')
ACCOUNT = {'name': '@fixture', 'groups': [{'name': '&public', 'path': '/&public'}]}
CUSTOM = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 300"><text x="40" y="150">Shared &amp; public</text></svg>'


def render(path):
    board = default()
    account = None if path == '/guest' else ACCOUNT
    if path == '/shared':
        board.update(svg=CUSTOM, generation=1)
    if path == '/long':
        account = {**ACCOUNT, 'name': '@' + 'long-handle-' * 18}
        board['text'] = '<script>notExecutable()</script> ' + '长标题 Long text ' * 80
    return home_html(
        DATA,
        account=account,
        login_enabled=True,
        public_board=board,
        post_index={'items': POSTS},
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/favicon.png':
            self.send_response(204)
            self.end_headers()
            return
        image = path == '/_public-board/art.svg'
        svg = CUSTOM if 'v=1' in self.path else default()['svg']
        payload = svg.encode() if image else render(path)
        self.send_response(200)
        self.send_header('Content-Type', 'image/svg+xml' if image else 'text/html; charset=utf-8')
        for key, value in HOME_BROWSER_HEADERS.items():
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def ink(page):
    return page.locator('[data-ascii-canvas]').evaluate('(canvas) => canvas.toDataURL()')


def settled(page):
    page.wait_for_timeout(250)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not page.evaluate('window.cspErrors'), page.evaluate('window.cspErrors')


def check_layout(page):
    settled(page)
    buttons = page.locator('[data-ascii-piece]')
    boxes = [button.bounding_box() for button in buttons.all()]
    assert max(box['y'] for box in boxes) - min(box['y'] for box in boxes) <= 1
    for box in boxes:
        assert box['height'] >= 44 and box['width'] >= 44, box
    assert page.locator('[data-ascii-piece][aria-pressed=true]').count() == 1
    canvas_color = page.locator('[data-ascii-canvas]').evaluate('el => getComputedStyle(el).color')
    assert canvas_color == page.locator('body').evaluate('el => getComputedStyle(el).color')
    assert (
        page.locator('.latest').bounding_box()['y'] < page.locator('.activity').bounding_box()['y']
    )


def check_scenes(page, *, touch=False):
    controls = page.locator('[data-ascii-controls]')
    before = controls.bounding_box()
    for name in ('flow', 'aurora', 'community', 'galaxy'):
        button = page.locator(f'[data-ascii-piece={name}]')
        if touch:
            button.tap()
        else:
            button.focus()
            page.keyboard.press('Enter')
        expect(button).to_have_attribute('aria-pressed', 'true')
        settled(page)
        after = controls.bounding_box()
        assert abs(before['y'] - after['y']) < 1, (before, after)
        if name == 'community':
            expect(page.locator('.signal-viewport img')).to_be_visible()
            expect(page.locator('[data-ascii-canvas]')).to_be_hidden()
        else:
            expect(page.locator('[data-ascii-canvas]')).to_be_visible()
    check_layout(page)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    # Export the actual rendered fixtures, not reconstructed mockups.
    for path in ('/', '/guest', '/shared', '/long'):
        (OUTPUT / f'{path[1:] or "home"}.html').write_bytes(render(path))
    (OUTPUT / 'art.svg').write_text(default()['svg'])
    (OUTPUT / 'headers.json').write_text(json.dumps(HOME_BROWSER_HEADERS, indent=2) + '\n')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    results = []
    try:
        with sync_playwright() as tool:
            options = (
                {'executable_path': os.environ['MSG_BROWSER_EXECUTABLE']}
                if os.environ.get('MSG_BROWSER_EXECUTABLE')
                else {}
            )
            browser = tool.chromium.launch(**options)
            for width in (320, 360, 390, 768, 1440):
                for theme in ('light', 'dark'):
                    context = browser.new_context(
                        viewport={'width': width, 'height': 960},
                        color_scheme=theme,
                        locale='zh-CN',
                        reduced_motion='reduce',
                        has_touch=width < 768,
                    )
                    page = context.new_page()
                    errors, external = [], []
                    page.on('pageerror', lambda error, errors=errors: errors.append(str(error)))
                    page.on(
                        'request',
                        lambda request, external=external: (
                            external.append(request.url)
                            if not request.url.startswith(origin + '/')
                            else None
                        ),
                    )
                    page.add_init_script(
                        "window.cspErrors=[];document.addEventListener('securitypolicyviolation',"
                        'e=>cspErrors.push(e.violatedDirective));'
                    )
                    page.goto(origin, wait_until='networkidle')
                    check_layout(page)
                    page.screenshot(path=str(OUTPUT / f'home-{width}-{theme}.png'), full_page=True)
                    check_scenes(page)
                    if width < 768:
                        check_scenes(page, touch=True)
                    # A still frame changes ink with the theme, but never resumes motion.
                    before = ink(page)
                    page.emulate_media(color_scheme='dark' if theme == 'light' else 'light')
                    settled(page)
                    assert ink(page) != before
                    before = ink(page)
                    page.wait_for_timeout(350)
                    assert ink(page) == before
                    page.evaluate("document.documentElement.dataset.theme='" + theme + "'")
                    settled(page)
                    assert ink(page) != before
                    # Menus, editor access and local drafts survive the new layout.
                    page.locator('.public-board-menu > summary').click()
                    page.locator('[data-edit]').click()
                    expect(page.locator('#public-board-editor')).to_be_visible()
                    page.locator('[name=text]').fill('Unsaved local fixture')
                    page.locator('[data-close]').click()
                    page.locator('.public-board-menu > summary').click()
                    page.keyboard.press('Escape')
                    expect(page.locator('.public-board-menu')).not_to_have_attribute('open', '')
                    page.locator('.public-board-menu > summary').click()
                    page.locator('[data-edit]').click()
                    expect(page.locator('[name=text]')).to_have_value('Unsaved local fixture')
                    page.locator('[name=text]').fill(default()['text'])
                    page.locator('[data-close]').click()
                    page.goto(origin + '/shared', wait_until='networkidle')
                    expect(page.locator('[data-ascii-piece=community]')).to_have_attribute(
                        'aria-pressed', 'true'
                    )
                    expect(page.locator('.signal-viewport img')).to_be_visible()
                    page.goto(origin + '/long', wait_until='networkidle')
                    settled(page)
                    expect(page.locator('[data-content]')).to_contain_text(
                        '<script>notExecutable()</script>'
                    )
                    page.goto(origin + '/guest', wait_until='networkidle')
                    settled(page)
                    expect(page.locator('.account .primary')).to_have_attribute('href', '/login')
                    assert not errors and not external, (errors, external)
                    results.append({'width': width, 'theme': theme, 'passed': True})
                    context.close()
            context = browser.new_context(reduced_motion='no-preference')
            page = context.new_page()
            page.goto(origin, wait_until='networkidle')
            before = ink(page)
            page.wait_for_timeout(400)
            assert ink(page) != before
            page.locator('.public-board-menu > summary').click()
            page.locator('[data-pause]').click()
            page.wait_for_timeout(200)
            before = ink(page)
            page.wait_for_timeout(400)
            assert ink(page) == before
            page.evaluate("document.documentElement.dataset.theme='dark'")
            page.wait_for_timeout(250)
            assert ink(page) != before
            before = ink(page)
            page.wait_for_timeout(400)
            assert ink(page) == before
            context.close()
            context = browser.new_context(
                java_script_enabled=False, viewport={'width': 320, 'height': 960}
            )
            page = context.new_page()
            page.goto(origin + '/guest')
            expect(page.locator('.signal-viewport img')).to_be_visible()
            expect(page.locator('[data-ascii-controls]')).to_be_hidden()
            expect(page.locator('.post-title a').first).to_have_text(POSTS[0]['title'])
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    (OUTPUT / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    print(f'{len(results)} layout/theme cases, animation/pause and no-JavaScript fallback passed.')


if __name__ == '__main__':
    main()
