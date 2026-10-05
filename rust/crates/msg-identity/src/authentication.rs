//! Authentication admission only. Full resource checks must follow a fresh admission.
use crate::{certificates::CertificateValidator, models::*, require, store::*};
use chrono::Duration;
use msg_core::{unb64, Error, Json, Result, MAX_BYTES};
use msg_crypto::{key_id, subject_id, Signature};
use msg_protocol::Request;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use subtle::ConstantTimeEq;

/// Supplied by the trusted operation registry, never by a request envelope.
#[derive(Deserialize)]
pub struct OperationPolicy {
    pub name: String,
    pub version: u32,
    pub effect: String,
    pub entries: BTreeSet<String>,
    pub require_signature: bool,
    pub anonymous_only: bool,
}
#[derive(Clone, Copy)]
pub enum Entry {
    Network,
    LocalAdmin,
    Worker,
}
impl Entry {
    fn name(self) -> &'static str {
        match self {
            Self::Network => "network",
            Self::LocalAdmin => "local_admin",
            Self::Worker => "worker",
        }
    }
}

/// Fields are private and there is no Deserialize implementation.
/// Request data cannot assert an actor or mint a principal.
///
/// ```compile_fail
/// let _ = serde_json::from_str::<msg_identity::authentication::Principal>("{}");
/// ```
#[derive(Serialize)]
pub struct Principal {
    actor: Option<String>,
    subject: Option<String>,
    credential_id: Option<String>,
    method: &'static str,
    certificates: Vec<String>,
    ceiling: Vec<Grant>,
}
impl Principal {
    pub fn actor(&self) -> Option<&str> {
        self.actor.as_deref()
    }
    pub fn subject(&self) -> Option<&str> {
        self.subject.as_deref()
    }
    pub fn credential_id(&self) -> Option<&str> {
        self.credential_id.as_deref()
    }
    pub fn certificates(&self) -> &[String] {
        &self.certificates
    }
    pub fn ceiling(&self) -> &[Grant] {
        &self.ceiling
    }
    pub fn method(&self) -> &str {
        self.method
    }
}

pub enum Admission {
    /// Authenticated identity only, not permission to execute.
    Fresh(Principal),
    /// Revoked/exhausted credentials receive no fresh principal.
    /// Return the already committed result; never invoke a handler with this branch.
    Replay(Json),
}

pub struct AuthenticationService {
    certificates: CertificateValidator,
    runtime: RuntimeGeneration,
    primary_ceiling: Vec<Grant>,
    temporary_ceiling: Vec<Grant>,
    oauth_policy: Option<crate::oauth::OAuthPolicy>,
}
impl AuthenticationService {
    pub fn new(
        certificates: CertificateValidator,
        store: &dyn AuthorityStore,
        primary_ceiling: Vec<Grant>,
        temporary_ceiling: Vec<Grant>,
    ) -> Result<Self> {
        Ok(Self {
            certificates,
            runtime: RuntimeGeneration::capture(store)?,
            primary_ceiling,
            temporary_ceiling,
            oauth_policy: None,
        })
    }
    pub fn with_oauth_policy(mut self, policy: crate::oauth::OAuthPolicy) -> Self {
        self.oauth_policy = Some(policy);
        self
    }
    pub fn authenticate(
        &mut self,
        request: &Request,
        spec: &OperationPolicy,
        store: &dyn AuthorityStore,
        entry: Entry,
        now: Timestamp,
    ) -> Result<Admission> {
        self.authenticate_for_resource(request, spec, store, entry, now, None)
    }
    /// `resource` must be supplied by the adapter from its validated request URL,
    /// never copied from a field in the operation envelope.
    pub fn authenticate_for_resource(
        &mut self,
        request: &Request,
        spec: &OperationPolicy,
        store: &dyn AuthorityStore,
        entry: Entry,
        now: Timestamp,
        resource: Option<&str>,
    ) -> Result<Admission> {
        let requested_subject = optional_string(request.field("subject")?)?;
        check_archived(store, requested_subject)?;
        self.runtime.require_current(store)?;
        let anchor = store.setting("active_root_certificate")?;
        require(
            anchor
                .as_ref()
                .map(|v| v.as_str())
                .transpose()?
                .unwrap_or(self.certificates.root_id())
                == self.certificates.root_id(),
            "service_restart_required",
        )?;
        require(
            request.field("operation")?.as_str()? == spec.name
                && request.field("contract_version")?.as_integer()? == spec.version.to_string(),
            "operation_policy_mismatch",
        )?;
        if store.has_setting("recovery_quarantine")? {
            return Err(Error(if spec.effect == "read" {
                "recovery_quarantined"
            } else {
                "writes_paused"
            }));
        }
        require(
            request.field("target_service")?.as_str()? == self.certificates.service(),
            "wrong_service",
        )?;
        request.verify_payload_digest()?;
        let proof = request.field("proof")?;
        if spec.effect != "read" || !proof.is_null() {
            let expiry =
                optional_string(request.field("expires_at")?)?.ok_or(Error("request_expired"))?;
            let expiry = Timestamp::parse(expiry)?;
            require(now < expiry, "request_expired")?;
            require(
                expiry.0 - now.0 <= Duration::seconds(300),
                "request_expiry_too_long",
            )?;
        }
        let mut replay = None;
        let principal = match spec.name.as_str() {
            "identity.register" => {
                let signed = proof.as_object().map_err(|_| Error("proof_required"))?;
                require(signed.contains_key("signature"), "proof_required")?;
                let public = unb64(argument(request, "public_key")?.as_str()?, 32)?;
                let uid = subject_id(&public);
                require(
                    !setting_truthy(store, &format!("delegated_identity:{uid}"))?,
                    "delegated_identity_not_upgradable",
                )?;
                require(
                    public.len() == 32 && requested_subject == Some(uid.as_str()),
                    "subject_mismatch",
                )?;
                request.verify_signature_bytes(&public)?;
                Principal {
                    actor: Some(uid.clone()),
                    subject: Some(uid),
                    credential_id: Some(key_id(&public)),
                    method: "signature",
                    certificates: vec![],
                    ceiling: self.primary_ceiling.clone(),
                }
            }
            "identity.temporary" | "identity.custodial_create" => {
                require(
                    proof.is_null() && requested_subject.is_none(),
                    "invalid_bootstrap",
                )?;
                if let Some(public) = request.field("arguments")?.as_object()?.get("public_key") {
                    if truthy(public)? {
                        let uid = subject_id(&unb64(public.as_str()?, 32)?);
                        require(
                            !setting_truthy(store, &format!("delegated_identity:{uid}"))?,
                            "delegated_identity_not_upgradable",
                        )?;
                    }
                }
                let nonce = unb64(argument(request, "nonce")?.as_str()?, 64)?;
                require(nonce.len() >= 24, "invalid_bootstrap_nonce")?;
                let prefix = if spec.name == "identity.custodial_create" {
                    "u_cust_"
                } else {
                    "u_tmp_"
                };
                let uid = format!(
                    "{}{}",
                    prefix,
                    &format!("{:x}", Sha256::digest(&nonce))[..32]
                );
                Principal {
                    credential_id: Some(format!("t_{}", &uid[2..])),
                    actor: Some(uid.clone()),
                    subject: Some(uid),
                    method: "token",
                    certificates: vec![],
                    ceiling: self.temporary_ceiling.clone(),
                }
            }
            "identity.token_recover" => {
                require(
                    proof.is_null() && requested_subject.is_some(),
                    "invalid_recovery_proof",
                )?;
                let old = store.credential(argument(request, "credential_id")?.as_str()?)?;
                crate::oauth::require_binding(
                    store,
                    &old,
                    now,
                    self.oauth_policy.as_ref(),
                    &self.temporary_ceiling,
                    resource,
                )?;
                require(
                    old.kind == CredentialKind::Token && requested_subject == Some(&old.subject_id),
                    "recovery_unavailable",
                )?;
                let actor = store.subject(&old.subject_id)?;
                require(
                    !actor.local_only && actor.resource_id != "u_root",
                    "local_only",
                )?;
                let secret = unb64(argument(request, "recovery_secret")?.as_str()?, 64)?;
                require(secret.len() >= 32, "invalid_recovery_secret")?;
                let mut hasher = Sha256::new();
                hasher.update(b"token-recovery-v1\0");
                hasher.update(&secret);
                let verifier = format!("{:x}", hasher.finalize());
                let delivery = store
                    .recovery_delivery(&old.id, &actor.resource_id)?
                    .ok_or(Error("recovery_unavailable"))?;
                require(
                    bool::from(delivery.verifier.as_bytes().ct_eq(verifier.as_bytes()))
                        && delivery.expires_at > now
                        && delivery.request_id
                            == argument(request, "original_request_id")?.as_str()?,
                    "recovery_unavailable",
                )?;
                if delivery.consumed {
                    replay = store.request_result(
                        Some(&actor.resource_id),
                        request.field("request_id")?.as_str()?,
                        request.field("payload_digest")?.as_str()?,
                    )?;
                    let previous = replay.as_ref().ok_or(Error("recovery_unavailable"))?;
                    let data = previous
                        .as_object()?
                        .get("data")
                        .ok_or(Error("recovery_unavailable"))?
                        .as_object()?;
                    require(
                        data.get("previous_credential")
                            .and_then(|v| v.as_str().ok())
                            == Some(&old.id),
                        "recovery_unavailable",
                    )?;
                } else {
                    require(
                        old.revoked_at.is_none() && old.expires_at.is_some_and(|v| v > now),
                        "recovery_unavailable",
                    )?;
                }
                let mut ceiling = Vec::new();
                for grant in &self.primary_ceiling {
                    if grant.capability == "identity.basic"
                        && grant.operations.contains("identity.token_recover@1")
                    {
                        let mut grant = grant.clone();
                        grant.operations = BTreeSet::from(["identity.token_recover@1".to_owned()]);
                        ceiling.push(grant);
                    }
                }
                require(!ceiling.is_empty(), "recovery_unavailable")?;
                Principal {
                    actor: Some(actor.resource_id.clone()),
                    subject: Some(actor.resource_id),
                    credential_id: Some(old.id),
                    method: "recovery",
                    certificates: vec![],
                    ceiling,
                }
            }
            _ if proof.is_null() => {
                let anonymous_write = matches!(
                    spec.name.as_str(),
                    "communication.internet_receive" | "identity.link_claim"
                ) && spec.version == 1
                    && spec.anonymous_only
                    && spec.effect == "transaction";
                require(
                    requested_subject.is_none() && (spec.effect == "read" || anonymous_write),
                    "authentication_required",
                )?;
                Principal {
                    actor: None,
                    subject: None,
                    credential_id: None,
                    method: "anonymous",
                    certificates: vec![],
                    ceiling: vec![],
                }
            }
            _ => {
                let proof = proof.as_object()?;
                let signature: Option<Signature> =
                    proof.get("signature").map(decode).transpose()?;
                let cid = match &signature {
                    Some(s) => s.key_id.as_str(),
                    None => proof
                        .get("credential_id")
                        .ok_or(Error("invalid_proof"))?
                        .as_str()?,
                };
                let credential = store.credential(cid)?;
                let actor = store.subject(&credential.subject_id)?;
                require(
                    credential.not_before <= now && credential.expires_at.is_none_or(|v| now < v),
                    "credential_expired",
                )?;
                let (method, ids): (&str, Vec<String>) = match &signature {
                    Some(signature) => {
                        require(
                            credential.kind == CredentialKind::SigningKey,
                            "wrong_credential_kind",
                        )?;
                        msg_crypto::verify(
                            &credential.verifier.0,
                            &request.signing_bytes()?,
                            signature,
                            "request",
                        )?;
                        (
                            "signature",
                            decode(proof.get("certificates").ok_or(Error("invalid_proof"))?)?,
                        )
                    }
                    None => {
                        require(
                            credential.kind == CredentialKind::Token,
                            "wrong_credential_kind",
                        )?;
                        let token = unb64(
                            proof.get("token").ok_or(Error("invalid_proof"))?.as_str()?,
                            MAX_BYTES,
                        )?;
                        let hash = Sha256::digest(&token);
                        require(
                            bool::from(credential.verifier.0.as_slice().ct_eq(hash.as_slice())),
                            "invalid_token",
                        )?;
                        crate::oauth::require_binding(
                            store,
                            &credential,
                            now,
                            self.oauth_policy.as_ref(),
                            &self.temporary_ceiling,
                            resource,
                        )?;
                        ("token", vec![])
                    }
                };
                if credential.revoked_at.is_some() {
                    if method == "token"
                        && matches!(
                            spec.name.as_str(),
                            "identity.token_rotate" | "identity.custodial_upgrade_finish"
                        )
                    {
                        replay = store.request_result(
                            Some(&actor.resource_id),
                            request.field("request_id")?.as_str()?,
                            request.field("payload_digest")?.as_str()?,
                        )?;
                    }
                    let previous = replay.as_ref().ok_or(Error("credential_revoked"))?;
                    let data = previous
                        .as_object()?
                        .get("data")
                        .ok_or(Error("credential_revoked"))?
                        .as_object()?;
                    require(
                        data.get("previous_credential")
                            .and_then(|v| v.as_str().ok())
                            == Some(cid),
                        "credential_revoked",
                    )?;
                    let successor = store.credential(
                        data.get("credential_id")
                            .ok_or(Error("credential_revoked"))?
                            .as_str()?,
                    )?;
                    require(
                        successor.subject_id == actor.resource_id
                            && successor.revoked_at.is_none()
                            && successor.expires_at.is_none_or(|v| v > now),
                        "credential_revoked",
                    )?;
                }
                let subject = store.subject(
                    requested_subject
                        .filter(|v| !v.is_empty())
                        .unwrap_or(&actor.resource_id),
                )?;
                if let Some(binding) =
                    store.setting(&format!("delegated_identity:{}", actor.resource_id))?
                {
                    let binding: DelegatedIdentity = decode(&binding)?;
                    require(
                        subject.resource_id == binding.grantor,
                        "delegated_subject_required",
                    )?;
                    require(ids.contains(&binding.certificate_id), "delegation_required")?;
                    if binding.max_uses.is_some_and(|max| binding.uses >= max) {
                        let previous = store
                            .request_result(
                                Some(&subject.resource_id),
                                request.field("request_id")?.as_str()?,
                                request.field("payload_digest")?.as_str()?,
                            )?
                            .ok_or(Error("delegation_exhausted"))?;
                        require(
                            previous
                                .as_object()?
                                .get("actor")
                                .and_then(|v| v.as_str().ok())
                                == Some(actor.resource_id.as_str()),
                            "delegation_exhausted",
                        )?;
                        replay = Some(previous);
                    }
                }
                require(
                    !(actor.local_only
                        || subject.local_only
                        || actor.resource_id == "u_root"
                        || subject.resource_id == "u_root")
                        || matches!(entry, Entry::LocalAdmin),
                    "local_only",
                )?;
                require(spec.entries.contains(entry.name()), "entry_not_allowed")?;
                let operation = format!("{}@{}", spec.name, spec.version);
                require(
                    credential
                        .ceiling
                        .iter()
                        .any(|g| g.operations.contains(&operation)),
                    "credential_ceiling",
                )?;
                if actor.kind == SubjectKind::Custodial
                    && method == "token"
                    && spec.effect != "read"
                {
                    let download = spec.name == "transfer.open"
                        && argument(request, "direction").and_then(Json::as_str).ok()
                            == Some("download");
                    require(
                        custodial_write(&spec.name) || download,
                        "custodial_operation_not_supported",
                    )?;
                }
                let confirmation = actor.kind == SubjectKind::Custodial
                    && method == "token"
                    && matches!(
                        spec.name.as_str(),
                        "achievement.finish"
                            | "achievement.pin"
                            | "achievement.unpin"
                            | "achievement.reorder"
                    );
                require(
                    !spec.require_signature || method == "signature" || confirmation,
                    "signature_required",
                )?;
                let mut validated = Vec::new();
                let mut delegated = false;
                for id in ids {
                    let cert = self.certificates.validate(&id, store, now)?;
                    require(
                        cert.subject_id == actor.resource_id && cert.key_id == credential.id,
                        "certificate_subject_mismatch",
                    )?;
                    if cert.kind == CertificateKind::Delegation {
                        for source in &cert.authority_sources {
                            if let Some(fact) =
                                store.setting(&format!("delegation:{}", source.id))?
                            {
                                delegated |= fact
                                    .as_object()?
                                    .get("grantor")
                                    .and_then(|v| v.as_str().ok())
                                    == Some(subject.resource_id.as_str());
                            }
                        }
                    }
                    validated.push(cert.resource_id);
                }
                require(
                    actor.resource_id == subject.resource_id || delegated,
                    "delegation_required",
                )?;
                Principal {
                    actor: Some(actor.resource_id),
                    subject: Some(subject.resource_id),
                    credential_id: Some(credential.id),
                    method,
                    certificates: validated,
                    ceiling: credential.ceiling,
                }
            }
        };
        check_archived(store, principal.actor())?;
        check_archived(store, principal.subject())?;
        Ok(match replay {
            Some(result) => Admission::Replay(result),
            None => Admission::Fresh(principal),
        })
    }
}
fn optional_string(value: &Json) -> Result<Option<&str>> {
    if value.is_null() {
        Ok(None)
    } else {
        value.as_str().map(Some)
    }
}
fn argument<'a>(request: &'a Request, name: &str) -> Result<&'a Json> {
    request
        .field("arguments")?
        .as_object()?
        .get(name)
        .ok_or(Error("missing_field"))
}
fn check_archived(store: &dyn AuthorityStore, subject: Option<&str>) -> Result<()> {
    require(
        !setting_truthy(
            store,
            &format!("identity_archived:{}", subject.unwrap_or("None")),
        )?,
        "account_archived",
    )
}
fn custodial_write(name: &str) -> bool {
    matches!(
        name,
        "identity.rename"
            | "identity.oauth_approve"
            | "content.post_create"
            | "content.post_edit"
            | "content.file_put"
            | "content.attach"
            | "file.create"
            | "file.copy"
            | "file.write"
            | "file.patch"
            | "discussion.reply"
            | "discussion.quote"
            | "discussion.repost"
            | "identity.token_rotate"
            | "identity.custodial_upgrade_start"
            | "identity.custodial_upgrade_finish"
            | "identity.custodial_rewrap_entry"
            | "identity.custodial_rewrap_revision"
            | "identity.custodial_rewrap_ack"
            | "achievement.start"
            | "achievement.answer"
            | "achievement.finish"
            | "achievement.pin"
            | "achievement.unpin"
            | "achievement.reorder"
    )
}
