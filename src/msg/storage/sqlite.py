"""One transactional metadata session; handlers never own commit rights."""
from __future__ import annotations

import asyncio
import contextvars
import sqlite3
import tempfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from msg.core.codec import canonical, decode, digest, loads, wire
from msg.core.errors import Failure, require
from msg.core.models import (
    AuditEvent, Certificate, CertificateRequest, CertificateRequestState, Credential,
    EffectJob, EmailSettings, Event, Membership, OperationResult, Organization, Page,
    Resource, ResourceRef, Revision, Subject, TransferChunk, TransferSession,
)

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
CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, digest TEXT UNIQUE NOT NULL, previous TEXT, body TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'append_only_audit'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'append_only_audit'); END;
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, dedupe TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, next_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watches (subject TEXT, resource TEXT, PRIMARY KEY(subject,resource));
CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, sender TEXT, recipient TEXT, resource TEXT, event_id TEXT, body TEXT NOT NULL);
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
 status TEXT NOT NULL CHECK(status IN ('active','destroyed')),
 created_at TEXT NOT NULL, destroyed_at TEXT);
CREATE TABLE IF NOT EXISTS custodial_upgrades (
 id TEXT PRIMARY KEY, subject TEXT NOT NULL, credential_id TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','pending_rewrap','failed','completed')),
 expires_at TEXT NOT NULL, challenge TEXT NOT NULL,
 ephemeral_nonce TEXT, ephemeral_ciphertext TEXT, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS custodial_upgrades_subject ON custodial_upgrades(subject,status,expires_at);
"""


class SqliteSession:
    def __init__(self, connection, *, write: bool):
        self._connection, self.write = connection, write
        self.owner_task = asyncio.current_task()
        self.closed = False

    def check(self, write=False):
        require(not self.closed, "transaction_closed")
        require(asyncio.current_task() is self.owner_task, "transaction_cross_task")
        if write:
            require(self.write, "read_only_transaction")

    def execute(self, sql, parameters=(), *, write=False):
        self.check(write)
        try:
            return self._connection.execute(sql, parameters)
        except sqlite3.IntegrityError as exc:
            raise Failure("constraint_conflict") from exc

    def one(self, sql, parameters=()):
        return self.execute(sql, parameters).fetchone()

    def rows(self, sql, parameters=()):
        return self.execute(sql, parameters).fetchall()

    async def resource(self, id):
        row = self.one("SELECT body FROM resources WHERE id=?", (id,))
        require(row is not None, "not_found")
        return decode(Resource, loads(row[0]))

    async def resolve(self, path):
        require(isinstance(path, str) and path.startswith("/"), "invalid_path")
        parts = path.rstrip("/").split("/")[1:]
        require(all(p not in {".", ".."} and "\\" not in p and "\x00" not in p for p in parts), "invalid_path")
        if len(parts) == 2 and parts[0] == "_id":
            return (await self.resource(parts[1])).id
        row = self.one("SELECT id FROM resources WHERE parent IS NULL")
        require(row is not None, "not_initialized")
        rid = row[0]
        if path == "/":
            return rid
        for part in parts:
            require(bool(part), "invalid_path")
            row = self.one("SELECT id FROM resources WHERE parent=? AND name=?", (rid, part))
            require(row is not None, "not_found")
            rid = row[0]
        return rid

    async def path(self, id):
        segments, seen = [], set()
        r = await self.resource(id)
        while r.parent is not None:
            require(r.id not in seen, "parent_cycle")
            seen.add(r.id)
            segments.append(r.name)
            r = await self.resource(r.parent)
        return "/" + "/".join(reversed(segments))

    async def ancestors(self, id):
        result, seen = [], set()
        r = await self.resource(id)
        while r.parent is not None:
            require(r.id not in seen, "parent_cycle")
            seen.add(r.id)
            r = await self.resource(r.parent)
            result.append(r)
        return tuple(reversed(result))

    async def children(self, parent, cursor=None, limit=50):
        require(type(limit) is int and 1 <= limit <= 500, "invalid_limit")
        rows = self.rows("SELECT body FROM resources WHERE parent=? AND id>? ORDER BY id LIMIT ?",
                         (parent, cursor or "", limit + 1))
        values = tuple(decode(Resource, loads(row[0])) for row in rows[:limit])
        return Page(items=values, next_cursor=values[-1].id if len(rows) > limit else None)

    async def revision(self, ref):
        rid = ref.revision or (await self.resource(ref.id)).revision
        row = self.one("SELECT body FROM revisions WHERE id=? AND resource_id=?", (rid, ref.id))
        require(row is not None, "revision_not_found")
        return decode(Revision, loads(row[0]))

    async def history(self, id, cursor=None, limit=50):
        require(1 <= limit <= 500, "invalid_limit")
        rows = self.rows("SELECT body FROM revisions WHERE resource_id=? AND id>? ORDER BY id LIMIT ?",
                         (id, cursor or "", limit+1))
        values = tuple(decode(Revision, loads(r[0])) for r in rows[:limit])
        return Page(items=values, next_cursor=values[-1].id if len(rows)>limit else None)

    async def insert(self, resource):
        self.check(True)
        if resource.parent is not None:
            await self.resource(resource.parent)
        data = wire(resource)
        self.execute("INSERT INTO resources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            resource.id, resource.type, resource.name, resource.parent, resource.owner,
            resource.group, resource.mode, resource.generation, resource.revision,
            resource.state, data['created_at'], data['modified_at'], canonical(resource).decode()), write=True)
        for tag in resource.tags:
            self.execute("INSERT INTO resource_tags VALUES (?,?)", (resource.id,tag), write=True)

    async def replace(self, resource, expected_generation):
        self.check(True)
        old = await self.resource(resource.id)
        require(old.generation == expected_generation, "generation_conflict",
                details={"generation":old.generation, "revision":old.revision})
        require(resource.generation == expected_generation + 1, "invalid_generation")
        require(resource.type == old.type and resource.created_at == old.created_at and
                resource.created_by == old.created_by, "immutable_creation_fact")
        if resource.parent is not None:
            require(resource.parent != resource.id, "parent_cycle")
            parent = await self.resource(resource.parent)
            require(all(a.id != resource.id for a in await self.ancestors(parent.id)), "parent_cycle")
        if any(getattr(resource,k)!=getattr(old,k) for k in ('parent','owner','group','mode','state')):
            self.set_setting('authorization_epoch',self.setting('authorization_epoch',0)+1)
        data = wire(resource)
        changed = self.execute("""UPDATE resources SET name=?,parent=?,owner=?,grp=?,mode=?,
            generation=?,revision=?,state=?,modified_at=?,body=? WHERE id=? AND generation=?""",
            (resource.name, resource.parent, resource.owner, resource.group, resource.mode,
             resource.generation, resource.revision, resource.state, data['modified_at'],
             canonical(resource).decode(), resource.id, expected_generation), write=True).rowcount
        require(changed == 1, "generation_conflict")
        if resource.tags != old.tags:
            self.execute("DELETE FROM resource_tags WHERE resource_id=?", (resource.id,), write=True)
            for tag in resource.tags:
                self.execute("INSERT INTO resource_tags VALUES (?,?)", (resource.id,tag), write=True)

    async def append_revision(self, revision):
        self.check(True)
        for parent in revision.parents:
            await self.revision(ResourceRef(id=revision.resource_id, revision=parent))
        self.execute("INSERT INTO revisions VALUES (?,?,?,?)", (
            revision.id, revision.resource_id, wire(revision.created_at), canonical(revision).decode()), write=True)
        for relation in revision.relations:
            self.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)", (
                revision.id, revision.resource_id, relation.type, relation.target.id,
                relation.target.revision, canonical(relation).decode()), write=True)

    async def subject(self, id):
        row = self.one("SELECT body FROM identities WHERE id=? AND kind='subject'", (id,))
        require(row is not None, "subject_not_found")
        return decode(Subject, loads(row[0]))

    async def organization(self, id):
        row = self.one("SELECT body FROM identities WHERE id=? AND kind='organization'", (id,))
        require(row is not None, "group_not_found")
        return decode(Organization, loads(row[0]))

    async def credential(self, id):
        row = self.one("SELECT body FROM credentials WHERE id=?", (id,))
        require(row is not None, "credential_not_found")
        return decode(Credential, loads(row[0]))

    async def certificate(self, id):
        row = self.one("SELECT body FROM certificates WHERE id=?", (id,))
        require(row is not None, "certificate_not_found")
        return decode(Certificate, loads(row[0]))

    async def certificate_revoked(self, id):
        row = self.one("SELECT revoked FROM certificates WHERE id=?", (id,))
        require(row is not None, "certificate_not_found")
        return bool(row[0])

    async def memberships(self, subject):
        return tuple(decode(Membership, loads(row[0])) for row in
                     self.rows("SELECT body FROM memberships WHERE subject=? ORDER BY org", (subject,)))

    async def request_result(self, subject, id, digest):
        row = self.one("SELECT digest,body FROM results WHERE subject=? AND request_id=?", (subject,id))
        if row is None:
            return None
        require(row[0] == digest, "idempotency_conflict")
        return decode(OperationResult, loads(row[1]))

    async def save_result(self, subject, digest, result):
        self.execute("INSERT INTO results VALUES (?,?,?,?)", (
            subject, result.request_id, digest, canonical(result).decode()), write=True)

    async def update_identity(self, record, expected_generation):
        self.check(True)
        if isinstance(record, Membership):
            self.set_setting('authorization_epoch',self.setting('authorization_epoch',0)+1)
            table, where, args = 'memberships', 'org=? AND subject=?', (record.organization_id,record.subject_id)
            generation = record.version
        elif isinstance(record, EmailSettings):
            table, where, args = 'emails', 'subject=?', (record.subject_id,)
            generation = expected_generation + 1
        else:
            table, where, args = 'identities', 'id=?', (record.resource_id,)
            generation = record.auth_version if isinstance(record, Subject) else record.membership_version
        row = self.one(f"SELECT generation FROM {table} WHERE {where}", args)
        require((row is None and expected_generation == -1) or
                (row is not None and row[0] == expected_generation), "generation_conflict")
        payload = canonical(record).decode()
        if row is None:
            if table == 'identities':
                self.execute("INSERT INTO identities VALUES (?,?,?,?)", (*args,
                    'subject' if isinstance(record,Subject) else 'organization', generation,payload), write=True)
            elif table == 'memberships':
                self.execute("INSERT INTO memberships VALUES (?,?,?,?)", (*args,generation,payload), write=True)
            else:
                self.execute("INSERT INTO emails VALUES (?,?,?)", (*args,generation,payload), write=True)
        else:
            self.execute(f"UPDATE {table} SET generation=?,body=? WHERE {where}",
                         (generation,payload,*args), write=True)

    async def save_credential(self, credential, expected_auth_version):
        subject = await self.subject(credential.subject_id)
        require(subject.auth_version == expected_auth_version, "auth_version_conflict")
        old=self.one("SELECT body FROM credentials WHERE id=?",(credential.id,))
        if old is not None:
            previous=decode(Credential,loads(old[0]))
            require(previous.subject_id==credential.subject_id and previous.kind==credential.kind and
                    previous.verifier==credential.verifier,"credential_identity_immutable")
        self.execute("INSERT INTO credentials VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                     (credential.id,credential.subject_id,canonical(credential).decode()),write=True)

    async def csr(self, id):
        row = self.one("SELECT body FROM csrs WHERE id=?",(id,))
        require(row is not None,"csr_not_found")
        return decode(CertificateRequest,loads(row[0]))

    async def csr_state(self, id):
        row=self.one("SELECT state_body FROM csrs WHERE id=?",(id,))
        require(row is not None,"csr_not_found")
        return decode(CertificateRequestState,loads(row[0]))

    async def save_csr(self, request):
        state=CertificateRequestState(request_id=request.resource_id,status='pending',generation=0)
        self.execute("INSERT INTO csrs VALUES (?,?,?,?,?)",(request.resource_id,0,'pending',
            canonical(request).decode(),canonical(state).decode()),write=True)

    async def transition_csr(self, state, expected_generation):
        require(state.generation == expected_generation+1,"invalid_generation")
        old=await self.csr_state(state.request_id)
        require(old.status=='pending',"csr_not_pending")
        require(old.generation==expected_generation,"generation_conflict")
        self.execute("UPDATE csrs SET generation=?,state=?,state_body=? WHERE id=? AND generation=?",
            (state.generation,state.status,canonical(state).decode(),state.request_id,expected_generation),write=True)

    async def register_certificate(self, certificate, csr_id, expected_generation):
        self.execute("INSERT INTO certificates VALUES (?,?,?,?,?)",(
            certificate.resource_id,certificate.subject_id,certificate.parent_certificate_id,0,
            canonical(certificate).decode()),write=True)
        if csr_id is not None:
            self.set_setting('certificate_request:'+certificate.resource_id,csr_id)
            await self.transition_csr(CertificateRequestState(request_id=csr_id,status='issued',
                generation=expected_generation+1,certificate_id=certificate.resource_id),expected_generation)

    async def revoke_certificate(self, id, event):
        await self.certificate(id)
        self.execute("UPDATE certificates SET revoked=1 WHERE id=?",(id,),write=True)
        self.set_setting('authorization_epoch',self.setting('authorization_epoch',0)+1)
        await self.append_audit(event)

    async def transfer(self,id):
        row=self.one("SELECT body FROM transfers WHERE id=?",(id,))
        require(row is not None,"transfer_not_found")
        return decode(TransferSession,loads(row[0]))

    async def save_transfer(self,session,expected_generation):
        if expected_generation is None:
            self.execute("INSERT INTO transfers (id,subject,generation,body) VALUES (?,?,?,?)",
                         (session.id,session.subject_id,session.generation,canonical(session).decode()),write=True)
        else:
            require(session.generation==expected_generation+1,"invalid_generation")
            count=self.execute("UPDATE transfers SET generation=?,body=? WHERE id=? AND generation=?",
                (session.generation,canonical(session).decode(),session.id,expected_generation),write=True).rowcount
            require(count==1,"generation_conflict")

    async def put_chunk(self,chunk):
        self.check(True)
        start,end=chunk.offset,chunk.offset+chunk.content.size
        rows=self.rows("SELECT offset,length,body FROM chunks WHERE transfer_id=? AND offset<? AND offset+length>?",
                       (chunk.transfer_id,end,start))
        if rows:
            require(len(rows)==1 and rows[0][0]==start and rows[0][1]==chunk.content.size and
                    decode(TransferChunk,loads(rows[0][2])).content==chunk.content,"chunk_conflict")
            return
        self.execute("INSERT INTO chunks VALUES (?,?,?,?)",(chunk.transfer_id,start,chunk.content.size,
                     canonical(chunk).decode()),write=True)

    async def missing_ranges(self,id,cursor=None,limit=50):
        session=await self.transfer(id)
        require(session.expected_size is not None,"size_required")
        require(1<=limit<=500,"invalid_limit")
        start=int(cursor or 0)
        require(0<=start<=session.expected_size,"invalid_cursor")
        gaps=[]
        for row in self.execute("SELECT offset,length FROM chunks WHERE transfer_id=? AND offset+length>? ORDER BY offset",(id,start)):
            if row[0]>start:
                gaps.append((start,row[0]))
                if len(gaps)>limit:
                    return Page(items=tuple(gaps[:limit]),next_cursor=str(gaps[limit][0]))
            start=max(start,row[0]+row[1])
        if start<session.expected_size:
            gaps.append((start,session.expected_size))
        return Page(items=tuple(gaps[:limit]),next_cursor=str(gaps[limit][0]) if len(gaps)>limit else None)

    async def append_event(self,event):
        self.execute("INSERT INTO events (id,body) VALUES (?,?)",(event.id,canonical(event).decode()),write=True)

    async def append_audit(self,event):
        previous=self.one("SELECT digest FROM audit ORDER BY seq DESC LIMIT 1")
        prev=previous[0] if previous else None
        data=wire(replace(event,previous_digest=prev,entry_digest=''))
        data.pop('entry_digest')
        entry=replace(event,previous_digest=prev,entry_digest=digest(data))
        self.execute("INSERT INTO audit (digest,previous,body) VALUES (?,?,?)",
                     (entry.entry_digest,prev,canonical(entry).decode()),write=True)

    async def enqueue(self,job):
        self.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)",(job.id,job.dedupe_key,job.kind,
            job.state,wire(job.next_attempt_at),canonical(job).decode()),write=True)

    async def job(self,id):
        row=self.one("SELECT body FROM jobs WHERE id=?",(id,))
        require(row is not None,'job_not_found')
        return decode(EffectJob,loads(row[0]))

    async def save_job(self,job):
        self.execute("UPDATE jobs SET state=?,next_at=?,body=? WHERE id=?",(job.state,
            wire(job.next_attempt_at),canonical(job).decode(),job.id),write=True)

    def setting(self,key,default=None):
        row=self.one("SELECT value FROM settings WHERE key=?",(key,))
        return loads(row[0]) if row else default

    def set_setting(self,key,value):
        self.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key,canonical(value).decode()),write=True)


class SqliteMetadataStore:
    def __init__(self,path: Path, *, busy_timeout: float=10):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.busy_timeout=busy_timeout
        self._current=contextvars.ContextVar('msg_transaction_'+uuid.uuid4().hex,default=None)
        conn=self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def _connect(self):
        conn=sqlite3.connect(self.path,timeout=self.busy_timeout,isolation_level=None,check_same_thread=False)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @asynccontextmanager
    async def transaction(self, *, write):
        existing=self._current.get()
        if existing is not None:
            existing.check(write)
            name='nested_'+uuid.uuid4().hex
            existing.execute('SAVEPOINT '+name)
            try:
                yield existing
                existing.execute('RELEASE SAVEPOINT '+name)
            except BaseException:
                existing.execute('ROLLBACK TO SAVEPOINT '+name)
                existing.execute('RELEASE SAVEPOINT '+name)
                raise
            return
        conn=self._connect()
        tx=SqliteSession(conn,write=write)
        token=None
        try:
            if not write:
                conn.execute("PRAGMA query_only=ON")
            try:
                beginning=asyncio.create_task(asyncio.to_thread(conn.execute,"BEGIN IMMEDIATE" if write else "BEGIN"))
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
                raise Failure("server_busy",retryable=True) from exc
            token=self._current.set(tx)
            yield tx
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            tx.closed=True
            if token is not None:
                self._current.reset(token)
            conn.close()

    async def close(self):
        # Connections are scoped to transactions, not retained per account.
        return None

    def backup(self,destination: Path):
        source=self._connect()
        target=sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()


class FakeMetadataStore(SqliteMetadataStore):
    """Disposable transactional fake backed by a real isolated SQLite database.

    Uses the same isolation/rollback behavior rather than unrelated dictionaries.
    It deliberately is not an independent SQL correctness oracle.
    """
    def __init__(self,path=None,**kwargs):
        self._temporary=tempfile.TemporaryDirectory(prefix='msg-fake-')
        super().__init__(Path(self._temporary.name)/'metadata.db',**kwargs)

    async def close(self):
        self._temporary.cleanup()
