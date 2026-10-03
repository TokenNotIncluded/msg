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
  function gravityAcceleration(position, wells, limits = {}) {
    const acceleration = [0, 0, 0], strength = limits.gravity_strength ?? 8;
    for (const well of wells) {
      const offset = well.position.map((v, i) => v - position[i]), distance = Math.hypot(...offset);
      if (distance < 1e-8 || distance >= well.influence) continue;
      const magnitude = Math.min(strength, strength * (well.radius + (limits.gravity_softening ?? 8)) ** 2 / Math.max(distance ** 2, .01)) * (1 - distance / well.influence) ** 2;
      for (let i = 0; i < 3; i++) acceleration[i] += offset[i] / distance * magnitude;
    }
    const length = Math.hypot(...acceleration), cap = limits.gravity_max_acceleration ?? 8;
    return length > cap ? acceleration.map(v => v * cap / length) : acceleration;
  }
  function planetContact(start, end, velocity, wells, limits = {}) {
    let beginning = [...start];
    const shipRadius = limits.ship_radius ?? 1.2, margin = .02;
    for (const well of wells) {
      const offset = beginning.map((v, i) => v - well.position[i]), distance = Math.hypot(...offset);
      const boundary = well.radius + shipRadius + margin;
      if (distance < boundary) {
        const normal = distance > 1e-8 ? offset.map(v => v / distance) : [0, 1, 0];
        const correction = well.position.map((v, i) => v + normal[i] * boundary - beginning[i]);
        beginning = beginning.map((v, i) => v + correction[i]); end = end.map((v, i) => v + correction[i]);
        const inward = dot(velocity, normal);
        if (inward < 0) velocity = velocity.map((v, i) => v - normal[i] * inward);
      }
    }
    const movement = end.map((v, i) => v - beginning[i]), length2 = dot(movement, movement);
    let first = null;
    if (length2 > 1e-12) for (const well of wells) {
      const offset = beginning.map((v, i) => v - well.position[i]), along = dot(offset, movement);
      const boundary = well.radius + shipRadius, discriminant = along ** 2 - length2 * (dot(offset, offset) - boundary ** 2);
      if (along >= 0 || discriminant < 0) continue;
      const fraction = (-along - Math.sqrt(discriminant)) / length2;
      if (fraction >= 0 && fraction <= 1 && (!first || fraction < first.fraction)) first = {fraction, well};
    }
    if (first) {
      const {fraction, well} = first, touch = beginning.map((v, i) => v + movement[i] * fraction);
      const offset = touch.map((v, i) => v - well.position[i]), distance = Math.hypot(...offset);
      const normal = offset.map(v => v / Math.max(distance, 1e-8));
      end = well.position.map((v, i) => v + normal[i] * (well.radius + shipRadius + margin));
      const inward = dot(velocity, normal);
      if (inward < 0) velocity = velocity.map((v, i) => v - normal[i] * inward);
    }
    for (let pass = 0; pass < 4; pass++) {
      let corrected = false;
      for (const well of wells) {
        const offset = end.map((v, i) => v - well.position[i]), distance = Math.hypot(...offset);
        const boundary = well.radius + shipRadius + margin;
        if (distance >= boundary - 1e-9) continue;
        const normal = distance > 1e-8 ? offset.map(v => v / distance) : [0, 1, 0];
        end = well.position.map((v, i) => v + normal[i] * boundary);
        const inward = dot(velocity, normal);
        if (inward < 0) velocity = velocity.map((v, i) => v - normal[i] * inward);
        corrected = true;
      }
      if (!corrected) break;
    }
    if (wells.some(well => Math.hypot(...end.map((v, i) => v - well.position[i])) < well.radius + shipRadius + margin - 1e-9)) {
      // Overlapping spheres can alternate projections forever. Exit their union
      // on the shortest axis ray, with the same interval/tie order as authority.
      let best = null;
      for (let axis = 0; axis < 3; axis++) for (const sign of [-1, 1]) {
        const intervals = [];
        for (const well of wells) {
          const offset = end.map((v, i) => v - well.position[i]), along = offset[axis] * sign;
          const boundary = well.radius + shipRadius + margin;
          const discriminant = boundary ** 2 - (dot(offset, offset) - along ** 2);
          if (discriminant < 0) continue;
          const half = Math.sqrt(discriminant), enter = -along - half, leave = -along + half;
          if (leave >= 0) intervals.push([enter, leave]);
        }
        let distance = 0;
        for (const [enter, leave] of intervals.sort((a, b) => a[0] - b[0] || a[1] - b[1])) {
          if (enter > distance + 1e-9) break;
          if (leave >= distance) distance = leave + 1e-6;
        }
        if (!best || distance < best.distance) best = {distance, axis, sign};
      }
      end = [...end]; end[best.axis] += best.sign * best.distance;
      for (const well of wells) {
        const offset = end.map((v, i) => v - well.position[i]), distance = Math.hypot(...offset);
        if (distance > well.radius + shipRadius + margin + 2e-6) continue;
        const normal = offset.map(v => v / Math.max(distance, 1e-8)), inward = dot(velocity, normal);
        if (inward < 0) velocity = velocity.map((v, i) => v - normal[i] * inward);
      }
    }
    return {position:end, velocity};
  }
  function integrateFlight(state, seconds, controls, limits = {}, now = 0, gravity = {}) {
    const dt = Number.isFinite(seconds) ? clamp(seconds, 0, .04) : 0;
    if (!dt || state.hp <= 0) return;
    const braking = controls.brake === true, yaw = controls.yaw, pitch = controls.pitch;
    const forward = [Math.sin(yaw) * Math.cos(pitch), Math.sin(pitch), -Math.cos(yaw) * Math.cos(pitch)];
    const right = [Math.cos(yaw), 0, Math.sin(yaw)];
    const direction = forward.map((v, i) => v * controls.throttle + right[i] * controls.strafe + (i === 1 ? controls.lift : 0));
    const hasControls = [controls.throttle, controls.strafe, controls.lift].some(n => Math.abs(n) > 1e-8), dashEnd = state.dash_until_ms ?? 0;
    const start = now - dt * 1000, dashStart = dashEnd - (limits.dash_seconds ?? 1) * 1000;
    const boundaries = [start, ...[dashStart, dashEnd].filter(at => at > start && at < now), now].sort((a, b) => a - b);
    const burn = limits.fuel_burn_rate ?? 1.5, regen = limits.fuel_regen_rate ?? 6, emergency = limits.fuel_emergency_regen_rate ?? 2;
    const wells = [...(gravity?.wells ?? [])];
    if (state.home_body?.private) wells.push({...state.home_body, influence:Math.max(36, state.home_body.radius * 8)});
    const segment = (duration, target = null, acceleration = 0) => {
      const result = inertialSegment(state.velocity, duration, target, acceleration, limits);
      state.velocity = result.velocity; return result.movement;
    };
    for (let i = 1; i < boundaries.length; i++) {
      const duration = (boundaries[i] - boundaries[i - 1]) / 1000, middle = (boundaries[i] + boundaries[i - 1]) / 2;
      const dash = middle >= dashStart && middle < dashEnd, thrust = hasControls ? direction : dash ? forward : null;
      let movement;
      if (braking) {
        movement = segment(duration, [0, 0, 0], limits.brake_deceleration ?? 48);
        state.fuel = Math.min(100, state.fuel + regen * duration);
      }
      else if (thrust && state.fuel > 0) {
        const powered = Math.min(duration, state.fuel / burn), length = Math.max(1, Math.hypot(...thrust));
        const speed = dash ? limits.max_speed ?? 60 : limits.cruise_speed ?? 20;
        movement = segment(powered, thrust.map(v => v * speed / length), dash ? limits.dash_acceleration ?? 96 : limits.thrust_acceleration ?? 24);
        state.fuel = Math.max(0, state.fuel - powered * burn);
        if (powered < duration) {
          const coast = segment(duration - powered); movement = movement.map((v, i) => v + coast[i]);
          state.fuel = Math.min(100, state.fuel + emergency * (duration - powered));
        }
      } else {
        movement = segment(duration); state.fuel = Math.min(100, state.fuel + (thrust ? emergency : regen) * duration);
      }
      if (!braking) {
        const acceleration = gravityAcceleration(state.position, wells, limits);
        movement = movement.map((v, i) => v + acceleration[i] * duration ** 2 / 2);
        state.velocity = state.velocity.map((v, i) => v + acceleration[i] * duration);
        const speed = Math.hypot(...state.velocity), cap = limits.max_speed ?? 60;
        if (speed > cap) state.velocity = state.velocity.map(v => v * cap / speed);
      }
      const contact = planetContact(state.position, state.position.map((v, i) => v + movement[i]), state.velocity, wells, limits);
      state.position = contact.position; state.velocity = contact.velocity;
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
    reconcile(ship, reset = false, age = 0, controls = null, limits = {}, now = 0, gravity = {}, resume = false) {
      const previous = [...this.position], error = previous.map((p, i) => p - ship.position[i]);
      if (reset || ship.region !== this.state.region || (ship.hp <= 0) !== (this.state.hp <= 0) || (!resume && Math.hypot(...error) > 45)) this.reset(ship);
      else {
        this.state = { ...ship, position: [...ship.position], velocity: [...ship.velocity] };
        // Bring the physical tick to the render clock before applying a visual
        // correction; otherwise the 15 Hz tick remainder jitters the camera.
        if (controls) for (let remaining = clamp(age, 0, .15); remaining > 0; ) {
          const dt = Math.min(remaining, .04); remaining -= dt;
          integrateFlight(this.state, dt, controls, limits, now - remaining * 1000, gravity);
        }
        this.offset = previous.map((p, i) => p - this.state.position[i]);
      }
    }
    step(seconds, controls, limits, now, reduced = false, gravity = {}) {
      const dt = Number.isFinite(seconds) ? clamp(seconds, 0, .12) : 0;
      // A slow 20-30 FPS device still advances the whole visible interval.
      // Smaller physics substeps retain dash/fuel boundaries and hard bounds.
      for (let remaining = dt; remaining > 0; ) {
        const step = Math.min(remaining, .04); remaining -= step;
        integrateFlight(this.state, step, controls, limits, now - remaining * 1000, gravity);
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
      this.yawRate = 0;
      this.pitchRate = 0;
      this.bank = 0;
      this.thrust = 0;
      this.boost = false;
      this.trail = [];
      this.trailClock = 0;
    }
    get speed() { return Math.hypot(...this.velocity); }
    neutral() { this.yawRate = 0; this.pitchRate = 0; this.thrust = 0; this.boost = false; }
    halt() { this.neutral(); this.velocity.fill(0); this.trail.length = 0; }
    steer(seconds, input = {}) {
      const dt = Number.isFinite(seconds) ? clamp(seconds, 0, .04) : 0;
      if (!dt) return;
      // Bounded angular acceleration, with attitude stabilization on release.
      // Integrate each rate analytically once; wire v1 still carries absolute angles.
      const response = 8, decay = Math.exp(-response * dt);
      for (const [angle, rate, maxRate] of [['yaw', 'yawRate', 1.15], ['pitch', 'pitchRate', 1]]) {
        const value = Number.isFinite(input[angle]) ? clamp(input[angle], -1, 1) : 0;
        const target = -value * maxRate, previous = this[rate];
        this[angle] += target * dt + (previous - target) * (1 - decay) / response;
        this[rate] = target + (previous - target) * decay;
      }
      this.yaw %= M.TAU;
      const pitch = clamp(this.pitch, -1.35, 1.35);
      if (pitch !== this.pitch) this.pitchRate = 0;
      this.pitch = pitch;
    }
    step(seconds, keys, obstacles = [], reduced = false, steering = null, throttle = null) {
      const dt = Number.isFinite(seconds) ? clamp(seconds, 0, .04) : 0;
      if (!dt) return;
      const axis = (positive, negative) => Number(keys.has(positive)) - Number(keys.has(negative));
      this.steer(dt, steering || {yaw:axis('arrowright', 'arrowleft'), pitch:axis('arrowup', 'arrowdown')});
      const b = flightBasis(this.yaw, this.pitch);
      const local = [axis('d', 'a'), axis('e', 'q'), -(throttle ?? axis('w', 's'))];
      const length = Math.max(1, Math.hypot(...local));
      const braking = keys.has(' ') || (reduced && !local.some(Boolean));
      this.boost = !reduced && !braking && keys.has('shift') && local[2] < 0;
      this.thrust = braking ? 0 : -local[2];
      this.bank = 0;
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
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  function tokenLanes(seed, anchors = []) {
    const points = anchors.slice(0, 64), visited = new Set([0]), lanes = [];
    while (visited.size < points.length) {
      let best = null;
      for (const i of visited) for (let j = 0; j < points.length; j++) {
        if (visited.has(j)) continue;
        const delta = points[j].position.map((v, k) => v - points[i].position[k]), distance2 = dot(delta, delta);
        if (!best || distance2 < best.distance2 || distance2 === best.distance2 && (i < best.i || i === best.i && j < best.j)) best = {i, j, distance2, delta};
      }
      if (!best) break;
      const {i, j, delta, distance2} = best, length = Math.sqrt(distance2), direction = length ? delta.map(v => v / length) : [1, 0, 0];
      const lateral = cross(direction, [0, 1, 0]);
      const sideways = Math.sqrt(dot(lateral, lateral)) <= 1e-6 ? cross(direction, [1, 0, 0]) : lateral;
      const normalLength = Math.sqrt(dot(sideways, sideways)), normal = sideways.map(v => v / normalLength);
      const binormal = cross(direction, normal);
      const sign = M.random('token-lane:v1:' + seed + ':' + points[i].id + ':' + points[j].id)() < .5 ? 1 : -1;
      lanes.push({a:points[i].position, b:points[j].position, normal, binormal, length, sign});
      visited.add(j);
    }
    return lanes;
  }
  function lanePoint(lane, t) {
    const bend = Math.sin(Math.PI * t) * Math.min(18, lane.length * .08) * lane.sign;
    return lane.a.map((v, i) => v + (lane.b[i] - v) * t + lane.normal[i] * bend);
  }
  function tokenNebula(seed, count = 2300, layout = {}) {
    const highway = layout.version === 2 && layout.layout_version === 1 && layout.anchors?.length >= 2;
    const random = M.random((highway ? 'token-highways:v6:' : 'token-ribbons:v5:') + seed), dust = [], clouds = [];
    const lanes = highway ? tokenLanes(seed, layout.anchors) : [], total = lanes.reduce((sum, lane) => sum + lane.length, 0);
    const laneCount = highway ? Math.floor(count * 4 / 5) : 0;
    const phases = Array.from({ length: 3 }, () => random() * M.TAU);
    const center = (t, band) => {
      const phase = phases[band];
      return [(t - .5) * 940, Math.sin(t * 8 + phase) * (35 + band * 30) + (band - 1) * 44,
        Math.cos(t * 6 + phase) * 115 + (band - 1) * 160];
    };
    const scatter = () => (random() + random() + random() - 1.5);
    for (let i = 0; i < count; i++) {
      let p;
      if (i < laneCount) {
        let distance = (i + .5) / laneCount * total, lane = lanes[0];
        for (const candidate of lanes) {
          if (!candidate.length) continue;
          lane = candidate;
          if (distance <= candidate.length) break;
          distance -= candidate.length;
        }
        p = lanePoint(lane, lane.length ? clamp(distance / lane.length, 0, 1) : 0);
        const across = (random() - .5) * 3, around = (random() - .5) * 3;
        p = p.map((v, k) => v + lane.normal[k] * across + lane.binormal[k] * around);
      } else {
        const t = random(), band = i % 3; p = center(t, band);
        const width = 5 + 22 * (1 + Math.sin(t * 13 + phases[band]));
        for (let k = 0; k < 3; k++) p[k] += scatter() * width;
        if (i % 5 === 0) for (let k = 0; k < 3; k++) p[k] = (random() - .5) * (k === 1 ? 750 : 1500);
      }
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
    return { dust: new Float32Array(dust), clouds: new Float32Array(clouds), lanes };
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
  // Each single-axis stick owns its pointer, so both thumbs can steer together.
  class FlightStick {
    constructor(element, {enabled, engage, change, cancel}) {
      this.element = element; this.axis = element.dataset.flightAxis;
      this.enabled = enabled; this.engage = engage; this.change = change; this.cancel = cancel;
      this.pointerId = null; this.keyboard = new Set(); this.value = 0; this.display = 0; this.velocity = 0; this.feedback = 0;
      element.addEventListener('pointerdown', e => {
        if (!enabled() || e.button !== 0 || this.pointerId !== null) return;
        e.preventDefault(); engage(true);
        const rect = element.getBoundingClientRect(), vertical = this.axis === 'pitch';
        this.center = vertical ? rect.top + rect.height / 2 : rect.left + rect.width / 2;
        this.travel = Math.max(1, (vertical ? rect.height : rect.width) / 2 - 23);
        const coordinate = vertical ? e.clientY : e.clientX;
        this.grabOffset = e.target.closest?.('[data-stick-thumb]') ? coordinate - (this.center + this.display * this.travel) : 0;
        this.pointerId = e.pointerId; element.setPointerCapture(e.pointerId);
        this.move(e);
      });
      element.addEventListener('pointermove', e => { if (e.pointerId === this.pointerId) this.move(e); });
      element.addEventListener('pointerup', e => {
        if (e.pointerId !== this.pointerId) return;
        const id = this.pointerId; this.pointerId = null;
        this.value = 0; this.feedback = 0; change(); this.paint();
        if (element.hasPointerCapture(id)) element.releasePointerCapture(id);
      });
      for (const type of ['pointercancel', 'lostpointercapture']) element.addEventListener(type, e => {
        if (e.pointerId !== this.pointerId) return;
        this.reset(); cancel();
      });
      element.addEventListener('keydown', e => {
        if (!enabled() || e.ctrlKey || e.metaKey || e.altKey || e.isComposing) return;
        const key = e.key.toLowerCase(), valid = this.axis === 'pitch' ? ['arrowup', 'arrowdown'] : ['arrowleft', 'arrowright'];
        if (!valid.includes(key)) return;
        e.preventDefault(); engage(false); this.keyboard.add(key);
        this.value = this.keyValue(); change(); this.paint();
      });
      element.addEventListener('keyup', e => {
        if (!this.keyboard.delete(e.key.toLowerCase())) return;
        e.preventDefault(); this.value = this.keyValue(); change(); this.paint();
      });
      element.addEventListener('blur', () => { if (this.keyboard.size) { this.reset(); cancel(); } });
      this.paint();
    }
    get active() { return this.pointerId !== null || this.keyboard.size > 0; }
    keyValue() {
      return this.axis === 'pitch' ? Number(this.keyboard.has('arrowup')) - Number(this.keyboard.has('arrowdown'))
        : Number(this.keyboard.has('arrowright')) - Number(this.keyboard.has('arrowleft'));
    }
    move(e) {
      const coordinate = this.axis === 'pitch' ? e.clientY : e.clientX;
      this.value = clamp((coordinate - this.center - this.grabOffset) / this.travel, -1, 1);
      this.display = this.value; this.velocity = 0;
      this.paint(); this.change();
    }
    frame(seconds, keyboardValue = 0) {
      if (this.pointerId !== null) return;
      const target = this.keyboard.size ? this.value : keyboardValue, dt = clamp(seconds || 0, 0, .04);
      if (this.display === target && this.velocity === 0 && this.feedback === target) return;
      this.feedback = target;
      const error = this.display - target, rate = this.velocity + 18 * error, decay = Math.exp(-18 * dt);
      this.display = target + (error + rate * dt) * decay;
      this.velocity = (this.velocity - 18 * rate * dt) * decay;
      if (Math.abs(this.display - target) < .001 && Math.abs(this.velocity) < .001) { this.display = target; this.velocity = 0; }
      this.paint();
    }
    paint() {
      this.element.style.setProperty('--stick-position', String(clamp(this.display, -1, 1)));
      const percent = Math.round((this.active ? this.value : this.feedback) * 100);
      this.element.classList.toggle('held', this.active || percent !== 0);
      this.element.setAttribute('aria-valuenow', String(percent));
      const direction = this.axis === 'pitch' ? (percent > 0 ? '抬头' : '低头') : (percent > 0 ? '右转' : '左转');
      this.element.setAttribute('aria-valuetext', percent ? direction + ' ' + Math.abs(percent) + '%' : '回中');
      const output = this.element.parentElement.querySelector('[data-stick-value]');
      if (output) output.textContent = percent ? direction + ' ' + Math.abs(percent) + '%' : '回中';
    }
    reset() {
      const id = this.pointerId; this.pointerId = null; this.keyboard.clear(); this.value = 0; this.feedback = 0;
      this.display = 0; this.velocity = 0;
      if (id !== null && this.element.hasPointerCapture(id)) this.element.releasePointerCapture(id);
      this.paint();
    }
  }
  class FlightThrottle {
    constructor(element, {enabled, engage, change, cancel, retainsFocus = () => false}) {
      this.element = element; this.enabled = enabled; this.engage = engage; this.change = change; this.cancel = cancel;
      this.value = 0; this.pointerId = null;
      element.addEventListener('pointerdown', e => {
        if (!enabled() || e.button !== 0 || this.pointerId !== null) return;
        e.preventDefault(); engage(true);
        const rect = element.getBoundingClientRect(); this.bottom = rect.bottom - 22; this.travel = Math.max(1, rect.height - 44);
        this.grabOffset = e.target.closest?.('[data-throttle-thumb]') ? e.clientY - (this.bottom - this.value * this.travel) : 0;
        this.pointerId = e.pointerId; element.setPointerCapture(e.pointerId); this.move(e);
      });
      element.addEventListener('pointermove', e => { if (e.pointerId === this.pointerId) this.move(e); });
      element.addEventListener('pointerup', e => {
        if (e.pointerId !== this.pointerId) return;
        const id = this.pointerId; this.pointerId = null;
        if (element.hasPointerCapture(id)) element.releasePointerCapture(id);
        this.paint();
      });
      for (const type of ['pointercancel', 'lostpointercapture']) element.addEventListener(type, e => {
        if (e.pointerId !== this.pointerId) return;
        this.reset(); cancel();
      });
      element.addEventListener('keydown', e => {
        if (!enabled() || e.ctrlKey || e.metaKey || e.altKey || e.isComposing) return;
        const key = e.key.toLowerCase(), steps = {arrowup:.01, arrowright:.01, arrowdown:-.01, arrowleft:-.01, pageup:.1, pagedown:-.1};
        if (!Object.hasOwn(steps,key) && !['home','end'].includes(key)) return;
        e.preventDefault(); engage(false); this.set(key === 'home' ? 0 : key === 'end' ? 1 : this.value + steps[key]);
      });
      element.addEventListener('blur', e => { if (!retainsFocus(e.relatedTarget)) { this.reset(); cancel(); } });
      this.paint();
    }
    get active() { return this.pointerId !== null; }
    set(value) { this.value = Math.round(clamp(value,0,1) * 100) / 100; this.paint(); this.change(); }
    move(e) { this.set((this.bottom - e.clientY + this.grabOffset) / this.travel); }
    paint() {
      const percent = Math.round(this.value * 100); this.element.style.setProperty('--throttle-position',String(this.value));
      this.element.classList.toggle('held',this.active); this.element.classList.toggle('powered',percent > 0);
      this.element.setAttribute('aria-valuenow',String(percent)); this.element.setAttribute('aria-valuetext',percent + '% 推力设定，松手保持');
      const output = this.element.parentElement.querySelector('[data-throttle-value]');
      if (output && output.textContent !== percent + '%') output.textContent = percent + '%';
    }
    reset() {
      const id = this.pointerId; this.pointerId = null; this.value = 0;
      if (id !== null && this.element.hasPointerCapture(id)) this.element.releasePointerCapture(id);
      this.paint();
    }
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
      this.labelSizes = new WeakMap();
      this.labelObserver = new ResizeObserver(entries => {
        for (const {target} of entries) this.labelSizes.delete(target);
        this.wake();
      });
      this.avatars = globalThis.MSGUniversePlanets?.AvatarLayer ? new globalThis.MSGUniversePlanets.AvatarLayer(labels, {wake:() => this.wake(), now:() => this.callbacks.now?.() ?? Date.now()}) : null;
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
      this.observerClient = callbacks.observer || null;
      this.observing = true;
      this.observeRegion = 0;
      this.wantFlight = false;
      this.remoteShips = new Map();
      this.shipLabels = new Map();
      this.shotEvents = new Map();
      this.combatEvents = new Map();
      this.combatSeen = new Map();
      this.localShot = null;
      this.localLaserAt = -Infinity;
      this.combatStatus = null;
      this.collectEvents = new Map();
      this.motionEvents = new Map();
      this.collectibles = null;
      this.prediction = null;
      this.gameActions = new Set();
      this.homeBody = null;
      this.homeMesh = null;
      this.flightPointers = new Map();
      this.flightControls = new Map();
      this.flightSticks = [];
      this.effects = globalThis.MSGFlightEffects ? new globalThis.MSGFlightEffects() : null;
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
        this.drawBuffers = new Map();
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
        this.attributes = [];
        for (const [name, size, offset] of [
          ["aPosition", 3, 0],
          ["aColor", 4, 12],
          ["aSize", 1, 28],
        ]) {
          const loc = gl.getAttribLocation(this.program, name);
          gl.enableVertexAttribArray(loc);
          this.attributes.push({loc, size, offset});
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
      if (this.settingPilot) return;
      this.settingPilot = true;
      try {
      if (active && (!this.available || this.lost)) return;
      if (active && this.network && !this.wantFlight) this.callbacks.prepareFlight?.();
      const changed = active !== this.wantFlight;
      this.wantFlight = active;
      if (changed) this.callbacks.pilot?.(active);
      if (active && this.network && (!this.network.connected || !this.network.self)) {
        if (!this.network.localSession) this.network.connect();
        if (!this.network.localSession) {
          document.getElementById('game-hud').hidden = false;
          if (this.flightButton) this.flightButton.textContent = '取消连接';
          return;
        }
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
        this.offlineMode = Boolean(this.network && !this.network.connected);
        this.network?.resume();
        this.updateFlight(0);
      } else {
        this.camera.target = [...this.flight.position];
        this.camera.distance = 90;
        this.flight = null;
        this.prediction = null;
        this.offlineMode = false;
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
      } finally { this.settingPilot = false; }
    }
    stopFlightInput(notifyNetwork = true) {
      this.keys.clear();
      this.pointers?.clear();
      this.start = null;
      const captured = [...(this.flightPointers?.keys() || [])]; this.flightPointers?.clear();
      for (const id of captured) if (this.canvas.hasPointerCapture?.(id)) this.canvas.releasePointerCapture(id);
      this.flightControls?.clear();
      for (const stick of this.flightSticks || []) stick.reset();
      this.flightThrottle?.reset();
      this.flight?.neutral();
      this.gameActions.clear();
      if (notifyNetwork) this.network?.suspend();
      for (const button of document.querySelectorAll?.('[data-flight-key]') || []) button.classList.remove('held');
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
          if (key === ' ' && !e.repeat) this.previewLaser();
          if (key === 'b') this.flightThrottle?.reset();
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
        if (this.flight && !this.isFlightInputTarget(e.target)) this.stopFlightInput();
      });
      canvas.addEventListener('focus', () => { if (this.flight && !document.hidden) this.network?.resume(); });
      canvas.addEventListener('pointerdown', e => {
        if (!this.flight || e.button !== 0 || this.flightPointers.size || !this.flightEnabled()) return;
        e.preventDefault(); canvas.focus({ preventScroll: true });
        this.network?.resume();
        this.flightPointers.set(e.pointerId, {x:e.clientX,y:e.clientY}); canvas.setPointerCapture(e.pointerId);
        this.flight.yawRate = 0; this.flight.pitchRate = 0; this.wake();
      });
      canvas.addEventListener('pointermove', e => {
        const previous = this.flightPointers.get(e.pointerId);
        if (!previous || !this.flight) return;
        if (!this.flightEnabled()) { this.stopFlightInput(); return; }
        e.preventDefault();
        this.flight.yaw = (this.flight.yaw - (e.clientX - previous.x) * .004) % M.TAU;
        this.flight.pitch = clamp(this.flight.pitch + (e.clientY - previous.y) * .004,-1.35,1.35);
        this.flight.yawRate = 0; this.flight.pitchRate = 0;
        this.flightPointers.set(e.pointerId,{x:e.clientX,y:e.clientY}); this.sendFlightInput(); this.wake();
      });
      for (const type of ['pointerup','pointercancel','lostpointercapture']) canvas.addEventListener(type,e => {
        if (!this.flightPointers.has(e.pointerId)) return;
        this.flightPointers.delete(e.pointerId);
        if (canvas.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId);
        if (this.flight) { this.flight.yawRate = 0; this.flight.pitchRate = 0; }
        if (type !== 'pointerup') this.stopFlightInput();
        else { this.sendFlightInput(); this.wake(); }
      });
      const controls = {
        enabled: () => this.flightEnabled(),
        engage: pointer => { if (pointer) canvas.focus({preventScroll:true}); this.network?.resume(); },
        change: () => { this.sendFlightInput(); this.wake(); },
        cancel: () => this.stopFlightInput(),
        retainsFocus: target => this.isFlightInputTarget(target),
      };
      this.flightSticks = [...document.querySelectorAll('[data-flight-axis]')].map(element => new FlightStick(element, {
        ...controls,
      }));
      const throttle = document.querySelector('[data-flight-throttle]');
      this.flightThrottle = throttle ? new FlightThrottle(throttle,controls) : null;
      document.querySelector('[data-throttle-zero]')?.addEventListener('click', () => {
        this.flightThrottle?.reset(); this.sendFlightInput(); this.wake();
      });
      for (const button of document.querySelectorAll('[data-flight-key]')) {
        const key = button.dataset.flightKey;
        const press = (id) => {
          if (!this.flightControls.has(id)) this.network?.resume();
          this.flightControls.set(id, key); button.classList.add('held'); this.wake();
          if (key === 'b') this.flightThrottle?.reset();
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
          if (!this.flightEnabled()) return;
          this.canvas.focus({preventScroll:true}); this.network.resume();
          this.queueGameAction(action); this.sendFlightInput();
        });
      }
    }
    queueGameAction(action) {
      if (!this.flight || this.network?.self?.hp <= 0) return;
      if (action === 'laser') this.previewLaser();
      if (!this.network?.connected) return;
      this.gameActions.add(action); this.wake();
    }
    combatMessage(text, kind = 'ready', until = this.network.serverNow + 1300) {
      this.combatStatus = {text, kind, until};
      this.updateCombatHud(); this.wake();
    }
    actionFuel(action) {
      return this.network?.limits?.[action + '_fuel'] ?? ({laser:1, shield:12, dash:18}[action] ?? 0);
    }
    previewLaser(notify = true) {
      const ship = this.network?.connected && this.network.self, now = this.network?.serverNow ?? this.callbacks.now?.() ?? Date.now();
      if (!this.flight || this.network?.self?.hp <= 0 || (this.network && !ship && !this.network.localSession)) return false;
      if (ship && ship.fuel < this.actionFuel('laser')) {
        if (notify) this.combatMessage('燃料不足 · 靠近字符补充', 'empty');
        return false;
      }
      const cooldown = (this.network?.limits?.laser_cooldown_ms ?? 400);
      const ready = Math.max(ship?.laser_ready_ms ?? 0, (this.localLaserAt ?? -Infinity) + cooldown);
      if (now < ready) {
        if (notify) this.combatMessage('激光冷却 ' + ((ready - now) / 1000).toFixed(1) + 's', 'cooldown', ready);
        return false;
      }
      const b = flightBasis(this.flight.yaw, this.flight.pitch);
      const position = this.flight.position.map((v, i) => v - b.eye[i] * 3.2);
      const offeredRange = this.network?.limits?.laser_range;
      const range = Number.isFinite(offeredRange) && offeredRange >= 1 && offeredRange <= 1000 ? offeredRange : this.network?.connected ? 60 : 180;
      this.localShot = {position, end:this.flight.position.map((v, i) => v - b.eye[i] * range), at_ms:now, intent:true};
      this.localLaserAt = now;
      this.effects?.fire({position:this.flight.position, yaw:this.flight.yaw, pitch:this.flight.pitch, now, range, reduced:this.reduced.matches});
      this.combatMessage(ship ? '开火请求' : '本地开火 · 未确认命中', 'intent', now + 700);
      return true;
    }
    updateCombatHud() {
      const now = this.network?.serverNow ?? 0;
      const active = this.flight && this.combatStatus?.until > now;
      const status = document.getElementById('game-combat');
      if (status) {
        const text = active ? this.combatStatus.text : '';
        if (status.textContent !== text) status.textContent = text;
        status.dataset.state = active ? this.combatStatus.kind : 'ready';
        status.setAttribute('aria-live', active && ['hit', 'kill', 'damaged', 'destroyed', 'empty'].includes(this.combatStatus.kind) ? 'polite' : 'off');
      }
      const reticle = document.getElementById('pilot-reticle');
      if (reticle) reticle.dataset.combat = active ? this.combatStatus.kind : 'ready';
    }
    receiveCombatEvents(events, now) {
      this.combatEvents ??= new Map(); this.combatSeen ??= new Map();
      for (const [id, at] of this.combatSeen) if (now - at > 8500) this.combatSeen.delete(id);
      for (const [id, event] of this.combatEvents) if (now - event.at_ms >= 900) this.combatEvents.delete(id);
      for (const event of events) {
        const age = now - event.at_ms;
        if (!['laser', 'hit', 'death'].includes(event.type) || age < 0 || age >= 900 ||
            event.at_ms < (this.combatSince ?? 0) ||
            this.combatSeen.has(event.id)) continue;
        this.combatSeen.set(event.id, event.at_ms);
        if (this.combatSeen.size > 512) this.combatSeen.delete(this.combatSeen.keys().next().value);
        const own = this.network.self?.id;
        this.effects?.confirmed(event, own, now, {reduced:this.reduced.matches});
        if (event.type === 'laser') {
          if (event.position && event.end && age < 400) this.shotEvents.set(event.id, event);
          if (this.shotEvents.size > 48) this.shotEvents.delete(this.shotEvents.keys().next().value);
          if (event.player_id === own) {
            this.localShot = null;
            this.combatMessage('已发射', 'fired', event.at_ms + 700);
          }
        } else {
          if (event.position) this.combatEvents.set(event.id, event);
          if (this.combatEvents.size > 48) this.combatEvents.delete(this.combatEvents.keys().next().value);
          if (event.player_id === own) this.combatMessage(event.type === 'death' ? '飞船被击毁' : '受到攻击', event.type === 'death' ? 'destroyed' : 'damaged', event.at_ms + 1400);
          else if (event.target_id === own) this.combatMessage(event.type === 'death' ? '击毁确认' : '命中确认', event.type === 'death' ? 'kill' : 'hit', event.at_ms + 1400);
        }
      }
      const feedback = this.effects?.sample(now).feedback;
      if (feedback) this.combatMessage(feedback.text, feedback.kind, feedback.until);
    }
    flightIntent(controlled = true) {
      const keys = new Set([...this.keys, ...this.flightControls.values()]);
      const axis = (positive, negative) => controlled ? Number(keys.has(positive)) - Number(keys.has(negative)) : 0;
      const throttle = keys.has('w') || keys.has('s') ? axis('w','s') : controlled ? this.flightThrottle?.value || 0 : 0;
      return { throttle:controlled && !keys.has('b') ? throttle : 0, strafe:axis('d', 'a'), lift:axis('e', 'q'),
        yaw:-this.flight.yaw, pitch:-this.flight.pitch, brake:controlled && keys.has('b') };
    }
    flightEnabled() {
      return Boolean(this.flight && !document.hidden && (!this.network || (this.network.connected || this.network.localSession) && !(this.network.self?.hp <= 0)) && !document.querySelector('dialog[open]'));
    }
    isFlightInputTarget(target) {
      return target === this.canvas || Boolean(target?.closest?.('[data-flight-axis], [data-flight-throttle], [data-throttle-zero], [data-flight-key], [data-game-action]'));
    }
    flightControlled() {
      return !document.hidden && !document.querySelector?.('dialog[open]') &&
        (this.isFlightInputTarget(document.activeElement) || this.flightControls.size > 0 || this.flightSticks?.some(stick => stick.active) ||
          this.flightThrottle?.active || document.activeElement?.closest?.('[data-flight-throttle], [data-throttle-zero]'));
    }
    flightSteering(controlled = true) {
      const keys = new Set([...this.keys, ...this.flightControls.values()]);
      const axis = (positive, negative) => Number(keys.has(positive)) - Number(keys.has(negative));
      const value = name => (this.flightSticks || []).filter(stick => stick.axis === name).reduce((sum, stick) => sum + stick.value, 0);
      return controlled ? {yaw:clamp(axis('arrowright', 'arrowleft') + value('yaw'), -1, 1),
        pitch:clamp(axis('arrowup', 'arrowdown') + value('pitch'), -1, 1)} : {yaw:0, pitch:0};
    }
    sendFlightInput() {
      if (!this.flight || this.network?.self?.hp <= 0) return;
      const controlled = this.flightControlled();
      const keys = new Set([...this.keys, ...this.flightControls.values()]);
      const actions = [...this.gameActions];
      if (keys.has(' ')) actions.push('laser');
      if (controlled && actions.includes('laser')) this.previewLaser(false);
      if (!this.network?.connected) { this.gameActions.clear(); return; }
      this.network.setInput({...this.flightIntent(controlled), actions:controlled ? [...new Set(actions)] : []});
      this.gameActions.clear();
    }
    syncCollectibles(field) {
      if (!field) return;
      const geometryKey = field.seed ? field.seed + ':' + (field.layout_version ?? 0) + ':' + JSON.stringify(field.anchors ?? []) : this.fieldKey;
      const changed = field.seed && geometryKey !== this.fieldKey;
      if (changed) {
        this.sceneSeed = field.seed;
        this.fieldKey = geometryKey;
        const scenery = tokenNebula(field.seed, field.count, field);
        this.dust = scenery.dust; this.clouds = scenery.clouds;
        this.tokenLanes = scenery.lanes;
        this.collectEvents?.clear(); this.collectSince = this.network?.serverNow ?? 0;
        for (const token of this.tokenNodes.values()) token.remove();
        this.tokenNodes.clear();
      }
      if (!changed && this.collectibles?.revision === field.revision) return;
      this.collectibles = { ...this.collectibles, ...field };
      this.rebuildRenderDust();
    }
    rebuildRenderDust() {
      if (!this.dust) return;
      // Keep stable IDs in the full dust array; rebuild the compact GPU stream
      // only on a server bitmap revision, never 2,300 DOM nodes per frame.
      const dust = [];
      for (let i = 0; i < this.dust.length / 8; i++) if (!this.glyphTaken(i))
        dust.push(...this.dust.subarray(i * 8, i * 8 + 8));
      this.renderDust = new Float32Array(dust);
    }
    glyphTaken(id) { return Boolean(this.collectibles?.mask[id >> 3] & (1 << (id & 7))) || Boolean(this.offlineMode && this.offlineTaken?.has(id)); }
    offlineCollect(dt) {
      this.offlineCollectClock = (this.offlineCollectClock || 0) + dt;
      if (this.offlineCollectClock < .1 || !this.dust || !this.prediction) return;
      this.offlineCollectClock = 0;
      this.offlineTaken ??= new Set();
      const p = this.flight.position, radius = this.collectibles?.radius ?? 8;
      let count = 0;
      for (let id = 0; id < this.dust.length / 8 && count < 32; id++) {
        if (this.glyphTaken(id)) continue;
        const at = id * 8;
        if ((this.dust[at]-p[0]) ** 2 + (this.dust[at+1]-p[1]) ** 2 + (this.dust[at+2]-p[2]) ** 2 > radius ** 2) continue;
        this.offlineTaken.add(id); count++;
      }
      if (count) {
        this.prediction.state.fuel = Math.min(100, this.prediction.state.fuel + count * (this.collectibles?.fuel ?? 4));
        this.offlinePickup = {count, until:this.network.serverNow + 800};
        this.rebuildRenderDust();
      }
    }
    receiveFlightHello(hello) {
      this.authorityAt = this.network.serverNow;
      this.offlineTaken?.clear(); this.offlinePickup = null;
      this.effects?.clear({keepSeen:this.combatSelfId === hello.self.id});
      if (this.combatSelfId !== hello.self.id) {
        this.combatSeen?.clear(); this.combatSince = hello.server_time_ms ?? this.network.serverNow;
        this.combatSelfId = hello.self.id;
      }
      this.localShot = null; this.combatEvents?.clear(); this.shotEvents.clear(); this.combatStatus = null;
      this.syncCollectibles(hello.collectibles);
      this.homeBody = hello.self.home_body?.private ? hello.self.home_body : null;
      this.homeMesh = this.homeBody ? planetMesh(this.homeBody.id) : null;
      if (this.flight) {
        const resumeControls = this.offlineMode && this.prediction?.state.region === hello.self.region &&
          (this.prediction.state.hp <= 0) === (hello.self.hp <= 0);
        if (!resumeControls) this.stopFlightInput(false);
        if (this.offlineMode && this.prediction && !this.reduced.matches) {
          this.prediction.position = [...this.flight.position];
          this.prediction.reconcile(hello.self, false, 0, null, this.network.limits, this.network.serverNow, this.network.gravity, true);
          this.flight.position = [...this.prediction.position];
        } else { this.prediction = new FlightPrediction(hello.self); this.flight.position = [...hello.self.position]; }
        this.flight.velocity = [...hello.self.velocity];
        if (!resumeControls) { this.flight.yaw = -hello.self.yaw; this.flight.pitch = -hello.self.pitch; this.flight.neutral(); }
      }
      this.offlineMode = false;
      this.rebuildRenderDust();
      if (this.wantFlight) this.setPilot(true);
    }
    receiveFlightSnapshot(snapshot) {
      const now = this.network.serverNow, time = snapshot.state_time_ms ?? snapshot.server_time_ms ?? now;
      this.authorityAt = now;
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
        const correction = reset.has(ship.id) || ship.region !== this.prediction.state.region || (ship.hp <= 0) !== (this.prediction.state.hp <= 0);
        if (correction) {
          this.effects?.clear({keepSeen:true});
          this.stopFlightInput(false);
          this.flight.yaw = -ship.yaw; this.flight.pitch = -ship.pitch;
          // Clear both continuous input and pending one-shot actions, even at HP 0.
          // clearInput sends a neutral frame without suspending/status recursion.
          if (this.network.clearInput) this.network.clearInput();
          else this.network.setInput({...this.flightIntent(false), actions:[]});
        }
        const controlled = this.flightControlled() && ship.hp > 0;
        this.prediction.reconcile(ship, correction, (now - time) / 1000, this.flightIntent(controlled), this.network.limits, now, this.network.gravity);
        this.flight.position = [...this.prediction.position];
      }
      this.syncCollectibles(snapshot.collectibles);
      for (const event of snapshot.events) if (event.type === 'collect' && event.at_ms >= (this.collectSince ?? 0) && now - event.at_ms >= 0 && now - event.at_ms < 800 && !this.collectEvents.has(event.id))
        this.collectEvents.set(event.id, event);
      for (const [id, event] of this.collectEvents) if (now - event.at_ms >= 800) this.collectEvents.delete(id);
      this.receiveCombatEvents(snapshot.events, now);
      for (const [id, event] of this.shotEvents) if (now - event.at_ms > 400) this.shotEvents.delete(id);
      this.wake();
    }
    receiveObserverSnapshot(snapshot) {
      if (this.wantFlight || !this.observing || !this.observerClient?.connected) return;
      const now = this.observerClient.serverNow, time = snapshot.state_time_ms ?? snapshot.server_time_ms ?? now;
      const players = snapshot.players.filter(ship => ship.region === this.observeRegion);
      const live = new Set(players.map(ship => ship.id));
      for (const [id] of this.remoteShips) if (!live.has(id)) this.remoteShips.delete(id);
      for (const ship of players) {
        const previous = this.remoteShips.get(ship.id), track = previous?.track ?? new MotionTrack();
        const reset = previous && (previous.state.region !== ship.region || (previous.state.hp <= 0) !== (ship.hp <= 0));
        track.push(ship, time, reset);
        this.remoteShips.set(ship.id, {state:ship, track, position:previous && !reset ? previous.position : [...ship.position], yaw:ship.yaw, pitch:ship.pitch});
      }
      this.syncCollectibles(snapshot.collectibles);
      this.wake();
    }
    sceneConnection() {
      return this.flight && this.network?.connected ? this.network : !this.wantFlight && this.observing && this.observerClient?.connected ? this.observerClient : null;
    }
    clearRemoteShips() {
      this.remoteShips.clear(); this.shotEvents.clear();
      this.collectEvents?.clear(); this.motionEvents?.clear();
      this.combatEvents?.clear();
      this.localShot = null; this.localLaserAt = -Infinity; this.combatStatus = null;
      this.effects?.clear({keepSeen:true});
      this.updateCombatHud();
      for (const label of this.shipLabels.values()) label.remove();
      this.shipLabels.clear(); this.wake();
    }
    updateFlight(dt) {
      const flight = this.flight, now = this.callbacks.now?.() ?? Date.now();
      const bodies = this.graph.nodes.filter(node => node.kind === 'user' || node.kind === 'private')
        .map(node => ({ node, position: node.position, radius: this.planetRadius(node, now) * 1.07 }));
      const keys = new Set([...this.keys, ...this.flightControls.values()]);
      const controlled = this.flightControlled();
      const steering = this.flightSteering(controlled);
      for (const stick of this.flightSticks || []) stick.frame(dt, steering[stick.axis]);
      if (this.network) {
        const ship = this.network.connected && this.network.self;
        if (ship) {
          if (controlled && ship.hp > 0) {
            flight.steer(dt, steering);
            this.sendFlightInput();
          } else flight.neutral();
          this.prediction ??= new FlightPrediction(ship);
          if (!dt) this.prediction.reset(ship);
          const age = Math.max(0, this.network.serverNow - (this.authorityAt ?? this.network.serverNow));
          const advance = Math.min(dt, Math.max(0, .25 - age / 1000 + dt));
          flight.position = [...this.prediction.step(advance, this.flightIntent(controlled), this.network.limits, this.network.serverNow, this.reduced.matches, this.network.gravity)];
          flight.velocity = [...this.prediction.state.velocity];
          flight.thrust = controlled && ship.hp > 0 ? this.flightIntent(controlled).throttle : 0;
          flight.boost = false;
          flight.bank = 0;
          this.offlineMode = false;
        } else if (this.network.localSession && !(this.network.self?.hp <= 0)) {
          this.offlineMode = true;
          this.prediction ??= new FlightPrediction(this.network.self ?? {position:[...flight.position], velocity:[...flight.velocity], hp:100, fuel:100, region:0});
          if (controlled) flight.steer(dt, steering); else flight.neutral();
          this.sendFlightInput();
          flight.position = [...this.prediction.step(dt, this.flightIntent(controlled), this.network.limits, this.network.serverNow, this.reduced.matches, this.network.gravity)];
          flight.velocity = [...this.prediction.state.velocity];
          flight.thrust = controlled ? this.flightIntent(controlled).throttle : 0;
          this.offlineCollect(dt);
        } else { flight.neutral(); if (this.flightThrottle?.value || this.flightThrottle?.active) this.flightThrottle.reset(); }
        for (const remote of this.remoteShips.values()) {
          const sample = remote.track?.sample(this.network.serverNow - 240);
          if (sample) { remote.position = sample.position; remote.yaw = sample.yaw; remote.pitch = sample.pitch; }
        }
      } else flight.step(dt, controlled ? keys : new Set(), bodies, this.reduced.matches, steering, this.flightIntent(controlled).throttle);
      const smoothing = this.paused || this.reduced.matches || !dt ? 1 : 1 - Math.exp(-dt * 9);
      // Interpolate angles across the wrap boundary, never via a full rotation.
      const angle = Math.atan2(Math.sin(flight.yaw - this.camera.yaw), Math.cos(flight.yaw - this.camera.yaw));
      this.camera.yaw += angle * smoothing;
      this.camera.pitch = mix(this.camera.pitch, flight.pitch, smoothing);
      const basis = this.basis();
      this.camera.target = flight.position.map((v, i) => v - basis.eye[i] * 8 + basis.up[i] * 4);
      this.camera.distance = this.reduced.matches ? 34 : mix(this.camera.distance, flight.boost ? 38 : 34, smoothing);
      const kick = this.effects?.sample(this.network?.serverNow ?? now).kick ?? 0;
      if (kick && !this.reduced.matches) this.camera.distance += kick * .32;
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
      if (speed) speed.textContent = this.flight && (!this.network || this.network.connected || this.network.localSession) ? Math.round(this.flight.speed).toString().padStart(3, '0') : '—';
      if (!approach) return;
      const node = !clear && this.flight && this.nearby;
      approach.disabled = !node;
      approach.textContent = node ? '查看 ' + M.handle(node.name || node.title || 'Signal') + ' ↗' : '靠近星球查看';
      if (this.network) this.updateGameHud();
    }
    updateGameHud() {
      const local = !this.network.connected && this.network.localSession;
      const ship = this.network.connected ? this.network.self : local ? this.prediction?.state : null, now = this.network.serverNow;
      for (const field of ['hp', 'fuel']) {
        const output = document.getElementById('game-' + field), meter = document.getElementById('game-' + field + '-meter');
        if (output) output.textContent = ship ? Math.round(ship[field]).toString() : '—';
        if (meter) { meter.value = ship ? ship[field] : 0; meter.closest('.game-vital').dataset.critical = String(ship && ship[field] < 25); }
      }
      const player = document.getElementById('game-player');
      if (player) player.textContent = local ? '本地驾驶' : ship ? ship.handle : '—';
      const score = document.getElementById('game-score'); if (score) score.textContent = ship && Number.isFinite(ship.score) ? String(ship.score) : '—';
      const collected = document.getElementById('game-collected');
      if (collected) collected.textContent = local ? String(this.offlineTaken?.size ?? 0) + ' 本地' : ship && Number.isSafeInteger(ship.collected) ? String(ship.collected) : '—';
      const pickup = document.getElementById('game-pickup');
      if (pickup) {
        const latest = [...(this.collectEvents?.values() ?? [])].filter(event => event.player_id === ship?.id).at(-1);
        const active = latest && now - latest.at_ms < 800;
        const message = local && this.offlinePickup?.until > now ? '本地吸收 +' + this.offlinePickup.count : active ? '吸收 +' + latest.glyph_ids.length + (latest.fuel_added > 0 ? ' · 燃料 +' + latest.fuel_added.toFixed(1) : '') : '';
        if (pickup.textContent !== message) pickup.textContent = message;
      }
      const respawn = document.getElementById('game-respawn');
      if (respawn) { respawn.hidden = !ship || ship.hp > 0; respawn.textContent = ship && ship.hp <= 0 ? '重生倒计时 ' + Math.max(0, (ship.respawn_at_ms - now) / 1000).toFixed(1) + 's' : ''; }
      const region = document.getElementById('game-region');
      if (region) region.textContent = ship ? '区域 ' + String(ship.region).padStart(2, '0') + ' / 地图' : '区域地图';
      this.updateCombatHud();
      for (const button of document.querySelectorAll('[data-game-action]')) {
        const action = button.dataset.gameAction === 'fire' ? 'laser' : button.dataset.gameAction;
        const remaining = ship ? Math.max(0, (ship[action + '_ready_ms'] - now) / 1000) : Infinity;
        const lowFuel = ship && ship.fuel < this.actionFuel(action);
        button.disabled = !ship || ship.hp <= 0 || (local ? action !== 'laser' : remaining > 0 || lowFuel);
        button.dataset.active = String(action === 'shield' && ship && ship.shield_until_ms > now);
        button.querySelector('.game-cooldown').textContent = local ? action === 'laser' ? '本地开火' : '需要连接' : ship ? ship.hp <= 0 ? '等待重生' : lowFuel ? '燃料不足' : remaining > 0 ? remaining.toFixed(1) + 's' : '就绪' : '—';
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
      this.styleCache = new Map();
      this.nodeById = new Map(graph.nodes.map(node => [node.id,node]));
      this.nearby = null;
      if (this.flight) this.updateFlightHud(true);
      this.satellites = graph.nodes.filter(n => n.orbitCenter);
      this.signals = graph.nodes.filter(n => n.kind !== 'user' && n.kind !== 'private');
      this.reindex();
      this.wake();
    }
    style(node, now = this.callbacks.now?.() ?? Date.now()) {
      const second = Math.floor(now / 1000);
      if (!this.styleCache || this.styleCacheSecond !== second) { this.styleCache = new Map(); this.styleCacheSecond = second; }
      if (this.styleCache.has(node)) return this.styleCache.get(node);
      const following = (node.relations?.following || []).map(id => this.nodeById?.get(id)).filter(Boolean);
      const style = M.appearance(node, now, {following}), stellar = style.stellar;
      if (!style.root && (node.kind === 'user' || node.kind === 'private')) {
        const warmth = stellar.known ? stellar.heat : 0, warm = [1,.76,.42];
        style.color = style.color.map((value,i) => mix(value,warm[i],warmth * .8));
        style.light = stellar.brightness;
      }
      this.styleCache.set(node,style); return style;
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
    planetRadius(node, now) {
      if (node.id === 'u_root') return 5.4;
      const facts = node.star || {}, certificate = facts.certificate || {};
      this.radiusFacts ??= new WeakMap();
      let saved = this.radiusFacts.get(node);
      if (!saved || saved.checked !== facts.checked_at || saved.expires !== certificate.expires_at) {
        saved = {checked:facts.checked_at, expires:certificate.expires_at,
          checkedAt:typeof facts.checked_at === 'string' ? Date.parse(facts.checked_at) : NaN,
          expiresAt:typeof certificate.expires_at === 'string' ? Date.parse(certificate.expires_at) : NaN};
        this.radiusFacts.set(node,saved);
      }
      // Only timestamp parsing is cached. Freshness and certificate boundaries
      // are tested against the current time, including updates to the facts.
      const fresh = Number.isFinite(saved.checkedAt) && saved.checkedAt <= now + 1000 && now - saved.checkedAt < 90000;
      return fresh && certificate.state === 'valid' && saved.expiresAt > now ? 2.05 : 1.5;
    }
    basis() {
      const c = this.camera;
      if (this.cameraBasis?.yaw === c.yaw && this.cameraBasis.pitch === c.pitch) return this.cameraBasis.value;
      const
        sy = Math.sin(c.yaw),
        cy = Math.cos(c.yaw),
        sp = Math.sin(c.pitch),
        cp = Math.cos(c.pitch);
      const value = {right:[cy,0,-sy], up:[-sp*sy,cp,-sp*cy], eye:[cp*sy,sp,cp*cy]};
      this.cameraBasis = {yaw:c.yaw,pitch:c.pitch,value};
      return value;
    }
    verticalShift() {
      // Leave the selected star above the mobile inspector, not underneath it.
      return !this.flight && this.width < 600 && this.focusId ? .5 : 0;
    }
    project(p, basis = this.basis()) {
      return this.projectVertex(p, 0, basis);
    }
    projectVertex(vertices, offset, b = this.basis()) {
      const target = this.camera.target, x = vertices[offset] - target[0],
        y = vertices[offset + 1] - target[1], z = vertices[offset + 2] - target[2];
      const depth = this.camera.distance - (x * b.eye[0] + y * b.eye[1] + z * b.eye[2]);
      if (depth < 1) return null;
      const scale = (1.72 * this.height * 0.5) / depth;
      return {
        x: this.width / 2 + (x * b.right[0] + y * b.right[1] + z * b.right[2]) * scale,
        y: this.height * (.5 - this.verticalShift() / 2) - (x * b.up[0] + y * b.up[1] + z * b.up[2]) * scale,
        depth,
        scale,
      };
    }
    sphereVisible(position, radius, b = this.basis()) {
      const c = this.camera, x = position[0] - c.target[0], y = position[1] - c.target[1], z = position[2] - c.target[2];
      const depth = c.distance - (x * b.eye[0] + y * b.eye[1] + z * b.eye[2]);
      if (depth + radius < 1) return false;
      const horizontal = x * b.right[0] + y * b.right[1] + z * b.right[2];
      const vertical = x * b.up[0] + y * b.up[1] + z * b.up[2];
      const side = this.width / (this.height * 1.72), shift = this.verticalShift();
      const top = (.5 - shift / 2) / .86, bottom = (.5 + shift / 2) / .86;
      // Sphere/plane distances stay conservative at the perspective edges;
      // a projected center plus a naive circular margin clips an oblique limb.
      return Math.abs(horizontal) - depth * side <= radius * Math.hypot(1, side) &&
        vertical - depth * top <= radius * Math.hypot(1, top) &&
        -vertical - depth * bottom <= radius * Math.hypot(1, bottom);
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
      c.addEventListener("blur", e => {
        if (this.isFlightInputTarget(e.relatedTarget)) { this.keys.clear(); this.sendFlightInput(); }
        else this.stopFlightInput();
      });
      document.addEventListener("visibilitychange", () => {
        this.stopFlightInput();
        this.keys.clear();
        this.pointers.clear();
        this.last = 0;
        if (document.hidden) {
          this.avatars?.clear();
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
      if (!this.flight && this.sceneConnection()) {
        for (const remote of this.remoteShips.values()) {
          const sample = remote.track?.sample(this.observerClient.serverNow - 240);
          if (sample) { remote.position = sample.position; remote.yaw = sample.yaw; remote.pitch = sample.pitch; }
        }
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
      this.drawVertices('clouds', this.clouds, gl.POINTS, 3, true);
      this.drawVertices('dust', this.renderDust ?? this.dust, gl.POINTS, 2, true);
      const geometry = this.geometry();
      gl.enable(gl.DEPTH_TEST);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      this.drawVertices('solids', geometry.solids, gl.TRIANGLES);
      gl.disable(gl.DEPTH_TEST);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
      this.drawVertices('triangles', geometry.triangles, gl.TRIANGLES);
      this.drawVertices('lines', geometry.lines, gl.LINES);
      this.drawVertices('points', geometry.points, gl.POINTS);
      this.finish();
    }
    drawVertices(key, vertices, mode, pointKind = 0, immutable = false) {
      if (!vertices.length) return;
      const gl = this.gl;
      this.drawBuffers ??= new Map();
      let slot = this.drawBuffers.get(key);
      if (!slot) { slot = {buffer:gl.createBuffer(), capacity:0, source:null, data:null}; this.drawBuffers.set(key, slot); }
      gl.bindBuffer(gl.ARRAY_BUFFER, slot.buffer);
      for (const {loc, size, offset} of this.attributes)
        gl.vertexAttribPointer(loc, size, gl.FLOAT, false, 32, offset);
      if (immutable) {
        // Collectible bitmap updates replace the typed array. Until then this
        // exact frozen field is already resident on the GPU, even while flying.
        if (slot.source !== vertices) {
          gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.STATIC_DRAW); slot.source = vertices;
        }
      } else {
        if (vertices.length > slot.capacity) {
          slot.capacity = Math.max(256, 2 ** Math.ceil(Math.log2(vertices.length)));
          slot.data = new Float32Array(slot.capacity);
          gl.bufferData(gl.ARRAY_BUFFER, slot.data.byteLength, gl.DYNAMIC_DRAW);
        }
        slot.data.set(vertices);
        gl.bufferSubData(gl.ARRAY_BUFFER, 0, slot.data.subarray(0, vertices.length));
      }
      gl.uniform1f(this.uniforms.uPoints, pointKind || (mode === gl.POINTS ? 1 : 0));
      gl.drawArrays(mode, 0, vertices.length / 8);
    }
    finish() {
      this.updateLabels();
      this.updateShipLabels();
      this.updateTokens();
      this.callbacks.camera?.(this.camera.target);
      this.dirty = false;
      if (!this.paused || this.destination || this.keys.size || this.flightControls.size || this.flightThrottle?.value > 0 ||
          (this.flight && (this.flight.speed > .01 || this.flight.trail.length || this.localShot ||
            this.shotEvents.size || this.combatEvents?.size || this.combatStatus?.until > this.network?.serverNow || this.effects?.sample(this.network?.serverNow ?? Date.now()).active)))
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
      if (this.flight && this.network?.connected) {
        // These guides use only the server's frozen public lane descriptor.
        // Limit nearby guide geometry; availability still comes from the bitmap.
        const nearby = (this.tokenLanes ?? []).map(lane => {
          const direction = lane.b.map((v, i) => v - lane.a[i]), length2 = dot(direction, direction);
          const offset = this.flight.position.map((v, i) => v - lane.a[i]);
          const t = length2 ? clamp(dot(offset, direction) / length2, 0, 1) : 0;
          return {lane, distance:Math.hypot(...lanePoint(lane, t).map((v, i) => v - this.flight.position[i]))};
        }).filter(item => item.distance < 120).sort((a, b) => a.distance - b.distance).slice(0, this.width < 600 ? 6 : 12);
        for (const {lane} of nearby) {
          let previous = lanePoint(lane, 0);
          for (let i = 1; i <= 24; i++) {
            const current = lanePoint(lane, i / 24);
            line(previous, current, [.7, .7, .69], .13); previous = current;
          }
        }
      }
      let interactionEdges = 0;
      for (const [a, z, type, meta] of this.view.links) {
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
        if (related) for (const direction of meta?.directions || []) {
          const forward = direction.source === a.id, t = forward ? .64 : .36;
          const at = t => a.position.map((v,i) => mix(v,z.position[i],t)+(i===1?Math.sin(t*Math.PI)*10:0));
          const tip = at(t), tail = at(t+(forward?-.035:.035));
          const color = direction.source === this.focusId ? [.98,.77,.42] : [.55,.83,.90];
          for (const sign of [-1,1]) line(tip,tail.map((v,i)=>v+b.right[i]*sign*1.2),color,.85);
        }
        const weight = meta?.interactionWeight || 0;
        if (!type.startsWith('private') && weight > 0 && interactionEdges++ < (this.width < 600 ? 12 : 32)) {
          const strands = 1 + Math.ceil(weight * 3), color = [.87,.72,.49];
          for (let strand=0;strand<strands;strand++) {
            const side = (strand-(strands-1)/2)*(1.2+weight*1.5);
            let prior = a.position;
            for(let j=1;j<=24;j++) {
              const t=j/24, envelope=Math.sin(t*Math.PI), phase=this.paused||this.reduced.matches?0:this.clock*.9;
              const p=a.position.map((v,i)=>mix(v,z.position[i],t)+(i===1?envelope*10:0)+b.right[i]*side*envelope+b.up[i]*Math.sin(t*M.TAU+phase+strand)*envelope*weight);
              line(prior,p,color,.12+weight*.22); prior=p;
            }
          }
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
        const p = node.position, style = this.style(node, now);
        const selected = node.id === this.focusId, hovered = node.id === this.hoverId;
        const isStar = node.kind === 'user' || node.kind === 'private';
        const col = isStar ? style.color : palette[node.kind] || palette.post;
        const light = isStar ? style.light : .7;
        let traits = this.visualTraits.get(node);
        if (!traits) {
          const rng = M.random('shape:' + node.id);
          traits = { phase: rng() * M.TAU, speed: .025 + rng() * .085, tilt: .8 + rng() * .6 };
          this.visualTraits.set(node, traits);
        }
        const pulse = 1 + (this.paused ? 0 : Math.sin(this.clock * (1 + traits.speed * 8) + traits.phase) * style.pulse);
        points.push(...vertex(p, col, light * pulse * (isStar ? .38 : 1), style.root ? 25 : isStar ? 5.5 : 2.2));
        const projected = this.project(p, b);
        const selectionExtent = selected || hovered ? (style.root ? 17 : 6.7) * Math.SQRT2 : 0;
        // Balance arcs sit below the body; selection corners extend farther
        // than the solid planet. Cull the body separately with its exact size.
        let extent = Math.max(isStar ? style.radius * 6 : 2, selectionExtent);
        for (const descriptor of [node.post_ring, node.orbit]) {
          const center = descriptor?.center;
          const radius = Math.max(descriptor?.radius || 0, ...(descriptor?.ring_radii || [0]));
          const offset = center?.length === 3 ? Math.hypot(center[0] - p[0], center[1] - p[1], center[2] - p[2]) : 0;
          extent = Math.max(extent, offset + radius);
        }
        // Pinned selection/root nodes remain in the readable view. Their
        // offscreen decoration need not consume a detail slot or CPU geometry.
        if (!this.sphereVisible(p, extent, b)) continue;
        if (!selected && !hovered && !style.root && (!projected || projected.scale * style.radius < 3 || detailed >= (globalThis.MSGUniversePlanets ? this.software || this.width < 700 ? 12 : 24 : this.software ? 24 : 64))) continue;
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
          if (globalThis.MSGUniversePlanets?.appendSurface) {
            if (this.sphereVisible(p, style.radius * 1.008, b)) globalThis.MSGUniversePlanets.appendSurface(solids, {
            node, style, clock:this.clock, towardEye, sun, pixelRadius:(projected?.scale ?? 0) * style.radius,
            software:this.software, mobile:this.width < 700, wake:() => this.wake(),
            });
          }
          else for (const face of (traits.mesh ??= planetMesh(node.id))) {
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
          } else if (style.stellar?.stage === 'star') {
            ring(p,r*2.0,col,.28,{tilt:.35,sides:48});
            ring(p,r*2.7,col,.12,{tilt:-.65,sides:48,dashed:true});
            for (const axis of [b.right,b.up]) for (const sign of [-1,1])
              line(p.map((v,i)=>v+axis[i]*r*1.5*sign),p.map((v,i)=>v+axis[i]*r*3.0*sign),col,.24);
          } else if (style.stellar?.stage === 'satellite') {
            ring(p,r*1.8,[.58,.66,.73],.12+style.stellar.satelliteProgress*.15,{tilt:.6,sides:32,dashed:true});
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
      if (this.flight && (!this.network || (this.network.connected || this.network.localSession) && !(this.network.self?.hp <= 0))) this.shipGeometry({ lines, points, solids });
      const scene = this.sceneConnection();
      if (scene) {
        const serverNow = scene.serverNow;
        for (const {state:ship, position, yaw = ship.yaw, pitch = ship.pitch} of this.remoteShips.values()) {
          if (this.flight && ship.id === this.network.self?.id || ship.hp <= 0) continue;
          const flight = {position, yaw:-yaw, pitch:-pitch, bank:0, trail:[],
            thrust:Math.hypot(...ship.velocity) > 2 ? 1 : 0, boost:false};
          this.shipGeometry({lines, points, solids}, flight, ship.guest ? [.74, .77, .80] : [.8, .87, .84]);
          if (ship.shield_until_ms > serverNow) ring(position, 4.5, [.6, .87, .95], .6, {tilt:.4, sides:32});
        }
        const own = this.flight && this.network.self;
        if (own?.shield_until_ms > serverNow) ring(this.flight.position, 4.5, [.6, .87, .95], .6, {tilt:.4, sides:32});
      }
      if (this.flight) {
        if (this.effects) {
          const effect = this.effects.geometry(this.network?.serverNow ?? now);
          for (const [name, destination] of Object.entries({lines, points, triangles, solids})) destination.push(...effect[name]);
        } else this.combatGeometry({lines, points, triangles}, b);
      }
      return { lines, points, triangles, solids };
    }
    combatGeometry({lines, points, triangles}, basis) {
      const now = this.network?.serverNow ?? this.callbacks.now?.() ?? Date.now(), reduced = this.reduced.matches;
      const vertex = (p, alpha, size = 1) => [...p, .96, .96, .94, alpha, size];
      const line = (a, b, alpha) => lines.push(...vertex(a, alpha), ...vertex(b, alpha));
      const beam = event => {
        const lifetime = event.intent ? 250 : 400, age = now - event.at_ms;
        if (age < 0 || age >= lifetime) return;
        const alpha = (event.intent ? .6 : .95) * (1 - age / lifetime);
        const direction = unit(event.end.map((v, i) => v - event.position[i]));
        const length = Math.hypot(...event.end.map((v, i) => v - event.position[i]));
        const start = event.position.map((v, i) => v + direction[i] * Math.min(3.2, length / 4));
        const end = reduced ? event.end : event.position.map((v, i) => mix(v, event.end[i], clamp(.18 + age / 150, 0, 1)));
        const width = event.intent ? .09 : .16;
        const corners = [start, end].flatMap(p => [-1, 1].map(sign => p.map((v, i) => v + basis.right[i] * width * sign)));
        for (const index of [0, 1, 2, 1, 3, 2]) triangles.push(...vertex(corners[index], alpha));
        line(start, end, alpha);
        points.push(...vertex(start, alpha, age < 120 ? 2.6 : .7), ...vertex(end, alpha, 1.0));
        if (age < 130) for (const axis of [basis.right, basis.up]) {
          const radius = reduced ? .7 : 1.2 * (1 - age / 130);
          line(start.map((v, i) => v - axis[i] * radius), start.map((v, i) => v + axis[i] * radius), alpha);
        }
      };
      if (this.localShot) {
        if (now - this.localShot.at_ms >= 250) this.localShot = null;
        else beam(this.localShot);
      }
      for (const [id, event] of this.shotEvents) {
        if (now - event.at_ms >= 400) this.shotEvents.delete(id);
        else beam(event);
      }
      for (const [id, event] of this.combatEvents ?? []) {
        const lifetime = event.type === 'death' ? 900 : 550, age = now - event.at_ms;
        if (age < 0 || age >= lifetime) { this.combatEvents.delete(id); continue; }
        const progress = age / lifetime, death = event.type === 'death';
        const count = reduced ? 4 : death ? 16 : 8, radius = reduced ? 3 : (death ? 9 : 4) * (.2 + progress);
        const alpha = .9 * (1 - progress);
        for (let i = 0; i < count; i++) {
          const angle = i / count * M.TAU, direction = basis.right.map((v, k) => v * Math.cos(angle) + basis.up[k] * Math.sin(angle));
          const tip = event.position.map((v, k) => v + direction[k] * radius);
          const tail = event.position.map((v, k) => v + direction[k] * radius * .55);
          line(tail, tip, alpha); points.push(...vertex(tip, alpha, death ? .45 : .3));
          const size = death ? .24 : .14;
          const corners = [[-1,-1],[1,-1],[1,1],[-1,1]].map(([x,y]) => tip.map((v,k) => v + basis.right[k]*x*size + basis.up[k]*y*size));
          for (const index of [0,1,2,0,2,3]) triangles.push(...vertex(corners[index], alpha));
        }
        points.push(...vertex(event.position, alpha * .7, death ? 3.2 : 1.4));
      }
    }
    absorptionGlyphs() {
      if (!this.flight || !this.network?.connected || this.reduced.matches) return [];
      const result = [], now = this.network.serverNow, own = this.network.self.id;
      const events = [...(this.collectEvents?.values() ?? [])];
      events.sort((a, b) => Number(b.player_id === own) - Number(a.player_id === own));
      for (const event of events) {
        const age = now - event.at_ms, progress = clamp(age / 500, 0, 1);
        if (age < 0 || progress >= 1) continue;
        const destination = event.player_id === this.network.self.id ? this.flight.position : this.remoteShips.get(event.player_id)?.position;
        if (!destination) continue;
        for (const id of event.glyph_ids) {
          const origin = this.dust.subarray(id * 8, id * 8 + 3);
          const t = progress * progress;
          const position = [...origin].map((v, i) => mix(v, destination[i], t));
          const projected = this.project(position);
          // Only visible particles use the budget; distant peers cannot hide an own pickup.
          if (!projected || projected.x < 0 || projected.x > this.width || projected.y < 0 || projected.y > this.height) continue;
          result.push({ key:'collect:' + event.id + ':' + id, id, position, projected, progress });
          if (result.length >= 64) return result;
        }
      }
      return result;
    }
    renderSoftware() {
      const ctx = this.context, dpr = this.canvas.width / Math.max(this.width, 1), basis = this.basis();
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, this.width, this.height);
      for (let i = 0; i < this.clouds.length; i += this.width < 600 ? 16 : 8) {
        const p = this.projectVertex(this.clouds, i, basis);
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
        const p = this.projectVertex(dust, i, basis);
        if (!p || p.x < 0 || p.x > this.width || p.y < 0 || p.y > this.height) continue;
        ctx.fillStyle = `rgba(220,220,220,${dust[i + 6] * .65})`;
        const size = clamp(p.scale * dust[i + 7], .5, 3);
        ctx.fillRect(p.x, p.y, size, size);
      }
      const geometry = this.geometry();
      const color = (array, offset) => `rgba(${Math.round(array[offset + 3] * 255)},${Math.round(array[offset + 4] * 255)},${Math.round(array[offset + 5] * 255)},${clamp(array[offset + 6], 0, 1)})`;
      for (const [type, width] of [['solids', 24], ['triangles', 24], ['lines', 16]]) {
        const array = geometry[type], faces = [];
        for (let i = 0; i < array.length; i += width) {
          const vertices = [];
          for (let j = 0; j < width; j += 8) vertices.push(this.projectVertex(array, i + j, basis));
          if (vertices.some(p => !p)) continue;
          faces.push({offset:i, vertices, depth:vertices.reduce((sum, p) => sum + p.depth, 0)});
        }
        if (type === 'solids') faces.sort((a, b) => b.depth - a.depth);
        for (const {offset:i, vertices} of faces) {
          ctx.beginPath(); ctx.moveTo(vertices[0].x, vertices[0].y);
          for (const p of vertices.slice(1)) ctx.lineTo(p.x, p.y);
          if (type !== 'lines') {
            ctx.closePath(); ctx.fillStyle = color(array, i); ctx.fill();
            // Same-color coverage closes Canvas antialias gaps between opaque faces.
            // Transparent cloud faces and line geometry retain their original alpha.
            if (type === 'solids' && [6, 14, 22].every(offset => array[i + offset] >= 1)) {
              ctx.strokeStyle = ctx.fillStyle; ctx.lineWidth = .6; ctx.lineJoin = 'round'; ctx.stroke();
            }
          }
          else { ctx.strokeStyle = color(array, i); ctx.lineWidth = 1; ctx.stroke(); }
        }
      }
      const array = geometry.points;
      for (let i = 0; i < array.length; i += 8) {
        const p = this.projectVertex(array, i, basis);
        if (!p) continue;
        const r = clamp(p.scale * array[i + 7], 2, 50), rgb = `${Math.round(array[i + 3] * 255)},${Math.round(array[i + 4] * 255)},${Math.round(array[i + 5] * 255)}`;
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
      const flying = Boolean(this.flight), small = this.width < 600;
      const maximum = flying ? this.reduced.matches ? small ? 28 : 56 : small ? 44 : 84 : small ? 20 : 40;
      for (let i = 0; i < this.dust.length && visible.size < maximum; i += 8 * (flying ? 7 : 17)) {
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
        token.style.opacity = flying ? clamp(.22 + p.scale * .035, .22, .42) : clamp(.12 + p.scale * .025, .12, .25);
      }
      for (const effect of this.absorptionGlyphs()) {
        const p = effect.projected;
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
      const used = [], labelRects = [],
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
          this.labelObserver?.observe(el);
        }
        const look = this.style(n);
        const labelText =
          (n.kind === 'cluster' ? '⋯ ' : look.root ? '✦ ' : look.stellar?.stage === 'star' ? '☼ ' : look.stellar?.stage === 'satellite' ? '◦ ' : look.certified ? '◇ ' : '') +
          (n.kind === 'user'
            ? M.handle(n.name || n.title || 'Signal')
            : n.name || n.title || 'Signal');
        const sizeKey = [labelText,look.root,look.certified,this.width].join('|');
        let measured = this.labelSizes?.get(el);
        if (el.textContent !== labelText) el.textContent = labelText;
        el.classList.toggle('root-label', look.root);
        el.classList.toggle('certified-label', look.certified);
        el.style.setProperty('--star-color', `rgb(${look.color.map(v => Math.round(v * 255)).join(',')})`);
        el.style.opacity = n.id === this.focusId || look.root ? 1 : .35 + look.light * .45;
        el.classList.toggle("selected", n.id === this.focusId);
        const left=Math.round(p.x+12),top=Math.round(p.y-7);
        if (!measured || measured.key !== sizeKey) {
          const rect=el.getBoundingClientRect();
          measured={key:sizeKey,width:rect.width,height:rect.height};
          this.labelSizes ??= new WeakMap();this.labelSizes.set(el,measured);
        }
        labelRects.push({left,top,right:left+measured.width,bottom:top+measured.height});
        el.style.transform = `translate(${left}px,${top}px)`;
      }
      for (const [id, el] of this.labelNodes)
        if (!visible.has(id)) {
          this.labelObserver?.unobserve?.(el);
          el.remove();
          this.labelNodes.delete(id);
        }
      this.avatars?.update({nodes:this.view.nodes, project:p => this.project(p), width:this.width, height:this.height,
        focusId:this.focusId, labelRects, radius:(node,now) => this.planetRadius(node,now), software:this.software, hidden:document.hidden});
    }
    updateShipLabels() {
      const container = document.getElementById('ship-labels');
      const scene = this.sceneConnection();
      if (!container || !scene) {
        for (const label of this.shipLabels.values()) label.remove();
        this.shipLabels.clear(); return;
      }
      const visible = new Set(), now = scene.serverNow, up = this.basis().up;
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
        const own = String(Boolean(this.flight && ship.id === this.network.self?.id)), shielded = String(ship.shield_until_ms > now);
        if (label.dataset.self !== own) label.dataset.self = own;
        if (label.dataset.shielded !== shielded) label.dataset.shielded = shielded;
        const transform = `translate(${Math.round(projected.x)}px,${Math.round(projected.y)}px)`;
        if (label.style.transform !== transform) label.style.transform = transform;
      }
      for (const [id, label] of this.shipLabels) if (!visible.has(id)) { label.remove(); this.shipLabels.delete(id); }
    }
  }
  globalThis.MSGUniverseFlight = Object.freeze({ Flight, FlightStick, FlightThrottle, flightBasis, tokenNebula, tokenLanes, lanePoint, planetMesh, MotionTrack, FlightPrediction, integrateFlight, inertialSegment, gravityAcceleration, planetContact });
  globalThis.MSGUniverseRenderer = UniverseRenderer;
})();
