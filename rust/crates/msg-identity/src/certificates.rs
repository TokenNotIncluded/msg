//! Current-state CA chains, issuance limits and live delegation sources.
use crate::{
    models::*,
    policy::*,
    require,
    store::{setting_truthy, AuthorityStore},
};
use msg_core::{digest, Error, Result};
use std::collections::BTreeSet;

const ROOT: &str = "u_root";
const ONLINE_CA: &str = "u_online_ca";
const ONLINE_ISSUABLE: &[&str] = &[
    "identity.basic",
    "resource.basic",
    "discussion.basic",
    "communication.basic",
    "discovery.basic",
    "transfer.basic",
    "group.basic",
    "cert.request",
    "git.basic",
    "hosting.basic",
    "keystore.basic",
    "batch.basic",
    "sharing.basic",
    "money.basic",
    "store.basic",
    "bounty.basic",
    "orders.basic",
    "delivery.basic",
];

pub struct CertificateValidator {
    registry: Registry,
    root: Certificate,
    root_bytes: String,
    root_public_key: [u8; 32],
    service: String,
}
impl CertificateValidator {
    /// The anchor and registry must come from trusted local configuration.
    pub fn new(
        registry: Registry,
        root: Certificate,
        root_public_key: [u8; 32],
        service: String,
    ) -> Result<Self> {
        let root_bytes = wire(&root)?.canonical()?;
        Ok(Self {
            registry,
            root,
            root_bytes,
            root_public_key,
            service,
        })
    }
    pub fn root_id(&self) -> &str {
        &self.root.resource_id
    }
    pub fn service(&self) -> &str {
        &self.service
    }
    pub fn validate(
        &self,
        id: &str,
        store: &dyn AuthorityStore,
        now: Timestamp,
    ) -> Result<Certificate> {
        Validation {
            validator: self,
            store,
            now,
            work: 0,
        }
        .walk(id, &BTreeSet::new(), None, None)
    }
    pub fn validate_publication(
        &self,
        cert: &Certificate,
        csr: &CertificateRequest,
        store: &dyn AuthorityStore,
        now: Timestamp,
    ) -> Result<Certificate> {
        let bytes = csr.signing_bytes()?;
        require(csr.request_digest == digest(&bytes), "csr_digest_mismatch")?;
        msg_crypto::verify(&csr.public_key.0, &bytes, &csr.possession_proof, "csr")?;
        require(
            cert.subject_id == csr.subject_id
                && cert.issuer_id == csr.requested_issuer
                && cert.key_id == msg_crypto::key_id(&csr.public_key.0)
                && cert.kind == csr.kind
                && wire(&cert.grants)? == wire(&csr.grants)?
                && wire(&cert.issuance)? == wire(&csr.issuance)?
                && wire(&cert.authority_sources)? == wire(&csr.authority_sources)?
                && cert.delegation_depth == csr.delegation_depth
                && cert.target_service == csr.target_service,
            "csr_certificate_mismatch",
        )?;
        require(
            duration_within(cert, csr.requested_ttl_seconds),
            "csr_ttl_exceeded",
        )?;
        Validation {
            validator: self,
            store,
            now,
            work: 0,
        }
        .walk(
            &cert.resource_id,
            &BTreeSet::new(),
            Some(cert),
            Some(&csr.resource_id),
        )
    }
}
fn duration_within(cert: &Certificate, seconds: u64) -> bool {
    let duration = cert.expires_at.0 - cert.not_before.0;
    duration
        .num_microseconds()
        .is_some_and(|micros| micros >= 0 && (micros as u128) <= u128::from(seconds) * 1_000_000)
}

struct Validation<'a> {
    validator: &'a CertificateValidator,
    store: &'a dyn AuthorityStore,
    now: Timestamp,
    work: usize,
}
impl Validation<'_> {
    fn spend(&mut self) -> Result<()> {
        self.work += 1;
        require(self.work <= 4096, "authority_work_limit")
    }
    fn allowed_issuance(&self, grant: &Grant, policy: &IssuancePolicy) -> Result<bool> {
        self.validator.registry.validate_grant(grant, self.store)?;
        for allowed in &policy.issue_grants {
            if grant_subset(grant, allowed, self.store)? {
                return Ok(true);
            }
        }
        Ok(false)
    }
    fn can_issue(&self, parent: &Certificate, capability: &str, resource: &str) -> Result<bool> {
        for grant in &parent.grants {
            if grant_covers(grant, capability, "cert.publish@1", resource, self.store)? {
                return Ok(true);
            }
        }
        Ok(false)
    }
    fn walk(
        &mut self,
        id: &str,
        seen: &BTreeSet<String>,
        proposed: Option<&Certificate>,
        issuance_resource: Option<&str>,
    ) -> Result<Certificate> {
        self.spend()?;
        require(!seen.contains(id) && seen.len() < 32, "certificate_cycle")?;
        let mut seen = seen.clone();
        seen.insert(id.to_owned());
        let cert = match proposed {
            Some(cert) => cert.clone(),
            None => self.store.certificate(id)?,
        };
        require(cert.resource_id == id, "storage_record_mismatch")?;
        require(
            cert.target_service == self.validator.service,
            "wrong_service",
        )?;
        require(
            cert.not_before <= self.now && self.now < cert.expires_at,
            "certificate_expired",
        )?;
        if proposed.is_none() {
            require(!self.store.certificate_revoked(id)?, "certificate_revoked")?;
        }
        if id == self.validator.root_id() {
            require(
                wire(&cert)?.canonical()? == self.validator.root_bytes,
                "trust_anchor_mismatch",
            )?;
            require(
                cert.subject_id == ROOT
                    && cert.issuer_id == ROOT
                    && cert.parent_certificate_id.is_none()
                    && cert.kind == CertificateKind::Ca,
                "invalid_root_certificate",
            )?;
            msg_crypto::verify(
                &self.validator.root_public_key,
                &cert.signing_bytes()?,
                &cert.signature,
                "certificate",
            )?;
            return Ok(cert);
        }
        require(
            cert.subject_id != ROOT && cert.parent_certificate_id.is_some(),
            "local_only",
        )?;
        let parent = self.walk(
            cert.parent_certificate_id
                .as_deref()
                .ok_or(Error("local_only"))?,
            &seen,
            None,
            None,
        )?;
        require(
            parent.kind == CertificateKind::Ca && parent.issuance.is_some(),
            "issuer_not_ca",
        )?;
        let policy = parent.issuance.as_ref().ok_or(Error("issuer_not_ca"))?;
        require(cert.issuer_id == parent.subject_id, "issuer_mismatch")?;
        let key = self.store.credential(&parent.key_id)?;
        require(
            key.subject_id == parent.subject_id && key.revoked_at.is_none(),
            "issuer_key_revoked",
        )?;
        msg_crypto::verify(
            &key.verifier.0,
            &cert.signing_bytes()?,
            &cert.signature,
            "certificate",
        )?;
        let subject_key = self.store.credential(&cert.key_id)?;
        require(
            subject_key.subject_id == cert.subject_id,
            "certificate_key_mismatch",
        )?;
        require(subject_key.current_at(self.now), "certificate_key_revoked")?;
        require(
            parent.not_before <= cert.not_before && cert.expires_at <= parent.expires_at,
            "certificate_validity_escalation",
        )?;
        require(
            duration_within(&cert, policy.max_cert_ttl_seconds),
            "certificate_ttl_escalation",
        )?;
        require(
            cert.delegation_depth <= policy.max_delegation_depth,
            "delegation_depth_exceeded",
        )?;
        let resource = match issuance_resource {
            Some(id) => id.to_owned(),
            None => self
                .store
                .setting(&format!("certificate_request:{}", cert.resource_id))?
                .map(|v| v.as_str().map(str::to_owned))
                .transpose()?
                .unwrap_or_else(|| cert.resource_id.clone()),
        };
        require(
            self.can_issue(&parent, "cert.issue", &resource)?,
            "issuer_cannot_issue",
        )?;
        for grant in &cert.grants {
            require(
                self.allowed_issuance(grant, policy)?,
                "issuance_scope_exceeded",
            )?;
        }
        if cert.subject_id == ONLINE_CA {
            require(
                parent.resource_id == self.validator.root_id()
                    && cert.kind == CertificateKind::Ca
                    && cert
                        .issuance
                        .as_ref()
                        .is_some_and(|p| p.max_child_ca_depth == 0)
                    && cert.grants.len() == 1
                    && cert.grants[0].capability == "cert.issue"
                    && cert.grants[0].operations.contains("cert.publish@1"),
                "online_ca_policy_exceeded",
            )?;
            for grant in &cert
                .issuance
                .as_ref()
                .ok_or(Error("online_ca_policy_exceeded"))?
                .issue_grants
            {
                require(
                    ONLINE_ISSUABLE.contains(&grant.capability.as_str())
                        && !self
                            .validator
                            .registry
                            .capability(&grant.capability, grant.version)?
                            .ca_only,
                    "online_ca_policy_exceeded",
                )?;
            }
        }
        if parent.subject_id == ONLINE_CA {
            require(
                matches!(
                    cert.kind,
                    CertificateKind::Identity | CertificateKind::Delegation
                ) && (cert.kind != CertificateKind::Delegation
                    || !cert.authority_sources.is_empty())
                    && cert
                        .grants
                        .iter()
                        .all(|g| ONLINE_ISSUABLE.contains(&g.capability.as_str())),
                "online_ca_policy_exceeded",
            )?;
        }
        if cert.kind == CertificateKind::Ca {
            require(
                cert.issuance.is_some() && policy.max_child_ca_depth > 0,
                "ca_depth_exceeded",
            )?;
            let child = cert.issuance.as_ref().ok_or(Error("ca_depth_exceeded"))?;
            require(
                self.can_issue(&parent, "cert.ca.issue", &resource)?,
                "issuer_cannot_issue_ca",
            )?;
            require(
                child.max_child_ca_depth < policy.max_child_ca_depth,
                "ca_depth_exceeded",
            )?;
            let mut level = 1;
            let mut ancestor = parent.clone();
            while ancestor.resource_id != self.validator.root_id() {
                self.spend()?;
                level += 1;
                require(level <= 3, "ca_depth_exceeded")?;
                ancestor = self.store.certificate(
                    ancestor
                        .parent_certificate_id
                        .as_deref()
                        .ok_or(Error("issuer_not_ca"))?,
                )?;
            }
            require(child.max_child_ca_depth <= 3 - level, "ca_depth_exceeded")?;
            require(
                child.max_cert_ttl_seconds <= policy.max_cert_ttl_seconds
                    && child.max_delegation_depth <= policy.max_delegation_depth,
                "issuance_policy_escalation",
            )?;
            for grant in &child.issue_grants {
                require(
                    self.allowed_issuance(grant, policy)?,
                    "issuance_scope_exceeded",
                )?;
            }
        } else {
            require(cert.issuance.is_none(), "unexpected_issuance_policy")?;
        }
        for source in &cert.authority_sources {
            self.authority(source, &cert, &seen)?;
        }
        Ok(cert)
    }
    fn authority(
        &mut self,
        source: &ResourceRef,
        cert: &Certificate,
        seen: &BTreeSet<String>,
    ) -> Result<()> {
        self.spend()?;
        let resource = self.store.resource(&source.id)?;
        require(
            resource.kind == "delegation" && resource.state == "active",
            "authority_source_inactive",
        )?;
        require(
            source.revision == resource.revision,
            "authority_source_changed",
        )?;
        let fact: DelegationFact = decode(
            &self
                .store
                .setting(&format!("delegation:{}", source.id))?
                .ok_or(Error("authority_source_invalid"))?,
        )?;
        require(fact.grantee == cert.subject_id, "authority_source_invalid")?;
        require(fact.expires_at > self.now, "authority_source_expired")?;
        let key = fact
            .grantor_credential_id
            .as_ref()
            .map(|id| self.store.credential(id))
            .transpose()?;
        if let Some(key) = &key {
            require(key.current_at(self.now), "authority_source_inactive")?;
            require(
                !setting_truthy(self.store, &format!("identity_archived:{}", key.subject_id))?,
                "authority_source_inactive",
            )?;
        }
        for grant in &fact.grants {
            require(
                self.store.resource(&grant.scope.resource_id)?.owner == fact.grantor,
                "authority_source_lost",
            )?;
            if let Some(key) = &key {
                let mut allowed = false;
                for parent in &key.ceiling {
                    allowed |= grant_subset(grant, parent, self.store)?;
                }
                require(allowed, "authority_source_lost")?;
            }
        }
        for grant in cert
            .grants
            .iter()
            .chain(cert.issuance.iter().flat_map(|p| &p.issue_grants))
        {
            let mut allowed = false;
            for parent in &fact.grants {
                allowed |= grant_subset(grant, parent, self.store)?;
            }
            require(allowed, "delegation_scope_exceeded")?;
        }
        if let Some(parent) = &fact.parent_certificate {
            self.walk(parent, seen, None, None)?;
        }
        Ok(())
    }
}
