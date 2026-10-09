//! Narrow current-state read port. A snapshot must not mix database generations.
use crate::{models::*, require};
use msg_core::{Error, Json, Result};
use std::collections::BTreeSet;

pub struct RecoveryDelivery {
    pub verifier: String,
    pub expires_at: Timestamp,
    pub consumed: bool,
    pub request_id: String,
}

#[derive(Clone, Copy)]
pub enum Record {
    Subject,
    Credential,
    Certificate,
    Resource,
}

pub trait AuthorityStore {
    fn record(&self, kind: Record, id: &str) -> Result<Json>;
    fn setting(&self, key: &str) -> Result<Option<Json>>;
    /// Presence, not decoded truthiness: even `false`/`null` quarantine denies.
    fn has_setting(&self, key: &str) -> Result<bool>;
    fn certificate_revoked(&self, id: &str) -> Result<bool>;
    fn request_result(&self, subject: Option<&str>, id: &str, digest: &str)
        -> Result<Option<Json>>;
    fn oauth_state(&self, id: &str) -> Result<Option<crate::oauth::OAuthState>>;
    fn custodial_binding(&self, subject: &str) -> Result<Option<(String, String)>>;
    fn recovery_delivery(
        &self,
        credential: &str,
        subject: &str,
    ) -> Result<Option<RecoveryDelivery>>;

    fn subject(&self, id: &str) -> Result<Subject> {
        let value: Subject = decode(&self.record(Record::Subject, id)?)?;
        require(value.resource_id == id, "storage_record_mismatch")?;
        Ok(value)
    }
    fn credential(&self, id: &str) -> Result<Credential> {
        let value: Credential = decode(&self.record(Record::Credential, id)?)?;
        require(value.id == id, "storage_record_mismatch")?;
        Ok(value)
    }
    fn certificate(&self, id: &str) -> Result<Certificate> {
        let value: Certificate = decode(&self.record(Record::Certificate, id)?)?;
        require(value.resource_id == id, "storage_record_mismatch")?;
        Ok(value)
    }
    fn resource(&self, id: &str) -> Result<ResourceAuthority> {
        let value: ResourceAuthority = decode(&self.record(Record::Resource, id)?)?;
        require(value.id == id, "storage_record_mismatch")?;
        Ok(value)
    }
    fn ancestors(&self, id: &str) -> Result<Vec<ResourceAuthority>> {
        let mut seen = BTreeSet::new();
        let mut result = Vec::new();
        let mut current = self.resource(id)?;
        while let Some(parent) = &current.parent {
            require(seen.insert(current.id.clone()), "parent_cycle")?;
            require(seen.len() <= 1024, "authority_work_limit")?;
            current = self.resource(parent)?;
            result.push(current.clone());
        }
        result.reverse();
        Ok(result)
    }
}

pub fn truthy(value: &Json) -> Result<bool> {
    if value.is_null() {
        return Ok(false);
    }
    if let Ok(value) = value.as_bool() {
        return Ok(value);
    }
    if let Ok(value) = value.as_str() {
        return Ok(!value.is_empty());
    }
    if let Ok(value) = value.as_array() {
        return Ok(!value.is_empty());
    }
    if let Ok(value) = value.as_object() {
        return Ok(!value.is_empty());
    }
    // Arbitrary precision integers are not converted through f64.
    let encoded = value.canonical()?;
    Ok(!matches!(encoded.as_str(), "0" | "0.0" | "-0.0"))
}

pub fn setting_truthy(store: &dyn AuthorityStore, key: &str) -> Result<bool> {
    store
        .setting(key)?
        .as_ref()
        .map(truthy)
        .transpose()
        .map(|v| v.unwrap_or(false))
}

/// Once a runtime generation is stale it cannot recover without a new instance.
/// No public reset function, and a malformed generation latches the same fence.
pub struct RuntimeGeneration {
    generation: Option<String>,
    stale: bool,
}
impl RuntimeGeneration {
    pub fn capture(store: &dyn AuthorityStore) -> Result<Self> {
        Ok(Self {
            generation: Self::read(store)?,
            stale: false,
        })
    }
    fn read(store: &dyn AuthorityStore) -> Result<Option<String>> {
        store
            .setting("recovery_runtime_generation")?
            .map(|v| {
                let s = v.as_str().map_err(|_| Error("recovery_runtime_stale"))?;
                require(!s.is_empty(), "recovery_runtime_stale")?;
                Ok(s.to_owned())
            })
            .transpose()
    }
    pub fn require_current(&mut self, store: &dyn AuthorityStore) -> Result<()> {
        match Self::read(store) {
            Ok(value) if value == self.generation => (),
            _ => self.stale = true,
        }
        require(!self.stale, "recovery_runtime_stale")
    }
}
