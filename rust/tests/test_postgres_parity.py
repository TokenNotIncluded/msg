"""Native PostgreSQL writes against the real Python PostgreSQL implementation.

Only new, randomly named databases on an explicitly selected local test server
are used. SQL inputs in the native helper are compiled constants, not JSON data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from collections import Counter
from dataclasses import replace
from pathlib import Path

import psycopg
from postgres_fixture import database
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from test_storage_parity import (
    NOW,
    TABLES as METADATA_TABLES,
    D,
    action,
    audit,
    insert,
    metadata_cases,
    python_steps as base_python_steps,
    resource,
    revision,
)

from msg.core.codec import canonical, decode, wire
from msg.core.errors import Failure
from msg.core.identifiers import hex_id
from msg.core.models import (
    BlobRef,
    Certificate,
    CertificateRequest,
    CertificateRequestState,
    Credential,
    EffectJob,
    EmailSettings,
    Membership,
    Organization,
    Principal,
    ResourceRef,
    Signature,
    Subject,
    TransferChunk,
    TransferSession,
)
from msg.storage.postgres import PostgresMetadataStore

TABLES = (
    *METADATA_TABLES,
    'identities',
    'memberships',
    'emails',
    'credentials',
    'csrs',
    'certificates',
    'transfers',
    'chunks',
    'jobs',
)
REPORT = []


def check(name, okay, detail=None, group='postgres_parity'):
    REPORT.append({
        'name': name,
        'group': group,
        'ok': bool(okay),
        **({'detail': detail} if not okay else {}),
    })


def require_disposable(dsn):
    if not conninfo_to_dict(dsn)['dbname'].startswith('msg_rust_test_'):
        raise RuntimeError('Refusing to modify any existing application database')


def reset(dsn, baseline):
    require_disposable(dsn)
    with psycopg.connect(dsn) as c:
        c.execute(
            sql.SQL('TRUNCATE {} RESTART IDENTITY CASCADE').format(
                sql.SQL(',').join(map(sql.Identifier, TABLES))
            )
        )
        for table, (columns, rows) in baseline.items():
            query = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
                sql.Identifier(table),
                sql.SQL(',').join(map(sql.Identifier, columns)),
                sql.SQL(',').join(sql.Placeholder() for _ in columns),
            )
            for row in rows:
                c.execute(query, row)


def snapshot(dsn):
    with psycopg.connect(dsn) as c:
        result = {}
        for table in TABLES:
            rows = c.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table)))
            result[table] = ([x.name for x in rows.description], sorted(rows.fetchall(), key=repr))
        return result


async def python_steps(tx, steps, store, effects):
    out = []
    classes = {
        'subject': Subject,
        'organization': Organization,
        'membership': Membership,
        'email': EmailSettings,
    }
    for step in steps:
        kind = step['action']
        if kind == 'identity':
            await tx.update_identity(
                decode(classes[step['record_kind']], step['record']), step['expected']
            )
        elif kind == 'subject':
            out.append(wire(await tx.subject(step['id'])))
        elif kind == 'credential':
            await tx.save_credential(decode(Credential, step['record']), step['expected'])
        elif kind == 'get_credential':
            out.append(wire(await tx.credential(step['id'])))
        elif kind == 'csr':
            await tx.save_csr(decode(CertificateRequest, step['record']))
        elif kind == 'csr_state':
            out.append(wire(await tx.csr_state(step['id'])))
        elif kind == 'transition_csr':
            await tx.transition_csr(
                decode(CertificateRequestState, step['record']), step['expected']
            )
        elif kind == 'certificate':
            await tx.register_certificate(
                decode(Certificate, step['record']), step['csr'], step.get('expected', 0)
            )
        elif kind == 'revoke_certificate':
            await tx.revoke_certificate(step['id'], decode(type(audit()), step['event']))
        elif kind == 'get_certificate':
            out.append(wire(await tx.certificate(step['id'])))
        elif kind == 'transfer':
            await tx.save_transfer(decode(TransferSession, step['record']), step['expected'])
        elif kind == 'get_transfer':
            out.append(wire(await tx.transfer(step['id'])))
        elif kind == 'chunk':
            await tx.put_chunk(decode(TransferChunk, step['record']))
        elif kind == 'missing':
            out.append(wire(await tx.missing_ranges(step['id'], step['cursor'], step['limit'])))
        elif kind == 'enqueue':
            await tx.enqueue(decode(EffectJob, step['record']))
        elif kind == 'save_job':
            await tx.save_job(decode(EffectJob, step['record']))
        elif kind == 'job':
            out.append(wire(await tx.job(step['id'])))
        elif kind == 'path':
            out.append(await tx.path(step['id']))
        elif kind == 'resolve':
            out.append(
                await (
                    tx.resolve_migrated(step['path'])
                    if step.get('migrated')
                    else tx.resolve(step['path'])
                )
            )
        elif kind == 'children':
            out.append(wire(await tx.children(step['id'], step['cursor'], step['limit'])))
        elif kind == 'history':
            out.append(wire(await tx.history(step['id'], step['cursor'], step['limit'])))
        elif kind == 'get_revision':
            out.append(wire(await tx.revision(decode(ResourceRef, step['reference']))))
        elif kind == 'savepoint':
            try:
                async with store.transaction(write=True) as inner:
                    data = await python_steps(inner, step['steps'], store, effects)
                out.append(data)
            except Failure as exc:
                if not step.get('recover'):
                    raise
                out.append({'error': exc.code})
        else:
            out.extend(await base_python_steps(tx, [step], store, effects))
    return out


async def run_python(store, steps):
    effects = []
    try:
        async with store.transaction(write=True) as tx:
            out = await python_steps(tx, steps, store, effects)
        return {'status': 'ok', 'values': out}, effects
    except Failure as exc:
        return {'status': 'error', 'code': exc.code, 'cleanup': []}, effects


class Native:
    def __init__(self, binary):
        self.binary = str(binary)

    def start(self, dsn, steps, **kwargs):
        env = {**os.environ, 'MSG_PARITY_POSTGRES_DSN': dsn}
        p = subprocess.Popen(
            [self.binary],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        p.stdin.write(json.dumps({'steps': steps, **kwargs}, ensure_ascii=False) + '\n')
        p.stdin.flush()
        return p

    def run(self, dsn, steps, **kwargs):
        p = self.start(dsn, steps, **kwargs)
        stdout, stderr = p.communicate(timeout=30)
        if p.returncode:
            raise AssertionError('Native helper failed: ' + stderr)
        lines = [json.loads(s) for s in stdout.splitlines()]
        result = lines[-1]
        if result.get('uncertain') is False:
            result.pop('uncertain')
        return result, [item['effect'] for item in lines if 'effect' in item]


def extended_cases():
    subject = Subject(
        resource_id='u_alice', kind='registered', primary_group='g_public', auth_version=0
    )
    organization = Organization(resource_id='g_team', membership_version=0)
    membership = Membership(
        organization_id='g_team', subject_id='u_alice', role='member', version=0
    )
    email = EmailSettings(
        subject_id='u_alice', address='public-test@example.invalid', verified_at=None
    )
    credential = Credential(
        id='key_a',
        subject_id='u_alice',
        kind='signing_key',
        verifier=bytes([3]) * 32,
        ceiling=(),
        not_before=NOW,
        expires_at=None,
        revoked_at=None,
    )
    sig = Signature(key_id='key_a', algorithm='Ed25519', value=bytes([1]) * 64)
    csr = CertificateRequest(
        resource_id='csr_a',
        applicant='u_alice',
        subject_id='u_alice',
        requested_issuer='u_root',
        public_key=credential.verifier,
        kind='capability',
        grants=(),
        issuance=None,
        requested_ttl_seconds=3600,
        target_service='https://fixture.invalid',
        delegation_depth=0,
        authority_sources=(),
        request_digest=D,
        possession_proof=sig,
    )
    cert = Certificate(
        resource_id='cert_a',
        serial='fixture',
        subject_id='u_alice',
        key_id='key_a',
        issuer_id='u_root',
        parent_certificate_id=None,
        authority_sources=(),
        kind='capability',
        grants=(),
        not_before=NOW,
        expires_at=NOW.replace(year=2027),
        target_service='https://fixture.invalid',
        delegation_depth=0,
        issuance=None,
        signature=sig,
    )
    transfer = TransferSession(
        id='transfer',
        subject_id='u_alice',
        direction='upload',
        state='open',
        target=None,
        expected_size=100,
        expected_digest=D,
        expires_at=NOW.replace(year=2027),
        generation=0,
    )
    chunk = TransferChunk(
        transfer_id='transfer',
        offset=10,
        content=BlobRef(digest=D, size=20, media_type='text/plain'),
    )
    principal = Principal(
        actor='u_alice',
        subject='u_alice',
        credential_id='key_a',
        method='signature',
        ceiling=(),
        certificates=(),
    )
    job = EffectJob(
        id='job',
        event_id='event',
        kind='tool',
        dedupe_key='dedupe',
        principal=principal,
        operation='tool.run@1',
        arguments={},
        state='pending',
        attempts=0,
        next_attempt_at=NOW,
        lease_until=None,
    )

    def identity(record, kind='subject', expected=-1):
        return action('identity', record_kind=kind, record=record, expected=expected)

    cases = []
    for kind, r, field in [
        ('subject', subject, 'auth_version'),
        ('organization', organization, 'membership_version'),
        ('membership', membership, 'version'),
    ]:
        cases.extend([
            (kind + '-insert', [identity(r, kind)]),
            (kind + '-update', [identity(r, kind), identity(replace(r, **{field: 1}), kind, 0)]),
            (kind + '-conflict', [identity(r, kind), identity(replace(r, **{field: 2}), kind, 1)]),
        ])
    cases.extend([
        ('subject-read', [identity(subject), action('subject', id='u_alice')]),
        (
            'email-update',
            [
                identity(email, 'email'),
                identity(
                    replace(email, verified_at=NOW, enabled_events=frozenset({'reply', 'message'})),
                    'email',
                    0,
                ),
            ],
        ),
        (
            'credential-read',
            [
                identity(subject),
                action('credential', record=credential, expected=0),
                action('get_credential', id=credential.id),
            ],
        ),
        (
            'credential-revoke',
            [
                identity(subject),
                action('credential', record=credential, expected=0),
                action('credential', record=replace(credential, revoked_at=NOW), expected=0),
            ],
        ),
        (
            'credential-version',
            [identity(subject), action('credential', record=credential, expected=1)],
        ),
        (
            'credential-immutable',
            [
                identity(subject),
                action('credential', record=credential, expected=0),
                action(
                    'credential', record=replace(credential, verifier=bytes([9]) * 32), expected=0
                ),
            ],
        ),
        ('csr-pending', [action('csr', record=csr), action('csr_state', id=csr.resource_id)]),
        (
            'csr-rejected',
            [
                action('csr', record=csr),
                action(
                    'transition_csr',
                    record=CertificateRequestState(
                        request_id=csr.resource_id, status='rejected', generation=1
                    ),
                    expected=0,
                ),
            ],
        ),
        (
            'certificate-with-csr',
            [
                action('csr', record=csr),
                action('certificate', record=cert, csr=csr.resource_id, expected=0),
                action('csr_state', id=csr.resource_id),
                action('get_certificate', id=cert.resource_id),
            ],
        ),
        (
            'certificate-without-csr',
            [
                action('certificate', record=cert, csr=None),
                action('get_certificate', id=cert.resource_id),
            ],
        ),
        (
            'certificate-csr-failed',
            [
                action('csr', record=csr),
                action('certificate', record=cert, csr=csr.resource_id, expected=1),
            ],
        ),
        (
            'certificate-revoked',
            [
                action('certificate', record=cert, csr=None),
                action('revoke_certificate', id=cert.resource_id, event=audit()),
            ],
        ),
        (
            'transfer-roundtrip',
            [
                action('transfer', record=transfer, expected=None),
                action('get_transfer', id=transfer.id),
            ],
        ),
        (
            'transfer-transition',
            [
                action('transfer', record=transfer, expected=None),
                action(
                    'transfer',
                    record=replace(
                        transfer,
                        state='sealed',
                        generation=1,
                        output=ResourceRef(id='r_doc', revision='v1'),
                    ),
                    expected=0,
                ),
            ],
        ),
        (
            'transfer-conflict',
            [
                action('transfer', record=transfer, expected=None),
                action('transfer', record=replace(transfer, generation=2), expected=1),
            ],
        ),
        (
            'chunk-repeat',
            [
                action('transfer', record=transfer, expected=None),
                action('chunk', record=chunk),
                action('chunk', record=chunk),
            ],
        ),
        (
            'chunk-conflict',
            [
                action('transfer', record=transfer, expected=None),
                action('chunk', record=chunk),
                action('chunk', record=replace(chunk, offset=20)),
            ],
        ),
        (
            'chunk-content-conflict',
            [
                action('transfer', record=transfer, expected=None),
                action('chunk', record=chunk),
                action(
                    'chunk',
                    record=replace(
                        chunk, content=replace(chunk.content, digest='sha256:' + 'a' * 64)
                    ),
                ),
            ],
        ),
        ('job-roundtrip', [action('enqueue', record=job), action('job', id=job.id)]),
        (
            'job-update',
            [
                action('enqueue', record=job),
                action(
                    'save_job',
                    record=replace(
                        job, state='running', attempts=1, lease_until=NOW.replace(hour=1)
                    ),
                ),
                action('job', id=job.id),
            ],
        ),
        (
            'job-dedupe',
            [action('enqueue', record=job), action('enqueue', record=replace(job, id='other'))],
        ),
    ])
    for cursor in (None, '0', '30', '100'):
        for limit in (1, 2, 50):
            cases.append((
                f'missing-{cursor}-{limit}',
                [
                    action('transfer', record=transfer, expected=None),
                    action('chunk', record=chunk),
                    action('chunk', record=replace(chunk, offset=50)),
                    action('missing', id=transfer.id, cursor=cursor, limit=limit),
                ],
            ))
    for path, migrated in [
        ('/', False),
        ('/document', False),
        ('/_id/r_doc', False),
        ('/_id/' + hex_id('r_doc'), False),
        ('/*' + hex_id('r_doc'), False),
        ('/document', True),
    ]:
        cases.append((
            'resolve-' + path,
            [insert(), action('resolve', path=path, migrated=migrated)],
        ))
    cases.extend([
        ('path', [insert(), action('path', id='r_doc')]),
        ('children', [insert(), action('children', id='r_root', cursor=None, limit=1)]),
        (
            'history',
            [
                insert(),
                action('revision', record=revision()),
                action('history', id='r_doc', cursor=None, limit=1),
            ],
        ),
        (
            'revision-read',
            [
                insert(),
                action('revision', record=revision()),
                action('get_revision', reference=ResourceRef(id='r_doc', revision='v1')),
            ],
        ),
        (
            'revision-hex',
            [
                insert(),
                action('revision', record=revision()),
                action(
                    'get_revision', reference=ResourceRef(id=hex_id('r_doc'), revision=hex_id('v1'))
                ),
            ],
        ),
        (
            'read-after-rename',
            [
                insert(),
                action('replace', record=replace(resource(), name='new', generation=1), expected=0),
                action('resolve', path='/document', migrated=True),
                action('resolve', path='/new', migrated=False),
            ],
        ),
    ])
    return cases


async def run(args):
    native = Native(args.binary.resolve())
    with database() as py_dsn, database() as rs_dsn:
        py_store = PostgresMetadataStore(py_dsn)
        rs_store = PostgresMetadataStore(rs_dsn)
        for store in [py_store, rs_store]:
            async with store.transaction(write=True) as tx:
                await tx.insert(resource('r_root', parent=None, name=''))
        baseline = snapshot(py_dsn)
        try:
            for name, steps in metadata_cases() + extended_cases():
                reset(py_dsn, baseline)
                reset(rs_dsn, baseline)
                expected, py_effects = await run_python(py_store, steps)
                actual, rs_effects = native.run(rs_dsn, steps)
                py_db, rs_db = snapshot(py_dsn), snapshot(rs_dsn)
                different = [t for t in TABLES if py_db[t] != rs_db[t]]
                check(
                    name,
                    canonical(actual) == canonical(expected)
                    and not different
                    and py_effects == rs_effects,
                    {
                        'python': expected,
                        'rust': actual,
                        'tables': different,
                        'effects': [py_effects, rs_effects],
                    },
                )
            reset(rs_dsn, baseline)
            actual, _ = native.run(
                rs_dsn, [action('set', key='read-only', value=True)], read_only=True
            )
            check(
                'read-only-write-rejected',
                actual.get('code') == 'read_only_transaction' and snapshot(rs_dsn) == baseline,
                actual,
                group='postgres_safety',
            )
            actual, _ = native.run(
                rs_dsn,
                [
                    action('set', key='before', value=True),
                    action('ignore', steps=[insert(resource(parent='absent'))]),
                ],
            )
            check(
                'ignored-write-error-cannot-commit',
                actual.get('status') == 'error' and snapshot(rs_dsn) == baseline,
                actual,
                group='postgres_safety',
            )
        finally:
            await py_store.close()
            await rs_store.close()
    report = {
        'backend': 'postgresql',
        'checks': len(REPORT),
        'failures': sum(not x['ok'] for x in REPORT),
        'groups': dict(Counter(x['group'] for x in REPORT)),
        'cases': REPORT,
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'cases'}))
    return report['failures']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    raise SystemExit(bool(asyncio.run(run(parser.parse_args()))))


if __name__ == '__main__':
    main()
