#!/usr/bin/env python3
"""Real local MSG mailboxes with synthetic tasks; not a model/harness ranking."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
import platform
import random
import statistics
import tempfile
import time
import traceback
from contextlib import ExitStack, contextmanager
from pathlib import Path

import httpx

from msg.client import ClientState
from msg.client_events import LocalEvents
from msg.client_listener import listen
from msg.client_subagents import LocalAgents


@contextmanager
def count_http():
    """Count and reject any accidental HTTP from a local-only measurement."""
    counter = {'attempts': 0}
    original_sync, original_async = httpx.Client.send, httpx.AsyncClient.send

    def blocked(*args, **kwargs):
        counter['attempts'] += 1
        raise RuntimeError('Unexpected HTTP in local mailbox benchmark')

    async def blocked_async(*args, **kwargs):
        return blocked(*args, **kwargs)

    httpx.Client.send, httpx.AsyncClient.send = blocked, blocked_async
    try:
        yield counter
    finally:
        httpx.Client.send, httpx.AsyncClient.send = original_sync, original_async


def load_baseline(path):
    spec = importlib.util.spec_from_file_location('msg_listener_baseline', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.listen


async def trial(function, native, durations, workers, interval, idle_seconds):
    with tempfile.TemporaryDirectory(prefix='msg-listener-bench-') as root, ExitStack() as stack:
        directory = Path(root)
        state = ClientState(directory / 'account', server='https://benchmark.invalid')
        store = LocalAgents(state, username='benchmark')
        store.create('lead')
        store.create('worker')
        events = stack.enter_context(LocalEvents(store.path)) if native else None
        requests = stack.enter_context(count_http())
        ready = asyncio.Event()
        acknowledged = [asyncio.Event() for _ in durations]
        seen, latencies = set(), []
        reads = 0

        async def fetch(cursor, tail=False):
            nonlocal reads
            result = await asyncio.to_thread(store.inbox, 'lead', cursor=cursor, tail=tail)
            reads += 1
            ready.set()
            return result

        class Output:
            def write(self, line):
                event = json.loads(line)
                if event['id'] in seen:
                    raise RuntimeError('Duplicate message delivery in uninterrupted trial')
                seen.add(event['id'])
                body = json.loads(event['message'])
                latencies.append((time.monotonic_ns() - body['completed_ns']) / 1_000_000)
                acknowledged[body['task']].set()

            def flush(self):
                pass

        options = {'wait_for_change': events.wait} if events is not None else {}
        reader = asyncio.create_task(
            function(
                fetch,
                context={'source': 'local', 'agent': 'lead'},
                cursor_file=directory / 'cursor.json',
                interval=interval,
                max_events=len(durations),
                output=Output(),
                **options,
            )
        )
        tasks = []
        try:
            await asyncio.wait_for(ready.wait(), 10)
            idle_start, idle_cpu = time.perf_counter(), time.process_time()
            idle_reads, idle_requests = reads, requests['attempts']
            await asyncio.sleep(idle_seconds)
            idle = {
                'seconds': time.perf_counter() - idle_start,
                'cpu_seconds': time.process_time() - idle_cpu,
                'source_reads': reads - idle_reads,
                'http_requests': requests['attempts'] - idle_requests,
            }

            async def worker(index):
                # Release a worker slot only after the coordinator observes its
                # result. The same independent tasks run with 1 and N workers.
                for number in range(index, len(durations), workers):
                    await asyncio.sleep(durations[number])
                    body = json.dumps({'task': number, 'completed_ns': time.monotonic_ns()})
                    await asyncio.to_thread(
                        store.send,
                        'worker',
                        'lead',
                        body,
                        message_id=f'task-{number}',
                    )
                    await acknowledged[number].wait()

            started, cpu, active_reads = time.perf_counter(), time.process_time(), reads
            tasks = [asyncio.create_task(worker(index)) for index in range(workers)]
            await asyncio.wait_for(
                asyncio.gather(*tasks, reader), max(30, len(durations) * interval * 3)
            )
            elapsed, cpu_seconds = time.perf_counter() - started, time.process_time() - cpu
            expected = {f'task-{number}' for number in range(len(durations))}
            if seen != expected:
                raise RuntimeError('Incomplete task results')
            ordered = sorted(latencies)
            return {
                'status': 'measured',
                'workers': workers,
                'tasks': len(durations),
                'task_successes': len(seen),
                'duplicate_deliveries': 0,
                'total_task_seconds': elapsed,
                'active_cpu_seconds': cpu_seconds,
                'active_source_reads': reads - active_reads,
                'http_requests': requests['attempts'],
                'idle': idle,
                'completion_to_observation_ms': {
                    'median': statistics.median(latencies),
                    'p95_nearest_rank': ordered[math.ceil(len(ordered) * 0.95) - 1],
                    'max': max(latencies),
                },
            }
        finally:
            for task in [reader, *tasks]:
                task.cancel()
            await asyncio.gather(reader, *tasks, return_exceptions=True)


async def measure(args):
    modes = [('local_events', listen, True)]
    missing = {
        'remote_events': {'status': 'not_implemented'},
        'harness_native': {
            'status': 'not_measured',
            'reason': 'No native harness adapters or runs.',
        },
        'remote_polling': {'status': 'separate_test', 'artifact': 'remote-idle.json'},
    }
    if args.baseline_listener:
        modes.append(('legacy_listen', load_baseline(args.baseline_listener), False))
    else:
        missing['legacy_listen'] = {
            'status': 'not_measured',
            'reason': 'Provide --baseline-listener.',
        }
    if platform.system() != 'Linux':
        modes = [entry for entry in modes if not entry[2]]
        missing['local_events'] = {
            'status': 'not_supported',
            'reason': 'Native test requires Linux.',
        }
    results, failed = [], False
    for number in range(args.trials):
        rng = random.Random(args.seed + number)
        durations = [rng.uniform(0.02, 0.08) for _ in range(args.tasks)]
        # Alternate order to reduce systematic warm-up/order bias.
        for name, function, native in modes if number % 2 == 0 else list(reversed(modes)):
            pair = {}
            for workers in (1, args.workers):
                try:
                    row = await trial(
                        function, native, durations, workers, args.poll_interval, args.idle_seconds
                    )
                except Exception as exc:
                    traceback.print_exc()
                    row = {'status': 'failed', 'error_type': type(exc).__name__, 'workers': workers}
                    failed = True
                pair[str(workers)] = row
            result = {'mode': name, 'trial': number, 'runs': pair}
            if all(row['status'] == 'measured' for row in pair.values()):
                speedup = (
                    pair['1']['total_task_seconds'] / pair[str(args.workers)]['total_task_seconds']
                )
                result.update(observed_speedup=speedup, parallel_efficiency=speedup / args.workers)
            results.append(result)
    return {
        'schema_version': 1,
        'status': 'failed' if failed else 'measured',
        'revision': args.revision,
        'python': platform.python_version(),
        'platform': platform.platform(),
        'poll_interval_seconds': args.poll_interval,
        'idle_seconds_requested': args.idle_seconds,
        'seed': args.seed,
        'baseline_sha256': hashlib.sha256(args.baseline_listener.read_bytes()).hexdigest()
        if args.baseline_listener
        else None,
        'workload': 'Seeded synthetic sleep tasks, real local MSG SQLite send/read/checkpoint operations.',
        'scope': 'Single process with concurrent tasks and SQLite worker threads; not LLM or harness work.',
        'model_calls': 0,
        'model_tokens': None,
        'ranking': None,
        'missing_comparisons': missing,
        'results': results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline-listener', type=Path)
    parser.add_argument('--revision', default='unrecorded')
    parser.add_argument('--trials', type=int, default=3)
    parser.add_argument('--tasks', type=int, default=8)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--poll-interval', type=float, default=1.0)
    parser.add_argument('--idle-seconds', type=float, default=2.0)
    parser.add_argument('--seed', type=int, default=1729)
    args = parser.parse_args()
    if args.trials < 1 or args.tasks < 2 or not 2 <= args.workers <= args.tasks:
        parser.error('Use trials >= 1 and 2 <= workers <= tasks.')
    if any(not math.isfinite(v) or v <= 0 for v in (args.poll_interval, args.idle_seconds)):
        parser.error('Intervals must be finite and positive.')
    result = asyncio.run(measure(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'status': result['status'], 'output': str(args.output)}))
    return int(result['status'] == 'failed')


if __name__ == '__main__':
    raise SystemExit(main())
