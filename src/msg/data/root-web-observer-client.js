/* Same-origin, read-only public flight observation. No pilot identity or controls. */
(() => {
  'use strict';

  const VERSION = 1;
  const MAX_FRAME = 196608;
  const MAX_TIME = 8.64e15;
  const HELLO_MS = 8000;
  const SNAPSHOT_MS = 5000;
  const RETRY_MS = [500, 1000, 2000, 4000, 8000];
  const noop = () => {};
  const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const finite = (value, min, max) => Number.isFinite(value) && value >= min && value <= max;
  const integer = (value, min, max = Number.MAX_SAFE_INTEGER) => Number.isSafeInteger(value) && value >= min && value <= max;
  const time = value => finite(value, 0, MAX_TIME);
  const region = value => integer(value, 0, 18);
  const text = (value, max) => typeof value === 'string' && value.length > 0 && value.length <= max &&
    !/[\u0000-\u001f\u007f]/.test(value);
  const id = value => text(value, 128) && /^[A-Za-z0-9_-]+$/.test(value);
  const vector = (value, max) => Array.isArray(value) && value.length === 3 &&
    value.every(component => finite(component, -max, max));
  const frozenVector = value => Object.freeze([...value]);
  const clock = () => globalThis.performance?.now?.() ?? Date.now();
  const fields = (value, allowed, required = allowed) => object(value) &&
    Object.keys(value).every(key => allowed.includes(key)) &&
    required.every(key => Object.prototype.hasOwnProperty.call(value, key));
  function boundedFrame(raw) {
    if (typeof raw !== 'string' || raw.length > MAX_FRAME) return false;
    // The wire limit is UTF-8 bytes, including non-ASCII handles and region names.
    let bytes = 0;
    for (const character of raw) {
      const point = character.codePointAt(0);
      bytes += point < 128 ? 1 : point < 2048 ? 2 : point < 65536 ? 3 : 4;
      if (bytes > MAX_FRAME) return false;
    }
    return true;
  }

  // Same public geometry contract as the pilot transport, without arbitrary metadata.
  function collectibleMask(value, count = 2300) {
    const bytes = Math.ceil(count / 8), alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
    if (typeof value !== 'string' || value.length !== Math.ceil(bytes / 3) * 4 ||
        !/^[A-Za-z0-9+/]+={0,2}$/.test(value)) return null;
    const result = [], raw = value.replace(/=+$/, '');
    let bits = 0, buffer = 0;
    for (const letter of raw) {
      buffer = (buffer << 6) | alphabet.indexOf(letter); bits += 6;
      if (bits >= 8) { bits -= 8; result.push((buffer >>> bits) & 255); }
    }
    if (result.length !== bytes || buffer & ((1 << bits) - 1) ||
        count % 8 && result.at(-1) >>> (count % 8)) return null;
    return Object.freeze(result);
  }
  function body(value, well = false) {
    if (!fields(value, ['id', 'position', 'radius', ...(well ? ['influence'] : [])]) ||
        !text(value.id, 160) || !/^[A-Za-z0-9_-]+$/.test(value.id) || !vector(value.position, 400) ||
        Math.hypot(...value.position) > 400.000001 || !finite(value.radius, 1, 6) ||
        (well && value.influence !== Math.max(36, value.radius * 8))) return null;
    return Object.freeze({ id: value.id, position: frozenVector(value.position), radius: value.radius,
      ...(well ? { influence: value.influence } : {}) });
  }
  function gravity(value) {
    if (!fields(value, ['version', 'wells']) || value.version !== 1 ||
        !Array.isArray(value.wells) || value.wells.length > 256) return null;
    const wells = value.wells.map(item => body(item, true));
    if (wells.some(item => !item) || new Set(wells.map(item => item.id)).size !== wells.length) return null;
    return Object.freeze({ version: 1, wells: Object.freeze(wells) });
  }
  function fieldGeometry(value) {
    return JSON.stringify([value.version, value.seed, value.count, value.radius, value.fuel,
      value.respawn_ms, value.layout_version, value.anchors]);
  }
  function collectibles(value, hello = false, previous = null) {
    const base = ['version', 'revision', 'taken'];
    if (!object(value) || ![1, 2].includes(value.version) || !integer(value.revision, 0)) return null;
    const full = hello || value.version === 2 || value.seed !== undefined;
    const keys = full ? [...base, 'seed', 'count', 'radius', 'fuel', 'respawn_ms',
      ...(value.version === 2 ? ['layout_version', 'anchors'] : [])] : base;
    if (!fields(value, keys)) return null;
    const mask = collectibleMask(value.taken);
    if (!mask) return null;
    const result = { version: value.version, revision: value.revision, mask };
    if (full) {
      if (value.count !== 2300 || typeof value.seed !== 'string' || !/^[a-f0-9]{32}$/.test(value.seed) ||
          !finite(value.radius, .1, 12) || !finite(value.fuel, 0, 100) || !integer(value.respawn_ms, 1000, 3600000)) return null;
      Object.assign(result, { seed: value.seed, count: value.count, radius: value.radius, fuel: value.fuel, respawn_ms: value.respawn_ms });
      if (value.version === 2) {
        if (value.layout_version !== 1 || !Array.isArray(value.anchors) || value.anchors.length < 2 || value.anchors.length > 64) return null;
        const anchors = value.anchors.map(item => body(item));
        if (anchors.some(item => !item) || new Set(anchors.map(item => item.id)).size !== anchors.length) return null;
        Object.assign(result, { layout_version: 1, anchors: Object.freeze(anchors) });
      }
    } else {
      if (!previous || previous.version !== 1) return null;
      Object.assign(result, { seed: previous.seed, count: previous.count, radius: previous.radius,
        fuel: previous.fuel, respawn_ms: previous.respawn_ms });
    }
    if (previous?.seed === result.seed && fieldGeometry(previous) !== fieldGeometry(result)) return null;
    return Object.freeze(result);
  }

  const SHIP_REQUIRED = ['id', 'subject_id', 'handle', 'guest', 'position', 'velocity', 'yaw', 'pitch', 'hp', 'fuel',
    'shield_until_ms', 'laser_ready_ms', 'shield_ready_ms', 'dash_ready_ms', 'respawn_at_ms', 'region', 'ack_seq'];
  const SHIP_FIELDS = [...SHIP_REQUIRED, 'region_ready_ms', 'score', 'collected', 'dash_until_ms'];
  function ship(value) {
    if (!fields(value, SHIP_FIELDS, SHIP_REQUIRED) || !id(value.id) || value.id.length > 64 || !text(value.handle, 160) ||
        typeof value.guest !== 'boolean' ||
        (value.subject_id !== null && (!text(value.subject_id, 160) || value.guest)) ||
        !vector(value.position, 480) || !vector(value.velocity, 60.001) ||
        !finite(value.yaw, -Math.PI, Math.PI) || !finite(value.pitch, -Math.PI / 2, Math.PI / 2) ||
        !finite(value.hp, 0, 100) || !finite(value.fuel, 0, 100) || !region(value.region) || !integer(value.ack_seq, -1) ||
        !['shield_until_ms', 'laser_ready_ms', 'shield_ready_ms', 'dash_ready_ms', 'respawn_at_ms'].every(key => time(value[key])) ||
        (value.region_ready_ms !== undefined && !time(value.region_ready_ms)) ||
        (value.score !== undefined && !integer(value.score, 0)) ||
        (value.collected !== undefined && !integer(value.collected, 0)) ||
        (value.dash_until_ms !== undefined && !time(value.dash_until_ms))) return null;
    const result = {};
    for (const key of SHIP_FIELDS) {
      if (value[key] !== undefined) result[key] = ['position', 'velocity'].includes(key) ? frozenVector(value[key]) : value[key];
    }
    return Object.freeze(result);
  }

  class Client {
    constructor(callbacks = {}) {
      this._callbacks = Object.fromEntries(['onSnapshot', 'onStatus', 'onHello', 'onError']
        .map(key => [key, typeof callbacks[key] === 'function' ? callbacks[key] : noop]));
      this._socket = this._hello = this._snapshot = this._anchor = null;
      this._helloTimer = this._snapshotTimer = this._retryTimer = this._stableTimer = null;
      this._wanted = false;
      this._destroyed = false;
      this._retryCount = 0;
      this._onVisibility = () => {
        if (!this._wanted) return;
        if (this._hidden()) {
          this._clearRetry();
          this._close(1000, 'Observer hidden');
          this._status('suspended', 'Flight observation paused while the page is hidden.');
        } else this._open();
      };
      this._onPageHide = () => this.disconnect();
      globalThis.document?.addEventListener('visibilitychange', this._onVisibility);
      globalThis.addEventListener?.('pagehide', this._onPageHide);
    }

    get connected() { return Boolean(this._hello && this._socket?.readyState === 1); }
    get snapshot() { return this._snapshot; }
    get regions() { return this._hello?.regions ?? null; }
    get serverNow() {
      return this._anchor ? this._anchor.server + Math.max(0, clock() - this._anchor.local) : Date.now();
    }

    _notify(key, ...args) {
      try { this._callbacks[key](...args); } catch (_) { /* A view cannot alter the transport lifecycle. */ }
    }
    _status(state, message) { this._notify('onStatus', state, message); }
    _hidden() { return globalThis.document?.visibilityState === 'hidden' || globalThis.document?.hidden === true; }
    _clearRetry() { globalThis.clearTimeout(this._retryTimer); this._retryTimer = null; }

    connect() {
      if (this._destroyed || this._wanted && (this._socket || this._retryTimer !== null)) return;
      this._wanted = true;
      this._retryCount = 0;
      this._clearRetry();
      if (this._hidden()) { this._status('suspended', 'Flight observation paused while the page is hidden.'); return; }
      this._open();
    }

    disconnect() {
      if (this._destroyed) return;
      this._wanted = false;
      this._clearRetry();
      this._close(1000, 'Observer ended');
      this._status('disconnected', 'Flight observation disconnected.');
    }

    destroy() {
      if (this._destroyed) return;
      this.disconnect();
      this._destroyed = true;
      globalThis.document?.removeEventListener('visibilitychange', this._onVisibility);
      globalThis.removeEventListener?.('pagehide', this._onPageHide);
      for (const key of Object.keys(this._callbacks)) this._callbacks[key] = noop;
    }

    _open() {
      if (!this._wanted || this._destroyed || this._hidden() || this._socket) return;
      const location = globalThis.location;
      if (!location || !['http:', 'https:'].includes(location.protocol) || !location.host ||
          typeof globalThis.WebSocket !== 'function') {
        this._wanted = false;
        this._status('failed', 'Flight observation is unavailable in this browser.');
        return;
      }
      this._status(this._retryCount ? 'reconnecting' : 'connecting', 'Connecting to public flight…');
      if (!this._wanted || this._destroyed || this._hidden()) return;
      let socket;
      try { socket = new globalThis.WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/_flight`); }
      catch (_) { this._retry(); return; }
      this._socket = socket;
      socket.onopen = () => {
        if (socket !== this._socket || !this._wanted) return;
        if (this._hidden()) { this._onVisibility(); return; }
        // This is the only outbound frame. There is deliberately no input API.
        try { socket.send(JSON.stringify({ v: VERSION, type: 'observe' })); }
        catch (_) { this._lost(socket, 1006); }
      };
      socket.onmessage = message => { if (socket === this._socket) this._receive(message.data); };
      socket.onerror = () => { if (socket === this._socket) this._status('reconnecting', 'Flight observation interrupted.'); };
      socket.onclose = message => this._lost(socket, message.code);
      this._helloTimer = globalThis.setTimeout(() => {
        if (socket === this._socket && !this._hello) this._lost(socket, 1006);
      }, HELLO_MS);
    }

    _close(code, reason) {
      const socket = this._socket;
      this._socket = null;
      for (const key of ['_helloTimer', '_snapshotTimer', '_stableTimer']) {
        globalThis.clearTimeout(this[key]); this[key] = null;
      }
      this._hello = this._snapshot = this._anchor = null;
      if (socket) {
        socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null;
        try { socket.close(code, reason); } catch (_) { /* Already closed. */ }
      }
    }

    _lost(socket, code) {
      if (socket !== this._socket) return;
      this._close(1000, 'Observer reconnect');
      if (!this._wanted || this._destroyed) return;
      if ([1002, 1003, 1008, 1009, 4013].includes(code)) {
        this._wanted = false;
        this._status('failed', 'Flight observation was rejected.');
      } else if (this._hidden()) this._status('suspended', 'Flight observation paused while the page is hidden.');
      else this._retry();
    }

    _retry() {
      this._clearRetry();
      if (!this._wanted || this._destroyed || this._hidden()) return;
      if (this._retryCount >= RETRY_MS.length) {
        this._wanted = false;
        this._status('failed', 'Flight observation is unavailable.');
        return;
      }
      const delay = RETRY_MS[this._retryCount++];
      this._status('reconnecting', 'Flight observation disconnected; reconnecting…');
      if (!this._wanted || this._destroyed || this._hidden()) return;
      this._retryTimer = globalThis.setTimeout(() => { this._retryTimer = null; this._open(); }, delay);
    }

    _watchSnapshot() {
      globalThis.clearTimeout(this._snapshotTimer);
      const socket = this._socket;
      this._snapshotTimer = globalThis.setTimeout(() => {
        if (socket === this._socket && !this._hidden()) this._lost(socket, 1006);
      }, SNAPSHOT_MS);
    }

    _badFrame() {
      this._wanted = false;
      this._clearRetry();
      this._close(1002, 'Invalid observer frame');
      this._notify('onError', 'Invalid public flight data.');
      this._status('failed', 'Flight observation data could not be verified.');
    }

    _receive(raw) {
      if (!boundedFrame(raw)) { this._badFrame(); return; }
      let value;
      try { value = JSON.parse(raw); } catch (_) { this._badFrame(); return; }
      if (!object(value) || value.v !== VERSION) { this._badFrame(); return; }
      if (value.type === 'observer_hello') this._receiveHello(value);
      else if (value.type === 'observer_snapshot') this._receiveSnapshot(value);
      else this._badFrame();
    }

    _receiveHello(value) {
      if (this._hello || !fields(value, ['v', 'type', 'regions', 'server_time_ms', 'state_time_ms', 'tick_hz', 'limits', 'gravity', 'collectibles']) ||
          !time(value.server_time_ms) || !time(value.state_time_ms) || value.state_time_ms > value.server_time_ms || value.tick_hz !== 15 ||
          !fields(value.limits, ['max_players', 'max_snapshot_bytes', 'snapshot_hz', 'world_extent']) ||
          value.limits.max_players !== 96 || value.limits.max_snapshot_bytes !== MAX_FRAME ||
          value.limits.snapshot_hz !== 5 || value.limits.world_extent !== 480 ||
          !Array.isArray(value.regions) || value.regions.length !== 19) { this._badFrame(); return; }
      const regions = [], seen = new Set();
      for (const item of value.regions) {
        if (!fields(item, ['id', 'name', 'center', 'radius']) || !region(item.id) || seen.has(item.id) ||
            !text(item.name, 80) || !vector(item.center, 480) || !finite(item.radius, 1, 480)) { this._badFrame(); return; }
        seen.add(item.id);
        regions.push(Object.freeze({ id: item.id, name: item.name, center: frozenVector(item.center), radius: item.radius }));
      }
      const wells = gravity(value.gravity), field = collectibles(value.collectibles, true);
      if (!wells || !field) { this._badFrame(); return; }
      this._hello = Object.freeze({ v: VERSION, type: 'observer_hello', regions: Object.freeze(regions),
        server_time_ms: value.server_time_ms, state_time_ms: value.state_time_ms, tick_hz: 15,
        limits: Object.freeze({ max_players: 96, max_snapshot_bytes: MAX_FRAME, snapshot_hz: 5, world_extent: 480 }),
        gravity: wells, collectibles: field });
      this._anchor = { server: value.server_time_ms, local: clock() };
      globalThis.clearTimeout(this._helloTimer); this._helloTimer = null;
      this._stableTimer = globalThis.setTimeout(() => { this._retryCount = 0; this._stableTimer = null; }, 30000);
      this._watchSnapshot();
      const accepted = this._hello;
      this._status('connected', 'Public flight connected.');
      if (this._hello === accepted) this._notify('onHello', accepted);
    }

    _receiveSnapshot(value) {
      if (!this._hello || !fields(value, ['v', 'type', 'tick', 'server_time_ms', 'state_time_ms', 'players', 'events',
        'total_players', 'region_counts', 'gravity', 'collectibles']) || !integer(value.tick, 0) ||
          !time(value.server_time_ms) || !time(value.state_time_ms) || value.state_time_ms > value.server_time_ms ||
          !Array.isArray(value.players) || value.players.length > 96 ||
          !integer(value.total_players, 0, 96) || value.total_players !== value.players.length ||
          !Array.isArray(value.events) || value.events.length !== 0) { this._badFrame(); return; }
      const players = [], seen = new Set(), counts = Array(19).fill(0);
      for (const candidate of value.players) {
        const player = ship(candidate);
        if (!player || seen.has(player.id)) { this._badFrame(); return; }
        seen.add(player.id); players.push(player); counts[player.region]++;
      }
      const countKeys = Array.from({ length: 19 }, (_, i) => String(i));
      if (!fields(value.region_counts, countKeys) || countKeys.some(key => value.region_counts[key] !== counts[Number(key)])) {
        this._badFrame(); return;
      }
      const previous = this._snapshot?.collectibles ?? this._hello.collectibles;
      const wells = gravity(value.gravity), field = collectibles(value.collectibles, false, previous);
      if (!wells || !field) { this._badFrame(); return; }
      // Valid duplicate/late packets cannot rewind a view or keep a stale stream alive.
      if (this._snapshot && value.tick <= this._snapshot.tick) return;
      if (value.server_time_ms < (this._snapshot?.server_time_ms ?? this._hello.server_time_ms - 1000) ||
          value.state_time_ms < (this._snapshot?.state_time_ms ?? this._hello.state_time_ms) ||
          field.seed === previous.seed && field.revision < previous.revision) { this._badFrame(); return; }
      this._snapshot = Object.freeze({ v: VERSION, type: 'observer_snapshot', tick: value.tick,
        server_time_ms: value.server_time_ms, state_time_ms: value.state_time_ms,
        players: Object.freeze(players), events: Object.freeze([]), total_players: players.length,
        region_counts: Object.freeze(Object.fromEntries(countKeys.map(key => [key, counts[Number(key)]]))),
        gravity: wells, collectibles: field });
      this._anchor = { server: Math.max(this.serverNow, value.server_time_ms), local: clock() };
      this._watchSnapshot();
      this._notify('onSnapshot', this._snapshot);
    }
  }

  globalThis.MSGFlightObserver = Object.freeze({ Client });
})();
