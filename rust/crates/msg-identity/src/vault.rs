//! The existing custodial vault; self-custody accounts never enter this path.
//! The caller must authorize the current operation in the same transaction.
use crate::{models::*, mutations::IdentityKey, require, store::AuthorityStore};
use hmac::{Hmac, Mac};
use msg_core::{Error, Result};
use msg_crypto::{age_recipient, key_id, vault_box, Ed25519Signer};
use sha2::Sha256;
use zeroize::Zeroizing;

#[derive(Clone)]
pub struct VaultRecord {
    pub subject: String,
    pub signing_key_id: String,
    pub encryption_key_id: String,
    pub signing: Option<vault_box::SealedSecret>,
    pub encryption: Option<vault_box::SealedSecret>,
    pub status: String,
    pub created_at: Timestamp,
    pub destroyed_at: Option<Timestamp>,
}

pub trait VaultStore: AuthorityStore {
    fn vault_record(&self, subject: &str) -> Result<Option<VaultRecord>>;
    fn primary_identity_key(&self, subject: &str) -> Result<Option<IdentityKey>>;
}

fn key(token_secret: &[u8; 32]) -> Result<Zeroizing<[u8; 32]>> {
    let mut mac =
        Hmac::<Sha256>::new_from_slice(token_secret).map_err(|_| Error("invalid_service_key"))?;
    mac.update(b"custodial-vault-aesgcm-v1");
    Ok(Zeroizing::new(mac.finalize().into_bytes().into()))
}
fn aad(subject: &str, key_id: &str, kind: &str) -> Result<String> {
    wire(&serde_json::json!({"domain":"msg-custodial-vault-v1","subject":subject,"key_id":key_id,"kind":kind}))?.canonical()
}
fn read(store: &dyn VaultStore, subject: &str, encryption: bool) -> Result<VaultRecord> {
    require(
        !store.has_setting("recovery_quarantine")?,
        "recovery_quarantined",
    )?;
    let record = store
        .vault_record(subject)?
        .ok_or(Error("custodial_vault_unavailable"))?;
    require(record.subject == subject, "storage_record_mismatch")?;
    require(
        record.status == "active" || (encryption && record.status == "decrypt_only"),
        "custodial_vault_unavailable",
    )?;
    Ok(record)
}

/// Open only the live primary custodial signing key. No private-key export API
/// exists on the returned signer. A caller cannot use this to sign for a
/// self-custody account or resume a key after custody was retired.
pub fn open_signer(
    store: &dyn VaultStore,
    token_secret: &[u8; 32],
    subject: &str,
    now: Timestamp,
) -> Result<Ed25519Signer> {
    let owner = store.subject(subject)?;
    require(
        owner.kind == SubjectKind::Custodial && !owner.local_only,
        "not_custodial",
    )?;
    let record = read(store, subject, false)?;
    let primary = store
        .primary_identity_key(subject)?
        .ok_or(Error("identity_key_not_found"))?;
    require(
        primary.key_id == record.signing_key_id && primary.retired_at.is_none(),
        "custodial_signing_key_unavailable",
    )?;
    let credential = store.credential(&record.signing_key_id)?;
    require(
        credential.subject_id == subject
            && credential.kind == CredentialKind::SigningKey
            && credential.current_at(now),
        "custodial_signing_key_unavailable",
    )?;
    let sealed = record
        .signing
        .as_ref()
        .ok_or(Error("custodial_vault_unavailable"))?;
    let bytes = vault_box::open(
        &*key(token_secret)?,
        aad(subject, &record.signing_key_id, "identity")?.as_bytes(),
        sealed,
    )
    .map_err(|_| Error("custodial_vault_corrupt"))?;
    let seed: &[u8; 32] = bytes
        .as_slice()
        .try_into()
        .map_err(|_| Error("custodial_vault_corrupt"))?;
    let signer = Ed25519Signer::from_seed(seed);
    require(
        key_id(&signer.public_key()) == record.signing_key_id
            && signer.public_key().as_slice() == primary.public_key.0.as_slice()
            && credential.verifier.0.as_slice() == primary.public_key.0.as_slice(),
        "custodial_vault_corrupt",
    )?;
    Ok(signer)
}

pub fn open_age_identity(
    store: &dyn VaultStore,
    token_secret: &[u8; 32],
    subject: &str,
) -> Result<Zeroizing<String>> {
    let record = read(store, subject, true)?;
    let sealed = record
        .encryption
        .as_ref()
        .ok_or(Error("custodial_vault_unavailable"))?;
    let bytes = vault_box::open(
        &*key(token_secret)?,
        aad(subject, &record.encryption_key_id, "encryption")?.as_bytes(),
        sealed,
    )
    .map_err(|_| Error("custodial_vault_corrupt"))?;
    let identity = Zeroizing::new(
        std::str::from_utf8(&bytes)
            .map_err(|_| Error("custodial_vault_corrupt"))?
            .to_owned(),
    );
    let recipient = age_recipient::recipient_from_identity(&identity)
        .map_err(|_| Error("custodial_vault_corrupt"))?;
    require(
        age_recipient::encryption_key_id(&age_recipient::public_from_recipient(&recipient)?)
            == record.encryption_key_id,
        "custodial_vault_corrupt",
    )?;
    Ok(identity)
}

pub fn seal_keys(
    token_secret: &[u8; 32],
    subject: &str,
    signing_seed: &[u8; 32],
    age_private: &[u8; 32],
    now: Timestamp,
) -> Result<VaultRecord> {
    let signing = Ed25519Signer::from_seed(signing_seed);
    let signing_key_id = key_id(&signing.public_key());
    let identity = age_recipient::identity_from_private(age_private);
    let recipient = age_recipient::recipient_from_identity(&identity)?;
    let encryption_key_id =
        age_recipient::encryption_key_id(&age_recipient::public_from_recipient(&recipient)?);
    let vault_key = key(token_secret)?;
    Ok(VaultRecord {
        signing: Some(vault_box::seal(
            &vault_key,
            aad(subject, &signing_key_id, "identity")?.as_bytes(),
            signing_seed,
        )?),
        encryption: Some(vault_box::seal(
            &vault_key,
            aad(subject, &encryption_key_id, "encryption")?.as_bytes(),
            identity.as_bytes(),
        )?),
        subject: subject.into(),
        signing_key_id,
        encryption_key_id,
        status: "active".into(),
        created_at: now,
        destroyed_at: None,
    })
}

fn upgrade_aad(subject: &str, challenge: &str) -> Result<String> {
    wire(&serde_json::json!({"domain":"msg-custodial-upgrade-ephemeral-v1","subject":subject,"challenge_id":challenge}))?.canonical()
}
pub fn seal_upgrade_private(
    token_secret: &[u8; 32],
    subject: &str,
    challenge: &str,
    private: &[u8; 32],
) -> Result<vault_box::SealedSecret> {
    vault_box::seal(
        &*key(token_secret)?,
        upgrade_aad(subject, challenge)?.as_bytes(),
        private,
    )
}
pub fn open_upgrade_private(
    token_secret: &[u8; 32],
    subject: &str,
    challenge: &str,
    sealed: &vault_box::SealedSecret,
) -> Result<Zeroizing<[u8; 32]>> {
    let bytes = vault_box::open(
        &*key(token_secret)?,
        upgrade_aad(subject, challenge)?.as_bytes(),
        sealed,
    )
    .map_err(|_| Error("custodial_upgrade_challenge_corrupt"))?;
    let fixed: &[u8; 32] = bytes
        .as_slice()
        .try_into()
        .map_err(|_| Error("custodial_upgrade_challenge_corrupt"))?;
    Ok(Zeroizing::new(*fixed))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn opens_python_vault_ciphertext_with_the_existing_domains() {
        let sealed = vault_box::SealedSecret {
            nonce: "AAECAwQFBgcICQoL".into(),
            ciphertext: "XngSQSqS5S2zJ-2pfPKR_3HBhEbroZupgu6eQhPKYdsXYDDCSRAKYxFTgZoue9qb".into(),
        };
        let key_id = "k_b600306cfa76723fdec395e53a9b3d9fdb78b1e2d7a23c32fcbcd2dc6d0c4092";
        let key = key(&[9; 32]).unwrap();
        assert_eq!(
            msg_core::b64(key.as_ref()),
            "uwtpO4w0Rf0mMYdvOWpXvElt5RQwJCsZ0iCKtEV0lGA"
        );
        let plain = vault_box::open(
            &key,
            aad("u_cust_fixture", key_id, "identity")
                .unwrap()
                .as_bytes(),
            &sealed,
        )
        .unwrap();
        assert_eq!(&*plain, &[42; 32]);
        assert!(vault_box::open(
            &key,
            aad("u_other", key_id, "identity").unwrap().as_bytes(),
            &sealed
        )
        .is_err());
        assert!(vault_box::open(
            &key,
            aad("u_cust_fixture", key_id, "encryption")
                .unwrap()
                .as_bytes(),
            &sealed
        )
        .is_err());
    }
}
