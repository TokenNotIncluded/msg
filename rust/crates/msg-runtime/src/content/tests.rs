use super::*;

#[test]
fn python_manifest_bytes_and_signature_domain_are_preserved() {
    let revision: Revision = decode(&Json::parse(r#"{"format_version":1,"id":"v_test","resource_id":"r_test","parents":[],"content":{"digest":"sha256:177914cf6d85fddd444984ca9d50e391dc520f648c2319cc42dc99be176b9795","size":7,"media_type":"text/markdown"},"relations":[{"type":"reply_to","target":{"id":"r_parent","revision":"v_parent"},"excerpt":null}],"actor":"u_alice","subject":"u_alice","author":"u_alice","created_at":"2026-10-21T12:00:00.000000Z","manifest_digest":"","signature":null}"#).unwrap()).unwrap();
    let bytes = manifest(&revision).unwrap().canonical().unwrap();
    // hashlib.sha256(json.dumps(..., ensure_ascii=False, sort_keys=True,
    // separators=(',', ':')).encode()) from the Python wire representation.
    assert_eq!(
        digest(bytes.as_bytes()),
        "sha256:7da9a410b341dd83b2e3c2d63bc60e5455e5e0afb19a9e56c06ac77aa5abe240"
    );
    assert!(!bytes.contains("signature"));
    assert!(!bytes.contains("summary"));
    let signer = msg_crypto::Ed25519Signer::from_seed(&[71; 32]);
    let signature = signer.sign(bytes.as_bytes(), "revision").unwrap();
    verify(
        &signer.public_key(),
        bytes.as_bytes(),
        &signature,
        "revision",
    )
    .unwrap();
    assert!(verify(
        &signer.public_key(),
        bytes.as_bytes(),
        &signature,
        "request"
    )
    .is_err());
}

#[test]
fn aliases_cannot_substitute_for_canonical_generation_precondition() {
    let values = Json::parse(r#"[["r_canonical",7]]"#).unwrap();
    assert_expected(&values, "r_canonical", 7).unwrap();
    assert_eq!(
        assert_expected(&values, "r_canonical", 8),
        Err(Error("generation_conflict"))
    );
    assert_eq!(
        assert_expected(&values, "/path", 7),
        Err(Error("expected_generation_required"))
    );
    assert!(valid_id("../escape", "invalid_revision_id").is_err());
    assert!(validate_name("revisions").is_err());
    assert!(validate_name("@claimed").is_err());
    assert!(validate_name(&"界".repeat(120)).is_ok());
    assert!(validate_name(&"界".repeat(121)).is_err());
}

#[test]
fn tag_normalization_matches_nfkc_casefold_and_unicode_limits() {
    let tags =
        Json::parse(r#"["  ＦＯＯ ","foo","Straße","STRASSE","é","É","\u001cX\u001f"]"#).unwrap();
    assert_eq!(
        normalize_tags(&tags).unwrap(),
        vec!["foo", "strasse", "x", "é"]
    );
    assert!(normalize_tags(&Json::parse(r#"["a/b"]"#).unwrap()).is_err());
    assert!(normalize_tags(&Json::parse(r#"["-a"]"#).unwrap()).is_err());
    assert!(summary(Some("界".repeat(280))).is_ok());
    assert!(summary(Some("界".repeat(281))).is_err());
    assert_eq!(summary(Some(String::new())).unwrap(), None);
}

#[test]
fn utf8_sources_validate_across_every_chunk_boundary() {
    let data = "a自由🦀\n".as_bytes();
    for boundary in 0..=data.len() {
        let mut sink = Utf8::default();
        sink.write_all(&data[..boundary]).unwrap();
        sink.write_all(&data[boundary..]).unwrap();
        assert!(sink.tail.is_empty() && !sink.invalid);
    }
    let mut sink = Utf8::default();
    sink.write_all(&[0xf0, 0x9f]).unwrap();
    assert!(!sink.tail.is_empty());
    assert!(sink.write_all(b"x").is_err());
    assert!(sink.invalid);
}

#[test]
fn templates_are_finite_typed_data_with_no_evaluation() {
    let template = parse_template("task@2\ntitle:str!\ndone:bool=false\npriority:enum(low,high)?=low\nref:ref?\ncount:int=3\n").unwrap();
    let supplied = Json::parse(r#"{"title":"自由","ref":{"id":"r_1","revision":null}}"#).unwrap();
    assert_eq!(
        template_values(&template, Some(&supplied))
            .unwrap()
            .canonical()
            .unwrap(),
        r#"{"count":3,"done":false,"priority":"low","ref":{"id":"r_1","revision":null},"title":"自由"}"#
    );
    assert!(template_values(&template, None).is_err());
    assert!(template_values(
        &template,
        Some(&Json::parse(r#"{"title":"a","count":true}"#).unwrap())
    )
    .is_err());
    assert!(template_values(
        &template,
        Some(&Json::parse(r#"{"title":"a","extra":0}"#).unwrap())
    )
    .is_err());
    assert!(parse_template("a@1\na:str!=x").is_err());
    assert!(parse_template("a@1\na:str\na:bool").is_err());
    assert!(parse_template("a@1\na:exec=system()").is_err());
    let equals = parse_template("a@1\na:enum(a=b,c)=a=b").unwrap();
    assert_eq!(
        template_values(&equals, None).unwrap().canonical().unwrap(),
        r#"{"a":"a=b"}"#
    );
}

#[test]
fn svg_allows_local_art_but_rejects_active_and_external_payloads() {
    safe_svg(br##"<svg xmlns="http://www.w3.org/2000/svg"><g><path d="M 1 2" fill="#abc"/><animate attributeName="opacity" values="0;1"/></g></svg>"##).unwrap();
    for bad in [
        r#"<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>"#,
        r#"<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>"#,
        r#"<svg xmlns="http://www.w3.org/2000/svg"><animate attributeName="href" values="x"/></svg>"#,
        r#"<svg xmlns="http://www.w3.org/2000/svg"><a href="local"/></svg>"#,
        r#"<svg xmlns="http://www.w3.org/2000/svg"><g xmlns="evil"/></svg>"#,
        r#"<!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg"/>"#,
        r#"<svg xmlns="http://www.w3.org/2000/svg"><style>fill:URL (a)</style></svg>"#,
        r#"<svg xmlns="http://www.w3.org/2000/svg"><g>https://example.test</g></svg>"#,
    ] {
        assert!(safe_svg(bad.as_bytes()).is_err(), "{bad}");
    }
}
