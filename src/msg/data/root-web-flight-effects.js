/* Bounded presentation only. A local shot never decides a hit or changes ship state. */
(() => {
  'use strict';
  const TAU = Math.PI * 2;
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  const validTime = value => Number.isFinite(value) && value >= 0;
  const validPoint = point => Array.isArray(point) && point.length === 3 && point.every(Number.isFinite);
  const at = (point, direction, distance) => point.map((value, axis) => value + direction[axis] * distance);
  const unit = vector => {
    const length = Math.hypot(...vector);
    return length > 1e-6 ? vector.map(value => value / length) : [0, 0, -1];
  };
  const basis = (yaw, pitch) => ({
    forward:[-Math.cos(pitch) * Math.sin(yaw), -Math.sin(pitch), -Math.cos(pitch) * Math.cos(yaw)],
    right:[Math.cos(yaw), 0, -Math.sin(yaw)],
    up:[-Math.sin(pitch) * Math.sin(yaw), Math.cos(pitch), -Math.sin(pitch) * Math.cos(yaw)],
  });
  const cross = (a, b) => [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
  const shotBasis = forward => {
    const right = unit(cross(forward, Math.abs(forward[1]) > .95 ? [1,0,0] : [0,1,0]));
    return {forward, right, up:unit(cross(right, forward))};
  };
  const limits = Object.freeze({shots:24, impacts:24, seen:512, seenMs:8500});
  const shotMs = 420, hitMs = 650, deathMs = 1100;
  const warm = [1, .91, .7], halo = [1, .63, .3], hitColor = [.68, 1, .9], hurtColor = [1, .49, .35];

  class FlightEffects {
    static limits = limits;
    constructor() { this.clear(); }
    clear({keepSeen = false} = {}) {
      const seen = keepSeen ? this.seen : null;
      this.shots = [];
      this.previews = [];
      this.impacts = [];
      this.seen = seen || new Map();
      this.feedback = null;
      this.noticeAt = -Infinity;
      this.noticePriority = -1;
      this.kickAt = this.flashAt = -Infinity;
      this.kickStrength = this.flashStrength = 0;
    }
    prune(now) {
      const retain = (items, lifetime) => {
        let count = 0;
        for (const item of items) if (now - item.at < lifetime(item)) items[count++] = item;
        items.length = count;
      };
      retain(this.shots, () => shotMs);
      retain(this.previews, () => 900);
      retain(this.impacts, impact => impact.death ? deathMs : hitMs);
      for (const [id, time] of this.seen) if (now - time >= limits.seenMs) this.seen.delete(id);
      if (this.feedback && now >= this.feedback.until) this.feedback = null;
    }
    notice(kind, text, time, until, priority) {
      if (time < this.noticeAt || time === this.noticeAt && priority < this.noticePriority) return;
      this.noticeAt = time; this.noticePriority = priority;
      this.feedback = {kind, text, until};
    }
    pulse(now, reduced, strength = 1) {
      this.flashAt = now; this.flashStrength = reduced ? 0 : strength;
      this.kickAt = now; this.kickStrength = reduced ? 0 : strength;
    }
    fire({position, yaw, pitch, now, reduced = false} = {}) {
      if (!validPoint(position) || !Number.isFinite(yaw) || !Number.isFinite(pitch) || !validTime(now)) return false;
      this.prune(now);
      const axes = basis(yaw, pitch), origin = [...position];
      const shot = {origin, end:at(origin, axes.forward, 60), axes, at:now, flashAt:now, local:true, reduced};
      this.shots.push(shot); this.previews.push(shot);
      if (this.shots.length > limits.shots) this.shots.shift();
      if (this.previews.length > limits.shots) this.previews.shift();
      this.pulse(now, reduced);
      this.notice('intent', '本地开火', now, now + 700, 0);
      return true;
    }
    /* Call only for events received from the authoritative server stream.
       hit/death.player_id is the victim; target_id is the shooter. */
    confirmed(event, ownId, now, {reduced = false} = {}) {
      if (!event || !['laser', 'hit', 'death'].includes(event.type) ||
          !['string', 'number'].includes(typeof event.id) || !validTime(event.at_ms) ||
          !validTime(now) || now < event.at_ms || now - event.at_ms >= 900 ||
          !validPoint(event.position) || event.type === 'laser' && !validPoint(event.end)) return false;
      const id = typeof event.id + ':' + event.id;
      this.prune(now);
      if (this.seen.has(id)) return false;
      this.seen.set(id, event.at_ms);
      if (this.seen.size > limits.seen) this.seen.delete(this.seen.keys().next().value);
      const own = ownId != null && event.player_id === ownId;
      if (event.type === 'laser') {
        // Confirmation replaces one recent own preview; latency never flashes it twice.
        let preview = -1, nearest = Infinity;
        if (own) for (let index = 0; index < this.previews.length; index++) {
          const shot = this.previews[index], delta = Math.abs(shot.at - event.at_ms);
          if (shot.local && shot.at <= event.at_ms && delta <= 600 && delta < nearest &&
              Math.hypot(...shot.origin.map((value, axis) => value - event.position[axis])) < 12) {
            preview = index; nearest = delta;
          }
        }
        const local = preview >= 0 ? this.previews.splice(preview, 1)[0] : null;
        if (local) {
          const index = this.shots.indexOf(local);
          if (index >= 0) this.shots.splice(index, 1);
        }
        const origin = [...event.position], end = [...event.end], axes = shotBasis(unit(end.map((value, axis) => value - origin[axis])));
        if (now - event.at_ms < shotMs) {
          this.shots.push({origin, end, axes, at:event.at_ms, flashAt:local ? local.flashAt : event.at_ms, local:false, reduced});
          if (this.shots.length > limits.shots) this.shots.shift();
        }
        if (own) {
          if (!local && now - event.at_ms < 160) this.pulse(event.at_ms, reduced);
          this.notice('fired', '已发射', event.at_ms, event.at_ms + 700, 1);
        }
      } else {
        const death = event.type === 'death';
        this.impacts.push({position:[...event.position], at:event.at_ms, death, own, reduced});
        if (this.impacts.length > limits.impacts) this.impacts.shift();
        if (own) {
          this.notice(death ? 'destroyed' : 'damaged', death ? '飞船被击毁' : '受到攻击', event.at_ms, event.at_ms + 1400, death ? 5 : 3);
          if (now - event.at_ms < 160) this.pulse(event.at_ms, reduced, death ? .85 : .55);
        } else if (ownId != null && event.target_id === ownId) {
          this.notice(death ? 'kill' : 'hit', death ? '击毁确认' : '命中确认', event.at_ms, event.at_ms + 1400, death ? 4 : 3);
        }
      }
      return true;
    }
    sample(now) {
      this.prune(now);
      const fade = (time, duration) => now >= time ? clamp(1 - (now - time) / duration, 0, 1) : 0;
      const kick = this.kickStrength * Math.pow(fade(this.kickAt, 180), 2);
      return {feedback:this.feedback, kick, flash:this.flashStrength * fade(this.flashAt, 140),
        active:!!(this.shots.length || this.impacts.length || this.feedback || kick)};
    }
    geometry(now) {
      this.prune(now);
      const geometry = {lines:[], points:[], triangles:[], solids:[]};
      const vertex = (array, point, color, alpha, size = 1) => array.push(...point, ...color, alpha, size);
      const line = (a, b, color, alpha) => {vertex(geometry.lines, a, color, alpha); vertex(geometry.lines, b, color, alpha);};
      const point = (p, color, alpha, size) => vertex(geometry.points, p, color, alpha, size);
      const ribbon = (start, end, axis, width, color, alpha) => {
        const corners = [at(start, axis, -width), at(start, axis, width), at(end, axis, -width), at(end, axis, width)];
        for (const index of [0,1,2,1,3,2]) vertex(geometry.triangles, corners[index], color, alpha);
      };
      for (const shot of this.shots) {
        const age = now - shot.at;
        if (age < 0 || age >= shotMs) continue;
        const {axes, origin, end, reduced} = shot, length = Math.hypot(...end.map((value, axis) => value - origin[axis]));
        const muzzleDistance = Math.min(3.6, length), muzzle = at(origin, axes.forward, muzzleDistance);
        const alpha = .9 * (1 - age / shotMs), travel = reduced ? 1 : clamp(.08 + age / 165, 0, 1);
        const headDistance = muzzleDistance + Math.max(0, length - muzzleDistance) * travel;
        const head = at(origin, axes.forward, headDistance);
        const tail = at(origin, axes.forward, Math.max(muzzleDistance, headDistance - (reduced ? length : 18)));
        // A narrow bright core and two crossed translucent ribbons remain readable from either side.
        line(muzzle, head, warm, alpha * .4); line(tail, head, warm, alpha);
        for (const axis of [axes.right, axes.up]) ribbon(tail, head, axis, .2, halo, alpha * .3);
        point(head, warm, alpha, .95);
        const flashAge = now - shot.flashAt;
        if (flashAge >= 0 && flashAge < 120) {
          const flash = 1 - flashAge / 120, radius = reduced ? .65 : .55 + .85 * flash;
          for (let index = 0; index < (reduced ? 4 : 6); index++) {
            const angle = index / (reduced ? 4 : 6) * TAU;
            const ray = axes.right.map((value, axis) => value * Math.cos(angle) + axes.up[axis] * Math.sin(angle));
            line(at(muzzle, ray, radius * .3), at(muzzle, ray, radius), warm, flash * .9);
          }
          point(muzzle, warm, flash * .85, reduced ? .75 : 1.45);
        }
      }
      for (const impact of this.impacts) {
        const age = now - impact.at, duration = impact.death ? deathMs : hitMs;
        if (age < 0 || age >= duration) continue;
        const progress = age / duration, alpha = .9 * (1 - progress), count = impact.reduced ? 4 : impact.death ? 18 : 10;
        const color = impact.own ? hurtColor : hitColor, radius = impact.reduced ? .8 : (impact.death ? 10 : 4) * (.15 + progress);
        for (let index = 0; index < count; index++) {
          const y = 1 - 2 * (index + .5) / count, angle = index * 2.399963229728653;
          const horizontal = Math.sqrt(1 - y*y), direction = [horizontal*Math.cos(angle), y, horizontal*Math.sin(angle)];
          const tip = at(impact.position, direction, radius), tail = at(impact.position, direction, radius * .58);
          line(tail, tip, color, alpha); point(tip, color, alpha, impact.death ? .48 : .32);
        }
        point(impact.position, color, alpha * .7, impact.reduced ? .7 : impact.death ? 2.6 : 1.35);
      }
      return geometry;
    }
  }
  globalThis.MSGFlightEffects = FlightEffects;
})();
