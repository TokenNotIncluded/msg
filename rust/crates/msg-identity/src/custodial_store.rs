//! Narrow transaction ports for custody exit; no private key leaves the vault.
use crate::{models::Timestamp, mutations::IdentityStore};
use msg_core::{Json, Result};
use msg_crypto::vault_box::SealedSecret;
use serde::{Deserialize, Serialize};

#[derive(Clone, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(deny_unknown_fields)]
pub struct HistoryItem {
    pub id: String,
    pub revision: String,
    pub ciphertext_digest: String,
}

#[derive(Clone)]
pub struct Upgrade {
    pub id: String,
    pub subject: String,
    pub credential_id: String,
    pub status: String,
    pub expires_at: Timestamp,
    pub challenge: Json,
    pub ephemeral: Option<SealedSecret>,
    pub details: Json,
}

pub trait CustodialStore: IdentityStore {
    fn custodial_upgrade(&self, id: &str) -> Result<Option<Upgrade>>;
    fn pending_custodial_upgrade(&self, subject: &str, now: Timestamp) -> Result<bool>;
    fn expire_custodial_upgrades(&mut self, subject: &str, now: Timestamp) -> Result<()>;
    fn insert_custodial_upgrade(&mut self, record: &Upgrade) -> Result<()>;
    /// Compare status AND the complete previous body before changing state.
    /// Challenge, subject, source credential and deadline are immutable.
    fn replace_custodial_upgrade(&mut self, before: &Upgrade, after: &Upgrade) -> Result<()>;
    fn owned_age_inventory(&self, subject: &str) -> Result<Vec<HistoryItem>>;
    fn latest_recovery_policy(&self, subject: &str) -> Result<Json>;
    /// Permanently erase signing fields; retain only decryption if requested.
    fn retire_custodial_vault(&mut self, subject: &str, retain: bool, now: Timestamp)
        -> Result<()>;
}
