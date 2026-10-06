use super::*;
use crate::records::decode;
use std::{
    fs::{DirBuilder, Permissions},
    io,
    os::unix::fs::{symlink, DirBuilderExt, MetadataExt, PermissionsExt},
    sync::atomic::{AtomicU64, Ordering},
};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "msg-native-content-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        DirBuilder::new().mode(0o700).create(&path).unwrap();
        Self(path)
    }
    fn store(&self) -> GitContentStore {
        GitContentStore::open(&self.0.join("store"), ContentOptions::default()).unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
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
fn storage_kinds_and_empty_content_round_trip() {
    let f = Fixture::new();
    let store = f.store();
    for media in [
        "text/markdown",
        "text/plain",
        "application/json",
        "application/msg-template",
        "application/octet-stream",
    ] {
        for data in [b"".as_slice(), "Hello\0中文\n".as_bytes()] {
            let blob = store.put_bytes(data, media, None).unwrap();
            assert_eq!(store.read_bytes(&blob, 1024).unwrap(), data);
            assert_eq!(blob.digest, msg_core::digest(data));
        }
    }
    assert_eq!(fs::read_dir(&store.staging).unwrap().count(), 0);
}
#[test]
fn ranges_cross_buffers_and_reject_invalid_bounds() {
    let f = Fixture::new();
    let store = f.store();
    let data: Vec<u8> = (0..200_001).map(|n| (n % 251) as u8).collect();
    for media in ["text/plain", "application/octet-stream"] {
        let blob = store.put_bytes(&data, media, None).unwrap();
        for (start, end) in [
            (0, 0),
            (0, 1),
            (65_532, 65_540),
            (5, 180_000),
            (200_001, 200_001),
        ] {
            let mut output = Vec::new();
            store
                .read_to(&blob, Some((start, end)), &mut output)
                .unwrap();
            assert_eq!(output, data[start as usize..end as usize]);
        }
        assert_eq!(
            store.read_to(&blob, Some((2, 1)), &mut Vec::new()).err(),
            Some(Error("invalid_byte_range"))
        );
        assert_eq!(
            store
                .read_to(&blob, Some((0, 200_002)), &mut Vec::new())
                .err(),
            Some(Error("invalid_byte_range"))
        );
        assert_eq!(
            store.read_bytes(&blob, 200_000).err(),
            Some(Error("use_transfer"))
        );
    }
}
#[test]
fn failed_source_quota_and_digest_do_not_publish() {
    struct Broken;
    impl Read for Broken {
        fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
            Err(io::Error::other("fixture"))
        }
    }
    let f = Fixture::new();
    let store = f.store();
    assert_eq!(
        store.put(&mut Broken, "text/plain", None, 1024).err(),
        Some(Error("content_store_error"))
    );
    assert_eq!(
        store.put(&mut &b"abc"[..], "text/plain", None, 2).err(),
        Some(Error("content_too_large"))
    );
    assert_eq!(
        store
            .put_bytes(b"abc", "text/plain", Some(&msg_core::digest(b"different")))
            .err(),
        Some(Error("digest_mismatch"))
    );
    assert_eq!(
        store
            .put_bytes(b"abc", "text/plain", Some("sha256:../wrong"))
            .err(),
        Some(Error("invalid_digest"))
    );
    for path in [&store.staging, &store.index, &store.binary] {
        assert_eq!(fs::read_dir(path).unwrap().count(), 0);
    }
    assert_eq!(store.git(&["for-each-ref"], None).unwrap(), "");
}
#[test]
fn binary_duplicate_keeps_existing_hard_link_inode() {
    let f = Fixture::new();
    let store = f.store();
    let blob = store
        .put_bytes(b"same binary", "application/octet-stream", None)
        .unwrap();
    let path = store.binary.join(files::key(&blob.digest).unwrap());
    let link = f.0.join("lfs-link");
    fs::hard_link(&path, &link).unwrap();
    let before = fs::metadata(&path).unwrap();
    store
        .put_bytes(b"same binary", "application/octet-stream", None)
        .unwrap();
    assert_eq!(fs::metadata(&path).unwrap().ino(), before.ino());
    assert_eq!(fs::metadata(link).unwrap().nlink(), 2);
}
#[test]
fn corrupt_binary_never_reaches_sink_or_gets_replaced() {
    let f = Fixture::new();
    let store = f.store();
    let blob = store
        .put_bytes(b"correct", "application/octet-stream", None)
        .unwrap();
    let path = store.binary.join(files::key(&blob.digest).unwrap());
    fs::write(&path, b"corrupt").unwrap();
    let mut sink = Vec::new();
    assert_eq!(
        store.read_to(&blob, None, &mut sink).err(),
        Some(Error("content_digest_mismatch"))
    );
    assert!(sink.is_empty());
    assert_eq!(
        store
            .put_bytes(b"correct", "application/octet-stream", None)
            .err(),
        Some(Error("content_digest_mismatch"))
    );
    assert_eq!(fs::read(path).unwrap(), b"corrupt");
}
#[test]
fn corrupt_index_and_truncated_binary_fail_closed() {
    let f = Fixture::new();
    let store = f.store();
    let blob = store
        .put_bytes(b"correct", "application/octet-stream", None)
        .unwrap();
    fs::write(store.binary.join(files::key(&blob.digest).unwrap()), b"x").unwrap();
    assert_eq!(
        store.read_bytes(&blob, 1024).err(),
        Some(Error("content_size_mismatch"))
    );
    let index = store.index.join(files::key(&blob.digest).unwrap());
    for value in [
        r#"{"kind":"git","oid":"--help","size":7}"#,
        r#"{"kind":"binary","size":7,"size":7}"#,
        r#"{"kind":"binary","size":-1}"#,
    ] {
        fs::write(&index, value).unwrap();
        assert!(store.read_bytes(&blob, 1024).is_err());
    }
}
#[test]
fn bad_digest_and_negative_size_cannot_escape_root() {
    let f = Fixture::new();
    let store = f.store();
    for digest in [
        "../outside",
        "sha256:../../etc/passwd",
        "sha256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    ] {
        let blob = BlobRef {
            digest: digest.into(),
            size: 1,
            media_type: "text/plain".into(),
        };
        assert_eq!(
            store.read_bytes(&blob, 10).err(),
            Some(Error("invalid_digest"))
        );
    }
    let blob = BlobRef {
        digest: msg_core::digest(b""),
        size: -1,
        media_type: "text/plain".into(),
    };
    assert_eq!(
        store.read_bytes(&blob, 10).err(),
        Some(Error("invalid_blob_size"))
    );
}
#[test]
fn symlink_roots_indices_and_binary_objects_are_rejected() {
    let f = Fixture::new();
    let store = f.store();
    symlink(&store.root, f.0.join("alias")).unwrap();
    assert!(GitContentStore::open(&f.0.join("alias"), ContentOptions::default()).is_err());
    let blob = store
        .put_bytes(b"abc", "application/octet-stream", None)
        .unwrap();
    let path = store.binary.join(files::key(&blob.digest).unwrap());
    let external = f.0.join("external");
    fs::write(&external, b"abc").unwrap();
    fs::remove_file(&path).unwrap();
    symlink(&external, &path).unwrap();
    assert_eq!(
        store.read_bytes(&blob, 10).err(),
        Some(Error("unsafe_content_path"))
    );
    assert_eq!(
        store
            .put_bytes(b"abc", "application/octet-stream", None)
            .err(),
        Some(Error("unsafe_content_path"))
    );
    let index = store.index.join(files::key(&blob.digest).unwrap());
    fs::remove_file(&index).unwrap();
    symlink(&external, index).unwrap();
    assert_eq!(
        store.read_bytes(&blob, 10).err(),
        Some(Error("unsafe_content_path"))
    );
    assert_eq!(fs::read(external).unwrap(), b"abc");
}
#[test]
fn pins_are_hashed_and_idempotent() {
    let f = Fixture::new();
    let store = f.store();
    for media in ["text/plain", "application/octet-stream"] {
        let blob = store.put_bytes(b"pin me", media, None).unwrap();
        let lease = "../../untrusted/lease\0中文";
        assert!(!store.pinned(&blob, lease).unwrap());
        store.pin(&blob, lease).unwrap();
        store.pin(&blob, lease).unwrap();
        assert!(store.pinned(&blob, lease).unwrap());
        store.unpin(&blob, lease).unwrap();
        store.unpin(&blob, lease).unwrap();
        assert!(!store.pinned(&blob, lease).unwrap());
    }
}
#[test]
fn revisions_are_canonical_parented_and_retryable() {
    let f = Fixture::new();
    let store = f.store();
    let blob = store.put_bytes(b"revision", "text/plain", None).unwrap();
    let first = revision(&blob, "v_first", &[]);
    let oid = store.commit_revision("t_中文", &first).unwrap();
    assert_eq!(store.commit_revision("t_中文", &first).unwrap(), oid);
    assert_eq!(
        store
            .git(&["show", &format!("{oid}:manifest.json")], None)
            .unwrap(),
        encode(&first).unwrap().canonical().unwrap()
    );
    let child = store
        .commit_revision("t_中文", &revision(&blob, "v_second", &["v_first"]))
        .unwrap();
    assert!(store
        .git(&["cat-file", "commit", &child], None)
        .unwrap()
        .contains(&format!("parent {oid}\n")));
    let mut conflicting = first.clone();
    conflicting.author = "u_other".into();
    assert_eq!(
        store.commit_revision("t_中文", &conflicting).err(),
        Some(Error("revision_content_conflict"))
    );
    assert_eq!(
        fs::read_to_string(store.root.join("revisions/v_first")).unwrap(),
        format!("{oid}\n")
    );
}
#[test]
fn revision_paths_and_missing_parents_are_checked() {
    let f = Fixture::new();
    let store = f.store();
    let blob = store.put_bytes(b"x", "text/plain", None).unwrap();
    assert_eq!(
        store
            .commit_revision("t", &revision(&blob, "../../escape", &[]))
            .err(),
        Some(Error("invalid_revision_id"))
    );
    assert_eq!(
        store
            .commit_revision("t", &revision(&blob, "v_second", &["missing"]))
            .err(),
        Some(Error("revision_content_missing"))
    );
    assert!(!store.root.join("revisions/v_second").exists());
}
#[test]
fn shared_layout_requires_explicit_permission_migration() {
    let f = Fixture::new();
    let store = f.store();
    let old = fs::metadata(&store.root).unwrap().mode();
    assert!(GitContentStore::open(
        &store.root,
        ContentOptions {
            group_read: true,
            ..ContentOptions::default()
        }
    )
    .is_err());
    assert_eq!(fs::metadata(&store.root).unwrap().mode(), old);
}
#[test]
fn newly_created_shared_files_have_the_intended_mode() {
    let f = Fixture::new();
    let store = GitContentStore::open(
        &f.0.join("shared"),
        ContentOptions {
            group_read: true,
            ..ContentOptions::default()
        },
    )
    .unwrap();
    let blob = store
        .put_bytes(b"group", "application/octet-stream", None)
        .unwrap();
    for dir in [&store.root, &store.binary, &store.index] {
        assert_eq!(fs::metadata(dir).unwrap().mode() & 0o2777, 0o2750);
    }
    for dir in [&store.binary, &store.index] {
        assert_eq!(
            fs::metadata(dir.join(files::key(&blob.digest).unwrap()))
                .unwrap()
                .mode()
                & 0o777,
            0o640
        );
    }
    GitContentStore::open(
        &store.root,
        ContentOptions {
            group_read: true,
            ..ContentOptions::default()
        },
    )
    .unwrap();
}
#[test]
fn git_timeout_kills_and_reaps_the_child() {
    let f = Fixture::new();
    let store = f.store();
    let script = f.0.join("slow-git");
    // Disposable test executable, not a runtime shell adapter.
    fs::write(&script, "#!/bin/sh\necho $$ > child.pid\nexec sleep 30\n").unwrap();
    fs::set_permissions(&script, Permissions::from_mode(0o700)).unwrap();
    let store = GitContentStore {
        options: ContentOptions {
            git_binary: script,
            git_timeout: Duration::from_millis(100),
            ..ContentOptions::default()
        },
        ..store
    };
    assert_eq!(
        store.put_bytes(b"timeout", "text/plain", None).err(),
        Some(Error("content_store_timeout"))
    );
    let pid = fs::read_to_string(store.root.join("child.pid")).unwrap();
    assert!(!Path::new(&format!("/proc/{}", pid.trim())).exists());
    assert_eq!(fs::read_dir(&store.staging).unwrap().count(), 0);
    assert_eq!(fs::read_dir(&store.index).unwrap().count(), 0);
}
