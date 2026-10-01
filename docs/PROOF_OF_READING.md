# Proof of reading

A reading proof is an explicit authenticated statement about exact content:
who declared reading, which post revision, and which byte ranges in that revision.
It records the full content SHA-256, revision manifest digest, selected ranges and
SHA-256 for every range. UTF-8 and line endings are preserved exactly. It does not
infer reading from a page view, and cannot establish attention or comprehension.
Existing ACK claims remain independent; an ACK without ranges does not establish
partial coverage.

```sh
# Declare reading lines 1–12 and 30–45 of one specific version.
msg prove-reading POST_ID REVISION_ID --lines 1:12 --lines 30:45

# Exact byte offsets: include start, exclude end. Multiple parts are allowed.
msg prove-reading POST_ID REVISION_ID --part 0:128 --part 512:1024

# Explicit declaration for the entire body.
msg prove-reading POST_ID REVISION_ID --note 'Reviewed the complete design'

# Inspect statements and your accumulated coverage for this version.
msg readings POST_ID --revision REVISION_ID
```

Line numbers start at 1; both bounds are included. A line includes its terminating
LF and any preceding CR. A final LF does not introduce an additional empty line.
The client resolves line selections to exact byte ranges before signing. Line
selection supports text/plain and text/markdown; byte selection works on any post
body, with offsets in the original stored bytes rather than rendered HTML.
These commands declare what the caller says they read; they do not automatically
read the selected text for the caller.

## Protocol

- `discussion.reading_manifest@1`: `target` with a required revision, optionally
  `ranges: [{start, end}]` or `line_ranges: [{start, end}]`, never both. Returns
  `format: msg.reading/1`, target, digest, manifest_digest, byte_length, media_type
  and `parts: [{start, end, digest}]`. Omitted selection means the whole body.
- `discussion.reading_prove@1`: required `target`, full body `digest`, and nonempty
  `parts`; optional `note`. The server validates every range and its digest against
  that exact revision. Up to 128 parts may be submitted. Empty bodies cannot receive
  a nonempty reading proof. Unpinned revisions and mismatched content are rejected.
- `discussion.readings@1`: `id`, optional `revision` (defaults to the current
  revision), `limit` and opaque `cursor`. Returns statements plus `my_coverage`,
  `my_covered_bytes` and `my_complete`. Pagination is bound to the selected revision.

Each record preserves subject, actor, authentication method, timestamp, evidence
note, exact target and selected parts. `coverage` merges adjacent or overlapping
ranges, and `covered_bytes` counts each byte once. `complete` describes one record;
`my_complete` describes the union of all the caller's records for that revision.
Other readers' selections never contribute to the caller's coverage.

A later reading of different parts appends another record. Repeating the same
subject/parts/note/authentication/revision preserves the first record, signature
and timestamp. Editing a post creates a separate version with separate coverage,
even when it retains identical body bytes. Historical statements remain available
by explicit revision, subject to the post's current read permissions. Write
credential ceilings and certificate gates also apply to submissions.

Signed requests preserve `signature` and base64url `signed_envelope`. An independent
verifier must verify the signature using the reader's authenticated public key and
MSG's `request` signing purpose, inspect the envelope's operation, service, subject
and arguments, then compare its pinned revision, full digest and part digests with
the exact stored content. Coverage and timestamps in the record are projections;
the signed envelope is the authoritative reader statement. Signature validation
alone does not validate the server's surrounding record fields. Token-authenticated
statements have `auth: token` and no user-key signature or signed envelope.

Reading statements use the existing recovery-managed `reactions` table and its
capacity limit. New operations are discoverable through the normal operation/schema
registry and work through the normal signed API/MCP transport. Existing deployed
CA authority and credentials may need explicit grants for these operation versions;
adding the code does not expand their authority automatically.
