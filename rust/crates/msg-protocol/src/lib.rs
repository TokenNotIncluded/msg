//! Transport-neutral envelope decoding and signed bytes, without an executor.
//! Verifying a signature here does not authorize any resource operation.
use std::collections::{BTreeMap, BTreeSet};

use chrono::{DateTime, Datelike, Timelike, Utc};
use msg_core::{digest, unb64, Error, Json, Result, MAX_BYTES};
use msg_crypto::Signature;

const REQUIRED: [&str; 11] = [
    "request_id", "protocol_version", "operation", "contract_version", "target_service",
    "subject", "arguments", "expected_generations", "expires_at", "payload_digest", "proof",
];

/// No Debug or public field access: proofs can contain bearer credentials.
pub struct Request {
    fields: BTreeMap<String, Json>,
}

fn check(condition: bool) -> Result<()> {
    if condition { Ok(()) } else { Err(Error("invalid_request_envelope")) }
}
fn string(value: &Json, min: usize, max: usize) -> Result<&str> {
    let text = value.as_str()?;
    check((min..=max).contains(&text.chars().count()))?;
    Ok(text)
}
fn nonnegative(value: &Json, positive: bool) -> Result<()> {
    let integer = value.as_integer()?;
    check(!integer.starts_with('-') && (!positive || integer != "0"))
}
fn exact_fields(fields: &BTreeMap<String, Json>, names: &[&str]) -> Result<()> {
    check(fields.len() == names.len() && names.iter().all(|name| fields.contains_key(*name)))
}
fn strings(value: &Json, max_items: usize, min_length: usize, max_length: usize) -> Result<()> {
    let items = value.as_array()?;
    check(items.len() <= max_items)?;
    for item in items { string(item, min_length, max_length)?; }
    Ok(())
}
fn signature_shape(value: &Json) -> Result<()> {
    let signature = value.as_object()?;
    exact_fields(signature, &["key_id", "algorithm", "value"])?;
    string(&signature["key_id"], 1, 160)?;
    check(signature["algorithm"].as_str()? == "ed25519")?;
    unb64(signature["value"].as_str()?, MAX_BYTES)?;
    Ok(())
}
fn proof_shape(value: &Json) -> Result<()> {
    if value.is_null() { return Ok(()); }
    let proof = value.as_object()?;
    if proof.contains_key("signature") {
        exact_fields(proof, &["signature", "certificates"])?;
        signature_shape(&proof["signature"])?;
        strings(&proof["certificates"], 16, 1, 160)
    } else {
        exact_fields(proof, &["credential_id", "token"])?;
        string(&proof["credential_id"], 1, 160)?;
        unb64(proof["token"].as_str()?, MAX_BYTES)?;
        Ok(())
    }
}

impl Request {
    pub fn parse(raw: &str) -> Result<Self> {
        let fields = Json::parse(raw)?.into_object()?;
        let result = Self::decode(fields);
        // Do not expose parser internals or rejected request/proof contents.
        result.map_err(|error| match error.0 {
            "invalid_type" => Error("invalid_request_envelope"),
            _ => error,
        })
    }

    fn decode(mut fields: BTreeMap<String, Json>) -> Result<Self> {
        check(REQUIRED.iter().all(|name| fields.contains_key(*name)))?;
        check(fields.keys().all(|name| REQUIRED.contains(&name.as_str()) || matches!(name.as_str(), "source" | "return_fields")))?;
        fields.entry("return_fields".to_owned()).or_insert_with(Json::empty_array);
        fields.entry("source".to_owned()).or_insert_with(|| Json::string("unknown"));
        string(&fields["request_id"], 1, 128)?;
        check(fields["protocol_version"].as_integer()? == "1")?;
        nonnegative(&fields["contract_version"], true)?;
        string(&fields["target_service"], 0, 2048)?;
        if !fields["subject"].is_null() { string(&fields["subject"], 0, 160)?; }
        fields["arguments"].as_object()?;
        let operation = string(&fields["operation"], 1, 128)?;
        check(operation.contains('.') && operation.split('.').all(|part| {
            let mut bytes = part.bytes();
            bytes.next().is_some_and(|first| first.is_ascii_lowercase())
                && bytes.all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_')
        }))?;
        let expected = fields["expected_generations"].as_array()?;
        check(expected.len() <= 64)?;
        let mut seen = BTreeSet::new();
        for item in expected {
            let pair = item.as_array()?;
            check(pair.len() == 2)?;
            let id = string(&pair[0], 1, 160)?;
            nonnegative(&pair[1], false)?;
            if !seen.insert(id) { return Err(Error("duplicate_expected_generation")); }
        }
        let payload_digest = fields["payload_digest"].as_str()?;
        check(payload_digest.len() == 71 && payload_digest.starts_with("sha256:") && payload_digest[7..].bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)))?;
        check(matches!(fields["source"].as_str()?, "msg" | "manual" | "mcp" | "unknown"))?;
        strings(&fields["return_fields"], 30, 0, MAX_BYTES)?;
        proof_shape(&fields["proof"])?;
        if !fields["expires_at"].is_null() {
            let text = string(&fields["expires_at"], 1, 40)?;
            check(text.ends_with('Z'))?;
            // The rollout profile accepts RFC3339 UTC. Python CLI emits this
            // form. Non-RFC3339 datetime.fromisoformat aliases remain gated.
            let time = DateTime::parse_from_rfc3339(text).map_err(|_| Error("invalid_datetime"))?;
            check((1..=9999).contains(&time.year()) && time.nanosecond() < 1_000_000_000)?;
            let normalized = time.with_timezone(&Utc).format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string();
            fields.insert("expires_at".to_owned(), Json::string(normalized));
        }
        let request = Self { fields };
        request.canonical()?;
        Ok(request)
    }

    pub fn canonical(&self) -> Result<String> {
        Json::from_object(self.fields.clone()).canonical()
    }
    pub fn payload_digest(&self) -> Result<String> {
        let mut payload = self.fields.clone();
        for field in ["proof", "payload_digest", "source", "expires_at"] { payload.remove(field); }
        Ok(digest(Json::from_object(payload).canonical()?.as_bytes()))
    }
    pub fn signing_bytes(&self) -> Result<Vec<u8>> {
        let mut signed = self.fields.clone();
        signed.remove("proof");
        Ok(Json::from_object(signed).canonical()?.into_bytes())
    }
    pub fn verify_payload_digest(&self) -> Result<()> {
        if self.fields["payload_digest"].as_str()? == self.payload_digest()? {
            Ok(())
        } else { Err(Error("payload_digest_mismatch")) }
    }
    pub fn verify_signature_bytes(&self, public: &[u8]) -> Result<()> {
        self.verify_payload_digest()?;
        let proof = self.fields["proof"].as_object().map_err(|_| Error("signature_required"))?;
        let raw = proof.get("signature").ok_or(Error("signature_required"))?.canonical()?;
        let signature: Signature = serde_json::from_str(&raw).map_err(|_| Error("invalid_signature"))?;
        msg_crypto::verify(public, &self.signing_bytes()?, &signature, "request")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn packet() -> serde_json::Value {
        serde_json::json!({
            "protocol_version":1, "request_id":"test-1", "operation":"discovery.get",
            "contract_version":1, "target_service":"https://msg.lmm.best", "subject":null,
            "arguments":{"id":"r_agents"}, "expected_generations":[],
            "expires_at":"2026-10-05T00:00:00Z", "payload_digest":format!("sha256:{}", "0".repeat(64)), "proof":null
        })
    }

    #[test]
    fn retry_digest_excludes_only_transport_metadata() {
        let mut value = packet();
        let first = Request::parse(&value.to_string()).unwrap();
        value["expires_at"] = "2026-10-05T00:02:00Z".into();
        value["source"] = "mcp".into();
        let refreshed = Request::parse(&value.to_string()).unwrap();
        assert_eq!(first.payload_digest().unwrap(), refreshed.payload_digest().unwrap());
        assert_ne!(first.signing_bytes().unwrap(), refreshed.signing_bytes().unwrap());
        value["request_id"] = "other".into();
        assert_ne!(first.payload_digest().unwrap(), Request::parse(&value.to_string()).unwrap().payload_digest().unwrap());
    }

    #[test]
    fn defaults_and_timestamp_match_python_record() {
        let request = Request::parse(&packet().to_string()).unwrap();
        let canonical = request.canonical().unwrap();
        assert!(canonical.contains("\"source\":\"unknown\""));
        assert!(canonical.contains("\"return_fields\":[]"));
        assert!(canonical.contains("2026-10-05T00:00:00.000000Z"));
    }

    #[test]
    fn rejects_unknown_fields_and_bool_integers() {
        for (key, bad) in [("extra", serde_json::json!(0)), ("protocol_version", serde_json::json!(true)), ("contract_version", serde_json::json!(1.0)), ("expected_generations", serde_json::json!([["r", true]])), ("expected_generations", serde_json::json!([["r", 0], ["r", 1]])), ("proof", serde_json::json!({"credential_id":"c", "token":"YQ=="}))] {
            let mut value = packet();
            value[key] = bad;
            assert!(Request::parse(&value.to_string()).is_err());
        }
    }
}
