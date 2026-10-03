#!/usr/bin/env node
/* CPU-only benchmark: real renderer prototypes, explicit DOM/GL/Canvas stubs.
 * This does not measure GPU execution, rasterization, browser layout, network,
 * or flight physics. Run the same file against both immutable source roots:
 * node scripts/flight-performance.bench.cjs --source-root /path/to/source --json /tmp/proof.json
 * Timing is evidence, never a pass/fail assertion. Stubs count submitted work.
 */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const {performance} = require('node:perf_hooks');
const {spawnSync} = require('node:child_process');

function options(argv) {
  const result = {sourceRoot:path.resolve(__dirname, '..'), rounds:5, frames:30, warmup:20, json:null};
  for (let i=0; i<argv.length; i++) {
    const option=argv[i], value=argv[++i];
    if (option==='--source-root') result.sourceRoot=path.resolve(value);
    else if (option==='--json') result.json=path.resolve(value);
    else if (['--rounds','--frames','--warmup'].includes(option)) {
      const n=Number(value);
      if (!Number.isSafeInteger(n) || n<1 || n>10000) throw Error('Invalid '+option);
      result[option.slice(2)]=n;
    } else throw Error('Unknown option: '+option);
  }
  return result;
}
const config=options(process.argv.slice(2));
const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const sourceNames=['root-web-model.js','root-web-planets.js','root-web-renderer.js'];
const sources=Object.fromEntries(sourceNames.map(name=>[name,fs.readFileSync(path.join(config.sourceRoot,'src/msg/data',name))]));
const sourceDigests=Object.fromEntries(Object.entries(sources).map(([name,bytes])=>[name,sha(bytes)]));
const counters=()=>({bufferData:0,bufferDataAllocatedBytes:0,bufferDataUploadedBytes:0,
  bufferSubData:0,bufferSubDataUploadedBytes:0,drawArrays:0,drawVertices:0,domWrites:0,
  canvasPaths:0,canvasFills:0,canvasStrokes:0,canvasRects:0,canvasGradients:0});

function environment(software,width,height) {
  let counts=counters(), nextBuffer=0, nextFrame=0;
  const idle=[];
  const reset=()=>{counts=counters();};
  class Element {
    constructor() {
      this.children=[]; this.dataset={}; this.attributes=new Map(); this.parent=null;
      this.classList={add(){counts.domWrites++;},remove(){counts.domWrites++;},toggle(){counts.domWrites++;}};
      this.style=new Proxy({setProperty(){counts.domWrites++;}}, {set(target,key,value){counts.domWrites++;target[key]=value;return true;}});
    }
    set textContent(value){this._text=value;counts.domWrites++;} get textContent(){return this._text||'';}
    append(...elements){for(const el of elements){el.parent=this;this.children.push(el);}counts.domWrites++;}
    remove(){if(this.parent)this.parent.children=this.parent.children.filter(el=>el!==this);counts.domWrites++;}
    replaceChildren(...elements){this.children=[];this.append(...elements);}
    addEventListener(){} removeEventListener(){} setPointerCapture(){} releasePointerCapture(){}
    setAttribute(key,value){this.attributes.set(key,value);counts.domWrites++;}
    getAttribute(key){return this.attributes.get(key)??null;}
    removeAttribute(key){this.attributes.delete(key);counts.domWrites++;}
    querySelectorAll(selector){return selector==='.star-label'?this.children.filter(el=>el.className==='star-label'):[];}
    getBoundingClientRect(){return {left:0,top:0,right:width,bottom:height,width,height};}
    focus(){} contains(el){return this.children.includes(el);}
  }
  const constants=['ARRAY_BUFFER','DYNAMIC_DRAW','STATIC_DRAW','STREAM_DRAW','FLOAT','BLEND','DEPTH_TEST',
    'SRC_ALPHA','ONE','ONE_MINUS_SRC_ALPHA','VERTEX_SHADER','FRAGMENT_SHADER','COMPILE_STATUS','LINK_STATUS',
    'COLOR_BUFFER_BIT','DEPTH_BUFFER_BIT','POINTS','TRIANGLES','LINES'];
  const gl=Object.fromEntries(constants.map((name,i)=>[name,i+1]));
  Object.assign(gl,{
    createBuffer:()=>({id:++nextBuffer}),createShader:()=>({}),createProgram:()=>({}),
    getShaderParameter:()=>true,getProgramParameter:()=>true,getAttribLocation:()=>0,
    getUniformLocation:()=>({}),getParameter:()=>1,
    bufferData(target,data){counts.bufferData++;if(typeof data==='number')counts.bufferDataAllocatedBytes+=data;
      else counts.bufferDataUploadedBytes+=data.byteLength;},
    bufferSubData(target,offset,data){counts.bufferSubData++;counts.bufferSubDataUploadedBytes+=data.byteLength;},
    drawArrays(mode,first,count){counts.drawArrays++;counts.drawVertices+=count;},
  });
  for (const name of ['shaderSource','compileShader','attachShader','linkProgram','deleteShader','useProgram',
    'bindBuffer','enableVertexAttribArray','vertexAttribPointer','enable','disable','blendFunc','clearColor',
    'viewport','clear','uniform3fv','uniform3f','uniform1f','deleteBuffer']) gl[name]=()=>{};
  const ctx={setTransform(){},clearRect(){},beginPath(){counts.canvasPaths++;},moveTo(){},lineTo(){},closePath(){},
    fill(){counts.canvasFills++;},stroke(){counts.canvasStrokes++;},fillRect(){counts.canvasRects++;},
    createRadialGradient(){counts.canvasGradients++;return {addColorStop(){}};}};
  const labels=new Element(), tokenField=new Element(), canvas=new Element();
  canvas.width=width;canvas.height=height;
  canvas.getContext=kind=>kind==='webgl'?software?null:gl:kind==='2d'?ctx:null;
  const document={hidden:false,activeElement:canvas,getElementById:id=>id==='token-field'?tokenField:null,
    createElement:()=>new Element(),querySelectorAll:()=>[],querySelector:()=>null,addEventListener(){},removeEventListener(){}};
  const sandbox={console,performance,URL,TextEncoder,TextDecoder,Float32Array,Uint8Array,Map,Set,WeakMap,
    document,devicePixelRatio:1,location:{origin:'https://benchmark.invalid'},
    matchMedia:()=>({matches:false,addEventListener(){}}),ResizeObserver:class{observe(){} disconnect(){}},
    requestAnimationFrame:()=>++nextFrame,cancelAnimationFrame(){},requestIdleCallback:fn=>{idle.push(fn);return idle.length;},
    cancelIdleCallback(){},setTimeout:fn=>{idle.push(fn);return idle.length;},clearTimeout(){},addEventListener(){},removeEventListener(){}};
  const context=vm.createContext(sandbox);
  vm.runInContext('window=globalThis;',context);
  for (const name of sourceNames) vm.runInContext(sources[name].toString('utf8'),context,{filename:name});
  const Renderer=context.MSGUniverseRenderer;
  const renderer=new Renderer(canvas,labels,{now:()=>100000, fallback:message=>{throw Error(message);}});
  renderer.avatars?.dispose(); renderer.avatars=null;
  function drain() {let steps=0;while(idle.length){idle.shift()();if(++steps>100000)throw Error('Idle work did not settle');}}
  return {context,renderer,reset,counts:()=>({...counts}),drain};
}

function nodes() {
  const user=(id,position)=>({id,kind:'user',name:'@'+id.slice(2),path:'/@'+id.slice(2),position,star:{}});
  const near=user('u_e9ee0fdafccb77bb55dbd49106b921f6',[0,0,10]);
  const root=user('u_root',[100,0,10]); // The real pinned root can be outside the viewport.
  const mid=Array.from({length:18},(_,i)=>user('u_mid_'+i,[(i%6-2.5)*12, (Math.floor(i/6)-1)*14,-76-i%3*20]));
  const far=Array.from({length:16},(_,i)=>user('u_far_'+i,[(i%8-3.5)*40,(Math.floor(i/8)-.5)*30,-360-i%3*30]));
  const offscreen=Array.from({length:128},(_,i)=>user('u_out_'+i,[180+i*2,70+i%8*9,0]));
  return [near,root,...mid,...far,...offscreen];
}
const scenarios=[{name:'desktop-near-far',width:1280,height:800,software:false},
  {name:'mobile-near-far',width:390,height:844,software:false},
  {name:'software-near-far',width:1280,height:800,software:true}];
const input={version:1,seed:'flight-performance:v1',nodes:nodes(),camera:{target:[0,0,0],distance:24,yaw:0,pitch:0},
  nearId:'u_e9ee0fdafccb77bb55dbd49106b921f6',tokenCount:2300,scenarios};
function frameRenderer(scene) {
  const env=environment(scene.software,scene.width,scene.height),r=env.renderer,M=env.context.MSGUniverse;
  r.graph={nodes:nodes(),links:[]};r.focusId=input.nearId;r.camera=structuredClone(input.camera);r.reindex();
  r.flight=new env.context.MSGUniverseFlight.Flight([0,0,16]);
  r.network={connected:true,serverNow:100000,self:{id:'own',position:[0,0,16],velocity:[0,0,0],yaw:0,pitch:0,hp:100,fuel:100}};
  // Keep the authoritative sample and camera fixed: this benchmark isolates rendering,
  // and does not substitute an invented implementation for flight/network physics.
  r.updateFlight=()=>{};r.paused=false;r.last=0;
  r.prepareView();
  for(const node of r.view.nodes)if(node.kind==='user') {
    const style=M.appearance(node,100000),p=r.project(node.position);
    env.context.MSGUniversePlanets.surface(node.id,(p?.scale||0)*style.radius);
  }
  env.drain();
  const original=r.geometry;
  r.geometry=function(){const result=original.call(this);env.geometry=Object.fromEntries(Object.entries(result).map(([key,array])=>[key,array.length]));return result;};
  env.near=env.context.MSGUniversePlanets.surface(input.nearId,r.project(r.graph.nodes[0].position).scale*1.5);
  env.visible=r.view.nodes.map(node=>node.id);
  return env;
}
const percentile=(samples,fraction)=>{const sorted=[...samples].sort((a,b)=>a-b);return sorted[Math.min(sorted.length-1,Math.ceil(sorted.length*fraction)-1)];};
const summarize=samples=>({median_ms:percentile(samples,.5),p95_ms:percentile(samples,.95),minimum_ms:Math.min(...samples),maximum_ms:Math.max(...samples)});
function measure(env,operation) {
  for(let i=0;i<config.warmup;i++)operation();
  env.drain();
  const values=[],rounds=[],work=[];
  for(let round=0;round<config.rounds;round++) {
    const samples=[];env.reset();
    for(let i=0;i<config.frames;i++) {const start=performance.now();operation();samples.push(performance.now()-start);}
    values.push(...samples);rounds.push(summarize(samples));work.push(env.counts());
  }
  const average=Object.fromEntries(Object.keys(counters()).map(key=>[key,work.reduce((sum,row)=>sum+row[key],0)/(config.rounds*config.frames)]));
  return {...summarize(values),samples:values.length,rounds,submitted_work_per_frame:average,
    final_geometry_floats:env.geometry||null,final_geometry_bytes:env.geometry?Object.values(env.geometry).reduce((a,b)=>a+b,0)*4:null};
}
const results=[];
for (const scene of scenarios) {
  for(const operation of ['geometry','render']) {
    const env=frameRenderer(scene);let timestamp=1000;
    const near=env.near;
    const proof={level:near.level,faces:near.faces.length,rims:near.faces.filter(face=>face.rim).length,
      clouds:near.faces.filter(face=>face.cloud).length,geometry_sha256:sha(JSON.stringify(near.faces))};
    results.push({scene:scene.name,operation,width:scene.width,height:scene.height,
      visible_node_count:env.visible.length,visible_node_ids:env.visible,near_mesh:proof,
      particles:env.renderer.dust.length/8,cloud_points:env.renderer.clouds.length/8,
      ...measure(env,operation==='geometry'?()=>{env.renderer.clock+=1/60;env.renderer.geometry();}:()=>env.renderer.render(timestamp+=1000/60))});
    env.context.MSGUniversePlanets.clearSurfaceCache();env.renderer.avatars?.dispose();
  }
}
{
  const env=environment(false,1280,800),P=env.context.MSGUniversePlanets,body=P.surface(input.nearId,100);
  const node=input.nodes[0],style={radius:1.5,root:false};let clock=0;
  results.push({scene:'near-surface',operation:'appendSurface',near_mesh:{level:body.level,faces:body.faces.length,
    rims:body.faces.filter(face=>face.rim).length,clouds:body.faces.filter(face=>face.cloud).length,
    geometry_sha256:sha(JSON.stringify(body.faces))},...measure(env,()=>{
      const solids=[];P.appendSurface(solids,{node,style,clock:clock+=1/60,towardEye:[0,0,1],sun:[0,0,1],pixelRadius:100,defer:false});
      env.geometry={solids:solids.length};
    })});
  P.clearSurfaceCache();
}
const git=spawnSync('git',['rev-parse','HEAD'],{cwd:config.sourceRoot,encoding:'utf8'});
const proof={schema:'msg.flight-cpu-benchmark/1',source_root:config.sourceRoot,
  source_revision:git.status===0?git.stdout.trim():null,source_digests:sourceDigests,
  benchmark_sha256:sha(fs.readFileSync(__filename)),input_sha256:sha(JSON.stringify(input)),input,
  node:process.version,platform:process.platform,architecture:process.arch,
  rounds:config.rounds,frames_per_round:config.frames,warmup_frames:config.warmup,
  boundaries:['CPU only','GL calls are stubbed; submitted bytes are not GPU execution time',
    'Canvas rasterization and browser DOM/layout are stubbed','No network/physics benchmark','No timing pass/fail assertions'],results};
const output=JSON.stringify(proof,null,2)+'\n';
if(config.json)fs.writeFileSync(config.json,output);
else process.stdout.write(output);
if(config.json)for(const row of results)console.log(`${row.scene}/${row.operation}: median ${row.median_ms.toFixed(3)}ms p95 ${row.p95_ms.toFixed(3)}ms geometry ${row.final_geometry_bytes}B bufferData ${row.submitted_work_per_frame.bufferData} bufferSubData ${row.submitted_work_per_frame.bufferSubData}`);
