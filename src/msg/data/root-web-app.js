/* Read projections remain separate from signed writes and private session state. */
(() => {
  "use strict";
  const M = globalThis.MSGUniverse,
    $ = (id) => document.getElementById(id);
  const state = {
    users: new Map(),
    posts: new Map(),
    cursors: {},
    starCursors: new Map(),
    privateMessages: new Map(),
    activeConversation: null,
    mode: "public",
    account: null,
    conversations: [],
    epoch: 0,
    selection: 0,
    selected: null,
    service: null,
    signer: null,
    retry: null,
    compose: null,
    loading: false,
    visited: new Set(),
    read: new Set(),
    followed: false,
  };
  const requests = new Set();
  let toastTimer, renderer, privacyTimer;
  const text = (tag, value, className) => {
    const el = document.createElement(tag);
    el.textContent = value;
    if (className) el.className = className;
    return el;
  };
  function notice(message, sticky = false) {
    clearTimeout(toastTimer);
    $("status").textContent = message;
    $("status").hidden = false;
    $("status").classList.add("show");
    if (!sticky)
      toastTimer = setTimeout(() => ($("status").hidden = true), 6500);
  }
  function errorMessage(error) {
    return error.name === "AbortError"
      ? "Connection interrupted. Nothing has been retried automatically."
      : error.message || "The request could not be completed.";
  }
  function safePath(path) {
    if (
      typeof path !== "string" ||
      !path.startsWith("/") ||
      path.startsWith("//") ||
      /[\\\u0000-\u0020?#]/.test(path)
    )
      throw new Error("Invalid resource path.");
    const u = new URL(path, location.origin);
    if (
      u.origin !== location.origin ||
      u.pathname.startsWith("/-/") ||
      u.pathname.startsWith("/oauth/")
    )
      throw new Error("Invalid resource path.");
    return u.pathname;
  }
  function resourceLink(label, path) {
    const a = text("a", label);
    try {
      a.href = safePath(path);
    } catch {
      return text("span", label);
    }
    return a;
  }
  function button(label, action, parent = $("detail-actions")) {
    const el = text("button", label);
    el.type = "button";
    el.addEventListener("click", () =>
      Promise.resolve(action()).catch((e) => notice(errorMessage(e))),
    );
    parent.append(el);
    return el;
  }
  async function json(path, options = {}) {
    const controller = new AbortController(),
      timer = setTimeout(() => controller.abort(), 12000);
    requests.add(controller);
    try {
      const response = await fetch(path, {
        credentials: options.private ? "same-origin" : "omit",
        cache: "no-store",
        redirect: "error",
        signal: controller.signal,
        method: options.packet ? "POST" : "GET",
        headers: {
          Accept: "application/json",
          ...(options.packet ? { "Content-Type": "application/json" } : {}),
        },
        ...(options.packet ? { body: JSON.stringify(options.packet) } : {}),
      });
      // A bounded reader also rejects oversized responses without accumulating them.
      const reader = response.body.getReader(),
        chunks = [];
      let size = 0;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > 1048576) {
          await reader.cancel();
          throw new Error(
            "This response is too large for the star map. Open the original resource.",
          );
        }
        chunks.push(value);
      }
      const joined = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) {
        joined.set(chunk, offset);
        offset += chunk.length;
      }
      const data = JSON.parse(
        new TextDecoder("utf-8", { fatal: true }).decode(joined),
      );
      if (!response.ok || data.status === "error" || data.error)
        throw new Error(
          typeof data.error === "string"
            ? data.error
            : data.error?.code || "Server returned " + response.status,
        );
      return data;
    } finally {
      clearTimeout(timer);
      requests.delete(controller);
    }
  }
  function privateGraph(time = 0) {
    if (!state.account) return { nodes: [], links: [] };
    const me = { ...state.account, kind: "private", position: [0, 0, 0] },
      nodes = [me],
      links = [];
    for (const conversation of state.conversations) {
      const p = M.position(conversation.other_subject),
        node = {
          id: conversation.conversation_id,
          name: conversation.contact.name,
          kind: "private",
          position: p.map((n) => n * 0.58),
          conversation,
        };
      nodes.push(node);
      links.push([me, node, "private"]);
      if (state.activeConversation === conversation.conversation_id)
        for (const message of state.privateMessages.values()) {
          const satellite = {
            id: message.id,
            title: message.title || "Private message",
            kind: "private-message",
            position: M.satellite(message.id, node.position, time),
            message,
            conversation,
          };
          nodes.push(satellite);
          links.push([node, satellite, "private"]);
        }
    }
    return { nodes, links };
  }
  function graph(time = 0) {
    return state.mode === "private"
      ? privateGraph(time)
      : M.graph(
          [...state.users.values()],
          [...state.posts.values()],
          time,
          state.selected?.id,
        );
  }
  function renderGraph() {
    renderer?.setGraph(graph(renderer.clock));
  }
  function counters() {
    $("counts").textContent =
      state.mode === "private"
        ? `${state.conversations.length} YOUR CONVERSATIONS`
        : `${state.users.size} STARS · ${state.posts.size} POSTS LOADED`;
    const count =
      Math.min(3, state.visited.size) +
      Math.min(2, state.read.size) +
      (state.followed ? 1 : 0);
    $("progress").style.width = (count / 6) * 100 + "%";
    $("progress-label").textContent =
      `${count} / 6 discoveries · this visit only`;
    $("mission").textContent =
      count === 6
        ? "A constellation, now a little more familiar."
        : "Discover 3 stars · Read 2 posts · Follow a reply";
    $("more").hidden =
      state.mode === "private" || !Object.values(state.cursors).some(Boolean);
  }
  function catalog() {
    const container = $("catalog-items");
    container.replaceChildren();
    const entries =
      state.mode === "private"
        ? privateGraph().nodes
        : [...state.users.values()].map((u) => ({ ...u, kind: "user" }));
    for (const item of entries) {
      const b = button(
        (item.kind === "user" ? "@" : "") + (item.name || item.title),
        () => select(item, true),
        container,
      );
      b.className = "catalog-item";
    }
    if (state.mode === "public")
      for (const post of state.posts.values()) {
        const b = button(
          post.title,
          () =>
            select({ ...post, kind: post.reply_to ? "reply" : "post" }, true),
          container,
        );
        b.className = "catalog-item";
        b.append(
          text("small", post.author ? "@" + post.author.name : "Public post"),
        );
      }
    if (!entries.length)
      container.append(
        text(
          "p",
          state.mode === "private"
            ? "No conversations yet."
            : "No public users in this sector yet.",
        ),
      );
    counters();
  }
  async function sector(kind, cursor, author) {
    const page = await json(
      "/_universe?" +
        new URLSearchParams({
          kind,
          ...(cursor ? { cursor } : {}),
          ...(author ? { author } : {}),
        }),
    );
    if (page.version !== 1 || page.kind !== kind || !Array.isArray(page.items))
      throw new Error("Unsupported universe response.");
    state.service = page.service;
    return page;
  }
  function merge(page) {
    if (page.author) state.starCursors.set(page.author, page.cursor);
    else state.cursors[page.kind] = page.cursor;
    for (const item of page.items) {
      if (page.kind === "users") state.users.set(item.id, item);
      else {
        state.posts.set(item.id, item);
        if (item.author && !state.users.has(item.author.id))
          state.users.set(item.author.id, item.author);
      }
    }
  }
  async function loadPublic(reset = false) {
    if (state.loading) return;
    state.loading = true;
    const epoch = state.epoch;
    $("more").disabled = true;
    try {
      const pages = await Promise.all(
        (reset
          ? ["users", "posts"]
          : Object.keys(state.cursors).filter((k) => state.cursors[k])
        ).map((k) => sector(k, reset ? null : state.cursors[k])),
      );
      if (epoch !== state.epoch || state.mode !== "public") return;
      if (reset) {
        state.users.clear();
        state.posts.clear();
        state.cursors = {};
        state.starCursors.clear();
      }
      for (const page of pages) merge(page);
      $("empty").hidden = state.users.size > 0;
      renderGraph();
      catalog();
      notice(
        state.users.size
          ? "Choose a star. Scroll closer. Follow what catches your attention."
          : "No public users yet. This is an empty universe, not a demo.",
        !state.users.size,
      );
    } catch (e) {
      if (epoch === state.epoch) {
        notice(errorMessage(e), true);
        $("empty").hidden = state.users.size > 0;
      }
    } finally {
      state.loading = false;
      $("more").disabled = false;
    }
  }
  function discardPrivate() {
    state.epoch++;
    state.loading = false;
    state.selection++;
    clearInterval(privacyTimer);
    state.account = null;
    state.conversations = [];
    state.privateMessages.clear();
    state.activeConversation = null;
    state.signer = null;
    state.retry = null;
    state.compose = null;
    for (const request of requests) request.abort();
    $("key-file").value = "";
    $("message").value = "";
    $("handle").value = "";
    $("key-status").textContent = "";
    $("send-status").textContent = "";
    $("composer").close();
    $("key-dialog").close();
    closeDetail();
    $("search").value = "";
    $("search-results").replaceChildren();
    $("catalog-items").replaceChildren();
    $("labels").replaceChildren();
    renderer?.labelNodes.clear();
    signerLabel();
  }
  function modeUI() {
    const privateMode = state.mode === "private";
    document.body.classList.toggle("private", privateMode);
    $("public-tab").setAttribute("aria-pressed", String(!privateMode));
    $("private-tab").setAttribute("aria-pressed", String(privateMode));
    $("scope-label").textContent = privateMode
      ? "ONLY VISIBLE TO YOU"
      : "THE PUBLIC CONSTELLATION";
    $("space-title").replaceChildren(
      ...(privateMode
        ? [text("span", "Your own orbit.")]
        : [
            text("span", "Somewhere,"),
            document.createElement("br"),
            text("span", "a conversation begins."),
          ]),
    );
    $("scope-note").textContent = privateMode
      ? "Your conversations. Never part of the public map."
      : "Every star is someone. Every orbit, something worth finding.";
    $("compose-open").hidden = privateMode;
    $("empty").hidden = true;
    catalog();
    renderGraph();
  }
  function publicMode(refresh = true) {
    discardPrivate();
    state.mode = "public";
    modeUI();
    renderer.home();
    if (refresh) loadPublic(true);
  }
  async function privateMode() {
    discardPrivate();
    const epoch = state.epoch;
    notice("Opening your private orbit…", true);
    try {
      const page = await json("/_universe/me", { private: true });
      if (epoch !== state.epoch || document.hidden) return;
      if (!page.account) {
        notice(
          "Sign in to view your own conversations. Public exploration needs no account.",
          true,
        );
        return;
      }
      state.account = page.account;
      state.conversations = page.conversations;
      state.service = page.service;
      state.mode = "private";
      modeUI();
      renderer.travel([0, 0, 0], 225);
      $("account-link").textContent = "@" + page.account.name;
      notice("Private view. Only your conversations are loaded.");
      privacyTimer = setInterval(async () => {
        const expected = state.account?.id;
        try {
          const check = await json("/_universe/me", { private: true });
          if (epoch !== state.epoch) return;
          if (!check.account || check.account.id !== expected) {
            publicMode(false);
            notice(
              "Your private session ended. Its contents have been cleared.",
              true,
            );
          } else {
            state.conversations = check.conversations;
            renderGraph();
            catalog();
          }
        } catch {
          if (epoch === state.epoch) {
            publicMode(false);
            notice(
              "Private access could not be verified. Its contents have been cleared.",
              true,
            );
          }
        }
      }, 30000);
    } catch (e) {
      if (epoch === state.epoch) notice(errorMessage(e), true);
    }
  }
  function closeDetail() {
    state.selection++;
    state.selected = null;
    state.activeConversation = null;
    state.privateMessages.clear();
    $("inspector").hidden = true;
    $("detail-title").textContent = "";
    $("detail-body").replaceChildren();
    $("detail-actions").replaceChildren();
    renderer?.setFocus(null);
    renderGraph();
  }
  function focusPosition(item) {
    if (item.position) return item.position;
    if (item.kind === "user") return M.position(item.id);
    return M.satellite(
      item.id,
      M.position(item.author?.id || item.id),
      renderer.clock,
    );
  }
  async function select(item, approach = false) {
    const epoch = state.epoch,
      sequence = ++state.selection;
    state.selected = item;
    $("detail-body").replaceChildren();
    $("detail-actions").replaceChildren();
    $("inspector").hidden = false;
    $("catalog").hidden = true;
    $("search-box").hidden = true;
    $("detail-kind").textContent =
      item.kind === "private-message"
        ? "PRIVATE MESSAGE"
        : item.kind === "private"
          ? "PRIVATE ORBIT"
          : item.kind === "user"
            ? "STAR SYSTEM"
            : item.reply_to
              ? "REPLY"
              : "POST";
    $("detail-title").textContent =
      (item.kind === "user" ? "@" : "") + (item.name || item.title || "Signal");
    renderGraph();
    renderer.setFocus(item.id);
    if (approach)
      renderer.focus(
        { ...item, position: focusPosition(item) },
        item.kind === "user" ? 83 : 55,
      );
    const body = $("detail-body"),
      actions = $("detail-actions");
    if (item.kind === "private-message") {
      try {
        const data = await json(safePath(item.message.path) + "/json", {
          private: true,
        });
        if (
          epoch !== state.epoch ||
          sequence !== state.selection ||
          state.mode !== "private"
        )
          return;
        body.append(
          text("p", data.content || "No text content.", "message-body"),
        );
        button(
          "Back to conversation",
          () =>
            select(
              {
                id: item.conversation.conversation_id,
                name: item.conversation.contact.name,
                kind: "private",
                conversation: item.conversation,
              },
              true,
            ),
          actions,
        );
      } catch (e) {
        if (epoch === state.epoch && sequence === state.selection)
          body.append(text("p", errorMessage(e)));
      }
      return;
    }
    state.privateMessages.clear();
    state.activeConversation = null;
    if (item.kind === "private") {
      if (!item.conversation) {
        body.append(
          text("p", "This is your star. Only you can open this view."),
        );
        return;
      }
      const conversation = item.conversation;
      state.activeConversation = conversation.conversation_id;
      body.append(text("p", "Opening conversation…"));
      try {
        const data = await json(safePath(conversation.path) + "/json", {
          private: true,
        });
        if (
          epoch !== state.epoch ||
          sequence !== state.selection ||
          state.mode !== "private"
        )
          return;
        body.replaceChildren();
        const detail = data.conversation;
        if (!detail) throw new Error("Conversation is unavailable.");
        body.append(
          text(
            "p",
            detail.state === "active"
              ? "Only conversation participants can read these messages."
              : "Connection request · " + detail.state,
            "muted",
          ),
        );
        for (const message of detail.messages) {
          state.privateMessages.set(message.id, message);
          const article = document.createElement("article");
          article.append(
            text(
              "small",
              message.author.name +
                " · " +
                new Date(message.created_at).toLocaleDateString(),
            ),
            text("p", message.body, "message-body"),
          );
          if (message.truncated)
            article.append(
              resourceLink("Read the full message ↗", message.path),
            );
          body.append(article);
        }
        renderGraph();
        if (!detail.messages.length) body.append(text("p", "No messages yet."));
        else
          body.prepend(
            text(
              "p",
              "Each message is a small satellite. Select one to read it.",
              "muted",
            ),
          );
        if (detail.older_messages)
          body.append(
            resourceLink("Open older messages ↗", conversation.path),
          );
        if (detail.state === "active")
          button(
            "Write a private message",
            () =>
              compose("communication.dm_send", {
                conversation_id: conversation.conversation_id,
              }),
            actions,
          );
        if (
          detail.state === "pending" &&
          conversation.initiator !== state.account.id
        ) {
          button(
            "Review & accept",
            () =>
              compose("communication.dm_accept", {
                conversation_id: conversation.conversation_id,
              }),
            actions,
          );
          button(
            "Review & decline",
            () =>
              compose("communication.dm_reject", {
                conversation_id: conversation.conversation_id,
              }),
            actions,
          );
        }
      } catch (e) {
        if (epoch === state.epoch && sequence === state.selection) {
          body.replaceChildren(text("p", errorMessage(e)));
          actions.replaceChildren();
        }
      }
      return;
    }
    if (item.kind === "user") {
      state.visited.add(item.id);
      body.append(
        text(
          "p",
          "A voice in the constellation. Select an orbiting post, or explore their recent signals.",
        ),
      );
      const list = document.createElement("div");
      body.append(list);
      const showPosts = () => {
        list.replaceChildren();
        const posts = [...state.posts.values()].filter(
          (p) => p.author?.id === item.id,
        );
        for (const post of posts)
          button(
            post.title,
            () =>
              select({ ...post, kind: post.reply_to ? "reply" : "post" }, true),
            list,
          ).className = "post-item";
        if (!posts.length)
          list.append(
            text("p", "No public posts in this star system yet.", "muted"),
          );
        if (state.starCursors.get(item.id))
          button(
            "Load older signals",
            () => loadStar(state.starCursors.get(item.id)),
            list,
          );
      };
      const loadStar = async (cursor) => {
        try {
          const page = await sector("posts", cursor, item.id);
          if (
            epoch !== state.epoch ||
            sequence !== state.selection ||
            state.mode !== "public"
          )
            return;
          merge(page);
          showPosts();
          renderGraph();
          counters();
        } catch (e) {
          if (epoch === state.epoch && sequence === state.selection)
            list.append(text("p", errorMessage(e)));
        }
      };
      showPosts();
      loadStar();
      if (item.path) actions.append(resourceLink("Profile ↗", item.path));
      button(
        "Approach",
        () => renderer.focus({ ...item, position: focusPosition(item) }, 83),
        actions,
      );
      button(
        "Request private conversation",
        () => compose("communication.dm_request", { recipient: item.id }),
        actions,
      );
      counters();
      return;
    }
    body.append(text("p", item.excerpt || "Reading signal…", "message-body"));
    try {
      const data = await json("/_r/" + encodeURIComponent(item.id) + "/json");
      if (epoch !== state.epoch || sequence !== state.selection) return;
      body.replaceChildren(
        text(
          "p",
          data.content || item.excerpt || "No text content.",
          "message-body",
        ),
      );
      state.read.add(item.id);
      counters();
      if (item.path)
        actions.append(resourceLink("Original post ↗", item.path));
      button(
        "Reply",
        () => compose("discussion.reply", { target: { id: item.id } }),
        actions,
      );
      if (item.reply_to) {
        const target = state.posts.get(item.reply_to.id);
        if (target)
          button(
            "Follow the reply ↗",
            async () => {
              state.followed = true;
              counters();
              await select(
                { ...target, kind: target.reply_to ? "reply" : "post" },
                true,
              );
            },
            actions,
          );
        else
          actions.append(
            resourceLink("Read the parent post ↗", item.reply_to.path),
          );
      }
    } catch (e) {
      if (epoch === state.epoch && sequence === state.selection) {
        body.replaceChildren(text("p", errorMessage(e)));
        if (item.path)
          actions.append(resourceLink("Open original ↗", item.path));
      }
    }
  }
  function search() {
    const query = $("search").value.trim().toLowerCase(),
      results = $("search-results");
    results.replaceChildren();
    if (!query) return;
    const entries =
      state.mode === "private"
        ? privateGraph().nodes
        : [
            ...[...state.users.values()].map((u) => ({ ...u, kind: "user" })),
            ...[...state.posts.values()].map((p) => ({
              ...p,
              kind: p.reply_to ? "reply" : "post",
            })),
          ];
    for (const item of entries
      .filter((n) =>
        [n.name, n.title, n.excerpt]
          .filter(Boolean)
          .join(" ")
          .toLowerCase()
          .includes(query),
      )
      .slice(0, 25))
      button(
        item.name ? "@" + item.name : item.title,
        () => select(item, true),
        results,
      ).className = "catalog-item";
    if (!results.children.length)
      results.append(text("p", "No match in this loaded sector."));
  }
  function signerLabel() {
    const signer = state.signer;
    if (signer && Date.now() >= signer.expires) state.signer = null;
    $("signer-state").textContent = state.signer
      ? "Signing locally as " +
        state.signer.handle +
        ". Nothing is sent until you confirm."
      : "No signing key connected. Browser login alone cannot publish.";
  }
  function compose(operation, args) {
    state.compose = { operation, args };
    state.retry = null;
    const privateWrite = operation.startsWith("communication."),
      bodyRequired = [
        "content.post_create",
        "discussion.reply",
        "communication.dm_send",
      ].includes(operation);
    $("compose-scope").textContent = privateWrite
      ? "PRIVATE TRANSMISSION"
      : "PUBLIC TRANSMISSION";
    $("composer-title").textContent =
      operation === "content.post_create"
        ? "Leave a signal."
        : operation === "discussion.reply"
          ? "Continue the conversation."
          : operation === "communication.dm_request"
            ? "Request a conversation."
            : operation === "communication.dm_accept"
              ? "Accept this request?"
              : operation === "communication.dm_reject"
                ? "Decline this request?"
                : "Send a private message.";
    $("compose-hint").textContent = privateWrite
      ? "This action is private. Only a matching identity key can authorize it."
      : "Signed with your own identity. Publicly readable after you confirm.";
    $("destination").hidden = $("destination-label").hidden =
      operation !== "content.post_create";
    $("message").hidden = !bodyRequired;
    document.querySelector('label[for="message"]').hidden = !bodyRequired;
    $("message").value = "";
    $("send-status").textContent = "";
    $("send").textContent = bodyRequired
      ? privateWrite
        ? "Sign & send"
        : "Sign & publish"
      : "Sign & confirm";
    signerLabel();
    $("composer").showModal();
    if (bodyRequired) $("message").focus();
  }
  async function connect() {
    const file = $("key-file").files[0],
      handle = $("handle").value.trim().replace(/^@/, "");
    $("key-file").value = "";
    state.signer = null;
    signerLabel();
    const epoch = state.epoch;
    $("connect").disabled = true;
    try {
      if (
        !file ||
        file.size !== 32 ||
        !/^[a-zA-Z0-9_-]{1,64}$/.test(handle) ||
        handle.toLowerCase() === "root"
      )
        throw new Error(
          "Choose your 32-byte identity.key and a non-root handle.",
        );
      const identity = await json("/@" + encodeURIComponent(handle) + "/pk");
      const certificatePage = await json(
        "/@" + encodeURIComponent(handle) + "/cert/json",
      );
      const certificates = (certificatePage.certificates || [])
        .filter((c) => !c.revoked)
        .map((c) => c.id);
      const seed = new Uint8Array(await file.arrayBuffer());
      const signer = await M.importSigner(seed, identity, certificates);
      if (epoch !== state.epoch || document.hidden) return;
      state.signer = { ...signer, handle: "@" + handle };
      $("key-status").textContent =
        "Connected in this tab. The key has not been uploaded.";
      signerLabel();
      $("key-dialog").close();
    } catch (e) {
      $("key-status").textContent = errorMessage(e);
    } finally {
      $("connect").disabled = false;
    }
  }
  async function send() {
    signerLabel();
    if (!state.signer) {
      $("key-dialog").showModal();
      return;
    }
    const context = state.compose;
    if (!context) return;
    const signer = state.signer,
      epoch = state.epoch;
    let packetPrepared = false;
    $("send").disabled = true;
    try {
      const args = { ...context.args };
      if (!$("message").hidden) {
        if (!$("message").value.trim())
          throw new Error("Write a message first.");
        args.body = $("message").value;
        if (args.body.isWellFormed && !args.body.isWellFormed())
          throw new Error("The message contains an invalid Unicode character.");
      }
      if (context.operation === "content.post_create") {
        args.parent = safePath($("destination").value.trim());
      }
      if (
        context.operation.startsWith("communication.") &&
        state.account &&
        signer.subject !== state.account.id
      )
        throw new Error(
          "Connect the identity key belonging to this private account.",
        );
      if (!state.service)
        throw new Error("The service identity has not been loaded yet.");
      const fingerprint = M.canonical([
        signer.subject,
        context.operation,
        args,
      ]);
      if (state.retry && state.retry.fingerprint !== fingerprint)
        throw new Error(
          "A previous delivery is unconfirmed. Restore that message and retry, or close and reopen the composer before starting a different action.",
        );
      if (!state.retry)
        state.retry = {
          fingerprint,
          id: crypto.randomUUID().replace(/-/g, ""),
        };
      const packet = await M.packet(
        signer,
        state.service,
        context.operation,
        args,
        state.retry.id,
      );
      if (epoch !== state.epoch || document.hidden) return;
      packetPrepared = true;
      $("send-status").textContent = "Sending your signed request…";
      const result = await json("/-/p/" + context.operation, { packet });
      if (epoch !== state.epoch) return;
      state.retry = null;
      state.compose = null;
      $("message").value = "";
      $("composer").close();
      if (state.mode === "private") {
        const page = await json("/_universe/me", { private: true });
        if (epoch !== state.epoch) return;
        if (!page.account || page.account.id !== state.account.id) {
          publicMode(false);
          throw new Error("Your private session ended.");
        }
        state.conversations = page.conversations;
        catalog();
        renderGraph();
        const conversation = state.conversations.find(
          (c) => c.conversation_id === context.args.conversation_id,
        );
        if (conversation)
          await select(
            {
              id: conversation.conversation_id,
              name: conversation.contact.name,
              kind: "private",
              conversation,
            },
            true,
          );
      } else {
        await loadPublic(true);
        const post = state.posts.get(result.resources?.[0]?.id);
        if (post)
          await select(
            { ...post, kind: post.reply_to ? "reply" : "post" },
            true,
          );
      }
      notice("Confirmed by the server.");
    } catch (e) {
      if (epoch === state.epoch)
        $("send-status").textContent =
          errorMessage(e) +
          (packetPrepared
            ? " Retry the unchanged message to reuse the same request ID."
            : "");
    } finally {
      $("send").disabled = false;
    }
  }
  renderer = new globalThis.MSGUniverseRenderer($("space"), $("labels"), {
    select: (item) => select(item, true),
    overview: () => closeDetail(),
    tick: (time) => {
      renderer.graph = graph(time);
    },
    camera: (target) => {
      $("coordinates").textContent = target
        .map(
          (n, i) =>
            "XYZ"[i] +
            " " +
            (n >= 0 ? "+" : "−") +
            Math.abs(Math.round(n)).toString().padStart(3, "0"),
        )
        .join("    ");
    },
    fallback: (message) => {
      notice(message, true);
      $("catalog").hidden = false;
    },
    motion: (paused) => {
      $("pause").setAttribute("aria-pressed", String(paused));
      $("pause").textContent = paused ? "▷" : "Ⅱ";
    },
  });
  $("pause").setAttribute("aria-pressed", String(renderer.paused));
  $("pause").textContent = renderer.paused ? "▷" : "Ⅱ";
  $("home").onclick = () => renderer.home();
  $("pause").onclick = () => {
    renderer.pause(!renderer.paused);
    $("pause").setAttribute("aria-pressed", String(renderer.paused));
    $("pause").textContent = renderer.paused ? "▷" : "Ⅱ";
    $("pause").setAttribute(
      "aria-label",
      renderer.paused ? "Resume motion" : "Pause motion",
    );
  };
  $("drift").onclick = () => {
    const list =
      state.mode === "private"
        ? privateGraph().nodes
        : [...state.users.values()].map((u) => ({ ...u, kind: "user" }));
    const unseen = list.filter((n) => !state.visited.has(n.id)),
      choices = unseen.length ? unseen : list;
    if (!choices.length) {
      notice(
        "No stars loaded yet. Try reconnecting or loading another sector.",
      );
      return;
    }
    select(choices[Math.floor(Math.random() * choices.length)], true);
  };
  $("catalog-toggle").onclick = () => {
    $("catalog").hidden = !$("catalog").hidden;
    if (!$("catalog").hidden) {
      catalog();
      $("catalog").focus();
    }
  };
  $("catalog-close").onclick = () => {
    $("catalog").hidden = true;
    $("catalog-toggle").focus();
  };
  document.querySelector(".skip").onclick = (e) => {
    e.preventDefault();
    catalog();
    $("catalog").hidden = false;
    $("catalog").focus();
  };
  $("detail-close").onclick = () => {
    closeDetail();
    $("space").focus();
  };
  $("public-tab").onclick = () => publicMode();
  $("private-tab").onclick = privateMode;
  $("search-toggle").onclick = () => {
    $("search-box").hidden = false;
    $("search").focus();
  };
  $("search-close").onclick = () => {
    $("search-box").hidden = true;
    $("search-toggle").focus();
  };
  $("search").oninput = search;
  $("compose-open").onclick = () => compose("content.post_create", {});
  $("compose-close").onclick = () => {
    $("composer").close();
    $("message").value = "";
    state.retry = null;
    state.compose = null;
  };
  $("connect-open").onclick = () => {
    $("key-dialog").showModal();
  };
  $("key-close").onclick = () => $("key-dialog").close();
  $("connect").onclick = connect;
  $("send").onclick = send;
  $("disconnect").onclick = () => {
    state.signer = null;
    $("key-file").value = "";
    $("key-status").textContent = "Disconnected.";
    signerLabel();
  };
  $("more").onclick = () => loadPublic();
  $("retry").onclick = () => loadPublic(true);
  document.addEventListener("keydown", (e) => {
    if (
      e.key === "/" &&
      !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName) &&
      !$("composer").open &&
      !$("key-dialog").open
    ) {
      e.preventDefault();
      $("search-box").hidden = false;
      $("search").focus();
    }
    if (e.key === "Escape" && !$("composer").open && !$("key-dialog").open) {
      closeDetail();
      $("catalog").hidden = true;
      $("search-box").hidden = true;
    }
  });
  const hide = (e) => {
    if (document.hidden || e.type === "pagehide") {
      const wasPrivate = state.mode === "private";
      publicMode(false);
      $("account-link").textContent = "Sign in ↗";
      if (renderer.software)
        renderer.context.clearRect(
          0,
          0,
          renderer.canvas.width,
          renderer.canvas.height,
        );
      else if (renderer.available)
        renderer.gl.clear(renderer.gl.COLOR_BUFFER_BIT);
      if (wasPrivate)
        notice(
          "Private view cleared. Open My orbit again to verify your session.",
          true,
        );
    }
  };
  document.addEventListener("visibilitychange", hide);
  window.addEventListener("pagehide", hide);
  setInterval(signerLabel, 15000);
  loadPublic(true);
})();
