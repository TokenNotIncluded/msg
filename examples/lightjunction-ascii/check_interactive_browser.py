"""Real browser choices and lifecycle checks under an opaque pinned-script CSP."""

import argparse
import asyncio
import base64
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).parent
BODY = ROOT.joinpath('index.html').read_bytes()
SCRIPT = re.search(rb'<script>(.*?)</script>', BODY, re.S).group(1)
PIN = base64.b64encode(hashlib.sha256(SCRIPT).digest()).decode()
CSP = f"sandbox allow-scripts allow-pointer-lock; default-src 'none'; script-src 'sha256-{PIN}'; script-src-attr 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/@lightjunction/web/':
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('Content-Length', str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *_):
        pass


async def phase(page, value, timeout=60000):
    await page.wait_for_function(
        '(v)=>document.querySelector("#world").dataset.phase===v', arg=value, timeout=timeout
    )


async def move_until(page, kind, key, predicate):
    if kind == 'mobile':
        rect = await page.locator(f'[data-move="{key}"]').bounding_box()
        await page.mouse.move(rect['x'] + rect['width'] / 2, rect['y'] + rect['height'] / 2)
        await page.mouse.down()
    else:
        await page.keyboard.down(key)
    try:
        await page.wait_for_function(predicate, timeout=30000)
    finally:
        if kind == 'mobile':
            await page.mouse.up()
        else:
            await page.keyboard.up(key)
        await page.wait_for_timeout(200)


async def inspect(browser, origin, kind, branch):
    viewport = (
        {'width': 390, 'height': 844} if kind == 'mobile' else {'width': 1440, 'height': 1000}
    )
    ctx = await browser.new_context(viewport=viewport, reduced_motion='no-preference')
    page = await ctx.new_page()
    errors, requests = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
    page.on('request', lambda r: requests.append(r.url))
    response = await page.goto(origin + '/@lightjunction/web/')
    assert response.headers['content-security-policy'] == CSP
    assert hashlib.sha256(await response.body()).digest() == hashlib.sha256(BODY).digest()
    await page.wait_for_timeout(400)
    if branch == 'watch':
        await page.screenshot(
            path=str(ROOT / f'.impeccable/review/{kind}-third.png'), full_page=True
        )
    initial = await page.locator('#world').inner_text()
    assert len(initial.strip()) > 200 and all(ord(c) < 128 for c in initial)
    assert await page.evaluate('window.origin') == 'null'
    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    await phase(page, 'first-person')
    if branch == 'watch':
        await page.screenshot(
            path=str(ROOT / f'.impeccable/review/{kind}-first.png'), full_page=True
        )
    await phase(page, 'choice')
    await page.wait_for_timeout(650)
    if branch in {'open-room', 'outside-close', 'survive'}:
        await page.locator('#stance').click()
        await move_until(
            page,
            kind,
            's',
            '(v)=>Number(document.querySelector("#world").dataset.position.split(",")[1]) < -4.35',
        )
        assert await page.locator('#door').is_enabled()
        assert float(await page.locator('#world').get_attribute('data-floor')) < -1.8
        await page.locator('#door').click()
        if branch == 'outside-close':
            await page.wait_for_timeout(500)
            await page.locator('#door').click()
            await page.wait_for_function(
                '()=>document.querySelector("#world").dataset.door === "closed"'
            )
            assert await page.locator('#world').get_attribute('data-room') == 'false'
        else:
            await move_until(
                page,
                kind,
                's',
                '(v)=>Number(document.querySelector("#world").dataset.position.split(",")[1]) < -6.0',
            )
            assert await page.locator('#world').get_attribute('data-room') == 'true'
            if branch == 'survive':
                await page.locator('#door').click()
                await page.wait_for_function(
                    '()=>document.querySelector("#world").dataset.door === "closed"'
                )
                await phase(page, 'sheltered', timeout=90000)
                await page.screenshot(
                    path=str(ROOT / f'.impeccable/review/{kind}-sheltered.png'), full_page=True
                )
                await phase(page, 'aftermath', timeout=90000)
                await page.locator('#door').click()
                await move_until(
                    page,
                    kind,
                    'w',
                    '(v)=>Number(document.querySelector("#world").dataset.position.split(",")[1]) > -1.8',
                )
                assert await page.locator('#world').get_attribute('data-room') == 'false'
                assert float(await page.locator('#world').get_attribute('data-floor')) == 0
                assert await page.locator('#coffee').is_disabled()
                assert await page.locator('#stance').is_disabled()
                assert await page.locator('#door').is_disabled()
                await page.screenshot(
                    path=str(ROOT / f'.impeccable/review/{kind}-aftermath.png'), full_page=True
                )
                assert await page.locator('#world').get_attribute('data-phase') == 'aftermath'
                await page.locator('#restart').click()
                await phase(page, 'third-person')
                assert not errors, errors
                assert len(requests) == 1, requests
                await ctx.close()
                return {
                    'device': kind,
                    'branch': branch,
                    'walked_into_room': True,
                    'closed_door_survived': True,
                    'walked_out_after_blast': True,
                    'restarted': True,
                    'errors': errors,
                    'requests': requests,
                }
    await phase(page, 'dead', timeout=70000)
    dead_position = await page.locator('#world').get_attribute('data-position')
    if kind == 'desktop':
        await page.keyboard.up('s')
        await page.keyboard.up('Shift')
    await page.keyboard.down('w')
    await page.wait_for_timeout(300)
    await page.keyboard.up('w')
    assert await page.locator('#world').get_attribute('data-position') == dead_position
    assert (
        await page.locator('#stance').is_disabled() and await page.locator('#coffee').is_disabled()
    )
    death_time = float(await page.locator('#world').get_attribute('data-time'))
    if branch == 'watch':
        await page.screenshot(
            path=str(ROOT / f'.impeccable/review/{kind}-dead.png'), full_page=True
        )
        await page.wait_for_function(
            '()=>document.querySelector("#world").dataset.cycle==="2"', timeout=15000
        )
        await phase(page, 'third-person')
    else:
        await page.locator('#restart').click()
        await phase(page, 'third-person')
    assert await page.locator('#world').get_attribute('data-position') == '0.00,0.00'
    assert not errors, errors
    assert requests == [origin + '/@lightjunction/web/'], requests
    await ctx.close()
    return {
        'device': kind,
        'branch': branch,
        'death_time': death_time,
        'errors': errors,
        'requests': requests,
        'restarted': True,
    }


async def lifecycle(browser, origin):
    ctx = await browser.new_context(viewport={'width': 390, 'height': 844}, reduced_motion='reduce')
    page = await ctx.new_page()
    await page.goto(origin + '/@lightjunction/web/')
    await page.wait_for_timeout(400)
    assert await page.locator('#world').get_attribute('data-phase') == 'waiting'
    assert await page.locator('#world').get_attribute('data-time') == '0.00'
    await page.screenshot(path=str(ROOT / '.impeccable/review/reduced-static.png'), full_page=True)
    await page.locator('#start').click()
    await phase(page, 'choice')
    await page.wait_for_timeout(650)
    await page.locator('#pause').click()
    await page.wait_for_timeout(150)
    before = await page.locator('#world').get_attribute('data-time')
    await page.keyboard.down('w')
    await page.wait_for_timeout(500)
    await page.keyboard.up('w')
    assert await page.locator('#world').get_attribute('data-time') == before
    assert await page.locator('#world').get_attribute('data-position') == '0.00,0.00'
    await page.locator('#pause').click()
    await page.wait_for_timeout(500)
    assert float(await page.locator('#world').get_attribute('data-time')) > float(before)
    rect = await page.locator('#world').bounding_box()
    await page.mouse.move(rect['x'] + rect['width'] / 2, rect['y'] + rect['height'] / 2)
    await page.mouse.down()
    await page.mouse.move(rect['x'] + rect['width'] / 2 + 70, rect['y'] + rect['height'] / 2)
    await page.mouse.up()
    await page.wait_for_timeout(150)
    assert abs(float(await page.locator('#world').get_attribute('data-yaw'))) > 0.2
    await page.keyboard.down('w')
    await page.wait_for_timeout(300)
    await page.evaluate('window.dispatchEvent(new Event("blur"))')
    await page.wait_for_timeout(150)
    position = await page.locator('#world').get_attribute('data-position')
    await page.wait_for_timeout(500)
    assert await page.locator('#world').get_attribute('data-position') == position
    await page.keyboard.up('w')
    await page.locator('#restart').click()
    await phase(page, 'waiting')
    await ctx.close()
    return {
        'reduced_opt_in': True,
        'pause': True,
        'drag': True,
        'blur_releases_keys': True,
        'restart': True,
    }


async def pointer_lock_lifecycle(browser, origin):
    ctx = await browser.new_context(viewport={'width': 1440, 'height': 1000})
    page = await ctx.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    response = await page.goto(origin + '/@lightjunction/web/')
    assert response.headers['content-security-policy'] == CSP
    assert hashlib.sha256(await response.body()).digest() == hashlib.sha256(BODY).digest()
    await phase(page, 'choice')
    await page.wait_for_timeout(300)
    await page.evaluate("""() => {
        const el = document.querySelector('#world');
        const native = el.requestPointerLock.bind(el);
        window.delayedNativeGranted = false;
        window.restoreNativeLock = () => { el.requestPointerLock = native; };
        el.requestPointerLock = () => new Promise((resolve, reject) => setTimeout(() => {
            const pending = native();
            Promise.resolve(pending).then(() => {
                window.delayedNativeGranted = true;
                resolve();
            }, reject);
        }, 300));
    }""")
    rect = await page.locator('#world').bounding_box()
    mouse_x, mouse_y = rect['x'] + 400, rect['y'] + 300
    await page.mouse.click(mouse_x, mouse_y)
    await page.keyboard.press('p')
    await page.keyboard.press('p')
    await page.wait_for_timeout(1000)
    assert await page.evaluate('window.delayedNativeGranted'), (
        'Native delayed grant was not exercised'
    )
    assert await page.evaluate('document.pointerLockElement === null')
    await page.evaluate('window.restoreNativeLock()')
    await page.mouse.click(mouse_x, mouse_y)
    await page.wait_for_function(
        '()=>document.pointerLockElement===document.querySelector("#world")'
    )
    yaw = float(await page.locator('#world').get_attribute('data-yaw'))
    await page.mouse.move(mouse_x + 70, mouse_y)
    await page.wait_for_timeout(150)
    assert float(await page.locator('#world').get_attribute('data-yaw')) > yaw
    await page.keyboard.press('Escape')
    await page.wait_for_function('()=>document.pointerLockElement===null')
    await page.locator('#restart').click()
    await phase(page, 'third-person')
    assert not errors, errors
    await ctx.close()
    return {
        'delayed_native_grant_exercised': True,
        'cancel_then_resume_rejects_stale_lock': True,
        'fresh_lock': True,
        'relative_mouse_right_turns_right': True,
        'escape_unlock': True,
        'reset': True,
        'errors': errors,
    }


async def main(origin_override=None, pointer_lock_only=False):
    ROOT.joinpath('.impeccable/review').mkdir(parents=True, exist_ok=True)
    server = None
    if origin_override:
        origin = origin_override.rstrip('/')
    else:
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        origin = f'http://127.0.0.1:{server.server_port}'
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
            if pointer_lock_only:
                results = [await pointer_lock_lifecycle(browser, origin)]
            else:
                tasks = [
                    inspect(browser, origin, device, branch)
                    for device in ['desktop', 'mobile']
                    for branch in ['watch', 'open-room', 'outside-close', 'survive']
                ]
                results = await asyncio.gather(*tasks, lifecycle(browser, origin))
            await browser.close()
        report = {
            'origin': origin,
            'sha256': hashlib.sha256(BODY).hexdigest(),
            'script_pin': PIN,
            'csp': CSP,
            'cases': results,
        }
        report_name = (
            'pointer-lock-browser.json' if pointer_lock_only else 'interactive-browser.json'
        )
        ROOT.joinpath(f'.impeccable/review/{report_name}').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', help='Verify this deployed origin instead of the local server.')
    parser.add_argument(
        '--pointer-lock-only',
        action='store_true',
        help='Run the bounded native pointer-lock cancellation check.',
    )
    args = parser.parse_args()
    asyncio.run(main(args.origin, args.pointer_lock_only))
