/* Same-origin, server-authoritative flight transport. No identity or physics in inputs. */
(() => {
  'use strict';

  const VERSION = 1;
  const TICKET_KEY = 'msg-flight-resume-v1';
  const INPUT_MS = 50;
  const HELLO_MS = 8000;
  const SNAPSHOT_MS = 5000;
  const RETRY_MS = [500, 1000, 2000, 4000, 8000];
  const ACTIONS = new Set(['laser', 'shield', 'dash']);
  const INPUT_FIELDS = new Set(['throttle', 'strafe', 'lift', 'yaw', 'pitch', 'brake', 'actions']);
  const EVENT_TYPES = new Set(['laser', 'hit', 'shield', 'dash', 'death', 'respawn', 'region', 'collect']);
  const MAX_TIME = 8.64e15;
  const MAX_FRAME = 262144;
  const noop = () => {};
  const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const finite = (value, min, max) => Number.isFinite(value) && value >= min && value <= max;
  const integer = (value, min, max = Number.MAX_SAFE_INTEGER) =>
    Number.isSafeInteger(value) && value >= min && value <= max;
  const region = value => integer(value, 0, 18);
  const time = value => finite(value, 0, MAX_TIME);
  const text = (value, max) => typeof value === 'string' && value.length > 0 &&
    value.length <= max && !/[\u0000-\u001f\u007f]/.test(value);
  const id = value => text(value, 128) && /^[A-Za-z0-9_-]+$/.test(value);
  const ticket = value => typeof value === 'string' && /^[A-Za-z0-9._~-]{16,2048}$/.test(value);
  const vector = (value, max) => Array.isArray(value) && value.length === 3 &&
    value.every(component => finite(component, -max, max));
  const frozenVector = value => Object.freeze([...value]);
  const clock = () => globalThis.performance?.now?.() ?? Date.now();
  const neutral = (yaw = 0, pitch = 0) =>
    ({ throttle: 0, strafe: 0, lift: 0, yaw, pitch, brake: false, actions: [] });
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
    if (!object(value) || Object.keys(value).some(key => !['id', 'position', 'radius', ...(well ? ['influence'] : [])].includes(key)) ||
        !text(value.id, 160) || !/^[A-Za-z0-9_-]+$/.test(value.id) || !vector(value.position, 400) ||
        Math.hypot(...value.position) > 400.000001 || !finite(value.radius, 1, 6) ||
        (well && value.influence !== Math.max(36, value.radius * 8))) return null;
    return Object.freeze({ id: value.id, position: frozenVector(value.position), radius: value.radius,
      ...(well ? { influence: value.influence } : {}) });
  }
  function gravity(value) {
    if (!object(value) || value.version !== 1 || !Array.isArray(value.wells) || value.wells.length > 256) return null;
    const wells = value.wells.map(item => body(item, true));
    if (wells.some(item => !item) || new Set(wells.map(item => item.id)).size !== wells.length) return null;
    return Object.freeze({ version: 1, wells: Object.freeze(wells) });
  }
  function fieldGeometry(value) {
    return JSON.stringify([value.version, value.seed, value.count, value.radius, value.fuel,
      value.respawn_ms, value.layout_version, value.anchors]);
  }
  function collectibles(value, hello = false, previous = null) {
    if (!object(value) || ![1, 2].includes(value.version) || !integer(value.revision, 0)) return null;
    const mask = collectibleMask(value.taken);
    if (!mask) return null;
    const result = { version: value.version, revision: value.revision, mask };
    if (hello || value.version === 2 || value.seed !== undefined) {
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

  function ship(value, extent = 480, own = false) {
    if (!object(value) || !id(value.id) || value.id.length > 64 || !text(value.handle, 160) ||
        typeof value.guest !== 'boolean' ||
        (value.subject_id !== null && (!text(value.subject_id, 160) || value.guest)) ||
        !vector(value.position, extent) || !vector(value.velocity, 60.001) ||
        !finite(value.yaw, -Math.PI, Math.PI) || !finite(value.pitch, -Math.PI / 2, Math.PI / 2) ||
        !finite(value.hp, 0, 100) || !finite(value.fuel, 0, 100) || !region(value.region) ||
        !integer(value.ack_seq, -1) ||
        !['shield_until_ms', 'laser_ready_ms', 'shield_ready_ms', 'dash_ready_ms', 'respawn_at_ms']
          .every(key => time(value[key])) ||
        (value.region_ready_ms !== undefined && !time(value.region_ready_ms)) ||
        (value.score !== undefined && !integer(value.score, 0)) ||
        (value.collected !== undefined && !integer(value.collected, 0)) ||
        (value.dash_until_ms !== undefined && !time(value.dash_until_ms)) ||
        (!own && (value.home_position !== undefined || value.home_body !== undefined))) return null;
    const result = {
      id: value.id, subject_id: value.subject_id, handle: value.handle, guest: value.guest,
      position: frozenVector(value.position), velocity: frozenVector(value.velocity),
      yaw: value.yaw, pitch: value.pitch, hp: value.hp, fuel: value.fuel,
      shield_until_ms: value.shield_until_ms, laser_ready_ms: value.laser_ready_ms,
      shield_ready_ms: value.shield_ready_ms, dash_ready_ms: value.dash_ready_ms,
      respawn_at_ms: value.respawn_at_ms, region: value.region, ack_seq: value.ack_seq,
    };
    if (value.region_ready_ms !== undefined) result.region_ready_ms = value.region_ready_ms;
    if (value.score !== undefined) result.score = value.score;
    if (value.collected !== undefined) result.collected = value.collected;
    if (value.dash_until_ms !== undefined) result.dash_until_ms = value.dash_until_ms;
    if (own && value.home_position !== undefined) {
      if (!vector(value.home_position, extent)) return null;
      result.home_position = frozenVector(value.home_position);
    }
    if (own && value.home_body !== undefined) {
      const home = value.home_body;
      if (home === null) result.home_body = null;
      else {
        if (!object(home) || home.id !== 'flight_home_' + value.id ||
            !vector(home.position, extent) || !finite(home.radius, 1, 100) ||
            home.private !== true || !text(home.title, 128)) return null;
        result.home_body = Object.freeze({ id: home.id, position: frozenVector(home.position),
          radius: home.radius, private: true, title: home.title });
      }
    }
    return Object.freeze(result);
  }

  function event(value, extent, laserRange) {
    const validId = object(value) && (integer(value.id, 0) ||
      (typeof value.id === 'string' && /^evt_[1-9][0-9]{0,15}$/.test(value.id) &&
        Number.isSafeInteger(Number(value.id.slice(4)))));
    if (!validId || !EVENT_TYPES.has(value.type) ||
        !id(value.player_id) || !time(value.at_ms) ||
        (value.region !== undefined && !region(value.region)) ||
        (value.target_id !== undefined && !id(value.target_id)) ||
        (value.position !== undefined && !vector(value.position, value.type === 'laser' ? extent : 100000)) ||
        (value.end !== undefined && !vector(value.end, value.type === 'laser' ? extent + laserRange : 100000))) return null;
    // A ray can leave the world, but its origin and negotiated Euclidean length cannot.
    if (value.type === 'laser' && (!value.position || !value.end ||
        Math.hypot(...value.end.map((component, axis) => component - value.position[axis])) > laserRange + 1e-6)) return null;
    const result = { id: value.id, type: value.type, player_id: value.player_id, at_ms: value.at_ms };
    if (value.target_id !== undefined) result.target_id = value.target_id;
    if (value.position !== undefined) result.position = frozenVector(value.position);
    if (value.end !== undefined) result.end = frozenVector(value.end);
    if (value.region !== undefined) result.region = value.region;
    if (value.type === 'collect') {
      if (!Array.isArray(value.glyph_ids) || !value.glyph_ids.length || value.glyph_ids.length > 32 ||
          value.glyph_ids.some(glyph => !integer(glyph, 0, 2299)) || new Set(value.glyph_ids).size !== value.glyph_ids.length ||
          !finite(value.fuel_added, 0, 128) || !integer(value.collected, value.glyph_ids.length)) return null;
      result.glyph_ids = Object.freeze([...value.glyph_ids]); result.fuel_added = value.fuel_added; result.collected = value.collected;
    }
    return Object.freeze(result);
  }

  class Client {
    constructor(callbacks = {}) {
      this._callbacks = Object.fromEntries(['onStatus', 'onSnapshot', 'onHello', 'onError']
        .map(key => [key, typeof callbacks[key] === 'function' ? callbacks[key] : noop]));
      this._socket = null;
      this._opening = false;
      this._wanted = false;
      this._localSession = false;
      this._suspended = false;
      this._hello = null;
      this._lastHello = null;
      this._snapshot = null;
      this._self = null;
      this._anchor = null;
      this._seq = -1;
      this._retryCount = 0;
      this._input = neutral();
      this._heldActions = new Set();
      this._pendingActions = new Set();
      this._ticket = this._readTicket();
      this._onBlur = () => this.suspend();
      this._onVisibility = () => {
        if (this._hidden()) this.suspend();
        else if (this._wanted && !this._socket && !this._retryTimer) this._retry();
      };
      globalThis.addEventListener?.('blur', this._onBlur);
      globalThis.document?.addEventListener('visibilitychange', this._onVisibility);
      globalThis.addEventListener?.('pagehide', () => this.disconnect());
    }

    get connected() { return Boolean(this._hello && this._socket?.readyState === 1); }
    // Local play may start before hello; self remains server-validated data or null.
    get localSession() { return this._localSession; }
    get canPredict() { return Boolean(this.localSession && (!this._self || this._self.hp > 0) && !this._suspended && !this._hidden()); }
    get reconnecting() { return Boolean(this._wanted && !this.connected && this._retryCount); }
    get self() { return this._self; }
    get limits() { return this._hello?.limits ?? this._lastHello?.limits ?? {}; }
    get snapshot() { return this._snapshot; }
    get gravity() { return this._snapshot?.gravity ?? this._hello?.gravity ?? this._lastHello?.gravity ?? null; }
    get serverNow() {
      return this._anchor ? this._anchor.server + Math.max(0, clock() - this._anchor.local) : Date.now();
    }

    _notify(key, ...args) {
      try { this._callbacks[key](...args); } catch (_) { /* View errors cannot mutate the transport. */ }
    }

    _status(state, message) { this._notify('onStatus', state, message); }
    _hidden() { return globalThis.document?.visibilityState === 'hidden' || globalThis.document?.hidden === true; }
    _readTicket() {
      try {
        const value = globalThis.sessionStorage?.getItem(TICKET_KEY);
        return ticket(value) ? value : null;
      } catch (_) { return null; }
    }
    _saveTicket(value) {
      this._ticket = value;
      try { globalThis.sessionStorage?.setItem(TICKET_KEY, value); } catch (_) { /* RAM resume still works. */ }
    }
    _forgetTicket() {
      this._ticket = null;
      try { globalThis.sessionStorage?.removeItem(TICKET_KEY); } catch (_) { /* Storage can be disabled. */ }
    }

    connect() {
      if (this._wanted) return;
      this._localSession = true;
      this._wanted = true;
      this._retryCount = 0;
      this._suspended = this._hidden();
      this._clearRetry();
      if (this._suspended) { this._status('suspended', 'Flight paused while the page is hidden.'); return; }
      this._open();
    }

    disconnect() {
      this._localSession = false;
      this._wanted = false;
      this._clearRetry();
      this.clearInput();
      this._close(1000, 'Flight ended');
      this._status('disconnected', 'Flight disconnected.');
    }

    suspend() {
      this.clearInput();
      this._suspended = true;
      this._stopInputs();
      if (this._hidden()) this._clearRetry();
      if (this._wanted) this._status('suspended', 'Focus the flight canvas to resume.');
    }

    resume() {
      if (!this._localSession || this._hidden()) return false;
      this._suspended = false;
      this.clearInput();
      if (!this._wanted) return true; // Local play never reopens a rejected transport.
      if (!this._socket && !this._retryTimer) this._open();
      else if (this.connected) {
        this._startInputs();
        this._status('connected', 'Live flight connected.');
      }
      return true;
    }

    clearInput() {
      this._input = neutral(this._self?.yaw ?? this._input.yaw, this._self?.pitch ?? this._input.pitch);
      this._heldActions.clear();
      this._pendingActions.clear();
      // One neutral frame stops server-side held controls, including on blur/hide.
      if (this.connected) this._sendInput(true);
    }

    setInput(value) {
      if (!this.connected || this._suspended || this._hidden()) return false;
      if (!object(value) || Object.keys(value).some(key => !INPUT_FIELDS.has(key)) ||
          ['throttle', 'strafe', 'lift', 'yaw', 'pitch'].some(key =>
            value[key] !== undefined && !Number.isFinite(value[key])) ||
          (value.brake !== undefined && typeof value.brake !== 'boolean') ||
          (value.actions !== undefined && (!Array.isArray(value.actions) || value.actions.length > 8 ||
            value.actions.some(action => !ACTIONS.has(action))))) {
        this.clearInput();
        return false;
      }
      const actions = new Set(value.actions ?? []);
      for (const action of actions) {
        if (!this._heldActions.has(action)) this._pendingActions.add(action);
      }
      this._heldActions = actions;
      const clamp = value => Math.max(-1, Math.min(1, value ?? 0));
      const yaw = value.yaw ?? this._input.yaw;
      this._input = {
        throttle: clamp(value.throttle), strafe: clamp(value.strafe), lift: clamp(value.lift),
        yaw: Math.atan2(Math.sin(yaw), Math.cos(yaw)),
        pitch: Math.max(-1.35, Math.min(1.35, value.pitch ?? this._input.pitch)),
        brake: value.brake ?? false, actions: actions.has('laser') ? ['laser'] : [],
      };
      return true;
    }

    chooseRegion(value) {
      if (!this.connected || this._hidden() || !region(value) ||
          !this._hello.regions.some(item => item.id === value)) return false;
      this.clearInput();
      return this._send({ v: VERSION, type: 'region', region: value });
    }

    _open() {
      if (!this._wanted || this._hidden() || this._socket || this._opening) return;
      const location = globalThis.location;
      if (!location || !['http:', 'https:'].includes(location.protocol) || !location.host ||
          typeof globalThis.WebSocket !== 'function') {
        this._wanted = false;
        this._close(1000, 'Flight unavailable');
        this._status('failed', 'Live flight is unavailable in this browser.');
        return;
      }
      this._opening = true;
      this._status(this._retryCount ? 'reconnecting' : 'connecting', 'Connecting to live flight…');
      if (!this._wanted || this._hidden()) { this._opening = false; return; }
      let socket;
      try { socket = new globalThis.WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/_flight`); }
      catch (_) { this._opening = false; this._retry(); return; }
      this._socket = socket;
      this._opening = false;
      socket.onopen = () => {
        if (socket !== this._socket || !this._wanted) return;
        if (this._hidden()) {
          this._close(1000, 'Flight suspended', true);
          this._status('suspended', 'Focus the flight canvas to reconnect.');
          return;
        }
        const frame = { v: VERSION, type: 'join' };
        if (this._ticket) frame.resume = this._ticket;
        this._send(frame);
      };
      socket.onmessage = message => { if (socket === this._socket) this._receive(message.data); };
      socket.onerror = () => { if (socket === this._socket) this._status('reconnecting', 'Flight connection interrupted.'); };
      socket.onclose = message => this._lost(socket, message.code);
      // Cover DNS/TCP/TLS stalls as well as a missing application hello.
      this._helloTimer = globalThis.setTimeout(() => {
        if (socket === this._socket && !this._hello) this._lost(socket, 1006);
      }, HELLO_MS);
    }

    _send(frame) {
      if (this._socket?.readyState !== 1) return false;
      // Do not accumulate movement or combat frames behind a stalled connection.
      if ((this._socket.bufferedAmount ?? 0) > 16384) {
        this._lost(this._socket, 1006);
        return false;
      }
      try { this._socket.send(JSON.stringify(frame)); return true; }
      catch (_) { this._lost(this._socket, 1006); return false; }
    }

    _sendInput(force = false) {
      if (!this.connected || (!force && (this._suspended || this._hidden()))) return;
      if (this._seq >= Number.MAX_SAFE_INTEGER) { this._badFrame(); return; }
      const actions = force ? [] : [...new Set([...this._input.actions, ...this._pendingActions])];
      this._pendingActions.clear();
      this._send({ v: VERSION, type: 'input', seq: ++this._seq,
        throttle: this._input.throttle, strafe: this._input.strafe, lift: this._input.lift,
        yaw: this._input.yaw, pitch: this._input.pitch, brake: this._input.brake, actions });
    }

    _startInputs() {
      this._stopInputs();
      if (this.connected && !this._suspended && !this._hidden()) {
        this._inputTimer = globalThis.setInterval(() => this._sendInput(), INPUT_MS);
        this._watchSnapshot();
      }
    }
    _stopInputs() {
      globalThis.clearInterval(this._inputTimer);
      globalThis.clearTimeout(this._snapshotTimer);
      this._inputTimer = this._snapshotTimer = null;
    }
    _watchSnapshot() {
      globalThis.clearTimeout(this._snapshotTimer);
      this._snapshotTimer = null;
      if (!this.connected || this._suspended || this._hidden()) return;
      const socket = this._socket;
      this._snapshotTimer = globalThis.setTimeout(() => {
        if (socket === this._socket && !this._suspended && !this._hidden()) this._lost(socket, 1006);
      }, SNAPSHOT_MS);
    }
    _clearRetry() { globalThis.clearTimeout(this._retryTimer); this._retryTimer = null; }

    _close(code, reason, retainSession = false) {
      const socket = this._socket;
      this._socket = null;
      this._stopInputs();
      globalThis.clearTimeout(this._helloTimer);
      globalThis.clearTimeout(this._stableTimer);
      this._helloTimer = this._stableTimer = null;
      this._hello = null;
      if (!retainSession) this._lastHello = this._snapshot = this._self = this._anchor = null;
      this._input = neutral();
      this._heldActions.clear();
      this._pendingActions.clear();
      if (socket) {
        socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null;
        try { socket.close(code, reason); } catch (_) { /* Already closed. */ }
      }
    }

    _lost(socket, code) {
      if (socket !== this._socket) return;
      const rejected = [1002, 1003, 1008, 1009, 4013].includes(code);
      this._close(1000, 'Flight reconnect', this._wanted && !rejected);
      if (!this._wanted) return;
      if (rejected) {
        this._wanted = false;
        if ([1008, 4013].includes(code)) this._forgetTicket();
        this._status('failed', code === 4013 ? 'Your login expired. Sign in again to re-enter flight.' :
          'Flight connection was rejected. Re-enter flight to try again.');
        return;
      }
      if (this._hidden()) {
        this._suspended = true;
        this._status('suspended', 'Focus the flight canvas to reconnect.');
        return;
      }
      this._retry();
    }

    _retry() {
      this._clearRetry();
      if (!this._wanted || this._hidden()) return;
      // Cap the delay, not the retry lifetime: a temporary outage must not strand the flight.
      const delay = RETRY_MS[Math.min(this._retryCount, RETRY_MS.length - 1)];
      this._retryCount = Math.min(this._retryCount + 1, RETRY_MS.length);
      this._retryTimer = globalThis.setTimeout(() => { this._retryTimer = null; this._open(); }, delay);
      this._status('reconnecting', 'Flight disconnected; reconnecting…');
    }

    _badFrame() {
      this._wanted = false;
      this._clearRetry();
      this._close(1002, 'Invalid flight frame');
      this._notify('onError', 'Invalid flight data. Re-enter flight to reconnect.');
      this._status('failed', 'Flight data could not be verified.');
    }

    _receive(raw) {
      if (typeof raw !== 'string' || raw.length > MAX_FRAME) { this._badFrame(); return; }
      let value;
      try { value = JSON.parse(raw); } catch (_) { this._badFrame(); return; }
      if (!object(value) || value.v !== VERSION || typeof value.type !== 'string') { this._badFrame(); return; }
      if (value.type === 'hello') this._receiveHello(value);
      else if (value.type === 'snapshot') this._receiveSnapshot(value);
      else if (value.type === 'error' && text(value.message, 512)) this._notify('onError', value.message);
      else if (value.type === 'error' && typeof value.code === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(value.code)) {
        const messages = {
          region_unavailable: '区域暂不可用，请检查燃料、区域冷却和当前状态。',
          invalid_input: 'Flight controls were rejected by the server.',
        };
        this._notify('onError', messages[value.code] ?? 'Flight request was rejected by the server.');
      }
      else this._badFrame();
    }

    _receiveHello(value) {
      if (!object(value.limits) || !time(value.server_time_ms) ||
          (value.limits.world_extent !== undefined && !finite(value.limits.world_extent, 1, 100000)) ||
          (value.limits.laser_range !== undefined && !finite(value.limits.laser_range, 1, 1000))) {
        this._badFrame(); return;
      }
      const self = ship(value.self, value.limits.world_extent ?? 480, true);
      if (this._hello || !self || !ticket(value.resume) || !integer(value.tick_hz, 1, 60) ||
          !region(value.region) || value.region !== self.region || !object(value.limits) ||
          !Array.isArray(value.regions) || !value.regions.length || value.regions.length > 19) {
        this._badFrame(); return;
      }
      const regions = [], seen = new Set();
      for (const item of value.regions) {
        if (!object(item) || !region(item.id) || seen.has(item.id) || !text(item.name, 80) ||
            !vector(item.center, 100000) || !finite(item.radius, 1, 100000)) { this._badFrame(); return; }
        seen.add(item.id);
        regions.push(Object.freeze({ id: item.id, name: item.name,
          center: frozenVector(item.center), radius: item.radius }));
      }
      if (!seen.has(self.region)) { this._badFrame(); return; }
      // Limits are numeric metadata only; never retain arbitrary nested server payloads.
      const limits = {};
      for (const [key, limit] of Object.entries(value.limits)) {
        if (!/^[a-z][a-z0-9_]{0,63}$/.test(key) || !finite(limit, 0, 1e9)) { this._badFrame(); return; }
        limits[key] = limit;
      }
      // Old servers used 60u rays. Views consume the same normalized range as validation.
      limits.laser_range = value.limits.laser_range ?? 60;
      const field = value.collectibles === undefined ? null : collectibles(value.collectibles, true);
      if (value.collectibles !== undefined && !field) { this._badFrame(); return; }
      const wells = value.gravity === undefined ? null : gravity(value.gravity);
      if (value.gravity !== undefined && !wells) { this._badFrame(); return; }
      this._hello = Object.freeze({ v: VERSION, type: 'hello', self, resume: value.resume,
        server_time_ms: value.server_time_ms,
        tick_hz: value.tick_hz, region: value.region,
        regions: Object.freeze(regions), limits: Object.freeze(limits), ...(field ? { collectibles: field } : {}),
        ...(wells ? { gravity: wells } : {}) });
      this._lastHello = this._hello;
      // Hello starts a fresh authoritative stream, including after a server restart.
      this._snapshot = null;
      this._self = self;
      this._anchor = { server: value.server_time_ms, local: clock() };
      this._seq = Math.max(this._seq, self.ack_seq);
      this._input = neutral(self.yaw, self.pitch);
      this._saveTicket(value.resume);
      globalThis.clearTimeout(this._helloTimer);
      this._helloTimer = null;
      this._stableTimer = globalThis.setTimeout(() => { this._retryCount = 0; }, 30000);
      this._startInputs();
      this._status(this._suspended ? 'suspended' : 'connected', this._suspended ?
        'Focus the flight canvas to resume.' : 'Live flight connected.');
      this._notify('onHello', this._hello);
    }

    _receiveSnapshot(value) {
      if (!this._hello || !integer(value.tick, 0) || !time(value.server_time_ms) ||
          value.self_id !== this._hello.self.id || !region(value.region) ||
          !this._hello.regions.some(item => item.id === value.region) ||
          !Array.isArray(value.players) || !value.players.length || value.players.length > 96 ||
          !Array.isArray(value.events) || value.events.length > 256 ||
          !integer(value.total_players, value.players.length, 96) ||
          value.total_players !== value.players.length) { this._badFrame(); return; }
      const players = [], seen = new Set();
      let self;
      for (const candidate of value.players) {
        const player = ship(candidate, this._hello.limits.world_extent ?? 480);
        if (!player || seen.has(player.id) || !this._hello.regions.some(item => item.id === player.region)) {
          this._badFrame(); return;
        }
        seen.add(player.id);
        players.push(player);
        if (player.id === value.self_id) self = player;
      }
      if (!self || self.region !== value.region || self.subject_id !== this._hello.self.subject_id ||
          self.guest !== this._hello.self.guest) {
        this._badFrame(); return;
      }
      const events = [], eventIds = new Set();
      for (const candidate of value.events) {
        const item = event(candidate, this._hello.limits.world_extent ?? 480, this._hello.limits.laser_range);
        if (!item || eventIds.has(item.id)) { this._badFrame(); return; }
        eventIds.add(item.id); events.push(item);
      }
      const snapshot = {
        v: VERSION, type: 'snapshot', tick: value.tick, server_time_ms: value.server_time_ms,
        self_id: self.id, region: value.region, players: Object.freeze(players),
        events: Object.freeze(events), total_players: value.total_players,
      };
      if (value.state_time_ms !== undefined) {
        if (!time(value.state_time_ms) || value.state_time_ms > value.server_time_ms) { this._badFrame(); return; }
        snapshot.state_time_ms = value.state_time_ms;
      }
      if (value.collectibles !== undefined || this._hello.collectibles) {
        const field = collectibles(value.collectibles, false, this._snapshot?.collectibles ?? this._hello.collectibles);
        if (!this._hello.collectibles || !field) {
          this._badFrame(); return;
        }
        snapshot.collectibles = field;
      }
      if (value.gravity !== undefined || this._hello.gravity) {
        const wells = gravity(value.gravity);
        if (!wells) { this._badFrame(); return; }
        snapshot.gravity = wells;
      }
      if (value.region_counts !== undefined) {
        if (!object(value.region_counts) || Object.keys(value.region_counts).length !== this._hello.regions.length ||
            this._hello.regions.some(item => !integer(value.region_counts[String(item.id)], 0, value.total_players) ||
              value.region_counts[String(item.id)] !== players.filter(player => player.region === item.id).length)) {
          this._badFrame(); return;
        }
        snapshot.region_counts = Object.freeze(Object.fromEntries(this._hello.regions
          .map(item => [String(item.id), value.region_counts[String(item.id)]])));
      }
      // Duplicate/late valid snapshots must not rewind positions or cooldown clocks.
      if (this._snapshot && value.tick <= this._snapshot.tick) return;
      if (value.server_time_ms < (this._snapshot?.server_time_ms ?? this._hello.server_time_ms - 1000) ||
          snapshot.state_time_ms < (this._snapshot?.state_time_ms ?? 0) ||
          snapshot.collectibles && snapshot.collectibles.seed === (this._snapshot?.collectibles?.seed ?? this._hello.collectibles?.seed) &&
            snapshot.collectibles.revision < (this._snapshot?.collectibles?.revision ?? this._hello.collectibles.revision) ||
          self.ack_seq < this._self.ack_seq || self.ack_seq > this._seq) { this._badFrame(); return; }
      this._snapshot = Object.freeze(snapshot);
      // Only hello may provide own private home metadata; never copy it to the world snapshot.
      const home = this._hello.self;
      this._self = Object.freeze({ ...self,
        ...(home.home_position !== undefined ? { home_position: home.home_position } : {}),
        ...(home.home_body !== undefined ? { home_body: home.home_body } : {}),
      });
      // Variable packet transit must not rewind the render/cooldown clock.
      this._anchor = { server: Math.max(this.serverNow, value.server_time_ms), local: clock() };
      this._seq = Math.max(this._seq, self.ack_seq);
      this._watchSnapshot();
      this._notify('onSnapshot', this._snapshot);
    }
  }

  globalThis.MSGFlightClient = Object.freeze({ Client, collectibleMask });
})();
