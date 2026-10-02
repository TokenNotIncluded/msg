/* Geometry and interaction contracts, without a DOM-per-star or network. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
require('../../src/msg/data/root-web-model.js');
require('../../src/msg/data/root-web-map.js');
const M = globalThis.MSGUniverse, F = globalThis.MSGFlatMap;
const frames = new Map(); let frameId = 0;
globalThis.requestAnimationFrame = function (fn) {
  assert.equal(this, globalThis, 'native RAF requires its Window receiver');
  frames.set(++frameId, fn); return frameId;
};
globalThis.cancelAnimationFrame = function (id) {
  assert.equal(this, globalThis, 'native cancellation requires its Window receiver');
  frames.delete(id);
};
const flush = () => { for (const [id, fn] of [...frames]) { frames.delete(id); fn(); } };
const close = (a, b) => assert.ok(Math.abs(a - b) < 1e-7, `${a} != ${b}`);
const regions = Array.from({ length: 19 }, (_, id) => ({ id, name: 'sector-' + id,
  center: [(id % 5) * 75, 90 - id, Math.floor(id / 5) * 75], radius: 240 }));

class Element {
  constructor(document) {
    this.ownerDocument = document; this.listeners = new Map(); this.children = [];
    this.attributes = {}; this.textContent = ''; this.classList = { add() {} };
    this.dataset = {};
  }
  append(...children) { for (const child of children) { child.parentElement = this; this.children.push(child); } }
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(c => c !== this); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(key, fn) { if (!this.listeners.has(key)) this.listeners.set(key, new Set()); this.listeners.get(key).add(fn); }
  removeEventListener(key, fn) { this.listeners.get(key)?.delete(fn); }
  dispatch(key, props = {}) {
    const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...props };
    for (const fn of [...(this.listeners.get(key) || [])]) fn(event);
    return event;
  }
  focus() { this.ownerDocument.activeElement = this; this.dispatch('focus'); }
}
function setup() {
  const document = { createElement: () => new Element(document), activeElement: null };
  const canvas = new Element(document), container = new Element(document), status = new Element(document), actions = new Element(document);
  container.append(canvas, status, actions); status.id = 'region-map-status';
  const draws = [];
  canvas.context = new Proxy({}, { get(target, key) {
    if (key in target) return target[key];
    return (...args) => draws.push([key, ...args]);
  } });
  canvas.getContext = () => canvas.context;
  canvas.getBoundingClientRect = () => ({ left: 10, top: 20, width: 720, height: 480 });
  const captured = new Set();
  canvas.setPointerCapture = id => captured.add(id);
  canvas.hasPointerCapture = id => captured.has(id);
  canvas.releasePointerCapture = id => captured.delete(id);
  const selected = [], focused = []; let returned = 0;
  const map = new F.Map(canvas, { container, status, actions,
    onSelectRegion: id => selected.push(id), onRegionFocus: id => focused.push(id),
    onReturn3D: () => { returned++; } });
  map.update({ connected: true, fuel: 100, hp: 100, connectionMessage: '' });
  return { map, canvas, container, status, actions, selected, focused, draws, returned: () => returned };
}
const pointer = (canvas, type, id, x, y) => canvas.dispatch(type, {
  pointerId: id, clientX: x + 10, clientY: y + 20, button: 0 });

test('X/Z projection round trips at different aspect ratios and zoom levels', () => {
  for (const scale of [.0001, .4, 18]) for (const size of [{ width: 320, height: 280 }, { width: 1440, height: 480 }]) {
    const view = { x: -97, z: 181, scale };
    for (const p of [[0, 0, 0], [-480, 999, 480], [480, -999, -480]]) {
      const back = F.unproject(F.project(p, view, size), view, size);
      close(back[0], p[0]); close(back[2], p[2]); assert.equal(back[1], 0);
    }
  }
});
test('only real public stars are charted at the supplied authoritative position', () => {
  const actual = { id: 'u_1', kind: 'user', position: [29, 11, -73] };
  const graph = { nodes: [actual, { ...actual }, { id: 'secret', kind: 'private', position: [0, 0, 0] },
    { id: 'post', kind: 'post', position: [0, 0, 0] }, { id: 'bad', kind: 'user', position: [NaN, 1, 2] },
    { id: 'overflow', kind: 'user', position: [1e300, 1, 2] }] };
  const stars = F.publicStars(graph); assert.equal(stars.length, 1);
  assert.deepEqual(stars[0].position, actual.position);
  stars[0].position[0] = 77; assert.equal(actual.position[0], 29);
});
test('region geometry uses only valid server regions and keeps unknown counts unknown', () => {
  const actual = F.regionGeometry([regions[2], regions[0], regions[2],
    { ...regions[1], radius: -1 }, { ...regions[1], id: 19 }, { ...regions[1], center: [0, Infinity, 0] }]);
  assert.deepEqual(actual.map(r => r.id), [0, 2]);
  assert.deepEqual(actual[1].center, regions[2].center);
  assert.equal(F.regionPopulation(actual[0], null), null);
  assert.equal(F.regionPopulation(actual[0], {}), null);
  assert.equal(F.regionPopulation(actual[0], { 0: 0 }), 0);
  assert.equal(F.regionPopulation(actual[0], { 0: -3 }), null);
  assert.equal(F.regionPopulation(actual[0], Object.create({ 0: 99 })), null);
});
test('large loaded populations stay within the draw budget without losing aggregate members', () => {
  const nodes = Array.from({ length: 20000 }, (_, i) => ({ id: 'u_' + i, kind: 'user', position: M.position('u_' + i) }));
  const index = new M.SpatialIndex(nodes), view = { x: 0, z: 0, scale: .4 }, size = { width: 720, height: 480 };
  for (const budget of [1, 8, 64, 192]) {
    const cells = F.visibleStars(index, view, size, budget);
    assert.ok(cells.length <= budget);
    assert.equal(cells.reduce((n, c) => n + (c.count || 1), 0), nodes.length);
  }
  const dense = new M.SpatialIndex(Array.from({ length: 50000 }, (_, i) => ({ id: 'u_' + i, position: [0, i % 2, 0] })));
  const cells = F.visibleStars(dense, view, size); assert.ok(cells.length <= 192);
  assert.equal(cells.reduce((n, c) => n + (c.count || 1), 0), 50000);
});
test('culling checks X and Z viewport bounds rather than Y height', () => {
  const index = new M.SpatialIndex([{ id: 'above', position: [0, 90000, 0] }]);
  const view = { x: 0, z: 0, scale: 1 }, size = { width: 320, height: 200 };
  assert.equal(F.visibleStars(index, view, size)[0].id, 'above');
  for (const position of [[1000, 0, 0], [-1000, 0, 0], [0, 0, 1000], [0, 0, -1000]]) {
    assert.deepEqual(F.visibleStars(new M.SpatialIndex([{ id: 'outside', position }]), view, size), []);
  }
});
test('region callbacks request a jump while current region and player position wait for the server', () => {
  const t = setup(), player = { id: 'ship', position: [11, 7, 19], region: 2 };
  t.map.update({ regions, currentRegion: 2, players: [player], selfId: 'ship', regionCounts: { 0: 3, 2: 1 } });
  t.map.buttons.get(0).dispatch('click'); assert.deepEqual(t.selected, [0]);
  assert.equal(t.map.currentRegion, 2); assert.deepEqual(t.map.players[0].position, player.position);
  assert.equal(t.map.buttons.get(2).attributes['aria-pressed'], 'true');
  assert.equal(t.map.buttons.get(0).attributes['aria-pressed'], 'false');
  assert.match(t.map.buttons.get(1).textContent, /人数未知/);
  t.map.update({ currentRegion: 0 }); assert.equal(t.map.buttons.get(0).attributes['aria-pressed'], 'true');
  t.map.destroy();
});
test('partial snapshots preserve stars and regions; explicit empty data clears them', () => {
  const t = setup(), graph = { nodes: [{ id: 'u_1', kind: 'user', position: [55, 66, 77] }] };
  t.map.update({ graph, regions, currentRegion: 0, regionReadyMs: 103500, serverTimeMs: 100000 });
  const index = t.map.index;
  t.map.update({ players: [{ id: 'guest', position: [0, 0, 0] }], regionCounts: { 0: 1 } });
  assert.equal(t.map.index, index); assert.equal(t.map.regions.length, 19);
  assert.match(t.status.textContent, /冷却 4 秒/);
  assert.match(t.status.textContent, /1 颗已加载公开星点/);
  t.map.update({ serverTimeMs: 104000 }); assert.doesNotMatch(t.status.textContent, /冷却/);
  t.map.update({ graph: null, players: [], regions: [], currentRegion: null, regionCounts: null });
  assert.equal(t.map.stars.length, 0); assert.equal(t.map.players.length, 0); assert.equal(t.map.buttons.size, 0);
  assert.match(t.status.textContent, /等待服务器区域数据/); t.map.select(0); assert.deepEqual(t.selected, []);
  t.map.destroy();
});
test('pointer taps select real markers; drag, cancellation and pinch never request jumps', () => {
  const t = setup(); t.map.update({ regions, currentRegion: 2 }); t.map.show();
  pointer(t.canvas, 'pointerdown', 1, 360, 240); pointer(t.canvas, 'pointerup', 1, 360, 240);
  assert.deepEqual(t.selected, [0]); t.selected.length = 0;
  t.map.update({ currentRegion: 0 }); t.map.update({ currentRegion: 2 });
  pointer(t.canvas, 'pointerdown', 2, 360, 240); pointer(t.canvas, 'pointermove', 2, 380, 240);
  pointer(t.canvas, 'pointermove', 2, 360, 240); pointer(t.canvas, 'pointerup', 2, 360, 240);
  assert.deepEqual(t.selected, []);
  pointer(t.canvas, 'pointerdown', 3, 360, 240); pointer(t.canvas, 'pointercancel', 3, 360, 240);
  assert.deepEqual(t.selected, []);
  pointer(t.canvas, 'pointerdown', 4, 360, 240); pointer(t.canvas, 'pointerdown', 5, 400, 240);
  pointer(t.canvas, 'pointermove', 5, 450, 240); pointer(t.canvas, 'pointerup', 5, 450, 240);
  pointer(t.canvas, 'pointerup', 4, 360, 240); assert.deepEqual(t.selected, []);
  assert.ok(t.map.view.scale > t.map.limits().fit); t.map.destroy();
});
test('keyboard regions, zoom anchor, panning, and returning from a focused button work', () => {
  const t = setup(); t.map.update({ regions, currentRegion: 0 }); t.map.show();
  t.canvas.dispatch('keydown', { key: 'ArrowRight' }); assert.equal(t.map.focusedRegion, 1);
  assert.match(t.status.textContent, /选择 区域 01（人数未知）/);
  t.canvas.dispatch('keydown', { key: 'Enter' }); assert.deepEqual(t.selected, [1]); assert.equal(t.map.currentRegion, 0);
  const anchor = [121, 219], before = F.unproject(anchor, t.map.view, t.map.size);
  t.map.zoom(5, anchor); const after = F.unproject(anchor, t.map.view, t.map.size);
  close(before[0], after[0]); close(before[2], after[2]);
  const prior = t.map.view.x; t.canvas.dispatch('keydown', { key: 'ArrowRight', shiftKey: true }); assert.ok(t.map.view.x > prior);
  t.canvas.dispatch('keydown', { key: 'Home' }); assert.equal(t.map.view.x, 0); assert.equal(t.map.view.z, 0);
  t.map.zoom(18);
  t.map.buttons.get(4).focus(); assert.equal(t.map.focusedRegion, 4);
  const pixel = F.project(regions[4].center, t.map.view, t.map.size);
  assert.ok(pixel[0] >= 36 && pixel[0] <= t.map.size.width - 36);
  assert.ok(pixel[1] >= 36 && pixel[1] <= t.map.size.height - 36);
  t.container.dispatch('keydown', { key: 'Escape' }); assert.equal(t.returned(), 1); assert.equal(t.container.hidden, true);
  t.map.destroy();
});
test('bounded canvas drawing keeps Root and own ship distinct and creates only fixed controls', () => {
  const t = setup();
  const nodes = Array.from({ length: 10000 }, (_, i) => ({ id: 'u_' + i, kind: 'user', position: [i % 100, 0, Math.floor(i / 100)] }));
  nodes.push({ id: 'u_root', kind: 'user', position: [0, 0, 0] });
  const players = Array.from({ length: 96 }, (_, i) => ({ id: 'ship_' + i, position: [0, 0, 0] }));
  t.map.update({ graph: { nodes }, regions, currentRegion: 0, players, selfId: 'ship_0' });
  t.map.show(); flush();
  assert.ok(t.draws.filter(d => d[0] === 'arc').length <= 195);
  assert.ok(t.draws.some(d => d[0] === 'fillText' && d[1] === 'Root'));
  assert.ok(t.draws.some(d => d[0] === 'fillText' && d[1] === '你'));
  assert.equal(t.map.playerIndex.root.count, 95);
  assert.equal(t.map.regionList.children.length, 19); assert.equal(t.map.tools.children.length, 4);
  t.map.hide(); const before = t.draws.length; t.map.update({ players }); flush(); assert.equal(t.draws.length, before);
  t.map.destroy(); assert.equal(t.actions.children.length, 0); assert.equal(t.canvas.listeners.get('pointerup').size, 0);
  t.canvas.dispatch('keydown', { key: 'Enter' }); assert.deepEqual(t.selected, []);
});

test('disconnection removes live positions and counts and blocks every region selection path', () => {
  const t = setup(), graph = { nodes: [{ id: 'u_1', kind: 'user', position: [55, 66, 77] }] };
  t.map.update({ graph, regions, currentRegion: 2, selfId: 'ship',
    players: [{ id: 'ship', position: [11, 7, 19] }], regionCounts: { 2: 1 } }); t.map.show();
  t.map.update({ connected: false, connectionMessage: '连接已断开，返回飞行重新连接。' });
  assert.equal(t.map.currentRegion, null); assert.equal(t.map.counts, null);
  assert.equal(t.map.selfId, null); assert.deepEqual(t.map.players, []);
  assert.equal(t.map.stars.length, 1); assert.equal(t.map.regions.length, 19);
  assert.match(t.status.textContent, /实时玩家未连接.*连接已断开/);
  assert.doesNotMatch(t.status.textContent, /1 人在线|1 个实时玩家/);
  assert.equal(t.status.attributes['data-state'], 'disconnected');
  assert.ok([...t.map.buttons.values()].every(b => b.disabled));
  t.map.buttons.get(0).dispatch('click'); t.map.select(1);
  t.canvas.dispatch('keydown', { key: 'Enter' }); assert.deepEqual(t.selected, []);
  assert.equal(t.map.pendingRegion, null); assert.equal(t.map.visible, true); t.map.destroy();
});

test('region requests stay pending until their authoritative region arrives, and rejection stays in the map', () => {
  const t = setup(), confirmed = []; t.map.options.onRegionConfirmed = id => confirmed.push(id);
  t.map.update({ regions, currentRegion: 2, connectionMessage: '已暂停控制，仍可选择区域。' }); t.map.show();
  t.map.select(0); assert.deepEqual(t.selected, [0]); assert.equal(t.map.currentRegion, 2);
  assert.equal(t.map.visible, true); assert.equal(t.map.pendingRegion, 0);
  assert.match(t.status.textContent, /等待服务器确认进入区域 00/);
  t.map.select(1); assert.deepEqual(t.selected, [0]); assert.equal(t.map.pendingRegion, 0);
  t.map.update({ currentRegion: 1 }); assert.deepEqual(confirmed, []); assert.equal(t.map.pendingRegion, 0);
  t.map.rejectRegion('服务器拒绝切区，请检查燃料。');
  assert.equal(t.map.pendingRegion, null); assert.equal(t.map.visible, true);
  assert.match(t.status.textContent, /服务器拒绝切区/); assert.equal(t.status.attributes['data-state'], 'error');
  t.map.select(0); assert.deepEqual(t.selected, [0, 0]);
  t.map.update({ currentRegion: 0 }); assert.deepEqual(confirmed, [0]);
  assert.equal(t.map.pendingRegion, null); assert.equal(t.map.buttons.get(0).disabled, true);
  assert.equal(t.map.buttons.get(2).disabled, false); t.map.destroy();
});

test('fuel, cooldown, respawn and the current region explain why a jump cannot be requested', () => {
  for (const [changes, target, reason] of [
    [{ fuel: 19.9 }, 0, /燃料不足/], [{ hp: 0 }, 0, /重生后/],
    [{ regionReadyMs: 103500, serverTimeMs: 100000 }, 0, /冷却 4 秒/], [{}, 2, /已在此区域/],
  ]) {
    const t = setup(); t.map.update({ regions, currentRegion: 2, ...changes });
    t.map.select(target); assert.deepEqual(t.selected, []); assert.equal(t.map.pendingRegion, null);
    assert.equal(t.map.buttons.get(target).disabled, true); assert.match(t.status.textContent, reason);
    t.map.destroy();
  }
  const t = setup(); t.map.update({ regions, currentRegion: 2, fuel: 20, regionReadyMs: 100000, serverTimeMs: 100000 });
  t.map.select(0); assert.deepEqual(t.selected, [0]); t.map.destroy();
});

function appFlightMap() {
  const fs = require('node:fs'), vm = require('node:vm');
  const source = fs.readFileSync(require('node:path').join(__dirname, '../../src/msg/data/root-web-app.js'), 'utf8');
  const clientStart = source.indexOf('  if (globalThis.MSGFlightClient?.Client) {');
  const clientEnd = source.indexOf('  renderer = new globalThis.MSGUniverseRenderer', clientStart);
  const mapStart = source.indexOf('  if (globalThis.MSGFlatMap?.Map) {', clientEnd);
  const mapEnd = source.indexOf('  $("pause").setAttribute', mapStart);
  assert.ok(clientStart >= 0 && clientEnd > clientStart && mapStart > clientEnd && mapEnd > mapStart);
  const t = setup(), elements = new Map([['region-map-canvas', t.canvas], ['region-map', t.container],
    ['region-map-status', t.status], ['region-map-actions', t.actions]]), notices = [];
  const $ = id => { if (!elements.has(id)) elements.set(id, new Element(t.canvas.ownerDocument)); return elements.get(id); };
  const renderer = { flight: true, clearCount: 0, clearRemoteShips() { this.clearCount++; },
    stopFlightInput() {}, updateGameHud() {}, receiveFlightHello() {}, receiveFlightSnapshot() {} };
  const context = { $, renderer, notice: text => notices.push(text),
    MSGFlatMap: { Map: class { constructor(_, options) { t.map.options = options; return t.map; } } },
    MSGFlightClient: { Client: class {
      constructor(handlers) { this.handlers = handlers; this.connected = true; this.sent = []; this.resumed = 0; this.sendResult = true;
        this.self = { id: 'ship', region: 2, fuel: 100, hp: 100, region_ready_ms: 0 }; this.serverNow = 100000; }
      chooseRegion(id) { this.sent.push(id); return this.sendResult; }
      resume() { this.resumed++; }
    } },
  };
  vm.runInNewContext('let flightClient, flatMap;\n' + source.slice(clientStart, clientEnd) + source.slice(mapStart, mapEnd) +
    '\nglobalThis.client = flightClient;', context);
  const client = context.client;
  client.handlers.onHello({ self: client.self, region: 2, regions });
  const snapshot = region => ({ players: [{ ...client.self, region, position: [11, 7, 19] }], self_id: 'ship', region,
    region_counts: { [region]: 1 } });
  client.handlers.onSnapshot(snapshot(2));
  return { ...t, client, renderer, $, notices, snapshot };
}

test('app keeps connected pause usable but marks a lost pause disconnected and clears live map data', () => {
  const t = appFlightMap(); t.map.show(); t.client.handlers.onStatus('suspended');
  assert.equal(t.map.connected, true); assert.equal(t.map.currentRegion, 2); assert.equal(t.map.players.length, 1);
  assert.equal(t.map.buttons.get(0).disabled, false);
  assert.equal(t.$('game-connection').textContent, '已暂停');
  t.client.connected = false; t.client.self = null; t.client.handlers.onStatus('suspended');
  assert.equal(t.map.connected, false); assert.deepEqual(t.map.players, []); assert.equal(t.map.counts, null);
  assert.equal(t.$('game-connection').dataset.state, 'disconnected');
  assert.match(t.$('game-connection').textContent, /已断开/); assert.match(t.status.textContent, /返回飞行重新连接/);
  t.map.select(0); assert.deepEqual(t.client.sent, []); assert.equal(t.renderer.clearCount, 1);
  t.client.connected = true; t.client.self = { id: 'ship', region: 2, fuel: 100, hp: 100, region_ready_ms: 0 };
  t.client.handlers.onStatus('connected'); t.client.handlers.onHello({ self: t.client.self, region: 2, regions });
  t.client.handlers.onSnapshot(t.snapshot(2));
  assert.equal(t.map.currentRegion, 2); assert.equal(t.map.players.length, 1); assert.equal(t.map.buttons.get(0).disabled, false);
  assert.doesNotMatch(t.status.textContent, /已断开|未连接/); t.map.destroy();
});

test('app shows recoverable rejection inside the open map and returns to flight only after server confirmation', () => {
  const t = appFlightMap(); t.map.show(); t.map.select(0);
  assert.deepEqual(t.client.sent, [0]); assert.equal(t.map.visible, true); assert.equal(t.client.resumed, 0);
  t.client.handlers.onSnapshot(t.snapshot(2)); assert.equal(t.map.visible, true);
  t.client.handlers.onError('区域暂不可用，请检查燃料。');
  assert.match(t.status.textContent, /区域暂不可用/); assert.deepEqual(t.notices, []);
  assert.equal(t.map.visible, true); assert.equal(t.map.pendingRegion, null);
  t.map.select(1); assert.deepEqual(t.client.sent, [0, 1]);
  t.client.self = { ...t.client.self, region: 1, fuel: 80, region_ready_ms: 110000 };
  t.client.handlers.onSnapshot(t.snapshot(1)); assert.equal(t.map.visible, false); assert.equal(t.client.resumed, 1);
  assert.equal(t.map.currentRegion, 1); assert.equal(t.map.pendingRegion, null); t.map.destroy();
});

test('failed sends stay in the map and a late server confirmation cannot steal focus after returning manually', () => {
  const t = appFlightMap(); t.map.show(); t.client.sendResult = false; t.map.select(0);
  assert.equal(t.map.pendingRegion, null); assert.equal(t.map.visible, true); assert.match(t.status.textContent, /未发出/);
  assert.equal(t.client.resumed, 0); t.client.sendResult = true; t.map.select(1);
  assert.equal(t.map.pendingRegion, 1); t.map.return3D(); assert.equal(t.client.resumed, 1);
  const focused = t.$('space'); t.canvas.ownerDocument.activeElement = t.$('other-control');
  t.client.self = { ...t.client.self, region: 1 }; t.client.handlers.onSnapshot(t.snapshot(1));
  assert.equal(t.map.currentRegion, 1); assert.equal(t.client.resumed, 1);
  assert.notEqual(t.canvas.ownerDocument.activeElement, focused); t.map.destroy();
});
