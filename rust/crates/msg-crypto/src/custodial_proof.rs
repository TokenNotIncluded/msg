//! Existing X25519/HKDF possession proof for a custody exit challenge.
use hkdf::Hkdf;
use hmac::{Hmac, Mac};
use msg_core::{b64, Error, Result};
use sha2::Sha256;
use x25519_dalek::{PublicKey, StaticSecret};
use zeroize::Zeroizing;

pub fn public(private: &[u8; 32]) -> [u8; 32] {
    PublicKey::from(&StaticSecret::from(*private)).to_bytes()
}

pub fn proof(
    private: &[u8; 32],
    public: &[u8; 32],
    nonce: &[u8],
    context: &[u8],
) -> Result<String> {
    let shared = StaticSecret::from(*private).diffie_hellman(&PublicKey::from(*public));
    if !shared.was_contributory() || nonce.is_empty() || nonce.len() > 64 {
        return Err(Error("invalid_custodial_challenge"));
    }
    let mut derived = Zeroizing::new([0u8; 32]);
    Hkdf::<Sha256>::new(Some(nonce), shared.as_bytes())
        .expand(b"msg-custodial-upgrade-pop-v1", derived.as_mut())
        .map_err(|_| Error("invalid_custodial_challenge"))?;
    let mut mac = Hmac::<Sha256>::new_from_slice(derived.as_ref())
        .map_err(|_| Error("invalid_custodial_challenge"))?;
    mac.update(context);
    Ok(b64(&mac.finalize().into_bytes()))
}
