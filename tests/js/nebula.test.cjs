/* Run with node --test tests/js/nebula.test.cjs. No server or network. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
require('../../src/msg/data/root-web-model.js');
const M = globalThis.MSGUniverse;
const NOW = Date.parse('2026-10-01T16:00:00Z');
const stamp = (seconds) => new Date(NOW + seconds * 1000).toISOString();
test('random scenery varies by visit without moving identities', () => {
  assert.notDeepEqual(M.nebula(40, 'visit-a'), M.nebula(40, 'visit-b'));
  const points = Array.from({ length: 4000 }, (_, i) => M.position('u_density_' + i));
  assert.ok(points.every(p => p.every(Number.isFinite) && Math.hypot(...p) >= 37.99));
  assert.ok(Math.max(...points.map(p => p[1])) > 120);
  assert.ok(Math.min(...points.map(p => p[1])) < -120);
  assert.ok(Math.max(...points.map(p => Math.hypot(...p))) > 250);
});
test('LOD has a hard budget and preserves aggregate population', () => {
  const nodes = Array.from({ length: 20000 }, (_, i) => ({ id: 'u_' + i, position: M.position('u_' + i) }));
  const index = new M.SpatialIndex(nodes);
  for (const budget of [1, 8, 64, 256]) {
    const visible = index.query((p, radius) => ({ visible: true, size: radius * 100 }), { budget, threshold: 0 });
    assert.ok(visible.length <= budget);
    assert.equal(visible.reduce((n, cell) => n + (cell.count || 1), 0), nodes.length);
    assert.equal(new Set(visible.map(n => n.id)).size, visible.length);
  }
  const distant = index.query(() => ({ visible: true, size: 1 }));
  assert.equal(distant.length, 1);
  assert.equal(distant[0].count, nodes.length);
  assert.deepEqual(index.query(() => ({ visible: false, size: 0 })), []);
});
test('closer sectors resolve into the same real nodes, never synthetic accounts', () => {
  const nodes = Array.from({ length: 20 }, (_, i) => ({ id: 'u_' + i, position: M.position('u_' + i) }));
  const visible = new M.SpatialIndex(nodes).query(() => ({ visible: true, size: 100 }), { threshold: 0 });
  assert.deepEqual(new Set(visible), new Set(nodes));
});
test('coincident density stays aggregated without unbounded refinement', () => {
  const nodes = Array.from({ length: 100000 }, (_, i) => ({ id: 'u_' + i, position: [0, 0, 0] }));
  const view = new M.SpatialIndex(nodes).query(() => ({ visible: true, size: Infinity }));
  assert.equal(view.length, 1);
  assert.equal(view[0].kind, 'cluster');
  assert.equal(view[0].count, nodes.length);
});
test('rolling window evicts old entries while keeping the open star', () => {
  const nodes = new Map(Array.from({ length: 10000 }, (_, i) => ['u_' + i, i]));
  M.trimMap(nodes, 1000, new Set(['u_0', 'u_2']));
  assert.equal(nodes.size, 1000);
  assert.ok(nodes.has('u_0') && nodes.has('u_2') && nodes.has('u_9999'));
  assert.equal(nodes.has('u_3'), false);
});
const user = (star = {}, id = 'u_test') => ({
  id, name: 'test', kind: 'user',
  star: { checked_at: stamp(0), ...star },
});
test('root is an identity, never a user-controlled name', () => {
  assert.equal(M.appearance(user({}, 'u_root'), NOW).root, true);
  assert.equal(M.appearance({ ...user(), name: 'root', star: { role: 'root' } }, NOW).root, false);
  assert.deepEqual(M.position('u_root'), [0, 0, 0]);
  assert.ok(Math.hypot(...M.position('u_test')) >= 34);
});
test('identity positions and token geometry are stable', () => {
  assert.deepEqual(M.position('u_ada'), M.position('u_ada'));
  assert.notDeepEqual(M.position('u_ada'), M.position('u_kei'));
  assert.deepEqual(M.nebula(20), M.nebula(20));
  assert.equal(M.nebula(20).length, 20 * 8);
  assert.ok(M.nebula(20).every(Number.isFinite));
});
test('a currently valid certificate affects structure and not just color', () => {
  const basic = M.appearance(user(), NOW);
  const certified = M.appearance(user({ certificate: { state: 'valid', expires_at: stamp(300) } }), NOW);
  assert.equal(certified.certified, true);
  assert.ok(certified.radius > basic.radius);
  assert.notDeepEqual(certified.color, basic.color);
  assert.ok(M.appearance(user({}, 'u_root'), NOW).radius > certified.radius * 2);
});
for (const state of ['none', 'unknown', 'revoked']) {
  test(`certificate ${state} does not earn a badge`, () => {
    assert.equal(M.appearance(user({ certificate: { state, expires_at: stamp(300) } }), NOW).certified, false);
  });
}
test('certificate expiration and stale observations remove badges', () => {
  assert.equal(M.appearance(user({ certificate: { state: 'valid', expires_at: stamp(0) } }), NOW).certified, false);
  assert.equal(M.appearance(user({ checked_at: stamp(-100), certificate: { state: 'valid', expires_at: stamp(300) } }), NOW).certified, false);
});
test('live self-reported presence lights up a star only within its TTL', () => {
  const active = user({ presence: { state: 'available', self_reported: true, updated_at: stamp(-5), expires_at: stamp(20) } });
  assert.ok(M.appearance(active, NOW).light > M.appearance(user(), NOW).light);
  assert.equal(M.appearance(active, NOW).presenceLabel, 'Available · self-reported');
  assert.equal(M.appearance(active, NOW + 21000).presenceLabel, 'Presence unknown');
});
test('future, missing and expired activity do not fake online status', () => {
  const bad = user({ presence: { state: 'available', updated_at: stamp(200), expires_at: stamp(500) } });
  assert.equal(M.appearance(bad, NOW).presenceLabel, 'Presence unknown');
  assert.equal(M.appearance(user(), NOW).presenceLabel, 'Presence unknown');
});
test('public posting recency fades smoothly without claiming online', () => {
  const recent = M.appearance(user({ last_public_post_at: stamp(-60) }), NOW);
  const old = M.appearance(user({ last_public_post_at: stamp(-86400 * 30) }), NOW);
  assert.ok(recent.light > old.light);
  assert.equal(recent.presenceLabel, 'Presence unknown');
  assert.ok(old.light >= .25);
});
test('public money is exact even above the JS safe integer limit', () => {
  const result = M.reserve({ visibility: 'public', amount_minor: '9223372036854775807', scale: 6, code: 'MSG' });
  assert.equal(result.label, '9,223,372,036,854.775807 MSG');
  assert.ok(result.fraction <= 1);
});
test('unknown money does not mean zero', () => {
  assert.equal(M.reserve({ visibility: 'private' }).known, false);
  assert.equal(M.reserve(null).known, false);
  assert.equal(M.reserve({ visibility: 'public', amount_minor: '0', scale: 6, code: 'MSG' }).label, '0 MSG');
});
test('smallest money units remain visible and huge amounts do not grow the star', () => {
  const money = (value) => ({ visibility: 'public', amount_minor: value, scale: 6, code: 'MSG' });
  assert.equal(M.reserve(money('1')).label, '0.000001 MSG');
  assert.ok(M.reserve(money('1')).fraction > 0);
  assert.equal(M.appearance(user({ balance: money('1') }), NOW).radius,
    M.appearance(user({ balance: money('9223372036854775807') }), NOW).radius);
});
for (const amount of ['-1', 'NaN', '1.2', '<script>', '9'.repeat(100)]) {
  test(`invalid money (${amount.slice(0, 8)}) is not drawn`, () => {
    assert.equal(M.reserve({ visibility: 'public', amount_minor: amount, scale: 6, code: 'MSG' }).known, false);
  });
}
test('owner-only balance cannot bleed into the public graph', () => {
  const u = user({ balance: { visibility: 'self', amount_minor: '123', scale: 6, code: 'MSG' } });
  assert.equal(M.appearance(u, NOW).reserve.known, false);
  assert.equal(M.appearance({ ...u, kind: 'private' }, NOW).reserve.known, true);
});
