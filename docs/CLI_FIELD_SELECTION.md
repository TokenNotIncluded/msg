# CLI read field selection

`msg call` supports an explicit `--contract-version` (default `1`, preserving
existing calls). `--json` is opt-in and uses the selected server Registry
contract. It is not a client-side truncation of a full business response.

Discover ReadQuery v3 fields without executing the business query:

```sh
msg call discovery.read_query --contract-version 3 --json
```

Select a bounded page using signed server-side projection:

```sh
msg call discovery.read_query '{"parent":"/main","query_version":3,"limit":20}' \
  --contract-version 3 --json id,name,path
```

The unchanged JSON result envelope retains the cursor/next links and status.
For the next page, send only the returned cursor at the same contract version,
without repeating `--json`; the server-bound cursor retains the projection:

```sh
msg call discovery.read_query '{"cursor":"SERVER_RETURNED_CURSOR"}' --contract-version 3
```

A bare `--json` lists the `fields.items.enum` from the exact registered input
schema and does not enumerate resources. Contracts without this declared field
vocabulary return `json_fields_unavailable`; the client does not guess fields
from one object's data or import the server runtime. Non-read contracts return
`json_read_required` before business execution. Existing `fields`, a cursor, or
`--return-field` with `--json` returns `json_query_conflict`. Unknown, repeated,
empty, or whitespace-altered field names return `invalid_json_fields`.
Server schema failures retain the normal error result and nonzero exit status.

## Optional local output processing

Both options require explicit nonempty `--json FIELDS` on the first `msg call`
read; they are mutually exclusive. Bare field discovery cannot be combined
with formatting. A canonical `discovery.read_query` continuation can instead
format its cursor-bound fields without a new `--json` projection:

```sh
msg call discovery.read_query '{"cursor":"SERVER_RETURNED_CURSOR"}' \
  --contract-version 3 --jq '.data.items[] | .name'
```

The continuation must contain only `cursor`, preserve the original contract
version, and omit `--json` and `--return-field`. The client verifies the exact
read-only operation/version from Registry again, then signs the unchanged
cursor arguments. The server rechecks the cursor and current access; the client
does not infer or change its projection or automatically follow another page.
Other operations and mixed cursor/query arguments are rejected before network
access. Schema errors remain normal error results without formatting.

Only the returned authorized JSON envelope is processed, after a successful read.
Server error envelopes remain unchanged and return a nonzero status.

```sh
msg call discovery.read_query '{"parent":"/main","query_version":3,"limit":20}' \
  --contract-version 3 --json id,name --jq '.data.items[] | .name'
msg call discovery.read_query '{"parent":"/main","query_version":3,"limit":1}' \
  --contract-version 3 --json id,name --template 'Name: {{/data/items/0/name}}'
```

`--jq` uses an installed `jq` executable directly with an argument vector, never
a shell. It produces compact JSON values, one per line. No executable gives
`jq_unavailable`; invalid filters give `invalid_jq_query`. The process receives
an empty environment and an empty temporary working/module directory. Filters
containing the words `import`, `include`, or `modulemeta` are conservatively
rejected, even inside quoted strings or comments: module loading is unsupported.
No other command-line options or filenames are passed to jq. Query text is
limited to 4 KiB; input and output to 1 MiB; execution to 2 seconds. Output is
bounded while streaming, and overflow/timeout kills and waits for the child.
Errors are stable codes and never include jq diagnostics or private input.
These are I/O and elapsed-time limits, not a general resource sandbox: jq
intermediate allocations remain subject to the host's process memory limits.
Run only filters you intentionally choose; server content is always data.

`--template` supports only `{{JSON_POINTER}}` placeholders. JSON Pointer follows
object keys and array indices, never Python attributes: `/data/items/0/name`
selects that JSON value; `~1` escapes `/`, and `~0` escapes `~` in an object key.
`{{}}` selects the entire result. Values are substituted as JSON, so a string
is quoted and its terminal control characters are escaped. Literal template
text is preserved; the CLI appends a newline. There are no loops, expressions,
attribute traversal, template files or external access. Malformed placeholders,
invalid escapes, and missing pointers give `invalid_output_template`, not empty
success. Template text is limited to 4 KiB and the result to 1 MiB.

Field selection does not add automatic pagination or change authentication and
signing. Existing `read --field` and `search --fields` keep their own projection
options. The result envelope is unchanged: ordinary commands use readable text
on a terminal and JSON in a pipe; `--format json` explicitly requests JSON.
`call --json`, `--jq` and `--template` retain their explicit formatting behavior.
See [connections and output modes](CLIENT_CONNECTIONS.md).

## Test prerequisites

The real formatter regression tests require `jq` on `PATH`; absence fails rather
than skipping those assertions. The signed HTTP/read-only test additionally
requires the standard disposable PostgreSQL fixture from `CONTRIBUTING.md`.
