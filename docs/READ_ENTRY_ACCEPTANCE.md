# Read entry acceptance evidence (#78)

This maps supported contracts at commit `2eb1ea3` to executable tests. It is
not a claim that every requirement in #78 has been completed. Parameter semantics
are tested at their shared handler; dispatcher equivalence needs representative
signed packets and continuations, not every possible combination of predicates.

The four dispatchers are POST `/-/p/<operation>`, GET
`/-/g/<operation>/<encoding>/<packet>`, GraphQL query `/_read/graphql`, and MCP
HTTP `/-/mcp`. CLI/stdio have separate real-client coverage in
`test_share_transport_matrix.py` and `test_read_projection_cache_transports.py`.

| Supported contract | Existing semantic evidence (under `tests/`) |
| --- | --- |
| ReadQuery v1: parent/type/author/query/tag/state predicates; id/time/name ordering and asc/desc; fields, bounded page | `test_read_query_cursor.py`, `test_path_read_query.py`; shared predicates in `plugins/read_predicates.py` |
| ReadQuery v2 flat children/replies expansion, v3 recursive independently paged children/replies; no arbitrary nested predicates | `test_nested_read_query.py`, `issue_78_80/test_read_query_tree.py` |
| Page cursor binds principal/query/version/snapshot; current parent and child authorization; expiry and downgrade rejection | `test_read_query_cursor.py`, `issue_78_80/test_read_query_tree.py`, `issue_78_80/test_tree_query_ref.py` |
| ReadQuery depth/node/cost/response byte/deadline limits, including aggregate branches | `issue_78_80/test_read_query_tree.py::test_depth_and_response_budget`, `test_read_deadline`, `test_global_node_budget_covers_all_branches` |
| Search all/any/exact/exclusion, body/name/metadata; relevance/updated/created/name ordering, snippet/explain and explicit facets | `test_lexical_search_grep.py`, `test_search_source_relation_filters.py`, new `test_read_entry_contract_matrix.py` (any/exact/exclusion) |
| Search v3 source/relation filters; v5 current revision/source version, directional relations, presence predicates, typed scopes, title alias | `test_search_source_relation_filters.py`; older version schemas remain closed |
| Search cursor/current readable corpus, private snippet/facet/rank suppression | `test_lexical_search_grep.py::test_facets_use_all_currently_readable_matches_and_recheck_old_cursor`, `test_search_source_relation_filters.py::test_v3_old_cursor_rechecks_grants_before_items_facets_and_rank` |
| Sync v1: event sequence order, limit 1–100, encrypted known-ref cursor; no custom sort/filter | `test_sync_cursor.py`, `test_pagination.py` |
| Sync revocation/retention requires resync; >64 known refs use explicit checkpoint/ack writes, ordinary Sync remains read-only | `test_sync_checkpoint.py`, `test_sync_cursor.py::test_sync_over_64_refs_fails_without_advancing_cursor` |
| Compact path aliases and sealed QueryRef produce the same contracts; independent nested continuations | `test_path_read_query.py`, `test_query_ref.py`, `issue_78_80/test_tree_query_ref.py` |
| Successful/failed reads have no business mutation; current permission beats cache | `test_route_effect_matrix.py`, `test_read_projection_cache_transports.py` |

## Added dispatcher gap coverage

`test_read_entry_contract_matrix.py` adds four cases in a single disposable
installation fixture family, using actual HTTP/GraphQL/MCP adapters and real
signed requests:

- ReadQuery v1 and v3 combine type/author/text filtering, descending ordering,
  narrow fields and a one-item page. Both pages must contain exactly the two
  matching records, in order.
- Search v5 combines a typed scope, any/exact/exclusion/type/author filters, explicit order,
  fields and a one-item page. The continuation must exhaust exactly the two
  matches, without duplicate or excluded records. Separate decoys prove both
  exact and exclusion predicates affect membership.
- Sync v1 checks bounded first/continuation pages and cursor advancement.
  Unsupported user sort/filter is rejected rather than silently ignored.
- The exact same signed packet is submitted to each dispatcher without changing
  legacy arguments or resigning. Entire operation results must match.
- All four dispatchers reject over-limit pages identically. ReadQuery v1 retains
  its established `query_cost_exceeded`; closed newer schemas return
  `schema_validation`. V3 also exercises runtime `response_too_large` identically.
- Every public database table is unchanged after success and failure. Content
  mutations, job notifications, mail/webhook sends and sandbox launches are
  forbidden during these reads.

The matrix establishes dispatch preservation. It does not replace dedicated
nested budget, permission, cursor expiry, checkpoint, or lexical predicate tests,
and does not assert nonexistent Sync sort/filter semantics.

This evidence does not independently exhaust each scalar ordering enum (for
example ReadQuery time and Search updated), every timestamp boundary, or every
nested shape at every dispatcher. Such combinations use the same validated
packet/handler; their absence is not evidence of a transport-specific defect.
