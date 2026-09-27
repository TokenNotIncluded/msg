"""PostgreSQL metadata store with the same transactional session contract as SQLite.

All writes take one transaction-scoped advisory lock. The existing executor relies on
SQLite's BEGIN IMMEDIATE serialization for request replay, audit chaining and job
deduplication; this lock preserves that ordering across processes and hosts.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import subprocess
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from msg.core.errors import Failure
from msg.storage.sqlite import SqliteSession

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
INSERT INTO schema_version (version) VALUES (1) ON CONFLICT DO NOTHING;
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
CREATE TABLE IF NOT EXISTS chunks (transfer_id TEXT NOT NULL, "offset" BIGINT NOT NULL, length BIGINT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(transfer_id,"offset"));
CREATE TABLE IF NOT EXISTS events (seq BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, id TEXT UNIQUE NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (seq BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, digest TEXT UNIQUE NOT NULL, previous TEXT, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_accounts (
 id TEXT PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('subject','order_escrow','bounty_escrow')),
 subject_id TEXT REFERENCES identities(id), source_id TEXT,
 CHECK((kind='subject' AND subject_id=id AND source_id IS NULL) OR
       (kind IN ('order_escrow','bounty_escrow') AND subject_id IS NULL AND source_id IS NOT NULL)),
 UNIQUE(kind,source_id));
CREATE TABLE IF NOT EXISTS money_accounts (
 -- Historical column name; values are LedgerAccount IDs, not always subjects.
 subject_id TEXT NOT NULL CONSTRAINT money_accounts_ledger_account_fkey REFERENCES ledger_accounts(id),
 currency_id TEXT NOT NULL CHECK(currency_id='primary'),
 PRIMARY KEY(subject_id,currency_id));
CREATE TABLE IF NOT EXISTS money_bank_roles (
 subject_id TEXT PRIMARY KEY REFERENCES identities(id),
 status TEXT NOT NULL CHECK(status IN ('active','revoked')),
 granted_at TEXT NOT NULL, granted_by TEXT NOT NULL CHECK(granted_by='u_root'),
 revoked_at TEXT);
CREATE TABLE IF NOT EXISTS money_ledger (
 seq BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
 id TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL CHECK(kind IN ('mint','burn','transfer','redeem','refund')),
 currency_id TEXT NOT NULL CHECK(currency_id='primary'),
 amount_minor BIGINT NOT NULL CHECK(amount_minor>0),
 debit_account TEXT CONSTRAINT money_ledger_debit_ledger_account_fkey REFERENCES ledger_accounts(id),
 credit_account TEXT CONSTRAINT money_ledger_credit_ledger_account_fkey REFERENCES ledger_accounts(id),
 actor TEXT NOT NULL,
 request_id TEXT NOT NULL,
 reference TEXT,
 committed_at TEXT NOT NULL,
 policy_version INTEGER NOT NULL CHECK(policy_version>0),
 policy_digest TEXT NOT NULL,
 receipt TEXT NOT NULL,
 CHECK((kind='mint' AND debit_account IS NULL AND credit_account IS NOT NULL) OR
       (kind='burn' AND debit_account IS NOT NULL AND credit_account IS NULL) OR
       (kind IN ('transfer','redeem','refund') AND debit_account IS NOT NULL AND
        credit_account IS NOT NULL AND debit_account<>credit_account)),
 UNIQUE(actor,request_id));
CREATE INDEX IF NOT EXISTS money_ledger_debit ON money_ledger(debit_account,seq);
CREATE INDEX IF NOT EXISTS money_ledger_credit ON money_ledger(credit_account,seq);
CREATE OR REPLACE FUNCTION msg_money_ledger_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'append_only_money_ledger' USING ERRCODE = '23514'; END;
$$;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'money_ledger_no_update'
                AND tgrelid = 'money_ledger'::regclass) THEN
  CREATE TRIGGER money_ledger_no_update BEFORE UPDATE OR DELETE ON money_ledger
   FOR EACH ROW EXECUTE FUNCTION msg_money_ledger_append_only();
 END IF;
END $$;
CREATE TABLE IF NOT EXISTS store_packages (
 id TEXT PRIMARY KEY, listing_id TEXT NOT NULL, listing_revision TEXT NOT NULL,
 seller TEXT NOT NULL, revision TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
 manifest TEXT NOT NULL, payload_refs TEXT NOT NULL, digest TEXT NOT NULL,
 total_size BIGINT NOT NULL, delivery_mode TEXT NOT NULL, deposited_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bounty_listings (
 listing_id TEXT PRIMARY KEY REFERENCES resources(id),
 publisher TEXT NOT NULL REFERENCES identities(id),
 escrow_subject TEXT NOT NULL UNIQUE CONSTRAINT bounty_listings_escrow_account_fkey REFERENCES ledger_accounts(id),
 reward_minor BIGINT NOT NULL CHECK(reward_minor>0),
 budget_minor BIGINT NOT NULL CHECK(budget_minor>0),
 max_claims INTEGER NOT NULL CHECK(max_claims>0),
 claim_limit_per_subject INTEGER NOT NULL CHECK(claim_limit_per_subject>0),
 verifier_id TEXT NOT NULL, verifier_version INTEGER NOT NULL CHECK(verifier_version>0),
 eligibility TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('draft','active','paused','closed')),
 pause_reason TEXT, expires_at TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS bounty_listings_publisher ON bounty_listings(publisher,listing_id);
CREATE TABLE IF NOT EXISTS bounty_challenges (
 id TEXT PRIMARY KEY,
 listing_id TEXT NOT NULL REFERENCES bounty_listings(listing_id),
 claimant TEXT NOT NULL REFERENCES identities(id),
 key_id TEXT NOT NULL, nonce TEXT NOT NULL,
 issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 verifier_version INTEGER NOT NULL CHECK(verifier_version>0),
 payload TEXT NOT NULL, consumed_at TEXT);
CREATE INDEX IF NOT EXISTS bounty_challenges_claimant ON bounty_challenges(listing_id,claimant);
CREATE TABLE IF NOT EXISTS bounty_claims (
 id TEXT PRIMARY KEY,
 listing_id TEXT NOT NULL REFERENCES bounty_listings(listing_id),
 claimant TEXT NOT NULL REFERENCES identities(id),
 challenge_id TEXT NOT NULL UNIQUE REFERENCES bounty_challenges(id),
 proof_digest TEXT NOT NULL, status TEXT NOT NULL CHECK(status='paid'),
 reward_minor BIGINT NOT NULL CHECK(reward_minor>0),
 transaction_id TEXT NOT NULL UNIQUE REFERENCES money_ledger(id),
 claimed_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS bounty_claims_claimant ON bounty_claims(listing_id,claimant);
CREATE TABLE IF NOT EXISTS store_orders (
 id TEXT PRIMARY KEY,
 buyer TEXT NOT NULL REFERENCES identities(id),
 seller TEXT NOT NULL REFERENCES identities(id),
 listing_id TEXT NOT NULL REFERENCES resources(id),
 listing_revision TEXT NOT NULL,
 package_id TEXT, package_revision TEXT, package_digest TEXT,
 quantity INTEGER NOT NULL CHECK(quantity>0),
 unit_price_minor BIGINT NOT NULL CHECK(unit_price_minor>0),
 total_price_minor BIGINT NOT NULL CHECK(total_price_minor>0),
 currency_id TEXT NOT NULL CHECK(currency_id='primary'),
 escrow_subject TEXT NOT NULL UNIQUE CONSTRAINT store_orders_escrow_account_fkey REFERENCES ledger_accounts(id),
 escrow_policy TEXT NOT NULL, dispute_policy TEXT NOT NULL,
 terms_digest TEXT NOT NULL, delivery_target TEXT NOT NULL,
 payment_intent_digest TEXT NOT NULL,
 payment_transaction_id TEXT NOT NULL UNIQUE REFERENCES money_ledger(id),
 state TEXT NOT NULL CHECK(state IN
  ('funded','delivered','accepted','settled','cancelled','refunded','disputed')),
 created_at TEXT NOT NULL, funded_at TEXT NOT NULL,
 delivered_at TEXT, settled_at TEXT, receipt_refs TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS store_orders_buyer ON store_orders(buyer,id);
CREATE INDEX IF NOT EXISTS store_orders_seller ON store_orders(seller,id);
CREATE INDEX IF NOT EXISTS store_orders_listing ON store_orders(listing_id,state);
CREATE TABLE IF NOT EXISTS store_deliveries (
 id TEXT PRIMARY KEY,
 order_id TEXT NOT NULL UNIQUE REFERENCES store_orders(id),
 recipient_subject TEXT NOT NULL REFERENCES identities(id),
 kind TEXT NOT NULL,
 payload_refs TEXT NOT NULL, manifest TEXT NOT NULL,
 package_digest TEXT NOT NULL, delivery_digest TEXT NOT NULL,
 channel TEXT NOT NULL CHECK(channel='site'),
 state TEXT NOT NULL CHECK(state IN ('prepared','claimed')),
 prepared_at TEXT NOT NULL, claimed_at TEXT, receipt TEXT);
CREATE TABLE IF NOT EXISTS order_escrow_decisions (
 order_id TEXT PRIMARY KEY REFERENCES store_orders(id),
 id TEXT NOT NULL UNIQUE, body TEXT NOT NULL, signature TEXT NOT NULL,
 source_proof TEXT NOT NULL,
 transaction_id TEXT NOT NULL UNIQUE REFERENCES money_ledger(id));
CREATE OR REPLACE FUNCTION msg_escrow_decision_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'append_only_escrow_decision' USING ERRCODE = '23514'; END;
$$;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'escrow_decision_no_update'
                AND tgrelid = 'order_escrow_decisions'::regclass) THEN
  CREATE TRIGGER escrow_decision_no_update BEFORE UPDATE OR DELETE ON order_escrow_decisions
   FOR EACH ROW EXECUTE FUNCTION msg_escrow_decision_append_only();
 END IF;
END $$;
CREATE TABLE IF NOT EXISTS server_offers (
 offer_id TEXT PRIMARY KEY,
 resource_kind TEXT NOT NULL, unit TEXT NOT NULL,
 price_minor BIGINT NOT NULL CHECK(price_minor>0),
 min_quantity BIGINT NOT NULL CHECK(min_quantity>0),
 max_quantity BIGINT NOT NULL CHECK(max_quantity>=min_quantity),
 entitlement_kind TEXT NOT NULL,
 duration_seconds BIGINT CHECK(duration_seconds IS NULL OR duration_seconds>0),
 enabled BOOLEAN NOT NULL DEFAULT FALSE,
 price_revision TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resource_entitlements (
 id TEXT PRIMARY KEY,
 subject_id TEXT NOT NULL REFERENCES identities(id),
 offer_id TEXT NOT NULL REFERENCES server_offers(offer_id),
 purchase_request_id TEXT NOT NULL,
 quantity BIGINT NOT NULL CHECK(quantity>0),
 entitlement_kind TEXT NOT NULL,
 granted_at TEXT NOT NULL, expires_at TEXT,
 redeem_transaction_id TEXT UNIQUE REFERENCES money_ledger(id),
 UNIQUE(subject_id,purchase_request_id));
CREATE OR REPLACE FUNCTION msg_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'append_only_audit' USING ERRCODE = '23514'; END;
$$;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'audit_no_update'
                AND tgrelid = 'audit'::regclass) THEN
  CREATE TRIGGER audit_no_update BEFORE UPDATE OR DELETE ON audit
   FOR EACH ROW EXECUTE FUNCTION msg_audit_append_only();
 END IF;
END $$;
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, dedupe TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, next_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watches (subject TEXT, resource TEXT, PRIMARY KEY(subject,resource));
CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, sender TEXT, recipient TEXT, resource TEXT, event_id TEXT, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS webhook_endpoints (
 subject TEXT PRIMARY KEY, url TEXT NOT NULL, nonce TEXT NOT NULL, ciphertext TEXT NOT NULL,
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), generation INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reactions (subject TEXT, resource TEXT, kind TEXT, revision TEXT NOT NULL DEFAULT '', body TEXT NOT NULL, PRIMARY KEY(subject,resource,kind,revision));
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
CREATE TABLE IF NOT EXISTS handoffs (
 id TEXT PRIMARY KEY, from_subject TEXT NOT NULL, to_subject TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected','cancelled')),
 generation INTEGER NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS handoffs_from ON handoffs(from_subject,id);
CREATE INDEX IF NOT EXISTS handoffs_to ON handoffs(to_subject,id);
CREATE TABLE IF NOT EXISTS collaboration_leases (
 id TEXT PRIMARY KEY, holder TEXT NOT NULL, target TEXT NOT NULL REFERENCES resources(id),
 status TEXT NOT NULL CHECK(status IN ('active','released')),
 generation INTEGER NOT NULL, expires_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS collaboration_leases_holder ON collaboration_leases(holder,id);
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
 status TEXT NOT NULL CHECK(status IN ('active','destroyed')),
 created_at TEXT NOT NULL, destroyed_at TEXT);
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


# Kept as an import alias for the existing storage constructor and callers.
from msg.storage.ledger_migration import migrate_ledger_accounts as _migrate_ledger_accounts


def _postgres_sql(sql: str, *, has_parameters: bool = False) -> str:
    """Translate shared SQLite SQL without altering quoted text or comments."""
    ignore = re.match(r'(?is)^(\s*)INSERT\s+OR\s+IGNORE\s+INTO\b', sql)
    if ignore:
        sql = sql[:ignore.start()] + ignore.group(1) + 'INSERT INTO' + sql[ignore.end():]
        ending = re.search(r'\s*;\s*$', sql)
        if ending:
            sql = sql[:ending.start()] + ' ON CONFLICT DO NOTHING' + sql[ending.start():]
        else:
            sql += ' ON CONFLICT DO NOTHING'

    chunks_query = bool(re.search(r'\bchunks\b', sql, flags=re.IGNORECASE))
    output: list[str] = []
    position = 0
    state = 'sql'
    while position < len(sql):
        char = sql[position]
        next_char = sql[position + 1] if position + 1 < len(sql) else ''
        if state == 'sql':
            if char == "'":
                state = 'single'
            elif char == '"':
                state = 'double'
            elif char == '-' and next_char == '-':
                state = 'line_comment'
            elif char == '/' and next_char == '*':
                state = 'block_comment'
            elif char == '?':
                output.append('%s')
                position += 1
                continue
            elif (chunks_query and sql[position:position + 6].lower() == 'offset'
                  and (position == 0 or not (sql[position - 1].isalnum() or sql[position - 1] == '_'))
                  and (position + 6 == len(sql) or not (
                      sql[position + 6].isalnum() or sql[position + 6] == '_'))):
                output.append('"offset"')
                position += 6
                continue
        elif state == 'single' and char == "'":
            if next_char == "'":
                output.extend((char, next_char))
                position += 2
                continue
            state = 'sql'
        elif state == 'double' and char == '"':
            if next_char == '"':
                output.extend((char, next_char))
                position += 2
                continue
            state = 'sql'
        elif state == 'line_comment' and char == '\n':
            state = 'sql'
        elif state == 'block_comment' and char == '*' and next_char == '/':
            output.extend((char, next_char))
            position += 2
            state = 'sql'
            continue
        output.append('%%' if has_parameters and char == '%' else char)
        position += 1
    return ''.join(output)


class PostgresSession(SqliteSession):
    def __init__(self, connection, *, write: bool):
        super().__init__(connection, write=write)
        self.pending_effect_ids: list[str] = []

    def execute(self, sql, parameters=(), *, write=False):
        self.check(write)
        try:
            # With an empty parameter tuple psycopg still parses literal '%' as a
            # placeholder; SQL such as LIKE 'policy:%' must be sent unchanged.
            return self._connection.execute(
                _postgres_sql(sql, has_parameters=bool(parameters)), parameters or None)
        except psycopg.errors.IntegrityError as exc:
            raise Failure("constraint_conflict") from exc
        except (psycopg.errors.DeadlockDetected, psycopg.errors.LockNotAvailable,
                psycopg.errors.QueryCanceled) as exc:
            raise Failure("server_busy", retryable=True) from exc

    async def enqueue(self, job):
        await super().enqueue(job)
        self.pending_effect_ids.append(job.id)


class PostgresMetadataStore:
    """One connection per transaction; no mutable process-local authority state."""

    def __init__(self, dsn: str, *, signal=None, initialize: bool = True):
        self.dsn = dsn
        self.signal = signal
        self._current = contextvars.ContextVar('msg_pg_transaction_' + uuid.uuid4().hex, default=None)
        if initialize:
            with self._connect() as conn:
                # Multiple daemon workers may initialize the same fresh database.
                conn.execute('SELECT pg_advisory_xact_lock(725274758, 1886265951)')
                conn.execute(_SCHEMA)
                _migrate_ledger_accounts(conn)

    def _connect(self):
        return psycopg.connect(self.dsn, autocommit=False)

    async def _connect_async(self):
        pending = asyncio.create_task(asyncio.to_thread(self._connect))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            # Cancelling a coroutine does not stop a connection attempt in a thread.
            # Consume its result and close it instead of leaking a server connection.
            try:
                connection = await pending
                await asyncio.to_thread(connection.close)
            finally:
                raise

    @asynccontextmanager
    async def transaction(self, *, write):
        existing = self._current.get()
        if existing is not None:
            existing.check(write)
            name = 'nested_' + uuid.uuid4().hex
            existing.execute('SAVEPOINT ' + name)
            pending_at_entry = len(existing.pending_effect_ids)
            try:
                yield existing
                existing.execute('RELEASE SAVEPOINT ' + name)
            except BaseException:
                existing.execute('ROLLBACK TO SAVEPOINT ' + name)
                existing.execute('RELEASE SAVEPOINT ' + name)
                del existing.pending_effect_ids[pending_at_entry:]
                raise
            return

        conn = await self._connect_async()
        tx = PostgresSession(conn, write=write)
        token = None
        committed = False
        try:
            if write:
                # A fixed application-wide key serializes writers, including separate workers.
                # Waiting happens off the event loop so the holder can finish its request.
                async def acquire():
                    def blocking():
                        conn.execute("SET lock_timeout = '10s'")
                        conn.execute('SELECT pg_advisory_xact_lock(725274758, 1886265951)')
                    task = asyncio.create_task(asyncio.to_thread(blocking))
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        # The worker thread must finish before this connection is closed.
                        try:
                            await task
                        finally:
                            raise
                try:
                    await acquire()
                except (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled) as exc:
                    raise Failure('server_busy', retryable=True) from exc
            else:
                conn.execute('SET TRANSACTION READ ONLY')
            token = self._current.set(tx)
            yield tx
            conn.commit()
            committed = True
        except BaseException:
            conn.rollback()
            raise
        finally:
            tx.closed = True
            if token is not None:
                self._current.reset(token)
            conn.close()
        if committed and self.signal is not None and tx.pending_effect_ids:
            # The durable jobs table is authoritative. A signal only reduces worker latency.
            try:
                await self.signal.publish_pending(tuple(tx.pending_effect_ids))
            except Exception:
                logging.getLogger(__name__).exception('Valkey job signal failed; jobs remain durable')

    async def close(self):
        return None

    def backup(self, destination: Path):
        """Write a pg_dump custom-format archive at *destination*.

        The caller holds the store's write transaction while copying content trees.
        Credentials travel via the child environment, never command arguments.
        """
        destination = Path(destination)
        fields = conninfo_to_dict(self.dsn)
        password = fields.pop('password', None)
        env = os.environ.copy()
        if password is not None:
            env['PGPASSWORD'] = password
        safe_dsn = make_conninfo(**fields)
        subprocess.run(['pg_dump', '--format=custom', '--no-owner', '--no-acl',
                        '--file', str(destination), '--dbname', safe_dsn],
                       env=env, check=True, capture_output=True)
