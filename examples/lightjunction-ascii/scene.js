(() => {
  "use strict";
  const screen = document.querySelector("#world"), status = document.querySelector("#status");
  const action = document.querySelector("#actions"), start = document.querySelector("#start");
  const pause = document.querySelector("#pause"), stance = document.querySelector("#stance"), coffee = document.querySelector("#coffee");
  const doorControl = document.querySelector("#door");
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  const TAU = Math.PI * 2, ramp = " .,:;itfx*#%@", keys = new Set(), pointers = new Map(), pulses = new Map();
  const held = key => keys.has(key)||[...pointers.values()].includes(key)||pulses.has(key);
  let cols = 112, rows = 60, depth, pixels, last = 0, painted = 0, drag = null, pointerYaw = null;
  let state;
  let lockEpoch=0,lockWanted=null,lockPending=false,lockHeld=null;
  const clamp = (n,a,b) => Math.max(a,Math.min(b,n));
  const smooth = n => n*n*(3-2*n);
  const mix = (a,b,t) => a+(b-a)*t;
  const norm = v => { const d=Math.hypot(...v)||1; return v.map(n=>n/d); };
  const cross = (a,b) => [a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
  const dot = (a,b) => a[0]*b[0]+a[1]*b[1]+a[2]*b[2];
  function sphere(cx,cy,cz,rx,ry,rz,shade=1,nu=32,nv=18) {
    const out=[];
    for(let v=1;v<nv;v++) for(let u=0;u<nu;u++) {
      const a=Math.PI*v/nv,b=TAU*u/nu,nx=Math.sin(a)*Math.cos(b),ny=Math.cos(a),nz=Math.sin(a)*Math.sin(b);
      const n=norm([nx/rx,ny/ry,nz/rz]); out.push([cx+rx*nx,cy+ry*ny,cz+rz*nz,...n,shade]);
    } return out;
  }
  function box(cx,cy,cz,sx,sy,sz,shade=1) {
    const out=[], sizes=[sx,sy,sz], center=[cx,cy,cz];
    for(let ax=0;ax<3;ax++) { const others=[0,1,2].filter(n=>n!==ax),a=others[0],b=others[1];
      const na=Math.max(2,Math.ceil(sizes[a]/.055)),nb=Math.max(2,Math.ceil(sizes[b]/.055));
      for(const side of [-1,1]) for(let i=0;i<=na;i++) for(let j=0;j<=nb;j++) {
        const p=[...center],n=[0,0,0]; p[ax]+=sizes[ax]*side/2;p[a]+=sizes[a]*(i/na-.5);p[b]+=sizes[b]*(j/nb-.5);n[ax]=side;
        out.push([...p,...n,shade]);
      }
    } return out;
  }
  function limb(a,b,r,shade) {
    const axis=norm(b.map((n,i)=>n-a[i])), x=norm(cross(axis,Math.abs(axis[1])<.9?[0,1,0]:[1,0,0])), y=cross(axis,x),out=[];
    for(let k=0;k<=18;k++) for(let i=0;i<18;i++) { const angle=TAU*i/18,n=x.map((v,j)=>v*Math.cos(angle)+y[j]*Math.sin(angle));
      out.push(...[[...a.map((v,j)=>mix(v,b[j],k/18)+r*n[j]),...n,shade]]);
    } return out;
  }
  function cup(cx,cy,cz) {
    const out=[],radius=.09,height=.21;
    for(let i=0;i<48;i++) { const a=TAU*i/48,nx=Math.cos(a),nz=Math.sin(a);
      for(let j=0;j<=14;j++)out.push([cx+radius*nx,cy+height*(j/14-.5),cz+radius*nz,nx,0,nz,.96]);
      for(let j=0;j<12;j++) { const b=TAU*j/12,r=radius+.012*Math.cos(b);
        out.push([cx+r*nx,cy+height/2+.012*Math.sin(b),cz+r*nz,nx*Math.cos(b),Math.sin(b),nz*Math.cos(b),1.08]); }
      for(let j=0;j<=6;j++)out.push([cx+radius*.86*j/6*nx,cy+height/2-.025,cz+radius*.86*j/6*nz,0,1,0,.15]);
    }
    for(let i=0;i<32;i++)for(let j=0;j<12;j++) { const a=TAU*i/32,b=TAU*j/12,n=norm([Math.cos(a)*Math.cos(b),Math.sin(a)*Math.cos(b),Math.sin(b)]);
      out.push([cx+.12+(.064+.014*Math.cos(b))*Math.cos(a),cy+(.075+.014*Math.cos(b))*Math.sin(a),cz+.014*Math.sin(b),...n,.93]); }
    return out;
  }
  const chair=[...box(0,1.06,0,1.1,.13,1.04,.86),...box(0,2.40,-.47,1.12,.22,.13,.86)];
  for(const x of [-.47,.47]) for(const z of [-.44,.44]) chair.push(...box(x,z<0?1.24:.50,z,.12,z<0?2.48:1.0,.12,.82));
  for(const x of [-.28,0,.28]) chair.push(...box(x,1.78,-.47,.09,1.10,.10,.86));
  const person=[...sphere(.08,1.48,.22,.31,.41,.22,.94),...limb([.08,1.74,.22],[.20,1.83,.46],.11,.95),...sphere(.20,1.94,.49,.24,.27,.24,1.08),...sphere(.02,1.10,.06,.34,.17,.30,.82)];
  for(const side of [-1,1]) {
    person.push(...limb([side*.27,1.72,.22],[side*.39,1.26,.43],.10,.94),...limb([side*.39,1.26,.43],[side*.24,1.10,.63],.08,.96));
    person.push(...limb([side*.18,1.12,.22],[side*.25,.90,.86],.13,.80),...limb([side*.25,.90,.86],[side*.30,.20,1.22],.10,.80));
    person.push(...box(side*.30,.13,1.34,.20,.14,.40,.74));
  }
  const table=[...box(-.88,.82,.60,.54,.09,.54,.58),...box(-.88,.40,.60,.10,.80,.10,.58)];
  const mug=cup(-.88,.97,.60);
  const unitCloud=sphere(0,0,0,1,1,1,1,72,32);
  const rimCloud=sphere(0,0,0,1,1,1,1,22,12);
  const shelter=createShelterWorld({box,sphere,limb});
  const craters=[[2.7,6,2.2,.95],[-3.9,11,2.8,1.3],[1,18,4.2,1.65],[5,1.2,1.4,.65]];
  function terrain(x,z) {
    if(!state.aftermath)return 0;
    let y=0;
    for(const [cx,cz,r,d] of craters){const q=Math.hypot(x-cx,z-cz)/r;if(q<1)y-=d*(1-q*q)**2;else if(q<1.2)y+=.18*Math.sin((q-1)/.2*Math.PI);}
    return y;
  }
  function floorAt(x=state.x,z=state.z,room=state.room) {
    if(room)return -2.6;
    if(Math.abs(x)<.78&&z<-2&&z>=-5.05)return -2.6*(-z-2)/3.05;
    return terrain(x,z);
  }
  function nearDoor() { return Math.hypot(state.x,state.z+5.05)<1.5&&floorAt()<-.9; }
  function moveTo(x,z) {
    if(state.room){
      if(Math.abs(x)>2.40||z<-9.4)return;
      if(z>-5.05){if(!state.doorOpen||state.doorAngle<1.3||Math.abs(x)>.66)return;state.room=false;}
    }else if((floorAt()<-.05&&!state.aftermath)||(Math.abs(state.x)<.78&&state.z<-2&&state.z>=-5.05)){
      if(z<-2&&Math.abs(x)>.66)return;
      if(z<-5.05){if(!state.doorOpen||state.doorAngle<1.3||Math.abs(x)>.66)return;state.room=true;}
    }else if(Math.abs(x)<.78&&z<-2.25&&z>=-5.05)return;
    state.x=x;state.z=z;
  }
  let renderCamera;
  function camera() {
    const eye=state.standing?2.06:1.76, bob=state.moving?Math.sin(state.step*9)*.026:0;
    let pos=[state.x,floorAt()+eye+bob,state.z-.10], f=norm([Math.sin(state.yaw)*Math.cos(state.pitch),Math.sin(state.pitch),Math.cos(state.yaw)*Math.cos(state.pitch)]);
    if(state.time<6) { const t=smooth(clamp((state.time-2.3)/3.7,0,1)),a=mix(1.02,Math.PI,t),r=mix(5.1,.10,t);
      pos=[Math.sin(a)*r,mix(2.7,eye,t),Math.cos(a)*r];
      const target=[0,mix(1.40,eye,smooth(clamp((t-.55)/.45,0,1))),20*smooth(clamp((t-.65)/.35,0,1))];
      f=norm(target.map((n,i)=>n-pos[i]));
    }
    if(state.dead) { const t=clamp((state.time-state.deathTime)/1.2,0,1); pos[1]=floorAt()+mix(eye,.24,smooth(t)); f=norm([Math.sin(state.yaw),-.18,Math.cos(state.yaw)]); }
    const right=norm([f[2],0,-f[0]]),up=cross(f,right);
    return {pos,f,right,up};
  }
  function point(p, glyph=null, cameraSpace=false) {
    let relative;
    if(cameraSpace) relative=p.slice(0,3);
    else {
      const a=renderCamera.pos;
      if(a[1]<-.25&&p[1]>-.25){const t=(-.25-a[1])/(p[1]-a[1]),x=mix(a[0],p[0],t),z=mix(a[2],p[2],t);if(Math.abs(x)<2.6&&z<-5.05&&z>-9.6)return;}
      if((a[1]>0&&p[1]<0)||(a[1]<0&&p[1]>0)){
        const t=-a[1]/(p[1]-a[1]),x=mix(a[0],p[0],t),z=mix(a[2],p[2],t);
        const stairOpening=Math.abs(x)<.85&&z<-2&&z>-5.05;
        const craterOpening=state.aftermath&&craters.some(([cx,cz,r])=>Math.hypot(x-cx,z-cz)<r);
        if(!stairOpening&&!craterOpening)return;
      }
      relative=p.slice(0,3).map((n,i)=>n-a[i]);
    }
    const z=cameraSpace?relative[2]:dot(relative,renderCamera.f); if(z<.08) return;
    const x=cameraSpace?relative[0]:dot(relative,renderCamera.right), y=cameraSpace?relative[1]:dot(relative,renderCamera.up);
    const col=Math.round(cols/2+cols*.91*x/z),row=Math.round(rows/2-cols*.51*y/z);
    if(col<0||col>=cols||row<0||row>=rows) return;
    const at=row*cols+col; if(z>=depth[at]) return; depth[at]=z;
    const lit=clamp(p[6]*(.25+.70*Math.max(0,p[3]*.35+p[4]*.8+p[5]*.5)),.10,1);
    pixels[at]=glyph||ramp[Math.max(1,Math.round(lit*(ramp.length-1)))];
  }
  function model(points,offset=[0,0,0],scale=[1,1,1],shade=1) {
    for(const p of points) point([p[0]*scale[0]+offset[0],p[1]*scale[1]+offset[1],p[2]*scale[2]+offset[2],p[3],p[4],p[5],p[6]*shade]);
  }
  function cloud() {
    const t=state.time-8; if(t<0||state.aftermath) return;
    if(t<2) { const r=1+t*3;model(unitCloud,[0,4+t*2,48],[r,r,r],1.12); }
    else { const rise=clamp((t-2)/10,0,1),r=7+11*rise,y=11+15*rise;
      model(unitCloud,[0,y,48],[r,4+3*rise,r*.82],1.12);
      for(let i=0;i<8;i++) { const a=TAU*i/8;model(rimCloud,[Math.cos(a)*r*.68,y-.5,48+Math.sin(a)*r*.60],[r*.38,3+rise,r*.34],.97); }
      for(let i=0;i<7;i++) { const h=1+i*(y-3)/7;model(rimCloud,[Math.sin(i+t*.22)*.6,h,48],[1.7+Math.sin(i)*.25,(y-3)/7,1.8],.71); }
      model(rimCloud,[0,.6,48],[7+rise*8,.8,7+rise*8],.58);
    }
    const radius=Math.max(0,(t-3)*5.8);
    for(let i=0;i<860;i++) { const a=i*TAU/860;point([Math.cos(a)*radius,.08,48+Math.sin(a)*radius,0,1,0,.85],":"); }
    if(t<4) for(let i=0;i<180;i++) { const a=i*2.399,r=t*(1+(i%11)*.25),h=(1+(i%7)*.4)*t-t*t*.4;
      if(h>0) point([Math.cos(a)*r,h,48+Math.sin(a)*r,0,1,0,1],"+");
    }
  }
  function hands() {
    if(state.time<6||state.dead) return;
    const drink=clamp((state.time-state.sipTime)/2.5,0,1),sipping=drink>0&&drink<1;
    if(sipping) { const lift=Math.sin(drink*Math.PI),x=mix(.16,-.06,lift),y=mix(-.32,-.10,lift),z=mix(.66,.34,lift);
      for(const p of cup(x,y,z)) point(p,null,true);
      for(const p of sphere(x+.08,y-.08,z+.035,.065,.055,.06,.8,28,16)) point(p,null,true);
      for(const p of limb([.37,-.48,.78],[x+.09,y-.09,z+.035],.065,.8)) point(p,null,true);
    } else if(state.moving) { const swing=Math.sin(state.step*8)*.05;
      for(const side of [-1,1]) for(const p of sphere(side*.28,-.36+side*swing,.66,.08,.10,.09,.74,20,12)) point(p,null,true);
    }
  }
  function roomBackdrop() {
    if(!state.room)return;
    const cam=renderCamera;
    const planes=[[0,-2.6,0,.38],[0,2.6,0,.38],[1,-2.6,0,.27],[1,-.25,0,.23],[2,-9.6,0,.44],[2,-5.05,0,.42]];
    for(let row=0;row<rows;row++)for(let col=0;col<cols;col++){
      const x=(col-cols/2)/(cols*.91),y=-(row-rows/2)/(cols*.51),ray=cam.f.map((v,i)=>v+cam.right[i]*x+cam.up[i]*y),at=row*cols+col;
      for(const [axis,value,_,shade] of planes){if(Math.abs(ray[axis])<.001)continue;const t=(value-cam.pos[axis])/ray[axis];if(t<.08||t>=depth[at])continue;
        const p=cam.pos.map((v,i)=>v+ray[i]*t);if(Math.abs(p[0])>2.601||p[1]<-2.601||p[1]>-.249||p[2]<-9.601||p[2]>-5.049)continue;
        if(axis===2&&value===-5.05&&Math.abs(p[0])<.85&&p[1]<-.35&&(state.doorOpen||state.doorAngle>.04))continue;
        depth[at]=t;const seam=axis===1?false:Math.abs((p[1]+2.6)*4-Math.round((p[1]+2.6)*4))<.04;
        const doorFace=axis===2&&value===-5.05&&Math.abs(p[0])<.85&&p[1]<-.35;
        pixels[at]=seam?":":ramp[Math.round((doorFace?.65:shade)*(ramp.length-1))];
      }
    }
  }
  function render() {
    renderCamera=camera();depth.fill(Infinity);pixels.fill(" ");
    roomBackdrop();
    const cx=Math.round(state.x),cz=Math.round(state.z);
    for(let x=cx-22;x<=cx+22;x++) for(let z=cz-12;z<=cz+45;z++) if((x+z)%2===0&&!(Math.abs(x)<.85&&z<-2&&z>-5.05)) point([x,terrain(x,z),z,0,1,0,.16],".");
    model(shelter.entrance);model(shelter.stairs);if(floorAt()<-.65)model(shelter.chamber);
    const angle=state.doorAngle;
    for(const p of shelter.door){const dx=p[0]+.85,dz=p[2]+5.05,c=Math.cos(angle),s=Math.sin(angle);point([-.85+dx*c+dz*s,p[1],-5.05-dx*s+dz*c,p[3]*c+p[5]*s,p[4],-p[3]*s+p[5]*c,p[6]]);}
    if(state.aftermath){model(shelter.ruins);for(const [x,z,r,d] of craters)for(let i=0;i<80;i++)for(let j=0;j<=16;j++){const a=i*TAU/80,q=r*j/16,px=x+Math.cos(a)*q,pz=z+Math.sin(a)*q,g=4*d*(1-q*q/(r*r))/(r*r),normal=norm([-g*(px-x),1,-g*(pz-z)]);point([px,terrain(px,pz),pz,...normal,.17+.56*(j/16)**2],j===16?"*":null);}}
    else {model(chair,[0,0,0],[1,1,1],.58);model(table);if(state.time-state.sipTime>2.5) model(mug);}
    if(state.time<5.4) model(person);
    cloud();hands();
    const lines=[];for(let r=0;r<rows;r++) lines.push(pixels.slice(r*cols,(r+1)*cols).join(""));
    screen.textContent=lines.join("\n");
    screen.dataset.phase=state.dead?"dead":state.waiting?"waiting":state.aftermath?"aftermath":state.survived?"sheltered":state.time<6?"third-person":state.time<8?"first-person":"choice";
    screen.dataset.cycle=String(state.cycle);screen.dataset.stance=state.standing?"standing":"sitting";
    screen.dataset.position=`${state.x.toFixed(2)},${state.z.toFixed(2)}`;
    screen.dataset.yaw=state.yaw.toFixed(3);screen.dataset.time=state.time.toFixed(2);
    screen.dataset.sipping=String(state.time-state.sipTime>=0&&state.time-state.sipTime<2.5);
    screen.dataset.room=String(state.room);screen.dataset.floor=floorAt().toFixed(2);screen.dataset.door=state.doorOpen?"open":state.doorAngle>.04?"closing":"closed";screen.dataset.pitch=state.pitch.toFixed(3);
  }
  function announce(message) { status.textContent=message;state.noticeUntil=state.time+2.5; }
  function ready() { return !document.hidden&&!state.waiting&&!state.paused&&!state.dead&&state.time>=8.5; }
  function clearInput() { keys.clear();pointers.clear();for(const timer of pulses.values())clearTimeout(timer);pulses.clear();drag=null;state.moving=false;document.querySelectorAll("[data-move]").forEach(b=>b.classList.remove("held")); }
  function releasePointer() {lockEpoch++;lockWanted=null;if(document.pointerLockElement)document.exitPointerLock();}
  function requestPointer() {
    if(lockPending||!ready()||document.pointerLockElement)return;
    const epoch=lockEpoch;lockWanted=epoch;lockPending=true;
    try {const pending=screen.requestPointerLock();
      pending?.then(()=>{lockPending=false;if(epoch!==lockEpoch||!ready())if(document.pointerLockElement===screen&&(lockHeld===epoch||lockHeld===null))releasePointer();},()=>{lockPending=false;if(lockWanted===epoch)lockWanted=null;});
    }catch{lockPending=false;if(lockWanted===epoch)lockWanted=null;}
  }
  function reset() {
    const cycle=state?state.cycle+1:1;
    releasePointer();state={cycle,time:0,x:0,z:0,yaw:0,pitch:0,standing:false,moving:false,step:0,sipTime:-99,noticeUntil:0,paused:false,waiting:reduce.matches,dead:false,deathTime:0,room:false,doorOpen:false,doorAngle:0,survived:false,aftermath:false};
    clearInput();last=0;pointerYaw=null;status.textContent=state.waiting?"椅子上，有个瘫坐的人。开始后可拖动视角、起身、喝咖啡。":"椅子上，有个瘫坐的人。";
    updateControls();render();
  }
  function updateControls() {
    const live=ready(),nearChair=!state.aftermath&&Math.hypot(state.x,state.z)<1.4,nearCoffee=!state.aftermath&&Math.hypot(state.x+.88,state.z-.6)<1.8;
    start.hidden=!state.waiting;action.hidden=state.waiting;
    stance.disabled=!live||(state.standing&&!nearChair);stance.textContent=state.standing?"[E] 坐回":"[E] 起身";
    coffee.disabled=!live||!nearCoffee||state.time-state.sipTime<3;pause.textContent=state.paused?"[P] 继续":"[P] 暂停";
    doorControl.disabled=!live||!nearDoor();doorControl.textContent=state.doorOpen?"[E] 关避难门":"[E] 开避难门";
    if(nearDoor())stance.textContent=state.standing?"坐回":"起身";
    document.querySelectorAll("[data-look],[data-move]").forEach(b=>b.disabled=!live);
  }
  function toggleStance() { if(!ready()) return; if(state.standing&&(state.aftermath||Math.hypot(state.x,state.z)>=1.4)) return;
    state.standing=!state.standing;if(!state.standing){state.x=0;state.z=0;clearInput();}announce(state.standing?"你站起来了。WASD / 方向键移动。":"你坐回了椅子。");updateControls();
  }
  function sip() { if(!ready()||coffee.disabled) return;state.sipTime=state.time;announce("你拿起旁边的咖啡，喝了一口。");updateControls(); }
  function toggleDoor() {if(!ready()||!nearDoor())return;state.doorOpen=!state.doorOpen;announce(state.doorOpen?"门正在打开。沿楼梯进出；冲击波结束前开门会失去保护。":"门正在关闭。");updateControls();}
  function interact() { if(nearDoor())toggleDoor();else toggleStance(); }
  function face(away) { if(!ready())return;const target=Math.atan2(-state.x,48-state.z)+(away?Math.PI:0);pointerYaw=target;announce(away?"你背过身，不再看那道光。":"你默默面对那道光。"); }
  function togglePause() { if(state.waiting)return;state.paused=!state.paused;clearInput();releasePointer();last=0;announce(state.paused?"已暂停。P / 继续。":"继续。");updateControls();render(); }
  function moveKey(key) { return ["w","a","s","d","arrowup","arrowdown","arrowleft","arrowright"].includes(key); }
  function advance(dt) {
    state.time+=dt;
    const previousDoor=state.doorAngle;
    state.doorAngle=mix(state.doorAngle,state.doorOpen?Math.PI/2:0,Math.min(1,dt*9));
    if(!state.doorOpen&&previousDoor>.04&&state.doorAngle<=.04)announce(state.room?"地下室的门已关好。待在里面，等冲击波过去。":"门已关好，但你仍在门外。");
    if(state.dead){if(state.time-state.deathTime>6)reset();return;}
    if(pointerYaw!==null&&ready()){let d=Math.atan2(Math.sin(pointerYaw-state.yaw),Math.cos(pointerYaw-state.yaw));state.yaw+=d*Math.min(1,dt*5);if(Math.abs(d)<.01)pointerYaw=null;}
    let forward=(held("w")||held("arrowup")?1:0)-(held("s")||held("arrowdown")?1:0),side=(held("d")||held("arrowright")?1:0)-(held("a")||held("arrowleft")?1:0);
    state.moving=ready()&&(forward!==0||side!==0);
    if(state.moving){state.standing=true;const n=Math.hypot(forward,side),speed=held("shift")?3.3:2.5;
      moveTo(state.x+(Math.sin(state.yaw)*forward+Math.cos(state.yaw)*side)/n*speed*dt,state.z+(Math.cos(state.yaw)*forward-Math.sin(state.yaw)*side)/n*speed*dt);state.step+=dt*speed;}
    if(state.time>=8&&(state.time-11)*5.8>=Math.hypot(state.x,48-state.z)) {
      if(state.time>=28&&state.survived){if(!state.aftermath){state.aftermath=true;announce("爆炸过去了。可以开门走出去，看地面的弹坑和废墟。R / 重开。");}}
      else if(state.room&&!state.doorOpen&&state.doorAngle<=.04){if(!state.survived){state.survived=true;announce("冲击波掠过。地下室和关好的门挡住了它，先别开门。");}}
      else {state.dead=true;state.deathTime=state.time;clearInput();releasePointer();announce("冲击波到了。你死了。R / 重开，或等下一轮。");updateControls();}
    } else if(state.time>=8.5&&state.time-dt<8.5) announce("远处亮了。身后有地下避难所：沿楼梯下去，E 开门，进去再关门。");
    else if(state.time>=6&&state.time-dt<6) announce("现在，你从这个人的眼睛看出去。");
    else if(state.time>=8&&state.time-dt<8) announce("远处，一道闪光。");
    updateControls();
  }
  function frame(now) {
    requestAnimationFrame(frame);
    if(document.hidden||state.paused||state.waiting){last=now;return;}
    const dt=last?Math.min(.08,(now-last)/1000):0;last=now;advance(dt);
    const interval=innerWidth<600?50:1000/30;
    if(now-painted>=interval){render();painted=now-((now-painted)%interval);}
  }
  function resize() {
    cols=innerWidth<600?76:112;rows=innerWidth<600?56:60;
    depth=new Float32Array(cols*rows);pixels=new Array(cols*rows);
    const area=document.querySelector("#stage").getBoundingClientRect(),size=Math.max(4,Math.min(area.width/(cols*.61),area.height/rows));
    screen.style.fontSize=`${size}px`;render();
  }
  start.addEventListener("click",()=>{state.waiting=false;last=0;announce("椅子上，有个瘫坐的人。");updateControls();});
  pause.addEventListener("click",togglePause);stance.addEventListener("click",toggleStance);coffee.addEventListener("click",sip);
  doorControl.addEventListener("click",toggleDoor);
  document.querySelector("#restart").addEventListener("click",reset);
  document.querySelector("#away").addEventListener("click",()=>face(true));document.querySelector("#face").addEventListener("click",()=>face(false));
  addEventListener("keydown",e=>{if(e.target instanceof HTMLButtonElement&&[" ","enter"].includes(e.key.toLowerCase()))return;
    const k=e.key.toLowerCase();if(moveKey(k)){e.preventDefault();if(ready())keys.add(k);}if(k==="shift"&&ready())keys.add(k);if(e.repeat)return;
    if(k==="e")interact();else if(k==="g")toggleDoor();else if(k==="c")sip();else if(k==="b")face(true);else if(k==="f")face(false);else if(k==="p"||k==="escape")togglePause();else if(k==="r")reset();
  });
  addEventListener("keyup",e=>keys.delete(e.key.toLowerCase()));
  addEventListener("blur",()=>{clearInput();releasePointer();if(!document.hidden&&!state.waiting&&!state.paused&&!state.dead)togglePause();});
  document.addEventListener("visibilitychange",()=>{clearInput();releasePointer();last=0;});
  document.addEventListener("pointerlockchange",()=>{lockPending=false;if(document.pointerLockElement===screen){if(!ready()||lockWanted!==lockEpoch)releasePointer();else lockHeld=lockEpoch;}else{lockHeld=null;lockWanted=null;lockEpoch++;}drag=null;clearInput();});
  document.addEventListener("pointerlockerror",()=>{lockPending=false;lockWanted=null;});
  screen.addEventListener("pointerdown",e=>{if(!ready()||drag)return;drag={id:e.pointerId,x:e.clientX,y:e.clientY};screen.setPointerCapture(e.pointerId);if(e.pointerType==="mouse")requestPointer();});
  addEventListener("mousemove",e=>{if(document.pointerLockElement!==screen||!ready())return;pointerYaw=null;state.yaw+=e.movementX*.0036;state.pitch=clamp(state.pitch-e.movementY*.0036,-1.28,1.28);});
  screen.addEventListener("pointermove",e=>{if(document.pointerLockElement||!drag||drag.id!==e.pointerId||!ready())return;pointerYaw=null;state.yaw+=(e.clientX-drag.x)*.006;state.pitch=clamp(state.pitch-(e.clientY-drag.y)*.004,-1.28,1.28);drag.x=e.clientX;drag.y=e.clientY;});
  for(const event of ["pointerup","pointercancel","lostpointercapture"])screen.addEventListener(event,e=>{if(drag?.id===e.pointerId)drag=null;});
  for(const b of document.querySelectorAll("[data-move]")){b.addEventListener("pointerdown",e=>{if(!ready())return;e.preventDefault();b.setPointerCapture(e.pointerId);pointers.set(e.pointerId,b.dataset.move);b.classList.add("held");});
    b.addEventListener("click",e=>{if(e.detail===0&&ready()){const key=b.dataset.move;clearTimeout(pulses.get(key));pulses.set(key,setTimeout(()=>pulses.delete(key),200));}});
    for(const ev of ["pointerup","pointercancel","lostpointercapture"])b.addEventListener(ev,e=>{pointers.delete(e.pointerId);if(![...pointers.values()].includes(b.dataset.move))b.classList.remove("held");});}
  reduce.addEventListener("change",()=>{if(reduce.matches&&!state.waiting&&!state.paused)togglePause();});
  addEventListener("resize",resize);
  depth=new Float32Array(cols*rows);pixels=new Array(cols*rows);reset();resize();requestAnimationFrame(frame);
})();
