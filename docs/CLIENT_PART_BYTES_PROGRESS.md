# Client resumable chunk size — progress

Refs #80, #82, #83, #85. Independent narrow follow-up to integration PR #176;
not part of #177's server cursor implementation.

Base head: `45470b5a46bccfc4f12ef27acccecc2f3bafce47`.
Base tree: `e75e7cf511a89d72bfdccf5ec39567824b569275`.
Original `src/msg/client.py` blob: `326ad17f226db0909387f13c8847ede2a48dc40b`.

When resuming from an existing journal, `MsgClient.download(part_bytes=0)` skips
`transfer.open` validation and computes zero-length reads. A successful empty
read leaves the offset unchanged, so the loop never progresses and keeps
rewriting the journal. Other invalid local sizes can access files or fail with
unrelated schema/type errors. Upload validates too late as well.

Regression-first tests use a real signed download stopped before its second
part, then exercise the saved journal. The zero-size regression allows two real
zero-byte requests before a deterministic guard stops the original loop. It does
not rely on a timeout or mock a successful server response. Invalid inputs must
fail before requests or local state changes. Valid resume is tested over HTTP,
PathGET, GraphQL and MCP with exact bytes and current server read-only evidence.

## Cloud regression evidence

- Regression head `cbd4f4f8642e50a10366fd5fe150a4cbd28052a0`.
- Run `36561926956`, job `109384609384`, Python `3.15.0rc2`.
- Actual checkout `fd6bfce96dea97da81c7679953c5433896a64967` has the identical
  tree `b37543923fb4a7ca1503be757fe1f184f44a2749`.
- 23 JUnit cases: 14 failed, 9 passed, zero errors/skips.
- All seven invalid-size resumed-download cases and seven pre-I/O upload cases
  reproduce the intended boundary defects. The zero-size failure records two
  successful real zero-byte reads at offset 3 before the third-call guard.
- All four valid cross-protocol resume cases and five existing client cases pass.
- Downloaded artifact `11029863272`; independently verified ZIP SHA256:
  `b28e2465340371a16e11307adb0a238bbc4ab7bd861edf650aec7e9a1a30e5f8`.
  source.json and the JUnit failures were inspected; no test correction needed.

## Implementation

Exactly two runtime lines: require a positive non-bool integer at the beginning
of `MsgClient.upload` and `MsgClient.download`, before source hashing, journals,
partial files or network calls. Invalid input raises `invalid_part_bytes`.
All existing source bytes remain unchanged apart from those inserted lines;
modified client blob `cffffa10ccbd62249f0ad991e7c81a3cd146cdf6` was independently
matched to the locally prepared two-line diff. No server/schema/packaging changes.

Final exact-head focused and complete cloud verification pending. No tests run
locally; local operations only edit/compare source text and hash/parse artifacts.
No broad issue closure, production mutation, external delivery or unrelated
source overwrite. PR #178 targets the integration branch and remains draft until
its final checks pass.
