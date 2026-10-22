//! Signed history evidence and precise custody-retirement claims.
use crate::{
    custodial_store::{CustodialStore, HistoryItem, Upgrade},
    lifecycle::{field, optional, text},
    models::*,
    require,
    store::AuthorityStore,
};
use msg_core::{digest, unb64, Error, Json, Result};
use msg_crypto::{age_recipient, key_id, verify};
use std::collections::{BTreeMap, BTreeSet};

pub(crate) fn hash(value: &Json) -> Result<String> {
    Ok(digest(value.canonical()?.as_bytes()))
}
pub(crate) fn empty_map() -> Json {
    Json::from_object(BTreeMap::new())
}
pub(crate) fn map(value: &Json, key: &str) -> Result<BTreeMap<String, Json>> {
    optional(value, key)?
        .map(|v| v.clone().into_object())
        .transpose()
        .map(|value| value.unwrap_or_default())
}
pub(crate) fn set(value: &mut Json, key: &str, item: Json) -> Result<()> {
    let mut fields = value.clone().into_object()?;
    fields.insert(key.to_owned(), item);
    *value = Json::from_object(fields);
    Ok(())
}
pub(crate) fn remove(value: &mut Json, key: &str) -> Result<()> {
    let mut fields = value.clone().into_object()?;
    fields.remove(key);
    *value = Json::from_object(fields);
    Ok(())
}
fn default_json(value: &Json, key: &str, default: Json) -> Result<Json> {
    Ok(optional(value, key)?.cloned().unwrap_or(default))
}
pub fn inventory_commitment(record: &Upgrade) -> Result<String> {
    let details = &record.details;
    hash(&wire(&serde_json::json!({
        "domain":"msg-custodial-inventory-v1","subject_id":record.subject,"challenge_id":record.id,
        "version":default_json(details,"inventory_version",wire(&1)?)?,
        "old_identity_key_id":field(details,"old_identity_key_id")?,
        "new_identity_key_id":field(details,"new_identity_key_id")?,
        "old_encryption_key_id":field(details,"old_encryption_key_id")?,
        "new_encryption_key_id":field(details,"new_encryption_key_id")?,
        "recipient":field(&record.challenge,"encryption_recipient")?,
        "items":field(details,"age_inventory")?,
        "source_keys":default_json(details,"source_keys",empty_map())?
    }))?)
}
pub fn keyed_source(item: &HistoryItem, details: &Json) -> Result<Json> {
    let source_keys = map(details, "source_keys")?;
    let key = source_keys
        .get(&item.revision)
        .map(Json::as_str)
        .transpose()?
        .unwrap_or(text(details, "old_encryption_key_id")?);
    let mut value = wire(item)?;
    set(&mut value, "key_id", Json::string(key))?;
    Ok(value)
}
pub fn keyed_target(mapping: &Json, details: &Json) -> Result<Json> {
    let mut value = field(mapping, "new")?.clone();
    set(
        &mut value,
        "key_id",
        field(details, "new_encryption_key_id")?.clone(),
    )?;
    set(
        &mut value,
        "recipient",
        field(mapping, "recipient")?.clone(),
    )?;
    Ok(value)
}
pub struct Acknowledgement<'a> {
    pub subject: &'a str,
    pub challenge: &'a str,
    pub inventory_digest: &'a str,
    pub source: &'a Json,
    pub target: &'a Json,
    pub plaintext_digest: &'a str,
    pub request_id: &'a str,
    pub method: &'a str,
}
impl Acknowledgement<'_> {
    pub fn statement(&self) -> Result<Json> {
        wire(&serde_json::json!({"domain":"msg-custodial-history-ack-v1",
            "subject_id":self.subject,"challenge_id":self.challenge,"inventory_digest":self.inventory_digest,
            "source":self.source,"target":self.target,"method":self.method,
            "plaintext_digest":self.plaintext_digest,"request_id":self.request_id}))
    }
}
pub fn stage_statement(subject: &str, arguments: &Json, request: &str) -> Result<Json> {
    let mut decision = arguments.clone().into_object()?;
    decision.remove("migration_ack");
    wire(
        &serde_json::json!({"domain":"msg-custodial-decision-v1","subject_id":subject,
        "request_id":request,"decision":Json::from_object(decision)}),
    )
}

pub fn verify_challenge_binding<S: CustodialStore>(store: &S, record: &Upgrade) -> Result<()> {
    let details = &record.details;
    let challenge = &record.challenge;
    let old = (|| {
        let public = unb64(text(challenge, "public_key")?, 32)?;
        let old = store.credential(text(details, "old_identity_key_id")?)?;
        let age = age_recipient::public_from_recipient(text(challenge, "encryption_recipient")?)?;
        require(
            text(challenge, "subject_id")? == record.subject
                && text(challenge, "challenge_id")? == record.id
                && old.subject_id == record.subject
                && old.kind == CredentialKind::SigningKey
                && key_id(&old.verifier.0) == old.id
                && key_id(&public) == text(details, "new_identity_key_id")?
                && old.id != text(details, "new_identity_key_id")?
                && age_recipient::encryption_key_id(&age)
                    == text(details, "new_encryption_key_id")?
                && text(details, "old_encryption_key_id")?
                    != text(details, "new_encryption_key_id")?,
            "custodial_challenge_invalid",
        )?;
        verify(
            &old.verifier.0,
            challenge.canonical()?.as_bytes(),
            &decode(field(details, "server_signature")?)?,
            "custodial-upgrade-challenge",
        )?;
        let old_age = store
            .encryption_keys(&record.subject)?
            .into_iter()
            .find(|key| key.key_id == text(details, "old_encryption_key_id").unwrap_or(""))
            .ok_or(Error("custodial_challenge_invalid"))?;
        let public = age_recipient::public_from_recipient(&old_age.recipient)?;
        require(
            old_age.subject == record.subject
                && old_age.public_key.0.as_slice() == public.as_slice()
                && age_recipient::encryption_key_id(&public)
                    == text(details, "old_encryption_key_id")?,
            "custodial_challenge_invalid",
        )?;
        Ok(old)
    })()
    .map_err(|_: Error| Error("custodial_challenge_invalid"))?;
    if record.status != "completed" {
        require(old.revoked_at.is_none(), "custodial_signing_key_retired")?;
    }
    let retained = store.vault_record(&record.subject)?;
    if record.status == "pending_rewrap"
        || (record.status == "completed"
            && retained.is_some_and(|vault| vault.status == "decrypt_only"))
    {
        let target = store
            .encryption_keys(&record.subject)?
            .into_iter()
            .find(|key| key.key_id == text(details, "new_encryption_key_id").unwrap_or(""))
            .ok_or(Error("custodial_encryption_target_changed"))?;
        require(
            target.subject == record.subject
                && target.primary
                && target.retired_at.is_none()
                && target.recipient == text(challenge, "encryption_recipient")?,
            "custodial_encryption_target_changed",
        )?;
    }
    Ok(())
}

fn backup_check(
    store: &dyn AuthorityStore,
    record: &Json,
    upgrade: &Upgrade,
    now: Timestamp,
) -> Result<Json> {
    let id = store
        .setting("active_root_certificate")?
        .ok_or(Error("backup_retirement_invalid"))?;
    let root = store.certificate(id.as_str()?)?;
    let credential = store.credential(&root.key_id)?;
    require(
        root.resource_id == id.as_str()?
            && root.subject_id == "u_root"
            && root.issuer_id == "u_root"
            && root.parent_certificate_id.is_none()
            && root.kind == CertificateKind::Ca
            && root.issuance.is_some()
            && root.not_before <= now
            && now < root.expires_at
            && !store.certificate_revoked(&root.resource_id)?
            && credential.subject_id == "u_root"
            && credential.kind == CredentialKind::SigningKey
            && credential.current_at(now)
            && key_id(&credential.verifier.0) == credential.id
            && credential.id == root.key_id,
        "backup_retirement_invalid",
    )?;
    verify(
        &credential.verifier.0,
        &root.signing_bytes()?,
        &root.signature,
        "certificate",
    )?;
    require(
        record
            .as_object()?
            .keys()
            .map(String::as_str)
            .collect::<BTreeSet<_>>()
            == BTreeSet::from(["signature", "statement"]),
        "backup_retirement_invalid",
    )?;
    let statement = field(record, "statement")?;
    require(
        statement
            .as_object()?
            .keys()
            .map(String::as_str)
            .collect::<BTreeSet<_>>()
            == BTreeSet::from([
                "domain",
                "subject_id",
                "old_identity_key_id",
                "old_encryption_key_id",
                "backup_sets",
                "attested_at",
                "not_after",
                "operator",
            ]),
        "backup_retirement_invalid",
    )?;
    require(
        text(statement, "domain")? == "msg-custodial-backup-retirement-v1"
            && text(statement, "subject_id")? == upgrade.subject
            && text(statement, "old_identity_key_id")?
                == text(&upgrade.details, "old_identity_key_id")?
            && text(statement, "old_encryption_key_id")?
                == text(&upgrade.details, "old_encryption_key_id")?
            && !text(statement, "operator")?.is_empty(),
        "backup_retirement_invalid",
    )?;
    let sets: Vec<String> = decode(field(statement, "backup_sets")?)?;
    require(
        !sets.is_empty()
            && sets
                .iter()
                .all(|v| !v.is_empty() && v.chars().count() <= 256)
            && sets.windows(2).all(|pair| pair[0] < pair[1]),
        "backup_retirement_invalid",
    )?;
    let start: Timestamp = decode(field(statement, "attested_at")?)?;
    let end: Timestamp = decode(field(statement, "not_after")?)?;
    let signature = decode(field(record, "signature")?)?;
    verify(
        &credential.verifier.0,
        statement.canonical()?.as_bytes(),
        &signature,
        "custodial-backup-retirement-v1",
    )?;
    require(
        end > start && end.0 - start.0 <= chrono::Duration::days(400),
        "backup_retirement_invalid",
    )?;
    require(start <= now && now < end, "backup_retirement_expired")?;
    wire(
        &serde_json::json!({"scope":"listed_backup_sets_only","backup_sets":sets,
        "attested_at":start,"not_after":end,"signer_key_id":signature.key_id,"record_digest":hash(record)?}),
    )
}
fn backup(
    store: &dyn AuthorityStore,
    upgrade: &Upgrade,
    now: Option<Timestamp>,
) -> Result<(&'static str, Option<Json>)> {
    let Some(record) = store.setting(&format!(
        "custodial_backup_retirement:{}:{}",
        upgrade.subject,
        text(&upgrade.details, "old_identity_key_id")?
    ))?
    else {
        return Ok(("requires_offline_verification", None));
    };
    let Some(now) = now else {
        return Ok(("local_attestation_clock_unavailable", None));
    };
    Ok(match backup_check(store, &record, upgrade, now) {
        Ok(attestation) => ("local_attestation_verified", Some(attestation)),
        Err(Error("backup_retirement_expired")) => ("local_attestation_expired", None),
        Err(_) => ("local_attestation_invalid", None),
    })
}

pub struct Snapshot {
    pub data: Json,
}
impl Snapshot {
    pub fn unchanged(&self) -> Result<()> {
        require(
            !field(&self.data, "inventory_changed")?.as_bool()?,
            "custodial_inventory_changed",
        )
    }
    pub fn require_observation(&self, arguments: &Json) -> Result<()> {
        require(
            ["inventory_digest", "results_digest", "observed_digest"]
                .iter()
                .all(|key| {
                    optional(arguments, key).ok().flatten()
                        == optional(&self.data, key).ok().flatten()
                }),
            "custodial_migration_preview_stale",
        )
    }
}

pub fn snapshot<S: CustodialStore>(
    store: &S,
    record: &Upgrade,
    now: Option<Timestamp>,
) -> Result<Snapshot> {
    verify_challenge_binding(store, record)?;
    let details = &record.details;
    let frozen: Vec<HistoryItem> = decode(field(details, "age_inventory")?)
        .map_err(|_| Error("custodial_inventory_required"))?;
    let source_ids: BTreeSet<_> = frozen.iter().map(|item| item.revision.clone()).collect();
    require(
        frozen.windows(2).all(|pair| {
            (pair[0].id.as_str(), pair[0].revision.as_str())
                < (pair[1].id.as_str(), pair[1].revision.as_str())
        }) && source_ids.len() == frozen.len(),
        "custodial_inventory_corrupt",
    )?;
    let current = store.owned_age_inventory(&record.subject)?;
    let mappings = map(details, "rewrap_mappings")?;
    let acks = map(details, "rewrap_acks")?;
    let recovery_acks = map(details, "recovery_acks")?;
    let envelope = optional(details, "recovery_envelope")?.filter(|v| !v.is_null());
    let mut outputs: Vec<HistoryItem> = mappings
        .values()
        .filter(|value| {
            optional(value, "copy_kind")
                .ok()
                .flatten()
                .and_then(|v| v.as_str().ok())
                != Some("new_key_write")
        })
        .map(|value| decode(field(value, "new")?))
        .collect::<Result<_>>()?;
    if let Some(envelope) = envelope {
        outputs.push(decode(field(envelope, "ciphertext")?)?);
    }
    let added: Vec<_> = current
        .iter()
        .filter(|item| !outputs.contains(item) && !frozen.contains(item))
        .cloned()
        .collect();
    let missing: Vec<_> = frozen
        .iter()
        .filter(|item| !current.contains(item))
        .cloned()
        .collect();
    let mut expected = frozen.clone();
    expected.extend(
        outputs
            .iter()
            .filter(|item| !frozen.contains(item))
            .cloned(),
    );
    expected.sort_by(|a, b| (&a.id, &a.revision).cmp(&(&b.id, &b.revision)));
    let drift = current != expected;
    let commitment = inventory_commitment(record)?;
    for map in [&mappings, &acks, &recovery_acks] {
        require(
            map.keys().all(|key| source_ids.contains(key)),
            "custodial_mapping_corrupt",
        )?;
    }
    let mut targets = BTreeSet::new();
    let mut verified = BTreeSet::new();
    let mut legacy_acked = BTreeSet::new();
    let mut methods = BTreeMap::new();
    let public = unb64(text(&record.challenge, "public_key")?, 32)?;
    for (revision, mapping) in &mappings {
        let source = frozen
            .iter()
            .find(|item| &item.revision == revision)
            .ok_or(Error("custodial_mapping_corrupt"))?;
        let keyed = keyed_source(source, details)?;
        require(
            field(mapping, "old")? == &wire(source)?
                && text(mapping, "recipient")? == text(&record.challenge, "encryption_recipient")?
                && optional(mapping, "old_key_id")?.unwrap_or(field(&keyed, "key_id")?)
                    == field(&keyed, "key_id")?
                && optional(mapping, "new_key_id")?
                    .unwrap_or(field(details, "new_encryption_key_id")?)
                    == field(details, "new_encryption_key_id")?,
            "custodial_mapping_key_mismatch",
        )?;
        let target: HistoryItem = decode(field(mapping, "new")?)?;
        require(
            targets.insert((target.id.clone(), target.revision.clone())),
            "custodial_mapping_target_reused",
        )?;
        if !current.contains(&target) || missing.contains(source) {
            continue;
        }
        let Some(ack) = acks.get(revision) else {
            continue;
        };
        require(
            text(ack, "new_revision")? == target.revision
                && text(ack, "ciphertext_digest")? == target.ciphertext_digest,
            "custodial_rewrap_mapping_mismatch",
        )?;
        legacy_acked.insert(revision.clone());
        if optional(ack, "inventory_digest")?.and_then(|v| v.as_str().ok())
            != Some(commitment.as_str())
        {
            continue;
        }
        let target = keyed_target(mapping, details)?;
        let statement = Acknowledgement {
            subject: &record.subject,
            challenge: &record.id,
            inventory_digest: &commitment,
            source: &keyed,
            target: &target,
            plaintext_digest: text(ack, "plaintext_digest")?,
            request_id: text(ack, "request_id")?,
            method: "rewrap",
        }
        .statement()?;
        verify(
            &public,
            statement.canonical()?.as_bytes(),
            &decode(field(ack, "signature")?)?,
            "custodial-history-ack-v1",
        )?;
        verified.insert(revision.clone());
        methods.insert(revision.clone(), "rewrap");
    }
    if let Some(envelope) = envelope {
        require(
            text(envelope, "subject_id")? == record.subject
                && text(envelope, "old_key_id")? == text(details, "old_encryption_key_id")?
                && text(envelope, "new_key_id")? == text(details, "new_encryption_key_id")?
                && text(envelope, "recipient")? == text(&record.challenge, "encryption_recipient")?
                && text(envelope, "purpose")? == "retired-encryption-subkey-recovery",
            "custodial_recovery_envelope_mismatch",
        )?;
        if current.contains(&decode(field(envelope, "ciphertext")?)?) {
            for (revision, ack) in &recovery_acks {
                let source = frozen
                    .iter()
                    .find(|item| &item.revision == revision)
                    .ok_or(Error("custodial_mapping_corrupt"))?;
                if missing.contains(source)
                    || optional(ack, "inventory_digest")?.and_then(|v| v.as_str().ok())
                        != Some(commitment.as_str())
                {
                    continue;
                }
                let source = keyed_source(source, details)?;
                require(
                    text(&source, "key_id")? == text(envelope, "old_key_id")?,
                    "custodial_recovery_key_mismatch",
                )?;
                let statement = Acknowledgement {
                    subject: &record.subject,
                    challenge: &record.id,
                    inventory_digest: &commitment,
                    source: &source,
                    target: envelope,
                    plaintext_digest: text(ack, "plaintext_digest")?,
                    request_id: text(ack, "request_id")?,
                    method: "compatibility_recovery",
                }
                .statement()?;
                verify(
                    &public,
                    statement.canonical()?.as_bytes(),
                    &decode(field(ack, "signature")?)?,
                    "custodial-history-ack-v1",
                )?;
                verified.insert(revision.clone());
                methods.insert(revision.clone(), "compatibility_recovery");
            }
        }
    }
    let policy = store.latest_recovery_policy(&record.subject)?;
    let policy_opted = optional(&policy, "opted_in")?
        .map(Json::as_bool)
        .transpose()?
        .unwrap_or(false);
    let vault = store.vault_record(&record.subject)?;
    let switched = record.status == "completed";
    let (backup_status, attestation) = if switched {
        backup(store, record, now)?
    } else {
        ("requires_offline_verification", None)
    };
    let signing_deleted = vault.as_ref().is_some_and(|v| v.signing.is_none());
    let encryption_deleted = vault.as_ref().is_some_and(|v| v.encryption.is_none());
    let online = switched
        && vault.as_ref().is_some_and(|v| v.status == "destroyed")
        && signing_deleted
        && encryption_deleted;
    let retired = online && attestation.is_some();
    let retirement = serde_json::json!({"identity_switched":switched,"token_revoked":switched,
        "online_signing_key_deleted":signing_deleted,"online_encryption_key_deleted":encryption_deleted,
        "backup_retired":attestation.is_some(),"backup_status":backup_status,
        "backup_retirement_record":attestation,"server_key_retired":retired});
    let complete = !drift && verified == source_ids;
    let mut phase = if record.status == "pending" {
        "challenge"
    } else {
        "migrating"
    };
    if switched {
        phase = if complete {
            "history_recoverable"
        } else {
            "identity_switched"
        };
        if signing_deleted && encryption_deleted {
            phase = "online_key_retired";
        }
        if retired {
            phase = "server_key_retired";
        }
    }
    let resolution = optional(details, "resolution")?;
    let results = wire(
        &serde_json::json!({"mappings":mappings,"acks":acks,"recovery_envelope":envelope,"recovery_acks":recovery_acks,"resolution":resolution}),
    )?;
    let unresolved: Vec<_> = source_ids.difference(&verified).cloned().collect();
    let sources = frozen
        .iter()
        .map(|item| keyed_source(item, details))
        .collect::<Result<Vec<_>>>()?;
    Ok(Snapshot {
        data: wire(&serde_json::json!({
            "challenge_id":record.id,"status":record.status,"phase":phase,"recipient":field(&record.challenge,"encryption_recipient")?,
            "old_key_id":field(details,"old_encryption_key_id")?,"new_key_id":field(details,"new_encryption_key_id")?,
            "frozen":frozen,"sources":sources,"frozen_digest":hash(&wire(&frozen)?)?,"inventory_digest":commitment,
            "inventory_version":default_json(details,"inventory_version",wire(&1)?)?,"observed_digest":hash(&wire(&current)?)?,
            "results_digest":hash(&results)?,"mappings":mappings,"acks":acks,"recovery_envelope":envelope,"recovery_acks":recovery_acks,"methods":methods,
            "inventory_changed":drift,"delta":{"added":added,"missing":missing},"mapped_count":mappings.len(),"acked_count":legacy_acked.len(),
            "verified_revisions":verified,"unresolved_revisions":unresolved,"known_ciphertexts_migrated":!drift && legacy_acked == source_ids,
            "history_recoverable":complete,"verification_scope":"enumerated_and_client_verified_revisions_only",
            "recovery_policy_version":field(&policy,"version")?,"recovery_policy_requires_review":policy_opted,
            "external_ciphertexts_migrated_owner_claim":default_json(details,"external_ciphertexts_migrated",wire(&false)?)?,
            "external_ciphertexts_verified":false,"historical_revisions_require_migration":!complete,"finalize_ready":complete && !policy_opted,
            "vault_remains_active":vault.as_ref().is_some_and(|v| v.status=="active"),"retirement":retirement
        }))?,
    })
}
