//! Typed ports for identity mutations inside one existing metadata transaction.
//! There is no arbitrary SQL escape hatch and no commit operation on this port.
use crate::{
    browser::BrowserStore,
    models::*,
    vault::{VaultRecord, VaultStore},
};
use msg_core::{Json, Result};

#[derive(Clone)]
pub struct IdentityKey {
    pub key_id: String,
    pub subject: String,
    pub public_key: Verifier,
    pub created_at: Timestamp,
    pub retired_at: Option<Timestamp>,
    pub primary: bool,
}

#[derive(Clone)]
pub struct EncryptionKey {
    pub key_id: String,
    pub subject: String,
    pub recipient: String,
    pub public_key: Verifier,
    pub created_at: Timestamp,
    pub retired_at: Option<Timestamp>,
    pub primary: bool,
}

#[derive(Clone)]
pub struct TokenDelivery {
    pub credential_id: String,
    pub subject: String,
    pub request_id: String,
    pub request_digest: String,
    pub recovery_verifier: String,
    pub recovery_expires_at: Timestamp,
    pub claimed_at: Option<Timestamp>,
    pub consumed_at: Option<Timestamp>,
}

pub trait IdentityStore: BrowserStore + VaultStore {
    /// Resolve the caller's reference without changing the signed request.
    fn resolve_identity(&self, reference: &str) -> Result<String>;
    fn identity_revision(&self, reference: &ResourceRef) -> Result<Json>;
    /// Compare the persisted auth version; -1 permits only a new identity.
    fn update_subject(&mut self, subject: &Subject, expected: i64) -> Result<()>;
    /// Check live names AND retained aliases under the namespace root.
    fn claim_handle(&mut self, parent: &str, handle: &str, subject: &str) -> Result<()>;
    /// Decode and use the metadata store's typed resource insertion API.
    fn insert_identity_resource(&mut self, resource: &Json) -> Result<()>;
    fn replace_identity_resource(&mut self, resource: &Json, expected: i64) -> Result<()>;
    fn set_identity_setting(&mut self, key: &str, value: &Json) -> Result<()>;
    fn identity_result(&self, subject: &str, request: &str) -> Result<Option<Json>>;
    fn identity_credentials(&self, subject: &str) -> Result<Vec<Credential>>;
    fn identity_keys(&self, subject: &str) -> Result<Vec<IdentityKey>>;
    /// Only retirement/primary fields may change for an existing key row.
    fn save_identity_key(&mut self, key: &IdentityKey) -> Result<()>;
    fn encryption_keys(&self, subject: &str) -> Result<Vec<EncryptionKey>>;
    fn encryption_key_exists(&self, key: &str) -> Result<bool>;
    /// Only retirement/primary fields may change for an existing key row.
    fn save_encryption_key(&mut self, key: &EncryptionKey) -> Result<()>;
    fn custodial_migration_pending(&self, subject: &str, now: Timestamp) -> Result<bool>;
    fn identity_certificates(&self, subject: &str) -> Result<Vec<Certificate>>;
    fn register_certificate(&mut self, certificate: &Certificate) -> Result<()>;
    /// Decode and append through the store's hash-chained audit API.
    fn append_identity_audit(&mut self, audit: &Json) -> Result<()>;
    fn insert_token_delivery(&mut self, delivery: &TokenDelivery) -> Result<()>;
    fn insert_custodial_vault(&mut self, vault: &VaultRecord) -> Result<()>;
    fn token_delivery(&self, credential: &str) -> Result<Option<TokenDelivery>>;
    /// Atomically require claimed_at AND consumed_at NULL, then set claimed_at.
    fn claim_token_delivery(&mut self, credential: &str, now: Timestamp) -> Result<bool>;
    /// Atomically require consumed_at NULL, then set consumed_at.
    fn consume_token_delivery(&mut self, credential: &str, now: Timestamp) -> Result<bool>;
}
