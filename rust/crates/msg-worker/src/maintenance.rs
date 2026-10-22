//! Scheduled retention, due reminders, search rebuilding and conservative GC.
use crate::{
    process, AttemptContext, EffectOutcome, EffectResult, Maintenance, RuntimeContext, WorkerResult,
};
use chrono::Duration as TimeDelta;
use msg_core::{digest, Error, Json, Result};
use msg_identity::{
    authorization::AuthorizationService,
    models::{ResourceRef, Timestamp},
    store::{truthy, AuthorityStore},
    worker_authority::WorkerAuthority,
};
use msg_storage::{
    content::{recovery::ContentTransaction, GitContentStore},
    postgres::{PgSession, Value},
    records::{
        decode, encode, AuditEvent, BlobRef, EffectJob, Event, Resource, ResourceState, Revision,
        TransferChunk,
    },
};
use serde::Serialize;
use std::{
    collections::BTreeSet,
    time::{Duration, SystemTime},
};

const ONLINE_CA: &str = "u_online_ca";
const BATCH: usize = 64;
const PURGE_RECORDS: i64 = 4096;

pub struct MaintenanceService {
    contents: GitContentStore,
    authority: WorkerAuthority,
    authorizer: AuthorizationService,
}
impl MaintenanceService {
    pub fn new(
        contents: GitContentStore,
        authority: WorkerAuthority,
        authorizer: AuthorizationService,
    ) -> Self {
        Self {
            contents,
            authority,
            authorizer,
        }
    }
    pub fn scheduled(
        &mut self,
        action: Maintenance,
        context: &mut RuntimeContext<'_>,
    ) -> WorkerResult<Json> {
        let clock = context.clock();
        context.content_transaction(&self.contents, |unit| {
            run(action, unit, &self.contents, clock()?, true)
        })
    }
    pub fn execute(&mut self, context: &mut AttemptContext<'_, '_>) -> EffectResult<EffectOutcome> {
        let resource_gc = context.job().kind == "gc.resource";
        let action = if resource_gc {
            Maintenance::CollectGarbage
        } else {
            if context.job().operation != "system.maintenance" {
                return Err(Error("unknown_operation").into());
            }
            Maintenance::parse(
                context
                    .job()
                    .arguments
                    .get("action")
                    .ok_or(Error("invalid_job_arguments"))?
                    .as_str()?,
            )?
        };
        let contents = self.contents.clone();
        context.publish_content(&contents, |unit, job, now| {
            let tx = *unit.metadata();
            if resource_gc {
                // The committed tombstone consumed purge authority. Collection
                // is idempotent and cannot relax any other resource's retention.
                if job.operation != "content.purge" {
                    return Err(Error("unknown_operation"));
                }
                let id = job
                    .arguments
                    .get("id")
                    .ok_or(Error("invalid_job_arguments"))?
                    .as_str()?;
                if tx.resource_record(id)?.state != ResourceState::Purged {
                    return Err(Error("purge_not_committed"));
                }
            } else {
                self.authorize(tx, job, now)?;
            }
            let result = run(action, unit, &contents, now, false)?;
            if resource_gc {
                Json::parse(r#"{"code":"cleanup_requested"}"#)
            } else {
                Ok(result)
            }
        })?;
        Ok(EffectOutcome::Published)
    }
    fn authorize(&mut self, tx: &PgSession, job: &EffectJob, now: Timestamp) -> Result<()> {
        let principal =
            self.authority
                .current(&job.id, job.attempts, &mut self.authorizer, tx, now)?;
        if !self.authorizer.has(
            &principal,
            "system.maintenance",
            "system.maintenance@1",
            "r_root",
            tx,
            now,
        )? {
            return Err(Error("capability_required"));
        }
        Ok(())
    }
}
fn time(now: Timestamp) -> String {
    now.0.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()
}
fn hash(value: &impl Serialize) -> Result<String> {
    Ok(digest(encode(value)?.canonical()?.as_bytes()))
}
fn setting_enabled(tx: &PgSession) -> Result<bool> {
    let Some(value) = tx.setting("runtime_config")? else {
        return Ok(true);
    };
    Ok(value
        .as_object()?
        .get("cleanup_enabled")
        .map(truthy)
        .transpose()?
        .unwrap_or(true))
}

fn run(
    action: Maintenance,
    unit: &mut ContentTransaction<'_, &PgSession>,
    contents: &GitContentStore,
    now: Timestamp,
    scheduled: bool,
) -> Result<Json> {
    let tx = *unit.metadata();
    match action {
        Maintenance::CleanupExpired => cleanup(unit, contents, now, scheduled),
        Maintenance::DeliverDueTodos => deliver_due_todos(tx, contents, now),
        Maintenance::RebuildSearch => rebuild(tx, contents),
        Maintenance::CollectGarbage => {
            if scheduled {
                if !setting_enabled(tx)? {
                    return Json::parse(r#"{"skipped":"cleanup_disabled"}"#);
                }
                if let Some(last) = tx.setting("garbage_collection_last")? {
                    if truthy(&last)?
                        && now
                            .0
                            .signed_duration_since(Timestamp::parse(last.as_str()?)?.0)
                            < TimeDelta::hours(1)
                    {
                        return Json::parse(r#"{"skipped":"not_due"}"#);
                    }
                }
            }
            // This is a separate unit from cleanup. A content journal created
            // in this unit makes the physical collector reject the operation.
            let result = collect(tx, contents)?;
            if scheduled {
                tx.set_setting("garbage_collection_last", &encode(&now)?)?;
            }
            Ok(result)
        }
    }
}

/// Key-only pagination keeps individual records below the aggregate query cap.
/// The final SQL parameter is always the exclusive cursor.
fn scan(
    tx: &PgSession,
    sql: &'static str,
    values: &[Value],
    mut visit: impl FnMut(&str) -> Result<()>,
) -> Result<()> {
    let mut cursor = String::new();
    loop {
        let mut args = values.to_vec();
        args.push(cursor.clone().into());
        let rows = tx.rows(sql, &args)?;
        for row in &rows {
            let id = row.text(0)?;
            if id <= cursor.as_str() {
                return Err(Error("storage_record_mismatch"));
            }
            visit(id)?;
            cursor = id.to_owned();
        }
        if rows.len() < BATCH {
            return Ok(());
        }
    }
}
fn release(
    unit: &mut ContentTransaction<'_, &PgSession>,
    contents: &GitContentStore,
    blob: &BlobRef,
    lease: &str,
) -> Result<()> {
    if contents.pinned(blob, lease)? {
        unit.unpin(blob, lease)?;
    }
    Ok(())
}
fn purge(
    unit: &mut ContentTransaction<'_, &PgSession>,
    contents: &GitContentStore,
    resource: &Resource,
    now: Timestamp,
    request: &str,
    reason: &str,
) -> Result<()> {
    let tx = *unit.metadata();
    let mut revisions: Vec<Revision> = Vec::new();
    scan(
        tx,
        "SELECT id FROM revisions WHERE resource_id=$1 AND id>$2 ORDER BY id LIMIT 64",
        &[resource.id.as_str().into()],
        |id| {
            let row = tx
                .one(
                    "SELECT body FROM revisions WHERE id=$1 AND resource_id=$2",
                    &[id.into(), resource.id.as_str().into()],
                )?
                .ok_or(Error("revision_not_found"))?;
            let revision: Revision = decode(&row.json(0)?)?;
            if revision.id != id || revision.resource_id != resource.id {
                return Err(Error("storage_record_mismatch"));
            }
            revisions.push(revision);
            Ok(())
        },
    )?;
    tx.set_setting(
        &format!("purge_record:{}", resource.id),
        &encode(&serde_json::json!({"revisions":revisions,"reason":reason,"time":now}))?,
    )?;
    trim_purge_records(tx)?;
    let mut changed = resource.clone();
    changed.state = ResourceState::Purged;
    changed.revision = None;
    changed.generation = changed
        .generation
        .checked_add(1)
        .ok_or(Error("invalid_generation"))?;
    changed.modified_at = now;
    changed.modified_by = ONLINE_CA.into();
    tx.replace_resource(&changed, resource.generation)?;
    for revision in &revisions {
        release(unit, contents, &revision.content, &revision.id)?;
    }
    tx.execute(
        "DELETE FROM revisions WHERE resource_id=$1",
        &[resource.id.as_str().into()],
    )?;
    tx.execute(
        "DELETE FROM projections WHERE resource_id=$1",
        &[resource.id.as_str().into()],
    )?;
    tx.append_audit(&AuditEvent {
        event: Event {
            id: process::id("audit")?,
            event_type: "lifecycle.purge".into(),
            time: now,
            request_id: request.into(),
            actor: Some(ONLINE_CA.into()),
            subject: Some(ONLINE_CA.into()),
            resources: vec![ResourceRef {
                id: resource.id.clone(),
                revision: None,
            }],
            data: encode(&serde_json::json!({"reason":reason,"system_policy":true}))?
                .into_object()?,
        },
        authority: vec![],
        before_digest: Some(hash(resource)?),
        after_digest: Some(hash(&changed)?),
        previous_digest: None,
        entry_digest: String::new(),
        result: "purged".into(),
    })?;
    Ok(())
}
fn trim_purge_records(tx: &PgSession) -> Result<()> {
    let count = tx
        .one(
            "SELECT count(*) FROM settings WHERE key LIKE 'purge_record:%'",
            &[],
        )?
        .ok_or(Error("storage_record_mismatch"))?
        .integer(0)?;
    if count <= PURGE_RECORDS {
        return Ok(());
    }
    // Validate records before ranking them; malformed metadata is not a reason
    // to delete evidence. SQL only handles the already-validated timestamp key.
    scan(
        tx,
        "SELECT key FROM settings WHERE key LIKE 'purge_record:%' AND key>$1 ORDER BY key LIMIT 64",
        &[],
        |key| {
            let value = tx.setting(key)?.ok_or(Error("storage_record_mismatch"))?;
            if let Some(stamp) = value.as_object()?.get("time") {
                if truthy(stamp)? {
                    stamp.as_str()?;
                }
            }
            Ok(())
        },
    )?;
    tx.execute("DELETE FROM settings WHERE key IN (SELECT key FROM settings WHERE key LIKE 'purge_record:%' ORDER BY COALESCE(value::jsonb->>'time',''),key LIMIT $1)",&[(count-PURGE_RECORDS).into()])?;
    Ok(())
}
#[derive(Default, Serialize)]
struct Cleanup {
    expired_resources: u64,
    expired_transfers: u64,
    expired_query_sources: u64,
    expired_custodial_upgrades: u64,
}
fn policy_seconds(value: &Json) -> Result<i64> {
    value
        .as_integer()?
        .parse::<i64>()
        .ok()
        .filter(|n| *n >= 0)
        .ok_or(Error("invalid_retention_policy"))
}
fn cleanup(
    unit: &mut ContentTransaction<'_, &PgSession>,
    contents: &GitContentStore,
    now: Timestamp,
    scheduled: bool,
) -> Result<Json> {
    let tx = *unit.metadata();
    let mut counts = Cleanup::default();
    if !setting_enabled(tx)? {
        return encode(&counts);
    }
    let local = now
        .0
        .checked_add_signed(TimeDelta::hours(8))
        .ok_or(Error("invalid_datetime"))?;
    tx.execute(
        "DELETE FROM post_view_days WHERE day<$1",
        &[local.format("%Y-%m-%d").to_string().into()],
    )?;
    scan(
        tx,
        "SELECT key FROM settings WHERE key LIKE 'policy:%' AND key>$1 ORDER BY key LIMIT 64",
        &[],
        |key| {
            let policy = tx.setting(key)?.ok_or(Error("storage_record_mismatch"))?;
            let policy = policy.as_object()?;
            let Some(retention) = policy.get("retention_seconds") else {
                return Ok(());
            };
            let root = key
                .strip_prefix("policy:")
                .ok_or(Error("storage_record_mismatch"))?;
            let interval = policy
                .get("cleanup_interval_seconds")
                .map(policy_seconds)
                .transpose()?
                .unwrap_or(3600);
            if scheduled {
                if let Some(last) = tx.setting(&format!("cleanup_last:{root}"))? {
                    if truthy(&last)?
                        && now
                            .0
                            .signed_duration_since(Timestamp::parse(last.as_str()?)?.0)
                            .num_seconds()
                            < interval
                    {
                        return Ok(());
                    }
                }
            }
            let cutoff = now
                .0
                .checked_sub_signed(
                    TimeDelta::try_seconds(policy_seconds(retention)?)
                        .ok_or(Error("invalid_retention_policy"))?,
                )
                .ok_or(Error("invalid_datetime"))?;
            // Unique safety parents determine traversal. Reply/quote relations never
            // turn a topic's retention rule into authority over another subtree.
            let mut stack = Vec::new();
            scan(
                tx,
                "SELECT id FROM resources WHERE parent=$1 AND id>$2 ORDER BY id LIMIT 64",
                &[root.into()],
                |id| {
                    stack.push((id.to_owned(), false));
                    Ok(())
                },
            )?;
            let mut seen = BTreeSet::new();
            seen.insert(root.to_owned());
            while let Some((id, children_visited)) = stack.pop() {
                if !children_visited {
                    if !seen.insert(id.clone()) {
                        return Err(Error("parent_cycle"));
                    }
                    stack.push((id.clone(), true));
                    scan(
                        tx,
                        "SELECT id FROM resources WHERE parent=$1 AND id>$2 ORDER BY id LIMIT 64",
                        &[id.as_str().into()],
                        |child| {
                            stack.push((child.to_owned(), false));
                            Ok(())
                        },
                    )?;
                    continue;
                }
                let resource = tx.resource_record(&id)?;
                if resource.state == ResourceState::Purged
                    || resource.created_at.0 > cutoff
                    || !matches!(
                        resource.resource_type.as_str(),
                        "post"
                            | "file"
                            | "attachment"
                            | "skill"
                            | "template"
                            | "topic"
                            | "keystore"
                    )
                {
                    continue;
                }
                if tx
                    .one(
                        "SELECT id FROM resources WHERE parent=$1 AND state<>'purged' LIMIT 1",
                        &[id.as_str().into()],
                    )?
                    .is_some()
                {
                    continue;
                }
                purge(
                    unit,
                    contents,
                    &resource,
                    now,
                    &format!("retention:{root}:{}", time(now)),
                    "retention_expired",
                )?;
                counts.expired_resources += 1;
            }
            tx.set_setting(&format!("cleanup_last:{root}"), &encode(&now)?)?;
            Ok(())
        },
    )?;
    scan(
        tx,
        "SELECT id FROM transfers WHERE id>$1 ORDER BY id LIMIT 64",
        &[],
        |id| {
            let transfer = tx.transfer(id)?;
            if transfer.expires_at > now || transfer.state == "expired" {
                return Ok(());
            }
            let mut expired = transfer.clone();
            expired.state = "expired".into();
            expired.generation = expired
                .generation
                .checked_add(1)
                .ok_or(Error("invalid_generation"))?;
            tx.save_transfer(&expired, Some(transfer.generation))?;
            let mut cursor = -1_i64;
            loop {
                let rows = tx.rows("SELECT \"offset\" FROM chunks WHERE transfer_id=$1 AND \"offset\">$2 ORDER BY \"offset\" LIMIT 64",&[id.into(),cursor.into()])?;
                for row in &rows {
                    let offset = row.integer(0)?;
                    let chunk = tx
                        .one(
                            "SELECT body FROM chunks WHERE transfer_id=$1 AND \"offset\"=$2",
                            &[id.into(), offset.into()],
                        )?
                        .ok_or(Error("storage_record_mismatch"))?;
                    let chunk: TransferChunk = decode(&chunk.json(0)?)?;
                    if chunk.transfer_id != id || chunk.offset != offset {
                        return Err(Error("storage_record_mismatch"));
                    }
                    release(unit, contents, &chunk.content, &format!("{id}:{offset}"))?;
                    cursor = offset;
                }
                if rows.len() < BATCH {
                    break;
                }
            }
            if transfer.state == "sealed" && transfer.direction == "upload" {
                let blob = BlobRef {
                    digest: transfer
                        .expected_digest
                        .clone()
                        .ok_or(Error("invalid_transfer"))?,
                    size: transfer.expected_size.ok_or(Error("invalid_transfer"))?,
                    media_type: "application/octet-stream".into(),
                };
                release(unit, contents, &blob, &format!("{id}:sealed"))?;
            }
            tx.execute("DELETE FROM chunks WHERE transfer_id=$1", &[id.into()])?;
            counts.expired_transfers += 1;
            Ok(())
        },
    )?;
    scan(tx,"SELECT key FROM settings WHERE key LIKE 'query_ref_source:%' AND key>$1 ORDER BY key LIMIT 64",&[],|key| {
        let marker = tx.setting(key)?.ok_or(Error("storage_record_mismatch"))?;
        let marker = marker.as_object()?;
        let get = |name:&str|marker.get(name).ok_or(Error("invalid_query_ref_source"));
        if Timestamp::parse(get("retain_until")?.as_str()?)? > now { return Ok(()); }
        let id = key.strip_prefix("query_ref_source:").ok_or(Error("storage_record_mismatch"))?;
        let resource = match tx.resource_record(id) {
            Ok(resource)=>resource,
            Err(Error("not_found"))=>{ tx.execute("DELETE FROM settings WHERE key=$1",&[key.into()])?; return Ok(()); },
            Err(error)=>return Err(error),
        };
        if resource.state == ResourceState::Purged || resource.resource_type != "file" || resource.owner != get("subject")?.as_str()? || resource.parent.as_deref() != Some(get("parent")?.as_str()?) || resource.revision.as_deref() != Some(get("revision")?.as_str()?) || resource.mode != 0o600 {
            tx.execute("DELETE FROM settings WHERE key=$1",&[key.into()])?; return Ok(());
        }
        if tx.one("SELECT 1 FROM relations rel JOIN resources source ON source.id=rel.source_id AND source.revision=rel.revision_id AND source.state<>'purged' WHERE rel.target_id=$1 LIMIT 1",&[id.into()])?.is_some() { return Ok(()); }
        let mut active = false;
        scan(tx,"SELECT id FROM transfers WHERE id>$1 ORDER BY id LIMIT 64",&[],|transfer_id| {
            let session = tx.transfer(transfer_id)?;
            active |= session.expires_at > now && (session.target.as_ref().is_some_and(|r|r.id==id) || session.output.as_ref().is_some_and(|r|r.id==id));
            Ok(())
        })?;
        if active { return Ok(()); }
        purge(unit,contents,&resource,now,&format!("query-ref-retention:{id}:{}",time(now)),"query_ref_expired")?;
        tx.execute("DELETE FROM settings WHERE key=$1",&[key.into()])?;
        counts.expired_query_sources += 1;
        Ok(())
    })?;
    tx.execute(
        "DELETE FROM email_challenges WHERE expires<=$1",
        &[time(now).into()],
    )?;
    counts.expired_custodial_upgrades = tx.execute(
        "DELETE FROM custodial_upgrades WHERE status IN ('pending','failed') AND expires_at<=$1",
        &[time(now).into()],
    )?;
    encode(&counts)
}
fn rebuild(tx: &PgSession, contents: &GitContentStore) -> Result<Json> {
    tx.execute("DELETE FROM projections", &[])?;
    let mut count = 0_u64;
    scan(tx,"SELECT id FROM resources WHERE revision IS NOT NULL AND state<>'purged' AND id>$1 ORDER BY id LIMIT 64",&[],|id| {
        let revision = tx.revision(&ResourceRef { id:id.into(),revision:None })?;
        if revision.content.size <= 1_048_576 && (revision.content.media_type.starts_with("text/") || revision.content.media_type == "application/msg-template") {
            let bytes = contents.read_bytes(&revision.content,1_048_576)?;
            tx.execute("INSERT INTO projections(resource_id,text) VALUES($1,$2)",&[id.into(),String::from_utf8_lossy(&bytes).into_owned().into()])?;
            count += 1;
        }
        Ok(())
    })?;
    encode(&serde_json::json!({"indexed_resources":count}))
}
fn notice_id(id: &str, due_at: &str) -> Result<String> {
    let hash = hash(&("todo_due", id, due_at))?;
    Ok(format!("m_{}", &hash[7..39]))
}
fn deliver_due_todos(tx: &PgSession, contents: &GitContentStore, now: Timestamp) -> Result<Json> {
    let cursor = tx
        .setting("todo_delivery_cursor")?
        .map(|v| v.as_str().map(str::to_owned))
        .transpose()?
        .unwrap_or_default();
    let mut rows = tx.rows("SELECT id FROM resources WHERE type='todo' AND state='active' AND id>$1 ORDER BY id LIMIT 64",&[cursor.as_str().into()])?;
    if rows.len() < BATCH {
        rows.extend(tx.rows("SELECT id FROM resources WHERE type='todo' AND state='active' AND id<=$1 ORDER BY id LIMIT $2",&[cursor.into(),((BATCH-rows.len()) as i64).into()])?);
    }
    if let Some(last) = rows.last() {
        tx.set_setting("todo_delivery_cursor", &Json::string(last.text(0)?))?;
    }
    let mut delivered = 0_u64;
    for row in rows {
        let resource = tx.resource_record(row.text(0)?)?;
        if resource.revision.is_none() || resource.mode & 0o077 != 0 {
            continue;
        }
        let parent = tx.resource_record(resource.parent.as_deref().ok_or(Error("not_found"))?)?;
        if parent.name != "todos"
            || parent.parent.as_deref() != Some(&resource.owner)
            || parent.owner != resource.owner
            || parent.mode & 0o077 != 0
            || parent.state != ResourceState::Active
        {
            continue;
        }
        let revision = tx.revision(&ResourceRef {
            id: resource.id.clone(),
            revision: resource.revision.clone(),
        })?;
        let bytes = contents.read_bytes(&revision.content, msg_core::MAX_BYTES as u64)?;
        let details = Json::parse(std::str::from_utf8(&bytes).map_err(|_| Error("invalid_todo"))?)?;
        let details = details.as_object()?;
        let Some(due_at) = details.get("due_at") else {
            continue;
        };
        if !truthy(due_at)? {
            continue;
        }
        let status = details
            .get("status")
            .map(Json::as_str)
            .transpose()?
            .unwrap_or("");
        if !matches!(status, "pending" | "in_progress") || Timestamp::parse(due_at.as_str()?)? > now
        {
            continue;
        }
        let id = notice_id(&resource.id, due_at.as_str()?)?;
        let message = encode(
            &serde_json::json!({"id":id,"sender":null,"actor":ONLINE_CA,"recipient":resource.owner,"resource":{"id":resource.id,"revision":null},"time":now,"state":"delivered","source":"todo_due","due_at":due_at,"todo_revision_at_delivery":resource.revision,"status_at_delivery":status}),
        )?;
        let inserted = tx.execute("INSERT INTO messages(id,sender,recipient,resource,event_id,body) VALUES($1,NULL,$2,$3,$1,$4) ON CONFLICT(id) DO NOTHING",&[id.as_str().into(),resource.owner.as_str().into(),resource.id.as_str().into(),message.canonical()?.into()])?;
        if inserted == 0 {
            continue;
        }
        tx.append_audit(&AuditEvent {
            event: Event {
                id: process::id("audit")?,
                event_type: "identity.todo.due_notice".into(),
                time: now,
                request_id: format!("todo-due:{id}"),
                actor: Some(ONLINE_CA.into()),
                subject: Some(resource.owner.clone()),
                resources: vec![ResourceRef {
                    id: resource.id.clone(),
                    revision: resource.revision.clone(),
                }],
                data: encode(&serde_json::json!({"message_id":id,"due_at":due_at}))?
                    .into_object()?,
            },
            authority: vec![],
            before_digest: Some(hash(&resource)?),
            after_digest: Some(digest(message.canonical()?.as_bytes())),
            previous_digest: None,
            entry_digest: String::new(),
            result: "delivered".into(),
        })?;
        delivered += 1;
    }
    encode(&serde_json::json!({"delivered_todo_reminders":delivered}))
}

/// Read every retained SQL root under the same writer fence as the collector.
pub fn content_roots(tx: &PgSession) -> Result<(BTreeSet<String>, BTreeSet<String>)> {
    let mut live = BTreeSet::new();
    let mut revisions = BTreeSet::new();
    scan(
        tx,
        "SELECT id FROM revisions WHERE id>$1 ORDER BY id LIMIT 64",
        &[],
        |id| {
            let row = tx
                .one("SELECT body FROM revisions WHERE id=$1", &[id.into()])?
                .ok_or(Error("revision_not_found"))?;
            let revision: Revision = decode(&row.json(0)?)?;
            if revision.id != id {
                return Err(Error("storage_record_mismatch"));
            }
            live.insert(revision.content.digest);
            revisions.insert(id.to_owned());
            Ok(())
        },
    )?;
    scan(
        tx,
        "SELECT id FROM transfers WHERE id>$1 ORDER BY id LIMIT 64",
        &[],
        |id| {
            let mut cursor = -1_i64;
            loop {
                let rows = tx.rows("SELECT \"offset\" FROM chunks WHERE transfer_id=$1 AND \"offset\">$2 ORDER BY \"offset\" LIMIT 64",&[id.into(),cursor.into()])?;
                for row in &rows {
                    cursor = row.integer(0)?;
                    let row = tx
                        .one(
                            "SELECT body FROM chunks WHERE transfer_id=$1 AND \"offset\"=$2",
                            &[id.into(), cursor.into()],
                        )?
                        .ok_or(Error("storage_record_mismatch"))?;
                    let chunk: TransferChunk = decode(&row.json(0)?)?;
                    if chunk.transfer_id != id || chunk.offset != cursor {
                        return Err(Error("storage_record_mismatch"));
                    }
                    live.insert(chunk.content.digest);
                }
                if rows.len() < BATCH {
                    return Ok(());
                }
            }
        },
    )?;
    for (ids, body) in [
        (
            "SELECT id FROM store_packages WHERE id>$1 ORDER BY id LIMIT 64",
            "SELECT payload_refs FROM store_packages WHERE id=$1",
        ),
        (
            "SELECT id FROM store_deliveries WHERE id>$1 ORDER BY id LIMIT 64",
            "SELECT payload_refs FROM store_deliveries WHERE id=$1",
        ),
    ] {
        scan(tx, ids, &[], |id| {
            let row = tx
                .one(body, &[id.into()])?
                .ok_or(Error("storage_record_mismatch"))?;
            for reference in row.json(0)?.as_array()? {
                let blob: BlobRef = decode(
                    reference
                        .as_object()?
                        .get("blob")
                        .ok_or(Error("invalid_content_root"))?,
                )?;
                live.insert(blob.digest);
            }
            Ok(())
        })?;
    }
    if tx
        .one("SELECT to_regclass('arbitration_evidence')::text", &[])?
        .ok_or(Error("storage_record_mismatch"))?
        .optional_text(0)?
        .is_some()
    {
        scan(
            tx,
            "SELECT id FROM arbitration_evidence WHERE id>$1 ORDER BY id LIMIT 64",
            &[],
            |id| {
                let row = tx
                    .one(
                        "SELECT body FROM arbitration_evidence WHERE id=$1",
                        &[id.into()],
                    )?
                    .ok_or(Error("storage_record_mismatch"))?;
                let value = row.json(0)?;
                let blob: BlobRef = decode(
                    value
                        .as_object()?
                        .get("blob")
                        .ok_or(Error("invalid_content_root"))?,
                )?;
                live.insert(blob.digest);
                Ok(())
            },
        )?;
    }
    Ok((live, revisions))
}
fn collect(tx: &PgSession, contents: &GitContentStore) -> Result<Json> {
    let (digests, revisions) = content_roots(tx)?;
    let cutoff = SystemTime::now()
        .checked_sub(Duration::from_secs(3600))
        .ok_or(Error("invalid_datetime"))?;
    let result = contents.collect_unreferenced(&digests, &revisions, cutoff)?;
    encode(
        &serde_json::json!({"unreferenced_contents_removed":result.objects,"shared_bytes":"retained_if_referenced","backups":"managed_separately","unlinked_lfs_objects_removed":result.shared_objects,"unlinked_lfs_bytes_removed":result.shared_bytes}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reminders_match_python_stable_due_instant_ids() {
        assert_eq!(
            notice_id("todo_abc", "2026-10-21T08:00:00Z").unwrap(),
            "m_78bf2d76e75dcfaccba618fde684a0b3"
        );
        assert_ne!(
            notice_id("todo_abc", "2026-10-21T08:00:00Z").unwrap(),
            notice_id("todo_abc", "2026-10-21T08:01:00Z").unwrap()
        );
    }
    #[test]
    fn retention_never_accepts_boolean_or_negative_seconds() {
        for raw in ["true", "-1", "1.0", "9223372036854775808"] {
            assert!(policy_seconds(&Json::parse(raw).unwrap()).is_err());
        }
        assert_eq!(policy_seconds(&Json::parse("3600").unwrap()).unwrap(), 3600);
    }
}
