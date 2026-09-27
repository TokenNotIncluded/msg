# Legacy escrow migration

The supported source fixture is the actual pre-LedgerAccount commit
`ac083b3666b1b5268d90115129e2cc4e0dd11521`. The test runs that checkout's
Application, schema, market operations and v4 backup code in a separate process.
It does not initialize the old database with the current schema or manufacture
old foreign keys in place of this end-to-end fixture. Critical source hashes
are pinned in `tests/fixtures/legacy_market_source.json`.

```sh
git worktree add --detach .legacy-ledger ac083b3666b1b5268d90115129e2cc4e0dd11521
MSG_TEST_LEGACY_SOURCE="$PWD/.legacy-ledger" python3.15 -m pytest \
  tests/test_legacy_ledger_snapshot.py tests/test_ledger_migration_json.py \
  tests/test_ledger_accounts.py tests/test_ledger_migration_references.py \
  tests/test_ledger_account_isolation.py
```

PostgreSQL and its matching `pg_dump`/`pg_restore` are required. A missing or
modified source fixture fails, rather than silently skipping the check. All
accounts, keys, funds and files in this fixture are disposable test data. The
fixture's root private key is not in its service backup.

## Transaction and reference contracts

`storage/ledger_migration.py` separates account discovery, typed-account
installation, ledger validation, identity-reference validation, foreign-key
switching, inert-identity removal and registration-trigger installation. The
existing PostgreSQL schema advisory lock and one enclosing transaction cover
**every** stage, including DDL. No stage commits. Interrupting any publishing
stage rolls back its changes; the same restored database can be retried.

Direct identity-reference columns are discovered conservatively from the
schema. Structured `body` fields additionally have explicit typed JSON paths:
resource owner/parent/group/authors, Revision authors/relations, Event actor and
subject, certificate subjects/authority/scope, job principals, and the other
listed domain records. A new body-bearing table requires an explicit reference
contract before an old escrow identity can be retired. The schema coverage test
makes additions/removals visible to reviewers.

`Event.data`, operation result data, a resource name and a Revision change note
are **not** generically interpreted as identity references. They may contain a
historical account ID as ordinary text. In particular, a money receipt's debit
or credit account is a legitimate LedgerAccount reference; the signed receipt
and ledger row are never rewritten. Unknown structured references, non-inert
identities, credentials, certificates, relations and authority references fail
closed rather than silently deleting an identity that is still in use.

## Verification scope

The fixture contains funded, settled and refunded orders, active/closed
bounties, paid claims, consumed challenges, deliveries and ordinary resources.
Tests compare every field of the ledger (including exact receipt text), sequence
state, all account balances, total supply, order/package/delivery/bounty/claim
references and the original resources, revisions, events and results. They run
repeat initialization, two independent concurrent initializers, failures after
account creation/FK switch/identity deletion/trigger installation, and an
unmodified retry after rollback. Historical ledger UPDATE and DELETE remain
rejected by the dedicated append-only trigger.

A restored service remains behind its write/worker recovery-drill gates. The
migration cannot promote it. Passing this fixture proves the supported old-code
migration and restore path, **not** migration of an operator's production data.
Production inventory, a protected actual backup, preflight and a separately
authorized cutover/rollback exercise remain deployment acceptance requirements.
Do not run these fixture scripts against an existing or production database.
