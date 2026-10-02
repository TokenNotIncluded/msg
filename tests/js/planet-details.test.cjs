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
  assert.equal(P.surface('u_a',100,{mobile:true}).level,4);
  assert.equal(P.surface('u_a',100,{software:true}).level,4);
  assert.equal(P.surface('u_a',32,{mobile:true}).level,3);
  assert.equal(P.surface('u_a',32,{software:true}).level,3);
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
test('near crater rims are closed annuli with bounded geometry inside the original collider',()=>{
  const body=P.surface('u_e9ee0fdafccb77bb55dbd49106b921f6',70,{mobile:true});
  assert.equal(body.traits.mode,2);
  const rims=body.faces.filter(face=>face.rim);
  assert.ok(rims.length>0&&rims.length<=P.BUDGET.rimFaces);
  const first=rims.slice(0,P.BUDGET.rimSegments*2),edges=new Map();
  for(const face of first)for(let i=0;i<3;i++) {
    const a=face.vertices[i],b=face.vertices[(i+1)%3],key=[a.join(','),b.join(',')].sort().join('|');
    edges.set(key,(edges.get(key)||0)+1);
    assert.ok(Math.hypot(...a)>=P.BUDGET.surfaceMin&&Math.hypot(...a)<=1+1e-10);
  }
  assert.ok([...edges.values()].every(count=>count===1||count===2),'no torn or multiply covered edges');
  assert.equal([...edges.values()].filter(count=>count===1).length,P.BUDGET.rimSegments*2,'only the two closed circular boundaries remain');
  assert.equal(P.surface('u_e9ee0fdafccb77bb55dbd49106b921f6',32).faces.filter(face=>face.rim).length,0,'mid-distance work stays bounded');
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
  querySelectorAll(selector){return selector==='.star-label'?this.children.filter(n=>n.className==='star-label'):[];}
  getBoundingClientRect(){return this.rect||{left:0,top:0,right:0,bottom:0,width:0,height:0};}
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
const rectangle=(left,top,width,height)=>({left,top,right:left+width,bottom:top+height,width,height});
function markerRectangle(marker) {
  const [left,top]=marker.el.style.transform.match(/-?\d+(?:\.\d+)?/g).map(Number);
  return rectangle(left,top,parseFloat(marker.el.style.width),parseFloat(marker.el.style.height));
}
const overlaps=(a,b)=>a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top;
test('avatar placement measures the whole local username and tries another side without shrinking',async()=>{
  dom();const container=new Element(),label=new Element();container.rect=rectangle(120,80,1000,700);
  label.className='star-label';label.rect=rectangle(582,392,240,18);container.append(label);
  const layer=new P.AvatarLayer(container,{origin:'https://msg.example',fetcher:async()=>response()});
  try {
    layer.update(view([user(1)],{project:()=>({x:450,y:320,depth:40,scale:30})}));await drained(layer);
    const badge=markerRectangle(layer.nodes.get('u_1'));
    assert.equal(badge.width,36);assert.equal(badge.height,36);
    assert.ok(badge.right<450,'long username on the right makes the avatar change sides');
    assert.ok(!overlaps(badge,rectangle(462,312,240,18)),'DOM viewport rectangles are converted to container coordinates');
  } finally {layer.dispose();}
});
test('visible flight HUD and labels force a diagonal; hidden HUD does not reserve empty space',async()=>{
  dom();const container=new Element(),label=new Element(),hud=new Element();
  label.className='star-label';label.rect=rectangle(462,300,240,44);container.append(label);
  hud.rect=rectangle(300,285,110,70);document.querySelectorAll=()=>[hud];
  const layer=new P.AvatarLayer(container,{origin:'https://msg.example',fetcher:async()=>response()});
  try {
    const scene=view([user(1)],{project:()=>({x:450,y:320,depth:40,scale:30})});
    layer.update(scene);await drained(layer);
    let badge=markerRectangle(layer.nodes.get('u_1'));
    assert.ok(badge.top<300-6-badge.height,'both horizontal positions are blocked, so the badge moves above');
    assert.ok(!overlaps(badge,label.rect)&&!overlaps(badge,hud.rect));
    hud.hidden=true;layer.update(scene);badge=markerRectangle(layer.nodes.get('u_1'));
    assert.ok(badge.right<450&&badge.top>285,'hidden HUD permits the original left-side alternative');
  } finally {layer.dispose();}
});
test('a full HUD hides and cancels avatars instead of covering controls or fetching hidden images',async()=>{
  dom();const container=new Element(),hud=new Element(),pending=[];
  document.querySelectorAll=()=>hud.hidden?[]:[hud];hud.hidden=true;hud.rect=rectangle(320,210,300,230);
  const layer=new P.AvatarLayer(container,{origin:'https://msg.example',fetcher:(url,options)=>new Promise(resolve=>pending.push({resolve,options}))});
  try {
    const scene=view([user(1)],{project:()=>({x:450,y:320,depth:40,scale:30})});
    layer.update(scene);const request=[...layer.requests.values()][0];assert.equal(layer.nodes.size,1);
    hud.hidden=false;layer.update(scene);
    assert.equal(layer.nodes.size,0);assert.equal(layer.wanted.size,0);assert.equal(layer.requests.size,0);
    assert.ok(pending[0].options.signal.aborted);
    pending[0].resolve(response());await request.promise;
    assert.equal(layer.cache.size,0);assert.equal(layer.nodes.size,0,'late response cannot revive a hidden badge');
    layer.update(scene);assert.equal(pending.length,1,'no request starts while all positions are blocked');
  } finally {layer.dispose();}
});
test('mobile edge placement stays inside the viewport and keeps its 42px size cap',async()=>{
  dom();const layer=new P.AvatarLayer(new Element(),{origin:'https://msg.example',fetcher:async()=>response()});
  try {
    layer.update(view([user(1)],{width:390,project:()=>({x:360,y:220,depth:80,scale:40})}));await drained(layer);
    const badge=markerRectangle(layer.nodes.get('u_1'));
    assert.equal(badge.width,42);assert.ok(badge.left>=12&&badge.right<=378);
  } finally {layer.dispose();}
});
