/* Pure flight/scenery contracts. Run with node --test tests/js/flight.test.cjs. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
require('../../src/msg/data/root-web-model.js');
require('../../src/msg/data/root-web-renderer.js');
const { Flight, flightBasis, tokenNebula, planetMesh } = globalThis.MSGUniverseFlight;
const keys = (...values) => new Set(values);
const advance = (flight, seconds, input = keys(), bodies = [], reduced = false, hz = 60) => {
  for (let i = 0; i < Math.round(seconds * hz); i++) flight.step(1 / hz, input, bodies, reduced);
  return flight;
};
const close = (a, b, tolerance = 1e-8) => assert.ok(Math.abs(a - b) < tolerance, `${a} != ${b}`);

test('idle flight has no automatic forward motion', () => {
  const flight = advance(new Flight([10, 20, 30]), 2);
  assert.deepEqual(flight.position, [10, 20, 30]); assert.equal(flight.speed, 0);
});
test('thrust is in ship space, not orbit-camera panning', () => {
  const flight = advance(new Flight([0, 0, 0]), 1, keys('w'));
  assert.ok(flight.position[2] < -10); close(flight.position[0], 0); close(flight.position[1], 0);
  assert.ok(advance(new Flight([0, 0, 0], Math.PI / 2), 1, keys('w')).position[0] < -10);
});
test('reverse, strafe and lift are distinct axes', () => {
  for (const [key, axis, sign] of [['s', 2, 1], ['a', 0, -1], ['d', 0, 1], ['e', 1, 1], ['q', 1, -1]]) {
    const f = advance(new Flight([0, 0, 0]), 1, keys(key));
    assert.ok(f.position[axis] * sign > 10);
  }
});
test('diagonal movement does not multiply acceleration', () => {
  close(advance(new Flight([0, 0, 0]), 1, keys('w')).speed,
    advance(new Flight([0, 0, 0]), 1, keys('w', 'd', 'e')).speed);
});
test('analytic integration agrees at 30, 60 and 120 Hz', () => {
  const baseline = advance(new Flight([0, 0, 0]), 2, keys('w'), [], false, 60);
  for (const hz of [30, 120]) {
    const other = advance(new Flight([0, 0, 0]), 2, keys('w'), [], false, hz);
    close(baseline.speed, other.speed); close(baseline.position[2], other.position[2]);
  }
});
test('shift boost increases speed with a hard cap', () => {
  const slow = advance(new Flight([0, 0, 0]), 4, keys('w'));
  const fast = advance(new Flight([0, 0, 0]), 4, keys('w', 'shift'));
  assert.ok(fast.speed > slow.speed * 1.5); assert.ok(fast.speed <= 155.00001);
});
test('boost release decelerates instead of snapping to cruise speed', () => {
  const f = advance(new Flight([0, 0, 0]), 3, keys('w', 'shift'));
  const speed = f.speed; f.step(1 / 60, keys());
  assert.ok(f.speed < speed && f.speed > speed * .98);
});
test('space stops thrust even while W and shift are held', () => {
  const f = advance(new Flight([0, 0, 0]), 2, keys('w', 'shift'));
  advance(f, 1.5, keys('w', 'shift', ' '));
  assert.equal(f.speed, 0); assert.equal(f.thrust, 0); assert.equal(f.boost, false);
});
test('halt clears inertia and all visible trails', () => {
  const f = advance(new Flight([0, 0, 0]), 2, keys('w'));
  assert.ok(f.trail.length > 0); f.halt();
  assert.equal(f.speed, 0); assert.equal(f.trail.length, 0);
});
test('invalid deltas cannot teleport a ship', () => {
  const f = new Flight([0, 0, 0]);
  for (const dt of [0, -1, NaN, Infinity]) f.step(dt, keys('w'));
  assert.deepEqual(f.position, [0, 0, 0]);
  f.step(1000, keys('w')); const expected = new Flight([0, 0, 0]); expected.step(.04, keys('w'));
  assert.deepEqual(f.position, expected.position);
});
test('camera basis is orthonormal at steep pitch', () => {
  const b = flightBasis(5, 1.35), dot = (a, c) => a.reduce((s, v, i) => s + v * c[i], 0);
  for (const axis of Object.values(b)) close(Math.hypot(...axis), 1);
  close(dot(b.right, b.up), 0); close(dot(b.up, b.eye), 0); close(dot(b.right, b.eye), 0);
});
test('steering clamps pitch and keeps heading finite', () => {
  const f = advance(new Flight([0, 0, 0]), 20, keys('arrowup', 'arrowright'));
  close(f.pitch, 1.35); assert.ok(Number.isFinite(f.yaw)); assert.ok(Math.abs(f.bank) <= .5);
});
test('world bounds stop outward velocity', () => {
  const f = advance(new Flight([1199.99, 0, 0]), 1, keys('d'));
  assert.equal(f.position[0], 1200); assert.equal(f.velocity[0], 0);
});
test('swept contacts prevent high-speed tunneling', () => {
  const f = new Flight([0, 0, 12]); f.velocity = [0, 0, -1000];
  f.step(.04, keys(), [{ position: [0, 0, 0], radius: 5.4 }]);
  assert.ok(f.position[2] >= 9.4); assert.ok(f.velocity[2] >= 0);
});
test('normal approach stops outside a planet', () => {
  const f = advance(new Flight([0, 0, 25]), 4, keys('w'), [{ position: [0, 0, 0], radius: 5.4 }]);
  assert.ok(f.position[2] >= 9.4); assert.ok(f.position[2] < 9.5);
});
test('overlapping spawn is moved out along a finite normal', () => {
  const f = new Flight([0, 0, 0]); f.step(.02, keys(), [{ position: [0, 0, 0], radius: 2 }]);
  assert.ok(f.position.every(Number.isFinite)); assert.ok(Math.hypot(...f.position) >= 6.0);
});
test('contact preserves tangential velocity so a ship can escape', () => {
  const f = new Flight([0, 0, 9.5]); f.velocity = [10, 0, -30];
  f.step(.04, keys(), [{ position: [0, 0, 0], radius: 5.4 }]);
  assert.ok(f.velocity[0] > 0); assert.ok(Math.hypot(...f.position) >= 9.4);
});
test('reduced motion removes boost, bank and trails without disabling control', () => {
  const f = advance(new Flight([0, 0, 0]), 1, keys('w', 'shift', 'arrowright'), [], true);
  assert.ok(f.speed > 0); assert.equal(f.bank, 0); assert.equal(f.boost, false); assert.equal(f.trail.length, 0);
  advance(f, 1, keys(), [], true); assert.equal(f.speed, 0);
});
test('trail allocation is bounded and expires while stationary', () => {
  const f = advance(new Flight([0, 0, 0]), 10, keys('w'));
  assert.ok(f.trail.length <= 36); advance(f, 3, keys(' ')); assert.equal(f.trail.length, 0);
});
test('planet meshes are deterministic, closed and bounded', () => {
  const mesh = planetMesh('u_test'); assert.deepEqual(mesh, planetMesh('u_test'));
  assert.notDeepEqual(mesh, planetMesh('u_other')); assert.equal(mesh.length, 128);
  const edges = new Map();
  for (const face of mesh) {
    for (const vertex of face) assert.ok(Math.hypot(...vertex) > .93 && Math.hypot(...vertex) < 1.07);
    for (let i = 0; i < 3; i++) {
      const key = [JSON.stringify(face[i]), JSON.stringify(face[(i + 1) % 3])].sort().join('|');
      edges.set(key, (edges.get(key) || 0) + 1);
    }
  }
  assert.ok([...edges.values()].every(count => count === 2));
});
test('token nebula is seeded, bounded and monochrome', () => {
  const first = tokenNebula('same'); assert.deepEqual(first, tokenNebula('same'));
  assert.notDeepEqual(first.dust, tokenNebula('other').dust);
  assert.equal(first.dust.length, 2300 * 8); assert.equal(first.clouds.length, 72 * 8);
  for (const array of Object.values(first)) {
    assert.ok([...array].every(Number.isFinite));
    for (let i = 0; i < array.length; i += 8) {
      assert.equal(array[i + 3], array[i + 4]); assert.equal(array[i + 4], array[i + 5]);
      assert.ok(array[i + 6] >= 0 && array[i + 6] <= 1);
    }
  }
});
