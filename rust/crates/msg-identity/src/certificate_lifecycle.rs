//! CSR creation, publication and revocation under current certificate authority.
use crate::{
    authentication::{Entry, Principal},
    authorization::{AccessRequirement, AuthorizationService, Check},
    certificates::CertificateValidator,
    lifecycle::{field, new_id, optional, output, resource, signature, text},
    models::*,
    mutations::IdentityStore,
    require,
};
use msg_core::{digest, unb64, Error, Json, Result};
use msg_crypto::{key_id, verify};

pub trait CertificateStore: IdentityStore {
    fn certificate_request(&self, id: &str) -> Result<CertificateRequest>;
    fn certificate_request_state(&self, id: &str) -> Result<Json>;
    /// Enforce the existing 1024-pending / 8192-total CSR durability caps.
    fn save_certificate_request(&mut self, request: &CertificateRequest) -> Result<()>;
    fn transition_certificate_request(&mut self, state: &Json, expected: i64) -> Result<()>;
    fn publish_certificate(
        &mut self,
        certificate: &Certificate,
        csr: &str,
        expected: i64,
    ) -> Result<()>;
    fn revoke_identity_certificate(&mut self, id: &str, audit: &Json) -> Result<()>;
}

pub struct Service<'a> {
    pub validator: &'a CertificateValidator,
}
impl Service<'_> {
    pub fn supports(operation: &str, version: u32) -> bool {
        version == 1
            && matches!(
                operation,
                "cert.request" | "cert.get" | "cert.cancel" | "cert.publish" | "cert.revoke"
            )
    }

    pub fn handle<S: CertificateStore>(
        &self,
        store: &mut S,
        authorizer: &mut AuthorizationService,
        principal: &Principal,
        request: &Json,
        now: Timestamp,
    ) -> Result<Json> {
        store.require_live()?;
        let name = text(request, "operation")?;
        let version: u32 = decode(field(request, "contract_version")?)?;
        require(Self::supports(name, version), "operation_not_found")?;
        let operation = format!("{name}@{version}");
        let args = field(request, "arguments")?;
        if name == "cert.get" {
            let id = store.resolve_identity(text(args, "id")?)?;
            authorizer.require(
                principal,
                Entry::Network,
                now,
                &[AccessRequirement {
                    resource_id: id.clone(),
                    operation,
                    check: Check::Read,
                }],
                None,
                store.authorization_store(),
            )?;
            let object = store.resource(&id)?;
            if object.kind == "csr" {
                return output(
                    serde_json::json!({"request":store.certificate_request(&id)?,"state":store.certificate_request_state(&id)?}),
                    vec![],
                );
            }
            require(object.kind == "certificate", "not_a_certificate")?;
            return output(
                serde_json::json!({"certificate":store.certificate(&id)?,"revoked":store.certificate_revoked(&id)?}),
                vec![],
            );
        }
        signature(principal)?;
        let subject = principal
            .subject()
            .ok_or(Error("authentication_required"))?;
        let actor = principal.actor().ok_or(Error("authentication_required"))?;
        match name {
            "cert.request" => {
                authorizer.require_base(principal, &operation, subject, store, now)?;
                self.request(store, principal, args, now)
            }
            "cert.cancel" => {
                let id = store.resolve_identity(text(args, "id")?)?;
                let request = store.certificate_request(&id)?;
                require(request.applicant == subject, "csr_owner_required")?;
                authorizer.require_base(principal, &operation, &id, store, now)?;
                let state = store.certificate_request_state(&id)?;
                let generation: i64 = decode(field(&state, "generation")?)?;
                let mut updated = state.into_object()?;
                updated.insert("status".into(), Json::string("cancelled"));
                updated.insert(
                    "generation".into(),
                    wire(
                        &generation
                            .checked_add(1)
                            .ok_or(Error("invalid_generation"))?,
                    )?,
                );
                store.transition_certificate_request(&Json::from_object(updated), generation)?;
                output(
                    serde_json::json!({"status":"cancelled"}),
                    vec![ResourceRef { id, revision: None }],
                )
            }
            "cert.publish" => {
                let csr = store.certificate_request(text(args, "csr_id")?)?;
                let state = store.certificate_request_state(&csr.resource_id)?;
                require(text(&state, "status")? == "pending", "csr_not_pending")?;
                let generation: i64 = decode(field(&state, "generation")?)?;
                let certificate: Certificate = decode(field(args, "certificate")?)?;
                require(
                    certificate.issuer_id == actor && actor == subject && subject != "u_root",
                    "issuer_mismatch",
                )?;
                authorizer.ceiling(principal, &operation, &csr.resource_id, store, now)?;
                require(
                    authorizer.has(
                        principal,
                        "cert.issue",
                        &operation,
                        &csr.resource_id,
                        store,
                        now,
                    )?,
                    "certificate_issue_required",
                )?;
                store.insert_identity_resource(&resource(
                    &certificate.resource_id,
                    "certificate",
                    &certificate.resource_id,
                    "t_certificates",
                    (&certificate.subject_id, &certificate.issuer_id),
                    "0444",
                    now,
                )?)?;
                self.validator
                    .validate_publication(&certificate, &csr, store, now)?;
                store.publish_certificate(&certificate, &csr.resource_id, generation)?;
                let mut credential = store.credential(&certificate.key_id)?;
                if credential.ceiling.is_empty() {
                    credential.ceiling = certificate.grants.clone();
                    let subject = store.subject(&credential.subject_id)?;
                    store.save_credential(&credential, subject.auth_version)?;
                }
                output(
                    serde_json::json!({"certificate_id":certificate.resource_id,"status":"issued"}),
                    vec![ResourceRef {
                        id: certificate.resource_id,
                        revision: None,
                    }],
                )
            }
            "cert.revoke" => {
                let id = store.resolve_identity(text(args, "id")?)?;
                let certificate = store.certificate(&id)?;
                require(certificate.subject_id != "u_root", "local_only")?;
                require(
                    authorizer.has(principal, "cert.revoke", &operation, &id, store, now)?,
                    "certificate_revoke_required",
                )?;
                let audit = wire(
                    &serde_json::json!({"event":{"id":new_id("e")?,"type":"cert.revoke","time":now,
                    "request_id":text(request,"request_id")?,"actor":actor,"subject":subject,"resources":[{"id":id}],"data":{"reason":field(args,"reason")?}},
                    "authority":principal.certificates().iter().map(|id|ResourceRef{id:id.clone(),revision:None}).collect::<Vec<_>>(),
                    "before_digest":digest(wire(&certificate)?.canonical()?.as_bytes()),"after_digest":null,"previous_digest":null,"entry_digest":"","result":"revoked"}),
                )?;
                store.revoke_identity_certificate(&id, &audit)?;
                output(
                    serde_json::json!({"revoked":true}),
                    vec![ResourceRef { id, revision: None }],
                )
            }
            _ => Err(Error("operation_not_found")),
        }
    }

    fn request<S: CertificateStore>(
        &self,
        store: &mut S,
        principal: &Principal,
        args: &Json,
        now: Timestamp,
    ) -> Result<Json> {
        let subject = principal
            .subject()
            .ok_or(Error("authentication_required"))?;
        let actor = principal.actor().ok_or(Error("authentication_required"))?;
        let issuer = store.resolve_identity(text(args, "requested_issuer")?)?;
        require(store.resource(&issuer)?.kind == "user", "invalid_issuer")?;
        let public = unb64(text(args, "public_key")?, 32)?;
        let grants: Vec<Grant> = decode(field(args, "grants")?)?;
        for grant in &grants {
            self.validator.validate_grant(grant, store)?;
        }
        let issuance = optional(args, "issuance")?
            .filter(|value| !value.is_null())
            .map(decode::<IssuancePolicy>)
            .transpose()?;
        let kind: CertificateKind = decode(field(args, "kind")?)?;
        require(
            matches!(
                kind,
                CertificateKind::Identity | CertificateKind::Capability | CertificateKind::Ca
            ),
            "invalid_certificate_kind",
        )?;
        require(
            (kind == CertificateKind::Ca) == issuance.is_some(),
            "ca_issuance_required",
        )?;
        if let Some(policy) = &issuance {
            for grant in &policy.issue_grants {
                self.validator.validate_grant(grant, store)?;
            }
        }
        let mut csr = CertificateRequest {
            resource_id: new_id("csr")?,
            applicant: subject.into(),
            subject_id: subject.into(),
            requested_issuer: issuer,
            public_key: Verifier(public.clone()),
            kind,
            grants,
            issuance,
            requested_ttl_seconds: decode(field(args, "requested_ttl_seconds")?)?,
            target_service: self.validator.service().into(),
            delegation_depth: optional(args, "delegation_depth")?
                .map(decode)
                .transpose()?
                .unwrap_or(0),
            authority_sources: vec![],
            request_digest: String::new(),
            possession_proof: decode(field(args, "possession_proof")?)?,
        };
        require(csr.requested_ttl_seconds > 0, "invalid_ttl")?;
        let bytes = csr.signing_bytes()?;
        csr.request_digest = digest(&bytes);
        verify(&public, &bytes, &csr.possession_proof, "csr")?;
        let key = key_id(&public);
        match store.credential(&key) {
            Ok(existing) => require(existing.subject_id == subject, "key_owner_mismatch")?,
            Err(Error("credential_not_found")) => {
                let owner = store.subject(subject)?;
                store.save_credential(
                    &Credential {
                        id: key,
                        subject_id: subject.into(),
                        kind: CredentialKind::SigningKey,
                        verifier: Verifier(public),
                        ceiling: vec![],
                        not_before: now,
                        expires_at: None,
                        revoked_at: None,
                        source_credential_id: None,
                    },
                    owner.auth_version,
                )?;
            }
            Err(error) => return Err(error),
        }
        store.insert_identity_resource(&resource(
            &csr.resource_id,
            "csr",
            &csr.resource_id,
            "t_csr",
            (subject, actor),
            "0600",
            now,
        )?)?;
        store.save_certificate_request(&csr)?;
        output(
            serde_json::json!({"csr_id":csr.resource_id,"request_digest":csr.request_digest,"status":"pending"}),
            vec![ResourceRef {
                id: csr.resource_id,
                revision: None,
            }],
        )
    }
}
