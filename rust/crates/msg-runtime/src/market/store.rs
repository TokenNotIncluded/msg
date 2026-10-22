//! Immutable listing revisions and seller-owned package deposits.
use super::{field, hash, integer, require, text};
use crate::{
    content::{self, Body, Create, RevisionOptions},
    context::{new_id, output, Context, HandlerOutput},
};
use msg_core::{Error, Json, Result};
use msg_identity::{
    authorization::Check,
    models::{ResourceRef, Timestamp},
    store::AuthorityStore,
};
use msg_storage::{
    postgres::PgSession,
    records::{decode, encode, Resource, ResourceState, Revision},
};
use serde_json::json;
use std::collections::BTreeSet;

pub(super) fn supports(op: &str, version: u32) -> bool {
    match op {
        "store.listing_get" => (1..=2).contains(&version),
        "store.listing_create"
        | "store.listing_update"
        | "store.package_deposit"
        | "store.package_get" => version == 1,
        _ => false,
    }
}
pub(super) fn listing(ctx: &mut Context<'_>, id: &str, seller: bool) -> Result<Resource> {
    let result = (|| {
        let resource = ctx.tx.resource_record(&ctx.resolve(id, false)?)?;
        require(
            resource.resource_type == "listing" && resource.parent.as_deref() == Some("t_store"),
            "listing_not_found",
        )?;
        ctx.check(&resource.id, Check::Read)?;
        if seller {
            require(
                resource.owner == super::signed_subject(ctx)?,
                "listing_not_found",
            )?;
        }
        Ok(resource)
    })();
    result.map_err(|error: Error| match error.0 {
        "not_found" | "permission_denied" | "credential_ceiling" | "certificate_gate"
        | "ancestor_inactive" => Error("listing_not_found"),
        _ => error,
    })
}
pub(super) fn body(
    ctx: &Context<'_>,
    resource: &Resource,
    revision: Option<String>,
) -> Result<(Json, Revision)> {
    let revision = ctx.tx.revision(&ResourceRef {
        id: resource.id.clone(),
        revision,
    })?;
    let bytes = ctx
        .contents
        .read_bytes(&revision.content, msg_core::MAX_BYTES as u64)?;
    let mut value =
        Json::parse(std::str::from_utf8(&bytes).map_err(|_| Error("invalid_listing"))?)?
            .into_object()?;
    value
        .entry("mode".into())
        .or_insert_with(|| Json::string("sale"));
    Ok((Json::from_object(value), revision))
}
pub(super) fn package(tx: &PgSession, id: &str) -> Result<Json> {
    let row=tx.one("SELECT id,listing_id,listing_revision,seller,revision,kind,manifest,payload_refs,digest,total_size,delivery_mode,deposited_at FROM store_packages WHERE id=$1",&[id.into()])?.ok_or(Error("package_not_found"))?;
    encode(
        &json!({"id":row.text(0)?,"listing_id":row.text(1)?,"listing_revision":row.text(2)?,"seller":row.text(3)?,"revision":row.text(4)?,"kind":row.text(5)?,"manifest":row.json(6)?,"payload_refs":row.json(7)?,"digest":row.text(8)?,"total_size":row.integer(9)?,"delivery_mode":row.text(10)?,"deposited_at":row.text(11)?}),
    )
}
fn public(resource: &Resource, body: &Json) -> Result<Json> {
    let mut result = body.as_object()?.clone();
    for (name, value) in [
        ("listing_id", encode(&resource.id)?),
        ("listing_revision", encode(&resource.revision)?),
        ("seller", encode(&resource.owner)?),
        ("terms_revision", encode(&resource.revision)?),
        ("terms_digest", encode(&hash(field(body, "terms")?)?)?),
    ] {
        result.insert(name.into(), value);
    }
    Ok(Json::from_object(result))
}
fn validate(body: &Json, now: Timestamp) -> Result<()> {
    require(text(body, "mode")? == "sale", "listing_mode_unsupported")?;
    require(
        text(body, "currency_id")? == "primary",
        "unsupported_currency",
    )?;
    require(
        (1..=1_000_000_000_000_000_000).contains(&integer(body, "price_minor")?),
        "invalid_price",
    )?;
    require(
        (1..=1_000_000_000).contains(&integer(body, "quantity")?),
        "invalid_quantity",
    )?;
    require(body.canonical()?.len() <= 65536, "listing_too_large")?;
    if !field(body, "expires_at")?.is_null() && text(body, "state")? != "closed" {
        require(
            Timestamp::parse(text(body, "expires_at")?)? > now,
            "listing_expired",
        )?;
    }
    if text(body, "delivery_mode")? == "service" {
        require(
            text(body, "item_kind")? == "service" && field(body, "package_ref")?.is_null(),
            "invalid_delivery_mode",
        )?;
    } else {
        require(
            text(body, "item_kind")? != "service",
            "invalid_delivery_mode",
        )?;
    }
    if text(body, "state")? == "active" && text(body, "delivery_mode")? == "managed_instant" {
        require(!field(body, "package_ref")?.is_null(), "package_required")?;
    }
    Ok(())
}
fn result(resource: &Resource, body: &Json) -> Result<HandlerOutput> {
    output(
        json!({"listing":public(resource,body)?,"generation":resource.generation}),
        vec![ResourceRef {
            id: resource.id.clone(),
            revision: resource.revision.clone(),
        }],
    )
}
pub(super) fn handle(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    match ctx.request.field("operation")?.as_str()? {
        "store.listing_create" => {
            let seller = super::signed_subject(ctx)?.to_owned();
            ctx.authorizer.require_base(
                ctx.principal,
                &ctx.operation()?,
                &seller,
                ctx.tx,
                ctx.now,
            )?;
            ctx.check("t_store", Check::Create)?;
            let mut value = ctx.args()?.clone();
            let name = value
                .remove("name")
                .ok_or(Error("missing_argument"))?
                .as_str()?
                .to_owned();
            value
                .entry("mode".into())
                .or_insert_with(|| Json::string("sale"));
            value
                .entry("expires_at".into())
                .or_insert(Json::parse("null")?);
            value.insert("package_ref".into(), Json::parse("null")?);
            value.insert("state".into(), Json::string("draft"));
            let value = Json::from_object(value);
            validate(&value, ctx.now)?;
            let resource = content::create(
                ctx,
                Create {
                    parent: "t_store".into(),
                    kind: "listing",
                    name: Some(name),
                    id: None,
                    body: Some(Body::Bytes(
                        value.canonical()?.into_bytes(),
                        "application/json".into(),
                    )),
                    mode: Some(0o644),
                    revision: RevisionOptions::default(),
                },
            )?;
            require(resource.owner == seller, "seller_mismatch")?;
            result(&resource, &value)
        }
        "store.listing_update" => {
            let id = ctx.text("id")?.to_owned();
            let resource = listing(ctx, &id, true)?;
            ctx.check(&resource.id, Check::Write)?;
            content::assert_generation(ctx, &resource)?;
            let (old, _) = body(ctx, &resource, None)?;
            require(text(&old, "mode")? == "sale", "listing_mode_unsupported")?;
            let mut changes = ctx.args()?.clone();
            changes.remove("id");
            require(!changes.is_empty(), "empty_listing_update")?;
            let mut value = old.as_object()?.clone();
            value.extend(changes);
            let value = Json::from_object(value);
            require(
                text(&old, "state")? != "closed" || text(&value, "state")? == "closed",
                "listing_closed",
            )?;
            if !field(&value, "package_ref")?.is_null() {
                let p = package(ctx.tx, text(&value, "package_ref")?)?;
                require(
                    text(&p, "listing_id")? == resource.id
                        && text(&p, "seller")? == resource.owner
                        && field(&p, "kind")? == field(&value, "item_kind")?
                        && field(&p, "delivery_mode")? == field(&value, "delivery_mode")?,
                    "package_not_found",
                )?;
            }
            validate(&value, ctx.now)?;
            let updated = content::revise(
                ctx,
                resource,
                Body::Bytes(value.canonical()?.into_bytes(), "application/json".into()),
                RevisionOptions::default(),
            )?;
            result(&updated, &value)
        }
        "store.listing_get" => {
            if ctx
                .args()?
                .get("source")
                .map(|s| s.as_str())
                .transpose()?
                .unwrap_or("resource")
                == "server_offer"
            {
                return super::offers::listing(ctx);
            }
            let id = ctx.text("id")?.to_owned();
            let resource = listing(ctx, &id, false)?;
            let revision = ctx
                .args()?
                .get("revision")
                .map(|s| s.as_str().map(str::to_owned))
                .transpose()?;
            let (value, rev) = body(ctx, &resource, revision.clone())?;
            let mut result = public(&resource, &value)?.into_object()?;
            if text(&value, "mode")? == "bounty" {
                let live = bounty_projection(ctx, &resource)?;
                for (to, from) in [
                    ("current_state", "state"),
                    ("pause_reason", "pause_reason"),
                    ("current_budget_minor", "budget_minor"),
                    ("escrow_balance_minor", "escrow_balance_minor"),
                    ("paid_claims", "paid_claims"),
                ] {
                    result.insert(to.into(), field(&live, from)?.clone());
                }
                if revision.is_none() {
                    for name in ["state", "budget_minor"] {
                        result.insert(name.into(), field(&live, name)?.clone());
                    }
                }
            }
            result.insert("listing_revision".into(), Json::string(&rev.id));
            result.insert("terms_revision".into(), Json::string(&rev.id));
            output(
                json!({"listing":Json::from_object(result)}),
                vec![ResourceRef {
                    id: resource.id,
                    revision: Some(rev.id),
                }],
            )
        }
        "store.package_deposit" => deposit(ctx),
        "store.package_get" => {
            let value = package(ctx.tx, ctx.text("id")?)?;
            require(
                text(&value, "seller")? == super::signed_subject(ctx)?,
                "package_not_found",
            )?;
            listing(ctx, text(&value, "listing_id")?, true)?;
            output(json!({"package":value}), vec![])
        }
        _ => Err(Error("unknown_operation")),
    }
}
pub(super) fn no_direct_ancestor(tx: &PgSession, resource: &Resource) -> Result<()> {
    let mut ids: Vec<_> = tx
        .ancestors(&resource.id)?
        .into_iter()
        .map(|r| r.id)
        .collect();
    ids.push(resource.id.clone());
    for id in ids {
        require(
            tx.one(
                "SELECT 1 FROM dm_conversations WHERE resource_id=$1",
                &[id.into()],
            )?
            .is_none(),
            "payload_private_conversation",
        )?;
    }
    Ok(())
}
fn deposit(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let id = ctx.text("listing_id")?.to_owned();
    let resource = listing(ctx, &id, true)?;
    let (value, _) = body(ctx, &resource, None)?;
    require(text(&value, "mode")? == "sale", "listing_mode_unsupported")?;
    require(
        resource.revision.as_deref() == Some(ctx.text("listing_revision")?),
        "listing_revision_conflict",
    )?;
    require(
        text(&value, "delivery_mode")? == "managed_instant"
            && matches!(text(&value, "item_kind")?, "file" | "bundle" | "text")
            && text(&value, "state")? != "closed",
        "package_mode_unsupported",
    )?;
    let manifest = ctx.arg("manifest")?.clone();
    require(manifest.canonical()?.len() <= 65536, "manifest_too_large")?;
    let input: Vec<ResourceRef> = decode(ctx.arg("payload_refs")?)?;
    let mut refs = vec![];
    let mut blobs = vec![];
    let mut total = 0_i64;
    let mut seen = BTreeSet::new();
    for reference in input {
        require(reference.revision.is_some(), "payload_revision_required")?;
        let rid = ctx.resolve(&reference.id, false)?;
        let item = ctx.tx.resource_record(&rid)?;
        require(
            item.owner == resource.owner
                && item.state == ResourceState::Active
                && matches!(item.resource_type.as_str(), "file" | "attachment"),
            "payload_not_owned",
        )?;
        no_direct_ancestor(ctx.tx, &item)?;
        ctx.check(&rid, Check::Read)?;
        let revision = ctx.tx.revision(&ResourceRef {
            id: rid.clone(),
            revision: reference.revision,
        })?;
        if text(&value, "item_kind")? == "text" {
            require(
                revision.content.media_type.starts_with("text/"),
                "text_payload_required",
            )?;
        }
        total = total
            .checked_add(revision.content.size)
            .ok_or(Error("package_too_large"))?;
        require(total <= 1_099_511_627_776, "package_too_large")?;
        require(
            seen.insert((rid.clone(), revision.id.clone())),
            "duplicate_payload_ref",
        )?;
        refs.push(encode(
            &json!({"id":rid,"revision":revision.id,"blob":revision.content}),
        )?);
        blobs.push(revision.content);
    }
    require((1..=32).contains(&refs.len()), "invalid_payload_refs")?;
    require(
        text(&value, "item_kind")? == "bundle" || refs.len() == 1,
        "file_package_single_payload",
    )?;
    let id = new_id("pkg")?;
    let revision = new_id("pv")?;
    let kind = text(&value, "item_kind")?;
    let snapshot = encode(
        &json!({"seller":resource.owner,"listing_id":resource.id,"listing_revision":resource.revision,"revision":revision,"kind":kind,"manifest":manifest,"payload_refs":refs,"total_size":total,"delivery_mode":"managed_instant"}),
    )?;
    let digest = hash(&snapshot)?;
    for blob in blobs {
        ctx.pin(&blob, &revision)?;
    }
    ctx.tx.execute("INSERT INTO store_packages(id,listing_id,listing_revision,seller,revision,kind,manifest,payload_refs,digest,total_size,delivery_mode,deposited_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'managed_instant',$11)",&[id.as_str().into(),resource.id.as_str().into(),resource.revision.as_deref().into(),resource.owner.as_str().into(),revision.as_str().into(),kind.into(),manifest.canonical()?.into(),encode(&refs)?.canonical()?.into(),digest.as_str().into(),total.into(),super::time(ctx.now).into()])?;
    output(
        json!({"package":{"id":id,"revision":revision,"digest":digest,"total_size":total}}),
        vec![ResourceRef {
            id: resource.id,
            revision: resource.revision,
        }],
    )
}
fn bounty_projection(ctx: &Context<'_>, resource: &Resource) -> Result<Json> {
    let r=ctx.tx.one("SELECT publisher,escrow_subject,reward_minor,budget_minor,max_claims,claim_limit_per_subject,verifier_id,verifier_version,eligibility,state,pause_reason,expires_at FROM bounty_listings WHERE listing_id=$1",&[resource.id.as_str().into()])?.ok_or(Error("bounty_not_found"))?;
    let account = ctx
        .tx
        .one(
            "SELECT kind,subject_id,source_id FROM ledger_accounts WHERE id=$1",
            &[r.text(1)?.into()],
        )?
        .ok_or(Error("bounty_escrow_mismatch"))?;
    require(
        account.text(0)? == "bounty_escrow"
            && account.optional_text(1)?.is_none()
            && account.text(2)? == resource.id,
        "bounty_escrow_mismatch",
    )?;
    let (body, _) = body(ctx, resource, None)?;
    require(
        resource.resource_type == "listing"
            && resource.owner == r.text(0)?
            && text(&body, "mode")? == "bounty"
            && text(&body, "currency_id")? == "primary",
        "bounty_contract_mismatch",
    )?;
    for (name, v) in [
        ("reward_minor", encode(&r.integer(2)?)?),
        ("max_claims", encode(&r.integer(4)?)?),
        ("claim_limit_per_subject", encode(&r.integer(5)?)?),
        ("verifier_id", encode(&r.text(6)?)?),
        ("verifier_version", encode(&r.integer(7)?)?),
        ("eligibility", r.json(8)?),
        ("expires_at", encode(&r.optional_text(11)?)?),
    ] {
        require(field(&body, name)? == &v, "bounty_contract_mismatch")?;
    }
    let balance = super::ledger::balance(ctx.tx, r.text(1)?)?;
    let claims = ctx
        .tx
        .one(
            "SELECT count(*) FROM bounty_claims WHERE listing_id=$1 AND status='paid'",
            &[resource.id.as_str().into()],
        )?
        .ok_or(Error("bounty_contract_mismatch"))?
        .integer(0)?;
    let mut state = r.text(9)?;
    let mut reason = r.optional_text(10)?;
    if state != "closed" {
        if r.optional_text(11)?
            .map(Timestamp::parse)
            .transpose()?
            .is_some_and(|t| t <= ctx.now)
        {
            state = "paused";
            reason = Some("expired");
        } else if balance < r.integer(2)? {
            state = "paused";
            reason = Some("out_of_budget");
        } else if claims >= r.integer(4)? {
            state = "paused";
            reason = Some("claims_exhausted");
        }
    }
    encode(
        &json!({"state":state,"pause_reason":reason,"budget_minor":r.integer(3)?,"escrow_balance_minor":balance,"paid_claims":claims}),
    )
}
