//! Typed Python wire records. Credentials and signatures deliberately lack Debug.
use chrono::{DateTime, Datelike, Timelike, Utc};
use msg_core::{b64, unb64, Error, Json, Result};
use msg_crypto::Signature;
use serde::{de::DeserializeOwned, Deserialize, Deserializer, Serialize, Serializer};
use std::collections::{BTreeMap, BTreeSet};

pub fn decode<T: DeserializeOwned>(value: &Json) -> Result<T> {
    serde_json::from_str(&value.canonical()?).map_err(|_| Error("invalid_identity_record"))
}
pub fn wire<T: Serialize>(value: &T) -> Result<Json> {
    Json::parse(&serde_json::to_string(value).map_err(|_| Error("invalid_identity_record"))?)
}

// Serde normally makes every Option field optional. Python requires fields
// without a dataclass default, even when their explicit value may be null.
fn required_nullable<'de, D, T>(d: D) -> std::result::Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(d)
}
fn positive_u32<'de, D: Deserializer<'de>>(d: D) -> std::result::Result<u32, D::Error> {
    std::num::NonZeroU32::deserialize(d).map(std::num::NonZeroU32::get)
}
fn positive_u64<'de, D: Deserializer<'de>>(d: D) -> std::result::Result<u64, D::Error> {
    std::num::NonZeroU64::deserialize(d).map(std::num::NonZeroU64::get)
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct Timestamp(pub DateTime<Utc>);
impl Timestamp {
    pub fn parse(value: &str) -> Result<Self> {
        if !value.ends_with('Z') {
            return Err(Error("invalid_datetime"));
        }
        let time = DateTime::parse_from_rfc3339(value)
            .map_err(|_| Error("invalid_datetime"))?
            .with_timezone(&Utc);
        if !(1..=9999).contains(&time.year()) || time.nanosecond() >= 1_000_000_000 {
            return Err(Error("invalid_datetime"));
        }
        // Python truncates sub-microsecond digits, never rounds them.
        Ok(Self(
            time.with_nanosecond(time.nanosecond() / 1000 * 1000)
                .ok_or(Error("invalid_datetime"))?,
        ))
    }
}
impl Serialize for Timestamp {
    fn serialize<S: Serializer>(&self, s: S) -> std::result::Result<S::Ok, S::Error> {
        s.serialize_str(&self.0.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string())
    }
}
impl<'de> Deserialize<'de> for Timestamp {
    fn deserialize<D: Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        Self::parse(&String::deserialize(d)?).map_err(serde::de::Error::custom)
    }
}

#[derive(Clone)]
pub struct Verifier(pub Vec<u8>);
impl Serialize for Verifier {
    fn serialize<S: Serializer>(&self, s: S) -> std::result::Result<S::Ok, S::Error> {
        s.serialize_str(&b64(&self.0))
    }
}
impl<'de> Deserialize<'de> for Verifier {
    fn deserialize<D: Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        unb64(&String::deserialize(d)?, 4096)
            .map(Self)
            .map_err(serde::de::Error::custom)
    }
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResourceRef {
    pub id: String,
    #[serde(default)]
    pub revision: Option<String>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Scope {
    pub resource_id: String,
    #[serde(default)]
    pub descendants: bool,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Grant {
    pub capability: String,
    #[serde(deserialize_with = "positive_u32")]
    pub version: u32,
    pub scope: Scope,
    pub operations: BTreeSet<String>,
    pub constraints: BTreeMap<String, Json>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IssuancePolicy {
    pub issue_grants: Vec<Grant>,
    #[serde(deserialize_with = "positive_u64")]
    pub max_cert_ttl_seconds: u64,
    pub max_child_ca_depth: u32,
    pub max_delegation_depth: u32,
}
#[derive(Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SubjectKind {
    Temporary,
    Registered,
    Custodial,
    System,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Subject {
    pub resource_id: String,
    pub kind: SubjectKind,
    pub primary_group: String,
    pub auth_version: u64,
    #[serde(default)]
    pub local_only: bool,
}
#[derive(Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CredentialKind {
    SigningKey,
    SshKey,
    Token,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Credential {
    pub id: String,
    pub subject_id: String,
    pub kind: CredentialKind,
    pub verifier: Verifier,
    pub ceiling: Vec<Grant>,
    pub not_before: Timestamp,
    #[serde(deserialize_with = "required_nullable")]
    pub expires_at: Option<Timestamp>,
    #[serde(deserialize_with = "required_nullable")]
    pub revoked_at: Option<Timestamp>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_credential_id: Option<String>,
}
impl Credential {
    pub fn current_at(&self, now: Timestamp) -> bool {
        self.revoked_at.is_none()
            && self.not_before <= now
            && self.expires_at.is_none_or(|expires| now < expires)
    }
}
#[derive(Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CertificateKind {
    Identity,
    Delegation,
    Capability,
    Ca,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Certificate {
    pub resource_id: String,
    pub serial: String,
    pub subject_id: String,
    pub key_id: String,
    pub issuer_id: String,
    #[serde(deserialize_with = "required_nullable")]
    pub parent_certificate_id: Option<String>,
    pub authority_sources: Vec<ResourceRef>,
    pub kind: CertificateKind,
    pub grants: Vec<Grant>,
    pub not_before: Timestamp,
    pub expires_at: Timestamp,
    pub target_service: String,
    pub delegation_depth: u32,
    #[serde(deserialize_with = "required_nullable")]
    pub issuance: Option<IssuancePolicy>,
    pub signature: Signature,
}
impl Certificate {
    pub fn signing_bytes(&self) -> Result<Vec<u8>> {
        let mut fields = wire(self)?.into_object()?;
        fields.remove("signature");
        Ok(Json::from_object(fields).canonical()?.into_bytes())
    }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CertificateRequest {
    pub resource_id: String,
    pub applicant: String,
    pub subject_id: String,
    pub requested_issuer: String,
    pub public_key: Verifier,
    pub kind: CertificateKind,
    pub grants: Vec<Grant>,
    #[serde(deserialize_with = "required_nullable")]
    pub issuance: Option<IssuancePolicy>,
    #[serde(deserialize_with = "positive_u64")]
    pub requested_ttl_seconds: u64,
    pub target_service: String,
    pub delegation_depth: u32,
    pub authority_sources: Vec<ResourceRef>,
    pub request_digest: String,
    pub possession_proof: Signature,
}
impl CertificateRequest {
    pub fn signing_bytes(&self) -> Result<Vec<u8>> {
        let mut fields = wire(self)?.into_object()?;
        for key in ["resource_id", "request_digest", "possession_proof"] {
            fields.remove(key);
        }
        Ok(Json::from_object(fields).canonical()?.into_bytes())
    }
}

/// Only authority-relevant fields of a resource are exposed by the store.
/// The original body remains private to storage; it is not a public read response.
#[derive(Clone, Deserialize)]
pub struct ResourceAuthority {
    pub id: String,
    #[serde(rename = "type")]
    pub kind: String,
    #[serde(deserialize_with = "required_nullable")]
    pub parent: Option<String>,
    pub owner: String,
    pub group: String,
    pub mode: String,
    pub state: String,
    #[serde(deserialize_with = "required_nullable")]
    pub revision: Option<String>,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DelegationFact {
    pub grantor: String,
    pub grantee: String,
    pub grants: Vec<Grant>,
    pub expires_at: Timestamp,
    #[serde(deserialize_with = "required_nullable")]
    pub parent_certificate: Option<String>,
    #[serde(default)]
    pub grantor_credential_id: Option<String>,
}
#[derive(Clone, Deserialize)]
pub struct DelegatedIdentity {
    pub grantor: String,
    pub certificate_id: String,
    pub max_uses: Option<u64>,
    pub uses: u64,
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn required_null_is_not_a_missing_field() {
        let value = Json::parse(r#"{"id":"k","subject_id":"u","kind":"token","verifier":"","ceiling":[],"not_before":"2026-01-01T00:00:00Z","expires_at":null,"revoked_at":null}"#).unwrap();
        assert!(decode::<Credential>(&value).is_ok());
        for field in ["expires_at", "revoked_at"] {
            let mut fields = value.clone().into_object().unwrap();
            fields.remove(field);
            assert!(decode::<Credential>(&Json::from_object(fields)).is_err());
        }
    }
    #[test]
    fn grant_versions_and_issuance_ttls_are_positive() {
        let value = Json::parse(r#"{"capability":"x","version":0,"scope":{"resource_id":"r"},"operations":[],"constraints":{}}"#).unwrap();
        assert!(decode::<Grant>(&value).is_err());
        let value = Json::parse(r#"{"issue_grants":[],"max_cert_ttl_seconds":0,"max_child_ca_depth":0,"max_delegation_depth":0}"#).unwrap();
        assert!(decode::<IssuancePolicy>(&value).is_err());
    }
    #[test]
    fn json_records_keep_large_integers_and_reject_duplicate_members() {
        let value =
            Json::parse(r#"{"value":1234567890123456789012345678901234567890,"float":1.0}"#)
                .unwrap();
        let decoded: BTreeMap<String, Json> = decode(&value).unwrap();
        assert_eq!(
            wire(&decoded).unwrap().canonical().unwrap(),
            value.canonical().unwrap()
        );
        assert!(serde_json::from_str::<Json>(r#"{"value":1,"value":2}"#).is_err());
    }
}
