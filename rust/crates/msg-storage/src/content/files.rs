//! Filesystem primitives for service-owned content directories, never user paths.
use msg_core::{Error, Result};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, DirBuilder, File, Metadata, OpenOptions, Permissions},
    io::{Read, Seek, SeekFrom, Write},
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Component, Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};

pub(super) fn io(_: std::io::Error) -> Error {
    Error("content_store_error")
}
pub(super) fn hex(data: &[u8]) -> String {
    format!("{:x}", Sha256::digest(data))
}
pub(super) fn key(digest: &str) -> Result<&str> {
    let key = digest
        .strip_prefix("sha256:")
        .ok_or(Error("invalid_digest"))?;
    if key.len() != 64
        || !key
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(Error("invalid_digest"));
    }
    Ok(key)
}
pub(super) fn oid(value: &str) -> Result<&str> {
    if ![40, 64].contains(&value.len())
        || !value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(Error("invalid_content_index"));
    }
    Ok(value)
}
pub(super) fn segment(value: &str) -> Result<&str> {
    if value.is_empty()
        || value.len() > 160
        || [".", ".."].contains(&value)
        || !value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
    {
        return Err(Error("invalid_revision_id"));
    }
    Ok(value)
}

/// Reject symlink path components. Directory ownership and protection against
/// hostile same-UID processes remain an administrator responsibility; this is
/// not an openat sandbox. No request may choose these configured directories.
pub(super) fn absolute(path: &Path) -> Result<PathBuf> {
    let path = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir().map_err(io)?.join(path)
    };
    let mut checked = PathBuf::new();
    for component in path.components() {
        match component {
            Component::ParentDir => return Err(Error("unsafe_content_path")),
            Component::CurDir => continue,
            _ => checked.push(component),
        }
        match fs::symlink_metadata(&checked) {
            Ok(meta) if meta.file_type().is_symlink() => return Err(Error("unsafe_content_path")),
            Ok(_) => (),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
            Err(e) => return Err(io(e)),
        }
    }
    Ok(checked)
}
pub(super) fn directory(path: &Path, group: bool) -> Result<()> {
    absolute(path)?;
    let created = match fs::symlink_metadata(path) {
        Ok(_) => false,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            DirBuilder::new().mode(0o700).create(path).map_err(io)?;
            true
        }
        Err(e) => return Err(io(e)),
    };
    if created && group {
        fs::set_permissions(path, Permissions::from_mode(0o2750)).map_err(io)?;
    }
    check_directory(path, group)?;
    if created {
        sync_dir(path)?;
        if let Some(parent) = path.parent() {
            sync_dir(parent)?;
        }
    }
    Ok(())
}
pub(super) fn check_directory(path: &Path, group: bool) -> Result<()> {
    let meta = fs::symlink_metadata(path).map_err(io)?;
    if !meta.is_dir() || meta.file_type().is_symlink() || meta.mode() & 0o022 != 0 {
        return Err(Error("unsafe_content_path"));
    }
    if group && meta.mode() & 0o2077 != 0o2050 {
        return Err(Error("content_permission_migration_required"));
    }
    Ok(())
}
pub(super) fn sync_dir(path: &Path) -> Result<()> {
    OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_DIRECTORY | libc::O_CLOEXEC)
        .open(path)
        .and_then(|f| f.sync_all())
        .map_err(io)
}
pub(super) fn open(path: &Path) -> Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)
        .map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                Error("content_missing")
            } else {
                Error("unsafe_content_path")
            }
        })?;
    if !file.metadata().map_err(io)?.is_file() {
        return Err(Error("unsafe_content_path"));
    }
    Ok(file)
}
pub(super) fn bounded(path: &Path, limit: usize) -> Result<Vec<u8>> {
    let file = open(path)?;
    if file.metadata().map_err(io)?.len() > limit as u64 {
        return Err(Error("invalid_content_index"));
    }
    let mut bytes = Vec::new();
    file.take(limit as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(io)?;
    if bytes.len() > limit {
        return Err(Error("invalid_content_index"));
    }
    Ok(bytes)
}
fn unchanged(a: &Metadata, b: &Metadata) -> bool {
    (
        a.dev(),
        a.ino(),
        a.len(),
        a.mtime(),
        a.mtime_nsec(),
        a.ctime(),
        a.ctime_nsec(),
    ) == (
        b.dev(),
        b.ino(),
        b.len(),
        b.mtime(),
        b.mtime_nsec(),
        b.ctime(),
        b.ctime_nsec(),
    )
}
/// Read and hash with constant memory, stopping at a caller-supplied quota.
pub(super) fn stream(
    source: &mut impl Read,
    target: &mut impl Write,
    limit: u64,
) -> Result<(String, u64)> {
    let mut hash = Sha256::new();
    let mut total = 0_u64;
    let mut buffer = [0; 65_536];
    loop {
        let count = match source.read(&mut buffer) {
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            other => other.map_err(io)?,
        };
        if count == 0 {
            break;
        }
        total = total
            .checked_add(count as u64)
            .ok_or(Error("content_too_large"))?;
        if total > limit.min(i64::MAX as u64) {
            return Err(Error("content_too_large"));
        }
        hash.update(&buffer[..count]);
        target.write_all(&buffer[..count]).map_err(io)?;
    }
    Ok((format!("sha256:{:x}", hash.finalize()), total))
}
pub(super) fn verified_copy(
    source: &mut File,
    target: &mut impl Write,
    digest: &str,
    size: u64,
) -> Result<()> {
    let before = source.metadata().map_err(io)?;
    if before.len() != size {
        return Err(Error("content_size_mismatch"));
    }
    source.seek(SeekFrom::Start(0)).map_err(io)?;
    let (actual, length) = stream(source, target, size)?;
    if actual != digest || length != size || !unchanged(&before, &source.metadata().map_err(io)?) {
        return Err(Error("content_digest_mismatch"));
    }
    Ok(())
}
static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);
pub(super) struct Temporary {
    pub path: PathBuf,
    pub file: File,
}
impl Temporary {
    pub fn new(parent: &Path) -> Result<Self> {
        check_directory(parent, false)?;
        for _ in 0..128 {
            let path = parent.join(format!(
                ".pending-rust-{}-{}",
                std::process::id(),
                NEXT_TEMP.fetch_add(1, Ordering::Relaxed)
            ));
            let result = OpenOptions::new()
                .read(true)
                .write(true)
                .create_new(true)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .open(&path);
            match result {
                Ok(file) => return Ok(Self { path, file }),
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(e) => return Err(io(e)),
            }
        }
        Err(Error("content_store_error"))
    }
    pub fn rewind(&mut self) -> Result<()> {
        self.file.seek(SeekFrom::Start(0)).map(|_| ()).map_err(io)
    }
    pub fn permissions(&self, group: bool) -> Result<()> {
        self.file
            .set_permissions(Permissions::from_mode(if group { 0o640 } else { 0o600 }))
            .map_err(io)?;
        self.file.sync_all().map_err(io)
    }
    /// Publish without overwriting an existing inode (also preserves LFS links).
    pub fn install(&self, path: &Path) -> Result<bool> {
        match fs::hard_link(&self.path, path) {
            Ok(()) => {
                sync_dir(path.parent().ok_or(Error("unsafe_content_path"))?)?;
                Ok(true)
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => Ok(false),
            Err(e) => Err(io(e)),
        }
    }
}
impl Drop for Temporary {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

pub(super) fn replace(path: &Path, bytes: &[u8], group: bool) -> Result<()> {
    if let Ok(meta) = fs::symlink_metadata(path) {
        if !meta.is_file() || meta.file_type().is_symlink() {
            return Err(Error("unsafe_content_path"));
        }
    }
    let parent = path.parent().ok_or(Error("unsafe_content_path"))?;
    let mut temp = Temporary::new(parent)?;
    temp.file.write_all(bytes).map_err(io)?;
    temp.permissions(group)?;
    fs::rename(&temp.path, path).map_err(io)?;
    sync_dir(parent)
}
