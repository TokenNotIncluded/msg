use super::*;
use std::{
    fs::DirBuilder,
    os::unix::fs::{symlink, DirBuilderExt},
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Barrier,
    },
    time::UNIX_EPOCH,
};

static NEXT: AtomicU64 = AtomicU64::new(0);
struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "msg-lfs-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        DirBuilder::new().mode(0o700).create(&path).unwrap();
        Self(path)
    }
    fn store(&self, name: &str) -> LfsObjectStore {
        let repo = self.0.join(name);
        DirBuilder::new().mode(0o700).create(&repo).unwrap();
        LfsObjectStore::open(&repo, &self.0.join("shared"), Duration::from_millis(40)).unwrap()
    }
    fn stage(&self, bytes: &[u8]) -> (String, PathBuf) {
        let digest = files::hex(bytes);
        let path = self.0.join(format!("staged-{digest}"));
        fs::write(&path, bytes).unwrap();
        (digest, path)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn repositories_share_one_verified_inode_and_only_charge_new_bytes() {
    let f = Fixture::new();
    let a = f.store("a.git");
    let b = f.store("b.git");
    let data = b"shared\0bytes";
    let (digest, staged) = f.stage(data);
    assert!(a
        .publish(&digest, &staged, data.len() as u64, Some(data.len() as u64))
        .unwrap());
    let canonical = a.shared.join(&digest);
    let inode = fs::metadata(&canonical).unwrap().ino();
    assert!(!a
        .publish(&digest, &staged, data.len() as u64, Some(0))
        .unwrap());
    assert!(b
        .publish(&digest, &staged, data.len() as u64, Some(0))
        .unwrap());
    assert_eq!(a.shared_usage().unwrap(), data.len() as u64);
    for store in [&a, &b] {
        let path = store.path(&digest).unwrap();
        assert!(path.ends_with(
            Path::new("lfs/objects")
                .join(&digest[..2])
                .join(&digest[2..4])
                .join(&digest)
        ));
        assert_eq!(fs::metadata(&path).unwrap().ino(), inode);
        assert_eq!(store.size(&digest).unwrap(), Some(data.len() as u64));
        let mut read = Vec::new();
        store
            .read_to(&digest, data.len() as u64, Some((2, 9)), &mut read)
            .unwrap();
        assert_eq!(read, data[2..9]);
    }
    assert_eq!(fs::metadata(canonical).unwrap().nlink(), 3);
    let (other, source) = f.stage(b"extra");
    assert_eq!(
        a.publish(&other, &source, 5, Some(data.len() as u64))
            .unwrap_err(),
        Error("lfs_deployment_capacity_exceeded")
    );
    assert_eq!(a.size(&other).unwrap(), None);
}

#[test]
fn legacy_reads_do_not_create_shared_store_and_migration_is_explicit() {
    let f = Fixture::new();
    let store = f.store("legacy.git");
    let (digest, _) = f.stage(b"legacy");
    let path = store.path(&digest).unwrap();
    directory(path.parent().unwrap()).unwrap();
    fs::write(&path, b"legacy").unwrap();
    let original = fs::metadata(&path).unwrap().ino();
    assert_eq!(store.size(&digest).unwrap(), Some(6));
    let mut read = Vec::new();
    store.read_to(&digest, 6, None, &mut read).unwrap();
    assert_eq!(read, b"legacy");
    assert!(!store.shared.exists());
    assert_eq!(fs::metadata(&path).unwrap().ino(), original);
    assert_eq!(
        store.migrate_legacy(&digest, Some(6)).unwrap(),
        LfsMigration {
            changed: true,
            size: 6
        }
    );
    let shared = fs::metadata(store.shared.join(&digest)).unwrap();
    assert_eq!(fs::metadata(&path).unwrap().ino(), shared.ino());
    assert_ne!(shared.ino(), original);
    assert_eq!(
        store.migrate_legacy(&digest, Some(0)).unwrap(),
        LfsMigration {
            changed: false,
            size: 6
        }
    );
    assert_eq!(fs::read(path).unwrap(), b"legacy");
}

#[test]
fn corruption_and_bad_sizes_never_publish_or_reach_a_read_sink() {
    let f = Fixture::new();
    let store = f.store("a.git");
    let (digest, source) = f.stage(b"correct");
    for size in [0, 6, 8, u64::MAX] {
        assert_eq!(
            store.publish(&digest, &source, size, None).unwrap_err(),
            Error("lfs_size_mismatch")
        );
        assert_eq!(store.size(&digest).unwrap(), None);
    }
    store.publish(&digest, &source, 7, None).unwrap();
    let canonical = store.shared.join(&digest);
    let inode = fs::metadata(&canonical).unwrap().ino();
    fs::write(&source, b"corrupt").unwrap();
    assert_eq!(
        store.publish(&digest, &source, 7, None).unwrap_err(),
        Error("lfs_digest_mismatch")
    );
    assert_eq!(fs::metadata(&canonical).unwrap().ino(), inode);
    assert_eq!(fs::read(&canonical).unwrap(), b"correct");
    fs::write(&canonical, b"corrupt").unwrap();
    let mut sink = Vec::new();
    assert_eq!(
        store
            .read_to(&digest, 7, Some((0, 0)), &mut sink)
            .unwrap_err(),
        Error("lfs_digest_mismatch")
    );
    assert!(sink.is_empty());
    assert_eq!(
        store
            .read_to(&digest, 7, Some((4, 3)), &mut sink)
            .unwrap_err(),
        Error("invalid_byte_range")
    );
}

#[test]
fn paths_reject_symlinks_and_non_objects() {
    let f = Fixture::new();
    let store = f.store("a.git");
    for value in [
        "../escape".to_owned(),
        "A".repeat(64),
        "0".repeat(63),
        "0".repeat(65),
    ] {
        assert_eq!(store.size(&value).unwrap_err(), Error("invalid_lfs_oid"));
    }
    let (digest, source) = f.stage(b"abc");
    let link = f.0.join("source-link");
    symlink(&source, &link).unwrap();
    assert_eq!(
        store.publish(&digest, &link, 3, None).unwrap_err(),
        Error("lfs_object_conflict")
    );
    let path = store.path(&digest).unwrap();
    directory(path.parent().unwrap()).unwrap();
    symlink(&source, &path).unwrap();
    assert_eq!(
        store.size(&digest).unwrap_err(),
        Error("lfs_object_conflict")
    );
    assert_eq!(
        store.publish(&digest, &source, 3, None).unwrap_err(),
        Error("lfs_object_conflict")
    );
    fs::remove_file(path).unwrap();
    symlink(&source, store.shared.join(&digest)).unwrap();
    assert_eq!(
        store.publish(&digest, &source, 3, None).unwrap_err(),
        Error("lfs_object_conflict")
    );
    assert_eq!(
        store.shared_usage().unwrap_err(),
        Error("lfs_object_conflict")
    );
}

#[test]
fn collector_keeps_repository_links_index_references_and_recent_objects() {
    let f = Fixture::new();
    let store = f.store("a.git");
    let index = f.0.join("index");
    directory(&index).unwrap();
    let mut digests = Vec::new();
    for data in [b"linked".as_slice(), b"indexed", b"orphan"] {
        let (digest, source) = f.stage(data);
        store
            .publish(&digest, &source, data.len() as u64, None)
            .unwrap();
        digests.push(digest);
    }
    fs::remove_file(store.path(&digests[1]).unwrap()).unwrap();
    fs::remove_file(store.path(&digests[2]).unwrap()).unwrap();
    fs::write(index.join(&digests[1]), b"index").unwrap();
    assert_eq!(
        store.collect_unlinked(UNIX_EPOCH, &index).unwrap(),
        LfsCollection::default()
    );
    let future = SystemTime::now() + Duration::from_secs(1);
    assert_eq!(
        store.collect_unlinked(future, &index).unwrap(),
        LfsCollection {
            objects: 1,
            bytes: 6
        }
    );
    assert!(store.shared.join(&digests[0]).exists());
    assert!(store.shared.join(&digests[1]).exists());
    assert!(!store.shared.join(&digests[2]).exists());
    fs::remove_file(store.path(&digests[0]).unwrap()).unwrap();
    fs::remove_file(index.join(&digests[1])).unwrap();
    assert_eq!(
        store.collect_unlinked(future, &index).unwrap(),
        LfsCollection {
            objects: 2,
            bytes: 13
        }
    );
}

#[test]
fn python_compatible_lock_serializes_publication_and_capacity() {
    let f = Fixture::new();
    let mut a = f.store("a.git");
    let mut b = f.store("b.git");
    let first = f.stage(b"first");
    let second = f.stage(b"other");
    let held = a.lock().unwrap();
    assert_eq!(
        b.publish(&first.0, &first.1, 5, Some(5)).unwrap_err(),
        Error("server_busy")
    );
    drop(held);
    a.timeout = Duration::from_secs(2);
    b.timeout = Duration::from_secs(2);
    let barrier = Arc::new(Barrier::new(2));
    let mut children = Vec::new();
    for (store, (digest, source)) in [(a.clone(), first), (b, second)] {
        let barrier = Arc::clone(&barrier);
        children.push(thread::spawn(move || {
            barrier.wait();
            store.publish(&digest, &source, 5, Some(5))
        }));
    }
    let results: Vec<_> = children.into_iter().map(|t| t.join().unwrap()).collect();
    assert_eq!(results.iter().filter(|r| r.is_ok()).count(), 1);
    assert_eq!(
        results
            .iter()
            .filter(|r| **r == Err(Error("lfs_deployment_capacity_exceeded")))
            .count(),
        1
    );
    assert_eq!(a.shared_usage().unwrap(), 5);
}
