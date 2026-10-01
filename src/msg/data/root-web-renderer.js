/* Small native WebGL renderer. Scenery is never represented as a user. */
(() => {
  "use strict";
  const M = globalThis.MSGUniverse;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const mix = (a, b, t) => a + (b - a) * t;
  const vs = `attribute vec3 aPosition; attribute vec4 aColor; attribute float aSize;
    uniform vec3 uTarget; uniform vec3 uRight; uniform vec3 uUp; uniform vec3 uEye;
    uniform vec3 uLens; uniform float uHeight; uniform float uShift; varying mediump vec4 vColor; varying mediump float vSeed;
    void main(){ vec3 r=aPosition-uTarget; float d=uLens.z-dot(r,uEye);
      gl_Position=vec4(dot(r,uRight)*uLens.x/uLens.y,dot(r,uUp)*uLens.x,1.0002*d-0.2,d);
      gl_Position.y+=uShift*gl_Position.w;
      gl_PointSize=clamp(aSize*uLens.x*uHeight/max(d,0.1),1.0,100.0); vColor=aColor; vSeed=fract(dot(aPosition,vec3(.17,.31,.53))); }`;
  const fs = `precision mediump float; varying mediump vec4 vColor; varying mediump float vSeed; uniform float uPoints;
    void main(){float alpha=vColor.a;
      if(uPoints>1.5){ vec2 p=abs(gl_PointCoord-.5); float mask;
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
      this.sceneSeed = Array.from(crypto.getRandomValues(new Uint32Array(2))).join(':');
      this.dust = M.nebula(2300, this.sceneSeed);
      this.visualTraits = new WeakMap();
      this.tokenField = document.getElementById('token-field');
      this.tokenNodes = new Map();
      this.hoverId = null;
      canvas.addEventListener("webglcontextlost", (e) => {
        e.preventDefault();
        this.lost = true;
        this.available = false;
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
        this.wake();
      });
      this.init();
      this.events();
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
    setGraph(graph) {
      this.graph = graph;
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
      return this.width < 600 && this.focusId ? .5 : 0;
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
      c.addEventListener("blur", () => this.keys.clear());
      document.addEventListener("visibilitychange", () => {
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
      const dt = Math.min(0.04, this.last ? (timestamp - this.last) / 1000 : 0);
      this.last = timestamp;
      const c = this.camera;
      if (!this.paused) {
        this.clock += dt;
        if (!this.pointers.size && !this.keys.size && !this.destination)
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
      if (this.keys.size) {
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
      for (const node of this.satellites) node.position = M.satellite(node.id, node.orbitCenter, this.clock);
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
      gl.clear(gl.COLOR_BUFFER_BIT);
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
      const draw = (vertices, mode, tokenDust = false) => {
        if (!vertices.length) return;
        gl.bufferData(
          gl.ARRAY_BUFFER,
          new Float32Array(vertices),
          gl.DYNAMIC_DRAW,
        );
        gl.uniform1f(u.uPoints, tokenDust ? 2 : mode === gl.POINTS ? 1 : 0);
        gl.drawArrays(mode, 0, vertices.length / 8);
      };
      draw(this.dust, gl.POINTS, true);
      const geometry = this.geometry();
      draw(geometry.triangles, gl.TRIANGLES);
      draw(geometry.lines, gl.LINES);
      draw(geometry.points, gl.POINTS);
      this.finish();
    }
    finish() {
      this.updateLabels();
      this.updateTokens();
      this.callbacks.camera?.(this.camera.target);
      this.dirty = false;
      if (!this.paused || this.destination || this.keys.size)
        this.frame = requestAnimationFrame((t) => this.render(t));
    }
    geometry() {
      const lines = [], points = [], triangles = [], b = this.basis();
      const now = this.callbacks.now?.() ?? Date.now();
      const vertex = (p, col, alpha, size = 1) => [...p, ...col, alpha, size];
      const line = (a, z, col, alpha) => lines.push(...vertex(a, col, alpha), ...vertex(z, col, alpha));
      const ring = (p, radius, col, alpha, { fraction = 1, tilt = .35, sides = 80, start = 0, dashed = false } = {}) => {
        const at = (angle) => [p[0] + Math.cos(angle) * radius,
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
        const alpha = type === 'orbit' ? .10 : related ? .33 : .08;
        let last = a.position;
        for (let j = 1; j <= 24; j++) {
          const t = j / 24;
          const p = a.position.map((n, i) => mix(n, z.position[i], t) + (i === 1 ? Math.sin(t * Math.PI) * 10 : 0));
          line(last, p, col, alpha);
          last = p;
        }
      }
      let detailed = 0;
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
          traits = { phase: rng() * M.TAU, speed: .025 + rng() * .085, tilt: .8 + rng() * .6 };
          this.visualTraits.set(node, traits);
        }
        const pulse = 1 + (this.paused ? 0 : Math.sin(this.clock * (1 + traits.speed * 8) + traits.phase) * style.pulse);
        points.push(...vertex(p, col, light * pulse, style.root ? 23 : isStar ? 5.5 : 2.2));
        const projected = this.project(p);
        if (!selected && !hovered && !style.root && (!projected || projected.scale * style.radius < 3 || detailed >= 96)) continue;
        detailed++;
        if (isStar) {
          const r = style.radius, rotation = this.clock * traits.speed + traits.phase;
          const corners = [[0, r * 1.4 * traits.tilt, 0], [r, 0, 0], [0, 0, r], [-r, 0, 0], [0, 0, -r], [0, -r * 1.4 * traits.tilt, 0]].map(v => [
            p[0] + v[0] * Math.cos(rotation) - v[2] * Math.sin(rotation), p[1] + v[1],
            p[2] + v[0] * Math.sin(rotation) + v[2] * Math.cos(rotation)]);
          for (let j = 1; j <= 4; j++) {
            const k = j === 4 ? 1 : j + 1;
            line(corners[0], corners[j], col, light * .9);
            line(corners[5], corners[j], col, light * .55);
            line(corners[j], corners[k], col, light * .6);
            for (const apex of [0, 5]) triangles.push(...vertex(corners[apex], col, light * (style.root ? .20 : .10)),
              ...vertex(corners[j], col, light * .14), ...vertex(corners[k], col, light * .08));
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
      return { lines, points, triangles };
    }
    renderSoftware() {
      const ctx = this.context, dpr = this.canvas.width / Math.max(this.width, 1);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, this.width, this.height);
      for (let i = 0; i < this.dust.length; i += 8) {
        const p = this.project(this.dust.slice(i, i + 3));
        if (!p || p.x < 0 || p.x > this.width || p.y < 0 || p.y > this.height) continue;
        ctx.fillStyle = `rgba(220,220,220,${this.dust[i + 6] * .65})`;
        const size = clamp(p.scale * this.dust[i + 7], .5, 3);
        ctx.fillRect(p.x, p.y, size, size);
      }
      const geometry = this.geometry();
      const color = (array, offset) => `rgba(${array.slice(offset + 3, offset + 6).map(v => Math.round(v * 255)).join(',')},${clamp(array[offset + 6], 0, 1)})`;
      for (const [type, width] of [['triangles', 24], ['lines', 16]]) {
        const array = geometry[type];
        for (let i = 0; i < array.length; i += width) {
          const vertices = [];
          for (let j = 0; j < width; j += 8) vertices.push(this.project(array.slice(i + j, i + j + 3)));
          if (vertices.some(p => !p)) continue;
          ctx.beginPath(); ctx.moveTo(vertices[0].x, vertices[0].y);
          for (const p of vertices.slice(1)) ctx.lineTo(p.x, p.y);
          if (type === 'triangles') { ctx.closePath(); ctx.fillStyle = color(array, i); ctx.fill(); }
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
        token.style.transform = `translate(${Math.round(p.x)}px,${Math.round(p.y)}px)`;
        token.style.fontSize = clamp(p.scale * 4.5, 7, 12) + 'px';
        token.style.opacity = clamp(.12 + p.scale * .025, .12, .25);
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
  }
  globalThis.MSGUniverseRenderer = UniverseRenderer;
})();
