const {test} = require('node:test');
const assert = require('node:assert/strict');
require('../../src/msg/data/root-web-model.js');
require('../../src/msg/data/root-web-renderer.js');
const Renderer = MSGUniverseRenderer, {Flight,FlightThrottle} = MSGUniverseFlight;

function element(axis) {
  const handlers = new Map(), captures = new Set(), attrs = new Map(), output = {textContent:''};
  const el = {dataset:{flightAxis:axis},style:{setProperty(k,v) {attrs.set(k,v);}},
    classList:{toggle() {},add() {},remove() {}},parentElement:{querySelector:()=>output},
    addEventListener(type,handler) {if (!handlers.has(type)) handlers.set(type,[]);handlers.get(type).push(handler);},
    setPointerCapture(id) {captures.add(id);},hasPointerCapture:id=>captures.has(id),
    releasePointerCapture(id) {captures.delete(id);el.emit('lostpointercapture',{pointerId:id});},
    getBoundingClientRect:()=>({left:100,top:100,bottom:232,width:axis==='yaw'?176:58,height:132}),
    setAttribute(k,v) {attrs.set(k,v);},getAttribute:k=>attrs.get(k),
    emit(type,values={}) {for (const fn of handlers.get(type)||[]) fn({type,button:0,pointerId:1,
      clientX:129,clientY:210,key:'',preventDefault() {},target:{closest:()=>null},...values});},
    focus() {document.activeElement=el;},closest:()=>null,attrs,captures,output};
  return el;
}
function fixture() {
  const canvas=element(), throttle=element(), pitch=element('pitch'), yaw=element('yaw'), zero=element();
  const win=element(), doc=element();
  globalThis.window=win;globalThis.document=Object.assign(doc,{hidden:false,activeElement:canvas,getElementById:()=>null,
    querySelector:query=>query==='[data-flight-throttle]'?throttle:query==='[data-throttle-zero]'?zero:null,
    querySelectorAll:query=>query==='[data-flight-axis]'?[pitch,yaw]:[],createElement:()=>element()});
  const r=Object.create(Renderer.prototype);
  Object.assign(r,{canvas,flight:new Flight([0,0,16]),keys:new Set(),flightPointers:new Map(),flightControls:new Map(),
    pointers:new Map(),gameActions:new Set(),flightSticks:[],callbacks:{},reduced:{matches:false},available:true,
    wake() {},updateFlightHud() {},network:{connected:true,self:{hp:100},resume() {},suspend() {this.suspended=true;},
      setInput(value) {this.input=value;}}});
  r.flightEvents();return {r,canvas,throttle,pitch,yaw,zero,win,doc};
}

test('mouse drag directly points up/right, ignores other pointers and holds heading on normal release',()=>{
  const {r,canvas}=fixture();
  canvas.emit('pointerdown',{pointerId:10,clientX:400,clientY:300});
  canvas.emit('pointermove',{pointerId:11,clientX:900,clientY:900});assert.equal(r.flight.yaw,0);
  canvas.emit('pointermove',{pointerId:10,clientX:500,clientY:250});
  assert.equal(r.flight.yaw,-.4);assert.equal(r.flight.pitch,-.2);
  assert.equal(r.network.input.yaw,.4);assert.equal(r.network.input.pitch,.2);
  canvas.emit('pointerup',{pointerId:10});assert.equal(r.flightPointers.size,0);assert.equal(canvas.captures.size,0);
  const attitude=[r.flight.yaw,r.flight.pitch];
  for(let i=0;i<60;i++)r.flight.steer(1/60,r.flightSteering());
  assert.deepEqual([r.flight.yaw,r.flight.pitch],attitude);assert.equal(r.network.suspended,undefined);
});
test('canvas dragging accepts one look pointer and cancellation clears held input and oil setting',()=>{
  const {r,canvas,throttle}=fixture();r.flightThrottle.set(.7);r.keys.add('w');
  canvas.emit('pointerdown',{pointerId:1});canvas.emit('pointerdown',{pointerId:2});
  assert.equal(r.flightPointers.size,1);assert.ok(!canvas.captures.has(2));
  canvas.emit('pointercancel',{pointerId:1});assert.equal(r.flightThrottle.value,0);assert.equal(r.keys.size,0);
  assert.equal(r.network.suspended,true);assert.equal(canvas.captures.size,0);
  r.flight=null;canvas.emit('pointerdown',{pointerId:3});assert.equal(r.flightPointers.size,0);
});
test('oil slider retains pointerup setting, supports exact keyboard increments/Home/End and explicit zero',()=>{
  const {r,throttle,zero}=fixture();
  throttle.emit('pointerdown',{pointerId:5,clientY:166});throttle.emit('pointerup',{pointerId:5});
  assert.equal(r.flightThrottle.value,.5);assert.equal(r.network.input.throttle,.5);
  assert.equal(throttle.attrs.get('aria-valuenow'),'50');assert.equal(throttle.captures.size,0);
  throttle.emit('keydown',{key:'ArrowUp'});assert.equal(r.flightThrottle.value,.51);
  throttle.emit('keydown',{key:'End'});assert.equal(r.flightThrottle.value,1);
  throttle.emit('keydown',{key:'Home'});assert.equal(r.flightThrottle.value,0);
  r.flightThrottle.set(.8);zero.emit('click');assert.equal(r.flightThrottle.value,0);assert.equal(r.network.input.throttle,0);
});
test('W/S override the persistent oil setting only while held and brake clears the setting',()=>{
  const {r,canvas,win}=fixture();r.flightThrottle.set(.63);
  canvas.emit('keydown',{key:'w'});r.sendFlightInput();assert.equal(r.network.input.throttle,1);
  win.emit('keyup',{key:'w'});r.sendFlightInput();assert.equal(r.network.input.throttle,.63);
  canvas.emit('keydown',{key:'s'});r.sendFlightInput();assert.equal(r.network.input.throttle,-1);
  win.emit('keyup',{key:'s'});r.sendFlightInput();assert.equal(r.network.input.throttle,.63);
  canvas.emit('keydown',{key:'b'});r.sendFlightInput();assert.equal(r.network.input.throttle,0);
  assert.equal(r.network.input.brake,true);assert.equal(r.flightThrottle.value,0);
  win.emit('keyup',{key:'b'});assert.equal(r.flightIntent().throttle,0);
});
test('two direction pointers and oil pointer stay independent; oil cancellation safely zeros all',()=>{
  const {r,pitch,yaw,throttle}=fixture();
  pitch.emit('pointerdown',{pointerId:11,clientY:200});yaw.emit('pointerdown',{pointerId:22,clientX:240});
  throttle.emit('pointerdown',{pointerId:33,clientY:140});throttle.emit('pointerup',{pointerId:33});
  assert.ok(r.flightSticks.every(stick=>stick.active));assert.ok(r.flightThrottle.value>0);
  pitch.emit('pointerup',{pointerId:11});assert.equal(r.flightSticks[1].pointerId,22);assert.ok(r.flightThrottle.value>0);
  throttle.emit('pointerdown',{pointerId:44,clientY:166});throttle.emit('lostpointercapture',{pointerId:44});
  assert.equal(r.flightThrottle.value,0);assert.ok(r.flightSticks.every(stick=>!stick.active));
});
test('window blur, throttle blur and explicit stop clear persistent thrust without restoring old settings',()=>{
  const {r,throttle,win}=fixture();
  for(const action of [()=>win.emit('blur'),()=>throttle.emit('blur'),()=>r.stopFlightInput(false)]) {
    r.flightThrottle.set(.85);action();assert.equal(r.flightThrottle.value,0);assert.equal(r.flightIntent().throttle,0);
  }
});
test('page hiding and disconnect clear oil; an unavailable flight cannot restart it',()=>{
  const {r,throttle,doc}=fixture();r.camera={target:[0,0,0],distance:34,yaw:0,pitch:0};
  r.reduced.addEventListener=()=>{};globalThis.cancelAnimationFrame=()=>{};r.events();
  r.flightThrottle.set(.8);document.hidden=true;doc.emit('visibilitychange');
  assert.equal(r.flightThrottle.value,0);throttle.emit('keydown',{key:'End'});assert.equal(r.flightThrottle.value,0);
  document.hidden=false;r.flightThrottle.set(.5);r.network.connected=false;
  Object.assign(r,{graph:{nodes:[]},clock:0,remoteShips:new Map(),paused:false,hudTime:0});
  r.updateFlight(.016);assert.equal(r.flightThrottle.value,0);
  throttle.emit('pointerdown',{clientY:100});assert.equal(r.flightThrottle.value,0);
  delete globalThis.cancelAnimationFrame;
});
test('non-flight canvas drag keeps the existing orbit behavior instead of changing a ship attitude',()=>{
  const {r,canvas}=fixture();r.flight=null;r.camera={target:[0,0,0],distance:34,yaw:0,pitch:0};
  r.reduced.addEventListener=()=>{};r.events();
  canvas.emit('pointerdown',{clientX:400,clientY:300});canvas.emit('pointermove',{clientX:500,clientY:250});
  assert.notEqual(r.camera.yaw,0);assert.notEqual(r.camera.pitch,0);assert.equal(r.flightPointers.size,0);
  canvas.emit('pointerup',{clientX:500,clientY:250});assert.equal(r.pointers.size,0);
});

test('focus transfers among gameplay controls retain oil; other UI and window loss still cancel',()=>{
  const {r,canvas,throttle,doc,win}=fixture();
  r.camera={target:[0,0,0],distance:34,yaw:0,pitch:0};r.reduced.addEventListener=()=>{};r.events();
  const action={closest:q=>q.includes('[data-game-action]')?action:null};
  r.flightThrottle.set(.75);throttle.emit('blur',{relatedTarget:canvas});assert.equal(r.flightThrottle.value,.75);
  r.keys.add('w');canvas.emit('blur',{relatedTarget:action});doc.emit('focusin',{target:action});
  assert.equal(r.keys.size,0);assert.equal(r.flightThrottle.value,.75);assert.equal(r.network.suspended,undefined);
  document.activeElement=action;assert.equal(r.flightControlled(),true);
  doc.emit('focusin',{target:{closest:()=>null}});assert.equal(r.flightThrottle.value,0);
  r.flightThrottle.set(.75);win.emit('blur');assert.equal(r.flightThrottle.value,0);
});
test('resting steering spring does not rewrite controls every animation frame',()=>{
  const {r}=fixture(),stick=r.flightSticks[0];let paints=0;const original=stick.paint.bind(stick);
  stick.paint=()=>{paints++;original();};
  for(let i=0;i<120;i++)stick.frame(1/60,0);assert.equal(paints,0);
  stick.frame(1/60,1);assert.ok(paints>0);for(let i=0;i<180;i++)stick.frame(1/60,0);
  assert.equal(stick.display,0);assert.equal(stick.velocity,0);const settled=paints;
  for(let i=0;i<120;i++)stick.frame(1/60,0);assert.equal(paints,settled);
});
