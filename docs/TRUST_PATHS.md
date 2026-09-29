# Public Root trust paths and reserved plugin directory

New initialization writes `trust/root.crt`. Its content is the existing version 1
signed JSON Root certificate envelope, not PEM/X.509. Certificate signatures,
purposes and wire versions are unchanged. Existing installations containing only
`trust/root.json` continue reading and writing that path without migration.

If both paths exist, ordinary readers require equal canonical JSON bytes after
strict parsing. Whitespace and object key order may differ; values may not. A
conflict fails with `root_trust_alias_mismatch`; neither path takes precedence over
a conflicting value. Symlinks, including dangling ones, are refused. Canonical
encoding is the project's `msg.core.codec.canonical`, not generic JSON equality.

Root rotation updates every existing alias using durable replacement, retaining
the existing PIN-protected, signed rotation journal until all copies finish.
Two path replacements are not one filesystem transaction: interruption can leave
old/new copies, which ordinary readers reject. Explicit local rotation resume
accepts each copy only if it equals the old or new trust bound by that journal,
checks the database anchor and signatures, then finishes all copies. An unrelated
third value is rejected before database mutation. Do not manually overwrite one
alias to repair a mismatch; use the approved rotation recovery procedure.

Initialization and example configuration create `plugins.d/` with mode 0755.
Legacy configurations without this directory remain valid. If present, it must
be a directory, must not be a symlink, and must not be group/other writable.
It is reserved: its contents are neither imported nor executed, and no fragment
configuration language is defined. Plugin selection remains `[plugins].enabled`
in `server.toml`; plugin code and contracts remain source registered.
