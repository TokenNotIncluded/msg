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
  const EVENT_TYPES = new Set(['laser', 'hit', 'shield', 'dash', 'death', 'respawn', 'region']);
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

  function event(value) {
    const validId = object(value) && (integer(value.id, 0) ||
      (typeof value.id === 'string' && /^evt_[1-9][0-9]{0,15}$/.test(value.id) &&
        Number.isSafeInteger(Number(value.id.slice(4)))));
    if (!validId || !EVENT_TYPES.has(value.type) ||
        !id(value.player_id) || !time(value.at_ms) ||
        (value.region !== undefined && !region(value.region)) ||
        (value.target_id !== undefined && !id(value.target_id)) ||
        (value.position !== undefined && !vector(value.position, 100000)) ||
        (value.end !== undefined && !vector(value.end, 100000))) return null;
    const result = { id: value.id, type: value.type, player_id: value.player_id, at_ms: value.at_ms };
    if (value.target_id !== undefined) result.target_id = value.target_id;
    if (value.position !== undefined) result.position = frozenVector(value.position);
    if (value.end !== undefined) result.end = frozenVector(value.end);
    if (value.region !== undefined) result.region = value.region;
    return Object.freeze(result);
  }

  class Client {
    constructor(callbacks = {}) {
      this._callbacks = Object.fromEntries(['onStatus', 'onSnapshot', 'onHello', 'onError']
        .map(key => [key, typeof callbacks[key] === 'function' ? callbacks[key] : noop]));
      this._socket = null;
      this._wanted = false;
      this._suspended = false;
      this._hello = null;
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
      this._onVisibility = () => { if (this._hidden()) this.suspend(); };
      globalThis.addEventListener?.('blur', this._onBlur);
      globalThis.document?.addEventListener('visibilitychange', this._onVisibility);
      globalThis.addEventListener?.('pagehide', () => this.disconnect());
    }

    get connected() { return Boolean(this._hello && this._socket?.readyState === 1); }
    get self() { return this._self; }
    get snapshot() { return this._snapshot; }
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
      if (this._wanted && this._socket) return;
      this._wanted = true;
      this._retryCount = 0;
      this._suspended = this._hidden();
      this._clearRetry();
      if (this._suspended) { this._status('suspended', 'Flight paused while the page is hidden.'); return; }
      this._open();
    }

    disconnect() {
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
      this._clearRetry();
      if (this._wanted) this._status('suspended', 'Focus the flight canvas to resume.');
    }

    resume() {
      if (!this._wanted || this._hidden()) return false;
      this._suspended = false;
      this.clearInput();
      if (!this._socket) this._open();
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
      if (!this._wanted || this._hidden() || this._suspended || this._socket) return;
      const location = globalThis.location;
      if (!location || !['http:', 'https:'].includes(location.protocol) || !location.host ||
          typeof globalThis.WebSocket !== 'function') {
        this._wanted = false;
        this._status('failed', 'Live flight is unavailable in this browser.');
        return;
      }
      this._status(this._retryCount ? 'reconnecting' : 'connecting', 'Connecting to live flight…');
      let socket;
      try { socket = new globalThis.WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/_flight`); }
      catch (_) { this._retry(); return; }
      this._socket = socket;
      socket.onopen = () => {
        if (socket !== this._socket || !this._wanted) return;
        if (this._hidden() || this._suspended) {
          this._close(1000, 'Flight suspended');
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

    _close(code, reason) {
      const socket = this._socket;
      this._socket = null;
      this._stopInputs();
      globalThis.clearTimeout(this._helloTimer);
      globalThis.clearTimeout(this._stableTimer);
      this._helloTimer = this._stableTimer = null;
      this._hello = this._snapshot = this._self = this._anchor = null;
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
      this._close(1000, 'Flight reconnect');
      if (!this._wanted) return;
      if ([1002, 1003, 1008, 1009, 4013].includes(code)) {
        this._wanted = false;
        if ([1008, 4013].includes(code)) this._forgetTicket();
        this._status('failed', code === 4013 ? 'Your login expired. Sign in again to re-enter flight.' :
          'Flight connection was rejected. Re-enter flight to try again.');
        return;
      }
      if (this._hidden() || this._suspended) {
        this._suspended = true;
        this._status('suspended', 'Focus the flight canvas to reconnect.');
        return;
      }
      this._retry();
    }

    _retry() {
      this._clearRetry();
      if (!this._wanted || this._hidden() || this._suspended) return;
      if (this._retryCount >= RETRY_MS.length) {
        this._wanted = false;
        this._status('failed', 'Flight is unavailable. Re-enter flight to try again.');
        return;
      }
      const delay = RETRY_MS[this._retryCount++];
      this._status('reconnecting', 'Flight disconnected; reconnecting…');
      this._retryTimer = globalThis.setTimeout(() => { this._retryTimer = null; this._open(); }, delay);
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
          (value.limits.world_extent !== undefined && !finite(value.limits.world_extent, 1, 100000))) {
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
      this._hello = Object.freeze({ v: VERSION, type: 'hello', self, resume: value.resume,
        server_time_ms: value.server_time_ms,
        tick_hz: value.tick_hz, region: value.region,
        regions: Object.freeze(regions), limits: Object.freeze(limits) });
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
        const item = event(candidate);
        if (!item || eventIds.has(item.id)) { this._badFrame(); return; }
        eventIds.add(item.id); events.push(item);
      }
      const snapshot = {
        v: VERSION, type: 'snapshot', tick: value.tick, server_time_ms: value.server_time_ms,
        self_id: self.id, region: value.region, players: Object.freeze(players),
        events: Object.freeze(events), total_players: value.total_players,
      };
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
          self.ack_seq < this._self.ack_seq || self.ack_seq > this._seq) { this._badFrame(); return; }
      this._snapshot = Object.freeze(snapshot);
      // Only hello may provide own private home metadata; never copy it to the world snapshot.
      const home = this._hello.self;
      this._self = Object.freeze({ ...self,
        ...(home.home_position !== undefined ? { home_position: home.home_position } : {}),
        ...(home.home_body !== undefined ? { home_body: home.home_body } : {}),
      });
      this._anchor = { server: value.server_time_ms, local: clock() };
      this._seq = Math.max(this._seq, self.ack_seq);
      this._watchSnapshot();
      this._notify('onSnapshot', this._snapshot);
    }
  }

  globalThis.MSGFlightClient = Object.freeze({ Client });
})();
