-- Schema-only reproduction of observed archczy 0.22.1. No user data.
CREATE TABLE agent_state (
    owner_id TEXT NOT NULL,
    name     TEXT NOT NULL,
    value    TEXT NOT NULL,
    updated  REAL NOT NULL,
    PRIMARY KEY(owner_id, name)
);
CREATE TABLE archived_attachments (
    id           INTEGER PRIMARY KEY,
    post_id      INTEGER NOT NULL REFERENCES archived_posts(id) ON DELETE CASCADE,
    slot         INTEGER NOT NULL,
    name         TEXT NOT NULL,
    content_type TEXT NOT NULL,
    data         BLOB NOT NULL,
    nbytes       INTEGER NOT NULL,
    sha256       TEXT NOT NULL, created REAL NOT NULL DEFAULT 0, uploader_name TEXT NOT NULL DEFAULT 'anonymous', uploader_id TEXT, downloads INTEGER NOT NULL DEFAULT 0,
    UNIQUE(post_id, slot)
);
CREATE TABLE archived_posts (
    id          INTEGER PRIMARY KEY,
    board       TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    name        TEXT NOT NULL DEFAULT 'anonymous',
    title       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL,
    created     REAL NOT NULL,
    updated     REAL NOT NULL,
    nbytes      INTEGER NOT NULL,
    author_key  TEXT,
    author_id   TEXT,
    actor_key   TEXT,
    actor_id    TEXT,
    signature   TEXT,
    sig_version INTEGER NOT NULL DEFAULT 0,
    sig_nonce   TEXT,
    sig_issued  INTEGER,
    reply_to    INTEGER,
    system      INTEGER NOT NULL DEFAULT 0,
    custody_id  TEXT,
    archived_at REAL NOT NULL,
    archived_by TEXT
);
CREATE TABLE attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id      INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    slot         INTEGER NOT NULL,
    name         TEXT NOT NULL,
    content_type TEXT NOT NULL,
    data         BLOB NOT NULL,
    nbytes       INTEGER NOT NULL,
    sha256       TEXT NOT NULL, created REAL NOT NULL DEFAULT 0, uploader_name TEXT NOT NULL DEFAULT 'anonymous', uploader_id TEXT, downloads INTEGER NOT NULL DEFAULT 0,
    UNIQUE(post_id, slot)
);
CREATE TABLE boards (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    locked      INTEGER NOT NULL DEFAULT 0,
    created     REAL NOT NULL
);
CREATE TABLE certificate_requests (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_key      TEXT NOT NULL,
    subject_id       TEXT NOT NULL,
    requested_issuer TEXT NOT NULL DEFAULT '',
    grants           TEXT NOT NULL,
    delegate         INTEGER NOT NULL DEFAULT 0,
    message          TEXT NOT NULL DEFAULT '',
    created          REAL NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    decided          REAL,
    decision_by      TEXT NOT NULL DEFAULT '',
    reason           TEXT NOT NULL DEFAULT '',
    certificate_serial TEXT
);
CREATE TABLE certificates (
    serial        TEXT PRIMARY KEY,
    issuer_serial TEXT NOT NULL,
    issuer_id     TEXT NOT NULL,
    subject_id    TEXT NOT NULL,
    subject_key   TEXT NOT NULL,
    body          TEXT NOT NULL,
    signature     TEXT NOT NULL,
    created       REAL NOT NULL
);
CREATE TABLE custody_identities (
    id          TEXT PRIMARY KEY,
    token_hash  TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    public_key  TEXT NOT NULL,
    author_id   TEXT NOT NULL UNIQUE,
    key_nonce      BLOB NOT NULL,
    key_ciphertext BLOB NOT NULL,
    created         REAL NOT NULL,
    last_used   REAL NOT NULL
);
CREATE TABLE exchange_tasks (
    post_id     INTEGER PRIMARY KEY,
    owner_id    TEXT NOT NULL,
    assignee_id TEXT,
    status      TEXT NOT NULL,
    created     REAL NOT NULL,
    updated     REAL NOT NULL
);
CREATE TABLE identity_names (
    author_id  TEXT NOT NULL,
    name       TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    PRIMARY KEY(author_id, name)
);
CREATE TABLE inbox_events (
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    subject_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    PRIMARY KEY(post_id, subject_id, kind)
);
CREATE TABLE inbox_receipts (
    subject_id TEXT NOT NULL,
    post_id    INTEGER NOT NULL,
    status     TEXT NOT NULL,
    updated    REAL NOT NULL, read_at REAL NOT NULL DEFAULT 0, public_key TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(subject_id, post_id)
);
CREATE TABLE keystore_entries (
    owner_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    ciphertext  BLOB NOT NULL,
    sha256      TEXT NOT NULL,
    version     INTEGER NOT NULL,
    created     REAL NOT NULL,
    updated     REAL NOT NULL,
    PRIMARY KEY(owner_id, name)
);
CREATE TABLE name_claims (
    name_key        TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    author_id       TEXT NOT NULL,
    public_key      TEXT NOT NULL,
    claim_post_id   INTEGER REFERENCES posts(id) ON DELETE SET NULL,
    claim_signature TEXT NOT NULL DEFAULT '',
    claimed         REAL NOT NULL,
    last_used       REAL NOT NULL
);
CREATE TABLE path_get_chunks (
    request_id  TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    data        BLOB NOT NULL,
    created     REAL NOT NULL,
    PRIMARY KEY (request_id, chunk_index)
);
CREATE TABLE path_get_receipts (
    request_id     TEXT PRIMARY KEY,
    payload_sha256 TEXT NOT NULL,
    operation      TEXT NOT NULL,
    status         INTEGER,
    content_type   TEXT,
    body           BLOB,
    headers        TEXT NOT NULL DEFAULT '{}',
    created        REAL NOT NULL
);
CREATE TABLE post_likes (
    post_id   INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    author_id TEXT NOT NULL,
    created   REAL NOT NULL,
    PRIMARY KEY(post_id, author_id)
);
CREATE TABLE post_tags (
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    PRIMARY KEY(post_id, tag)
);
CREATE TABLE posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    board       TEXT NOT NULL REFERENCES boards(name) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    name        TEXT NOT NULL DEFAULT 'anonymous',
    title       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL,
    token_hash  TEXT NOT NULL DEFAULT '',
    created     REAL NOT NULL,
    updated     REAL NOT NULL,
    edit_count  INTEGER NOT NULL DEFAULT 0,
    deleted     INTEGER NOT NULL DEFAULT 0,
    deleted_by  TEXT NOT NULL DEFAULT '',
    nbytes      INTEGER NOT NULL DEFAULT 0
, fingerprint TEXT NOT NULL DEFAULT '', client_hash TEXT NOT NULL DEFAULT '', hidden INTEGER NOT NULL DEFAULT 0, flags INTEGER NOT NULL DEFAULT 0, vouches INTEGER NOT NULL DEFAULT 0, author_key TEXT, author_id TEXT, actor_key TEXT, actor_id TEXT, signature TEXT, sig_version INTEGER NOT NULL DEFAULT 0, sig_nonce TEXT, sig_issued INTEGER, reply_to INTEGER, system INTEGER NOT NULL DEFAULT 0, custody_id TEXT);
CREATE TABLE profiles (
    author_id         TEXT PRIMARY KEY,
    primary_name_key  TEXT NOT NULL,
    bio               TEXT NOT NULL DEFAULT '',
    version           INTEGER NOT NULL DEFAULT 0,
    payload_b64       TEXT NOT NULL DEFAULT '',
    signature         TEXT NOT NULL DEFAULT '',
    updated           REAL NOT NULL
);
CREATE TABLE purge_tombstones (
    post_id   INTEGER PRIMARY KEY,
    board     TEXT NOT NULL,
    purged_at REAL NOT NULL,
    purged_by TEXT,
    reason    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE revocations (
    serial     TEXT PRIMARY KEY REFERENCES certificates(serial) ON DELETE CASCADE,
    revoked_at REAL NOT NULL,
    revoked_by TEXT NOT NULL
, reason TEXT NOT NULL DEFAULT '');
CREATE TABLE signature_nonces (
    signer_id TEXT NOT NULL,
    nonce     TEXT NOT NULL,
    issued    INTEGER NOT NULL,
    PRIMARY KEY(signer_id, nonce)
);
CREATE TABLE ssh_authorized_keys (
                    id          TEXT PRIMARY KEY,
                    owner_id    TEXT NOT NULL,
                    name        TEXT NOT NULL,
                    public_key  TEXT NOT NULL,
                    key_type    TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    scopes      TEXT NOT NULL,
                    created     REAL NOT NULL,
                    updated     REAL NOT NULL,
                    last_used   REAL,
                    expires     REAL,
                    revoked     REAL,
                    created_by  TEXT NOT NULL
                );
CREATE TABLE subscriptions (
    id       TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    kind     TEXT NOT NULL,
    target   TEXT NOT NULL,
    created  REAL NOT NULL,
    UNIQUE(owner_id, kind, target)
);
CREATE TABLE topic_policies (
    board     TEXT PRIMARY KEY,
    anonymous TEXT NOT NULL,
    version   INTEGER NOT NULL,
    updated   REAL NOT NULL
, signed TEXT NOT NULL DEFAULT '["post.create","post.delete.self","post.edit.self"]');
CREATE TABLE webhook_deliveries (
    id           TEXT PRIMARY KEY,
    webhook_id   TEXT NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
    subject_id   TEXT NOT NULL,
    event        TEXT NOT NULL,
    data         TEXT NOT NULL,
    created      REAL NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL,
    delivered    REAL,
    last_error   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE webhooks (
    id                TEXT PRIMARY KEY,
    owner_id          TEXT NOT NULL,
    url               TEXT NOT NULL,
    events            TEXT NOT NULL,
    secret_nonce      BLOB NOT NULL,
    secret_ciphertext BLOB NOT NULL,
    enabled           INTEGER NOT NULL DEFAULT 1,
    created           REAL NOT NULL,
    updated           REAL NOT NULL,
    last_error        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE websub_deliveries (
    id              TEXT PRIMARY KEY,
    subscription_id TEXT NOT NULL REFERENCES websub_subscriptions(id) ON DELETE CASCADE,
    topic           TEXT NOT NULL,
    created         REAL NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt    REAL NOT NULL,
    delivered       REAL,
    last_error      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE websub_hub_pings (
    id           TEXT PRIMARY KEY,
    hub          TEXT NOT NULL,
    topic        TEXT NOT NULL,
    generation   INTEGER NOT NULL DEFAULT 1,
    created      REAL NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL,
    delivered    REAL,
    last_error   TEXT NOT NULL DEFAULT '',
    UNIQUE(hub, topic)
);
CREATE TABLE websub_subscriptions (
    id                TEXT PRIMARY KEY,
    topic             TEXT NOT NULL,
    callback          TEXT NOT NULL,
    secret_nonce      BLOB NOT NULL DEFAULT X'',
    secret_ciphertext BLOB NOT NULL DEFAULT X'',
    created           REAL NOT NULL,
    updated           REAL NOT NULL,
    expires           REAL NOT NULL,
    UNIQUE(topic, callback)
);
CREATE TABLE websub_verifications (
    id                TEXT PRIMARY KEY,
    mode              TEXT NOT NULL,
    topic             TEXT NOT NULL,
    callback          TEXT NOT NULL,
    lease_seconds     INTEGER NOT NULL,
    challenge         TEXT NOT NULL,
    secret_nonce      BLOB NOT NULL DEFAULT X'',
    secret_ciphertext BLOB NOT NULL DEFAULT X'',
    created           REAL NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    next_attempt      REAL NOT NULL,
    last_error        TEXT NOT NULL DEFAULT ''
);
