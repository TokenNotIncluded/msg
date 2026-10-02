"""One batched browser acceptance under MSG's real opaque hosted-page CSP."""

import asyncio
import base64
import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).parent
CAPTURE = '--no-capture' not in sys.argv
CSP = "sandbox; default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/@lightjunction/web/':
            self.send_error(404)
            return
        body = ROOT.joinpath('index.html').read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


async def capture(page, name):
    """Native capture avoids injecting a screenshot helper into the opaque page."""
    if not CAPTURE:
        return
    session = await page.context.new_cdp_session(page)
    try:
        result = await session.send(
            'Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': True}
        )
        ROOT.joinpath('.impeccable/review', name).write_bytes(base64.b64decode(result['data']))
    finally:
        await session.detach()


async def check_playback_controls(page):
    frame = "[...document.querySelectorAll('.frame')].findIndex(p=>getComputedStyle(p).visibility==='visible')"
    before = await page.evaluate(frame)
    await page.wait_for_timeout(650)
    moving = await page.evaluate(frame)
    assert moving != before, (before, moving)
    await page.locator('#pause').check()
    paused_state = await settled_pause(page)
    paused = paused_state['frame']
    await page.wait_for_timeout(650)
    stable_state = await page.evaluate(PAUSE_STATE)
    stable = stable_state['frame']
    assert stable_state == paused_state
    assert await page.evaluate(
        "[...document.querySelectorAll('.frame')].every(p=>getComputedStyle(p).animationPlayState==='paused')"
    )
    await page.locator('#pause').uncheck()
    await page.wait_for_timeout(650)
    resumed = await page.evaluate(frame)
    assert resumed != paused
    await page.locator('#pause').focus()
    await page.keyboard.press('Space')
    assert await page.locator('#pause').is_checked()
    keyboard_state = await settled_pause(page)
    keyboard_paused = keyboard_state['frame']
    await page.wait_for_timeout(650)
    assert await page.evaluate(PAUSE_STATE) == keyboard_state
    return {
        'advanced': [before, moving],
        'paused': [paused, stable],
        'resumed': resumed,
        'space_paused': keyboard_paused,
        'paused_time': paused_state['time'],
        'space_paused_time': keyboard_state['time'],
    }


PAUSE_STATE = """() => {
  const frames=[...document.querySelectorAll('.frame')];
  const animations=frames.flatMap(p=>p.getAnimations());
  return {frame:frames.findIndex(p=>getComputedStyle(p).visibility==='visible'),
    paused:animations.length===64 && animations.every(a=>a.playState==='paused'),
    pending:animations.some(a=>a.pending),time:animations[0]?.currentTime};
}"""


async def settled_pause(page):
    """CSS pause commits on a render tick, after the native checkbox has changed.

    Poll from Python: sandboxed pages cannot execute asynchronous rAF callbacks.
    Keep the later 650ms freeze assertion; settling does not replace verification.
    """
    previous = None
    for _ in range(20):
        state = await page.evaluate(PAUSE_STATE)
        if state['paused'] and not state['pending'] and state == previous:
            return state
        previous = state
        await page.wait_for_timeout(50)
    raise AssertionError(f'CSS animation did not settle paused: {previous}')


async def inspect(browser, origin, name, viewport, reduced=False):
    context = await browser.new_context(
        viewport=viewport, reduced_motion='reduce' if reduced else 'no-preference'
    )
    page = await context.new_page()
    errors, requests = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
    page.on('request', lambda r: requests.append(r.url))
    response = await page.goto(origin + '/@lightjunction/web/', wait_until='load')
    assert response.headers['content-security-policy'] == CSP
    await page.wait_for_timeout(350)
    await capture(page, f'{name}-chair.png')
    measured = await page.evaluate("""() => ({width:innerWidth,scroll:document.documentElement.scrollWidth,
      frames:document.querySelectorAll('.frame').length, scripts:document.scripts.length,
      outside:[...document.querySelectorAll('pre')].filter(p=>p.getBoundingClientRect().right>innerWidth+1).length,
      visible:[...document.querySelectorAll('.frame')].filter(p=>getComputedStyle(p).visibility==='visible').length})""")
    assert measured['scroll'] <= measured['width'], measured
    assert measured['outside'] == 0, measured
    assert measured['scripts'] == 0 and measured['frames'] == 64
    assert measured['visible'] == (0 if reduced else 1), measured
    await page.wait_for_timeout(8900)
    visible_later = await page.evaluate(
        "[...document.querySelectorAll('.frame')].filter(p=>getComputedStyle(p).visibility==='visible').length"
    )
    assert visible_later == (0 if reduced else 1), visible_later
    await capture(page, f'{name}-{"static" if reduced else "nuclear"}.png')
    # These same controls must work after reduced-motion users explicitly opt in.
    if not reduced:
        controls = await check_playback_controls(page)
        assert await page.get_by_role('link', name='重播').get_attribute('href') == './'
    else:
        assert (
            await page.evaluate("getComputedStyle(document.querySelector('.still')).visibility")
            == 'visible'
        )
        await page.locator('#motion').check()
        await page.wait_for_timeout(500)
        await capture(page, f'{name}-opt-in.png')
        visible = await page.evaluate(
            "[...document.querySelectorAll('.frame')].filter(p=>getComputedStyle(p).visibility==='visible').length"
        )
        assert visible == 1
        controls = await check_playback_controls(page)
    assert not errors, errors
    assert requests == [origin + '/@lightjunction/web/'], requests
    await context.close()
    return {
        'viewport': viewport,
        'reduced': reduced,
        'geometry': measured,
        'errors': errors,
        'requests': requests,
        'controls': controls,
    }


async def main():
    ROOT.joinpath('.impeccable/review').mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
            results = await asyncio.gather(
                inspect(browser, origin, 'desktop', {'width': 1440, 'height': 1000}),
                inspect(browser, origin, 'mobile', {'width': 390, 'height': 844}),
                inspect(browser, origin, 'reduced', {'width': 390, 'height': 844}, True),
            )
            await browser.close()
        report = {
            'sha256': hashlib.sha256(ROOT.joinpath('index.html').read_bytes()).hexdigest(),
            'csp': CSP,
            'cases': results,
        }
        ROOT.joinpath('.impeccable/review/browser.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    asyncio.run(main())
