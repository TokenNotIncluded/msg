//! Typed storage rows, not an operation schema or authorization decision.
//! The domain layer still owns tags, quotas, ACLs, receipt signing and policy.
use msg_core::{Error, Json, Result};
use msg_crypto::Signature;
use msg_identity::models::{ResourceRef, Timestamp};
use serde::{de::DeserializeOwned, Deserialize, Deserializer, Serialize, Serializer};
use std::collections::BTreeMap;

pub fn decode<T: DeserializeOwned>(value: &Json) -> Result<T> {
    serde_json::from_str(&value.canonical()?).map_err(|_| Error("invalid_storage_record"))
}
pub fn encode<T: Serialize>(value: &T) -> Result<Json> {
    Json::parse(&serde_json::to_string(value).map_err(|_| Error("invalid_storage_record"))?)
}
fn required_option<'de, T: Deserialize<'de>, D: Deserializer<'de>>(
    d: D,
) -> std::result::Result<Option<T>, D::Error> {
    Option::<T>::deserialize(d)
}
mod mode {
    use super::*;
    pub fn serialize<S: Serializer>(n: &u16, s: S) -> std::result::Result<S::Ok, S::Error> {
        if *n > 0o7777 {
            return Err(serde::ser::Error::custom("invalid_mode"));
        }
        s.serialize_str(&format!("{n:04o}"))
    }
    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> std::result::Result<u16, D::Error> {
        let value = String::deserialize(d)?;
        if value.len() != 4 || !value.bytes().all(|b| (b'0'..=b'7').contains(&b)) {
            return Err(serde::de::Error::custom("invalid_mode"));
        }
        u16::from_str_radix(&value, 8).map_err(serde::de::Error::custom)
    }
}
#[derive(Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ResourceState {
    Active,
    Archived,
    Purged,
}
impl ResourceState {
    pub(crate) fn sql(self) -> &'static str {
        match self {
            Self::Active => "active",
            Self::Archived => "archived",
            Self::Purged => "purged",
        }
    }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Resource {
    pub id: String,
    #[serde(rename = "type")]
    pub resource_type: String,
    pub type_version: i64,
    pub name: String,
    #[serde(deserialize_with = "required_option")]
    pub parent: Option<String>,
    pub owner: String,
    pub group: String,
    #[serde(with = "mode")]
    pub mode: u16,
    pub generation: i64,
    #[serde(deserialize_with = "required_option")]
    pub revision: Option<String>,
    pub state: ResourceState,
    pub created_at: Timestamp,
    pub created_by: String,
    pub modified_at: Timestamp,
    pub modified_by: String,
    #[serde(default)]
    pub tags: Vec<String>,
}
impl Resource {
    pub(crate) fn validate(&self) -> Result<()> {
        if self.generation < 0 {
            return Err(Error("invalid_nonnegative_integer"));
        }
        if self.type_version < 1 {
            return Err(Error("invalid_version"));
        }
        if self.mode > 0o7777 {
            return Err(Error("invalid_mode"));
        }
        if self.parent.as_deref() == Some(&self.id) {
            return Err(Error("parent_cycle"));
        }
        // Unicode tag normalization belongs to the still-unported domain layer.
        // Preserve existing normalized Unicode; never re-normalize stored bytes.
        if self.tags.len() > 16 || self.tags.windows(2).any(|w| w[0] >= w[1]) {
            return Err(Error("invalid_tags"));
        }
        Ok(())
    }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BlobRef {
    pub digest: String,
    pub size: i64,
    pub media_type: String,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Relation {
    #[serde(rename = "type")]
    pub relation_type: String,
    pub target: ResourceRef,
    #[serde(default)]
    pub excerpt: Option<[i64; 2]>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Revision {
    pub format_version: i64,
    pub id: String,
    pub resource_id: String,
    pub parents: Vec<String>,
    pub content: BlobRef,
    pub relations: Vec<Relation>,
    pub actor: String,
    pub subject: String,
    pub author: String,
    pub created_at: Timestamp,
    pub manifest_digest: String,
    #[serde(default)]
    pub signature: Option<Signature>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub summary: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub change_note: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_kind: Option<SourceKind>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_version: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_digest: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub signature_source: Option<SignatureSource>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SourceKind {
    Release,
    User,
    Operation,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SignatureSource {
    Custodial,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OperationError {
    pub code: String,
    pub retryable: bool,
    #[serde(default)]
    pub field_path: Option<String>,
    #[serde(default)]
    pub retry_after_seconds: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ResultStatus {
    Ok,
    Accepted,
    Error,
    Uncertain,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OperationResult {
    pub request_id: String,
    pub operation: String,
    pub status: ResultStatus,
    #[serde(deserialize_with = "required_option")]
    pub actor: Option<String>,
    #[serde(deserialize_with = "required_option")]
    pub subject: Option<String>,
    #[serde(default)]
    pub resources: Vec<ResourceRef>,
    #[serde(default)]
    pub committed_at: Option<Timestamp>,
    #[serde(default)]
    pub replayed: bool,
    #[serde(default)]
    pub receipt: Option<Signature>,
    #[serde(default)]
    pub error: Option<OperationError>,
    #[serde(default)]
    pub data: Option<BTreeMap<String, Json>>,
    #[serde(default)]
    pub output: Option<ResourceRef>,
    #[serde(default)]
    pub prefer_cli: bool,
    #[serde(default)]
    pub cli_url: Option<String>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Event {
    pub id: String,
    #[serde(rename = "type")]
    pub event_type: String,
    pub time: Timestamp,
    pub request_id: String,
    #[serde(deserialize_with = "required_option")]
    pub actor: Option<String>,
    #[serde(deserialize_with = "required_option")]
    pub subject: Option<String>,
    pub resources: Vec<ResourceRef>,
    pub data: BTreeMap<String, Json>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AuditEvent {
    pub event: Event,
    pub authority: Vec<ResourceRef>,
    #[serde(deserialize_with = "required_option")]
    pub before_digest: Option<String>,
    #[serde(deserialize_with = "required_option")]
    pub after_digest: Option<String>,
    #[serde(deserialize_with = "required_option")]
    pub previous_digest: Option<String>,
    pub entry_digest: String,
    pub result: String,
}
