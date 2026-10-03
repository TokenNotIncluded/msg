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
    canvas, width:1280, height:800, flight:new Flight(ship.position), keys:new Set(), flightControls:new Map(), gameActions:new Set(),
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
  assert.equal(r.network.input.brake, true); assert.equal(r.network.input.throttle, 0,'brake overrides thrust');
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
test('offline and online steering integrate once and send the same attitude through wire v1', () => {
  const online = renderer(), offline = renderer(); offline.network = null;
  online.keys.add('arrowup'); online.keys.add('arrowright');
  offline.keys = new Set(online.keys);
  globalThis.document.activeElement = online.canvas;
  for (let i = 0; i < 60; i++) {
    globalThis.document.activeElement = online.canvas; online.updateFlight(1 / 60);
    globalThis.document.activeElement = offline.canvas; offline.updateFlight(1 / 60);
  }
  assert.equal(online.flight.yaw, offline.flight.yaw); assert.equal(online.flight.pitch, offline.flight.pitch);
  assert.equal(online.network.input.yaw, -online.flight.yaw); assert.equal(online.network.input.pitch, -online.flight.pitch);
  assert.equal(online.flight.bank, 0); assert.equal(offline.flight.bank, 0);
});
test('authoritative region or respawn resets attitude and held steering without integrating an old angle', () => {
  const r = renderer(); r.updateFlight(0); r.keys.add('arrowright'); r.flight.steer(.04, {yaw:1});
  r.pointers = new Map(); r.flightPointers = new Map(); r.flightSticks = [];
  globalThis.document.querySelectorAll = () => [];
  r.network.self = {...self(), position:[100,0,0], yaw:1.3, pitch:.4, region:2};
  r.receiveFlightSnapshot({players:[r.network.self], events:[{id:'jump',type:'region',player_id:r.network.self.id,at_ms:1000}]});
  assert.equal(r.flight.yaw, -1.3); assert.equal(r.flight.pitch, -.4); assert.equal(r.flight.yawRate, 0);
  assert.equal(r.keys.size, 0); assert.equal(r.prediction.state.region, 2);
});
test('a modal neutralizes flight axes and fire without rotating or changing authoritative velocity', () => {
  const r = renderer(); r.keys = new Set(['w','arrowup',' ']);
  globalThis.document.querySelector = () => ({});
  const yaw = r.flight.yaw, pitch = r.flight.pitch;
  r.updateFlight(.02); r.sendFlightInput();
  assert.equal(r.flight.yaw, yaw); assert.equal(r.flight.pitch, pitch); assert.equal(r.flight.thrust, 0);
  assert.equal(r.network.input.throttle, 0); assert.deepEqual(r.network.input.actions, []);
  assert.equal(r.localShot, undefined);
  delete globalThis.document.querySelector;
});
test('death clears client-held and pending controls before respawn without suspending the connection', () => {
  const r = renderer(); r.updateFlight(0); r.keys = new Set(['w','d','e',' ']); r.sendFlightInput();
  r.pointers = new Map(); r.flightPointers = new Map(); r.flightSticks = [];
  r.network.pendingActions = new Set(['dash']); let cleared = 0;
  r.network.clearInput = function () {
    cleared++; this.pendingActions.clear(); this.input={throttle:0,strafe:0,lift:0,brake:false,actions:[],yaw:this.self.yaw,pitch:this.self.pitch};
  };
  r.network.suspend = () => {throw Error('death must keep automatic respawn connected');};
  r.network.self = {...self(), hp:0, yaw:.8, pitch:-.2};
  r.receiveFlightSnapshot({players:[r.network.self],events:[]});
  assert.equal(cleared,1);assert.equal(r.network.input.throttle,0);assert.equal(r.network.input.strafe,0);assert.equal(r.network.input.lift,0);
  assert.deepEqual(r.network.input.actions,[]);assert.equal(r.network.pendingActions.size,0);assert.equal(r.keys.size,0);
  r.network.self = {...self(), yaw:1.2, pitch:.3};
  r.receiveFlightSnapshot({players:[r.network.self],events:[]});
  assert.equal(cleared,2);assert.equal(r.flight.yaw,-1.2);assert.equal(r.flight.pitch,-.3);assert.equal(r.flight.yawRate,0);
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
  r.network.serverNow=1399; assert.equal(r.absorptionGlyphs().length,1);
  r.network.serverNow=1400; assert.deepEqual(r.absorptionGlyphs(),[]);
  r.network.serverNow=899; assert.deepEqual(r.absorptionGlyphs(),[]);
  r.network.serverNow=1000; r.reduced.matches=true; assert.deepEqual(r.absorptionGlyphs(),[]);
  r.reduced.matches=false;
  r.network.connected=false; assert.deepEqual(r.absorptionGlyphs(),[]);
  r.clearRemoteShips(); assert.equal(r.collectEvents.size,0);
});

for (const offscreen of [false,true]) test(`own absorption survives 64 earlier ${offscreen ? 'offscreen' : 'visible'} peer pickups within the bounded budget`, () => {
  const r=renderer(); r.tokenNodes=new Map(); r.network.serverNow=0;
  const field={version:2,layout_version:1,seed:'0'.repeat(32),count:2300,radius:8,fuel:4,respawn_ms:45000,
    revision:0,mask:Array(288).fill(0),anchors:[
      {id:'root',position:[0,0,0],radius:3},{id:'east',position:[300,0,0],radius:3},
    ]};
  r.syncCollectibles(field);
  const ownId=1800, ownPosition=Array.from(r.dust.subarray(ownId*8,ownId*8+3));
  r.network.self={...r.network.self,position:ownPosition}; r.flight.position=[...ownPosition];
  const peer={...r.network.self,id:'ship-far',position:[5,0,0]};
  const events=[
    {id:'evt_far_1',type:'collect',player_id:peer.id,at_ms:900,glyph_ids:Array.from({length:32},(_,i)=>i),fuel_added:0,collected:32},
    {id:'evt_far_2',type:'collect',player_id:peer.id,at_ms:967,glyph_ids:Array.from({length:32},(_,i)=>i+32),fuel_added:0,collected:64},
    {id:'evt_own',type:'collect',player_id:r.network.self.id,at_ms:967,glyph_ids:[ownId],fuel_added:4,collected:1},
  ];
  const mask=[...field.mask];
  for (const event of events) for (const id of event.glyph_ids) mask[id>>3]|=1<<(id&7);
  r.network.serverNow=1000;
  r.receiveFlightSnapshot({players:[r.network.self,peer],events,collectibles:{...field,revision:65,mask},state_time_ms:1000});
  assert.equal(r.collectEvents.size,3); assert.equal(r.glyphTaken(ownId),true);
  const beforeFuel=r.network.self.fuel, beforePosition=[...r.flight.position];
  let projections=0;
  r.project=position => {
    projections++;
    return {x:offscreen && position[0]<250 ? -20 : 100,y:200,scale:2};
  };
  const effects=r.absorptionGlyphs();
  assert.equal(effects[0].id,ownId,'own confirmation is first even when all peer effects are visible');
  assert.equal(effects.length,offscreen ? 1 : 64);
  assert.equal(effects.filter(effect=>effect.key.includes('evt_own')).length,1);
  assert.ok(effects.every(effect=>effect.projected.x>=0 && effect.projected.x<=r.width));
  assert.equal(projections,offscreen ? 65 : 64,'offscreen particles never consume the 64 display slots');
  assert.equal(r.network.self.fuel,beforeFuel); assert.deepEqual(r.flight.position,beforePosition);

  // Render the same visible results without projecting them for a second time.
  globalThis.document.createElement=()=>({style:{},remove() {}});
  r.tokenField={append() {}};
  const dust=r.dust; r.dust=new Float32Array();
  const absorption=r.absorptionGlyphs; r.absorptionGlyphs=()=>effects;
  r.updateTokens();
  assert.equal(r.tokenNodes.size,effects.length); assert.equal(projections,offscreen ? 65 : 64);
  r.dust=dust; r.absorptionGlyphs=absorption;
});

test('absorption visibility skips behind-camera and every outside-viewport edge before using a slot', () => {
  const r=renderer(); r.dust=MSGUniverseFlight.tokenNebula('0'.repeat(32)).dust;
  r.collectEvents=new Map([['evt_visible',{id:'evt_visible',type:'collect',player_id:r.network.self.id,
    at_ms:900,glyph_ids:[0,1,2,3,4,5]}]]);
  const projections=[null,{x:-1,y:200},{x:1281,y:200},{x:100,y:-1},{x:100,y:801},{x:100,y:200,scale:2}];
  r.project=()=>projections.shift();
  const effects=r.absorptionGlyphs();
  assert.deepEqual(effects.map(effect=>effect.id),[5]);
  assert.deepEqual(effects[0].projected,{x:100,y:200,scale:2});
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

test('combat geometry and caches stay bounded and reduce spatial debris', () => {
  const r=renderer(), events=Array.from({length:600},(_,id)=>({id,type:'death',player_id:'peer',position:[0,0,10],at_ms:900,region:0}));
  r.receiveCombatEvents(events,1000); assert.equal(r.combatSeen.size,512); assert.equal(r.combatEvents.size,48);
  const full=r.geometry().lines.length; r.reduced.matches=true;
  assert.ok(r.geometry().lines.length<full);
  r.network.serverNow=2000; r.geometry(); assert.equal(r.combatEvents.size,0);
});

test('cross-region server shots and hits keep confirmation and damage feedback', () => {
  const r=renderer(), own=r.network.self.id;
  const laser={id:700,type:'laser',player_id:own,position:[0,0,16],end:[0,0,-20],at_ms:1000,region:0};
  const hit={id:701,type:'hit',player_id:'peer',target_id:own,position:[0,0,-20],at_ms:1000,region:1};
  r.receiveFlightSnapshot({players:[r.network.self,{...self(),id:'peer',region:1}],events:[laser,hit]});
  assert.equal(r.shotEvents.size,1); assert.equal(r.combatEvents.size,1); assert.equal(r.combatStatus.text,'命中确认');
  r.receiveCombatEvents([{...hit,id:702,player_id:own,target_id:'peer',region:2}],1000);
  assert.equal(r.combatStatus.text,'受到攻击'); assert.equal(r.network.self.hp,100);
});

test('complete highway descriptors rebuild only on geometry or availability changes', () => {
  const r=renderer(); r.tokenNodes=new Map();
  const field={version:2,layout_version:1,seed:'0'.repeat(32),count:2300,revision:0,mask:Array(288).fill(0),anchors:[
    {id:'root',position:[0,0,0],radius:5.4},{id:'peer',position:[120,40,-30],radius:2.05},
  ]};
  r.syncCollectibles(field); const first=r.dust, compact=r.renderDust;
  assert.equal(r.tokenLanes.length,1); r.syncCollectibles({...field,anchors:field.anchors.map(a=>({...a}))});
  assert.equal(r.dust,first); assert.equal(r.renderDust,compact);
  const mask=[...field.mask];mask[0]=1;
  r.syncCollectibles({...field,revision:1,mask}); assert.equal(r.dust,first); assert.equal(r.renderDust.length,2299*8);
  r.collectEvents=new Map([['old-layout',{glyph_ids:[0],at_ms:900}]]);
  r.syncCollectibles({...field,seed:'1'.repeat(32)}); assert.notEqual(r.dust,first); assert.equal(r.renderDust.length,2300*8);
  assert.equal(r.collectEvents.size,0); assert.equal(r.collectSince,1000);
});


test('local session continues detached prediction without uploading local pose, fuel or attacks', () => {
  const r = renderer(Object.freeze(self())); r.network.connected=false; r.network.localSession=true;
  r.updateFlight(.04); // Transition clears held controls once.
  r.keys.add('w'); r.keys.add(' ');
  const before=[...r.flight.position], original=r.network.self;
  for(let i=0;i<120;i++){r.network.serverNow+=40; r.updateFlight(.04);}
  assert.notDeepEqual(r.flight.position,before); assert.ok(r.flight.speed>0);
  assert.equal(r.network.input,undefined); assert.equal(r.gameActions.size,0);
  assert.equal(r.network.self,original); assert.equal(original.fuel,80); assert.equal(original.hp,100);
  assert.ok(r.localShot?.intent); assert.ok(r.geometry().solids.length>0);
});
test('online extrapolation is capped, while local mode may continue after a real disconnect', () => {
  const r=renderer(); r.authorityAt=1000; r.keys.add('w'); r.updateFlight(.04);
  r.network.serverNow=1400; const before=[...r.flight.position]; r.updateFlight(.04);
  assert.deepEqual(r.flight.position,before);
  r.network.connected=false; r.network.localSession=true; r.updateFlight(.04); r.keys.add('w');
  r.updateFlight(.04); assert.notDeepEqual(r.flight.position,before);
});
test('reconnection smooths a large same-region local offset and discards local rewards', () => {
  const r=renderer(); r.network.connected=false; r.network.localSession=true;
  r.updateFlight(.04); r.flight.position=[90,0,16]; r.prediction.position=[90,0,16];
  r.offlineTaken=new Set([2,4]); const ship={...self(),position:[0,0,16],collected:3};
  r.network.connected=true; r.network.self=ship;
  r.receiveFlightHello({self:ship,server_time_ms:1000});
  assert.equal(r.offlineMode,false); assert.equal(r.offlineTaken.size,0);
  assert.deepEqual(r.flight.position,[90,0,16]); assert.deepEqual(r.prediction.state.position,ship.position);
  r.updateFlight(.04); assert.ok(r.flight.position[0]>0 && r.flight.position[0]<90);
  assert.equal(ship.collected,3); assert.equal(ship.fuel,80);
});
test('offline token absorption affects only detached fuel and local bitmap', () => {
  const r=renderer(); r.network.connected=false; r.network.localSession=true; r.updateFlight(.04);
  r.dust=new Float32Array([0,0,16,1,1,1,.5,1]); r.collectibles={revision:2,radius:8,fuel:4,mask:[0]};
  r.prediction.state.fuel=30; r.offlineCollect(.1);
  assert.equal(r.offlineTaken.size,1); assert.equal(r.prediction.state.fuel,34);
  assert.equal(r.collectibles.mask[0],0); assert.equal(r.network.self.fuel,80);
  assert.equal(r.renderDust.length,0); assert.equal(r.network.input,undefined);
});


test('read-only observer renders only real current-region peers without a self ship or control input', () => {
  const r=renderer(); r.flight=null; r.wantFlight=false; r.observing=true; r.observeRegion=0;
  r.observerClient={connected:true,serverNow:1000};
  const one={...self(),id:'real-a'}, two={...self(),id:'real-b',region:1};
  r.receiveObserverSnapshot({players:[one,two],state_time_ms:1000});
  assert.equal(r.remoteShips.size,1); assert.ok(r.remoteShips.has('real-a'));
  assert.ok(r.geometry().solids.length>0); assert.equal(r.network.input,undefined);
  r.observeRegion=1; r.receiveObserverSnapshot({players:[one,two],state_time_ms:1067});
  assert.equal(r.remoteShips.size,1); assert.ok(r.remoteShips.has('real-b'));
  r.observing=false; assert.equal(r.geometry().solids.length,0);
});
test('assembled weapon module gives immediate local geometry and only server events confirm a hit', () => {
  require('../../src/msg/data/root-web-flight-effects.js');
  const r=renderer(); r.effects=new MSGFlightEffects(); r.network.connected=false; r.network.localSession=true;
  const before=r.geometry().lines.length; r.queueGameAction('laser');
  assert.ok(r.geometry().lines.length>before); assert.equal(r.effects.sample(1000).feedback.kind,'intent');
  assert.equal(r.network.self.hp,100); assert.equal(r.network.input,undefined);
  r.network.connected=true;
  r.receiveCombatEvents([{id:'hit-proof',type:'hit',player_id:'peer',target_id:'ship-own',at_ms:1000,position:[0,0,-10]},
    {id:'laser-proof',type:'laser',player_id:'ship-own',at_ms:1000,position:[0,0,16],end:[0,0,-44]}],1000);
  assert.equal(r.combatStatus.kind,'hit'); assert.equal(r.effects.sample(1000).feedback.kind,'hit');
  assert.ok(r.effects.impacts.length>0);
});


test('initial local entry is guarded against synchronous status callbacks and exit cannot reopen a rejected transport', () => {
  const r=renderer(); r.flight=null; r.wantFlight=false; r.available=true; r.canvas={dataset:{},focus(){}};
  r.network.connected=false; r.network.localSession=false; r.network.self=null;
  let connects=0;
  r.network.connect=()=>{connects++;r.network.localSession=true;r.setPilot(true);};
  r.network.suspend=()=>r.setPilot(true); r.network.resume=()=>{}; r.network.disconnect=()=>{r.network.localSession=false;};
  r.setPilot(true); assert.equal(connects,1); assert.ok(r.flight); assert.equal(r.settingPilot,false);
  r.network.connected=false; r.network.localSession=true; r.setPilot(true); assert.equal(connects,1);
  r.setPilot(false); assert.equal(r.flight,null); assert.equal(r.wantFlight,false); assert.equal(r.network.localSession,false);
});
test('stellar tint and brightness share a bounded per-second cache and preserve the white root', () => {
  const r=renderer(); const now=Date.parse('2026-10-03T10:00:00Z');
  const node={id:'star-warm',kind:'user',star:{checked_at:new Date(now).toISOString(),post_count:{public:50,exact:true},
    last_public_post_at:new Date(now).toISOString(),activity:{window_days:14,window_end:new Date(now).toISOString(),
    recent_posts:20,previous_posts:20,active_days:6,previous_active_days:6,exact:true}}};
  const first=r.style(node,now); assert.equal(first.stellar.stage,'star'); assert.ok(first.color[0]>first.color[2]);
  assert.equal(r.style(node,now+400),first); assert.notEqual(r.style(node,now+1100),first);
  const root=r.style({...node,id:'u_root'},now); assert.deepEqual(root.color,[1,.98,.94]);
});


test('network loss preserves local throttle and held steering, then same-region recovery continues that intent', () => {
  const r=renderer(); r.updateFlight(0); r.flightThrottle={value:.75,reset(){this.value=0;}};
  r.keys.add('arrowright'); r.network.connected=false; r.network.localSession=true;
  r.updateFlight(.04); assert.equal(r.flightThrottle.value,.75); assert.ok(r.keys.has('arrowright'));
  const heading=r.flight.yaw; r.network.connected=true;
  r.receiveFlightHello({self:r.network.self,server_time_ms:1000});
  assert.equal(r.flightThrottle.value,.75); assert.ok(r.keys.has('arrowright')); assert.equal(r.flight.yaw,heading);
  r.sendFlightInput(); assert.equal(r.network.input.throttle,.75);
  r.stopFlightInput(false); assert.equal(r.flightThrottle.value,0);
});
