# Legacy Git preservation and private import

The old service's bare repositories are separate assets from its SQLite posts.
Neither SQLite preservation nor the content importer imports them. This workflow
preserves reachable Git history and imports it into a private v4 repository without
copying source hooks, repository configuration, filesystem credentials or authority.

## Fixed-ref capture

Use the filename-only source inventory to prepare a private JSON descriptor with
one `bare_repo_paths` entry. The source path stays in that protected file.

```sh
python -m msg.storage.legacy_git_capture \
  --ssh-host APPROVED_SSH_ALIAS \
  --private-source-descriptor /protected/source.json \
  --protected-target /protected/new-capture
```

The target directory must already exist with mode `0700`; output bundle and manifest
are created exclusively with mode `0600`. The source is accessed through authenticated
SSH and `sudo -n python3`, using read-only Git commands with optional locks, hooks,
fsmonitor, global/system config and replacement objects disabled. No source file is
created and no service is changed. The remote process enumerates all refs, rejects
anything outside heads/tags or an empty set, captures their explicit names, streams
the bundle directly to the protected local file, and verifies that refs and symbolic
HEAD did not change. Local bundle headers must exactly match the captured ref/OID
map. A concurrent change or unsupported ref fails rather than producing an accepted
manifest.

Local verification initializes an isolated verification repository under protected
storage, runs `git bundle verify`, imports objects and refs, and runs full strict
`git fsck`. The manifest binds bundle SHA-256, ref-map digest and default branch.
Output contains only counts and digests, never ref names, filenames or source text.
The scope is **all objects reachable from the captured heads/tags**, not reflogs,
unreachable objects, hooks, config, external LFS objects or separate submodule repos.
Preserve the old repository until explicit retention/retirement approval.

## Root-approved private import

`git_approval` constructs a payload binding bundle digest, ref-map digest, default
branch, destination service, parent ID/generation, existing registered operator,
new repository name and expiry within 24 hours. Sign its canonical JSON with the
existing destination Root and purpose `legacy-git-private-import-v1`. Store it as an
envelope containing `approval` and wire-format `signature`.

```sh
python -m msg.storage.legacy_git_import \
  --config /isolated/etc \
  --bundle /protected/repository.bundle \
  --signed-approval /protected/git-approval.json
```

Loading config loads that destination installation and its normal release-source
synchronization; it is not a read-only preflight. The approved parent must be owned
by the operator with mode `0700`. The new repo is `0600`; the native store never creates a
public Git-daemon export marker for this import. Old author strings and Git signatures remain in
unchanged Git objects, without asserting their validity as new service identities.
All branch/tag object IDs and the default branch must match the signed mapping.

The importer reuses `NativeGitStore.create`, `import_bundle` and `update_refs` inside
the normal metadata writer transaction, checks repository capacity and live recovery
state/write policy, and records the Root approval and ref map. Only a new, unique
repository is allowed. A failure rolls back metadata and removes that new physical
repository, including already-published refs; existing repositories are never removed
or overwritten. No old tasks, hooks/config, credentials or grants are activated.

Limits are 32 MiB per bundle, 128 heads/tags and 100,000 reachable objects. SHA-256
Git object format is unsupported by the current native store; SHA-1 object IDs are
preserved. LFS pointers and gitlink/submodule references are counted and cause import
to fail until a separate external-object migration is supplied, rather than claiming
complete preservation from a Git-only bundle. A signed owner can list refs through
the normal `git.refs` operation. Anonymous access is denied. Existing native clone
HTTP is public-only, so a private import is not an implicitly published clone URL.

## Test-only rehearsal

```sh
python -m msg.storage.legacy_git_rehearsal \
  --bundle /protected/repository.bundle \
  --manifest /protected/manifest.json \
  --protected-target /protected/new-rehearsal
```

This creates an isolated PostgreSQL cluster with no TCP listener, a new test-only
Root and registered operator, then imports the actual protected bundle into a private
namespace. Those identities never approve a production migration. Every data artifact
stays under the private target directory; `/tmp` targets are rejected. The final
`summary.json` (`0600`) contains counts/digests and checks for source immutability,
identical object IDs/refs, authenticated access, anonymous denial, unchanged credential/
certificate/job counts and cluster shutdown. The cluster is stopped and retained
privately for inspection. A successful rehearsal proves mechanical compatibility,
not production ownership mapping, publication authorization or deployment readiness.
