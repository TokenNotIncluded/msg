//! Native key identities and response-only token delivery.
//!
//! Authentication, operation-schema validation and resource authorization run
//! before these handlers, in the SAME transaction. These methods never commit.
//! A returned token claim may reach the transport only after its transaction
//! commits; persisted operation results contain identifiers, never token bytes.
use crate::{
    authentication::{OperationPolicy, Principal},
    browser::random_identifier,
    certificates::CertificateValidator,
    models::*,
    mutations::{EncryptionKey, IdentityKey, IdentityStore, TokenDelivery},
    policy::grant_subset,
    require,
    store::{AuthorityStore, Record},
};
use chrono::Duration;
use hmac::{Hmac, Mac};
use msg_core::{b64, digest, unb64, Error, Json, Result};
use msg_crypto::{age_recipient, key_id, verify, Ed25519Signer, Signature};
use rand_core::{OsRng, RngCore};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use subtle::ConstantTimeEq;
use zeroize::Zeroizing;

pub struct Service<'a> {
    pub namespace_root: &'a str,
    pub validator: &'a CertificateValidator,
    pub online_signer: &'a Ed25519Signer,
    pub token_secret: &'a [u8; 32],
    pub primary_ceiling: &'a [Grant],
    pub temporary_ceiling: &'a [Grant],
    pub base_grants: &'a [Grant],
    pub operations: &'a [OperationPolicy],
    pub certificate_ttl: u64,
    pub temporary_ttl: u64,
    pub recovery_window: u64,
}

struct Issuance<'a> {
    subject: &'a str,
    key: &'a str,
    grants: Option<Vec<Grant>>,
    ttl: Option<u64>,
    authority_source: Option<&'a Certificate>,
}

pub(crate) fn field<'a>(value: &'a Json, name: &str) -> Result<&'a Json> {
    value.as_object()?.get(name).ok_or(Error("invalid_request"))
}
pub(crate) fn text<'a>(value: &'a Json, name: &str) -> Result<&'a str> {
    field(value, name)?
        .as_str()
        .map_err(|_| Error("invalid_request"))
}
pub(crate) fn optional<'a>(value: &'a Json, name: &str) -> Result<Option<&'a Json>> {
    Ok(value.as_object()?.get(name))
}
fn after(now: Timestamp, ttl: u64) -> Result<Timestamp> {
    let seconds = i64::try_from(ttl).map_err(|_| Error("invalid_ttl"))?;
    now.0
        .checked_add_signed(Duration::try_seconds(seconds).ok_or(Error("invalid_ttl"))?)
        .map(Timestamp)
        .ok_or(Error("invalid_ttl"))
}
pub(crate) fn new_id(prefix: &str) -> Result<String> {
    Ok(format!("{prefix}_{}", random_identifier()?))
}
pub(crate) fn output(data: serde_json::Value, resources: Vec<ResourceRef>) -> Result<Json> {
    wire(&serde_json::json!({"data":data,"resources":resources}))
}
fn owner(store: &dyn AuthorityStore, principal: &Principal) -> Result<Subject> {
    let id = principal
        .subject()
        .ok_or(Error("identity_owner_required"))?;
    require(principal.actor() == Some(id), "identity_owner_required")?;
    let subject = store.subject(id)?;
    require(!subject.local_only && id != "u_root", "local_only")?;
    Ok(subject)
}
pub(crate) fn signature(principal: &Principal) -> Result<()> {
    require(principal.method() == "signature", "signature_required")
}
fn handle(handle: &str) -> Result<()> {
    require(
        (2..=41).contains(&handle.len())
            && handle.as_bytes()[0].is_ascii_lowercase()
            && handle
                .bytes()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'-')
            && !matches!(handle, "root" | "online-ca"),
        "invalid_handle",
    )
}
fn bump(subject: &Subject) -> Result<Subject> {
    let mut updated = subject.clone();
    updated.auth_version = subject
        .auth_version
        .checked_add(1)
        .ok_or(Error("invalid_generation"))?;
    Ok(updated)
}
fn expected(subject: &Subject) -> Result<i64> {
    i64::try_from(subject.auth_version).map_err(|_| Error("invalid_generation"))
}
fn token_id(request: &Json, subject: &str) -> Result<String> {
    Ok(format!(
        "t_{}",
        &digest(
            wire(&(text(request, "request_id")?, subject))?
                .canonical()?
                .as_bytes()
        )[7..39]
    ))
}
pub(crate) fn resource(
    id: &str,
    kind: &str,
    name: &str,
    parent: &str,
    owner_actor: (&str, &str),
    mode: &str,
    now: Timestamp,
) -> Result<Json> {
    let (owner, actor) = owner_actor;
    wire(
        &serde_json::json!({"id":id,"type":kind,"type_version":1,"name":name,
        "parent":parent,"owner":owner,"group":"g_public","mode":mode,"generation":0,
        "revision":null,"state":"active","created_at":now,"created_by":actor,
        "modified_at":now,"modified_by":actor,"tags":[]}),
    )
}

impl Service<'_> {
    pub fn supports(operation: &str, version: u32) -> bool {
        match operation {
            "identity.register"
            | "identity.upgrade"
            | "identity.token_rotate"
            | "identity.custodial_create" => (1..=2).contains(&version),
            "identity.temporary" | "identity.token_create" => (1..=3).contains(&version),
            "identity.key_add"
            | "identity.key_revoke"
            | "identity.token_recover"
            | "identity.identity_key_list"
            | "identity.identity_key_get"
            | "identity.encryption_key_list"
            | "identity.encryption_key_get"
            | "identity.encryption_key_rotate"
            | "identity.certificate_renew"
            | "identity.custodial_status"
            | "identity.upgrade_result" => version == 1,
            _ => false,
        }
    }

    pub fn handle<S: IdentityStore>(
        &self,
        store: &mut S,
        principal: &Principal,
        request: &Json,
        now: Timestamp,
    ) -> Result<Json> {
        store.require_live()?;
        let operation = text(request, "operation")?;
        let version: u32 = decode(field(request, "contract_version")?)?;
        require(Self::supports(operation, version), "operation_not_found")?;
        let arguments = field(request, "arguments")?;
        match operation {
            "identity.register" => {
                require(version == 2, "encryption_subkey_required")?;
                self.register(store, principal, request, arguments, now)
            }
            "identity.temporary" => {
                require(version == 3, "temporary_dual_keys_required")?;
                self.temporary(store, principal, request, arguments, now)
            }
            "identity.custodial_create" => {
                require(version == 2, "credential_delivery_upgrade_required")?;
                self.custodial_create(store, principal, request, arguments, now)
            }
            "identity.custodial_status" => self.custodial_status(store, principal, now),
            "identity.key_add" => self.key_add(store, principal, request, arguments, now),
            "identity.key_revoke" => self.key_revoke(store, principal, arguments, now),
            "identity.encryption_key_rotate" => {
                self.encryption_rotate(store, principal, arguments, now)
            }
            "identity.identity_key_list" | "identity.identity_key_get" => {
                self.identity_keys(store, arguments, operation.ends_with("_get"))
            }
            "identity.encryption_key_list" | "identity.encryption_key_get" => {
                self.encryption_keys(store, arguments, operation.ends_with("_get"))
            }
            "identity.token_create" => {
                self.token_create(store, principal, request, arguments, version, now)
            }
            "identity.token_rotate" => {
                require(version >= 2, "credential_delivery_upgrade_required")?;
                self.token_rotate(store, principal, request, arguments, now)
            }
            "identity.token_recover" => {
                self.token_recover(store, principal, request, arguments, now)
            }
            "identity.certificate_renew" => self.renew(store, principal, request, arguments, now),
            "identity.upgrade" => {
                require(version == 2, "encryption_subkey_required")?;
                self.upgrade(store, principal, request, arguments, now)
            }
            "identity.upgrade_result" => self.upgrade_result(store, principal, arguments, now),
            _ => Err(Error("operation_not_found")),
        }
    }

    fn issuer<S: IdentityStore>(&self, store: &S, now: Timestamp) -> Result<Certificate> {
        let id = store
            .setting("online_ca_certificate")?
            .ok_or(Error("issuer_not_ready"))?;
        let certificate = self.validator.validate(id.as_str()?, store, now)?;
        require(
            certificate.kind == CertificateKind::Ca
                && certificate.key_id == key_id(&self.online_signer.public_key())
                && certificate.issuance.is_some(),
            "issuer_not_ready",
        )?;
        Ok(certificate)
    }

    fn issue<S: IdentityStore>(
        &self,
        store: &mut S,
        principal: &Principal,
        request: &Json,
        issuance: Issuance<'_>,
        now: Timestamp,
    ) -> Result<Certificate> {
        let Issuance {
            subject,
            key,
            grants,
            ttl,
            authority_source,
        } = issuance;
        let issuer = self.issuer(store, now)?;
        let policy = issuer.issuance.as_ref().ok_or(Error("issuer_not_ready"))?;
        let lifetime = ttl.unwrap_or(self.certificate_ttl);
        require(
            lifetime > 0 && lifetime <= policy.max_cert_ttl_seconds,
            "certificate_ttl_escalation",
        )?;
        let mut expiry = after(now, lifetime)?.min(issuer.expires_at);
        let grants = match grants {
            Some(grants) => grants,
            None => {
                let mut available = Vec::new();
                let mut seen = BTreeSet::new();
                for requested in self.base_grants {
                    for allowed in &policy.issue_grants {
                        if (requested.capability.as_str(), requested.version)
                            != (allowed.capability.as_str(), allowed.version)
                        {
                            continue;
                        }
                        let mut candidate = requested.clone();
                        candidate.operations = requested
                            .operations
                            .intersection(&allowed.operations)
                            .cloned()
                            .collect();
                        if !candidate.operations.is_empty()
                            && grant_subset(&candidate, allowed, store)?
                        {
                            self.validator.validate_grant(&candidate, store)?;
                            if seen.insert(wire(&candidate)?.canonical()?) {
                                available.push(candidate);
                            }
                        }
                    }
                }
                available
            }
        };
        if let Some(source) = authority_source {
            require(
                source.subject_id == subject && source.key_id == key,
                "authority_source_invalid",
            )?;
            expiry = expiry.min(source.expires_at);
        }
        let mut certificate = Certificate {
            resource_id: new_id("cert")?,
            serial: new_id("serial")?,
            subject_id: subject.into(),
            key_id: key.into(),
            issuer_id: "u_online_ca".into(),
            parent_certificate_id: Some(issuer.resource_id.clone()),
            authority_sources: vec![],
            kind: CertificateKind::Identity,
            grants,
            not_before: now,
            expires_at: expiry,
            target_service: self.validator.service().into(),
            delegation_depth: 0,
            issuance: None,
            signature: Signature {
                key_id: key_id(&self.online_signer.public_key()),
                algorithm: "ed25519".into(),
                value: String::new(),
            },
        };
        certificate.signature = self
            .online_signer
            .sign(&certificate.signing_bytes()?, "certificate")?;
        store.insert_identity_resource(&resource(
            &certificate.resource_id,
            "certificate",
            &certificate.resource_id,
            "t_certificates",
            (subject, "u_online_ca"),
            "0444",
            now,
        )?)?;
        self.validator
            .validate_candidate(&certificate, store, now)?;
        store.register_certificate(&certificate)?;
        let source = match authority_source {
            Some(source) => {
                serde_json::json!({"kind":"certificate","certificate_id":source.resource_id})
            }
            None => {
                serde_json::json!({"kind":"possession_proof","key_id":key,"request_id":text(request,"request_id")?})
            }
        };
        store.append_identity_audit(&wire(&serde_json::json!({
            "event":{"id":new_id("audit")?,"type":"cert.auto.issue","time":now,"request_id":text(request,"request_id")?,
                "actor":principal.actor(),"subject":subject,"resources":[{"id":certificate.resource_id}],
                "data":{"automatic":true,"policy_version":1,"issuer":"u_online_ca","issuer_certificate_id":issuer.resource_id,
                    "signing_key_id":key_id(&self.online_signer.public_key()),"certificate_id":certificate.resource_id,
                    "authority_source":source,"grant_digest":digest(wire(&certificate.grants)?.canonical()?.as_bytes())}},
            "authority":[{"id":authority_source.map(|source| source.resource_id.as_str()).unwrap_or(subject)}],
            "before_digest":null,"after_digest":digest(wire(&certificate)?.canonical()?.as_bytes()),
            "previous_digest":null,"entry_digest":"","result":"issued"
        }))?)?;
        Ok(certificate)
    }

    fn make_user<S: IdentityStore>(
        &self,
        store: &mut S,
        id: &str,
        name: &str,
        kind: SubjectKind,
        now: Timestamp,
    ) -> Result<Subject> {
        handle(name)?;
        store.claim_handle(self.namespace_root, name, id)?;
        store.insert_identity_resource(&resource(
            id,
            "user",
            &format!("@{name}"),
            self.namespace_root,
            (id, id),
            "0755",
            now,
        )?)?;
        let subject = Subject {
            resource_id: id.into(),
            kind,
            primary_group: "g_public".into(),
            auth_version: 0,
            local_only: false,
        };
        store.update_subject(&subject, -1)?;
        for (name, mode) in [
            ("keys", "0555"),
            ("certificates", "0555"),
            ("files", "0700"),
            ("keystore", "0700"),
        ] {
            let folder = format!(
                "r_{}",
                &digest(wire(&(id, name))?.canonical()?.as_bytes())[7..39]
            );
            store.insert_identity_resource(&resource(
                &folder,
                "topic",
                name,
                id,
                (id, id),
                mode,
                now,
            )?)?;
        }
        Ok(subject)
    }

    fn add_keys<S: IdentityStore>(
        &self,
        store: &mut S,
        subject: &Subject,
        public: &[u8],
        recipient: &str,
        ceiling: &[Grant],
        now: Timestamp,
    ) -> Result<(String, String)> {
        require(public.len() == 32, "invalid_public_key")?;
        let encryption = age_recipient::public_from_recipient(recipient)?;
        require(public != encryption, "encryption_key_must_be_independent")?;
        let signing = key_id(public);
        let encryption_id = age_recipient::encryption_key_id(&encryption);
        let credential = Credential {
            id: signing.clone(),
            subject_id: subject.resource_id.clone(),
            kind: CredentialKind::SigningKey,
            verifier: Verifier(public.to_vec()),
            ceiling: ceiling.to_vec(),
            not_before: now,
            expires_at: None,
            revoked_at: None,
            source_credential_id: None,
        };
        store.save_credential(&credential, subject.auth_version)?;
        store.save_identity_key(&IdentityKey {
            key_id: signing.clone(),
            subject: subject.resource_id.clone(),
            public_key: Verifier(public.to_vec()),
            created_at: now,
            retired_at: None,
            primary: true,
        })?;
        store.save_encryption_key(&EncryptionKey {
            key_id: encryption_id.clone(),
            subject: subject.resource_id.clone(),
            recipient: recipient.into(),
            public_key: Verifier(encryption.to_vec()),
            created_at: now,
            retired_at: None,
            primary: true,
        })?;
        Ok((signing, encryption_id))
    }

    fn register<S: IdentityStore>(
        &self,
        store: &mut S,
        principal: &Principal,
        request: &Json,
        args: &Json,
        now: Timestamp,
    ) -> Result<Json> {
        signature(principal)?;
        self.issuer(store, now)?;
        let public = unb64(text(args, "public_key")?, 32)?;
        let id = principal.subject().ok_or(Error("subject_mismatch"))?;
        require(
            id == msg_crypto::subject_id(&public) && principal.actor() == Some(id),
            "subject_mismatch",
        )?;
        let subject = self.make_user(
            store,
            id,
            text(args, "handle")?,
            SubjectKind::Registered,
            now,
        )?;
        let (key, encryption) = self.add_keys(
            store,
            &subject,
            &public,
            text(args, "encryption_recipient")?,
            self.primary_ceiling,
            now,
        )?;
        let certificate = self.issue(
            store,
            principal,
            request,
            Issuance {
                subject: id,
                key: &key,
                grants: None,
                ttl: None,
                authority_source: None,
            },
            now,
        )?;
        output(
            serde_json::json!({"subject_id":id,"key_id":key,"encryption_key_id":encryption,
            "encryption_recipient":text(args,"encryption_recipient")?,"certificate_id":certificate.resource_id,"handle":text(args,"handle")?}),
            vec![ResourceRef {
                id: id.into(),
                revision: None,
            }],
        )
    }

    fn temporary<S: IdentityStore>(
        &self,
        store: &mut S,
        principal: &Principal,
        request: &Json,
        args: &Json,
        now: Timestamp,
    ) -> Result<Json> {
        let id = principal.subject().ok_or(Error("invalid_bootstrap"))?;
        require(
            principal.actor() == Some(id)
                && principal.method() == "token"
                && id.starts_with("u_tmp_"),
            "invalid_bootstrap",
        )?;
        let public = unb64(text(args, "public_key")?, 32)?;
        verify(&public,wire(&serde_json::json!({"subject_id":id,"nonce":field(args,"nonce")?,"public_key":field(args,"public_key")?,
            "encryption_recipient":field(args,"encryption_recipient")?,"request_id":text(request,"request_id")?}))?.canonical()?.as_bytes(),
            &decode(field(args,"possession_proof")?)?,"temporary-key-possession-v1")?;
        let subject = self.make_user(
            store,
            id,
            &format!("tmp-{}", &id[id.len() - 24..]),
            SubjectKind::Temporary,
            now,
        )?;
        let (signing, encryption) = self.add_keys(
            store,
            &subject,
            &public,
            text(args, "encryption_recipient")?,
            self.temporary_ceiling,
            now,
        )?;
        let credential = Credential {
            id: principal
                .credential_id()
                .ok_or(Error("invalid_bootstrap"))?
                .into(),
            subject_id: id.into(),
            kind: CredentialKind::Token,
            verifier: Verifier(Sha256::digest(self.issued_token(request, id)?.as_ref()).to_vec()),
            ceiling: self.temporary_ceiling.to_vec(),
            not_before: now,
            expires_at: Some(after(now, self.temporary_ttl)?),
            revoked_at: None,
            source_credential_id: None,
        };
        store.save_credential(&credential, 0)?;
        self.record_delivery(store, request, &credential, None, now)?;
        output(
            serde_json::json!({"subject_id":id,"credential_id":credential.id,"expires_at":credential.expires_at,
            "identity_key_id":signing,"encryption_key_id":encryption,"encryption_recipient":field(args,"encryption_recipient")?,
            "signature_source":"self_custody","server_signable":false,"server_decryptable":false}),
            vec![ResourceRef {
                id: id.into(),
                revision: None,
            }],
        )
    }

    fn custodial_create<S: IdentityStore>(
        &self,
        store: &mut S,
        principal: &Principal,
        request: &Json,
        args: &Json,
        now: Timestamp,
    ) -> Result<Json> {
        let id = principal.subject().ok_or(Error("invalid_bootstrap"))?;
        require(
            principal.actor() == Some(id)
                && principal.method() == "token"
                && id.starts_with("u_cust_"),
            "invalid_bootstrap",
        )?;
        let mut signing_seed = Zeroizing::new([0u8; 32]);
        OsRng
            .try_fill_bytes(signing_seed.as_mut())
            .map_err(|_| Error("randomness_unavailable"))?;
        let signer = Ed25519Signer::from_seed(&signing_seed);
        let (age_private, public) = msg_crypto::sealed_box::generate_key()?;
        let recipient = age_recipient::recipient_from_public(&public);
        let subject = self.make_user(
            store,
            id,
            text(args, "handle")?,
            SubjectKind::Custodial,
            now,
        )?;
        // Vault signing keys deliberately have no network authority of their own.
        let (signing, encryption) =
            self.add_keys(store, &subject, &signer.public_key(), &recipient, &[], now)?;
        let credential = Credential {
            id: principal
                .credential_id()
                .ok_or(Error("invalid_bootstrap"))?
                .into(),
            subject_id: id.into(),
            kind: CredentialKind::Token,
            verifier: Verifier(Sha256::digest(self.issued_token(request, id)?.as_ref()).to_vec()),
            ceiling: self.temporary_ceiling.to_vec(),
            not_before: now,
            expires_at: Some(after(now, self.temporary_ttl)?),
            revoked_at: None,
            source_credential_id: None,
        };
        store.save_credential(&credential, 0)?;
        self.record_delivery(store, request, &credential, None, now)?;
        store.insert_custodial_vault(&crate::vault::seal_keys(
            self.token_secret,
            id,
            &signing_seed,
            &age_private,
            now,
        )?)?;
        output(
            serde_json::json!({"subject_id":id,"credential_id":credential.id,"identity_key_id":signing,"encryption_key_id":encryption,
            "encryption_recipient":recipient,"expires_at":credential.expires_at,"signature_source":"custodial","server_signable":true,"server_decryptable":true}),
            vec![ResourceRef {
                id: id.into(),
                revision: None,
            }],
        )
    }

    fn custodial_status<S: IdentityStore>(
        &self,
        store: &S,
        principal: &Principal,
        now: Timestamp,
    ) -> Result<Json> {
        let subject = owner(store, principal)?;
        require(subject.kind == SubjectKind::Custodial, "not_custodial")?;
        let row = store
            .vault_record(&subject.resource_id)?
            .ok_or(Error("custodial_vault_unavailable"))?;
        crate::vault::open_signer(store, self.token_secret, &subject.resource_id, now)?;
        crate::vault::open_age_identity(store, self.token_secret, &subject.resource_id)?;
        output(
            serde_json::json!({"subject_id":subject.resource_id,"identity_key_id":row.signing_key_id,"encryption_key_id":row.encryption_key_id,
            "signature_source":"custodial","server_signable":true,"server_decryptable":true}),
            vec![],
        )
    }

    fn validate_ceiling<S: IdentityStore>(
        &self,
        store: &S,
        principal: &Principal,
        ceiling: &[Grant],
    ) -> Result<()> {
        for grant in ceiling {
            self.validator.validate_grant(grant, store)?;
            let mut allowed = false;
            for parent in principal.ceiling() {
                allowed |= grant.operations.is_subset(&parent.operations)
                    && grant_subset(grant, parent, store)?;
            }
            require(allowed, "credential_ceiling_escalation")?;
        }
        Ok(())
    }

    fn key_add<S: IdentityStore>(
        &self,
        store: &mut S,
        principal: &Principal,
        request: &Json,
        args: &Json,
        now: Timestamp,
    ) -> Result<Json> {
        signature(principal)?;
        let subject = owner(store, principal)?;
        let public = unb64(text(args, "public_key")?, 32)?;
        verify(
            &public,
            wire(&serde_json::json!({"subject_id":subject.resource_id,"public_key":b64(&public)}))?
                .canonical()?
                .as_bytes(),
            &decode(field(args, "possession_proof")?)?,
            "key-add",
        )?;
        let ceiling: Vec<Grant> = decode(field(args, "ceiling")?)?;
        self.validate_ceiling(store, principal, &ceiling)?;
        let key = key_id(&public);
        match store.credential(&key) {
            Err(Error("credential_not_found")) => (),
            Ok(_) => return Err(Error("key_exists")),
            Err(error) => return Err(error),
        }
        let credential = Credential {
            id: key.clone(),
            subject_id: subject.resource_id.clone(),
            kind: CredentialKind::SigningKey,
            verifier: Verifier(public),
            ceiling: ceiling.clone(),
            not_before: now,
            expires_at: None,
            revoked_at: None,
            source_credential_id: None,
        };
        store.save_credential(&credential, subject.auth_version)?;
        store.save_identity_key(&IdentityKey {
            key_id: key.clone(),
            subject: subject.resource_id.clone(),
            public_key: credential.verifier.clone(),
            created_at: now,
            retired_at: None,
            primary: false,
        })?;
        store.update_subject(&bump(&subject)?, expected(&subject)?)?;
        let issuer = self.issuer(store, now)?;
        let policy = issuer.issuance.as_ref().ok_or(Error("issuer_not_ready"))?;
        // The CA's signed policy is the concrete online-issuable capability set.
        let grants = ceiling
            .into_iter()
            .filter(|grant| {
                policy
                    .issue_grants
                    .iter()
                    .any(|allowed| allowed.capability == grant.capability)
            })
            .collect();
        let certificate = self.issue(
            store,
            principal,
            request,
            Issuance {
                subject: &subject.resource_id,
                key: &key,
                grants: Some(grants),
                ttl: None,
                authority_source: None,
            },
            now,
        )?;
        output(
            serde_json::json!({"key_id":key,"certificate_id":certificate.resource_id}),
            vec![],
        )
    }

    fn key_revoke<S: IdentityStore>(
        &self,
        store: &mut S,
        principal: &Principal,
        args: &Json,
        now: Timestamp,
    ) -> Result<Json> {
        signature(principal)?;
        let subject = owner(store, principal)?;
        let mut credential = store.credential(text(args, "key_id")?)?;
        require(
            credential.subject_id == subject.resource_id,
            "credential_owner_required",
        )?;
        require(
            credential.kind != CredentialKind::SigningKey
                || store
                    .identity_credentials(&subject.resource_id)?
                    .iter()
                    .any(|key| {
                        key.kind == CredentialKind::SigningKey
                            && key.revoked_at.is_none()
                            && key.id != credential.id
                    }),
            "last_signing_key",
        )?;
        credential.revoked_at = Some(now);
        store.save_credential(&credential, subject.auth_version)?;
        let mut keys = store.identity_keys(&subject.resource_id)?;
        keys.sort_by(|a, b| (a.created_at, &a.key_id).cmp(&(b.created_at, &b.key_id)));
        let mut replace_primary = false;
        for key in &mut keys {
            if key.key_id == credential.id {
                replace_primary = key.primary;
                key.primary = false;
                key.retired_at = Some(now);
                store.save_identity_key(key)?;
            }
        }
        if replace_primary {
            if let Some(key) = keys.iter_mut().find(|key| key.retired_at.is_none()) {
                key.primary = true;
                store.save_identity_key(key)?;
            }
        }
        store.update_subject(&bump(&subject)?, expected(&subject)?)?;
        output(
            serde_json::json!({"key_id":credential.id,"revoked":true}),
            vec![],
        )
    }

    fn encryption_rotate<S: IdentityStore>(
        &self,
        store: &mut S,
        principal: &Principal,
        args: &Json,
        now: Timestamp,
    ) -> Result<Json> {
        signature(principal)?;
        let subject = owner(store, principal)?;
        require(
            !store.custodial_migration_pending(&subject.resource_id, now)?,
            "custodial_migration_pending",
        )?;
        let recipient = text(args, "encryption_recipient")?;
        let public = age_recipient::public_from_recipient(recipient)?;
        let id = age_recipient::encryption_key_id(&public);
        let mut current = store
            .encryption_keys(&subject.resource_id)?
            .into_iter()
            .find(|key| key.primary)
            .ok_or(Error("encryption_key_not_found"))?;
        require(current.key_id != id, "encryption_key_unchanged")?;
        require(!store.encryption_key_exists(&id)?, "encryption_key_exists")?;
        current.primary = false;
        current.retired_at = Some(now);
        store.save_encryption_key(&current)?;
        store.save_encryption_key(&EncryptionKey {
            key_id: id.clone(),
            subject: subject.resource_id.clone(),
            recipient: recipient.into(),
            public_key: Verifier(public.to_vec()),
            created_at: now,
            retired_at: None,
            primary: true,
        })?;
        output(
            serde_json::json!({"subject_id":subject.resource_id,"key_id":id,"recipient":recipient,"previous_key_id":current.key_id,"old_ciphertexts_require_rewrap":true}),
            vec![],
        )
    }

    fn identity_keys<S: IdentityStore>(&self, store: &S, args: &Json, get: bool) -> Result<Json> {
        let subject = store.resolve_identity(text(args, "subject_id")?)?;
        store.subject(&subject)?;
        let mut keys = store.identity_keys(&subject)?;
        keys.sort_by(|a, b| (a.created_at, &a.key_id).cmp(&(b.created_at, &b.key_id)));
        let values: Vec<_> = keys.into_iter().map(|key| serde_json::json!({"key_id":key.key_id,"public_key":key.public_key,"created_at":key.created_at,"retired_at":key.retired_at,"primary":key.primary,"algorithm":"ed25519","purpose":"identity"})).collect();
        self.key_output(&subject, args, values, get, "identity_key_not_found")
    }

    fn encryption_keys<S: IdentityStore>(&self, store: &S, args: &Json, get: bool) -> Result<Json> {
        let subject = store.resolve_identity(text(args, "subject_id")?)?;
        store.subject(&subject)?;
        let mut keys = store.encryption_keys(&subject)?;
        keys.sort_by(|a, b| (a.created_at, &a.key_id).cmp(&(b.created_at, &b.key_id)));
        let values: Vec<_> = keys.into_iter().map(|key| serde_json::json!({"key_id":key.key_id,"recipient":key.recipient,"public_key":key.public_key,"created_at":key.created_at,"retired_at":key.retired_at,"primary":key.primary,"algorithm":"age-x25519","purpose":"encryption"})).collect();
        self.key_output(&subject, args, values, get, "encryption_key_not_found")
    }

    fn key_output(
        &self,
        subject: &str,
        args: &Json,
        values: Vec<serde_json::Value>,
        get: bool,
        error: &'static str,
    ) -> Result<Json> {
        if !get {
            return output(
                serde_json::json!({"subject_id":subject,"keys":values}),
                vec![],
            );
        }
        let requested = optional(args, "key_id")?.map(Json::as_str).transpose()?;
        let mut value = values
            .into_iter()
            .find(|value| match requested {
                Some(id) => value["key_id"].as_str() == Some(id),
                None => value["primary"].as_bool() == Some(true),
            })
            .ok_or(Error(error))?;
        value["subject_id"] = subject.into();
        output(value, vec![])
    }

    pub fn issued_token(&self, request: &Json, subject: &str) -> Result<Zeroizing<[u8; 32]>> {
        let args = field(request, "arguments")?;
        let nonce = Zeroizing::new(unb64(text(args, "nonce")?, 64)?);
        require(nonce.len() >= 24, "invalid_bootstrap_nonce")?;
        let body = wire(&serde_json::json!({"subject":subject,"request_id":text(request,"request_id")?,"nonce":field(args,"nonce")?}))?.canonical()?;
        let mut mac = Hmac::<Sha256>::new_from_slice(self.token_secret)
            .map_err(|_| Error("invalid_service_key"))?;
        mac.update(b"issued-token-v1\0");
        mac.update(body.as_bytes());
        Ok(Zeroizing::new(mac.finalize().into_bytes().into()))
    }

    fn record_delivery<S: IdentityStore>(
        &self,
        store: &mut S,
        request: &Json,
        credential: &Credential,
        deadline: Option<Timestamp>,
        now: Timestamp,
    ) -> Result<()> {
        let operation = text(request, "operation")?;
        let version: u32 = decode(field(request, "contract_version")?)?;
        let minimum = match operation {
            "identity.temporary" => 3,
            "identity.token_create" | "identity.token_rotate" | "identity.custodial_create" => 2,
            "identity.token_recover" => 1,
            _ => return Err(Error("credential_delivery_upgrade_required")),
        };
        require(version >= minimum, "credential_delivery_upgrade_required")?;
        let args = field(request, "arguments")?;
        let field_name = if operation == "identity.token_recover" {
            "new_recovery_secret"
        } else {
            "recovery_secret"
        };
        let secret = Zeroizing::new(unb64(text(args, field_name)?, 64)?);
        require(secret.len() >= 32, "invalid_recovery_secret")?;
        require(
            *secret != *Zeroizing::new(unb64(text(args, "nonce")?, 64)?),
            "recovery_secret_not_independent",
        )?;
        if operation == "identity.token_recover" {
            require(
                *secret != *Zeroizing::new(unb64(text(args, "recovery_secret")?, 64)?),
                "recovery_secret_not_independent",
            )?;
        }
        let expires = credential
            .expires_at
            .ok_or(Error("recovery_unavailable"))?
            .min(match deadline {
                Some(deadline) => deadline,
                None => after(now, self.recovery_window)?,
            });
        require(expires > now, "recovery_unavailable")?;
        let mut hash = Sha256::new();
        hash.update(b"token-recovery-v1\0");
        hash.update(secret.as_slice());
        store.insert_token_delivery(&TokenDelivery {
            credential_id: credential.id.clone(),
            subject: credential.subject_id.clone(),
            request_id: text(request, "request_id")?.into(),
            request_digest: text(request, "payload_digest")?.into(),
            recovery_verifier: format!("{:x}", hash.finalize()),
            recovery_expires_at: expires,
            claimed_at: None,
            consumed_at: None,
        })
    }

    fn token_create<S: IdentityStore>(
        &self,
        store: &mut S,
        principal: &Principal,
        request: &Json,
        args: &Json,
        version: u32,
        now: Timestamp,
    ) -> Result<Json> {
        signature(principal)?;
        require(version >= 2, "credential_delivery_upgrade_required")?;
        let subject = owner(store, principal)?;
        let ceiling: Vec<Grant> = if version == 3 && optional(args, "ceiling")?.is_none() {
            let reads: BTreeSet<String> = self
                .operations
                .iter()
                .filter(|op| {
                    op.effect == "read"
                        && !op.require_signature
                        && !["identity.", "root.", "system."]
                            .iter()
                            .any(|prefix| op.name.starts_with(prefix))
                })
                .map(|op| format!("{}@{}", op.name, op.version))
                .collect();
            principal
                .ceiling()
                .iter()
                .filter_map(|grant| {
                    let mut grant = grant.clone();
                    grant.operations = grant.operations.intersection(&reads).cloned().collect();
                    (!grant.operations.is_empty()).then_some(grant)
                })
                .collect()
        } else {
            decode(field(args, "ceiling")?)?
        };
        self.validate_ceiling(store, principal, &ceiling)?;
        let ttl: u64 = decode(field(args, "ttl")?)?;
        require((1..=86400).contains(&ttl), "invalid_ttl")?;
        let credential = Credential {
            id: token_id(request, &subject.resource_id)?,
            subject_id: subject.resource_id.clone(),
            kind: CredentialKind::Token,
            verifier: Verifier(
                Sha256::digest(self.issued_token(request, &subject.resource_id)?.as_ref()).to_vec(),
            ),
            ceiling,
            not_before: now,
            expires_at: Some(after(now, ttl)?),
            revoked_at: None,
            source_credential_id: if version >= 3 {
                Some(
                    principal
                        .credential_id()
                        .ok_or(Error("signature_required"))?
                        .into(),
                )
            } else {
                None
            },
        };
        store.save_credential(&credential, subject.auth_version)?;
        self.record_delivery(store, request, &credential, None, now)?;
        if version >= 3 {
            store.put_state(&format!("api:{}",credential.id),"api",credential.expires_at.ok_or(Error("invalid_ttl"))?,&wire(&serde_json::json!({"subject":subject.resource_id,"parent":principal.credential_id(),"auth_version":subject.auth_version}))?)?;
            if let Some(previous) = optional(args, "previous_credential")? {
                let mut old = store.credential(previous.as_str()?)?;
                require(
                    old.kind == CredentialKind::Token
                        && old.subject_id == subject.resource_id
                        && old.revoked_at.is_none(),
                    "credential_owner_required",
                )?;
                require(
                    store
                        .credential_state(&format!("api:{}", old.id))?
                        .is_some(),
                    "api_key_required",
                )?;
                old.revoked_at = Some(now);
                store.save_credential(&old, subject.auth_version)?;
            }
        }
        output(
            serde_json::json!({"subject_id":subject.resource_id,"credential_id":credential.id,"expires_at":credential.expires_at}),
            vec![],
        )
    }

    fn token_rotate<S: IdentityStore>(
        &self,
        store: &mut S,
        principal: &Principal,
        request: &Json,
        _args: &Json,
        now: Timestamp,
    ) -> Result<Json> {
        let subject = owner(store, principal)?;
        let mut old =
            store.credential(principal.credential_id().ok_or(Error("token_required"))?)?;
        require(old.kind == CredentialKind::Token, "token_required")?;
        // Derived API/browser grants must stay bound to their parent. Rotation
        // cannot turn a limited source-bound credential into an independent one.
        let credential = Credential {
            id: token_id(request, &subject.resource_id)?,
            subject_id: subject.resource_id.clone(),
            kind: CredentialKind::Token,
            verifier: Verifier(
                Sha256::digest(self.issued_token(request, &subject.resource_id)?.as_ref()).to_vec(),
            ),
            ceiling: old.ceiling.clone(),
            not_before: now,
            expires_at: Some(after(now, self.temporary_ttl)?),
            revoked_at: None,
            source_credential_id: old.source_credential_id.clone(),
        };
        require(!old.id.starts_with("t_session_"), "signature_required")?;
        store.save_credential(&credential, subject.auth_version)?;
        self.record_delivery(store, request, &credential, None, now)?;
        if let Some(api) = store.credential_state(&format!("api:{}", old.id))? {
            store.put_state(
                &format!("api:{}", credential.id),
                "api",
                credential.expires_at.ok_or(Error("invalid_ttl"))?,
                &api.body,
            )?;
        }
        old.revoked_at = Some(now);
        store.save_credential(&old, subject.auth_version)?;
        output(
            serde_json::json!({"subject_id":subject.resource_id,"credential_id":credential.id,"previous_credential":old.id,"expires_at":credential.expires_at}),
            vec![],
        )
    }

    fn token_recover<S: IdentityStore>(
        &self,
        store: &mut S,
        principal: &Principal,
        request: &Json,
        args: &Json,
        now: Timestamp,
    ) -> Result<Json> {
        require(principal.method() == "recovery", "invalid_recovery_proof")?;
        let subject = owner(store, principal)?;
        let mut old = store.credential(text(args, "credential_id")?)?;
        require(
            old.subject_id == subject.resource_id
                && old.kind == CredentialKind::Token
                && old.current_at(now),
            "recovery_unavailable",
        )?;
        let delivery = store
            .token_delivery(&old.id)?
            .ok_or(Error("recovery_unavailable"))?;
        require(
            delivery.subject == subject.resource_id
                && delivery.request_id == text(args, "original_request_id")?
                && delivery.consumed_at.is_none()
                && delivery.recovery_expires_at > now
                && text(request, "request_id")? != delivery.request_id,
            "recovery_unavailable",
        )?;
        let credential = Credential {
            id: token_id(request, &subject.resource_id)?,
            subject_id: subject.resource_id.clone(),
            kind: CredentialKind::Token,
            verifier: Verifier(
                Sha256::digest(self.issued_token(request, &subject.resource_id)?.as_ref()).to_vec(),
            ),
            ceiling: old.ceiling.clone(),
            not_before: now,
            expires_at: old.expires_at,
            revoked_at: None,
            source_credential_id: old.source_credential_id.clone(),
        };
        require(credential.id != old.id, "recovery_unavailable")?;
        self.record_delivery(
            store,
            request,
            &credential,
            Some(delivery.recovery_expires_at),
            now,
        )?;
        store.save_credential(&credential, subject.auth_version)?;
        old.revoked_at = Some(now);
        store.save_credential(&old, subject.auth_version)?;
        if let Some(api) = store.credential_state(&format!("api:{}", old.id))? {
            store.put_state(
                &format!("api:{}", credential.id),
                "api",
                credential.expires_at.ok_or(Error("recovery_unavailable"))?,
                &api.body,
            )?;
        }
        require(
            store.consume_token_delivery(&old.id, now)?,
            "recovery_unavailable",
        )?;
        output(
            serde_json::json!({"subject_id":subject.resource_id,"credential_id":credential.id,"previous_credential":old.id,"expires_at":credential.expires_at}),
            vec![],
        )
    }

    /// Call in a fresh write transaction AFTER the result transaction committed;
    /// release the returned secret only AFTER this claim transaction commits.
    pub fn claim_delivery<S: IdentityStore>(
        &self,
        store: &mut S,
        request: &Json,
        subject: &str,
        credential_id: &str,
        now: Timestamp,
    ) -> Result<Zeroizing<[u8; 32]>> {
        store.require_live()?;
        let credential = store.credential(credential_id)?;
        require(
            credential.subject_id == subject
                && credential.kind == CredentialKind::Token
                && credential.current_at(now),
            "credential_expired",
        )?;
        let token = self.issued_token(request, subject)?;
        require(
            bool::from(
                credential
                    .verifier
                    .0
                    .as_slice()
                    .ct_eq(Sha256::digest(token.as_ref()).as_slice()),
            ),
            "invalid_token_result",
        )?;
        let delivery = store
            .token_delivery(credential_id)?
            .ok_or(Error("token_delivery_unavailable"))?;
        require(
            delivery.subject == subject
                && delivery.request_id == text(request, "request_id")?
                && delivery.request_digest == text(request, "payload_digest")?
                && delivery.claimed_at.is_none()
                && delivery.consumed_at.is_none(),
            "token_delivery_unavailable",
        )?;
        require(
            store.claim_token_delivery(credential_id, now)?,
            "token_delivery_unavailable",
        )?;
        Ok(token)
    }

    fn renew<S: IdentityStore>(
        &self,
        store: &mut S,
        principal: &Principal,
        request: &Json,
        args: &Json,
        now: Timestamp,
    ) -> Result<Json> {
        signature(principal)?;
        let subject = owner(store, principal)?;
        require(
            subject.kind == SubjectKind::Registered,
            "registered_identity_required",
        )?;
        self.issuer(store, now)?;
        let mut candidates = Vec::new();
        for certificate in store.identity_certificates(&subject.resource_id)? {
            if certificate.kind != CertificateKind::Identity
                || Some(certificate.key_id.as_str()) != principal.credential_id()
            {
                continue;
            }
            match self
                .validator
                .validate(&certificate.resource_id, store, now)
            {
                Ok(current) => candidates.push(current),
                Err(Error(
                    "certificate_revoked"
                    | "certificate_expired"
                    | "certificate_key_revoked"
                    | "credential_revoked"
                    | "authority_source_inactive"
                    | "authority_source_changed"
                    | "authority_source_expired"
                    | "authority_source_lost",
                )) => (),
                Err(error) => return Err(error),
            }
        }
        candidates
            .sort_by(|a, b| (a.not_before, &a.resource_id).cmp(&(b.not_before, &b.resource_id)));
        let source = candidates.first().ok_or(Error("renewal_source_required"))?;
        let remaining = u64::try_from((source.expires_at.0 - now.0).num_seconds())
            .map_err(|_| Error("renewal_source_expiring"))?;
        require(remaining >= 1, "renewal_source_expiring")?;
        let ttl = optional(args, "ttl")?
            .map(decode::<u64>)
            .transpose()?
            .unwrap_or(remaining);
        require(ttl <= remaining, "renewal_ttl_exceeded")?;
        let grants = optional(args, "grants")?
            .map(decode::<Vec<Grant>>)
            .transpose()?
            .unwrap_or_else(|| source.grants.clone());
        for grant in &grants {
            self.validator.validate_grant(grant, store)?;
            let mut allowed = false;
            for parent in &source.grants {
                allowed |= grant.operations.is_subset(&parent.operations)
                    && grant_subset(grant, parent, store)?;
            }
            require(allowed, "renewal_scope_exceeded")?;
        }
        let cert = self.issue(
            store,
            principal,
            request,
            Issuance {
                subject: &subject.resource_id,
                key: &source.key_id,
                grants: Some(grants),
                ttl: Some(ttl),
                authority_source: Some(source),
            },
            now,
        )?;
        output(
            serde_json::json!({"subject_id":subject.resource_id,"certificate_id":cert.resource_id,"expires_at":cert.expires_at}),
            vec![],
        )
    }

    fn upgrade<S: IdentityStore>(
        &self,
        store: &mut S,
        principal: &Principal,
        request: &Json,
        args: &Json,
        now: Timestamp,
    ) -> Result<Json> {
        self.issuer(store, now)?;
        let subject = owner(store, principal)?;
        require(subject.kind == SubjectKind::Temporary, "not_temporary")?;
        let public = unb64(text(args, "public_key")?, 32)?;
        let encryption = age_recipient::public_from_recipient(text(args, "encryption_recipient")?)?;
        require(public != encryption, "encryption_key_must_be_independent")?;
        verify(&public,wire(&serde_json::json!({"subject_id":subject.resource_id,"public_key":field(args,"public_key")?,"handle":field(args,"handle")?,"encryption_recipient":field(args,"encryption_recipient")?}))?.canonical()?.as_bytes(),&decode(field(args,"possession_proof")?)?,"upgrade")?;
        let name = text(args, "handle")?;
        handle(name)?;
        let mut record = store
            .record(Record::Resource, &subject.resource_id)?
            .into_object()?;
        let parent = record
            .get("parent")
            .ok_or(Error("not_found"))?
            .as_str()?
            .to_owned();
        let generation: i64 = decode(record.get("generation").ok_or(Error("not_found"))?)?;
        store.claim_handle(&parent, name, &subject.resource_id)?;
        record.insert("name".into(), wire(&format!("@{name}"))?);
        record.insert(
            "generation".into(),
            wire(
                &generation
                    .checked_add(1)
                    .ok_or(Error("invalid_generation"))?,
            )?,
        );
        record.insert("modified_at".into(), wire(&now)?);
        record.insert("modified_by".into(), wire(&principal.actor())?);
        store.replace_identity_resource(&Json::from_object(record), generation)?;
        let mut updated = bump(&subject)?;
        updated.kind = SubjectKind::Registered;
        store.update_subject(&updated, expected(&subject)?)?;
        let key = key_id(&public);
        let encryption_id = age_recipient::encryption_key_id(&encryption);
        let identity_keys = store.identity_keys(&subject.resource_id)?;
        if let Some(existing) = identity_keys.iter().find(|key| key.primary) {
            require(
                existing.key_id == key && existing.public_key.0 == public,
                "temporary_identity_key_mismatch",
            )?;
        } else {
            store.save_identity_key(&IdentityKey {
                key_id: key.clone(),
                subject: subject.resource_id.clone(),
                public_key: Verifier(public.clone()),
                created_at: now,
                retired_at: None,
                primary: true,
            })?;
        }
        let encryption_keys = store.encryption_keys(&subject.resource_id)?;
        if let Some(existing) = encryption_keys.iter().find(|key| key.primary) {
            require(
                existing.key_id == encryption_id
                    && existing.recipient == text(args, "encryption_recipient")?,
                "temporary_encryption_key_mismatch",
            )?;
        } else {
            store.save_encryption_key(&EncryptionKey {
                key_id: encryption_id.clone(),
                subject: subject.resource_id.clone(),
                recipient: text(args, "encryption_recipient")?.into(),
                public_key: Verifier(encryption.to_vec()),
                created_at: now,
                retired_at: None,
                primary: true,
            })?;
        }
        let credential = Credential {
            id: key.clone(),
            subject_id: subject.resource_id.clone(),
            kind: CredentialKind::SigningKey,
            verifier: Verifier(public),
            ceiling: self.primary_ceiling.to_vec(),
            not_before: now,
            expires_at: None,
            revoked_at: None,
            source_credential_id: None,
        };
        store.save_credential(&credential, updated.auth_version)?;
        let certificate = self.issue(
            store,
            principal,
            request,
            Issuance {
                subject: &subject.resource_id,
                key: &key,
                grants: None,
                ttl: None,
                authority_source: None,
            },
            now,
        )?;
        for mut old in store.identity_credentials(&subject.resource_id)? {
            if old.kind == CredentialKind::Token && old.revoked_at.is_none() {
                old.revoked_at = Some(now);
                store.save_credential(&old, updated.auth_version)?;
            }
        }
        output(
            serde_json::json!({"subject_id":subject.resource_id,"key_id":key,"encryption_key_id":encryption_id,"encryption_recipient":field(args,"encryption_recipient")?,"certificate_id":certificate.resource_id,"handle":name}),
            vec![ResourceRef {
                id: subject.resource_id,
                revision: None,
            }],
        )
    }

    fn upgrade_result<S: IdentityStore>(
        &self,
        store: &S,
        principal: &Principal,
        args: &Json,
        now: Timestamp,
    ) -> Result<Json> {
        let subject = owner(store, principal)?;
        require(
            subject.kind == SubjectKind::Registered && principal.method() == "signature",
            "self_custody_signature_required",
        )?;
        let result = store
            .identity_result(&subject.resource_id, text(args, "upgrade_request_id")?)?
            .ok_or(Error("upgrade_not_completed"))?;
        require(
            text(&result, "operation")? == "identity.upgrade"
                && text(&result, "status")? == "ok"
                && text(&result, "subject")? == subject.resource_id,
            "upgrade_not_completed",
        )?;
        let data = field(&result, "data")?;
        require(
            Some(text(data, "key_id")?) == principal.credential_id(),
            "upgrade_new_key_required",
        )?;
        require(
            store
                .identity_keys(&subject.resource_id)?
                .iter()
                .any(|key| {
                    key.primary
                        && key.retired_at.is_none()
                        && Some(key.key_id.as_str()) == principal.credential_id()
                }),
            "upgrade_new_key_required",
        )?;
        require(
            store
                .encryption_keys(&subject.resource_id)?
                .iter()
                .any(|key| {
                    key.key_id == text(data, "encryption_key_id").unwrap_or("")
                        && key.recipient == text(data, "encryption_recipient").unwrap_or("")
                        && key.retired_at.is_none()
                }),
            "upgrade_encryption_key_changed",
        )?;
        let certificate = self
            .validator
            .validate(text(data, "certificate_id")?, store, now)?;
        require(
            certificate.subject_id == subject.resource_id
                && certificate.key_id == text(data, "key_id")?,
            "upgrade_certificate_mismatch",
        )?;
        output(
            serde_json::json!({"subject_id":field(data,"subject_id")?,"key_id":field(data,"key_id")?,"encryption_key_id":field(data,"encryption_key_id")?,"encryption_recipient":field(data,"encryption_recipient")?,"certificate_id":field(data,"certificate_id")?,"handle":field(data,"handle")?,"status":"completed","upgrade_request_id":field(args,"upgrade_request_id")?,"upgrade_committed_at":field(&result,"committed_at")?}),
            vec![],
        )
    }
}
