# Market boundary progress

## Scope and coordination

Issue #159, PR #168; references #85/#83, not completion of #71/#72 or production acceptance.
Base main: `476e64f04ed1d9c7dcab4a41c4f3cdb72faad6a7`, tree
`40ca207a3c49be347120e058e47ed328ec4e3098`. Branch: `fix/market-boundaries-20260929`.
Coordination: #83 comments5878359290 and5878591036; #159 comment5878570249.

This change does not modify #165/#167 core/tool/executor ownership, #155 recovery,
or #166 metadata sessions. No production, real funds, Root/PIN or outward delivery.

## Verified regression-first evidence

Red run **36483360971**, job109134178838, tested exact commit
`8692ad6c0056e793bbe7b0efc9f5fd0b08ab89c8` / tree
`907f51806164625749e57bf754713ae9f365f9b6`: **7 failed, 3 passed,
0 errors, 0 skipped**. All failures were new ownership/import assertions.
The real PostgreSQL buyer/seller privacy, forged-escrow zero-write guard,
and signed-receipt byte preservation/idempotent replay tests passed.

Artifact10998081065 was independently downloaded and hashed; source.json and JUnit
were read. SHA-256: `07f566e15500142c37223782d5cc10086bb9e92a967e5a204c52c374abae3600`.
No project tests ran locally.

## Implemented ownership

Before: market use cases imported records/catalog/posting/managed-delivery from
`plugins.orders/store/money/delivery`, including function-local imports, while
those entrypoints depended on market escrow and policies.

After:

- `market.order_records` owns signed subject/viewer checks, order IDs, authorized
  record lookup and explicitly legacy-compatible projections.
- `market.catalog` owns existing authorized listing/body/package reads, not
  version-specific schemas, defaults or registration.
- `market.ledger` owns policy constants, account requirements and the single
  posting implementation. It alone defines the existing `_ESCROW_WRITE` object;
  escrow re-exports that same object. It is not a public/network credential.
- `market.managed_delivery` owns existing bounded managed-package verification,
  preparation and read helpers; it does not replace manual/service/arbitration flows.

Old plugin paths re-export the same function objects, never wrappers or copies.
Existing market use cases and versioned handlers import the neutral owners.
The duplicate store subject check is the same function as the order subject check.
No new Manager, Factory, Repository, ORM, ledger or escrow state machine exists.

## Verified source assembly and review

Cloud assembly **36483781552** succeeded from input
`d0d7f4f61f138b0ced266f2324f4a90c1689b6b5`. Output commit
`f92390fe8f54fbf63fe21d770f37d6fdbb56190e`, tree
`f3f8a2c20c031e0fdf0e1129b2123e7350e7cfaa`.

Its audit checks exact input blobs and all **23** moved functions after identifier-only
renaming, every remaining handler/schema, and the complete existing market state
machines after excluding import statements. The only guard change is its owner;
its identity check and the checked escrow-release call path remain unchanged.
SQL strings, account locks, signatures, IDs, receipt purposes and transactions were
not rewritten. Shared posting still cannot commit or grant remote Root issuance.

Artifact10997307359 was downloaded, its audit/patch/source read, and its SHA-256
verified as `2dbcbf167086480216596757ffd32b42fa8ccc61cdea3d767e2862d1aba3ae74`.
The contained patch independently hashes to
`0f3ee2de9d5cb04c50ebf4b573c44652fa7a50a225f3e006f50944432e9173c9`.
The two one-shot assembly files are removed from the submitted tree.

## Final-head gates

Source review/assembly is not green test evidence. The new boundary contracts,
existing v1/v2/v3 checkout, clearing, Bounty, redemption and recovery regressions,
and complete four-shard exact-node-ID/conformance/build gate must run on the final
synchronized head. No skip, weakened assertion or published-version migration.
Actual final results and merge SHA are recorded in PR #168 and coordination #83;
this source commit does not predict those outcomes or close umbrella issues.
