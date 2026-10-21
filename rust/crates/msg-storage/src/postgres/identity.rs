//! Identity mutations on the caller's fenced PostgreSQL transaction.
//!
//! The runtime generation is captured at daemon startup, never from a request.
//! The caller supplies its latched recovery/drill-marker state to `new`.
use super::{PgSession, Row};
use crate::records::{self, IdentityRecord};
use msg_core::{b64, unb64, Error, Json, Result};
use msg_identity::{
    browser::BrowserStore,
    models::{decode, Certificate, Credential, Subject, Timestamp, Verifier},
    mutations::{EncryptionKey, IdentityKey, IdentityStore, TokenDelivery},
    sessions::CredentialState,
    store::{AuthorityStore, Record, RecoveryDelivery, RuntimeGeneration},
};

pub struct IdentitySession<'a> {
    session: &'a PgSession,
    generation: &'a mut RuntimeGeneration,
    quarantined: bool,
}

fn require(allowed: bool, code: &'static str) -> Result<()> {
    if allowed {
        Ok(())
    } else {
        Err(Error(code))
    }
}
fn time(value: Timestamp) -> String {
    value.0.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()
}
fn optional_time(row: &Row, column: usize) -> Result<Option<Timestamp>> {
    row.optional_text(column)?.map(Timestamp::parse).transpose()
}
fn identity_key(row: &Row) -> Result<IdentityKey> {
    let value = IdentityKey {
        key_id: row.text(0)?.into(),
        subject: row.text(1)?.into(),
        public_key: Verifier(unb64(row.text(2)?, 32)?),
        created_at: Timestamp::parse(row.text(3)?)?,
        retired_at: optional_time(row, 4)?,
        primary: row.integer(5)? != 0,
    };
    require(
        value.public_key.0.len() == 32 && msg_crypto::key_id(&value.public_key.0) == value.key_id,
        "storage_record_mismatch",
    )?;
    Ok(value)
}
fn encryption_key(row: &Row) -> Result<EncryptionKey> {
    let value = EncryptionKey {
        key_id: row.text(0)?.into(),
        subject: row.text(1)?.into(),
        recipient: row.text(2)?.into(),
        public_key: Verifier(unb64(row.text(3)?, 32)?),
        created_at: Timestamp::parse(row.text(4)?)?,
        retired_at: optional_time(row, 5)?,
        primary: row.integer(6)? != 0,
    };
    let public = msg_crypto::age_recipient::public_from_recipient(&value.recipient)?;
    require(
        public.as_slice() == value.public_key.0
            && msg_crypto::age_recipient::encryption_key_id(&public) == value.key_id,
        "storage_record_mismatch",
    )?;
    Ok(value)
}
fn delivery(row: &Row) -> Result<TokenDelivery> {
    Ok(TokenDelivery {
        credential_id: row.text(0)?.into(),
        subject: row.text(1)?.into(),
        request_id: row.text(2)?.into(),
        request_digest: row.text(3)?.into(),
        recovery_verifier: row.text(4)?.into(),
        recovery_expires_at: Timestamp::parse(row.text(5)?)?,
        claimed_at: optional_time(row, 6)?,
        consumed_at: optional_time(row, 7)?,
    })
}

impl<'a> IdentitySession<'a> {
    pub fn new(
        session: &'a PgSession,
        generation: &'a mut RuntimeGeneration,
        quarantined: bool,
    ) -> Result<Self> {
        let mut value = Self {
            session,
            generation,
            quarantined,
        };
        value.require_live()?;
        Ok(value)
    }

    fn mutate<T>(&mut self, action: impl FnOnce(&PgSession) -> Result<T>) -> Result<T> {
        self.require_live()?;
        self.session.require_write()?;
        self.session.capture(action(self.session), true)
    }
}

impl AuthorityStore for IdentitySession<'_> {
    fn record(&self, kind: Record, id: &str) -> Result<Json> {
        self.session.record(kind, id)
    }
    fn setting(&self, key: &str) -> Result<Option<Json>> {
        self.session.setting(key)
    }
    fn has_setting(&self, key: &str) -> Result<bool> {
        self.session.has_setting(key)
    }
    fn certificate_revoked(&self, id: &str) -> Result<bool> {
        self.session.certificate_revoked(id)
    }
    fn request_result(
        &self,
        subject: Option<&str>,
        id: &str,
        digest: &str,
    ) -> Result<Option<Json>> {
        self.session.request_result(subject, id, digest)
    }
    fn credential_state(&self, id: &str) -> Result<Option<CredentialState>> {
        self.session.credential_state(id)
    }
    fn custodial_binding(&self, subject: &str) -> Result<Option<(String, String)>> {
        self.session.custodial_binding(subject)
    }
    fn recovery_delivery(
        &self,
        credential: &str,
        subject: &str,
    ) -> Result<Option<RecoveryDelivery>> {
        self.session.recovery_delivery(credential, subject)
    }
}

impl BrowserStore for IdentitySession<'_> {
    fn require_live(&mut self) -> Result<()> {
        let result = (|| {
            self.session.active(false)?;
            self.generation.require_current(self.session)?;
            require(
                !self.quarantined && !self.session.has_setting("recovery_quarantine")?,
                "recovery_quarantined",
            )
        })();
        self.session.capture(result, self.session.write)
    }
    fn save_credential(&mut self, credential: &Credential, auth_version: u64) -> Result<()> {
        self.mutate(|tx| tx.save_credential(credential, auth_version))
    }
    fn put_state(&mut self, id: &str, kind: &str, expires: Timestamp, body: &Json) -> Result<()> {
        self.mutate(|tx| {
            let count = tx.execute(
                "INSERT INTO credential_states(id,kind,expires,body) VALUES($1,$2,$3,$4) ON CONFLICT(id) DO UPDATE SET expires=excluded.expires,body=excluded.body WHERE credential_states.kind=excluded.kind",
                &[id.into(), kind.into(), time(expires).into(), body.canonical()?.into()],
            )?;
            require(count == 1, "credential_state_conflict")
        })
    }
    fn replace_state(&mut self, id: &str, body: &Json) -> Result<()> {
        self.mutate(|tx| {
            let count = tx.execute(
                "UPDATE credential_states SET body=$1 WHERE id=$2",
                &[body.canonical()?.into(), id.into()],
            )?;
            require(count == 1, "invalid_grant")
        })
    }
    fn expire_states(&mut self, now: Timestamp) -> Result<()> {
        self.mutate(|tx| {
            tx.execute(
                "DELETE FROM credential_states WHERE expires<=$1",
                &[time(now).into()],
            )?;
            Ok(())
        })
    }
    fn state_count(&self) -> Result<u64> {
        let count = self
            .session
            .one("SELECT count(*) FROM credential_states", &[])?
            .ok_or(Error("storage_error"))?
            .integer(0)?;
        u64::try_from(count).map_err(|_| Error("storage_record_mismatch"))
    }
}

impl IdentityStore for IdentitySession<'_> {
    fn resolve_identity(&self, reference: &str) -> Result<String> {
        if reference.starts_with('/') {
            return self.session.resolve(reference, false);
        }
        require(
            !reference.is_empty()
                && reference.len() <= 160
                && reference
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"_.:-".contains(&byte)),
            "invalid_resource_id",
        )?;
        Ok(self.session.resource_record(reference)?.id)
    }
    fn update_subject(&mut self, subject: &Subject, expected: i64) -> Result<()> {
        self.mutate(|tx| {
            require(
                expected
                    .checked_add(1)
                    .and_then(|value| u64::try_from(value).ok())
                    == Some(subject.auth_version),
                "invalid_generation",
            )?;
            tx.update_identity(IdentityRecord::Subject(subject), expected)
        })
    }
    fn claim_handle(&mut self, parent: &str, handle: &str, subject: &str) -> Result<()> {
        self.mutate(|tx| {
            let name = format!("@{handle}");
            let taken = tx.one(
                "SELECT EXISTS(SELECT 1 FROM resources WHERE parent=$1 AND name=$2 AND id<>$3) OR EXISTS(SELECT 1 FROM resource_path_aliases WHERE parent_id=$1 AND name=$2 AND resource_id<>$3)",
                &[parent.into(), name.into(), subject.into()],
            )?.ok_or(Error("storage_error"))?.boolean(0)?;
            require(!taken, "handle_unavailable")
        })
    }
    fn insert_identity_resource(&mut self, resource: &Json) -> Result<()> {
        self.mutate(|tx| tx.insert_resource(&records::decode(resource)?))
    }
    fn replace_identity_resource(&mut self, resource: &Json, expected: i64) -> Result<()> {
        self.mutate(|tx| tx.replace_resource(&records::decode(resource)?, expected))
    }
    fn set_identity_setting(&mut self, key: &str, value: &Json) -> Result<()> {
        self.mutate(|tx| tx.set_setting(key, value))
    }
    fn identity_result(&self, subject: &str, request: &str) -> Result<Option<Json>> {
        match self.session.one(
            "SELECT digest FROM results WHERE subject=$1 AND request_id=$2",
            &[subject.into(), request.into()],
        )? {
            Some(row) => self
                .session
                .request_result(Some(subject), request, row.text(0)?),
            None => Ok(None),
        }
    }
    fn identity_credentials(&self, subject: &str) -> Result<Vec<Credential>> {
        self.session
            .rows(
                "SELECT id,substring(body,1,1048577) FROM credentials WHERE subject=$1 ORDER BY id",
                &[subject.into()],
            )?
            .into_iter()
            .map(|row| {
                let credential: Credential = decode(&row.json(1)?)?;
                require(
                    credential.subject_id == subject && credential.id == row.text(0)?,
                    "storage_record_mismatch",
                )?;
                Ok(credential)
            })
            .collect()
    }
    fn identity_keys(&self, subject: &str) -> Result<Vec<IdentityKey>> {
        self.session.rows("SELECT key_id,subject,public_key,created_at,retired_at,is_primary FROM identity_keys WHERE subject=$1 ORDER BY created_at,key_id", &[subject.into()])?
            .iter().map(identity_key).collect()
    }
    fn save_identity_key(&mut self, key: &IdentityKey) -> Result<()> {
        self.mutate(|tx| {
            require(key.public_key.0.len() == 32 && msg_crypto::key_id(&key.public_key.0) == key.key_id, "invalid_public_key")?;
            require(!key.primary || key.retired_at.is_none(), "identity_key_retired")?;
            if let Some(row) = tx.one("SELECT key_id,subject,public_key,created_at,retired_at,is_primary FROM identity_keys WHERE key_id=$1", &[key.key_id.as_str().into()])? {
                let old = identity_key(&row)?;
                require(old.subject == key.subject && old.public_key.0 == key.public_key.0 && old.created_at == key.created_at
                    && (old.retired_at.is_none() || old.retired_at == key.retired_at), "key_identity_immutable")?;
                tx.execute("UPDATE identity_keys SET retired_at=$1,is_primary=$2 WHERE key_id=$3", &[key.retired_at.map(time).into(), i64::from(key.primary).into(), key.key_id.as_str().into()])?;
            } else {
                tx.subject(&key.subject)?;
                tx.execute("INSERT INTO identity_keys(key_id,subject,public_key,created_at,retired_at,is_primary) VALUES($1,$2,$3,$4,$5,$6)", &[
                    key.key_id.as_str().into(), key.subject.as_str().into(), b64(&key.public_key.0).into(), time(key.created_at).into(), key.retired_at.map(time).into(), i64::from(key.primary).into(),
                ])?;
            }
            Ok(())
        })
    }
    fn encryption_keys(&self, subject: &str) -> Result<Vec<EncryptionKey>> {
        self.session.rows("SELECT key_id,subject,recipient,public_key,created_at,retired_at,is_primary FROM encryption_subkeys WHERE subject=$1 ORDER BY created_at,key_id", &[subject.into()])?
            .iter().map(encryption_key).collect()
    }
    fn encryption_key_exists(&self, key: &str) -> Result<bool> {
        self.session
            .one(
                "SELECT EXISTS(SELECT 1 FROM encryption_subkeys WHERE key_id=$1)",
                &[key.into()],
            )?
            .ok_or(Error("storage_error"))?
            .boolean(0)
    }
    fn save_encryption_key(&mut self, key: &EncryptionKey) -> Result<()> {
        self.mutate(|tx| {
            let public = msg_crypto::age_recipient::public_from_recipient(&key.recipient)?;
            require(public.as_slice() == key.public_key.0 && msg_crypto::age_recipient::encryption_key_id(&public) == key.key_id, "invalid_age_recipient")?;
            require(!key.primary || key.retired_at.is_none(), "encryption_key_retired")?;
            if let Some(row) = tx.one("SELECT key_id,subject,recipient,public_key,created_at,retired_at,is_primary FROM encryption_subkeys WHERE key_id=$1", &[key.key_id.as_str().into()])? {
                let old = encryption_key(&row)?;
                require(old.subject == key.subject && old.recipient == key.recipient && old.public_key.0 == key.public_key.0 && old.created_at == key.created_at
                    && (old.retired_at.is_none() || old.retired_at == key.retired_at), "key_identity_immutable")?;
                tx.execute("UPDATE encryption_subkeys SET retired_at=$1,is_primary=$2 WHERE key_id=$3", &[key.retired_at.map(time).into(), i64::from(key.primary).into(), key.key_id.as_str().into()])?;
            } else {
                tx.subject(&key.subject)?;
                tx.execute("INSERT INTO encryption_subkeys(key_id,subject,recipient,public_key,created_at,retired_at,is_primary) VALUES($1,$2,$3,$4,$5,$6,$7)", &[
                    key.key_id.as_str().into(), key.subject.as_str().into(), key.recipient.as_str().into(), b64(&key.public_key.0).into(), time(key.created_at).into(), key.retired_at.map(time).into(), i64::from(key.primary).into(),
                ])?;
            }
            Ok(())
        })
    }
    fn custodial_migration_pending(&self, subject: &str, now: Timestamp) -> Result<bool> {
        self.session.one(
            "SELECT EXISTS(SELECT 1 FROM custodial_upgrades u LEFT JOIN custodial_vault v ON v.subject=u.subject WHERE u.subject=$1 AND (u.status='pending_rewrap' OR (u.status='pending' AND u.expires_at>$2) OR v.status='decrypt_only'))",
            &[subject.into(), time(now).into()],
        )?.ok_or(Error("storage_error"))?.boolean(0)
    }
    fn identity_certificates(&self, subject: &str) -> Result<Vec<Certificate>> {
        self.session.rows("SELECT id,substring(body,1,1048577) FROM certificates WHERE subject=$1 AND revoked=0 ORDER BY id", &[subject.into()])?
            .into_iter().map(|row| {
                let cert: Certificate = decode(&row.json(1)?)?;
                require(cert.subject_id == subject && cert.resource_id == row.text(0)?, "storage_record_mismatch")?;
                Ok(cert)
            }).collect()
    }
    fn register_certificate(&mut self, certificate: &Certificate) -> Result<()> {
        self.mutate(|tx| tx.register_certificate(certificate, None))
    }
    fn append_identity_audit(&mut self, audit: &Json) -> Result<()> {
        self.mutate(|tx| tx.append_audit(&records::decode(audit)?))
    }
    fn insert_token_delivery(&mut self, value: &TokenDelivery) -> Result<()> {
        self.mutate(|tx| {
            require(value.claimed_at.is_none() && value.consumed_at.is_none(), "invalid_token_delivery")?;
            require(tx.credential(&value.credential_id)?.subject_id == value.subject, "credential_owner_required")?;
            tx.execute("INSERT INTO token_deliveries(credential_id,subject,request_id,request_digest,recovery_verifier,recovery_expires_at,claimed_at,consumed_at) VALUES($1,$2,$3,$4,$5,$6,NULL,NULL)", &[
                value.credential_id.as_str().into(), value.subject.as_str().into(), value.request_id.as_str().into(), value.request_digest.as_str().into(), value.recovery_verifier.as_str().into(), time(value.recovery_expires_at).into(),
            ])?;
            Ok(())
        })
    }
    fn token_delivery(&self, credential: &str) -> Result<Option<TokenDelivery>> {
        self.session.one("SELECT credential_id,subject,request_id,request_digest,recovery_verifier,recovery_expires_at,claimed_at,consumed_at FROM token_deliveries WHERE credential_id=$1", &[credential.into()])?
            .as_ref().map(delivery).transpose()
    }
    fn claim_token_delivery(&mut self, credential: &str, now: Timestamp) -> Result<bool> {
        self.mutate(|tx| Ok(tx.execute("UPDATE token_deliveries SET claimed_at=$1 WHERE credential_id=$2 AND claimed_at IS NULL AND consumed_at IS NULL", &[time(now).into(), credential.into()])? == 1))
    }
    fn consume_token_delivery(&mut self, credential: &str, now: Timestamp) -> Result<bool> {
        self.mutate(|tx| Ok(tx.execute("UPDATE token_deliveries SET consumed_at=$1 WHERE credential_id=$2 AND consumed_at IS NULL", &[time(now).into(), credential.into()])? == 1))
    }
}
