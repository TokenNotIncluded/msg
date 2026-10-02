const {test}=require('node:test');
const assert=require('node:assert/strict');
require('../../src/msg/data/root-web-model.js');
require('../../src/msg/data/root-web-planets.js');
const P=globalThis.MSGUniversePlanets;
const svg='<svg xmlns="http://www.w3.org/2000/svg"><circle r="12"><animate attributeName="r" values="10;12;10" dur="2s" repeatCount="indefinite"/></circle></svg>';
test('identity geology is stable, varied, continuous at shared vertices and bounded by the collider',()=>{
  P.clearSurfaceCache();const a=P.surface('u_ocean',100),b=P.surface('u_rock',100);
  assert.equal(P.surface('u_ocean',100),a);assert.equal(a.level,4);
  assert.notDeepEqual(a.faces.slice(0,30).map(f=>f.color),b.faces.slice(0,30).map(f=>f.color));
  const shared=new Map();let min=1,max=0;
  for(const face of a.faces.filter(f=>!f.cloud))for(const v of face.vertices){
    const r=Math.hypot(...v);min=Math.min(min,r);max=Math.max(max,r);
    assert.ok(r>=P.BUDGET.surfaceMin-1e-10 && r<=1+1e-10);
    const key=v.map(n=>(n/r).toFixed(8)).join(',');
    if(shared.has(key))assert.deepEqual(v,shared.get(key));else shared.set(key,v);
  }
  assert.ok(max-min>.008,'terrain has visible relief');
  P.clearSurfaceCache();assert.deepEqual(P.surface('u_ocean',100),a);
});
test('LOD and cache bound work across eviction; root remains pale and solid terrain stays inside radius',()=>{
  assert.equal(P.surface('u_a',100,{mobile:true}).level,3);
  assert.equal(P.surface('u_a',100,{software:true}).level,3);
  assert.equal(P.surface('u_a',3).faces.length,128);
  for(let i=0;i<64;i++)P.surface('u_'+i,3);
  assert.equal(P.surfaceCacheSize(),P.BUDGET.meshEntries);
  const root=P.surface('u_root',100);assert.equal(root.faces.length,2048);
  assert.ok(root.faces.every(f=>!f.cloud && Math.max(...f.color)-Math.min(...f.color)<.15));
});
test('same geometry serves GPU and software with rotation and day/night shade',()=>{
  const node={id:'u_terrain',position:[30,4,-5]},style={radius:1.5,root:false},config={node,style,pixelRadius:32,towardEye:[0,0,1],sun:[0,0,1],defer:false};
  const first=[],rotated=[],night=[];
  P.appendSurface(first,{...config,software:true,clock:0});
  P.appendSurface(rotated,{...config,software:true,clock:20});
  P.appendSurface(night,{...config,software:true,sun:[0,0,-1]});
  assert.ok(first.length>128*8);assert.equal(first.length%24,0);
  assert.notDeepEqual(first,rotated);assert.notDeepEqual(first,night);
  const day=first.filter((_,i)=>i%8===3).reduce((a,b)=>a+b,0),dark=night.filter((_,i)=>i%8===3).reduce((a,b)=>a+b,0);
  assert.ok(day>dark);
});
test('renderer starts with cached low LOD and builds higher detail in bounded idle chunks',()=>{
  P.clearSurfaceCache();const original=globalThis.requestIdleCallback,cancel=globalThis.cancelIdleCallback,jobs=[];let woke=0;
  globalThis.requestIdleCallback=fn=>{jobs.push(fn);return jobs.length;};globalThis.cancelIdleCallback=()=>{};
  try {
    const first=P.renderSurface('u_staged',100,{wake:()=>woke++});assert.equal(first.level,2);assert.equal(P.surfacePendingSize(),1);
    for(let i=0;i<50;i++)P.renderSurface('u_pending_'+i,100);
    assert.equal(P.surfacePendingSize(),4);
    for(let i=0;i<1000&&jobs.length;i++)jobs.shift()();
    assert.equal(woke,1);assert.equal(P.surfacePendingSize(),0);assert.equal(P.renderSurface('u_staged',100).level,4);
    assert.ok(P.surfaceCacheSize()<=P.BUDGET.meshEntries);
  }finally{P.clearSurfaceCache();globalThis.requestIdleCallback=original;globalThis.cancelIdleCallback=cancel;}
});
const user=(i,extra={})=>({id:'u_'+i,kind:'user',name:'@pilot-'+i,path:'/@pilot-'+i,position:[i,0,0],artwork:{avatar:{url:'/@pilot-'+i+'/art/avatar.svg'}},...extra});
test('avatar sources must match the exact public profile path; no private, cross-origin, credential or alternate paths',()=>{
  const origin='https://msg.example';assert.equal(P.avatarURL(user(1),origin),origin+'/@pilot-1/art/avatar.svg');
  for(const url of ['https://evil.example/@pilot-1/art/avatar.svg','https://msg.example/@pilot-1/art/avatar.svg',
    '/@pilot-2/art/avatar.svg','/@pilot-1/AVATAR.svg','/@pilot-1/art/avatar.svg?still=1','//@msg.example/@pilot-1/art/avatar.svg',
    '/@pilot-1/art/avatar.svg#fragment','/@pilot-1/art/../art/avatar.svg'])
    assert.equal(P.avatarURL(user(1,{artwork:{avatar:{url}}}),origin),null,url);
  assert.equal(P.avatarURL(user(1,{private:true}),origin),null);
  assert.equal(P.avatarURL(user(1,{kind:'private'}),origin),null);
});
test('image media and bounded streaming are checked before an SVG reaches an image',async()=>{
  assert.deepEqual(await P.avatarBytes(new Response(svg,{headers:{'content-type':'image/svg+xml'}})),new TextEncoder().encode(svg));
  await assert.rejects(P.avatarBytes(new Response(svg,{headers:{'content-type':'text/html'}})));
  await assert.rejects(P.avatarBytes(new Response(svg,{headers:{'content-type':'image/svg+xml','content-length':P.BUDGET.avatarBytes+1+''}})));
  const stream=new ReadableStream({start(controller){controller.enqueue(new Uint8Array(P.BUDGET.avatarBytes));controller.enqueue(new Uint8Array(1));controller.close();}});
  await assert.rejects(P.avatarBytes(new Response(stream,{headers:{'content-type':'image/svg+xml'}})));
});
class Element {
  constructor(){this.style={};this.children=[];this.attributes=new Map();this.events={};this.classList={values:new Set(),add(v){this.values.add(v);},remove(v){this.values.delete(v);}};}
  append(...nodes){this.children.push(...nodes);for(const n of nodes)n.parent=this;}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);}
  setAttribute(k,v){this.attributes.set(k,v);}getAttribute(k){return k==='src'?this.src??null:this.attributes.get(k)??null;}
  removeAttribute(k){this.attributes.delete(k);if(k==='src')delete this.src;}
  addEventListener(k,fn){this.events[k]=fn;}
}
function dom(){
  globalThis.document={hidden:false,createElement(){return new Element();},addEventListener(){},removeEventListener(){}};
  globalThis.DOMParser=class {parseFromString(text){return {documentElement:{localName:'svg',namespaceURI:'http://www.w3.org/2000/svg',attributes:[],querySelectorAll(){return [];}}};}};
}
const response=()=>new Response(svg,{headers:{'content-type':'image/svg+xml'}});
const view=(nodes,extra={})=>({nodes,project:([x])=>({x:150+x,y:220,depth:30+x,scale:10}),width:1000,height:700,...extra});
async function drained(layer){for(let i=0;i<20&&layer.requests.size;i++)await Promise.all([...layer.requests.values()].map(r=>r.promise));}
test('only nearby public avatars load, with concurrency, count, cache, animation and refresh bounds',async()=>{
  dom();let now=1000,active=0,peak=0;const calls=[],container=new Element();
  const layer=new P.AvatarLayer(container,{origin:'https://msg.example',now:()=>now,fetcher:async(url,options)=>{
    active++;peak=Math.max(peak,active);calls.push({url,options});await Promise.resolve();active--;return response();
  }});
  try{
    const nodes=Array.from({length:1000},(_,i)=>user(i));
    layer.update(view(nodes));await drained(layer);
    assert.equal(calls.length,8);assert.equal(layer.nodes.size,8);assert.ok(peak<=2);
    assert.ok(calls.every(c=>c.options.credentials==='omit'&&c.options.mode==='same-origin'&&c.options.redirect==='error'&&c.options.cache==='no-cache'));
    assert.ok([...layer.nodes.values()].every(m=>atob(m.img.src.split(',')[1]).includes('<animate')));
    assert.ok([...layer.nodes.values()].every(m=>m.el.style.transform.startsWith('translate(')));
    layer.update(view(nodes));await drained(layer);assert.equal(calls.length,8,'cache reused');
    now+=P.BUDGET.avatarTTL+1;layer.update(view(nodes));await drained(layer);assert.equal(calls.length,16,'current artwork is refetched');
    for(let batch=1;batch<5;batch++){layer.update(view(nodes.slice(batch*8,batch*8+8)));await drained(layer);}
    assert.ok(layer.cache.size<=P.BUDGET.avatarEntries);
    layer.update(view(nodes,{width:600}));await drained(layer);assert.equal(layer.nodes.size,4);
    const marker=[...layer.nodes.values()][0];marker.img.events.error();assert.equal(marker.img.src,undefined);
    layer.update(view(nodes,{width:600}));assert.equal(marker.img.src,undefined,'decode errors stay fallback until refresh');
    document.hidden=true;layer.visibility();assert.equal(layer.nodes.size,0);assert.equal(layer.cache.size,0);assert.equal(layer.requests.size,0);
  }finally{layer.dispose();}
});
test('hidden, departed and disposed markers cancel requests and never resurrect from late responses',async()=>{
  dom();const pending=[];const layer=new P.AvatarLayer(new Element(),{origin:'https://msg.example',fetcher:(url,options)=>new Promise(resolve=>pending.push({resolve,options}))});
  layer.update(view([user(1),user(2)]));const requests=[...layer.requests.values()];assert.equal(requests.length,2);
  layer.update(view([]));assert.equal(layer.nodes.size,0);assert.ok(pending.every(r=>r.options.signal.aborted));
  pending.forEach(r=>r.resolve(response()));await Promise.all(requests.map(r=>r.promise));assert.equal(layer.cache.size,0);
  layer.update(view([user(3)]));const last=[...layer.requests.values()][0];layer.dispose();pending.at(-1).resolve(response());await last.promise;
  assert.equal(layer.nodes.size,0);assert.equal(layer.cache.size,0);
});
test('a failed refresh removes the old artwork and a changed projected path fetches its current avatar',async()=>{
  dom();let now=1000,fail=false;const calls=[];
  const layer=new P.AvatarLayer(new Element(),{origin:'https://msg.example',now:()=>now,fetcher:async url=>{
    calls.push(url);return fail?new Response('<html>denied</html>',{status:403,headers:{'content-type':'text/html'}}):response();
  }});
  try {
    layer.update(view([user(1)]));await drained(layer);const marker=layer.nodes.get('u_1');assert.ok(marker.img.src);
    fail=true;now+=P.BUDGET.avatarTTL+1;layer.update(view([user(1)]));await drained(layer);assert.equal(marker.img.src,undefined);
    layer.update(view([user(1)]));assert.equal(calls.length,2,'failed refresh is bounded');
    fail=false;const renamed=user(1,{path:'/@new-name',artwork:{avatar:{url:'/@new-name/art/avatar.svg'}}});
    layer.update(view([renamed]));await drained(layer);assert.ok(marker.img.src);assert.equal(calls.at(-1),'https://msg.example/@new-name/art/avatar.svg');
  }finally{layer.dispose();}
});
