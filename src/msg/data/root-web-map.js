/* Public X/Z chart. All positions and region changes remain server-owned. */
(() => {
  "use strict";
  const M = globalThis.MSGUniverse;
  const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const point = value => Array.isArray(value) && value.length === 3 &&
    value.every(v => Number.isFinite(v) && Math.abs(v) <= 1_000_000);
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
  const label = region => "区域 " + String(region.id).padStart(2, "0");

  function project(position, view, size) {
    return [(position[0] - view.x) * view.scale + size.width / 2,
      (position[2] - view.z) * view.scale + size.height / 2];
  }
  function unproject(pixel, view, size) {
    return [(pixel[0] - size.width / 2) / view.scale + view.x, 0,
      (pixel[1] - size.height / 2) / view.scale + view.z];
  }
  function regionGeometry(regions) {
    const seen = new Set(), result = [];
    for (const r of Array.isArray(regions) ? regions : []) {
      if (!Number.isInteger(r?.id) || r.id < 0 || r.id > 18 || seen.has(r.id) ||
          !point(r.center) || !Number.isFinite(r.radius) || r.radius <= 0 || r.radius > 1_000_000) continue;
      seen.add(r.id);
      result.push({ id: r.id, name: String(r.name || "sector-" + r.id).slice(0, 80),
        center: [...r.center], radius: r.radius });
    }
    return result.sort((a, b) => a.id - b.id);
  }
  function publicStars(graph) {
    const seen = new Set();
    return (Array.isArray(graph?.nodes) ? graph.nodes : []).filter(node => {
      if (node?.kind !== "user" || typeof node.id !== "string" ||
          !point(node.position) || seen.has(node.id)) return false;
      seen.add(node.id); return true;
    }).map(node => ({ ...node, position: [...node.position] }));
  }
  function visibleStars(index, view, size, budget = 192) {
    return index.query((p, radius) => {
      const [x, y] = project(p, view, size), extent = radius * view.scale;
      return { visible: x + extent >= -8 && x - extent <= size.width + 8 &&
        y + extent >= -8 && y - extent <= size.height + 8, size: extent * 2 };
    }, { budget, threshold: 22 });
  }
  function nearestRegion(regions, pixel, view, size, distance = 28) {
    let chosen = null, best = distance;
    for (const region of regions) {
      const p = project(region.center, view, size), d = Math.hypot(p[0] - pixel[0], p[1] - pixel[1]);
      if (d < best) { chosen = region; best = d; }
    }
    return chosen;
  }
  function regionPopulation(region, counts) {
    return counts && own(counts, region.id) ? count(counts[region.id]) : null;
  }

  class FlatMap {
    constructor(canvas, options = {}) {
      this.canvas = canvas; this.options = options;
      this.container = options.container || canvas.parentElement;
      this.status = options.status; this.actions = options.actions;
      this.document = canvas.ownerDocument;
      this.size = { width: 1, height: 1 }; this.view = { x: 0, z: 0, scale: 1 };
      this.regions = []; this.stars = []; this.players = []; this.counts = null;
      this.currentRegion = null; this.focusedRegion = null; this.selfId = null;
      this.regionReadyMs = 0; this.serverTimeMs = null; this.visible = false; this.destroyed = false;
      this.index = new M.SpatialIndex([]); this.playerIndex = new M.SpatialIndex([]);
      this.pointers = new Map(); this.listeners = []; this.buttons = new Map();
      this.requestFrame = globalThis.requestAnimationFrame?.bind(globalThis) || (fn => setTimeout(fn, 16));
      this.cancelFrame = globalThis.cancelAnimationFrame?.bind(globalThis) || clearTimeout;
      canvas.setAttribute("aria-label", "平面星图，X / Z。方向键选择区域，Shift 加方向键平移，加减键缩放，Enter 申请进入区域，Escape 返回飞行。");
      if (this.status?.id) canvas.setAttribute("aria-describedby", this.status.id);
      canvas.classList.add("region-map-canvas"); canvas.tabIndex = 0;
      this.makeTools(); this.bind();
      if (globalThis.ResizeObserver) {
        this.observer = new ResizeObserver(() => this.resize()); this.observer.observe(canvas);
      } else this.listen(globalThis, "resize", () => this.resize());
    }
    listen(target, name, listener, options) {
      if (!target?.addEventListener) return;
      target.addEventListener(name, listener, options);
      this.listeners.push(() => target.removeEventListener(name, listener, options));
    }
    makeTools() {
      if (!this.actions) return;
      this.tools = this.document.createElement("div"); this.tools.className = "region-map-tools";
      const tools = [["放大 +", () => this.zoom(1.4)], ["缩小 −", () => this.zoom(1 / 1.4)],
        ["全图", () => this.reset()], ["我的位置", () => this.centerSelf()]];
      for (const [text, action] of tools) {
        const button = this.document.createElement("button"); button.type = "button";
        button.textContent = text; this.listen(button, "click", action); this.tools.append(button);
      }
      this.regionList = this.document.createElement("div"); this.regionList.className = "region-map-regions";
      this.regionList.setAttribute("aria-label", "可进入的区域");
      this.note = this.document.createElement("p"); this.note.className = "region-map-note";
      this.note.textContent = "拖动平移 · 滚轮 / 双指缩放。点区域数字或下方按钮申请跳转；消耗 20 燃料，冷却 10 秒。所有玩家在同一片星域。星点是公开身份，三角是实时玩家；区域人数由服务器报告。";
      this.actions.append(this.tools, this.regionList, this.note);
    }
    update(data = {}) {
      if (this.destroyed) return;
      if (own(data, "graph")) {
        this.stars = publicStars(data.graph);
        this.index = new M.SpatialIndex(this.stars.filter(star => star.id !== "u_root"));
      }
      if (own(data, "selfId")) this.selfId = typeof data.selfId === "string" ? data.selfId : null;
      if (own(data, "players")) {
        const seen = new Set();
        this.players = (Array.isArray(data.players) ? data.players : []).filter(p => {
          if (typeof p?.id !== "string" || !point(p.position) || seen.has(p.id)) return false;
          seen.add(p.id); return true;
        }).map(p => ({ ...p, position: [...p.position] }));
      }
      if (own(data, "players") || own(data, "selfId")) this.playerIndex = new M.SpatialIndex(this.players.filter(p => p.id !== this.selfId));
      if (own(data, "regions")) {
        this.regions = regionGeometry(data.regions); this.syncRegions();
        if (!this.regions.some(r => r.id === this.focusedRegion)) this.focusedRegion = null;
        if (!this.viewSet) this.reset();
      }
      if (own(data, "regionCounts")) this.counts = data.regionCounts && typeof data.regionCounts === "object" ? { ...data.regionCounts } : null;
      if (own(data, "currentRegion")) this.currentRegion = Number.isInteger(data.currentRegion) ? data.currentRegion : null;
      if (own(data, "regionReadyMs")) this.regionReadyMs = Math.max(0, Number(data.regionReadyMs) || 0);
      if (own(data, "serverTimeMs")) this.serverTimeMs = Number.isFinite(data.serverTimeMs) ? data.serverTimeMs : null;
      if (this.focusedRegion === null) this.focusedRegion = this.regions.find(r => r.id === this.currentRegion)?.id ?? this.regions[0]?.id ?? null;
      this.syncLabels(); this.schedule();
    }
    syncRegions() {
      if (!this.regionList) return;
      const ids = new Set(this.regions.map(r => r.id));
      for (const [id, button] of this.buttons) if (!ids.has(id)) { button.remove(); this.buttons.delete(id); }
      for (const region of this.regions) {
        if (this.buttons.has(region.id)) continue;
        const button = this.document.createElement("button"); button.type = "button";
        this.listen(button, "click", () => this.select(region.id));
        this.listen(button, "focus", () => this.focus(region.id, true));
        this.regionList.append(button); this.buttons.set(region.id, button);
      }
    }
    syncLabels() {
      for (const region of this.regions) {
        const button = this.buttons.get(region.id); if (!button) continue;
        const population = regionPopulation(region, this.counts), current = region.id === this.currentRegion;
        const text = label(region) + " · " + (population === null ? "人数未知" : population + " 人") + (current ? " · 当前" : "");
        if (button.textContent !== text) button.textContent = text;
        button.setAttribute("aria-pressed", String(current));
        button.setAttribute("title", region.name);
      }
      if (!this.status) return;
      const current = this.regions.find(r => r.id === this.currentRegion), population = current && regionPopulation(current, this.counts);
      const cooldown = this.serverTimeMs === null ? 0 : Math.max(0, this.regionReadyMs - this.serverTimeMs);
      const selected = this.regions.find(r => r.id === this.focusedRegion);
      const selectedPopulation = selected && regionPopulation(selected, this.counts);
      const text = (current ? "当前 " + label(current) + " · " + (population === null ? "人数未知" : population + " 人在线") : "等待当前区域") +
        (selected ? " · 选择 " + label(selected) + "（" + (selectedPopulation === null ? "人数未知" : selectedPopulation + " 人") + "）" : "") +
        " · " + this.stars.length.toLocaleString() + " 颗已加载公开星点 · " + this.players.length + " 个实时玩家" +
        (this.regions.length ? "" : " · 等待服务器区域数据") +
        (cooldown > 0 ? " · 区域切换冷却 " + Math.ceil(cooldown / 1000) + " 秒" : "");
      if (this.status.textContent !== text) this.status.textContent = text;
    }
    focus(id, reveal = false) {
      const region = this.regions.find(r => r.id === id); if (!region) return;
      if (reveal) {
        const p = project(region.center, this.view, this.size);
        if (p[0] < 36 || p[0] > this.size.width - 36 || p[1] < 36 || p[1] > this.size.height - 36) {
          this.view.x = region.center[0]; this.view.z = region.center[2]; this.schedule();
        }
      }
      if (this.focusedRegion === id) return;
      this.focusedRegion = id; this.options.onRegionFocus?.(id); this.syncLabels(); this.schedule();
    }
    select(id) {
      if (this.destroyed || !this.regions.some(r => r.id === id)) return;
      this.focus(id); this.options.onSelectRegion?.(id);
    }
    show() {
      if (this.destroyed) return;
      this.visible = true; if (this.container) this.container.hidden = false;
      this.resize(); this.canvas.focus({ preventScroll: true }); this.schedule();
    }
    hide() {
      this.visible = false; if (this.container) this.container.hidden = true;
      this.pointers.clear(); this.gesture = null;
      if (this.frame !== undefined) { this.cancelFrame(this.frame); this.frame = undefined; }
    }
    return3D() { this.hide(); this.options.onReturn3D?.(); }
    resize() {
      if (this.destroyed) return;
      const box = this.canvas.getBoundingClientRect(); if (box.width < 1 || box.height < 1) return;
      const old = this.size;
      this.size = { width: box.width, height: box.height };
      const ratio = Math.min(2, globalThis.devicePixelRatio || 1,
        Math.sqrt(2_000_000 / (box.width * box.height)));
      this.canvas.width = Math.max(1, Math.round(box.width * ratio));
      this.canvas.height = Math.max(1, Math.round(box.height * ratio));
      this.ratio = ratio;
      if (!this.viewSet || old.width === 1) this.reset();
      else this.view.scale *= Math.min(box.width, box.height) / Math.min(old.width, old.height);
      this.schedule();
    }
    limits() {
      let extent = 480;
      for (const r of this.regions) extent = Math.max(extent, Math.abs(r.center[0]) + r.radius, Math.abs(r.center[2]) + r.radius);
      return { fit: Math.max(.0001, Math.min(this.size.width, this.size.height) / (extent * 2.15)), extent };
    }
    reset() {
      const { fit } = this.limits(); this.view = { x: 0, z: 0, scale: fit };
      this.viewSet = this.size.width > 1; this.schedule();
    }
    centerSelf() {
      const self = this.players.find(p => p.id === this.selfId);
      if (!self) return;
      this.view.x = self.position[0]; this.view.z = self.position[2]; this.schedule();
    }
    pan(dx, dy) {
      const { extent } = this.limits();
      this.view.x = clamp(this.view.x - dx / this.view.scale, -extent * 2, extent * 2);
      this.view.z = clamp(this.view.z - dy / this.view.scale, -extent * 2, extent * 2); this.schedule();
    }
    zoom(factor, pixel = [this.size.width / 2, this.size.height / 2]) {
      if (!Number.isFinite(factor) || factor <= 0) return;
      const anchor = unproject(pixel, this.view, this.size), { fit } = this.limits();
      this.view.scale = clamp(this.view.scale * factor, fit * .55, fit * 18);
      const next = unproject(pixel, this.view, this.size);
      this.view.x += anchor[0] - next[0]; this.view.z += anchor[2] - next[2]; this.schedule();
    }
    pixel(event) { const box = this.canvas.getBoundingClientRect(); return [event.clientX - box.left, event.clientY - box.top]; }
    bind() {
      this.listen(this.container, "keydown", e => {
        if (e.key !== "Escape" || e.defaultPrevented || !this.visible) return;
        e.preventDefault(); e.stopPropagation?.(); this.return3D();
      });
      this.listen(this.canvas, "wheel", e => {
        e.preventDefault(); this.zoom(Math.exp(-clamp(e.deltaY, -250, 250) * .003), this.pixel(e));
      }, { passive: false });
      this.listen(this.canvas, "pointerdown", e => {
        if (e.button !== undefined && e.button !== 0) return;
        e.preventDefault(); this.canvas.focus({ preventScroll: true });
        this.canvas.setPointerCapture?.(e.pointerId);
        const pixel = this.pixel(e); this.pointers.set(e.pointerId, pixel);
        if (this.pointers.size === 1) this.gesture = { start: pixel, last: pixel, distance: 0, moved: false };
        else if (this.gesture) this.gesture.moved = true;
      });
      this.listen(this.canvas, "pointermove", e => {
        const pixel = this.pixel(e), previous = this.pointers.get(e.pointerId);
        if (!previous) { const r = nearestRegion(this.regions, pixel, this.view, this.size); if (r) this.focus(r.id); return; }
        const before = [...this.pointers.values()]; this.pointers.set(e.pointerId, pixel);
        if (this.pointers.size > 1) {
          const after = [...this.pointers.values()], mid = p => [(p[0][0] + p[1][0]) / 2, (p[0][1] + p[1][1]) / 2];
          const a = mid(before), b = mid(after), distance = p => Math.hypot(p[0][0] - p[1][0], p[0][1] - p[1][1]);
          this.pan(b[0] - a[0], b[1] - a[1]);
          const oldDistance = distance(before); if (oldDistance > 1) this.zoom(distance(after) / oldDistance, b);
          if (this.gesture) this.gesture.moved = true;
        } else if (this.gesture) {
          this.gesture.distance += Math.hypot(pixel[0] - previous[0], pixel[1] - previous[1]);
          if (this.gesture.distance > 6) this.gesture.moved = true;
          if (this.gesture.moved) this.pan(pixel[0] - previous[0], pixel[1] - previous[1]);
          this.gesture.last = pixel;
        }
      });
      const finish = (e, cancelled) => {
        if (!this.pointers.has(e.pointerId)) return;
        const gesture = this.gesture, pixel = this.pixel(e);
        this.pointers.delete(e.pointerId);
        if (cancelled && gesture) gesture.moved = true;
        if (!cancelled && !this.pointers.size && gesture && !gesture.moved && Math.hypot(pixel[0] - gesture.start[0], pixel[1] - gesture.start[1]) <= 6) {
          const r = nearestRegion(this.regions, pixel, this.view, this.size); if (r) this.select(r.id);
        }
        if (!this.pointers.size) this.gesture = null;
        if (this.canvas.hasPointerCapture?.(e.pointerId)) this.canvas.releasePointerCapture?.(e.pointerId);
      };
      this.listen(this.canvas, "pointerup", e => finish(e, false));
      this.listen(this.canvas, "pointercancel", e => finish(e, true));
      this.listen(this.canvas, "lostpointercapture", e => finish(e, true));
      this.listen(this.canvas, "keydown", e => {
        if (e.ctrlKey || e.altKey || e.metaKey) return;
        const directions = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
        if (directions[e.key]) {
          e.preventDefault(); e.stopPropagation?.(); const [dx, dy] = directions[e.key];
          if (e.shiftKey) this.pan(-dx * 64, -dy * 64);
          else this.directionalFocus(dx, dy);
        } else if (["+", "=", "-", "_", "Home", "0", "Enter", "Escape"].includes(e.key)) {
          e.preventDefault(); e.stopPropagation?.();
          if (e.key === "Escape") this.return3D();
          else if (e.key === "Enter") this.select(this.focusedRegion);
          else if (["Home", "0"].includes(e.key)) this.reset();
          else this.zoom(["+", "="].includes(e.key) ? 1.4 : 1 / 1.4);
        }
      });
    }
    directionalFocus(dx, dy) {
      const from = this.regions.find(r => r.id === this.focusedRegion) || this.regions[0]; if (!from) return;
      let best = null, score = Infinity;
      for (const r of this.regions) {
        const x = r.center[0] - from.center[0], z = r.center[2] - from.center[2], forward = x * dx + z * dy;
        if (forward <= 0) continue;
        const candidate = Math.hypot(x, z) + Math.abs(x * dy - z * dx) * 2;
        if (candidate < score) { best = r; score = candidate; }
      }
      if (best) this.focus(best.id, true);
    }
    schedule() {
      if (!this.visible || this.destroyed || this.frame !== undefined) return;
      this.frame = this.requestFrame(() => { this.frame = undefined; if (this.visible && !this.destroyed) this.draw(); });
    }
    draw() {
      const ctx = this.canvas.getContext("2d"); if (!ctx) return;
      const { width, height } = this.size, s = this.view.scale;
      ctx.setTransform(this.ratio || 1, 0, 0, this.ratio || 1, 0, 0);
      ctx.fillStyle = "#080909"; ctx.fillRect(0, 0, width, height);
      ctx.font = "11px monospace"; ctx.textAlign = "left"; ctx.textBaseline = "middle";
      const unit = 10 ** Math.ceil(Math.log10(70 / s));
      const low = unproject([0, 0], this.view, this.size), high = unproject([width, height], this.view, this.size);
      ctx.strokeStyle = "#202423"; ctx.lineWidth = 1; ctx.beginPath();
      for (let x = Math.ceil(low[0] / unit) * unit; x <= high[0]; x += unit) { const p = project([x, 0, 0], this.view, this.size); ctx.moveTo(p[0], 0); ctx.lineTo(p[0], height); }
      for (let z = Math.ceil(low[2] / unit) * unit; z <= high[2]; z += unit) { const p = project([0, 0, z], this.view, this.size); ctx.moveTo(0, p[1]); ctx.lineTo(width, p[1]); }
      ctx.stroke(); ctx.fillStyle = "#84918a"; ctx.fillText("X →   Z ↓", 12, 18);
      ctx.fillText("刻度 " + unit + " · " + (s / this.limits().fit).toFixed(1) + "×", 12, height - 15);
      const chart = visibleStars(this.index, this.view, this.size);
      const root = this.stars.find(star => star.id === "u_root");
      if (root) {
        const p = project(root.position, this.view, this.size);
        if (p[0] >= -8 && p[0] <= width + 8 && p[1] >= -8 && p[1] <= height + 8) chart.push(root);
      }
      for (const cell of chart) {
        const [x, y] = project(cell.position, this.view, this.size), cluster = cell.kind === "cluster";
        ctx.fillStyle = cell.id === "u_root" ? "#fff7d7" : cluster ? "#798882" : "#b7c3bc";
        ctx.beginPath(); ctx.arc(x, y, cell.id === "u_root" ? 4 : cluster ? Math.min(8, 3 + Math.log10(cell.count)) : 1.5, 0, Math.PI * 2); ctx.fill();
        if (cluster) { ctx.fillStyle = "#acb8b0"; ctx.fillText(cell.count.toLocaleString(), x + 9, y); }
        if (cell.id === "u_root") { ctx.fillStyle = "#fff7d7"; ctx.fillText("Root", x + 8, y - 8); }
      }
      for (const r of this.regions) {
        const [x, y] = project(r.center, this.view, this.size), current = r.id === this.currentRegion, focused = r.id === this.focusedRegion;
        if (current || focused) {
          ctx.strokeStyle = current ? "#729986" : "#9b9278"; ctx.setLineDash([4, 7]);
          ctx.beginPath(); ctx.arc(x, y, r.radius * s, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
        }
        ctx.fillStyle = current ? "#bddbc9" : focused ? "#e5d7b3" : "#778a80";
        ctx.fillRect(x - 3, y - 3, 6, 6); ctx.fillText(String(r.id).padStart(2, "0"), x + 7, y - 8);
        if (current || focused) {
          const n = regionPopulation(r, this.counts);
          ctx.fillText((current ? "当前 · " : "") + (n === null ? "人数未知" : n + " 人"), x + 7, y + 9);
        }
      }
      // Dense player sets aggregate with the same hard draw budget as stars.
      for (const p of visibleStars(this.playerIndex, this.view, this.size, 96)) {
        if (p.id === this.selfId) continue;
        this.drawPlayer(ctx, p, false);
      }
      const self = this.players.find(p => p.id === this.selfId); if (self) this.drawPlayer(ctx, self, true);
    }
    drawPlayer(ctx, player, self) {
      const [x, y] = project(player.position, this.view, this.size);
      if (x < -15 || y < -15 || x > this.size.width + 15 || y > this.size.height + 15) return;
      ctx.save(); ctx.translate(x, y); ctx.rotate(-(Number(player.yaw) || 0));
      ctx.strokeStyle = self ? "#fff2be" : "#89cab0"; ctx.lineWidth = self ? 2 : 1;
      ctx.beginPath(); ctx.moveTo(0, -6); ctx.lineTo(4, 4); ctx.lineTo(0, 2); ctx.lineTo(-4, 4); ctx.closePath(); ctx.stroke(); ctx.restore();
      if (self || player.kind === "cluster") {
        ctx.fillStyle = self ? "#fff2be" : "#89cab0"; ctx.fillText(self ? "你" : player.count + " 玩家", x + 8, y);
      }
    }
    destroy() {
      this.hide(); this.destroyed = true; this.observer?.disconnect();
      for (const remove of this.listeners.splice(0)) remove();
      this.tools?.remove(); this.regionList?.remove(); this.note?.remove(); this.buttons.clear();
    }
  }
  globalThis.MSGFlatMap = Object.freeze({ Map: FlatMap, project, unproject, regionGeometry,
    publicStars, visibleStars, nearestRegion, regionPopulation });
})();
