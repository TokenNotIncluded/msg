//! Durable undo for content pins, coupled to a metadata commit witness.
//!
//! `content_transaction` takes the existing metadata writer fence, reconciles
//! the previous journal, then runs one unit of work. Each pin's previous state
//! reaches disk before it changes. The final generation is written in the same
//! SQL transaction as the domain rows. After a crash, a new fenced transaction
//! can distinguish a committed generation from a rolled-back generation.
//!
//! Immutable blobs and revision objects are retained on failure; this module
//! never guesses that an object is safe to delete. All native content writers
//! and collectors must use this wrapper. Do not run old Python workers alongside
//! a pending native journal. This is not recovery of mutable Git receive-pack
//! refs, a restored authority snapshot, or arbitrary external effects.
use super::{files, GitContentStore, LfsMigration, LfsObjectStore};
use crate::{
    postgres::{PgFailure, PgSession, PgStore},
    records::{decode, encode, BlobRef, Revision},
    SqliteWriter, WriteFailure, WriteSession,
};
use msg_core::{Error, Json, Result, MAX_BYTES};
use msg_identity::store::AuthorityStore;
use serde::{Deserialize, Deserializer, Serialize};
use std::{fs, io::Read, os::unix::fs::MetadataExt, path::Path};

const GENERATION: &str = "content_recovery_generation";
const RUNTIME_GENERATION: &str = "recovery_runtime_generation";
pub(super) const JOURNAL: &str = "content-recovery.json";
const MAX_ACTIONS: usize = 4096;
const MAX_LEASE_BYTES: usize = 1024;
const RECOVERY_LEASE_PREFIX: &str = "msg:content-recovery:";

mod sealed {
    pub trait Metadata {}
    impl Metadata for crate::WriteSession<'_> {}
    impl Metadata for &crate::postgres::PgSession {}
}

/// Only the two transaction-owning adapters implement this interface.
pub trait ContentMetadata: sealed::Metadata {
    fn content_require_write(&self) -> Result<()>;
    fn content_setting(&self, key: &str) -> Result<Option<Json>>;
    fn content_has_setting(&self, key: &str) -> Result<bool>;
    fn content_set_generation(&mut self, value: &Json) -> Result<()>;
    fn content_on_rollback(&mut self, effect: Box<dyn FnOnce() -> Result<()>>) -> Result<()>;
}
impl ContentMetadata for WriteSession<'_> {
    fn content_require_write(&self) -> Result<()> {
        self.active()
    }
    fn content_setting(&self, key: &str) -> Result<Option<Json>> {
        self.setting(key)
    }
    fn content_has_setting(&self, key: &str) -> Result<bool> {
        self.has_setting(key)
    }
    fn content_set_generation(&mut self, value: &Json) -> Result<()> {
        self.set_setting(GENERATION, value)
    }
    fn content_on_rollback(&mut self, effect: Box<dyn FnOnce() -> Result<()>>) -> Result<()> {
        self.on_rollback(effect)
    }
}
impl ContentMetadata for &PgSession {
    fn content_require_write(&self) -> Result<()> {
        self.require_write()
    }
    fn content_setting(&self, key: &str) -> Result<Option<Json>> {
        self.setting(key)
    }
    fn content_has_setting(&self, key: &str) -> Result<bool> {
        self.has_setting(key)
    }
    fn content_set_generation(&mut self, value: &Json) -> Result<()> {
        self.set_setting(GENERATION, value)
    }
    fn content_on_rollback(&mut self, effect: Box<dyn FnOnce() -> Result<()>>) -> Result<()> {
        self.on_rollback(effect)
    }
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct UndoPin {
    blob: BlobRef,
    lease: String,
    pinned: bool,
}
impl UndoPin {
    fn apply(&self, content: &GitContentStore) -> Result<()> {
        if self.pinned {
            content.pin(&self.blob, &self.lease)
        } else {
            content.unpin(&self.blob, &self.lease)
        }
    }
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Journal {
    format_version: u8,
    #[serde(deserialize_with = "required_option")]
    before: Option<i64>,
    after: i64,
    #[serde(deserialize_with = "required_option")]
    runtime_generation: Option<String>,
    undo: Vec<UndoPin>,
}
fn recovery_required<T>(_: T) -> Error {
    Error("storage_recovery_required")
}
fn required_option<'de, T: Deserialize<'de>, D: Deserializer<'de>>(
    deserializer: D,
) -> std::result::Result<Option<T>, D::Error> {
    Option::<T>::deserialize(deserializer)
}
fn generation(metadata: &impl ContentMetadata) -> Result<Option<i64>> {
    metadata
        .content_setting(GENERATION)?
        .map(|value| {
            let number = value
                .as_integer()
                .map_err(recovery_required)?
                .parse::<i64>()
                .map_err(recovery_required)?;
            if number < 1 {
                return Err(Error("storage_recovery_required"));
            }
            Ok(number)
        })
        .transpose()
}
fn runtime_generation(metadata: &impl ContentMetadata) -> Result<Option<String>> {
    metadata
        .content_setting(RUNTIME_GENERATION)?
        .map(|value| {
            let value = value.as_str().map_err(recovery_required)?;
            if value.is_empty() {
                return Err(Error("storage_recovery_required"));
            }
            Ok(value.to_owned())
        })
        .transpose()
}
impl Journal {
    fn lease(&self) -> String {
        format!("{RECOVERY_LEASE_PREFIX}{}", self.after)
    }
    fn validate(&self) -> Result<()> {
        if self.format_version != 1
            || self.before.is_some_and(|value| value < 1)
            || self.before.unwrap_or(0).checked_add(1) != Some(self.after)
            || self
                .runtime_generation
                .as_ref()
                .is_some_and(String::is_empty)
            || self.undo.len() > MAX_ACTIONS
        {
            return Err(Error("storage_recovery_required"));
        }
        for undo in &self.undo {
            files::key(&undo.blob.digest).map_err(recovery_required)?;
            if undo.blob.size < 0
                || undo.lease.len() > MAX_LEASE_BYTES
                || undo.lease.starts_with(RECOVERY_LEASE_PREFIX)
            {
                return Err(Error("storage_recovery_required"));
            }
        }
        Ok(())
    }
    fn persist(&self, content: &GitContentStore) -> Result<()> {
        self.validate()?;
        let bytes = encode(self)
            .and_then(|value| value.canonical())
            .map_err(|_| Error("content_transaction_too_large"))?;
        files::replace(&content.root.join(JOURNAL), bytes.as_bytes(), false)
            .map_err(recovery_required)
    }
}

fn recover(metadata: &impl ContentMetadata, content: &GitContentStore) -> Result<()> {
    metadata.content_require_write()?;
    if metadata.content_has_setting("recovery_quarantine")? {
        return Err(Error("recovery_quarantined"));
    }
    content.layout()?;
    let path = content.root.join(JOURNAL);
    let file = match files::open(&path) {
        Ok(file) => file,
        Err(Error("content_missing")) => return Ok(()),
        Err(error) => return Err(recovery_required(error)),
    };
    let info = file.metadata().map_err(recovery_required)?;
    if info.nlink() != 1 || info.mode() & 0o077 != 0 || info.len() > MAX_BYTES as u64 {
        return Err(Error("storage_recovery_required"));
    }
    let mut bytes = String::new();
    file.take(MAX_BYTES as u64 + 1)
        .read_to_string(&mut bytes)
        .map_err(recovery_required)?;
    let journal: Journal = Json::parse(&bytes)
        .and_then(|value| decode(&value))
        .map_err(recovery_required)?;
    journal.validate()?;
    if journal.runtime_generation != runtime_generation(metadata)? {
        return Err(Error("storage_recovery_required"));
    }
    let current = generation(metadata)?;
    if current == journal.before {
        // Keeping the complete journal until every inverse is durable makes
        // a second crash during recovery safe: each inverse is idempotent.
        for undo in journal.undo.iter().rev() {
            undo.apply(content).map_err(recovery_required)?;
        }
    } else if current != Some(journal.after) {
        return Err(Error("storage_recovery_required"));
    }
    // Old pinned bytes remain protected even if collection runs later in the
    // same SQL transaction that removes their domain pins. Release these holds
    // only after the metadata commit outcome has been established.
    for undo in &journal.undo {
        if undo.pinned {
            content
                .unpin(&undo.blob, &journal.lease())
                .map_err(recovery_required)?;
        }
    }
    fs::remove_file(path).map_err(recovery_required)?;
    files::sync_dir(&content.root).map_err(recovery_required)
}

/// Content changes and their metadata share one adapter-owned transaction.
/// `metadata()` permits domain reads/writes; commit rights stay with the adapter.
/// Errors in content methods remain sticky even when a caller catches them.
pub struct ContentTransaction<'a, M: ContentMetadata> {
    metadata: &'a mut M,
    content: &'a GitContentStore,
    journal: Journal,
    persisted: bool,
    failed: Option<Error>,
}
impl<M: ContentMetadata> ContentTransaction<'_, M> {
    pub fn metadata(&mut self) -> &mut M {
        self.metadata
    }
    fn change<T>(&mut self, body: impl FnOnce(&mut Self) -> Result<T>) -> Result<T> {
        if let Some(error) = self.failed {
            return Err(error);
        }
        let result = body(self);
        if let Err(error) = result {
            self.failed = Some(error);
            Err(error)
        } else {
            result
        }
    }
    fn persist(&mut self) -> Result<()> {
        self.journal.persist(self.content)?;
        self.persisted = true;
        Ok(())
    }
    fn begin_external(&mut self) -> Result<()> {
        if !self.persisted {
            self.persist()?;
        }
        Ok(())
    }
    pub fn put(
        &mut self,
        source: &mut impl Read,
        media_type: &str,
        expected: Option<&str>,
        limit: u64,
    ) -> Result<BlobRef> {
        self.change(|unit| {
            unit.begin_external()?;
            unit.content.put(source, media_type, expected, limit)
        })
    }
    pub fn put_bytes(
        &mut self,
        bytes: &[u8],
        media_type: &str,
        expected: Option<&str>,
    ) -> Result<BlobRef> {
        self.put(&mut &bytes[..], media_type, expected, bytes.len() as u64)
    }
    fn change_pin(&mut self, blob: &BlobRef, lease: &str, pinned: bool) -> Result<()> {
        self.change(|unit| {
            if lease.starts_with(RECOVERY_LEASE_PREFIX) {
                return Err(Error("invalid_content_lease"));
            }
            if lease.len() > MAX_LEASE_BYTES || unit.journal.undo.len() >= MAX_ACTIONS {
                return Err(Error("content_transaction_too_large"));
            }
            let undo = UndoPin {
                blob: blob.clone(),
                lease: lease.to_owned(),
                pinned: unit.content.pinned(blob, lease)?,
            };
            unit.journal.undo.push(undo.clone());
            unit.persist()?;
            if undo.pinned {
                unit.content.pin(blob, &unit.journal.lease())?;
            }
            let content = GitContentStore::clone(unit.content);
            unit.metadata
                .content_on_rollback(Box::new(move || undo.apply(&content)))?;
            if pinned {
                unit.content.pin(blob, lease)
            } else {
                unit.content.unpin(blob, lease)
            }
        })
    }
    pub fn pin(&mut self, blob: &BlobRef, lease: &str) -> Result<()> {
        self.change_pin(blob, lease, true)
    }
    pub fn unpin(&mut self, blob: &BlobRef, lease: &str) -> Result<()> {
        self.change_pin(blob, lease, false)
    }
    pub fn commit_revision(&mut self, topic: &str, revision: &Revision) -> Result<String> {
        self.change(|unit| {
            unit.begin_external()?;
            unit.content.commit_revision(topic, revision)
        })
    }
    pub fn publish_lfs(
        &mut self,
        lfs: &LfsObjectStore,
        digest: &str,
        staged: &Path,
        size: u64,
        quota: Option<u64>,
    ) -> Result<bool> {
        self.change(|unit| {
            unit.begin_external()?;
            lfs.publish(digest, staged, size, quota)
        })
    }
    pub fn migrate_lfs(
        &mut self,
        lfs: &LfsObjectStore,
        digest: &str,
        quota: Option<u64>,
    ) -> Result<LfsMigration> {
        self.change(|unit| {
            unit.begin_external()?;
            lfs.migrate_legacy(digest, quota)
        })
    }
    fn finish(&mut self) -> Result<()> {
        if let Some(error) = self.failed {
            return Err(error);
        }
        if generation(self.metadata)? != self.journal.before
            || runtime_generation(self.metadata)? != self.journal.runtime_generation
            || self.metadata.content_has_setting("recovery_quarantine")?
        {
            return Err(Error("storage_recovery_required"));
        }
        if self.persisted {
            self.metadata
                .content_set_generation(&Json::parse(&self.journal.after.to_string())?)?;
        }
        Ok(())
    }
}

fn run<M: ContentMetadata, T>(
    metadata: &mut M,
    content: &GitContentStore,
    body: impl FnOnce(&mut ContentTransaction<'_, M>) -> Result<T>,
) -> Result<T> {
    recover(metadata, content)?;
    let before = generation(metadata)?;
    let mut unit = ContentTransaction {
        journal: Journal {
            format_version: 1,
            before,
            after: before
                .unwrap_or(0)
                .checked_add(1)
                .ok_or(Error("storage_recovery_required"))?,
            runtime_generation: runtime_generation(metadata)?,
            undo: Vec::new(),
        },
        metadata,
        content,
        persisted: false,
        failed: None,
    };
    let result = body(&mut unit)?;
    unit.finish()?;
    Ok(result)
}

impl SqliteWriter {
    /// An empty body performs startup recovery before admitting content work.
    pub fn content_transaction<T>(
        &mut self,
        content: &GitContentStore,
        body: impl FnOnce(&mut ContentTransaction<'_, WriteSession<'_>>) -> Result<T>,
    ) -> std::result::Result<T, WriteFailure> {
        self.transaction(|tx| run(tx, content, body))
    }
}
impl PgStore {
    pub fn content_transaction<T>(
        &self,
        content: &GitContentStore,
        body: impl FnOnce(&mut ContentTransaction<'_, &PgSession>) -> Result<T>,
    ) -> std::result::Result<T, PgFailure> {
        self.transaction(true, |tx| {
            let mut metadata = tx;
            run(&mut metadata, content, body)
        })
    }
}

#[cfg(test)]
mod tests;
