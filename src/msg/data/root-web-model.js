/* Deterministic geometry and protocol-v1 signing. No network or persistent state. */
(() => {
  "use strict";
  const TAU = Math.PI * 2;
  function handle(name) {
    return "@" + String(name || "").replace(/^\/?@+/, "");
  }
  function random(seed) {
    let h = 2166136261;
    for (const c of seed) h = Math.imul(h ^ c.codePointAt(0), 16777619);
    return () => {
      h += 0x6d2b79f5;
      let t = Math.imul(h ^ (h >>> 15), h | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function position(id) {
    if (id === 'u_root') return [0, 0, 0];
    const r = random(id), radius = 38 + Math.cbrt(r()) * 128;
    const angle = r() * TAU, elevation = (r() - .5) * 1.45;
    return [Math.cos(angle) * Math.cos(elevation) * radius,
      Math.sin(elevation) * radius, Math.sin(angle) * Math.cos(elevation) * radius];
  }
  function satellite(id, center, time = 0) {
    const r = random(id),
      radius = 8 + r() * 19,
      angle = r() * TAU + time * 0.022;
    return [
      center[0] + Math.cos(angle) * radius,
      center[1] + Math.sin(angle * 1.5) * (2 + r() * 6),
      center[2] + Math.sin(angle) * radius,
    ];
  }
  // Versioned visual space, independent of page ordering or account wealth.
  function nebula(count = 1800) {
    const rng = random('msg:token-manifold:v2:not-accounts'), cloud = [];
    for (let i = 0; i < count; i++) {
      const t = rng() * TAU, band = i % 3, spread = 3 + rng() * 14;
      const x = Math.sin(t) * (102 + band * 15);
      const y = Math.sin(t * 2) * 24 + (band - 1) * 17;
      const z = Math.cos(t) * 66 + Math.sin(t * 3) * 18;
      cloud.push(x + (rng() - .5) * spread, y + (rng() - .5) * spread,
        z + (rng() - .5) * spread, .85, .85, .85,
        .12 + rng() * .3, .3 + rng() * .65);
    }
    return cloud;
  }
  const validTime = (value) => typeof value === 'string' ? Date.parse(value) : NaN;
  function reserve(value) {
    const hidden = { known: false, label: 'Not public', fraction: 0 };
    if (!value || !['public', 'self'].includes(value.visibility)) return hidden;
    const { amount_minor: raw, scale, code } = value;
    if (typeof raw !== 'string' || !/^\d{1,19}$/.test(raw) ||
        !Number.isInteger(scale) || scale < 0 || scale > 12 ||
        typeof code !== 'string' || !/^[A-Za-z0-9_-]{1,16}$/.test(code)) return hidden;
    const amount = BigInt(raw);
    if (amount > 9223372036854775807n) return hidden;
    const divisor = 10n ** BigInt(scale);
    const whole = (amount / divisor).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    const decimal = scale ? (amount % divisor).toString().padStart(scale, '0').replace(/0+$/, '') : '';
    // Approximation is exclusively geometric. The amount above stays integer-exact.
    const units = Number(amount) / 10 ** scale;
    return { known: true, label: whole + (decimal ? '.' + decimal : '') + ' ' + code,
      fraction: amount === 0n ? 0 : Math.min(1, .08 + .92 * Math.log10(1 + units) / 4),
      visibility: value.visibility };
  }
  function appearance(node, now = Date.now()) {
    const facts = node.star || {}, root = node.id === 'u_root';
    const checked = validTime(facts.checked_at);
    const fresh = Number.isFinite(checked) && checked <= now + 1000 && now - checked < 90000;
    const certificate = facts.certificate || {};
    const certified = fresh && certificate.state === 'valid' && validTime(certificate.expires_at) > now;
    const presence = facts.presence || {}, reported = validTime(presence.updated_at);
    const live = fresh && presence.self_reported === true && reported <= now &&
      validTime(presence.expires_at) > now && ['available', 'busy', 'away'].includes(presence.state);
    const posted = validTime(facts.last_public_post_at);
    const recency = fresh && posted <= now ? Math.exp(-(now - posted) / 172800000) : 0;
    const light = live ? ({ available: 1, busy: .84, away: .52 })[presence.state] : .30 + .48 * recency;
    const gemColors = [[.93, .77, .49], [.71, .84, .70], [.84, .73, .90], [.91, .65, .52]];
    const tint = gemColors[Math.floor(random(node.id)() * gemColors.length)];
    const balance = facts.balance?.visibility === 'self' && node.kind !== 'private'
      ? null : fresh ? facts.balance : null;
    return {
      root, certified, stale: !fresh,
      radius: root ? 5.4 : certified ? 2.05 : 1.5,
      color: root ? [1, .98, .94] : certified ? tint : [.84, .84, .82],
      light: root ? 1 : light,
      pulse: root ? .035 : live && presence.state !== 'away' ? .045 : 0,
      presenceLabel: live ? presence.state[0].toUpperCase() + presence.state.slice(1) + ' · self-reported' : 'Presence unknown',
      certificateLabel: certified ? 'Valid certificate' : fresh && certificate.state === 'none' ? 'No active public certificate' : 'Certificate status unknown',
      reserve: reserve(balance),
    };
  }
  function graph(users, posts, time, focus) {
    const stars = users.map((user) => ({
      ...user,
      kind: "user",
      position: position(user.id),
    }));
    const index = new Map(stars.map((star) => [star.id, star]));
    const nodes = [...stars],
      links = [];
    for (const post of posts) {
      const star = index.get(post.author?.id);
      if (!star) continue;
      const node = {
        ...post,
        kind: post.reply_to ? "reply" : "post",
        position: satellite(post.id, star.position, time),
      };
      index.set(node.id, node);
      if (focus === star.id || focus === node.id) {
        nodes.push(node);
        links.push([star, node, "orbit"]);
      }
    }
    const pairs = new Set();
    for (const post of posts) {
      const a = index.get(post.id),
        b = index.get(post.reply_to?.id);
      if (!a || !b) continue;
      const from = index.get(a.author?.id),
        to = index.get(b.author?.id);
      if (from && to && from !== to) {
        const pair = [from.id, to.id].sort().join(":");
        if (!pairs.has(pair)) {
          links.push([from, to, "discussion"]);
          pairs.add(pair);
        }
      }
      if (focus === a.author?.id || focus === a.id || focus === b.id) {
        if (!nodes.includes(a)) nodes.push(a);
        if (!nodes.includes(b)) nodes.push(b);
        links.push([a, b, "reply"]);
      }
    }
    return { nodes, links };
  }
  const bytes = (value) => new TextEncoder().encode(value);
  const b64 = (buffer) =>
    btoa(String.fromCharCode(...new Uint8Array(buffer)))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  const unb64 = (value) =>
    Uint8Array.from(
      atob(
        value.replace(/-/g, "+").replace(/_/g, "/") +
          "=".repeat((4 - (value.length % 4)) % 4),
      ),
      (c) => c.charCodeAt(0),
    );
  function canonical(value) {
    if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
    if (value !== null && typeof value === "object")
      return (
        "{" +
        Object.keys(value)
          .sort()
          .map((key) => JSON.stringify(key) + ":" + canonical(value[key]))
          .join(",") +
        "}"
      );
    return JSON.stringify(value);
  }
  async function sha(value) {
    return Array.from(
      new Uint8Array(await crypto.subtle.digest("SHA-256", value)),
      (x) => x.toString(16).padStart(2, "0"),
    ).join("");
  }
  async function importSigner(seed, identity, certificates = []) {
    if (
      !crypto.subtle ||
      seed.length !== 32 ||
      identity.retired_at ||
      identity.subject_id === "u_root"
    ) {
      seed.fill(0);
      throw new Error(
        "Use your active 32-byte identity.key. Root keys cannot be used here.",
      );
    }
    const header = Uint8Array.from([
      48, 46, 2, 1, 0, 48, 5, 6, 3, 43, 101, 112, 4, 34, 4, 32,
    ]);
    const pkcs8 = new Uint8Array(48);
    pkcs8.set(header);
    pkcs8.set(seed, 16);
    try {
      const key = await crypto.subtle.importKey(
        "pkcs8",
        pkcs8,
        "Ed25519",
        false,
        ["sign"],
      );
      const pub = unb64(identity.public_key),
        verifyKey = await crypto.subtle.importKey(
          "raw",
          pub,
          "Ed25519",
          false,
          ["verify"],
        );
      const challenge = crypto.getRandomValues(new Uint8Array(32));
      const proof = await crypto.subtle.sign("Ed25519", key, challenge);
      if (
        !(await crypto.subtle.verify("Ed25519", verifyKey, proof, challenge)) ||
        identity.key_id !== "k_" + (await sha(pub))
      )
        throw new Error("This key does not belong to that active identity.");
      return {
        key,
        subject: identity.subject_id,
        keyId: identity.key_id,
        certificates,
        expires: Date.now() + 600000,
      };
    } finally {
      seed.fill(0);
      pkcs8.fill(0);
    }
  }
  const OPERATIONS = new Set([
    "content.post_create",
    "discussion.reply",
    "communication.dm_send",
    "communication.dm_request",
    "communication.dm_accept",
    "communication.dm_reject",
  ]);
  async function packet(signer, service, operation, args, requestId) {
    if (!signer || Date.now() >= signer.expires)
      throw new Error("Connect your identity key again.");
    if (!OPERATIONS.has(operation))
      throw new Error("Operation is not available in the universe.");
    const envelope = {
      request_id: requestId,
      protocol_version: 1,
      operation,
      contract_version: 1,
      target_service: service,
      subject: signer.subject,
      arguments: args,
      expected_generations: [],
      return_fields: [],
    };
    envelope.payload_digest =
      "sha256:" + (await sha(bytes(canonical(envelope))));
    envelope.expires_at = new Date(Date.now() + 180000)
      .toISOString()
      .replace("Z", "000Z");
    envelope.source = "manual";
    const signature = await crypto.subtle.sign(
      "Ed25519",
      signer.key,
      bytes("msg.lmm.best/v1/request\0" + canonical(envelope)),
    );
    return {
      ...envelope,
      proof: {
        signature: {
          algorithm: "ed25519",
          key_id: signer.keyId,
          value: b64(signature),
        },
        certificates: signer.certificates,
      },
    };
  }
  globalThis.MSGUniverse = Object.freeze({
    TAU,
    handle,
    random,
    position,
    nebula,
    appearance,
    reserve,
    satellite,
    graph,
    canonical,
    importSigner,
    packet,
  });
})();
