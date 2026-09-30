# Bounty / issue #72 acceptance

Scope: the prefunded PoP market contract and its isolated official fixture.
The source base is main `c8ed2610ab774dba3085972683c1d2c8a9232222`.
CI provenance belongs to the exact tested tree; final results are recorded in
the PR and issue, rather than inferred from test names or this document.

## Requirement to assertion map

All node IDs below are relative to `tests/`. Parameterized nodes must all run.
The tests use actual PostgreSQL and the registered executor unless a row
explicitly describes a CLI parsing or pure-policy test.

| Requirement | Test node or installed check | Actual assertion |
| --- | --- | --- |
| Integer, protected prefunding; publisher can be offline | `test_bounty.py::test_prefunded_bounty_pays_offline_publisher_and_stops_at_budget` | BountyEscrow is a LedgerAccount, has no Subject or credential; one funding leg, one payout, exact recipient/escrow balances and total supply; no publisher call needed to claim |
| Current catalog and immutable historical terms | `test_bounty.py::test_bounty_catalog_current_projection_and_historical_revision` | Underfunded, top-up, paid, closed projections; historical budget/state and terms Revision remain original while current fields report live values |
| Search and real CLI across all lifecycle states | `test_bounty_public_lifecycle.py::test_search_and_real_cli_preserve_terms_and_current_accounting` | Six transitions: underfunded/top-up/publisher pause/claims exhausted/inclusive expiry/close. Both public search APIs locate the exact Revision; real CLI get/store get agree on state, reason, budget, escrow and claims; historical signed Revision and content bytes are unchanged; all public table values and effect entrypoints prove reads cause no writes |
| Search semantics | Same six-transition test | Search returns Resource metadata/references, not accounting balances. Resource publication state is distinct from Bounty payout state; dereferencing through bounty.get/store.listing_get yields the current accounting projection. Existing published schemas are unchanged |
| Pause/resume/top-up/close conservation | `test_market_72.py::test_pause_preserves_budget_topup_does_not_resume_and_close_returns_remainder` | Publisher pause survives top-up; pending claim denied while paused, succeeds after resume; close refunds exact remainder; balances sum to supply |
| Claim cap cannot reopen through top-up | `test_market_72.py::test_exhausted_claim_limit_is_consistent_in_both_views_and_topup_cannot_reopen` | Current views remain claims_exhausted with positive escrow; resume denied; claimant sees own claim and outsider sees none |
| Expiry does not write during GET | `test_market_72.py::test_expired_listing_projection_does_not_mutate_state_or_emit_events` | Both views report expired; stored state and Event count remain unchanged; six-transition test additionally checks exact deadline and every table value |
| Current key/revocation and subject binding | `test_bounty.py::test_bad_proof_wrong_subject_expiry_and_top_up_do_not_spend`; `test_market_contracts.py::test_revoked_or_replaced_identity_key_cannot_spend_pending_challenge` | Wrong proof key/foreign subject/expired nonce denied; old revoked key and replacement non-primary key denied; no payout or consumed nonce |
| Canonical nonce fields and verifier version | `test_market_72.py::test_stored_challenge_fields_must_match_canonical_signed_payload`; `test_bounty_commit_boundary.py::test_unsupported_challenge_verifier_cannot_consume_budget_or_nonce` | Tampered stored nonce or unsupported version cannot spend; full row values unchanged in the version case |
| Eligibility and per-subject limit | `test_bounty.py::test_bad_proof_wrong_subject_expiry_and_top_up_do_not_spend`; `test_bounty.py::test_unknown_eligibility_constraint_fails_closed`; `test_bounty_commit_boundary.py::test_subject_limit_rejects_fresh_challenge_while_budget_remains` | Allowlist excludes other subject; unknown constraint rejected before funding; fresh valid challenge denied at subject cap even with remaining budget |
| Last reward concurrency and close race | `test_bounty.py::test_concurrent_last_reward_only_one_paid_claim`; `test_market_contracts.py::test_claim_close_race_and_event_failure_never_lose_or_duplicate_budget` | Exactly one payout at last reward; close always succeeds and escrow ends zero; all remaining funds belong to publisher/claimant, no lost supply |
| Claim/ledger/receipt/Event/Inbox/results transaction | `test_bounty_commit_boundary.py::test_deferred_claim_commit_failure_preserves_all_rows_and_retry`; event-failure node above | Real deferred PostgreSQL trigger fails at COMMIT after handler preparation; every public row value rolls back, then same request succeeds exactly once and replay changes no rows; injected Event failure also rolls back payout/nonce/minimal notice |
| CLI proof retries and actual restart | `test_bounty_public_lifecycle.py::test_real_cli_prove_replays_after_restart_without_second_nonce_or_reward` | Real signer/Registry/executor/PostgreSQL through CLI; new Application returns identical payout/receipt; one challenge/claim, every row value unchanged on retry |
| Mixed mutable/immutable terms fail closed | All nodes in `test_bounty_snapshot_integrity.py` | Reward/cap/subject cap/eligibility mismatch rejects claim and reads; doctor does not repair; management cannot move ledger funds |
| Real backup and persistent restore quarantine | `test_market_72.py::test_backup_restores_claim_nonce_inbox_receipt_and_replay_together` | Real backup/empty PostgreSQL restore retains exact claim, nonce, result, minimal Inbox and ledger rows; saved-result replay stays writes_paused, including after marker deletion; private reads remain quarantined |
| Official mint/fund entrypoints | `test_market_71.py::test_bank_fund_is_a_formal_command_without_unattended_flags`; `test_market_71.py::test_bank_fund_two_console_approvals_before_pin_or_writes`; installed `market_e2e` | Real daemon parser and MoneyAdmin through isolated TestRoot/TestConsoleIO; mint20 and bank fund20 require explicit approvals. Refusal precedes PIN/writes; no unattended flag |
| Official 20→10→5 fixture | `msg.admin.market_check::check_market_e2e`, run by installed official selftest | Bank prefunds10; current buyer IdentityKey PoP earns10; fixed text `msg.lmm.best store selftest` and hello.txt `delivery-ok\n` cost5; V4 Delivery waits for buyer's signed exact digest, then buyer5/bank15/all escrow0/supply20 |
| Published V3 automatic settlement preserved | `test_market_lifecycle.py::test_instant_delivery_settlement_and_claim_are_distinct`; `test_market_cli_bindings.py::test_explicit_buy_version_preserves_old_inputs_and_settlement_semantics` | Original published policies remain automatic; settled does not manufacture claimed or accepted. Explicit old versions 1/2/3 retain signed input and behavior |
| Current default V4 explicit acceptance | All nodes in `test_market_explicit_acceptance.py` | CLI selects V4; current buyer and exact delivered digest required; wrong/revoked signatures cannot settle or replay; one protected posting; restart preserves delivered escrow |
| Disabled/failed/unverified mail does not gate trade | `test_market_integration.py::test_explicitly_disabled_mail_never_calls_sender`; `test_market_delivery.py::test_verified_checkout_mail_is_minimal_and_not_claimed`; `test_market_delivery.py::test_unverified_checkout_has_no_order_information_until_owner_verifies`; installed `market_e2e` | Disabled sender never called; failed sender leaves settled business fact intact; unverified receives only minimal verification; verified notice has order reference/name snapshot/authenticated pickup, no product secret; local sink only |
| Send-time endpoint/current authorization | `test_market_delivery.py::test_send_time_revalidation_prevents_stale_or_cross_subject_delivery` | Disabled, unverified and foreign-subject endpoint variants cannot leak order facts |
| Bank has no special business privileges | `test_market_redemption.py::test_clearing_policy_is_pure_and_has_no_bank_or_role_exceptions`; `test_market_71.py::test_bank_fund_role_and_payment_rollback_together`; `test_market_cli_bindings.py::test_package_shortcut_binds_actual_id_and_preserves_seller_privacy` | Pure policy without bank/role override; failed bank funding preserves role/payment; another subject cannot inspect private package |
| Default/doctor/bootstrap/selftest guard | `test_market_contracts.py::test_default_doctor_is_read_only_and_selftest_cannot_seed_an_installed_server`; `test_market_recovery.py::test_market_doctor_cannot_mutate_or_synthesize_a_repair`; installed selftest | Default publishes no fake budget; doctor retains all table values; production selftest refuses seeding an installed server; official selftest is isolated |

## Version and field boundary

The original issue's automatic-settlement wording belongs to published V3.
The authoritative current design and default V4 require explicit signed buyer
acceptance. Both are tested; reverting V4 to automatic settlement would alter
the current contract. Delivery, claimed, accepted and settled are separate facts.

PoP proves possession of the current IdentityKey for one subject/challenge.
It does not prove a human, trustworthiness or resistance to Sybil identities.
The bank remains an ordinary Subject for catalog, verification and permissions.

This code acceptance does not authorize production mint/funding, Root/PIN,
SMTP/Webhook delivery or deployment. Real target-host/old-snapshot/current-pin
and cutover evidence remain separately tracked in #64–70 and #84.
