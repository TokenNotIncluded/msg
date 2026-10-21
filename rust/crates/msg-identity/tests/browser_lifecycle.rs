use msg_core::{b64, Error, Json, Result};
use msg_crypto::{key_id, subject_id, Ed25519Signer, Signature};
use msg_identity::{
    authentication::{Admission, AuthenticationService, Entry, OperationPolicy, Principal},
    browser::{state_id, BrowserStore, Config, Service},
    certificates::CertificateValidator,
    models::*,
    policy::Registry,
    sessions::CredentialState,
    store::{AuthorityStore, Record, RecoveryDelivery},
};
use msg_protocol::Request;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Default)]
struct Memory {
    records: BTreeMap<(u8, String), Json>,
    states: BTreeMap<String, CredentialState>,
    blocked: bool,
}
fn kind(record: Record) -> u8 {
    match record {
        Record::Subject => 0,
        Record::Credential => 1,
        Record::Certificate => 2,
        Record::Resource => 3,
    }
}
fn now() -> Timestamp {
    Timestamp::parse("2026-10-22T00:00:00Z").unwrap()
}
fn later(seconds: i64) -> Timestamp {
    Timestamp(now().0 + chrono::Duration::seconds(seconds))
}
impl AuthorityStore for Memory {
    fn record(&self, record: Record, id: &str) -> Result<Json> {
        self.records
            .get(&(kind(record), id.into()))
            .cloned()
            .ok_or(Error("not_found"))
    }
    fn setting(&self, _: &str) -> Result<Option<Json>> {
        Ok(None)
    }
    fn has_setting(&self, _: &str) -> Result<bool> {
        Ok(false)
    }
    fn certificate_revoked(&self, _: &str) -> Result<bool> {
        Ok(false)
    }
    fn request_result(&self, _: Option<&str>, _: &str, _: &str) -> Result<Option<Json>> {
        Ok(None)
    }
    fn credential_state(&self, id: &str) -> Result<Option<CredentialState>> {
        Ok(self.states.get(id).cloned())
    }
    fn custodial_binding(&self, _: &str) -> Result<Option<(String, String)>> {
        Ok(None)
    }
    fn recovery_delivery(&self, _: &str, _: &str) -> Result<Option<RecoveryDelivery>> {
        Ok(None)
    }
}
impl BrowserStore for Memory {
    fn require_live(&mut self) -> Result<()> {
        if self.blocked {
            Err(Error("recovery_quarantined"))
        } else {
            Ok(())
        }
    }
    fn save_credential(&mut self, credential: &Credential, version: u64) -> Result<()> {
        if self.subject(&credential.subject_id)?.auth_version != version {
            return Err(Error("auth_version_conflict"));
        }
        self.records
            .insert((1, credential.id.clone()), wire(credential)?);
        Ok(())
    }
    fn put_state(&mut self, id: &str, _: &str, expires: Timestamp, body: &Json) -> Result<()> {
        self.states.insert(
            id.into(),
            CredentialState {
                expires_at: expires,
                body: body.clone(),
            },
        );
        Ok(())
    }
    fn replace_state(&mut self, id: &str, body: &Json) -> Result<()> {
        self.states.get_mut(id).ok_or(Error("invalid_grant"))?.body = body.clone();
        Ok(())
    }
    fn expire_states(&mut self, now: Timestamp) -> Result<()> {
        self.states.retain(|_, state| state.expires_at > now);
        Ok(())
    }
    fn state_count(&self) -> Result<u64> {
        Ok(self.states.len() as u64)
    }
}
fn root(signer: &Ed25519Signer) -> Certificate {
    let mut certificate = Certificate {
        resource_id: "cert_root".into(),
        serial: "serial_root".into(),
        subject_id: "u_root".into(),
        key_id: key_id(&signer.public_key()),
        issuer_id: "u_root".into(),
        parent_certificate_id: None,
        authority_sources: vec![],
        kind: CertificateKind::Ca,
        grants: vec![],
        not_before: now(),
        expires_at: later(86400),
        target_service: "https://fixture.test".into(),
        delegation_depth: 0,
        issuance: Some(IssuancePolicy {
            issue_grants: vec![],
            max_cert_ttl_seconds: 86400,
            max_child_ca_depth: 1,
            max_delegation_depth: 0,
        }),
        signature: Signature {
            key_id: String::new(),
            algorithm: "ed25519".into(),
            value: String::new(),
        },
    };
    certificate.signature = signer
        .sign(&certificate.signing_bytes().unwrap(), "certificate")
        .unwrap();
    certificate
}
fn operation(name: &str, effect: &str, signature: bool) -> OperationPolicy {
    OperationPolicy {
        name: name.into(),
        version: 1,
        effect: effect.into(),
        entries: BTreeSet::from(["network".into()]),
        require_signature: signature,
        anonymous_only: false,
    }
}
fn operations() -> Vec<OperationPolicy> {
    vec![
        operation("identity.session_approve", "write", true),
        operation("content.post_get", "read", false),
        operation("content.post_create", "write", false),
        operation("content.delete", "write", false),
    ]
}
fn fixture() -> (Memory, Ed25519Signer, Vec<OperationPolicy>) {
    let signer = Ed25519Signer::from_seed(&[42; 32]);
    let id = subject_id(&signer.public_key());
    let operations = operations();
    let subject = Subject {
        resource_id: id.clone(),
        kind: SubjectKind::Registered,
        primary_group: "g_public".into(),
        auth_version: 1,
        local_only: false,
    };
    let credential = Credential {
        id: key_id(&signer.public_key()),
        subject_id: id.clone(),
        kind: CredentialKind::SigningKey,
        verifier: Verifier(signer.public_key().to_vec()),
        ceiling: vec![Grant {
            capability: "resource.basic".into(),
            version: 1,
            scope: Scope {
                resource_id: "r_root".into(),
                descendants: true,
            },
            operations: operations
                .iter()
                .map(|op| format!("{}@1", op.name))
                .collect(),
            constraints: BTreeMap::new(),
        }],
        not_before: now(),
        expires_at: Some(later(1800)),
        revoked_at: None,
        source_credential_id: None,
    };
    let mut memory = Memory::default();
    memory.records.insert((0, id), wire(&subject).unwrap());
    memory
        .records
        .insert((1, credential.id.clone()), wire(&credential).unwrap());
    for (id, parent) in [("r_root", None), ("t_wiki", Some("r_root"))] {
        memory.records.insert((3,id.into()),wire(&serde_json::json!({"id":id,"type":"topic","parent":parent,"owner":"u_owner","group":"g_public","mode":"0755","state":"active","revision":null})).unwrap());
    }
    (memory, signer, operations)
}
fn principal(memory: &Memory, signer: &Ed25519Signer, operation: &OperationPolicy) -> Principal {
    let mut raw = serde_json::json!({"request_id":"browser-approval","protocol_version":1,"operation":operation.name,"contract_version":1,"target_service":"https://fixture.test","subject":subject_id(&signer.public_key()),"arguments":{},"expected_generations":[],"expires_at":later(60),"payload_digest":format!("sha256:{}","0".repeat(64)),"proof":null,"return_fields":[],"source":"msg"});
    raw["payload_digest"] = Request::parse(&raw.to_string())
        .unwrap()
        .payload_digest()
        .unwrap()
        .into();
    let signature = signer
        .sign(
            &Request::parse(&raw.to_string())
                .unwrap()
                .signing_bytes()
                .unwrap(),
            "request",
        )
        .unwrap();
    raw["proof"] = serde_json::json!({"signature":signature,"certificates":[]});
    let root_signer = Ed25519Signer::from_seed(&[1; 32]);
    let validator = CertificateValidator::new(
        Registry::new(vec![]).unwrap(),
        root(&root_signer),
        root_signer.public_key(),
        "https://fixture.test".into(),
    )
    .unwrap();
    let mut authenticator = AuthenticationService::new(validator, memory, vec![], vec![]).unwrap();
    match authenticator
        .authenticate(
            &Request::parse(&raw.to_string()).unwrap(),
            operation,
            memory,
            Entry::Network,
            now(),
        )
        .unwrap()
    {
        Admission::Fresh(principal) => principal,
        Admission::Replay(_) => panic!("unexpected replay"),
    }
}
fn text<'a>(json: &'a Json, name: &str) -> &'a str {
    json.as_object().unwrap()[name].as_str().unwrap()
}

#[test]
fn signed_approval_is_one_use_limited_and_immediately_revocable() {
    let (mut memory, signer, operations) = fixture();
    let principal = principal(&memory, &signer, &operations[0]);
    let service = Service::new(
        Config {
            enabled: true,
            session_ttl: 3600,
        },
        "https://fixture.test",
        &[9; 32],
        &operations,
        &[],
    )
    .unwrap();
    let pending = service.pending(&mut memory, now()).unwrap();
    let value = text(&pending, "value");
    let code = text(&pending, "user_code");
    assert_eq!(code.len(), 8);
    for state in memory.states.values() {
        let stored = state.body.canonical().unwrap();
        assert!(!stored.contains(value));
        assert!(!stored.contains(code));
    }
    assert_eq!(
        text(
            &service.inspect(&mut memory, code, now()).unwrap(),
            "status"
        ),
        "pending"
    );
    service
        .approve(&mut memory, &principal, code, "approve", now())
        .unwrap();
    let logged = service.poll(&mut memory, value, now()).unwrap();
    let cookie = text(&logged, "cookie");
    assert_eq!(
        logged.as_object().unwrap()["expires"],
        wire(&later(1800)).unwrap()
    );
    assert!(service.poll(&mut memory, value, later(5)).is_err());
    let bearer = service.credentials(&mut memory, cookie, now()).unwrap();
    let credential = memory.credential(&bearer.credential_id).unwrap();
    let grants: Vec<_> = credential
        .ceiling
        .iter()
        .flat_map(|g| {
            g.operations
                .iter()
                .map(move |op| (op.as_str(), g.scope.resource_id.as_str()))
        })
        .collect();
    assert_eq!(
        grants,
        vec![
            ("content.post_get@1", "r_root"),
            ("content.post_create@1", "t_wiki")
        ]
    );
    assert!(!b64(bearer.secret.as_ref()).is_empty());
    let source = memory_source(&memory, &bearer.credential_id);
    assert!(service
        .create_session(&mut memory, &source, Some(cookie), None, now())
        .is_err());
    service.revoke(&mut memory, cookie, now()).unwrap();
    assert!(service.credentials(&mut memory, cookie, now()).is_err());
}

fn memory_source(memory: &Memory, id: &str) -> Json {
    memory
        .credential_state(&format!("browser:{id}"))
        .unwrap()
        .unwrap()
        .body
}

#[test]
fn polling_backoff_denial_and_expiry_commit_only_bounded_state() {
    let (mut memory, signer, operations) = fixture();
    let principal = principal(&memory, &signer, &operations[0]);
    let service = Service::new(
        Config {
            enabled: true,
            session_ttl: 3600,
        },
        "https://fixture.test",
        &[9; 32],
        &operations,
        &[],
    )
    .unwrap();
    let pending = service.pending(&mut memory, now()).unwrap();
    let value = text(&pending, "value");
    let code = text(&pending, "user_code");
    assert_eq!(
        text(&service.poll(&mut memory, value, now()).unwrap(), "error"),
        "authorization_pending"
    );
    assert_eq!(
        text(&service.poll(&mut memory, value, now()).unwrap(), "error"),
        "slow_down"
    );
    service
        .approve(&mut memory, &principal, code, "deny", now())
        .unwrap();
    assert_eq!(
        text(
            &service.poll(&mut memory, value, later(10)).unwrap(),
            "error"
        ),
        "access_denied"
    );
    assert!(service.poll(&mut memory, value, later(600)).is_err());
    assert!(service
        .approve(&mut memory, &principal, code, "approve", later(10))
        .is_err());
    for _ in 0..120 {
        service.rate(&mut memory, "127.0.0.1", now()).unwrap();
    }
    assert!(matches!(
        service.rate(&mut memory, "127.0.0.1", now()),
        Err(Error("slow_down"))
    ));
    service.rate(&mut memory, "127.0.0.1", later(600)).unwrap();
    assert_eq!(memory.states.len(), 1);
    memory.blocked = true;
    assert!(matches!(
        service.pending(&mut memory, now()),
        Err(Error("recovery_quarantined"))
    ));
}

#[test]
fn parent_revocation_and_auth_version_changes_invalidate_live_sessions() {
    for change_version in [false, true] {
        let (mut memory, signer, operations) = fixture();
        let principal = principal(&memory, &signer, &operations[0]);
        let service = Service::new(
            Config {
                enabled: true,
                session_ttl: 3600,
            },
            "https://fixture.test",
            &[9; 32],
            &operations,
            &[],
        )
        .unwrap();
        let pending = service.pending(&mut memory, now()).unwrap();
        service
            .approve(
                &mut memory,
                &principal,
                text(&pending, "user_code"),
                "approve",
                now(),
            )
            .unwrap();
        let logged = service
            .poll(&mut memory, text(&pending, "value"), now())
            .unwrap();
        let cookie = text(&logged, "cookie");
        service.credentials(&mut memory, cookie, now()).unwrap();
        if change_version {
            let mut subject = memory.subject(&subject_id(&signer.public_key())).unwrap();
            subject.auth_version += 1;
            memory
                .records
                .insert((0, subject.resource_id.clone()), wire(&subject).unwrap());
        } else {
            let mut parent = memory.credential(&key_id(&signer.public_key())).unwrap();
            parent.revoked_at = Some(now());
            memory.save_credential(&parent, 1).unwrap();
        }
        assert!(service.credentials(&mut memory, cookie, now()).is_err());
        assert!(memory.states.contains_key(&state_id("session", cookie)));
    }
}
