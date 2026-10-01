# Account selection and recovery — issue #219

Source baseline: `4bbdbca52b54f8d2de36a7a04b5207a9b9951d61` (msgctl 0.2.11).
Scope: client account selection, metadata listing and legacy migration boundaries.

## Reproductions and changes

- Restored account directories without `current-account.json` silently fall back
  to `default`, even when that is a different saved identity. Require an explicit
  selection when saved accounts exist and no legacy service singleton remains.
- A selected account with missing state can accidentally import a different old
  profile or create an empty replacement. Reject the missing selected state before
  migration; apply the legacy-handle guard to saved selections as well as explicit
  `--account` arguments. Explicit import remains available for recovery.
- `account list` creates state, changes selection and can move legacy credentials.
  Inspect config/data/state account directories and safe metadata without creating
  files, loading keys or migrating anything. Show incomplete accounts as well.
- `account use` can update the remembered service before rejecting an
  unauthenticated account. Check the saved subject before constructing client
  state. Failed selection preserves the previous service and account.
- The list's `selected` flag now reflects an invocation-only `--account` override,
  while the persistent default remains unchanged.

## Validation

`tests/test_client_account_recovery.py` adds regression coverage. Existing account,
connection and path tests continue to cover singleton migration, profile aliases,
hardware/software separation, imports, keys and journals. The service-alias CI
workflow includes all these files and records source and JUnit evidence.

The first PR commit contains only tests and workflow wiring, for baseline failure
evidence. The following implementation commit is verified separately. Exact run
results belong in PR #232; this file does not predeclare CI success.

Issue #219 also includes server-account retirement and full recovery acceptance.
Those are separate from this code change; the issue stays open. No production
account, credential, release or deployment is changed by this work.
