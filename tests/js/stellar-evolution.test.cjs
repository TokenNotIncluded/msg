/* Public window facts drive bounded visual stages; authoritative positions stay fixed. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
require('../../src/msg/data/root-web-model.js');
const M = globalThis.MSGUniverse;
const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-12, `${actual} != ${expected}`);
const NOW = Date.parse('2026-10-03T10:00:00Z');
const stamp = days => new Date(NOW + days * 86400000).toISOString();
const activity = (recent = 28, previous = 28, days = 7, previousDays = 7) => ({
  window_days: 14, window_end: stamp(0), recent_posts: recent, previous_posts: previous,
  active_days: days, previous_active_days: previousDays, exact: true,
});
const user = (id, facts = {}) => ({ id, name: id, kind: 'user', star: {
  checked_at: stamp(0), last_public_post_at: stamp(0),
  post_count: { public: 256, exact: true }, activity: activity(),
  layout: { version: 4, position: [120, 20, 40], orbit: { kind: 'orbit', parent_id: 'u_root' } },
  ...facts,
} });
const cold = (id, days = 30, facts = {}) => user(id, {
  last_public_post_at: stamp(-days), activity: activity(0, 0, 0, 0), ...facts,
});
const follow = (source, target, extra = {}) => ({ source, target, source_type: 'explicit', ...extra });
const context = (graph, node) => ({ following: node.relations.following.map(id => graph.nodes.find(other => other.id === id)) });

test('two public windows with multiple active days warm into a bounded star', () => {
  const style = M.stellarEvolution(user('u_active'), NOW);
  assert.equal(style.stage, 'star');
  assert.equal(style.known, true);
  assert.equal(style.approximate, false);
  close(style.heat, .85);
  assert.ok(style.temperature > 800 && style.temperature <= 11000);
  assert.ok(style.brightness > .9 && style.brightness <= 1);
  assert.deepEqual(M.appearance(user('u_active'), NOW).stellar, style);
  assert.deepEqual(M.stellarEvolution(user('u_active'), NOW), style);
});

test('lifetime counts and a single burst cannot impersonate sustained public activity', () => {
  for (const window of [activity(1000, 1000, 1, 1), activity(1000, 0, 7, 0), activity(0, 1000, 0, 7)]) {
    const node = user('u_burst', { post_count: { public: 10000, exact: true }, activity: window });
    assert.notEqual(M.stellarEvolution(node, NOW).stage, 'star');
  }
  const legacy = user('u_legacy', { activity: undefined });
  assert.equal(M.stellarEvolution(legacy, NOW).stage, 'warming');
  assert.equal(M.stellarEvolution(legacy, NOW).approximate, true);
});

test('confirmed stars require both count and active-day thresholds', () => {
  assert.equal(M.stellarEvolution(user('u_threshold', { activity: activity(14, 14, 4, 4) }), NOW).stage, 'star');
  for (const window of [activity(13, 14, 4, 4), activity(14, 13, 4, 4), activity(14, 14, 3, 4), activity(14, 14, 4, 3)]) {
    assert.notEqual(M.stellarEvolution(user('u_below', { activity: window }), NOW).stage, 'star');
  }
});

test('bounded, stale, future and unavailable facts never claim a cold satellite or star', () => {
  for (const facts of [
    { post_count: { public: null, exact: false, scanned: 4000 } },
    { post_count: { public: 256, exact: false } },
    { post_count: { public: -1, exact: true } },
    { last_public_post_at: 'invalid' },
    { last_public_post_at: stamp(1) },
    { checked_at: new Date(NOW - 90000).toISOString() },
    { checked_at: new Date(NOW + 1001).toISOString() },
  ]) {
    const style = M.stellarEvolution(cold('u_unknown', 30, facts), NOW, { following: [user('u_root')] });
    assert.equal(style.stage, 'unknown');
    assert.equal(style.known, false);
    assert.equal(style.parentId, null);
    assert.ok(Number.isFinite(style.heat) && Number.isFinite(style.temperature));
  }
});

test('malformed or old activity windows cannot certify sustained activity', () => {
  for (const window of [
    { ...activity(), exact: false }, { ...activity(), active_days: 8 },
    { ...activity(), recent_posts: null }, { ...activity(), recent_posts: 1, active_days: 7 },
    { ...activity(), window_days: 28 }, { ...activity(), window_end: 'invalid' },
    { ...activity(), window_end: stamp(-1) }, { ...activity(), recent_posts: 100000000 },
  ]) {
    const style = M.stellarEvolution(user('u_bad-window', { activity: window }), NOW);
    assert.notEqual(style.stage, 'star');
    assert.equal(style.approximate, true);
  }
});

test('public recency cools smoothly and presence contributes only within its self-reported TTL', () => {
  const samples = [0, 1, 2, 4, 7].map(days => M.stellarEvolution(user('u_cooling', { last_public_post_at: stamp(-days) }), NOW));
  for (let i = 1; i < samples.length; i++) {
    assert.ok(samples[i].heat < samples[i - 1].heat);
    assert.ok(samples[i].temperature < samples[i - 1].temperature);
    assert.ok(samples[i].brightness < samples[i - 1].brightness);
  }
  const presence = { state: 'available', self_reported: true,
    updated_at: new Date(NOW - 5000).toISOString(), expires_at: new Date(NOW + 1000).toISOString() };
  assert.equal(M.stellarEvolution(user('u_live', { presence }), NOW).heat, 1);
  close(M.stellarEvolution(user('u_expired', { presence: { ...presence, expires_at: stamp(0) } }), NOW).heat, .85);
  close(M.stellarEvolution(user('u_future', { presence: { ...presence, updated_at: stamp(1) } }), NOW).heat, .85);
  close(M.stellarEvolution(user('u_unreported', { presence: { ...presence, self_reported: false } }), NOW).heat, .85);
});

test('cold satellites use an existing loaded explicit outgoing follow and keep their server position', () => {
  const users = [cold('u_cold'), user('u_hot'), user('u_root')];
  const graph = M.graph(users, [], 0, null, { edges: [follow('u_cold', 'u_hot')] });
  const node = graph.nodes.find(node => node.id === 'u_cold');
  const style = M.stellarEvolution(node, NOW, context(graph, node));
  assert.equal(style.stage, 'satellite');
  assert.equal(style.parentId, 'u_hot');
  assert.ok(style.satelliteProgress > 0 && style.satelliteProgress < 1);
  assert.deepEqual(node.position, users[0].star.layout.position);
  assert.equal(M.appearance(node, NOW, context(graph, node)).stellar.parentId, 'u_hot');
  const later = M.stellarEvolution(cold('u_cold', 42), NOW, { following: [user('u_hot')] });
  assert.equal(later.satelliteProgress, 1);
});

test('default attachment, incoming fans, hidden targets and missing dates do not invent satellite follows', () => {
  const users = [cold('u_cold'), user('u_root'), user('u_hot')];
  for (const edges of [
    [follow('u_cold', 'u_root', { source_type: 'default' })],
    [follow('u_hot', 'u_cold')], [follow('u_cold', 'u_hidden')],
    [follow('u_cold', 'u_hot', { visibility: 'private' })],
  ]) {
    const graph = M.graph(users, [], 0, null, { edges });
    const node = graph.nodes.find(node => node.id === 'u_cold');
    assert.equal(M.stellarEvolution(node, NOW, context(graph, node)).stage, 'cooling');
  }
  const noDate = cold('u_no-date', 30, { last_public_post_at: null, post_count: { public: 0, exact: true } });
  assert.equal(M.stellarEvolution(noDate, NOW, { following: [user('u_root')] }).parentId, null);
  const legacyParent = user('u_old-hot', { activity: undefined });
  assert.equal(M.stellarEvolution(cold('u_cold'), NOW, { following: [legacyParent] }).parentId, null);
});

test('satellite parents are stable across order, prefer an eligible existing parent, and preserve partial SCCs', () => {
  const a = user('u_a'), b = user('u_b');
  const node = cold('u_cold');
  assert.equal(M.stellarEvolution(node, NOW, { following: [b, a] }).parentId, 'u_a');
  assert.deepEqual(M.stellarEvolution(node, NOW, { following: [b, a] }), M.stellarEvolution(node, NOW, { following: [a, b] }));
  const preferred = cold('u_cold', 30, { layout: { version: 4, position: [160, 0, 50], orbit: { kind: 'orbit', parent_id: 'u_b' } } });
  assert.equal(M.stellarEvolution(preferred, NOW, { following: [a, b] }).parentId, 'u_b');
  for (const kind of ['binary', 'multi']) {
    const binary = cold('u_cold', 30, { layout: { version: 4, position: [128, 0, 50],
      orbit: { kind, parent_id: 'u_a', members: ['u_cold', 'u_missing'], center: [160, 0, 50] } } });
    assert.equal(M.stellarEvolution(binary, NOW, { following: [a] }).stage, 'cooling');
    assert.deepEqual(M.graph([binary], []).nodes[0].position, [128, 0, 50]);
  }
});

test('Root stays the white anchor and an ordinary root name cannot claim it', () => {
  const root = user('u_root', { checked_at: 'invalid', post_count: { exact: false, public: null } });
  assert.equal(M.stellarEvolution(root, NOW).stage, 'anchor');
  assert.deepEqual(M.appearance(root, NOW).color, [1, .98, .94]);
  assert.equal(M.appearance(root, NOW).light, 1);
  assert.equal(M.stellarEvolution({ ...cold('u_fake'), name: 'root' }, NOW).stage, 'cooling');
});

test('loaded public follow and fan directions remain explicit, deduplicated and labelled partial', () => {
  const users = [user('u_a'), user('u_b'), user('u_c'), user('u_root')];
  const edges = [follow('u_a', 'u_b'), follow('u_b', 'u_a'), follow('u_a', 'u_b'),
    follow('u_c', 'u_a'), follow('u_c', 'u_root', { source_type: 'default' }), follow('u_a', 'u_unloaded')];
  const first = M.graph(users, [], 0, null, { edges });
  const second = M.graph([...users].reverse(), [], 9999, null, { edges: [...edges].reverse() });
  for (const graph of [first, second]) {
    assert.deepEqual(graph.nodes.find(node => node.id === 'u_a').relations,
      { following: ['u_b'], followers: ['u_b', 'u_c'], coverage: 'loaded-public', complete: false });
    const mutual = graph.links.find(link => link[2] === 'mutual');
    assert.deepEqual(mutual[3].directions, [{ source: 'u_a', target: 'u_b' }, { source: 'u_b', target: 'u_a' }]);
    assert.deepEqual(graph.links.find(link => link[2] === 'root-attachment')[3].directions, []);
  }
  const legacy = M.graph(users, [], 0, null, { edges: [follow('u_a', 'u_b', { mutual: true })] });
  assert.deepEqual(legacy.links[0][3].directions, [{ source: 'u_a', target: 'u_b' }]);
  assert.deepEqual(legacy.nodes.find(node => node.id === 'u_b').relations.following, []);
});

test('visible cross-author replies strengthen a bounded visual link without counting duplicate or private inputs', () => {
  const users = [user('u_a'), user('u_b')];
  const target = { id: 'p_target', author: { id: 'u_b' } };
  const reply = i => ({ id: 'p_reply_' + i, author: { id: 'u_a' }, reply_to: { id: target.id } });
  const one = M.graph(users, [target, reply(0)], 0, null, { edges: [follow('u_a', 'u_b')] });
  const content = [target, ...Array.from({ length: 80 }, (_, i) => reply(i)), reply(0),
    { ...reply('private'), visibility: 'private' }, { ...reply('dm'), kind: 'private-message' }];
  const many = M.graph(users, content, 0, null, { edges: [follow('u_a', 'u_b')] });
  const firstMeta = one.links.find(link => link[2] === 'follow')[3];
  const metadata = many.links.find(link => link[2] === 'follow')[3];
  assert.equal(firstMeta.interactionCount, 1);
  assert.ok(metadata.interactionWeight > firstMeta.interactionWeight);
  assert.equal(metadata.interactionCount, 64);
  assert.equal(metadata.interactionWeight, 1);
  assert.equal(metadata.interactionCapped, true);
  assert.equal(metadata.interactionExact, false);
  assert.equal(metadata.interactionCoverage, 'loaded-public');
  const duplicate = M.graph(users, [target, reply(0), reply(0)], 0, null, { edges: [follow('u_a', 'u_b')] });
  assert.equal(duplicate.links.find(link => link[2] === 'follow')[3].interactionCount, 1);
  const hiddenTarget = M.graph(users, [{ ...target, visibility: 'private' }, reply(0)], 0, null, { edges: [follow('u_a', 'u_b')] });
  assert.equal(hiddenTarget.links[0][3].interactionCount, 0);
  assert.deepEqual(content[0], target);
});
