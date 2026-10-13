"""Real process/transaction failure checks against a disposable PostgreSQL DB."""

from __future__ import annotations

import argparse
import asyncio
import json
import selectors
import time
from collections import Counter
from pathlib import Path

import psycopg
from postgres_fixture import database
from postgres_transport_fixture import LoseCommitAck
from test_postgres_parity import REPORT, Native, check, reset, snapshot
from test_storage_parity import action, audit, event, insert, ledger, once, resource

from msg.storage.postgres import PostgresMetadataStore


def line(p):
    with selectors.DefaultSelector() as selected:
        selected.register(p.stdout, selectors.EVENT_READ)
        if not selected.select(timeout=15):
            raise AssertionError('missing process checkpoint')
    return json.loads(p.stdout.readline())


def finish(p):
    p.stdin.write('continue\n')
    p.stdin.flush()
    out, err = p.communicate(timeout=15)
    if p.returncode:
        raise AssertionError('native process failed: ' + err)
    return json.loads(out.splitlines()[-1])


def kill(p):
    if p.poll() is None:
        p.kill()
    p.communicate(timeout=10)


def waiting_writer(dsn):
    deadline = time.monotonic() + 5
    with psycopg.connect(dsn, autocommit=True) as c:
        while time.monotonic() < deadline:
            if c.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND application_name='msgd-native' AND wait_event_type='Lock' AND wait_event='advisory')"
            ).fetchone()[0]:
                return True
            time.sleep(0.01)
    return False


async def run(args):
    native = Native(args.binary.resolve())
    with database() as dsn:
        store = PostgresMetadataStore(dsn)
        async with store.transaction(write=True) as tx:
            await tx.insert(resource('r_root', parent=None, name=''))
        baseline = snapshot(dsn)
        body = [insert(), action('event', record=event()), action('audit', record=audit())]
        try:
            for stage in ['before', 'after']:
                reset(dsn, baseline)
                p = native.start(
                    dsn,
                    [once(body + ([action('hold')] if stage == 'before' else []))],
                    hold_after_commit=stage == 'after',
                )
                try:
                    got = line(p)
                    visible = snapshot(dsn)
                    check(
                        'kill-' + stage + '-visibility',
                        got == {'checkpoint': 'transaction' if stage == 'before' else 'committed'}
                        and bool(visible['results'][1]) == (stage == 'after'),
                        got,
                        group='postgres_process',
                    )
                finally:
                    kill(p)
                actual, _ = native.run(dsn, [once(body)])
                after = snapshot(dsn)
                check(
                    'kill-' + stage + '-retry-once',
                    actual['status'] == 'ok'
                    and len(after['resources'][1]) == 2
                    and all(len(after[t][1]) == 1 for t in ['results', 'events', 'audit']),
                    actual,
                    group='postgres_process',
                )
            reset(dsn, baseline)
            first = native.start(dsn, [once(body + [action('hold')])])
            second = None
            try:
                check(
                    'first-writer-acquired',
                    line(first) == {'checkpoint': 'transaction'},
                    group='postgres_process',
                )
                second = native.start(dsn, [once(body)])
                blocked = waiting_writer(dsn)
                first_result = finish(first)
                out, err = second.communicate(timeout=15)
                check(
                    'concurrent-writers-one-commit',
                    blocked
                    and second.returncode == 0
                    and first_result == json.loads(out.splitlines()[-1])
                    and len(snapshot(dsn)['audit'][1]) == 1,
                    err,
                    group='postgres_process',
                )
            finally:
                kill(first)
                if second is not None:
                    kill(second)
            reset(dsn, baseline)
            second = None
            async with store.transaction(write=True) as tx:
                tx.set_setting('python-held', True)
                second = native.start(dsn, [action('set', key='native-held', value=True)])
                check('python-writer-blocks-native', waiting_writer(dsn), group='postgres_process')
            try:
                out, err = second.communicate(timeout=15)
                check(
                    'native-continues-after-python-commit',
                    json.loads(out.splitlines()[-1])['status'] == 'ok',
                    err,
                    group='postgres_process',
                )
            finally:
                kill(second)
            p = native.start(dsn, [action('set', key='native-held', value=True), action('hold')])
            try:
                line(p)

                async def python_contender():
                    async with store.transaction(write=True) as tx:
                        tx.set_setting('python-after', True)

                task = asyncio.create_task(python_contender())
                await asyncio.sleep(0.1)
                check('native-writer-blocks-python', not task.done(), group='postgres_process')
                finish(p)
                await asyncio.wait_for(task, 15)
            finally:
                kill(p)
            reset(dsn, baseline)
            p = native.start(
                dsn,
                [
                    action('effect', name='cleanup', hold=True),
                    action('set', key='abort', value=True),
                    action('fail'),
                ],
            )
            try:
                got = line(p)
                checkpoint = json.loads(p.stdout.readline())
                with psycopg.connect(dsn, autocommit=True) as c:
                    lock = c.execute(
                        'SELECT pg_try_advisory_lock(725274758,1886265951)'
                    ).fetchone()[0]
                    if lock:
                        c.execute('SELECT pg_advisory_unlock(725274758,1886265951)')
                check(
                    'fence-survives-sql-rollback',
                    got == {'effect': 'cleanup'}
                    and checkpoint == {'checkpoint': 'compensation'}
                    and not lock
                    and snapshot(dsn) == baseline,
                    group='postgres_process',
                )
                check(
                    'compensation-completes',
                    finish(p).get('code') == 'fixture_failure',
                    group='postgres_process',
                )
            finally:
                kill(p)
            reset(dsn, baseline)
            with LoseCommitAck(dsn) as proxy:
                actual, effects = native.run(
                    proxy.dsn, [action('effect', name='must-not-delete'), *body, ledger()]
                )
                check(
                    'lost-commit-response-is-uncertain',
                    proxy.dropped.is_set()
                    and actual.get('code') == 'storage_commit_uncertain'
                    and actual.get('uncertain') is True
                    and not effects,
                    actual,
                    group='postgres_process',
                )
            after = snapshot(dsn)
            actual, _ = native.run(dsn, [once(body)])
            check(
                'lost-commit-response-retry-no-duplicate',
                actual['status'] == 'ok' and after == snapshot(dsn) and len(after['audit'][1]) == 1,
                actual,
                group='postgres_process',
            )
            # Every schema mutation is isolated to this new test database and
            # reverted, so later tests never inherit a deliberately broken DB.
            reset(dsn, baseline)
            schema_changes = [
                (
                    'missing-dedupe',
                    'ALTER TABLE jobs DROP CONSTRAINT jobs_dedupe_key',
                    'ALTER TABLE jobs ADD CONSTRAINT jobs_dedupe_key UNIQUE(dedupe)',
                ),
                (
                    'missing-root-index',
                    'DROP INDEX one_root',
                    'CREATE UNIQUE INDEX one_root ON resources((1)) WHERE parent IS NULL',
                ),
                (
                    'wrong-primary-key',
                    'ALTER TABLE results DROP CONSTRAINT results_pkey; ALTER TABLE results ADD PRIMARY KEY(subject)',
                    'ALTER TABLE results DROP CONSTRAINT results_pkey; ALTER TABLE results ADD PRIMARY KEY(subject,request_id)',
                ),
                (
                    'missing-foreign-key',
                    'ALTER TABLE revisions DROP CONSTRAINT revisions_resource_id_fkey',
                    'ALTER TABLE revisions ADD CONSTRAINT revisions_resource_id_fkey FOREIGN KEY(resource_id) REFERENCES resources(id)',
                ),
            ]
            for name, break_sql, repair_sql in schema_changes:
                with psycopg.connect(dsn, autocommit=True) as c:
                    c.execute(break_sql)
                try:
                    actual, _ = native.run(dsn, [action('set', key='must-not-write', value=True)])
                    check(
                        name + '-rejected',
                        actual.get('code') == 'unsupported_storage_schema'
                        and snapshot(dsn) == baseline,
                        actual,
                        group='postgres_schema',
                    )
                finally:
                    with psycopg.connect(dsn, autocommit=True) as c:
                        c.execute(repair_sql)
        finally:
            await store.close()
    report = {
        'checks': len(REPORT),
        'failures': sum(not r['ok'] for r in REPORT),
        'groups': dict(Counter(r['group'] for r in REPORT)),
        'cases': REPORT,
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'cases'}))
    return report['failures']


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--report', type=Path, required=True)
    raise SystemExit(bool(asyncio.run(run(p.parse_args()))))


if __name__ == '__main__':
    main()
