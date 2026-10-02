"""Batched opaque-CSP acceptance, including deliberately streamed HTML parsing."""

import asyncio
import base64
import hashlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).parent
CAPTURE = '--no-capture' not in sys.argv
CSP = "sandbox; default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
FRAME_STATE = """() => {
  const projection=document.querySelector('.projection');
  const frames=[...document.querySelectorAll('.frame')];
  const active=frames.flatMap((p,i)=>{const s=getComputedStyle(p);
    return s.visibility==='visible' && Number(s.opacity)>0.5?[i]:[]});
  const animations=projection.getAnimations();
  return {frame:active.length===1?active[0]:-1,active,
    clock:Number(getComputedStyle(projection).getPropertyValue('--frame')),
    animations:animations.length,paused:animations.length===1 && animations[0].playState==='paused',
    pending:animations.some(a=>a.pending),time:animations[0]?.currentTime??null};
}"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/@lightjunction/web/', '/@lightjunction/web/?stream=1'):
            self.send_error(404)
            return
        body = ROOT.joinpath('index.html').read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.path.endswith('?stream=1'):
            # Multiple render ticks between parser chunks reproduce public clock drift.
            for start in range(0, len(body), 8192):
                self.wfile.write(body[start : start + 8192])
                self.wfile.flush()
                time.sleep(0.07)
        else:
            self.wfile.write(body)

    def log_message(self, *_):
        pass


async def capture(page, name, enabled):
    if not CAPTURE or not enabled:
        return
    session = await page.context.new_cdp_session(page)
    try:
        result = await session.send(
            'Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': True}
        )
        ROOT.joinpath('.impeccable/review', name).write_bytes(base64.b64decode(result['data']))
    finally:
        await session.detach()


def active_frame(state):
    assert state['animations'] == 1, state
    assert len(state['active']) == 1 and 0 <= state['frame'] < 64, state
    assert state['frame'] == state['clock'], state
    return state['frame']


async def settled_pause(page):
    # Native CSS pause commits at a render tick; callbacks cannot run in this sandbox.
    previous = None
    for _ in range(20):
        state = await page.evaluate(FRAME_STATE)
        active_frame(state)
        if state['paused'] and not state['pending'] and state == previous:
            return state
        previous = state
        await page.wait_for_timeout(50)
    raise AssertionError(f'CSS animation did not settle paused: {previous}')


async def check_playback_controls(page):
    before = active_frame(await page.evaluate(FRAME_STATE))
    await page.wait_for_timeout(650)
    moving = active_frame(await page.evaluate(FRAME_STATE))
    assert moving != before
    await page.locator('#pause').check()
    paused = await settled_pause(page)
    await page.wait_for_timeout(650)
    stable = await page.evaluate(FRAME_STATE)
    assert stable == paused
    await page.locator('#pause').uncheck()
    await page.wait_for_timeout(650)
    resumed = active_frame(await page.evaluate(FRAME_STATE))
    assert resumed != paused['frame']
    await page.locator('#pause').focus()
    await page.keyboard.press('Space')
    assert await page.locator('#pause').is_checked()
    keyboard = await settled_pause(page)
    await page.wait_for_timeout(650)
    assert await page.evaluate(FRAME_STATE) == keyboard
    await page.keyboard.press('Space')
    assert not await page.locator('#pause').is_checked()
    return {
        'advanced': [before, moving],
        'paused': [paused['frame'], stable['frame']],
        'resumed': resumed,
        'space_paused': keyboard['frame'],
        'paused_time': paused['time'],
        'space_paused_time': keyboard['time'],
    }


async def continuous_frames(page):
    """Observe every discrete step and an actual loop, including control pauses."""
    samples, seen, previous, wrapped = 0, set(), None, False
    deadline = asyncio.get_running_loop().time() + 28
    while asyncio.get_running_loop().time() < deadline:
        state = await page.evaluate(FRAME_STATE)
        frame = active_frame(state)
        seen.add(frame)
        samples += 1
        wrapped |= previous is not None and frame < previous
        previous = frame
        if len(seen) == 64 and wrapped:
            return {
                'samples': samples,
                'observed_frames': sorted(seen),
                'wrapped': wrapped,
                'one_active_frame': True,
                'single_parent_animation': True,
            }
        await page.wait_for_timeout(70)
    raise AssertionError(f'incomplete real 18s loop: samples={samples}, seen={sorted(seen)}')


async def inspect(
    browser,
    origin,
    name,
    viewport,
    reduced=False,
    *,
    stream=False,
    slow=False,
    capture_enabled=True,
):
    context = await browser.new_context(
        viewport=viewport, reduced_motion='reduce' if reduced else 'no-preference'
    )
    page = await context.new_page()
    errors, requests = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
    page.on('request', lambda r: requests.append(r.url))
    network = None
    if slow:
        network = await context.new_cdp_session(page)
        await network.send('Network.enable')
        await network.send(
            'Network.emulateNetworkConditions',
            {
                'offline': False,
                'latency': 40,
                'downloadThroughput': 32000,
                'uploadThroughput': 32000,
            },
        )
    url = origin + '/@lightjunction/web/' + ('?stream=1' if stream else '')
    response = await page.goto(url, wait_until='load', timeout=45000)
    assert response.headers['content-security-policy'] == CSP
    body_sha = hashlib.sha256(await response.body()).hexdigest()
    continuity = None if reduced else asyncio.create_task(continuous_frames(page))
    await page.wait_for_timeout(350)
    await capture(page, f'{name}-chair.png', capture_enabled)
    measured = await page.evaluate("""() => ({width:innerWidth,scroll:document.documentElement.scrollWidth,
      frames:document.querySelectorAll('.frame').length,scripts:document.scripts.length,
      outside:[...document.querySelectorAll('pre')].filter(p=>p.getBoundingClientRect().right>innerWidth+1).length})""")
    assert measured['scroll'] <= measured['width'] and measured['outside'] == 0, measured
    assert measured['scripts'] == 0 and measured['frames'] == 64
    state = await page.evaluate(FRAME_STATE)
    if reduced:
        assert state['active'] == [] and state['animations'] == 0, state
    else:
        active_frame(state)
    await page.wait_for_timeout(8900)
    await capture(page, f'{name}-{"static" if reduced else "nuclear"}.png', capture_enabled)
    if reduced:
        assert (
            await page.evaluate("getComputedStyle(document.querySelector('.still')).visibility")
            == 'visible'
        )
        await page.locator('#motion').check()
        continuity = asyncio.create_task(continuous_frames(page))
        await page.wait_for_timeout(500)
        await capture(page, f'{name}-opt-in.png', capture_enabled)
    controls = await check_playback_controls(page)
    complete_loop = await continuity
    assert await page.get_by_role('link', name='重播').get_attribute('href') == './'
    assert not errors, errors
    assert requests == [url], requests
    if network:
        await network.detach()
    await context.close()
    return {
        'viewport': viewport,
        'reduced': reduced,
        'stream': stream,
        'slow_network': slow,
        'sha256': body_sha,
        'geometry': measured,
        'errors': errors,
        'requests': requests,
        'controls': controls,
        'continuity': complete_loop,
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
                inspect(
                    browser,
                    origin,
                    'stream',
                    {'width': 390, 'height': 844},
                    stream=True,
                    capture_enabled=False,
                ),
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
