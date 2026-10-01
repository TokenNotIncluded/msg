"""Shared relational domain methods, with no driver or transaction ownership.

This SQL-oriented monolith shares the supported placeholder/query subset, not
an imaginary database-independent object store. Adapters own SQL translation,
schema migration, connections, writer fences, savepoints and commit/rollback.
Only this session's owning task can access its transaction or results.
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from dataclasses import replace

from msg.core.codec import canonical, decode, digest, loads, wire
from msg.core.errors import require
from msg.core.identifiers import HEX_ID_PATTERN, PREFIXED_HEX_ID_PATTERN
from msg.core.models import (
    Certificate,
    CertificateRequest,
    CertificateRequestState,
    Credential,
    EffectJob,
    EmailSettings,
    Membership,
    OperationResult,
    Organization,
    Page,
    Resource,
    ResourceRef,
    Revision,
    Subject,
    TransferChunk,
    TransferSession,
)
from msg.core.query import QueryResult, SettingValue, SqlParameters, SqlRow


class RelationalSession(ABC):
    # Adapters may supply an equivalent database expression. Keep the prefix
    # grammar aligned with the public reference converter, including legacy IDs.
    hex_reference_sql = (
        f"CASE WHEN id ~ '^{HEX_ID_PATTERN}$' THEN id "
        f"WHEN id ~ '^{PREFIXED_HEX_ID_PATTERN}$' THEN right(id,32) "
        "ELSE left(encode(sha256(convert_to('msg.hex-reference/v1','UTF8') "
        "|| decode('00','hex') || convert_to(id,'UTF8')),'hex'),32) END"
    )

    def __init__(self, *, write: bool):
        self.write = write
        self.owner_task = asyncio.current_task()
        self.closed = False
        self.rollback_effects = []

    def on_rollback(self, effect):
        """Compensate external work if this transaction or savepoint aborts."""
        self.check(write=True)
        self.rollback_effects.append(effect)

    async def run_rollback_effects(self, cause, start=0):
        effects = self.rollback_effects[start:]
        del self.rollback_effects[start:]
        if not effects:
            return

        async def compensate():
            for effect in reversed(effects):
                try:
                    await effect()
                except BaseException as exc:
                    # Keep the primary error and try every independent cleanup.
                    cause.add_note('rollback compensation failed: ' + type(exc).__name__)

        # A second cancellation must not abandon a pin operation (including its
        # worker thread) and release the writer fence before it finishes.
        pending = asyncio.create_task(compensate())
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                pass
        pending.result()

    def check(self, write=False):
        require(not self.closed, 'transaction_closed')
        require(asyncio.current_task() is self.owner_task, 'transaction_cross_task')
        if write:
            require(self.write, 'read_only_transaction')

    @abstractmethod
    def execute(
        self, sql: str, parameters: SqlParameters = (), *, write: bool = False
    ) -> QueryResult:
        """Run adapter-normalized SQL in this store-owned transaction."""
        raise NotImplementedError

    def one(self, sql: str, parameters: SqlParameters = ()) -> SqlRow | None:
        return self.execute(sql, parameters).fetchone()

    def rows(self, sql: str, parameters: SqlParameters = ()) -> list[SqlRow]:
        return self.execute(sql, parameters).fetchall()

    async def resource(self, id):
        row = self.one('SELECT body FROM resources WHERE id=?', (id,))
        if isinstance(id, str) and re.fullmatch(HEX_ID_PATTERN, id):
            rows = self.rows(
                'SELECT body FROM resources WHERE ' + self.hex_reference_sql + ' = ? LIMIT 2',
                (id,),
            )
            require(len(rows) <= 1, 'ambiguous_resource_id')
            row = rows[0] if rows else None
        require(row is not None, 'not_found')
        return decode(Resource, loads(row[0]))

    async def resolve(self, path):
        require(isinstance(path, str) and path.startswith('/'), 'invalid_path')
        parts = path.rstrip('/').split('/')[1:]
        require(
            all(p not in {'.', '..'} and '\\' not in p and '\x00' not in p for p in parts),
            'invalid_path',
        )
        if len(parts) == 2 and parts[0] == '_id':
            return (await self.resource(parts[1])).id
        if parts and parts[-1].startswith('*'):
            resource = await self.resource(parts[-1][1:])
            if len(parts) > 1:
                require(
                    resource.parent == await self.resolve('/' + '/'.join(parts[:-1])), 'not_found'
                )
            return resource.id
        row = self.one('SELECT id FROM resources WHERE parent IS NULL')
        require(row is not None, 'not_initialized')
        rid = row[0]
        if path == '/':
            return rid
        for part in parts:
            require(bool(part), 'invalid_path')
            row = self.one('SELECT id FROM resources WHERE parent=? AND name=?', (rid, part))
            require(row is not None, 'not_found')
            rid = row[0]
        return rid

    async def resolve_migrated(self, path):
        """Read-only old names in stable parent namespaces; never used by writes.

        Current children shadow old names, including a recreated directory.
        A renamed parent does not require copying aliases for every descendant.
        Resolution identifies a target only: callers must authorize it currently.
        """
        require(isinstance(path, str) and path.startswith('/'), 'invalid_path')
        parts = path.rstrip('/').split('/')[1:]
        require(
            len(parts) <= 256
            and all(
                p and p not in {'.', '..'} and '\\' not in p and '\x00' not in p for p in parts
            ),
            'invalid_path',
        )
        rid = await self.resolve('/')
        for part in parts:
            row = self.one('SELECT id FROM resources WHERE parent=? AND name=?', (rid, part))
            if row is None:
                row = self.one(
                    'SELECT resource_id FROM resource_path_aliases WHERE parent_id=? AND name=?',
                    (rid, part),
                )
            require(row is not None, 'not_found')
            rid = row[0]
        return (await self.resource(rid)).id

    async def path(self, id):
        segments, seen = [], set()
        r = await self.resource(id)
        while r.parent is not None:
            require(r.id not in seen, 'parent_cycle')
            seen.add(r.id)
            segments.append(r.name)
            r = await self.resource(r.parent)
        return '/' + '/'.join(reversed(segments))

    async def ancestors(self, id):
        result, seen = [], set()
        r = await self.resource(id)
        while r.parent is not None:
            require(r.id not in seen, 'parent_cycle')
            seen.add(r.id)
            r = await self.resource(r.parent)
            result.append(r)
        return tuple(reversed(result))

    async def children(self, parent, cursor=None, limit=50):
        require(type(limit) is int and 1 <= limit <= 500, 'invalid_limit')
        rows = self.rows(
            'SELECT body FROM resources WHERE parent=? AND id>? ORDER BY id LIMIT ?',
            (parent, cursor or '', limit + 1),
        )
        values = tuple(decode(Resource, loads(row[0])) for row in rows[:limit])
        return Page(items=values, next_cursor=values[-1].id if len(rows) > limit else None)

    async def revision(self, ref):
        rid = ref.revision or (await self.resource(ref.id)).revision
        resource_id = (await self.resource(ref.id)).id
        row = self.one(
            'SELECT body FROM revisions WHERE id=? AND resource_id=?', (rid, resource_id)
        )
        if isinstance(rid, str) and re.fullmatch(HEX_ID_PATTERN, rid):
            rows = self.rows(
                'SELECT body FROM revisions WHERE resource_id=? AND '
                + self.hex_reference_sql
                + ' = ? LIMIT 2',
                (resource_id, rid),
            )
            require(len(rows) <= 1, 'ambiguous_revision_id')
            row = rows[0] if rows else None
        require(row is not None, 'revision_not_found')
        return decode(Revision, loads(row[0]))

    async def history(self, id, cursor=None, limit=50):
        require(1 <= limit <= 500, 'invalid_limit')
        rows = self.rows(
            'SELECT body FROM revisions WHERE resource_id=? AND id>? ORDER BY id LIMIT ?',
            (id, cursor or '', limit + 1),
        )
        values = tuple(decode(Revision, loads(r[0])) for r in rows[:limit])
        return Page(items=values, next_cursor=values[-1].id if len(rows) > limit else None)

    async def insert(self, resource):
        self.check(True)
        if resource.parent is not None:
            await self.resource(resource.parent)
        data = wire(resource)
        self.execute(
            'INSERT INTO resources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                resource.id,
                resource.type,
                resource.name,
                resource.parent,
                resource.owner,
                resource.group,
                resource.mode,
                resource.generation,
                resource.revision,
                resource.state,
                data['created_at'],
                data['modified_at'],
                canonical(resource).decode(),
            ),
            write=True,
        )
        for tag in resource.tags:
            self.execute('INSERT INTO resource_tags VALUES (?,?)', (resource.id, tag), write=True)

    async def replace(self, resource, expected_generation):
        self.check(True)
        old = await self.resource(resource.id)
        require(
            old.generation == expected_generation,
            'generation_conflict',
            details={'generation': old.generation, 'revision': old.revision},
        )
        require(resource.generation == expected_generation + 1, 'invalid_generation')
        require(
            resource.type == old.type
            and resource.created_at == old.created_at
            and resource.created_by == old.created_by,
            'immutable_creation_fact',
        )
        if resource.parent is not None:
            require(resource.parent != resource.id, 'parent_cycle')
            parent = await self.resource(resource.parent)
            require(
                all(a.id != resource.id for a in await self.ancestors(parent.id)), 'parent_cycle'
            )
        if any(
            getattr(resource, k) != getattr(old, k)
            for k in ('parent', 'owner', 'group', 'mode', 'state')
        ):
            self.set_setting('authorization_epoch', self.setting('authorization_epoch', 0) + 1)
        if old.parent is not None and (old.parent, old.name) != (resource.parent, resource.name):
            self.execute(
                'INSERT INTO resource_path_aliases (parent_id,name,resource_id) VALUES (?,?,?) '
                'ON CONFLICT(parent_id,name) DO UPDATE SET resource_id=excluded.resource_id',
                (old.parent, old.name, old.id),
                write=True,
            )
        data = wire(resource)
        changed = self.execute(
            """UPDATE resources SET name=?,parent=?,owner=?,grp=?,mode=?,
            generation=?,revision=?,state=?,modified_at=?,body=? WHERE id=? AND generation=?""",
            (
                resource.name,
                resource.parent,
                resource.owner,
                resource.group,
                resource.mode,
                resource.generation,
                resource.revision,
                resource.state,
                data['modified_at'],
                canonical(resource).decode(),
                resource.id,
                expected_generation,
            ),
            write=True,
        ).rowcount
        require(changed == 1, 'generation_conflict')
        if resource.tags != old.tags:
            self.execute(
                'DELETE FROM resource_tags WHERE resource_id=?', (resource.id,), write=True
            )
            for tag in resource.tags:
                self.execute(
                    'INSERT INTO resource_tags VALUES (?,?)', (resource.id, tag), write=True
                )

    async def append_revision(self, revision):
        self.check(True)
        for parent in revision.parents:
            await self.revision(ResourceRef(id=revision.resource_id, revision=parent))
        self.execute(
            'INSERT INTO revisions VALUES (?,?,?,?)',
            (
                revision.id,
                revision.resource_id,
                wire(revision.created_at),
                canonical(revision).decode(),
            ),
            write=True,
        )
        for relation in revision.relations:
            self.execute(
                'INSERT INTO relations VALUES (?,?,?,?,?,?)',
                (
                    revision.id,
                    revision.resource_id,
                    relation.type,
                    relation.target.id,
                    relation.target.revision,
                    canonical(relation).decode(),
                ),
                write=True,
            )

    async def subject(self, id):
        row = self.one("SELECT body FROM identities WHERE id=? AND kind='subject'", (id,))
        require(row is not None, 'subject_not_found')
        return decode(Subject, loads(row[0]))

    async def organization(self, id):
        row = self.one("SELECT body FROM identities WHERE id=? AND kind='organization'", (id,))
        require(row is not None, 'group_not_found')
        return decode(Organization, loads(row[0]))

    async def credential(self, id):
        row = self.one('SELECT body FROM credentials WHERE id=?', (id,))
        require(row is not None, 'credential_not_found')
        return decode(Credential, loads(row[0]))

    async def certificate(self, id):
        row = self.one('SELECT body FROM certificates WHERE id=?', (id,))
        require(row is not None, 'certificate_not_found')
        return decode(Certificate, loads(row[0]))

    async def certificate_revoked(self, id):
        row = self.one('SELECT revoked FROM certificates WHERE id=?', (id,))
        require(row is not None, 'certificate_not_found')
        return bool(row[0])

    async def memberships(self, subject):
        if subject is None:
            return ()
        rows = self.rows(
            """SELECT m.org,m.body FROM memberships m
            JOIN resources r ON r.id=m.org
            WHERE m.subject=? AND r.type='organization' AND r.state='active'
            ORDER BY m.org""",
            (subject,),
        )
        memberships = ((org, decode(Membership, loads(body))) for org, body in rows)
        # A stale membership row must not revive an archived/missing organization
        # or borrow an organization/subject identifier from a mismatched body.
        active = tuple(
            member
            for org, member in memberships
            if member.status == 'active'
            and member.organization_id == org
            and member.subject_id == subject
            and org != 'g_public'
        )
        row = self.one("SELECT body FROM identities WHERE id=? AND kind='subject'", (subject,))
        if row is None:
            return active
        identity = decode(Subject, loads(row[0]))
        if identity.kind in {'registered', 'custodial', 'system'} and not identity.local_only:
            # /&public is a virtual group for formal subjects. Historical rows
            # may remain in storage, but never create a duplicate authorization.
            return (
                Membership(
                    organization_id='g_public', subject_id=subject, role='member', version=0
                ),
                *active,
            )
        return active

    async def request_result(self, subject, id, digest):
        row = self.one(
            'SELECT digest,body FROM results WHERE subject=? AND request_id=?', (subject, id)
        )
        if row is None:
            return None
        require(row[0] == digest, 'idempotency_conflict')
        return decode(OperationResult, loads(row[1]))

    async def save_result(self, subject, digest, result):
        from msg.storage.capacity import require_result_capacity

        require_result_capacity(self)
        self.execute(
            'INSERT INTO results VALUES (?,?,?,?)',
            (subject, result.request_id, digest, canonical(result).decode()),
            write=True,
        )

    async def update_identity(self, record, expected_generation):
        self.check(True)
        if isinstance(record, Membership):
            self.set_setting('authorization_epoch', self.setting('authorization_epoch', 0) + 1)
            table, where, args = (
                'memberships',
                'org=? AND subject=?',
                (record.organization_id, record.subject_id),
            )
            generation = record.version
        elif isinstance(record, EmailSettings):
            table, where, args = 'emails', 'subject=?', (record.subject_id,)
            generation = expected_generation + 1
        else:
            table, where, args = 'identities', 'id=?', (record.resource_id,)
            generation = (
                record.auth_version if isinstance(record, Subject) else record.membership_version
            )
        row = self.one(f'SELECT generation FROM {table} WHERE {where}', args)
        require(
            (row is None and expected_generation == -1)
            or (row is not None and row[0] == expected_generation),
            'generation_conflict',
        )
        payload = canonical(record).decode()
        if row is None:
            if table == 'identities':
                self.execute(
                    'INSERT INTO identities VALUES (?,?,?,?)',
                    (
                        *args,
                        'subject' if isinstance(record, Subject) else 'organization',
                        generation,
                        payload,
                    ),
                    write=True,
                )
            elif table == 'memberships':
                self.execute(
                    'INSERT INTO memberships VALUES (?,?,?,?)',
                    (*args, generation, payload),
                    write=True,
                )
            else:
                self.execute(
                    'INSERT INTO emails VALUES (?,?,?)', (*args, generation, payload), write=True
                )
        else:
            self.execute(
                f'UPDATE {table} SET generation=?,body=? WHERE {where}',
                (generation, payload, *args),
                write=True,
            )

    async def save_credential(self, credential, expected_auth_version):
        subject = await self.subject(credential.subject_id)
        require(subject.auth_version == expected_auth_version, 'auth_version_conflict')
        old = self.one('SELECT body FROM credentials WHERE id=?', (credential.id,))
        if old is not None:
            previous = decode(Credential, loads(old[0]))
            require(
                previous.subject_id == credential.subject_id
                and previous.kind == credential.kind
                and previous.verifier == credential.verifier
                and previous.source_credential_id == credential.source_credential_id,
                'credential_identity_immutable',
            )
        self.execute(
            'INSERT INTO credentials VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
            (credential.id, credential.subject_id, canonical(credential).decode()),
            write=True,
        )

    async def csr(self, id):
        row = self.one('SELECT body FROM csrs WHERE id=?', (id,))
        require(row is not None, 'csr_not_found')
        return decode(CertificateRequest, loads(row[0]))

    async def csr_state(self, id):
        row = self.one('SELECT state_body FROM csrs WHERE id=?', (id,))
        require(row is not None, 'csr_not_found')
        return decode(CertificateRequestState, loads(row[0]))

    async def save_csr(self, request):
        state = CertificateRequestState(
            request_id=request.resource_id, status='pending', generation=0
        )
        self.execute(
            'INSERT INTO csrs VALUES (?,?,?,?,?)',
            (
                request.resource_id,
                0,
                'pending',
                canonical(request).decode(),
                canonical(state).decode(),
            ),
            write=True,
        )

    async def transition_csr(self, state, expected_generation):
        require(state.generation == expected_generation + 1, 'invalid_generation')
        old = await self.csr_state(state.request_id)
        require(old.status == 'pending', 'csr_not_pending')
        require(old.generation == expected_generation, 'generation_conflict')
        self.execute(
            'UPDATE csrs SET generation=?,state=?,state_body=? WHERE id=? AND generation=?',
            (
                state.generation,
                state.status,
                canonical(state).decode(),
                state.request_id,
                expected_generation,
            ),
            write=True,
        )

    async def register_certificate(self, certificate, csr_id, expected_generation):
        self.execute(
            'INSERT INTO certificates VALUES (?,?,?,?,?)',
            (
                certificate.resource_id,
                certificate.subject_id,
                certificate.parent_certificate_id,
                0,
                canonical(certificate).decode(),
            ),
            write=True,
        )
        if csr_id is not None:
            self.set_setting('certificate_request:' + certificate.resource_id, csr_id)
            await self.transition_csr(
                CertificateRequestState(
                    request_id=csr_id,
                    status='issued',
                    generation=expected_generation + 1,
                    certificate_id=certificate.resource_id,
                ),
                expected_generation,
            )

    async def revoke_certificate(self, id, event):
        await self.certificate(id)
        self.execute('UPDATE certificates SET revoked=1 WHERE id=?', (id,), write=True)
        self.set_setting('authorization_epoch', self.setting('authorization_epoch', 0) + 1)
        await self.append_audit(event)

    async def transfer(self, id):
        row = self.one('SELECT body FROM transfers WHERE id=?', (id,))
        require(row is not None, 'transfer_not_found')
        return decode(TransferSession, loads(row[0]))

    async def save_transfer(self, session, expected_generation):
        if expected_generation is None:
            self.execute(
                'INSERT INTO transfers (id,subject,generation,body) VALUES (?,?,?,?)',
                (session.id, session.subject_id, session.generation, canonical(session).decode()),
                write=True,
            )
        else:
            require(session.generation == expected_generation + 1, 'invalid_generation')
            count = self.execute(
                'UPDATE transfers SET generation=?,body=? WHERE id=? AND generation=?',
                (session.generation, canonical(session).decode(), session.id, expected_generation),
                write=True,
            ).rowcount
            require(count == 1, 'generation_conflict')

    async def put_chunk(self, chunk):
        self.check(True)
        start, end = chunk.offset, chunk.offset + chunk.content.size
        rows = self.rows(
            'SELECT offset,length,body FROM chunks WHERE transfer_id=? AND offset<? AND offset+length>?',
            (chunk.transfer_id, end, start),
        )
        if rows:
            require(
                len(rows) == 1
                and rows[0][0] == start
                and rows[0][1] == chunk.content.size
                and decode(TransferChunk, loads(rows[0][2])).content == chunk.content,
                'chunk_conflict',
            )
            return
        self.execute(
            'INSERT INTO chunks VALUES (?,?,?,?)',
            (chunk.transfer_id, start, chunk.content.size, canonical(chunk).decode()),
            write=True,
        )

    async def missing_ranges(self, id, cursor=None, limit=50):
        session = await self.transfer(id)
        require(session.expected_size is not None, 'size_required')
        require(1 <= limit <= 500, 'invalid_limit')
        start = int(cursor or 0)
        require(0 <= start <= session.expected_size, 'invalid_cursor')
        gaps = []
        for row in self.execute(
            'SELECT offset,length FROM chunks WHERE transfer_id=? AND offset+length>? ORDER BY offset',
            (id, start),
        ):
            if row[0] > start:
                gaps.append((start, row[0]))
                if len(gaps) > limit:
                    return Page(items=tuple(gaps[:limit]), next_cursor=str(gaps[limit][0]))
            start = max(start, row[0] + row[1])
        if start < session.expected_size:
            gaps.append((start, session.expected_size))
        return Page(
            items=tuple(gaps[:limit]),
            next_cursor=str(gaps[limit][0]) if len(gaps) > limit else None,
        )

    async def append_event(self, event):
        self.execute(
            'INSERT INTO events (id,body) VALUES (?,?)',
            (event.id, canonical(event).decode()),
            write=True,
        )

    async def append_audit(self, event):
        previous = self.one('SELECT digest FROM audit ORDER BY seq DESC LIMIT 1')
        prev = previous[0] if previous else None
        data = wire(replace(event, previous_digest=prev, entry_digest=''))
        data.pop('entry_digest')
        entry = replace(event, previous_digest=prev, entry_digest=digest(data))
        self.execute(
            'INSERT INTO audit (digest,previous,body) VALUES (?,?,?)',
            (entry.entry_digest, prev, canonical(entry).decode()),
            write=True,
        )

    async def enqueue(self, job):
        self.execute(
            'INSERT INTO jobs VALUES (?,?,?,?,?,?)',
            (
                job.id,
                job.dedupe_key,
                job.kind,
                job.state,
                wire(job.next_attempt_at),
                canonical(job).decode(),
            ),
            write=True,
        )

    async def job(self, id):
        row = self.one('SELECT body FROM jobs WHERE id=?', (id,))
        require(row is not None, 'job_not_found')
        return decode(EffectJob, loads(row[0]))

    async def save_job(self, job):
        self.execute(
            'UPDATE jobs SET state=?,next_at=?,body=? WHERE id=?',
            (job.state, wire(job.next_attempt_at), canonical(job).decode(), job.id),
            write=True,
        )

    def setting(self, key: str, default: SettingValue = None) -> SettingValue:
        row = self.one('SELECT value FROM settings WHERE key=?', (key,))
        return loads(row[0]) if row else default

    def set_setting(self, key: str, value: SettingValue) -> None:
        self.execute(
            'INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, canonical(value).decode()),
            write=True,
        )
