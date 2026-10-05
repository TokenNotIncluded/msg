//! Unix writer fence and atomic metadata sessions. No service entry point.
//!
//! Run this synchronous API on the database-owning blocking thread. Do not drop
//! an async wrapper and assume that its in-flight transaction was cancelled.
//! One transaction owns the fence until rollback AND compensation have finished.
use crate::{database_error, records, ReadOnly, Session};
use fs2::FileExt;
use msg_core::{Error, Json, Result};
use msg_identity::store::{AuthorityStore, Record};
use records::{AuditEvent, Event, OperationResult, Resource, Revision};
use rusqlite::{params, Connection, OpenFlags, OptionalExtension, TransactionBehavior};
use std::{
    cell::Cell,
    fs::{self, File, OpenOptions},
    io::ErrorKind,
    marker::PhantomData,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    panic::{catch_unwind, resume_unwind, AssertUnwindSafe},
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant},
};

#[derive(Debug)]
pub struct WriteFailure {
    pub cause: Error,
    /// Stable diagnostic codes only: never SQL text, credentials or row bodies.
    pub cleanup_errors: Vec<Error>,
}
impl From<Error> for WriteFailure {
    fn from(cause: Error) -> Self {
        Self {
            cause,
            cleanup_errors: Vec::new(),
        }
    }
}
/// Only the store can construct this access state.
#[derive(Default)]
pub struct WriteAccess {
    effects: Vec<Box<dyn FnOnce() -> Result<()>>>,
    cleanup_errors: Vec<Error>,
    savepoint: u64,
    commit_uncertain: bool,
}
pub type WriteSession<'a> = Session<'a, WriteAccess>;

pub struct SqliteWriter {
    connection: Connection,
    path: PathBuf,
    timeout: Duration,
    needs_recovery: bool,
    identity: (u64, u64),
}
impl SqliteWriter {
    /// Open a pre-migrated WAL database. Never create it or change its schema.
    /// The configured database path must identify one file, not a symlink or
    /// hard-link alias that another writer could fence under a different name.
    pub fn open_existing(path: &Path, timeout: Duration) -> Result<Self> {
        if timeout.as_millis() > i32::MAX as u128 {
            return Err(Error("invalid_timeout"));
        }
        let metadata = fs::symlink_metadata(path).map_err(|_| Error("storage_error"))?;
        if !metadata.is_file() || metadata.nlink() != 1 {
            return Err(Error("unsafe_storage_path"));
        }
        let path = fs::canonicalize(path).map_err(|_| Error("storage_error"))?;
        let mut connection = Connection::open_with_flags(
            &path,
            OpenFlags::SQLITE_OPEN_READ_WRITE
                | OpenFlags::SQLITE_OPEN_NO_MUTEX
                | OpenFlags::SQLITE_OPEN_NOFOLLOW,
        )
        .map_err(database_error)?;
        connection.busy_timeout(timeout).map_err(database_error)?;
        connection
            .execute_batch(
                "PRAGMA foreign_keys=ON; PRAGMA trusted_schema=OFF; PRAGMA synchronous=FULL;",
            )
            .map_err(database_error)?;
        let mode: String = connection
            .query_row("PRAGMA journal_mode", [], |r| r.get(0))
            .map_err(database_error)?;
        if mode != "wal" {
            return Err(Error("unsupported_storage_journal"));
        }
        {
            let session = Session {
                transaction: connection.transaction().map_err(database_error)?,
                access: ReadOnly,
                poison: Cell::new(None),
                _owner: PhantomData,
            };
            session.validate_schema()?;
            validate_write_schema(&session.transaction)?;
        }
        Ok(Self {
            connection,
            path,
            timeout,
            needs_recovery: false,
            identity: (metadata.dev(), metadata.ino()),
        })
    }

    /// Return a value only after COMMIT succeeds. All fallible writes are sticky:
    /// ignoring their error cannot commit an earlier partial mutation.
    ///
    /// Sessions cannot escape, commit themselves, or move to another thread.
    /// ```compile_fail
    /// use msg_storage::WriteSession;
    /// fn handler(tx: &mut WriteSession<'_>) { tx.commit().unwrap(); }
    /// ```
    /// ```compile_fail
    /// use msg_storage::WriteSession;
    /// fn assert_send<T: Send>() {}
    /// fn check() { assert_send::<WriteSession<'static>>(); }
    /// ```
    /// ```compile_fail
    /// use msg_storage::SqliteWriter;
    /// fn escape(store: &mut SqliteWriter) { let _tx = store.transaction(|tx| Ok(tx)); }
    /// ```
    /// ```compile_fail
    /// use msg_storage::Snapshot;
    /// use msg_core::Json;
    /// fn mutate_read(tx: &mut Snapshot<'_>) { tx.set_setting("x", &Json::parse("1").unwrap()); }
    /// ```
    pub fn transaction<T>(
        &mut self,
        f: impl FnOnce(&mut WriteSession<'_>) -> Result<T>,
    ) -> std::result::Result<T, WriteFailure> {
        if self.needs_recovery {
            return Err(Error("storage_recovery_required").into());
        }
        let deadline = Instant::now()
            .checked_add(self.timeout)
            .ok_or(Error("invalid_timeout"))?;
        // This guard is intentionally outside the SQL transaction. Do not unlink
        // its file on release: a second inode would break the process fence.
        let _fence = acquire_fence(&self.path, deadline)?;
        let metadata =
            fs::symlink_metadata(&self.path).map_err(|_| Error("storage_recovery_required"))?;
        if !metadata.is_file()
            || metadata.nlink() != 1
            || (metadata.dev(), metadata.ino()) != self.identity
        {
            self.needs_recovery = true;
            return Err(Error("storage_recovery_required").into());
        }
        self.connection
            .busy_timeout(deadline.saturating_duration_since(Instant::now()))
            .map_err(database_error)?;
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(database_error)?;
        let mut session = Session {
            transaction,
            access: WriteAccess::default(),
            poison: Cell::new(None),
            _owner: PhantomData,
        };
        // Revalidate after the writer lock: an old schema read is not authority.
        let outcome = catch_unwind(AssertUnwindSafe(|| {
            session.validate_schema()?;
            validate_write_schema(&session.transaction)?;
            let result = f(&mut session)?;
            session.active()?;
            if let Err(error) = session.transaction.execute_batch("COMMIT") {
                return Err(session.commit_failed(error));
            }
            Ok(result)
        }));
        match outcome {
            Ok(Ok(value)) => {
                session.access.effects.clear();
                Ok(value)
            }
            other => {
                if !session.transaction.is_autocommit() {
                    if let Err(error) = session.transaction.execute_batch("ROLLBACK") {
                        session.access.cleanup_errors.push(database_error(error));
                    }
                }
                // An I/O error can leave COMMIT's outcome unknown. Do not undo
                // external data that might now be referenced by committed rows.
                if !session.access.commit_uncertain {
                    session.compensate(0);
                }
                self.needs_recovery = session.access.commit_uncertain
                    || !session.access.cleanup_errors.is_empty()
                    || !session.transaction.is_autocommit();
                match other {
                    Ok(Err(cause)) => Err(WriteFailure {
                        cause,
                        cleanup_errors: std::mem::take(&mut session.access.cleanup_errors),
                    }),
                    Err(panic) => resume_unwind(panic),
                    Ok(Ok(_)) => unreachable!(),
                }
            }
        }
    }
}
fn acquire_fence(path: &Path, deadline: Instant) -> Result<File> {
    let mut name = path
        .file_name()
        .ok_or(Error("unsafe_storage_path"))?
        .to_os_string();
    name.push(".writer.lock");
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK)
        .open(path.with_file_name(name))
        .map_err(|_| Error("unsafe_writer_lock"))?;
    let metadata = file.metadata().map_err(|_| Error("unsafe_writer_lock"))?;
    if !metadata.is_file() || metadata.nlink() != 1 {
        return Err(Error("unsafe_writer_lock"));
    }
    loop {
        match file.try_lock_exclusive() {
            Ok(()) => return Ok(file),
            Err(e) if e.kind() == ErrorKind::Interrupted => {}
            Err(e) if e.kind() == ErrorKind::WouldBlock => {}
            Err(_) => return Err(Error("writer_lock_error")),
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(Error("server_busy"));
        }
        thread::sleep(remaining.min(Duration::from_millis(10)));
    }
}
fn validate_write_schema(db: &Connection) -> Result<()> {
    // Only known tables/columns may participate. This is not a schema migrator.
    for (table, columns) in [
        ("resources", "id,type,name,parent,owner,grp,mode,generation,revision,state,created_at,modified_at,body"),
        ("resource_tags", "resource_id,tag"), ("resource_path_aliases", "parent_id,name,resource_id"),
        ("revisions", "id,resource_id,created_at,body"),
        ("relations", "revision_id,source_id,type,target_id,target_revision,body"),
        ("events", "seq,id,body"), ("audit", "seq,digest,previous,body"),
    ] {
        let exists: bool = db.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE name=? AND type='table')", [table], |r| r.get(0)).map_err(database_error)?;
        if !exists { return Err(Error("unsupported_storage_schema")); }
        db.prepare(&format!("SELECT {columns} FROM {table} LIMIT 0")).map_err(|_| Error("unsupported_storage_schema"))?;
    }
    // Result uniqueness is a durability requirement, not an optional index.
    let mut statement = db
        .prepare("SELECT name,pk FROM pragma_table_info('results') WHERE pk>0 ORDER BY pk")
        .map_err(database_error)?;
    let keys = statement
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))
        .map_err(database_error)?
        .collect::<std::result::Result<Vec<_>, _>>()
        .map_err(database_error)?;
    if keys != [("subject".into(), 1), ("request_id".into(), 2)] {
        return Err(Error("unsupported_storage_schema"));
    }
    validate_constraints(db)?;
    Ok(())
}
fn validate_constraints(db: &Connection) -> Result<()> {
    for (table, expected) in [
        ("resources", &["id"][..]),
        ("revisions", &["id"]),
        ("settings", &["key"]),
        ("resource_tags", &["tag", "resource_id"]),
        ("resource_path_aliases", &["parent_id", "name"]),
        ("events", &["seq"]),
        ("audit", &["seq"]),
    ] {
        let mut stmt = db
            .prepare("SELECT name FROM pragma_table_info(?1) WHERE pk>0 ORDER BY pk")
            .map_err(database_error)?;
        let actual = stmt
            .query_map([table], |r| r.get::<_, String>(0))
            .map_err(database_error)?
            .collect::<std::result::Result<Vec<_>, _>>()
            .map_err(database_error)?;
        if actual != expected {
            return Err(Error("unsupported_storage_schema"));
        }
    }
    for (table, expected) in [
        ("resources", &["parent", "name"][..]),
        ("events", &["id"]),
        ("audit", &["digest"]),
    ] {
        let mut stmt = db
            .prepare("SELECT name FROM pragma_index_list(?1) WHERE \"unique\"=1 AND partial=0")
            .map_err(database_error)?;
        let indexes = stmt
            .query_map([table], |r| r.get::<_, String>(0))
            .map_err(database_error)?
            .collect::<std::result::Result<Vec<_>, _>>()
            .map_err(database_error)?;
        let mut found = false;
        for name in indexes {
            let mut stmt = db
                .prepare("SELECT name FROM pragma_index_info(?1) ORDER BY seqno")
                .map_err(database_error)?;
            let columns = stmt
                .query_map([name], |r| r.get::<_, Option<String>>(0))
                .map_err(database_error)?
                .collect::<std::result::Result<Vec<_>, _>>()
                .map_err(database_error)?;
            found |= columns
                .iter()
                .map(|v| v.as_deref())
                .eq(expected.iter().map(|v| Some(*v)));
        }
        if !found {
            return Err(Error("unsupported_storage_schema"));
        }
    }
    for (name, expected) in [
        ("one_root", "createuniqueindexone_rootonresources((1))whereparentisnull"),
        ("audit_no_update", "createtriggeraudit_no_updatebeforeupdateonauditbeginselectraise(abort,'append_only_audit');end"),
        ("audit_no_delete", "createtriggeraudit_no_deletebeforedeleteonauditbeginselectraise(abort,'append_only_audit');end"),
    ] {
        let sql: Option<String> = db.query_row("SELECT sql FROM sqlite_schema WHERE name=?1", [name], |r| r.get(0)).optional().map_err(database_error)?;
        let normalized = sql.unwrap_or_default().chars().filter(|c| !c.is_ascii_whitespace()).collect::<String>().to_ascii_lowercase();
        if normalized.trim_end_matches(';') != expected { return Err(Error("unsupported_storage_schema")); }
    }
    for (table, expected) in [
        (
            "resources",
            &[("parent", "resources", "id", "NO ACTION")][..],
        ),
        (
            "resource_tags",
            &[("resource_id", "resources", "id", "CASCADE")],
        ),
        (
            "resource_path_aliases",
            &[
                ("parent_id", "resources", "id", "CASCADE"),
                ("resource_id", "resources", "id", "CASCADE"),
            ],
        ),
        (
            "revisions",
            &[("resource_id", "resources", "id", "NO ACTION")],
        ),
        (
            "relations",
            &[("revision_id", "revisions", "id", "CASCADE")],
        ),
    ] {
        let mut stmt = db.prepare("SELECT \"from\",\"table\",\"to\",on_delete FROM pragma_foreign_key_list(?1) ORDER BY \"from\"").map_err(database_error)?;
        let actual = stmt
            .query_map([table], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                ))
            })
            .map_err(database_error)?
            .collect::<std::result::Result<Vec<_>, _>>()
            .map_err(database_error)?;
        if !actual
            .iter()
            .map(|(a, b, c, d)| (a.as_str(), b.as_str(), c.as_str(), d.as_str()))
            .eq(expected.iter().copied())
        {
            return Err(Error("unsupported_storage_schema"));
        }
    }
    Ok(())
}
impl WriteSession<'_> {
    fn commit_failed(&mut self, error: rusqlite::Error) -> Error {
        if self.transaction.is_autocommit() {
            self.access.commit_uncertain = true;
            Error("commit_outcome_uncertain")
        } else {
            database_error(error)
        }
    }
    fn mutate<T>(&mut self, f: impl FnOnce(&mut Self) -> Result<T>) -> Result<T> {
        self.active()?;
        let result = f(self);
        if let Err(error) = &result {
            self.poison.set(Some(*error));
        }
        result
    }
    pub fn on_rollback(&mut self, effect: impl FnOnce() -> Result<()> + 'static) -> Result<()> {
        self.active()?;
        self.access.effects.push(Box::new(effect));
        Ok(())
    }
    fn compensate(&mut self, start: usize) {
        for effect in self.access.effects.drain(start..).rev() {
            match catch_unwind(AssertUnwindSafe(effect)) {
                Ok(Ok(())) => {}
                Ok(Err(error)) => self.access.cleanup_errors.push(error),
                Err(_) => self
                    .access
                    .cleanup_errors
                    .push(Error("rollback_effect_panicked")),
            }
        }
    }
    /// A rejected inner unit may be handled ONLY after its changes and external
    /// rollback effects are undone. A full SQLite abort poisons the outer unit.
    pub fn savepoint<T>(&mut self, f: impl FnOnce(&mut Self) -> Result<T>) -> Result<T> {
        self.active()?;
        self.access.savepoint = self
            .access
            .savepoint
            .checked_add(1)
            .ok_or(Error("savepoint_limit"))?;
        let name = format!("msg_sp_{}", self.access.savepoint);
        self.transaction
            .execute_batch(&format!("SAVEPOINT {name}"))
            .map_err(database_error)?;
        let effects_start = self.access.effects.len();
        let cleanup_start = self.access.cleanup_errors.len();
        let result = catch_unwind(AssertUnwindSafe(|| {
            let value = f(self)?;
            self.active()?;
            self.transaction
                .execute_batch(&format!("RELEASE {name}"))
                .map_err(database_error)?;
            Ok(value)
        }));
        match result {
            Ok(Ok(value)) => Ok(value),
            other => {
                let sql_rollback = if self.transaction.is_autocommit() {
                    Err(Error("transaction_aborted"))
                } else {
                    self.transaction
                        .execute_batch(&format!("ROLLBACK TO {name}; RELEASE {name}"))
                        .map_err(database_error)
                };
                if let Err(error) = sql_rollback {
                    self.access.cleanup_errors.push(error);
                }
                self.compensate(effects_start);
                self.poison
                    .set(if self.access.cleanup_errors.len() == cleanup_start {
                        None
                    } else {
                        Some(Error("storage_recovery_required"))
                    });
                match other {
                    Ok(Err(error)) => Err(error),
                    Err(panic) => resume_unwind(panic),
                    Ok(Ok(_)) => unreachable!(),
                }
            }
        }
    }
    pub fn set_setting(&mut self, key: &str, value: &Json) -> Result<()> {
        self.mutate(|s| {
            let raw = value.canonical()?;
            if raw.len() > msg_core::MAX_BYTES { return Err(Error("storage_record_too_large")); }
            s.transaction.execute("INSERT INTO settings(key,value) VALUES(?1,?2) ON CONFLICT(key) DO UPDATE SET value=excluded.value", params![key, raw]).map_err(database_error)?;
            Ok(())
        })
    }
    pub fn insert_resource(&mut self, resource: &Resource) -> Result<()> {
        self.mutate(|s| {
            resource.validate()?;
            let body = records::encode(resource)?.canonical()?;
            if let Some(parent) = &resource.parent { s.resource(parent)?; }
            s.transaction.execute("INSERT INTO resources(id,type,name,parent,owner,grp,mode,generation,revision,state,created_at,modified_at,body) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13)",
                params![resource.id, resource.resource_type, resource.name, resource.parent, resource.owner, resource.group, resource.mode, resource.generation, resource.revision, resource.state.sql(), timestamp(resource.created_at)?, timestamp(resource.modified_at)?, body]).map_err(database_error)?;
            s.insert_tags(resource)
        })
    }
    fn insert_tags(&self, resource: &Resource) -> Result<()> {
        for tag in &resource.tags {
            self.transaction
                .execute(
                    "INSERT INTO resource_tags(resource_id,tag) VALUES(?1,?2)",
                    params![resource.id, tag],
                )
                .map_err(database_error)?;
        }
        Ok(())
    }
    pub fn replace_resource(
        &mut self,
        resource: &Resource,
        expected_generation: i64,
    ) -> Result<()> {
        self.mutate(|s| {
            resource.validate()?;
            let old: Resource = records::decode(&s.record(Record::Resource, &resource.id)?)?;
            if old.id != resource.id { return Err(Error("storage_record_mismatch")); }
            if old.generation != expected_generation { return Err(Error("generation_conflict")); }
            if expected_generation.checked_add(1) != Some(resource.generation) { return Err(Error("invalid_generation")); }
            if old.resource_type != resource.resource_type || old.created_at != resource.created_at || old.created_by != resource.created_by { return Err(Error("immutable_creation_fact")); }
            if let Some(parent) = &resource.parent {
                if parent == &resource.id || s.ancestors(parent)?.iter().any(|a| a.id == resource.id) { return Err(Error("parent_cycle")); }
            }
            let body = records::encode(resource)?.canonical()?;
            if old.parent != resource.parent || old.owner != resource.owner || old.group != resource.group || old.mode != resource.mode || old.state != resource.state {
                let epoch = s.setting("authorization_epoch")?.map(|v| v.as_integer()?.parse::<i64>().map_err(|_| Error("invalid_authorization_epoch"))).transpose()?.unwrap_or(0);
                let next = epoch.checked_add(1).filter(|_| epoch >= 0).ok_or(Error("invalid_authorization_epoch"))?;
                s.set_setting("authorization_epoch", &Json::parse(&next.to_string())?)?;
            }
            if let Some(parent) = &old.parent {
                if old.parent != resource.parent || old.name != resource.name {
                    s.transaction.execute("INSERT INTO resource_path_aliases(parent_id,name,resource_id) VALUES(?1,?2,?3) ON CONFLICT(parent_id,name) DO UPDATE SET resource_id=excluded.resource_id", params![parent, old.name, old.id]).map_err(database_error)?;
                }
            }
            let changed = s.transaction.execute("UPDATE resources SET name=?1,parent=?2,owner=?3,grp=?4,mode=?5,generation=?6,revision=?7,state=?8,modified_at=?9,body=?10 WHERE id=?11 AND generation=?12",
                params![resource.name,resource.parent,resource.owner,resource.group,resource.mode,resource.generation,resource.revision,resource.state.sql(),timestamp(resource.modified_at)?,body,resource.id,expected_generation]).map_err(database_error)?;
            if changed != 1 { return Err(Error("generation_conflict")); }
            if old.tags != resource.tags {
                s.transaction.execute("DELETE FROM resource_tags WHERE resource_id=?1", [&resource.id]).map_err(database_error)?;
                s.insert_tags(resource)?;
            }
            Ok(())
        })
    }
    pub fn append_revision(&mut self, revision: &Revision) -> Result<()> {
        self.mutate(|s| {
            if revision.format_version < 1 { return Err(Error("invalid_version")); }
            if revision.content.size < 0 { return Err(Error("invalid_nonnegative_integer")); }
            for parent in &revision.parents {
                let exists: bool = s.transaction.query_row("SELECT EXISTS(SELECT 1 FROM revisions WHERE id=?1 AND resource_id=?2)", params![parent, revision.resource_id], |r| r.get(0)).map_err(database_error)?;
                if !exists { return Err(Error("revision_not_found")); }
            }
            s.transaction.execute("INSERT INTO revisions(id,resource_id,created_at,body) VALUES(?1,?2,?3,?4)", params![revision.id,revision.resource_id,timestamp(revision.created_at)?,records::encode(revision)?.canonical()?]).map_err(database_error)?;
            for relation in &revision.relations {
                if relation.excerpt.is_some_and(|[a,b]| a < 0 || a > b) { return Err(Error("invalid_byte_range")); }
                s.transaction.execute("INSERT INTO relations(revision_id,source_id,type,target_id,target_revision,body) VALUES(?1,?2,?3,?4,?5,?6)", params![revision.id,revision.resource_id,relation.relation_type,relation.target.id,relation.target.revision,records::encode(relation)?.canonical()?]).map_err(database_error)?;
            }
            Ok(())
        })
    }
    /// Storage-only port: the caller must authenticate/authorize the operation,
    /// redact secret delivery data and produce its receipt before saving.
    /// Neither this method nor a result lookup grants execution authority.
    pub fn save_result(
        &mut self,
        subject: Option<&str>,
        digest: &str,
        result: &OperationResult,
    ) -> Result<()> {
        self.mutate(|s| {
            if subject != result.subject.as_deref() || subject == Some("") {
                return Err(Error("storage_record_mismatch"));
            }
            if !digest.strip_prefix("sha256:").is_some_and(|v| {
                v.len() == 64
                    && v.bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            }) {
                return Err(Error("invalid_digest"));
            }
            let count: i64 = s
                .transaction
                .query_row("SELECT COUNT(*) FROM results", [], |r| r.get(0))
                .map_err(database_error)?;
            if count >= 100_000 {
                return Err(Error("storage_capacity_exceeded"));
            }
            s.transaction
                .execute(
                    "INSERT INTO results(subject,request_id,digest,body) VALUES(?1,?2,?3,?4)",
                    params![
                        subject.unwrap_or(""),
                        result.request_id,
                        digest,
                        records::encode(result)?.canonical()?
                    ],
                )
                .map_err(database_error)?;
            Ok(())
        })
    }
    pub fn append_event(&mut self, event: &Event) -> Result<()> {
        self.mutate(|s| {
            s.transaction
                .execute(
                    "INSERT INTO events(id,body) VALUES(?1,?2)",
                    params![event.id, records::encode(event)?.canonical()?],
                )
                .map_err(database_error)?;
            Ok(())
        })
    }
    pub fn append_audit(&mut self, event: &AuditEvent) -> Result<()> {
        self.mutate(|s| {
            let previous: Option<String> = s
                .transaction
                .query_row(
                    "SELECT digest FROM audit ORDER BY seq DESC LIMIT 1",
                    [],
                    |r| r.get(0),
                )
                .optional()
                .map_err(database_error)?;
            let mut entry = event.clone();
            entry.previous_digest = previous;
            entry.entry_digest.clear();
            let mut fields = records::encode(&entry)?.into_object()?;
            fields.remove("entry_digest");
            entry.entry_digest =
                msg_core::digest(Json::from_object(fields).canonical()?.as_bytes());
            s.transaction
                .execute(
                    "INSERT INTO audit(digest,previous,body) VALUES(?1,?2,?3)",
                    params![
                        entry.entry_digest,
                        entry.previous_digest,
                        records::encode(&entry)?.canonical()?
                    ],
                )
                .map_err(database_error)?;
            Ok(())
        })
    }
}
fn timestamp(value: msg_identity::models::Timestamp) -> Result<String> {
    Ok(records::encode(&value)?.as_str()?.to_owned())
}

#[cfg(test)]
mod tests;
