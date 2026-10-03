/* Deterministic work budgets and unchanged near-planet output, not timing tests. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {createHash} = require('node:crypto');
require('../../src/msg/data/root-web-model.js');
require('../../src/msg/data/root-web-planets.js');
require('../../src/msg/data/root-web-renderer.js');
const P = globalThis.MSGUniversePlanets, Renderer = globalThis.MSGUniverseRenderer;

test('near crater terrain, clouds, rotation and light retain the original GPU vertex bytes', () => {
  P.clearSurfaceCache();
  const config = {node:{id:'u_e9ee0fdafccb77bb55dbd49106b921f6', position:[30,4,-5]},
    style:{radius:3, root:false}, pixelRadius:100, towardEye:[0,0,1], sun:[0,0,1], defer:false};
  // These hashes were captured from the original 7d36ad11 implementation.
  const golden = [
    [0,34056,'7c3ecb4fec691d0d8b12d3e352091d67af69ae8608c59c550fe12319d6b8ef0b'],
    [1,34104,'c566b739a9a740c6e1b49bf774934775e5dcdc66c1b56c3086db2ea946fd9a47'],
    [10,35304,'d6f0b33ddfc54460af2edd7c2028fdf7532898a088a84d7033fed8b43271cb40'],
    [60,34560,'3568b696609e64620663d9c374c749121e93937a7a8657b3392aba099f4abe2d'],
  ];
  for (const [clock,length,hash] of golden) {
    const vertices = []; P.appendSurface(vertices, {...config,clock});
    assert.equal(vertices.length,length);
    assert.equal(createHash('sha256').update(Buffer.from(new Float32Array(vertices).buffer)).digest('hex'),hash);
  }
});

test('crossing a projected LOD boundary reuses both finished meshes without a new job', () => {
  P.clearSurfaceCache(); const medium = P.surface('u_boundary',59), near = P.surface('u_boundary',61);
  for (let i = 0; i < 100; i++) {
    assert.equal(P.renderSurface('u_boundary',59),medium);
    assert.equal(P.renderSurface('u_boundary',61),near);
  }
  assert.equal(P.surfacePendingSize(),0); assert.equal(P.surfaceCacheSize(),2);
});

test('a different requested LOD keeps finished terrain until the bounded idle build completes', () => {
  P.clearSurfaceCache(); const jobs = [], original = globalThis.requestIdleCallback, cancel = globalThis.cancelIdleCallback;
  globalThis.requestIdleCallback = fn => {jobs.push(fn); return jobs.length;}; globalThis.cancelIdleCallback = () => {};
  try {
    const near = P.surface('u_fallback',100);
    assert.equal(P.renderSurface('u_fallback',30),near);
    assert.equal(P.surfaceCacheSize(),1); assert.equal(P.surfacePendingSize(),1);
    for (let i = 0; i < 1000 && jobs.length; i++) jobs.shift()();
    assert.equal(P.renderSurface('u_fallback',30).level,3);
    assert.equal(P.renderSurface('u_fallback',100),near);
    assert.equal(P.surfacePendingSize(),0);
  } finally {P.clearSurfaceCache(); globalThis.requestIdleCallback = original; globalThis.cancelIdleCallback = cancel;}
});

function gpuRenderer() {
  const calls = [], gl = {ARRAY_BUFFER:1, STATIC_DRAW:2, DYNAMIC_DRAW:3, FLOAT:4, POINTS:5, TRIANGLES:6,
    createBuffer() {return {};}, bindBuffer(target,buffer) {this.bound = buffer;},
    vertexAttribPointer() {}, uniform1f() {},
    bufferData(target,data,usage) {calls.push({type:'allocate',buffer:this.bound,usage,data:typeof data === 'number' ? data : [...data]});},
    bufferSubData(target,offset,data) {calls.push({type:'upload',buffer:this.bound,data:[...data]});},
    drawArrays(mode,start,count) {calls.push({type:'draw',buffer:this.bound,mode,count});},
  };
  const r = Object.create(Renderer.prototype);
  Object.assign(r,{gl,attributes:[{loc:0,size:3,offset:0}],uniforms:{uPoints:0}});
  return {r,gl,calls};
}
test('static clouds and all 2300 glyphs upload once, and a confirmed bitmap replacement uploads once more', () => {
  const {r,gl,calls} = gpuRenderer();
  const dust = new Float32Array(2300 * 8), clouds = new Float32Array(24 * 8);
  for (let frame = 0; frame < 10; frame++) {
    r.drawVertices('clouds',clouds,gl.POINTS,3,true);
    r.drawVertices('dust',dust,gl.POINTS,2,true);
  }
  assert.equal(calls.filter(c => c.type === 'allocate').length,2);
  assert.equal(calls.filter(c => c.type === 'upload').length,0);
  assert.equal(calls.filter(c => c.type === 'draw' && c.count === 2300).length,10);
  r.drawVertices('dust',new Float32Array(2299 * 8),gl.POINTS,2,true);
  assert.equal(calls.filter(c => c.type === 'allocate').length,3);
  assert.equal(calls.at(-1).count,2299);
});
test('dynamic buffers reuse capacity, keep passes separate and upload each changed frame', () => {
  const {r,gl,calls} = gpuRenderer(), vertices = Array(24).fill(1);
  r.drawVertices('solids',vertices,gl.TRIANGLES);
  vertices[0] = 9; r.drawVertices('solids',vertices,gl.TRIANGLES);
  r.drawVertices('points',Array(8).fill(3),gl.POINTS);
  r.drawVertices('solids',Array(512).fill(7),gl.TRIANGLES);
  r.drawVertices('solids',vertices,gl.TRIANGLES);
  const allocations = calls.filter(c => c.type === 'allocate'), uploads = calls.filter(c => c.type === 'upload');
  assert.equal(allocations.length,3); assert.equal(uploads.length,5);
  assert.equal(uploads[1].data[0],9); assert.equal(uploads[4].data.length,24);
  assert.equal(uploads[0].buffer,uploads[1].buffer); assert.notEqual(uploads[1].buffer,uploads[2].buffer);
});

test('screen-space culling retains a visible limb and near-plane intersection but rejects empty directions', () => {
  const r = Object.create(Renderer.prototype);
  Object.assign(r,{width:1280,height:800,camera:{target:[0,0,0],distance:34,yaw:0,pitch:0}});
  assert.equal(r.sphereVisible([0,0,0],3),true);
  assert.equal(r.sphereVisible([34,0,0],3),true,'planet limb touches the right edge');
  assert.equal(r.sphereVisible([32.9,0,0],1),true,'an oblique limb survives outside the projected center margin');
  assert.equal(r.sphereVisible([0,0,34],3),true,'camera intersects the sphere');
  assert.equal(r.sphereVisible([1000,0,0],3),false);
  assert.equal(r.sphereVisible([0,0,40],3),false);
});
