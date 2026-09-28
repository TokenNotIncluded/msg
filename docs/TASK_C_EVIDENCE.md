# Task C: client/read integrity increment

Refs #76, #78, #79, #82, #83 and #85. This is a partial implementation and an
acceptance index, not closure of any umbrella issue or a deployment approval.
Base: `67401759a853c69fc5cb234982e1f7020b650542` (main after #114).
Local branch: `fix/task-c-client-content-safety-final-20260928` (retains the
initial C commit and a separate evidence-helper correction).

## Implemented boundary and evidence

| Requirement | Implementation / default | Executable evidence |
|---|---|---|
| #76: reads preserve business facts and cannot dispatch effects | Reuse the existing nine-source authorization matrix. Compare every field in every authoritative public PostgreSQL table before/after successful and denied reads, including history, pinned revisions, Search/Sync/Inbox/Outbox. Trap content publication/pins, maintenance, workers, mail, webhooks, sandbox execution and Valkey `publish_pending`. | `test_authorization_matrix.py`, `read_only_evidence.py`, `test_read_only_evidence.py`; the helper's negative controls reject an in-place SQL update, a send before network access and attempted notifications/sends even when their exceptions are caught. A configured idle signal also has a positive control. |
| #78/#82: Following | `communication.following@1` is a private read projection of existing `watches`, empty by default, disabled with the communication plugin. No new table, grant, notification feed or implicit watch. Stable resource-ID pagination; limits 1–100, shared scan/node/cost/deadline/response budget; principal-bound 15-minute cursor; closed schema and cursor revalidation. Current revoke/unwatch/archive always wins. | `test_following.py`: normal/empty/anonymous/wrong subject/expired cursor/unknown constraints/budget denial; full-state comparisons; HTTP, signed PathGET, GraphQL, MCP and real CLI parity; HEAD/cache reauthorization; TUI explicit continuation. |
| #79: exact text boundaries | Unified diffs use LF boundaries as Git does, preserving CR and Unicode separators inside signed source bytes. Markdown locations use only CR/LF/CRLF line boundaries. Existing independent Revision signatures, uniqueness checks, old bodies and key IDs remain unchanged. | `test_text_patch_line_boundaries.py`: real `git apply` oracle, no-final-newline and fake-heading rejection; existing `test_text_patch.py` / `test_revision_rebase.py` remain binding. |
| #82/#85: resilient clients | Reject malformed origins and non-object envelopes with stable bounded error codes. Shared result decoding rejects malformed error fields rather than rewriting signed receipts. TUI catches transport disconnects, drops stale selections/cursors on identity or server changes (also during an in-flight request), and never retries or writes automatically. | `test_client_response_boundaries.py`, `test_tui_transport_boundaries.py`, existing transport/client/TUI regressions. |
| #83: operational checks | `doctor` reports Following's empty/read-only/bounded contract or disabled state. Isolated `selftest` verifies empty/authenticated/watch/revoke flow with its own fixture. Existing offline schemas, Registry deep copies and exact node-ID gate remain unchanged. | `test_following.py`, `test_operations_admin.py`, `test_dictionary.py`, `test_ci_shards.py`; full core shards and conformance must run on the final source SHA. |

## Client entrypoints

`msg following` returns one JSON result page using the existing signed client;
`msg following --limit 10` selects the first page size. Continue explicitly with
`msg following --cursor TOKEN`, without adding a new limit. The TUI command is
`following`, then `n`. `GET/HEAD /@name/following` uses the same principal-scoped
operation; another subject is denied. Opaque continuation links require the same
current authorization. Tokens here are query cursors, never reusable credentials.
Only the new operation and its two input field codes are appended to the existing
shortcode dictionary. Previously published codes and signed authority ceilings
are not rewritten or expanded.

Watches do not have historical membership records. Pagination bounds resource
creation and stable ID order; it does **not** promise an immutable snapshot of
past subscriptions. No historical membership table is invented to hide this gap.

## Untouched boundaries and remaining acceptance

* #77 individual SOUL/AGENTS/Notes/Legacy signatures, recovery history and R1–R5
  acceptance are not completed by this increment. No auto-Legacy/Memory/SOUL or
  authority-bearing honors are introduced.
* #78 complex nested read/QueryRef/Transfer, long Sync/read resync and all search
  facets remain subject to their existing acceptance inventory. Following does
  not stand in for those requirements or silently enlarge their limits.
* #79 checkout file operations, batch publication/GC/recovery and the complete
  attachment/LinkSet/Range matrix still need final combined-head evidence with B.
* #82 the TUI remains read-only. Design-required explicit write flows and real
  sshd/ForcedCommand/AuthorizedKeysCommand, bwrap/resource-limit, DNS/redirect/SSRF
  and RSS acceptance are not claimed complete. No process-name, TTY or environment
  variable is promoted to OS identity; no production network capability is enabled.
* A owns quarantine/recovery and B owns physical storage, worker attempts and
  deadlines. This branch does not replace either domain implementation. #112
  quarantine denial and independent underlying revocation/field-pruning evidence
  must both survive integration; no bypass or weakened expected error is added.

## Reproducible verification and reporting

Use the target Python 3.15 environment with real PostgreSQL/Valkey/Git/LFS and
required age/nginx tools. Run each index 0–3 with
`python scripts/ci_shards.py run INDEX 4`, then
`python scripts/ci_shards.py check 4`. Also run
`python -m pytest conformance --junitxml=artifacts/conformance.xml` with the real
tokenizer cache, `python -m build`, `python -m compileall -q src`, and
`git diff --check`. Do not skip tests, substitute row counts for node IDs, or
credit a run from a previous head.

The accompanying handoff records the frozen commit/tree, environment, commands,
JUnit IDs, passed/failed/skipped counts and build evidence. Local Python 3.13
results, when used as supplemental evidence, are explicitly **not** Python 3.15
CI or final A+B+C integration evidence. Remote PR/issue writes and merges must be
reported only after the respective API action succeeds; this local increment
alone closes no issue and modifies no cloud design or production state.
