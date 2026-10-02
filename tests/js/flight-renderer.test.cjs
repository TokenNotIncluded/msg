/* Authoritative multiplayer render/input contracts; no network or invented peers. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
require('../../src/msg/data/root-web-model.js');
require('../../src/msg/data/root-web-renderer.js');
const Renderer = globalThis.MSGUniverseRenderer;
const {Flight, flightBasis} = globalThis.MSGUniverseFlight;
const self = () => ({id:'ship-own', handle:'pilot-own', guest:true, position:[0, 0, 16], velocity:[0, 0, 0],
  yaw:0, pitch:0, hp:100, fuel:80, laser_ready_ms:0, shield_ready_ms:0, dash_ready_ms:0,
  shield_until_ms:0, respawn_at_ms:0, region:0, score:0});
function renderer(ship = self()) {
  const canvas = {};
  const r = Object.create(Renderer.prototype);
  Object.assign(r, {
    canvas, flight:new Flight(ship.position), keys:new Set(), flightControls:new Map(), gameActions:new Set(),
    graph:{nodes:[], links:[]}, view:{nodes:[], links:[]}, camera:{target:[0,0,0], yaw:0, pitch:0, distance:34},
    callbacks:{now:() => 1000}, reduced:{matches:false}, paused:true, hudTime:0, clock:0, visualTraits:new WeakMap(),
    remoteShips:new Map(), shotEvents:new Map(), shipLabels:new Map(), homeBody:null,
    network:{connected:true, self:ship, serverNow:1000, setInput(value) {this.input = value;}},
    wake() {}, updateFlightHud() {}, basis() {return flightBasis(this.camera.yaw, this.camera.pitch);},
    project() {return {x:100,y:100,depth:20,scale:2};},
  });
  globalThis.document = {activeElement:canvas, getElementById() {return null;}};
  return r;
}
test('renderer sends control intent without position, health, identity or damage', () => {
  const r = renderer(); r.keys = new Set(['w','d','q',' ','b']); r.gameActions.add('shield');
  r.sendFlightInput();
  assert.deepEqual(Object.keys(r.network.input).sort(), ['actions','brake','lift','pitch','strafe','throttle','yaw']);
  assert.deepEqual(r.network.input.actions, ['shield','laser']);
  assert.equal(r.network.input.brake, true); assert.equal(r.network.input.throttle, 1);
  assert.equal(r.network.input.lift, -1); assert.equal(r.network.input.strafe, 1);
  assert.equal(r.gameActions.size, 0);
});
test('Space is attack and B is brake, without mixing boost into the wire', () => {
  const r = renderer(); r.keys.add(' '); r.sendFlightInput();
  assert.deepEqual(r.network.input.actions, ['laser']); assert.equal(r.network.input.brake, false);
  r.keys = new Set(['shift']); r.sendFlightInput(); assert.deepEqual(r.network.input.actions, []);
});
test('authoritative positions and velocity correct the visual ship', () => {
  const ship = Object.freeze({...self(), position:[20,30,40], velocity:[1,2,3]});
  const r = renderer(ship); r.flight.position = [0,0,0]; r.keys.add('w');
  r.updateFlight(0);
  assert.deepEqual(r.flight.position, ship.position); assert.deepEqual(r.flight.velocity, ship.velocity);
  assert.equal(ship.hp, 100); assert.equal(ship.fuel, 80);
});
test('disconnected controls neither move the visual ship nor send attacks', () => {
  const r = renderer(); r.network.connected = false; r.keys.add('w'); r.keys.add(' ');
  const before = [...r.flight.position]; r.updateFlight(.04); r.queueGameAction('dash'); r.sendFlightInput();
  assert.deepEqual(r.flight.position, before); assert.equal(r.network.input, undefined);
  assert.equal(r.gameActions.size, 0); assert.equal(r.flight.thrust, 0);
});
test('dead ships cannot queue skills and movement is neutral', () => {
  const r = renderer({...self(), hp:0, respawn_at_ms:4000}); r.keys.add('w');
  r.queueGameAction('laser'); r.updateFlight(.02);
  assert.equal(r.gameActions.size, 0); assert.equal(r.network.input, undefined); assert.equal(r.flight.thrust, 0);
});
test('snapshot replacement removes departed real peers and ages real laser events', () => {
  const r = renderer(), peer = {...self(), id:'ship-peer'};
  r.receiveFlightSnapshot({players:[self(), peer], events:[{id:1,type:'laser',position:[0,0,0],end:[0,0,10],at_ms:900}]});
  assert.equal(r.remoteShips.size, 2); assert.equal(r.shotEvents.size, 1);
  r.network.serverNow = 1500;
  r.receiveFlightSnapshot({players:[self()], events:[]});
  assert.equal(r.remoteShips.has('ship-peer'), false); assert.equal(r.shotEvents.size, 0);
});
test('remote ship geometry exists only for current living server peers', () => {
  const r = renderer(); const alone = r.geometry().solids.length;
  r.remoteShips.set('peer', {state:{...self(),id:'peer'},position:[10,0,16]});
  assert.ok(r.geometry().solids.length > alone);
  r.remoteShips.get('peer').state.hp = 0; assert.equal(r.geometry().solids.length, alone);
  r.network.connected = false; assert.equal(r.geometry().solids.length, 0);
});
test('only received server laser events create beam geometry', () => {
  const r = renderer(); const baseline = r.geometry().lines.length;
  r.keys.add(' '); r.queueGameAction('laser'); assert.equal(r.geometry().lines.length, baseline);
  r.shotEvents.set(1, {at_ms:900,position:[0,0,0],end:[10,0,0]});
  assert.ok(r.geometry().lines.length > baseline);
  r.network.serverNow = 1600; assert.equal(r.geometry().lines.length, baseline);
});
test('new hello reanchors an existing ship to server spawn and orientation', () => {
  const r = renderer(); r.wantFlight = false;
  r.receiveFlightHello({self:{...self(),position:[200,50,10],yaw:.7,pitch:.2,
    home_body:{id:'self-home',position:[200,43,10],radius:3,private:true}}});
  assert.deepEqual(r.flight.position, [200,50,10]); assert.equal(r.flight.yaw, -.7); assert.equal(r.flight.pitch, -.2);
  assert.equal(r.homeBody.private, true);
});
