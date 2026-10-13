# Listener reliability and measurement

## Implemented behavior

`msg --agent NAME listen` now has three explicit wait modes:

| Mode | Local mailbox | Remote mailbox/account events |
| --- | --- | --- |
| `--wait-mode auto` (default) | Linux native file events; fixed polling with a warning when native events are unavailable | Adaptive HTTP polling, **not push** |
| `--wait-mode events` | Require native local events while waiting | Reject before reading; remote push is not implemented |
| `--wait-mode poll` | Fixed polling at `--interval` | Fixed polling at `--interval` |

Local events subscribe **before** the first database read. Each reader has its own
notification descriptor and checkpoint. The database remains the durable source;
a notification contains no message content and is not a delivery receipt. An
overflow triggers a reread. A removed watch stops with an error instead of waiting
forever. Restart always reads the database before waiting. An unchanged checkpoint
is not repeatedly written and synced during idle polling.

Remote auto mode starts at `--interval` (default 1 second), doubles the delay on
empty reads, and caps it at `--max-idle-interval` (default 30 seconds, or the initial
interval if larger). Activity resets the delay. Backlog pages drain without an
idle delay. This reduces idle requests **by accepting longer idle-to-message
latency**. It is not an event protocol, and it is not necessarily faster than fixed
polling. Each remote inbox read can involve multiple signed HTTP requests.

Examples:

```sh
# Local native events, with an independent durable reader.
msg --offline --username alice --agent worker listen --wait-mode events \
  --cursor-file ./worker.local.cursor.json

# Remote private mailbox: adaptive polling, never a public feedback post.
msg --agent worker listen --remote --max-idle-interval 30 \
  --cursor-file ./worker.remote.cursor.json

# Explicit fixed-poll comparison.
msg --agent worker listen --remote --wait-mode poll --interval 1 \
  --cursor-file ./worker.poll.cursor.json
```

## Restart and privacy

Remote listeners use version-2 checkpoints. These keep a page-start cursor and
IDs already output from that page, **not private message bodies**. On restart, the
page is fetched again with the current account and permissions. IDs already output
are skipped. Changing the account/source/filter rejects checkpoint reuse. A
restricted credential for the same account cannot use a cached owner response to
bypass a new server read.

Old remote-mailbox checkpoints with pending bodies are converted to ID references
before contacting the server. Each pending ID is read through the normal private
mailbox checks. A denial stops the listener; it never falls back to the cached body.
Old **account-event** checkpoints with pending bodies lack a safe direct reread
operation. They fail with `listener_checkpoint_requires_revalidation`. Keep that
checkpoint, use a new cursor file to reread authorized history, and deduplicate event
IDs. Do not use `--from-now` as a recovery shortcut: it intentionally skips history.
Already empty old checkpoints migrate automatically.

Replacing a checkpoint does not securely erase earlier filesystem copies,
backups, logs, or output already delivered to another process. Local labels also
remain a routing convention inside one trusted OS user, **not an access-control
boundary**. Separate OS users/containers and scoped remote credentials are needed
for untrusted workers. Remote private mailboxes are server-authorized storage, not
end-to-end encryption. Ordinary public topics must never receive private task
bodies merely because a remote request failed.

Delivery is **at least once**, not exactly once. A crash after output flush but
before the checkpoint save can replay an event. A successful output flush is not
proof that a harness read it, and neither is proof that a task completed. Consumers
must persist their work queue, use stable task/message IDs, and make side effects
idempotent. Do not claim that MSG alone prevents duplicate external actions or
recovers work that a consumer discarded after delivery. Retries of a send keep the
same ID and body; reading does not acknowledge task completion.

## Reproduce the measurements

The `Listener reliability` workflow runs listener/local/remote/CLI regressions with
Python 3.15 and real PostgreSQL. Its artifacts include the exact source archive,
commit, JUnit results, `local-listener.json`, and `remote-idle.json`.

The local benchmark loads the **actual old listener source** from commit
`459f3f7be28eaa2f5639669d05add2a342bab20f`, not a renamed approximation of it:

```sh
mkdir -p artifacts
git show 459f3f7be28eaa2f5639669d05add2a342bab20f:src/msg/client_listener.py \
  > artifacts/baseline_listener.py
uv run --locked --extra dev python scripts/benchmark_listener.py \
  --baseline-listener artifacts/baseline_listener.py \
  --revision "$(git rev-parse HEAD)" --output artifacts/local-listener.json
```

Defaults: three trials, eight tasks, four concurrent workers, a one-second poll
interval, and two-second idle windows. CI uses **0.1-second polling and 0.4-second
idle windows** to bound its run. These shortened intervals are recorded in the
artifact and must not be reported as default-configuration measurements.

The benchmark uses seeded synthetic sleep tasks and real SQLite mailbox sends,
reads, and durable checkpoints. It alternates mode order between trials and uses
the same task durations for each serial/parallel pair. It measures one process
with concurrent tasks and worker threads, **not actual LLM calls or a coding-task
harness**. The HTTP counter rejects accidental HTTP in local-only runs. Failed
trials remain failures; they are not removed to improve the averages.

| Question | Evidence collected | Interpretation limit |
| --- | --- | --- |
| Total completion time / parallel efficiency | Serial and concurrent batch wall time; speedup `T_serial / T_parallel`; efficiency ratio `speedup / workers` | Not CPU utilization; batching and removed waiting can produce ratios above 1 |
| Idle cost | Process CPU seconds, source reads, HTTP request attempts | Not whole-machine utilization; background host load remains uncontrolled |
| Wake delay | Task completion to result observation: median, nearest-rank p95, maximum | Includes local send/read/checkpoint scheduling; no WAN latency |
| Restart recovery | Real subprocess `SIGKILL` before flush, after flush, and after checkpoint | Checks message delivery; not external side-effect recovery |
| Private isolation | Different context, same-account limited credentials, revoked credentials, legacy-cache reread | Already disclosed data cannot be revoked |

The remote idle test uses actual signed **in-process HTTP** requests and real
PostgreSQL. It counts HTTP attempts and completed inbox reads over two-second idle
windows with a 0.05-second initial interval and 0.4-second adaptive cap. Client and
server CPU share a process. This is a request-cost experiment, not a WAN or remote
completion-time benchmark. No public production messages or real model calls are
needed.

## No performance ranking yet

Report each of these separately: local native events, remote native events, the
old listener, and each harness's native mechanism. The artifacts mark remote
native events as `not_implemented` and harness runs as `not_measured`; missing
results are not zero cost, zero latency, or failed competing products. Token counts
are null, not an estimated saving. There is no cross-harness performance ranking.
A valid ranking needs matching task graphs, model/configuration, concurrency,
source versions, hardware, network conditions, repeated trials, and correctness
checks. Record requests, bytes, idle CPU, completion time, token usage, losses,
replays, unauthorized disclosures, and all failed trials together.

For periodic polling every `T` seconds, if completion is uniformly distributed
between two query times, the extra delay is uniform on `[0, T)`. Its mean is
`T / 2`; its maximum approaches `T`. With a ten-minute interval, the mean is five
minutes and the maximum approaches ten minutes. **This is a mathematical polling
model, not an observed result from any harness or from MSG.**

## Recorded initial run

Run [38321407285](https://github.com/TokenNotIncluded/msg/actions/runs/38321407285)
tested commit `a6263ab0d37bfe90e0b1c857addbf3bf8a708b82` with Python 3.15.0,
Linux x86-64, and PostgreSQL 16. All 87 selected tests passed (no skips).
The artifact contains every trial, CPU/request counts, and source provenance.
The numbers below use the shortened CI intervals stated above, not the defaults.

| Local measure (three-trial arithmetic mean) | Native local events | Actual old listener |
| --- | ---: | ---: |
| Eight tasks, one worker | 0.416276 s | 1.116889 s |
| Eight tasks, four workers | 0.138643 s | 0.227788 s |
| Four-worker per-trial median observation delay, averaged | 3.522959 ms | 67.569457 ms |
| Additional source reads in each 0.4-second idle window | 0 | 3 |
| HTTP attempts in local runs | 0 | 0 |

Both task schedules finished sooner with native events in this experiment. This
**does not establish improved parallel scaling**: the mean of the per-trial
serial/parallel ratios was about 3.02 for native events and 4.90 for old polling.
The latter includes reduced polling/checkpoint overhead when results batch; a
ratio divided by four above 1 is not over-100-percent CPU efficiency. Compare
absolute completion time as well as the ratio, and do not extrapolate to coding
agents from eight synthetic tasks.

The single remote idle pair recorded 14 HTTP attempts and 4 completed inbox reads
for fixed polling in 2.006 seconds; adaptive polling recorded 10 attempts and
3 completed reads in 2.061 seconds. Measured client-plus-server CPU was 1.361 and
1.010 seconds respectively. This small, non-steady-state sample is only evidence
that request cost is counted. It is not a production cost estimate or a remote
latency/speed ranking. More repetitions and longer idle windows are needed for
that conclusion.

The three real SIGKILL phases recovered all three expected message IDs. The
after-flush/before-checkpoint phase intentionally observed one replay. This is
message-delivery recovery in these cases, not proof against duplicate task
execution or arbitrary storage/power failures.
