const {test} = require('node:test');
const assert = require('node:assert/strict');
require('../../src/msg/data/root-web-flight-effects.js');
const Effects = globalThis.MSGFlightEffects;
const fire = (effects, overrides = {}) => effects.fire({position:[0,0,16], yaw:0, pitch:0, now:1000, ...overrides});
const event = (overrides = {}) => ({id:'evt_1', type:'laser', player_id:'own', position:[0,0,16], end:[0,0,-44], at_ms:1000, ...overrides});
const empty = geometry => Object.values(geometry).every(array => array.length === 0);
function validGeometry(geometry) {
  for (const [type, array] of Object.entries(geometry)) {
    assert.equal(array.length % (type === 'lines' ? 16 : type === 'triangles' || type === 'solids' ? 24 : 8), 0);
    assert.ok(array.every(Number.isFinite));
    for (let index = 0; index < array.length; index += 8) {
      assert.ok(array.slice(index + 3, index + 7).every(value => value >= 0 && value <= 1));
      assert.ok(array[index + 7] > 0);
    }
  }
}
test('offline local fire has an immediate muzzle and beam, without inventing a confirmed hit', () => {
  const effects = new Effects(), ship = Object.freeze({hp:100, fuel:80, position:Object.freeze([0,0,16])});
  assert.equal(fire(effects, {position:ship.position}), true);
  const geometry = effects.geometry(1000), state = effects.sample(1000);
  validGeometry(geometry);
  assert.ok(geometry.lines.length >= 8 * 16);
  assert.ok(geometry.points.length >= 16);
  assert.ok(geometry.triangles.length >= 96);
  assert.equal(effects.impacts.length, 0);
  assert.equal(state.feedback.kind, 'intent'); assert.equal(state.feedback.text, '本地开火');
  assert.equal(state.kick, 1); assert.equal(state.flash, 1);
  assert.deepEqual(ship, {hp:100, fuel:80, position:[0,0,16]});
});
test('tracer travels forward and fades, while local shots never grow an impact by themselves', () => {
  const effects = new Effects(); fire(effects);
  const first = effects.geometry(1000).points[2], later = effects.geometry(1100).points[2];
  assert.ok(later < first, 'renderer forward is -eye');
  assert.equal(effects.impacts.length, 0);
  assert.equal(effects.sample(1180).kick, 0);
  assert.ok(empty(effects.geometry(1420)));
  assert.equal(effects.sample(1700).feedback, null);
});
test('renderer yaw and pitch are respected instead of the oppositely signed server angles', () => {
  const effects = new Effects(); fire(effects, {yaw:Math.PI/2, pitch:-.5});
  const head = effects.geometry(1000).points.slice(0, 3);
  assert.ok(head[0] < 0); assert.ok(head[1] > 0); assert.ok(Math.abs(head[2] - 16) < 1e-10);
});
test('own server laser replaces local preview and does not flash or kick a second time', () => {
  const effects = new Effects(); fire(effects);
  const incoming = Object.freeze({...event({at_ms:1040}), position:Object.freeze([0,0,16]), end:Object.freeze([0,0,-12])});
  assert.equal(effects.confirmed(incoming, 'own', 1100), true);
  assert.equal(effects.shots.length, 1); assert.equal(effects.previews.length, 0);
  assert.equal(effects.shots[0].local, false); assert.equal(effects.shots[0].flashAt, 1000);
  assert.equal(effects.kickAt, 1000); assert.equal(effects.flashAt, 1000);
  assert.equal(effects.sample(1100).feedback.kind, 'fired');
  assert.equal(effects.impacts.length, 0, 'laser.target_id never itself becomes a hit');
  assert.deepEqual(incoming.end, [0,0,-12]);
});
test('late server confirmation does not replay an expired local muzzle', () => {
  const effects = new Effects(); fire(effects);
  effects.geometry(1420);
  assert.equal(effects.shots.length, 0); assert.equal(effects.previews.length, 1);
  assert.equal(effects.confirmed(event({at_ms:1400}), 'own', 1500), true);
  assert.equal(effects.shots.length, 1); assert.equal(effects.shots[0].flashAt, 1000);
  assert.equal(effects.sample(1500).flash, 0); assert.equal(effects.sample(1500).kick, 0);
});
test('a delayed acknowledgement replaces the earlier shot and leaves a later local shot intact', () => {
  const effects = new Effects(); fire(effects); fire(effects, {now:1400});
  const later = effects.shots.at(-1);
  assert.equal(effects.confirmed(event(), 'own', 1460), true);
  assert.ok(effects.shots.includes(later)); assert.ok(effects.previews.includes(later));
  assert.equal(effects.previews.length, 1); assert.equal(effects.shots.length, 1);
  const unmatched = new Effects(); fire(unmatched, {now:1400});
  assert.equal(unmatched.confirmed(event(), 'own', 1460), true);
  assert.equal(unmatched.previews.length, 1, 'an old event cannot consume a later request');
  assert.equal(unmatched.shots[0].at, 1400);
  const queued = new Effects(); fire(queued); fire(queued, {now:1410});
  const newest = queued.shots.at(-1);
  queued.confirmed(event({at_ms:1400}), 'own', 1460);
  assert.ok(queued.shots.includes(newest)); assert.ok(queued.previews.includes(newest));
  assert.equal(queued.shots.find(shot => !shot.local).flashAt, 1000, 'server delay belongs to the earlier eligible request');
});
test('only hit/death confirms damage, with victim and shooter fields interpreted correctly', () => {
  const effects = new Effects();
  effects.confirmed(event({target_id:'peer'}), 'own', 1000);
  assert.equal(effects.impacts.length, 0); assert.equal(effects.feedback.kind, 'fired');
  effects.confirmed(event({id:'hit-1', type:'hit', player_id:'peer', target_id:'own'}), 'own', 1000);
  assert.equal(effects.feedback.kind, 'hit'); assert.equal(effects.feedback.text, '命中确认');
  effects.confirmed(event({id:'hit-2', type:'hit', player_id:'own', target_id:'peer', at_ms:1010}), 'own', 1010);
  assert.equal(effects.feedback.kind, 'damaged');
  effects.confirmed(event({id:'death-1', type:'death', player_id:'peer', target_id:'own', at_ms:1020}), 'own', 1020);
  assert.equal(effects.feedback.kind, 'kill');
  effects.confirmed(event({id:'death-2', type:'death', player_id:'own', target_id:'peer', at_ms:1030}), 'own', 1030);
  assert.equal(effects.feedback.kind, 'destroyed');
  validGeometry(effects.geometry(1030)); assert.equal(effects.impacts.length, 4);
});
test('duplicate, future, stale and malformed server events never create effects or notices', () => {
  const effects = new Effects();
  assert.equal(effects.confirmed(event(), 'own', 1000), true);
  const before = effects.sample(1000), lengths = [effects.shots.length, effects.impacts.length];
  for (const incoming of [event(), event({id:'future', at_ms:1001}), event({id:'stale', at_ms:99}),
    event({id:'nan', position:[NaN,0,0]}), event({id:'invalid-end', end:[0, Infinity, 0]}),
    event({id:'not-server-combat', type:'collect'}), event({id:null})]) {
    assert.equal(effects.confirmed(incoming, 'own', 1000), false);
  }
  assert.deepEqual([effects.shots.length, effects.impacts.length], lengths);
  assert.deepEqual(effects.sample(1000), before);
});
test('same-timestamp or older laser acknowledgements cannot overwrite a true hit', () => {
  const effects = new Effects();
  effects.confirmed(event({id:'hit', type:'hit', player_id:'peer', target_id:'own'}), 'own', 1000);
  effects.confirmed(event({id:'laser-late'}), 'own', 1020);
  assert.equal(effects.sample(1020).feedback.kind, 'hit');
  effects.confirmed(event({id:'laser-old', at_ms:990}), 'own', 1020);
  assert.equal(effects.sample(1020).feedback.kind, 'hit');
});
test('reduced motion removes camera kick and moving debris while retaining muzzle, beam and confirmations', () => {
  const regular = new Effects(), reduced = new Effects();
  fire(regular); fire(reduced, {reduced:true});
  const geometry = reduced.geometry(1000);
  assert.ok(geometry.lines.length > 0); assert.ok(geometry.triangles.length > 0); assert.ok(geometry.points.length > 0);
  assert.equal(reduced.sample(1000).kick, 0); assert.equal(reduced.sample(1000).feedback.kind, 'intent');
  assert.equal(reduced.sample(1000).flash, 0, 'no screen flash, but the local muzzle remains readable');
  assert.deepEqual(reduced.geometry(1000).points.slice(0,3), reduced.geometry(1100).points.slice(0,3));
  const hit = event({id:'reduced-hit', type:'hit', player_id:'peer', target_id:'own'});
  regular.confirmed(hit, 'own', 1000); reduced.confirmed(hit, 'own', 1000, {reduced:true});
  assert.equal(reduced.sample(1000).feedback.kind, 'hit');
  assert.equal(reduced.impacts[0].reduced, true);
  assert.ok(reduced.geometry(1150).lines.length < regular.geometry(1150).lines.length);
  assert.deepEqual(reduced.geometry(1430).points.slice(0,3), reduced.geometry(1550).points.slice(0,3));
});
test('sustained bursts bound all pools and geometry and expiry releases everything', () => {
  const effects = new Effects();
  for (let index = 0; index < 2000; index++) {
    fire(effects);
    effects.confirmed(event({id:'hit-' + index, type:'death', player_id:'peer', target_id:'own'}), 'own', 1000);
    effects.confirmed(event({id:'laser-' + index, player_id:'peer'}), 'own', 1000);
  }
  assert.ok(effects.shots.length <= Effects.limits.shots);
  assert.ok(effects.previews.length <= Effects.limits.shots);
  assert.ok(effects.impacts.length <= Effects.limits.impacts);
  assert.ok(effects.seen.size <= Effects.limits.seen);
  const geometry = effects.geometry(1000); validGeometry(geometry);
  assert.ok(Object.values(geometry).reduce((count, array) => count + array.length, 0) < 24000);
  assert.ok(empty(effects.geometry(2400))); assert.equal(effects.sample(2400).active, false);
  effects.sample(9500); assert.equal(effects.seen.size, 0); assert.equal(effects.previews.length, 0);
});
test('clear drops the old world, including replay IDs; fresh own IDs never inherit old feedback', () => {
  const effects = new Effects(); fire(effects);
  const hit = event({type:'hit', player_id:'own', target_id:'peer'});
  effects.confirmed(hit, 'own', 1000); effects.clear();
  assert.equal(effects.sample(1000).active, false); assert.ok(empty(effects.geometry(1000)));
  assert.equal(effects.seen.size, 0);
  assert.equal(effects.confirmed(hit, 'fresh-own', 1000), true);
  assert.equal(effects.feedback, null, 'old victim is now a remote player');
});
test('same-world visual cleanup preserves replay protection when explicitly requested', () => {
  const effects = new Effects(), incoming = event({type:'hit', player_id:'own', target_id:'peer'});
  effects.confirmed(incoming, 'own', 1000); effects.clear({keepSeen:true});
  assert.equal(effects.sample(1000).active, false); assert.ok(empty(effects.geometry(1000)));
  assert.equal(effects.confirmed(incoming, 'own', 1100), false);
  effects.clear(); assert.equal(effects.confirmed(incoming, 'own', 1100), true);
});
test('invalid local inputs produce no geometry, feedback or non-finite vertices', () => {
  const effects = new Effects();
  for (const invalid of [{position:[0,0]}, {position:[0,NaN,0]}, {yaw:Infinity}, {pitch:NaN}, {now:-1}, {now:undefined}]) {
    assert.equal(fire(effects, invalid), false);
  }
  assert.equal(effects.sample(1000).active, false); assert.ok(empty(effects.geometry(1000)));
});

test('local FX respects finite negotiated beam range and keeps invalid values bounded', () => {
  for(const [range,expected] of [[60,60],[180,180],[1000,1000],[0,180],[Infinity,180],[1001,180]]) {
    const effects=new Effects();fire(effects,{range});assert.deepEqual(effects.shots[0].end,[0,0,16-expected]);
    assert.equal(effects.impacts.length,0);
  }
});
