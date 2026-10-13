//! Native Git/blob content storage, compatible with Python's private.git,
//! index, binary, pins and revisions layout. Synchronous and Unix-only.
//!
//! This is a trusted storage port, not an authorization boundary or an executor.
//! The caller supplies quotas and holds the metadata writer fence while coupling
//! mutations to metadata. Wait for an in-flight call to finish before releasing
//! that fence; abandoning an async blocking task is not cancellation.
//!
//! Files and Git refs are durable before successful return. This is NOT an
//! atomic metadata/content commit or a durable cross-store recovery journal.
//! Errors may leave unreferenced content, which must not be blindly deleted.
mod files;
mod git;
#[cfg(test)]
mod tests;
use crate::records::{encode, BlobRef, Revision};
use files::{io, Temporary};
use msg_core::{Error, Json, Result};
use serde::{Deserialize, Serialize};
use std::{
    fs,
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    time::Duration,
};

/// Administrator-supplied configuration, never deserialized from a request.
#[derive(Clone)]
pub struct ContentOptions {
    pub binary_dir: Option<PathBuf>,
    pub staging_dir: Option<PathBuf>,
    pub group_read: bool,
    pub git_binary: PathBuf,
    pub git_timeout: Duration,
}
impl Default for ContentOptions {
    fn default() -> Self {
        Self {
            binary_dir: None,
            staging_dir: None,
            group_read: false,
            git_binary: "/usr/bin/git".into(),
            git_timeout: Duration::from_secs(120),
        }
    }
}
pub struct GitContentStore {
    root: PathBuf,
    repo: PathBuf,
    index: PathBuf,
    binary: PathBuf,
    staging: PathBuf,
    options: ContentOptions,
}
#[derive(Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase", deny_unknown_fields)]
enum Entry {
    Git { oid: String, size: u64 },
    Binary { size: u64 },
}
impl Entry {
    fn size(&self) -> u64 {
        match self {
            Self::Git { size, .. } | Self::Binary { size } => *size,
        }
    }
}
impl GitContentStore {
    /// Initialize or open a content store in protected, service-owned paths.
    /// Existing directory permissions are never repaired implicitly. Parent
    /// directories must already exist; shared layouts require explicit migration.
    pub fn open(root: &Path, options: ContentOptions) -> Result<Self> {
        if !options.git_binary.is_absolute() || options.git_timeout.is_zero() {
            return Err(Error("invalid_content_configuration"));
        }
        let root = files::absolute(root)?;
        let store = Self {
            repo: root.join("private.git"),
            index: root.join("index"),
            binary: files::absolute(
                options
                    .binary_dir
                    .as_deref()
                    .unwrap_or(&root.join("binary")),
            )?,
            staging: files::absolute(
                options
                    .staging_dir
                    .as_deref()
                    .unwrap_or(&root.join("staging")),
            )?,
            root,
            options,
        };
        // Do not chmod/create anything before rejecting an existing bad layout.
        for path in [&store.root, &store.index, &store.binary] {
            if fs::symlink_metadata(path).is_ok() {
                files::check_directory(path, store.options.group_read)?;
            }
        }
        for path in [&store.repo, &store.staging] {
            if fs::symlink_metadata(path).is_ok() {
                files::check_directory(path, false)?;
            }
        }
        files::directory(&store.root, store.options.group_read)?;
        files::directory(&store.staging, false)?;
        if store.repo.exists()
            && store.options.group_read
            && store.git(&["config", "--local", "core.sharedRepository"], None)? != "0640"
        {
            return Err(Error("content_permission_migration_required"));
        }
        files::directory(&store.index, store.options.group_read)?;
        files::directory(&store.binary, store.options.group_read)?;
        if !store.repo.exists() {
            let mut command = store.command();
            command.args([
                "-c",
                "init.templateDir=",
                "init",
                "--bare",
                "--object-format=sha1",
            ]);
            if store.options.group_read {
                command.arg("--shared=0640");
            }
            command.arg(&store.repo);
            store.run(&mut command, None, 65_536)?;
            files::sync_dir(&store.root)?;
        }
        store.layout()?;
        if store.git(&["rev-parse", "--is-bare-repository"], None)? != "true" {
            return Err(Error("invalid_content_repository"));
        }
        Ok(store)
    }
    fn layout(&self) -> Result<()> {
        for path in [
            &self.root,
            &self.index,
            &self.binary,
            &self.staging,
            &self.repo,
        ] {
            files::absolute(path)?;
            files::check_directory(path, false)?;
        }
        Ok(())
    }
    fn entry(&self, blob: &BlobRef) -> Result<Entry> {
        self.layout()?;
        let key = files::key(&blob.digest)?;
        if blob.size < 0 {
            return Err(Error("invalid_blob_size"));
        }
        let bytes = files::bounded(&self.index.join(key), 4096)?;
        let json =
            Json::parse(std::str::from_utf8(&bytes).map_err(|_| Error("invalid_content_index"))?)?;
        let entry: Entry =
            serde_json::from_str(&json.canonical()?).map_err(|_| Error("invalid_content_index"))?;
        if entry.size() != blob.size as u64 {
            return Err(Error("content_size_mismatch"));
        }
        if let Entry::Git { oid, .. } = &entry {
            files::oid(oid)?;
        }
        Ok(entry)
    }
    /// Stream into private staging. A wrong expected digest or source error
    /// cannot publish an index/ref. `limit` is the caller's admitted byte quota.
    pub fn put(
        &self,
        source: &mut impl Read,
        media_type: &str,
        expected: Option<&str>,
        limit: u64,
    ) -> Result<BlobRef> {
        self.layout()?;
        if let Some(expected) = expected {
            files::key(expected)?;
        }
        let mut staged = Temporary::new(&self.staging)?;
        let (digest, size) = files::stream(source, &mut staged.file, limit)?;
        if expected.is_some_and(|expected| expected != digest) {
            return Err(Error("digest_mismatch"));
        }
        staged.file.sync_all().map_err(io)?;
        let key = files::key(&digest)?;
        let entry = if media_type.starts_with("text/")
            || ["application/json", "application/msg-template"].contains(&media_type)
        {
            staged.rewind()?;
            let mut output = self.run(
                self.command().args(["hash-object", "-w", "--stdin"]),
                Some(staged.file.try_clone().map_err(io)?),
                128,
            )?;
            let mut oid = String::new();
            output.file.read_to_string(&mut oid).map_err(io)?;
            let oid = files::oid(oid.trim())?.to_owned();
            self.ref_set(&format!("refs/staging/{key}"), &oid)?;
            Entry::Git { oid, size }
        } else {
            let destination = self.binary.join(key);
            if fs::symlink_metadata(&destination).is_ok() {
                files::verified_copy(
                    &mut files::open(&destination)?,
                    &mut std::io::sink(),
                    &digest,
                    size,
                )?;
            } else {
                // Create on the destination filesystem; this also inherits the
                // intended setgid directory rather than the staging directory's gid.
                let mut published = Temporary::new(&self.binary)?;
                staged.rewind()?;
                files::verified_copy(&mut staged.file, &mut published.file, &digest, size)?;
                published.permissions(self.options.group_read)?;
                if !published.install(&destination)? {
                    files::verified_copy(
                        &mut files::open(&destination)?,
                        &mut std::io::sink(),
                        &digest,
                        size,
                    )?;
                }
            }
            Entry::Binary { size }
        };
        files::replace(
            &self.index.join(key),
            encode(&entry)?.canonical()?.as_bytes(),
            self.options.group_read,
        )?;
        Ok(BlobRef {
            digest,
            size: size as i64,
            media_type: media_type.into(),
        })
    }
    pub fn put_bytes(
        &self,
        bytes: &[u8],
        media_type: &str,
        expected: Option<&str>,
    ) -> Result<BlobRef> {
        let mut reader = bytes;
        self.put(&mut reader, media_type, expected, bytes.len() as u64)
    }
    fn snapshot(&self, blob: &BlobRef) -> Result<Temporary> {
        let entry = self.entry(blob)?;
        let mut snapshot = Temporary::new(&self.staging)?;
        match entry {
            Entry::Binary { .. } => {
                files::verified_copy(
                    &mut files::open(&self.binary.join(files::key(&blob.digest)?))?,
                    &mut snapshot.file,
                    &blob.digest,
                    blob.size as u64,
                )?;
            }
            Entry::Git { oid, .. } => {
                snapshot = self.run(
                    self.command().args(["cat-file", "blob", &oid]),
                    None,
                    blob.size as u64,
                )?;
                files::verified_copy(
                    &mut snapshot.file,
                    &mut std::io::sink(),
                    &blob.digest,
                    blob.size as u64,
                )?;
            }
        }
        snapshot.rewind()?;
        Ok(snapshot)
    }
    /// Deliver from a verified private snapshot using 64 KiB buffers. Integrity
    /// is checked before any bytes reach the sink, even for an empty/small range.
    /// Snapshot disk usage is O(blob size), memory O(1); the caller enforces
    /// concurrency/disk quotas. A failed sink may have accepted a partial range.
    pub fn read_to(
        &self,
        blob: &BlobRef,
        range: Option<(u64, u64)>,
        sink: &mut impl Write,
    ) -> Result<()> {
        if blob.size < 0 {
            return Err(Error("invalid_blob_size"));
        }
        let (start, end) = range.unwrap_or((0, blob.size as u64));
        if start > end || end > blob.size as u64 {
            return Err(Error("invalid_byte_range"));
        }
        let mut snapshot = self.snapshot(blob)?;
        snapshot.file.seek(SeekFrom::Start(start)).map_err(io)?;
        let copied =
            std::io::copy(&mut (&mut snapshot.file).take(end - start), sink).map_err(io)?;
        if copied != end - start {
            return Err(Error("content_truncated"));
        }
        Ok(())
    }
    pub fn read_bytes(&self, blob: &BlobRef, limit: u64) -> Result<Vec<u8>> {
        if blob.size < 0 {
            return Err(Error("invalid_blob_size"));
        }
        if blob.size as u64 > limit {
            return Err(Error("use_transfer"));
        }
        let mut bytes = Vec::new();
        self.read_to(blob, None, &mut bytes)?;
        Ok(bytes)
    }
    fn pin_path(&self, blob: &BlobRef, lease: &str, create: bool) -> Result<PathBuf> {
        let root = self.root.join("pins");
        let parent = root.join(files::hex(lease.as_bytes()));
        if create {
            files::directory(&root, false)?;
            files::directory(&parent, false)?;
        }
        files::absolute(&parent)?;
        Ok(parent.join(files::key(&blob.digest)?))
    }
    pub fn pin(&self, blob: &BlobRef, lease: &str) -> Result<()> {
        // Verify that a pin retains precisely the promised bytes, not just a
        // well-formed index pointing to a corrupt or different object.
        self.snapshot(blob)?;
        match self.entry(blob)? {
            Entry::Git { oid, .. } => self.ref_set(
                &format!(
                    "refs/pins/{}/{}",
                    files::hex(lease.as_bytes()),
                    files::key(&blob.digest)?
                ),
                &oid,
            ),
            Entry::Binary { .. } => {
                files::replace(&self.pin_path(blob, lease, true)?, b"1\n", false)
            }
        }
    }
    pub fn unpin(&self, blob: &BlobRef, lease: &str) -> Result<()> {
        match self.entry(blob)? {
            Entry::Git { .. } => self
                .git(
                    &[
                        "update-ref",
                        "--no-deref",
                        "-d",
                        &format!(
                            "refs/pins/{}/{}",
                            files::hex(lease.as_bytes()),
                            files::key(&blob.digest)?
                        ),
                    ],
                    None,
                )
                .map(|_| ()),
            Entry::Binary { .. } => {
                let path = self.pin_path(blob, lease, false)?;
                match fs::symlink_metadata(&path) {
                    Ok(meta) if meta.file_type().is_symlink() || !meta.is_file() => {
                        Err(Error("unsafe_content_path"))
                    }
                    Ok(_) => {
                        fs::remove_file(&path).map_err(io)?;
                        files::sync_dir(path.parent().ok_or(Error("unsafe_content_path"))?)
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
                    Err(e) => Err(io(e)),
                }
            }
        }
    }
    pub fn pinned(&self, blob: &BlobRef, lease: &str) -> Result<bool> {
        match self.entry(blob)? {
            Entry::Git { oid, .. } => {
                // show-ref's missing-ref status is distinct from a corrupt index;
                // check existence via for-each-ref, which succeeds for no matches.
                let reference = format!(
                    "refs/pins/{}/{}",
                    files::hex(lease.as_bytes()),
                    files::key(&blob.digest)?
                );
                let text = self.git(
                    &[
                        "for-each-ref",
                        "--format=%(refname) %(objectname)",
                        &reference,
                    ],
                    None,
                )?;
                Ok(text
                    .lines()
                    .any(|line| line == format!("{reference} {oid}")))
            }
            Entry::Binary { .. } => match files::bounded(&self.pin_path(blob, lease, false)?, 2) {
                Ok(bytes) if bytes == b"1\n" => Ok(true),
                Ok(_) => Err(Error("invalid_content_pin")),
                Err(Error("content_missing")) => Ok(false),
                Err(e) => Err(e),
            },
        }
    }
    /// Commit canonical revision bytes and content to the existing topic refs.
    /// The domain layer still validates signatures, parent authority and quotas.
    /// Existing IDs are immutable: a same-manifest retry reuses the stored commit;
    /// conflicting reuse fails instead of overwriting history.
    pub fn commit_revision(&self, topic: &str, revision: &Revision) -> Result<String> {
        files::segment(&revision.id)?;
        if revision.parents.len() > 64 {
            return Err(Error("too_many_revision_parents"));
        }
        for parent in &revision.parents {
            files::segment(parent)?;
        }
        self.snapshot(&revision.content)?;
        let manifest = encode(revision)?.canonical()?;
        let manifest_oid =
            self.git(&["hash-object", "-w", "--stdin"], Some(manifest.as_bytes()))?;
        files::oid(&manifest_oid)?;
        let mut tree_input = format!("100644 blob {manifest_oid}\tmanifest.json\n");
        if let Entry::Git { oid, .. } = self.entry(&revision.content)? {
            tree_input.push_str(&format!("100644 blob {oid}\tcontent\n"));
        }
        let tree = self.git(&["mktree"], Some(tree_input.as_bytes()))?;
        files::oid(&tree)?;
        let directory = self.root.join("revisions");
        files::directory(&directory, false)?;
        let parents: Vec<String> = revision
            .parents
            .iter()
            .map(|parent| {
                let bytes = files::bounded(&directory.join(parent), 128).map_err(|e| {
                    if e == Error("content_missing") {
                        Error("revision_content_missing")
                    } else {
                        e
                    }
                })?;
                Ok(files::oid(
                    std::str::from_utf8(&bytes)
                        .map_err(|_| Error("invalid_content_index"))?
                        .trim(),
                )?
                .to_owned())
            })
            .collect::<Result<_>>()?;
        let path = directory.join(&revision.id);
        let mut commit = match files::bounded(&path, 128) {
            Ok(bytes) => files::oid(
                std::str::from_utf8(&bytes)
                    .map_err(|_| Error("invalid_content_index"))?
                    .trim(),
            )?
            .to_owned(),
            Err(Error("content_missing")) => {
                let mut command = self.command();
                command.args(["commit-tree", &tree]);
                for parent in &parents {
                    command.args(["-p", parent]);
                }
                // Deterministic native commits; Python-created existing commits
                // retain their original wall-clock timestamps and object IDs.
                let date = revision.created_at.0.to_rfc3339();
                command
                    .env("GIT_AUTHOR_DATE", &date)
                    .env("GIT_COMMITTER_DATE", &date);
                let mut input = Temporary::new(&self.staging)?;
                writeln!(input.file, "{}", revision.id).map_err(io)?;
                input.rewind()?;
                let mut output =
                    self.run(&mut command, Some(input.file.try_clone().map_err(io)?), 128)?;
                let mut text = String::new();
                output.file.read_to_string(&mut text).map_err(io)?;
                files::oid(text.trim())?.to_owned()
            }
            Err(e) => return Err(e),
        };
        self.check_commit(&commit, &tree, &parents)?;
        let reference = format!(
            "refs/topics/{}/{}/{}",
            files::hex(topic.as_bytes()),
            files::hex(revision.resource_id.as_bytes()),
            files::hex(revision.id.as_bytes())
        );
        self.ref_set(&reference, &commit)?;
        let mut mapping = Temporary::new(&directory)?;
        writeln!(mapping.file, "{commit}").map_err(io)?;
        mapping.permissions(false)?;
        if !mapping.install(&path)? {
            let existing = files::bounded(&path, 128)?;
            commit = files::oid(
                std::str::from_utf8(&existing)
                    .map_err(|_| Error("invalid_content_index"))?
                    .trim(),
            )?
            .to_owned();
            self.check_commit(&commit, &tree, &parents)?;
            self.ref_set(&reference, &commit)?;
        }
        Ok(commit)
    }
    fn check_commit(&self, commit: &str, tree: &str, parents: &[String]) -> Result<()> {
        let body = self.git(&["cat-file", "commit", files::oid(commit)?], None)?;
        let headers: Vec<&str> = body.split("\n\n").next().unwrap_or("").lines().collect();
        let actual: Vec<&str> = headers
            .iter()
            .filter_map(|line| line.strip_prefix("parent "))
            .collect();
        if headers.first() != Some(&format!("tree {tree}").as_str())
            || actual != parents.iter().map(String::as_str).collect::<Vec<_>>()
        {
            return Err(Error("revision_content_conflict"));
        }
        Ok(())
    }
}
