/* Authoritative server coordinates and bounded, deterministic author rings. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
require('../../src/msg/data/root-web-model.js');
const M = globalThis.MSGUniverse;
const user = (id, position, count = 0, orbit = {}) => ({ id, name: id, star: {
  layout: { version: 4, position, orbit: { kind: 'orbit', center: [0, 0, 0], parent_id: 'u_root',
    source_type: 'default', group_id: null, members: [id], radius: 100, phase: 0, tilt: 0, period: 1, ...orbit } },
  post_count: { public: count, exact: count !== null, scanned: count || 0 },
} });
const posts = (author, count) => Array.from({ length: count }, (_, i) => ({ id: 'p_' + i, author: { id: author }, title: 'visible ' + i }));
const byId = graph => new Map(graph.nodes.map(node => [node.id, node]));
const distance = (a, b) => Math.hypot(...a.map((v, i) => v - b[i]));
const close = (a, b, tolerance = 1e-9) => assert.ok(Math.abs(a - b) <= tolerance, `${a} != ${b}`);

test('v4 server positions are shared by navigation, graph and orbital metadata without time drift', () => {
  const center = [140.125, 23.5, -82.25];
  const stars = [user('u_root', [99, 99, 99]), user('u_a', center, 2)];
  const first = byId(M.graph(stars, posts('u_a', 2), 0, 'u_a'));
  const later = byId(M.graph([...stars].reverse(), posts('u_a', 2).reverse(), 100000, 'u_a'));
  assert.deepEqual(M.starPosition(stars[1]), center);
  assert.deepEqual(M.position(stars[1]), center);
  assert.deepEqual(first.get('u_root').position, [0, 0, 0]);
  assert.deepEqual(first.get('u_a').position, center);
  assert.equal(first.get('u_a').layoutVersion, 4);
  for (const [id, node] of first) assert.deepEqual(node.position, later.get(id).position);
  first.get('u_a').position[0] = 900;
  assert.equal(stars[1].star.layout.position[0], center[0]);
});

test('unknown, unsupported and invalid server layouts retain stable v3 compatibility', () => {
  for (const layout of [null, { version: 3, position: [1, 2, 3] },
    { version: 4, position: [Infinity, 0, 0] }, { version: 4, position: [481, 0, 0] }]) {
    const star = { id: 'u_old', star: { layout } };
    assert.deepEqual(M.starPosition(star), M.position('u_old'));
    assert.equal(M.graph([star], []).nodes[0].layoutVersion, 3);
  }
});

test('true public post count grows the ring monotonically with explicit caps and multiple planes-free lanes', () => {
  let previous = 0;
  for (const count of [0, 1, 2, 5, 12, 24, 50, 96, 192, 384, 768, 10000, 1000000000]) {
    const ring = M.postRing(user('u_a', [30, 0, 50], count), 24);
    assert.equal(ring.count, count);
    assert.ok(ring.radius >= previous && ring.radius <= 24);
    assert.ok(ring.ring_radii.length >= 1 && ring.ring_radii.length <= 4);
    assert.ok(ring.ring_radii.every(radius => radius >= 8 && radius <= ring.radius));
    previous = ring.radius;
  }
  assert.equal(M.postRing(user('u_a', [30, 0, 50], 10000)).ring_radii.length, 4);
});

test('a loaded 24-post page never pretends to be the author total', () => {
  const star = user('u_a', [80, 0, 80], null);
  const first = M.graph([star], posts('u_a', 1), 0, 'u_a');
  const next = M.graph([star], posts('u_a', 24), 0, 'u_a');
  assert.equal(next.rings[0].count, null);
  assert.equal(next.rings[0].known, false);
  assert.equal(next.rings[0].loaded_count, 24);
  assert.equal(first.rings[0].radius, next.rings[0].radius);
  assert.deepEqual(byId(first).get('p_0').position, byId(next).get('p_0').position);
});

test('posts all occupy their author plane and narrow deterministic ID phases, independent of page order', () => {
  const star = user('u_a', [100, 20, 100], 10000), content = posts('u_a', 1000);
  const first = M.graph([star], content, 0, 'u_a');
  const reverse = M.graph([star], [...content].reverse(), 9000, 'u_a');
  const nodes = first.nodes.filter(node => node.orbitCenter);
  assert.ok(nodes.length > 100 && nodes.length <= 160);
  assert.deepEqual(nodes.map(node => node.id), reverse.nodes.filter(node => node.orbitCenter).map(node => node.id));
  const bins = new Set();
  for (const node of nodes) {
    const orbit = node.orbit, displacement = node.position.map((v, i) => v - star.star.layout.position[i]);
    close(displacement.reduce((sum, v, i) => sum + v * orbit.normal[i], 0), 0);
    close(Math.hypot(...displacement), orbit.radius);
    const slot = orbit.lane + ':' + orbit.slot;
    assert.ok(!bins.has(slot)); bins.add(slot);
    close(orbit.phase / M.TAU * 64 - orbit.slot - .5, 0, .101);
    assert.deepEqual(M.satellite(node.id, node.orbitCenter, 99999, orbit), node.position);
  }
  for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
    assert.ok(distance(nodes[i].position, nodes[j].position) > 1.3);
  }
  assert.equal(first.rings[0].loaded_count, 1000);
  assert.equal(first.rings[0].displayed_count, nodes.length);
  assert.equal(first.rings[0].count, 10000);
});

test('selected real posts survive LOD and no synthetic posts are added', () => {
  const content = posts('u_a', 1000), chosen = 'p_999';
  const graph = M.graph([user('u_a', [100, 20, 100], 1000)], content, 0, chosen);
  assert.ok(graph.nodes.some(node => node.id === chosen));
  assert.ok(graph.nodes.every(node => node.id === 'u_a' || content.some(post => post.id === node.id)));
});

test('binary and multi descriptors keep authoritative centers rather than recomputing a partial SCC', () => {
  const barycenter = [160, 0, 50];
  const a = user('u_a', [128, 0, 50], 40, { kind: 'binary', center: barycenter, group_id: 'ab', members: ['u_a', 'u_b'], source_type: 'explicit' });
  const b = user('u_b', [192, 0, 50], 40, { kind: 'binary', center: barycenter, group_id: 'ab', members: ['u_a', 'u_b'], source_type: 'explicit' });
  const partial = M.graph([a], []), complete = M.graph([b, a], []);
  assert.deepEqual(partial.nodes[0].position, byId(complete).get('u_a').position);
  assert.deepEqual(partial.nodes[0].orbit.center, barycenter);
  assert.equal(distance(byId(complete).get('u_a').position, byId(complete).get('u_b').position), 64);
  const multi = user('u_c', [160, 64, 50], 40, { kind: 'multi', center: barycenter, members: ['u_a', 'u_b', 'u_c'] });
  assert.deepEqual(M.graph([multi], []).nodes[0].orbit.members, multi.star.layout.orbit.members);
});

test('default Root attachments remain distinct from explicit signed follow and mutual edges', () => {
  const users = [user('u_root', [0, 0, 0]), user('u_a', [120, 0, 0]), user('u_b', [180, 0, 0])];
  const topology = { edges: [
    { source: 'u_a', target: 'u_root', source_type: 'default', mutual: true },
    { source: 'u_a', target: 'u_b', source_type: 'explicit', mutual: true },
    { source: 'u_b', target: 'u_a', source_type: 'explicit', mutual: true },
    { source: 'u_b', target: 'u_hidden', source_type: 'explicit' },
    { source: 'u_b', target: 'u_a', source_type: 'default' },
  ] };
  const graph = M.graph(users, [], 0, null, topology);
  assert.deepEqual(graph.links.map(link => link[2]), ['root-attachment', 'mutual']);
  assert.equal(graph.links[0][3].source_type, 'default');
  assert.ok(!graph.nodes.some(node => node.id === 'u_hidden'));
});

test('two authoritative explicit edges form a mutual pair even when legacy metadata lacks the flag', () => {
  const topology = { edges: [
    { source: 'u_a', target: 'u_b', source_type: 'explicit' },
    { source: 'u_b', target: 'u_a', source_type: 'explicit' },
    { source: 'u_root', target: 'u_b', source_type: 'explicit' },
  ] };
  const graph = M.graph([user('u_root', [0, 0, 0]), user('u_a', [120, 0, 0]), user('u_b', [180, 0, 0])], [], 0, null, topology);
  assert.deepEqual(graph.links.map(link => link[2]), ['mutual', 'follow']);
});

// Backend owns and regenerates this shared precision fixture. It can be passed
// explicitly during isolated worktree integration before its commit is merged.
const fixturePath = process.env.MSG_PLANET_FIXTURE || path.join(__dirname, '../fixtures/planet-layout-v4.json');
test('Python-generated planet layouts pass through JS unchanged across pages and render time', () => {
  const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
  assert.equal(fixture.version, M.LAYOUT_VERSION);
  for (const scenario of fixture.scenarios) {
    const users = scenario.nodes.map(id => ({ id, star: { layout: scenario.layout[id] } }));
    const graph = M.graph(users, [], 0, null, { edges: scenario.edges });
    for (const star of graph.nodes) {
      assert.deepEqual(star.position, scenario.layout[star.id].position);
      assert.deepEqual(M.starPosition(users.find(user => user.id === star.id)), star.position);
      assert.deepEqual(M.graph([users.find(user => user.id === star.id)], [], 86400).nodes[0].position, star.position);
      assert.deepEqual(star.orbit, scenario.layout[star.id].orbit);
    }
    const binary = graph.nodes.filter(star => star.orbit.kind === 'binary');
    if (binary.length === 2) close(distance(binary[0].position, binary[1].position), 64, 2e-6);
  }
});

test('public geometry excludes self-only counts, balances, private entries and private-message content', () => {
  const star = user('u_a', [100, 0, 80], 3);
  star.star.post_count.private_visible = 900;
  star.star.post_count.own = 903;
  star.star.balance = { visibility: 'self', amount_minor: '999999', scale: 6, code: 'MSG' };
  const privateUser = { ...user('u_secret', [200, 0, 80], 999), kind: 'private' };
  const content = [...posts('u_a', 1), { id: 'secret-post', author: { id: 'u_a' }, visibility: 'private' },
    { id: 'secret-message', author: { id: 'u_a' }, kind: 'private-message' }];
  const graph = M.graph([star, privateUser], content, 0, 'u_a');
  assert.equal(graph.rings[0].count, 3);
  const node = byId(graph).get('u_a');
  assert.equal(node.star.post_count.private_visible, undefined);
  assert.equal(node.star.post_count.own, undefined);
  assert.equal(node.star.balance.visibility, 'unavailable');
  assert.ok(!graph.nodes.some(node => node.id.startsWith('secret') || node.id === 'u_secret'));
  assert.equal(star.star.post_count.private_visible, 900);
});

test('a private ring stays in the current identity scope and uses its explicit personal center', () => {
  const star = { ...user('u_a', [300, 0, 80], 3), kind: 'private', position: [0, 0, 0] };
  star.star.post_count.private_visible = 1000;
  const mine = M.privateRing(star, 'u_a', 24), foreign = M.privateRing(star, 'u_else', 24);
  assert.equal(mine.count, 1000);
  assert.deepEqual(mine.center, [0, 0, 0]);
  assert.equal(foreign.count, null);
  assert.equal(M.postRing(star, 24).count, 3);
});
