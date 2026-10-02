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
test('first render before the public graph response has an empty pinned set', () => {
  const methods = ['init', 'events', 'flightEvents', 'resize', 'wake'];
  const saved = Object.fromEntries(methods.map(name => [name, Renderer.prototype[name]]));
  const globals = ['document', 'matchMedia', 'ResizeObserver'];
  const originals = Object.fromEntries(globals.map(name => [name, globalThis[name]]));
  let firstFrames = 0;
  try {
    globalThis.document = {getElementById() {return null;}};
    globalThis.matchMedia = () => ({matches:false});
    globalThis.ResizeObserver = class {observe() {}};
    for (const name of methods) Renderer.prototype[name] = function () {};
    Renderer.prototype.wake = function () {
      this.width = 1280; this.height = 800; this.prepareView(); firstFrames++;
      assert.deepEqual(this.view.nodes, []);
    };
    const r = new Renderer({addEventListener() {}, dataset:{}}, {});
    assert.deepEqual(r.pinned, []); assert.equal(firstFrames, 1);
  } finally {
    for (const [name, value] of Object.entries(saved)) Renderer.prototype[name] = value;
    for (const [name, value] of Object.entries(originals)) globalThis[name] = value;
  }
});
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
test('local fire intent draws a beam without creating an authoritative laser or hit', () => {
  const r = renderer(); const baseline = r.geometry().lines.length;
  r.keys.add(' '); r.queueGameAction('laser');
  assert.equal(r.shotEvents.size, 0); assert.equal(r.combatEvents?.size ?? 0, 0);
  assert.equal(r.localShot.intent, true); assert.ok(r.geometry().lines.length > baseline);
  assert.equal(r.network.self.hp, 100); assert.equal(r.network.self.fuel, 80);
  r.localShot = null;
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

test('shared bitmap removes every glyph from GPU/software dust only after confirmation and restores availability', () => {
  const r=renderer(); r.sceneSeed='preview'; r.tokenNodes=new Map();
  const clear=Object.freeze(Array(288).fill(0)), field={seed:'0'.repeat(32),count:2300,revision:0,mask:clear};
  r.syncCollectibles(field); assert.equal(r.dust.length,2300*8); assert.equal(r.renderDust.length,2300*8);
  r.flight.position=Array.from(r.dust.subarray(0,3));
  r.updateFlight(.01); assert.equal(r.renderDust.length,2300*8,'proximity alone never claims a glyph');
  const mask=[...clear]; mask[0]=3; mask[287]=8;
  r.syncCollectibles({revision:1,mask});
  assert.equal(r.glyphTaken(0),true); assert.equal(r.glyphTaken(1),true); assert.equal(r.glyphTaken(2299),true);
  assert.equal(r.renderDust.length,2297*8);
  r.syncCollectibles({revision:2,mask:clear}); assert.equal(r.renderDust.length,2300*8);
});

test('absorption animation only follows confirmed events and stops immediately on disconnect', () => {
  const r=renderer(); r.dust=MSGUniverseFlight.tokenNebula('0'.repeat(32)).dust; r.collectEvents=new Map();
  assert.deepEqual(r.absorptionGlyphs(),[]);
  r.collectEvents.set('evt_1',{id:'evt_1',type:'collect',player_id:r.network.self.id,at_ms:900,glyph_ids:[0]});
  const effects=r.absorptionGlyphs(); assert.equal(effects.length,1); assert.equal(effects[0].id,0);
  r.network.connected=false; assert.deepEqual(r.absorptionGlyphs(),[]);
  r.clearRemoteShips(); assert.equal(r.collectEvents.size,0);
});

test('repeated snapshots never replay an expired authoritative impact or change ship values', () => {
  const frozen = Object.freeze({...self(), position:Object.freeze([0,0,16]), velocity:Object.freeze([0,0,0])});
  const r=renderer(frozen), event={id:'evt_1',type:'hit',player_id:'peer',target_id:frozen.id,position:[0,0,10],at_ms:900,region:0};
  r.receiveCombatEvents([event],1000); const message=r.combatStatus;
  assert.equal(message.text,'命中确认'); assert.equal(r.combatEvents.size,1);
  r.combatEvents.clear(); r.receiveCombatEvents([event],1100);
  assert.equal(r.combatEvents.size,0); assert.equal(r.combatStatus,message);
  assert.equal(frozen.hp,100); assert.equal(frozen.fuel,80); assert.equal(frozen.laser_ready_ms,0);
  r.receiveCombatEvents([{...event,id:'evt_future',at_ms:2000},{...event,id:'evt_old',at_ms:0}],1000);
  assert.equal(r.combatEvents.size,0);
});

test('victim and shooter fields distinguish damage, hit and destruction confirmations', () => {
  const r=renderer(), base={position:[0,0,16],at_ms:1000,region:0};
  r.receiveCombatEvents([{...base,id:1,type:'laser',player_id:r.network.self.id,target_id:'peer',end:[0,0,-20]}],1000);
  assert.equal(r.combatStatus.text,'已发射'); assert.equal(r.combatEvents.size,0);
  r.receiveCombatEvents([{...base,id:2,type:'hit',player_id:r.network.self.id,target_id:'peer'}],1000);
  assert.equal(r.combatStatus.text,'受到攻击');
  r.receiveCombatEvents([{...base,id:3,type:'death',player_id:'peer',target_id:r.network.self.id}],1000);
  assert.equal(r.combatStatus.text,'击毁确认');
  r.receiveCombatEvents([{...base,id:4,type:'death',player_id:r.network.self.id,target_id:'peer'}],1000);
  assert.equal(r.combatStatus.text,'飞船被击毁'); assert.equal(r.network.self.hp,100);
});

test('fire intent explains fuel and cooldown, expires without a snapshot, and reduced motion keeps static feedback', () => {
  const r=renderer(); r.network.self.fuel=0;
  assert.equal(r.previewLaser(),false); assert.match(r.combatStatus.text,/燃料不足/); assert.equal(r.localShot,undefined);
  r.network.self.fuel=80; r.network.self.laser_ready_ms=1500;
  assert.equal(r.previewLaser(),false); assert.match(r.combatStatus.text,/冷却 0.5s/);
  r.network.serverNow=1600; assert.equal(r.previewLaser(),true);
  const first=r.localShot; assert.equal(r.previewLaser(false),false); assert.equal(r.localShot,first);
  r.reduced.matches=true; assert.ok(r.geometry().triangles.length>0);
  r.network.serverNow=2000; r.geometry(); assert.equal(r.localShot,null);
});

test('combat dedup survives same-player reconnection but new hello clears old-world IDs', () => {
  const r=renderer(), event={id:1,type:'hit',player_id:r.network.self.id,position:[0,0,16],at_ms:1000};
  r.receiveFlightHello({self:r.network.self,server_time_ms:1000});
  r.receiveCombatEvents([event],1000); assert.equal(r.combatSeen.size,1);
  r.clearRemoteShips(); assert.equal(r.combatSeen.size,1); assert.equal(r.combatEvents.size,0);
  r.receiveFlightHello({self:r.network.self,server_time_ms:1100});
  r.receiveCombatEvents([event],1100); assert.equal(r.combatEvents.size,0);
  const fresh={...self(),id:'new-own'}; r.network.self=fresh;
  r.receiveFlightHello({self:fresh,server_time_ms:1200}); assert.equal(r.combatSeen.size,0);
  r.receiveCombatEvents([{...event,player_id:fresh.id,at_ms:1200}],1200); assert.equal(r.combatEvents.size,1);
});

test('combat geometry and caches stay bounded, omit other regions and reduce spatial debris', () => {
  const r=renderer(), events=Array.from({length:600},(_,id)=>({id,type:'death',player_id:'peer',position:[0,0,10],at_ms:900,region:0}));
  r.receiveCombatEvents(events,1000); assert.equal(r.combatSeen.size,512); assert.equal(r.combatEvents.size,48);
  const full=r.geometry().lines.length; r.reduced.matches=true;
  assert.ok(r.geometry().lines.length<full);
  r.network.serverNow=2000; r.geometry(); assert.equal(r.combatEvents.size,0);
  r.receiveCombatEvents([{id:700,type:'death',player_id:'peer',position:[0,0,10],at_ms:2000,region:1}],2000);
  assert.equal(r.combatEvents.size,0);
});
