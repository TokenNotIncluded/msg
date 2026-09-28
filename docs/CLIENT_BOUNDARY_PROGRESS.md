# Client dependency boundary progress — 2026-09-29

Refs #158/#82/#83/#85. Branch `fix/client-boundary-20260929` starts at tool-boundary head `f14fd1533f70983b046df4b4c813993e8a536d12` (#165). It does not write the #155 recovery, #156 executor or #166 metadata-session branches.

## Regression-first work

This commit adds seven boundary/compatibility/installation checks and a standalone fresh-interpreter client probe. It contains no implementation fix yet. The probe imports client/CLI/TUI modules under a strict server-import guard, creates/reloads protected client key files, and sends the same genuinely signed request through HTTP, path GET, GraphQL and MCP using an in-process mock HTTP endpoint. It never sends to a real service.

The independent workflow builds the real wheel and installs it with normal dependencies into a fresh virtualenv outside the source checkout. It runs pip check, both command help entrypoints and the probe with explicit rejection of server-only packages. No PostgreSQL/Valkey service is configured for this workflow; full repository CI remains unchanged and mandatory.

## Intended ownership and compatibility

- Compact result serialization belongs to protocol codec; executor keeps only a compatibility import.
- Atomic protected local-file replacement has one narrowly named owner, not the Git/CAS backend. Existing permissions/fsync/replacement semantics remain unchanged.
- Custodial proof input/derivation and signed migration statements share one state-free owner; server-held vault and retirement verification stay server-side. Existing exports remain aliases, not copied security code.
- MCP version negotiation constants must not require importing the server implementation from the network client.
- Base installation expresses client dependencies; the server extra is the complete server install. The existing dev extra retains server dependencies and msg/msgd command names are unchanged. Documentation must explicitly migrate full server installation to the server extra; no automatic package installation.

## Evidence still required

Read the RED run before implementation. Then verify helper compatibility, four real client transport calls, a dependency-clean wheel environment, all existing credential journal/recovery and server tests, full exact-node-ID/conformance/build gates and current integration tree. No size, speed, Windows/Android field result or production readiness is claimed by this refactor. Keep #158 open until those facts are proved.
