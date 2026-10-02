/* Small native WebGL renderer. Scenery is never represented as a user. */
(() => {
  "use strict";
  const M = globalThis.MSGUniverse;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const mix = (a, b, t) => a + (b - a) * t;
  const vs = `attribute vec3 aPosition; attribute vec4 aColor; attribute float aSize;
    uniform vec3 uTarget; uniform vec3 uRight; uniform vec3 uUp; uniform vec3 uEye;
    uniform mediump float uPoints; uniform vec3 uLens; uniform float uHeight; uniform float uShift; varying mediump vec4 vColor; varying mediump float vSeed;
    void main(){ vec3 r=aPosition-uTarget; float d=uLens.z-dot(r,uEye);
      gl_Position=vec4(dot(r,uRight)*uLens.x/uLens.y,dot(r,uUp)*uLens.x,1.0002*d-0.2,d);
      gl_Position.y+=uShift*gl_Position.w;
      gl_PointSize=clamp(aSize*uLens.x*uHeight/max(d,0.1),1.0,uPoints>2.5?280.0:100.0); vColor=aColor; vSeed=fract(dot(aPosition,vec3(.17,.31,.53))); }`;
  const fs = `precision mediump float; varying mediump vec4 vColor; varying mediump float vSeed; uniform mediump float uPoints;
    void main(){float alpha=vColor.a;
      if(uPoints>2.5){float d=length(gl_PointCoord-.5)*2.0; if(d>1.0)discard; alpha*=pow(1.0-d*d,3.0);}
      else if(uPoints>1.5){ vec2 p=abs(gl_PointCoord-.5); float mask;
        if(vSeed<.33) mask=step(.22,p.x)*step(p.x,.40)*step(p.y,.38)+step(.27,p.y)*step(p.y,.4)*step(.13,p.x)*step(p.x,.40);
        else if(vSeed<.66) mask=max(step(p.x,.12)*step(p.y,.4),step(p.y,.12)*step(p.x,.4));
        else mask=step(p.x,.32)*step(p.y,.32);
        alpha*=clamp(mask,0.0,1.0);
      } else if(uPoints>0.5){float d=length(gl_PointCoord-0.5)*2.0;
        if(d>1.0)discard; alpha*=exp(-d*d*9.0)+0.035*(1.0-d);}
      gl_FragColor=vec4(vColor.rgb,alpha);}`;
  const palette = {
    user: [.85, .85, .83], post: [.80, .80, .78], reply: [.94, .94, .92],
    private: [.75, .84, .76], 'private-message': [.75, .84, .76],
  };
  const dot = (a, b) => a.reduce((sum, v, i) => sum + v * b[i], 0);
  const unit = v => { const length = Math.hypot(...v) || 1; return v.map(n => n / length); };
  function flightBasis(yaw, pitch) {
    const sy = Math.sin(yaw), cy = Math.cos(yaw), sp = Math.sin(pitch), cp = Math.cos(pitch);
    return { right: [cy, 0, -sy], up: [-sp * sy, cp, -sp * cy], eye: [cp * sy, sp, cp * cy] };
  }
  const angleMix = (a, b, t) => a + Math.atan2(Math.sin(b - a), Math.cos(b - a)) * t;
  // A bounded sample timeline renders peers between authoritative physics ticks.
  // Network arrival time is deliberately not the interpolation parameter.
  class MotionTrack {
    constructor() { this.samples = []; }
    push(ship, time, reset = false) {
      const previous = this.samples.at(-1);
      if (reset || previous && (previous.ship.region !== ship.region ||
          (previous.ship.hp <= 0) !== (ship.hp <= 0) ||
          Math.hypot(...ship.position.map((v, i) => v - previous.ship.position[i])) > 45)) this.samples.length = 0;
      if (this.samples.at(-1)?.time >= time) return;
      this.samples.push({ ship, time });
      if (this.samples.length > 8) this.samples.shift();
    }
    sample(time) {
      const samples = this.samples;
      if (!samples.length) return null;
      let a = samples[0], b;
      if (time <= a.time) return { ...a.ship, position: [...a.ship.position], velocity: [...a.ship.velocity] };
      for (let i = 1; i < samples.length; i++) {
        b = samples[i];
        if (time <= b.time) {
          const duration = (b.time - a.time) / 1000, t = clamp((time - a.time) / (b.time - a.time), 0, 1);
          const t2 = t * t, t3 = t2 * t;
          // Hermite uses the server's velocities, avoiding a 5 Hz staircase.
          const position = a.ship.position.map((p, axis) => (2 * t3 - 3 * t2 + 1) * p +
            (t3 - 2 * t2 + t) * duration * a.ship.velocity[axis] +
            (-2 * t3 + 3 * t2) * b.ship.position[axis] + (t3 - t2) * duration * b.ship.velocity[axis]);
          const velocity = a.ship.velocity.map((v, axis) => mix(v, b.ship.velocity[axis], t));
          return { ...b.ship, position, velocity, yaw: angleMix(a.ship.yaw, b.ship.yaw, t), pitch: mix(a.ship.pitch, b.ship.pitch, t) };
        }
        a = b;
      }
      // A delayed snapshot never permits unlimited motion while disconnected.
      const elapsed = clamp((time - a.time) / 1000, 0, .3);
      return { ...a.ship, position: a.ship.position.map((p, i) => p + a.ship.velocity[i] * elapsed), velocity: [...a.ship.velocity] };
    }
  }
  function inertialSegment(velocity, dt, target, acceleration, limits = {}) {
    const cap = limits.max_speed ?? 60, speed = Math.hypot(...velocity);
    const v = speed > cap ? velocity.map(n => n * cap / speed) : [...velocity];
    let next, movement;
    if (!target) {
      const drag = limits.coast_drag ?? .32, factor = Math.exp(-drag * dt);
      next = v.map(n => n * factor); movement = v.map(n => n * (drag > 0 ? (1 - factor) / drag : dt));
    } else {
      const difference = target.map((n, i) => n - v[i]), distance = Math.hypot(...difference);
      if (!distance || acceleration <= 0) return { velocity: v, movement: v.map(n => n * dt) };
      const accelerating = Math.min(dt, distance / acceleration), blend = Math.min(1, acceleration * dt / distance);
      const weighted = acceleration / distance * accelerating * accelerating / 2 + Math.max(0, dt - accelerating);
      next = v.map((n, i) => n + difference[i] * blend);
      movement = v.map((n, i) => n * dt + difference[i] * weighted);
    }
    if (Math.hypot(...next) <= (limits.stop_speed ?? .12)) next.fill(0);
    return { velocity: next, movement };
  }
  function integrateFlight(state, seconds, controls, limits = {}, now = 0) {
    const dt = Number.isFinite(seconds) ? clamp(seconds, 0, .04) : 0;
    if (!dt || state.hp <= 0) return;
    const braking = controls.brake === true, yaw = controls.yaw, pitch = controls.pitch;
    const forward = [Math.sin(yaw) * Math.cos(pitch), Math.sin(pitch), -Math.cos(yaw) * Math.cos(pitch)];
    const right = [Math.cos(yaw), 0, Math.sin(yaw)];
    const direction = forward.map((v, i) => v * controls.throttle + right[i] * controls.strafe + (i === 1 ? controls.lift : 0));
    const hasControls = [controls.throttle, controls.strafe, controls.lift].some(n => Math.abs(n) > 1e-8), dashEnd = state.dash_until_ms ?? 0;
    const start = now - dt * 1000, dashStart = dashEnd - (limits.dash_seconds ?? 1) * 1000;
    const boundaries = [start, ...[dashStart, dashEnd].filter(at => at > start && at < now), now].sort((a, b) => a - b);
    const segment = (duration, target = null, acceleration = 0) => {
      const result = inertialSegment(state.velocity, duration, target, acceleration, limits);
      state.velocity = result.velocity; state.position = state.position.map((v, i) => v + result.movement[i]);
    };
    for (let i = 1; i < boundaries.length; i++) {
      const duration = (boundaries[i] - boundaries[i - 1]) / 1000, middle = (boundaries[i] + boundaries[i - 1]) / 2;
      const dash = middle >= dashStart && middle < dashEnd, thrust = hasControls ? direction : dash ? forward : null;
      if (braking) segment(duration, [0, 0, 0], limits.brake_deceleration ?? 48);
      else if (thrust && state.fuel > 0) {
        const powered = Math.min(duration, state.fuel / 8), length = Math.max(1, Math.hypot(...thrust));
        const speed = dash ? limits.max_speed ?? 60 : limits.cruise_speed ?? 20;
        segment(powered, thrust.map(v => v * speed / length), dash ? limits.dash_acceleration ?? 96 : limits.thrust_acceleration ?? 24);
        state.fuel = Math.max(0, state.fuel - powered * 8);
        if (powered < duration) segment(duration - powered);
      } else segment(duration);
    }
    const radius = Math.hypot(...state.position), extent = limits.world_extent ?? 480;
    if (radius > extent) {
      const normal = state.position.map(v => v / radius), outward = Math.max(0, dot(normal, state.velocity));
      state.position = normal.map(v => v * extent);
      state.velocity = state.velocity.map((v, i) => v - normal[i] * outward);
    }
  }
  class FlightPrediction {
    constructor(ship) { this.reset(ship); }
    reset(ship) {
      this.state = { ...ship, position: [...ship.position], velocity: [...ship.velocity] };
      this.offset = [0, 0, 0]; this.position = [...ship.position];
    }
    reconcile(ship, reset = false, age = 0, controls = null, limits = {}, now = 0) {
      const previous = [...this.position], error = previous.map((p, i) => p - ship.position[i]);
      if (reset || ship.region !== this.state.region || (ship.hp <= 0) !== (this.state.hp <= 0) || Math.hypot(...error) > 45) this.reset(ship);
      else {
        this.state = { ...ship, position: [...ship.position], velocity: [...ship.velocity] };
        // Bring the physical tick to the render clock before applying a visual
        // correction; otherwise the 15 Hz tick remainder jitters the camera.
        if (controls) for (let remaining = clamp(age, 0, .15); remaining > 0; ) {
          const dt = Math.min(remaining, .04); remaining -= dt;
          integrateFlight(this.state, dt, controls, limits, now - remaining * 1000);
        }
        this.offset = previous.map((p, i) => p - this.state.position[i]);
      }
    }
    step(seconds, controls, limits, now, reduced = false) {
      const dt = Number.isFinite(seconds) ? clamp(seconds, 0, .12) : 0;
      // A slow 20-30 FPS device still advances the whole visible interval.
      // Smaller physics substeps retain dash/fuel boundaries and hard bounds.
      for (let remaining = dt; remaining > 0; ) {
        const step = Math.min(remaining, .04); remaining -= step;
        integrateFlight(this.state, step, controls, limits, now - remaining * 1000);
      }
      const decay = reduced ? 0 : Math.exp(-dt * 7);
      this.offset = this.offset.map(v => v * decay);
      this.position = this.state.position.map((v, i) => v + this.offset[i]);
      return this.position;
    }
  }
  // Local navigation only. No identity, persistence, network or account side effects.
  class Flight {
    constructor(position, yaw = 0, pitch = 0) {
      this.position = [...position];
      this.velocity = [0, 0, 0];
      this.yaw = yaw;
      this.pitch = pitch;
      this.bank = 0;
      this.thrust = 0;
      this.boost = false;
      this.trail = [];
      this.trailClock = 0;
    }
    get speed() { return Math.hypot(...this.velocity); }
    halt() { this.velocity.fill(0); this.thrust = 0; this.boost = false; this.trail.length = 0; }
    step(seconds, keys, obstacles = [], reduced = false) {
      const dt = Number.isFinite(seconds) ? clamp(seconds, 0, .04) : 0;
      if (!dt) return;
      const axis = (positive, negative) => Number(keys.has(positive)) - Number(keys.has(negative));
      const turn = axis('arrowright', 'arrowleft');
      this.yaw = (this.yaw - turn * dt * 1.15) % M.TAU;
      this.pitch = clamp(this.pitch + axis('arrowup', 'arrowdown') * dt, -1.35, 1.35);
      const b = flightBasis(this.yaw, this.pitch);
      const local = [axis('d', 'a'), axis('e', 'q'), -axis('w', 's')];
      const length = Math.max(1, Math.hypot(...local));
      const braking = keys.has(' ') || (reduced && !local.some(Boolean));
      this.boost = !reduced && !braking && keys.has('shift') && local[2] < 0;
      this.thrust = braking ? 0 : -local[2];
      this.bank = mix(this.bank, reduced ? 0 : clamp(-turn * .4 - local[0] * .18, -.5, .5), 1 - Math.exp(-dt * 7));
      const drag = braking ? 12 : .65, damping = Math.exp(-drag * dt);
      const acceleration = braking ? 0 : this.boost ? 115 : 42;
      const limit = this.boost ? 155 : Math.max(65, this.speed * damping);
      const from = [...this.position];
      for (let i = 0; i < 3; i++) {
        const a = (local[0] * b.right[i] + local[1] * b.up[i] + local[2] * b.eye[i]) * acceleration / length;
        // Exact constant-force integration: 30/60/120 Hz have the same handling.
        this.position[i] += this.velocity[i] * (1 - damping) / drag + a / drag * (dt - (1 - damping) / drag);
        this.velocity[i] = this.velocity[i] * damping + a * (1 - damping) / drag;
      }
      const speed = this.speed;
      if (speed > limit) this.velocity = this.velocity.map(v => v * limit / speed);
      if (braking && this.speed < .025) this.velocity.fill(0);
      // Swept sphere checks prevent tunneling through loaded planets during boost.
      // Four contact passes are bounded; overlapping planets may require strafing out.
      for (let pass = 0; pass < 4; pass++) {
        const delta = this.position.map((v, i) => v - from[i]);
        const length2 = dot(delta, delta);
        let first = null;
        for (const body of obstacles) {
          const radius = body.radius + 4.0, rel = from.map((v, i) => v - body.position[i]);
          const c = dot(rel, rel) - radius * radius;
          if (c < 0) { first = { t: 0, body, radius, inside: true }; break; }
          if (length2 < 1e-12) continue;
          const q = dot(rel, delta), discriminant = q * q - length2 * c;
          if (q >= 0 || discriminant < 0) continue;
          const t = (-q - Math.sqrt(discriminant)) / length2;
          if (t >= 0 && t <= 1 && (!first || t < first.t)) first = { t, body, radius };
        }
        if (!first) break;
        const hit = from.map((v, i) => v + delta[i] * first.t);
        let normal = hit.map((v, i) => v - first.body.position[i]);
        normal = Math.hypot(...normal) < 1e-9 ? b.eye : unit(normal);
        this.position = first.body.position.map((v, i) => v + normal[i] * (first.radius + .015));
        const inward = Math.min(0, dot(this.velocity, normal));
        this.velocity = this.velocity.map((v, i) => v - normal[i] * inward);
        for (let i = 0; i < 3; i++) from[i] = this.position[i];
      }
      for (let i = 0; i < 3; i++) {
        const bounded = clamp(this.position[i], -1200, 1200);
        if (bounded !== this.position[i]) this.velocity[i] = 0;
        this.position[i] = bounded;
      }
      for (const sample of this.trail) sample.age += dt;
      this.trail = this.trail.filter(sample => sample.age < 1.4);
      this.trailClock += dt;
      if (!reduced && this.speed > 5 && this.trailClock >= .04) {
        this.trail.push({ position: this.position.map((v, i) => v + b.eye[i] * 2.6), age: 0 });
        this.trailClock = 0;
        if (this.trail.length > 36) this.trail.shift();
      }
    }
  }
  function tokenNebula(seed, count = 2300) {
    const random = M.random('token-ribbons:v5:' + seed), dust = [], clouds = [];
    const phases = Array.from({ length: 3 }, () => random() * M.TAU);
    const center = (t, band) => {
      const phase = phases[band];
      return [(t - .5) * 940, Math.sin(t * 8 + phase) * (35 + band * 30) + (band - 1) * 44,
        Math.cos(t * 6 + phase) * 115 + (band - 1) * 160];
    };
    const scatter = () => (random() + random() + random() - 1.5);
    for (let i = 0; i < count; i++) {
      const t = random(), band = i % 3, p = center(t, band);
      const width = 5 + 22 * (1 + Math.sin(t * 13 + phases[band]));
      for (let k = 0; k < 3; k++) p[k] += scatter() * width;
      if (i % 5 === 0) for (let k = 0; k < 3; k++) p[k] = (random() - .5) * (k === 1 ? 750 : 1500);
      const length = Math.hypot(...p);
      if (length > 475) for (let k = 0; k < 3; k++) p[k] *= 475 / length;
      const clearance = clamp((Math.hypot(...p) - 24) / 80, .08, 1);
      dust.push(...p, .83, .83, .83, (.14 + random() * .38) * clearance, .23 + random() * .55);
    }
    for (let i = 0; i < 72; i++) {
      const band = i % 3, t = Math.floor(i / 3) / 23, p = center(t, band);
      const clearance = clamp((Math.hypot(...p) - 30) / 100, 0, 1);
      clouds.push(...p, .66, .66, .66, .09 * clearance, 36 + random() * 32);
    }
    return { dust: new Float32Array(dust), clouds: new Float32Array(clouds) };
  }
  function planetMesh(id) {
    const phase = M.random('terrain:' + id)() * M.TAU;
    const top = [0, 1, 0], bottom = [0, -1, 0], rim = [[1, 0, 0], [0, 0, 1], [-1, 0, 0], [0, 0, -1]];
    let faces = [];
    for (let i = 0; i < 4; i++) faces.push([top, rim[(i + 1) % 4], rim[i]], [bottom, rim[i], rim[(i + 1) % 4]]);
    for (let depth = 0; depth < 2; depth++) {
      const next = [];
      for (const [a, b, c] of faces) {
        const middle = (p, q) => unit(p.map((v, i) => v + q[i]));
        const ab = middle(a, b), bc = middle(b, c), ca = middle(c, a);
        next.push([a, ab, ca], [ab, b, bc], [ca, bc, c], [ab, bc, ca]);
      }
      faces = next;
    }
    return faces.map(face => face.map(v => {
      const height = .045 * Math.sin(v[0] * 8 + phase) * Math.cos(v[2] * 7 - phase) + .02 * Math.sin(v[1] * 12 + phase);
      return v.map(n => n * (1 + height));
    }));
  }
  class UniverseRenderer {
    constructor(canvas, labels, callbacks = {}) {
      this.canvas = canvas;
      this.labels = labels;
      this.callbacks = callbacks;
      this.camera = {
        target: [0, 0, 0],
        distance: 370,
        yaw: 0.62,
        pitch: 0.48,
      };
      this.graph = { nodes: [], links: [] };
      this.view = this.graph;
      this.index = new M.SpatialIndex([]);
      this.pinned = [];
      this.satellites = [];
      this.focusId = null;
      this.keys = new Set();
      this.pointers = new Map();
      this.labelNodes = new Map();
      this.dirty = true;
      this.reduced = matchMedia("(prefers-reduced-motion: reduce)");
      this.paused = this.reduced.matches;
      this.clock = 0;
      this.last = 0;
      this.frame = 0;
      this.available = false;
      this.lost = false;
      this.sceneSeed = 'public-preview';
      const scenery = tokenNebula(this.sceneSeed);
      this.dust = scenery.dust;
      this.clouds = scenery.clouds;
      this.flight = null;
      this.network = callbacks.flight || null;
      this.wantFlight = false;
      this.remoteShips = new Map();
      this.shipLabels = new Map();
      this.shotEvents = new Map();
      this.collectEvents = new Map();
      this.motionEvents = new Map();
      this.collectibles = null;
      this.prediction = null;
      this.gameActions = new Set();
      this.homeBody = null;
      this.homeMesh = null;
      this.flightPointers = new Map();
      this.flightControls = new Map();
      this.flightHud = document.getElementById('pilot-hud');
      this.flightButton = document.getElementById('pilot-toggle');
      this.nearby = null;
      this.hudTime = 0;
      this.visualTraits = new WeakMap();
      this.tokenField = document.getElementById('token-field');
      this.tokenNodes = new Map();
      this.hoverId = null;
      canvas.addEventListener("webglcontextlost", (e) => {
        e.preventDefault();
        this.stopFlightInput();
        this.clearRemoteShips();
        this.lost = true;
        this.available = false;
        if (this.flightButton) this.flightButton.disabled = true;
        cancelAnimationFrame(this.frame);
        this.frame = 0;
        this.labels.replaceChildren();
        this.tokenField?.replaceChildren();
        this.tokenNodes.clear();
        this.callbacks.fallback?.(
          "The graphics context paused. The star catalog still works.",
        );
      });
      canvas.addEventListener("webglcontextrestored", () => {
        this.lost = false;
        this.labelNodes.clear();
        this.init();
        if (this.flightButton) this.flightButton.disabled = !this.available;
        this.wake();
      });
      this.init();
      if (this.flightButton) this.flightButton.disabled = !this.available;
      this.events();
      this.flightEvents();
      this.observer = new ResizeObserver(() => this.resize());
      this.observer.observe(canvas);
      this.resize();
      this.wake();
    }
    init() {
      try {
        this.gl = this.canvas.getContext("webgl", {
          alpha: true,
          antialias: true,
          powerPreference: "low-power",
          preserveDrawingBuffer: false,
        });
        if (!this.gl) {
          this.context = this.canvas.getContext("2d");
          if (!this.context)
            throw new Error(
              "Graphics are unavailable. Explore using the star catalog.",
            );
          this.software = true;
          this.available = true;
          this.canvas.dataset.renderer = "canvas-3d";
          return;
        }
        const gl = this.gl;
        const shader = (type, text) => {
          const s = gl.createShader(type);
          gl.shaderSource(s, text);
          gl.compileShader(s);
          if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
            throw new Error(
              "The graphics shader could not start: " + gl.getShaderInfoLog(s),
            );
          return s;
        };
        const vertex = shader(gl.VERTEX_SHADER, vs),
          fragment = shader(gl.FRAGMENT_SHADER, fs);
        this.program = gl.createProgram();
        gl.attachShader(this.program, vertex);
        gl.attachShader(this.program, fragment);
        gl.linkProgram(this.program);
        gl.deleteShader(vertex);
        gl.deleteShader(fragment);
        if (!gl.getProgramParameter(this.program, gl.LINK_STATUS))
          throw new Error("The graphics program could not start.");
        gl.useProgram(this.program);
        this.buffer = gl.createBuffer();
        this.uniforms = {};
        for (const name of [
          "uTarget",
          "uRight",
          "uUp",
          "uEye",
          "uLens",
          "uHeight",
          "uShift",
          "uPoints",
        ])
          this.uniforms[name] = gl.getUniformLocation(this.program, name);
        gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
        for (const [name, size, offset] of [
          ["aPosition", 3, 0],
          ["aColor", 4, 12],
          ["aSize", 1, 28],
        ]) {
          const loc = gl.getAttribLocation(this.program, name);
          gl.enableVertexAttribArray(loc);
          gl.vertexAttribPointer(loc, size, gl.FLOAT, false, 32, offset);
        }
        gl.enable(gl.BLEND);
        gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
        gl.disable(gl.DEPTH_TEST);
        gl.clearColor(0, 0, 0, 0);
        this.available = true;
        this.canvas.dataset.renderer = "webgl";
      } catch (error) {
        this.available = false;
        this.callbacks.fallback?.(error.message);
      }
    }
    resize() {
      const rect = this.canvas.getBoundingClientRect();
      this.width = rect.width;
      this.height = rect.height;
      const dpr = Math.min(devicePixelRatio || 1, 1.75);
      this.canvas.width = Math.round(rect.width * dpr);
      this.canvas.height = Math.round(rect.height * dpr);
      this.wake();
    }
    setPilot(active) {
      if (active && (!this.available || this.lost)) return;
      if (active && this.network && !this.wantFlight) this.callbacks.prepareFlight?.();
      this.wantFlight = active;
      if (active && this.network && (!this.network.connected || !this.network.self)) {
        this.network.connect();
        document.getElementById('game-hud').hidden = false;
        if (this.flightButton) this.flightButton.textContent = '取消连接';
        return;
      }
      if (active === Boolean(this.flight)) {
        if (!active && this.network) {
          this.network.disconnect(); this.clearRemoteShips(); this.homeBody = null;
          document.getElementById('game-hud').hidden = true;
          if (this.flightButton) this.flightButton.textContent = '驾驶';
        }
        return;
      }
      this.stopFlightInput();
      this.destination = null;
      this.nearby = null;
      if (active) {
        this.focusId = null;
        this.callbacks.overview?.();
        const b = this.basis(), c = this.camera, ship = this.network?.self;
        this.flight = ship ? new Flight(ship.position, -ship.yaw, -ship.pitch)
          : new Flight(c.target.map((v, i) => v + b.eye[i] * Math.max(12, c.distance - 34)), c.yaw, c.pitch);
        if (ship) this.flight.velocity = [...ship.velocity];
        if (ship) this.prediction = new FlightPrediction(ship);
        this.network?.resume();
        this.updateFlight(0);
      } else {
        this.camera.target = [...this.flight.position];
        this.camera.distance = 90;
        this.flight = null;
        this.prediction = null;
        this.network?.disconnect();
        this.clearRemoteShips(); this.homeBody = null;
      }
      this.reindex();
      this.last = 0;
      this.canvas.dataset.piloting = String(active);
      document.getElementById('universe')?.classList.toggle('piloting', active);
      if (this.flightHud) this.flightHud.hidden = !active;
      for (const id of ['game-hud', 'game-actions']) {
        const el = document.getElementById(id); if (el) el.hidden = !active || !this.network;
      }
      if (this.flightButton) {
        this.flightButton.setAttribute('aria-pressed', String(active));
        this.flightButton.textContent = active ? '退出飞行' : '驾驶';
      }
      this.updateFlightHud(true);
      if (active) this.canvas.focus({ preventScroll: true });
      this.wake();
    }
    stopFlightInput(notifyNetwork = true) {
      this.keys.clear();
      this.pointers.clear();
      this.start = null;
      this.flightPointers?.clear();
      this.flightControls?.clear();
      if (!this.network) this.flight?.halt();
      this.gameActions.clear();
      if (notifyNetwork) this.network?.suspend();
      for (const button of document.querySelectorAll('[data-flight-key]')) button.classList.remove('held');
      // Focusing Inspect must not disable that button between pointerdown and
      // click. Re-resolve its identity when clicked; input cancellation alone
      // does not invalidate the current nearby star.
      if (this.flight) this.updateFlightHud();
    }
    flightEvents() {
      const canvas = this.canvas;
      this.flightButton?.addEventListener('click', () => this.setPilot(!(this.flight || this.wantFlight)));
      document.getElementById('pilot-inspect')?.addEventListener('click', () => this.inspectNearby());
      canvas.addEventListener('keydown', e => {
        if (e.ctrlKey || e.metaKey || e.altKey || e.isComposing) return;
        const key = e.key.toLowerCase();
        if (key === 'f' && !e.repeat) { e.preventDefault(); this.setPilot(!(this.flight || this.wantFlight)); return; }
        if (key === 'escape' && this.wantFlight && !this.flight) { e.preventDefault(); this.setPilot(false); return; }
        if (!this.flight) return;
        if (key === 'escape') {
          e.preventDefault(); this.setPilot(false); this.flightButton?.focus(); return;
        }
        if (key === 'h') { e.preventDefault(); this.home(); return; }
        if (key === 'enter' && !e.repeat) { e.preventDefault(); this.inspectNearby(); return; }
        if (this.network && !e.repeat && ['j', 'k', 'shift'].includes(key)) {
          e.preventDefault(); this.network.resume(); this.queueGameAction(key === 'j' ? 'shield' : 'dash'); return;
        }
        if (['w', 'a', 's', 'd', 'q', 'e', 'arrowup', 'arrowdown', 'arrowleft', 'arrowright', ' ', 'shift', 'b'].includes(key)) {
          if (!e.repeat) this.network?.resume();
          e.preventDefault(); this.keys.add(key); this.wake();
        }
      });
      window.addEventListener('keyup', e => { this.keys.delete(e.key.toLowerCase()); });
      window.addEventListener('blur', () => { this.stopFlightInput(); this.last = 0; });
      window.addEventListener('pagehide', () => {
        this.stopFlightInput(); this.last = 0;
        cancelAnimationFrame(this.frame); this.frame = 0;
      });
      document.addEventListener('focusin', e => {
        if (this.flight && e.target !== canvas) this.stopFlightInput();
      });
      canvas.addEventListener('focus', () => { if (this.flight && !document.hidden) this.network?.resume(); });
      canvas.addEventListener('pointerdown', e => {
        if (!this.flight || e.button !== 0) return;
        e.preventDefault(); canvas.focus({ preventScroll: true });
        this.network?.resume();
        canvas.setPointerCapture(e.pointerId);
        this.flightPointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      });
      canvas.addEventListener('pointermove', e => {
        const previous = this.flightPointers.get(e.pointerId);
        if (!this.flight || !previous) return;
        this.flight.yaw -= (e.clientX - previous.x) * .004;
        this.flight.pitch = clamp(this.flight.pitch + (e.clientY - previous.y) * .003, -1.35, 1.35);
        this.flightPointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
        this.wake();
      });
      for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
        canvas.addEventListener(type, e => {
          const held = this.flightPointers.has(e.pointerId);
          this.flightPointers.delete(e.pointerId);
          if (type === 'pointercancel' || (type === 'lostpointercapture' && held)) this.stopFlightInput();
        });
      }
      for (const button of document.querySelectorAll('[data-flight-key]')) {
        const key = button.dataset.flightKey;
        const press = (id) => {
          if (!this.flightControls.has(id)) this.network?.resume();
          this.flightControls.set(id, key); button.classList.add('held'); this.wake();
        };
        const release = (id) => {
          this.flightControls.delete(id);
          if (![...this.flightControls.values()].includes(key)) button.classList.remove('held');
          this.sendFlightInput();
          this.wake();
        };
        button.addEventListener('pointerdown', e => {
          if (!this.flight || e.button !== 0) return;
          e.preventDefault(); canvas.focus({ preventScroll: true });
          button.setPointerCapture(e.pointerId); press(e.pointerId);
        });
        for (const type of ['pointerup', 'pointercancel', 'lostpointercapture'])
          button.addEventListener(type, e => {
            const held = this.flightControls.has(e.pointerId);
            release(e.pointerId);
            if (type === 'pointercancel' || (type === 'lostpointercapture' && held)) this.stopFlightInput();
          });
        button.addEventListener('keydown', e => {
          if (this.flight && [' ', 'Enter'].includes(e.key)) { e.preventDefault(); press('keyboard:' + key); }
        });
        button.addEventListener('keyup', e => {
          if ([' ', 'Enter'].includes(e.key)) { e.preventDefault(); release('keyboard:' + key); }
        });
        button.addEventListener('blur', () => release('keyboard:' + key));
      }
      for (const button of document.querySelectorAll('[data-game-action]')) {
        const action = button.dataset.gameAction === 'fire' ? 'laser' : button.dataset.gameAction;
        button.addEventListener('click', () => {
          if (!this.flight || !this.network?.connected) return;
          this.canvas.focus({preventScroll:true}); this.network.resume();
          this.queueGameAction(action); this.sendFlightInput();
        });
      }
    }
    queueGameAction(action) {
      if (!this.network?.connected || !this.flight || this.network.self?.hp <= 0) return;
      this.gameActions.add(action); this.wake();
    }
    flightIntent(controlled = true) {
      const keys = new Set([...this.keys, ...this.flightControls.values()]);
      const axis = (positive, negative) => controlled ? Number(keys.has(positive)) - Number(keys.has(negative)) : 0;
      return { throttle:axis('w', 's'), strafe:axis('d', 'a'), lift:axis('e', 'q'),
        yaw:-this.flight.yaw, pitch:-this.flight.pitch, brake:controlled && keys.has('b') };
    }
    sendFlightInput() {
      if (!this.network?.connected || !this.flight) return;
      const keys = new Set([...this.keys, ...this.flightControls.values()]);
      const actions = [...this.gameActions];
      if (keys.has(' ')) actions.push('laser');
      this.network.setInput({...this.flightIntent(), actions:[...new Set(actions)]});
      this.gameActions.clear();
    }
    syncCollectibles(field) {
      if (!field) return;
      if (field.seed && field.seed !== this.sceneSeed) {
        this.sceneSeed = field.seed;
        const scenery = tokenNebula(field.seed, field.count);
        this.dust = scenery.dust; this.clouds = scenery.clouds;
        for (const token of this.tokenNodes.values()) token.remove();
        this.tokenNodes.clear();
      }
      if (this.collectibles?.revision === field.revision && !field.seed) return;
      this.collectibles = { ...this.collectibles, ...field };
      // Keep stable IDs in the full dust array; rebuild the compact GPU stream
      // only on a server bitmap revision, never 2,300 DOM nodes per frame.
      const dust = [];
      for (let i = 0; i < this.dust.length / 8; i++) if (!this.glyphTaken(i))
        dust.push(...this.dust.subarray(i * 8, i * 8 + 8));
      this.renderDust = new Float32Array(dust);
    }
    glyphTaken(id) { return Boolean(this.collectibles?.mask[id >> 3] & (1 << (id & 7))); }
    receiveFlightHello(hello) {
      this.syncCollectibles(hello.collectibles);
      this.homeBody = hello.self.home_body?.private ? hello.self.home_body : null;
      this.homeMesh = this.homeBody ? planetMesh(this.homeBody.id) : null;
      if (this.flight) {
        this.flight.position = [...hello.self.position]; this.flight.velocity = [...hello.self.velocity];
        this.flight.yaw = -hello.self.yaw; this.flight.pitch = -hello.self.pitch;
        this.prediction = new FlightPrediction(hello.self);
      }
      if (this.wantFlight) this.setPilot(true);
    }
    receiveFlightSnapshot(snapshot) {
      const now = this.network.serverNow, time = snapshot.state_time_ms ?? snapshot.server_time_ms ?? now;
      this.motionEvents ??= new Map(); this.collectEvents ??= new Map();
      const reset = new Set();
      for (const event of snapshot.events) if (['region', 'respawn'].includes(event.type) && !this.motionEvents.has(event.id)) {
        reset.add(event.player_id); this.motionEvents.set(event.id, event.at_ms);
      }
      for (const [id, at] of this.motionEvents) if (now - at > 8000) this.motionEvents.delete(id);
      const live = new Set(snapshot.players.map(ship => ship.id));
      for (const [id] of this.remoteShips) if (!live.has(id)) this.remoteShips.delete(id);
      for (const ship of snapshot.players) {
        const previous = this.remoteShips.get(ship.id);
        const track = previous?.track ?? new MotionTrack();
        track.push(ship, time, reset.has(ship.id));
        const sameRegion = previous?.state.region === ship.region && !reset.has(ship.id) && (previous.state.hp <= 0) === (ship.hp <= 0);
        this.remoteShips.set(ship.id, {state:ship, track, position:sameRegion ? previous.position : [...ship.position], yaw:ship.yaw, pitch:ship.pitch});
      }
      if (this.flight && this.network?.self) {
        const ship = this.network.self;
        this.prediction ??= new FlightPrediction(ship);
        const controlled = document.activeElement === this.canvas || this.flightControls.size > 0;
        this.prediction.reconcile(ship, reset.has(ship.id), (now - time) / 1000, this.flightIntent(controlled), this.network.limits, now);
        this.flight.position = [...this.prediction.position];
      }
      this.syncCollectibles(snapshot.collectibles);
      for (const event of snapshot.events) if (event.type === 'collect' && now - event.at_ms < 800 && !this.collectEvents.has(event.id))
        this.collectEvents.set(event.id, event);
      for (const [id, event] of this.collectEvents) if (now - event.at_ms >= 800) this.collectEvents.delete(id);
      for (const event of snapshot.events) if (event.type === 'laser' && event.position && event.end && now - event.at_ms < 400)
        this.shotEvents.set(event.id, event);
      for (const [id, event] of this.shotEvents) if (now - event.at_ms > 400) this.shotEvents.delete(id);
      this.wake();
    }
    clearRemoteShips() {
      this.remoteShips.clear(); this.shotEvents.clear();
      this.collectEvents?.clear(); this.motionEvents?.clear();
      for (const label of this.shipLabels.values()) label.remove();
      this.shipLabels.clear(); this.wake();
    }
    updateFlight(dt) {
      const flight = this.flight, now = this.callbacks.now?.() ?? Date.now();
      const bodies = this.graph.nodes.filter(node => node.kind === 'user' || node.kind === 'private')
        .map(node => ({ node, position: node.position, radius: M.appearance(node, now).radius * 1.07 }));
      const keys = new Set([...this.keys, ...this.flightControls.values()]);
      if (this.network) {
        const ship = this.network.connected && this.network.self;
        if (ship) {
          const controlled = document.activeElement === this.canvas || this.flightControls.size > 0;
          if (controlled && ship.hp > 0) {
            flight.yaw -= (Number(keys.has('arrowright')) - Number(keys.has('arrowleft'))) * dt * 1.15;
            flight.pitch = clamp(flight.pitch - (Number(keys.has('arrowup')) - Number(keys.has('arrowdown'))) * dt, -1.35, 1.35);
            this.sendFlightInput();
          }
          this.prediction ??= new FlightPrediction(ship);
          if (!dt) this.prediction.reset(ship);
          flight.position = [...this.prediction.step(dt, this.flightIntent(controlled), this.network.limits, this.network.serverNow, this.reduced.matches)];
          flight.velocity = [...this.prediction.state.velocity];
          flight.thrust = controlled && ship.hp > 0 ? Number(keys.has('w')) - Number(keys.has('s')) : 0;
          flight.boost = false;
          const bank = controlled ? -(Number(keys.has('arrowright')) - Number(keys.has('arrowleft'))) * .25 -
            (Number(keys.has('d')) - Number(keys.has('a'))) * .16 : 0;
          flight.bank = this.reduced.matches ? 0 : mix(flight.bank, bank, 1 - Math.exp(-dt * 7));
        } else { flight.thrust = 0; flight.boost = false; }
        for (const remote of this.remoteShips.values()) {
          const sample = remote.track?.sample(this.network.serverNow - 240);
          if (sample) { remote.position = sample.position; remote.yaw = sample.yaw; remote.pitch = sample.pitch; }
        }
      } else flight.step(dt, keys, bodies, this.reduced.matches);
      const smoothing = this.paused || this.reduced.matches || !dt ? 1 : 1 - Math.exp(-dt * 9);
      // Interpolate angles across the wrap boundary, never via a full rotation.
      const angle = Math.atan2(Math.sin(flight.yaw - this.camera.yaw), Math.cos(flight.yaw - this.camera.yaw));
      this.camera.yaw += angle * smoothing;
      this.camera.pitch = mix(this.camera.pitch, flight.pitch, smoothing);
      const basis = this.basis();
      this.camera.target = flight.position.map((v, i) => v - basis.eye[i] * 8 + basis.up[i] * 4);
      this.camera.distance = this.reduced.matches ? 34 : mix(this.camera.distance, flight.boost ? 38 : 34, smoothing);
      this.nearby = null;
      let nearest = 40;
      for (const body of bodies) {
        const distance = Math.hypot(...flight.position.map((v, i) => v - body.position[i])) - body.radius;
        if (distance < nearest) { nearest = distance; this.nearby = body.node; }
      }
      this.hudTime += dt;
      if (this.hudTime > .1 || !dt) { this.updateFlightHud(); this.hudTime = 0; }
    }
    updateFlightHud(clear = false) {
      const speed = document.getElementById('pilot-speed'), approach = document.getElementById('pilot-inspect');
      if (speed) speed.textContent = this.flight && (!this.network || this.network.connected) ? Math.round(this.flight.speed).toString().padStart(3, '0') : '—';
      if (!approach) return;
      const node = !clear && this.flight && this.nearby;
      approach.disabled = !node;
      approach.textContent = node ? '查看 ' + M.handle(node.name || node.title || 'Signal') + ' ↗' : '靠近星球查看';
      if (this.network) this.updateGameHud();
    }
    updateGameHud() {
      const ship = this.network.connected && this.network.self, now = this.network.serverNow;
      for (const field of ['hp', 'fuel']) {
        const output = document.getElementById('game-' + field), meter = document.getElementById('game-' + field + '-meter');
        if (output) output.textContent = ship ? Math.round(ship[field]).toString() : '—';
        if (meter) { meter.value = ship ? ship[field] : 0; meter.closest('.game-vital').dataset.critical = String(ship && ship[field] < 25); }
      }
      const player = document.getElementById('game-player');
      if (player) player.textContent = ship ? ship.handle : '—';
      const score = document.getElementById('game-score'); if (score) score.textContent = ship && Number.isFinite(ship.score) ? String(ship.score) : '—';
      const collected = document.getElementById('game-collected');
      if (collected) collected.textContent = ship && Number.isSafeInteger(ship.collected) ? String(ship.collected) : '—';
      const pickup = document.getElementById('game-pickup');
      if (pickup) {
        const latest = [...(this.collectEvents?.values() ?? [])].filter(event => event.player_id === ship?.id).at(-1);
        const active = latest && now - latest.at_ms < 800;
        const message = active ? '吸收 +' + latest.glyph_ids.length + (latest.fuel_added > 0 ? ' · 燃料 +' + latest.fuel_added.toFixed(1) : '') : '';
        if (pickup.textContent !== message) pickup.textContent = message;
      }
      const respawn = document.getElementById('game-respawn');
      if (respawn) { respawn.hidden = !ship || ship.hp > 0; respawn.textContent = ship && ship.hp <= 0 ? '重生倒计时 ' + Math.max(0, (ship.respawn_at_ms - now) / 1000).toFixed(1) + 's' : ''; }
      const region = document.getElementById('game-region');
      if (region) region.textContent = ship ? '区域 ' + String(ship.region).padStart(2, '0') + ' / 地图' : '区域地图';
      for (const button of document.querySelectorAll('[data-game-action]')) {
        const action = button.dataset.gameAction === 'fire' ? 'laser' : button.dataset.gameAction;
        const remaining = ship ? Math.max(0, (ship[action + '_ready_ms'] - now) / 1000) : Infinity;
        button.disabled = !ship || ship.hp <= 0 || remaining > 0;
        button.dataset.active = String(action === 'shield' && ship && ship.shield_until_ms > now);
        button.querySelector('.game-cooldown').textContent = ship ? remaining > 0 ? remaining.toFixed(1) + 's' : '就绪' : '—';
      }
    }
    inspectNearby() {
      // Re-resolve from the current authorized graph; no stale private selection.
      const node = this.flight && this.nearby && this.graph.nodes.find(n => n.id === this.nearby.id);
      if (!node) return;
      this.setPilot(false);
      this.callbacks.select?.(node);
    }
    shipGeometry(geometry, flight = this.flight, color = [.86, .88, .9]) {
      const b = flightBasis(flight.yaw, flight.pitch), { lines, points, solids } = geometry;
      const bank = flight.bank, right = b.right.map((v, i) => v * Math.cos(bank) + b.up[i] * Math.sin(bank));
      const up = b.up.map((v, i) => v * Math.cos(bank) - b.right[i] * Math.sin(bank));
      const at = ([x, y, z]) => flight.position.map((v, i) => v + right[i] * x + up[i] * y + b.eye[i] * z);
      const vertices = [[0, 0, -3.8], [-2.7, -.35, 2], [2.7, -.35, 2], [0, .85, .7], [0, -.55, 1.7], [0, .1, 2.1]].map(at);
      const face = (indices, light) => { for (const i of indices) solids.push(...vertices[i], ...color.map(v => v * light), 1, 1); };
      face([0, 1, 3], .56); face([0, 3, 2], .84); face([1, 2, 4], .2);
      face([0, 4, 1], .28); face([0, 2, 4], .35); face([1, 5, 3], .48); face([3, 5, 2], .62);
      for (const [a, z] of [[0, 1], [0, 2], [0, 3], [1, 3], [2, 3]])
        lines.push(...vertices[a], .9, .9, .9, .35, 1, ...vertices[z], .9, .9, .9, .35, 1);
      const nozzle = at([0, 0, 2.15]);
      points.push(...nozzle, .9, .93, 1, .75, flight.thrust > 0 ? 1.7 : .6);
      if (!this.reduced.matches && flight.thrust > 0) {
        const tail = at([0, 0, flight.boost ? 10 : 5.5]);
        lines.push(...nozzle, .95, .97, 1, .7, 1, ...tail, .85, .9, 1, .02, 1);
      }
      for (let i = 1; i < flight.trail.length; i++) {
        const previous = flight.trail[i - 1], sample = flight.trail[i];
        const alpha = .22 * (1 - sample.age / 1.4);
        lines.push(...previous.position, .88, .9, .95, alpha, 1, ...sample.position, .88, .9, .95, alpha, 1);
      }
    }
    setGraph(graph) {
      this.graph = graph;
      this.nearby = null;
      if (this.flight) this.updateFlightHud(true);
      this.satellites = graph.nodes.filter(n => n.orbitCenter);
      this.signals = graph.nodes.filter(n => n.kind !== 'user' && n.kind !== 'private');
      this.reindex();
      this.wake();
    }
    reindex() {
      this.pinned = this.graph.nodes.filter(n => n.id === 'u_root' || n.id === this.focusId);
      this.index = new M.SpatialIndex(this.graph.nodes.filter(n =>
        (n.kind === 'user' || n.kind === 'private') && n.id !== 'u_root' && n.id !== this.focusId));
    }
    prepareView() {
      const basis = this.basis(), lens = this.height * .86, c = this.camera;
      const classify = (position, radius) => {
        const delta = position.map((v, i) => v - c.target[i]);
        const dot = axis => delta.reduce((sum, v, i) => sum + v * axis[i], 0);
        const depth = c.distance - dot(basis.eye), far = depth + radius;
        if (far <= 1) return { visible: false, size: 0 };
        const margin = radius * lens / Math.max(1, depth - radius);
        const x = this.width / 2 + dot(basis.right) * lens / Math.max(depth, 1);
        const y = this.height * (.5 - this.verticalShift() / 2) - dot(basis.up) * lens / Math.max(depth, 1);
        return { visible: depth <= radius || (x + margin >= 0 && x - margin <= this.width &&
          y + margin >= 0 && y - margin <= this.height), size: margin * 2 };
      };
      const nodes = this.index.query(classify, { budget: this.software ? 128 : 256, threshold: 55 });
      nodes.push(...this.pinned);
      const present = new Set(nodes.map(n => n.id));
      for (const n of this.signals || []) if (!present.has(n.id)) {
        if (classify(n.position, 2).visible) { nodes.push(n); present.add(n.id); }
      }
      this.view = { nodes, links: this.graph.links.filter(([a, b]) => present.has(a.id) && present.has(b.id)) };
      this.canvas.dataset.visibleNodes = String(nodes.length);
      this.canvas.dataset.loadedNodes = String(this.graph.nodes.length);
    }
    setFocus(id) {
      if (this.flight && id !== this.focusId) this.setPilot(false);
      this.focusId = id;
      this.reindex();
      this.wake();
    }
    focus(node, distance = 74) {
      if (this.width < 600) distance = Math.max(distance, node.id === 'u_root' ? 195 : 125);
      this.focusId = node.id;
      this.reindex();
      this.travel(node.position, distance);
    }
    travel(target, distance) {
      if (this.flight) this.setPilot(false);
      this.destination = { target: [...target], distance };
      if (this.reduced.matches) {
        this.camera.target = [...target];
        this.camera.distance = distance;
        this.destination = null;
      }
      this.wake();
    }
    home() {
      this.focusId = null;
      this.reindex();
      this.travel([0, 0, 0], 370);
      this.callbacks.overview?.();
    }
    pause(value) {
      if (value && this.flight) this.setPilot(false);
      this.paused = value;
      this.wake();
    }
    wake() {
      this.dirty = true;
      if (!this.frame && this.available && !document.hidden && !this.lost)
        this.frame = requestAnimationFrame((t) => this.render(t));
    }
    basis() {
      const c = this.camera,
        sy = Math.sin(c.yaw),
        cy = Math.cos(c.yaw),
        sp = Math.sin(c.pitch),
        cp = Math.cos(c.pitch);
      return {
        right: [cy, 0, -sy],
        up: [-sp * sy, cp, -sp * cy],
        eye: [cp * sy, sp, cp * cy],
      };
    }
    verticalShift() {
      // Leave the selected star above the mobile inspector, not underneath it.
      return !this.flight && this.width < 600 && this.focusId ? .5 : 0;
    }
    project(p) {
      const b = this.basis(),
        r = p.map((n, i) => n - this.camera.target[i]),
        dot = (v) => r.reduce((s, n, i) => s + n * v[i], 0),
        depth = this.camera.distance - dot(b.eye);
      if (depth < 1) return null;
      const scale = (1.72 * this.height * 0.5) / depth;
      return {
        x: this.width / 2 + dot(b.right) * scale,
        y: this.height * (.5 - this.verticalShift() / 2) - dot(b.up) * scale,
        depth,
        scale,
      };
    }
    hit(x, y) {
      let best = null,
        score = Infinity;
      for (const node of this.view.nodes) {
        const p = this.project(node.position);
        if (!p) continue;
        const d = Math.hypot(p.x - x, p.y - y);
        const radius = node.kind === 'cluster' ? 4 : M.appearance(node, this.callbacks.now?.() ?? Date.now()).radius;
        if (d < Math.max(15, Math.min(36, p.scale * radius * 1.6)) && d < score) {
          score = d;
          best = node;
        }
      }
      return best;
    }
    events() {
      const c = this.canvas;
      c.addEventListener("contextmenu", (e) => e.preventDefault());
      c.addEventListener("pointerdown", (e) => {
        if (this.flight) return;
        if (e.button !== 0 && e.button !== 2) return;
        c.focus({ preventScroll: true });
        c.setPointerCapture(e.pointerId);
        this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
        if (this.pointers.size > 1 && this.start) this.start.dragged = true;
        this.start = {
          x: e.clientX,
          y: e.clientY,
          dragged: this.pointers.size > 1,
        };
        this.destination = null;
        this.wake();
      });
      c.addEventListener("pointermove", (e) => {
        if (this.flight) return;
        const old = this.pointers.get(e.pointerId);
        if (!old) {
          const r = c.getBoundingClientRect();
          const hovered = this.hit(e.clientX - r.left, e.clientY - r.top);
          c.style.cursor = hovered ? 'pointer' : 'grab';
          if (this.hoverId !== (hovered?.id || null)) {
            this.hoverId = hovered?.id || null;
            this.wake();
          }
          return;
        }
        const next = { x: e.clientX, y: e.clientY },
          dx = next.x - old.x,
          dy = next.y - old.y;
        if (Math.hypot(e.clientX - this.start.x, e.clientY - this.start.y) > 5)
          this.start.dragged = true;
        if (this.pointers.size === 2) {
          const other = [...this.pointers.entries()].find(
            ([id]) => id !== e.pointerId,
          )[1];
          const before = Math.hypot(old.x - other.x, old.y - other.y),
            after = Math.hypot(next.x - other.x, next.y - other.y);
          if (after > 8)
            this.camera.distance = clamp(
              (this.camera.distance * before) / after,
              22,
              900,
            );
        } else if (e.shiftKey || e.buttons === 2)
          this.pan(
            (-dx / this.height) * this.camera.distance,
            (dy / this.height) * this.camera.distance,
          );
        else {
          this.camera.yaw -= dx * 0.004;
          this.camera.pitch = clamp(this.camera.pitch + dy * 0.003, -1.3, 1.3);
        }
        this.pointers.set(e.pointerId, next);
        this.wake();
      });
      const release = (e) => {
        if (this.flight) return;
        const start = this.start;
        this.pointers.delete(e.pointerId);
        if (
          !this.pointers.size &&
          start &&
          !start.dragged &&
          e.type === "pointerup" &&
          e.button === 0
        ) {
          const r = c.getBoundingClientRect(),
            node = this.hit(e.clientX - r.left, e.clientY - r.top);
          if (node?.kind === 'cluster') {
            this.callbacks.overview?.();
            this.travel(node.position, Math.max(45, node.radius * 3));
          }
          else if (node) this.callbacks.select?.(node);
        }
        this.wake();
      };
      c.addEventListener("pointerup", release);
      c.addEventListener("pointercancel", release);
      c.addEventListener('pointerleave', () => { this.hoverId = null; this.wake(); });
      c.addEventListener(
        "wheel",
        (e) => {
          e.preventDefault();
          if (this.flight) return;
          this.destination = null;
          this.camera.distance = clamp(
            this.camera.distance *
              Math.exp(clamp(e.deltaY, -200, 200) * 0.0015),
            22,
            900,
          );
          this.wake();
        },
        { passive: false },
      );
      c.addEventListener("keydown", (e) => {
        if (this.flight || e.ctrlKey || e.metaKey || e.altKey || e.isComposing) return;
        const key = e.key.toLowerCase();
        if (
          [
            "w",
            "a",
            "s",
            "d",
            "q",
            "e",
            "arrowleft",
            "arrowright",
            "arrowup",
            "arrowdown",
            "h",
          ].includes(key)
        ) {
          e.preventDefault();
          if (key === "h") this.home();
          else this.keys.add(key);
          this.wake();
        }
      });
      c.addEventListener("keyup", (e) => this.keys.delete(e.key.toLowerCase()));
      c.addEventListener("blur", () => this.stopFlightInput());
      document.addEventListener("visibilitychange", () => {
        this.stopFlightInput();
        this.keys.clear();
        this.pointers.clear();
        this.last = 0;
        if (document.hidden) {
          cancelAnimationFrame(this.frame);
          this.frame = 0;
        } else this.wake();
      });
      this.reduced.addEventListener("change", () => {
        this.pause(this.reduced.matches);
        this.callbacks.motion?.(this.paused);
      });
    }
    pan(x, y) {
      const b = this.basis();
      this.camera.target = this.camera.target.map((n, i) =>
        clamp(n + b.right[i] * x + b.up[i] * y, -500, 500),
      );
    }
    render(timestamp) {
      this.frame = 0;
      if (!this.available || document.hidden || this.lost) return;
      const dt = Math.max(0, Math.min(0.12, this.last ? (timestamp - this.last) / 1000 : 0));
      this.last = timestamp;
      const c = this.camera;
      if (!this.paused) {
        this.clock += dt;
        if (!this.flight && !this.pointers.size && !this.keys.size && !this.destination)
          c.yaw += dt * 0.008;
      }
      if (this.destination) {
        const t = 1 - Math.exp(-dt * 5);
        c.target = c.target.map((n, i) =>
          mix(n, this.destination.target[i], t),
        );
        c.distance = mix(c.distance, this.destination.distance, t);
        if (
          Math.abs(c.distance - this.destination.distance) < 0.03 &&
          Math.hypot(
            ...c.target.map((n, i) => n - this.destination.target[i]),
          ) < 0.03
        )
          this.destination = null;
      }
      if (this.flight) this.updateFlight(dt);
      else if (this.keys.size) {
        this.destination = null;
        const s = dt * c.distance * 0.45;
        this.pan(
          (this.keys.has("d") - this.keys.has("a")) * s,
          (this.keys.has("w") - this.keys.has("s")) * s,
        );
        c.yaw +=
          (this.keys.has("arrowright") - this.keys.has("arrowleft")) * dt;
        c.pitch = clamp(
          c.pitch +
            (this.keys.has("arrowup") - this.keys.has("arrowdown")) * dt,
          -1.3,
          1.3,
        );
        c.distance = clamp(
          c.distance * Math.exp((this.keys.has("q") - this.keys.has("e")) * dt),
          22,
          900,
        );
      }
      this.callbacks.tick?.(this.clock);
      for (const node of this.satellites) node.position = M.satellite(node.id, node.orbitCenter, this.clock, node.orbit);
      this.prepareView();
      if (this.software) {
        this.renderSoftware();
        this.finish();
        return;
      }
      const gl = this.gl,
        b = this.basis(),
        u = this.uniforms;
      gl.viewport(0, 0, this.canvas.width, this.canvas.height);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      gl.useProgram(this.program);
      gl.uniform3fv(u.uTarget, c.target);
      gl.uniform3fv(u.uRight, b.right);
      gl.uniform3fv(u.uUp, b.up);
      gl.uniform3fv(u.uEye, b.eye);
      gl.uniform3f(
        u.uLens,
        1.72,
        this.width / Math.max(1, this.height),
        c.distance,
      );
      gl.uniform1f(u.uHeight, this.canvas.height);
      gl.uniform1f(u.uShift, this.verticalShift());
      const draw = (vertices, mode, pointKind = 0) => {
        if (!vertices.length) return;
        gl.bufferData(
          gl.ARRAY_BUFFER,
          vertices instanceof Float32Array ? vertices : new Float32Array(vertices),
          gl.DYNAMIC_DRAW,
        );
        gl.uniform1f(u.uPoints, pointKind || (mode === gl.POINTS ? 1 : 0));
        gl.drawArrays(mode, 0, vertices.length / 8);
      };
      draw(this.clouds, gl.POINTS, 3);
      draw(this.renderDust ?? this.dust, gl.POINTS, 2);
      const geometry = this.geometry();
      gl.enable(gl.DEPTH_TEST);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      draw(geometry.solids, gl.TRIANGLES);
      gl.disable(gl.DEPTH_TEST);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
      draw(geometry.triangles, gl.TRIANGLES);
      draw(geometry.lines, gl.LINES);
      draw(geometry.points, gl.POINTS);
      this.finish();
    }
    finish() {
      this.updateLabels();
      this.updateShipLabels();
      this.updateTokens();
      this.callbacks.camera?.(this.camera.target);
      this.dirty = false;
      if (!this.paused || this.destination || this.keys.size || this.flightControls.size ||
          (this.flight && (this.flight.speed > .01 || this.flight.trail.length)))
        this.frame = requestAnimationFrame((t) => this.render(t));
    }
    geometry() {
      const lines = [], points = [], triangles = [], solids = [], b = this.basis();
      const now = this.callbacks.now?.() ?? Date.now();
      const vertex = (p, col, alpha, size = 1) => [...p, ...col, alpha, size];
      const line = (a, z, col, alpha) => lines.push(...vertex(a, col, alpha), ...vertex(z, col, alpha));
      const ring = (p, radius, col, alpha, { fraction = 1, tilt = .35, sides = 80, start = 0, dashed = false, basis = null } = {}) => {
        const at = (angle) => basis ? p.map((value, i) => value + radius * (Math.cos(angle) * basis[0][i] + Math.sin(angle) * basis[1][i])) : [p[0] + Math.cos(angle) * radius,
          p[1] + Math.sin(angle) * radius * Math.sin(tilt), p[2] + Math.sin(angle) * radius * Math.cos(tilt)];
        const count = Math.max(1, Math.ceil(sides * fraction));
        for (let j = 0; j < count; j++) {
          if (dashed && j % 5 === 4) continue;
          line(at(start + j / sides * M.TAU), at(start + Math.min(fraction, (j + 1) / sides) * M.TAU), col, alpha);
        }
      };
      for (const [a, z, type] of this.view.links) {
        const related = [a.id, z.id, a.author?.id, z.author?.id].includes(this.focusId);
        const col = type.startsWith('private') ? palette.private : palette.reply;
        const alpha = type === 'mutual' ? related ? .7 : .4 : type === 'follow' ? related ? .3 : .13 : type === 'root-attachment' ? .07 : type === 'orbit' ? .10 : related ? .33 : .08;
        let last = a.position;
        for (let j = 1; j <= 24; j++) {
          const t = j / 24;
          const p = a.position.map((n, i) => mix(n, z.position[i], t) + (i === 1 ? Math.sin(t * Math.PI) * 10 : 0));
          line(last, p, col, alpha);
          last = p;
        }
      }
      let detailed = 0;
      const groups = new Set();
      for (const node of this.view.nodes) {
        if (node.kind === 'cluster') {
          // Clusters count ONLY loaded, authorized identities; not fake users
          // or a global popularity estimate. They have no certificate/money.
          points.push(...vertex(node.position, [.67, .73, .70], .38, Math.min(18, 4 + Math.log2(node.count))));
          continue;
        }
        const p = node.position, style = M.appearance(node, now);
        const selected = node.id === this.focusId, hovered = node.id === this.hoverId;
        const isStar = node.kind === 'user' || node.kind === 'private';
        const col = isStar ? style.color : palette[node.kind] || palette.post;
        const light = isStar ? style.light : .7;
        let traits = this.visualTraits.get(node);
        if (!traits) {
          const rng = M.random('shape:' + node.id);
          traits = { phase: rng() * M.TAU, speed: .025 + rng() * .085, tilt: .8 + rng() * .6, mesh: planetMesh(node.id) };
          this.visualTraits.set(node, traits);
        }
        const pulse = 1 + (this.paused ? 0 : Math.sin(this.clock * (1 + traits.speed * 8) + traits.phase) * style.pulse);
        points.push(...vertex(p, col, light * pulse * (isStar ? .38 : 1), style.root ? 25 : isStar ? 5.5 : 2.2));
        const projected = this.project(p);
        if (!selected && !hovered && !style.root && (!projected || projected.scale * style.radius < 3 || detailed >= (this.software ? 24 : 64))) continue;
        detailed++;
        if (isStar) {
          const postRing = node.post_ring;
          if (postRing?.visible && postRing.ring_radii?.length && postRing.basis?.length === 2) {
            for (const radius of postRing.ring_radii)
              ring(postRing.center || p, radius, col, selected ? .5 : .25, {basis:postRing.basis, sides:64});
          }
          const orbit = node.orbit;
          if (['binary', 'multi'].includes(orbit?.kind) && orbit.center?.length === 3 && !groups.has(orbit.group_id)) {
            groups.add(orbit.group_id);
            ring(orbit.center, orbit.radius, [.78, .83, .88], .25, {tilt:orbit.tilt || 0, sides:64, dashed:true});
            points.push(...vertex(orbit.center, [.85, .89, .94], .7, .6));
          }
          const r = style.radius, rotation = this.clock * traits.speed + traits.phase;
          const rotate = v => [v[0] * Math.cos(rotation) - v[2] * Math.sin(rotation), v[1],
            v[0] * Math.sin(rotation) + v[2] * Math.cos(rotation)];
          const cameraPosition = this.camera.target.map((v, i) => v + b.eye[i] * this.camera.distance);
          const towardEye = unit(cameraPosition.map((v, i) => v - p[i]));
          const sun = style.root ? towardEye : unit(p.map(v => -v));
          for (const face of traits.mesh) {
            const local = face.map(rotate), normal = unit(local[0].map((v, i) => v + local[1][i] + local[2][i]));
            if (dot(normal, towardEye) < -.12) continue;
            const diffuse = Math.max(0, dot(normal, sun));
            const rimLight = (1 - Math.max(0, dot(normal, towardEye))) ** 3;
            const terrain = .90 + .1 * Math.sin(normal[1] * 19 + traits.phase);
            const shade = style.root ? .72 + .26 * diffuse : (.14 + .62 * diffuse + .18 * rimLight) * terrain;
            const faceColor = col.map(v => v * shade * (.35 + light * .65));
            for (const v of local) solids.push(...vertex(v.map((n, i) => p[i] + n * r), faceColor, 1));
          }
          if (style.root) {
            // The trust anchor has a white core, three thin coronas and a cross.
            ring(p, r * 1.8, col, .26, { tilt: .7 });
            ring(p, r * 2.2, col, .13, { tilt: -.8, dashed: true });
            ring(p, r * 2.7, col, .07, { tilt: .1 });
            for (const axis of [b.right, b.up]) for (const sign of [-1, 1]) {
              const from = p.map((v, i) => v + axis[i] * r * 1.7 * sign);
              const to = p.map((v, i) => v + axis[i] * r * 3.9 * sign);
              line(from, to, col, .26);
            }
          } else if (style.certified) {
            // An angular seal is distinct from the round balance arc.
            ring(p, r * 2.3, col, .38, { sides: 6, tilt: -.55 });
            const mark = p.map((v, i) => v + b.up[i] * r * 3.0);
            const diamond = [b.up, b.right, b.up.map(v => -v), b.right.map(v => -v)]
              .map(axis => mark.map((v, i) => v + axis[i] * .8));
            for (let i = 0; i < 4; i++) line(diamond[i], diamond[(i + 1) % 4], col, .8);
          }
          if (style.reserve.known && style.reserve.fraction > 0) {
            const center = [p[0], p[1] - r * 1.9, p[2]];
            const radius = r * (style.root ? 3.2 : 3.5);
            ring(center, radius, [.9, .9, .86], .055, { tilt: .6 });
            ring(center, radius, [.9, .9, .86], .44, { fraction: style.reserve.fraction, tilt: .6, start: -Math.PI / 2, dashed: true });
          }
        } else {
          const r = 1.1;
          const diamond = [b.up, b.right, b.up.map(v => -v), b.right.map(v => -v)]
            .map(axis => p.map((v, i) => v + axis[i] * r));
          for (let i = 0; i < 4; i++) line(diamond[i], diamond[(i + 1) % 4], col, .5);
        }
        if (selected || hovered) {
          const size = style.root ? 17 : 6.7;
          for (const sx of [-1, 1]) for (const sy of [-1, 1]) {
            const corner = p.map((v, i) => v + b.right[i] * size * sx + b.up[i] * size * sy);
            for (const [axis, sign] of [[b.right, sx], [b.up, sy]])
              line(corner, corner.map((v, i) => v - axis[i] * sign * size * .25), [1, 1, 1], selected ? .65 : .25);
          }
        }
      }
      if (this.homeBody && this.flight) {
        const body = this.homeBody;
        const mesh = this.homeMesh || planetMesh(body.id);
        for (const face of mesh) for (const v of face)
          solids.push(...vertex(v.map((value, i) => body.position[i] + value * body.radius), [.38, .43, .40], 1));
        ring(body.position, body.radius * 1.5, [.67, .75, .7], .3, {tilt:.15});
      }
      if (this.flight && (!this.network || this.network.connected && this.network.self?.hp > 0)) this.shipGeometry({ lines, points, solids });
      if (this.flight && this.network?.connected) {
        const serverNow = this.network.serverNow;
        for (const {state:ship, position, yaw = ship.yaw, pitch = ship.pitch} of this.remoteShips.values()) {
          if (ship.id === this.network.self?.id || ship.hp <= 0) continue;
          const flight = {position, yaw:-yaw, pitch:-pitch, bank:0, trail:[],
            thrust:Math.hypot(...ship.velocity) > 2 ? 1 : 0, boost:false};
          this.shipGeometry({lines, points, solids}, flight, ship.guest ? [.74, .77, .80] : [.8, .87, .84]);
          if (ship.shield_until_ms > serverNow) ring(position, 4.5, [.6, .87, .95], .6, {tilt:.4, sides:32});
        }
        const own = this.network.self;
        if (own?.shield_until_ms > serverNow) ring(this.flight.position, 4.5, [.6, .87, .95], .6, {tilt:.4, sides:32});
        for (const [id, event] of this.shotEvents) {
          const age = serverNow - event.at_ms;
          if (age > 400) { this.shotEvents.delete(id); continue; }
          line(event.position, event.end, [.98, .66, .46], clamp(1 - age / 400, 0, 1));
        }
      }
      return { lines, points, triangles, solids };
    }
    absorptionGlyphs() {
      if (!this.flight || !this.network?.connected || this.reduced.matches) return [];
      const result = [], now = this.network.serverNow;
      for (const event of this.collectEvents?.values() ?? []) {
        const age = now - event.at_ms, progress = clamp(age / 500, 0, 1);
        if (age < 0 || progress >= 1) continue;
        const destination = event.player_id === this.network.self.id ? this.flight.position : this.remoteShips.get(event.player_id)?.position;
        if (!destination) continue;
        for (const id of event.glyph_ids) {
          const origin = this.dust.subarray(id * 8, id * 8 + 3);
          const t = progress * progress;
          result.push({ key:'collect:' + event.id + ':' + id, id, position:[...origin].map((v, i) => mix(v, destination[i], t)), progress });
          if (result.length >= 64) return result;
        }
      }
      return result;
    }
    renderSoftware() {
      const ctx = this.context, dpr = this.canvas.width / Math.max(this.width, 1);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, this.width, this.height);
      for (let i = 0; i < this.clouds.length; i += this.width < 600 ? 16 : 8) {
        const p = this.project(this.clouds.slice(i, i + 3));
        if (!p) continue;
        const radius = clamp(p.scale * this.clouds[i + 7], 2, 140);
        if (p.x + radius < 0 || p.x - radius > this.width || p.y + radius < 0 || p.y - radius > this.height) continue;
        const fog = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, radius);
        fog.addColorStop(0, `rgba(168,168,168,${this.clouds[i + 6]})`);
        fog.addColorStop(.45, `rgba(168,168,168,${this.clouds[i + 6] * .48})`);
        fog.addColorStop(1, 'rgba(168,168,168,0)');
        ctx.fillStyle = fog; ctx.fillRect(p.x - radius, p.y - radius, radius * 2, radius * 2);
      }
      const dust = this.renderDust ?? this.dust;
      for (let i = 0; i < dust.length; i += 8) {
        const p = this.project(dust.subarray(i, i + 3));
        if (!p || p.x < 0 || p.x > this.width || p.y < 0 || p.y > this.height) continue;
        ctx.fillStyle = `rgba(220,220,220,${dust[i + 6] * .65})`;
        const size = clamp(p.scale * dust[i + 7], .5, 3);
        ctx.fillRect(p.x, p.y, size, size);
      }
      const geometry = this.geometry();
      const color = (array, offset) => `rgba(${array.slice(offset + 3, offset + 6).map(v => Math.round(v * 255)).join(',')},${clamp(array[offset + 6], 0, 1)})`;
      for (const [type, width] of [['solids', 24], ['triangles', 24], ['lines', 16]]) {
        let array = geometry[type];
        if (type === 'solids') {
          const faces = [];
          for (let i = 0; i < array.length; i += 24) {
            const depth = [0, 8, 16].map(j => this.project(array.slice(i + j, i + j + 3))?.depth ?? -1);
            if (depth.every(d => d > 0)) faces.push({ vertices: array.slice(i, i + 24), depth: depth.reduce((a, b) => a + b, 0) });
          }
          array = faces.sort((a, b) => b.depth - a.depth).flatMap(face => face.vertices);
        }
        for (let i = 0; i < array.length; i += width) {
          const vertices = [];
          for (let j = 0; j < width; j += 8) vertices.push(this.project(array.slice(i + j, i + j + 3)));
          if (vertices.some(p => !p)) continue;
          ctx.beginPath(); ctx.moveTo(vertices[0].x, vertices[0].y);
          for (const p of vertices.slice(1)) ctx.lineTo(p.x, p.y);
          if (type !== 'lines') { ctx.closePath(); ctx.fillStyle = color(array, i); ctx.fill(); }
          else { ctx.strokeStyle = color(array, i); ctx.lineWidth = 1; ctx.stroke(); }
        }
      }
      const array = geometry.points;
      for (let i = 0; i < array.length; i += 8) {
        const p = this.project(array.slice(i, i + 3));
        if (!p) continue;
        const r = clamp(p.scale * array[i + 7], 2, 50), rgb = array.slice(i + 3, i + 6).map(v => Math.round(v * 255)).join(',');
        const gradient = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r);
        gradient.addColorStop(0, `rgba(${rgb},${array[i + 6]})`);
        gradient.addColorStop(.3, `rgba(${rgb},${array[i + 6] * .16})`);
        gradient.addColorStop(1, `rgba(${rgb},0)`);
        ctx.fillStyle = gradient; ctx.fillRect(p.x - r, p.y - r, r * 2, r * 2);
      }
    }
    updateTokens() {
      if (!this.tokenField) return;
      const occupied = this.view.nodes.map(n => this.project(n.position)).filter(Boolean);
      const visible = new Set();
      const maximum = this.width < 600 ? 20 : 40;
      for (let i = 0; i < this.dust.length && visible.size < maximum; i += 8 * 17) {
        if (this.glyphTaken(i / 8)) continue;
        const p = this.project(this.dust.slice(i, i + 3));
        if (!p || p.x < 50 || p.x > this.width - 50 || p.y < 115 || p.y > this.height - 130 ||
            occupied.some(q => Math.hypot(q.x - p.x, q.y - p.y) < 28)) continue;
        visible.add(i);
        let token = this.tokenNodes.get(i);
        if (!token) {
          token = document.createElement('span');
          token.textContent = ['{', '}', '[]', '::', '<>', '/', '+', '_', '01'][(i / 8) % 9];
          this.tokenField.append(token); this.tokenNodes.set(i, token);
        }
        token.style.transform = `translate(${p.x.toFixed(2)}px,${p.y.toFixed(2)}px)`;
        token.style.fontSize = clamp(p.scale * 4.5, 7, this.flight ? 17 : 12) + 'px';
        token.style.opacity = clamp(.12 + p.scale * .025, .12, .25);
      }
      for (const effect of this.absorptionGlyphs()) {
        const p = this.project(effect.position);
        if (!p || p.x < 0 || p.x > this.width || p.y < 0 || p.y > this.height) continue;
        visible.add(effect.key);
        let token = this.tokenNodes.get(effect.key);
        if (!token) {
          token = document.createElement('span'); token.textContent = ['{', '}', '[]', '::', '<>', '/', '+', '_', '01'][effect.id % 9];
          this.tokenField.append(token); this.tokenNodes.set(effect.key, token);
        }
        token.style.transform = `translate(${p.x.toFixed(2)}px,${p.y.toFixed(2)}px)`;
        token.style.fontSize = clamp(p.scale * 4.5, 8, 18) + 'px'; token.style.opacity = .8 * (1 - effect.progress);
      }
      for (const [id, token] of this.tokenNodes) if (!visible.has(id)) { token.remove(); this.tokenNodes.delete(id); }
    }
    updateLabels() {
      const candidates = this.view.nodes
        .map((n) => ({ n, p: this.project(n.position) }))
        .filter(
          ({ p }) =>
            p &&
            p.x > 30 &&
            p.x < this.width - 70 &&
            p.y > 125 &&
            p.y < this.height - 150,
        )
        .sort(
          (a, b) =>
            (b.n.id === this.focusId) - (a.n.id === this.focusId) ||
            (b.n.id === 'u_root') - (a.n.id === 'u_root') ||
            (b.n.id === this.hoverId) - (a.n.id === this.hoverId) ||
            a.p.depth - b.p.depth,
        );
      const used = [],
        visible = new Set();
      for (const { n, p } of candidates) {
        if (used.length >= (this.width < 600 ? 5 : 8)) break;
        if (
          used.some(
            (q) => Math.abs(q.x - p.x) < 100 && Math.abs(q.y - p.y) < 32,
          )
        )
          continue;
        used.push(p);
        visible.add(n.id);
        let el = this.labelNodes.get(n.id);
        if (!el) {
          el = document.createElement("span");
          el.className = "star-label";

          this.labels.append(el);
          this.labelNodes.set(n.id, el);
        }
        const look = M.appearance(n, this.callbacks.now?.() ?? Date.now());
        el.textContent =
          (n.kind === 'cluster' ? '⋯ ' : look.root ? '✦ ' : look.certified ? '◇ ' : '') +
          (n.kind === 'user'
            ? M.handle(n.name || n.title || 'Signal')
            : n.name || n.title || 'Signal');
        el.classList.toggle('root-label', look.root);
        el.classList.toggle('certified-label', look.certified);
        el.style.setProperty('--star-color', `rgb(${look.color.map(v => Math.round(v * 255)).join(',')})`);
        el.style.opacity = n.id === this.focusId || look.root ? 1 : .35 + look.light * .45;
        el.classList.toggle("selected", n.id === this.focusId);
        el.style.transform = `translate(${Math.round(p.x + 12)}px,${Math.round(p.y - 7)}px)`;
      }
      for (const [id, el] of this.labelNodes)
        if (!visible.has(id)) {
          el.remove();
          this.labelNodes.delete(id);
        }
    }
    updateShipLabels() {
      const container = document.getElementById('ship-labels');
      if (!container || !this.network?.connected || !this.flight) {
        for (const label of this.shipLabels.values()) label.remove();
        this.shipLabels.clear(); return;
      }
      const visible = new Set(), now = this.network.serverNow, up = this.basis().up;
      for (const {state:ship, position} of this.remoteShips.values()) {
        if (ship.hp <= 0) continue;
        const projected = this.project(position.map((value, i) => value + up[i] * 4));
        if (!projected || projected.x < 0 || projected.y < 75 || projected.x > this.width || projected.y > this.height - 65) continue;
        visible.add(ship.id);
        let label = this.shipLabels.get(ship.id);
        if (!label) {
          label = document.createElement('span'); label.className = 'ship-label';
          const name = document.createElement('span'); name.className = 'ship-name';
          const hp = document.createElement('progress'); hp.className = 'ship-hp'; hp.max = 100; hp.value = 0;
          label.append(name, hp); container.append(label); this.shipLabels.set(ship.id, label);
        }
        if (label.firstElementChild.textContent !== ship.handle) label.firstElementChild.textContent = ship.handle;
        if (label.lastElementChild.value !== ship.hp) label.lastElementChild.value = ship.hp;
        const own = String(ship.id === this.network.self?.id), shielded = String(ship.shield_until_ms > now);
        if (label.dataset.self !== own) label.dataset.self = own;
        if (label.dataset.shielded !== shielded) label.dataset.shielded = shielded;
        const transform = `translate(${Math.round(projected.x)}px,${Math.round(projected.y)}px)`;
        if (label.style.transform !== transform) label.style.transform = transform;
      }
      for (const [id, label] of this.shipLabels) if (!visible.has(id)) { label.remove(); this.shipLabels.delete(id); }
    }
  }
  globalThis.MSGUniverseFlight = Object.freeze({ Flight, flightBasis, tokenNebula, planetMesh, MotionTrack, FlightPrediction, integrateFlight, inertialSegment });
  globalThis.MSGUniverseRenderer = UniverseRenderer;
})();
