//! Focused failure injection; Python-created full schemas are tested separately.
use super::*;
use std::{
    cell::RefCell,
    rc::Rc,
    sync::atomic::{AtomicU64, Ordering},
    time::{SystemTime, UNIX_EPOCH},
};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "msg-writer-{}-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        let fixture = Self(path);
        let db = Connection::open(fixture.db()).unwrap();
        db.execute_batch("PRAGMA journal_mode=WAL;
            CREATE TABLE schema_version(version INTEGER PRIMARY KEY); INSERT INTO schema_version VALUES(1);
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE identities(id TEXT PRIMARY KEY,kind TEXT,body TEXT);
            CREATE TABLE credentials(id TEXT PRIMARY KEY,subject TEXT,body TEXT);
            CREATE TABLE certificates(id TEXT PRIMARY KEY,subject TEXT,parent TEXT,revoked INTEGER,body TEXT);
            CREATE TABLE resources(id TEXT PRIMARY KEY,type TEXT,name TEXT,parent TEXT REFERENCES resources(id) DEFERRABLE INITIALLY DEFERRED,owner TEXT,grp TEXT,mode INTEGER,generation INTEGER,revision TEXT,state TEXT,created_at TEXT,modified_at TEXT,body TEXT,UNIQUE(parent,name));
            CREATE UNIQUE INDEX one_root ON resources((1)) WHERE parent IS NULL;
            CREATE TABLE results(subject TEXT,request_id TEXT,digest TEXT,body TEXT,PRIMARY KEY(subject,request_id));
            CREATE TABLE oauth_states(id TEXT PRIMARY KEY,expires TEXT,body TEXT);
            CREATE TABLE custodial_vault(subject TEXT PRIMARY KEY,signing_key_id TEXT,status TEXT);
            CREATE TABLE token_deliveries(credential_id TEXT,subject TEXT,recovery_verifier TEXT,recovery_expires_at TEXT,consumed_at TEXT,request_id TEXT);
            CREATE TABLE resource_tags(resource_id TEXT REFERENCES resources(id) ON DELETE CASCADE,tag TEXT,PRIMARY KEY(tag,resource_id));
            CREATE TABLE resource_path_aliases(parent_id TEXT REFERENCES resources(id) ON DELETE CASCADE,name TEXT,resource_id TEXT REFERENCES resources(id) ON DELETE CASCADE,PRIMARY KEY(parent_id,name));
            CREATE TABLE revisions(id TEXT PRIMARY KEY,resource_id TEXT REFERENCES resources(id),created_at TEXT,body TEXT);
            CREATE TABLE relations(revision_id TEXT REFERENCES revisions(id) ON DELETE CASCADE,source_id TEXT,type TEXT,target_id TEXT,target_revision TEXT,body TEXT);
            CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,body TEXT);
            CREATE TABLE audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,digest TEXT UNIQUE,previous TEXT,body TEXT);
            CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'append_only_audit'); END;
            CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'append_only_audit'); END;
        ").unwrap();
        fixture
    }
    fn db(&self) -> PathBuf {
        self.0.join("metadata.db")
    }
    fn writer(&self) -> SqliteWriter {
        SqliteWriter::open_existing(&self.db(), Duration::from_millis(40)).unwrap()
    }
    fn setting(&self, key: &str) -> Option<String> {
        Connection::open(self.db())
            .unwrap()
            .query_row("SELECT value FROM settings WHERE key=?", [key], |r| {
                r.get(0)
            })
            .optional()
            .unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
fn value() -> Json {
    Json::parse("1").unwrap()
}
fn effect(tx: &mut WriteSession<'_>, log: &Rc<RefCell<Vec<i32>>>, n: i32) {
    let log = Rc::clone(log);
    tx.on_rollback(move || {
        log.borrow_mut().push(n);
        Ok(())
    })
    .unwrap();
}
#[test]
fn commit_and_read_your_writes() {
    let fixture = Fixture::new();
    let mut writer = fixture.writer();
    let got = writer
        .transaction(|tx| {
            tx.set_setting("x", &value())?;
            tx.setting("x")?.unwrap().canonical()
        })
        .unwrap();
    assert_eq!(got, "1");
    assert_eq!(fixture.setting("x").as_deref(), Some("1"));
}
#[test]
fn handler_failure_rolls_back_in_reverse_order() {
    let fixture = Fixture::new();
    let mut writer = fixture.writer();
    let log = Rc::new(RefCell::new(Vec::new()));
    let result: std::result::Result<(), _> = writer.transaction(|tx| {
        effect(tx, &log, 1);
        tx.set_setting("x", &value())?;
        effect(tx, &log, 2);
        Err(Error("fixture_failure"))
    });
    assert_eq!(result.unwrap_err().cause, Error("fixture_failure"));
    assert_eq!(*log.borrow(), [2, 1]);
    assert!(fixture.setting("x").is_none());
    writer
        .transaction(|tx| tx.set_setting("x", &value()))
        .unwrap();
}
#[test]
fn swallowed_sql_failure_cannot_commit_partial_work() {
    let fixture = Fixture::new();
    let mut writer = fixture.writer();
    writer.connection.execute_batch("CREATE TRIGGER reject_bad BEFORE INSERT ON settings WHEN NEW.key='bad' BEGIN SELECT RAISE(ABORT,'fixture'); END;").unwrap();
    let error = writer
        .transaction(|tx| {
            tx.set_setting("x", &value())?;
            let _ = tx.set_setting("bad", &value());
            Ok(())
        })
        .unwrap_err();
    assert_eq!(error.cause, Error("constraint_conflict"));
    assert!(fixture.setting("x").is_none());
}
#[test]
fn sqlite_full_abort_cannot_restart_in_autocommit() {
    let fixture = Fixture::new();
    let mut writer = fixture.writer();
    writer.connection.execute_batch("CREATE TRIGGER reject_bad BEFORE INSERT ON settings WHEN NEW.key='bad' BEGIN SELECT RAISE(ROLLBACK,'fixture'); END;").unwrap();
    let error = writer
        .transaction(|tx| {
            tx.set_setting("x", &value())?;
            assert!(tx.set_setting("bad", &value()).is_err());
            assert!(tx.set_setting("autocommit_escape", &value()).is_err());
            Ok(())
        })
        .unwrap_err();
    assert_eq!(error.cause, Error("constraint_conflict"));
    assert!(fixture.setting("x").is_none());
    assert!(fixture.setting("autocommit_escape").is_none());
}
#[test]
fn savepoint_failure_isolated_from_outer_work() {
    let fixture = Fixture::new();
    let mut writer = fixture.writer();
    let log = Rc::new(RefCell::new(Vec::new()));
    writer
        .transaction(|tx| {
            effect(tx, &log, 1);
            tx.set_setting("outer", &value())?;
            let result: Result<()> = tx.savepoint(|tx| {
                tx.set_setting("inner", &value())?;
                effect(tx, &log, 2);
                Err(Error("fixture_failure"))
            });
            assert!(result.is_err());
            assert_eq!(*log.borrow(), [2]);
            assert!(tx.setting("inner")?.is_none());
            Ok(())
        })
        .unwrap();
    assert_eq!(*log.borrow(), [2]);
    assert!(fixture.setting("inner").is_none());
    assert!(fixture.setting("outer").is_some());
}
#[test]
fn released_inner_effects_remain_until_outer_commit() {
    let fixture = Fixture::new();
    let mut writer = fixture.writer();
    let log = Rc::new(RefCell::new(Vec::new()));
    let _: std::result::Result<(), _> = writer.transaction(|tx| {
        effect(tx, &log, 1);
        tx.savepoint(|tx| {
            effect(tx, &log, 2);
            tx.savepoint(|tx| {
                effect(tx, &log, 3);
                Ok(())
            })
        })?;
        Err(Error("fixture_failure"))
    });
    assert_eq!(*log.borrow(), [3, 2, 1]);
}
#[test]
fn ignored_inner_mutation_failure_is_rolled_back() {
    let fixture = Fixture::new();
    let mut writer = fixture.writer();
    writer.connection.execute_batch("CREATE TRIGGER reject_bad BEFORE INSERT ON settings WHEN NEW.key='bad' BEGIN SELECT RAISE(ABORT,'fixture'); END;").unwrap();
    writer
        .transaction(|tx| {
            tx.set_setting("outer", &value())?;
            assert!(tx
                .savepoint(|tx| {
                    tx.set_setting("inner", &value())?;
                    let _ = tx.set_setting("bad", &value());
                    Ok(())
                })
                .is_err());
            tx.set_setting("after", &value())
        })
        .unwrap();
    assert!(fixture.setting("inner").is_none());
    assert!(fixture.setting("after").is_some());
}
#[test]
fn deferred_commit_failure_returns_no_success_and_compensates() {
    let fixture = Fixture::new();
    let mut writer = fixture.writer();
    let log = Rc::new(RefCell::new(Vec::new()));
    let result = writer.transaction(|tx| {
        tx.set_setting("x", &value())?;
        effect(tx, &log, 1);
        // Test-only injection: callers have no public SQL/transaction handle.
        tx.transaction
            .execute(
                "INSERT INTO resources(id,parent) VALUES('orphan','missing')",
                [],
            )
            .map_err(database_error)?;
        Ok("must_not_escape")
    });
    assert_eq!(result.unwrap_err().cause, Error("constraint_conflict"));
    assert!(fixture.setting("x").is_none());
    assert_eq!(*log.borrow(), [1]);
}
#[test]
fn outer_panic_rolls_back_and_runs_all_effects() {
    let fixture = Fixture::new();
    let mut writer = fixture.writer();
    let log = Rc::new(RefCell::new(Vec::new()));
    assert!(catch_unwind(AssertUnwindSafe(|| {
        let _: std::result::Result<(), _> = writer.transaction(|tx| {
            tx.set_setting("x", &value())?;
            effect(tx, &log, 1);
            panic!("fixture panic")
        });
    }))
    .is_err());
    assert!(fixture.setting("x").is_none());
    assert_eq!(*log.borrow(), [1]);
    writer
        .transaction(|tx| tx.set_setting("after", &value()))
        .unwrap();
}
#[test]
fn inner_panic_can_be_caught_only_after_rollback() {
    let fixture = Fixture::new();
    let mut writer = fixture.writer();
    writer
        .transaction(|tx| {
            assert!(catch_unwind(AssertUnwindSafe(|| {
                let _: Result<()> = tx.savepoint(|tx| {
                    tx.set_setting("inner", &value())?;
                    panic!("fixture panic")
                });
            }))
            .is_err());
            tx.set_setting("after", &value())
        })
        .unwrap();
    assert!(fixture.setting("inner").is_none());
    assert!(fixture.setting("after").is_some());
}
#[test]
fn cleanup_failure_preserves_cause_continues_and_disables_writer() {
    let fixture = Fixture::new();
    let mut writer = fixture.writer();
    let log = Rc::new(RefCell::new(Vec::new()));
    let result: std::result::Result<(), _> = writer.transaction(|tx| {
        effect(tx, &log, 1);
        tx.on_rollback(|| Err(Error("cleanup_failed")))?;
        effect(tx, &log, 2);
        tx.on_rollback(|| panic!("cleanup panic"))?;
        Err(Error("primary"))
    });
    let error = result.unwrap_err();
    assert_eq!(error.cause, Error("primary"));
    assert_eq!(
        error.cleanup_errors,
        [Error("rollback_effect_panicked"), Error("cleanup_failed")]
    );
    assert_eq!(*log.borrow(), [2, 1]);
    assert_eq!(
        writer.transaction(|_| Ok(())).unwrap_err().cause,
        Error("storage_recovery_required")
    );
}
#[test]
fn writer_fence_covers_compensation_after_sqlite_abort() {
    let fixture = Fixture::new();
    let mut writer = fixture.writer();
    writer.connection.execute_batch("CREATE TRIGGER reject_bad BEFORE INSERT ON settings WHEN NEW.key='bad' BEGIN SELECT RAISE(ROLLBACK,'fixture'); END;").unwrap();
    let other_path = fixture.db();
    let result = writer.transaction(|tx| {
        tx.on_rollback(move || {
            let mut other = SqliteWriter::open_existing(&other_path, Duration::ZERO)?;
            assert_eq!(
                other
                    .transaction(|tx| tx.set_setting("interleaved", &value()))
                    .unwrap_err()
                    .cause,
                Error("server_busy")
            );
            Ok(())
        })?;
        tx.set_setting("bad", &value())
    });
    assert!(result.is_err());
    assert!(fixture.setting("interleaved").is_none());
    fixture
        .writer()
        .transaction(|tx| tx.set_setting("after", &value()))
        .unwrap();
}
#[test]
fn nested_full_abort_never_commits_outer_work() {
    let fixture = Fixture::new();
    let mut writer = fixture.writer();
    writer.connection.execute_batch("CREATE TRIGGER reject_bad BEFORE INSERT ON settings WHEN NEW.key='bad' BEGIN SELECT RAISE(ROLLBACK,'fixture'); END;").unwrap();
    let result = writer.transaction(|tx| {
        tx.set_setting("outer", &value())?;
        let _ = tx.savepoint(|tx| tx.set_setting("bad", &value()));
        assert!(tx.set_setting("escape", &value()).is_err());
        Ok(())
    });
    assert!(result.is_err());
    assert!(fixture.setting("outer").is_none());
    assert!(fixture.setting("escape").is_none());
}
#[test]
fn missing_bad_journal_or_schema_is_not_repaired() {
    let fixture = Fixture::new();
    assert!(SqliteWriter::open_existing(&fixture.0.join("missing"), Duration::ZERO).is_err());
    assert!(!fixture.0.join("missing").exists());
    let db = Connection::open(fixture.db()).unwrap();
    db.execute_batch("PRAGMA journal_mode=DELETE").unwrap();
    drop(db);
    assert!(matches!(
        SqliteWriter::open_existing(&fixture.db(), Duration::ZERO),
        Err(Error("unsupported_storage_journal"))
    ));
    let db = Connection::open(fixture.db()).unwrap();
    db.execute_batch("PRAGMA journal_mode=WAL; UPDATE schema_version SET version=99;")
        .unwrap();
    drop(db);
    assert!(matches!(
        SqliteWriter::open_existing(&fixture.db(), Duration::ZERO),
        Err(Error("unsupported_storage_schema"))
    ));
}
#[test]
fn lock_symlink_and_file_aliases_are_rejected() {
    let fixture = Fixture::new();
    let mut writer = fixture.writer();
    let target = fixture.0.join("target");
    fs::write(&target, "keep").unwrap();
    std::os::unix::fs::symlink(&target, fixture.0.join("metadata.db.writer.lock")).unwrap();
    assert_eq!(
        writer.transaction(|_| Ok(())).unwrap_err().cause,
        Error("unsafe_writer_lock")
    );
    assert_eq!(fs::read_to_string(target).unwrap(), "keep");
    let link = fixture.0.join("alias");
    std::os::unix::fs::symlink(fixture.db(), &link).unwrap();
    assert!(matches!(
        SqliteWriter::open_existing(&link, Duration::ZERO),
        Err(Error("unsafe_storage_path"))
    ));
    fs::hard_link(fixture.db(), fixture.0.join("hardlink")).unwrap();
    assert!(matches!(
        SqliteWriter::open_existing(&fixture.db(), Duration::ZERO),
        Err(Error("unsafe_storage_path"))
    ));
}
#[test]
fn replaced_database_and_excessive_timeout_fail_closed() {
    let fixture = Fixture::new();
    let mut writer = fixture.writer();
    assert!(matches!(
        SqliteWriter::open_existing(&fixture.db(), Duration::MAX),
        Err(Error("invalid_timeout"))
    ));
    fs::rename(fixture.db(), fixture.0.join("old.db")).unwrap();
    let replacement = Connection::open(fixture.db()).unwrap();
    drop(replacement);
    assert_eq!(
        writer.transaction(|_| Ok(())).unwrap_err().cause,
        Error("storage_recovery_required")
    );
}

#[test]
fn uncertain_commit_does_not_destroy_possibly_committed_external_data() {
    let fixture = Fixture::new();
    let mut writer = fixture.writer();
    let log = Rc::new(RefCell::new(Vec::new()));
    let result: std::result::Result<(), _> = writer.transaction(|tx| {
        tx.set_setting("committed", &value())?;
        effect(tx, &log, 1);
        // Deterministic commit-result injection, not a simulated physical disk:
        // commit really persisted, but the driver reports an I/O failure.
        tx.transaction.execute_batch("COMMIT").unwrap();
        Err(tx.commit_failed(rusqlite::Error::SqliteFailure(
            rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_IOERR),
            None,
        )))
    });
    assert_eq!(result.unwrap_err().cause, Error("commit_outcome_uncertain"));
    assert!(log.borrow().is_empty());
    assert!(fixture.setting("committed").is_some());
    assert_eq!(
        writer.transaction(|_| Ok(())).unwrap_err().cause,
        Error("storage_recovery_required")
    );
}
#[test]
fn missing_uniqueness_or_audit_guard_is_not_repaired() {
    for sql in ["DROP INDEX one_root", "DROP TRIGGER audit_no_delete", "DROP TABLE results; CREATE TABLE results(subject TEXT,request_id TEXT,digest TEXT,body TEXT)"] {
        let fixture = Fixture::new(); let db = Connection::open(fixture.db()).unwrap(); db.execute_batch(sql).unwrap(); drop(db);
        assert!(matches!(SqliteWriter::open_existing(&fixture.db(), Duration::ZERO), Err(Error("unsupported_storage_schema"))));
    }
}
