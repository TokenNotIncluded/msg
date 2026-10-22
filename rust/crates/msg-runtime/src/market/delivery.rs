//! Verified buyer delivery with bounded reads and explicit acknowledgement.
use super::{
    checkout, email, field, hash, integer, ledger,
    order::{self, Mutation, Order, Settlement},
    require, store, text, time,
};
use crate::{
    content::{self, Body, Create, RevisionOptions},
    context::{new_id, output, Context, HandlerOutput},
};
use msg_core::{b64, digest, Error, Json, Result};
use msg_identity::{
    authentication::Entry,
    authorization::{AccessRequirement, Check},
    models::{ResourceRef, Timestamp},
};
use msg_storage::{
    content::GitContentStore,
    postgres::PgSession,
    records::{decode, encode, BlobRef, ResourceState, TransferSession},
};
use serde_json::json;
use std::io;

pub(super) fn supports(op: &str, v: u32) -> bool {
    match op {
        "delivery.get" | "delivery.accept" | "delivery.prepare" => (1..=2).contains(&v),
        "delivery.claim"
        | "delivery.notify"
        | "delivery.submit"
        | "delivery.part_get"
        | "delivery.transfer_open" => v == 1,
        _ => false,
    }
}
pub(super) fn read(tx: &PgSession, id: &str) -> Result<Option<Json>> {
    tx.one("SELECT id,order_id,recipient_subject,kind,payload_refs,manifest,package_digest,delivery_digest,channel,state,prepared_at,claimed_at,receipt FROM store_deliveries WHERE order_id=$1",&[id.into()])?.map(|r|encode(&json!({"id":r.text(0)?,"order_id":r.text(1)?,"recipient_subject":r.text(2)?,"kind":r.text(3)?,"payload_refs":r.json(4)?,"manifest":r.json(5)?,"package_digest":r.optional_text(6)?,"delivery_digest":r.text(7)?,"channel":r.text(8)?,"state":r.text(9)?,"prepared_at":r.text(10)?,"claimed_at":r.optional_text(11)?,"receipt":r.optional_text(12)?.map(Json::parse).transpose()?}))).transpose()
}
pub(super) fn summary(delivery: &Json) -> Result<Json> {
    encode(
        &json!({"delivery_id":field(delivery,"id")?,"order_id":field(delivery,"order_id")?,"state":field(delivery,"state")?,"channel":field(delivery,"channel")?,"package_digest":field(delivery,"package_digest")?,"delivery_digest":field(delivery,"delivery_digest")?}),
    )
}
pub(super) fn verify_blob(contents: &GitContentStore, blob: &BlobRef) -> Result<()> {
    contents
        .read_to(blob, None, &mut io::sink())
        .map_err(|e| match e.0 {
            "content_missing" => Error("package_missing"),
            "content_truncated" | "content_size_mismatch" | "content_digest_mismatch" => {
                Error("package_digest_mismatch")
            }
            _ => e,
        })
}
struct Package {
    kind: String,
    manifest: Json,
    refs: Vec<Json>,
}
fn package(
    tx: &PgSession,
    contents: &GitContentStore,
    order: &Order,
    legacy: bool,
) -> Result<Package> {
    let error = if legacy {
        "delivery_package_mismatch"
    } else {
        "package_digest_mismatch"
    };
    let id = order.package_id.as_deref().ok_or(Error(error))?;
    let p = store::package(tx, id).map_err(|e| {
        if e.0 == "package_not_found" {
            Error(if legacy { error } else { "package_missing" })
        } else {
            e
        }
    })?;
    require(
        text(&p, "listing_id")? == order.listing_id
            && text(&p, "seller")? == order.seller
            && Some(text(&p, "revision")?) == order.package_revision.as_deref()
            && Some(text(&p, "digest")?) == order.package_digest.as_deref()
            && text(&p, "delivery_mode")? == "managed_instant"
            && if legacy {
                matches!(text(&p, "kind")?, "file" | "bundle")
            } else {
                matches!(text(&p, "kind")?, "file" | "bundle" | "text")
            },
        error,
    )?;
    let mut snapshot = p.as_object()?.clone();
    snapshot.remove("id");
    snapshot.remove("digest");
    snapshot.remove("deposited_at");
    let size = integer(&p, "total_size")?;
    require(
        Some(hash(&Json::from_object(snapshot))?.as_str()) == order.package_digest.as_deref()
            && (0..=if legacy { 1_048_576 } else { 1_099_511_627_776 }).contains(&size),
        error,
    )?;
    let refs = field(&p, "payload_refs")?.as_array()?;
    require(
        (1..=32).contains(&refs.len())
            && (!legacy || text(&p, "kind")? == "bundle" || refs.len() == 1),
        error,
    )?;
    let mut total = 0_i64;
    for reference in refs {
        let blob: BlobRef = decode(field(reference, "blob")?)?;
        require(blob.size >= 0 && (!legacy || blob.size <= 1_048_576), error)?;
        if legacy {
            let bytes = contents.read_bytes(&blob, 1_048_576)?;
            require(
                bytes.len() as i64 == blob.size && digest(&bytes) == blob.digest,
                error,
            )?;
        } else {
            verify_blob(contents, &blob)?;
        }
        total = total.checked_add(blob.size).ok_or(Error(error))?;
    }
    require(total == size, error)?;
    Ok(Package {
        kind: text(&p, "kind")?.into(),
        manifest: field(&p, "manifest")?.clone(),
        refs: refs.to_vec(),
    })
}
fn body_for(order: &Order, id: &str, kind: &str, manifest: &Json, refs: &[Json]) -> Result<Json> {
    let commitment = match &order.package_digest {
        Some(digest) => digest.clone(),
        None => hash(
            &json!({"listing_revision":order.listing_revision,"terms_digest":order.terms_digest}),
        )?,
    };
    encode(
        &json!({"delivery_id":id,"order_id":order.id,"recipient_subject":order.buyer,"kind":kind,"manifest":manifest,"payload_refs":refs,"package_digest":commitment,"channel":"site"}),
    )
}
pub(super) fn verify_managed(
    tx: &PgSession,
    contents: &GitContentStore,
    order: &Order,
    delivery: &Json,
) -> Result<()> {
    require(
        order.delivery_target == encode(&json!({"subject_id":order.buyer,"channel":"site"}))?
            && text(delivery, "order_id")? == order.id
            && text(delivery, "recipient_subject")? == order.buyer
            && text(delivery, "channel")? == "site",
        "delivery_recipient_mismatch",
    )?;
    require(
        Some(text(delivery, "package_digest")?) == order.package_digest.as_deref(),
        "delivery_mismatch",
    )?;
    let package = package(tx, contents, order, true)?;
    require(
        text(delivery, "kind")? == package.kind
            && field(delivery, "manifest")? == &package.manifest
            && field(delivery, "payload_refs")? == &encode(&package.refs)?
            && text(delivery, "delivery_digest")?
                == hash(&body_for(
                    order,
                    text(delivery, "id")?,
                    &package.kind,
                    &package.manifest,
                    &package.refs,
                )?)?,
        "delivery_mismatch",
    )
}
fn insert(
    tx: &PgSession,
    order: &Order,
    id: &str,
    package: &Package,
    now: Timestamp,
) -> Result<Json> {
    let body = body_for(order, id, &package.kind, &package.manifest, &package.refs)?;
    tx.execute("INSERT INTO store_deliveries(id,order_id,recipient_subject,kind,payload_refs,manifest,package_digest,delivery_digest,channel,state,prepared_at,claimed_at,receipt) VALUES($1,$2,$3,$4,$5,$6,$7,$8,'site','prepared',$9,NULL,NULL)",&[id.into(),order.id.as_str().into(),order.buyer.as_str().into(),package.kind.as_str().into(),encode(&package.refs)?.canonical()?.into(),package.manifest.canonical()?.into(),text(&body,"package_digest")?.into(),hash(&body)?.into(),time(now).into()])?;
    read(tx, &order.id)?.ok_or(Error("delivery_not_found"))
}
pub(super) fn prepare_managed(ctx: &Context<'_>, order: &Order) -> Result<Json> {
    require(
        order.state == "funded" && order.delivered_at.is_none(),
        "order_not_deliverable",
    )?;
    require(
        order.delivery_target == encode(&json!({"subject_id":order.buyer,"channel":"site"}))?,
        "delivery_recipient_mismatch",
    )?;
    require(
        order.currency_id == "primary"
            && ledger::balance(ctx.tx, &order.escrow_subject)? == order.total_price_minor,
        "escrow_balance_mismatch",
    )?;
    let package = package(ctx.tx, ctx.contents, order, true)?;
    let delivery = insert(ctx.tx, order, &new_id("dlv")?, &package, ctx.now)?;
    require(ctx.tx.execute("UPDATE store_orders SET state='delivered',delivered_at=$1 WHERE id=$2 AND buyer=$3 AND state='funded' AND delivered_at IS NULL",&[time(ctx.now).into(),order.id.as_str().into(),order.buyer.as_str().into()])?==1,"order_not_deliverable")?;
    Ok(delivery)
}
fn prepare(
    ctx: &mut Context<'_>,
    mutation: &mut Mutation,
    package: Package,
    envelope: Option<Json>,
) -> Result<Json> {
    require(
        mutation.order.state == "funded" && mutation.order.delivered_at.is_none(),
        "order_not_deliverable",
    )?;
    email::validate_target(ctx.tx, &mutation.order, false, envelope.is_some())?;
    let id = new_id("dlv")?;
    for reference in &package.refs {
        let blob: BlobRef = decode(field(reference, "blob")?)?;
        ctx.pin(&blob, &id)?;
    }
    let delivery = insert(ctx.tx, &mutation.order, &id, &package, ctx.now)?;
    if let Some(envelope) = envelope {
        ctx.tx.execute(
            "INSERT INTO delivery_envelopes(order_id,body) VALUES($1,$2)",
            &[
                mutation.order.id.as_str().into(),
                envelope.canonical()?.into(),
            ],
        )?;
    }
    mutation.transition(
        ctx.tx,
        "delivered",
        ctx.now,
        ctx.principal
            .actor()
            .ok_or(Error("authentication_required"))?,
        ctx.request.field("request_id")?.as_str()?,
        "verified_delivery",
    )?;
    ctx.tx.execute(
        "UPDATE store_orders SET delivered_at=$1 WHERE id=$2",
        &[time(ctx.now).into(), mutation.order.id.as_str().into()],
    )?;
    mutation.order.delivered_at = Some(time(ctx.now));
    let order = &mutation.order;
    let message = encode(
        &json!({"id":new_id("message")?,"sender":order.seller,"actor":order.seller,"recipient":order.buyer,"order_id":order.id,"time":ctx.now,"resource":{"id":order.buyer,"revision":null},"state":"delivered","source":"system","href":format!("/_orders/{}/_delivery",order.id)}),
    )?;
    ctx.tx.execute(
        "INSERT INTO messages VALUES($1,$2,$3,$4,$5,$6)",
        &[
            text(&message, "id")?.into(),
            order.seller.as_str().into(),
            order.buyer.as_str().into(),
            order.buyer.as_str().into(),
            format!("delivery:{}", order.id).into(),
            message.canonical()?.into(),
        ],
    )?;
    email::enqueue(ctx, &mut mutation.order)?;
    Ok(delivery)
}
pub(super) fn automatic(ctx: &mut Context<'_>, mutation: &mut Mutation) -> Result<()> {
    let package = match package(ctx.tx, ctx.contents, &mutation.order, false) {
        Ok(p) => p,
        Err(error @ Error("package_missing" | "package_digest_mismatch")) => {
            let buyer = mutation.order.buyer.clone();
            mutation.settle(
                ctx.tx,
                &ctx.settings.receipt_signer,
                Settlement {
                    now: ctx.now,
                    actor: &buyer,
                    request: ctx.request.field("request_id")?.as_str()?,
                    reason: error.0,
                    refund: mutation.order.total_price_minor,
                    decision: None,
                },
            )?;
            return Ok(());
        }
        Err(error) => return Err(error),
    };
    prepare(ctx, mutation, package, None)?;
    if integer(&mutation.locked, "version")? == 4 {
        return Ok(());
    }
    let buyer = mutation.order.buyer.clone();
    mutation.transition(
        ctx.tx,
        "accepted",
        ctx.now,
        &buyer,
        ctx.request.field("request_id")?.as_str()?,
        "managed_instant_verified",
    )?;
    mutation.settle(
        ctx.tx,
        &ctx.settings.receipt_signer,
        Settlement {
            now: ctx.now,
            actor: &buyer,
            request: ctx.request.field("request_id")?.as_str()?,
            reason: "managed_instant_verified",
            refund: 0,
            decision: None,
        },
    )?;
    Ok(())
}
pub(super) fn verified(
    tx: &PgSession,
    contents: &GitContentStore,
    order: &Order,
    locked: &Json,
    bytes: bool,
) -> Result<Json> {
    let delivery = read(tx, &order.id)?.ok_or(Error("delivery_not_found"))?;
    email::validate_target(tx, order, false, false)?;
    require(
        text(&delivery, "recipient_subject")? == order.buyer,
        "delivery_recipient_mismatch",
    )?;
    let refs = field(&delivery, "payload_refs")?.as_array()?;
    let expected = body_for(
        order,
        text(&delivery, "id")?,
        text(&delivery, "kind")?,
        field(&delivery, "manifest")?,
        refs,
    )?;
    require(
        field(&delivery, "package_digest")? == field(&expected, "package_digest")?
            && text(&delivery, "delivery_digest")? == hash(&expected)?,
        "delivery_digest_mismatch",
    )?;
    match text(field(locked, "listing")?, "delivery_mode")? {
        "managed_instant" => {
            let package = store::package(
                tx,
                order
                    .package_id
                    .as_deref()
                    .ok_or(Error("delivery_digest_mismatch"))?,
            )
            .map_err(|e| {
                if e.0 == "package_not_found" {
                    Error("delivery_digest_mismatch")
                } else {
                    e
                }
            })?;
            require(
                Some(text(&package, "digest")?) == order.package_digest.as_deref()
                    && field(&package, "manifest")? == field(&delivery, "manifest")?
                    && field(&package, "payload_refs")? == field(&delivery, "payload_refs")?,
                "delivery_digest_mismatch",
            )?;
        }
        "sealed_manual" => {
            let envelope = tx
                .one(
                    "SELECT body FROM delivery_envelopes WHERE order_id=$1",
                    &[order.id.as_str().into()],
                )?
                .ok_or(Error("delivery_digest_mismatch"))?
                .json(0)?;
            require(
                text(&envelope, "recipient_subject")? == order.buyer
                    && field(&envelope, "recipient_key")? == field(locked, "recipient_key")?
                    && Some(field(&envelope, "ciphertext_ref")?) == refs.first(),
                "delivery_recipient_mismatch",
            )?;
        }
        _ => {}
    }
    if bytes {
        for reference in refs {
            verify_blob(contents, &decode(field(reference, "blob")?)?)?;
        }
    }
    Ok(delivery)
}
fn buyer(ctx: &Context<'_>, signed: bool) -> Result<Order> {
    let subject = if signed {
        super::signed_subject(ctx)?
    } else {
        super::viewer(ctx)?
    };
    let order = checkout::owned(ctx, ctx.text("order_id")?, subject)?;
    require(order.buyer == subject, "order_not_found")?;
    Ok(order)
}
fn modern_read(ctx: &Context<'_>, order: &Order) -> Result<HandlerOutput> {
    let locked = order::get_contract(ctx.tx, &order.id)?;
    let delivery = verified(ctx.tx, ctx.contents, order, &locked, true)?;
    let mut payloads = vec![];
    let mut budget = 65536_i64;
    for (index, reference) in field(&delivery, "payload_refs")?
        .as_array()?
        .iter()
        .enumerate()
    {
        let blob: BlobRef = decode(field(reference, "blob")?)?;
        let mut item=encode(&json!({"id":field(reference,"id")?,"revision":field(reference,"revision")?,"digest":blob.digest,"size":blob.size,"media_type":blob.media_type,"payload_index":index}))?.into_object()?;
        if blob.size <= budget {
            item.insert(
                "data".into(),
                Json::string(b64(&ctx.contents.read_bytes(&blob, 65536)?)),
            );
            budget -= blob.size;
        } else {
            item.insert("transfer".into(),encode(&json!({"operation":"delivery.transfer_open","arguments":{"order_id":order.id,"delivery_digest":field(&delivery,"delivery_digest")?,"payload_index":index}}))?);
            item.insert("next".into(),encode(&json!({"operation":"delivery.part_get","arguments":{"order_id":order.id,"delivery_digest":field(&delivery,"delivery_digest")?,"payload_index":index,"offset":0,"length":65536_i64.min(blob.size)}}))?);
        }
        payloads.push(Json::from_object(item));
    }
    let mut result = delivery.as_object()?.clone();
    for key in ["id", "payload_refs", "receipt"] {
        result.remove(key);
    }
    result.insert("delivery_id".into(), field(&delivery, "id")?.clone());
    result.insert("payloads".into(), encode(&payloads)?);
    output(json!({"delivery":Json::from_object(result)}), vec![])
}
fn legacy_read(ctx: &Context<'_>, order: &Order) -> Result<HandlerOutput> {
    let delivery = read(ctx.tx, &order.id)?.ok_or(Error("delivery_not_found"))?;
    verify_managed(ctx.tx, ctx.contents, order, &delivery)?;
    let mut payloads = vec![];
    for reference in field(&delivery, "payload_refs")?.as_array()? {
        let blob: BlobRef = decode(field(reference, "blob")?)?;
        payloads.push(encode(&json!({"id":field(reference,"id")?,"revision":field(reference,"revision")?,"media_type":blob.media_type,"digest":blob.digest,"size":blob.size,"data":b64(&ctx.contents.read_bytes(&blob,1_048_576)?)}))?);
    }
    output(
        json!({"delivery":{"delivery_id":field(&delivery,"id")?,"order_id":order.id,"recipient_subject":order.buyer,"kind":field(&delivery,"kind")?,"manifest":field(&delivery,"manifest")?,"package_digest":field(&delivery,"package_digest")?,"delivery_digest":field(&delivery,"delivery_digest")?,"channel":"site","state":field(&delivery,"state")?,"prepared_at":field(&delivery,"prepared_at")?,"claimed_at":field(&delivery,"claimed_at")?,"payloads":payloads,"notification":email::legacy_status(ctx,&order.id)?}}),
        vec![],
    )
}
pub(super) fn handle(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let operation = ctx.request.field("operation")?.as_str()?.to_owned();
    let version = ctx
        .request
        .field("contract_version")?
        .as_integer()?
        .to_owned();
    if operation == "delivery.submit" {
        return submit(ctx);
    }
    let signed = !matches!(operation.as_str(), "delivery.get" | "delivery.part_get");
    let order = buyer(ctx, signed)?;
    match operation.as_str() {
        "delivery.get" => {
            if version == "2" || checkout::has_contract(ctx.tx, &order.id)? {
                modern_read(ctx, &order)
            } else {
                legacy_read(ctx, &order)
            }
        }
        "delivery.prepare" if version == "1" => output(
            json!({"delivery":summary(&prepare_managed(ctx,&order)?)?}),
            vec![],
        ),
        "delivery.prepare" => {
            let mut mutation = Mutation::begin(
                ctx.tx,
                ctx.contents,
                &ctx.settings.receipt_signer,
                &order.id,
            )?;
            require(
                text(field(&mutation.locked, "listing")?, "delivery_mode")? == "managed_instant",
                "seller_delivery_required",
            )?;
            if mutation.order.state == "funded" {
                automatic(ctx, &mut mutation)?;
            }
            let state = mutation.order.state.clone();
            let resources = mutation.publish_context(ctx)?.into_iter().collect();
            output(json!({"order_id":order.id,"state":state}), resources)
        }
        "delivery.accept" => {
            if version == "2" || checkout::has_contract(ctx.tx, &order.id)? {
                accept(ctx, &order)
            } else {
                let (receipt, decision, delivery) =
                    super::legacy::settle(ctx, &order, "buyer_accept")?;
                output(
                    json!({"order_id":order.id,"state":"settled","delivery_id":field(&delivery.ok_or(Error("delivery_not_found"))?,"id")?,"receipt":receipt,"decision":decision}),
                    vec![],
                )
            }
        }
        "delivery.claim" => {
            let delivery = read(ctx.tx, &order.id)?.ok_or(Error("delivery_not_found"))?;
            verify_managed(ctx.tx, ctx.contents, &order, &delivery)?;
            require(
                text(&delivery, "delivery_digest")? == ctx.text("delivery_digest")?,
                "delivery_mismatch",
            )?;
            super::legacy::verify_checkout(ctx.tx, ctx, &order, &delivery)?;
            require(
                text(&delivery, "state")? == "prepared",
                "delivery_not_acceptable",
            )?;
            require(ctx.tx.execute("UPDATE store_deliveries SET state='claimed',claimed_at=$1 WHERE order_id=$2 AND recipient_subject=$3 AND state='prepared'",&[time(ctx.now).into(),order.id.as_str().into(),order.buyer.as_str().into()])?==1,"delivery_not_acceptable")?;
            output(
                json!({"order_id":order.id,"delivery_id":field(&delivery,"id")?,"state":"claimed"}),
                vec![],
            )
        }
        "delivery.notify" => {
            let delivery = read(ctx.tx, &order.id)?.ok_or(Error("delivery_not_found"))?;
            verify_managed(ctx.tx, ctx.contents, &order, &delivery)?;
            let status = email::queue_legacy(
                ctx,
                &order,
                ctx.args()?
                    .get("enabled")
                    .map(Json::as_bool)
                    .transpose()?
                    .unwrap_or(true),
            )?;
            output(json!({"order_id":order.id,"notification":status}), vec![])
        }
        "delivery.part_get" => part(ctx, &order),
        "delivery.transfer_open" => transfer(ctx, &order),
        _ => Err(Error("unknown_operation")),
    }
}
fn accept(ctx: &mut Context<'_>, order: &Order) -> Result<HandlerOutput> {
    let mut mutation = Mutation::begin(
        ctx.tx,
        ctx.contents,
        &ctx.settings.receipt_signer,
        &order.id,
    )?;
    let delivery = verified(ctx.tx, ctx.contents, order, &mutation.locked, true)?;
    require(
        text(&delivery, "delivery_digest")? == ctx.text("delivery_digest")?,
        "delivery_mismatch",
    )?;
    require(
        matches!(order.state.as_str(), "delivered" | "settled"),
        "delivery_not_acceptable",
    )?;
    if text(&delivery, "state")? == "claimed" {
        return output(
            json!({"order_id":order.id,"state":order.state,"delivery_id":field(&delivery,"id")?,"receipt":field(&delivery,"receipt")?}),
            vec![],
        );
    }
    let mut settlement = Json::parse("[]")?;
    if order.state == "delivered" {
        mutation.transition(
            ctx.tx,
            "accepted",
            ctx.now,
            &order.buyer,
            ctx.request.field("request_id")?.as_str()?,
            "buyer_acceptance",
        )?;
        settlement = mutation.settle(
            ctx.tx,
            &ctx.settings.receipt_signer,
            Settlement {
                now: ctx.now,
                actor: &order.buyer,
                request: ctx.request.field("request_id")?.as_str()?,
                reason: "buyer_acceptance",
                refund: 0,
                decision: None,
            },
        )?;
    }
    let body = encode(
        &json!({"order_id":order.id,"delivery_id":field(&delivery,"id")?,"subject":order.buyer,"delivery_digest":field(&delivery,"delivery_digest")?,"request_digest":ctx.request.field("payload_digest")?,"claimed_at":ctx.now}),
    )?;
    let receipt = encode(
        &json!({"body":body,"signature":ctx.settings.receipt_signer.sign(body.canonical()?.as_bytes(),"delivery-ack")?}),
    )?;
    ctx.tx.execute(
        "UPDATE store_deliveries SET state='claimed',claimed_at=$1,receipt=$2 WHERE order_id=$3",
        &[
            time(ctx.now).into(),
            receipt.canonical()?.into(),
            order.id.as_str().into(),
        ],
    )?;
    let state = mutation.order.state.clone();
    let resources = mutation.publish_context(ctx)?.into_iter().collect();
    output(
        json!({"order_id":order.id,"state":state,"delivery_id":field(&delivery,"id")?,"receipt":receipt,"settlement_receipts":settlement}),
        resources,
    )
}
fn submit(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let seller = super::signed_subject(ctx)?.to_owned();
    let order = checkout::owned(ctx, ctx.text("order_id")?, &seller)?;
    require(order.seller == seller, "order_not_found")?;
    let mut mutation = Mutation::begin(
        ctx.tx,
        ctx.contents,
        &ctx.settings.receipt_signer,
        &order.id,
    )?;
    let mode = text(field(&mutation.locked, "listing")?, "delivery_mode")?.to_owned();
    require(
        matches!(mode.as_str(), "sealed_manual" | "service"),
        "order_delivery_mode_unsupported",
    )?;
    require(order.state == "funded", "order_not_deliverable")?;
    require(
        ctx.now
            < order::add(
                Timestamp::parse(
                    order
                        .funded_at
                        .as_deref()
                        .ok_or(Error("delivery_deadline_passed"))?,
                )?,
                integer(
                    field(field(&mutation.locked, "policy")?, "policy")?,
                    "delivery_timeout_seconds",
                )?,
            )?,
        "delivery_deadline_passed",
    )?;
    let reference: ResourceRef = decode(ctx.arg("payload_ref")?)?;
    require(reference.revision.is_some(), "payload_revision_required")?;
    let resource = ctx.tx.resource_record(&reference.id)?;
    require(
        resource.owner == seller
            && matches!(resource.resource_type.as_str(), "file" | "attachment")
            && resource.state == ResourceState::Active,
        "payload_not_owned",
    )?;
    store::no_direct_ancestor(ctx.tx, &resource)?;
    ctx.check(&resource.id, Check::Read)?;
    let revision = ctx.tx.revision(&reference)?;
    let blob = &revision.content;
    require(
        blob.digest == ctx.text("payload_digest")?,
        "delivery_digest_mismatch",
    )?;
    verify_blob(ctx.contents, blob)?;
    let payload = encode(&json!({"id":resource.id,"revision":revision.id,"blob":blob}))?;
    let (manifest, envelope) = if mode == "sealed_manual" {
        email::validate_target(ctx.tx, &order, false, true)?;
        let key = field(&mutation.locked, "recipient_key")?;
        require(
            ctx.args()?
                .get("recipient_key_id")
                .map(Json::as_str)
                .transpose()?
                == Some(text(key, "key_id")?),
            "delivery_recipient_mismatch",
        )?;
        let mut prefix = vec![];
        ctx.contents
            .read_to(blob, Some((0, 64_u64.min(blob.size as u64))), &mut prefix)?;
        require(
            matches!(
                blob.media_type.as_str(),
                "application/age" | "application/octet-stream"
            ) && prefix.starts_with(b"age-encryption.org/v1\n"),
            "encrypted_delivery_required",
        )?;
        (
            encode(&json!({"encryption":"age-x25519","recipient_key_id":field(key,"key_id")?}))?,
            Some(encode(
                &json!({"recipient_subject":order.buyer,"recipient_key":key,"ciphertext_ref":payload,"seller_request_digest":ctx.request.field("payload_digest")?}),
            )?),
        )
    } else {
        require(
            !ctx.args()?.contains_key("recipient_key_id"),
            "unexpected_recipient_key",
        )?;
        (
            encode(&json!({"type":"service-evidence","acceptance":"explicit-buyer-signature"}))?,
            None,
        )
    };
    let kind = text(field(&mutation.locked, "listing")?, "item_kind")?.into();
    let delivery = prepare(
        ctx,
        &mut mutation,
        Package {
            kind,
            manifest,
            refs: vec![payload],
        },
        envelope,
    )?;
    let resources = mutation.publish_context(ctx)?.into_iter().collect();
    output(
        json!({"order_id":order.id,"delivery_id":field(&delivery,"id")?,"delivery_digest":field(&delivery,"delivery_digest")?,"state":"delivered"}),
        resources,
    )
}
fn selected(ctx: &Context<'_>, order: &Order, bytes: bool) -> Result<(Json, BlobRef)> {
    let locked = order::get_contract(ctx.tx, &order.id)?;
    let delivery = verified(ctx.tx, ctx.contents, order, &locked, bytes)?;
    require(
        text(&delivery, "delivery_digest")? == ctx.text("delivery_digest")?,
        "delivery_digest_mismatch",
    )?;
    let index = integer(ctx.request.field("arguments")?, "payload_index")?;
    let reference = field(&delivery, "payload_refs")?
        .as_array()?
        .get(usize::try_from(index).map_err(|_| Error("payload_not_found"))?)
        .ok_or(Error("payload_not_found"))?;
    let blob = decode(field(reference, "blob")?)?;
    Ok((delivery, blob))
}
fn part(ctx: &Context<'_>, order: &Order) -> Result<HandlerOutput> {
    let (delivery, blob) = selected(ctx, order, false)?;
    let args = ctx.request.field("arguments")?;
    let start = integer(args, "offset")?;
    let length = integer(args, "length")?;
    let end = start
        .checked_add(length)
        .ok_or(Error("invalid_byte_range"))?;
    require(
        start >= 0 && length > 0 && length <= 65536 && end <= blob.size,
        "invalid_byte_range",
    )?;
    let mut bytes = vec![];
    ctx.contents
        .read_to(&blob, Some((start as u64, end as u64)), &mut bytes)?;
    require(bytes.len() as i64 == length, "delivery_digest_mismatch")?;
    let following = if end < blob.size {
        let mut next = args.as_object()?.clone();
        next.insert("offset".into(), encode(&end)?);
        next.insert("length".into(), encode(&65536_i64.min(blob.size - end))?);
        Json::from_object(next)
    } else {
        Json::parse("null")?
    };
    output(
        json!({"order_id":order.id,"delivery_id":field(&delivery,"id")?,"delivery_digest":field(&delivery,"delivery_digest")?,"payload_digest":blob.digest,"offset":start,"size":blob.size,"data":b64(&bytes),"chunk_digest":digest(&bytes),"next":following}),
        vec![],
    )
}
pub(crate) fn negotiate(ctx: &Context<'_>) -> Result<Json> {
    let args = ctx.request.field("arguments")?;
    let cap = |name: &str, default: usize| -> Result<i64> {
        Ok(if ctx.args()?.contains_key(name) {
            integer(args, name)?.min(default as i64)
        } else {
            default as i64
        })
    };
    let request = cap("max_request_bytes", ctx.settings.max_request_bytes)?;
    let response = cap("max_response_bytes", ctx.settings.max_response_bytes)?;
    let path = cap("max_path_bytes", ctx.settings.max_path_bytes)?;
    let mut packet = Json::parse(&ctx.request.canonical()?)?.into_object()?;
    packet.insert("operation".into(), Json::string("transfer.part_put"));
    packet.insert("request_id".into(), Json::string("r".repeat(64)));
    packet.insert(
        "payload_digest".into(),
        Json::string(format!("sha256:{}", "0".repeat(64))),
    );
    packet.insert("arguments".into(),encode(&json!({"transfer_id":format!("tr_{}","0".repeat(32)),"offset":i64::MAX,"data":"","digest":format!("sha256:{}","0".repeat(64))}))?);
    let overhead = Json::from_object(packet).canonical()?.len() as i64 + 48;
    let path_budget = (path - "/-/g/transfer.part_put/j/".len() as i64) * 3 / 4 - overhead;
    let part = [
        ctx.settings.max_part_bytes as i64,
        cap("requested_part_bytes", ctx.settings.max_part_bytes)?,
        (request - overhead) * 3 / 4,
        path_budget * 3 / 4,
        (response - 1024) * 3 / 4,
    ]
    .into_iter()
    .min()
    .ok_or(Error("transport_limit_too_small"))?;
    require(part >= 1, "transport_limit_too_small")?;
    encode(
        &json!({"part_bytes":part,"max_request_bytes":request,"max_response_bytes":response,"max_path_bytes":path}),
    )
}
pub(crate) fn open_download(
    ctx: &mut Context<'_>,
    target: ResourceRef,
    limits: &Json,
) -> Result<TransferSession> {
    let count = ctx
        .tx
        .one("SELECT count(*) FROM transfers", &[])?
        .ok_or(Error("storage_capacity_exceeded"))?
        .integer(0)?;
    let staged: i64 = ctx
        .tx
        .one("SELECT COALESCE(sum(length),0)::text FROM chunks", &[])?
        .ok_or(Error("storage_capacity_exceeded"))?
        .text(0)?
        .parse()
        .map_err(|_| Error("storage_capacity_exceeded"))?;
    require(
        (0..4096).contains(&count) && (0..=4_294_967_296).contains(&staged),
        "storage_capacity_exceeded",
    )?;
    ctx.authorizer.require(
        ctx.principal,
        Entry::Network,
        ctx.now,
        &[AccessRequirement {
            resource_id: target.id.clone(),
            operation: "transfer.open@1".into(),
            check: Check::Read,
        }],
        None,
        ctx.tx,
    )?;
    let revision = ctx.tx.revision(&target)?;
    let transfer = TransferSession {
        id: new_id("tr")?,
        subject_id: ctx.subject()?.into(),
        direction: "download".into(),
        state: "open".into(),
        target: Some(ResourceRef {
            id: target.id,
            revision: Some(revision.id),
        }),
        expected_size: Some(revision.content.size),
        expected_digest: Some(revision.content.digest),
        expires_at: order::add(
            ctx.now,
            i64::try_from(ctx.settings.transfer_ttl)
                .map_err(|_| Error("invalid_transfer_expiry"))?,
        )?,
        generation: 0,
        output: None,
    };
    require(transfer.expires_at > ctx.now, "invalid_transfer_expiry")?;
    ctx.tx.save_transfer(&transfer, None)?;
    let mut limits = limits.as_object()?.clone();
    limits.insert(
        "media_type".into(),
        Json::string(revision.content.media_type),
    );
    ctx.tx.execute(
        "UPDATE transfers SET limits=$1 WHERE id=$2",
        &[
            Json::from_object(limits).canonical()?.into(),
            transfer.id.as_str().into(),
        ],
    )?;
    Ok(transfer)
}
fn transfer(ctx: &mut Context<'_>, order: &Order) -> Result<HandlerOutput> {
    let (delivery, blob) = selected(ctx, order, true)?;
    let parent = ctx
        .tx
        .one(
            "SELECT id FROM resources WHERE parent=$1 AND name='files'",
            &[order.buyer.as_str().into()],
        )?
        .ok_or(Error("upload_parent_required"))?
        .text(0)?
        .to_owned();
    ctx.check(&parent, Check::Create)?;
    let resource = content::create(
        ctx,
        Create {
            parent,
            kind: "file",
            name: None,
            id: None,
            body: Some(Body::Existing(blob.clone())),
            mode: Some(0o600),
            revision: RevisionOptions::default(),
        },
    )?;
    let reference = ResourceRef {
        id: resource.id,
        revision: resource.revision,
    };
    let limits = negotiate(ctx)?;
    let session = open_download(ctx, reference.clone(), &limits)?;
    let mut data=encode(&json!({"transfer_id":session.id,"order_id":order.id,"delivery_id":field(&delivery,"id")?,"delivery_digest":field(&delivery,"delivery_digest")?,"size":blob.size,"digest":blob.digest,"state":session.state,"expires_at":session.expires_at}))?.into_object()?;
    data.extend(limits.into_object()?);
    output(json!(Json::from_object(data)), vec![reference])
}
