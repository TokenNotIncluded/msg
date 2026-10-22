//! Event projections share the committing transaction. Captured subscriptions
//! are revalidated against current credentials and ACLs before disclosing IDs.
use crate::{
    content::normalize_tag,
    context::{new_id, probe, Context},
};
use msg_core::{digest, Error, Json, Result};
use msg_identity::{
    authentication::{Admission, AuthenticationService, Entry, Principal},
    authorization::{AccessRequirement, AuthorizationService, Check},
    models::{decode, wire, ResourceRef, Timestamp},
    sessions::{BrowserPolicy, CredentialState},
    store::{truthy, AuthorityStore, Record, RecoveryDelivery},
    worker_authority::{SubscriptionAuthorityStore, SubscriptionKey, WorkerAuthority},
};
use msg_storage::{
    content::GitContentStore,
    postgres::PgSession,
    records::{EffectJob, Event, Resource, ResourceState},
};
use serde_json::json;
use std::collections::{BTreeMap, BTreeSet};

pub(crate) const WATCH_EVENTS: &[&str] = &[
    "content.post_create",
    "content.post_edit",
    "discussion.reply",
    "content.archive",
];
const QUERY_EVENTS: &[&str] = &[
    "content.post_create",
    "content.post_edit",
    "discussion.reply",
    "content.archive",
    "content.tags_set",
    "content.move",
    "content.chown",
];
fn require(ok: bool, code: &'static str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(Error(code))
    }
}
pub(crate) fn field<'a>(value: &'a Json, key: &str) -> Result<&'a Json> {
    value
        .as_object()?
        .get(key)
        .ok_or(Error("watch_record_invalid"))
}
fn text<'a>(value: &'a Json, key: &str) -> Result<&'a str> {
    field(value, key)?.as_str()
}
fn integer(value: &Json, key: &str) -> Result<i64> {
    decode(field(value, key)?)
}
pub(crate) fn hashed(value: &Json) -> Result<String> {
    Ok(digest(value.canonical()?.as_bytes()))
}

pub(crate) struct SubscriptionStore<'a> {
    pub tx: &'a PgSession,
    pub contents: &'a GitContentStore,
}
impl AuthorityStore for SubscriptionStore<'_> {
    fn record(&self, kind: Record, id: &str) -> Result<Json> {
        self.tx.record(kind, id)
    }
    fn setting(&self, key: &str) -> Result<Option<Json>> {
        self.tx.setting(key)
    }
    fn has_setting(&self, key: &str) -> Result<bool> {
        self.tx.has_setting(key)
    }
    fn certificate_revoked(&self, id: &str) -> Result<bool> {
        self.tx.certificate_revoked(id)
    }
    fn request_result(
        &self,
        subject: Option<&str>,
        id: &str,
        digest: &str,
    ) -> Result<Option<Json>> {
        self.tx.request_result(subject, id, digest)
    }
    fn credential_state(&self, id: &str) -> Result<Option<CredentialState>> {
        self.tx.credential_state(id)
    }
    fn custodial_binding(&self, subject: &str) -> Result<Option<(String, String)>> {
        self.tx.custodial_binding(subject)
    }
    fn recovery_delivery(
        &self,
        credential: &str,
        subject: &str,
    ) -> Result<Option<RecoveryDelivery>> {
        self.tx.recovery_delivery(credential, subject)
    }
}
impl SubscriptionAuthorityStore for SubscriptionStore<'_> {
    fn authority(&self) -> &dyn AuthorityStore {
        self.tx
    }
    fn subscription_snapshot(&self, key: &SubscriptionKey) -> Result<Json> {
        match key {
            SubscriptionKey::Webhook {
                owner,
                scope,
                generation,
            } => {
                let resource = self.tx.resource_record(scope)?;
                require(
                    resource.owner == *owner && resource.state == ResourceState::Active,
                    "subscription_identity_required",
                )?;
                let value = self
                    .tx
                    .setting(&format!("webhook_subscription:{owner}:{scope}"))?
                    .ok_or(Error("subscription_identity_required"))?;
                require(
                    truthy(field(&value, "enabled")?)?
                        && integer(&value, "generation")? == *generation,
                    "subscription_identity_required",
                )?;
                let principal = field(&value, "principal")?;
                require(
                    text(principal, "actor")? == owner
                        && text(principal, "subject")? == owner
                        && text(principal, "method")? == "signature",
                    "subscription_identity_required",
                )?;
                Ok(principal.clone())
            }
            SubscriptionKey::Watch { resource, revision } => {
                let (row, saved) = watch_record(self.tx, self.contents, resource)?;
                require(
                    row.state == ResourceState::Active
                        && row.revision.as_deref() == Some(revision)
                        && text(&saved, "status")? == "active",
                    "subscription_identity_required",
                )?;
                Ok(field(&saved, "principal")?.clone())
            }
            SubscriptionKey::SavedQuery { resource, revision } => {
                let row = self.tx.resource_record(resource)?;
                require(
                    row.resource_type == "saved_query" && row.state == ResourceState::Active,
                    "saved_query_not_found",
                )?;
                let saved = stored_json(
                    self.tx,
                    self.contents,
                    &ResourceRef {
                        id: resource.clone(),
                        revision: Some(revision.clone()),
                    },
                    131072,
                )?;
                require(text(&saved, "subject")? == row.owner, "saved_query_invalid")?;
                let principal = field(&saved, "principal")?;
                require(
                    text(principal, "subject")? == row.owner
                        && text(principal, "actor")? == row.owner,
                    "saved_query_invalid",
                )?;
                Ok(principal.clone())
            }
        }
    }
}
pub(crate) fn authority(ctx: &Context<'_>) -> WorkerAuthority {
    WorkerAuthority::new(
        Some(BrowserPolicy {
            enabled: ctx.settings.browser.enabled,
        }),
        ctx.catalog.temporary_ceiling.clone(),
    )
}
pub(crate) fn stored_json(
    tx: &PgSession,
    contents: &GitContentStore,
    reference: &ResourceRef,
    limit: u64,
) -> Result<Json> {
    let revision = tx.revision(reference)?;
    let bytes = contents.read_bytes(&revision.content, limit)?;
    require(
        digest(&bytes) == revision.content.digest,
        "content_digest_mismatch",
    )?;
    Json::parse(std::str::from_utf8(&bytes).map_err(|_| Error("invalid_json"))?)
}
pub(crate) fn watch_record(
    tx: &PgSession,
    contents: &GitContentStore,
    id: &str,
) -> Result<(Resource, Json)> {
    let resource = tx.resource_record(id)?;
    require(resource.resource_type == "watch", "watch_not_found")?;
    let saved = stored_json(
        tx,
        contents,
        &ResourceRef {
            id: id.to_owned(),
            revision: None,
        },
        131072,
    )?;
    let check = (|| {
        let fields = saved.as_object()?;
        require(
            [
                "id",
                "subject",
                "target",
                "query_ref",
                "event_types",
                "delivery",
                "created_at",
                "expires_at",
                "status",
                "legacy",
                "principal",
                "operation",
            ]
            .iter()
            .all(|k| fields.contains_key(*k)),
            "watch_record_invalid",
        )?;
        require(
            text(&saved, "id")? == id
                && text(&saved, "subject")? == resource.owner
                && matches!(text(&saved, "status")?, "active" | "cancelled")
                && text(&saved, "delivery")? == "inbox",
            "watch_record_invalid",
        )?;
        text(&saved, "target")?;
        field(&saved, "legacy")?.as_bool()?;
        let events = field(&saved, "event_types")?.as_array()?;
        require(
            !events.is_empty()
                && events
                    .iter()
                    .all(|v| v.as_str().is_ok_and(|v| QUERY_EVENTS.contains(&v))),
            "watch_record_invalid",
        )?;
        let (operation, version) = text(&saved, "operation")?
            .rsplit_once('@')
            .ok_or(Error("watch_record_invalid"))?;
        require(
            matches!(
                operation,
                "communication.watch" | "communication.watch_create"
            ) && !version.starts_with('0')
                && version.parse::<u32>().is_ok_and(|v| v > 0),
            "watch_record_invalid",
        )?;
        Timestamp::parse(text(&saved, "created_at")?)?;
        if !field(&saved, "expires_at")?.is_null() {
            Timestamp::parse(text(&saved, "expires_at")?)?;
        }
        require(
            text(field(&saved, "principal")?, "subject")? == resource.owner,
            "watch_record_invalid",
        )?;
        if !field(&saved, "query_ref")?.is_null() {
            let reference: ResourceRef = decode(field(&saved, "query_ref")?)?;
            require(reference.revision.is_some(), "watch_record_invalid")?;
            text(&saved, "query_binding")?;
        }
        Ok(())
    })();
    check.map_err(|_: Error| Error("watch_record_invalid"))?;
    Ok((resource, saved))
}

fn check(
    authorizer: &mut AuthorizationService,
    tx: &PgSession,
    principal: &Principal,
    operation: &str,
    id: &str,
    permission: Check,
    now: Timestamp,
) -> Result<()> {
    authorizer.require(
        principal,
        Entry::Worker,
        now,
        &[AccessRequirement {
            resource_id: id.to_owned(),
            operation: operation.to_owned(),
            check: permission,
        }],
        None,
        tx,
    )?;
    Ok(())
}
/// Ownership and both current principals are checked. The source principal is
/// internal authority and is deliberately not serializable as a public output.
pub(crate) struct WatchQuery {
    pub arguments: BTreeMap<String, Json>,
    pub principal: Principal,
}
pub(crate) fn watch_query(ctx: &mut Context<'_>, reference: &ResourceRef) -> Result<WatchQuery> {
    let revision = reference
        .revision
        .as_ref()
        .ok_or(Error("saved_query_revision_required"))?;
    let resource = ctx.tx.resource_record(&reference.id)?;
    require(
        resource.resource_type == "saved_query" && resource.state == ResourceState::Active,
        "saved_query_not_found",
    )?;
    require(
        ctx.principal.subject() == Some(resource.owner.as_str())
            && ctx.principal.actor() == Some(resource.owner.as_str()),
        "permission_denied",
    )?;
    require(
        resource.mode & 0o400 != 0
            && ctx
                .tx
                .ancestors(&resource.id)?
                .iter()
                .all(|r| r.state == "active"),
        "permission_denied",
    )?;
    ctx.check(
        resource
            .parent
            .as_deref()
            .ok_or(Error("saved_query_invalid"))?,
        Check::List,
    )?;
    ctx.authorizer.require_base(
        ctx.principal,
        &ctx.operation()?,
        &resource.id,
        ctx.tx,
        ctx.now,
    )?;
    let saved = stored_json(ctx.tx, ctx.contents, reference, 131072)?;
    require(
        text(&saved, "subject")? == resource.owner
            && saved
                .as_object()?
                .keys()
                .map(String::as_str)
                .collect::<BTreeSet<_>>()
                == BTreeSet::from([
                    "subject",
                    "created_at",
                    "descriptor",
                    "descriptor_digest",
                    "principal",
                    "source_digest",
                ]),
        "saved_query_invalid",
    )?;
    ctx.catalog.validate(
        &ctx.catalog
            .resource_type("saved_query", 1)?
            .content_schema
            .as_ref()
            .ok_or(Error("saved_query_invalid"))?
            .id,
        &saved,
    )?;
    let descriptor = field(&saved, "descriptor")?;
    require(
        descriptor
            .as_object()?
            .keys()
            .map(String::as_str)
            .collect::<BTreeSet<_>>()
            == BTreeSet::from(["operation", "contract_version", "arguments"]),
        "saved_query_invalid",
    )?;
    require(
        hashed(descriptor)? == text(&saved, "descriptor_digest")?,
        "saved_query_digest_mismatch",
    )?;
    require(
        text(descriptor, "operation")? == "discovery.read_query"
            && integer(descriptor, "contract_version")? == 1,
        "watch_query_unsupported",
    )?;
    let mut arguments = field(descriptor, "arguments")?.as_object()?.clone();
    require(
        arguments.contains_key("parent")
            && arguments
                .keys()
                .all(|k| matches!(k.as_str(), "parent" | "type" | "author" | "state" | "tag")),
        "watch_query_unsupported",
    )?;
    let spec = ctx.catalog.operation("discovery.read_query", 1)?;
    require(spec.effect == "read", "saved_query_invalid")?;
    ctx.catalog
        .validate(&spec.input_schema.id, &Json::from_object(arguments.clone()))?;
    let parent = ctx.resolve(arguments["parent"].as_str()?, false)?;
    arguments.insert("parent".into(), Json::string(&parent));
    if let Some(value) = arguments.get("author") {
        let id = ctx.resolve(value.as_str()?, false)?;
        arguments.insert("author".into(), Json::string(id));
    }
    if let Some(value) = arguments.get("tag") {
        let tag = normalize_tag(value)?;
        arguments.insert("tag".into(), Json::string(tag));
    }
    let adapter = SubscriptionStore {
        tx: ctx.tx,
        contents: ctx.contents,
    };
    let principal = authority(ctx).current_subscription(
        &SubscriptionKey::SavedQuery {
            resource: resource.id.clone(),
            revision: revision.clone(),
        },
        ctx.authorizer,
        &adapter,
        ctx.now,
    )?;
    require(
        principal.subject() == Some(resource.owner.as_str())
            && principal.actor() == Some(resource.owner.as_str()),
        "permission_denied",
    )?;
    ctx.authorizer
        .require_base(&principal, "query.save@1", &resource.id, ctx.tx, ctx.now)?;
    let scope = ctx.tx.resource_record(&parent)?;
    require(scope.state == ResourceState::Active, "ancestor_inactive")?;
    let permission = if ctx
        .catalog
        .resource_type(&scope.resource_type, scope.type_version as u32)?
        .container
    {
        Check::List
    } else {
        Check::Read
    };
    for source in [&principal, ctx.principal] {
        check(
            ctx.authorizer,
            ctx.tx,
            source,
            "discovery.read_query@1",
            &parent,
            permission,
            ctx.now,
        )?;
    }
    check(
        ctx.authorizer,
        ctx.tx,
        ctx.principal,
        "discovery.read_query@1",
        &parent,
        Check::List,
        ctx.now,
    )?;
    Ok(WatchQuery {
        arguments,
        principal,
    })
}
pub(crate) fn suppressed(error: Error) -> bool {
    matches!(
        error.0,
        "permission_denied"
            | "authentication_required"
            | "credential_ceiling"
            | "capability_required"
            | "certificate_gate"
            | "tool_certificate_required"
            | "delegation_scope"
            | "ancestor_inactive"
            | "account_archived"
            | "local_only"
            | "credential_revoked"
            | "credential_expired"
            | "credential_kind_mismatch"
            | "credential_subject_mismatch"
            | "credential_not_found"
            | "certificate_revoked"
            | "certificate_expired"
            | "certificate_not_found"
            | "certificate_subject_mismatch"
            | "delegation_exhausted"
            | "delegation_expired"
            | "invalid_delegation"
            | "browser_session_revoked"
            | "browser_session_expired"
            | "custodial_key_changed"
            | "worker_identity_required"
            | "subscription_identity_required"
            | "not_found"
            | "unknown_operation"
            | "operation_disabled"
            | "watch_query_scope_changed"
            | "saved_query_not_found"
            | "saved_query_invalid"
            | "saved_query_digest_mismatch"
            | "watch_query_unsupported"
            | "watch_not_found"
            | "webhook_scope_changed"
    )
}
fn corrupt(error: Error) -> bool {
    matches!(
        error.0,
        "content_missing"
            | "content_truncated"
            | "content_size_mismatch"
            | "content_digest_mismatch"
            | "invalid_json"
            | "duplicate_json_key"
            | "non_finite_float"
            | "revision_not_found"
            | "watch_record_invalid"
            | "watch_content_invalid"
            | "use_transfer"
    )
}

pub(crate) fn webhook_capacity(tx: &PgSession) -> Result<()> {
    let active = tx
        .one(
            "SELECT COUNT(*) FROM jobs WHERE kind='webhook' AND state IN ('pending','running')",
            &[],
        )?
        .ok_or(Error("storage_capacity_exceeded"))?
        .integer(0)?;
    require((0..1024).contains(&active), "storage_capacity_exceeded")?;
    let total = tx
        .one("SELECT COUNT(*) FROM jobs WHERE kind='webhook'", &[])?
        .ok_or(Error("storage_capacity_exceeded"))?
        .integer(0)?;
    if total >= 8192 {
        // Bound each cleanup page even for a legacy database above its cap.
        let mut remaining = total - 8192 + 1;
        while remaining > 0 {
            let rows=tx.rows("SELECT id FROM jobs WHERE kind='webhook' AND state IN ('done','failed') ORDER BY next_at,id LIMIT $1",&[remaining.min(256).into()])?;
            require(!rows.is_empty(), "storage_capacity_exceeded")?;
            for row in rows {
                remaining-=tx.execute("DELETE FROM jobs WHERE id=$1 AND kind='webhook' AND state IN ('done','failed')",&[row.text(0)?.into()])? as i64;
            }
        }
    }
    Ok(())
}
pub(crate) fn job(
    ctx: &Context<'_>,
    event: &str,
    kind: &str,
    dedupe: &str,
    principal: &Principal,
    operation: &str,
    arguments: &Json,
) -> Result<EffectJob> {
    decode(&wire(
        &json!({"id":new_id("job")?,"event_id":event,"kind":kind,"dedupe_key":dedupe,"principal":principal,"operation":operation,"arguments":arguments,"state":"pending","attempts":0,"next_attempt_at":ctx.now,"lease_until":null}),
    )?)
}
fn websub(
    ctx: &mut Context<'_>,
    event: &Event,
    authenticator: &mut AuthenticationService,
) -> Result<()> {
    if ctx.settings.websub_hubs.is_empty() || event.resources.is_empty() {
        return Ok(());
    }
    let mut changed = false;
    for reference in &event.resources {
        if matches!(
            ctx.tx
                .resource_record(&reference.id)?
                .resource_type
                .as_str(),
            "post" | "topic" | "space"
        ) {
            changed = true;
            break;
        }
    }
    if !changed {
        return Ok(());
    }
    let request = probe(
        "discovery.feed",
        1,
        &Json::from_object(BTreeMap::new()),
        &ctx.settings.service_url,
        None,
        ctx.now,
    )?;
    let principal = match authenticator.authenticate(
        &request,
        &ctx.catalog.operation("discovery.feed", 1)?.policy(),
        ctx.tx,
        Entry::Network,
        ctx.now,
    )? {
        Admission::Fresh(p) => p,
        Admission::Replay(_) => return Err(Error("invalid_anonymous_principal")),
    };
    require(
        principal.subject().is_none()
            && principal.actor().is_none()
            && principal.method() == "anonymous",
        "invalid_anonymous_principal",
    )?;
    let output = {
        let mut anonymous = Context {
            tx: ctx.tx,
            contents: ctx.contents,
            catalog: ctx.catalog,
            principal: &principal,
            request: &request,
            now: ctx.now,
            settings: ctx.settings,
            deadline: ctx.deadline,
            authorizer: ctx.authorizer,
            content_write: None,
            cursor_key: ctx.cursor_key,
        };
        crate::discovery::handle(&mut anonymous)?
    };
    let items = output
        .data
        .as_ref()
        .and_then(|d| d.get("items"))
        .ok_or(Error("invalid_feed_projection"))?;
    let fingerprint = hashed(items)?;
    let previous = ctx
        .tx
        .setting("websub:public_feed")?
        .unwrap_or(Json::string(hashed(&Json::empty_array())?));
    if previous.as_str()? == fingerprint {
        return Ok(());
    }
    ctx.tx
        .set_setting("websub:public_feed", &Json::string(&fingerprint))?;
    for hub in ctx.settings.websub_hubs.iter().collect::<BTreeSet<_>>() {
        let dedupe = format!("{}:websub:{}", event.id, hashed(&Json::string(hub))?);
        let args =
            wire(&json!({"hub":hub,"topic":format!("{}/rss.xml",ctx.settings.service_url)}))?;
        ctx.tx.enqueue(&job(
            ctx,
            &event.id,
            "websub",
            &dedupe,
            &principal,
            "discovery.feed",
            &args,
        )?)?;
    }
    Ok(())
}
fn webhook_exists(
    tx: &PgSession,
    event: &str,
    owner: &str,
    resource: &str,
    category: &str,
    dedupe: &str,
) -> Result<bool> {
    if let Some(row) = tx.one(
        "SELECT body FROM jobs WHERE kind='webhook' AND dedupe=$1",
        &[dedupe.into()],
    )? {
        let job: EffectJob = decode(&row.json(0)?)?;
        return Ok(job.event_id == event
            && job.operation == "communication.webhook_subscribe"
            && job
                .arguments
                .get("recipient_subject")
                .and_then(|v| v.as_str().ok())
                == Some(owner)
            && job
                .arguments
                .get("resource_id")
                .and_then(|v| v.as_str().ok())
                == Some(resource)
            && job.arguments.get("category").and_then(|v| v.as_str().ok()) == Some(category));
    }
    let prefix = format!(
        "{}:%",
        event
            .replace('!', "!!")
            .replace('%', "!%")
            .replace('_', "!_")
    );
    let mut after = String::new();
    loop {
        let rows=tx.rows("SELECT id,body FROM jobs WHERE kind='webhook' AND dedupe LIKE $1 ESCAPE '!' AND id>$2 ORDER BY id LIMIT 128",&[prefix.as_str().into(),after.as_str().into()])?;
        if rows.is_empty() {
            return Ok(false);
        }
        for row in rows {
            after = row.text(0)?.to_owned();
            let job: EffectJob = decode(&row.json(1)?)?;
            if job.event_id == event
                && job.operation == "communication.webhook_subscribe"
                && job
                    .arguments
                    .get("recipient_subject")
                    .and_then(|v| v.as_str().ok())
                    == Some(owner)
                && job
                    .arguments
                    .get("resource_id")
                    .and_then(|v| v.as_str().ok())
                    == Some(resource)
                && job.arguments.get("category").and_then(|v| v.as_str().ok()) == Some(category)
            {
                return Ok(true);
            }
        }
    }
}
fn webhooks(ctx: &mut Context<'_>, event: &Event) -> Result<()> {
    let category = match event.event_type.as_str() {
        "content.post_create" | "discussion.reply" => "resource.created",
        "content.post_edit" => "resource.updated",
        _ => return Ok(()),
    };
    let authority = authority(ctx);
    for reference in &event.resources {
        let resource = ctx.tx.resource_record(&reference.id)?;
        let mut scopes = vec![resource.clone()];
        if let Some(parent) = &resource.parent {
            scopes.push(ctx.tx.resource_record(parent)?);
        }
        for scope in scopes {
            let owner = &scope.owner;
            let Some(saved) = ctx
                .tx
                .setting(&format!("webhook_subscription:{owner}:{}", scope.id))?
            else {
                continue;
            };
            if !truthy(field(&saved, "enabled")?)?
                || !field(&saved, "events")?
                    .as_array()?
                    .iter()
                    .any(|v| v.as_str().ok() == Some(category))
            {
                continue;
            }
            let Some(endpoint) = ctx.tx.one(
                "SELECT enabled,generation FROM webhook_endpoints WHERE subject=$1",
                &[owner.as_str().into()],
            )?
            else {
                continue;
            };
            if endpoint.integer(0)? != 1
                || endpoint.integer(1)? != integer(&saved, "endpoint_generation")?
            {
                continue;
            }
            let dedupe = format!("{}:{owner}:{}:{category}:webhook", event.id, reference.id);
            if webhook_exists(ctx.tx, &event.id, owner, &reference.id, category, &dedupe)? {
                continue;
            }
            let checked = (|| {
                let adapter = SubscriptionStore {
                    tx: ctx.tx,
                    contents: ctx.contents,
                };
                let principal = authority.current_subscription(
                    &SubscriptionKey::Webhook {
                        owner: owner.clone(),
                        scope: scope.id.clone(),
                        generation: integer(&saved, "generation")?,
                    },
                    ctx.authorizer,
                    &adapter,
                    ctx.now,
                )?;
                require(
                    resource.state == ResourceState::Active && scope.state == ResourceState::Active,
                    "webhook_scope_changed",
                )?;
                require(
                    ctx.authorizer.has(
                        &principal,
                        "webhook.domain",
                        "communication.webhook_subscribe@1",
                        &scope.id,
                        ctx.tx,
                        ctx.now,
                    )?,
                    "capability_required",
                )?;
                for id in [&scope.id, &resource.id] {
                    check(
                        ctx.authorizer,
                        ctx.tx,
                        &principal,
                        "communication.webhook_subscribe@1",
                        id,
                        Check::Read,
                        ctx.now,
                    )?;
                }
                Ok(principal)
            })();
            let principal = match checked {
                Ok(p) => p,
                Err(error) if suppressed(error) => continue,
                Err(error) => return Err(error),
            };
            webhook_capacity(ctx.tx)?;
            let args = wire(
                &json!({"recipient_subject":owner,"resource_id":reference.id,"scope_id":scope.id,"category":category,"endpoint_generation":endpoint.integer(1)?,"subscription_generation":integer(&saved,"generation")?}),
            )?;
            ctx.tx.enqueue(&job(
                ctx,
                &event.id,
                "webhook",
                &dedupe,
                &principal,
                "communication.webhook_subscribe",
                &args,
            )?)?;
        }
    }
    Ok(())
}
fn query_member(tx: &PgSession, id: &str, args: &BTreeMap<String, Json>) -> Result<bool> {
    let resource = tx.resource_record(id)?;
    let state = args
        .get("state")
        .map(Json::as_str)
        .transpose()?
        .unwrap_or("active");
    let actual = match resource.state {
        ResourceState::Active => "active",
        ResourceState::Archived => "archived",
        ResourceState::Purged => "purged",
    };
    if actual != state || resource.parent.as_deref() != Some(args["parent"].as_str()?) {
        return Ok(false);
    }
    if args
        .get("type")
        .is_some_and(|v| v.as_str().ok() != Some(resource.resource_type.as_str()))
        || args
            .get("author")
            .is_some_and(|v| v.as_str().ok() != Some(resource.owner.as_str()))
    {
        return Ok(false);
    }
    if let Some(tag) = args.get("tag") {
        return Ok(tx
            .one(
                "SELECT 1 FROM resource_tags WHERE resource_id=$1 AND tag=$2",
                &[id.into(), tag.as_str()?.into()],
            )?
            .is_some());
    }
    Ok(true)
}
fn watch_event(
    ctx: &mut Context<'_>,
    event: &Event,
    resource: &Resource,
    saved: &Json,
) -> Result<()> {
    if text(saved, "status")? != "active"
        || !field(saved, "event_types")?
            .as_array()?
            .iter()
            .any(|v| v.as_str().ok() == Some(event.event_type.as_str()))
    {
        return Ok(());
    }
    if !field(saved, "expires_at")?.is_null()
        && ctx.now >= Timestamp::parse(text(saved, "expires_at")?)?
    {
        return Ok(());
    }
    let adapter = SubscriptionStore {
        tx: ctx.tx,
        contents: ctx.contents,
    };
    let principal = authority(ctx).current_subscription(
        &SubscriptionKey::Watch {
            resource: resource.id.clone(),
            revision: resource
                .revision
                .clone()
                .ok_or(Error("watch_record_invalid"))?,
        },
        ctx.authorizer,
        &adapter,
        ctx.now,
    )?;
    let (operation, version) = text(saved, "operation")?
        .rsplit_once('@')
        .ok_or(Error("watch_record_invalid"))?;
    let version = version.parse().map_err(|_| Error("watch_record_invalid"))?;
    require(
        ctx.catalog.operation(operation, version)?.enabled,
        "operation_disabled",
    )?;
    let request = probe(
        operation,
        version,
        &Json::from_object(BTreeMap::new()),
        &ctx.settings.service_url,
        principal.subject(),
        ctx.now,
    )?;
    let mut watcher = Context {
        tx: ctx.tx,
        contents: ctx.contents,
        catalog: ctx.catalog,
        principal: &principal,
        request: &request,
        now: ctx.now,
        settings: ctx.settings,
        deadline: ctx.deadline,
        authorizer: ctx.authorizer,
        content_write: None,
        cursor_key: ctx.cursor_key,
    };
    let target = text(saved, "target")?;
    watcher.check(target, Check::Read)?;
    let query = if field(saved, "query_ref")?.is_null() {
        None
    } else {
        let query = watch_query(&mut watcher, &decode(field(saved, "query_ref")?)?)?;
        require(
            query.arguments["parent"].as_str()? == target
                && hashed(&Json::from_object(query.arguments.clone()))?
                    == text(saved, "query_binding")?,
            "watch_query_scope_changed",
        )?;
        Some(query)
    };
    for reference in &event.resources {
        if let Some(query) = &query {
            if !query_member(watcher.tx, &reference.id, &query.arguments)? {
                continue;
            }
            for source in [&principal, &query.principal] {
                check(
                    watcher.authorizer,
                    watcher.tx,
                    source,
                    "discovery.read_query@1",
                    &reference.id,
                    Check::Read,
                    watcher.now,
                )?;
            }
        } else if reference.id != target
            && !watcher
                .tx
                .ancestors(&reference.id)?
                .iter()
                .any(|r| r.id == target)
        {
            continue;
        }
        watcher.check(&reference.id, Check::Read)?;
        let recipient = text(saved, "subject")?;
        if watcher
            .tx
            .one(
                "SELECT 1 FROM messages WHERE event_id=$1 AND recipient=$2 AND resource=$3",
                &[
                    event.id.as_str().into(),
                    recipient.into(),
                    reference.id.as_str().into(),
                ],
            )?
            .is_some()
        {
            continue;
        }
        let id = new_id("message")?;
        let body = wire(
            &json!({"id":id,"sender":event.subject,"recipient":recipient,"resource":{"id":reference.id},"source":if field(saved,"legacy")?.as_bool()? {"follow"}else{"watch"},"time":event.time}),
        )?;
        watcher.tx.execute("INSERT INTO messages(id,sender,recipient,resource,event_id,body) VALUES($1,$2,$3,$4,$5,$6)",&[id.into(),event.subject.as_deref().into(),recipient.into(),reference.id.as_str().into(),event.id.as_str().into(),body.canonical()?.into()])?;
    }
    Ok(())
}
fn watches(ctx: &mut Context<'_>, event: &Event) -> Result<()> {
    if ctx.tx.has_setting("recovery_quarantine")?
        || !QUERY_EVENTS.contains(&event.event_type.as_str())
    {
        return Ok(());
    }
    let mut after = String::new();
    loop {
        let rows=ctx.tx.rows("SELECT id FROM resources WHERE type='watch' AND state='active' AND id>$1 ORDER BY id LIMIT 128",&[after.as_str().into()])?;
        if rows.is_empty() {
            return Ok(());
        }
        for row in rows {
            after = row.text(0)?.to_owned();
            let (resource, saved) = match watch_record(ctx.tx, ctx.contents, &after) {
                Ok(v) => v,
                Err(error) if corrupt(error) => {
                    eprintln!(
                        "watch_projection_corrupt watch_digest={} error_code={}",
                        digest(after.as_bytes()),
                        error.0
                    );
                    continue;
                }
                Err(error) => return Err(error),
            };
            if let Err(error) = watch_event(ctx, event, &resource, &saved) {
                if !suppressed(error) {
                    return Err(error);
                }
            }
        }
    }
}
pub(crate) fn enqueue(
    ctx: &mut Context<'_>,
    event: &Event,
    authenticator: &mut AuthenticationService,
) -> Result<()> {
    websub(ctx, event, authenticator)?;
    if ctx
        .settings
        .plugins
        .iter()
        .any(|name| name == "communication")
    {
        webhooks(ctx, event)?;
    }
    watches(ctx, event)
}
