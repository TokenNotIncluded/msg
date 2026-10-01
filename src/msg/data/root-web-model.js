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
    const r = random(id),
      radius = 22 + Math.sqrt(r()) * 128,
      angle = r() * TAU;
    return [
      Math.cos(angle) * radius,
      (r() - 0.5) * 64,
      Math.sin(angle) * radius,
    ];
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
    satellite,
    graph,
    canonical,
    importSigner,
    packet,
  });
})();
