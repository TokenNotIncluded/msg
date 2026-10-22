//! Atomic custody exit, bounded age rewrapping and client-signed history ACKs.
use crate::{
    authentication::{Entry, Principal},
    authorization::{AccessRequirement, AuthorizationService, Check},
    custodial_migration::{
        self as migration, hash, keyed_source, keyed_target, map, remove, set, snapshot,
        Acknowledgement,
    },
    custodial_store::{CustodialStore, HistoryItem, Upgrade},
    lifecycle::{
        self, after, bump, expected, field, new_id, optional, output, owner, text, Issuance,
    },
    models::*,
    mutations::{EncryptionKey, IdentityKey},
    require,
    store::Record,
    vault,
};
use msg_core::{b64, digest, unb64, Error, Json, Result};
use msg_crypto::{age_file, age_recipient, custodial_proof, key_id, verify};
use rand_core::{OsRng, RngCore};
use serde::Serialize;
use subtle::ConstantTimeEq;
use zeroize::Zeroizing;

pub enum Outcome {
    Complete(Json),
    Create(Box<PreparedContent>),
}
enum ContentOperation {
    RecoveryEnvelope,
    Rewrap(HistoryItem),
}

/// Only this module can construct a plan. Runtime creates exactly this encrypted
/// content in the same transaction, then hands its actual revision to finish.
pub struct PreparedContent {
    record: Upgrade,
    parent: String,
    name: String,
    ciphertext: Vec<u8>,
    operation: ContentOperation,
    request_id: String,
    payload_digest: String,
    actor: String,
    credential: String,
    auth_version: u64,
    now: Timestamp,
}
impl PreparedContent {
    pub fn parent(&self) -> &str {
        &self.parent
    }
    pub fn name(&self) -> &str {
        &self.name
    }
    pub fn ciphertext(&self) -> &[u8] {
        &self.ciphertext
    }
}

pub fn supports(operation: &str, version: u32) -> bool {
    match operation {
        "identity.custodial_upgrade_finish" | "identity.custodial_rewrap_ack" => {
            (1..=2).contains(&version)
        }
        "identity.custodial_upgrade_start"
        | "identity.custodial_upgrade_result"
        | "identity.custodial_upgrade_inventory"
        | "identity.custodial_rewrap_entry"
        | "identity.custodial_rewrap_revision"
        | "identity.custodial_migration_get" => version == 1,
        _ => false,
    }
}

pub fn handle<S: CustodialStore>(
    service: &lifecycle::Service<'_>,
    store: &mut S,
    authorizer: &mut AuthorizationService,
    principal: &Principal,
    request: &Json,
    now: Timestamp,
    read: &dyn Fn(&Json, u64) -> Result<Vec<u8>>,
) -> Result<Outcome> {
    store.require_live()?;
    let operation = text(request, "operation")?;
    let version: u32 = decode(field(request, "contract_version")?)?;
    require(supports(operation, version), "operation_not_found")?;
    let subject = owner(store, principal)?;
    authorizer.require_base(
        principal,
        &format!("{operation}@{version}"),
        &subject.resource_id,
        store,
        now,
    )?;
    let mut flow = Flow {
        service,
        store,
        authorizer,
        principal,
        request,
        now,
        subject,
    };
    let args = field(request, "arguments")?;
    match operation {
        "identity.custodial_upgrade_start" => flow.start(args).map(Outcome::Complete),
        "identity.custodial_upgrade_finish" => flow.upgrade_finish(args),
        "identity.custodial_upgrade_result" => flow.result(args).map(Outcome::Complete),
        "identity.custodial_upgrade_inventory" => {
            let record = flow.migration(args, false)?;
            Ok(Outcome::Complete(wire(
                &serde_json::json!({"resources":[],"data":snapshot(flow.store,&record,Some(now))?.data}),
            )?))
        }
        "identity.custodial_rewrap_entry" | "identity.custodial_rewrap_revision" => flow
            .rewrap(args, operation.ends_with("_entry"), read)
            .map(|plan| Outcome::Create(Box::new(plan))),
        "identity.custodial_migration_get" => flow.get(args).map(Outcome::Complete),
        "identity.custodial_rewrap_ack" => flow.ack(args).map(Outcome::Complete),
        _ => Err(Error("operation_not_found")),
    }
}

struct Flow<'a, 'config, S> {
    service: &'a lifecycle::Service<'config>,
    store: &'a mut S,
    authorizer: &'a mut AuthorizationService,
    principal: &'a Principal,
    request: &'a Json,
    now: Timestamp,
    subject: Subject,
}

pub(crate) fn check_handle_change<S: crate::mutations::IdentityStore>(
    store: &mut S,
    resource: &Json,
    handle: &str,
    now: Timestamp,
    record: bool,
) -> Result<(String, Timestamp)> {
    lifecycle::handle(handle)?;
    let id = text(resource, "id")?;
    store.claim_handle(text(resource, "parent")?, handle, id)?;
    let key = format!("identity_handle_renamed_at:{id}");
    let mut next = store
        .setting(&key)?
        .map(|value| decode::<Timestamp>(&value).and_then(|last| after(last, 604800)))
        .transpose()?
        .unwrap_or(now);
    let name = format!("@{handle}");
    if name != text(resource, "name")? {
        require(now >= next, "handle_rename_cooldown")?;
        if record {
            store.set_identity_setting(&key, &wire(&now)?)?;
            next = after(now, 604800)?;
        }
    }
    Ok((name, next))
}

fn activate<S: CustodialStore>(store: &mut S, record: &Upgrade, now: Timestamp) -> Result<()> {
    let key = text(&record.details, "new_encryption_key_id")?;
    let mut current = store
        .encryption_keys(&record.subject)?
        .into_iter()
        .find(|key| key.primary)
        .ok_or(Error("encryption_key_not_found"))?;
    if current.key_id == key {
        return Ok(());
    }
    require(
        current.key_id == text(&record.details, "old_encryption_key_id")?,
        "custodial_encryption_target_changed",
    )?;
    current.primary = false;
    current.retired_at = Some(now);
    store.save_encryption_key(&current)?;
    let recipient = text(&record.challenge, "encryption_recipient")?;
    store.save_encryption_key(&EncryptionKey {
        key_id: key.into(),
        subject: record.subject.clone(),
        recipient: recipient.into(),
        public_key: Verifier(age_recipient::public_from_recipient(recipient)?.to_vec()),
        created_at: now,
        retired_at: None,
        primary: true,
    })
}
fn server_proof(service: &lifecycle::Service<'_>, record: &Upgrade) -> Result<String> {
    let private = vault::open_upgrade_private(
        service.token_secret,
        &record.subject,
        &record.id,
        record
            .ephemeral
            .as_ref()
            .ok_or(Error("custodial_upgrade_challenge_corrupt"))?,
    )?;
    let public =
        age_recipient::public_from_recipient(text(&record.challenge, "encryption_recipient")?)?;
    let fields = record.challenge.as_object()?;
    let context = Json::from_object(
        [
            "subject_id",
            "challenge_id",
            "request_id",
            "handle",
            "public_key",
            "encryption_recipient",
            "server_public",
            "nonce",
            "expires_at",
        ]
        .into_iter()
        .map(|name| {
            Ok((
                name.to_owned(),
                fields
                    .get(name)
                    .ok_or(Error("custodial_upgrade_challenge_corrupt"))?
                    .clone(),
            ))
        })
        .collect::<Result<_>>()?,
    );
    custodial_proof::proof(
        &private,
        &public,
        &unb64(text(&context, "nonce")?, 64)?,
        context.canonical()?.as_bytes(),
    )
}

impl<S: CustodialStore> Flow<'_, '_, S> {
    fn token(&self) -> Result<()> {
        require(
            self.subject.kind == SubjectKind::Custodial && self.principal.method() == "token",
            "custodial_token_required",
        )
    }
    fn audit(&mut self, kind: &str, data: &Json) -> Result<()> {
        self.store.append_identity_audit(&wire(&serde_json::json!({
            "event":{"id":new_id("audit")?,"type":kind,"time":self.now,"request_id":text(self.request,"request_id")?,
                "actor":self.principal.actor(),"subject":self.subject.resource_id,"resources":[{"id":self.subject.resource_id}],"data":data},
            "authority":[{"id":self.subject.resource_id}],"before_digest":null,"after_digest":hash(data)?,
            "previous_digest":null,"entry_digest":"","result":"committed"
        }))?)
    }
    fn read_access(&mut self, id: &str) -> Result<()> {
        self.authorizer.require(
            self.principal,
            Entry::Network,
            self.now,
            &[AccessRequirement {
                resource_id: id.into(),
                operation: format!(
                    "{}@{}",
                    text(self.request, "operation")?,
                    field(self.request, "contract_version")?.as_integer()?
                ),
                check: Check::Read,
            }],
            None,
            self.store.authorization_store(),
        )?;
        Ok(())
    }
    fn start(&mut self, args: &Json) -> Result<Json> {
        self.token()?;
        let uid = &self.subject.resource_id;
        let user = self.store.record(Record::Resource, uid)?;
        check_handle_change(self.store, &user, text(args, "handle")?, self.now, false)?;
        require(
            !self.store.pending_custodial_upgrade(uid, self.now)?,
            "custodial_upgrade_already_pending",
        )?;
        let public = unb64(text(args, "public_key")?, 32)?;
        require(public.len() == 32, "invalid_public_key")?;
        let age = age_recipient::public_from_recipient(text(args, "encryption_recipient")?)?;
        require(
            public.as_slice() != age.as_slice(),
            "encryption_key_must_be_independent",
        )?;
        let old = self
            .store
            .identity_keys(uid)?
            .into_iter()
            .find(|key| key.primary)
            .ok_or(Error("upgrade_keys_must_change"))?;
        let old_age = self
            .store
            .encryption_keys(uid)?
            .into_iter()
            .find(|key| key.primary)
            .ok_or(Error("upgrade_keys_must_change"))?;
        require(
            old.key_id != key_id(&public)
                && old_age.key_id != age_recipient::encryption_key_id(&age),
            "upgrade_keys_must_change",
        )?;
        let possession = wire(
            &serde_json::json!({"subject_id":uid,"handle":field(args,"handle")?,"public_key":field(args,"public_key")?,
            "encryption_recipient":field(args,"encryption_recipient")?,"request_id":text(self.request,"request_id")?}),
        )?;
        verify(
            &public,
            possession.canonical()?.as_bytes(),
            &decode(field(args, "possession_proof")?)?,
            "custodial-upgrade-start",
        )?;
        self.store.expire_custodial_upgrades(uid, self.now)?;
        let id = new_id("cupg")?;
        let mut private = Zeroizing::new([0; 32]);
        let mut nonce = [0; 24];
        OsRng
            .try_fill_bytes(private.as_mut())
            .and_then(|_| OsRng.try_fill_bytes(&mut nonce))
            .map_err(|_| Error("random_unavailable"))?;
        let expires = after(self.now, 300)?;
        let challenge = wire(
            &serde_json::json!({"subject_id":uid,"challenge_id":id,"request_id":text(self.request,"request_id")?,
            "handle":field(args,"handle")?,"public_key":field(args,"public_key")?,"encryption_recipient":field(args,"encryption_recipient")?,
            "server_public":b64(&custodial_proof::public(&private)),"nonce":b64(&nonce),"expires_at":expires}),
        )?;
        let ephemeral = vault::seal_upgrade_private(self.service.token_secret, uid, &id, &private)?;
        let signed = vault::open_signer(self.store, self.service.token_secret, uid, self.now)?
            .sign(
                challenge.canonical()?.as_bytes(),
                "custodial-upgrade-challenge",
            )?;
        let details = wire(
            &serde_json::json!({"old_identity_key_id":old.key_id,"old_encryption_key_id":old_age.key_id,
            "new_identity_key_id":key_id(&public),"new_encryption_key_id":age_recipient::encryption_key_id(&age),
            "age_inventory":self.store.owned_age_inventory(uid)?,"rewrap_mappings":{},"rewrap_acks":{},"inventory_version":1,"recovery_acks":{},
            "signing_possession_digest":hash(field(args,"possession_proof")?)?,"server_signature":signed}),
        )?;
        self.store.insert_custodial_upgrade(&Upgrade {
            id,
            subject: uid.clone(),
            credential_id: self
                .principal
                .credential_id()
                .ok_or(Error("custodial_token_required"))?
                .into(),
            status: "pending".into(),
            expires_at: expires,
            challenge: challenge.clone(),
            ephemeral: Some(ephemeral),
            details,
        })?;
        let mut data = challenge;
        set(&mut data, "status", Json::string("pending"))?;
        set(&mut data, "signature_source", Json::string("custodial"))?;
        set(&mut data, "server_signature", wire(&signed)?)?;
        wire(&serde_json::json!({"resources":[],"data":data}))
    }
    fn migration(&self, args: &Json, pending: bool) -> Result<Upgrade> {
        let record = self
            .store
            .custodial_upgrade(text(args, "challenge_id")?)?
            .ok_or(Error("custodial_upgrade_not_pending"))?;
        require(
            record.subject == self.subject.resource_id,
            "custodial_upgrade_not_pending",
        )?;
        if self.subject.kind == SubjectKind::Custodial {
            require(
                self.principal.method() == "token"
                    && self.principal.credential_id() == Some(record.credential_id.as_str()),
                "custodial_token_required",
            )?;
            require(
                matches!(record.status.as_str(), "pending" | "pending_rewrap"),
                "custodial_upgrade_not_pending",
            )?;
        } else {
            require(
                self.subject.kind == SubjectKind::Registered
                    && self.principal.method() == "signature"
                    && record.status == "completed"
                    && self.principal.credential_id()
                        == Some(text(&record.details, "new_identity_key_id")?),
                "self_custody_signature_required",
            )?;
        }
        require(
            !pending || record.status != "pending",
            "custodial_upgrade_not_pending_rewrap",
        )?;
        Ok(record)
    }
    fn result(&self, args: &Json) -> Result<Json> {
        require(
            self.subject.kind == SubjectKind::Registered && self.principal.method() == "signature",
            "self_custody_signature_required",
        )?;
        let record = self
            .store
            .custodial_upgrade(text(args, "challenge_id")?)?
            .ok_or(Error("custodial_upgrade_not_completed"))?;
        require(
            record.subject == self.subject.resource_id && record.status == "completed",
            "custodial_upgrade_not_completed",
        )?;
        let result = field(&record.details, "completed_result")?;
        require(
            self.principal.credential_id() == Some(text(result, "identity_key_id")?),
            "custodial_upgrade_new_key_required",
        )?;
        wire(&serde_json::json!({"resources":[],"data":result}))
    }
    fn fail(&mut self, record: &Upgrade, reason: &str) -> Result<Outcome> {
        let mut updated = record.clone();
        updated.status = "failed".into();
        updated.ephemeral = None;
        self.store.replace_custodial_upgrade(record, &updated)?;
        Ok(Outcome::Complete(output(
            serde_json::json!({"challenge_id":record.id,"status":"failed","reason":reason}),
            vec![],
        )?))
    }
    fn upgrade_finish(&mut self, args: &Json) -> Result<Outcome> {
        if optional(args, "action")?.is_some() {
            return self.transition(args);
        }
        self.token()?;
        let record = self.migration(args, false)?;
        require(record.status == "pending", "custodial_upgrade_not_pending")?;
        require(self.now < record.expires_at, "custodial_upgrade_expired")?;
        if !bool::from(
            server_proof(self.service, &record)?
                .as_bytes()
                .ct_eq(text(args, "age_proof")?.as_bytes()),
        ) {
            return self.fail(&record, "age_possession_failed");
        }
        let statement = wire(
            &serde_json::json!({"subject_id":self.subject.resource_id,"challenge_id":record.id,
            "age_proof":field(args,"age_proof")?,"external_ciphertexts_migrated":field(args,"external_ciphertexts_migrated")?,"request_id":text(self.request,"request_id")?}),
        )?;
        match verify(
            &unb64(text(&record.challenge, "public_key")?, 32)?,
            statement.canonical()?.as_bytes(),
            &decode(field(args, "migration_ack")?)?,
            "custodial-upgrade-finish",
        ) {
            Ok(()) => (),
            Err(Error("invalid_signature")) => return self.fail(&record, "migration_ack_failed"),
            Err(error) => return Err(error),
        }
        let inventory = self.store.owned_age_inventory(&record.subject)?;
        require(
            wire(&inventory)? == *field(&record.details, "age_inventory")?,
            "custodial_inventory_changed",
        )?;
        let policy = self.store.latest_recovery_policy(&record.subject)?;
        let opted = optional(&policy, "opted_in")?
            .map(Json::as_bool)
            .transpose()?
            .unwrap_or(false);
        let external = field(args, "external_ciphertexts_migrated")?.as_bool()?;
        if !inventory.is_empty() || opted || !external {
            activate(self.store, &record, self.now)?;
            let mut updated = record.clone();
            updated.status = "pending_rewrap".into();
            updated.ephemeral = None;
            set(
                &mut updated.details,
                "external_ciphertexts_migrated",
                wire(&external)?,
            )?;
            set(
                &mut updated.details,
                "migration_ack",
                field(args, "migration_ack")?.clone(),
            )?;
            set(
                &mut updated.details,
                "age_possession_digest",
                Json::string(hash(field(args, "age_proof")?)?),
            )?;
            self.store.replace_custodial_upgrade(&record, &updated)?;
            return Ok(Outcome::Complete(output(
                serde_json::json!({"status":"pending_rewrap","challenge_id":record.id,
                "server_tracked_age_revisions":inventory.len(),"age_inventory_digest":hash(&wire(&inventory)?)?,"age_inventory":inventory,
                "recovery_policy_requires_review":opted,"external_migration":if external {"owner_declared_only"} else {"owner_not_confirmed"},
                "token_remains_active":true,"vault_remains_active":true}),
                vec![],
            )?));
        }
        self.switch_identity(&record, record.clone(), false)
            .map(Outcome::Complete)
    }
    fn transition(&mut self, args: &Json) -> Result<Outcome> {
        let record = self.migration(args, false)?;
        let public = unb64(text(&record.challenge, "public_key")?, 32)?;
        verify(
            &public,
            migration::stage_statement(&record.subject, args, text(self.request, "request_id")?)?
                .canonical()?
                .as_bytes(),
            &decode(field(args, "migration_ack")?)?,
            "custodial-decision-v1",
        )?;
        let action = text(args, "action")?;
        let expected = if record.status == "pending" {
            require(
                action == "refresh" && self.now < record.expires_at,
                "custodial_upgrade_not_pending_rewrap",
            )?;
            server_proof(self.service, &record)?
        } else {
            text(&record.details, "age_possession_digest")?.into()
        };
        let presented = if record.status == "pending" {
            text(args, "age_proof")?.into()
        } else {
            hash(field(args, "age_proof")?)?
        };
        require(
            bool::from(expected.as_bytes().ct_eq(presented.as_bytes())),
            "age_possession_failed",
        )?;
        let state = snapshot(self.store, &record, Some(self.now))?;
        state.require_observation(args)?;
        let mut updated = record.clone();
        match action {
            "refresh" => {
                let delta = field(&state.data, "delta")?;
                let added: Vec<HistoryItem> = decode(field(delta, "added")?)?;
                require(
                    !added.is_empty() || !field(delta, "missing")?.as_array()?.is_empty(),
                    "custodial_inventory_unchanged",
                )?;
                let mut frozen: Vec<HistoryItem> =
                    decode(field(&updated.details, "age_inventory")?)?;
                frozen.extend(added.iter().cloned());
                frozen.sort_by(|a, b| (&a.id, &a.revision).cmp(&(&b.id, &b.revision)));
                set(&mut updated.details, "age_inventory", wire(&frozen)?)?;
                let version: u64 = decode(field(&state.data, "inventory_version")?)?;
                let version = version.checked_add(1).ok_or(Error("invalid_generation"))?;
                set(&mut updated.details, "inventory_version", wire(&version)?)?;
                let mut keys = map(&updated.details, "source_keys")?;
                let mut mappings = map(&updated.details, "rewrap_mappings")?;
                for item in added {
                    let key = self
                        .store
                        .setting(&format!("keystore_key:{}", item.revision))?
                        .unwrap_or(field(&record.details, "old_encryption_key_id")?.clone());
                    if key == *field(&record.details, "new_encryption_key_id")? {
                        mappings.insert(item.revision.clone(),wire(&serde_json::json!({"old":item,"new":item,"old_key_id":key,"new_key_id":key,
                            "recipient":field(&record.challenge,"encryption_recipient")?,"copy_kind":"new_key_write","created_at":self.now}))?);
                    }
                    keys.insert(item.revision, key);
                }
                set(&mut updated.details, "source_keys", Json::from_object(keys))?;
                set(
                    &mut updated.details,
                    "rewrap_mappings",
                    Json::from_object(mappings),
                )?;
                let mut delta = delta.clone();
                set(&mut delta, "version", wire(&version)?)?;
                set(&mut updated.details, "last_delta", delta)?;
                remove(&mut updated.details, "resolution")?;
            }
            "recovery_envelope" => {
                state.unchanged()?;
                require(
                    record.status != "pending",
                    "custodial_upgrade_not_pending_rewrap",
                )?;
                if optional(&record.details, "recovery_envelope")?.is_none_or(Json::is_null) {
                    return self
                        .recovery_envelope(record)
                        .map(|plan| Outcome::Create(Box::new(plan)));
                }
            }
            "finalize" => {
                let resolution = text(args, "resolution")?;
                if resolution == "verified"
                    || !field(field(&state.data, "delta")?, "added")?
                        .as_array()?
                        .is_empty()
                {
                    state.unchanged()?;
                }
                require(
                    !(record.status == "completed"
                        && field(
                            field(&state.data, "retirement")?,
                            "online_encryption_key_deleted",
                        )?
                        .as_bool()?),
                    "custodial_upgrade_already_finalized",
                )?;
                require(
                    optional(args, "reviewed_policy_version")?
                        == optional(&state.data, "recovery_policy_version")?,
                    "custodial_recovery_policy_review_required",
                )?;
                let mut losses: Vec<String> = optional(args, "loss_revisions")?
                    .map(decode)
                    .transpose()?
                    .unwrap_or_default();
                losses.sort();
                let unresolved: Vec<String> = decode(field(&state.data, "unresolved_revisions")?)?;
                if resolution == "verified" {
                    require(
                        field(&state.data, "history_recoverable")?.as_bool()?,
                        "custodial_history_ack_required",
                    )?;
                    require(losses.is_empty(), "custodial_loss_scope_mismatch")?;
                } else {
                    require(
                        matches!(resolution, "accept_loss" | "retain_decrypt")
                            && optional(args, "reason")?
                                .is_some_and(|v| v.as_str().is_ok_and(|s| !s.is_empty())),
                        "custodial_resolution_required",
                    )?;
                    require(
                        losses == unresolved
                            && (resolution != "accept_loss" || !unresolved.is_empty()),
                        "custodial_loss_scope_mismatch",
                    )?;
                }
                let external = field(args, "external_ciphertexts_migrated")?.as_bool()?;
                require(
                    resolution == "retain_decrypt" || external,
                    "custodial_external_history_unresolved",
                )?;
                set(
                    &mut updated.details,
                    "resolution",
                    wire(
                        &serde_json::json!({"decision":resolution,"revisions":unresolved,
                    "reason":optional(args,"reason")?,"inventory_digest":field(&state.data,"inventory_digest")?,"results_digest":field(&state.data,"results_digest")?,
                    "request_id":text(self.request,"request_id")?,"signature":field(args,"migration_ack")?,"reviewed_policy_version":field(args,"reviewed_policy_version")?,
                    "external_migration":if external {"owner_declared_unverified"}else{"unknown_retained"}}),
                    )?,
                )?;
                set(
                    &mut updated.details,
                    "external_ciphertexts_migrated",
                    wire(&external)?,
                )?;
                return self
                    .switch_identity(&record, updated, resolution == "retain_decrypt")
                    .map(Outcome::Complete);
            }
            _ => return Err(Error("invalid_custodial_transition")),
        }
        self.store.replace_custodial_upgrade(&record, &updated)?;
        self.audit(&format!("identity.custodial_{action}"),&wire(&serde_json::json!({"challenge_id":record.id,
            "before_digest":hash(&record.details)?,"after_digest":hash(&updated.details)?,"decision_signature":field(args,"migration_ack")?}))?)?;
        Ok(Outcome::Complete(wire(
            &serde_json::json!({"resources":[],"data":snapshot(self.store,&updated,Some(self.now))?.data}),
        )?))
    }
    fn switch_identity(
        &mut self,
        before: &Upgrade,
        mut record: Upgrade,
        retain: bool,
    ) -> Result<Json> {
        let uid = record.subject.clone();
        let key = text(&record.details, "new_identity_key_id")?.to_owned();
        let mut completed = if self.subject.kind != SubjectKind::Registered {
            self.token()?;
            let endorsement=vault::open_signer(self.store,self.service.token_secret,&uid,self.now)?.sign(wire(&serde_json::json!({
                "subject_id":uid,"challenge_id":record.id,"old_identity_key_id":field(&record.details,"old_identity_key_id")?,
                "new_identity_key_id":key,"new_encryption_key_id":field(&record.details,"new_encryption_key_id")?,
                "inventory_digest":field(&snapshot(self.store,&record,None)?.data,"inventory_digest")?
            }))?.canonical()?.as_bytes(),"custodial-upgrade")?;
            let mut revoked = Vec::new();
            for mut old in self.store.identity_credentials(&uid)? {
                if old.revoked_at.is_none()
                    && matches!(old.kind, CredentialKind::Token | CredentialKind::SigningKey)
                {
                    old.revoked_at = Some(self.now);
                    self.store
                        .save_credential(&old, self.subject.auth_version)?;
                    revoked.push(old.id);
                }
            }
            let public = unb64(text(&record.challenge, "public_key")?, 32)?;
            self.store.save_credential(
                &Credential {
                    id: key.clone(),
                    subject_id: uid.clone(),
                    kind: CredentialKind::SigningKey,
                    verifier: Verifier(public.clone()),
                    ceiling: self.service.primary_ceiling.to_vec(),
                    not_before: self.now,
                    expires_at: None,
                    revoked_at: None,
                    source_credential_id: None,
                },
                self.subject.auth_version,
            )?;
            for mut old in self.store.identity_keys(&uid)? {
                if old.primary {
                    old.primary = false;
                    old.retired_at = Some(self.now);
                    self.store.save_identity_key(&old)?;
                }
            }
            self.store.save_identity_key(&IdentityKey {
                key_id: key.clone(),
                subject: uid.clone(),
                public_key: Verifier(public),
                created_at: self.now,
                retired_at: None,
                primary: true,
            })?;
            activate(self.store, &record, self.now)?;
            let mut user = self.store.record(Record::Resource, &uid)?;
            let (name, _) = check_handle_change(
                self.store,
                &user,
                text(&record.challenge, "handle")?,
                self.now,
                true,
            )?;
            let generation: i64 = decode(field(&user, "generation")?)?;
            set(&mut user, "name", Json::string(name))?;
            set(
                &mut user,
                "generation",
                wire(
                    &generation
                        .checked_add(1)
                        .ok_or(Error("invalid_generation"))?,
                )?,
            )?;
            set(&mut user, "modified_at", wire(&self.now)?)?;
            set(&mut user, "modified_by", wire(&self.principal.actor())?)?;
            self.store.replace_identity_resource(&user, generation)?;
            let mut subject = bump(&self.subject)?;
            subject.kind = SubjectKind::Registered;
            self.store
                .update_subject(&subject, expected(&self.subject)?)?;
            let certificate=self.service.issue(self.store,self.principal,self.request,Issuance {subject:&uid,key:&key,grants:None,ttl:None,authority_source:None,
                audit_source:Some(wire(&serde_json::json!({"kind":"custodial_upgrade","challenge_id":record.id,"new_key_id":key}))?),delegation:None},self.now)?;
            self.audit("identity.custodial_tokens_revoked",&wire(&serde_json::json!({"challenge_id":record.id,"credentials":revoked,"new_key_id":key,"old_key_endorsement":endorsement}))?)?;
            wire(
                &serde_json::json!({"status":"completed","subject_id":uid,"identity_key_id":key,
                "encryption_key_id":field(&record.details,"new_encryption_key_id")?,"encryption_recipient":field(&record.challenge,"encryption_recipient")?,
                "certificate_id":certificate.resource_id,"previous_credential":self.principal.credential_id(),"credential_id":key,"signature_source":"custodial"}),
            )?
        } else {
            field(&record.details, "completed_result")?.clone()
        };
        self.store.retire_custodial_vault(&uid, retain, self.now)?;
        record.status = "completed".into();
        record.ephemeral = None;
        let current = snapshot(self.store, &record, Some(self.now))?;
        for field_name in [
            "phase",
            "history_recoverable",
            "verification_scope",
            "retirement",
        ] {
            set(
                &mut completed,
                field_name,
                field(&current.data, field_name)?.clone(),
            )?;
        }
        for (name, value) in [
            ("completion_scope", Json::string("identity_switch_only")),
            (
                "server_tracked_age_revisions",
                wire(&field(&record.details, "age_inventory")?.as_array()?.len())?,
            ),
            ("vault_destroyed", wire(&!retain)?),
            (
                "external_migration",
                Json::string("owner_declared_unverified"),
            ),
            ("external_ciphertexts_verified", wire(&false)?),
            (
                "resolution",
                optional(&record.details, "resolution")?
                    .cloned()
                    .unwrap_or(wire(&Option::<u8>::None)?),
            ),
            ("challenge_id", Json::string(&record.id)),
            (
                "finalization_request_id",
                Json::string(text(self.request, "request_id")?),
            ),
        ] {
            set(&mut completed, name, value)?;
        }
        set(&mut record.details, "completed_result", completed.clone())?;
        self.store.replace_custodial_upgrade(before, &record)?;
        self.audit("identity.custodial_online_key_retirement",&wire(&serde_json::json!({"challenge_id":record.id,
            "old_identity_key_id":field(&record.details,"old_identity_key_id")?,"old_encryption_key_id":field(&record.details,"old_encryption_key_id")?,
            "retirement":field(&current.data,"retirement")?,"resolution":optional(&record.details,"resolution")?,"before_digest":hash(&before.details)?}))?)?;
        wire(&serde_json::json!({"resources":[{"id":uid}],"data":completed}))
    }
    fn plan(
        &self,
        record: Upgrade,
        name: String,
        ciphertext: Vec<u8>,
        operation: ContentOperation,
    ) -> Result<PreparedContent> {
        let parent = self
            .store
            .identity_child(&record.subject, "keystore")?
            .ok_or(Error("keystore_missing"))?;
        Ok(PreparedContent {
            record,
            parent,
            name,
            ciphertext,
            operation,
            request_id: text(self.request, "request_id")?.into(),
            payload_digest: text(self.request, "payload_digest")?.into(),
            actor: self
                .principal
                .actor()
                .ok_or(Error("identity_owner_required"))?
                .into(),
            credential: self
                .principal
                .credential_id()
                .ok_or(Error("identity_owner_required"))?
                .into(),
            auth_version: self.subject.auth_version,
            now: self.now,
        })
    }
    fn recovery_envelope(&self, record: Upgrade) -> Result<PreparedContent> {
        let old = text(&record.details, "old_encryption_key_id")?;
        let recipient = text(&record.challenge, "encryption_recipient")?;
        let keys = self.store.encryption_keys(&record.subject)?;
        let old_key = keys
            .iter()
            .find(|key| {
                key.key_id == old
                    && key.subject == record.subject
                    && key.retired_at.is_some()
                    && !key.primary
            })
            .ok_or(Error("custodial_recovery_requires_retired_owned_key"))?;
        require(
            self.store
                .vault_record(&record.subject)?
                .is_some_and(|v| v.encryption_key_id == old),
            "custodial_recovery_key_mismatch",
        )?;
        require(
            keys.iter()
                .any(|key| key.primary && key.recipient == recipient && key.key_id != old),
            "custodial_recovery_recipient_mismatch",
        )?;
        let identity =
            vault::open_age_identity(self.store, self.service.token_secret, &record.subject)?;
        // Field declaration order is canonical-v1 lexical order; serialization
        // streams the private identity directly into a zeroizing buffer.
        #[derive(Serialize)]
        struct Payload<'a> {
            age_identity: &'a str,
            challenge_id: &'a str,
            encryption_key_id: &'a str,
            encryption_recipient: &'a str,
            format: &'a str,
            purpose: &'a str,
            subject_id: &'a str,
        }
        let payload = Payload {
            age_identity: &identity,
            challenge_id: &record.id,
            encryption_key_id: old,
            encryption_recipient: &old_key.recipient,
            format: "msg-custodial-retired-key-v1",
            purpose: "retired-encryption-subkey-recovery",
            subject_id: &record.subject,
        };
        let mut plaintext = Zeroizing::new(Vec::new());
        serde_json::to_writer(&mut *plaintext, &payload)
            .map_err(|_| Error("custodial_recovery_encryption_failed"))?;
        let ciphertext = age_file::encrypt(recipient, &plaintext)?;
        let name = format!("retired-key-{}.age", &hash(&wire(&record.id)?)?[7..31]);
        self.plan(record, name, ciphertext, ContentOperation::RecoveryEnvelope)
    }
    fn rewrap(
        &mut self,
        args: &Json,
        current_only: bool,
        read: &dyn Fn(&Json, u64) -> Result<Vec<u8>>,
    ) -> Result<PreparedContent> {
        let record = self.migration(args, true)?;
        snapshot(self.store, &record, Some(self.now))?.unchanged()?;
        require(
            text(&record.challenge, "encryption_recipient")? == text(args, "new_recipient")?
                && text(&record.details, "old_encryption_key_id")?
                    == text(args, "old_encryption_key_id")?,
            "custodial_rewrap_key_mismatch",
        )?;
        require(
            self.store.vault_record(&record.subject)?.is_some_and(|v| {
                v.encryption_key_id == text(args, "old_encryption_key_id").unwrap_or("")
                    && matches!(v.status.as_str(), "active" | "decrypt_only")
            }),
            "custodial_rewrap_unknown_old_key",
        )?;
        activate(self.store, &record, self.now)?;
        let reference: ResourceRef = decode(field(args, "ciphertext_ref")?)?;
        require(
            reference.revision.is_some(),
            "custodial_rewrap_revision_required",
        )?;
        let resource = self.store.record(Record::Resource, &reference.id)?;
        let folder = self
            .store
            .identity_child(&record.subject, "keystore")?
            .ok_or(Error("custodial_rewrap_not_owned"))?;
        require(
            text(&resource, "parent")? == folder
                && text(&resource, "type")? == "keystore"
                && text(&resource, "owner")? == record.subject
                && text(&resource, "state")? != "purged",
            "custodial_rewrap_not_owned",
        )?;
        if current_only {
            lifecycle::assert_generation(
                self.request,
                &reference.id,
                decode(field(&resource, "generation")?)?,
            )?;
            require(
                optional(&resource, "revision")?.and_then(|v| v.as_str().ok())
                    == reference.revision.as_deref(),
                "revision_conflict",
            )?;
        }
        self.read_access(&reference.id)?;
        let revision = self.store.identity_revision(&reference)?;
        let content = field(&revision, "content")?;
        let old = HistoryItem {
            id: reference.id.clone(),
            revision: text(&revision, "id")?.into(),
            ciphertext_digest: text(content, "digest")?.into(),
        };
        let frozen: Vec<HistoryItem> = decode(field(&record.details, "age_inventory")?)?;
        require(
            frozen.contains(&old),
            "custodial_rewrap_not_in_frozen_inventory",
        )?;
        require(
            text(&keyed_source(&old, &record.details)?, "key_id")?
                == text(args, "old_encryption_key_id")?,
            "custodial_rewrap_key_mismatch",
        )?;
        require(
            !map(&record.details, "rewrap_mappings")?.contains_key(&old.revision),
            "custodial_rewrap_already_mapped",
        )?;
        require(
            self.store
                .setting(&format!("keystore_format:{}", old.revision))?
                .as_ref()
                .and_then(|v| v.as_str().ok())
                == Some("age")
                && decode::<u64>(field(content, "size")?)? <= 1_048_576,
            "custodial_rewrap_age_required",
        )?;
        let old_ciphertext = read(content, 1_048_576)?;
        require(
            digest(&old_ciphertext) == old.ciphertext_digest,
            "blob_digest_mismatch",
        )?;
        let identity =
            vault::open_age_identity(self.store, self.service.token_secret, &record.subject)?;
        let ciphertext =
            age_file::rewrap(&identity, text(args, "new_recipient")?, &old_ciphertext)?;
        let name = format!(
            "migration-{}.age",
            &hash(&wire(&(&record.id, &old.revision))?)?[7..31]
        );
        self.plan(record, name, ciphertext, ContentOperation::Rewrap(old))
    }
    fn get(&mut self, args: &Json) -> Result<Json> {
        let record = self.migration(args, true)?;
        snapshot(self.store, &record, Some(self.now))?;
        let frozen: Vec<HistoryItem> = decode(field(&record.details, "age_inventory")?)?;
        let source = frozen
            .iter()
            .find(|item| item.revision == text(args, "old_revision").unwrap_or(""))
            .ok_or(Error("custodial_migration_not_found"))?;
        let mappings = map(&record.details, "rewrap_mappings")?;
        let mapping = mappings.get(&source.revision);
        let envelope =
            optional(&record.details, "recovery_envelope")?.filter(|value| !value.is_null());
        let target: HistoryItem = decode(if let Some(mapping) = mapping {
            field(mapping, "new")?
        } else {
            field(
                envelope.ok_or(Error("custodial_migration_not_found"))?,
                "ciphertext",
            )?
        })?;
        for item in [source, &target] {
            self.read_access(&item.id)?;
            let revision = self.store.identity_revision(&ResourceRef {
                id: item.id.clone(),
                revision: Some(item.revision.clone()),
            })?;
            require(
                text(field(&revision, "content")?, "digest")? == item.ciphertext_digest,
                "custodial_migration_digest_mismatch",
            )?;
        }
        let acks = map(
            &record.details,
            if mapping.is_some() {
                "rewrap_acks"
            } else {
                "recovery_acks"
            },
        )?;
        wire(
            &serde_json::json!({"resources":[],"output":{"id":target.id,"revision":target.revision},"data":{
            "status":record.status,"source":source,"mapped":mapping.map(|value|field(value,"new")).transpose()?,
            "recovery_envelope":if mapping.is_none(){envelope}else{None},"method":if mapping.is_some(){"rewrap"}else{"compatibility_recovery"},
            "recipient":field(&record.challenge,"encryption_recipient")?,"client_ack":acks.get(&source.revision),"old_revision_unchanged":true}}),
        )
    }
    fn ack(&mut self, args: &Json) -> Result<Json> {
        let before = self.migration(args, true)?;
        let state = snapshot(self.store, &before, Some(self.now))?;
        state.unchanged()?;
        let mut record = before.clone();
        let frozen: Vec<HistoryItem> = decode(field(&record.details, "age_inventory")?)?;
        let source = frozen
            .iter()
            .find(|item| item.revision == text(args, "old_revision").unwrap_or(""))
            .ok_or(Error("custodial_rewrap_not_in_frozen_inventory"))?;
        let method = optional(args, "method")?
            .map(Json::as_str)
            .transpose()?
            .unwrap_or("rewrap");
        let mappings = map(&record.details, "rewrap_mappings")?;
        let committed = if method == "rewrap" {
            keyed_target(
                mappings
                    .get(&source.revision)
                    .ok_or(Error("custodial_rewrap_mapping_mismatch"))?,
                &record.details,
            )?
        } else {
            require(
                method == "compatibility_recovery",
                "custodial_recovery_key_mismatch",
            )?;
            let envelope = field(&record.details, "recovery_envelope")?;
            require(
                text(&keyed_source(source, &record.details)?, "key_id")?
                    == text(envelope, "old_key_id")?,
                "custodial_recovery_key_mismatch",
            )?;
            envelope.clone()
        };
        let target: HistoryItem = if method == "rewrap" {
            decode(field(
                mappings
                    .get(&source.revision)
                    .ok_or(Error("custodial_rewrap_mapping_mismatch"))?,
                "new",
            )?)?
        } else {
            decode(field(&committed, "ciphertext")?)?
        };
        require(
            target.revision == text(args, "new_revision")?
                && target.ciphertext_digest == text(args, "ciphertext_digest")?,
            "custodial_rewrap_mapping_mismatch",
        )?;
        let revision = self.store.identity_revision(&ResourceRef {
            id: target.id.clone(),
            revision: Some(target.revision.clone()),
        })?;
        require(
            text(field(&revision, "content")?, "digest")? == target.ciphertext_digest,
            "custodial_rewrap_mapping_mismatch",
        )?;
        let plaintext = text(args, "plaintext_digest")?;
        require(
            plaintext.len() == 71
                && plaintext.starts_with("sha256:")
                && plaintext.as_bytes()[7..]
                    .iter()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b)),
            "invalid_plaintext_digest",
        )?;
        let scoped = optional(args, "inventory_digest")?.is_some();
        let (statement, purpose) = if scoped {
            require(
                optional(args, "inventory_digest")? == optional(&state.data, "inventory_digest")?,
                "custodial_migration_preview_stale",
            )?;
            (
                Acknowledgement {
                    subject: &record.subject,
                    challenge: &record.id,
                    inventory_digest: text(&state.data, "inventory_digest")?,
                    source: &keyed_source(source, &record.details)?,
                    target: &committed,
                    plaintext_digest: plaintext,
                    request_id: text(self.request, "request_id")?,
                    method,
                }
                .statement()?,
                "custodial-history-ack-v1",
            )
        } else {
            require(method == "rewrap", "custodial_inventory_ack_required")?;
            (
                wire(
                    &serde_json::json!({"subject_id":record.subject,"challenge_id":record.id,"old":source,"new":target,
                "plaintext_digest":plaintext,"request_id":text(self.request,"request_id")?}),
                )?,
                "custodial-rewrap-ack",
            )
        };
        verify(
            &unb64(text(&record.challenge, "public_key")?, 32)?,
            statement.canonical()?.as_bytes(),
            &decode(field(args, "decryption_ack")?)?,
            purpose,
        )?;
        let bucket = if method == "rewrap" {
            "rewrap_acks"
        } else {
            "recovery_acks"
        };
        let mut acks = map(&record.details, bucket)?;
        require(
            acks.get(&source.revision).is_none_or(|previous| {
                optional(previous, "inventory_digest").ok().flatten()
                    != optional(args, "inventory_digest").ok().flatten()
            }),
            "custodial_rewrap_already_acked",
        )?;
        let mut ack = wire(
            &serde_json::json!({"old_revision":source.revision,"new_revision":target.revision,"ciphertext_digest":target.ciphertext_digest,
            "plaintext_digest":plaintext,"signature":field(args,"decryption_ack")?,"request_id":text(self.request,"request_id")?,"verified_at":self.now,"method":method}),
        )?;
        if scoped {
            set(
                &mut ack,
                "inventory_digest",
                field(&state.data, "inventory_digest")?.clone(),
            )?;
        }
        acks.insert(source.revision.clone(), ack.clone());
        set(&mut record.details, bucket, Json::from_object(acks))?;
        self.store.replace_custodial_upgrade(&before, &record)?;
        self.audit("identity.custodial_rewrap_ack",&wire(&serde_json::json!({"challenge_id":record.id,"ack":ack,"verification_scope":"enumerated_revision_only"}))?)?;
        let current = snapshot(self.store, &record, None)?;
        output(
            serde_json::json!({"challenge_id":record.id,"old_revision":source.revision,"new_revision":target.revision,
            "client_decryption_acked":true,"inventory_bound":scoped,"vault_remains_active":field(&current.data,"vault_remains_active")?,"finalize_ready":field(&current.data,"finalize_ready")?}),
            vec![],
        )
    }
}

pub fn finish_content<S: CustodialStore>(
    store: &mut S,
    principal: &Principal,
    request: &Json,
    plan: PreparedContent,
    reference: &ResourceRef,
    now: Timestamp,
) -> Result<Json> {
    store.require_live()?;
    require(
        principal.actor() == Some(plan.actor.as_str())
            && principal.subject() == Some(plan.record.subject.as_str())
            && principal.credential_id() == Some(plan.credential.as_str())
            && text(request, "request_id")? == plan.request_id
            && text(request, "payload_digest")? == plan.payload_digest
            && now == plan.now
            && store.subject(&plan.record.subject)?.auth_version == plan.auth_version
            && store.credential(&plan.credential)?.current_at(now),
        "custodial_content_binding_mismatch",
    )?;
    let revision = store.identity_revision(reference)?;
    let resource = store.record(Record::Resource, &reference.id)?;
    let ciphertext_digest = digest(&plan.ciphertext);
    require(
        text(&resource, "parent")? == plan.parent
            && reference.revision.as_deref() == Some(text(&revision, "id")?)
            && text(&resource, "revision")? == text(&revision, "id")?
            && decode::<Timestamp>(field(&resource, "created_at")?)? == now
            && decode::<Timestamp>(field(&revision, "created_at")?)? == now
            && text(&resource, "name")? == plan.name
            && text(&resource, "type")? == "keystore"
            && text(&resource, "owner")? == plan.record.subject
            && text(&resource, "state")? == "active"
            && text(&revision, "actor")? == plan.actor
            && text(&revision, "subject")? == plan.record.subject
            && text(field(&revision, "content")?, "digest")? == ciphertext_digest
            && decode::<u64>(field(field(&revision, "content")?, "size")?)?
                == plan.ciphertext.len() as u64,
        "custodial_content_binding_mismatch",
    )?;
    let target = HistoryItem {
        id: reference.id.clone(),
        revision: text(&revision, "id")?.into(),
        ciphertext_digest,
    };
    store.set_identity_setting(
        &format!("keystore_format:{}", target.revision),
        &Json::string("age"),
    )?;
    store.set_identity_setting(
        &format!("keystore_key:{}", target.revision),
        field(&plan.record.details, "new_encryption_key_id")?,
    )?;
    let mut record = plan.record.clone();
    let (event, data, result) = match plan.operation {
        ContentOperation::RecoveryEnvelope => {
            let envelope = wire(
                &serde_json::json!({"subject_id":record.subject,"challenge_id":record.id,
                "old_key_id":field(&record.details,"old_encryption_key_id")?,"new_key_id":field(&record.details,"new_encryption_key_id")?,
                "recipient":field(&record.challenge,"encryption_recipient")?,"purpose":"retired-encryption-subkey-recovery","method":"compatibility_recovery","ciphertext":target}),
            )?;
            set(&mut record.details, "recovery_envelope", envelope)?;
            let data = wire(
                &serde_json::json!({"challenge_id":record.id,"before_digest":hash(&plan.record.details)?,"after_digest":hash(&record.details)?,
                "decision_signature":field(field(request,"arguments")?,"migration_ack")?}),
            )?;
            let result = wire(
                &serde_json::json!({"resources":[],"data":snapshot(store,&record,Some(now))?.data}),
            )?;
            ("identity.custodial_recovery_envelope", data, result)
        }
        ContentOperation::Rewrap(old) => {
            let mapping = wire(
                &serde_json::json!({"old":old,"new":target,"recipient":field(&record.challenge,"encryption_recipient")?,
                "old_key_id":field(&record.details,"old_encryption_key_id")?,"new_key_id":field(&record.details,"new_encryption_key_id")?,"created_at":now,"copy_kind":"historical_revision"}),
            )?;
            let mut mappings = map(&record.details, "rewrap_mappings")?;
            mappings.insert(old.revision, mapping.clone());
            set(
                &mut record.details,
                "rewrap_mappings",
                Json::from_object(mappings),
            )?;
            let data = wire(
                &serde_json::json!({"challenge_id":record.id,"mapping":mapping,"old_revision_unchanged":true,"plaintext_exposed":false}),
            )?;
            let active = store
                .vault_record(&record.subject)?
                .is_some_and(|v| v.status == "active");
            let result = output(
                serde_json::json!({"mapping":mapping,"old_revision_unchanged":true,"client_decryption_acked":false,"client_decryption_verified":false,
                "vault_remains_active":active,"new_revision":target.revision,"new_recipient":field(&record.challenge,"encryption_recipient")?,"ciphertext_digest":target.ciphertext_digest}),
                vec![ResourceRef {
                    id: target.id,
                    revision: Some(target.revision),
                }],
            )?;
            (text(request, "operation")?, data, result)
        }
    };
    store.replace_custodial_upgrade(&plan.record, &record)?;
    store.append_identity_audit(&wire(&serde_json::json!({"event":{"id":new_id("audit")?,"type":event,"time":now,"request_id":plan.request_id,
        "actor":plan.actor,"subject":record.subject,"resources":[{"id":record.subject}],"data":data},"authority":[{"id":record.subject}],
        "before_digest":null,"after_digest":hash(&data)?,"previous_digest":null,"entry_digest":"","result":"committed"}))?)?;
    Ok(result)
}
