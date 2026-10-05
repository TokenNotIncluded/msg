//! JSONL conformance driver. Not shipped as msg/msgd; no network or storage.
//! The deterministic signer is public test material, never a service identity.
use std::io::{self, BufRead, Read, Write};

use msg_core::{b64, digest, unb64, Error, Json, MAX_BYTES};
use msg_crypto::{key_id, subject_id, Ed25519Signer, Signature};
use msg_protocol::Request;
use serde_json::{json, Value};

fn text<'a>(value: &'a Value, key: &str) -> Result<&'a str, Error> {
    value.get(key).and_then(Value::as_str).ok_or(Error("invalid_test_input"))
}
fn run(value: Value) -> Result<Value, Error> {
    match text(&value, "action")? {
        "canonical" => {
            let canonical = Json::parse(text(&value, "raw")?)?.canonical()?;
            Ok(json!({"canonical":canonical, "digest":digest(canonical.as_bytes())}))
        }
        "base64" => {
            let bytes = unb64(text(&value, "raw")?, MAX_BYTES)?;
            Ok(json!({"encoded":b64(&bytes), "digest":digest(&bytes)}))
        }
        "request" => {
            let request = Request::parse(text(&value, "raw")?)?;
            let verified = match value.get("public").and_then(Value::as_str) {
                Some(public) => Some(request.verify_signature_bytes(&unb64(public, 32)?).is_ok()),
                None => None,
            };
            Ok(json!({"canonical":request.canonical()?, "payload_digest":request.payload_digest()?, "signing":b64(&request.signing_bytes()?), "digest_valid":request.verify_payload_digest().is_ok(), "verified":verified}))
        }
        "sign" => {
            let signer = Ed25519Signer::from_seed(&std::array::from_fn(|i| i as u8));
            let payload = unb64(text(&value, "payload")?, MAX_BYTES)?;
            let signature = signer.sign(&payload, text(&value, "purpose")?)?;
            Ok(json!({"public":b64(&signer.public_key()), "key_id":key_id(&signer.public_key()), "subject_id":subject_id(&signer.public_key()), "signature":signature}))
        }
        "verify" => {
            let signature: Signature = serde_json::from_value(value.get("signature").cloned().ok_or(Error("invalid_test_input"))?).map_err(|_| Error("invalid_test_input"))?;
            msg_crypto::verify(&unb64(text(&value, "public")?, 32)?, &unb64(text(&value, "payload")?, MAX_BYTES)?, &signature, text(&value, "purpose")?)?;
            Ok(json!({"verified":true}))
        }
        _ => Err(Error("unknown_test_action")),
    }
}
fn main() -> io::Result<()> {
    let stdin = io::stdin();
    let mut reader = stdin.lock();
    let mut stdout = io::BufWriter::new(io::stdout().lock());
    loop {
        let mut raw = String::new();
        // Bound the test transport too, before allocating an unbounded line.
        let read = (&mut reader).take((MAX_BYTES * 8 + 1) as u64).read_line(&mut raw)?;
        if read == 0 { break; }
        if raw.len() > MAX_BYTES * 8 { return Err(io::Error::other("test_input_too_large")); }
        let result = serde_json::from_str(&raw).map_err(|_| Error("invalid_test_input")).and_then(run);
        let output = match result {
            Ok(data) => json!({"ok":true, "data":data}),
            Err(error) => json!({"ok":false, "code":error.0}),
        };
        writeln!(stdout, "{output}")?;
        stdout.flush()?;
    }
    Ok(())
}
