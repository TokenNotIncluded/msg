/* Real transport lifecycle with a fake server and monotonic clock; no DOM or network. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../src/msg/data/root-web-flight-client.js'), 'utf8');
const BASE_TIME = 1700000000000;
const SELF_ID = 'flight_' + '1'.repeat(32);
const OTHER_ID = 'flight_' + '2'.repeat(32);
const RESUME = 'test_game_ram_resume_ticket_1234567890';
const plain = value => JSON.parse(JSON.stringify(value));
const ship = (overrides = {}) => ({
  id: SELF_ID, subject_id: null, handle: 'guest-1234', guest: true,
  position: [10, 20, 30], velocity: [0, 0, 0], yaw: .4, pitch: .2,
  hp: 100, fuel: 100, shield_until_ms: 0, laser_ready_ms: BASE_TIME + 400,
  shield_ready_ms: 0, dash_ready_ms: 0, respawn_at_ms: 0,
  region: 0, ack_seq: 7, region_ready_ms: 0, score: 0, ...overrides,
});
const regions = Array.from({ length: 19 }, (_, id) =>
  ({ id, name: 'sector-' + String(id).padStart(2, '0'), center: [0, 0, 0], radius: 240 }));
const hello = (overrides = {}) => ({
  v: 1, type: 'hello', self: ship(), resume: RESUME,
  server_time_ms: BASE_TIME, tick_hz: 15, region: 0, regions,
  limits: { world_extent: 480, max_players: 96, laser_cooldown_ms: 400 }, ...overrides,
});
const snapshot = (overrides = {}) => ({
  v: 1, type: 'snapshot', tick: 1, server_time_ms: BASE_TIME + 100,
  self_id: SELF_ID, region: 0, players: [ship()], events: [], total_players: 1, ...overrides,
});

function browser({ hidden = false, storageThrows = false, storedTicket, callbackOverrides = {} } = {}) {
  let now = 0, wallOffset = 0, nextId = 1;
  const jobs = new Map(), sockets = [], stored = new Map(), documentEvents = new Map(), windowEvents = new Map();
  const statuses = [], snapshots = [], hellos = [], errors = [];
  if (storedTicket !== undefined) stored.set('msg-flight-resume-v1', storedTicket);
  const schedule = (fn, delay, repeat = false) => {
    const id = nextId++;
    jobs.set(id, { fn, due: now + delay, repeat, delay });
    return id;
  };
  const clock = {
    get now() { return now; },
    wallShift(ms) { wallOffset += ms; },
    advance(ms) {
      const end = now + ms;
      let iterations = 0;
      while (true) {
        const next = [...jobs.entries()].filter(([, job]) => job.due <= end)
          .sort((a, b) => a[1].due - b[1].due || a[0] - b[0])[0];
        if (!next) break;
        assert.ok(++iterations < 10000, 'timer loop must be bounded');
        const [id, job] = next;
        now = job.due;
        if (job.repeat) job.due += job.delay;
        else jobs.delete(id);
        job.fn();
      }
      now = end;
    },
  };
  class FakeWebSocket {
    constructor(url) {
      this.url = url; this.readyState = 0; this.bufferedAmount = 0; this.sent = [];
      sockets.push(this);
    }
    open() { this.readyState = 1; this.onopen?.({}); }
    send(value) {
      assert.equal(this.readyState, 1);
      if (this.sendThrows) throw new Error('synthetic send failure');
      this.sent.push(JSON.parse(value));
    }
    receive(value) { this.onmessage?.({ data: JSON.stringify(value) }); }
    raw(value) { this.onmessage?.({ data: value }); }
    close(code, reason) { this.readyState = 3; this.closed = { code, reason }; }
    remoteClose(code = 1006) { this.readyState = 3; this.onclose?.({ code }); }
  }
  const document = {
    hidden, visibilityState: hidden ? 'hidden' : 'visible',
    addEventListener: (name, fn) => documentEvents.set(name, fn),
  };
  const context = vm.createContext({
    WebSocket: FakeWebSocket, location: { protocol: 'https:', host: 'msg.example:8443' }, document,
    sessionStorage: {
      getItem(key) { if (storageThrows) throw new Error('storage disabled'); return stored.get(key) ?? null; },
      setItem(key, value) { if (storageThrows) throw new Error('storage disabled'); stored.set(key, value); },
      removeItem(key) { if (storageThrows) throw new Error('storage disabled'); stored.delete(key); },
    },
    Date: { now: () => BASE_TIME + now + wallOffset }, performance: { now: () => now },
    setTimeout: (fn, ms) => schedule(fn, ms), clearTimeout: id => jobs.delete(id),
    setInterval: (fn, ms) => schedule(fn, ms, true), clearInterval: id => jobs.delete(id),
    addEventListener: (name, fn) => windowEvents.set(name, fn),
  });
  vm.runInContext(source, context);
  const client = new context.MSGFlightClient.Client({
    onStatus: (...args) => statuses.push(args), onSnapshot: value => snapshots.push(value),
    onHello: value => hellos.push(value), onError: value => errors.push(value), ...callbackOverrides,
  });
  return {
    client, clock, sockets, stored, statuses, snapshots, hellos, errors,
    hide() {
      document.hidden = true; document.visibilityState = 'hidden'; documentEvents.get('visibilitychange')?.();
    },
    show() {
      document.hidden = false; document.visibilityState = 'visible'; documentEvents.get('visibilitychange')?.();
    },
    blur: () => windowEvents.get('blur')?.(),
    pagehide: () => windowEvents.get('pagehide')?.(),
    join(value = hello()) { client.connect(); const ws = sockets.at(-1); ws.open(); ws.receive(value); return ws; },
  };
}

test('joins only the exact same-origin endpoint, with no user credential or resume in its URL', () => {
  const b = browser({ storedTicket: RESUME });
  b.client.connect(); b.client.connect();
  assert.equal(b.sockets.length, 1);
  const ws = b.sockets[0];
  assert.equal(ws.url, 'wss://msg.example:8443/_flight');
  ws.open();
  assert.deepEqual(ws.sent, [{ v: 1, type: 'join', resume: RESUME }]);
  assert.equal(b.client.connected, false);
  ws.receive(hello());
  assert.equal(b.client.connected, true);
  assert.deepEqual(plain(b.client.self), ship());
  assert.equal(b.hellos.length, 1);
  assert.deepEqual([...b.stored.keys()], ['msg-flight-resume-v1']);
});

test('blocked storage is optional and invalid stored tickets are never transmitted', () => {
  const blocked = browser({ storageThrows: true });
  let ws = blocked.join();
  assert.deepEqual(ws.sent[0], { v: 1, type: 'join' });
  ws.remoteClose(); blocked.clock.advance(500); ws = blocked.sockets.at(-1); ws.open();
  assert.equal(ws.sent[0].resume, RESUME, 'an in-memory game ticket still resumes');
  const bad = browser({ storedTicket: '\nuser-credential' });
  ws = bad.join();
  assert.deepEqual(ws.sent[0], { v: 1, type: 'join' });
});

test('snapshots contain the server-spawned self and all world players, with immutable bounded records', () => {
  const b = browser(), ws = b.join();
  ws.receive(snapshot({ players: [ship(), ship({ id: OTHER_ID, region: 18 })], total_players: 2 }));
  assert.equal(b.snapshots.length, 1);
  assert.deepEqual(plain(b.client.self.position), [10, 20, 30]);
  assert.equal(b.client.snapshot.players[1].region, 18);
  assert.ok(Object.isFrozen(b.client.snapshot));
  assert.ok(Object.isFrozen(b.client.snapshot.players[1].position));
});

test('inputs are limited to controls at 20Hz and sequence starts above the server acknowledgement', () => {
  const b = browser(), ws = b.join();
  assert.equal(b.client.setInput({ throttle: 2, strafe: -3, lift: .4, yaw: 20, pitch: 9, brake: true, actions: ['laser'] }), true);
  b.clock.advance(1000);
  const frames = ws.sent.filter(frame => frame.type === 'input');
  assert.equal(frames.length, 20);
  assert.equal(frames[0].seq, 8); assert.equal(frames.at(-1).seq, 27);
  assert.deepEqual(Object.keys(frames[0]).sort(),
    ['v', 'type', 'seq', 'throttle', 'strafe', 'lift', 'yaw', 'pitch', 'brake', 'actions'].sort());
  assert.equal(frames[0].throttle, 1); assert.equal(frames[0].strafe, -1);
  assert.equal(frames[0].lift, .4); assert.equal(frames[0].pitch, 1.35);
  assert.ok(Math.abs(frames[0].yaw) <= Math.PI);
  assert.deepEqual(frames[0].actions, ['laser']);
});

test('a newly joined server ship acknowledges -1 and its first valid control sequence is zero', () => {
  const b = browser(), ws = b.join(hello({ self: ship({ ack_seq: -1 }) }));
  ws.receive(snapshot({ players: [ship({ ack_seq: -1 })] }));
  assert.equal(b.client.connected, true);
  b.clock.advance(50); assert.equal(ws.sent.at(-1).seq, 0);
});

test('physics, identity, nonfinite inputs and unknown combat actions cannot cross the wire', () => {
  for (const value of [{ position: [3, 4, 5] }, { hp: 0 }, { damage: 999 }, { subject_id: 'root' },
    { throttle: NaN }, { yaw: Infinity }, { actions: ['admin'] }, { actions: 'laser' }, { brake: 1 }]) {
    const b = browser(), ws = b.join();
    b.client.setInput({ throttle: 1, actions: ['laser'] });
    assert.equal(b.client.setInput(value), false);
    b.clock.advance(50);
    const frame = ws.sent.at(-1);
    assert.equal(frame.throttle, 0); assert.deepEqual(frame.actions, []);
    assert.equal(Object.hasOwn(frame, 'position'), false); assert.equal(Object.hasOwn(frame, 'hp'), false);
  }
});

test('shield and dash are consumed once per press while laser can remain held', () => {
  const b = browser(), ws = b.join();
  b.client.setInput({ actions: ['laser', 'shield', 'dash'] }); b.clock.advance(50);
  assert.deepEqual(ws.sent.at(-1).actions, ['laser', 'shield', 'dash']);
  for (let i = 0; i < 4; i++) {
    b.client.setInput({ actions: ['laser', 'shield', 'dash'] }); b.clock.advance(50);
    assert.deepEqual(ws.sent.at(-1).actions, ['laser']);
  }
  b.client.setInput({ actions: [] }); b.client.setInput({ actions: ['shield'] }); b.clock.advance(50);
  assert.deepEqual(ws.sent.at(-1).actions, ['shield']);
});

test('a quick laser button press survives the renderer clearing actions before the 20Hz send', () => {
  const b = browser(), ws = b.join();
  b.client.setInput({ actions: ['laser'] }); b.clock.advance(16);
  b.client.setInput({ actions: [] }); b.clock.advance(34);
  assert.deepEqual(ws.sent.at(-1).actions, ['laser']);
  b.clock.advance(50); assert.deepEqual(ws.sent.at(-1).actions, []);
  b.client.setInput({ actions: ['laser'] }); b.clock.advance(50);
  assert.deepEqual(ws.sent.at(-1).actions, ['laser'], 'held laser is de-duplicated against its pending press');
});

test('region selection is explicit, validated and releases pending movement and skills first', () => {
  const b = browser(), ws = b.join();
  b.client.setInput({ throttle: 1, actions: ['dash', 'laser'] });
  assert.equal(b.client.chooseRegion(18), true);
  assert.deepEqual(ws.sent.at(-1), { v: 1, type: 'region', region: 18 });
  assert.equal(ws.sent.at(-2).throttle, 0); assert.deepEqual(ws.sent.at(-2).actions, []);
  for (const value of [-1, 19, 1.5, '1', NaN]) assert.equal(b.client.chooseRegion(value), false);
  b.clock.advance(50); assert.deepEqual(ws.sent.at(-1).actions, []);
});

test('explicit map navigation works while controls are suspended and does not resume combat', () => {
  const b = browser(), ws = b.join(); b.client.suspend();
  assert.equal(b.client.chooseRegion(18), true);
  assert.deepEqual(ws.sent.at(-1), { v: 1, type: 'region', region: 18 });
  const count = ws.sent.length; b.clock.advance(1000); assert.equal(ws.sent.length, count);
  assert.equal(b.client.setInput({ actions: ['laser'] }), false);
  b.hide(); assert.equal(b.client.chooseRegion(0), false);
});

test('server time is anchored at hello/snapshot and immune to wall-clock jumps for cooldowns', () => {
  const b = browser(), ws = b.join(hello({ server_time_ms: BASE_TIME + 8000 }));
  assert.equal(b.client.serverNow, BASE_TIME + 8000);
  b.clock.advance(125); assert.equal(b.client.serverNow, BASE_TIME + 8125);
  b.clock.wallShift(-3600000); assert.equal(b.client.serverNow, BASE_TIME + 8125);
  ws.receive(snapshot({ server_time_ms: BASE_TIME + 8100 }));
  assert.equal(b.client.serverNow, BASE_TIME + 8125, 'a delayed snapshot cannot rewind the monotonic clock');
  b.clock.advance(100); assert.equal(b.client.serverNow, BASE_TIME + 8225);
  b.clock.wallShift(7200000); assert.equal(b.client.serverNow, BASE_TIME + 8225);
});

test('duplicate and late valid snapshots cannot rewind positions or server clocks', () => {
  const b = browser(), ws = b.join();
  ws.receive(snapshot({ tick: 2, server_time_ms: BASE_TIME + 200, players: [ship({ position: [1, 2, 3] })] }));
  ws.receive(snapshot({ tick: 1 })); ws.receive(snapshot({ tick: 2 }));
  assert.equal(b.snapshots.length, 1); assert.equal(b.client.snapshot.tick, 2);
  assert.deepEqual(plain(b.client.self.position), [1, 2, 3]);
});

test('blur sends a neutral frame immediately, stops the pump and requires explicit resume', () => {
  const b = browser(), ws = b.join();
  b.client.setInput({ throttle: 1, actions: ['laser', 'shield'] }); b.clock.advance(50);
  b.blur();
  assert.equal(ws.sent.at(-1).throttle, 0); assert.deepEqual(ws.sent.at(-1).actions, []);
  const count = ws.sent.length; b.clock.advance(1000); assert.equal(ws.sent.length, count);
  assert.equal(b.client.setInput({ throttle: 1 }), false);
  assert.equal(b.client.resume(), true); b.clock.advance(50);
  assert.equal(ws.sent.at(-1).throttle, 0); assert.deepEqual(ws.sent.at(-1).actions, []);
});

test('hidden pages neither connect nor silently restart combat when made visible', () => {
  const b = browser({ hidden: true });
  b.client.connect(); b.clock.advance(10000); assert.equal(b.sockets.length, 0);
  assert.equal(b.client.resume(), false); b.show(); b.clock.advance(10000); assert.equal(b.sockets.length, 0);
  assert.equal(b.client.resume(), true);
  const ws = b.sockets[0]; ws.open(); ws.receive(hello());
  b.client.setInput({ actions: ['laser', 'dash'] }); b.clock.advance(50); b.hide();
  assert.deepEqual(ws.sent.at(-1).actions, []);
  const count = ws.sent.length; b.show(); b.clock.advance(1000); assert.equal(ws.sent.length, count);
  b.client.resume(); b.clock.advance(50); assert.deepEqual(ws.sent.at(-1).actions, []);
});

test('an in-flight handshake is closed if focus or visibility is lost before it opens', () => {
  const b = browser(); b.client.connect(); const ws = b.sockets[0]; b.hide(); ws.open();
  assert.deepEqual(ws.sent, []); assert.equal(ws.closed.code, 1000);
  b.show(); b.client.resume(); assert.equal(b.sockets.length, 2);
});

test('disconnect removes stale ships and input, ignores late frames and does not reconnect', () => {
  const b = browser(), ws = b.join(); ws.receive(snapshot());
  const late = ws.onmessage;
  b.client.setInput({ actions: ['laser', 'dash'] }); b.client.disconnect();
  assert.equal(b.client.connected, false); assert.equal(b.client.self, null); assert.equal(b.client.snapshot, null);
  late({ data: JSON.stringify(snapshot({ tick: 2 })) });
  b.clock.advance(60000); assert.equal(b.sockets.length, 1); assert.equal(b.snapshots.length, 1);
  assert.deepEqual(ws.sent.at(-1).actions, []);
});

test('connection loss clears all state and reconnects with resume and acknowledged sequence', () => {
  const b = browser(), ws = b.join(); ws.receive(snapshot());
  b.client.setInput({ throttle: 1, actions: ['shield', 'laser'] }); b.clock.advance(50);
  ws.remoteClose();
  assert.equal(b.client.self, null); assert.equal(b.client.snapshot, null);
  const count = ws.sent.length; b.clock.advance(499); assert.equal(b.sockets.length, 1); assert.equal(ws.sent.length, count);
  b.clock.advance(1); const resumed = b.sockets.at(-1); resumed.open();
  assert.equal(resumed.sent[0].resume, RESUME);
  resumed.receive(hello({ self: ship({ ack_seq: 50 }) })); b.clock.advance(50);
  assert.equal(resumed.sent.at(-1).seq, 51); assert.equal(resumed.sent.at(-1).throttle, 0);
  assert.deepEqual(resumed.sent.at(-1).actions, []);
});

test('reconnect is bounded even if the server repeatedly accepts hello then drops the connection', () => {
  const b = browser(); let ws = b.join();
  for (const delay of [500, 1000, 2000, 4000, 8000]) {
    ws.remoteClose(); b.clock.advance(delay); ws = b.sockets.at(-1); ws.open(); ws.receive(hello());
  }
  ws.remoteClose(); b.clock.advance(60000);
  assert.equal(b.sockets.length, 6); assert.equal(b.statuses.at(-1)[0], 'failed');
  assert.equal(b.client.connected, false); assert.equal(b.client.resume(), false);
});

test('losing visibility cancels a reconnect and visible focus is required to retry', () => {
  const b = browser(), ws = b.join(); ws.remoteClose(); b.hide(); b.clock.advance(10000);
  assert.equal(b.sockets.length, 1); b.show(); b.clock.advance(10000); assert.equal(b.sockets.length, 1);
  b.client.resume(); assert.equal(b.sockets.length, 2);
});

test('policy rejection forgets only the game ticket and never loops an invalid resume', () => {
  const b = browser(), ws = b.join(); ws.remoteClose(1008); b.clock.advance(10000);
  assert.equal(b.sockets.length, 1); assert.equal(b.stored.size, 0);
  b.client.connect(); const next = b.sockets.at(-1); next.open();
  assert.deepEqual(next.sent[0], { v: 1, type: 'join' });
});

test('expired browser identity cannot reconnect forever with a stale authenticated game ticket', () => {
  const b = browser(), ws = b.join(); ws.remoteClose(4013); b.clock.advance(10000);
  assert.equal(b.sockets.length, 1); assert.equal(b.stored.size, 0); assert.equal(b.statuses.at(-1)[0], 'failed');
});

test('handshake timeout reconnects and excessive send backlog never queues more controls', () => {
  const b = browser(); b.client.connect(); const ws = b.sockets[0]; ws.open();
  b.clock.advance(8000); assert.equal(ws.closed.code, 1000); b.clock.advance(500);
  const next = b.sockets.at(-1); next.open(); next.receive(hello());
  next.bufferedAmount = 20000; b.client.setInput({ throttle: 1, actions: ['laser'] }); b.clock.advance(50);
  assert.equal(b.client.connected, false); assert.equal(next.sent.length, 1);
});

test('a socket that never opens also times out instead of hanging indefinitely in connecting', () => {
  const b = browser(); b.client.connect(); const ws = b.sockets[0];
  b.clock.advance(8000); assert.equal(ws.closed.code, 1000); assert.equal(ws.sent.length, 0);
  b.clock.advance(500); assert.equal(b.sockets.length, 2);
  b.client.disconnect(); b.clock.advance(60000); assert.equal(b.sockets.length, 2);
});

test('an open but silent server stops controls and reconnects after the snapshot deadline', () => {
  const b = browser(), ws = b.join(); ws.receive(snapshot());
  b.client.setInput({ throttle: 1, actions: ['laser'] });
  b.clock.advance(4999); assert.equal(b.client.connected, true);
  b.clock.advance(1); assert.equal(b.client.connected, false); assert.equal(b.client.snapshot, null);
  const count = ws.sent.length; b.clock.advance(500); assert.equal(ws.sent.length, count);
  assert.equal(b.sockets.length, 2);
});

test('valid fresh snapshots reset the deadline, while duplicate ticks cannot fake liveness', () => {
  const b = browser(), ws = b.join(); ws.receive(snapshot());
  b.clock.advance(4000); ws.receive(snapshot({ tick: 2, server_time_ms: BASE_TIME + 4000 }));
  b.clock.advance(4000); assert.equal(b.client.connected, true);
  ws.receive(snapshot({ tick: 2, server_time_ms: BASE_TIME + 4000 }));
  b.clock.advance(1000); assert.equal(b.client.connected, false);
});

test('suspended controls stop the deadline and explicit resume starts a fresh snapshot deadline', () => {
  const b = browser(), ws = b.join(); ws.receive(snapshot()); b.client.suspend();
  const count = ws.sent.length; b.clock.advance(60000);
  assert.equal(b.client.connected, true); assert.equal(ws.sent.length, count);
  b.client.resume(); b.clock.advance(4999); assert.equal(b.client.connected, true);
  b.clock.advance(1); assert.equal(b.client.connected, false);
});

test('malformed, oversized, binary, unknown and incompatible frames fail closed and remove old players', () => {
  for (const data of ['{broken', ' '.repeat(262145), new Uint8Array([1]),
    JSON.stringify({ v: 1, type: 'teleport', position: [0, 0, 0] }),
    JSON.stringify({ v: 2, type: 'snapshot' })]) {
    const b = browser(), ws = b.join(); ws.receive(snapshot()); ws.raw(data);
    assert.equal(b.client.connected, false); assert.equal(b.client.snapshot, null); assert.equal(b.client.self, null);
    assert.equal(ws.closed.code, 1002); assert.equal(b.errors.length, 1);
    b.clock.advance(60000); assert.equal(b.sockets.length, 1);
    assert.equal(b.errors[0].includes(RESUME), false);
  }
});

test('invalid or missing self, duplicate ships and out-of-bounds snapshot data reject the entire world', () => {
  const corruptions = [
    { self_id: OTHER_ID }, { players: [ship({ id: OTHER_ID })] },
    { players: [ship(), ship()], total_players: 2 },
    { players: [ship({ subject_id: 'u_forged', guest: false })] },
    { players: [ship({ hp: -1 })] }, { players: [ship({ fuel: 101 })] },
    { players: [ship({ position: [481, 0, 0] })] }, { players: [ship({ velocity: [61, 0, 0] })] },
    { players: [ship({ yaw: 4 })] }, { players: [ship({ pitch: 2 })] },
    { players: [ship({ ack_seq: -1 })] }, { total_players: 0 }, { region: 1 },
    { events: [{ id: 1, type: 'admin', player_id: SELF_ID, at_ms: BASE_TIME }] },
    { server_time_ms: null }, { players: Array.from({ length: 97 }, (_, i) => ship({ id: 'p_' + i })), total_players: 97 },
  ];
  for (const override of corruptions) {
    const b = browser(), ws = b.join(); ws.receive(snapshot()); ws.receive(snapshot({ tick: 2, ...override }));
    assert.equal(b.client.connected, false, JSON.stringify(override));
    assert.equal(b.client.snapshot, null); assert.equal(b.client.self, null);
  }
});

test('signed identity and larger world bounds come from hello, without a locally invented spawn', () => {
  const signed = ship({ subject_id: 'u_signed', guest: false, handle: '@signed', position: [700, -600, 500] });
  const b = browser(), ws = b.join(hello({ self: signed, limits: { world_extent: 1000 } }));
  assert.deepEqual(plain(b.client.self.position), signed.position);
  ws.receive(snapshot({ players: [signed] })); assert.equal(b.client.connected, true);
});

test('invalid hello, duplicate hello and snapshot before hello cannot establish multiplayer state', () => {
  for (const frame of [hello({ self: ship({ position: [1000, 0, 0] }) }), hello({ resume: 'invalid' }),
    hello({ regions: [regions[0], regions[0]] }), hello({ limits: { world_extent: -1 } }),
    hello({ server_time_ms: null }), snapshot()]) {
    const b = browser(); b.client.connect(); const ws = b.sockets[0]; ws.open(); ws.receive(frame);
    assert.equal(b.client.connected, false); assert.equal(b.client.self, null); assert.equal(b.hellos.length, 0);
  }
  const b = browser(), ws = b.join(); ws.receive(hello()); assert.equal(b.client.connected, false);
});

test('validated events and counts are retained; hostile extra fields are not copied to the view', () => {
  const b = browser(), ws = b.join();
  const event = { id: 1, type: 'laser', player_id: SELF_ID, target_id: OTHER_ID,
    position: [1, 2, 3], end: [4, 5, 6], at_ms: BASE_TIME, token: 'never expose this' };
  ws.receive(snapshot({ events: [event], region_counts: Object.fromEntries(regions.map(item => [item.id, item.id === 0 ? 1 : 0])),
    token: 'never expose this', players: [ship({ token: 'never expose this' })] }));
  assert.equal(b.client.snapshot.events.length, 1); assert.equal(b.client.snapshot.region_counts[0], 1);
  assert.equal(JSON.stringify(b.client.snapshot).includes('token'), false);
  assert.ok(Object.isFrozen(b.client.snapshot.events[0]));
});

test('own private home remains own-only across snapshots and private remote identities stay redacted', () => {
  const privateSelf = ship({ guest: false, subject_id: null,
    home_position: [20, 30, 40], home_body: { id: 'flight_home_' + SELF_ID,
      position: [20, 30, 40], radius: 3, private: true, title: '你的私有星球' } });
  const b = browser(), ws = b.join(hello({ self: privateSelf }));
  ws.receive(snapshot({ players: [ship({ guest: false }), ship({ id: OTHER_ID, guest: false, region: 1 })], total_players: 2 }));
  assert.equal(b.client.connected, true); assert.equal(b.client.self.home_body.private, true);
  assert.deepEqual(plain(b.client.self.home_position), [20, 30, 40]);
  assert.equal(JSON.stringify(b.client.snapshot).includes('home_body'), false);
  assert.equal(JSON.stringify(b.client.snapshot).includes('home_position'), false);
  ws.receive(snapshot({ tick: 2, players: [ship({ guest: false }),
    ship({ id: OTHER_ID, guest: false, home_position: [10, 20, 30] })], total_players: 2 }));
  assert.equal(b.client.connected, false, 'a world snapshot may never carry own-only homes');
});

test('malformed private home and region counts cannot reach the renderer', () => {
  for (const home_body of [{ id: 'flight_home_' + OTHER_ID, position: [0, 0, 0], radius: 3, private: true, title: 'home' },
    { id: 'flight_home_' + SELF_ID, position: [500, 0, 0], radius: 3, private: true, title: 'home' }]) {
    const b = browser(), ws = b.join(hello({ self: ship({ home_body }) }));
    assert.equal(b.client.connected, false); assert.equal(b.hellos.length, 0);
  }
  for (const region_counts of [Array(19).fill(0), { 0: 1 },
    Object.fromEntries(regions.map(item => [item.id, item.id === 0 ? 2 : 0])),
    Object.fromEntries(regions.map(item => [item.id, 1])),
    Object.fromEntries(regions.map(item => [item.id, item.id === 1 ? 1 : 0]))]) {
    const b = browser(), ws = b.join(); ws.receive(snapshot({ region_counts }));
    assert.equal(b.client.snapshot, null); assert.equal(b.client.connected, false);
  }
});

test('acknowledgements and authoritative timestamps cannot move backwards or acknowledge unsent controls', () => {
  for (const changes of [{ players: [ship({ ack_seq: 2 })] },
    { players: [ship({ ack_seq: 100 })] }, { server_time_ms: BASE_TIME - 1001 }, { total_players: 2 }]) {
    const b = browser(), ws = b.join(); ws.receive(snapshot(changes));
    assert.equal(b.client.connected, false); assert.equal(b.client.self, null);
  }
});

test('actual server event ids and code-only recoverable errors are accepted without exposing raw frames', () => {
  const b = browser(), ws = b.join();
  ws.receive(snapshot({ events: [{ id: 'evt_1', type: 'laser', player_id: SELF_ID,
    region: 0, position: [1, 2, 3], end: [1, 2, -57], at_ms: BASE_TIME }] }));
  assert.equal(b.client.snapshot.events[0].id, 'evt_1');
  ws.receive({ v: 1, type: 'error', code: 'region_unavailable' });
  assert.equal(b.client.connected, true); assert.equal(b.errors.length, 1);
  assert.ok(b.errors[0].includes('燃料'));
});

test('view callback exceptions do not turn a good server frame into a protocol failure', () => {
  const fail = () => { throw new Error('synthetic view failure'); };
  const b = browser({ callbackOverrides: { onStatus: fail, onHello: fail, onSnapshot: fail } }), ws = b.join();
  ws.receive(snapshot()); assert.equal(b.client.connected, true); assert.equal(b.errors.length, 0);
  b.pagehide(); assert.equal(b.client.connected, false);
});

const field = (changes = {}) => ({version:1,seed:'0'.repeat(32),count:2300,radius:4.5,fuel:4,
  respawn_ms:45000,revision:0,taken:Buffer.alloc(288).toString('base64'),...changes});
test('shared collectible bitmap and collection events stay bounded and authoritative', () => {
  const b=browser(), ws=b.join(hello({collectibles:field(),self:ship({collected:0,dash_until_ms:0})}));
  const mask=Buffer.alloc(288); mask[0]=1; mask[287]=8;
  const event={id:'evt_2',type:'collect',player_id:SELF_ID,at_ms:BASE_TIME,glyph_ids:[0,2299],fuel_added:8,collected:2};
  ws.receive(snapshot({collectibles:field({revision:1,taken:mask.toString('base64')}),state_time_ms:BASE_TIME,
    players:[ship({collected:2,dash_until_ms:BASE_TIME+1000})],events:[event]}));
  assert.equal(b.client.connected,true); assert.equal(b.client.self.collected,2);
  assert.equal(b.client.self.dash_until_ms,BASE_TIME+1000);
  assert.equal(b.client.snapshot.collectibles.mask[0],1); assert.equal(b.client.snapshot.collectibles.mask[287],8);
  assert.ok(Object.isFrozen(b.client.snapshot.collectibles.mask));
  assert.deepEqual(plain(b.client.snapshot.events[0].glyph_ids),[0,2299]);
  assert.equal(b.client.snapshot.events[0].fuel_added,8);
  const frames=ws.sent.length; assert.equal(b.client.setInput({glyph_ids:[0],fuel:100,collected:999}),false);
  assert.equal(ws.sent.length,frames+1); assert.equal(ws.sent.at(-1).type,'input');
  assert.equal('glyph_ids' in ws.sent.at(-1),false);
});

test('malformed collectible metadata, unused mask bits and collection claims fail closed', () => {
  const badMask=Buffer.alloc(288); badMask[287]=16;
  for (const changes of [{count:2301},{seed:'user-selected-seed'},{radius:100},{taken:''},
    {taken:badMask.toString('base64')},{taken:Buffer.alloc(289).toString('base64')},{respawn_ms:0}]) {
    const b=browser(); b.join(hello({collectibles:field(changes)})); assert.equal(b.client.connected,false);
  }
  for (const event of [{glyph_ids:[2300],fuel_added:4,collected:1},{glyph_ids:[0,0],fuel_added:8,collected:2},
    {glyph_ids:Array.from({length:33},(_,i)=>i),fuel_added:100,collected:33},
    {glyph_ids:[0],fuel_added:-1,collected:1},{glyph_ids:[0],fuel_added:4,collected:-1}]) {
    const b=browser(),ws=b.join(hello({collectibles:field()}));
    ws.receive(snapshot({collectibles:field(),events:[{id:'evt_1',type:'collect',player_id:SELF_ID,at_ms:BASE_TIME,...event}]}));
    assert.equal(b.client.connected,false);
  }
});

test('late bitmap revisions cannot rewind a field; live revisions and physical times cannot go backwards', () => {
  const b=browser(),ws=b.join(hello({collectibles:field()}));
  ws.receive(snapshot({tick:2,server_time_ms:BASE_TIME+200,state_time_ms:BASE_TIME+180,collectibles:field({revision:1})}));
  ws.receive(snapshot({tick:1,state_time_ms:BASE_TIME,collectibles:field()}));
  assert.equal(b.client.connected,true); assert.equal(b.client.snapshot.collectibles.revision,1);
  ws.receive(snapshot({tick:3,server_time_ms:BASE_TIME+400,state_time_ms:BASE_TIME+380,collectibles:field()}));
  assert.equal(b.client.connected,false);
  const c=browser(),socket=c.join();
  socket.receive(snapshot({state_time_ms:BASE_TIME+1,server_time_ms:BASE_TIME}));
  assert.equal(c.client.connected,false);
});

test('backpressure reconnect rebinds seed and bitmap without stale pickups or resource claims', () => {
  const b=browser(),ws=b.join(hello({collectibles:field()}));
  ws.bufferedAmount=16385; b.clock.advance(50); assert.equal(b.client.connected,false);
  assert.equal(b.client.self,null); assert.equal(b.client.snapshot,null);
  b.clock.advance(500); const next=b.sockets.at(-1); next.open();
  next.receive(hello({collectibles:field({seed:'1'.repeat(32),revision:9}),self:ship({collected:12})}));
  assert.equal(b.hellos.at(-1).collectibles.seed,'1'.repeat(32)); assert.equal(b.client.self.collected,12);
  b.clock.advance(50); assert.equal('collected' in next.sent.at(-1),false);
});
