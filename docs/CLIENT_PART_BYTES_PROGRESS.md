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

Status: regression and focused GitHub Actions workflow prepared; cloud failure
and implementation evidence pending. No tests run locally. No broad issue
closure, production mutation, external delivery or unrelated source overwrite.
