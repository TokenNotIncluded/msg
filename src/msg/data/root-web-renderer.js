/* Small native WebGL renderer. Scenery is never represented as a user. */
(() => {
  "use strict";
  const M = globalThis.MSGUniverse;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const mix = (a, b, t) => a + (b - a) * t;
  const vs = `attribute vec3 aPosition; attribute vec4 aColor; attribute float aSize;
    uniform vec3 uTarget; uniform vec3 uRight; uniform vec3 uUp; uniform vec3 uEye;
    uniform vec3 uLens; uniform float uHeight; varying vec4 vColor;
    void main(){ vec3 r=aPosition-uTarget; float d=uLens.z-dot(r,uEye);
      gl_Position=vec4(dot(r,uRight)*uLens.x/uLens.y,dot(r,uUp)*uLens.x,1.0002*d-0.2,d);
      gl_PointSize=clamp(aSize*uLens.x*uHeight/max(d,0.1),1.0,100.0); vColor=aColor; }`;
  const fs = `precision mediump float; varying vec4 vColor; uniform float uPoints;
    void main(){float alpha=vColor.a;if(uPoints>0.5){float d=length(gl_PointCoord-0.5)*2.0;
      if(d>1.0)discard; alpha*=exp(-d*d*9.0)+0.045*(1.0-d);}
      gl_FragColor=vec4(vColor.rgb,alpha);}`;
  const palette = {
    user: [1, 0.9, 0.72],
    post: [0.52, 0.77, 0.91],
    reply: [0.72, 0.79, 1],
    private: [0.58, 0.81, 0.77],
    "private-message": [0.58, 0.81, 0.77],
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
      const rng = M.random("msg:decorative-dust:not-accounts");
      this.dust = [];
      for (let i = 0; i < 2100; i++) {
        const a = rng() * M.TAU,
          r = Math.sqrt(rng()) * 225,
          y = (rng() - 0.5) * (6 + (18 * r) / 225);
        this.dust.push(
          Math.cos(a) * r,
          y,
          Math.sin(a) * r,
          0.61,
          0.64,
          0.67,
          0.24 + rng() * 0.29,
          0.22 + rng() * 0.52,
        );
      }
      for (let i = 0; i < 250; i++)
        this.dust.push(
          (rng() - 0.5) * 950,
          (rng() - 0.5) * 600,
          (rng() - 0.5) * 950,
          0.58,
          0.63,
          0.69,
          0.35,
          0.45,
        );
      canvas.addEventListener("webglcontextlost", (e) => {
        e.preventDefault();
        this.lost = true;
        this.available = false;
        cancelAnimationFrame(this.frame);
        this.frame = 0;
        this.labels.replaceChildren();
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
      this.wake();
    }
    setFocus(id) {
      this.focusId = id;
      this.wake();
    }
    focus(node, distance = 74) {
      this.focusId = node.id;
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
    project(p) {
      const b = this.basis(),
        r = p.map((n, i) => n - this.camera.target[i]),
        dot = (v) => r.reduce((s, n, i) => s + n * v[i], 0),
        depth = this.camera.distance - dot(b.eye);
      if (depth < 1) return null;
      const scale = (1.72 * this.height * 0.5) / depth;
      return {
        x: this.width / 2 + dot(b.right) * scale,
        y: this.height / 2 - dot(b.up) * scale,
        depth,
        scale,
      };
    }
    hit(x, y) {
      let best = null,
        score = Infinity;
      for (const node of this.graph.nodes) {
        const p = this.project(node.position);
        if (!p) continue;
        const d = Math.hypot(p.x - x, p.y - y);
        if (d < Math.max(15, Math.min(28, p.scale * 3)) && d < score) {
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
          c.style.cursor = this.hit(e.clientX - r.left, e.clientY - r.top)
            ? "pointer"
            : "grab";
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
          if (node) this.callbacks.select?.(node);
        }
        this.wake();
      };
      c.addEventListener("pointerup", release);
      c.addEventListener("pointercancel", release);
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
      const draw = (vertices, mode) => {
        if (!vertices.length) return;
        gl.bufferData(
          gl.ARRAY_BUFFER,
          new Float32Array(vertices),
          gl.DYNAMIC_DRAW,
        );
        gl.uniform1f(u.uPoints, mode === gl.POINTS ? 1 : 0);
        gl.drawArrays(mode, 0, vertices.length / 8);
      };
      draw(this.dust, gl.POINTS);
      const lines = [],
        points = [],
        triangles = [];
      const vertex = (p, color, alpha, size = 1) => [
        ...p,
        ...color,
        alpha,
        size,
      ];
      const line = (a, z, col, opacity) =>
        lines.push(...vertex(a, col, opacity), ...vertex(z, col, opacity));
      for (const [a, z, type] of this.graph.links) {
        const col =
            type === "private"
              ? palette.private
              : type === "orbit"
                ? palette.post
                : palette.reply,
          segments = type === "orbit" ? 1 : 24;
        let last = a.position;
        for (let j = 1; j <= segments; j++) {
          const t = j / segments,
            p = a.position.map(
              (n, i) =>
                mix(n, z.position[i], t) +
                (i === 1
                  ? Math.sin(t * Math.PI) * (type === "private" ? 8 : 14)
                  : 0),
            );
          line(last, p, col, type === "orbit" ? 0.12 : 0.16);
          last = p;
        }
      }
      for (const node of this.graph.nodes) {
        const p = node.position,
          col = palette[node.kind] || palette.user,
          selected = node.id === this.focusId;
        points.push(
          ...vertex(
            p,
            col,
            selected ? 0.95 : 0.8,
            node.kind === "user" || node.kind === "private" ? 5 : 2.8,
          ),
        );
        if (node.kind === "user" || node.kind === "private") {
          const r = selected ? 2.5 : 1.25,
            rotation = this.clock * 0.08;
          const corners = [
            [0, r * 1.6, 0],
            [r, 0, 0],
            [0, 0, r],
            [-r, 0, 0],
            [0, 0, -r],
            [0, -r * 1.6, 0],
          ].map((v) => [
            p[0] + v[0] * Math.cos(rotation) - v[2] * Math.sin(rotation),
            p[1] + v[1],
            p[2] + v[0] * Math.sin(rotation) + v[2] * Math.cos(rotation),
          ]);
          for (let j = 1; j <= 4; j++) {
            const k = j === 4 ? 1 : j + 1;
            line(corners[0], corners[j], col, 0.72);
            line(corners[5], corners[j], col, 0.38);
            line(corners[j], corners[k], col, 0.38);
            triangles.push(
              ...vertex(corners[0], col, 0.09),
              ...vertex(corners[j], col, 0.09),
              ...vertex(corners[k], col, 0.09),
            );
          }
        }
        if (selected) {
          for (const radius of [6, 9.5]) {
            for (let j = 0; j < 90; j++) {
              const a = (j / 90) * M.TAU,
                z = ((j + 1) / 90) * M.TAU;
              if (radius === 9.5 && j % 9 > 5) continue;
              line(
                [
                  p[0] + Math.cos(a) * radius,
                  p[1],
                  p[2] + Math.sin(a) * radius,
                ],
                [
                  p[0] + Math.cos(z) * radius,
                  p[1],
                  p[2] + Math.sin(z) * radius,
                ],
                col,
                radius === 6 ? 0.45 : 0.22,
              );
            }
          }
        }
      }
      draw(triangles, gl.TRIANGLES);
      draw(lines, gl.LINES);
      draw(points, gl.POINTS);
      this.finish();
    }
    finish() {
      this.updateLabels();
      this.callbacks.camera?.(this.camera.target);
      this.dirty = false;
      if (!this.paused || this.destination || this.keys.size)
        this.frame = requestAnimationFrame((t) => this.render(t));
    }
    renderSoftware() {
      const ctx = this.context,
        dpr = this.canvas.width / Math.max(this.width, 1);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, this.width, this.height);
      const stroke = (points, color, width = 1) => {
        ctx.beginPath();
        let started = false;
        for (const pos of points) {
          const p = this.project(pos);
          if (!p) {
            started = false;
            continue;
          }
          if (!started) {
            ctx.moveTo(p.x, p.y);
            started = true;
          } else ctx.lineTo(p.x, p.y);
        }
        ctx.strokeStyle = color;
        ctx.lineWidth = width;
        ctx.stroke();
      };
      for (let i = 0; i < this.dust.length; i += 8) {
        const p = this.project(this.dust.slice(i, i + 3));
        if (!p || p.x < 0 || p.x > this.width || p.y < 0 || p.y > this.height)
          continue;
        ctx.fillStyle = "rgba(173,184,197," + this.dust[i + 6] * 0.6 + ")";
        const size = Math.min(1.5, Math.max(0.55, p.scale * 0.35));
        ctx.fillRect(p.x, p.y, size, size);
      }
      for (const [a, z, type] of this.graph.links) {
        const points = [];
        for (let j = 0; j <= 24; j++) {
          const t = j / 24;
          points.push(
            a.position.map(
              (n, i) =>
                mix(n, z.position[i], t) +
                (i === 1
                  ? Math.sin(t * Math.PI) * (type === "orbit" ? 0 : 14)
                  : 0),
            ),
          );
        }
        stroke(
          points,
          type === "private"
            ? "rgba(141,200,190,.22)"
            : type === "orbit"
              ? "rgba(145,187,206,.16)"
              : "rgba(164,179,220,.20)",
        );
      }
      const sorted = this.graph.nodes
        .map((n) => ({ n, p: this.project(n.position) }))
        .filter(({ p }) => p)
        .sort((a, b) => b.p.depth - a.p.depth);
      for (const { n, p } of sorted) {
        const color =
            n.kind === "user"
              ? "239,215,168"
              : n.kind.startsWith("private")
                ? "148,207,196"
                : "145,191,216",
          selected = n.id === this.focusId,
          r = Math.min(
            20,
            Math.max(3, p.scale * (n.kind === "user" ? 3 : 1.8)),
          );
        const glow = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r * 3);
        glow.addColorStop(0, "rgba(" + color + ",.55)");
        glow.addColorStop(0.25, "rgba(" + color + ",.12)");
        glow.addColorStop(1, "rgba(" + color + ",0)");
        ctx.fillStyle = glow;
        ctx.fillRect(p.x - r * 3, p.y - r * 3, r * 6, r * 6);
        const pos = n.position,
          s = Math.min(2, selected ? 2 : 1.2),
          top = [pos[0], pos[1] + s * 1.6, pos[2]],
          bottom = [pos[0], pos[1] - s * 1.6, pos[2]],
          corners = [
            [s, 0, 0],
            [0, 0, s],
            [-s, 0, 0],
            [0, 0, -s],
          ].map((v) => v.map((x, i) => x + pos[i]));
        if (n.kind === "user" || n.kind === "private") {
          for (const corner of corners)
            stroke([top, corner, bottom], "rgba(" + color + ",.7)");
          stroke([...corners, corners[0]], "rgba(" + color + ",.4)");
        } else {
          ctx.fillStyle = "rgba(" + color + ",.95)";
          ctx.fillRect(p.x - 1.4, p.y - 1.4, 2.8, 2.8);
        }
        if (selected)
          for (const radius of [6, 9.5]) {
            const ring = [];
            for (let j = 0; j <= 90; j++) {
              const a = (j / 90) * M.TAU;
              ring.push([
                pos[0] + Math.cos(a) * radius,
                pos[1],
                pos[2] + Math.sin(a) * radius,
              ]);
            }
            stroke(ring, "rgba(" + color + ",.35)");
          }
      }
    }
    updateLabels() {
      const candidates = this.graph.nodes
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
            a.p.depth - b.p.depth,
        );
      const used = [],
        visible = new Set();
      for (const { n, p } of candidates) {
        if (used.length >= 14) break;
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
          el.textContent =
            (n.kind === "user" ? "@" : "") + (n.name || n.title || "Signal");
          this.labels.append(el);
          this.labelNodes.set(n.id, el);
        }
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
