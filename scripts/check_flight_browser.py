"""Verify offline rendering or the complete app against a real disposable world.

Default: python scripts/check_flight_browser.py (synthetic renderer only).
Set MSG_FLIGHT_TEST_URL and MSG_FLIGHT_ACCOUNTS_FILE for real app,
CSP, WebSocket, physics and two generated fixture identities. The URL must be
loopback; no production combat or user credentials are used. Playwright and
Chromium are required; MSG_BROWSER_PATH selects an installed browser.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

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


def check_multiplayer_app(browser, base_url: str, accounts_file: str | None = None) -> list:
    """Real packaged app, real WS/physics and two isolated fixture identities.

    The supplied service must be the disposable loopback fixture. This never
    runs combat against a public deployment or accepts client-reported state.
    """
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    parsed = urlsplit(base_url)
    assert parsed.hostname in {'127.0.0.1', 'localhost', '::1'}, 'Loopback fixture required'
    assert accounts_file, 'Full app checks require two generated loopback OAuth accounts'
    path = Path(accounts_file)
    assert path.stat().st_mode & 0o077 == 0, 'Fixture account file must be private'
    fixture_accounts = json.loads(path.read_text())
    assert fixture_accounts.get('scope') == 'disposable loopback TestRoot only'
    accounts = fixture_accounts['accounts'][:2]
    assert len(accounts) == 2
    contexts, pages, errors, checks = [], [], [], []
    controls = [[], []]
    wire = [[], []]
    collected_events = [{}, {}]
    probe = """window.__flightTransportEvents=[];
    const Socket=window.WebSocket;
    window.WebSocket=class extends Socket {constructor(...args) {super(...args);
      this.addEventListener('close', event => {
        window.__flightTransportEvents.push({code:event.code,reason:event.reason});
        window.__flightTransportEvents.splice(0,window.__flightTransportEvents.length-4);
      });
    }};
    Object.defineProperty(window, 'MSGUniverseRenderer', {
      configurable:true, set(Renderer) {
        Object.defineProperty(window, 'MSGUniverseRenderer', {configurable:true,
          value:class extends Renderer {constructor(...args) {super(...args); window.__renderer=this;}}
        });
      }
    });"""

    def wait(page, expression, **kwargs):
        # Function predicates avoid Playwright's string eval under real CSP.
        if not expression.lstrip().startswith('() =>'):
            expression = '() => (' + expression + ')'
        try:
            return page.wait_for_function(expression, **kwargs)
        except Exception:
            # Bounded diagnostics exclude cookies, resume tickets and account bodies.
            print(
                json.dumps(
                    page.evaluate("""() => ({
                      available:window.__renderer?.available,
                      flight:!!window.__renderer?.flight,
                      state:window.__renderer?.network?.state,
                      connected:window.__renderer?.network?.connected,
                      players:window.__renderer?.network?.snapshot?.players.length,
                      connection:document.getElementById('game-connection')?.textContent,
                      status:document.getElementById('status')?.textContent
                      ,closes:window.__flightTransportEvents
                    })"""),
                    ensure_ascii=False,
                ),
                flush=True,
            )
            print(json.dumps(wire, ensure_ascii=False), flush=True)
            print(json.dumps(errors, ensure_ascii=False), flush=True)
            raise

    try:
        for index, viewport in enumerate((
            {'width': 1280, 'height': 800},
            {'width': 390, 'height': 844},
        )):
            context = browser.new_context(
                viewport=viewport, has_touch=index == 1, is_mobile=index == 1
            )
            contexts.append(context)
            # Generated local OAuth cookies only; do not print or trace them.
            context.add_cookies(accounts[index]['cookies'])
            context.add_init_script(probe)
            page = context.new_page()
            pages.append(page)
            page.set_default_timeout(15000)
            page.on('pageerror', lambda error: errors.append(str(error)))

            def capture_control(message, captured=controls[index]):
                packet = json.loads(message)
                if packet.get('type') == 'input':
                    # Keep only non-secret control fields, never join/resume.
                    captured.append({
                        key: packet[key] for key in ('seq', 'throttle', 'actions', 'brake')
                    })
                    del captured[:-256]

            page.on(
                'websocket', lambda socket, capture=capture_control: socket.on('framesent', capture)
            )

            def capture_server(message, captured=wire[index], pickups=collected_events[index]):
                packet = json.loads(message)
                safe = {key: packet[key] for key in ('type', 'tick', 'code') if key in packet}
                if packet.get('type') == 'hello':
                    safe['self_ack'] = packet.get('self', {}).get('ack_seq')
                if packet.get('type') == 'snapshot':
                    for event in packet.get('events', []):
                        if event.get('type') == 'collect':
                            pickups[event['id']] = {
                                key: event[key]
                                for key in (
                                    'id',
                                    'player_id',
                                    'glyph_ids',
                                    'fuel_added',
                                    'collected',
                                )
                            }
                    safe['ships'] = [
                        {key: ship.get(key) for key in ('ack_seq', 'hp', 'fuel', 'region', 'score')}
                        for ship in packet.get('players', [])
                    ]
                    safe['events'] = [
                        {key: event.get(key) for key in ('id', 'type', 'at_ms')}
                        for event in packet.get('events', [])
                    ]
                captured.append(safe)
                del captured[:-3]

            page.on(
                'websocket',
                lambda socket, capture=capture_server: socket.on('framereceived', capture),
            )
            page.goto(base_url.rstrip('/') + '/@root/web/')
            page.evaluate(
                '(subject) => {window.__peerSubject=subject;}', accounts[1 - index]['subject_id']
            )
            wait(page, 'window.__renderer?.available && __renderer.graph.nodes.length > 0')
            page.locator('#pilot-toggle').click()
            wait(
                page,
                '__renderer.network?.connected && __renderer.flight && __renderer.network.snapshot',
            )
        first, second = pages
        for page in pages:
            wait(
                page,
                '__renderer.network.snapshot.players.some(p => p.subject_id === window.__peerSubject)',
            )
        for index, page in enumerate(pages):
            assert (
                page.evaluate('__renderer.network.self.subject_id') == accounts[index]['subject_id']
            )
            assert page.evaluate("""() => {
                  const r=__renderer, self=r.network.self, node=r.graph.nodes.find(n => n.id===self.subject_id);
                  return !!node && self.home_position && MSGUniverse.starPosition(node)
                    .every((value,i) => Math.abs(value-self.home_position[i])<1e-6);
                }""")
            assert page.evaluate("""() => {
                  const n=__renderer.network, field=n.snapshot.collectibles;
                  return n.limits.fuel_burn_rate===1.5 && field.version===2 && field.radius===8 &&
                    field.anchors.length>1 && n.gravity.wells.length>1;
                }""")
        checks.append(
            'two real identities, WS/CSP spawn, public gravity and shared fuel-highway descriptor'
        )

        # Exercise ordinary controls in the real app; never relocate a server
        # ship or fabricate a snapshot to reach the procedural pickup field.
        for page in pages:
            page.locator('#space').focus()
            page.keyboard.down('b')
            wait(page, 'Math.hypot(...__renderer.network.self.velocity) < .1')
            wait(page, '__renderer.network.self.fuel > 90', timeout=15000)
            page.keyboard.up('b')
        homes = [page.evaluate('__renderer.network.self.position') for page in pages]
        first.locator('#space').focus()
        first.keyboard.down('w')
        wait(first, 'Math.hypot(...__renderer.network.self.velocity) > 18')
        first.keyboard.up('w')
        wait(first, '__renderer.network._input.throttle === 0')
        first.wait_for_timeout(100)
        neutral_seq = controls[0][-1]['seq']
        assert controls[0][-1]['throttle'] == 0
        wait(first, f'__renderer.network.self.ack_seq >= {neutral_seq}')
        released = first.evaluate(
            '({position:__renderer.network.self.position,fuel:__renderer.network.self.fuel})'
        )
        first.wait_for_timeout(650)
        coast = first.evaluate(
            '({position:__renderer.network.self.position,velocity:__renderer.network.self.velocity,fuel:__renderer.network.self.fuel})'
        )
        assert math.dist(released['position'], coast['position']) > 6
        assert 10 < math.hypot(*coast['velocity']) < 20
        assert coast['fuel'] >= released['fuel'] - 0.01
        first.keyboard.down('b')
        wait(first, 'Math.hypot(...__renderer.network.self.velocity) < .1')
        first.keyboard.up('b')
        checks.append(
            'real thrust release coasts without fuel burn and B brakes authoritative velocity'
        )

        def navigate(page, destination):
            # The fixture controller uses normal bounded input frames. Keeping
            # focus off the canvas prevents held UI keys replacing those frames.
            page.locator('#help-toggle').focus()
            page.evaluate('() => __renderer.network.resume()')
            deadline = time.monotonic() + 40
            refuels = 0
            while time.monotonic() < deadline:
                approach = page.evaluate(
                    """target => {
                  const r=__renderer, d=target.map((v,i)=>v-r.network.self.position[i]);
                  const distance=Math.hypot(...d), scale=Math.min(1,distance/20)/Math.max(distance,.001);
                  r.flight.yaw=r.flight.pitch=0;
                  r.network.setInput({throttle:-d[2]*scale,strafe:d[0]*scale,lift:d[1]*scale,
                    yaw:0,pitch:0,brake:false,actions:[]});
                  r.wake(); return {distance,fuel:r.network.self.fuel};
                }""",
                    destination,
                )
                distance = approach['distance']
                if distance < 4:
                    break
                if approach['fuel'] < 2:
                    assert refuels < 3, 'navigation exceeded its bounded fuel budget'
                    page.evaluate(
                        """() => __renderer.network.setInput({throttle:0,strafe:0,lift:0,yaw:0,pitch:0,brake:true,actions:[]})"""
                    )
                    wait(page, '__renderer.network.self.fuel > 40', timeout=12000)
                    refuels += 1
                    deadline += 12
                page.wait_for_timeout(50)
            else:
                raise AssertionError(
                    f'ordinary input did not reach the requested local point: {distance}'
                )
            page.evaluate(
                """() => __renderer.network.setInput({throttle:0,strafe:0,lift:0,yaw:0,pitch:0,brake:true,actions:[]})"""
            )
            wait(page, '__renderer.network._input.brake === true')
            page.wait_for_timeout(100)
            brake_frame = controls[pages.index(page)][-1]
            assert brake_frame['brake']
            wait(
                page,
                f'__renderer.network.self.ack_seq >= {brake_frame["seq"]} && Math.hypot(...__renderer.network.self.velocity) < .1',
            )
            assert math.dist(page.evaluate('__renderer.network.self.position'), destination) < 4.5

        # Keep the waiting peer parked while the other uses ordinary navigation.
        second.locator('#help-toggle').focus()
        second.evaluate("""() => {
          const n=__renderer.network; n.resume();
          n.setInput({throttle:0,strafe:0,lift:0,yaw:0,pitch:0,brake:true,actions:[]});
        }""")
        peer_position = second.evaluate('__renderer.network.self.position')
        target = first.evaluate(
            """peer => {
          const r=__renderer, own=r.network.self.position;
          return Array.from({length:2300},(_,id)=>({id,position:Array.from(r.dust.subarray(id*8,id*8+3))}))
            .filter(glyph=>!r.glyphTaken(glyph.id) && Math.hypot(...glyph.position.map((v,i)=>v-own[i]))>12 && Math.hypot(...glyph.position.map((v,i)=>v-peer[i]))>12)
            .sort((a,b)=> Math.hypot(...a.position.map((v,i)=>v-own[i]))+Math.hypot(...a.position.map((v,i)=>v-peer[i]))
              -Math.hypot(...b.position.map((v,i)=>v-own[i]))-Math.hypot(...b.position.map((v,i)=>v-peer[i])))[0];
        }""",
            peer_position,
        )
        assert target
        ids = [page.evaluate('__renderer.network.self.id') for page in pages]
        navigate(first, target['position'])
        wait(first, f'__renderer.glyphTaken({target["id"]})')
        wait(second, f'__renderer.glyphTaken({target["id"]})')
        first_claims = [
            event for event in collected_events[0].values() if target['id'] in event['glyph_ids']
        ]
        assert len(first_claims) == 1 and first_claims[0]['player_id'] == ids[0]
        navigate(second, target['position'])
        second.wait_for_timeout(400)
        shared_claims = {
            event['id']: event
            for seen in collected_events
            for event in seen.values()
            if target['id'] in event['glyph_ids']
        }
        assert len(shared_claims) == 1
        assert next(iter(shared_claims.values()))['player_id'] == ids[0]
        for page in pages:
            assert page.evaluate(f'__renderer.glyphTaken({target["id"]})')
            assert page.locator('#game-collected').inner_text() == str(
                page.evaluate('__renderer.network.self.collected')
            )
        assert first.evaluate('__renderer.sceneSeed') == second.evaluate('__renderer.sceneSeed')
        checks.append(
            'two real clients share glyph geometry and availability; one glyph credits only the first server claimant'
        )
        for index, page in enumerate(pages):
            page.screenshot(
                path=str(ARTIFACTS / ('pickup-mobile.png' if index else 'pickup-desktop.png'))
            )
            navigate(page, homes[index])
            # Keep subsequent combat checks independent of navigation fuel.
            wait(page, '__renderer.network.self.fuel > 80', timeout=15000)

        # Aim is ordinary orientation input. Positions/HP are never overwritten.
        def aim(page):
            page.locator('#space').focus()
            page.evaluate("""() => {
              const r=__renderer, self=r.network.self;
              const other=r.network.snapshot.players.find(ship => ship.subject_id===window.__peerSubject);
              const d=other.position.map((value,i) => value-self.position[i]);
              r.flight.yaw=-Math.atan2(d[0],-d[2]);
              r.flight.pitch=-Math.atan2(d[1],Math.hypot(d[0],d[2]));
              r.network.resume(); r.sendFlightInput(); r.wake();
            }""")

        aim(first)
        distance = first.evaluate("""() => {const n=__renderer.network, peer=n.snapshot.players.find(p=>p.subject_id===window.__peerSubject);
          return Math.hypot(...peer.position.map((v,i)=>v-n.self.position[i]));} """)
        if distance > 42:
            first.keyboard.down('w')
            wait(
                first,
                """() => {const n=__renderer.network, peer=n.snapshot.players.find(p=>p.subject_id===window.__peerSubject);
              return Math.hypot(...peer.position.map((v,i)=>v-n.self.position[i])) < 38;}""",
                timeout=8000,
            )
            first.keyboard.up('w')
            first.keyboard.down('b')
            wait(first, 'Math.hypot(...__renderer.network.self.velocity) < .1')
            first.keyboard.up('b')
        aim(first)
        first.keyboard.down('b')
        wait(first, '__renderer.network.self.laser_ready_ms <= __renderer.network.serverNow')
        first.evaluate("""() => {
          document.getElementById('space').addEventListener('keydown', event => {
            if (event.key !== ' ' || event.repeat) return;
            const r=__renderer;
            window.__fireIntent={visible:r.localShot?.intent===true,
              fuel:r.network.self.fuel, hp:r.network.self.hp};
          });
        }""")
        first.keyboard.down(' ')
        assert first.evaluate('window.__fireIntent?.visible === true')
        wait(first, '__renderer.shotEvents.size > 0')
        assert first.evaluate('__renderer.geometry().triangles.length > 0')
        first.screenshot(path=str(ARTIFACTS / 'fire-desktop.png'))
        wait(
            first,
            '__renderer.network.snapshot.players.some(p => p.subject_id === window.__peerSubject && p.hp < 100)',
        )
        first.keyboard.up(' ')
        wait(second, '__renderer.network.self.hp < 100')
        wait(first, "document.getElementById('game-combat').dataset.state === 'hit'")
        wait(second, "document.getElementById('game-combat').dataset.state === 'damaged'")
        assert first.evaluate("""() => {
          const r=__renderer, snapshot=r.network.snapshot;
          const seen=Array.from(r.combatSeen.keys()).join(',');
          const active=Array.from(r.combatEvents.keys()).join(',');
          r.receiveFlightSnapshot(snapshot);
          return seen===Array.from(r.combatSeen.keys()).join(',') &&
            active===Array.from(r.combatEvents.keys()).join(',');
        }""")
        second.screenshot(path=str(ARTIFACTS / 'hit-mobile.png'))
        assert first.locator('#ship-labels .ship-label').count() >= 1
        checks.append(
            'Space shows immediate fire intent, a visible server beam, real hit/damage HUD and deduplicated snapshot effects'
        )
        first.keyboard.down(' ')
        wait(
            first,
            '__renderer.network.snapshot.players.some(p => p.subject_id === window.__peerSubject && p.hp === 0)',
        )
        first.keyboard.up(' ')
        wait(second, '__renderer.network.self.hp === 0')
        wait(second, 'document.getElementById("game-respawn").textContent.includes("重生")')
        wait(
            second,
            '__renderer.network.self.hp === 100 && __renderer.network.self.respawn_at_ms === 0',
        )
        assert first.evaluate('__renderer.network.self.score') >= 1
        checks.append('server death, respawn countdown and automatic recovery preserve the session')
        for action, field in (
            ('shield', 'shield_until_ms'),
            ('dash', 'dash_ready_ms'),
            ('fire', 'laser_ready_ms'),
        ):
            second.locator(f'[data-game-action="{action}"]').click()
            wait(second, f'__renderer.network.self.{field} > __renderer.network.serverNow')
            wait(
                second,
                f'document.querySelector(\'[data-game-action="{action}"] .game-cooldown\').textContent !== "就绪"',
            )
        checks.append('mobile laser, shield and dash buttons use server cooldowns')
        # A keyboard-accessible touch control must resume and release neutral.
        second.locator('[data-flight-key="w"]').focus()
        second.keyboard.down('Enter')
        wait(second, '__renderer.network._input.throttle === 1')
        second.wait_for_timeout(100)
        assert controls[1][-1]['throttle'] == 1
        second.keyboard.up('Enter')
        wait(second, '__renderer.network._input.throttle === 0')
        second.wait_for_timeout(100)
        assert controls[1][-1]['throttle'] == 0
        checks.append('keyboard thrust button resumes and releases neutral input')
        for index, page in enumerate(pages):
            page.evaluate("""() => {const badge=document.createElement('p'); badge.textContent='隔离联机测试 / 真实测试服务器 / 非生产';
              badge.style.cssText='position:fixed;right:12px;bottom:4px;font:10px monospace;color:#aaa;z-index:30;pointer-events:none';document.body.append(badge);} """)
            page.screenshot(
                path=str(ARTIFACTS / ('actual-mobile.png' if index else 'actual-desktop.png'))
            )
        # Disconnect all combat and clear stale peers, then resume the same ship.
        saved = second.evaluate(
            '({id:__renderer.network.self.id,shield:__renderer.network.self.shield_ready_ms})'
        )
        second.locator('#game-region').click()
        wait(second, '!document.getElementById("region-map").hidden')
        contexts[1].set_offline(True)
        wait(second, '!__renderer.network.connected', timeout=10000)
        wait(second, 'document.getElementById("region-map-status").dataset.state === "disconnected"')
        assert second.locator('#region-map-status').is_visible()
        assert '断开' in second.locator('#region-map-status').inner_text()
        assert '实时玩家未连接' in second.locator('#region-map-status').inner_text()
        assert second.locator('#region-map-actions button:not(:disabled)').count() == 0
        second.screenshot(path=str(ARTIFACTS / 'actual-map-disconnected-mobile.png'))
        checks.append('paused map shows lost connection inside the map and disables region requests')
        second.locator('#space').focus()
        before_offline_attack = len(controls[1])
        second.keyboard.press(' ')
        second.wait_for_timeout(100)
        assert not any('laser' in frame['actions'] for frame in controls[1][before_offline_attack:])
        assert second.evaluate('__renderer.remoteShips.size') == 0
        assert second.locator('[data-game-action="fire"]').is_disabled()
        contexts[1].set_offline(False)
        second.locator('#region-map-close').click()
        wait(second, '__renderer.network.connected && __renderer.network.self', timeout=15000)
        assert second.evaluate('__renderer.network.self.id') == saved['id']
        assert second.evaluate('__renderer.network.self.shield_ready_ms') == saved['shield']
        checks.append('disconnect stops combat; reconnect retains server identity and cooldown')
        region = (second.evaluate('__renderer.network.self.region') + 1) % 19
        # Real pointerdown focus used to expand the status label and move this
        # button by 201px on phone, so pointerup never reached its click handler.
        region_button = second.locator('#game-region')
        before_box = region_button.bounding_box()
        second.mouse.move(
            before_box['x'] + before_box['width'] / 2,
            before_box['y'] + before_box['height'] / 2,
        )
        second.mouse.down()
        second.wait_for_timeout(50)
        paused_box = region_button.bounding_box()
        assert abs(paused_box['x'] - before_box['x']) < 0.1
        assert abs(paused_box['y'] - before_box['y']) < 0.1
        second.mouse.up()
        wait(second, '!document.getElementById("region-map").hidden')
        second.locator(f'#region-map-actions button[title="sector-{region:02d}"]').click()
        wait(second, '__renderer.network.self.region === ' + str(region))
        assert second.locator('#region-map').is_hidden()
        checks.append('paused map selection moves only after server accepts the region')
        # Actual app selection, not the old synthetic renderer callback.
        inspect_destination = first.evaluate("""() => {
          const r=__renderer, own=r.network.self.position;
          const node=r.graph.nodes.filter(n=>n.kind==='user').sort((a,b)=>
            Math.hypot(...a.position.map((v,i)=>v-own[i]))-Math.hypot(...b.position.map((v,i)=>v-own[i])))[0];
          return node.position.map((v,i)=>v+(i===1?7:0));
        }""")
        navigate(first, inspect_destination)
        first.locator('#space').focus()
        wait(first, '!document.getElementById("pilot-inspect").disabled')
        inspected_id = first.evaluate('__renderer.nearby.id')
        first.locator('#pilot-inspect').click()
        wait(first, '!document.getElementById("inspector").hidden')
        first.locator('#detail-body .identity-facts').wait_for(state='attached')
        assert first.locator('#detail-body .identity-facts').count() == 1
        assert first.evaluate('__renderer.focusId') == inspected_id
        assert first.evaluate('__renderer.flight === null')
        checks.append('native pointer Inspect opens the actual authorized app inspector')
        assert not errors, errors
        return checks
    finally:
        for context in contexts:
            context.close()


def run() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    checks = []
    with sync_playwright() as pw:
        options = {'headless': True}
        executable = os.environ.get('MSG_BROWSER_PATH')
        if executable:
            options['executable_path'] = executable
        browser = pw.chromium.launch(**options)
        service_url = os.environ.get('MSG_FLIGHT_TEST_URL')
        if service_url:
            checks = check_multiplayer_app(
                browser, service_url, os.environ.get('MSG_FLIGHT_ACCOUNTS_FILE')
            )
            browser.close()
            (ARTIFACTS / 'actual-results.json').write_text(
                json.dumps(
                    {
                        'checks': checks,
                        'scope': 'real disposable loopback HTTP/WS/physics; not production',
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + '\n'
            )
            print(json.dumps(checks, ensure_ascii=False, indent=2))
            return
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
            assert page.locator('#pilot-inspect').inner_text() == '靠近星球查看'
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
