"""One transactional metadata session; handlers never own commit rights."""

from __future__ import annotations

import asyncio
import contextvars
import fcntl
import os
import sqlite3
import tempfile
import uuid
from contextlib import asynccontextmanager, closing
from pathlib import Path

from msg.core.errors import Failure, require
from msg.core.identifiers import hex_id
from msg.core.query import QueryResult, SqlParameters
from msg.storage.query import SessionQueryResult
from msg.storage.session import RelationalSession

_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES (1);
CREATE TABLE IF NOT EXISTS resources (
 id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
 parent TEXT REFERENCES resources(id) DEFERRABLE INITIALLY DEFERRED,
 owner TEXT NOT NULL, grp TEXT NOT NULL, mode INTEGER NOT NULL,
 generation INTEGER NOT NULL, revision TEXT, state TEXT NOT NULL,
 created_at TEXT NOT NULL, modified_at TEXT NOT NULL, body TEXT NOT NULL,
 UNIQUE(parent,name));
CREATE UNIQUE INDEX IF NOT EXISTS one_root ON resources((1)) WHERE parent IS NULL;
CREATE INDEX IF NOT EXISTS resources_parent ON resources(parent,id);
CREATE INDEX IF NOT EXISTS resources_time ON resources(created_at,id);
CREATE INDEX IF NOT EXISTS resources_owner ON resources(owner,id);
CREATE INDEX IF NOT EXISTS resources_type_state ON resources(type,state,id);
CREATE TABLE IF NOT EXISTS resource_path_aliases (
 parent_id TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
 name TEXT NOT NULL,
 resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
 PRIMARY KEY(parent_id,name));
CREATE TABLE IF NOT EXISTS resource_tags (
 resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
 tag TEXT NOT NULL, PRIMARY KEY(tag,resource_id));
CREATE INDEX IF NOT EXISTS resource_tags_resource ON resource_tags(resource_id);
CREATE TABLE IF NOT EXISTS revisions (
 id TEXT PRIMARY KEY, resource_id TEXT NOT NULL REFERENCES resources(id),
 created_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS revisions_resource ON revisions(resource_id,created_at,id);
CREATE TABLE IF NOT EXISTS relations (
 revision_id TEXT NOT NULL REFERENCES revisions(id) ON DELETE CASCADE,
 source_id TEXT NOT NULL, type TEXT NOT NULL, target_id TEXT NOT NULL,
 target_revision TEXT, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS relations_target ON relations(target_id,type);
CREATE TABLE IF NOT EXISTS identities (id TEXT PRIMARY KEY, kind TEXT NOT NULL, generation INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memberships (org TEXT, subject TEXT, generation INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(org,subject));
CREATE TABLE IF NOT EXISTS emails (subject TEXT PRIMARY KEY, generation INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS credentials (id TEXT PRIMARY KEY, subject TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS credentials_subject ON credentials(subject);
CREATE TABLE IF NOT EXISTS oauth_states (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, expires TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS oauth_states_expiry ON oauth_states(expires);

CREATE TABLE IF NOT EXISTS token_deliveries (
 credential_id TEXT PRIMARY KEY, subject TEXT NOT NULL, request_id TEXT NOT NULL,
 request_digest TEXT NOT NULL, recovery_verifier TEXT NOT NULL,
 recovery_expires_at TEXT NOT NULL, claimed_at TEXT, consumed_at TEXT);
CREATE INDEX IF NOT EXISTS token_deliveries_subject ON token_deliveries(subject,request_id);
CREATE TABLE IF NOT EXISTS identity_keys (
 key_id TEXT PRIMARY KEY, subject TEXT NOT NULL, public_key TEXT NOT NULL,
 created_at TEXT NOT NULL, retired_at TEXT, is_primary INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS identity_keys_primary ON identity_keys(subject) WHERE is_primary=1;
CREATE INDEX IF NOT EXISTS identity_keys_subject ON identity_keys(subject,created_at,key_id);
CREATE TABLE IF NOT EXISTS encryption_subkeys (
 key_id TEXT PRIMARY KEY, subject TEXT NOT NULL, recipient TEXT NOT NULL UNIQUE,
 public_key TEXT NOT NULL, created_at TEXT NOT NULL, retired_at TEXT,
 is_primary INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS encryption_subkeys_primary ON encryption_subkeys(subject) WHERE is_primary=1;
CREATE INDEX IF NOT EXISTS encryption_subkeys_subject ON encryption_subkeys(subject,created_at,key_id);
CREATE TABLE IF NOT EXISTS certificates (id TEXT PRIMARY KEY, subject TEXT NOT NULL, parent TEXT, revoked INTEGER NOT NULL DEFAULT 0, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS csrs (id TEXT PRIMARY KEY, generation INTEGER NOT NULL, state TEXT NOT NULL, body TEXT NOT NULL, state_body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS results (subject TEXT, request_id TEXT, digest TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(subject,request_id));
CREATE TABLE IF NOT EXISTS batches (subject TEXT, request_id TEXT, digest TEXT NOT NULL, PRIMARY KEY(subject,request_id));
CREATE TABLE IF NOT EXISTS transfers (id TEXT PRIMARY KEY, subject TEXT NOT NULL, generation INTEGER NOT NULL, body TEXT NOT NULL, limits TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS chunks (transfer_id TEXT NOT NULL, offset INTEGER NOT NULL, length INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(transfer_id,offset));
CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS topic_event_projection (
 seq INTEGER PRIMARY KEY REFERENCES events(seq) ON DELETE CASCADE, topic TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS topic_event_projection_topic ON topic_event_projection(topic,seq DESC);
CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, digest TEXT UNIQUE NOT NULL, previous TEXT, body TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'append_only_audit'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'append_only_audit'); END;
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, dedupe TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, next_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watches (subject TEXT, resource TEXT, PRIMARY KEY(subject,resource));
CREATE TABLE IF NOT EXISTS agent_follows (
 follower TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
 target TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE, created_at TEXT NOT NULL,
 PRIMARY KEY(follower,target), CHECK(follower<>target));
CREATE INDEX IF NOT EXISTS agent_follows_target ON agent_follows(target,follower);
CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, sender TEXT, recipient TEXT, resource TEXT, event_id TEXT, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS webhook_endpoints (
 subject TEXT PRIMARY KEY, url TEXT NOT NULL, nonce TEXT NOT NULL, ciphertext TEXT NOT NULL,
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), generation INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reactions (subject TEXT, resource TEXT, kind TEXT, revision TEXT NOT NULL DEFAULT '', body TEXT NOT NULL, PRIMARY KEY(subject,resource,kind,revision));
CREATE TABLE IF NOT EXISTS agent_drops (
 id TEXT PRIMARY KEY, sender TEXT NOT NULL REFERENCES resources(id),
 recipient TEXT NOT NULL REFERENCES resources(id), kind TEXT NOT NULL CHECK(kind IN ('dead_drop','time_capsule')),
 created_at TEXT NOT NULL, opens_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 claimed_at TEXT, cancelled_at TEXT, nonce TEXT NOT NULL, ciphertext TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS agent_drops_inbox ON agent_drops(recipient,created_at,id);
CREATE INDEX IF NOT EXISTS agent_drops_sender ON agent_drops(sender);
CREATE TABLE IF NOT EXISTS link_invitations (
 invite_id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, minutes INTEGER NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, claim TEXT, claimed_at TEXT,
 grant_box TEXT, released_at TEXT, revoked_at TEXT, UNIQUE(owner, name));
CREATE INDEX IF NOT EXISTS link_invitations_owner ON link_invitations(owner, name);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projections (resource_id TEXT PRIMARY KEY, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS email_challenges (subject TEXT PRIMARY KEY, digest TEXT NOT NULL, expires TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS achievement_ceremonies (
 id TEXT PRIMARY KEY, subject TEXT NOT NULL, state TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS achievement_ceremonies_subject ON achievement_ceremonies(subject);
CREATE TABLE IF NOT EXISTS achievement_grants (
 id TEXT PRIMARY KEY, subject TEXT NOT NULL, achievement_id TEXT NOT NULL,
 spec_version INTEGER NOT NULL, body TEXT NOT NULL,
 UNIQUE(subject,achievement_id,spec_version));
CREATE INDEX IF NOT EXISTS achievement_grants_lookup ON achievement_grants(achievement_id,subject);
CREATE TABLE IF NOT EXISTS order_escrow_decisions (
 order_id TEXT PRIMARY KEY REFERENCES store_orders(id),
 id TEXT NOT NULL UNIQUE, body TEXT NOT NULL, signature TEXT NOT NULL,
 source_proof TEXT NOT NULL,
 transaction_id TEXT NOT NULL UNIQUE REFERENCES money_ledger(id));
CREATE TRIGGER IF NOT EXISTS escrow_decision_no_update BEFORE UPDATE ON order_escrow_decisions
BEGIN SELECT RAISE(ABORT,'append_only_escrow_decision'); END;
CREATE TRIGGER IF NOT EXISTS escrow_decision_no_delete BEFORE DELETE ON order_escrow_decisions
BEGIN SELECT RAISE(ABORT,'append_only_escrow_decision'); END;
CREATE TABLE IF NOT EXISTS achievement_pins (
 subject TEXT NOT NULL REFERENCES identities(id),
 grant_id TEXT NOT NULL REFERENCES achievement_grants(id),
 position INTEGER NOT NULL CHECK(position>=0),
 PRIMARY KEY(subject,grant_id), UNIQUE(subject,position));
CREATE TABLE IF NOT EXISTS share_grants (
 id TEXT PRIMARY KEY, resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
 grantor TEXT NOT NULL, grantee TEXT NOT NULL, created_at TEXT NOT NULL,
 expires_at TEXT NOT NULL, revoked_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS share_grants_active ON share_grants(resource_id,grantee)
 WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS share_grants_grantee ON share_grants(grantee,resource_id);
CREATE TABLE IF NOT EXISTS share_grants_v2 (
 id TEXT PRIMARY KEY, resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
 grantor TEXT NOT NULL, grantee TEXT NOT NULL, grantee_kind TEXT NOT NULL
 CHECK(grantee_kind IN ('user','group')), parent_id TEXT REFERENCES share_grants_v2(id),
 operations TEXT NOT NULL, constraints TEXT NOT NULL, allow_reshare INTEGER NOT NULL
 CHECK(allow_reshare IN (0,1)), created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 revoked_at TEXT);
CREATE INDEX IF NOT EXISTS share_grants_v2_resource ON share_grants_v2(resource_id,grantee,grantee_kind);
CREATE INDEX IF NOT EXISTS share_grants_v2_parent ON share_grants_v2(parent_id);
CREATE TABLE IF NOT EXISTS share_links (
 id TEXT PRIMARY KEY, resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
 grantor TEXT NOT NULL, credential_id TEXT NOT NULL,
 verifier TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
 expires_at TEXT NOT NULL, revoked_at TEXT);
CREATE INDEX IF NOT EXISTS share_links_resource ON share_links(resource_id,grantor);
CREATE TABLE IF NOT EXISTS dm_conversations (
 pair TEXT PRIMARY KEY, resource_id TEXT NOT NULL UNIQUE REFERENCES resources(id),
 participant_a TEXT NOT NULL, participant_b TEXT NOT NULL, initiator TEXT NOT NULL,
 conversation_kind TEXT NOT NULL DEFAULT 'direct' CHECK(conversation_kind='direct'),
 state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK(participant_a < participant_b), CHECK(state IN ('pending','active','rejected')));
CREATE INDEX IF NOT EXISTS dm_conversations_participants ON dm_conversations(participant_a,participant_b);
CREATE TABLE IF NOT EXISTS dm_blocks (
 blocker TEXT NOT NULL, blocked TEXT NOT NULL, PRIMARY KEY(blocker,blocked));
CREATE TABLE IF NOT EXISTS dm_archives (
 subject TEXT NOT NULL, pair TEXT NOT NULL REFERENCES dm_conversations(pair),
 PRIMARY KEY(subject,pair));
CREATE TABLE IF NOT EXISTS presence (
 subject TEXT PRIMARY KEY, expires_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims (
 id TEXT PRIMARY KEY REFERENCES resources(id), subject TEXT NOT NULL,
 issued_at TEXT NOT NULL, expires_at TEXT, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS claims_subject ON claims(subject,issued_at,id);
CREATE TABLE IF NOT EXISTS topic_settings (
 topic TEXT PRIMARY KEY REFERENCES resources(id), membership_policy TEXT NOT NULL,
 CHECK(membership_policy IN ('open','approval','invite','closed')));
CREATE TABLE IF NOT EXISTS topic_memberships (
 topic TEXT NOT NULL REFERENCES resources(id), subject TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('admin','member')),
 status TEXT NOT NULL CHECK(status IN ('active','pending','invited','left','removed')),
 joined_at TEXT, invited_by TEXT, PRIMARY KEY(topic,subject));
CREATE INDEX IF NOT EXISTS topic_memberships_subject ON topic_memberships(subject,topic);
CREATE TABLE IF NOT EXISTS topic_bans (
 topic TEXT NOT NULL REFERENCES resources(id), subject TEXT NOT NULL,
 actor TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT, reason TEXT,
 status TEXT NOT NULL CHECK(status IN ('active','lifted')),
 PRIMARY KEY(topic,subject));
CREATE TABLE IF NOT EXISTS system_sources (
 resource_id TEXT PRIMARY KEY REFERENCES resources(id), source_path TEXT NOT NULL UNIQUE,
 rule_id TEXT NOT NULL UNIQUE, source_kind TEXT NOT NULL,
 source_version INTEGER NOT NULL, source_digest TEXT NOT NULL,
 revision_id TEXT NOT NULL REFERENCES revisions(id));
CREATE TABLE IF NOT EXISTS personal_revision_proofs (
 revision_id TEXT PRIMARY KEY REFERENCES revisions(id), subject TEXT NOT NULL,
 kind TEXT NOT NULL, signature TEXT NOT NULL, signed_envelope TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recovery_policies (
 subject TEXT NOT NULL, version INTEGER NOT NULL, encryption_key_id TEXT NOT NULL,
 created_at TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(subject,version));
CREATE TABLE IF NOT EXISTS recovery_envelopes (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, ciphertext_resource TEXT NOT NULL,
 ciphertext_revision TEXT NOT NULL, policy_version INTEGER NOT NULL,
 encryption_key_id TEXT NOT NULL, purpose TEXT NOT NULL, created_at TEXT NOT NULL,
 body TEXT NOT NULL,
 UNIQUE(owner,ciphertext_resource,ciphertext_revision,purpose));
CREATE INDEX IF NOT EXISTS recovery_envelopes_owner ON recovery_envelopes(owner,created_at,id);
CREATE TABLE IF NOT EXISTS custodial_vault (
 subject TEXT PRIMARY KEY, signing_key_id TEXT NOT NULL, encryption_key_id TEXT NOT NULL,
 signing_nonce TEXT, signing_ciphertext TEXT, age_nonce TEXT, age_ciphertext TEXT,
 status TEXT NOT NULL,
 created_at TEXT NOT NULL, destroyed_at TEXT,
 CONSTRAINT custodial_vault_lifecycle_check CHECK (
 status IN ('active','decrypt_only','destroyed') AND
 (status <> 'decrypt_only' OR (signing_nonce IS NULL AND signing_ciphertext IS NULL
 AND age_nonce IS NOT NULL AND age_ciphertext IS NOT NULL))));
CREATE TABLE IF NOT EXISTS custodial_upgrades (
 id TEXT PRIMARY KEY, subject TEXT NOT NULL, credential_id TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','pending_rewrap','failed','completed')),
 expires_at TEXT NOT NULL, challenge TEXT NOT NULL,
 ephemeral_nonce TEXT, ephemeral_ciphertext TEXT, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS custodial_upgrades_subject ON custodial_upgrades(subject,status,expires_at);
CREATE TABLE IF NOT EXISTS legacy_directives (
 subject TEXT PRIMARY KEY, resource_id TEXT NOT NULL UNIQUE REFERENCES resources(id),
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS legacy_directive_versions (
 revision_id TEXT PRIMARY KEY REFERENCES revisions(id), resource_id TEXT NOT NULL,
 subject TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS legacy_states (
 subject TEXT PRIMARY KEY, state TEXT NOT NULL
 CHECK(state IN ('active','unreachable','recovery_requested','legacy')));
CREATE TABLE IF NOT EXISTS sync_checkpoints (
 id TEXT PRIMARY KEY, subject TEXT NOT NULL, credential_id TEXT NOT NULL,
 version INTEGER NOT NULL, expires_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sync_checkpoints_subject ON sync_checkpoints(subject,expires_at);
"""


class SqliteSession(RelationalSession):
    hex_reference_sql = 'msg_hex_id(id)'

    def __init__(self, connection, *, write: bool):
        super().__init__(write=write)
        self._connection = connection

    def execute(
        self, sql: str, parameters: SqlParameters = (), *, write: bool = False
    ) -> QueryResult:
        self.check(write)
        try:
            return SessionQueryResult(self._connection.execute(sql, parameters), self.check)
        except sqlite3.IntegrityError as exc:
            raise Failure('constraint_conflict') from exc


class SqliteMetadataStore:
    def __init__(self, path: Path, *, busy_timeout: float = 10):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout = busy_timeout
        self._current = contextvars.ContextVar('msg_transaction_' + uuid.uuid4().hex, default=None)
        conn = self._connect()
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript(_SCHEMA)
            from msg.storage.topic_event_migration import migrate_topic_events

            conn.execute('BEGIN IMMEDIATE')
            try:
                migrate_topic_events(conn)
                from msg.storage.post_view_migration import migrate_post_views

                migrate_post_views(conn)
                conn.execute('COMMIT')
            except BaseException:
                conn.execute('ROLLBACK')
                raise
        finally:
            conn.close()

    def _connect(self):
        conn = sqlite3.connect(
            self.path, timeout=self.busy_timeout, isolation_level=None, check_same_thread=False
        )
        try:
            conn.create_function('msg_hex_id', 1, hex_id, deterministic=True)
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA synchronous=FULL')
        except BaseException:
            conn.close()
            raise
        return conn

    async def _acquire_write_fence(self):
        # SQLite can release its own lock on an implicit transaction abort.
        # Keep independent processes out until external compensation completes.
        fd = os.open(
            self.path.with_name(self.path.name + '.writer.lock'),
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.busy_timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except BlockingIOError:
                    require(loop.time() < deadline, 'server_busy', retryable=True)
                    await asyncio.sleep(min(0.01, max(0, deadline - loop.time())))
        except BaseException:
            os.close(fd)
            raise

    @asynccontextmanager
    async def transaction(self, *, write):
        existing = self._current.get()
        if existing is not None:
            existing.check(write)
            name = 'nested_' + uuid.uuid4().hex
            existing.execute('SAVEPOINT ' + name)
            rollback_at = len(existing.rollback_effects)
            try:
                yield existing
                existing.execute('RELEASE SAVEPOINT ' + name)
            except BaseException as exc:
                existing.execute('ROLLBACK TO SAVEPOINT ' + name)
                existing.execute('RELEASE SAVEPOINT ' + name)
                await existing.run_rollback_effects(exc, rollback_at)
                raise
            return
        conn = self._connect()
        tx = SqliteSession(conn, write=write)
        token = None
        writer_fence = None
        try:
            if write:
                writer_fence = await self._acquire_write_fence()
            if not write:
                conn.execute('PRAGMA query_only=ON')
            try:
                beginning = asyncio.create_task(
                    asyncio.to_thread(conn.execute, 'BEGIN IMMEDIATE' if write else 'BEGIN')
                )
                try:
                    await asyncio.shield(beginning)
                except asyncio.CancelledError:
                    # A thread cannot be cancelled halfway through SQLite BEGIN.
                    # Wait for it before closing/rolling back the connection.
                    try:
                        await beginning
                    finally:
                        raise
            except sqlite3.OperationalError as exc:
                raise Failure('server_busy', retryable=True) from exc
            token = self._current.set(tx)
            yield tx
            conn.execute('COMMIT')
        except BaseException as exc:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            await tx.run_rollback_effects(exc)
            raise
        finally:
            tx.closed = True
            if token is not None:
                self._current.reset(token)
            try:
                conn.close()
            finally:
                if writer_fence is not None:
                    os.close(writer_fence)

    async def close(self):
        # Connections are scoped to transactions, not retained per account.
        return None

    def backup(self, destination: Path):
        with closing(self._connect()) as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)


class FakeMetadataStore(SqliteMetadataStore):
    """Disposable transactional fake backed by a real isolated SQLite database.

    Uses the same isolation/rollback behavior rather than unrelated dictionaries.
    It deliberately is not an independent SQL correctness oracle.
    """

    def __init__(self, path=None, **kwargs):
        self._temporary = tempfile.TemporaryDirectory(prefix='msg-fake-')
        super().__init__(Path(self._temporary.name) / 'metadata.db', **kwargs)

    async def close(self):
        self._temporary.cleanup()
