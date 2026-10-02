/* Tick/render independence, authority corrections and shared collectible geometry. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {createHash} = require('node:crypto');
require('../../src/msg/data/root-web-model.js');
require('../../src/msg/data/root-web-renderer.js');
const {MotionTrack, FlightPrediction, integrateFlight, inertialSegment, tokenNebula} = MSGUniverseFlight;
const close = (actual, expected, tolerance = 1e-8) => assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} != ${expected}`);
const ship = (overrides = {}) => ({id:'own',position:[0,0,0],velocity:[0,0,0],hp:100,fuel:100,region:0,yaw:0,pitch:0,dash_until_ms:0,...overrides});
const neutral = {throttle:0,strafe:0,lift:0,yaw:0,pitch:0,brake:false};

test('5 Hz authoritative snapshots render constant velocity at every 60 Hz frame', () => {
  const track = new MotionTrack();
  const frames = [];
  for (let frame = 0; frame <= 180; frame++) {
    const time = frame * 1000 / 60;
    if (frame % 12 === 0) track.push(ship({position:[time * .02,0,0],velocity:[20,0,0]}), time);
    const position = track.sample(time - 240).position[0];
    if (time >= 300) { close(position, (time - 240) * .02); frames.push(position); }
  }
  for (let i = 1; i < frames.length; i++) close(frames[i] - frames[i - 1], 1 / 3);
  assert.ok(track.samples.length <= 8);
});

test('Hermite acceleration and wrapped headings interpolate physical sample time', () => {
  const track = new MotionTrack();
  track.push(ship({yaw:Math.PI-.05}),1000);
  track.push(ship({position:[.48,0,0],velocity:[4.8,0,0],yaw:-Math.PI+.05}),1200);
  const mid = track.sample(1100); close(mid.position[0], .12); close(Math.abs(mid.yaw), Math.PI);
  close(track.sample(1200).position[0], .48);
  close(track.sample(100000).position[0], .48 + 4.8 * .3);
});

test('warp, death and respawn never interpolate through an old ship trajectory', () => {
  const track = new MotionTrack(); track.push(ship({velocity:[20,0,0]}),1000);
  track.push(ship({position:[300,0,0],region:1}),1200);
  assert.deepEqual(track.sample(1000).position,[300,0,0]); assert.equal(track.samples.length,1);
  track.push(ship({position:[301,0,0],region:1,hp:0}),1400);
  assert.deepEqual(track.sample(1300).position,[301,0,0]);
  track.push(ship({position:[0,0,0]}),1600,true);
  assert.deepEqual(track.sample(1500).position,[0,0,0]);
});

test('visual thrust, coast and brake preserve authoritative target handling across frame rates', () => {
  for (const hz of [30,60,120]) {
    const state = ship();
    for (let i=1;i<=2*hz;i++) integrateFlight(state,1/hz,{...neutral,throttle:1},{},i*1000/hz);
    close(state.velocity[2],-20); close(state.position[2],-31.66666666666667);
    const before=state.position[2];
    for (let i=1;i<=hz;i++) integrateFlight(state,1/hz,neutral,{},2000+i*1000/hz);
    close(state.velocity[2],-20*Math.exp(-.32)); assert.ok(before-state.position[2]>17);
    for (let i=1;i<=hz;i++) integrateFlight(state,1/hz,{...neutral,brake:true},{},3000+i*1000/hz);
    close(state.velocity[2],0);
  }
});

test('own ship corrections stay continuous, and authoritative hard transitions snap immediately', () => {
  const prediction=new FlightPrediction(ship({velocity:[0,0,-20]}));
  let previous=prediction.position[2];
  for(let frame=1;frame<=120;frame++) {
    const time=frame*1000/60;
    if(frame%12===0) prediction.reconcile(ship({position:[0,0,-time*.02],velocity:[0,0,-20]}));
    const position=prediction.step(1/60,{...neutral,throttle:1},{},time)[2];
    assert.ok(position<previous); assert.ok(Math.abs(position-previous)<.45);
    previous=position;
  }
  prediction.reconcile(ship({position:[300,0,0],region:1}),true);
  assert.deepEqual(prediction.position,[300,0,0]);
  prediction.reconcile(ship({position:[301,0,0],region:1,hp:0}));
  assert.deepEqual(prediction.position,[301,0,0]);
  prediction.reconcile(ship({position:[0,0,0],hp:100}),true);
  assert.deepEqual(prediction.position,[0,0,0]);
});

test('fuel exhaustion coasts, dash endpoints soften, and prediction cannot alter the server record', () => {
  const authoritative=Object.freeze({...ship({velocity:[0,0,-20],fuel:0}),position:Object.freeze([0,0,0]),velocity:Object.freeze([0,0,-20])});
  const prediction=new FlightPrediction(authoritative);
  prediction.step(.04,{...neutral,throttle:1},{},40);
  assert.ok(prediction.state.velocity[2]<-19); assert.deepEqual(authoritative.position,[0,0,0]); assert.equal(authoritative.fuel,0);
  const dash=ship({velocity:[0,0,-60],dash_until_ms:1000});
  integrateFlight(dash,.04,{...neutral,throttle:1},{},1020);
  assert.ok(dash.velocity[2]<-59 && dash.velocity[2]>-60);
  const result=inertialSegment([0,0,-20],1,[0,0,0],48);
  assert.deepEqual(result.velocity,[0,0,0]); close(result.movement[2],-25/6);
});

test('all 2300 shared glyph coordinates match the Python Float32 golden and are reachable', () => {
  const dust=tokenNebula('0'.repeat(32)).dust, positions=new Float32Array(2300*3);
  for(let id=0;id<2300;id++) {
    const p=dust.subarray(id*8,id*8+3); positions.set(p,id*3);
    assert.ok(Math.hypot(...p)<=475.00004);
  }
  assert.equal(createHash('sha256').update(Buffer.from(positions.buffer)).digest('hex'),
    '259f170fb59c49eb4d39e12d7302e4ccbc38a95c4356a6f804f66878068c6238');
});

test('slow render frames subdivide the whole interval without advancing a paused or invalid clock', () => {
  for(const hz of [10,20,30,60,120]) {
    const prediction=new FlightPrediction(ship());
    for(let i=1;i<=hz*2;i++) prediction.step(1/hz,{...neutral,throttle:1},{},i*1000/hz);
    close(prediction.position[2],-31.66666666666667); close(prediction.state.velocity[2],-20);
    const before=[...prediction.position];
    for(const invalid of [0,-1,NaN,Infinity]) prediction.step(invalid,{...neutral,throttle:1},{},3000);
    assert.deepEqual(prediction.position,before);
  }
});
