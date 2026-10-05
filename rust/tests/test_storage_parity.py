"""Native writes against real Python storage; all data and keys are disposable.

This tests storage primitives, not an authenticated operation executor. The
local `once` helper deliberately models only the atomic ledger boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import selectors
import sqlite3
import subprocess
import tempfile
from collections import Counter
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from msg.core.codec import canonical, decode, digest, wire
from msg.core.errors import Failure
from msg.core.models import (
    AuditEvent,
    BlobRef,
    Event,
    OperationError,
    OperationResult,
    Relation,
    Resource,
    ResourceRef,
    Revision,
)
from msg.security.crypto import Ed25519Signer
from msg.storage.sqlite import SqliteMetadataStore

NOW = datetime(2026, 10, 5, tzinfo=UTC)
D = digest({'task': 'writer-test'})
TABLES = (
    'resources',
    'resource_tags',
    'resource_path_aliases',
    'revisions',
    'relations',
    'settings',
    'results',
    'events',
    'audit',
)
REPORT = []


def resource(id='r_doc', *, parent='r_root', name='document', **kwargs):
    return Resource(
        id=id,
        type='file' if parent else 'space',
        type_version=1,
        name=name,
        parent=parent,
        owner='u_alice',
        group='g_public',
        mode=0o0644,
        generation=0,
        revision=None,
        state='active',
        created_at=NOW,
        created_by='u_alice',
        modified_at=NOW,
        modified_by='u_alice',
        **kwargs,
    )


def result(subject='u_alice', request_id='req', **kwargs):
    return OperationResult(
        request_id=request_id,
        operation='fixture.write',
        status='ok',
        actor=subject,
        subject=subject,
        committed_at=NOW,
        **kwargs,
    )


def event(id='event-1', **kwargs):
    return Event(
        id=id,
        type='fixture.changed',
        time=NOW,
        request_id='req',
        actor='u_alice',
        subject='u_alice',
        resources=(ResourceRef(id='r_doc'),),
        data=kwargs,
    )


def audit(id='event-1'):
    return AuditEvent(
        event=event(id, n=10**60),
        authority=(),
        before_digest=None,
        after_digest=D,
        previous_digest='ignored by appender',
        entry_digest='ignored by appender',
        result='ok',
    )


def revision(id='v1', **kwargs):
    return Revision(
        format_version=1,
        id=id,
        resource_id='r_doc',
        parents=(),
        content=BlobRef(digest=D, size=123, media_type='text/plain'),
        relations=(),
        actor='u_alice',
        subject='u_alice',
        author='u_alice',
        created_at=NOW,
        manifest_digest=D,
        **kwargs,
    )


def action(kind, **kwargs):
    return {'action': kind, **wire(kwargs)}


def insert(record=None):
    return action('insert', record=record or resource())


def set_value(key, value=1):
    return action('set', key=key, value=value)


def ledger(record=None, **kwargs):
    record = record or result()
    return action('result', record=record, subject=record.subject, digest=D, **kwargs)


def once(body, record=None):
    record = record or result()
    return action('once', subject=record.subject, digest=D, record=record, steps=body)


def check(name, ok, detail=None, group='parity'):
    REPORT.append({
        'name': name,
        'group': group,
        'ok': bool(ok),
        **({'detail': detail} if not ok else {}),
    })


def dump(path):
    with closing(sqlite3.connect(path)) as db:
        return {
            table: sorted(db.execute(f'SELECT * FROM {table}').fetchall(), key=repr)
            for table in TABLES
        }


def schema(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(
            'SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY name'
        ).fetchall()


def backup(source, target):
    with closing(sqlite3.connect(source)) as db, closing(sqlite3.connect(target)) as dst:
        db.backup(dst)


async def fixture(path):
    store = SqliteMetadataStore(path, busy_timeout=0.08)
    async with store.transaction(write=True) as tx:
        await tx.insert(resource('r_root', parent=None, name=''))
    return store


async def python_steps(tx, values, store, effects):
    output = []
    for step in values:
        kind = step['action']
        if kind == 'set':
            tx.set_setting(step['key'], step['value'])
        elif kind == 'read':
            output.append(tx.setting(step['key']))
        elif kind == 'resource':
            output.append(wire(await tx.resource(step['id'])))
        elif kind == 'insert':
            await tx.insert(decode(Resource, step['record']))
        elif kind == 'replace':
            await tx.replace(decode(Resource, step['record']), step['expected'])
        elif kind == 'revision':
            await tx.append_revision(decode(Revision, step['record']))
        elif kind == 'result':
            await tx.save_result(
                step['subject'], step['digest'], decode(OperationResult, step['record'])
            )
        elif kind == 'lookup':
            output.append(
                wire(await tx.request_result(step['subject'], step['id'], step['digest']))
            )
        elif kind == 'event':
            await tx.append_event(decode(Event, step['record']))
        elif kind == 'audit':
            await tx.append_audit(decode(AuditEvent, step['record']))
        elif kind == 'savepoint':
            try:
                async with store.transaction(write=True) as inner:
                    data = await python_steps(inner, step['steps'], store, effects)
                output.append(data)
            except Failure as exc:
                if not step.get('recover'):
                    raise
                output.append({'error': exc.code})
        elif kind == 'effect':

            async def cleanup(name=step['name']):
                effects.append(name)

            tx.on_rollback(cleanup)
        elif kind == 'fail':
            raise Failure('fixture_failure')
        elif kind == 'once':
            record = decode(OperationResult, step['record'])
            stored = await tx.request_result(step['subject'], record.request_id, step['digest'])
            if stored is None:
                await python_steps(tx, step['steps'], store, effects)
                await tx.save_result(step['subject'], step['digest'], record)
                stored = record
            output.append(wire(stored))
        else:
            raise AssertionError(f'Unsupported Python fixture action: {kind}')
    return output


async def run_python(store, steps):
    effects = []
    try:
        async with store.transaction(write=True) as tx:
            output = await python_steps(tx, steps, store, effects)
        return {'status': 'ok', 'values': output}, effects
    except Failure as exc:
        return {'status': 'error', 'code': exc.code, 'cleanup': []}, effects


class Native:
    def __init__(self, binary):
        self.binary = str(binary)

    def run(self, path, steps, **kwargs):
        completed = subprocess.run(
            [self.binary, str(path)],
            input=json.dumps({'steps': steps, **kwargs}) + '\n',
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        lines = [json.loads(line) for line in completed.stdout.splitlines()]
        return lines[-1], [line['effect'] for line in lines if 'effect' in line]

    def start(self, path, steps, **kwargs):
        process = subprocess.Popen(
            [self.binary, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        process.stdin.write(json.dumps({'steps': steps, **kwargs}) + '\n')
        process.stdin.flush()
        return process


def line(process):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(10):
            raise AssertionError('Native process did not reach its checkpoint')
    raw = process.stdout.readline()
    if not raw:
        raise AssertionError('Native exited before checkpoint: ' + process.stderr.read())
    return json.loads(raw)


def finish(process):
    process.stdin.write('continue\n')
    process.stdin.flush()
    stdout, stderr = process.communicate(timeout=15)
    if process.returncode:
        raise AssertionError(f'Native failed: {process.returncode}: {stderr}')
    return json.loads(stdout.splitlines()[-1])


def kill(process):
    process.kill()
    process.communicate(timeout=10)


def metadata_cases():
    doc = resource()
    rev = revision()
    cases = [
        ('insert-read-resource', [insert(), action('resource', id=doc.id)]),
        (
            'canonical-settings',
            [
                set_value('numbers', {'n': 10**80, 'f': -0.0, 'text': '中文😀é'}),
                action('read', key='numbers'),
            ],
        ),
        ('unicode-tags', [insert(resource(tags=('café', '安全', '苹果')))]),
        (
            'rename-alias',
            [
                insert(),
                action('replace', record=replace(doc, name='renamed', generation=1), expected=0),
            ],
        ),
        (
            'tags-update',
            [
                insert(),
                action(
                    'replace',
                    record=replace(doc, tags=('rust', 'storage'), generation=1),
                    expected=0,
                ),
            ],
        ),
        (
            'move-alias',
            [
                insert(resource('r_other', name='other')),
                insert(),
                action('replace', record=replace(doc, parent='r_other', generation=1), expected=0),
            ],
        ),
        (
            'generation-conflict',
            [insert(), action('replace', record=replace(doc, generation=2), expected=1)],
        ),
        (
            'invalid-generation',
            [insert(), action('replace', record=replace(doc, generation=2), expected=0)],
        ),
        (
            'immutable-type',
            [
                insert(),
                action('replace', record=replace(doc, type='topic', generation=1), expected=0),
            ],
        ),
        (
            'immutable-creator',
            [
                insert(),
                action(
                    'replace', record=replace(doc, created_by='u_bob', generation=1), expected=0
                ),
            ],
        ),
        (
            'immutable-time',
            [
                insert(),
                action(
                    'replace',
                    record=replace(doc, created_at=NOW.replace(hour=1), generation=1),
                    expected=0,
                ),
            ],
        ),
        ('missing-parent', [insert(resource(parent='missing'))]),
        ('duplicate-child', [insert(), insert(resource('r_duplicate'))]),
        ('second-root', [insert(resource('second-root', parent=None, name=''))]),
        (
            'parent-cycle',
            [
                insert(),
                insert(resource('r_child', parent=doc.id, name='child')),
                action('replace', record=replace(doc, parent='r_child', generation=1), expected=0),
            ],
        ),
        ('append-revision', [insert(), action('revision', record=rev)]),
        (
            'revision-chain',
            [
                insert(),
                action('revision', record=rev),
                action(
                    'revision',
                    record=replace(
                        rev,
                        id='v2',
                        parents=('v1',),
                        summary='changed',
                        source_kind='user',
                        source_version=2,
                    ),
                ),
            ],
        ),
        (
            'revision-relations',
            [
                insert(),
                action(
                    'revision',
                    record=replace(
                        rev,
                        relations=(
                            Relation(
                                type='reference', target=ResourceRef(id='r_root'), excerpt=(0, 12)
                            ),
                        ),
                    ),
                ),
            ],
        ),
        (
            'revision-parent-missing',
            [insert(), action('revision', record=replace(rev, parents=('missing',)))],
        ),
        (
            'revision-wrong-resource',
            [
                insert(),
                insert(resource('r_other', name='other')),
                action('revision', record=replace(rev, resource_id='r_other')),
                action('revision', record=replace(rev, id='v2', parents=('v1',))),
            ],
        ),
        (
            'revision-duplicate',
            [insert(), action('revision', record=rev), action('revision', record=rev)],
        ),
        ('result-read', [ledger(), action('lookup', subject='u_alice', id='req', digest=D)]),
        (
            'result-anonymous',
            [ledger(result(None)), action('lookup', subject=None, id='req', digest=D)],
        ),
        (
            'result-namespace',
            [
                ledger(),
                ledger(result('u_bob')),
                action('lookup', subject='u_charlie', id='req', digest=D),
            ],
        ),
        ('result-duplicate', [insert(), ledger(), ledger()]),
        (
            'digest-conflict',
            [
                ledger(),
                action('lookup', subject='u_alice', id='req', digest=digest({'different': True})),
            ],
        ),
        ('event', [action('event', record=event())]),
        (
            'duplicate-event',
            [insert(), action('event', record=event()), action('event', record=event())],
        ),
        (
            'audit-chain',
            [action('audit', record=audit()), action('audit', record=audit('event-2'))],
        ),
        (
            'atomic-resource-event-audit-result',
            [
                insert(),
                action('revision', record=rev),
                action('event', record=event()),
                action('audit', record=audit()),
                ledger(),
            ],
        ),
        (
            'atomic-failure-at-result',
            [
                ledger(),
                insert(),
                action('event', record=event()),
                action('audit', record=audit()),
                ledger(),
            ],
        ),
        (
            'rollback-all',
            [
                insert(),
                ledger(),
                action('event', record=event()),
                action('audit', record=audit()),
                action('fail'),
            ],
        ),
        (
            'savepoint-recover',
            [
                set_value('before'),
                action('savepoint', steps=[set_value('inner'), action('fail')], recover=True),
                set_value('after'),
            ],
        ),
        (
            'savepoint-outer-failure',
            [action('savepoint', steps=[insert(), ledger()]), action('fail')],
        ),
        (
            'nested-savepoints',
            [
                action(
                    'savepoint',
                    steps=[
                        set_value('first'),
                        action(
                            'savepoint', steps=[set_value('second'), action('fail')], recover=True
                        ),
                        set_value('third'),
                    ],
                )
            ],
        ),
        (
            'rollback-effects',
            [
                action('effect', name='outer'),
                action('savepoint', steps=[action('effect', name='inner')]),
                action('effect', name='last'),
                action('fail'),
            ],
        ),
        (
            'savepoint-effects',
            [
                action('effect', name='outer'),
                action(
                    'savepoint',
                    steps=[action('effect', name='inner'), action('fail')],
                    recover=True,
                ),
            ],
        ),
        (
            'same-unit-retry',
            [
                once([insert(), action('audit', record=audit())]),
                once([insert(), action('audit', record=audit())]),
            ],
        ),
    ]
    for name, field, value in [
        ('owner', 'owner', 'u_bob'),
        ('group', 'group', 'g_other'),
        ('mode', 'mode', 0o0740),
        ('state', 'state', 'archived'),
    ]:
        cases.append((
            f'authorization-epoch-{name}',
            [
                insert(),
                action('replace', record=replace(doc, generation=1, **{field: value}), expected=0),
                action('read', key='authorization_epoch'),
            ],
        ))
    for status in ('ok', 'accepted', 'error', 'uncertain'):
        record = replace(
            result(data={'large': 10**90, 'text': '安全'}),
            status=status,
            error=OperationError(code='fixture', retryable=False) if status == 'error' else None,
        )
        cases.append((f'result-status-{status}', [ledger(record)]))
    signer = Ed25519Signer.from_bytes(bytes([7]) * 32)
    cases.append((
        'signed-receipt-bytes',
        [ledger(result(receipt=signer.sign(b'fixture-only', purpose='receipt')))],
    ))
    cases.append((
        'historical-error-message',
        [
            ledger(
                replace(
                    result(),
                    status='error',
                    error=OperationError(code='fixture', retryable=True, message='可重试'),
                )
            )
        ],
    ))
    return cases


async def parity_cases(native, directory):
    cases = metadata_cases()
    for index, (name, steps) in enumerate(cases):
        py = directory / f'py-{index}.db'
        rs = directory / f'rs-{index}.db'
        store = await fixture(py)
        backup(py, rs)
        before_schema = schema(rs)
        expected, py_effects = await run_python(store, steps)
        actual, rs_effects = native.run(rs, steps)
        same = (
            canonical(actual) == canonical(expected)
            and dump(py) == dump(rs)
            and py_effects == rs_effects
            and schema(rs) == before_schema
        )
        check(
            name,
            same,
            {
                'python': expected,
                'rust': actual,
                'tables_differ': [t for t in TABLES if dump(py)[t] != dump(rs)[t]],
                'effects': [py_effects, rs_effects],
            },
        )
        await store.close()


async def boundary_cases(native, directory):
    path = directory / 'boundaries.db'
    store = await fixture(path)
    baseline_settings = dump(path)['settings']
    actual, _ = native.run(path, [action('result', subject='u_bob', digest=D, record=result())])
    check(
        'mismatched-result-subject',
        actual.get('code') == 'storage_record_mismatch' and not dump(path)['results'],
        group='native-safety',
    )
    actual, _ = native.run(path, [ledger(result(''))])
    check(
        'empty-subject-not-anonymous-alias',
        actual.get('code') == 'storage_record_mismatch',
        group='native-safety',
    )
    actual, _ = native.run(
        path, [action('result', subject='u_alice', digest='not-a-digest', record=result())]
    )
    check('malformed-result-digest', actual.get('code') == 'invalid_digest', group='native-safety')
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "CREATE TRIGGER fixture_abort BEFORE INSERT ON settings WHEN NEW.key='reject' BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
        db.commit()
    actual, _ = native.run(
        path, [set_value('before'), action('ignore', steps=[set_value('reject')])]
    )
    check(
        'swallowed-write-fails-closed',
        actual.get('code') == 'constraint_conflict' and dump(path)['settings'] == baseline_settings,
        group='native-safety',
    )
    with closing(sqlite3.connect(path)) as db:
        db.execute('DROP TRIGGER fixture_abort')
        db.execute(
            "CREATE TRIGGER fixture_abort BEFORE INSERT ON settings WHEN NEW.key='reject' BEGIN SELECT RAISE(ROLLBACK,'fixture'); END"
        )
        db.commit()
    actual, _ = native.run(
        path,
        [set_value('before'), action('ignore', steps=[set_value('reject')]), set_value('escape')],
    )
    check(
        'implicit-abort-no-autocommit-write',
        actual.get('code') == 'constraint_conflict' and dump(path)['settings'] == baseline_settings,
        group='native-safety',
    )
    with closing(sqlite3.connect(path)) as db:
        db.execute('DROP TRIGGER fixture_abort')
        db.execute(
            "CREATE TRIGGER fixture_deferred AFTER INSERT ON settings WHEN NEW.key='deferred' BEGIN INSERT INTO resources SELECT 'orphan',type,'orphan','missing',owner,grp,mode,generation,revision,state,created_at,modified_at,body FROM resources WHERE id='r_root'; END"
        )
        db.commit()
    actual, effects = native.run(
        path, [action('effect', name='cleanup'), set_value('deferred'), ledger()]
    )
    check(
        'commit-failure-undoes-result-and-data',
        actual.get('code') == 'constraint_conflict'
        and effects == ['cleanup']
        and not dump(path)['results']
        and dump(path)['settings'] == baseline_settings
        and len(dump(path)['resources']) == 1,
        group='native-safety',
    )
    actual, effects = native.run(
        path,
        [
            action('effect', name='first'),
            action('effect', name='bad', fail=True),
            action('effect', name='panic', panic=True),
            action('fail'),
        ],
    )
    check(
        'compensation-errors-preserve-primary',
        actual.get('code') == 'fixture_failure'
        and actual.get('cleanup') == ['rollback_effect_panicked', 'fixture_cleanup']
        and effects == ['panic', 'bad', 'first'],
        group='native-safety',
    )
    await store.close()

    capacity = directory / 'capacity.db'
    cap_store = await fixture(capacity)
    with closing(sqlite3.connect(capacity)) as db:
        db.execute(
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000) INSERT INTO results SELECT 'fixture', CAST(x AS TEXT), 'fixture', '{}' FROM n"
        )
        db.commit()
    expected, _ = await run_python(cap_store, [insert(), ledger()])
    actual, _ = native.run(capacity, [insert(), ledger()])
    check(
        'result-capacity-rolls-back-resource',
        actual == expected
        and actual.get('code') == 'storage_capacity_exceeded'
        and len(dump(capacity)['resources']) == 1,
        group='parity',
    )
    await cap_store.close()


async def process_cases(native, directory):
    for stage in ('before', 'after'):
        path = directory / f'crash-{stage}.db'
        store = await fixture(path)
        body = [insert(), action('event', record=event()), action('audit', record=audit())]
        command = once(body + ([action('hold')] if stage == 'before' else []))
        process = native.start(path, [command], hold_after_commit=stage == 'after')
        try:
            got = line(process)
            checkpoint = 'transaction' if stage == 'before' else 'committed'
            check(f'crash-{stage}-checkpoint', got == {'checkpoint': checkpoint}, group='process')
            visible = dump(path)
            check(
                f'crash-{stage}-visibility',
                bool(visible['results']) == (stage == 'after')
                and len(visible['resources']) == (2 if stage == 'after' else 1),
                group='process',
            )
        finally:
            kill(process)
        actual, _ = native.run(path, [once(body)])
        rows = dump(path)
        check(
            f'crash-{stage}-retry-single-commit',
            actual['status'] == 'ok'
            and len(rows['resources']) == 2
            and len(rows['results']) == len(rows['events']) == len(rows['audit']) == 1,
            group='process',
        )
        await store.close()

    path = directory / 'competing.db'
    store = await fixture(path)
    body = [insert(), action('audit', record=audit())]
    first = native.start(path, [once(body + [action('hold')])])
    second = None
    try:
        check(
            'competing-writer-first-checkpoint',
            line(first) == {'checkpoint': 'transaction'},
            group='process',
        )
        second = native.start(path, [once(body)], timeout_ms=3000)
        one = finish(first)
        stdout, stderr = second.communicate(timeout=10)
        two = json.loads(stdout.splitlines()[-1])
        check(
            'competing-writers-share-one-result',
            second.returncode == 0 and one == two and len(dump(path)['audit']) == 1,
            stderr,
            group='process',
        )
    finally:
        if first.poll() is None:
            kill(first)
        if second is not None and second.poll() is None:
            kill(second)

    async with store.transaction(write=True) as tx:
        tx.set_setting('python-holds', 1)
        actual, _ = native.run(path, [set_value('must-not-write')], timeout_ms=50)
        check(
            'python-fence-blocks-rust', actual.get('code') == 'server_busy', actual, group='process'
        )
    rust = native.start(path, [set_value('rust-holds'), action('hold')])
    try:
        line(rust)
        try:
            async with store.transaction(write=True):
                raise AssertionError('Python crossed native writer fence')
        except Failure as exc:
            check('rust-fence-blocks-python', exc.code == 'server_busy', group='process')
        finish(rust)
    finally:
        if rust.poll() is None:
            kill(rust)

    rust = native.start(
        path, [action('effect', name='cleanup', hold=True), set_value('abort'), action('fail')]
    )
    try:
        check('compensation-started', line(rust) == {'effect': 'cleanup'}, group='process')
        # The next checkpoint is line-buffered; read directly after its known
        # predecessor rather than asking select about already buffered data.
        checkpoint = json.loads(rust.stdout.readline())
        check(
            'compensation-checkpoint', checkpoint == {'checkpoint': 'compensation'}, group='process'
        )
        with closing(sqlite3.connect(path, isolation_level=None, timeout=0)) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('ROLLBACK')
        try:
            async with store.transaction(write=True):
                raise AssertionError('Python crossed compensation fence')
        except Failure as exc:
            check('fence-held-after-sql-rollback', exc.code == 'server_busy', group='process')
        outcome = finish(rust)
        check(
            'compensation-finishes-before-release',
            outcome.get('code') == 'fixture_failure',
            group='process',
        )
    finally:
        if rust.poll() is None:
            kill(rust)
    actual, _ = native.run(path, [set_value('after-compensation')])
    check('writer-usable-after-clean-rollback', actual['status'] == 'ok', group='process')
    await store.close()


async def main_async(args):
    native = Native(args.binary.resolve())
    with tempfile.TemporaryDirectory(prefix='msg-writer-parity-') as root:
        directory = Path(root)
        await parity_cases(native, directory)
        await boundary_cases(native, directory)
        await process_cases(native, directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args))
    except Exception as exc:
        REPORT.append({'name': 'harness', 'group': 'harness', 'ok': False, 'detail': repr(exc)})
    failures = [case for case in REPORT if not case['ok']]
    report = {
        'cases': len(REPORT),
        'groups': dict(Counter(c['group'] for c in REPORT)),
        'failures': len(failures),
        'first_failures': failures[:12],
        'checks': REPORT,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=True, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'checks'}, ensure_ascii=True))
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
