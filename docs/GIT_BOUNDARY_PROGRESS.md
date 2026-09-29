# Git transport boundary progress

## Scope and coordination

Issue #163. Branch `fix/git-transport-boundaries-20260929`. Base main
`08c58928ee3f6c6cb350d9e9fba75d072bd78f08`, tree
`8a62150d8273a2c75775872694749cb529835966` includes merged #165.
Coordination: #163 comment5878830664 and #83 comment5878871933.
No changes to parallel #166/#167/#169 executor/storage or #155 recovery branches.

Main was compared with the inspected source baseline476e64f: only #165's eight
files changed, none of the repository/SSH/HTTP modules under review. The source
blobs remain repositories940a1a1bd987a2d97273734a8c0646097abf3c83,
ssh_gitd6465ce37fd7e4743773942144e91e10a7c847af, and
http_routes04a7de0431f1211893111e887ef87661ef877c0f.

## Regression-first stage

Nine new tests cover shared import isolation, actual store/adapter ownership,
one shared publication implementation, shared HTTP-helper ownership, explicit
composition and two existing-behavior characterizations (real Git public reads
with unchanged database/refs, and bounded reference batch parsing).
Production source is unchanged in the first commit. Obtain actual red cloud
JUnit/source evidence before implementation; do not claim a result from intent.

## Observed and intended boundaries

NativeGitStore currently owns physical repositories and HTTP/LFS Request/Response
handling; HTTP receive imports guarded_command from ssh_git, which imports the
store back. The planned separation is:

- NativeGitStore keeps repository/LFS paths, bounded store operations and physical
  publication primitives, taking a snapshot of required settings rather than
  retaining an Application service locator. Existing operation registration and
  bundle worker behavior remain in their current module.
- transports.git_http.GitHTTPAdapter composes a NativeGitStore and owns all HTTP
  envelopes, streaming/spooling and signed/Basic protocol adaptation.
- extensions.git_publication owns the existing ReferenceGuard and guarded Git
  subprocess lifecycle. SSH and HTTP call this same implementation. Historical
  ssh_git imports remain identical compatibility aliases, not duplicate wrappers.
- transports.http_common owns the three existing body/JSON/error helpers and base
  headers; HTTP routes and Git HTTP import it, avoiding a new router/adapter cycle.

These are source ownership changes, not a new Git workflow/authorization engine.
Original capacity limits, request IDs, signed envelopes, current-authority checks,
reference-transaction phases, replay cache, uncertainty reporting, LFS digest and
shared-volume constraints, and bundle job fences remain. No protocol version,
database schema, receipt, real repository migration or deployment change.

## Cloud acceptance

The dedicated workflow runs real PostgreSQL/Valkey/Git/git-lfs and the existing
HTTP/SSH/bundle/LFS/read-only and completion-fence tests. The full four-shard exact
node-ID/conformance/build and applicable Recovery safety gates remain required on
the final combined tree. No local project execution, skipped test, weakened
assertion, production credentials, real funds/outbound delivery or recovery promotion.


## Implemented extraction and source audit

NativeGitStore no longer owns any HTTP handler, Request/Response conversion,
body spooler or Application property. It snapshots repository/blob paths and
keeps its original physical store algorithms. GitHTTPAdapter explicitly composes
that store and retains existing HTTP/LFS behavior. ContextVar identities used by
registered operations remain singular; upload semaphores move with HTTP spooling.

The original ReferenceGuard, hook program, bounded stdin relay and guarded Git
command are unchanged in git_publication. SSH exposes same-object aliases and its
original receive_pack entry. HTTP calls the same shared guard. The bundle worker
algorithm, including current_attempt and transaction fencing, is untouched.

HTTP helpers/base headers have one owner in http_common; routes and Git HTTP use
it. transports.http and its outer passive/recovery boundary are unchanged. The
assembly audits every moved/retained method, every existing registration and
bundle worker body, all shared publication functions, helper functions/headers,
and route functions. Only settings snapshots, explicit store attribute delegation
and import/adapter selection are permitted differences. Source-only AST evidence
is not a successful project test. Final source and patch are uploaded; one-shot
tooling is removed from the submitted tree before focused/full validation.
