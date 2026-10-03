/* Decorative geology is seeded by identity; trust and collision radii stay authoritative. */
(() => {
  "use strict";
  const M = globalThis.MSGUniverse;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const mix = (a, b, t) => a + (b - a) * t;
  const dot = (a, b) => a.reduce((s, v, i) => s + v * b[i], 0);
  const unit = v => { const n = Math.hypot(...v) || 1; return v.map(x => x / n); };
  const cross = (a, b) => [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
  const blend = (a, b, t) => a.map((v, i) => mix(v, b[i], t));
  const BUDGET = Object.freeze({ meshEntries: 32, meshPending: 4, idleFaces: 64, idleMs: 3, avatars: 8, mobileAvatars: 4,
    avatarEntries: 16, avatarConcurrent: 2, avatarBytes: 98304, avatarTTL: 60000,
    surfaceMin: .952, surfaceMax: 1, cloudRadius: 1.008, rimSegments: 24, rimFaces: 432 });
  const meshes = new Map(), topologies = new Map(), pending = new Map();
  let buildTimer=null;
  const lod=pixelRadius => pixelRadius>=60 ? 4 : pixelRadius>=14 ? 3 : 2;
  function topology(level) {
    if (topologies.has(level)) return topologies.get(level);
    const top = [0,1,0], bottom = [0,-1,0], rim = [[1,0,0],[0,0,1],[-1,0,0],[0,0,-1]];
    let faces = [];
    for (let i=0; i<4; i++) faces.push([top,rim[(i+1)%4],rim[i]], [bottom,rim[i],rim[(i+1)%4]]);
    for (let i=0; i<level; i++) {
      const next = [];
      for (const [a,b,c] of faces) {
        const middle = (p,q) => unit(p.map((v,k) => v+q[k]));
        const ab=middle(a,b), bc=middle(b,c), ca=middle(c,a);
        next.push([a,ab,ca],[ab,b,bc],[ca,bc,c],[ab,bc,ca]);
      }
      faces=next;
    }
    topologies.set(level,faces); return faces;
  }
  function traits(id) {
    const rng=M.random('geology:v1:'+id), mode=Math.floor(rng()*3);
    const palettes = [
      {sea:[.12,.32,.40],land:[.37,.50,.31],rock:[.66,.61,.43]},
      {sea:[.26,.22,.20],land:[.55,.35,.24],rock:[.76,.58,.38]},
      {sea:[.15,.31,.38],land:[.45,.58,.57],rock:[.66,.75,.73]},
    ];
    const craters=[];
    for (let i=0; i<9; i++) {
      const jitter=[rng()-.5,rng()-.5,rng()-.5], y=1-2*(i+.5)/9, r=Math.sqrt(1-y*y), angle=i*Math.PI*(3-Math.sqrt(5));
      craters.push({center:unit([r*Math.cos(angle)+jitter[0]*.12,y+jitter[1]*.12,r*Math.sin(angle)+jitter[2]*.12]),
        radius:.14+rng()*.16});
    }
    return {mode, palette:palettes[mode], offset:[rng()*61,rng()*61,rng()*61],
      seed:Math.floor(rng()*2147483647), seaLevel:mode===0 ? .38+rng()*.16 : mode===2 ? .28 : -.1,
      phase:rng()*M.TAU, speed:.018+rng()*.045, ice:mode===2 ? .67 : .86+rng()*.08, craters};
  }
  function noise(p, seed) {
    const cell=p.map(Math.floor), f=p.map((v,i) => {const t=v-cell[i]; return t*t*(3-2*t);});
    const hash=(x,y,z) => {
      let n=Math.imul(x,374761393)+Math.imul(y,668265263)+Math.imul(z,2147483647)+seed;
      n=Math.imul(n^(n>>>13),1274126177);return ((n^(n>>>16))>>>0)/4294967296;
    };
    const plane=z => mix(mix(hash(cell[0],cell[1],z),hash(cell[0]+1,cell[1],z),f[0]),
      mix(hash(cell[0],cell[1]+1,z),hash(cell[0]+1,cell[1]+1,z),f[0]),f[1]);
    return mix(plane(cell[2]),plane(cell[2]+1),f[2]);
  }
  function sample(direction,t) {
    const field=(frequency,offset=0) => noise(direction.map((v,i) => v*frequency+t.offset[i]+offset),t.seed);
    const continental=field(2.6)*.64+field(5.2)*.25+field(10.4)*.11;
    const ridge=(1-Math.abs(field(17)*2-1))**3;
    let relief=.013*ridge+.009*(field(34)-.5), crater=0, basin=0, craterRim=0;
    if (t.mode!==0) for (const c of t.craters) {
      const distance=Math.sqrt(Math.max(0,2-2*dot(direction,c.center)))/c.radius;
      if (distance<1.35) {
        const floor=Math.exp(-distance*distance*5), rim=Math.exp(-(((distance-.95)/.14)**2));
        crater += -.022*floor+.009*rim;
        basin=Math.max(basin,floor);craterRim=Math.max(craterRim,rim);
      }
    }
    const water=continental<t.seaLevel;
    // All solid microterrain is INSIDE the existing spherical collider. Clouds are intangible.
    const radius=clamp(.969+(water ? 0 : relief+.017*(continental-.45))+crater,BUDGET.surfaceMin,BUDGET.surfaceMax);
    let color=water ? blend(t.palette.sea,[.09,.19,.25],clamp((t.seaLevel-continental)*2,0,.8)) :
      blend(t.palette.land,t.palette.rock,clamp((continental-.48)*3+ridge*.23,0,1));
    if (t.mode===1) color=blend(color,t.palette.sea,.12+.20*Math.sin(direction[1]*31+continental*9)**2);
    const ice=clamp((Math.abs(direction[1])-t.ice+field(13)*.045)*18,0,1);
    color=blend(color,[.86,.90,.88],ice);
    // Material edges remain legible in earthshine, without lighting the night side like day.
    color=blend(color,color.map(v=>v*.28),basin*.85);
    color=blend(color,[.82,.83,.76],craterRim*.82);
    return {radius,color};
  }
  function* generateSurface(id,level) {
    const t=traits(id), faces=[], verticesByDirection=new Map();
    for (const face of topology(level)) {
      const center=unit(face[0].map((v,i) => v+face[1][i]+face[2][i])), terrain=sample(center,t);
      const vertices=face.map(v=>{
        const key=v.join(',');if(verticesByDirection.has(key))return verticesByDirection.get(key);
        const point=v.map(n=>n*sample(v,t).radius);verticesByDirection.set(key,point);return point;
      });
      let normal=unit(cross(vertices[1].map((v,i)=>v-vertices[0][i]),vertices[2].map((v,i)=>v-vertices[0][i])));
      if (dot(normal,center)<0) normal=normal.map(v=>-v);
      faces.push({vertices,normal,color:id==='u_root' ? blend([.75,.74,.69],[1,.98,.89],terrain.color[0]) : terrain.color,cloud:false});
      yield;
    }
    if (level>=4 && t.mode!==0 && id!=='u_root') for (const crater of t.craters) {
      // Closed annuli give a small projected crater an actual readable rim;
      // sampling its narrow edge once per sphere face loses that contour.
      const axis=Math.abs(crater.center[1])>.9?[1,0,0]:[0,1,0];
      const tangent=unit(cross(crater.center,axis)),bitangent=cross(crater.center,tangent);
      const edge=fraction=>Array.from({length:BUDGET.rimSegments},(_,i)=>{
        const angle=i/BUDGET.rimSegments*M.TAU,span=2*Math.asin(crater.radius*fraction/2);
        const direction=crater.center.map((v,k)=>v*Math.cos(span)+(tangent[k]*Math.cos(angle)+bitangent[k]*Math.sin(angle))*Math.sin(span));
        const radius=Math.min(BUDGET.surfaceMax,sample(direction,t).radius+.005);
        return direction.map(v=>v*radius);
      });
      const inner=edge(.82),outer=edge(1.06);
      for (let i=0;i<BUDGET.rimSegments;i++) {
        const next=(i+1)%BUDGET.rimSegments;
        for (const vertices of [[inner[i],outer[i],outer[next]],[inner[i],outer[next],inner[next]]]) {
          faces.push({vertices,normal:unit(vertices[0].map((v,k)=>v+vertices[1][k]+vertices[2][k])),
            color:blend(t.palette.rock,[.9,.91,.83],.65),cloud:false,rim:true});
          yield;
        }
      }
    }
    let clouds=0;
    if (level>=3 && t.mode!==1 && id!=='u_root') for (const face of topology(level)) {
      const center=unit(face[0].map((v,i)=>v+face[1][i]+face[2][i]));
      const cloud=frequency=>noise(center.map((v,i)=>v*frequency+t.offset[i]+17),t.seed);
      const density=cloud(5)*.7+cloud(12)*.3;
      if (density>.68 && clouds<512) {
        faces.push({vertices:face.map(v=>v.map(n=>n*BUDGET.cloudRadius)),normal:center,
          color:[.82,.87,.87],alpha:clamp((density-.68)*1.1,.025,.16),cloud:true});
        clouds++;
      }
      yield;
    }
    return {faces,traits:t,level};
  }
  function saveMesh(key,result) {
    const identity=key.slice(0,key.lastIndexOf(':')+1);
    meshes.delete(key);meshes.set(key,result);
    // Keep the adjacent LOD when the projected radius crosses a boundary.
    // Repeatedly rebuilding both sides of that boundary causes visible stalls.
    const siblings=[...meshes.keys()].filter(previous=>previous.startsWith(identity));
    while(siblings.length>2)meshes.delete(siblings.shift());
    while(meshes.size>BUDGET.meshEntries) meshes.delete(meshes.keys().next().value);
    return result;
  }
  function surface(id,pixelRadius,options={}) {
    const level=lod(pixelRadius,options),key=id+':'+level;
    if(meshes.has(key))return saveMesh(key,meshes.get(key));
    const builder=generateSurface(id,level);let step;
    do{step=builder.next();}while(!step.done);
    return saveMesh(key,step.value);
  }
  function queueBuild() {
    if(buildTimer!==null || !pending.size)return;
    const work=()=>{
      buildTimer=null;
      if(globalThis.document?.hidden){pending.clear();return;}
      const [key,job]=pending.entries().next().value||[];if(!job)return;
      const start=performance.now();let step;
      // Bounded jobs avoid a near-planet cold-cache stall during flight.
      for(let i=0;i<BUDGET.idleFaces && performance.now()-start<BUDGET.idleMs;i++) {step=job.builder.next();if(step.done)break;}
      if(step?.done){pending.delete(key);saveMesh(key,step.value);job.wake?.();}
      queueBuild();
    };
    buildTimer=globalThis.requestIdleCallback ? requestIdleCallback(work,{timeout:100}) : setTimeout(work,16);
  }
  function renderSurface(id,pixelRadius,{wake,...options}={}) {
    const level=lod(pixelRadius,options),key=id+':'+level;
    if(meshes.has(key))return saveMesh(key,meshes.get(key));
    if(level>2 && !pending.has(key) && pending.size<BUDGET.meshPending) {
      pending.set(key,{builder:generateSurface(id,level),wake});queueBuild();
    }
    // Use already finished geology while the requested detail builds; a new
    // coarse fallback must not evict the previous finished near mesh.
    for(const [saved,body] of meshes)if(saved.startsWith(id+':'))return body;
    return surface(id,0);
  }
  function clearSurfaceCache() {
    meshes.clear();pending.clear();
    if(buildTimer!==null){if(globalThis.cancelIdleCallback)cancelIdleCallback(buildTimer);else clearTimeout(buildTimer);buildTimer=null;}
  }
  function appendSurface(solids,{node,style,clock=0,towardEye=[0,0,1],sun=[0,0,1],pixelRadius=10,software=false,mobile=false,wake,defer=true}) {
    const options={software,mobile,wake},body=(defer?renderSurface:surface)(node.id,pixelRadius,options), t=body.traits;
    const angle=t.phase+clock*t.speed,cloudAngle=t.phase+clock*t.speed*1.24;
    const rotation=[Math.cos(angle),Math.sin(angle)],cloudRotation=[Math.cos(cloudAngle),Math.sin(cloudAngle)];
    const px=node.position[0],py=node.position[1],pz=node.position[2],radius=style.radius;
    let count=0;
    for (const face of body.faces) {
      const [c,s]=face.cloud?cloudRotation:rotation,n=face.normal;
      const nx=n[0]*c-n[2]*s,ny=n[1],nz=n[0]*s+n[2]*c;
      const facing=nx*towardEye[0]+ny*towardEye[1]+nz*towardEye[2];
      if (facing<-.08) continue;
      const diffuse=Math.max(0,nx*sun[0]+ny*sun[1]+nz*sun[2]),rim=(1-Math.max(0,facing))**3;
      const stellar=!style.root && style.stellar, heat=stellar?.known?stellar.heat:0;
      const shade=(style.root ? .52+.45*diffuse : .34+.60*diffuse+.06*rim)*(stellar ? .18+.82*stellar.brightness : 1);
      const red=clamp(face.color[0]*shade*(1+heat*.6),0,1),green=clamp(face.color[1]*shade*(1+heat*.12),0,1),blue=clamp(face.color[2]*shade*(1-heat*.25),0,1),alpha=face.alpha??1;
      for(const v of face.vertices)solids.push(px+(v[0]*c-v[2]*s)*radius,py+v[1]*radius,pz+(v[0]*s+v[2]*c)*radius,
        red,green,blue,alpha,1);
      count++;
    }
    return count;
  }
  function avatarURL(node,origin=globalThis.location?.origin) {
    if (node.kind!=='user' || node.visibility==='private' || node.private===true || !origin) return null;
    const raw=node.artwork?.avatar?.url, profile=node.path;
    if (typeof raw!=='string' || typeof profile!=='string' || !/^\/@[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(profile)) return null;
    try {
      const url=new URL(raw,origin);
      if (url.origin!==origin || url.username || url.password || url.search || url.hash ||
          url.pathname!==profile+'/art/avatar.svg' || raw!==url.pathname) return null;
      return url.href;
    } catch {return null;}
  }
  function svgData(raw) {
    if (!(raw instanceof Uint8Array) || !raw.length || raw.length>BUDGET.avatarBytes) throw Error('Invalid avatar size');
    const svg=new TextDecoder('utf-8',{fatal:true}).decode(raw);
    const plain=svg.replaceAll('http://www.w3.org/2000/svg','');
    if (/<!|url\s*\(|@import|javascript:|data:|https?:|\/\//i.test(plain)) throw Error('External avatar content');
    const doc=new DOMParser().parseFromString(svg,'image/svg+xml'), root=doc.documentElement;
    const allowed=new Set(['svg','g','defs','title','desc','text','tspan','rect','circle','ellipse','path','line',
      'polyline','polygon','style','animate','animateTransform','animateMotion','set']);
    const animated=new Set(['opacity','fill','stroke','transform','x','y','cx','cy','r','rx','ry','d',
      'visibility','stroke-width','font-size']);
    if (root.localName!=='svg' || root.namespaceURI!=='http://www.w3.org/2000/svg') throw Error('Invalid SVG avatar');
    for (const element of [root,...root.querySelectorAll('*')]) {
      if (!allowed.has(element.localName) || element.namespaceURI!==root.namespaceURI) throw Error('Invalid SVG element');
      for (const attr of element.attributes) {
        const name=attr.localName.toLowerCase();
        if (name.startsWith('on') || ['href','src'].includes(name) || name==='attributename' && !animated.has(attr.value.toLowerCase()))
          throw Error('Active SVG avatar');
      }
    }
    let binary=''; for (const value of raw) binary+=String.fromCharCode(value);
    return 'data:image/svg+xml;base64,'+btoa(binary);
  }
  async function avatarBytes(response) {
    if (!response.ok || response.headers.get('content-type')?.split(';')[0].trim().toLowerCase()!=='image/svg+xml')
      throw Error('Invalid avatar response');
    const length=response.headers.get('content-length');
    if (length!==null && (!/^\d+$/.test(length) || Number(length)>BUDGET.avatarBytes)) throw Error('Avatar too large');
    if (!response.body?.getReader) {
      const raw=new Uint8Array(await response.arrayBuffer());
      if (raw.length>BUDGET.avatarBytes) throw Error('Avatar too large');
      return raw;
    }
    const reader=response.body.getReader(), chunks=[]; let lengthRead=0;
    try {
      for (;;) {const {value,done}=await reader.read(); if (done) break;
        lengthRead+=value.length; if(lengthRead>BUDGET.avatarBytes) {await reader.cancel(); throw Error('Avatar too large');}
        chunks.push(value);
      }
    } finally {reader.releaseLock();}
    const raw=new Uint8Array(lengthRead); let offset=0;
    for (const chunk of chunks) {raw.set(chunk,offset); offset+=chunk.length;}
    return raw;
  }
  class AvatarLayer {
    constructor(container,{wake=()=>{},fetcher=globalThis.fetch?.bind(globalThis),now=()=>Date.now(),origin=globalThis.location?.origin}={}) {
      this.container=container; this.wake=wake; this.fetcher=fetcher; this.now=now; this.origin=origin;
      this.nodes=new Map(); this.cache=new Map(); this.requests=new Map(); this.wanted=new Set(); this.timer=0; this.disposed=false; this.sources=new WeakMap();
      this.visibility=()=>{if(document.hidden)this.clear(); else if(this.state)this.update({...this.state,hidden:false});};
      this.pagehide=()=>this.clear();
      globalThis.document?.addEventListener?.('visibilitychange',this.visibility);
      globalThis.addEventListener?.('pagehide',this.pagehide);
    }
    source(node) {
      const raw=node.artwork?.avatar?.url, path=node.path, kind=node.kind, visibility=node.visibility, privateValue=node.private;
      let saved=this.sources.get(node);
      if (!saved || saved.raw!==raw || saved.path!==path || saved.kind!==kind || saved.visibility!==visibility || saved.privateValue!==privateValue || saved.origin!==this.origin) {
        saved={raw,path,kind,visibility,privateValue,origin:this.origin,url:avatarURL(node,this.origin)};
        this.sources.set(node,saved);
      }
      return saved.url;
    }
    obstacles(labelRects) {
      const box=this.container.getBoundingClientRect?.()||{left:0,top:0};
      const doc=this.container.ownerDocument||globalThis.document;
      const elements=[...(Array.isArray(labelRects) ? [] : this.container.querySelectorAll?.('.star-label')||[]),
        ...(doc?.querySelectorAll?.('#pilot-hud, #game-hud, #game-actions, .flightbar, .topbar, #region-map, #inspector, #catalog, #search-box')||[])];
      const rects=Array.isArray(labelRects) ? [...labelRects] : [];
      for(const element of elements) {
        if(element.hidden)continue;
        const style=globalThis.getComputedStyle?.(element);
        if(style && (style.display==='none' || style.visibility==='hidden' || Number(style.opacity)===0))continue;
        const rect=element.getBoundingClientRect?.();
        if(!rect || rect.width<=0 || rect.height<=0)continue;
        rects.push({left:rect.left-box.left,top:rect.top-box.top,right:rect.right-box.left,bottom:rect.bottom-box.top});
      }
      return rects;
    }
    placement(p,size,edge,width,height,obstacles) {
      // Keep the same badge size and distance from its planet. Try the other
      // side, then diagonals and poles; a crowded HUD wins over decoration.
      const diagonal=Math.SQRT1_2;
      for(const [dx,dy] of [[1,0],[-1,0],[diagonal,-diagonal],[-diagonal,-diagonal],
        [diagonal,diagonal],[-diagonal,diagonal],[0,-1],[0,1]]) {
        const left=Math.round(p.x+dx*edge-size/2),top=Math.round(p.y+dy*edge-size/2);
        const right=left+size,bottom=top+size;
        if(left<12 || top<12 || right>width-12 || bottom>height-12)continue;
        if(obstacles.some(rect=>left<rect.right+6 && right>rect.left-6 && top<rect.bottom+6 && bottom>rect.top-6))continue;
        return {left,top};
      }
      return null;
    }
    update(state) {
      if(this.disposed)return; this.state=state;
      if(state.hidden || globalThis.document?.hidden || this.container.isConnected===false) {this.clear(); return;}
      const {nodes,project,width,height,focusId}=state, mobile=width<700 || state.software;
      const candidates=[], now=this.now();
      for (const node of nodes) {
        const url=this.source(node); if(!url)continue;
        const p=project(node.position),radius=state.radius ? state.radius(node,now) : M.appearance(node,now).radius;
        if(!p || p.depth<=radius || p.scale*radius<7 || p.x<16 || p.x>width-16 || p.y<100 || p.y>height-120)continue;
        candidates.push({node,url,p,radius});
      }
      candidates.sort((a,b)=>Number(b.node.id===focusId)-Number(a.node.id===focusId)||a.p.depth-b.p.depth||a.node.id.localeCompare(b.node.id));
      // Batch current DOM measurements before changing any avatar styles.
      // Labels have already been placed by the renderer in this container.
      const obstacles=candidates.length ? this.obstacles(state.labelRects) : [], visible=new Set();
      const chosen=candidates.slice(0,mobile?BUDGET.mobileAvatars:BUDGET.avatars).map(candidate=>{
        const {p,radius}=candidate,size=clamp(p.scale*radius*.8,28,mobile?42:56),edge=p.scale*radius+size*.62+8;
        return {...candidate,size,position:this.placement(p,size,edge,width,height,obstacles)};
      }).filter(candidate=>candidate.position);
      this.wanted=new Set(chosen.map(c=>c.url));
      for(const [url,request] of this.requests) if(!this.wanted.has(url)) {request.controller.abort(); this.requests.delete(url);}
      for(const {node,url,size,position} of chosen) {
        visible.add(node.id); let marker=this.nodes.get(node.id);
        if(!marker) {
          const el=document.createElement('span'),img=document.createElement('img'),fallback=document.createElement('span');
          el.className='planet-avatar';img.alt='';img.draggable=false;img.decoding='async';
          fallback.className='planet-avatar-fallback';fallback.textContent=M.handle(node.name||'')[0]?.toUpperCase()||'·';
          el.append(img,fallback);this.container.append(el);marker={el,img,fallback,url:null};this.nodes.set(node.id,marker);
          img.addEventListener('error',()=>{
            img.removeAttribute('src');el.classList.remove('has-image');
            if(marker.url)this.cache.set(marker.url,{data:null,expires:this.now()+BUDGET.avatarTTL});
          });
        }
        if(marker.url!==url) {marker.url=url;marker.img.removeAttribute('src');marker.el.classList.remove('has-image');}
        const initial=M.handle(node.name||'')[0]?.toUpperCase()||'·',label=M.handle(node.name||'Signal')+' avatar';
        if(marker.fallback.textContent!==initial)marker.fallback.textContent=initial;
        if(marker.el.getAttribute('aria-label')!==label)marker.el.setAttribute('aria-label',label);
        marker.el.style.transform=`translate(${position.left}px,${position.top}px)`;
        marker.el.style.width=marker.el.style.height=size+'px';
        const saved=this.cache.get(url);
        if(saved?.data && marker.img.getAttribute('src')!==saved.data) {marker.img.src=saved.data;marker.el.classList.add('has-image');}
        else if(saved && !saved.data) {marker.img.removeAttribute('src');marker.el.classList.remove('has-image');}
      }
      for(const [id,marker] of this.nodes)if(!visible.has(id)){marker.el.remove();this.nodes.delete(id);}
      this.pump();this.schedule();
    }
    pump() {
      if(this.disposed || !this.fetcher || globalThis.document?.hidden)return;
      for(const url of this.wanted) {
        if(this.requests.size>=BUDGET.avatarConcurrent)break;
        const saved=this.cache.get(url);
        if(this.requests.has(url) || saved && this.now()<saved.expires)continue;
        const controller=new AbortController(), request={controller};this.requests.set(url,request);
        const promise=(async()=>{
          try {
            const response=await this.fetcher(url,{credentials:'omit',mode:'same-origin',redirect:'error',cache:'no-cache',signal:controller.signal});
            if(response.url && response.url!==url)throw Error('Unexpected avatar path');
            const data=svgData(await avatarBytes(response));
            if(controller.signal.aborted || !this.wanted.has(url) || this.disposed)return;
            this.cache.delete(url);this.cache.set(url,{data,expires:this.now()+BUDGET.avatarTTL});
          } catch {
            if(!controller.signal.aborted && !this.disposed){this.cache.delete(url);this.cache.set(url,{data:null,expires:this.now()+BUDGET.avatarTTL});}
          } finally {
            if(this.requests.get(url)===request)this.requests.delete(url);
            while(this.cache.size>BUDGET.avatarEntries)this.cache.delete(this.cache.keys().next().value);
            if(!this.disposed && this.wanted.has(url) && !globalThis.document?.hidden){this.update(this.state);this.wake();}
          }
        })();request.promise=promise;
      }
    }
    schedule() {
      clearTimeout(this.timer);this.timer=0;
      let expires=Infinity;
      for(const url of this.wanted) {const saved=this.cache.get(url);if(saved)expires=Math.min(expires,saved.expires);}
      if(Number.isFinite(expires))this.timer=setTimeout(()=>{this.timer=0;if(this.state)this.update(this.state);this.wake();},Math.max(250,expires-this.now()+1));
    }
    clear() {
      clearTimeout(this.timer);this.timer=0;this.wanted.clear();
      for(const request of this.requests.values())request.controller.abort();this.requests.clear();
      for(const marker of this.nodes.values())marker.el.remove();this.nodes.clear();this.cache.clear();
    }
    dispose() {
      this.disposed=true;this.clear();globalThis.document?.removeEventListener?.('visibilitychange',this.visibility);
      globalThis.removeEventListener?.('pagehide',this.pagehide);this.state=null;
    }
  }
  globalThis.MSGUniversePlanets=Object.freeze({BUDGET,surface,renderSurface,appendSurface,avatarURL,svgData,avatarBytes,AvatarLayer,
    clearSurfaceCache,surfaceCacheSize:()=>meshes.size,surfacePendingSize:()=>pending.size});
})();
