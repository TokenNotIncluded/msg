/* Public observer protocol and lifecycle with a deterministic server/clock. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../src/msg/data/root-web-observer-client.js'), 'utf8');
const BASE = 1700000000000;
const plain = value => JSON.parse(JSON.stringify(value));
const regions = Array.from({ length: 19 }, (_, id) => ({ id, name: 'sector-' + id, center: [id * 5, 0, 0], radius: 240 }));
const anchors = [
  { id: 'u_root', position: [0, 0, 0], radius: 3 },
  { id: 'u_other', position: [90, 20, -10], radius: 2 },
];
const gravity = () => ({ version: 1, wells: anchors.map(item => ({ ...plain(item), influence: 36 })) });
const field = (overrides = {}) => ({ version: 2, seed: '0123456789abcdef'.repeat(2), count: 2300,
  revision: 3, taken: Buffer.alloc(Math.ceil(2300 / 8)).toString('base64'), radius: 8, fuel: 4,
  respawn_ms: 45000, layout_version: 1, anchors: plain(anchors), ...overrides });
const hello = (overrides = {}) => ({ v: 1, type: 'observer_hello', regions: plain(regions),
  server_time_ms: BASE, state_time_ms: BASE - 10, tick_hz: 15,
  limits: { max_players: 96, max_snapshot_bytes: 196608, snapshot_hz: 5, world_extent: 480 },
  gravity: gravity(), collectibles: field(), ...overrides });
const ship = (overrides = {}) => ({ id: 'flight_' + '1'.repeat(32), subject_id: 'u_root', handle: '@root', guest: false,
  position: [10, 20, 30], velocity: [1, 2, 3], yaw: .4, pitch: .2, hp: 100, fuel: 92,
  shield_until_ms: BASE + 1000, laser_ready_ms: BASE + 400, shield_ready_ms: BASE + 5000,
  dash_ready_ms: BASE + 2000, dash_until_ms: BASE + 100, region_ready_ms: BASE + 3000,
  respawn_at_ms: 0, region: 0, ack_seq: 7, score: 8, collected: 2, ...overrides });
const counts = players => Object.fromEntries(regions.map(item => [String(item.id), players.filter(p => p.region === item.id).length]));
const snapshot = (overrides = {}) => {
  const players = overrides.players ?? [];
  return { v: 1, type: 'observer_snapshot', tick: 1, server_time_ms: BASE + 100, state_time_ms: BASE + 90,
    players, events: [], total_players: players.length, region_counts: counts(players),
    gravity: gravity(), collectibles: field(), ...overrides };
};

function browser({ hidden = false, callbackOverrides = {}, protocol = 'https:', webSocketThrows = false } = {}) {
  let now = 0, wallOffset = 0, nextId = 1;
  const jobs = new Map(), sockets = [], documents = new Map(), windows = new Map();
  const statuses = [], snapshots = [], hellos = [], errors = [];
  const events = map => ({
    addEventListener(name, fn) { if (!map.has(name)) map.set(name, new Set()); map.get(name).add(fn); },
    removeEventListener(name, fn) { map.get(name)?.delete(fn); },
  });
  const emit = (map, name) => { for (const fn of [...(map.get(name) ?? [])]) fn(); };
  const schedule = (fn, delay) => { const id = nextId++; jobs.set(id, { fn, due: now + delay }); return id; };
  const clock = {
    get now() { return now; },
    wallShift(delta) { wallOffset += delta; },
    advance(delta) {
      const end = now + delta;
      let iterations = 0;
      while (true) {
        const next = [...jobs.entries()].filter(([, job]) => job.due <= end).sort((a, b) => a[1].due - b[1].due || a[0] - b[0])[0];
        if (!next) break;
        assert.ok(++iterations < 1000, 'reconnect timers must be bounded');
        const [id, job] = next; now = job.due; jobs.delete(id); job.fn();
      }
      now = end;
    },
  };
  class FakeWebSocket {
    constructor(url) {
      if (webSocketThrows) throw new Error('connection construction failed');
      this.url = url; this.readyState = 0; this.sent = []; sockets.push(this);
    }
    open() { this.readyState = 1; this.onopen?.({}); }
    send(raw) { assert.equal(this.readyState, 1); if (this.sendThrows) throw new Error('send failed'); this.sent.push(JSON.parse(raw)); }
    receive(value) { this.raw(JSON.stringify(value)); }
    raw(data) { this.onmessage?.({ data }); }
    close(code, reason) { this.readyState = 3; this.closed = { code, reason }; }
    remoteClose(code = 1006) { this.readyState = 3; this.onclose?.({ code }); }
  }
  const document = { hidden, visibilityState: hidden ? 'hidden' : 'visible', ...events(documents) };
  Object.defineProperty(document, 'cookie', { get() { throw new Error('observer must not read cookies'); } });
  const sandbox = {
    WebSocket: FakeWebSocket, location: { protocol, host: 'msg.example:8443' }, document,
    Date: { now: () => BASE + now + wallOffset }, performance: { now: () => now },
    setTimeout: schedule, clearTimeout: id => jobs.delete(id), ...events(windows),
  };
  for (const name of ['localStorage', 'sessionStorage']) Object.defineProperty(sandbox, name, {
    get() { throw new Error('observer must not access identity or resume storage'); },
  });
  const context = vm.createContext(sandbox);
  vm.runInContext(source, context);
  const client = new context.MSGFlightObserver.Client({
    onStatus: (...args) => statuses.push(args), onSnapshot: value => snapshots.push(value),
    onHello: value => hellos.push(value), onError: value => errors.push(value), ...callbackOverrides,
  });
  return {
    client, clock, jobs, sockets, statuses, snapshots, hellos, errors, context,
    hide() { document.hidden = true; document.visibilityState = 'hidden'; emit(documents, 'visibilitychange'); },
    show() { document.hidden = false; document.visibilityState = 'visible'; emit(documents, 'visibilitychange'); },
    pagehide() { emit(windows, 'pagehide'); },
    listenerCount() { return [...documents.values(), ...windows.values()].reduce((sum, group) => sum + group.size, 0); },
    join(value = hello()) { client.connect(); const ws = sockets.at(-1); ws.open(); ws.receive(value); return ws; },
  };
}

function assertFailed(b, ws) {
  assert.equal(b.client.connected, false);
  assert.equal(ws.closed.code, 1002);
  assert.equal(b.statuses.at(-1)[0], 'failed');
  assert.equal(b.errors.length, 1);
  assert.equal(b.client.snapshot, null);
  assert.equal(b.client.regions, null);
  assert.equal(b.jobs.size, 0);
  b.clock.advance(60000);
  assert.equal(b.sockets.length, 1, 'invalid data must not reconnect automatically');
}

test('only observes the same-origin endpoint and never reads credential storage or sends controls', () => {
  const b = browser(), ws = b.join();
  b.client.connect();
  assert.equal(b.sockets.length, 1);
  assert.equal(ws.url, 'wss://msg.example:8443/_flight');
  assert.deepEqual(ws.sent, [{ v: 1, type: 'observe' }]);
  assert.equal(b.client.connected, true);
  assert.equal(typeof b.client.setInput, 'undefined');
  assert.equal(typeof b.client.chooseRegion, 'undefined');
  assert.equal(b.client.self, undefined);
  for (let i = 1; i <= 10; i++) { b.clock.advance(1000); ws.receive(snapshot({ tick: i, server_time_ms: BASE + i * 1000, state_time_ms: BASE + i * 1000 - 10 })); }
  assert.deepEqual(ws.sent, [{ v: 1, type: 'observe' }], 'watchdogs never emit input or keepalive frames');
  const local = browser({ protocol: 'http:' });
  assert.equal(local.join().url, 'ws://msg.example:8443/_flight');
});

test('zero-player snapshots are valid; public regions and real geometry are immutable copies', () => {
  const b = browser(), input = hello(), ws = b.join(input);
  ws.receive(snapshot());
  assert.equal(b.snapshots.length, 1);
  assert.equal(b.client.snapshot.total_players, 0);
  assert.equal(b.client.snapshot.players.length, 0);
  assert.deepEqual(plain(b.client.snapshot.region_counts), counts([]));
  assert.equal(b.client.regions.length, 19);
  assert.equal(b.hellos[0].collectibles.mask.length, 288);
  input.regions[0].center[0] = 999;
  assert.equal(b.client.regions[0].center[0], 0);
  for (const value of [b.client.regions, b.client.regions[0], b.client.regions[0].center,
    b.hellos[0], b.hellos[0].limits, b.hellos[0].gravity.wells[0].position,
    b.hellos[0].collectibles.anchors[0], b.hellos[0].collectibles.mask,
    b.client.snapshot, b.client.snapshot.players, b.client.snapshot.events, b.client.snapshot.region_counts]) assert.ok(Object.isFrozen(value));
});

test('two real Ship.wire records retain positions, controls, cooldowns and public identity only', () => {
  const b = browser(), ws = b.join();
  const players = [ship(), ship({ id: 'flight_' + '2'.repeat(32), guest: true, subject_id: null, handle: 'guest-1234', region: 18, ack_seq: -1 })];
  ws.receive(snapshot({ players }));
  assert.deepEqual(plain(b.client.snapshot.players), players);
  assert.equal(b.client.snapshot.region_counts['0'], 1);
  assert.equal(b.client.snapshot.region_counts['18'], 1);
  assert.ok(Object.isFrozen(b.client.snapshot.players[0]));
  assert.ok(Object.isFrozen(b.client.snapshot.players[0].position));
  assert.ok(Object.isFrozen(b.client.snapshot.players[0].velocity));
  const privatePilot = ship({ subject_id: null, handle: 'pilot-5678', guest: false });
  ws.receive(snapshot({ tick: 2, players: [privatePilot] }));
  assert.equal(b.client.snapshot.players[0].subject_id, null, 'anonymous projection must not become an identity');
});

test('accepts the 96-player cap without depending on a local/self ship', () => {
  const b = browser(), ws = b.join();
  const players = Array.from({ length: 96 }, (_, index) => ship({ id: 'flight_' + index, region: index % 19 }));
  ws.receive(snapshot({ players }));
  assert.equal(b.client.snapshot.total_players, 96);
  assert.equal(Object.values(b.client.snapshot.region_counts).reduce((a, c) => a + c, 0), 96);
});

test('duplicate and stale ticks do not rewind state, invoke callbacks or refresh the watchdog', () => {
  const b = browser(), ws = b.join();
  ws.receive(snapshot({ tick: 10, players: [ship()] }));
  const accepted = b.client.snapshot;
  b.clock.advance(4000);
  ws.receive(snapshot({ tick: 10, players: [ship({ position: [0, 0, 0] })] }));
  ws.receive(snapshot({ tick: 9 }));
  assert.equal(b.client.snapshot, accepted);
  assert.equal(b.snapshots.length, 1);
  b.clock.advance(1000);
  assert.equal(b.client.connected, false);
  assert.equal(b.statuses.at(-1)[0], 'reconnecting');
});

test('render clock follows monotonic elapsed time and cannot rewind on transit delay or wall-clock changes', () => {
  const b = browser(), ws = b.join();
  b.clock.advance(500);
  assert.equal(b.client.serverNow, BASE + 500);
  ws.receive(snapshot());
  assert.equal(b.client.serverNow, BASE + 500);
  b.clock.wallShift(-1000000); b.clock.advance(100);
  assert.equal(b.client.serverNow, BASE + 600);
});

test('rejects incomplete, conflicting or credential-bearing hello frames', () => {
  const malformed = [
    hello({ resume: 'secret' }), hello({ self: ship() }), hello({ cookie: 'secret' }), hello({ subject_id: 'u_root' }),
    hello({ v: 2 }), hello({ tick_hz: 60 }), hello({ state_time_ms: BASE + 1 }),
    hello({ regions: regions.slice(1) }), hello({ regions: [...regions.slice(1), regions[1]] }),
    hello({ regions: regions.map(item => item.id === 0 ? { ...item, owner: 'private' } : item) }),
    hello({ limits: { ...hello().limits, world_extent: 100000 } }),
    hello({ limits: { ...hello().limits, token: 1 } }), hello({ limits: { max_players: 96 } }),
  ];
  for (const value of malformed) { const b = browser(), ws = b.join(value); assertFailed(b, ws); }
  const b = browser(), ws = b.join(); ws.receive(hello()); assertFailed(b, ws);
});

test('rejects identity metadata and arbitrary private fields at every snapshot/ship boundary', () => {
  const malformed = [
    snapshot({ self_id: 'someone' }), snapshot({ region: 0 }), snapshot({ resume: 'secret' }), snapshot({ self: ship() }),
    ...['home_position', 'home_body', 'token', 'cookie', 'private_subject_id', 'identity', '__proto__'].map(key =>
      snapshot({ players: [{ ...ship(), [key]: { value: 'private' } }] })),
  ];
  for (const value of malformed) { const b = browser(), ws = b.join(); ws.receive(value); assertFailed(b, ws); }
});

test('rejects malformed, nonfinite or out-of-bounds players and duplicate IDs', () => {
  const malformed = [
    { position: [481, 0, 0] }, { position: [0, 0] }, { position: [NaN, 0, 0] }, { velocity: [61, 0, 0] },
    { yaw: 4 }, { pitch: Infinity }, { hp: -1 }, { fuel: 101 }, { region: 19 }, { ack_seq: -2 },
    { guest: true, subject_id: 'u_root' }, { handle: 'bad\nname' }, { id: '../private' },
    { laser_ready_ms: NaN }, { shield_ready_ms: -1 }, { dash_until_ms: 'wrong' }, { score: .5 }, { collected: -1 },
  ];
  for (const patch of malformed) { const b = browser(), ws = b.join(); ws.receive(snapshot({ players: [ship(patch)] })); assertFailed(b, ws); }
  const b = browser(), ws = b.join(); ws.receive(snapshot({ players: [ship(), ship()] })); assertFailed(b, ws);
  const missing = ship(); delete missing.position;
  const c = browser(), other = c.join(); other.receive(snapshot({ players: [missing] })); assertFailed(c, other);
});

test('requires all 19 exact region counts and matching totals, with no combat payload', () => {
  const malformed = [
    snapshot({ total_players: 1 }), snapshot({ players: Array.from({ length: 97 }, (_, i) => ship({ id: 'flight_' + i })) }),
    snapshot({ region_counts: {} }), snapshot({ region_counts: { ...counts([]), '19': 0 } }),
    snapshot({ region_counts: { ...counts([]), '0': 1 } }), snapshot({ region_counts: { ...counts([]), '0': false } }),
    snapshot({ events: [{ type: 'hit' }] }), snapshot({ events: {} }), snapshot({ state_time_ms: BASE + 200 }),
    snapshot({ tick: .1 }),
  ];
  for (const value of malformed) { const b = browser(), ws = b.join(); ws.receive(value); assertFailed(b, ws); }
});

test('legacy particle descriptors inherit geometry and new seeds can refresh the public world', () => {
  const legacy = field({ version: 1 }); delete legacy.layout_version; delete legacy.anchors;
  const b = browser(), ws = b.join(hello({ collectibles: legacy }));
  ws.receive(snapshot({ collectibles: { version: 1, revision: 4, taken: legacy.taken } }));
  assert.equal(b.client.snapshot.collectibles.seed, legacy.seed);
  assert.equal(b.client.snapshot.collectibles.radius, 8);
  const newSeed = field({ seed: 'f'.repeat(32), revision: 0 });
  ws.receive(snapshot({ tick: 2, collectibles: newSeed, gravity: { version: 1, wells: [] } }));
  assert.equal(b.client.snapshot.collectibles.seed, newSeed.seed);
  assert.equal(b.client.snapshot.gravity.wells.length, 0, 'removed public planets refresh without private metadata');
});

test('fails closed on invalid geometry, masks or same-seed geometry changes', () => {
  const malformed = [
    hello({ gravity: { version: 1, wells: [gravity().wells[0], gravity().wells[0]] } }),
    hello({ gravity: { ...gravity(), home: 'secret' } }),
    hello({ gravity: { version: 1, wells: [{ ...gravity().wells[0], influence: 999 }] } }),
    hello({ gravity: { version: 1, wells: [{ ...gravity().wells[0], position: [400, 400, 0] }] } }),
    hello({ collectibles: field({ count: 999 }) }), hello({ collectibles: field({ taken: 'AAAA' }) }),
    hello({ collectibles: field({ token: 'private' }) }), hello({ collectibles: field({ anchors: [anchors[0], anchors[0]] }) }),
  ];
  for (const value of malformed) { const b = browser(), ws = b.join(value); assertFailed(b, ws); }
  const b = browser(), ws = b.join(); ws.receive(snapshot({ collectibles: field({ radius: 4 }) })); assertFailed(b, ws);
  const c = browser(), other = c.join();
  const mask = Buffer.alloc(288); mask[287] = 128;
  other.receive(snapshot({ collectibles: field({ taken: mask.toString('base64') }) })); assertFailed(c, other);
});

test('newer ticks cannot roll back timestamps or particle revision', () => {
  for (const override of [
    { server_time_ms: BASE + 99 }, { state_time_ms: BASE + 89 }, { collectibles: field({ revision: 2 }) },
  ]) {
    const b = browser(), ws = b.join(); ws.receive(snapshot()); ws.receive(snapshot({ tick: 2, ...override })); assertFailed(b, ws);
  }
});

test('rejects unbounded, non-JSON, binary, pilot-protocol and pre-hello frames', () => {
  for (const raw of [' '.repeat(196609), '"' + '树'.repeat(100000) + '"', '{', 'null', '[]',
    '{"v":1,"type":"snapshot"}', '{"v":1,"type":"observer_snapshot","tick":1e309}', new Uint8Array(1)]) {
    const b = browser(), ws = b.join(); ws.raw(raw); assertFailed(b, ws);
  }
  const b = browser(); b.client.connect(); const ws = b.sockets[0]; ws.open(); ws.receive(snapshot()); assertFailed(b, ws);
});

test('hidden pages close all network/watchdog work, then visible pages observe a fresh stream', () => {
  const b = browser({ hidden: true }); b.client.connect();
  assert.equal(b.sockets.length, 0); assert.equal(b.jobs.size, 0);
  b.show(); const ws = b.sockets[0]; ws.open(); ws.receive(hello()); ws.receive(snapshot());
  const late = ws.onmessage;
  b.hide();
  assert.equal(ws.closed.code, 1000); assert.equal(b.client.connected, false); assert.equal(b.jobs.size, 0);
  assert.equal(b.client.snapshot, null);
  late({ data: JSON.stringify(snapshot({ tick: 999 })) });
  assert.equal(b.snapshots.length, 1);
  b.clock.advance(60000); assert.equal(b.sockets.length, 1);
  b.show(); const next = b.sockets[1]; next.open(); next.receive(hello()); next.receive(snapshot());
  assert.deepEqual(next.sent, [{ v: 1, type: 'observe' }]); assert.equal(b.client.connected, true);
});

test('backoff is bounded and explicit disconnect cancels pending retries', () => {
  const b = browser(); let ws = b.join();
  for (const delay of [500, 1000, 2000, 4000, 8000]) {
    ws.remoteClose(); const count = b.sockets.length;
    b.clock.advance(delay - 1); assert.equal(b.sockets.length, count);
    b.clock.advance(1); ws = b.sockets.at(-1); ws.open(); ws.receive(hello());
  }
  ws.remoteClose(); assert.equal(b.statuses.at(-1)[0], 'failed'); assert.equal(b.jobs.size, 0);
  b.clock.advance(60000); assert.equal(b.sockets.length, 6);
  b.client.connect(); const last = b.sockets.at(-1); last.remoteClose();
  b.client.disconnect(); assert.equal(b.jobs.size, 0); b.clock.advance(60000); assert.equal(b.sockets.length, 7);
  b.hide(); b.show(); assert.equal(b.sockets.length, 7, 'visibility cannot undo an explicit disconnect');
});

test('handshake stalls, missing fresh snapshots and send errors enter bounded reconnect', () => {
  const b = browser(); b.client.connect(); b.clock.advance(7999); assert.equal(b.sockets.length, 1);
  b.clock.advance(1); assert.equal(b.sockets[0].readyState, 3);
  b.clock.advance(500); assert.equal(b.sockets.length, 2);
  const c = browser(), ws = c.join(); c.clock.advance(5000);
  assert.equal(ws.readyState, 3); c.clock.advance(500); assert.equal(c.sockets.length, 2);
  const d = browser(); d.client.connect(); const faulty = d.sockets[0]; faulty.sendThrows = true; faulty.open();
  d.clock.advance(500); assert.equal(d.sockets.length, 2);
  const unavailable = browser({ webSocketThrows: true }); unavailable.client.connect(); unavailable.clock.advance(60000);
  assert.equal(unavailable.statuses.at(-1)[0], 'failed'); assert.equal(unavailable.jobs.size, 0);
});

test('policy close codes stop automatic reconnect without changing account authority', () => {
  for (const code of [1002, 1003, 1008, 1009, 4013]) {
    const b = browser(), ws = b.join(); ws.remoteClose(code);
    assert.equal(b.statuses.at(-1)[0], 'failed'); assert.equal(b.jobs.size, 0);
    b.clock.advance(60000); assert.equal(b.sockets.length, 1);
  }
});

test('destroy releases listeners, timers and callbacks, and stale events cannot restart transport', () => {
  const b = browser(), ws = b.join(); const lateOpen = ws.onopen, lateMessage = ws.onmessage, lateClose = ws.onclose;
  assert.equal(b.listenerCount(), 2);
  b.client.destroy(); b.client.destroy(); b.client.connect(); b.show(); b.pagehide();
  assert.equal(b.listenerCount(), 0); assert.equal(b.jobs.size, 0);
  lateOpen({}); lateMessage({ data: JSON.stringify(hello()) }); lateClose({ code: 1006 });
  b.clock.advance(60000);
  assert.equal(b.sockets.length, 1); assert.equal(b.hellos.length, 1);
  assert.equal(b.client.snapshot, null); assert.equal(ws.onmessage, null);
});

test('pagehide disconnects, and view callback errors cannot corrupt accepted protocol data', () => {
  const b = browser({ callbackOverrides: {
    onHello() { throw new Error('view failure'); }, onSnapshot() { throw new Error('view failure'); },
  } });
  const ws = b.join(); ws.receive(snapshot()); assert.equal(b.client.connected, true);
  b.pagehide(); assert.equal(b.client.connected, false); assert.equal(b.jobs.size, 0);
});

test('a callback that disconnects during connecting/connected does not leak sockets or timers', () => {
  let client;
  const b = browser({ callbackOverrides: { onStatus(state) { if (state === 'connecting') client.disconnect(); } } });
  client = b.client; client.connect(); assert.equal(b.sockets.length, 0); assert.equal(b.jobs.size, 0);
  const c = browser({ callbackOverrides: { onStatus(state) { if (state === 'connected') client.disconnect(); } } });
  client = c.client; const ws = c.join(); assert.equal(ws.readyState, 3); assert.equal(c.jobs.size, 0); assert.equal(c.hellos.length, 0);
});
