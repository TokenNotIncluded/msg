//! Bounded AES-256-GCM key storage. Callers supply a domain-separated AAD.
use aes_gcm::{aead::AeadInPlace, Aes256Gcm, KeyInit, Nonce};
use msg_core::{b64, unb64, Error, Result};
use rand_core::{OsRng, RngCore};
use zeroize::Zeroizing;

#[derive(Clone)]
pub struct SealedSecret {
    pub nonce: String,
    pub ciphertext: String,
}

pub fn seal(key: &[u8; 32], aad: &[u8], secret: &[u8]) -> Result<SealedSecret> {
    if secret.len() > 8176 || aad.len() > 8192 {
        return Err(Error("invalid_vault_secret"));
    }
    let mut nonce = [0u8; 12];
    OsRng
        .try_fill_bytes(&mut nonce)
        .map_err(|_| Error("randomness_unavailable"))?;
    let mut ciphertext = Zeroizing::new(secret.to_vec());
    Aes256Gcm::new_from_slice(key)
        .map_err(|_| Error("invalid_vault_secret"))?
        .encrypt_in_place(&Nonce::from(nonce), aad, &mut *ciphertext)
        .map_err(|_| Error("invalid_vault_secret"))?;
    Ok(SealedSecret {
        nonce: b64(&nonce),
        ciphertext: b64(&ciphertext),
    })
}

pub fn open(key: &[u8; 32], aad: &[u8], sealed: &SealedSecret) -> Result<Zeroizing<Vec<u8>>> {
    let invalid = Error("invalid_vault_secret");
    let nonce: [u8; 12] = unb64(&sealed.nonce, 12)
        .map_err(|_| invalid)?
        .try_into()
        .map_err(|_| invalid)?;
    let mut secret = Zeroizing::new(unb64(&sealed.ciphertext, 8192).map_err(|_| invalid)?);
    if secret.len() < 16 || aad.len() > 8192 {
        return Err(invalid);
    }
    Aes256Gcm::new_from_slice(key)
        .map_err(|_| invalid)?
        .decrypt_in_place(&Nonce::from(nonce), aad, &mut *secret)
        .map_err(|_| invalid)?;
    Ok(secret)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn domain_and_key_are_authenticated() {
        let sealed = seal(&[1; 32], b"subject/key/identity", &[2; 32]).unwrap();
        assert_eq!(
            &*open(&[1; 32], b"subject/key/identity", &sealed).unwrap(),
            &[2; 32]
        );
        assert!(open(&[1; 32], b"subject/key/encryption", &sealed).is_err());
        assert!(open(&[3; 32], b"subject/key/identity", &sealed).is_err());
    }
}
