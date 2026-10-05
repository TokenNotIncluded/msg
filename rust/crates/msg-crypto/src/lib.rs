//! Native signature primitives, not authentication or certificate authorization.
use ed25519_dalek::{Signer as _, SigningKey, VerifyingKey};
use msg_core::{b64, unb64, Error, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Signature {
    pub key_id: String,
    pub algorithm: String,
    pub value: String,
}

pub fn key_id(public: &[u8]) -> String {
    format!("k_{:x}", Sha256::digest(public))
}
pub fn subject_id(public: &[u8]) -> String {
    let mut digest = Sha256::new();
    digest.update(b"msg-subject-v1\0");
    digest.update(public);
    format!("u_{}", &format!("{:x}", digest.finalize())[..32])
}
pub fn framed(payload: &[u8], purpose: &str) -> Result<Vec<u8>> {
    if purpose.is_empty() || !purpose.is_ascii() || purpose.contains('\0') {
        return Err(Error("invalid_signature_purpose"));
    }
    let mut message = Vec::with_capacity(17 + purpose.len() + payload.len());
    message.extend_from_slice(b"msg.lmm.best/v1/");
    message.extend_from_slice(purpose.as_bytes());
    message.push(0);
    message.extend_from_slice(payload);
    Ok(message)
}

/// Intentionally no Debug/Serialize/Clone or private-key export.
/// ed25519-dalek's default zeroize feature clears the owned key on drop.
pub struct Ed25519Signer(SigningKey);
impl Ed25519Signer {
    pub fn from_seed(seed: &[u8; 32]) -> Self {
        Self(SigningKey::from_bytes(seed))
    }
    pub fn public_key(&self) -> [u8; 32] {
        self.0.verifying_key().to_bytes()
    }
    pub fn sign(&self, payload: &[u8], purpose: &str) -> Result<Signature> {
        Ok(Signature {
            key_id: key_id(&self.public_key()),
            algorithm: "ed25519".to_owned(),
            value: b64(&self.0.sign(&framed(payload, purpose)?).to_bytes()),
        })
    }
}

/// Verifies bytes only. Callers must separately enforce credential state,
/// subject binding, expiry, service origin, replay rules and CA authorization.
pub fn verify(public: &[u8], payload: &[u8], signature: &Signature, purpose: &str) -> Result<()> {
    let invalid = |_| Error("invalid_signature");
    if signature.algorithm != "ed25519" || signature.key_id != key_id(public) {
        return Err(Error("invalid_signature"));
    }
    let public: &[u8; 32] = public.try_into().map_err(invalid)?;
    let key = VerifyingKey::from_bytes(public).map_err(|_| Error("invalid_signature"))?;
    let bytes = unb64(&signature.value, 64).map_err(|_| Error("invalid_signature"))?;
    let signature = ed25519_dalek::Signature::from_slice(&bytes).map_err(|_| Error("invalid_signature"))?;
    key.verify_strict(&framed(payload, purpose)?, &signature).map_err(|_| Error("invalid_signature"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn domains_keys_and_message_are_bound() {
        let signer = Ed25519Signer::from_seed(&[42; 32]);
        let signature = signer.sign(b"hello", "request").unwrap();
        assert!(verify(&signer.public_key(), b"hello", &signature, "request").is_ok());
        assert!(verify(&signer.public_key(), b"hello", &signature, "receipt").is_err());
        assert!(verify(&signer.public_key(), b"other", &signature, "request").is_err());
        assert!(verify(&[0; 32], b"hello", &signature, "request").is_err());
        assert!(framed(b"x", "").is_err());
        assert!(framed(b"x", "bad\0purpose").is_err());
        assert!(framed(b"x", "中文").is_err());
    }

    #[test]
    fn rejects_weak_key_identity_forgery() {
        let mut identity = [0; 32];
        identity[0] = 1;
        let mut forged = [0; 64];
        forged[0] = 1;
        let signature = Signature { key_id: key_id(&identity), algorithm: "ed25519".to_owned(), value: b64(&forged) };
        assert!(verify(&identity, b"forgery", &signature, "request").is_err());
    }
}
