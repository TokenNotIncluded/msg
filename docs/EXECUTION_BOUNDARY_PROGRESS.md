# Execution boundary progress

## Scope and coordination

- Base: `476e64f04ed1d9c7dcab4a41c4f3cdb72faad6a7`, tree `40ca207a3c49be347120e058e47ed328ec4e3098`.
- Branch: `fix/execution-boundaries-20260929`; coordination: #83 comment 5878058356.
- Primary issues: #156 execution-kernel dependencies; #160 tool runner contract. Shared wire ownership is a slice of #158, not complete client packaging.
- Do not modify #155's concurrent recovery branch. Do not touch production Root/PIN, deployment, funds, outbound delivery or recovery promotion.

## Evidence ledger

1. Main CI 36472507327 succeeded. Source inspected from artifact 10992168743; downloaded SHA-256 `9abbc69988b8ffcb45f9931dc87e291145a7680f275bf7aadbedb7bed2189d10` matches the provider digest. This is a baseline, not evidence for new code.
2. This commit adds regression tests and a focused cloud workflow only. It is deliberately expected to fail against the unchanged implementation. No new test result is claimed yet.
3. All project execution is delegated to GitHub Actions. The existing full four-shard/node-ID, conformance, packaging and recovery gates remain unchanged.

## Dependency inventory before the change

`core.executor -> plugins.batch -> transports.packet -> plugins.schemas`

`core.executor -> plugins.discovery / plugins.communication`, plus the whole `Application` back-reference for limits, projections and webhook enqueueing.

`workers.effects -> workers.sandbox -> workers.effects.ToolResult`; the declared `ToolExecutor.invoke(...) -> ResourceRef` is not the actual callable runner and conflates local output with worker publication.

## Planned ownership

Bounded request/result envelopes and batch parsing will have one neutral core owner, with compatibility re-exports. Request-derived event IDs keep identical canonical bytes. The executor receives explicit size, result-projection and transactional event-enqueue dependencies; no concrete plugin imports or Application service locator. These are fixed trusted collaborators, not configurable before/after workflows.

A neutral `ToolRunner` callable and `ToolResult` own the real isolated-runner contract. The worker retains all current-authority, output-path, policy, attempt/state/deadline, quarantine and transactional-publication checks. The unused mismatched ToolExecutor declaration will be removed, not emulated by a second lifecycle.

## Completion criteria

- Obtain red cloud evidence before implementation.
- Preserve all existing semantic tests and signed/shortcode/API versions.
- Confirm production, SSH and isolated test assembly use explicit collaborators.
- Run focused regressions and complete final-head CI; inspect artifacts and review before any merge.
- Close only issues whose actual acceptance criteria are met. #158/#83/#85 and production-evidence blockers remain open unless separately established.
