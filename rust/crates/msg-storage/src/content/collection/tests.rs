use super::*;
use crate::{
    content::ContentOptions,
    records::{decode, encode, BlobRef, Revision},
};
use std::{
    fs::{DirBuilder, File, FileTimes},
    os::unix::fs::{symlink, DirBuilderExt},
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};

static NEXT: AtomicU64 = AtomicU64::new(0);
struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "msg-content-gc-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        DirBuilder::new().mode(0o700).create(&path).unwrap();
        Self(path)
    }
    fn store(&self) -> GitContentStore {
        GitContentStore::open(&self.0.join("content"), ContentOptions::default()).unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn old(path: &Path) {
    File::open(path)
        .unwrap()
        .set_times(FileTimes::new().set_modified(SystemTime::now() - Duration::from_secs(7200)))
        .unwrap();
}
fn cutoff() -> SystemTime {
    SystemTime::now() - Duration::from_secs(3600)
}
fn revision(blob: &BlobRef, id: &str, parents: &[&str]) -> Revision {
    decode(
        &encode(
            &serde_json::json!({"format_version":1,"id":id,"resource_id":"r_test",
        "parents":parents,"content":blob,"relations":[],"actor":"u_alice","subject":"u_alice",
        "author":"u_alice","created_at":"2026-10-06T00:00:00.000000Z",
        "manifest_digest":format!("sha256:{}", "a".repeat(64)),"signature":null}),
        )
        .unwrap(),
    )
    .unwrap()
}

#[test]
fn roots_pins_and_grace_keep_bytes_and_history() {
    let f = Fixture::new();
    let store = f.store();
    let mut roots = BTreeSet::new();
    let mut retained = Vec::new();
    for media in ["text/plain", "application/octet-stream"] {
        for kind in ["root", "pin", "recovery", "young", "orphan"] {
            let bytes = format!("{media}:{kind}");
            let blob = store.put_bytes(bytes.as_bytes(), media, None).unwrap();
            let index = store.index.join(files::key(&blob.digest).unwrap());
            if kind != "young" {
                old(&index);
            }
            match kind {
                "root" => {
                    roots.insert(blob.digest.clone());
                }
                "pin" => store.pin(&blob, "held").unwrap(),
                "recovery" => store.pin(&blob, "msg:content-recovery:7").unwrap(),
                _ => (),
            }
            retained.push((kind, blob));
        }
    }
    let blob = &retained[0].1;
    let first = store
        .commit_revision("t", &revision(blob, "v_first", &[]))
        .unwrap();
    let second = store
        .commit_revision("t", &revision(blob, "v_second", &["v_first"]))
        .unwrap();
    old(&store.root.join("revisions/v_first"));
    old(&store.root.join("revisions/v_second"));
    let revisions = BTreeSet::from(["v_second".to_owned()]);
    assert_eq!(
        store
            .collect_unreferenced(&roots, &revisions, cutoff())
            .unwrap()
            .objects,
        2
    );
    for (kind, blob) in retained {
        if kind == "orphan" {
            assert!(!store.index.join(files::key(&blob.digest).unwrap()).exists());
        } else {
            assert!(store.read_bytes(&blob, 1024).is_ok());
        }
    }
    assert!(!store.root.join("revisions/v_first").exists());
    assert!(store.root.join("revisions/v_second").exists());
    let refs = store
        .git(
            &["for-each-ref", "--format=%(refname)", "refs/topics"],
            None,
        )
        .unwrap();
    assert_eq!(refs.lines().count(), 1);
    assert!(refs.ends_with(&files::hex(b"v_second")));
    assert!(store
        .git(&["cat-file", "commit", &second], None)
        .unwrap()
        .contains(&format!("parent {first}\n")));
    assert!(store.git(&["cat-file", "commit", &first], None).is_ok());
}

#[test]
fn lfs_repository_hard_link_outlives_content_index() {
    let f = Fixture::new();
    let store = f.store();
    let blob = store
        .put_bytes(b"shared bytes", "application/octet-stream", None)
        .unwrap();
    let oid = files::key(&blob.digest).unwrap();
    let source = f.0.join("source");
    fs::write(&source, b"shared bytes").unwrap();
    let repo = f.0.join("public.git");
    DirBuilder::new().mode(0o700).create(&repo).unwrap();
    let lfs = LfsObjectStore::open(&repo, &store.binary, Duration::from_secs(2)).unwrap();
    lfs.publish(oid, &source, blob.size as u64, None).unwrap();
    old(&store.index.join(oid));
    old(&store.binary.join(oid));
    let roots = BTreeSet::new();
    let collected = store
        .collect_unreferenced(&roots, &roots, cutoff())
        .unwrap();
    assert_eq!(collected.objects, 1);
    assert_eq!(collected.shared_objects, 0);
    assert!(store.binary.join(oid).exists());
    let mut bytes = Vec::new();
    lfs.read_to(oid, blob.size as u64, None, &mut bytes)
        .unwrap();
    assert_eq!(bytes, b"shared bytes");
    fs::remove_file(lfs.path(oid).unwrap()).unwrap();
    let collected = store
        .collect_unreferenced(&roots, &roots, cutoff())
        .unwrap();
    assert_eq!(collected.shared_objects, 1);
    assert_eq!(collected.shared_bytes, blob.size as u64);
    assert!(!store.binary.join(oid).exists());
}

#[test]
fn pending_journal_and_unsafe_pin_abort_before_collection() {
    let f = Fixture::new();
    let store = f.store();
    let blob = store
        .put_bytes(b"retained", "application/octet-stream", None)
        .unwrap();
    let index = store.index.join(files::key(&blob.digest).unwrap());
    old(&index);
    fs::write(store.root.join(JOURNAL), b"pending").unwrap();
    assert_eq!(
        store
            .collect_unreferenced(&BTreeSet::new(), &BTreeSet::new(), cutoff())
            .unwrap_err(),
        Error("content_collection_requires_committed_roots")
    );
    assert!(index.exists());
    fs::remove_file(store.root.join(JOURNAL)).unwrap();
    store.pin(&blob, "p").unwrap();
    let pin = store.pin_path(&blob, "p", false).unwrap();
    fs::remove_file(&pin).unwrap();
    symlink(&index, pin).unwrap();
    assert_eq!(
        store
            .collect_unreferenced(&BTreeSet::new(), &BTreeSet::new(), cutoff())
            .unwrap_err(),
        Error("unsafe_content_path")
    );
    assert!(index.exists());
}
