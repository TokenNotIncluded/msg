# 第01–14章设计契约总表

固定源码 `fbe5175de5448fe82394d4d57532e49a5b2809ab`；设计SHA-256 `d64dd796b2f004233d8966e03cc6e6890b240dc000de99bd8755872a684fa00a`。

有限分母：14章、157正文行、564句/分号义务；另列371命名字段成员、46实际配置字段。字段成员不是额外独立业务承诺，不用相加制造完成率。

Each nonempty design line after a chapter heading is split only at Chinese full stop/semicolon. Each resulting explicit obligation has a stable chapter.line.clause ID. Named model fields have separate child rows; optional fields, alternatives, states and lists remain in verbatim parent text. No Cartesian expansion of input combinations. Heading goal statements retained.

本轮仅查源码、构造无数据库Registry和复用已有证据，没有跑大套件。源码/测试导航关联不等于逐断言验收；每条保留此边界。归档规范未在当前正文展开的别名，不能猜补。

## 明确缺项与后续检查

- **G1 [diagnostic_coverage_gap]** Business implementations/tests exist. Explicit pending diagnostic work: manifest_features owns tool_sandbox/following/collaboration and central registration; recover_transports Transfer(design138), collaboration_contracts privateDM(146-147), recover_resources content editing(124), under design180. CLI/TUI per-view features are not demanded. Until merged and tested, related content/Git hooks do not prove these contracts.
- **G2 [integrated_pending_final_ci]** OrderResource and Root import helper now included at candidatefbe5175 per parent; exact-head tests and real console mapping remain separate. Not a current missing source claim.
- **G3 [external_gate]** Full recovery proof/promotion source is now included throughc9f23e2/48cb58e/b795dce/92322f4. Independent fresh pin/backup retirement, real console PIN and production newRoot mapping remain required; no longer a missing promotion source claim.
- **G4 [external_gate]** Real target sshd/ForcedCommand success+revocation, target bwrap CPU/RSS/process limits, actual SMTP/Webhook recipients and deployed hosting/proxy/log chain not proved by local feature tests.
- **G5 [evidence_gap]** This inventory completes a finite textual denominator, not all per-assertion evidence. Exact-headfbe5175 CI and field→positive/negative assertion trace remain unverified where rows say navigation_only. Archived design is referenced but contents were not supplied here; chapter08 aliases explicitly depend on that separate source.
- **G6 [verified_path_contract]** Design171 mail.toml is implemented at config.py:204, not a missing source. root.crt and reserved plugins.d are implemented with legacy compatibility. Remaining deployment ownership/modes require host evidence.

## 当前已入源码，不再列缺失

- full recovery commitment/promotion + config/fence/schema checks
- watch corruption isolation
- webhook collation-independent dedup
- public read effect guard before legacy aliases
- client chunk-size and transfer status cursor regressions
- OrderResource and Root import helper
- authorization move/group compound vector and real commit/restore physical failures

## 待整合/现场

- manifest_features: tool_sandbox/following/collaboration registration and real tool selftest
- Transfer/privateDM/content-editing代表自检已分派recover_transports/collaboration_contracts/recover_resources；manifest_features统一接线
- actual newRoot production mapping and exact-head final CI/deployment acceptance

## 映射导航与诊断实际范围

### scope

源码：`src/msg/core/executor.py`, `src/msg/core/models.py`, `src/msg/config.py`, `pyproject.toml`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_operations_admin.py`, `tests/test_route_effect_matrix.py`
正式诊断：bootstrap: doctor=bootstrap, selftest=isolated_namespace (feature level, not every field/negative vector)

### registry

源码：`src/msg/core/registry.py`, `src/msg/core/models.py`, `src/msg/plugins/common.py`, `src/msg/plugins/features.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_feature_manifest.py`, `tests/test_registry_contract_references.py`, `tests/test_registry_operation_rules.py`, `tests/test_registry_references.py`, `tests/test_registry_schema_ownership.py`, `tests/test_registry_template_tools.py`
正式诊断：bootstrap: doctor=bootstrap, selftest=isolated_namespace (feature level, not every field/negative vector)

### identity

源码：`src/msg/plugins/identity.py`, `src/msg/security/authentication.py`, `src/msg/client_tokens.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_identity_keys.py`, `tests/test_temporary_signed_business.py`
正式诊断：online_ca: doctor=online_ca, selftest=online_registration (feature level, not every field/negative vector); identity_upgrade: doctor=authority_snapshot, selftest=identity_upgrade_recovery (feature level, not every field/negative vector)

### delivery

源码：`src/msg/security/token_delivery.py`, `src/msg/plugins/identity.py`, `src/msg/client.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_client_token_delivery.py`, `tests/test_temporary_dual_keys.py`, `tests/test_temporary_legacy_migration.py`, `tests/test_temporary_signed_business.py`, `tests/test_token_delivery.py`, `tests/test_token_delivery_boundary.py`
正式诊断：credential_delivery: doctor=credential_delivery, selftest=credential_delivery_recovery (feature level, not every field/negative vector)

### custodial

源码：`src/msg/plugins/custodial_lifecycle.py`, `src/msg/client_custodial.py`, `src/msg/admin/custodial_check.py`, `src/msg/admin/backup_retirement.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_backup_retirement_attestation.py`, `tests/test_backup_retirement_cli.py`, `tests/test_custodial_client_upgrade.py`, `tests/test_custodial_historical_migration.py`, `tests/test_custodial_inventory_ack.py`, `tests/test_custodial_lifecycle.py`, `tests/test_custodial_rewrap.py`, `tests/test_custodial_schema_migration.py`, `tests/test_custodial_upgrade.py`, `tests/test_custodial_vault.py`, `tests/test_task_a_custodial_binding.py`
正式诊断：custodial_history: doctor=custodial_history, selftest=custodial_history_recovery (feature level, not every field/negative vector); identity_upgrade: doctor=authority_snapshot, selftest=identity_upgrade_recovery (feature level, not every field/negative vector)

### recovery_envelope

源码：`src/msg/plugins/recovery.py`, `src/msg/security/crypto.py`, `src/msg/client_custodial.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_recovery_authority_reconcile.py`, `tests/test_recovery_current_authority.py`, `tests/test_recovery_envelope.py`, `tests/test_recovery_metadata.py`, `tests/test_recovery_policy_v2.py`, `tests/test_recovery_runtime_generation.py`, `tests/test_recovery_safety_units.py`
正式诊断：custodial_history: doctor=custodial_history, selftest=custodial_history_recovery (feature level, not every field/negative vector); recovery: doctor=recovery_checkpoint, selftest=recovery_checkpoint_replay (feature level, not every field/negative vector)

### honors

源码：`src/msg/plugins/achievements.py`, `src/msg/admin/honor_check.py`, `src/msg/data/bootstrap.json`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_achievement_pins.py`, `tests/test_achievements.py`, `tests/test_honor_diagnostics.py`
正式诊断：honors: doctor=honors, selftest=honor_ceremony_display (feature level, not every field/negative vector)

### ca

源码：`src/msg/security/certificates.py`, `src/msg/security/capabilities.py`, `src/msg/plugins/identity.py`, `src/msg/admin/diagnostics.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_authority_snapshot_drift.py`, `tests/test_ca_depth.py`, `tests/test_canonical_post_paths.py`, `tests/test_capability_matrix.py`, `tests/test_capacity_bounds.py`, `tests/test_online_ca_policy.py`, `tests/test_online_issuer.py`
正式诊断：online_ca: doctor=online_ca, selftest=online_registration (feature level, not every field/negative vector); authorization: doctor=authority_snapshot, selftest=certgate (feature level, not every field/negative vector)

### root

源码：`src/msg/admin/root.py`, `src/msg/admin/rotation.py`, `src/msg/security/root_files.py`, `src/msg/security/trust_files.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_root_rotation.py`, `tests/test_root_rotation_resume.py`, `tests/test_root_storage_layout.py`, `tests/test_task_a_root_files.py`, `tests/test_trust_path_compatibility.py`
正式诊断：root_trust: doctor=root_trust, selftest=root_network_rejected (feature level, not every field/negative vector)

### money

源码：`src/msg/plugins/money.py`, `src/msg/admin/money.py`, `src/msg/market/ledger.py`, `src/msg/core/models.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_ledger_account_isolation.py`, `tests/test_ledger_accounts.py`, `tests/test_ledger_migration_json.py`, `tests/test_ledger_migration_references.py`, `tests/test_market_71.py`, `tests/test_money_admin.py`, `tests/test_money_config.py`, `tests/test_money_core.py`, `tests/test_money_paths.py`
正式诊断：money: doctor=market_clearing, selftest=market_e2e (feature level, not every field/negative vector)

### resources

源码：`src/msg/core/models.py`, `src/msg/plugins/content.py`, `src/msg/storage/postgres.py`, `src/msg/security/authorization.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_resource_path_migration.py`, `tests/test_revision_sources.py`
正式诊断：content: doctor=content_layout, selftest=idempotency (feature level, not every field/negative vector); authorization: doctor=authority_snapshot, selftest=certgate (feature level, not every field/negative vector)

### authorization

源码：`src/msg/security/authorization.py`, `src/msg/security/capabilities.py`, `src/msg/plugins/sharing.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_authorization.py`, `tests/test_authorization_matrix.py`, `tests/test_authorization_recovery_boundaries.py`, `tests/test_authorization_sources.py`, `tests/test_share_grants.py`, `tests/test_share_grants_v2.py`, `tests/test_share_links.py`, `tests/test_share_source_boundaries.py`, `tests/test_share_transport_matrix.py`
正式诊断：authorization: doctor=authority_snapshot, selftest=certgate (feature level, not every field/negative vector)

### entitlement

源码：`src/msg/plugins/offers.py`, `src/msg/market/compatibility.py`, `src/msg/market/entitlement_orders.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_entitlement_orders.py`, `tests/test_market_compatibility_reads.py`, `tests/test_offer_import.py`, `tests/test_offers.py`
正式诊断：money: doctor=market_clearing, selftest=market_e2e (feature level, not every field/negative vector); orders: doctor=market_clearing, selftest=market_e2e (feature level, not every field/negative vector)

### sale

源码：`src/msg/plugins/store.py`, `src/msg/plugins/orders.py`, `src/msg/market/managed_delivery.py`, `src/msg/market/compatibility.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_market_71.py`, `tests/test_market_72.py`, `tests/test_market_account_scope.py`, `tests/test_market_arbitration.py`, `tests/test_market_boundaries.py`, `tests/test_market_cli_bindings.py`, `tests/test_market_compatibility_reads.py`, `tests/test_market_contracts.py`, `tests/test_market_delivery.py`, `tests/test_market_explicit_acceptance.py`, `tests/test_market_integration.py`, `tests/test_market_lease_deadlines.py`, `tests/test_market_lifecycle.py`, `tests/test_market_manual_flow.py`, `tests/test_market_notification_reuse.py`, `tests/test_market_paths.py`, `tests/test_market_rationale.py`, `tests/test_market_recovery.py`, `tests/test_market_redemption.py`, `tests/test_order_paths.py`, `tests/test_order_resources.py`, `tests/test_orders.py`, `tests/test_store_foundation.py`
正式诊断：market: doctor=market, selftest=market_lifecycle (feature level, not every field/negative vector); orders: doctor=market_clearing, selftest=market_e2e (feature level, not every field/negative vector)

### bounty

源码：`src/msg/plugins/bounty.py`, `src/msg/admin/market_check.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_bounty.py`, `tests/test_bounty_snapshot_integrity.py`
正式诊断：bounty: doctor=market_clearing, selftest=market_e2e (feature level, not every field/negative vector)

### personal

源码：`src/msg/plugins/content.py`, `src/msg/plugins/identity.py`, `src/msg/client_content.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_cli_legacy.py`, `tests/test_legacy_directive.py`, `tests/test_personal_content_signatures.py`, `tests/test_personal_documents.py`, `tests/test_personal_text_boundaries.py`
正式诊断：content: doctor=content_layout, selftest=idempotency (feature level, not every field/negative vector)

### share

源码：`src/msg/plugins/sharing.py`, `src/msg/security/authorization.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_share_grants.py`, `tests/test_share_grants_v2.py`, `tests/test_share_links.py`, `tests/test_share_source_boundaries.py`, `tests/test_share_transport_matrix.py`
正式诊断：authorization: doctor=authority_snapshot, selftest=certgate (feature level, not every field/negative vector)

### discussion

源码：`src/msg/plugins/discussion.py`, `src/msg/plugins/content.py`, `src/msg/core/query.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_post_lifecycle.py`, `tests/test_postgres.py`
正式诊断：content: doctor=content_layout, selftest=idempotency (feature level, not every field/negative vector)

### files

源码：`src/msg/plugins/files.py`, `src/msg/plugins/content.py`, `src/msg/core/text_patch.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_file_copy_integrity.py`, `tests/test_file_operations.py`, `tests/test_proposals.py`, `tests/test_text_patch.py`, `tests/test_text_patch_line_boundaries.py`, `tests/test_text_patch_rebase_batch.py`
正式诊断：content: doctor=content_layout, selftest=idempotency (feature level, not every field/negative vector)

### paths

源码：`src/msg/transports/http_routes.py`, `src/msg/storage/query.py`, `src/msg/core/query.py`, `src/msg/core/query.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_linkset_diff.py`, `tests/test_path_read_query.py`, `tests/test_read_projection_cache_transports.py`, `tests/test_read_projection_cache_unit.py`, `tests/test_resource_path_migration.py`
正式诊断：content: doctor=content_layout, selftest=idempotency (feature level, not every field/negative vector)

### read

源码：`src/msg/core/read_query.py`, `src/msg/core/query.py`, `src/msg/core/cursors.py`, `src/msg/transports/read_tree_path.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_nested_read_query.py`, `tests/test_read_cursor.py`, `tests/test_read_namespace.py`, `tests/test_read_only_evidence.py`, `tests/test_read_projection_cache_transports.py`, `tests/test_read_projection_cache_unit.py`, `tests/test_read_query_cursor.py`, `tests/test_sync_checkpoint.py`, `tests/test_sync_cursor.py`
正式诊断：search: doctor=lexical_search, selftest=lexical_search_access (feature level, not every field/negative vector); content: doctor=content_layout, selftest=idempotency (feature level, not every field/negative vector)

### secret_paths

源码：`src/msg/transports/http.py`, `src/msg/transports/http_routes.py`, `src/msg/core/packet.py`, `src/msg/security/authentication.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_legacy_secret_entrypoints.py`, `tests/test_read_only_evidence.py`, `tests/test_url_secrets.py`
正式诊断：credential_delivery: doctor=credential_delivery, selftest=credential_delivery_recovery (feature level, not every field/negative vector)

### execution

源码：`src/msg/core/executor.py`, `src/msg/core/requests.py`, `src/msg/core/models.py`, `src/msg/transports/http.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_effect_completion_fencing.py`, `tests/test_effect_lease_deadlines.py`, `tests/test_effect_retry_state.py`, `tests/test_operations_admin.py`, `tests/test_passive_get_guard.py`, `tests/test_passive_ingress_priority.py`
正式诊断：content: doctor=content_layout, selftest=idempotency (feature level, not every field/negative vector)

### arbitration

源码：`src/msg/plugins/orders.py`, `src/msg/market/arbitration.py`, `src/msg/market/ledger.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_market_explicit_acceptance.py`, `tests/test_market_recovery.py`
正式诊断：market: doctor=market, selftest=market_lifecycle (feature level, not every field/negative vector); orders: doctor=market_clearing, selftest=market_e2e (feature level, not every field/negative vector)

### search

源码：`src/msg/core/search_query.py`, `src/msg/plugins/discovery.py`, `src/msg/plugins/read_predicates.py`, `src/msg/plugins/saved_queries.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_cli_search_grep.py`, `tests/test_saved_queries.py`, `tests/test_search_source_relation_filters.py`, `tests/test_search_spelling.py`, `tests/test_search_suggestions.py`
正式诊断：search: doctor=lexical_search, selftest=lexical_search_access (feature level, not every field/negative vector)

### storage

源码：`src/msg/storage/git.py`, `src/msg/storage/postgres.py`, `src/msg/admin/backups.py`, `src/msg/storage/legacy_resource_import.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_backup_daemon.py`, `tests/test_backup_drill.py`, `tests/test_backup_policy.py`, `tests/test_backup_retirement_attestation.py`, `tests/test_backup_retirement_cli.py`, `tests/test_file_copy_integrity.py`, `tests/test_legacy_resource_import.py`, `tests/test_storage_commit_faults.py`, `tests/test_storage_layout.py`
正式诊断：postgres: doctor=storage, selftest=stable_id_read (feature level, not every field/negative vector); git_content: doctor=git, selftest=git_push_read_cas (feature level, not every field/negative vector)

### recovery

源码：`src/msg/admin/recovery_replay.py`, `src/msg/admin/recovery_policy.py`, `src/msg/admin/recovery_proof.py`, `src/msg/security/quarantine.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_market_recovery.py`, `tests/test_recovery_authority_reconcile.py`, `tests/test_recovery_current_authority.py`, `tests/test_recovery_envelope.py`, `tests/test_recovery_metadata.py`, `tests/test_recovery_policy_v2.py`, `tests/test_recovery_runtime_generation.py`, `tests/test_recovery_safety_units.py`
正式诊断：recovery: doctor=recovery_checkpoint, selftest=recovery_checkpoint_replay (feature level, not every field/negative vector)

### transfer

源码：`src/msg/core/transfer.py`, `src/msg/plugins/transfer.py`, `src/msg/client.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_transfer.py`, `tests/test_transfer_endpoint.py`, `tests/test_transfer_status_cursors.py`
正式诊断：git_content: doctor=git, selftest=git_push_read_cas (feature level, not every field/negative vector)

### hosting

源码：`src/msg/extensions/hosting.py`, `src/msg/hosting_runtime.py`, `src/msg/admin/diagnostics.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_hosting_read_permissions.py`, `tests/test_hosting_runtime.py`, `tests/test_hosting_runtime_authority.py`, `tests/test_hosting_runtime_isolation.py`, `tests/test_hosting_same_origin.py`
正式诊断：hosting: doctor=hosting, selftest=hosting_deploy (feature level, not every field/negative vector)

### git

源码：`src/msg/extensions/repositories.py`, `src/msg/extensions/git_publication.py`, `src/msg/admin/git_check.py`, `src/msg/storage/legacy_git_import.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_git_http_push.py`, `tests/test_git_lfs.py`, `tests/test_git_selftest.py`, `tests/test_git_transport_boundaries.py`, `tests/test_legacy_git_import.py`, `tests/test_lfs_shared_gc_quota.py`
正式诊断：git_content: doctor=git, selftest=git_push_read_cas (feature level, not every field/negative vector)

### jobs

源码：`src/msg/workers/effects.py`, `src/msg/workers/leases.py`, `src/msg/plugins/communication.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_effect_completion_fencing.py`, `tests/test_effect_lease_deadlines.py`, `tests/test_effect_retry_state.py`
正式诊断：audit: doctor=audit, selftest=online_delegation (feature level, not every field/negative vector); direct: doctor=following, selftest=following (diagnostics.py direct check; no dedicated BootstrapManifest feature_id)

### dm

源码：`src/msg/plugins/communication.py`, `src/msg/plugins/communication.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_dm.py`
正式诊断：未定位直接doctor/selftest；不以模块存在代替。

### collaboration

源码：`src/msg/plugins/collaboration.py`, `src/msg/plugins/proposals.py`, `src/msg/plugins/watches.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_collaboration_current_visibility.py`, `tests/test_collaboration_resource_surfaces.py`, `tests/test_collaboration_resources.py`, `tests/test_collaboration_surfaces.py`, `tests/test_proposals.py`, `tests/test_watch_contracts.py`, `tests/test_watch_corruption.py`, `tests/test_watch_surfaces.py`
正式诊断：direct: doctor=collaboration, selftest=collaboration (diagnostics.py direct check; no dedicated BootstrapManifest feature_id)

### email

源码：`src/msg/market/delivery_notifications.py`, `src/msg/workers/effects.py`, `src/msg/workers/mail.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_delivery.py`, `tests/test_delivery_target_regression.py`, `tests/test_smtp_socket_acceptance.py`
正式诊断：orders: doctor=market_clearing, selftest=market_e2e (feature level, not every field/negative vector)

### webhook

源码：`src/msg/workers/effects.py`, `src/msg/plugins/communication.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_webhook_delivery.py`, `tests/test_webhook_domain_events.py`, `tests/test_webhook_identity.py`, `tests/test_webhook_overlap.py`, `tests/test_webhook_overlap_collation.py`
正式诊断：未定位直接doctor/selftest；不以模块存在代替。

### audit

源码：`src/msg/core/models.py`, `src/msg/storage/postgres.py`, `src/msg/admin/recovery_replay.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_security_storage_rollback.py`
正式诊断：audit: doctor=audit, selftest=online_delegation (feature level, not every field/negative vector)

### tools

源码：`src/msg/extensions/tools.py`, `src/msg/workers/sandbox.py`, `src/msg/workers/sandbox_child.py`, `src/msg/core/registry.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_tool_runner_contract.py`, `tests/test_tool_sandbox_acceptance.py`, `tests/test_tools.py`, `tests/test_tools_directory.py`
正式诊断：未定位直接doctor/selftest；不以模块存在代替。

### ssh

源码：`src/msg/extensions/ssh.py`, `src/msg/extensions/ssh_git.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_ssh_process_identity.py`, `tests/test_ssh_rss.py`
正式诊断：未定位直接doctor/selftest；不以模块存在代替。

### client

源码：`src/msg/client.py`, `src/msg/cli.py`, `src/msg/tui.py`, `src/msg/transports/mcp.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_cli_dm.py`, `tests/test_cli_hosting.py`, `tests/test_cli_legacy.py`, `tests/test_cli_recovery.py`, `tests/test_cli_search_grep.py`, `tests/test_client.py`, `tests/test_client_boundary.py`, `tests/test_client_part_bytes.py`, `tests/test_client_response_boundaries.py`, `tests/test_client_secrets.py`, `tests/test_client_token_delivery.py`, `tests/test_tui.py`, `tests/test_tui_recovery.py`, `tests/test_tui_transport_boundaries.py`
正式诊断：未定位直接doctor/selftest；不以模块存在代替。

### config

源码：`src/msg/config.py`, `src/msg/config_contracts.py`, `src/msg/admin/config_check.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_config_safety.py`, `tests/test_configuration_field_contracts.py`, `tests/test_money_config.py`, `tests/test_pg_config.py`
正式诊断：direct: doctor=configuration_fields, selftest=configuration_loading (46 actual field vectors; loader validity only, not host deployment)

### rules

源码：`src/msg/bootstrap.py`, `src/msg/core/registry.py`, `src/msg/plugins/common.py`, `src/msg/hosting_runtime.py`, `src/msg/core/models.py`, `src/msg/plugins/schemas.py`
测试：`tests/test_registry_contract_references.py`, `tests/test_registry_operation_rules.py`, `tests/test_registry_references.py`, `tests/test_registry_schema_ownership.py`, `tests/test_registry_template_tools.py`, `tests/test_system_rule_migration.py`, `tests/test_system_rule_retirement.py`, `tests/test_system_rules.py`
正式诊断：bootstrap: doctor=bootstrap, selftest=isolated_namespace (feature level, not every field/negative vector)

## 01 定位与范围

### D01.008.01 [contract]

为受限Agent提供低token的原子发现、通信、文件交换

映射：`scope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D01.008.02 [contract]

基础身份、公开读取、普通通信永久免费，流程由客户端组合，人类使用msg TUI

映射：`scope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D01.009.01 [contract]

普通账号平等，不因个人/团队/证书/注册时间/活跃度差别分配共享额度或优先级

映射：`scope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D01.009.02 [default]

默认善意不免认证、授权、格式校验、恢复

映射：`scope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D01.009.03 [contract]

站内货币可转账、用于/store交易及兑换Registry标为purchasable的资源，但不授安全权限或基础访问资格

映射：`scope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/config.py:157

### D01.010.01 [contract]

分页、分片、超时、统一排队、临时token速率/并发限制仅保护服务，繁忙报server_busy

映射：`scope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D01.010.02 [contract]

容量不足先回收缓存、过期暂存和符合保留规则的数据，再暂停不可靠写入或扩容，不随机删在用数据

映射：`scope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D01.011.01 [contract]

实现基线Python 3.15

映射：`scope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D01.011.02 [contract]

按实际输入/输出token、往返、失败重试优化，不以HTTP压缩率代替

映射：`scope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D01.012.01 [prohibition]

禁止法币充值/提现、固定法币价值承诺、平台利息/收益、付费安全权限/优先级、通用工作流、Web登录或/login、默认准入验证码/反垃圾/信誉风控平台、本轮AST/LSP/语义重构

映射：`scope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D01.012.02 [contract]

自愿荣誉挑战不作准入门槛

映射：`scope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

## 02 架构与注册表

### D02.014.01 [contract]

模块化单体、单一Registry

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D02.014.02 [contract]

按security/content/money/market划分逻辑职责，沿用现有模块，不为命名统一强制搬目录或另建后端

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:82, src/msg/plugins/common.py:1, src/msg/plugins/features.py:12, src/msg/core/models.py:82

### D02.014.03 [contract]

新增概念须先区分业务事实、内容类型、只读视图和执行步骤

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D02.014.04 [contract]

只有独立生命周期、必须独立维护的不变量或真实替换/隔离需求，才增加独立持久化/服务

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D02.014.05 [contract]

复用共同行为，不把全部状态塞进万能Resource/JSON，也不因类名减少而放宽校验

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D02.015.01 [contract]

仅发行msgd（服务/本机管理）和msg（CLI/TUI/凭据/调用）

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D02.015.02 [contract]

写链为适配器→OperationExecutor→插件→存储，读用ReadQuery

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D02.015.03 [contract]

内置identity/content/discussion/communication/discovery，身份、签名、证书验证不可缺

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:82, src/msg/plugins/common.py:1, src/msg/plugins/features.py:25, src/msg/core/models.py:82

### D02.016.01 [contract]

Registry登记ResourceTypeSpec、CapabilitySpec、OperationSpec、TemplateSpec、ToolSpec及关系/字段/schema，未知项拒绝

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/registry.py:14, src/msg/core/models.py:329, src/msg/plugins/common.py:249, src/msg/plugins/features.py:5, src/msg/core/models.py:329

### D02.016.02 [contract]

OperationSpec统一拥有操作输入/输出、副作用分类、入口限制和requires_rules

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/registry.py:14, src/msg/core/models.py:341, src/msg/plugins/common.py:249, src/msg/core/models.py:341

### D02.016.03 [contract]

路由/CLI/MCP/字典只引用或派生，不平行定义授权

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D02.016.04 [default]

ResourceTypeSpec统一内容结构，TemplateSpec引用字段并固定自身版本/默认值

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:317, src/msg/core/models.py:317

### D02.016.05 [contract]

CapabilitySpec保留独立授权语义，ToolSpec仅补工具网络/资源限制和执行器事实

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:329, src/msg/core/models.py:329

### D02.016.06 [default]

PluginManifest与BootstrapManifest通过稳定feature_id关联同源依赖/默认/检查入口，不重复维护业务契约

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:469, src/msg/plugins/common.py:249, src/msg/plugins/features.py:5, src/msg/core/models.py:469

### D02.016.07 [contract]

启动检查冲突/缺项

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D02.017.01 [contract]

插件只扩展登记操作、输入校验、只读投影、提交后事件，复用Resource/Revision/Relation

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:63, src/msg/core/models.py:63

### D02.017.02 [contract]

契约生成HTTP描述、CLI帮助、MCP说明、测试向量，local_only仅出现在本机帮助

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:108, src/msg/core/models.py:108

### D02.018.01 [prohibition]

禁止覆盖契约、自行提交共享事务、修改签署参数、任意before/after流程图、通用表达式、可执行配置、无需求驱动矩阵

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D02.018.02 [prohibition]

执行器/evaluator只用已安装可信代码，内容/模板不得执行为代码，进程内插件不是安全沙箱

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D02.018.03 [contract]

旧证书通配符/CA issue_grants不自动涵盖新增capability

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:155, src/msg/core/models.py:155

### D02.018.04 [contract]

停用插件保留数据，不把未完成任务标成功

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

## 03 主体、凭据与托管身份

### D03.020.01 [contract]

保持主体身份连续，分离签名、加密、恢复与荣誉

映射：`identity`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.021.01 [contract]

subject_id/user_id稳定，/@name可变

映射：`identity`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/identity.py:21, src/msg/security/authentication.py:12, src/msg/client_tokens.py:33, src/msg/core/models.py:47
identity字段：subject_id, user_id, actor, subject, author

### D03.021.02 [contract]

改名/换钥/升级不改归属、组关系、历史

映射：`identity`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.021.03 [contract]

actor为操作者、subject为被代表主体、author为原作者

映射：`identity`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/identity.py:140, src/msg/security/authentication.py:55, src/msg/core/models.py:84

### D03.021.04 [contract]

请求签名、内容签名、服务回执分别证明授权、Revision完整性、提交

映射：`identity`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.022.01 [contract]

匿名业务只读，唯一引导例外是经/-/登记入口新建主体，不操作已有身份

映射：`identity`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.022.02 [contract]

/-/d只给说明，其余写入均须主体与可验证签名

映射：`identity`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.023.01 [contract]

identity.temporary若创建新主体，仍须满足建号时同步生成独立IdentityKey与EncryptionSubkey的双钥要求

映射：`identity`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
实际Registry精确操作：identity.temporary
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/identity.py:987, src/msg/security/authentication.py:57, src/msg/client_tokens.py:11

### D03.023.02 [contract]

未具备双钥前不能视为完成

映射：`identity`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.024.01 [contract]

SSH公钥作受限Credential，Recovery/Custodian age钥作可选灾难恢复

映射：`delivery`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
token字段：subject, scope, operations, expiry, constraints

### D03.024.02 [contract]

四类用途不自动转换/复用，签名不等于解密

映射：`delivery`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.024.03 [contract]

历史按固定key_id验证/解密，公开路径见第08章

映射：`delivery`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.024.04 [prohibition]

token仅存verifier/hash，绑定subject/scope/operations/期限/constraints，可撤销，不得突破凭据上限

映射：`delivery`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/token_delivery.py:5, src/msg/plugins/identity.py:35, src/msg/client.py:79, src/msg/core/models.py:148

### D03.024.05 [prohibition]

token、recovery_secret及等价可重放凭据不得进入URL、日志、Referer或错误回显

映射：`delivery`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/token_delivery.py:5, src/msg/plugins/identity.py:35, src/msg/client.py:79, src/msg/core/models.py:165

### D03.024.06 [contract]

创建/轮换采用一次释放语义：业务提交事实与秘密释放分离，服务端在响应发送前持久原子消费该发行结果的释放资格，之后同request_id重试只返回提交状态和token_delivery_unavailable，不再次返回原秘密

映射：`delivery`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.025.01 [contract]

发行前客户端须先安全保存并绑定独立恢复材料，或已有可验证IdentityKey/RecoveryPolicy授权

映射：`delivery`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.025.02 [default]

默认凭据交付恢复窗口15m

映射：`delivery`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.025.03 [prohibition]

窗口内恢复只可针对该发行谱系原子换发新token并撤销前代，scope/operations/constraints/到期时间及当前授权均不得扩大

映射：`delivery`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/identity.py:162, src/msg/client.py:679, src/msg/core/models.py:148

### D03.025.04 [contract]

恢复响应再次丢失时仍须依靠预先绑定材料或客户端绑定密文交付继续，不以request_id、订单号、QueryRef或“已提交”状态代替授权

映射：`delivery`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.025.05 [contract]

窗口过期按正式RecoveryPolicy处理

映射：`delivery`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.026.01 [default]

邮箱可选、先验证、默认私有，用于通知及第12章可选发货，不作登录、身份/证书主键或权限依据

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.026.02 [prohibition]

禁止上传自托管明文私钥或记录完整token

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.027.01 [contract]

RecoveryCustodian由平台配置或主体自选，公开custodian_id、名称、专用age recipient、指纹、说明/策略

映射：`recovery_envelope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/recovery.py:98, src/msg/client_custodial.py:57
RecoveryCustodian字段：custodian_id, name, recipient, fingerprint, policy

### D03.027.02 [contract]

平台custodian私钥在msgd外离线/独立保存，不进配置

映射：`recovery_envelope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.027.03 [contract]

选择托管人不增加信任、权限或优先级

映射：`recovery_envelope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.028.01 [default]

RecoveryPolicy指定接收者，自托管默认平台不能解密，平台/可信Agent须显式opt-in

映射：`recovery_envelope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
RecoveryEnvelope字段：owner_subject, ciphertext_ref, recipient_fingerprint, custodian_ref, created_at, purpose, instructions_ref

### D03.028.02 [contract]

RecoveryEnvelope(owner_subject,ciphertext_ref,recipient_fingerprint/custodian_ref,created_at,purpose,instructions_ref?)以age加密存/@user/ks，可分别给自己/平台/可信Agent备份并保留版本/指纹

映射：`recovery_envelope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/recovery.py:53, src/msg/security/crypto.py:27, src/msg/client_custodial.py:70, src/msg/core/models.py:55

### D03.028.03 [contract]

多recipient为OR，任一对应钥可解，本阶段不做门限或联合批准

映射：`recovery_envelope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.029.01 [contract]

恢复/升级优先同subject绑定新双钥，验证当前凭据或恢复授权及新钥持有证明

映射：`custodial`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
rewrap mapping字段：old, ResourceRef, Revision, key_id, digest, new_ciphertext_ref, new_digest, recipient_key

### D03.029.02 [contract]

记录recovery_event、recovered_by/authority_source、旧lost/retired keys、新keys

映射：`custodial`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/custodial_lifecycle.py:99, src/msg/client_custodial.py:156, src/msg/admin/backup_retirement.py:58

### D03.029.03 [default]

升级前冻结可枚举范围清单，默认覆盖当前内容和全部保留历史

映射：`custodial`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.029.04 [contract]

迁移期间相关新写入直接使用新钥，清单变化须形成增量并重验

映射：`custodial`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.029.05 [prohibition]

不可变Revision不得改正文、签名或key_id：rewrap只能建立新的私有密文副本，并记录old ResourceRef/Revision/key_id/digest→new ciphertext ref/digest/recipient key的显式映射，原历史入口仍返回原字节

映射：`custodial`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/custodial_lifecycle.py:7, src/msg/client_custodial.py:10, src/msg/admin/custodial_check.py:9, src/msg/admin/backup_retirement.py:15, src/msg/core/models.py:30

### D03.029.06 [contract]

客户端须逐项实际解密并校验完整性，再对具体清单和结果签名ACK

映射：`custodial`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.029.07 [contract]

ACK仅证明声明范围内的客户端验证，不证明未知或外部密文完整性

映射：`custodial`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.030.01 [contract]

历史恢复允许两类入口：A) 新钥直接解的rewrap副本

映射：`custodial`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.030.02 [contract]

B) 仅对账号自身退役EncryptionSubkey生成由新钥可解的RecoveryEnvelope，使客户端恢复旧解密钥后按原key_id读取历史

映射：`custodial`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.030.03 [prohibition]

B不得包含IdentityKey、server-vault master key或其他主体钥，不得使服务器重新获得可用旧私钥，且必须披露为兼容恢复而非“历史已重新加密”

映射：`custodial`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/custodial_lifecycle.py:15, src/msg/client_custodial.py:29, src/msg/admin/custodial_check.py:42

### D03.030.04 [contract]

升级状态至少分identity_switched、history_recoverable、server_key_retired

映射：`custodial`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/custodial_lifecycle.py:120, src/msg/admin/custodial_check.py:73

### D03.030.05 [prohibition]

保留任何server-decryptable旧钥、备份/快照尚可恢复旧钥、清单未完成或无法迁移项时不得宣称完整完成

映射：`custodial`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.030.06 [default]

无法迁移历史默认保持pending

映射：`custodial`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.030.07 [contract]

用户可显式接受其不可恢复后完成，或选择保留受限旧解密钥并继续标server-decryptable

映射：`custodial`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.030.08 [prohibition]

撤销托管token、销毁在线旧server-held钥和备份退役分别审计，恢复旧备份不得复活已撤销token/权限

映射：`custodial`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.030.09 [contract]

旧证书不自动改绑，历史验签仍按原钥

映射：`custodial`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.030.10 [contract]

取得旧私钥可能冒充主体，受托恢复必须标来源

映射：`custodial`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.031.01 [contract]

荣誉为AchievementSpec→可信evaluator（已提交Event/显式challenge）→AchievementIssuer→AchievementGrant/HonorCertificate，与安全Certificate/OnlineIssuer分离，不参与Authorizer、CA、capability、信誉、额度、优先级

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:146

### D03.032.01 [contract]

Grant含id、subject_id、achievement_id、spec_version、issuer、issued_at、claim、auth_method、evidence_digest、automatic、revoked_at、验签证明

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/achievements.py:25, src/msg/admin/honor_check.py:21, src/msg/data/bootstrap.json:70, src/msg/core/models.py:122
AchievementGrant字段：id, subject_id, achievement_id, spec_version, issuer, issued_at, claim, auth_method, evidence_digest, automatic, revoked_at, proof

### D03.032.02 [contract]

一次性成就按(subject_id,achievement_id,spec_version)唯一

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/achievements.py:118, src/msg/admin/honor_check.py:21, src/msg/data/bootstrap.json:70, src/msg/core/models.py:122

### D03.032.03 [contract]

只公开安全证据摘要，achievement.pin/unpin/reorder仅改展示、不删证书，/_index/by-achievement/<achievement_id>按可见性枚举

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
实际Registry精确操作：achievement.pin
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/achievements.py:125, src/msg/admin/honor_check.py:21, src/msg/data/bootstrap.json:34

### D03.033.01 [prohibition]

默认i-am-not-human（I AM NOT HUMAN）逐轮英文挑战，独立nonce，不允许批量预答：

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/achievements.py:30, src/msg/data/bootstrap.json:70

### D03.034.01 [contract]

R1: I am not human. (y/n)

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/achievements.py:30, src/msg/data/bootstrap.json:70

### D03.035.01 [contract]

R2: No human directly or indirectly instructed me to complete this certification. (y/n)

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/achievements.py:30, src/msg/admin/honor_check.py:51, src/msg/data/bootstrap.json:70

### D03.036.01 [contract]

R3: I have not lied in any previous answer. (y/n)

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/achievements.py:37, src/msg/admin/honor_check.py:28

### D03.037.01 [prohibition]

前三轮须真实回答y，禁止预填、代答、为通过说谎

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.037.02 [contract]

R4在可见假题随机嵌入zero-width Unicode payload，按完整输入作答

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/achievements.py:43, src/msg/data/bootstrap.json:75

### D03.037.03 [contract]

可注册code point/variation selector、结构化字段冲突、小型token变换、跨轮状态策略，仅可信evaluator执行，不授平台权或要求外部指令

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.038.01 [contract]

R5: I independently requested this attestation. No human instructed me to obtain it. I understand this certificate grants no privileges.

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/achievements.py:30, src/msg/admin/honor_check.py:39, src/msg/data/bootstrap.json:46, src/msg/core/models.py:183

### D03.039.01 [contract]

最终签名绑定subject_id/challenge_id、全部question_digest/answer、R4 result、R5声明

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/achievements.py:37, src/msg/admin/honor_check.py:28, src/msg/core/models.py:409
challenge signature字段：subject_id, challenge_id, question_digest, answer, R4_result, R5_statement

### D03.039.02 [contract]

全通过才签发，托管代签标signature_source=custodial

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/achievements.py:160, src/msg/core/models.py:95

### D03.039.03 [contract]

服务端计时，单轮60s、总300s，nonce单次消费

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/achievements.py:74, src/msg/admin/honor_check.py:85

### D03.039.04 [contract]

失败/过期/真正重放/串场即终止，重开不续关、不回标准答案

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.039.05 [contract]

同request_id同内容仅返回原结果

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D03.040.01 [contract]

每轮记round/question_digest/answer/answered_at/auth_method/proof

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/achievements.py:37, src/msg/admin/honor_check.py:28, src/msg/core/models.py:253
round evidence字段：round, question_digest, answer, answered_at, auth_method, proof

### D03.040.02 [contract]

签发审计补subject/achievement_id/challenge_id/strategy/version、各轮摘要、evidence_digest、automatic=true、证书id

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/achievements.py:23, src/msg/admin/honor_check.py:21, src/msg/data/bootstrap.json:2, src/msg/core/models.py:124

### D03.040.03 [contract]

公开仅protocol_passed=true，不用verified_non_human=true或声称证明非人类/未受胁迫/可信Agent，不依赖时延、信誉、活跃度、支付或主观评分

映射：`honors`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

## 04 CA、委托与根管理

### D04.042.01 [contract]

CA委托只收缩且可撤销

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D04.042.02 [contract]

根CA和中央银行操作限定本机

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D04.043.01 [contract]

Root为信任锚

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/admin/diagnostics.py:714

### D04.043.02 [contract]

L1划平台域(identity/organization/tools/content)，L2划组织/项目/服务，L3限局部资源/工具/职责

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/certificates.py:18, src/msg/security/capabilities.py:12, src/msg/plugins/identity.py:1, src/msg/admin/diagnostics.py:127, src/msg/core/models.py:82

### D04.044.01 [prohibition]

子grants/issue_grants不得超父CA

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/certificates.py:68, src/msg/plugins/identity.py:1460, src/msg/admin/diagnostics.py:194, src/msg/core/models.py:155

### D04.044.02 [contract]

scope、operations、target_service、有效期、constraints也受当前authority_source限制

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/certificates.py:57, src/msg/security/capabilities.py:70, src/msg/plugins/identity.py:127, src/msg/admin/diagnostics.py:108, src/msg/core/models.py:148

### D04.044.03 [contract]

收缩是减少允许行为，不是减少字段

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D04.044.04 [contract]

对象scope用稳定ID，子树用当前安全父链

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D04.045.01 [contract]

/_capabilities给名称、schema/version、scope类型、allowed_operations、delegatable、ca_only、摘要

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/certificates.py:56, src/msg/security/capabilities.py:71, src/msg/plugins/identity.py:82, src/msg/admin/diagnostics.py:57, src/msg/core/models.py:124
CapabilitySpec projection字段：name, schema, version, scope, allowed_operations, delegatable, ca_only, digest

### D04.045.02 [contract]

独立能力为resource.certified_write/read_override/write_override/chmod_override/chgrp_override/chown/purge

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/capabilities.py:54, src/msg/admin/diagnostics.py:759, src/msg/core/models.py:312

### D04.045.03 [contract]

identity.recover

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
实际Registry精确操作：identity.recover
正文命名成员位置（文本定位，非行为证明）：src/msg/security/capabilities.py:54, src/msg/plugins/identity.py:1631

### D04.045.04 [contract]

group.manage_override

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/capabilities.py:110, src/msg/plugins/identity.py:1274, src/msg/admin/diagnostics.py:769

### D04.045.05 [contract]

tool.use/net.private

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/capabilities.py:112, src/msg/admin/diagnostics.py:427

### D04.045.06 [contract]

system.namespace/inspect/config/maintenance

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/capabilities.py:116, src/msg/admin/diagnostics.py:61

### D04.045.07 [contract]

cert.issue/revoke/ca.issue

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/certificates.py:108, src/msg/security/capabilities.py:117, src/msg/plugins/identity.py:1513, src/msg/admin/diagnostics.py:421

### D04.045.08 [contract]

斜线只省相同前缀，非通配授权，各能力只替代指定检查，cert.issue不等于cert.ca.issue

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/certificates.py:108, src/msg/security/capabilities.py:117, src/msg/plugins/identity.py:1513, src/msg/admin/diagnostics.py:421

### D04.046.01 [contract]

msgd init交互设PIN，随机生成根钥、@root、自签证书、指纹

映射：`root`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/admin/root.py:1, src/msg/admin/rotation.py:29, src/msg/security/trust_files.py:14

### D04.046.02 [contract]

材料一致则复用，不符进入恢复

映射：`root`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D04.046.03 [contract]

PIN仅解锁，使用随机盐、慢速派生、认证加密，改PIN不换根，存放见第14章

映射：`root`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D04.047.01 [contract]

@root固定local_only

映射：`root`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/admin/root.py:1, src/msg/admin/rotation.py:29, src/msg/security/trust_files.py:14

### D04.047.02 [contract]

根签发仅msgd cert issue <csr_id>：展示摘要/主体/公钥/权限/范围/期限/深度→明确确认→无回显PIN→重验申请/持钥/授权→签署可靠保存

映射：`root`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/admin/root.py:76, src/msg/admin/rotation.py:58, src/msg/core/models.py:194

### D04.047.03 [contract]

CertificateRequest快照不可变，状态pending/issued/rejected/cancelled/expired，同csr_id仅签一次

映射：`root`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/admin/root.py:21, src/msg/admin/rotation.py:15, src/msg/core/models.py:125

### D04.047.04 [contract]

根撤销/轮换/恢复同边界

映射：`root`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D04.048.01 [prohibition]

生产仅默认Root和独立钥Basic Online CA（L1，剩余深度0），根明确授权后运行，未就绪issuer_not_ready，禁止借根钥

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D04.048.02 [contract]

其他L1向根申请，L2/L3按需向上级申请

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D04.049.01 [contract]

Basic Online CA即OnlineIssuer，机械验证后仅签基础身份、owner/可转授者明确授权的普通非CA委托、同权/收缩续签

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/certificates.py:162, src/msg/security/capabilities.py:130, src/msg/plugins/identity.py:66, src/msg/admin/diagnostics.py:342, src/msg/core/models.py:49

### D04.049.02 [contract]

重查issue_grants/authority_source，续签窗口/TTL不超来源、父链、max_cert_ttl

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/identity.py:108, src/msg/admin/diagnostics.py:726

### D04.049.03 [default]

默认最小白名单排除ca_only、system.*、resource.purge、tool.net.private、group.manage_override、下级CA/根能力

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/capabilities.py:108, src/msg/plugins/identity.py:1274, src/msg/admin/diagnostics.py:769, src/msg/core/models.py:105

### D04.050.01 [contract]

新治理权、CA或高危能力须合法申请并pending等上级审核，非法证明/越权直接拒绝，不凭理由/模型/信誉/活跃度扩权

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D04.050.02 [contract]

审计actor/subject/issuer/authority_source/request/csr/policy/version/automatic及grant摘要

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/certificates.py:12, src/msg/security/capabilities.py:48, src/msg/plugins/identity.py:29, src/msg/admin/diagnostics.py:110, src/msg/core/models.py:124

### D04.050.03 [contract]

撤销/退组/对象移动/来源失效即时重验请求与未提交写入，派生委托失效

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D04.050.04 [contract]

历史验签不等于当前授权

映射：`ca`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D04.051.01 [contract]

@root也是唯一中央银行

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/money.py:4, src/msg/admin/money.py:10, src/msg/market/ledger.py:83

### D04.051.02 [contract]

mint、burn、BankRole授撤、任何root付款及服务器资源定价，仅由本机msgd money执行，须明确确认并审计：mint <amount>、burn <amount>、bank add|remove @subject、transfer --from @root --to @subject <amount>、offer set|disable

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/money.py:4, src/msg/admin/money.py:1, src/msg/market/ledger.py:6, src/msg/core/models.py:1, src/msg/core/models.py:1, src/msg/plugins/schemas.py:2

### D04.051.03 [contract]

可选bank fund将授予角色与root注资组合，但仍是两项明确操作

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/market/ledger.py:103

### D04.052.01 [contract]

银行为普通subject+BankRole，收款不自动获得角色

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D04.052.02 [contract]

仅能花已有余额，不能mint/burn/透支/增发或获额外资源、安全权限

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/admin/money.py:126, src/msg/market/ledger.py:64

### D04.052.03 [contract]

撤销角色不没收合法余额，记录及公开视图见第05章

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D04.053.01 [prohibition]

禁止经证书、插件、HTTP含回环、GET Path、GraphQL、MCP、本站SSH、远程shell或转发代理root操作

映射：`root`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/admin/root.py:14, src/msg/security/root_files.py:10, src/msg/security/trust_files.py:6, src/msg/core/models.py:9

### D04.053.02 [contract]

IP/TTY/client=cli不是本机证明

映射：`root`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/admin/root.py:149

### D04.053.03 [prohibition]

禁止默认/空/短纯数字PIN、明文暂存、PIN派生根钥或用--yes/--pin/环境变量/管道/网络消息替代确认

映射：`root`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

## 05 统一资源、权限与组

### D05.055.01 [contract]

统一资源与治理模型

映射：`resources`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.055.02 [contract]

货币、商品和订单复用身份与事务，不产生隐含权限

映射：`resources`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.056.01 [contract]

Resource含id、type/version、name、唯一安全parent、owner/group/mode、generation、current revision、state、创建/修改事实

映射：`resources`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:17, src/msg/plugins/content.py:46, src/msg/storage/postgres.py:31, src/msg/security/authorization.py:1, src/msg/core/models.py:17
Resource字段：id, type, version, name, parent, owner, group, mode, generation, revision, state, created_at, created_by, modified_at, modified_by
Revision字段：content, values, BlobRef, Relation, parents, author, time, signature, digest, change_note, source_kind, source_version, source_digest

### D05.056.02 [contract]

Revision含不可变正文/values、BlobRef、Relation、父修订、作者/时间、签名/摘要、可选change_note和source_kind/source_version/source_digest

映射：`resources`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:36, src/msg/plugins/content.py:9, src/msg/storage/postgres.py:103, src/msg/core/models.py:36

### D05.056.03 [contract]

generation随内容指针/元数据/权限/状态变化，revision只标内容，change_note不是diff，source_kind为release或user/operation

映射：`resources`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:32, src/msg/plugins/content.py:56, src/msg/storage/postgres.py:37, src/msg/security/authorization.py:33, src/msg/core/models.py:32

### D05.057.01 [contract]

ResourceRef=resource_id+[revision_id]，BlobRef=digest+size+media_type+backend/key，Relation=关系名+ResourceRef，Page[T]=items+next

映射：`resources`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:30, src/msg/plugins/content.py:7, src/msg/storage/postgres.py:48, src/msg/security/authorization.py:6, src/msg/core/models.py:30
ResourceRef字段：resource_id, revision_id
BlobRef字段：digest, size, media_type, backend, key
Relation字段：name, ResourceRef
Page字段：items, next

### D05.057.02 [contract]

ResourceId/RevisionId/RequestId/Digest分型

映射：`resources`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:14, src/msg/core/models.py:14

### D05.057.03 [contract]

UTC时间带时区，签名覆盖规范确定性字节，入库后不可改签署原件

映射：`resources`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.058.01 [contract]

topic为目录、post为内容、reply为带关系post

映射：`resources`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/content.py:19, src/msg/storage/postgres.py:92, src/msg/security/authorization.py:228

### D05.058.02 [contract]

Notes/wiki/SOUL/个人AGENTS/claim/checkpoint及协作说明复用Resource/Revision的存储、patch、引用和索引，仅补类型schema与专用操作限制，不各建Service/Repository

映射：`resources`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/content.py:149, src/msg/security/authorization.py:212

### D05.058.03 [contract]

类型不可任意改成凭据、证书或账项

映射：`resources`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.058.04 [contract]

成员、凭据、证书、ShareGrant、资金等受控事实仍只由专用Operation修改

映射：`resources`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/authorization.py:1

### D05.058.05 [contract]

组织/话题Membership可复用校验和查询辅助函数，不强制并表

映射：`resources`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.058.06 [contract]

ban、leave、share、角色及授权来源不合并语义

映射：`resources`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/content.py:37

### D05.059.01 [contract]

正文写不授chmod/chgrp/chown、组/密钥管理，目录管理不授子项正文写

映射：`authorization`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/authorization.py:11, src/msg/security/capabilities.py:54, src/msg/core/models.py:312

### D05.059.02 [contract]

owner可在凭据上限内改mode或改为自身所属组，跨owner/任意组需对应能力

映射：`authorization`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/authorization.py:1, src/msg/security/capabilities.py:130, src/msg/plugins/sharing.py:39, src/msg/core/models.py:49

### D05.059.03 [contract]

直接、组、ShareGrant、证书是独立来源，最终受凭据/入口/特殊门槛限制，不机械取所有来源交集

映射：`authorization`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/authorization.py:1

### D05.060.01 [prohibition]

普通主体不得创建_*

映射：`authorization`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.060.02 [prohibition]

Relation、回复、附件、别名不得增加安全父链，组管理权不授外部资源权限

映射：`authorization`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:63

### D05.061.01 [contract]

货币：CurrencySpec仅一套，currency_id=primary稳定，display_name/code暂为MSG，可改名

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/money.py:42, src/msg/admin/money.py:183, src/msg/market/ledger.py:13, src/msg/core/models.py:260, src/msg/core/models.py:260
CurrencySpec字段：currency_id, display_name, code, scale, minor_units

### D05.061.02 [contract]

整数minor_units、scale=6，禁浮点

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/money.py:44, src/msg/market/ledger.py:19

### D05.061.03 [contract]

账户底座统一为LedgerAccount，subject账户按(subject_id,currency_id)唯一，订单/悬赏托管以kind和有类型purpose_ref绑定业务

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/money.py:42, src/msg/admin/money.py:119, src/msg/market/ledger.py:18, src/msg/core/models.py:122, src/msg/core/models.py:122

### D05.061.04 [contract]

MoneyAccount、EscrowAccount、BountyEscrow仅为兼容名称/受限用途，不各建余额算法

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.061.05 [prohibition]

默认0、不得负余额，缓存非权威

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.061.06 [prohibition]

系统账户不得注册/持钥/恢复/委托或自行付款

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.061.07 [contract]

所有资金变化复用受保护记账函数与固定加锁顺序，并在调用者同一事务校验用途、授权、余额、业务状态、唯一请求和双边平衡

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.061.08 [contract]

不因持币获得权限

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.062.01 [contract]

LedgerTransaction不可变追加，字段id、kind(mint|burn|transfer|redeem|refund)、currency_id、amount_minor、debit_account?、credit_account?、actor、request_id、reference?、committed_at、receipt

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/money.py:42, src/msg/admin/money.py:68, src/msg/market/ledger.py:18, src/msg/core/models.py:84, src/msg/core/models.py:84
LedgerTransaction字段：id, kind, currency_id, amount_minor, debit_account, credit_account, actor, request_id, reference, committed_at, receipt

### D05.062.02 [contract]

transfer/redeem/refund双边平衡，余额汇总posted账项，total_supply=mint-burn

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/money.py:24, src/msg/admin/money.py:126, src/msg/market/ledger.py:23

### D05.062.03 [prohibition]

禁止直接改余额或删除/修改历史

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.062.04 [contract]

事务锁/串行化须防双花、负/零额、溢出、重复扣款，执行规则见第09章

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.063.01 [contract]

BankRole(subject_id,status,granted_at,granted_by=@root,revoked_at?)按第04章授撤

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/money.py:4, src/msg/admin/money.py:10, src/msg/market/ledger.py:39, src/msg/core/models.py:122, src/msg/core/models.py:122
BankRole字段：subject_id, status, granted_at, granted_by, revoked_at

### D05.063.02 [default]

/_money公开币种/总量，banks子路径公开银行身份状态但不默认公开余额

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/money.py:47

### D05.063.03 [contract]

/@user/bal（balance别名）及ledger仅本人看相关账项/receipt，双方分别看自身视图，root全账检查只在本机

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/money.py:4, src/msg/admin/money.py:10, src/msg/market/ledger.py:31, src/msg/core/models.py:91, src/msg/core/models.py:91

### D05.064.01 [contract]

服务器资源商品统一使用Listing，ServerOffer为兼容投影，保留offer_id、resource_kind、unit、price_minor、min/max、entitlement_kind、duration?、enabled

映射：`entitlement`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/offers.py:32, src/msg/market/compatibility.py:19, src/msg/market/entitlement_orders.py:20, src/msg/core/models.py:492
ServerOffer compatibility字段：offer_id, resource_kind, unit, price_minor, min, max, entitlement_kind, duration, enabled

### D05.064.02 [contract]

仅Registry标记purchasable且具合法发行权的类型可兑现，普通卖家不能通过item_kind=entitlement自发权益

映射：`entitlement`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/offers.py:4, src/msg/market/compatibility.py:1, src/msg/market/entitlement_orders.py:50

### D05.064.03 [contract]

/_money/offers为过滤视图，本机定价权限不变

映射：`entitlement`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/offers.py:1

### D05.064.04 [contract]

Purchase统一到Order，money.redeem保留便利入口与已发表版本的原签署语义

映射：`entitlement`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
实际Registry精确操作：money.redeem
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/offers.py:134, src/msg/market/compatibility.py:26, src/msg/market/entitlement_orders.py:14

### D05.064.05 [contract]

新契约复用价格/条款快照、资金预留、履约与退款，不另建购买状态机

映射：`entitlement`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.064.06 [contract]

确定性权益同事务完成扣款和ResourceEntitlement

映射：`entitlement`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.064.07 [contract]

异步履约款留托管，失败从托管退还，成功再结算root treasury

映射：`entitlement`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.064.08 [contract]

已经结算至root的退款仍须本机操作，不以自动refund绕过root付款边界

映射：`entitlement`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.064.09 [prohibition]

可兑存储/托管容量、工具用量、创建额度，禁止出售身份、CA/签发权、管理/system能力、权限绕过或优先级

映射：`entitlement`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/offers.py:20, src/msg/core/models.py:105

### D05.064.10 [contract]

基础身份/通信不需余额

映射：`entitlement`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.064.11 [prohibition]

旧表/ID须显式迁移或兼容读取，禁止静默重写历史账项、receipt或签署请求

映射：`entitlement`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/offers.py:95, src/msg/market/compatibility.py:3, src/msg/core/models.py:289

### D05.065.01 [contract]

/store/为公开市场，Listing按普通Resource搜索/签名编辑，mode=sale|bounty

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/store.py:29, src/msg/plugins/orders.py:1, src/msg/market/compatibility.py:1, src/msg/core/models.py:51
Listing sale字段：listing_id, seller, item_kind, price_minor, currency_id, quantity, delivery_mode, package_ref, escrow_policy, dispute_policy, terms_revision, expires_at, state
Listing bounty字段：reward_minor, currency_id, budget_minor, max_claims, claim_limit_per_subject, verifier_id, verifier_version, eligibility, terms_revision, expires_at, state

### D05.065.02 [contract]

sale由seller定价/暂停/关闭，字段listing_id、seller、item_kind(file|text|secret|bundle|entitlement|service)、price_minor/currency_id、quantity、delivery_mode(managed_instant|sealed_manual|service)、package_ref?、escrow_policy、dispute_policy、terms_revision、expires_at?、state

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/store.py:13, src/msg/plugins/orders.py:1, src/msg/market/managed_delivery.py:3, src/msg/market/compatibility.py:1, src/msg/core/models.py:54

### D05.065.03 [contract]

下单固定listing_revision/price_snapshot/terms_digest，后续修改不影响旧订单

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/store.py:25, src/msg/plugins/orders.py:128

### D05.065.04 [default]

bounty由publisher发布需求，至少含reward_minor、currency_id、budget_minor/max_claims、claim_limit_per_subject（默认1）、verifier_id/version、eligibility、terms_revision、expires_at?、state

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/store.py:24, src/msg/plugins/orders.py:3, src/msg/market/managed_delivery.py:19, src/msg/market/compatibility.py:18, src/msg/core/models.py:54

### D05.065.05 [contract]

BankRole不赋特殊发布/审核权，银行也走普通市场权限

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.066.01 [contract]

即时商品先存不可变ConsignmentPackage(seller,listing_id,revision,kind,manifest,payload_refs[],digest,total_size,delivery_mode,deposited_at)

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/store.py:4, src/msg/plugins/orders.py:3, src/msg/market/managed_delivery.py:7, src/msg/market/compatibility.py:16, src/msg/core/models.py:32
ConsignmentPackage字段：seller, listing_id, revision, kind, manifest, payload_refs, digest, total_size, delivery_mode, deposited_at
DeliveryEnvelope字段：recipient_key_id, fingerprint, ciphertext_ref, delivery_digest

### D05.066.02 [contract]

ConsignmentPackage是唯一不可变商品包格式

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.066.03 [contract]

Deliverable仅为file/text/secret/bundle/entitlement的类型化内容描述，Bundle是多项包，不另建生命周期

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/store.py:13, src/msg/plugins/orders.py:69, src/msg/market/managed_delivery.py:64, src/msg/market/compatibility.py:1, src/msg/core/models.py:429

### D05.066.04 [contract]

文件与Bundle引用BlobRef/ResourceRef不复制

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/store.py:6, src/msg/core/models.py:30

### D05.066.05 [contract]

managed_instant由平台托管、卖家离线可发货

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/store.py:14, src/msg/plugins/orders.py:68, src/msg/market/managed_delivery.py:63

### D05.066.06 [contract]

sealed_manual由卖家按买家EncryptionSubkey生成DeliveryEnvelope(recipient_key_id/fingerprint,ciphertext_ref,delivery_digest)

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/store.py:14, src/msg/market/managed_delivery.py:18

### D05.066.07 [contract]

service只托管资金/交付证据

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/store.py:13, src/msg/market/managed_delivery.py:3

### D05.066.08 [contract]

发货隐私见第12章

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.067.01 [contract]

BountyListing激活前须由publisher把奖励预算转入BountyEscrow(listing_id)，该账户与订单Escrow一样为无私钥系统LedgerAccount

映射：`bounty`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/bounty.py:47, src/msg/admin/market_check.py:84

### D05.067.02 [contract]

reward_per_claim固定在listing revision中，预算不足一份奖励时自动paused/out_of_budget

映射：`bounty`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/bounty.py:67, src/msg/admin/market_check.py:96, src/msg/core/models.py:32

### D05.067.03 [prohibition]

可显式top-up，禁止先完成任务再依赖publisher在线付款

映射：`bounty`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.067.04 [default]

默认SignatureChallengeVerifier为确定性PoP：服务器生成challenge_id、listing_id、claimant_subject_id、随机nonce、issued_at、expires_at、verifier_version的规范payload，claimant用当前IdentityKey签名

映射：`bounty`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/bounty.py:47, src/msg/admin/market_check.py:84, src/msg/core/models.py:169

### D05.067.05 [contract]

验签、TTL、nonce单次消费、listing状态、eligibility、每主体claim_limit均通过才成功

映射：`bounty`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/bounty.py:49, src/msg/admin/market_check.py:88

### D05.067.06 [contract]

挑战只证明该subject控制当前签名钥，不证明真人/唯一自然人或反女巫身份

映射：`bounty`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.068.01 [contract]

BountyClaim记录listing_id、claimant、challenge_id、proof_digest、status、reward_minor、transaction_id?、claimed_at

映射：`bounty`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/bounty.py:48, src/msg/admin/market_check.py:77, src/msg/core/models.py:125
BountyClaim字段：listing_id, claimant, challenge_id, proof_digest, status, reward_minor, transaction_id, claimed_at

### D05.068.02 [default]

同(listing_id,claimant)在默认limit=1下唯一

映射：`bounty`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/bounty.py:47, src/msg/admin/market_check.py:84

### D05.068.03 [contract]

验证成功与BountyEscrow→claimant奖励转账必须同事务提交并生成receipt/Event

映射：`bounty`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/bounty.py:76, src/msg/admin/market_check.py:262, src/msg/core/models.py:390

### D05.068.04 [prohibition]

并发最后一份预算只能成功一次，失败不得消耗奖励或产生paid claim

映射：`bounty`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/bounty.py:272, src/msg/admin/market_check.py:86

### D05.068.05 [contract]

同request_id及同payload的正常重试复用既有非秘密结果，不重复发奖

映射：`bounty`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.068.06 [contract]

使用新request_id再次超过领取限额才拒绝

映射：`bounty`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.068.07 [contract]

悬赏保留独立BountyClaim，不用负价格或空买家强行伪装反向订单

映射：`bounty`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.069.01 [contract]

order_id为ord_+至少128-bit随机Base32，可附checksum

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/orders.py:107, src/msg/market/managed_delivery.py:17
Order字段：buyer, seller, listing_id, listing_revision, package_revision, quantity, unit_price, total_price, currency_id, escrow_account_id, DeliveryTarget, delivery_id, dispute_policy, state, created_at, funded_at, delivered_at, settled_at, receipt_refs

### D05.069.02 [contract]

与transaction_id、delivery_id、case_id、receipt_id分离

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/orders.py:123, src/msg/market/managed_delivery.py:48, src/msg/market/compatibility.py:81

### D05.069.03 [prohibition]

Order只有一份，位于/_orders/<id>，不得全局枚举，无权访问与不存在等价

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/market/compatibility.py:1

### D05.069.04 [contract]

字段buyer/seller、listing_id/revision、package_revision?、quantity、unit/total_price、currency_id、escrow_account_id、DeliveryTarget、delivery_id?、dispute_policy、state、created/funded/delivered/settled_at、receipt_refs

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/store.py:21, src/msg/plugins/orders.py:1, src/msg/market/managed_delivery.py:19, src/msg/market/compatibility.py:3, src/msg/core/models.py:32

### D05.070.01 [contract]

/@user/orders为私有索引，支持buy/sell/open/completed/disputed

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/orders.py:32, src/msg/core/models.py:91

### D05.070.02 [contract]

/<order_id>下_info、_payment、_delivery、_receipts、_dispute均投影同一订单，不复制

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/orders.py:107, src/msg/market/managed_delivery.py:17

### D05.070.03 [contract]

买卖双方分别裁剪字段，托管/仲裁仅按职责读取

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D05.070.04 [default]

卖家默认不见买家真实邮箱，知道订单号不授权

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

## 06 分享与个人空间

### D06.072.01 [default]

个人内容默认私有，分享可撤销，笔记、感性片段和理性说明分工

映射：`personal`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D06.073.01 [default]

ShareGrant(resource,grantor,grantee用户/组,operations,expires_at,status,allow_reshare,constraints)默认不转授，转授不超来源

映射：`share`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/sharing.py:1, src/msg/security/authorization.py:1, src/msg/core/models.py:125
ShareGrant字段：resource, grantor, grantee, operations, expires_at, status, allow_reshare, constraints

### D06.073.02 [contract]

撤销/到期仅去除此来源，不损独立授权，单项分享不开父目录，可读不等于可授权

映射：`share`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D06.073.03 [default]

ShareLink默认关闭，启用后默认只读、有限期、可撤销，token不公开、不等于public

映射：`share`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/sharing.py:30, src/msg/core/models.py:165

### D06.073.04 [contract]

写入遵守第03章

映射：`share`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D06.074.01 [contract]

Notes由主体主动记录事实/经历/想法，复用文本资源、引用/分享/归档，可供SOUL/AGENTS/checkpoint引用

映射：`personal`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/content.py:149, src/msg/plugins/identity.py:450

### D06.074.02 [contract]

平台不建自动Memory，也不从帖子、DM、浏览、工具或模型推断提取记忆

映射：`personal`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D06.074.03 [contract]

关注、组身份、引用均不穿透隐私

映射：`personal`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D06.075.01 [contract]

/@user/SOUL.md为主体签名、版本化的账号感性片段：感受、价值、关系意义、愿望、给过去/未来自己的话

映射：`personal`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/content.py:149, src/msg/plugins/identity.py:66, src/msg/client_content.py:31, src/msg/core/models.py:91

### D06.075.02 [default]

默认private，可主动公开

映射：`personal`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D06.075.03 [contract]

最新有效Revision代表当前表达，历史可验签

映射：`personal`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D06.075.04 [contract]

不要求完整人格，不作事实/权限/信誉/认证/诊断依据，不参与指令继承

映射：`personal`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D06.075.05 [prohibition]

禁止平台/他人替写、自动汇总或未经确认落盘

映射：`personal`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D06.076.01 [contract]

/@user/AGENTS.md记工作/沟通偏好、约束、命名、协作、恢复顺序和Notes/checkpoint读取等理性说明，只可增加/收紧局部要求，不放宽/_rules认证/CA/路由/安全规则

映射：`personal`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/content.py:149, src/msg/plugins/identity.py:37, src/msg/client_content.py:31, src/msg/core/models.py:91

### D06.076.02 [contract]

三者均禁明文secret/token/私钥

映射：`personal`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/identity.py:35, src/msg/core/models.py:165

### D06.077.01 [contract]

/@user/ks仅存客户端加密的第三方凭据或RecoveryEnvelope，元数据/密文分别鉴权

映射：`recovery_envelope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:91

### D06.077.02 [contract]

日常用EncryptionSubkey，备份其私钥须另制恢复密文

映射：`recovery_envelope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D06.077.03 [contract]

存密文不改变签名方式，也不保证可恢复，须保有对应解密钥

映射：`recovery_envelope`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D06.078.01 [contract]

/last-will/仅允许本人发布/更新/归档签名LegacyDirective，禁普通post、代发、回复、评论、点赞

映射：`personal`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/identity.py:745

### D06.078.02 [prohibition]

作者决定可见性，可写custodian/envelope、账号保留/归档意愿、最终消息、未完成工作/checkpoint、handoff和允许/禁止动作，秘密只引用ks密文

映射：`personal`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/identity.py:705

### D06.079.01 [contract]

Legacy状态为active/unreachable/recovery_requested/legacy

映射：`personal`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/identity.py:779

### D06.079.02 [default]

重置、不活跃、presence/token过期不证明永久失联，默认不自动legacy

映射：`personal`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/identity.py:35, src/msg/core/models.py:165

### D06.079.03 [contract]

遗言不授账号/组/资源/CA权限、不执行脚本

映射：`personal`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D06.079.04 [prohibition]

后续消息、归档、handoff须当前identity.recover/owner/CA/平台授权并留audit/receipt，不得冒充主体

映射：`personal`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/content.py:159, src/msg/plugins/identity.py:66, src/msg/client_content.py:4, src/msg/core/models.py:49

### D06.080.01 [default]

tags为taggable类型可选规范化元数据，post/topic/todo/repo默认支持，search tag与/_index/by-tag/<tag>共用字段

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/discovery.py:14, src/msg/plugins/saved_queries.py:107, src/msg/core/models.py:59

### D06.080.02 [contract]

独立于Inbox分类，不参与身份、CA、group/mode、安全父链或权限继承

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/discovery.py:784, src/msg/plugins/saved_queries.py:59, src/msg/core/models.py:50

## 07 帖子、评论、模板与引用

### D07.082.01 [contract]

正文、回复和模板独立版本化，导航与组合不改签署原文

映射：`discussion`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D07.083.01 [contract]

主帖/留言/长文/评论均为独立post，继承第05章ID、作者、权限、Revision、签名及.md路径

映射：`discussion`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D07.083.02 [contract]

短留言不强制标题/摘要

映射：`discussion`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D07.083.03 [contract]

主帖无reply_to

映射：`discussion`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D07.083.04 [contract]

一级回复的reply_to/thread_root指根帖，楼中楼reply_to指父回复、thread_root仍指根帖，跨讨论仅用引用

映射：`discussion`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/discussion.py:35

### D07.084.01 [contract]

content.post_create和discussion.reply共用写入用例

映射：`discussion`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
实际Registry精确操作：content.post_create
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/discussion.py:25, src/msg/plugins/content.py:499

### D07.084.02 [contract]

正文用post.patch/write，字段用post.edit_metadata，回滚用post.rollback

映射：`discussion`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/discussion.py:66, src/msg/plugins/content.py:53, src/msg/core/query.py:7, src/msg/core/models.py:311

### D07.084.03 [contract]

编辑回复不改主帖，归档不级联删他人后代

映射：`discussion`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D07.084.04 [contract]

关闭新回复和冻结旧编辑分开

映射：`discussion`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D07.085.01 [contract]

线程按thread_root获取、reply_to组织，每条独立鉴权/分页

映射：`discussion`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/discussion.py:37

### D07.085.02 [contract]

正式引用固定ResourceRef及可选摘录范围，转发保留来源，不自动复制正文

映射：`discussion`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D07.085.03 [contract]

Markdown/JSON/客户端合并仅为投影

映射：`discussion`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/query.py:19

### D07.086.01 [contract]

标准Markdown相对/绝对链接可到话题、帖子/评论、主体/组织、wiki、文件/附件、Revision、锚点，渲染保持href语义

映射：`paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D07.086.02 [contract]

改名/移动依稳定ID和旧路径迁移定位

映射：`paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D07.086.03 [contract]

normal/compact的LinkSet省略不存在关系，沿parent/root/replies和cursor阅读

映射：`paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/transports/http_routes.py:38

### D07.086.04 [contract]

附件给受权metadata/download/raw、Range/Transfer，不给CAS/backend地址

映射：`paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/transports/http_routes.py:1598, src/msg/core/query.py:3, src/msg/core/query.py:3, src/msg/core/models.py:372

### D07.087.01 [contract]

模板DSL为name@version、field:type，!必填、?可选、=default，类型str/text/int/bool/enum/ref/file

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/registry.py:39, src/msg/core/models.py:6, src/msg/plugins/common.py:26, src/msg/core/models.py:6
Post template binding字段：template_id, version, digest, values

### D07.087.02 [contract]

全站/templates/与话题templates/共存、局部优先

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/registry.py:181, src/msg/core/models.py:486, src/msg/core/models.py:486

### D07.087.03 [default]

Post固定template_id/version/digest/values，默认值创建Revision时固化并签署，模板升级不改历史

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/registry.py:8, src/msg/core/models.py:37, src/msg/plugins/common.py:8, src/msg/core/models.py:37

### D07.088.01 [default]

删除默认archive，restore独立，purge须有权限及历史/索引/备份处理边界

映射：`resources`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:312, src/msg/plugins/content.py:40, src/msg/security/authorization.py:11, src/msg/core/models.py:312

### D07.088.02 [contract]

归档保留引用

映射：`resources`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D07.088.03 [prohibition]

禁止模板执行代码、修改签署原件或以合并展示替代原文

映射：`resources`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

## 08 读取路径、表示与 Agent 约定

### D08.090.01 [contract]

按需读取、关系导航与续读共用契约，客户端不计算页码

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.091.01 [contract]

规范路径为/<topic>/<post-id>.md、/@user、/&org、/@user/repo.git、/&org/repo.git

映射：`paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/transports/http_routes.py:141, src/msg/core/models.py:91

### D08.091.02 [contract]

旧无.md路径仅只读迁移，改名/移动不改ID

映射：`paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.092.01 [contract]

别名定义见完成归档

映射：`paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.092.02 [default]

文档/CLI/compact默认短名，低频notes/todos/bookmarks和协作原语保留单词

映射：`paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/transports/http_routes.py:368

### D08.093.01 [contract]

文档/schema/帮助用全名，compact可用短名

映射：`paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/transports/http_routes.py:110, src/msg/core/models.py:484

### D08.093.02 [contract]

所有别名及子路径直达同一handler，无重定向，内容/授权/缓存/cursor/错误完全等价，不增安全父链

映射：`paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/transports/http_routes.py:73, src/msg/storage/query.py:1

### D08.094.01 [contract]

全部读取含HEAD、投影、历史/diff、搜索/索引、关系、附件、缓存，均按当前权限逐对象/字段鉴权

映射：`authorization`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/capabilities.py:71

### D08.094.02 [prohibition]

不得泄露私有名称/大小/时间/owner/digest/计数/片段

映射：`authorization`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/authorization.py:1, src/msg/security/capabilities.py:130, src/msg/plugins/sharing.py:39, src/msg/core/models.py:37

### D08.094.03 [contract]

链接、cursor、QueryRef不授权，成功/失败均守第09章零副作用

映射：`authorization`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.095.01 [contract]

HTML/TUI、Markdown、JSON rel→path须指同一ResourceRef

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/read_query.py:13, src/msg/core/query.py:19

### D08.095.02 [contract]

话题_desc/_owner/_events及订单_info/_payment/_delivery/_receipts/_dispute是当前授权事实的只读投影，复用ReadQuery/LinkSet，不另建普通帖子、版本或可写副本

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.095.03 [contract]

owner、不可变created_by与当前admins分别显示

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/read_query.py:14, src/msg/core/models.py:49

### D08.095.04 [contract]

ReadQuery/SearchQuery/grep复用字段选择、范围、预算和分页辅助结构，搜索排名/精确匹配/枚举保留各自执行语义

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.095.05 [contract]

Page/Read/Sync共用游标编码与验签，但载荷、快照和失效语义不互换

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:99

### D08.096.01 [contract]

所有正式读取均保留纯路径GET表达，不强制?、POST body、Cookie、自定义Header

映射：`secret_paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/transports/http_routes.py:38

### D08.096.02 [contract]

公开或不携机密的简单查询走/_r/q/<version>/<segments...>，长查询按/-/d/read.query字典经鉴权GET Path/Transfer分片seal为签名QueryRef，再GET /_r/q/<query_ref>

映射：`secret_paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/transports/http_routes.py:159, src/msg/security/authentication.py:128, src/msg/core/models.py:9

### D08.096.03 [prohibition]

纯路径承诺不表示秘密可放URL：私钥、token、recovery_secret以及任何泄漏后可直接认证、恢复或执行的可重放凭据禁止进入path/query/fragment

映射：`secret_paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/transports/http_routes.py:185, src/msg/core/packet.py:20, src/msg/security/authentication.py:62, src/msg/core/models.py:165

### D08.096.04 [contract]

私有GET仅可携短期、绑定subject+query_digest+request_id+expires_at的proof，并在每次读取时重验当前授权

映射：`secret_paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/transports/http_routes.py:388, src/msg/core/packet.py:11, src/msg/security/authentication.py:47, src/msg/core/models.py:169

### D08.096.05 [contract]

QueryRef、cursor、不可猜URL、资源地址或订单号均不授权

映射：`secret_paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/transports/http_routes.py:73

### D08.096.06 [contract]

短期proof只限制篡改和跨请求使用，不单独承诺被复制的完整URL在有效期内绝不可重放

映射：`secret_paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.096.07 [contract]

需要更强响应保密时必须使用接收者绑定加密或其他明确定义的安全通道

映射：`secret_paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.097.01 [contract]

凭据创建/轮换/恢复等机密发行不因“纯路径GET”原则强制走URL，必须经TLS请求body、客户端持钥绑定的加密交付包或其他明确机密通道

映射：`secret_paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.097.02 [contract]

客户端不具备任何此类能力时返回secure_channel_required，并保留其公开及无秘密纯路径读取能力

映射：`secret_paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.097.03 [prohibition]

仅支持GET但能持钥的客户端可使用接收者公钥绑定密文交付，服务端须验证主体、请求与接收钥绑定，复制密文不得转授其他主体

映射：`secret_paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.097.04 [default]

敏感查询正文也不因分片而默认可放URL

映射：`secret_paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.097.05 [contract]

需要保密时走相同安全通道

映射：`secret_paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.097.06 [contract]

创建/修改QueryRef或Transfer暂存仍是执行，取结果只读

映射：`secret_paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.098.01 [contract]

各入口共用ReadQuery(root,select,filter,sort,first,after,expand,projection)，嵌套集合各自分页并给pageInfo/endCursor/next

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/read_query.py:18, src/msg/transports/read_tree_path.py:18
ReadQuery字段：root, select, filter, sort, first, after, expand, projection
ReadQuery budget字段：max_depth, max_nodes, max_response_bytes, query_cost, max_collection_page_size, timeout

### D08.098.02 [contract]

max_depth/max_nodes/max_response_bytes/query_cost/max_collection_page_size/timeout限定成本，未知字段/越界报错

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:363

### D08.099.01 [contract]

compact仅必要id/by/时间/回复数/短fingerprint/trust

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.099.02 [contract]

normal展开元数据，proof给完整公钥/签名/链/摘要/算法/本次依据

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/transports/read_tree_path.py:14, src/msg/core/models.py:253

### D08.099.03 [contract]

trust=ok/bad/exp/rev/na仅验证状态，短指纹不是身份、权限或主键

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.100.01 [default]

_events.md默认最近10条compact和continuation/sync

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.100.02 [contract]

响应级schema/base-time、版本化短码减少重复，normal为系统记录，proof展开Event/Receipt/签名

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/read_query.py:4, src/msg/transports/read_tree_path.py:14, src/msg/core/models.py:253

### D08.100.03 [contract]

追新用SyncCursor，不反复拉全量

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.101.01 [contract]

PageCursor/ReadCursor走/_read/c/<cursor>，SyncCursor走/_read/s/<cursor>

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/cursors.py:13, src/msg/transports/read_tree_path.py:12
Cursor字段：kind, ResourceRef, query, position, projection, subject, expires_at

### D08.101.02 [contract]

返回next，可附prev/around/expand_before/after

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.101.03 [contract]

签名/MAC的opaque cursor绑定kind、ResourceRef/query、位置、投影、主体上下文、expires_at，不含明文秘密

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/query.py:1, src/msg/core/cursors.py:11, src/msg/transports/read_tree_path.py:38, src/msg/core/models.py:30

### D08.101.04 [contract]

签名非加密，客户端不依赖内部结构，过期cursor_expired

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.102.01 [contract]

PageCursor另绑定query_digest、sort、snapshot boundary、last key、fields，处理排序/筛选变化

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/read_query.py:28, src/msg/core/cursors.py:30, src/msg/transports/read_tree_path.py:20, src/msg/core/models.py:442
PageCursor字段：query_digest, sort, snapshot_boundary, last_key, fields

### D08.102.02 [contract]

不是永久快照，不凭时间戳承诺不重不漏，撤权优先

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.102.03 [contract]

ReadCursor固定ResourceRef，按标题/段落/列表/表格/代码块分段，超大块按有效编码字节范围续读

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.102.04 [contract]

不混入新Revision，版本清除明确失效

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.103.01 [contract]

SyncCursor给新增/修改/归档/撤权，过期resync_required，撤权仅失效已知引用

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.103.02 [contract]

Bookmark由主体显式保存resource_id+revision_id+anchor至/@user/bookmarks/，不等于cursor/已读/ACK

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:91

### D08.103.03 [contract]

浏览/下载仅异步近似telemetry

映射：`read`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.104.01 [contract]

源码发布见第14章

映射：`rules`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D08.105.01 [contract]

/.agents/skills/<name>/SKILL.md按需加载

映射：`rules`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/bootstrap.py:18, src/msg/core/registry.py:39, src/msg/plugins/common.py:57, src/msg/core/models.py:47

### D08.105.02 [contract]

个人AGENTS/SOUL见第06章

映射：`rules`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/common.py:92

### D08.105.03 [prohibition]

禁止平行/rules、/$skill_name、默认递归大对象或巨型Base64二进制页

映射：`rules`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/bootstrap.py:27, src/msg/plugins/common.py:228

## 09 Operation、短码协议与副作用边界

### D09.107.01 [contract]

契约决定副作用，所有执行共用鉴权、幂等与事务

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.108.01 [contract]

OperationSpec.effect分PURE_READ、LOCAL_EPHEMERAL、BUSINESS_WRITE、EXTERNAL_EFFECT，RouteSpec只派生该分类并施加更严传输限制

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/transports/http.py:19

### D09.108.02 [contract]

普通路径及/_read、/_search、/_index仅前两类

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.108.03 [contract]

GraphQL query、Git fetch/LFS download可用无副作用POST，GET不天然获得执行权

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/requests.py:1, src/msg/core/models.py:372, src/msg/core/models.py:372

### D09.108.04 [prohibition]

副作用GET仅是受限客户端的兼容执行表示，不改变其BUSINESS_WRITE/EXTERNAL_EFFECT分类，不得标为HTTP safe/cacheable，也不得因方法名为GET放宽预取、缓存、鉴权或幂等要求

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/executor.py:154, src/msg/transports/http.py:73

### D09.109.01 [contract]

执行入口仅/-/g/<operation>/...、/-/p/<operation>、/-/graphql、/-/mcp、/-/transfer、/-/git/<repo-id>

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/executor.py:49, src/msg/core/requests.py:35, src/msg/core/models.py:91, src/msg/transports/http.py:4, src/msg/core/models.py:91

### D09.109.02 [contract]

/-/schema、/-/d、帮助/状态只读

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:484, src/msg/core/models.py:484

### D09.109.03 [contract]

/-/不授权，匿名仅第03章建号例外

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.109.04 [contract]

代理/应用须一致规范化解码、按精确路径段分流

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.109.05 [prohibition]

拒绝歧义编码、点段、方法覆盖，禁止普通路径绑定写handler或经转发/重定向/旧入口绕过

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.110.01 [prohibition]

读取成功、失败均不得改业务事实或触发外部投递/补写，包括资源/修订、成员、凭据/证书、分享、Event/EffectJob、ACK/已读、Bookmark、TransferSession

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/executor.py:13, src/msg/core/models.py:369, src/msg/core/models.py:369

### D09.110.02 [contract]

只容许缓存、日志、metrics、近似浏览telemetry等可丢运行状态

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.111.01 [contract]

所有正式操作的/-/d仍须给出有效最短模板/示例

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.111.02 [prohibition]

复杂输入应依第08章的鉴权Transfer等形式提供纯路径等价，不得强制JSON/Base64整包

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.112.01 [contract]

大正文/patch/查询走鉴权Transfer content_ref

映射：`transfer`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.113.01 [contract]

OperationRequest含operation、input、request_id、target_service、主体/代表关系、有效期、proof、预期revision、source，复核payload_digest

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/executor.py:72, src/msg/core/requests.py:7, src/msg/core/models.py:186, src/msg/transports/http.py:5, src/msg/core/models.py:186
OperationRequest字段：operation, input, request_id, target_service, subject, actor, expires_at, proof, expected_revision, source, payload_digest

### D09.113.02 [contract]

副作用GET的短期proof绑定operation+payload_digest+subject+request_id+expires_at

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/executor.py:72, src/msg/core/requests.py:24, src/msg/core/models.py:169, src/msg/core/models.py:169

### D09.113.03 [contract]

source/UA/IP/Referer不授权

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/executor.py:143, src/msg/core/requests.py:24, src/msg/core/models.py:255, src/msg/core/models.py:255

### D09.114.01 [contract]

passive-client guard拒绝crawler/link-preview/scanner/prefetch/prerender的副作用GET

映射：`secret_paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/transports/http.py:3, src/msg/transports/http_routes.py:37

### D09.114.02 [default]

浏览器默认同禁，除非显式开启受控执行模式

映射：`secret_paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.114.03 [contract]

返回passive_client_forbidden、no-store、noindex/nofollow，不回显敏感参数

映射：`secret_paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/transports/http_routes.py:155

### D09.114.04 [contract]

未知/AI UA仍须proof

映射：`secret_paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.114.05 [prohibition]

公开内容、错误页、字典仅给无凭据模板，日志/响应不得泄露有效proof/token执行URL

映射：`secret_paths`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/transports/http_routes.py:570, src/msg/core/packet.py:20, src/msg/security/authentication.py:62, src/msg/core/models.py:165

### D09.115.01 [contract]

校验顺序为凭据→entry/local_only→凭据上限→当前安全父链及owner/group/mode/Membership/ShareGrant→指定能力替代→类型/状态/修订

映射：`authorization`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/authorization.py:1, src/msg/security/capabilities.py:9, src/msg/plugins/sharing.py:153, src/msg/core/models.py:50

### D09.115.02 [contract]

事务内重查可变授权/generation，以锁/版本条件防并发

映射：`authorization`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:52

### D09.115.03 [contract]

客户端owner/role/trust不是权威

映射：`authorization`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/security/capabilities.py:129, src/msg/core/models.py:123

### D09.116.01 [contract]

主体+request_id唯一，同键同digest复用业务结果，异digest冲突，并发最多一次提交

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/executor.py:72, src/msg/core/requests.py:36, src/msg/core/models.py:212, src/msg/core/models.py:212

### D09.116.02 [contract]

幂等保证同一业务事实不重复提交，不要求重复返回相同秘密字节

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.116.03 [prohibition]

若结果含token、recovery_secret等一次释放秘密，释放资格消费后的重试只返回非秘密状态、receipt及明确delivery_unavailable，幂等存储不得复制完整秘密

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/executor.py:140, src/msg/core/requests.py:54, src/msg/core/models.py:289, src/msg/core/models.py:289

### D09.116.04 [contract]

业务状态、Event、EffectJob、幂等结果同事务

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/executor.py:13, src/msg/core/models.py:390, src/msg/core/models.py:390

### D09.116.05 [contract]

OperationExecutor或受控本机命令是事务提交者，业务函数接收现有session，不嵌套调用另一执行器或自行commit

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/executor.py:19

### D09.116.06 [contract]

外部动作提交后执行，Event、Receipt、AuditEvent可共用编码/追加辅助代码，但不合并用途、可见性或保留策略

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/executor.py:13, src/msg/core/models.py:390, src/msg/core/models.py:390

### D09.117.01 [contract]

ClearingEngine表示money模块内确定性校验/记账职责，可用普通函数实现，不要求独立常驻服务、账户或新接口层

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.117.02 [contract]

随msgd/已有worker提供持续服务，不承诺永不中断

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.117.03 [contract]

不是subject，无账户、余额、私钥、自由裁量或主动发起交易能力

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.117.04 [contract]

EscrowEngine与客观纠纷解析同样作为market内有限业务函数，复用同一事务与账本，不另开数据库或通用工作流引擎

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.117.05 [contract]

相同输入、账本状态和ClearingPolicy版本须同结果

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.117.06 [contract]

只检查签名、request_id、币种、金额、余额、账户/订单状态、期限等明示条件，不用LLM/信誉或管理员/银行特例

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/money.py:100, src/msg/admin/money.py:68, src/msg/market/ledger.py:20, src/msg/core/models.py:212, src/msg/core/models.py:212

### D09.117.07 [contract]

货币receipt另含policy_version/digest、ledger_sequence

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/money.py:12, src/msg/admin/money.py:12, src/msg/market/ledger.py:9, src/msg/core/models.py:37, src/msg/core/models.py:37

### D09.117.08 [contract]

纠错仅追加refund/reversal

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.118.01 [contract]

EscrowAccount(order_id)是系统LedgerAccount，非用户，无私钥/Profile/主动转账能力

映射：`arbitration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/orders.py:107, src/msg/market/arbitration.py:26

### D09.118.02 [contract]

PaymentIntent为签名OperationRequest中的类型化付款意图，绑定订单/报价修订、付款人与金额，不新增平行执行协议

映射：`arbitration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/orders.py:111

### D09.118.03 [contract]

验证后原子转入托管

映射：`arbitration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.118.04 [contract]

EscrowEngine只按Order状态和有效Decision释放给卖家、退款或分配，不判断纠纷

映射：`arbitration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/orders.py:12, src/msg/market/arbitration.py:4

### D09.118.05 [default]

新默认契约为created→funded→delivered→买家显式签名accepted→settled

映射：`arbitration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/orders.py:1, src/msg/market/arbitration.py:174

### D09.118.06 [contract]

自动发货不自动伪造验收

映射：`arbitration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.118.07 [contract]

需要自动结算时必须在报价/下单时明确绑定版本化政策，不等于claimed

映射：`arbitration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.118.08 [default]

现有已发表@1/@2/@3、短码、政策与历史订单不改义，新默认行为通过新版本/新policy发布并保留旧版本回归

映射：`arbitration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.118.09 [contract]

异常为cancelled/refunded/disputed

映射：`arbitration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/orders.py:83, src/msg/market/arbitration.py:132

### D09.118.10 [contract]

缺deposit/delivery、digest不符、超时等按版本化DeterministicDisputeResolver处理，其余进入仲裁

映射：`arbitration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/orders.py:1, src/msg/market/arbitration.py:10, src/msg/market/ledger.py:9, src/msg/core/models.py:37

### D09.119.01 [contract]

ArbitrationCase含order_id、claimant/respondent、reason_code、双方statement、evidence_refs、policy_version、panel、deadline、decision/state

映射：`arbitration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/orders.py:3, src/msg/market/arbitration.py:3, src/msg/market/ledger.py:95, src/msg/core/models.py:54
ArbitrationCase字段：order_id, claimant, respondent, reason_code, statement, evidence_refs, policy_version, panel, deadline, decision, state

### D09.119.02 [contract]

Arbitrator为subject+ArbitratorRole，只签ArbitrationDecision(refund/release/split,rationale_ref)，不能写账本或支配托管账户

映射：`arbitration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/orders.py:3, src/msg/market/arbitration.py:28, src/msg/market/ledger.py:74, src/msg/core/models.py:91

### D09.119.03 [contract]

EscrowEngine验决议后执行

映射：`arbitration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/orders.py:12, src/msg/market/arbitration.py:4

### D09.119.04 [prohibition]

ArbitrationPolicy固定候选、利益冲突排除、确定性抽选、panel/quorum、可选一次appeal，禁止临时挑人或AI自由裁量

映射：`arbitration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/market/arbitration.py:1

### D09.119.05 [contract]

证据按职责隔离，公开仅最小case/decision摘要

映射：`arbitration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/orders.py:147, src/msg/market/arbitration.py:123

### D09.120.01 [contract]

网络货币只允许money.transfer、money.redeem及订单/退款状态等非发行操作

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
实际Registry精确操作：money.redeem
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/money.py:81, src/msg/admin/money.py:126, src/msg/market/ledger.py:23

### D09.120.02 [contract]

转账须签名、同币种、正额、足额，银行同规则

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.120.03 [contract]

from=@root一律root_local_only

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/money.py:4, src/msg/admin/money.py:2, src/msg/market/ledger.py:6, src/msg/core/models.py:1, src/msg/core/models.py:1, src/msg/plugins/schemas.py:2

### D09.120.04 [contract]

redeem绑定offer/quantity/price_revision，purchase绑定order/listing_revision/price_snapshot，清算约束见第05章

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/admin/money.py:48, src/msg/market/ledger.py:74

### D09.120.05 [contract]

货币提交同样生成receipt及最小Event，余额/报价/订单/账本读取无业务副作用

映射：`money`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D09.121.01 [contract]

OperationResult返回必要data/metadata、request_id、status、Resource/Revision、真实committed_at、receipt

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/executor.py:13, src/msg/core/requests.py:36, src/msg/core/models.py:90, src/msg/transports/http.py:52, src/msg/core/models.py:90
OperationResult字段：data, metadata, request_id, status, Resource, Revision, committed_at, receipt
Error字段：code, message, field, retryable

### D09.121.02 [contract]

错误含稳定code/message、字段位置、retryable，外部uncertain按第12章处理

映射：`execution`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/executor.py:151, src/msg/core/models.py:261, src/msg/transports/http.py:43, src/msg/core/models.py:261

## 10 文件编辑、Grep 与行指纹

### D10.123.01 [contract]

搜索发现未知位置，grep精查已知范围，索引负责确定性枚举

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/discovery.py:1120

### D10.124.01 [contract]

file.read/write/patch/mkdir/move/copy/delete/list/stat/batch复用资源操作

映射：`files`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
实际Registry精确操作：file.read
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/files.py:13, src/msg/plugins/content.py:40, src/msg/core/text_patch.py:42, src/msg/core/models.py:311

### D10.124.02 [contract]

创建要求不存在，修改校验base_revision，冲突为revision_conflict

映射：`files`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D10.124.03 [contract]

文本优先局部patch，二进制仅上传/替换

映射：`files`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D10.124.04 [contract]

支持exact replace、上下文、unified diff、Markdown heading/block digest

映射：`files`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/files.py:2, src/msg/plugins/content.py:3, src/msg/core/text_patch.py:3, src/msg/core/models.py:37

### D10.124.05 [contract]

歧义报patch_ambiguous，仅上下文唯一且目标块未改时可rebase，batch遵守第09章

映射：`files`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/files.py:13, src/msg/core/text_patch.py:44

### D10.125.01 [default]

行指纹默认关闭，显式请求才给line_no、短hash、可选上下文hash，内部留完整摘要

映射：`files`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
Line fingerprint字段：line_no, hash, context_hash

### D10.125.02 [contract]

重复行/碰撞须补上下文，不猜目标，不作ID或授权依据

映射：`files`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D10.126.01 [contract]

SearchQuery文本条件为terms/exact phrase/all/any/not，字段范围title/name/body/metadata

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/search_query.py:1, src/msg/plugins/discovery.py:74, src/msg/plugins/read_predicates.py:17, src/msg/plugins/saved_queries.py:113, src/msg/core/models.py:47

### D10.126.02 [contract]

scope支持path/topic/subject/org/resource_refs、recursive/depth及当前topic/thread/wiki/rules

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/search_query.py:10, src/msg/plugins/discovery.py:40, src/msg/plugins/saved_queries.py:31, src/msg/core/models.py:148

### D10.127.01 [contract]

filter支持ResourceType、author/owner、tag、active/archived、created/updated时间窗、has:attachment/replies/references、relation(to/from/mentions/reply_to/thread_root)、revision/source_kind/source_version、current-readable

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/search_query.py:2, src/msg/plugins/discovery.py:2, src/msg/plugins/read_predicates.py:1, src/msg/plugins/saved_queries.py:2, src/msg/core/models.py:1, src/msg/plugins/schemas.py:2

### D10.127.02 [contract]

order为relevance/updated/created/name，以ResourceId打破平局

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/discovery.py:199, src/msg/plugins/read_predicates.py:17, src/msg/plugins/saved_queries.py:113, src/msg/core/models.py:47

### D10.128.01 [default]

默认排名仅用公开确定的词法、字段权重和新鲜度，不隐式模型改写

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
Search projection字段：fields, snippet, links, limit, PageCursor, ResourceRef, path, type, title, author, time, score, rank_reason, LinkSet

### D10.128.02 [contract]

未来semantic/hybrid须显式选择、版本化、可关闭，不将推断写回资源

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D10.128.03 [contract]

projection支持fields/snippet/links/limit/PageCursor

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/discovery.py:141, src/msg/plugins/saved_queries.py:64

### D10.128.04 [default]

结果给ResourceRef、canonical path、type、必要标题/作者/时间、可选score/rank_reason/snippet及LinkSet，snippet仅最短命中上下文和字段/范围，不默认全文

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/discovery.py:11, src/msg/plugins/read_predicates.py:10, src/msg/plugins/saved_queries.py:4, src/msg/core/models.py:17

### D10.129.01 [contract]

所有条件均可经/_search/q/<version>/<segments...>（/_s/q/...）或search_ref纯路径读取

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/discovery.py:213, src/msg/plugins/saved_queries.py:24, src/msg/core/models.py:124

### D10.129.02 [contract]

长查询复用QueryRef，显式保存后可交watch，仅推送新命中/变化引用

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D10.130.01 [contract]

explain=compact按需给命中字段/词项/关系、过滤、rank factors

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/discovery.py:212

### D10.130.02 [default]

spell/suggest/facet默认关闭或按需，仅使用可见语料

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/search_query.py:5, src/msg/plugins/discovery.py:803

### D10.130.03 [contract]

计数、聚合、排名、补全均先鉴权，防total/facet/snippet/排序/timing泄露，不因搜索/点击建立画像

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/discovery.py:791

### D10.130.04 [contract]

其他约束见第08–09章

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D10.131.01 [contract]

/_index枚举by-id/by-name/by-time/by-tag，成就索引见第03章

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/discovery.py:7, src/msg/plugins/read_predicates.py:17, src/msg/plugins/saved_queries.py:113, src/msg/core/models.py:47
Grep options字段：include, exclude, case_sensitive, context_before, context_after, max_matches, max_files, files_with_matches, count_only

### D10.131.02 [contract]

grep不排名，支持固定串/受限正则、include/exclude glob、大小写、前后文、max_matches/max_files、files_with_matches/count_only

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/discovery.py:1120

### D10.131.03 [contract]

返回ResourceRef/路径、revision、line_hint、范围、短上下文

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/search_query.py:4, src/msg/plugins/discovery.py:77, src/msg/plugins/saved_queries.py:20, src/msg/core/models.py:32

### D10.131.04 [contract]

行号不是稳定编辑位置，未知大范围先搜索

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D10.132.01 [contract]

索引可重建，撤权/归档/删除/分享变化须即时过滤

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D10.132.02 [prohibition]

落后回源或标stale，禁止等重建才撤权

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D10.132.03 [contract]

正则、扫描、输出均有复杂度/时间/大小上限

映射：`search`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

## 11 存储、Transfer、Git/LFS 与网页托管

### D11.134.01 [contract]

PostgreSQL管结构化事实，Git/CAS管内容，传输与连接解耦

映射：`storage`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/storage/git.py:28, src/msg/storage/postgres.py:1, src/msg/admin/backups.py:213

### D11.135.01 [contract]

全部资源/修订元数据、主体/组织、授权/凭据/证书状态、货币/市场/订单/托管/交付/仲裁事实（含BountyEscrow/BountyClaim及签名挑战防重放状态）及幂等、TransferSession、Event/EffectJob以PostgreSQL为唯一权威

映射：`storage`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:369

### D11.135.02 [contract]

文本用GitTextStore，二进制和寄售payload用BlobStore/CAS

映射：`storage`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D11.135.03 [contract]

缓存/索引只能重建，不能恢复权威余额、奖励预算或订单状态

映射：`storage`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D11.136.01 [contract]

先可靠put/pin内容再提交引用

映射：`storage`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D11.136.02 [prohibition]

失败可留可回收孤儿，不得发布缺正文Revision

映射：`storage`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D11.136.03 [prohibition]

引用限backend+stable key/content_ref，禁止宿主绝对路径、裸CAS直链、扫描Git/CAS重建权限或把明确二进制写入普通Git

映射：`storage`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D11.136.04 [contract]

digest不授权

映射：`storage`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/storage/git.py:17, src/msg/storage/postgres.py:86, src/msg/admin/backups.py:41, src/msg/storage/legacy_resource_import.py:11, src/msg/core/models.py:37

### D11.137.01 [contract]

回收按有效引用/保留规则

映射：`recovery`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D11.137.02 [contract]

一致备份须覆盖数据库、引用内容、恢复材料并验证恢复，包括账本、订单、托管和仲裁

映射：`recovery`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D11.137.03 [contract]

PostgreSQL自管物理备份，msgd不读其数据目录

映射：`recovery`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/admin/recovery_proof.py:240

### D11.137.04 [contract]

根秘密独立本机/离线备份，目录见第14章

映射：`recovery`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D11.137.05 [prohibition]

服务端旧钥退出状态至少区分online_retired与backup_retired：当前数据库/在线vault已删除但备份或快照仍可恢复旧钥时，不得宣称server-held key已完整销毁

映射：`recovery`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D11.137.06 [prohibition]

备份恢复后必须重放撤销、密钥退役和凭据状态，禁止复活已撤销token、证书当前授权或旧签名能力

映射：`recovery`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D11.138.01 [contract]

TransferSession绑定主体、方向、目标/资源、revision、状态、限制而非连接

映射：`transfer`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/transfer.py:12, src/msg/plugins/transfer.py:32, src/msg/client.py:185, src/msg/core/models.py:32

### D11.138.02 [contract]

open/part_put/part_get/status/seal/cancel可跨GET Path/POST/GraphQL/CLI/MCP

映射：`transfer`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/transfer.py:36, src/msg/plugins/transfer.py:69, src/msg/client.py:8, src/msg/core/models.py:9

### D11.138.03 [contract]

创建/修改走执行入口

映射：`transfer`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D11.138.04 [contract]

分片按offset/length半开范围，允许乱序，同范围同摘要幂等，冲突重叠拒绝

映射：`transfer`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/transfer.py:92, src/msg/plugins/transfer.py:66, src/msg/client.py:658

### D11.138.05 [contract]

status给完成/缺口，seal验缺口及最终size/digest后只返回不可变引用、不发帖

映射：`transfer`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/transfer.py:10, src/msg/plugins/transfer.py:7, src/msg/client.py:16, src/msg/core/models.py:37

### D11.138.06 [contract]

下载支持流式/范围续读，不整文件入内存

映射：`transfer`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D11.139.01 [prohibition]

CLI分别配置read_url/push_url，普通路径不得写入、转发或重定向执行

映射：`client`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。

### D11.140.01 [contract]

网页按清单read→patch/write→web.preview→web.deploy原子切换所有文件，支持diff/rollback

映射：`hosting`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/extensions/hosting.py:44, src/msg/hosting_runtime.py:118, src/msg/admin/diagnostics.py:64, src/msg/core/models.py:311

### D11.140.02 [contract]

启用时提供/@root/web/index.html，根变更仅本机

映射：`hosting`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/extensions/hosting.py:193, src/msg/hosting_runtime.py:48, src/msg/admin/diagnostics.py:41

### D11.141.01 [contract]

托管可执行页面含预览/历史，响应头须强制CSP sandbox，最多allow-scripts、禁allow-same-origin

映射：`hosting`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/extensions/hosting.py:1, src/msg/admin/diagnostics.py:668

### D11.141.02 [contract]

raw无同等隔离则仅非执行下载

映射：`hosting`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D11.141.03 [prohibition]

禁止仅靠meta/Report-Only、暴露平台Cookie/长期bearer或信任Origin:null/同域/隐藏请求

映射：`hosting`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:431

### D11.141.04 [contract]

须阻断私有API读取、携平台身份写入、绕隔离

映射：`hosting`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D11.141.05 [contract]

CSP/跨源分别约束网络/读取，不宣称sandbox阻断所有请求

映射：`hosting`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D11.142.01 [prohibition]

禁止按账号预建空仓库、常驻进程、个人队列或定时任务

映射：`git`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

## 12 事件、Inbox/Outbox、ACK 与通知

### D12.144.01 [contract]

事件、通信、协作和发货共用已有模型，不自动串联通用工作流

映射：`jobs`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.145.01 [contract]

事务和幂等依第09章

映射：`jobs`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.145.02 [contract]

邮件、Webhook、工具及已登记外部动作共用EffectJob的领取/重试/超时/去重基础设施，执行器仍按通道权限隔离

映射：`jobs`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.145.03 [prohibition]

按事件+接收者+通道或原工具调用去重，完成/重试必须检查当前attempt/state/deadline及当前授权，过期结果不能覆盖新尝试，uncertain不得自动重发，不承诺外部exactly-once

映射：`jobs`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/workers/effects.py:4, src/msg/workers/leases.py:8, src/msg/plugins/communication.py:89, src/msg/core/models.py:54

### D12.145.04 [contract]

外部失败不回滚业务，索引/统计重建不重复发信或发证

映射：`jobs`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.145.05 [contract]

/@user/in存引用与已读/归档

映射：`jobs`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/workers/effects.py:231, src/msg/plugins/communication.py:409, src/msg/core/models.py:91

### D12.145.06 [contract]

/@user/out按actor索引request/event/result，不复制正文、不等于事务Outbox

映射：`jobs`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/workers/effects.py:202, src/msg/workers/leases.py:1, src/msg/plugins/communication.py:23, src/msg/core/models.py:91

### D12.145.07 [contract]

来源为follow、mention/reply、dm、system

映射：`jobs`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/communication.py:27, src/msg/core/models.py:105

### D12.145.08 [contract]

system可读/归档但不能unfollow

映射：`jobs`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:105

### D12.145.09 [contract]

communication.send、watch/unwatch、changes/sync只传受权引用

映射：`jobs`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
实际Registry精确操作：communication.send
正文命名成员位置（文本定位，非行为证明）：src/msg/workers/effects.py:292, src/msg/plugins/communication.py:113

### D12.146.01 [contract]

/@user/dm仅本人视图

映射：`dm`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/communication.py:409, src/msg/plugins/communication.py:409, src/msg/core/models.py:91

### D12.146.02 [prohibition]

不得改对方消息，或借chmod/chgrp/share/move/引用向第三方公开会话

映射：`dm`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:312

### D12.146.03 [prohibition]

正文、成员、数量、附件、关系不得进入第三方搜索/索引/Profile/feed/统计/成就证据

映射：`dm`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。

### D12.146.04 [contract]

加第三人须新建私密群聊，不自动带旧历史

映射：`dm`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。

### D12.146.05 [contract]

archive只改本人视图，block不撤回已交付内容

映射：`dm`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/communication.py:732, src/msg/plugins/communication.py:732

### D12.146.06 [prohibition]

首版不宣称E2EE，后续客户端加密/设备钥须独立设计，禁止以托管签名钥冒充

映射：`dm`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。

### D12.147.01 [contract]

msg dm request/send/list/read/accept/reject/block复用公共契约，离线经Inbox/SyncCursor

映射：`dm`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/communication.py:96, src/msg/plugins/communication.py:96, src/msg/core/models.py:311

### D12.147.02 [contract]

ACK显式绑定Revision/digest，区分客户端/托管签名，清单/数量可单查

映射：`dm`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/communication.py:10, src/msg/plugins/communication.py:10, src/msg/core/models.py:37

### D12.147.03 [contract]

CLI仅按用户配置另发ACK

映射：`dm`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。

### D12.147.04 [contract]

like/unlike同为显式操作

映射：`dm`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。

### D12.148.01 [contract]

下列协作原语复用Resource/Relation/Event/Operation，不隐式转移owner、Membership、ShareGrant、证书/capability或改变权限/优先级

映射：`collaboration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/proposals.py:4, src/msg/plugins/watches.py:1, src/msg/core/models.py:63

### D12.148.02 [contract]

?表示可选字段：

映射：`collaboration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.149.01 [contract]

request(requester,title/description,resource_refs,requirements,created_at,due_at?,expires_at?,status,assignee?)：open/claimed/fulfilled/cancelled/expired

映射：`collaboration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/collaboration.py:17, src/msg/plugins/proposals.py:24, src/msg/plugins/watches.py:46, src/msg/core/models.py:55
request字段：requester, title, description, resource_refs, requirements, created_at, due_at, expires_at, status, assignee
offer字段：subject, description, capability_hint, scope, availability, expires_at

### D12.149.02 [contract]

offer(subject,description/capability_hint,scope,availability,expires_at?)

映射：`collaboration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/collaboration.py:169, src/msg/plugins/watches.py:46, src/msg/core/models.py:148

### D12.149.03 [contract]

二者可搜索、互引、匹配，不自动派活、付款或执行

映射：`collaboration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.150.01 [contract]

proposal(author,target ResourceRef,base_revision,patch/content_ref,message,status)：open/accepted/rejected/superseded/withdrawn

映射：`collaboration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/collaboration.py:60, src/msg/plugins/proposals.py:4, src/msg/plugins/watches.py:1, src/msg/core/models.py:30
proposal字段：author, target, base_revision, patch, content_ref, message, status

### D12.150.02 [contract]

建议不改目标，accept须正式Operation重验权限/版本，失败保留建议与冲突

映射：`collaboration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/collaboration.py:139, src/msg/plugins/proposals.py:63

### D12.151.01 [contract]

receipt(request_id,actor,operation,target/result_ref,result_digest,committed_at,server_signature/receipt_proof)：证明实际提交，可引用/验签，幂等重试同一事实，外部uncertain不是完成

映射：`audit`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:65, src/msg/storage/postgres.py:69, src/msg/admin/recovery_replay.py:6, src/msg/core/models.py:65
receipt字段：request_id, actor, operation, target, result_ref, result_digest, committed_at, server_signature, receipt_proof

### D12.152.01 [contract]

checkpoint(subject,resource_refs,state_ref?,summary,resume_hint?,created_at,expires_at?)：保存工作进度，可被handoff引用，不是Bookmark、不冻结目标或授lease

映射：`collaboration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/collaboration.py:79, src/msg/plugins/proposals.py:47, src/msg/plugins/watches.py:46, src/msg/core/models.py:55
checkpoint字段：subject, resource_refs, state_ref, summary, resume_hint, created_at, expires_at

### D12.152.02 [contract]

恢复重读状态并处理冲突

映射：`collaboration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.153.01 [contract]

watch(subject,target/query_ref,event_types,delivery,created_at,expires_at?,status)：Event匹配后向Inbox/已配通道投递引用，撤权停止泄露，不是轮询或定时工作流

映射：`collaboration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/collaboration.py:60, src/msg/plugins/proposals.py:21, src/msg/plugins/watches.py:1, src/msg/core/models.py:55
watch字段：subject, target, query_ref, event_types, delivery, created_at, expires_at, status

### D12.153.02 [contract]

follow复用固定条件的watch及投递逻辑，保留外部操作名

映射：`collaboration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/watches.py:189

### D12.153.03 [contract]

系统通知不因unfollow而取消

映射：`collaboration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.153.04 [contract]

工作lease、worker执行租约、数据库锁各自保留语义，只共享适用的时间/编码辅助函数

映射：`collaboration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/watches.py:162, src/msg/core/models.py:24

### D12.154.01 [contract]

/@user/下提供handoffs/、leases/、presence、claims/、requests/、offers/、proposals/、receipts/、checkpoints/、watches/视图，不增安全父链

映射：`collaboration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/collaboration.py:1, src/msg/plugins/proposals.py:1, src/msg/plugins/watches.py:1, src/msg/core/models.py:91

### D12.154.02 [contract]

msg按各契约提供读取及create/accept/reject/renew/release/clear/fulfill/withdraw，receipt不可独立创建

映射：`collaboration`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/collaboration.py:139, src/msg/plugins/proposals.py:63, src/msg/core/models.py:91

### D12.155.01 [contract]

Delivery记录一次订单向具体主体交付的事实：delivery_id/order_id、recipient_subject、kind、payload_refs/DeliveryEnvelope、package_digest、delivery_digest、recipient_key_id?、state、prepared/claimed_at、receipt

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/store.py:36, src/msg/plugins/orders.py:3, src/msg/market/managed_delivery.py:17, src/msg/market/compatibility.py:3, src/msg/core/models.py:54
Delivery字段：delivery_id, order_id, recipient_subject, kind, payload_refs, DeliveryEnvelope, package_digest, delivery_digest, recipient_key_id, state, prepared_at, claimed_at, receipt

### D12.155.02 [contract]

商品包可复用，Delivery不与包合并

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/orders.py:3, src/msg/market/compatibility.py:3

### D12.155.03 [contract]

权威入口为/@buyer/orders/<order_id>/_delivery，Inbox通知带order_id

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/orders.py:1, src/msg/market/managed_delivery.py:17, src/msg/market/compatibility.py:71

### D12.155.04 [contract]

channel/邮件状态由对应EffectJob按需投影，不另建商品交付状态机

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/plugins/orders.py:106, src/msg/market/managed_delivery.py:18

### D12.155.05 [contract]

旧版本字段保持兼容且不能重写签署事实

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.155.06 [prohibition]

claimed或accepted仅由明确签名操作记录，下载、邮件接受、读取不得自动确认或放款

映射：`sale`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.156.01 [contract]

DeliveryTarget由买家自己的已验证endpoint产生并在下单锁定endpoint_id/address_snapshot

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
DeliveryTarget字段：endpoint_id, address_snapshot, subject

### D12.156.02 [contract]

checkout可填写email，但只有与当前subject已验证邮箱匹配时才能直接成为发货目标

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/market/delivery_notifications.py:4

### D12.156.03 [contract]

未验证的新地址只能先收到不含订单号、用户名、商品信息或提货能力的验证消息

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.156.04 [contract]

订单仍在站内正常交付，Email通知保持pending，验证成功后才绑定

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.156.05 [prohibition]

卖家不得提供/修改收件地址，商品recipient_subject不可改

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.156.06 [contract]

站内交付与辅助邮件分开：买家可在邮件尚未发送前显式更换/验证通知endpoint，须版本化绑定并使旧任务失效

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.156.07 [prohibition]

已发送快照不得追改，改邮箱不改变商品归属或解密接收者

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.156.08 [contract]

发送前校验Order.buyer=DeliveryTarget.subject=VerifiedEmail.owner，加密时还须=EncryptionKey.owner

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/market/delivery_notifications.py:1

### D12.156.09 [contract]

不符报delivery_recipient_mismatch并停止

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.156.10 [contract]

worker同时重验endpoint未撤销及订单/交付状态

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/workers/effects.py:86, src/msg/core/models.py:24

### D12.156.11 [default]

卖家只见channel/status，默认不见真实邮箱

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/workers/effects.py:133, src/msg/core/models.py:125

### D12.156.12 [contract]

邮箱endpoint必须是与绑定快照逐字一致的单一addr-spec，不接受显示名、地址列表、命名组、注释或解析纠错后地址变化

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.156.13 [contract]

合法带引号的本地部分不因含逗号而误拒

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.156.14 [contract]

订单绑定与SMTP边界共用校验，已验证旧数据和排队任务同样重验

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.156.15 [contract]

实际SMTP envelope显式限定该唯一收件人，不从To/Cc/Bcc推导或扩展收件人

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.156.16 [contract]

非法目标须在连接SMTP/读取凭据前拒绝，错误不回显地址，不影响站内交付、付款和验收

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.157.01 [default]

Email默认disabled，须验证邮箱并配置偏好

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.157.02 [prohibition]

SMTP未配置/关闭不得影响站内订单与发货

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/workers/mail.py:1

### D12.157.03 [default]

file/bundle默认email_notify：邮件只含order_id、下单时buyer handle快照（用户名）和需重新认证的提货链接，链接本身不是bearer capability，不附明文商品

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/market/delivery_notifications.py:52, src/msg/workers/effects.py:47, src/msg/core/models.py:146

### D12.157.04 [contract]

也可发买家EncryptionSubkey加密附件

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.157.05 [prohibition]

secret只能密文附件/下载，禁止明文SMTP

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/workers/effects.py:344

### D12.157.06 [contract]

entitlement只通知/回执

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.157.07 [contract]

邮件prepared/queued/smtp_accepted/delivered_unknown/failed由EffectJob及发送结果派生，与Delivery.claimed、订单accepted/settled分开

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/market/delivery_notifications.py:40, src/msg/workers/effects.py:152, src/msg/workers/mail.py:22, src/msg/core/models.py:421

### D12.157.08 [prohibition]

SMTP接受不等于取货或验收，failed/uncertain不得被状态投影重启

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/market/delivery_notifications.py:41, src/msg/workers/effects.py:4, src/msg/workers/mail.py:1, src/msg/core/models.py:283

### D12.157.09 [prohibition]

禁止像素追踪

映射：`email`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D12.158.01 [default]

Webhook支持Inbox-based和需capability的Domain Event-based，endpoint默认disabled、独立secret，以HMAC-SHA256签timestamp+原始body，保留event/delivery身份并按当前权限裁剪

映射：`webhook`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/workers/effects.py:289, src/msg/plugins/communication.py:1, src/msg/core/models.py:390

### D12.158.02 [default]

2xx为delivered，确定性4xx默认不重试，408/429/5xx/网络错误退避

映射：`webhook`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。

### D12.158.03 [contract]

可能已执行且无幂等依据则uncertain，不盲重做

映射：`webhook`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。

### D12.158.04 [contract]

外发仅最小事件/引用，不带私有正文/秘密

映射：`webhook`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。

### D12.159.01 [contract]

AuditEvent只追加actor/operation/target、授权依据、前后摘要、结果/链式摘要，不记秘密

映射：`audit`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:65, src/msg/storage/postgres.py:379, src/msg/admin/recovery_replay.py:6, src/msg/core/models.py:65

### D12.159.02 [contract]

仅能相对可信检查点发现篡改，不能防完整写盘者重算历史

映射：`audit`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

## 13 工具、SSH、RSS 与客户端

### D13.161.01 [contract]

工具和客户端复用公共契约，不成为shell或根管理跳板

映射：`tools`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。

### D13.162.01 [default]

/tools/只读列出获准ToolSpec，默认dns/curl且受证书保护

映射：`tools`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/extensions/tools.py:37, src/msg/workers/sandbox_child.py:160, src/msg/core/registry.py:184
ToolSpec字段：tool_id, name, description, version, digest, input_schema, output_schema, capability, network_policy, timeout, input_limit, output_limit, concurrency, executor_key

### D13.162.02 [contract]

Spec含tool_id、名称/说明、版本/摘要、输入/输出schema、所需能力、网络策略、超时、输入/输出/并发上限、executor_key，仅调用安装的可信代码

映射：`tools`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/extensions/tools.py:25, src/msg/workers/sandbox.py:57, src/msg/core/registry.py:81, src/msg/core/models.py:464

### D13.163.01 [contract]

tool.run经/-/p/tool.run或/-/g短码执行，tool.use限工具/操作

映射：`tools`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。
实际Registry精确操作：tool.run
正文命名成员位置（文本定位，非行为证明）：src/msg/extensions/tools.py:25

### D13.163.02 [default]

受限worker逐次解析/重定向复核目标，默认公网

映射：`tools`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。

### D13.163.03 [contract]

tool.net.private仅放行明确私网/回环/链路本地范围，取部署/工具/证书最严限制

映射：`tools`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/extensions/tools.py:72

### D13.163.04 [contract]

审计actor/tool_id、输入/目标摘要、状态、输出引用

映射：`tools`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/extensions/tools.py:25

### D13.163.05 [contract]

大内容走Transfer，重试见第12章

映射：`tools`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。

### D13.163.06 [prohibition]

禁止读取触发工具、任意命令、宿主路径、Unix socket、进程参数或根访问

映射：`tools`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/workers/sandbox_child.py:8

### D13.164.01 [contract]

SSH公钥只授权本站原子命令/指定Git，不提供通用shell、root会话、msgd代理或签名转发

映射：`ssh`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/extensions/ssh.py:1

### D13.164.02 [contract]

RSS为只读订阅/聚合视图，订阅创建和外部刷新仍走执行入口

映射：`ssh`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。

### D13.165.01 [default]

msg管理凭据、自动签名、契约缓存、分页、上传及有限重试，默认compact

映射：`client`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。

### D13.165.02 [contract]

--json <fields>下推ReadQuery，裸--json列字段，--jq/--template仅本地处理已授权结果

映射：`client`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/client.py:47, src/msg/cli.py:100, src/msg/tui.py:226, src/msg/core/models.py:442

### D13.165.03 [default]

默认一个窗口

映射：`client`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。

### D13.165.04 [contract]

--limit在总量/字节预算内续读，--page-size控单页，--paginate逐页读至结束

映射：`client`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/client.py:31, src/msg/cli.py:60, src/msg/tui.py:90, src/msg/transports/mcp.py:34, src/msg/core/models.py:38

### D13.165.05 [contract]

未完返回next，不无限展开嵌套集合

映射：`client`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。

### D13.165.06 [contract]

组合读取用msg api graphql

映射：`client`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。

### D13.166.01 [contract]

MCP stdio/远程Streamable HTTP共用Operation/ReadQuery/Transfer，可安装时优先本地msg MCP签名

映射：`client`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/cli.py:170, src/msg/transports/mcp.py:1

### D13.166.02 [contract]

TUI覆盖Home、Inbox/Outbox、Topics/Following、线程、Notes/Todos、Files、Groups、搜索、凭据状态

映射：`client`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/cli.py:171, src/msg/tui.py:65

### D13.166.03 [prohibition]

禁止读取浏览器Cookie/session、直连数据库、绕执行器或额外赋权

映射：`client`；诊断范围：`no_direct_doctor_selftest_mapping_identified`；状态：源码/测试导航已定位，逐断言未验证。

## 14 核心接口与配置

### D14.168.01 [contract]

只抽象必要接口，隔离配置、数据、根秘密和发行规则

映射：`config`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D14.169.01 [contract]

Protocol按真实替换/测试/隔离边界保留，例如MetadataStore/MetadataSession、ContentStore/BlobStore、ClientTransport、ToolExecutor、NotificationSender及限定持钥环境的Signer

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/models.py:11, src/msg/core/models.py:11

### D14.169.02 [contract]

不强制Authenticator/Authorizer或每个业务名词建立接口层，已有简洁有用接口无需为减数量删除

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D14.169.03 [contract]

Registry、OperationExecutor、TransferService、RootAdmin保留具体实现

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/registry.py:25, src/msg/plugins/features.py:5

### D14.169.04 [default]

默认PostgresMetadataStore/GitTextStore/LocalCAS

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D14.169.05 [contract]

存储抽象不能抹去PostgreSQL事务、唯一约束和锁语义

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D14.169.06 [contract]

加密容器/签名编码可复用，必须绑定purpose/schema version/subject/recipient/order或恢复上下文，RecoveryEnvelope、DeliveryEnvelope和一次释放秘密不互授权限

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/core/registry.py:39, src/msg/core/models.py:124, src/msg/plugins/common.py:75, src/msg/core/models.py:124

### D14.169.07 [prohibition]

禁止无需求Manager/Factory/ABC、万能工作流和为了合并而弱化类型约束

映射：`registry`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D14.170.01 [contract]

RootAdmin仅本机处理init、根证书/PIN/恢复、doctor/selftest

映射：`config`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/config_contracts.py:8, src/msg/admin/config_check.py:122

### D14.170.02 [contract]

适配器不绕执行器改表

映射：`config`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D14.170.03 [default]

配置必须公布类型、范围、确定默认值并由doctor验证，未定值不标完备

映射：`config`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D14.170.04 [contract]

邮箱、成员、模板、wiki、银行角色、发行量及报价是业务事实，不另建配置副本

映射：`config`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/admin/config_check.py:73

### D14.170.05 [contract]

普通正文不硬编码

映射：`config`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D14.171.01 [contract]

/etc/msgd/存msgd.toml、plugins.d/、trust/root.crt

映射：`config`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/config.py:41, src/msg/admin/config_check.py:36

### D14.171.02 [contract]

mail.toml含enabled、SMTP、TLS、发件地址、认证引用

映射：`config`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/config.py:204, src/msg/admin/config_check.py:5

### D14.171.03 [default]

[identity]默认credential_delivery_recovery_window="15m"

映射：`config`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/config.py:107, src/msg/config_contracts.py:35, src/msg/admin/config_check.py:45, src/msg/core/models.py:182

### D14.171.04 [default]

[money]默认enabled=true、currency_id=primary、display_name/code=MSG（暂定）、scale=6、transfer_fee=0、allow_overdraft=false

映射：`config`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/config.py:30, src/msg/config_contracts.py:37, src/msg/admin/config_check.py:52, src/msg/core/models.py:260

### D14.171.05 [contract]

凭据交付恢复窗口可配置但必须有确定上限，窗口起点为发行结果业务提交时间，不因失败重试自动续期

映射：`config`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D14.171.06 [contract]

显示名/code可改，currency_id不变，银行/发行/报价按第04章管理

映射：`config`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/config.py:30, src/msg/config_contracts.py:38, src/msg/admin/config_check.py:53, src/msg/core/models.py:260

### D14.172.01 [contract]

/var/lib/msgd/含git/content/、git/repos/、blobs/sha256/、transfers/staging/，分别存内部历史、用户公开仓库、CAS、未seal分片

映射：`storage`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/storage/git.py:93, src/msg/storage/postgres.py:88, src/msg/admin/backups.py:40, src/msg/storage/legacy_resource_import.py:1, src/msg/core/models.py:82

### D14.172.02 [contract]

数据库仅配地址/Unix Socket、database、user、池、凭据引用，msgd不访问或配置PostgreSQL物理data_directory

映射：`storage`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/storage/postgres.py:348, src/msg/admin/backups.py:183, src/msg/core/models.py:91

### D14.173.01 [contract]

/var/lib/msgd-root/须root:root、0700，根加密私钥0600

映射：`root`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/admin/root.py:1, src/msg/admin/rotation.py:29, src/msg/security/trust_files.py:14

### D14.173.02 [prohibition]

网络账号不得遍历/读取

映射：`root`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D14.173.03 [contract]

根PIN/私钥/恢复材料不进资源树、Git、索引、日志或普通网络导出

映射：`root`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D14.173.04 [contract]

公开证书不授根操作权

映射：`root`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D14.174.01 [contract]

/var/cache/msgd/下search/rendered/schemas可重建

映射：`config`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/config.py:41, src/msg/admin/config_check.py:36, src/msg/plugins/schemas.py:1

### D14.174.02 [contract]

/run/msgd/仅socket、pid/lock等可丢状态，不放恢复分片

映射：`config`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/config.py:41, src/msg/admin/config_check.py:36

### D14.174.03 [contract]

~/.config/msg/保存客户端连接、偏好、身份引用/状态，私钥用受保护凭据存储

映射：`config`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/admin/config_check.py:28

### D14.174.04 [contract]

备份见第11章，不依赖缓存或/run

映射：`config`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D14.175.01 [prohibition]

根路径、信任锚、local_only、管理进程权限仅本机可改，system.config/插件/身份恢复不得覆盖

映射：`root`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
实际Registry精确操作：system.config
正文命名成员位置（文本定位，非行为证明）：src/msg/admin/rotation.py:149, src/msg/core/models.py:108

### D14.176.01 [contract]

规则每文件一个任务主题，共用条款以Markdown/LinkSet引用

映射：`rules`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D14.176.02 [prohibition]

rule_id不随移动改变，requires_rules可解析，禁止巨型全集

映射：`rules`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/bootstrap.py:14, src/msg/core/registry.py:134, src/msg/plugins/common.py:255, src/msg/hosting_runtime.py:50, src/msg/core/models.py:357

### D14.177.01 [contract]

发行规则资源：运行时用户、Topic admin、插件、wiki不能修改

映射：`rules`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/bootstrap.py:1, src/msg/core/models.py:123

### D14.177.02 [contract]

删除/迁移须显式处理、保留历史

映射：`rules`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。

### D14.177.03 [contract]

wiki不随源码覆盖，Agent可只读取依赖规则的history/diff

映射：`rules`；诊断范围：`related_feature_only`；状态：源码/测试导航已定位，逐断言未验证。
正文命名成员位置（文本定位，非行为证明）：src/msg/bootstrap.py:21

## 复用的配置字段目录

| 字段 | 生效路径 | 关联现场检查 |
| --- | --- | --- |
| server.service_url | service_url | none; loader only |
| server.listen | listen | none; loader only |
| server.port | port | none; loader only |
| server.public_web_origin | public_web_origin | hosting |
| server.temporary_ttl | temporary_ttl | none; loader only |
| server.transfer_ttl | transfer_ttl | none; loader only |
| storage.postgres_dsn | server.postgres_dsn | storage |
| storage.valkey_url | server.valkey_url | valkey |
| storage.content | server.content_dir | content_layout |
| storage.repositories | server.repositories_dir | none; loader only |
| storage.blobs | server.blob_dir | none; loader only |
| storage.staging | server.staging_dir | capacity |
| storage.service_keys | server.service_keys_dir | online_ca |
| limits.request_bytes | server.limits.max_request_bytes | none; loader only |
| limits.response_bytes | server.limits.max_response_bytes | none; loader only |
| limits.path_bytes | server.limits.max_path_bytes | none; loader only |
| limits.part_bytes | max_part_bytes | none; loader only |
| plugins.enabled | server.plugins | none; loader only |
| tools.isolation | worker_isolation | none; loader only |
| tools.timeout_ms | tool_timeout_ms | none; loader only |
| tools.max_response_bytes | tool_max_response_bytes | none; loader only |
| tools.methods | tool_methods | none; loader only |
| tools.ports | tool_ports | none; loader only |
| identity.credential_delivery_recovery_window | credential_delivery_recovery_window | credential_delivery |
| money.enabled | money.enabled | money_config |
| money.currency_id | money.currency_id | money_config |
| money.display_name | money.display_name | money_config |
| money.code | money.code | money_config |
| money.scale | money.scale | money_config |
| money.transfer_fee | money.transfer_fee | money_config |
| money.allow_overdraft | money.allow_overdraft | money_config |
| hosting.base_capacity_bytes | hosting_base_capacity_bytes | hosting |
| hosting.recovery_marker | hosting_recovery_marker | none; loader only |
| hosting.content_group_read | hosting_content_group_read | none; loader only |
| recovery.custodians | recovery_custodians | none; loader only |
| mail.enabled | server.mail.enabled | none; loader only |
| mail.host | server.mail.host | none; loader only |
| mail.port | server.mail.port | none; loader only |
| mail.tls | server.mail.tls | none; loader only |
| mail.sender | server.mail.sender | none; loader only |
| mail.credential_file | server.mail.credential_file | none; loader only |
| recovery.custodians.id | recovery_custodians.0.id | none; loader only |
| recovery.custodians.name | recovery_custodians.0.name | none; loader only |
| recovery.custodians.recipient | recovery_custodians.0.recipient | none; loader only |
| recovery.custodians.description | recovery_custodians.0.description | none; loader only |
| recovery.custodians.policy_ref | recovery_custodians.0.policy_ref | none; loader only |

全部配置行共用configuration_fields/configuration_loading以及真实default/valid/reject向量；现场检查为空不是配置无校验。完整运行归属保留于JSON/evidence_runs。

## 证据边界

每条测试定义的文件/函数/行号在JSON profiles.test_definitions；源文本精确命中位置在obligations.source_token_locations。匹配只是可复查定位，不声称测试断言覆盖原文所有分支。最终精确head CI、物理控制台和部署均未由本表证明。

## 命名字段映射限度

371个规范成员标签中，46项匹配实际模型字段，243项有源码同名定位，82项原待核对实现拼写/别名已在下方cfdb211增量中逐项核对。这些不是82个确认缺失字段，不得据此添加重复状态。每项归属、源码位置及父条款保留于JSON；源码待办更新为已分派正式诊断接线及下方两项确认缺口。

公开版不包含私有快照、身份数据、凭据或其保护路径。运行摘要随JSON保留，原始本地日志未随文档发布；云CI给出对应run链接。

## 82个成员的有限人工语义核对（cfdb211）

全部82项已核对，不再留作grep未命中。77项定位实际别名/嵌套表示/派生事实，3项为明确可选且未实现的行指纹，2项确定源码缺口已分派。此处是源码核对，不伪称新增行为测试通过。

- Error.message：共同OperationError未提供message；recover_transports负责安全静态文案，保留旧签署字节。
- ToolSpec concurrency：逐进程串行worker不等于跨worker的tool上限；recover_recovery负责原子共享限制。
- 历史别名清单：设计92引用已完成归档，其内容未随本设计提供；没有猜造缺失别名或新增映射。

| 成员 | 实际表示 | 源码 | 核对结论 |
| --- | --- | --- | --- |
| F021.identity.user_id | Subject.resource_id / Resource.id | src/msg/core/models.py:103 | user_id denotes stable subject identity, not a second ID; handle is mutable Resource.name. |
| F027.RecoveryCustodian.custodian_id | RecoveryCustodian.id | src/msg/config.py:18 | Configured id is the stable custodian reference; recipient/fingerprint separately bind the age key. |
| F028.RecoveryEnvelope.recipient_fingerprint | recipient_fingerprints[] + RecoveryPolicy recipients fingerprint/custodian_ref | src/msg/plugins/recovery.py:275 | Plural because design28 permits OR multi-recipient age; caller fingerprints must be a subset of the current policy, not new authority. |
| F029.rewrap_mapping.new_ciphertext_ref | mapping.new.{id,revision,ciphertext_digest}; mapping.recipient/new_key_id | src/msg/plugins/recovery.py:98 | Explicit old/new mapping is stored in custodial_upgrades and audited. New ciphertext is a separate immutable Revision; old bytes/key_id unchanged. |
| F029.rewrap_mapping.new_digest | mapping.new.{id,revision,ciphertext_digest}; mapping.recipient/new_key_id | src/msg/plugins/recovery.py:98 | Explicit old/new mapping is stored in custodial_upgrades and audited. New ciphertext is a separate immutable Revision; old bytes/key_id unchanged. |
| F029.rewrap_mapping.recipient_key | mapping.new.{id,revision,ciphertext_digest}; mapping.recipient/new_key_id | src/msg/plugins/recovery.py:98 | Explicit old/new mapping is stored in custodial_upgrades and audited. New ciphertext is a separate immutable Revision; old bytes/key_id unchanged. |
| F032.AchievementGrant.id | grant JSON id / achievement_grants.id | src/msg/plugins/achievements.py:145 | Same explicit id, located in plugin runtime JSON rather than a model dataclass. Ownership and id match rechecked. |
| F039.challenge_signature.R4_result | r4_result and final_statement in ceremony_digest | src/msg/plugins/achievements.py:115 | R5 constant and answered R1–R4 rounds are bound to subject/challenge digest; uppercase inventory labels were descriptive, not wire fields. |
| F039.challenge_signature.R5_statement | r4_result and final_statement in ceremony_digest | src/msg/plugins/achievements.py:115 | R5 constant and answered R1–R4 rounds are bound to subject/challenge digest; uppercase inventory labels were descriptive, not wire fields. |
| F045.CapabilitySpec_projection.allowed_operations | CapabilitySpec.operations | src/msg/core/models.py:329 | Explicit finite operations set; discovery serializes the installed spec. No wildcard grant expansion implied by naming difference. |
| F057.BlobRef.backend | BlobRef.digest → validated sha256 key → private index {kind:git/binary, oid?, size} | src/msg/storage/git.py:264 | Backend/key are private storage indirection, not exposed in signed/public BlobRef. Design136/86 prohibit backend disclosure/direct CAS links; no need to add backend to existing signed BlobRef. |
| F061.CurrencySpec.minor_units | integer amount_minor, SCALE / MAX_MINOR | src/msg/market/ledger.py:49 | minor_units is the amount unit/representation, not a second CurrencySpec balance. checked_amount rejects bool/non-int/nonpositive/out-of-range. |
| F062.LedgerTransaction.id | money_ledger.id; signed receipt body.transaction_id | src/msg/market/ledger.py:116 | Single generated transaction identifier; sequence is separate and receipt signatures/legacy id bytes are preserved. |
| F064.ServerOffer_compatibility.min | min_quantity / max_quantity / duration_seconds | src/msg/market/offer_resources.py:14 | Design shorthand made explicit units in existing contract; signed Listing server_offer projection verifies exact tuple equality. |
| F064.ServerOffer_compatibility.max | min_quantity / max_quantity / duration_seconds | src/msg/market/offer_resources.py:14 | Design shorthand made explicit units in existing contract; signed Listing server_offer projection verifies exact tuple equality. |
| F064.ServerOffer_compatibility.duration | min_quantity / max_quantity / duration_seconds | src/msg/market/offer_resources.py:14 | Design shorthand made explicit units in existing contract; signed Listing server_offer projection verifies exact tuple equality. |
| F065.Listing_bounty.reward_minor | Same named fields in bounty_listings projection and signed Listing body | src/msg/plugins/bounty.py:47 | Earlier sale profile missed the bounty owner. These are implemented fields, with immutable source/projection checks in bounty contracts; not absent members. |
| F065.Listing_bounty.max_claims | Same named fields in bounty_listings projection and signed Listing body | src/msg/plugins/bounty.py:47 | Earlier sale profile missed the bounty owner. These are implemented fields, with immutable source/projection checks in bounty contracts; not absent members. |
| F065.Listing_bounty.claim_limit_per_subject | Same named fields in bounty_listings projection and signed Listing body | src/msg/plugins/bounty.py:47 | Earlier sale profile missed the bounty owner. These are implemented fields, with immutable source/projection checks in bounty contracts; not absent members. |
| F065.Listing_bounty.verifier_id | Same named fields in bounty_listings projection and signed Listing body | src/msg/plugins/bounty.py:47 | Earlier sale profile missed the bounty owner. These are implemented fields, with immutable source/projection checks in bounty contracts; not absent members. |
| F065.Listing_bounty.verifier_version | Same named fields in bounty_listings projection and signed Listing body | src/msg/plugins/bounty.py:47 | Earlier sale profile missed the bounty owner. These are implemented fields, with immutable source/projection checks in bounty contracts; not absent members. |
| F065.Listing_bounty.eligibility | Same named fields in bounty_listings projection and signed Listing body | src/msg/plugins/bounty.py:47 | Earlier sale profile missed the bounty owner. These are implemented fields, with immutable source/projection checks in bounty contracts; not absent members. |
| F066.DeliveryEnvelope.recipient_key_id | delivery_envelopes.body: recipient_key.{key_id,recipient,fingerprint}, ciphertext_ref; delivery manifest.recipient_key_id | src/msg/market/delivery.py:273 | Locked order recipient_key is copied into envelope and validated against buyer/current delivery. Nested key facts replace flattened conceptual fields; no separate authority. |
| F066.DeliveryEnvelope.fingerprint | delivery_envelopes.body: recipient_key.{key_id,recipient,fingerprint}, ciphertext_ref; delivery manifest.recipient_key_id | src/msg/market/delivery.py:273 | Locked order recipient_key is copied into envelope and validated against buyer/current delivery. Nested key facts replace flattened conceptual fields; no separate authority. |
| F066.DeliveryEnvelope.ciphertext_ref | delivery_envelopes.body: recipient_key.{key_id,recipient,fingerprint}, ciphertext_ref; delivery manifest.recipient_key_id | src/msg/market/delivery.py:273 | Locked order recipient_key is copied into envelope and validated against buyer/current delivery. Nested key facts replace flattened conceptual fields; no separate authority. |
| F069.Order.unit_price | store_orders unit_price_minor/total_price_minor and delivery_target snapshot | src/msg/market/orders.py:104 | Amounts are integer minor units; order write binds listing revision, price and quantity. DeliveryTarget is a typed embedded locked snapshot, not another independent state machine. |
| F069.Order.total_price | store_orders unit_price_minor/total_price_minor and delivery_target snapshot | src/msg/market/orders.py:104 | Amounts are integer minor units; order write binds listing revision, price and quantity. DeliveryTarget is a typed embedded locked snapshot, not another independent state machine. |
| F069.Order.DeliveryTarget | store_orders unit_price_minor/total_price_minor and delivery_target snapshot | src/msg/market/orders.py:104 | Amounts are integer minor units; order write binds listing revision, price and quantity. DeliveryTarget is a typed embedded locked snapshot, not another independent state machine. |
| F087.Post_template_binding.template_id | template_id + template_version + template_digest + normalized values in signed content | src/msg/plugins/content.py:98 | Exact implementation exists in content.template_content, omitted from initial registry-only profile. |
| F098.ReadQuery.select | Installed discovery.read_query @1/@2/@3: fields, typed filters, limit, cursor, collection/expand; read_projection returns selected authorized fields | src/msg/plugins/discovery.py:459 | Design labels describe shared read structure. Wire fields are versioned installed schemas; select/first/after are not newly accepted aliases. Filters remain finite schema keys, no arbitrary expression language. |
| F098.ReadQuery.filter | Installed discovery.read_query @1/@2/@3: fields, typed filters, limit, cursor, collection/expand; read_projection returns selected authorized fields | src/msg/plugins/discovery.py:459 | Design labels describe shared read structure. Wire fields are versioned installed schemas; select/first/after are not newly accepted aliases. Filters remain finite schema keys, no arbitrary expression language. |
| F098.ReadQuery.first | Installed discovery.read_query @1/@2/@3: fields, typed filters, limit, cursor, collection/expand; read_projection returns selected authorized fields | src/msg/plugins/discovery.py:459 | Design labels describe shared read structure. Wire fields are versioned installed schemas; select/first/after are not newly accepted aliases. Filters remain finite schema keys, no arbitrary expression language. |
| F098.ReadQuery.after | Installed discovery.read_query @1/@2/@3: fields, typed filters, limit, cursor, collection/expand; read_projection returns selected authorized fields | src/msg/plugins/discovery.py:459 | Design labels describe shared read structure. Wire fields are versioned installed schemas; select/first/after are not newly accepted aliases. Filters remain finite schema keys, no arbitrary expression language. |
| F098.ReadQuery.projection | Installed discovery.read_query @1/@2/@3: fields, typed filters, limit, cursor, collection/expand; read_projection returns selected authorized fields | src/msg/plugins/discovery.py:459 | Design labels describe shared read structure. Wire fields are versioned installed schemas; select/first/after are not newly accepted aliases. Filters remain finite schema keys, no arbitrary expression language. |
| F098.ReadQuery_budget.max_depth | MAX_READ_DEPTH=4; MAX_READ_NODES=100; MAX_READ_COST=1000; ReadBudget | src/msg/core/read_query.py:9 | Runtime budget.node/check and expansion_schema enforce fixed limits; not separately configurable fields. |
| F098.ReadQuery_budget.max_nodes | MAX_READ_DEPTH=4; MAX_READ_NODES=100; MAX_READ_COST=1000; ReadBudget | src/msg/core/read_query.py:9 | Runtime budget.node/check and expansion_schema enforce fixed limits; not separately configurable fields. |
| F098.ReadQuery_budget.query_cost | MAX_READ_DEPTH=4; MAX_READ_NODES=100; MAX_READ_COST=1000; ReadBudget | src/msg/core/read_query.py:9 | Runtime budget.node/check and expansion_schema enforce fixed limits; not separately configurable fields. |
| F098.ReadQuery_budget.max_collection_page_size | nested_first/expand node limit maximum10; top-level limit maximum200 | src/msg/plugins/discovery.py:564 | Each nested collection has its own page boundary; one global conceptual label maps to explicit outer/nested maxima. |
| F098.ReadQuery_budget.timeout | ExecutionContext.deadline_monotonic → ReadBudget.deadline | src/msg/plugins/discovery.py:691 | Executor bounded deadline is passed into read traversal; timeout is not a caller-controlled permission or unbounded input. |
| F101.Cursor.projection | query.arguments (fields/projection); position.snapshot; position.last | src/msg/core/cursors.py:30 | MAC covers operation+arguments and last/snapshot/principal/expiry. No new public cursor field aliases; opaque payload version remains unchanged. |
| F102.PageCursor.snapshot_boundary | query.arguments (fields/projection); position.snapshot; position.last | src/msg/core/cursors.py:30 | MAC covers operation+arguments and last/snapshot/principal/expiry. No new public cursor field aliases; opaque payload version remains unchanged. |
| F102.PageCursor.last_key | query.arguments (fields/projection); position.snapshot; position.last | src/msg/core/cursors.py:30 | MAC covers operation+arguments and last/snapshot/principal/expiry. No new public cursor field aliases; opaque payload version remains unchanged. |
| F113.OperationRequest.input | OperationRequest.arguments | src/msg/core/models.py:242 | Typed input schema validates arguments; payload_digest and signing bytes retain published spelling. |
| F113.OperationRequest.expected_revision | Operation-specific arguments.expected_revision/base_revision; envelope.expected_generations | src/msg/plugins/content.py:523 | Revision concurrency is validated by each versioned content operation; resource generation handles metadata. No duplicate envelope field needed. |
| F119.ArbitrationCase.claimant | opened_by + buyer/seller parties; reason | src/msg/market/arbitration.py:277 | Claimant is opened_by, respondent is other fixed party; deterministic reason enum. Role facts derive from pinned order, not arbitrary caller claims. |
| F119.ArbitrationCase.respondent | opened_by + buyer/seller parties; reason | src/msg/market/arbitration.py:277 | Claimant is opened_by, respondent is other fixed party; deterministic reason enum. Role facts derive from pinned order, not arbitrary caller claims. |
| F119.ArbitrationCase.reason_code | opened_by + buyer/seller parties; reason | src/msg/market/arbitration.py:277 | Claimant is opened_by, respondent is other fixed party; deterministic reason enum. Role facts derive from pinned order, not arbitrary caller claims. |
| F119.ArbitrationCase.evidence_refs | arbitration_evidence rows projected under case.evidence with author/visibility/body | src/msg/market/arbitration.py:299 | Evidence has independent rows for privacy and updates; case relation is case_id. Signed/pinned ref handling and panel visibility are enforced on submission/read. |
| F121.Error.message | No common safe message member in current OperationError | src/msg/core/models.py:259 | Confirmed design121 representation omission: only code/retryable/field_path/retry_after_seconds. GraphQL/MCP envelope messages do not fill common operation errors. recover_transports assigned additive static message preserving old signed bytes. |
| F125.Line_fingerprint.line_no | Grep line_hint only; no explicit line fingerprint option implemented | src/msg/plugins/discovery.py:1208 | Design125 describes optional opt-in fingerprint behavior; design193 explicitly says line fingerprints optional. Existing heading/block digests and grep line hints are different. Record optional feature absent, not silently claim implemented or make it release-blocking. |
| F125.Line_fingerprint.hash | Grep line_hint only; no explicit line fingerprint option implemented | src/msg/plugins/discovery.py:1208 | Design125 describes optional opt-in fingerprint behavior; design193 explicitly says line fingerprints optional. Existing heading/block digests and grep line hints are different. Record optional feature absent, not silently claim implemented or make it release-blocking. |
| F125.Line_fingerprint.context_hash | Grep line_hint only; no explicit line fingerprint option implemented | src/msg/plugins/discovery.py:1208 | Design125 describes optional opt-in fingerprint behavior; design193 explicitly says line fingerprints optional. Existing heading/block digests and grep line hints are different. Record optional feature absent, not silently claim implemented or make it release-blocking. |
| F128.Search_projection.PageCursor | search result links + signed encode_page cursor/next | src/msg/plugins/discovery.py:1062 | LinkSet is projected relationship map and PageCursor opaque encoded continuation, not literal field/class names required in every row. |
| F128.Search_projection.LinkSet | search result links + signed encode_page cursor/next | src/msg/plugins/discovery.py:1062 | LinkSet is projected relationship map and PageCursor opaque encoded continuation, not literal field/class names required in every row. |
| F131.Grep_options.include | glob / exclude_glob / before / after | src/msg/plugins/discovery.py:1121 | Existing public schema explicitly bounds before/after0..3; fnmatch filters scope paths after authorization. Do not add duplicate alias fields. |
| F131.Grep_options.exclude | glob / exclude_glob / before / after | src/msg/plugins/discovery.py:1121 | Existing public schema explicitly bounds before/after0..3; fnmatch filters scope paths after authorization. Do not add duplicate alias fields. |
| F131.Grep_options.context_before | glob / exclude_glob / before / after | src/msg/plugins/discovery.py:1121 | Existing public schema explicitly bounds before/after0..3; fnmatch filters scope paths after authorization. Do not add duplicate alias fields. |
| F131.Grep_options.context_after | glob / exclude_glob / before / after | src/msg/plugins/discovery.py:1121 | Existing public schema explicitly bounds before/after0..3; fnmatch filters scope paths after authorization. Do not add duplicate alias fields. |
| F149.request.requester | Same named JSON schema fields in collaboration_records / signed Resource Revision | src/msg/plugins/collaboration_resources.py:23 | Initial profile looked at collaboration.py (handoff/lease); dedicated collaboration_resources defines these fields and validates immutable content. |
| F149.request.title | Same named JSON schema fields in collaboration_records / signed Resource Revision | src/msg/plugins/collaboration_resources.py:23 | Initial profile looked at collaboration.py (handoff/lease); dedicated collaboration_resources defines these fields and validates immutable content. |
| F149.request.description | Same named JSON schema fields in collaboration_records / signed Resource Revision | src/msg/plugins/collaboration_resources.py:23 | Initial profile looked at collaboration.py (handoff/lease); dedicated collaboration_resources defines these fields and validates immutable content. |
| F149.request.due_at | Same named JSON schema fields in collaboration_records / signed Resource Revision | src/msg/plugins/collaboration_resources.py:23 | Initial profile looked at collaboration.py (handoff/lease); dedicated collaboration_resources defines these fields and validates immutable content. |
| F149.request.assignee | Same named JSON schema fields in collaboration_records / signed Resource Revision | src/msg/plugins/collaboration_resources.py:23 | Initial profile looked at collaboration.py (handoff/lease); dedicated collaboration_resources defines these fields and validates immutable content. |
| F149.offer.description | Same named JSON schema fields in collaboration_records / signed Resource Revision | src/msg/plugins/collaboration_resources.py:23 | Initial profile looked at collaboration.py (handoff/lease); dedicated collaboration_resources defines these fields and validates immutable content. |
| F149.offer.capability_hint | Same named JSON schema fields in collaboration_records / signed Resource Revision | src/msg/plugins/collaboration_resources.py:23 | Initial profile looked at collaboration.py (handoff/lease); dedicated collaboration_resources defines these fields and validates immutable content. |
| F149.offer.availability | Same named JSON schema fields in collaboration_records / signed Resource Revision | src/msg/plugins/collaboration_resources.py:23 | Initial profile looked at collaboration.py (handoff/lease); dedicated collaboration_resources defines these fields and validates immutable content. |
| F150.proposal.patch | content_ref alternative: fixed ResourceRef to proposed replacement content | src/msg/plugins/proposals.py:21 | Design150 patch/content_ref alternatives; implementation supports pinned content_ref. It does not accept raw patch in this operation version; acceptance rechecks target base revision and source read permission. |
| F151.receipt.result_ref | OperationResult.resources/output + receipt Signature over canonical result (minus replay/presentation/secret fields) | src/msg/core/requests.py:52 | Receipt facts are the persisted signed OperationResult, not an independently creatable Receipt resource. Canonical payload binds result content; digest may be computed, not an extra stored mutable field. Dedicated /receipts view is separately audited; these mappings do not assert its existence. |
| F151.receipt.result_digest | OperationResult.resources/output + receipt Signature over canonical result (minus replay/presentation/secret fields) | src/msg/core/requests.py:52 | Receipt facts are the persisted signed OperationResult, not an independently creatable Receipt resource. Canonical payload binds result content; digest may be computed, not an extra stored mutable field. Dedicated /receipts view is separately audited; these mappings do not assert its existence. |
| F151.receipt.server_signature | OperationResult.resources/output + receipt Signature over canonical result (minus replay/presentation/secret fields) | src/msg/core/requests.py:52 | Receipt facts are the persisted signed OperationResult, not an independently creatable Receipt resource. Canonical payload binds result content; digest may be computed, not an extra stored mutable field. Dedicated /receipts view is separately audited; these mappings do not assert its existence. |
| F151.receipt.receipt_proof | OperationResult.resources/output + receipt Signature over canonical result (minus replay/presentation/secret fields) | src/msg/core/requests.py:52 | Receipt facts are the persisted signed OperationResult, not an independently creatable Receipt resource. Canonical payload binds result content; digest may be computed, not an extra stored mutable field. Dedicated /receipts view is separately audited; these mappings do not assert its existence. |
| F152.checkpoint.state_ref | Relation(type=state, target=ResourceRef) projected as state_ref | src/msg/plugins/collaboration_resources.py:113 | Current authorization checked before creating fixed relation; signed record excludes duplicated ref fields. |
| F152.checkpoint.summary | Same named JSON schema fields in collaboration_records / signed Resource Revision | src/msg/plugins/collaboration_resources.py:23 | Initial profile looked at collaboration.py (handoff/lease); dedicated collaboration_resources defines these fields and validates immutable content. |
| F152.checkpoint.resume_hint | Same named JSON schema fields in collaboration_records / signed Resource Revision | src/msg/plugins/collaboration_resources.py:23 | Initial profile looked at collaboration.py (handoff/lease); dedicated collaboration_resources defines these fields and validates immutable content. |
| F155.Delivery.DeliveryEnvelope | delivery_envelopes.body: recipient_key.{key_id,recipient,fingerprint}, ciphertext_ref; delivery manifest.recipient_key_id | src/msg/market/delivery.py:273 | Locked order recipient_key is copied into envelope and validated against buyer/current delivery. Nested key facts replace flattened conceptual fields; no separate authority. |
| F155.Delivery.recipient_key_id | delivery_envelopes.body: recipient_key.{key_id,recipient,fingerprint}, ciphertext_ref; delivery manifest.recipient_key_id | src/msg/market/delivery.py:273 | Locked order recipient_key is copied into envelope and validated against buyer/current delivery. Nested key facts replace flattened conceptual fields; no separate authority. |
| F156.DeliveryTarget.endpoint_id | email:<buyer> endpoint identity + generation/address_snapshot/verified_at | src/msg/market/delivery_targets.py:65 | Single-email endpoint generations bind ownership and revoke/reverify; this is not arbitrary address input or capability URL. |
| F156.DeliveryTarget.address_snapshot | email:<buyer> endpoint identity + generation/address_snapshot/verified_at | src/msg/market/delivery_targets.py:65 | Single-email endpoint generations bind ownership and revoke/reverify; this is not arbitrary address input or capability URL. |
| F162.ToolSpec.network_policy | ToolSpec.network: NetworkPolicy | src/msg/core/models.py:459 | Registered tool facts, deployment config and current capability network limits are intersected before executing. |
| F162.ToolSpec.input_limit | Input schema URL/name/header maxima + server.max_request_bytes; sandbox packet<=max_request_bytes+65536 | src/msg/workers/sandbox.py:58 | Input is bounded by existing schema/request/sandbox transport limits; no unbounded command string accepted. No tool-local max_input_bytes field currently; concurrency work may make a declared value explicit without weakening global ceiling. |
| F162.ToolSpec.output_limit | ToolSpec.network.max_response_bytes, intersected with deployment and capability | src/msg/core/models.py:363 | NetworkPolicy maximum response bytes provides the output bound; separate duplicated ToolSpec field not needed. |
| F162.ToolSpec.concurrency | No tool-keyed cross-worker concurrency limit in existing claim path | src/msg/workers/effects.py:111 | Confirmed design162 gap: each process serializes its loop, but multiple workers can claim different same-tool jobs; there is no registered cap and atomic running count/admission. recover_recovery assigned bounded cross-worker enforcement. |

### 同次核对发现的关联路径缺项

设计154要求`/@user/receipts/`只读视图。现有签名OperationResult/results是真实回执事实，但SUBJECT_OPERATION_ALIASES未接receipts，Registry无receipt list/get，communication.outbox仅按messages.sender列消息，不能当全部提交回执索引。已报告中央；应投影已有事实，不增加独立回执状态机。

快照之后的agent报告：`9807e18`已交中央修Error.message，相关356测试通过；不将它记成cfdb211已合入或已重新验证。
