const {test} = require('node:test');
const assert = require('node:assert/strict');
require('../../src/msg/data/root-web-model.js');
require('../../src/msg/data/root-web-renderer.js');
const {FlightStick} = globalThis.MSGUniverseFlight;

function element(axis) {
  const handlers = new Map(), captures = new Set(), attributes = new Map(), classes = new Set();
  return {
    dataset:{flightAxis:axis}, style:{setProperty(name,value) {attributes.set(name,value);}},
    parentElement:{querySelector() {return null;}}, classList:{toggle(name,active) {active ? classes.add(name) : classes.delete(name);}},
    addEventListener(name,handler) {handlers.set(name,handler);}, setAttribute(name,value) {attributes.set(name,value);},
    getBoundingClientRect() {return {top:100,left:100,width:axis==='pitch'?58:176,height:axis==='pitch'?132:58};},
    setPointerCapture(id) {captures.add(id);}, hasPointerCapture(id) {return captures.has(id);},
    releasePointerCapture(id) {captures.delete(id); handlers.get('lostpointercapture')?.({pointerId:id});},
    emit(name,options={}) {handlers.get(name)?.({button:0,pointerId:1,clientX:188,clientY:166,preventDefault() {},target:{closest() {return null;}},...options});},
    captures, attributes, classes,
  };
}
test('two rods own independent captures, ignore cross-axis movement and release without cancelling the other hand', () => {
  const pitch = element('pitch'), yaw = element('yaw'); let cancellations=0, sticks=[];
  const callbacks={enabled:()=>true,engage() {},change() {},cancel() {cancellations++;for(const stick of sticks)stick.reset();}};
  const left = new FlightStick(pitch,callbacks), right = new FlightStick(yaw,callbacks); sticks=[left,right];
  pitch.emit('pointerdown',{pointerId:11,clientX:129,clientY:200});
  yaw.emit('pointerdown',{pointerId:22,clientX:230,clientY:129});
  assert.ok(left.value>0 && right.value>0); assert.ok(pitch.captures.has(11)); assert.ok(yaw.captures.has(22));
  const pitchValue=left.value;pitch.emit('pointermove',{pointerId:11,clientX:-500,clientY:200});assert.equal(left.value,pitchValue);
  const yawValue=right.value;yaw.emit('pointermove',{pointerId:22,clientX:230,clientY:-500});assert.equal(right.value,yawValue);
  pitch.emit('pointerdown',{pointerId:33,clientY:100});assert.equal(left.pointerId,11,'a second finger cannot steal an owned rod');
  pitch.emit('pointerup',{pointerId:11});assert.equal(left.value,0);assert.equal(right.pointerId,22);assert.equal(cancellations,0);
  const presentation=left.display;left.frame(1/60);assert.ok(left.display<presentation && left.display>0,'thumb returns from its visible position');
  yaw.emit('pointercancel',{pointerId:22});assert.equal(cancellations,1);assert.equal(left.value,0);assert.equal(right.value,0);
  assert.equal(left.display,0);assert.equal(right.display,0);assert.equal(yaw.captures.size,0);
});
test('a released rod can be re-grabbed during its return, and unavailable flight accepts no input', () => {
  const el=element('yaw');let enabled=true;
  const stick=new FlightStick(el,{enabled:()=>enabled,engage() {},change() {},cancel() {}});
  el.emit('pointerdown',{clientX:240});el.emit('pointerup');stick.frame(.02);
  const visible=stick.display, thumbX=188+visible*65;
  el.emit('pointerdown',{pointerId:2,clientX:thumbX,target:{closest:()=>({})}});
  assert.ok(Math.abs(stick.display-visible)<1e-12,'grabbing the returning thumb preserves its actual position');
  el.emit('pointermove',{pointerId:2,clientX:100});assert.equal(stick.value,-1);
  stick.reset();enabled=false;el.emit('pointerdown',{pointerId:3,clientX:260});assert.equal(stick.value,0);assert.equal(stick.pointerId,null);
});
