use msg_core::{Error, Json, Result};
use msg_identity::{
    models::*,
    sessions::{require_binding, BrowserPolicy, CredentialState},
    store::{AuthorityStore, Record, RecoveryDelivery},
};
use std::collections::BTreeMap;

struct Memory {
    states: BTreeMap<String, CredentialState>,
}
fn now() -> Timestamp {
    Timestamp::parse("2026-10-05T00:00:00Z").unwrap()
}
fn credential(id: &str, parent: Option<&str>) -> Credential {
    Credential {
        id: id.into(),
        subject_id: "u_fixture".into(),
        kind: CredentialKind::Token,
        verifier: Verifier(vec![0; 32]),
        ceiling: vec![],
        not_before: now(),
        expires_at: Some(Timestamp::parse("2030-01-01T00:00:00Z").unwrap()),
        revoked_at: None,
        source_credential_id: parent.map(String::from),
    }
}
fn source(session: Option<&str>) -> CredentialState {
    let body = serde_json::json!({"parent":"t_parent","subject":"u_fixture","auth_version":1,"ceiling":[],"session":session});
    CredentialState {
        expires_at: Timestamp::parse("2030-01-01T00:00:00Z").unwrap(),
        body: Json::parse(&body.to_string()).unwrap(),
    }
}
impl AuthorityStore for Memory {
    fn record(&self, kind: Record, _id: &str) -> Result<Json> {
        match kind {
            Record::Subject => wire(&Subject {
                resource_id: "u_fixture".into(),
                kind: SubjectKind::Registered,
                primary_group: "g_fixture".into(),
                auth_version: 1,
                local_only: false,
            }),
            Record::Credential => wire(&credential("t_parent", None)),
            _ => Err(Error("not_found")),
        }
    }
    fn setting(&self, _key: &str) -> Result<Option<Json>> {
        Ok(None)
    }
    fn has_setting(&self, _key: &str) -> Result<bool> {
        Ok(false)
    }
    fn certificate_revoked(&self, _id: &str) -> Result<bool> {
        Ok(false)
    }
    fn request_result(
        &self,
        _subject: Option<&str>,
        _id: &str,
        _digest: &str,
    ) -> Result<Option<Json>> {
        Ok(None)
    }
    fn credential_state(&self, id: &str) -> Result<Option<CredentialState>> {
        Ok(self.states.get(id).cloned())
    }
    fn custodial_binding(&self, _subject: &str) -> Result<Option<(String, String)>> {
        Ok(None)
    }
    fn recovery_delivery(
        &self,
        _credential: &str,
        _subject: &str,
    ) -> Result<Option<RecoveryDelivery>> {
        Ok(None)
    }
}
#[test]
fn browser_disabled_does_not_accept_a_valid_stored_binding() {
    let memory = Memory {
        states: BTreeMap::from([("browser:t_session_fixture".into(), source(None))]),
    };
    let c = credential("t_session_fixture", Some("t_parent"));
    for policy in [None, Some(BrowserPolicy { enabled: false })] {
        assert!(matches!(
            require_binding(&memory, &c, now(), policy.as_ref(), &[]),
            Err(Error("browser_sessions_disabled"))
        ));
    }
    require_binding(
        &memory,
        &c,
        now(),
        Some(&BrowserPolicy { enabled: true }),
        &[],
    )
    .unwrap();
}
#[test]
fn restored_session_cycle_is_bounded_and_denied() {
    let memory = Memory {
        states: BTreeMap::from([
            ("api:t_api_fixture".into(), source(Some("session:cycle"))),
            ("session:cycle".into(), source(Some("session:cycle"))),
        ]),
    };
    assert!(matches!(
        require_binding(
            &memory,
            &credential("t_api_fixture", Some("t_parent")),
            now(),
            None,
            &[]
        ),
        Err(Error("invalid_grant"))
    ));
}
#[test]
fn restored_excessive_source_depth_is_bounded_and_denied() {
    let mut memory = Memory {
        states: BTreeMap::from([("api:t_api_fixture".into(), source(Some("session:0")))]),
    };
    for i in 0..33 {
        let next = (i < 32).then(|| format!("session:{}", i + 1));
        memory
            .states
            .insert(format!("session:{i}"), source(next.as_deref()));
    }
    assert!(matches!(
        require_binding(
            &memory,
            &credential("t_api_fixture", Some("t_parent")),
            now(),
            None,
            &[]
        ),
        Err(Error("invalid_grant"))
    ));
}
