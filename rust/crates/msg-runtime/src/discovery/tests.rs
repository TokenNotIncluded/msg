use super::*;

#[test]
fn local_resource_addresses_do_not_fetch_or_normalize_remote_targets() {
    let service = "https://msg.example";
    assert_eq!(
        parse_address("https://msg.example/_r/r_a/rev/v_1", service).unwrap(),
        ("r_a".into(), Some("v_1".into()))
    );
    assert_eq!(
        parse_address("/*0123456789abcdef0123456789abcdef/raw", service).unwrap(),
        ("0123456789abcdef0123456789abcdef".into(), None)
    );
    assert_eq!(
        parse_address("/@a/notes/n", service).unwrap(),
        ("/@a/notes/n".into(), None)
    );
    for path in [
        "https://evil.example/_r/r_a",
        "https://msg.example@evil.example/_r/r_a",
        "https://msg.example/a/../_r/r_a",
        "https://msg.example/_r/r_a?x=1",
        "/_r/%72_a",
        "/_r/r_a/rev/v_1/extra",
        "/_r/r_a\\b",
    ] {
        assert!(parse_address(path, service).is_err(), "{path}");
    }
}

#[test]
fn projection_compaction_never_removes_empty_content_or_changes_identifiers() {
    let mut data = json!({"id":"r_a","revision":null,"content":"","items":[],"relations":[{"target":{"id":"r_b","revision":null},"excerpt":null}],"size":0});
    compact(&mut data);
    assert_eq!(
        data,
        json!({"id":"r_a","content":"","items":[],"relations":[{"target":{"id":"r_b"}}],"size":0})
    );
    assert_eq!(short_path("/@user/certificates/c"), "/@user/cert/c");
    assert_eq!(short_path("/main/certificates/c"), "/main/certificates/c");
}

#[test]
fn markdown_segments_respect_utf8_blocks_and_fence_state() {
    let bytes = "# 标题\n\n````rust\nlet x = \"自由🦀\";\n````\n\nNext paragraph.".as_bytes();
    let mut state = SegmentState {
        offset: 0,
        fence: false,
        line_start: true,
        continued: false,
    };
    let mut reconstructed = String::new();
    let mut iterations = 0;
    while state.offset < bytes.len() as u64 {
        iterations += 1;
        assert!(iterations < 20);
        let start = state.offset as usize;
        let end = (start + 32 + 4).min(bytes.len());
        let (next, text) =
            segment_window(&bytes[start..end], state, 32, bytes.len() as u64).unwrap();
        assert!(text.len() <= 32);
        assert!(next.offset > state.offset);
        reconstructed.push_str(&text);
        state = next;
    }
    assert_eq!(reconstructed.as_bytes(), bytes);
    assert!(!state.fence);
    let bad = [b'a', 0xff, b'b', b'c', b'd', b'e'];
    assert!(segment_window(
        &bad,
        SegmentState {
            offset: 0,
            fence: false,
            line_start: true,
            continued: false
        },
        32,
        bad.len() as u64
    )
    .is_err());
}

#[test]
fn active_attachment_types_stay_download_only() {
    assert_eq!(attachment_kind("image/svg+xml"), "file");
    assert_eq!(attachment_kind("text/html"), "file");
    assert_eq!(attachment_kind("IMAGE/PNG; charset=binary"), "image");
    assert_eq!(attachment_kind("text/markdown; charset=utf-8"), "text");
    assert_eq!(
        plain_text("**title** [label](https://example.test) <script>x</script>\n自由").unwrap(),
        "title label x 自由"
    );
    assert_eq!(shorten("自由空间", 3), "自由…");
}
