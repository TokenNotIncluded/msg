//! The existing `msg-x25519-v1` envelope: X25519, HKDF-SHA256 and AES-256-GCM.
//! Encryption keys are independent of signing keys. This module performs no I/O.
use std::collections::BTreeMap;

use aes_gcm::{aead::AeadInPlace, Aes256Gcm, KeyInit, Nonce};
use hkdf::Hkdf;
use msg_core::{b64, unb64, Error, Json, Result};
use rand_core::{OsRng, RngCore};
use sha2::Sha256;
use x25519_dalek::{PublicKey, SharedSecret, StaticSecret};
use zeroize::Zeroizing;

pub const FORMAT: &str = "msg-x25519-v1";
/// Matches the Python client's small-file encryption limit.
pub const MAX_PLAINTEXT_BYTES: usize = 512 * 1024;
/// The historical download limit; decryption does not narrow it to 512 KiB.
pub const MAX_ENVELOPE_BYTES: usize = 1024 * 1024;

fn random_bytes<const N: usize>() -> Result<Zeroizing<[u8; N]>> {
    let mut bytes = Zeroizing::new([0; N]);
    OsRng
        .try_fill_bytes(&mut bytes[..])
        .map_err(|_| Error("random_source_unavailable"))?;
    Ok(bytes)
}

/// Generate an independent encryption key. The caller must keep the private
/// bytes local and persist them securely; the returned buffer clears on drop.
pub fn generate_key() -> Result<(Zeroizing<[u8; 32]>, [u8; 32])> {
    let private = random_bytes()?;
    let public = PublicKey::from(&StaticSecret::from(*private)).to_bytes();
    Ok((private, public))
}

fn derive(
    shared: &SharedSecret,
    salt: &[u8; 32],
    recipient: &[u8; 32],
    ephemeral: &[u8; 32],
) -> Result<Zeroizing<[u8; 32]>> {
    let mut context = b"msg-keystore-v1\0".to_vec();
    context.extend_from_slice(recipient);
    context.extend_from_slice(ephemeral);
    let mut key = Zeroizing::new([0; 32]);
    Hkdf::<Sha256>::new(Some(salt), shared.as_bytes())
        .expand(&context, &mut key[..])
        .map_err(|_| Error("encryption_failed"))?;
    Ok(key)
}

/// Encrypt using fresh OS randomness for each ephemeral key, salt and nonce.
pub fn encrypt(plaintext: &[u8], recipient_public: &[u8]) -> Result<Vec<u8>> {
    if plaintext.len() > MAX_PLAINTEXT_BYTES {
        return Err(Error("use_streaming_age_or_openpgp"));
    }
    let recipient: [u8; 32] = recipient_public
        .try_into()
        .map_err(|_| Error("invalid_encryption_input"))?;
    let ephemeral = StaticSecret::from(*random_bytes::<32>()?);
    let public = PublicKey::from(&ephemeral).to_bytes();
    let shared = ephemeral.diffie_hellman(&PublicKey::from(recipient));
    if !shared.was_contributory() {
        return Err(Error("invalid_encryption_recipient"));
    }
    let salt = random_bytes::<32>()?;
    let nonce = random_bytes::<12>()?;
    let key = derive(&shared, &salt, &recipient, &public)?;
    let mut header = BTreeMap::from([
        ("format".to_owned(), Json::string(FORMAT)),
        ("recipient".to_owned(), Json::string(b64(&recipient))),
        ("ephemeral".to_owned(), Json::string(b64(&public))),
        ("salt".to_owned(), Json::string(b64(&salt[..]))),
        ("nonce".to_owned(), Json::string(b64(&nonce[..]))),
    ]);
    let aad = Json::from_object(header.clone()).canonical()?;
    let mut ciphertext = Zeroizing::new(plaintext.to_vec());
    Aes256Gcm::new_from_slice(&key[..])
        .map_err(|_| Error("encryption_failed"))?
        .encrypt_in_place(&Nonce::from(*nonce), aad.as_bytes(), &mut *ciphertext)
        .map_err(|_| Error("encryption_failed"))?;
    header.insert("ciphertext".to_owned(), Json::string(b64(&ciphertext)));
    Ok(Json::from_object(header).canonical()?.into_bytes())
}

struct Envelope {
    recipient: [u8; 32],
    ephemeral: [u8; 32],
    salt: [u8; 32],
    nonce: [u8; 12],
    ciphertext: Vec<u8>,
    aad: Vec<u8>,
}

fn bytes(value: &Json, limit: usize) -> Result<Vec<u8>> {
    unb64(value.as_str().map_err(|_| Error("invalid_base64"))?, limit)
}

fn fixed<const N: usize>(value: &Json) -> Result<[u8; N]> {
    bytes(value, N)?
        .try_into()
        .map_err(|_| Error("invalid_ciphertext_envelope"))
}

fn parse(content: &[u8]) -> Result<Envelope> {
    if content.len() > MAX_ENVELOPE_BYTES {
        return Err(Error("ciphertext_envelope_too_large"));
    }
    let text = std::str::from_utf8(content).map_err(|_| Error("invalid_json"))?;
    let mut fields = Json::parse(text)?
        .into_object()
        .map_err(|_| Error("invalid_ciphertext_envelope"))?;
    let names = [
        "format",
        "recipient",
        "ephemeral",
        "salt",
        "nonce",
        "ciphertext",
    ];
    if fields.len() != names.len() || names.iter().any(|name| !fields.contains_key(*name)) {
        return Err(Error("invalid_ciphertext_envelope"));
    }
    if fields["format"].as_str().ok() != Some(FORMAT) {
        return Err(Error("invalid_ciphertext_format"));
    }
    let recipient = fixed(&fields["recipient"])?;
    let ephemeral = fixed(&fields["ephemeral"])?;
    let salt = fixed(&fields["salt"])?;
    let nonce = fixed(&fields["nonce"])?;
    let ciphertext = bytes(&fields["ciphertext"], MAX_ENVELOPE_BYTES)?;
    if ciphertext.len() < 16 {
        return Err(Error("invalid_ciphertext_envelope"));
    }
    fields.remove("ciphertext");
    Ok(Envelope {
        recipient,
        ephemeral,
        salt,
        nonce,
        ciphertext,
        aad: Json::from_object(fields).canonical()?.into_bytes(),
    })
}

/// Check bounded envelope shape only. This does not authenticate ciphertext or
/// grant access to a resource; only the recipient can decrypt its contents.
pub fn validate_envelope(content: &[u8]) -> Result<()> {
    parse(content).map(|_| ())
}

/// Authenticate the entire envelope before returning plaintext. Owned secret
/// buffers clear on drop; callers remain responsible for their input buffers.
pub fn decrypt(content: &[u8], private: &[u8]) -> Result<Zeroizing<Vec<u8>>> {
    let envelope = parse(content)?;
    let secret = StaticSecret::from(
        <[u8; 32]>::try_from(private).map_err(|_| Error("invalid_encryption_key"))?,
    );
    let recipient = PublicKey::from(&secret).to_bytes();
    if recipient != envelope.recipient {
        return Err(Error("wrong_decryption_recipient"));
    }
    let shared = secret.diffie_hellman(&PublicKey::from(envelope.ephemeral));
    if !shared.was_contributory() {
        return Err(Error("ciphertext_authentication_failed"));
    }
    let key = derive(&shared, &envelope.salt, &recipient, &envelope.ephemeral)?;
    let mut plaintext = Zeroizing::new(envelope.ciphertext);
    Aes256Gcm::new_from_slice(&key[..])
        .map_err(|_| Error("ciphertext_authentication_failed"))?
        .decrypt_in_place(&Nonce::from(envelope.nonce), &envelope.aad, &mut *plaintext)
        .map_err(|_| Error("ciphertext_authentication_failed"))?;
    Ok(plaintext)
}

#[cfg(test)]
mod tests {
    use super::*;

    // Generated independently with Python cryptography 50.0.2, using recipient
    // private [7;32], ephemeral private [42;32], salt [3;32] and nonce [4;12].
    const PYTHON_ENVELOPE: &str = r#"{"ciphertext":"8QQLJE6wLzgixfIFhEhzo6WupV7befWC_EoMxgPAwx5TIQ","ephemeral":"B6r_Pp_BZydVRPTDpqF82Dfy7G54zYpXsePfs8wDWnY","format":"msg-x25519-v1","nonce":"BAQEBAQEBAQEBAQE","recipient":"E75P6uryBMf9M1j8nAByGIHRdCeBKCJ-xnTzf3_pe20","salt":"AwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwM"}"#;

    fn fixture() -> BTreeMap<String, Json> {
        Json::parse(PYTHON_ENVELOPE).unwrap().into_object().unwrap()
    }

    fn encode(fields: BTreeMap<String, Json>) -> Vec<u8> {
        Json::from_object(fields).canonical().unwrap().into_bytes()
    }

    #[test]
    fn decrypts_python_vector_and_noncanonical_json() {
        let expected = b"Rust\0Python\xff\xe8\x87\xaa\xe7\x94\xb1";
        assert_eq!(
            &*decrypt(PYTHON_ENVELOPE.as_bytes(), &[7; 32]).unwrap(),
            expected
        );
        // Whitespace and member order are not part of the authenticated header.
        let members = fixture()
            .iter()
            .rev()
            .map(|(name, value)| format!("  \"{name}\": {}", value.canonical().unwrap()))
            .collect::<Vec<_>>()
            .join(",\n");
        let reordered = format!("{{\n{members}\n}}");
        assert_eq!(&*decrypt(reordered.as_bytes(), &[7; 32]).unwrap(), expected);
    }

    #[test]
    fn random_round_trips_include_empty_binary_and_size_limit() {
        let (private, public) = generate_key().unwrap();
        let (other_private, other_public) = generate_key().unwrap();
        assert_ne!(&*private, &*other_private);
        assert_ne!(public, other_public);
        for plaintext in [
            vec![],
            b"\0\xff\n\xe4\xb8\xad\xe6\x96\x87".to_vec(),
            vec![42; MAX_PLAINTEXT_BYTES],
        ] {
            let sealed = encrypt(&plaintext, &public).unwrap();
            validate_envelope(&sealed).unwrap();
            assert!(sealed.len() <= MAX_ENVELOPE_BYTES);
            assert_eq!(&*decrypt(&sealed, &private[..]).unwrap(), &plaintext);
        }
        let first = parse(&encrypt(b"same", &public).unwrap()).unwrap();
        let second = parse(&encrypt(b"same", &public).unwrap()).unwrap();
        assert_ne!(first.ephemeral, second.ephemeral);
        assert_ne!(first.salt, second.salt);
        assert_ne!(first.nonce, second.nonce);
    }

    #[test]
    fn authenticates_recipient_and_every_header_field() {
        for name in ["recipient", "ephemeral", "salt", "nonce", "ciphertext"] {
            let mut fields = fixture();
            let mut value = bytes(&fields[name], MAX_ENVELOPE_BYTES).unwrap();
            value[0] ^= 1;
            fields.insert(name.to_owned(), Json::string(b64(&value)));
            let result = decrypt(&encode(fields), &[7; 32]);
            assert_eq!(
                result.unwrap_err(),
                Error(if name == "recipient" {
                    "wrong_decryption_recipient"
                } else {
                    "ciphertext_authentication_failed"
                })
            );
        }
        assert_eq!(
            decrypt(PYTHON_ENVELOPE.as_bytes(), &[9; 32]).unwrap_err(),
            Error("wrong_decryption_recipient")
        );
    }

    #[test]
    fn rejects_low_order_keys_in_both_directions() {
        let mut identity = [0; 32];
        identity[0] = 1;
        let mut modulus = [255; 32];
        modulus[0] = 237;
        modulus[31] = 127;
        for key in [[0; 32], identity, modulus] {
            assert_eq!(
                encrypt(b"secret", &key).unwrap_err(),
                Error("invalid_encryption_recipient")
            );
            let mut fields = fixture();
            fields.insert("ephemeral".to_owned(), Json::string(b64(&key)));
            assert_eq!(
                decrypt(&encode(fields), &[7; 32]).unwrap_err(),
                Error("ciphertext_authentication_failed")
            );
        }
    }

    #[test]
    fn rejects_ambiguous_malformed_and_oversized_envelopes() {
        for raw in ["[]", "null", "{}", "{\"format\":0,\"format\":1}"] {
            assert!(validate_envelope(raw.as_bytes()).is_err());
        }
        let duplicate = PYTHON_ENVELOPE.replacen('{', "{\"\\u0066ormat\":\"msg-x25519-v1\",", 1);
        assert_eq!(
            validate_envelope(duplicate.as_bytes()).unwrap_err(),
            Error("duplicate_json_key")
        );
        for (name, value) in [
            ("extra", Json::string("ignored")),
            ("format", Json::string("other-v1")),
            ("recipient", Json::empty_array()),
            ("ephemeral", Json::string("invalid=")),
            ("salt", Json::string(b64(&[0; 31]))),
            ("nonce", Json::string(b64(&[0; 13]))),
            ("ciphertext", Json::string(b64(&[0; 15]))),
        ] {
            let mut fields = fixture();
            fields.insert(name.to_owned(), value);
            assert!(validate_envelope(&encode(fields)).is_err());
        }
        let mut missing = fixture();
        missing.remove("nonce");
        assert!(validate_envelope(&encode(missing)).is_err());
        assert_eq!(
            encrypt(&vec![0; MAX_PLAINTEXT_BYTES + 1], &[1; 32]).unwrap_err(),
            Error("use_streaming_age_or_openpgp")
        );
        assert_eq!(
            validate_envelope(&vec![b' '; MAX_ENVELOPE_BYTES + 1]).unwrap_err(),
            Error("ciphertext_envelope_too_large")
        );
        assert_eq!(
            decrypt(PYTHON_ENVELOPE.as_bytes(), &[7; 31]).unwrap_err(),
            Error("invalid_encryption_key")
        );
        assert!(validate_envelope(&[255]).is_err());
    }
}
