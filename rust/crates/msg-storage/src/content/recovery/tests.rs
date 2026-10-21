use super::*;
use crate::content::ContentOptions;
use rusqlite::{Connection, OptionalExtension};
use std::{
    fs::DirBuilder,
    os::unix::{
        fs::{symlink, DirBuilderExt},
        process::ExitStatusExt,
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
    thread,
    time::{Duration, Instant},
};

const DATA: &[u8] = b"durable content";
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "msg-content-recovery-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        DirBuilder::new().mode(0o700).create(&path).unwrap();
        let fixture = Self(path);
        Connection::open(fixture.db()).unwrap().execute_batch(
            "PRAGMA journal_mode=WAL;
            CREATE TABLE schema_version(version INTEGER PRIMARY KEY); INSERT INTO schema_version VALUES(1);
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE identities(id TEXT PRIMARY KEY,kind TEXT,body TEXT);
            CREATE TABLE credentials(id TEXT PRIMARY KEY,subject TEXT,body TEXT);
            CREATE TABLE certificates(id TEXT PRIMARY KEY,subject TEXT,parent TEXT,revoked INTEGER,body TEXT);
            CREATE TABLE resources(id TEXT PRIMARY KEY,type TEXT,name TEXT,parent TEXT REFERENCES resources(id) DEFERRABLE INITIALLY DEFERRED,owner TEXT,grp TEXT,mode INTEGER,generation INTEGER,revision TEXT,state TEXT,created_at TEXT,modified_at TEXT,body TEXT,UNIQUE(parent,name));
            CREATE UNIQUE INDEX one_root ON resources((1)) WHERE parent IS NULL;
            CREATE TABLE results(subject TEXT,request_id TEXT,digest TEXT,body TEXT,PRIMARY KEY(subject,request_id));
            CREATE TABLE credential_states(id TEXT PRIMARY KEY,kind TEXT,expires TEXT,body TEXT);
            CREATE TABLE custodial_vault(subject TEXT PRIMARY KEY,signing_key_id TEXT,status TEXT);
            CREATE TABLE token_deliveries(credential_id TEXT,subject TEXT,recovery_verifier TEXT,recovery_expires_at TEXT,consumed_at TEXT,request_id TEXT);
            CREATE TABLE resource_tags(resource_id TEXT REFERENCES resources(id) ON DELETE CASCADE,tag TEXT,PRIMARY KEY(tag,resource_id));
            CREATE TABLE resource_path_aliases(parent_id TEXT REFERENCES resources(id) ON DELETE CASCADE,name TEXT,resource_id TEXT REFERENCES resources(id) ON DELETE CASCADE,PRIMARY KEY(parent_id,name));
            CREATE TABLE revisions(id TEXT PRIMARY KEY,resource_id TEXT REFERENCES resources(id),created_at TEXT,body TEXT);
            CREATE TABLE relations(revision_id TEXT REFERENCES revisions(id) ON DELETE CASCADE,source_id TEXT,type TEXT,target_id TEXT,target_revision TEXT,body TEXT);
            CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,body TEXT);
            CREATE TABLE audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,digest TEXT UNIQUE,previous TEXT,body TEXT);
            CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'append_only_audit'); END;
            CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'append_only_audit'); END;",
        ).unwrap();
        fixture
    }
    fn db(&self) -> PathBuf {
        self.0.join("metadata.db")
    }
    fn writer(&self) -> SqliteWriter {
        SqliteWriter::open_existing(&self.db(), Duration::from_secs(1)).unwrap()
    }
    fn store(&self) -> GitContentStore {
        GitContentStore::open(&self.0.join("store"), ContentOptions::default()).unwrap()
    }
    fn setting(&self, key: &str) -> Option<String> {
        Connection::open(self.db())
            .unwrap()
            .query_row("SELECT value FROM settings WHERE key=?", [key], |row| {
                row.get(0)
            })
            .optional()
            .unwrap()
    }
    fn set(&self, key: &str, value: &str) {
        Connection::open(self.db()).unwrap().execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [key, value],
        ).unwrap();
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn blob(media: &str) -> BlobRef {
    BlobRef {
        digest: msg_core::digest(DATA),
        size: DATA.len() as i64,
        media_type: media.to_owned(),
    }
}

#[test]
fn committed_pin_removal_retains_recovery_hold_until_next_fenced_transaction() {
    let f = Fixture::new();
    let content = f.store();
    let blob = content
        .put_bytes(DATA, "application/octet-stream", None)
        .unwrap();
    content.pin(&blob, "old").unwrap();
    f.writer()
        .content_transaction(&content, |unit| unit.unpin(&blob, "old"))
        .unwrap();
    assert!(!content.pinned(&blob, "old").unwrap());
    assert!(content.pinned(&blob, "msg:content-recovery:1").unwrap());
    assert_eq!(f.setting(GENERATION).as_deref(), Some("1"));
    assert!(content.root.join(JOURNAL).exists());
    f.writer()
        .content_transaction(&content, |_| Ok(()))
        .unwrap();
    assert!(!content.pinned(&blob, "old").unwrap());
    assert!(!content.pinned(&blob, "msg:content-recovery:1").unwrap());
    assert!(!content.root.join(JOURNAL).exists());
}

#[test]
fn rollback_and_repeated_recovery_restore_pins_in_reverse_order() {
    let f = Fixture::new();
    let content = f.store();
    let blob = content.put_bytes(DATA, "text/plain", None).unwrap();
    content.pin(&blob, "old").unwrap();
    let error = f
        .writer()
        .content_transaction(&content, |unit| {
            unit.unpin(&blob, "old")?;
            unit.pin(&blob, "old")?;
            unit.unpin(&blob, "old")?;
            unit.pin(&blob, "new")?;
            unit.metadata()
                .set_setting("domain-row", &Json::string("uncommitted"))?;
            Err::<(), _>(Error("rejected"))
        })
        .unwrap_err();
    assert_eq!(error.cause, Error("rejected"));
    assert!(error.cleanup_errors.is_empty());
    assert!(content.pinned(&blob, "old").unwrap());
    assert!(!content.pinned(&blob, "new").unwrap());
    assert_eq!(f.setting("domain-row"), None);
    assert_eq!(f.setting(GENERATION), None);
    for _ in 0..2 {
        f.writer()
            .content_transaction(&content, |_| Ok(()))
            .unwrap();
        assert!(content.pinned(&blob, "old").unwrap());
        assert!(!content.pinned(&blob, "new").unwrap());
    }
    assert!(!content.pinned(&blob, "msg:content-recovery:1").unwrap());
}

#[test]
fn caught_content_errors_cannot_commit_domain_rows() {
    let f = Fixture::new();
    let content = f.store();
    let error = f
        .writer()
        .content_transaction(&content, |unit| {
            let expected = msg_core::digest(b"other bytes");
            assert_eq!(
                unit.put_bytes(DATA, "text/plain", Some(&expected)).err(),
                Some(Error("digest_mismatch"))
            );
            unit.metadata()
                .set_setting("domain-row", &Json::string("must roll back"))
        })
        .unwrap_err();
    assert_eq!(error.cause, Error("digest_mismatch"));
    assert_eq!(f.setting("domain-row"), None);
    f.writer()
        .content_transaction(&content, |_| Ok(()))
        .unwrap();
    assert!(!content.root.join(JOURNAL).exists());
    let blob = content.put_bytes(DATA, "text/plain", None).unwrap();
    content.pin(&blob, "msg:content-recovery:1").unwrap();
    assert_eq!(
        f.writer()
            .content_transaction(&content, |unit| unit.unpin(&blob, "msg:content-recovery:1"))
            .unwrap_err()
            .cause,
        Error("invalid_content_lease")
    );
    assert!(content.pinned(&blob, "msg:content-recovery:1").unwrap());
}

#[test]
fn corrupt_unknown_and_restored_journals_fail_closed_across_reopen() {
    let f = Fixture::new();
    let content = f.store();
    let blob = content
        .put_bytes(DATA, "application/octet-stream", None)
        .unwrap();
    let path = content.root.join(JOURNAL);
    let valid =
        r#"{"format_version":1,"before":null,"after":1,"runtime_generation":null,"undo":[]}"#;
    for bytes in [
        "{}",
        r#"{"format_version":1,"before":null,"after":1,"after":1,"runtime_generation":null,"undo":[]}"#,
        r#"{"format_version":1,"before":null,"after":1,"undo":[]}"#,
        r#"{"format_version":1,"before":null,"after":1,"runtime_generation":null,"undo":[],"extra":true}"#,
        r#"{"format_version":1,"before":null,"after":2,"runtime_generation":null,"undo":[]}"#,
    ] {
        files::replace(&path, bytes.as_bytes(), false).unwrap();
        for _ in 0..2 {
            assert_eq!(
                f.writer()
                    .content_transaction(&content, |_| Ok(()))
                    .unwrap_err()
                    .cause,
                Error("storage_recovery_required")
            );
            assert_eq!(fs::read_to_string(&path).unwrap(), bytes);
        }
    }
    files::replace(&path, valid.as_bytes(), false).unwrap();
    f.set(GENERATION, "9");
    assert_eq!(
        f.writer()
            .content_transaction(&content, |_| Ok(()))
            .unwrap_err()
            .cause,
        Error("storage_recovery_required")
    );
    f.set(GENERATION, "1");
    f.set(RUNTIME_GENERATION, "\"restored-generation\"");
    assert_eq!(
        f.writer()
            .content_transaction(&content, |_| Ok(()))
            .unwrap_err()
            .cause,
        Error("storage_recovery_required")
    );
    f.set("recovery_quarantine", "null");
    assert_eq!(
        f.writer()
            .content_transaction(&content, |_| Ok(()))
            .unwrap_err()
            .cause,
        Error("recovery_quarantined")
    );
    assert_eq!(content.read_bytes(&blob, 1024).unwrap(), DATA);
    assert!(path.exists());
}

#[test]
fn journal_paths_reject_symlinks_and_extra_hard_links() {
    let f = Fixture::new();
    let content = f.store();
    f.writer()
        .content_transaction(&content, |unit| {
            unit.put_bytes(DATA, "application/octet-stream", None)
                .map(|_| ())
        })
        .unwrap();
    let path = content.root.join(JOURNAL);
    let extra = f.0.join("extra-journal");
    fs::hard_link(&path, &extra).unwrap();
    assert_eq!(
        f.writer()
            .content_transaction(&content, |_| Ok(()))
            .unwrap_err()
            .cause,
        Error("storage_recovery_required")
    );
    fs::remove_file(&path).unwrap();
    symlink(&extra, &path).unwrap();
    assert_eq!(
        f.writer()
            .content_transaction(&content, |_| Ok(()))
            .unwrap_err()
            .cause,
        Error("storage_recovery_required")
    );
    assert!(extra.exists());
}

struct Running(Child);
impl Drop for Running {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn killed_child(root: &Path, case: &str, media: &str) {
    let mut child = Running(
        Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "content::recovery::tests::crash_child",
                "--ignored",
                "--nocapture",
            ])
            .env("MSG_CONTENT_CRASH_ROOT", root)
            .env("MSG_CONTENT_CRASH_CASE", case)
            .env("MSG_CONTENT_CRASH_MEDIA", media)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let started = Instant::now();
    while !root.join("ready").exists() {
        assert!(
            child.0.try_wait().unwrap().is_none(),
            "crash fixture exited early"
        );
        assert!(
            started.elapsed() < Duration::from_secs(10),
            "crash fixture did not become ready"
        );
        thread::sleep(Duration::from_millis(10));
    }
    child.0.kill().unwrap();
    assert_eq!(child.0.wait().unwrap().signal(), Some(9));
}

#[test]
fn sigkill_before_and_after_sql_commit_recover_binary_and_git_pins() {
    for media in ["application/octet-stream", "text/plain"] {
        for case in ["before-commit", "after-commit"] {
            let f = Fixture::new();
            let content = f.store();
            let blob = content.put_bytes(DATA, media, None).unwrap();
            content.pin(&blob, "old").unwrap();
            killed_child(&f.0, case, media);
            assert!(!content.pinned(&blob, "old").unwrap());
            assert!(content.pinned(&blob, "new").unwrap());
            assert!(content.pinned(&blob, "msg:content-recovery:1").unwrap());
            f.writer()
                .content_transaction(&content, |_| Ok(()))
                .unwrap();
            let committed = case == "after-commit";
            assert_eq!(content.pinned(&blob, "old").unwrap(), !committed);
            assert_eq!(content.pinned(&blob, "new").unwrap(), committed);
            assert_eq!(f.setting("domain-row").is_some(), committed);
            assert!(!content.pinned(&blob, "msg:content-recovery:1").unwrap());
            assert!(!content.root.join(JOURNAL).exists());
            assert_eq!(content.read_bytes(&blob, 1024).unwrap(), DATA);
        }
    }
}

/// Spawned by the parent test, which kills this process with SIGKILL. Ordinary
/// test runs skip it; no unwinding or rollback callback runs at the crash point.
#[test]
#[ignore]
fn crash_child() {
    let Ok(root) = std::env::var("MSG_CONTENT_CRASH_ROOT") else {
        return;
    };
    let root = PathBuf::from(root);
    let media = std::env::var("MSG_CONTENT_CRASH_MEDIA").unwrap();
    let case = std::env::var("MSG_CONTENT_CRASH_CASE").unwrap();
    let content = GitContentStore::open(&root.join("store"), ContentOptions::default()).unwrap();
    let mut writer =
        SqliteWriter::open_existing(&root.join("metadata.db"), Duration::from_secs(1)).unwrap();
    writer
        .content_transaction(&content, |unit| {
            unit.unpin(&blob(&media), "old")?;
            unit.pin(&blob(&media), "new")?;
            unit.metadata()
                .set_setting("domain-row", &Json::string("committed"))?;
            if case == "before-commit" {
                fs::write(root.join("ready"), b"ready").unwrap();
                loop {
                    thread::park();
                }
            }
            Ok(())
        })
        .unwrap();
    fs::write(root.join("ready"), b"ready").unwrap();
    loop {
        thread::park();
    }
}
