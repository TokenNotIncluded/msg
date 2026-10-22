//! Durable target and saved-query subscriptions. Event projection is in
//! notifications; these handlers only change subscriptions in the same unit.
use crate::{
    content::{self, Body, Create, RevisionOptions},
    context::{new_id, output, Context, HandlerOutput},
    notifications::{field, hashed, suppressed, watch_query, watch_record, WATCH_EVENTS},
};
use chrono::Duration;
use msg_core::{Error, Json, Result};
use msg_identity::{
    authorization::{AccessRequirement, Check},
    models::{decode, wire, ResourceRef, Timestamp},
};
use msg_storage::records::{Resource, ResourceState};
use serde_json::json;

fn require(ok: bool, code: &'static str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(Error(code))
    }
}
pub fn supports(name: &str, version: u32) -> bool {
    match name {
        "communication.watch_create" => matches!(version, 1 | 2),
        "communication.watch_get"
        | "communication.watch_cancel"
        | "communication.watch_list"
        | "communication.watch"
        | "communication.unwatch" => version == 1,
        _ => false,
    }
}
pub fn requirements(_: &Context<'_>) -> Result<Vec<AccessRequirement>> {
    Ok(vec![])
}
pub fn handle(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    match ctx.request.field("operation")?.as_str()? {
        "communication.watch_create" => {
            let version: u32 = decode(ctx.request.field("contract_version")?)?;
            let (target, reference, binding) = if version == 1 {
                require(
                    !ctx.args()?.contains_key("query_ref"),
                    "watch_query_unsupported",
                )?;
                require(ctx.args()?.contains_key("target"), "watch_target_required")?;
                (ctx.resolve(ctx.text("target")?, false)?, None, None)
            } else {
                let reference: ResourceRef = decode(ctx.arg("query_ref")?)?;
                require(
                    reference.revision.is_some(),
                    "watch_query_revision_required",
                )?;
                let query = watch_query(ctx, &reference)?;
                (
                    query.arguments["parent"].as_str()?.to_owned(),
                    Some(reference),
                    Some(hashed(&Json::from_object(query.arguments))?),
                )
            };
            let (resource, saved) = create(ctx, &target, false, reference, binding)?;
            output(
                json!({"id":resource.id,"status":field(&saved,"status")?}),
                vec![],
            )
        }
        "communication.watch_get" => {
            let id = ctx.text("id")?.to_owned();
            let (_, saved) = owned(ctx, &id)?;
            let public = public(ctx, &saved)?;
            output(
                serde_json::to_value(public).map_err(|_| Error("watch_record_invalid"))?,
                vec![],
            )
        }
        "communication.watch_cancel" => {
            let id = ctx.text("id")?.to_owned();
            let (resource, saved) = owned(ctx, &id)?;
            let id = resource.id.clone();
            if field(&saved, "status")?.as_str()? != "cancelled" {
                cancel(ctx, resource, saved)?;
            }
            output(json!({"id":id,"status":"cancelled"}), vec![])
        }
        "communication.watch_list" => {
            let subject = ctx.subject()?.to_owned();
            ctx.authorizer.require_base(
                ctx.principal,
                &ctx.operation()?,
                &subject,
                ctx.tx,
                ctx.now,
            )?;
            let rows=ctx.tx.rows("SELECT id FROM resources WHERE type='watch' AND owner=$1 AND state='active' ORDER BY id LIMIT 101",&[subject.into()])?;
            require(rows.len() <= 100, "watch_list_limit")?;
            let mut items = Vec::new();
            for row in rows {
                let (_, saved) = owned(ctx, row.text(0)?)?;
                items.push(public(ctx, &saved)?);
            }
            output(json!({"items":items}), vec![])
        }
        "communication.watch" | "communication.unwatch" => {
            let enabled = ctx.request.field("operation")?.as_str()? == "communication.watch";
            let subject = ctx.subject()?.to_owned();
            let target = ctx.resolve(ctx.text("id")?, false)?;
            if enabled {
                ctx.check(&target, Check::Read)?;
            }
            ctx.authorizer.require_base(
                ctx.principal,
                &ctx.operation()?,
                &subject,
                ctx.tx,
                ctx.now,
            )?;
            ctx.authorizer
                .ceiling(ctx.principal, &ctx.operation()?, &target, ctx.tx, ctx.now)?;
            legacy(ctx, &target, enabled)?;
            if enabled {
                ctx.tx.execute("INSERT INTO watches(subject,resource) VALUES($1,$2) ON CONFLICT(subject,resource) DO NOTHING",&[subject.into(),target.as_str().into()])?;
            } else {
                ctx.tx.execute(
                    "DELETE FROM watches WHERE subject=$1 AND resource=$2",
                    &[subject.into(), target.as_str().into()],
                )?;
            }
            output(
                json!({"watching":enabled}),
                if enabled {
                    vec![ResourceRef {
                        id: target,
                        revision: None,
                    }]
                } else {
                    vec![]
                },
            )
        }
        _ => Err(Error("unknown_operation")),
    }
}
fn create(
    ctx: &mut Context<'_>,
    target: &str,
    legacy: bool,
    query: Option<ResourceRef>,
    binding: Option<String>,
) -> Result<(Resource, Json)> {
    let subject = ctx.subject()?.to_owned();
    ctx.authorizer
        .require_base(ctx.principal, &ctx.operation()?, &subject, ctx.tx, ctx.now)?;
    ctx.check(target, Check::Read)?;
    ctx.authorizer
        .ceiling(ctx.principal, &ctx.operation()?, target, ctx.tx, ctx.now)?;
    let expires = ctx
        .args()?
        .get("expires_at")
        .cloned()
        .unwrap_or(Json::parse("null")?);
    if !expires.is_null() {
        let expires = Timestamp::parse(expires.as_str()?)?;
        let maximum = ctx
            .now
            .0
            .checked_add_signed(Duration::days(365))
            .ok_or(Error("watch_expiry_invalid"))?;
        require(
            ctx.now < expires && expires.0 <= maximum,
            "watch_expiry_invalid",
        )?;
    }
    if legacy {
        if let Some(value) = legacy_rows(ctx, target)?.into_iter().next() {
            return Ok(value);
        }
    }
    let id = new_id("watch")?;
    let saved = wire(
        &json!({"id":id,"subject":subject,"target":target,"query_ref":query,"event_types":if legacy{wire(&WATCH_EVENTS)?}else{ctx.arg("event_types")?.clone()},"delivery":"inbox","created_at":ctx.now,"expires_at":expires,"status":"active","legacy":legacy,"principal":ctx.principal,"operation":ctx.operation()?,"query_binding":binding}),
    )?;
    let resource = content::create(
        ctx,
        Create {
            parent: subject,
            kind: "watch",
            name: Some(id.clone()),
            id: Some(id),
            body: Some(Body::Bytes(
                saved.canonical()?.into_bytes(),
                "application/json".into(),
            )),
            mode: Some(0o600),
            revision: RevisionOptions::default(),
        },
    )?;
    Ok((resource, saved))
}
fn owned(ctx: &mut Context<'_>, id: &str) -> Result<(Resource, Json)> {
    let id = ctx.resolve(id, false)?;
    let (resource, saved) = watch_record(ctx.tx, ctx.contents, &id)?;
    require(
        ctx.principal.subject() == Some(resource.owner.as_str())
            && field(&saved, "subject")?.as_str()? == resource.owner,
        "permission_denied",
    )?;
    ctx.authorizer.require_base(
        ctx.principal,
        &ctx.operation()?,
        &resource.id,
        ctx.tx,
        ctx.now,
    )?;
    Ok((resource, saved))
}
fn public(ctx: &mut Context<'_>, saved: &Json) -> Result<Json> {
    let mut result = saved
        .as_object()?
        .iter()
        .filter(|(key, _)| {
            !matches!(
                key.as_str(),
                "principal" | "operation" | "legacy" | "query_binding"
            )
        })
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect::<std::collections::BTreeMap<_, _>>();
    if let Err(error) = ctx.check(field(saved, "target")?.as_str()?, Check::Read) {
        if suppressed(error) {
            result.remove("target");
            result.remove("query_ref");
        } else {
            return Err(error);
        }
    }
    if !field(saved, "query_ref")?.is_null() {
        if let Err(error) = watch_query(ctx, &decode(field(saved, "query_ref")?)?) {
            if suppressed(error) {
                result.remove("target");
                result.remove("query_ref");
            } else {
                return Err(error);
            }
        }
    }
    if !field(saved, "expires_at")?.is_null()
        && ctx.now >= Timestamp::parse(field(saved, "expires_at")?.as_str()?)?
        && field(saved, "status")?.as_str()? == "active"
    {
        result.insert("status".into(), Json::string("expired"));
    }
    Ok(Json::from_object(result))
}
fn cancel(ctx: &mut Context<'_>, resource: Resource, saved: Json) -> Result<()> {
    let mut fields = saved.clone().into_object()?;
    fields.insert("status".into(), Json::string("cancelled"));
    let mut updated = content::revise(
        ctx,
        resource,
        Body::Bytes(
            Json::from_object(fields).canonical()?.into_bytes(),
            "application/json".into(),
        ),
        RevisionOptions::default(),
    )?;
    let expected = updated.generation;
    updated.state = ResourceState::Archived;
    updated.generation += 1;
    updated.modified_at = ctx.now;
    updated.modified_by = ctx
        .principal
        .actor()
        .ok_or(Error("authentication_required"))?
        .to_owned();
    ctx.tx.replace_resource(&updated, expected)?;
    if field(&saved, "legacy")?.as_bool()? {
        ctx.tx.execute(
            "DELETE FROM watches WHERE subject=$1 AND resource=$2",
            &[
                field(&saved, "subject")?.as_str()?.into(),
                field(&saved, "target")?.as_str()?.into(),
            ],
        )?;
    }
    Ok(())
}
fn legacy_rows(ctx: &Context<'_>, target: &str) -> Result<Vec<(Resource, Json)>> {
    let mut found = Vec::new();
    let mut after = String::new();
    loop {
        let rows=ctx.tx.rows("SELECT id FROM resources WHERE type='watch' AND owner=$1 AND state='active' AND id>$2 ORDER BY id LIMIT 128",&[ctx.subject()?.into(),after.as_str().into()])?;
        if rows.is_empty() {
            return Ok(found);
        }
        for row in rows {
            after = row.text(0)?.to_owned();
            let (resource, saved) = watch_record(ctx.tx, ctx.contents, &after)?;
            if field(&saved, "legacy")?.as_bool()?
                && field(&saved, "target")?.as_str()? == target
                && field(&saved, "status")?.as_str()? == "active"
            {
                found.push((resource, saved));
            }
        }
    }
}
pub(crate) fn legacy(ctx: &mut Context<'_>, target: &str, enabled: bool) -> Result<()> {
    if enabled {
        create(ctx, target, true, None, None)?;
    } else {
        for (resource, saved) in legacy_rows(ctx, target)? {
            cancel(ctx, resource, saved)?;
        }
    }
    Ok(())
}
