//! Conservative collection under the metadata writer fence. Domain code supplies
//! all retained Revision, transfer-chunk and market roots from one snapshot.
use super::{files, recovery::JOURNAL, Entry, GitContentStore, LfsObjectStore};
use msg_core::{Error, Json, Result};
use std::{
    collections::BTreeSet,
    fs,
    os::unix::fs::MetadataExt,
    path::Path,
    time::{Duration, SystemTime},
};

#[derive(Debug, Default, PartialEq, Eq)]
pub struct ContentCollection {
    pub objects: u64,
    pub shared_objects: u64,
    pub shared_bytes: u64,
}
fn key(name: &str) -> bool {
    name.len() == 64
        && name
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn present(path: &Path) -> Result<bool> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(e) => Err(files::io(e)),
    }
}
impl GitContentStore {
    /// Run in a separate `content_transaction` after cleanup has committed.
    /// Digests are complete `sha256:…` strings; revision IDs are unhashed IDs.
    /// Never pass only current heads: retained history and in-flight work are roots.
    pub fn collect_unreferenced(
        &self,
        live_digests: &BTreeSet<String>,
        live_revisions: &BTreeSet<String>,
        cutoff: SystemTime,
    ) -> Result<ContentCollection> {
        self.layout()?;
        // Starting the wrapper resolves an older journal. A new journal here
        // means this unit has already changed content; its roots are not yet
        // committed, so irreversible collection must wait for another unit.
        if present(&self.root.join(JOURNAL))? {
            return Err(Error("content_collection_requires_committed_roots"));
        }
        let mut live: BTreeSet<String> = live_digests
            .iter()
            .map(|digest| files::key(digest).map(str::to_owned))
            .collect::<Result<_>>()?;
        let mut revisions = BTreeSet::new();
        for revision in live_revisions {
            files::segment(revision)?;
            revisions.insert(files::hex(revision.as_bytes()));
        }
        let git_pins = self.git(&["for-each-ref", "--format=%(refname)", "refs/pins"], None)?;
        for reference in git_pins.lines() {
            let digest = reference
                .rsplit('/')
                .next()
                .ok_or(Error("invalid_content_pin"))?;
            if !key(digest) {
                return Err(Error("invalid_content_pin"));
            }
            live.insert(digest.to_owned());
        }
        let pins = self.root.join("pins");
        if present(&pins)? {
            files::check_directory(&pins, false)?;
            for lease in fs::read_dir(&pins).map_err(files::io)? {
                let lease = lease.map_err(files::io)?.path();
                files::check_directory(&lease, false)?;
                for pin in fs::read_dir(lease).map_err(files::io)? {
                    let pin = pin.map_err(files::io)?.path();
                    let Some(name) = pin
                        .file_name()
                        .and_then(|value| value.to_str())
                        .filter(|value| key(value))
                    else {
                        continue;
                    };
                    if files::bounded(&pin, 2)? != b"1\n" {
                        return Err(Error("invalid_content_pin"));
                    }
                    live.insert(name.to_owned());
                }
            }
        }
        let staging = self.git(
            &["for-each-ref", "--format=%(refname)", "refs/staging"],
            None,
        )?;
        let topics = self.git(
            &["for-each-ref", "--format=%(refname)", "refs/topics"],
            None,
        )?;
        // A corrupt ref vocabulary is not permission to remove an unknown root.
        for reference in staging.lines().chain(topics.lines()) {
            if !reference.rsplit('/').next().is_some_and(key) {
                return Err(Error("invalid_content_index"));
            }
        }
        let mut result = ContentCollection::default();
        let mut binary_removed = false;
        for entry in fs::read_dir(&self.index).map_err(files::io)? {
            let path = entry.map_err(files::io)?.path();
            let Some(name) = path
                .file_name()
                .and_then(|value| value.to_str())
                .filter(|value| key(value))
            else {
                continue;
            };
            let info = fs::symlink_metadata(&path).map_err(files::io)?;
            if !info.is_file() || info.file_type().is_symlink() || info.nlink() != 1 {
                return Err(Error("unsafe_content_path"));
            }
            if live.contains(name) || info.modified().map_err(files::io)? > cutoff {
                continue;
            }
            let bytes = files::bounded(&path, 4096)?;
            let json = Json::parse(
                std::str::from_utf8(&bytes).map_err(|_| Error("invalid_content_index"))?,
            )?;
            let entry: Entry = serde_json::from_str(&json.canonical()?)
                .map_err(|_| Error("invalid_content_index"))?;
            match entry {
                Entry::Git { oid, size } => {
                    files::oid(&oid)?;
                    if size > i64::MAX as u64 {
                        return Err(Error("invalid_content_index"));
                    }
                    for reference in staging
                        .lines()
                        .filter(|value| value.rsplit('/').next() == Some(name))
                    {
                        self.git(&["update-ref", "--no-deref", "-d", reference], None)?;
                    }
                }
                Entry::Binary { size } => {
                    if size > i64::MAX as u64 {
                        return Err(Error("invalid_content_index"));
                    }
                    let binary = self.binary.join(name);
                    if present(&binary)? {
                        let info = fs::symlink_metadata(&binary).map_err(files::io)?;
                        if !info.is_file() || info.file_type().is_symlink() {
                            return Err(Error("unsafe_content_path"));
                        }
                        if info.nlink() == 1 {
                            fs::remove_file(binary).map_err(files::io)?;
                            binary_removed = true;
                        }
                    }
                }
            }
            fs::remove_file(path).map_err(files::io)?;
            result.objects += 1;
        }
        if binary_removed {
            files::sync_dir(&self.binary)?;
        }
        if result.objects > 0 {
            files::sync_dir(&self.index)?;
        }
        for reference in topics.lines() {
            let id = reference
                .rsplit('/')
                .next()
                .ok_or(Error("invalid_content_index"))?;
            if !revisions.contains(id) {
                self.git(&["update-ref", "--no-deref", "-d", reference], None)?;
            }
        }
        let mappings = self.root.join("revisions");
        if present(&mappings)? {
            files::check_directory(&mappings, false)?;
            let mut removed = false;
            for entry in fs::read_dir(&mappings).map_err(files::io)? {
                let path = entry.map_err(files::io)?.path();
                let name = path
                    .file_name()
                    .and_then(|value| value.to_str())
                    .ok_or(Error("invalid_content_index"))?;
                // Private interrupted publication files are not Revision IDs.
                if name.starts_with(".pending-") {
                    continue;
                }
                files::segment(name)?;
                let info = fs::symlink_metadata(&path).map_err(files::io)?;
                if !info.is_file() || info.file_type().is_symlink() || info.nlink() != 1 {
                    return Err(Error("unsafe_content_path"));
                }
                if !live_revisions.contains(name) && info.modified().map_err(files::io)? <= cutoff {
                    fs::remove_file(path).map_err(files::io)?;
                    removed = true;
                }
            }
            if removed {
                files::sync_dir(&mappings)?;
            }
        }
        // Reachability, including retained commit ancestry, remains Git's final
        // guard. Do not use --prune=now even when callers supply a newer cutoff.
        self.git(&["gc", "--prune=1.hour.ago"], None)?;
        let shared = LfsObjectStore::open(&self.repo, &self.binary, Duration::from_secs(30))?
            .collect_unlinked(cutoff, &self.index)?;
        result.shared_objects = shared.objects;
        result.shared_bytes = shared.bytes;
        Ok(result)
    }
}

#[cfg(test)]
mod tests;
