# Template and tool registration boundary

The installed Registry indexes `TemplateSpec` and `ToolSpec` by resource ID and
explicit contract version. These maps reject unknown lookups and duplicates,
snapshot nested data, and stop accepting additions after freeze. They do not
change the serialized model fields, published operation versions, or short codes.
Tool operation and input/output schema references are checked at freeze.

The content plugin registers the shipped template declarations from the existing
bootstrap source. The template version is the DSL header version; renderer version
1 identifies the finite, non-executable renderer. Fields and defaults are frozen
metadata. Creating or updating a user's template still writes Resource/Revision
facts. Runtime parsing and version resolution remain authoritative for those
resources. Registered fields are reused only when the selected revision's digest
exactly matches the installed declaration; later or user-created revisions keep
their own fields, defaults, digest, and template relation.

The tools plugin registers the existing DNS and HTTP descriptors alongside their
schemas. A runtime tool revision must match the installed executor and schemas.
Its pinned revision and network limits remain resource facts; authorization,
certificate ceilings, deployment limits, and worker rechecks still apply. Registry
registration supplies no permission and does not execute resource content.

Focused coverage is in `tests/test_registry_template_tools.py`, alongside the
existing tool invocation/worker, schema ownership, and Registry reference tests.
This boundary does not assert completion of the broader chapter 15 acceptance
matrix or production verification.
