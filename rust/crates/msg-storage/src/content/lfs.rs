//! Shared LFS bytes. Repository hard links remain the authorization roots.
//!
//! Publication, migration and collection require the caller's metadata writer
//! fence and current authority. The additional shared-file lock serializes quota
//! accounting with Python workers. Reading never migrates a legacy object.
use super::files::{self, Temporary};
use fs2::FileExt;
use msg_core::{Error, Result};
use std::{
    fs::{self, File, Metadata, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant, SystemTime},
};

#[derive(Clone)]
pub struct LfsObjectStore {
    root: PathBuf,
    shared: PathBuf,
    timeout: Duration,
}

#[derive(Debug, PartialEq, Eq)]
pub struct LfsMigration {
    pub changed: bool,
    pub size: u64,
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct LfsCollection {
    pub objects: u64,
    pub bytes: u64,
}

fn conflict(_: std::io::Error) -> Error {
    Error("lfs_object_conflict")
}
fn oid(value: &str) -> Result<&str> {
    if value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        Ok(value)
    } else {
        Err(Error("invalid_lfs_oid"))
    }
}
fn safe(path: &Path) -> Result<PathBuf> {
    files::absolute(path).map_err(|_| Error("lfs_object_conflict"))
}
fn check_directory(path: &Path) -> Result<()> {
    safe(path)?;
    files::check_directory(path, false).map_err(|_| Error("lfs_object_conflict"))
}
fn directory(path: &Path) -> Result<()> {
    safe(path)?;
    match fs::symlink_metadata(path) {
        Ok(_) => check_directory(path),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            let parent = path.parent().ok_or(Error("lfs_object_conflict"))?;
            directory(parent)?;
            files::directory(path, false).map_err(|_| Error("lfs_object_conflict"))
        }
        Err(e) => Err(conflict(e)),
    }
}
fn exists(path: &Path) -> Result<bool> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(e) => Err(conflict(e)),
    }
}
fn size_valid(size: u64) -> Result<()> {
    if size > i64::MAX as u64 {
        Err(Error("lfs_size_mismatch"))
    } else {
        Ok(())
    }
}
fn copy_verified(path: &Path, digest: &str, size: u64, sink: &mut impl Write) -> Result<Metadata> {
    size_valid(size)?;
    safe(path)?;
    let mut source = files::open(path).map_err(|e| match e.0 {
        "content_missing" => Error("lfs_object_missing"),
        _ => Error("lfs_object_conflict"),
    })?;
    let before = source.metadata().map_err(conflict)?;
    if before.len() != size {
        return Err(Error("lfs_size_mismatch"));
    }
    let (actual, length) = files::stream(&mut source, sink, size).map_err(|e| match e.0 {
        "content_too_large" => Error("lfs_size_mismatch"),
        _ => Error("lfs_object_conflict"),
    })?;
    let after = source.metadata().map_err(conflict)?;
    if length != size {
        return Err(Error("lfs_size_mismatch"));
    }
    if actual != format!("sha256:{digest}") {
        return Err(Error("lfs_digest_mismatch"));
    }
    if !files::unchanged(&before, &after) {
        return Err(Error("lfs_object_conflict"));
    }
    Ok(after)
}

impl LfsObjectStore {
    /// Configure service-owned paths without creating or migrating any object.
    pub fn open(repo: &Path, shared: &Path, timeout: Duration) -> Result<Self> {
        if Instant::now().checked_add(timeout).is_none() {
            return Err(Error("invalid_lfs_lock_timeout"));
        }
        let store = Self {
            root: safe(repo)?.join("lfs/objects"),
            shared: safe(shared)?,
            timeout,
        };
        for path in [repo, &store.root, &store.shared] {
            if exists(path)? {
                check_directory(path)?;
            }
        }
        Ok(store)
    }
    pub fn path(&self, digest: &str) -> Result<PathBuf> {
        let digest = oid(digest)?;
        let path = self
            .root
            .join(&digest[..2])
            .join(&digest[2..4])
            .join(digest);
        safe(&path)
    }
    /// A stat is deliberately read-only. It is not an integrity proof or a grant.
    pub fn size(&self, digest: &str) -> Result<Option<u64>> {
        let path = self.path(digest)?;
        match fs::symlink_metadata(path) {
            Ok(meta) if meta.is_file() && !meta.file_type().is_symlink() => Ok(Some(meta.len())),
            Ok(_) => Err(Error("lfs_object_conflict")),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(conflict(e)),
        }
    }
    /// Verify a private snapshot before emitting any bytes, including a range.
    /// Temporary disk use is O(size); the caller enforces read concurrency limits.
    pub fn read_to(
        &self,
        digest: &str,
        size: u64,
        range: Option<(u64, u64)>,
        sink: &mut impl Write,
    ) -> Result<()> {
        let path = self.path(digest)?;
        size_valid(size)?;
        let (start, end) = range.unwrap_or((0, size));
        if start > end || end > size {
            return Err(Error("invalid_byte_range"));
        }
        if self.size(digest)?.is_none() {
            return Err(Error("lfs_object_missing"));
        }
        // A legacy repository can be read even before a shared store exists.
        let staging = self.root.parent().ok_or(Error("lfs_object_conflict"))?;
        let mut snapshot = Temporary::new(staging).map_err(|_| Error("lfs_object_conflict"))?;
        copy_verified(&path, digest, size, &mut snapshot.file)?;
        snapshot
            .file
            .seek(SeekFrom::Start(start))
            .map_err(conflict)?;
        let copied =
            std::io::copy(&mut (&mut snapshot.file).take(end - start), sink).map_err(conflict)?;
        if copied != end - start {
            return Err(Error("lfs_size_mismatch"));
        }
        Ok(())
    }
    fn lock(&self) -> Result<File> {
        directory(&self.shared)?;
        let path = self.shared.join(".msg-lfs.lock");
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK)
            .open(path)
            .map_err(conflict)?;
        let meta = file.metadata().map_err(conflict)?;
        if !meta.is_file() || meta.nlink() != 1 || meta.mode() & 0o022 != 0 {
            return Err(Error("lfs_object_conflict"));
        }
        let started = Instant::now();
        loop {
            match FileExt::try_lock_exclusive(&file) {
                Ok(()) => return Ok(file),
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    if started.elapsed() >= self.timeout {
                        return Err(Error("server_busy"));
                    }
                    thread::sleep(
                        Duration::from_millis(10)
                            .min(self.timeout - started.elapsed().min(self.timeout)),
                    );
                }
                Err(e) => return Err(conflict(e)),
            }
        }
    }
    /// Approximate without a writer fence; publication calls this under its lock.
    pub fn shared_usage(&self) -> Result<u64> {
        if !exists(&self.shared)? {
            return Ok(0);
        }
        check_directory(&self.shared)?;
        let mut total = 0_u64;
        for entry in fs::read_dir(&self.shared).map_err(conflict)? {
            let path = entry.map_err(conflict)?.path();
            if path
                .file_name()
                .and_then(|s| s.to_str())
                .is_none_or(|s| oid(s).is_err())
            {
                continue;
            }
            let info = fs::symlink_metadata(path).map_err(conflict)?;
            if !info.is_file() || info.file_type().is_symlink() {
                return Err(Error("lfs_object_conflict"));
            }
            total = total
                .checked_add(info.len())
                .ok_or(Error("lfs_deployment_capacity_exceeded"))?;
        }
        Ok(total)
    }
    fn publish_locked(
        &self,
        digest: &str,
        staged: &Path,
        size: u64,
        quota: Option<u64>,
    ) -> Result<bool> {
        let destination = self.path(digest)?;
        let shared = self.shared.join(digest);
        copy_verified(staged, digest, size, &mut std::io::sink())?;
        if exists(&destination)? {
            copy_verified(&destination, digest, size, &mut std::io::sink())?;
        }
        let parent = destination.parent().ok_or(Error("lfs_object_conflict"))?;
        directory(parent)?;
        if fs::metadata(&self.shared).map_err(conflict)?.dev()
            != fs::metadata(parent).map_err(conflict)?.dev()
        {
            return Err(Error("lfs_shared_volume_required"));
        }
        if exists(&shared)? {
            copy_verified(&shared, digest, size, &mut std::io::sink())?;
        } else {
            if let Some(quota) = quota {
                if self
                    .shared_usage()?
                    .checked_add(size)
                    .is_none_or(|n| n > quota)
                {
                    return Err(Error("lfs_deployment_capacity_exceeded"));
                }
            }
            let mut temporary =
                Temporary::new(&self.shared).map_err(|_| Error("lfs_object_conflict"))?;
            copy_verified(staged, digest, size, &mut temporary.file)?;
            temporary.file.sync_all().map_err(conflict)?;
            if !temporary
                .install(&shared)
                .map_err(|_| Error("lfs_object_conflict"))?
            {
                copy_verified(&shared, digest, size, &mut std::io::sink())?;
            }
        }
        let shared_info = fs::symlink_metadata(&shared).map_err(conflict)?;
        if let Ok(info) = fs::symlink_metadata(&destination) {
            if (info.dev(), info.ino()) == (shared_info.dev(), shared_info.ino()) {
                return Ok(false);
            }
        }
        // Replace the repository link atomically. The canonical shared inode is
        // never overwritten, and a legacy ACL root survives every crash point.
        let temporary = Temporary::new(parent).map_err(|_| Error("lfs_object_conflict"))?;
        fs::remove_file(&temporary.path).map_err(conflict)?;
        fs::hard_link(&shared, &temporary.path).map_err(conflict)?;
        fs::rename(&temporary.path, &destination).map_err(conflict)?;
        files::sync_dir(parent).map_err(|_| Error("lfs_object_conflict"))?;
        Ok(true)
    }
    /// A retained object after a metadata rollback is safe; never unlink shared
    /// bytes as compensation for a failed or ambiguously committed publication.
    pub fn publish(
        &self,
        digest: &str,
        staged: &Path,
        size: u64,
        quota: Option<u64>,
    ) -> Result<bool> {
        oid(digest)?;
        size_valid(size)?;
        let _lock = self.lock()?;
        self.publish_locked(digest, staged, size, quota)
    }
    pub fn migrate_legacy(&self, digest: &str, quota: Option<u64>) -> Result<LfsMigration> {
        let source = self.path(digest)?;
        let _lock = self.lock()?;
        let size = self.size(digest)?.ok_or(Error("lfs_object_missing"))?;
        let changed = self.publish_locked(digest, &source, size, quota)?;
        Ok(LfsMigration { changed, size })
    }
    /// The caller must first reconcile pending content work and hold the metadata
    /// writer fence. `index` is the content-store index, not a request parameter.
    pub fn collect_unlinked(&self, older_than: SystemTime, index: &Path) -> Result<LfsCollection> {
        let index = safe(index)?;
        if exists(&index)? {
            check_directory(&index)?;
        }
        if !exists(&self.shared)? {
            return Ok(LfsCollection::default());
        }
        let _lock = self.lock()?;
        let mut removed = LfsCollection::default();
        for entry in fs::read_dir(&self.shared).map_err(conflict)? {
            let path = entry.map_err(conflict)?.path();
            let Some(name) = path
                .file_name()
                .and_then(|s| s.to_str())
                .filter(|s| oid(s).is_ok())
            else {
                continue;
            };
            let info = fs::symlink_metadata(&path).map_err(conflict)?;
            if !info.is_file() || info.file_type().is_symlink() {
                return Err(Error("lfs_object_conflict"));
            }
            if info.nlink() != 1
                || info.modified().map_err(conflict)? > older_than
                || exists(&index.join(name))?
            {
                continue;
            }
            fs::remove_file(path).map_err(conflict)?;
            removed.objects += 1;
            removed.bytes = removed
                .bytes
                .checked_add(info.len())
                .ok_or(Error("lfs_object_conflict"))?;
        }
        if removed.objects > 0 {
            files::sync_dir(&self.shared).map_err(|_| Error("lfs_object_conflict"))?;
        }
        Ok(removed)
    }
}

#[cfg(test)]
mod tests;
