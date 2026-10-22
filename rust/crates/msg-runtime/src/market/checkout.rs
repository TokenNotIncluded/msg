//! Versioned checkout; historical checkout never acquires a new settlement policy.
use super::{
    email, field, hash, integer, ledger,
    order::{self, Mutation, Order, Settlement},
    require, store, text, time,
};
use crate::context::{output, Context, HandlerOutput};
use msg_core::{digest, Error, Json, Result};
use msg_identity::models::Timestamp;
use msg_storage::{
    postgres::{PgSession, Value},
    records::encode,
};
use rand_core::{OsRng, RngCore};
use serde_json::json;
use std::collections::BTreeSet;

pub(super) fn supports(op: &str, v: u32) -> bool {
    match op {
        "orders.buy" => (1..=4).contains(&v),
        "orders.create" | "orders.cancel" | "orders.get" | "orders.list" => (1..=2).contains(&v),
        "orders.fund" | "orders.payment" | "orders.contract" => v == 1,
        _ => false,
    }
}
pub(super) fn owned(ctx: &Context<'_>, id: &str, viewer: &str) -> Result<Order> {
    let order = order::read(ctx.tx, id)?;
    require(
        viewer == order.buyer || viewer == order.seller,
        "order_not_found",
    )?;
    if has_contract(ctx.tx, id)? {
        let locked = order::get_contract(ctx.tx, id)?;
        order::verify_source(
            ctx.tx,
            ctx.contents,
            &ctx.settings.receipt_signer,
            &order,
            &locked,
        )?;
        order::validate_projection(ctx.tx, &order, &locked)?;
    } else {
        require(
            ctx.tx
                .one(
                    "SELECT 1 FROM resources WHERE id=$1 AND type='order'",
                    &[id.into()],
                )?
                .is_none(),
            "order_resource_contract_missing",
        )?;
    }
    Ok(order)
}
pub(super) fn has_contract(tx: &PgSession, id: &str) -> Result<bool> {
    Ok(tx
        .one(
            "SELECT 1 FROM order_contracts WHERE order_id=$1",
            &[id.into()],
        )?
        .is_some())
}
pub(super) fn legacy_view(order: &Order, viewer: &str) -> Result<Json> {
    let source = encode(order)?;
    let mut result = std::collections::BTreeMap::new();
    for key in [
        "id",
        "buyer",
        "seller",
        "listing_id",
        "listing_revision",
        "package_revision",
        "package_digest",
        "quantity",
        "unit_price_minor",
        "total_price_minor",
        "currency_id",
        "escrow_policy",
        "dispute_policy",
        "terms_digest",
        "state",
        "created_at",
        "funded_at",
        "delivered_at",
        "settled_at",
    ] {
        result.insert(key.into(), field(&source, key)?.clone());
    }
    result.insert(
        "payment_status".into(),
        Json::string(if order.state == "refunded" {
            "refunded"
        } else if order.payment_transaction_id.is_some() {
            "funded"
        } else {
            "pending"
        }),
    );
    result.insert(
        "delivery_channel".into(),
        field(&order.delivery_target, "channel")?.clone(),
    );
    if viewer == order.buyer {
        for key in ["delivery_target", "payment_intent_digest", "receipt_refs"] {
            result.insert(key.into(), field(&source, key)?.clone());
        }
    }
    Ok(Json::from_object(result))
}
pub(super) fn view(tx: &PgSession, order: &Order, viewer: &str) -> Result<Json> {
    let email = email::notification_view(tx, order)?;
    let mut projected = order.clone();
    let mut target = projected.delivery_target.into_object()?;
    target.insert("email".into(), email.clone());
    projected.delivery_target = Json::from_object(target);
    let mut result = legacy_view(&projected, viewer)?.into_object()?;
    if has_contract(tx, &order.id)? {
        let locked = order::get_contract(tx, &order.id)?;
        order::validate_projection(tx, order, &locked)?;
        for (key, value) in [
            ("order_digest", encode(&hash(&locked)?)?),
            ("contract_version", field(&locked, "version")?.clone()),
            (
                "delivery_mode",
                field(field(&locked, "listing")?, "delivery_mode")?.clone(),
            ),
            (
                "policy_digest",
                field(field(&locked, "policy")?, "policy_digest")?.clone(),
            ),
        ] {
            result.insert(key.into(), value);
        }
        if integer(&locked, "version")? == 5 {
            result.insert(
                "settlement_policy".into(),
                field(&locked, "settlement_policy")?.clone(),
            );
            let fact = tx.one(
                "SELECT body FROM order_settlements WHERE order_id=$1",
                &[order.id.as_str().into()],
            )?;
            result.insert(
                "entitlement_id".into(),
                match fact {
                    Some(row) => field(&row.json(0)?, "entitlement_id")?.clone(),
                    None => Json::parse("null")?,
                },
            );
        }
        if let Some(key) = locked
            .as_object()?
            .get("recipient_key")
            .filter(|k| !k.is_null())
        {
            result.insert("recipient_key".into(), key.clone());
        }
    }
    result.insert("email_status".into(), field(&email, "state")?.clone());
    Ok(Json::from_object(result))
}
pub(super) fn order_id() -> Result<String> {
    let mut random = [0_u8; 20];
    OsRng
        .try_fill_bytes(&mut random)
        .map_err(|_| Error("random_unavailable"))?;
    let alphabet = b"abcdefghijklmnopqrstuvwxyz234567";
    let mut result = String::from("ord_");
    let (mut value, mut bits) = (0_u32, 0_u32);
    for byte in random {
        value = (value << 8) | u32::from(byte);
        bits += 8;
        while bits >= 5 {
            bits -= 5;
            result.push(alphabet[((value >> bits) & 31) as usize] as char);
        }
    }
    Ok(result)
}
pub(super) fn policy_snapshot(tx: &PgSession, id: &str) -> Result<Json> {
    let value = if let Some(row) = tx.one(
        "SELECT body,digest FROM arbitration_policies WHERE id=$1",
        &[id.into()],
    )? {
        let value = row.json(0)?;
        require(hash(&value)? == row.text(1)?, "arbitration_policy_corrupt")?;
        value
    } else {
        require(id == "dispute-v1", "arbitration_policy_unknown")?;
        encode(
            &json!({"id":"dispute-v1","version":1,"funding_timeout_seconds":900,"delivery_timeout_seconds":86400,"case_timeout_seconds":604800,"decision_lifetime_seconds":86400,"panel_size":3,"quorum":2,"candidates":[],"allow_appeal":false,"appeal_seconds":3600,"selection":"sha256-order-policy-round-subject-v1","member_invalidation":"hold-no-replacement-v1","rules":"objective-faults-v1"}),
        )?
    };
    let keys: BTreeSet<_> = value.as_object()?.keys().map(String::as_str).collect();
    require(
        keys == BTreeSet::from([
            "id",
            "version",
            "funding_timeout_seconds",
            "delivery_timeout_seconds",
            "case_timeout_seconds",
            "decision_lifetime_seconds",
            "panel_size",
            "quorum",
            "candidates",
            "allow_appeal",
            "appeal_seconds",
            "selection",
            "member_invalidation",
            "rules",
        ]),
        "arbitration_policy_invalid",
    )?;
    require(
        (1..=80).contains(&text(&value, "id")?.chars().count())
            && integer(&value, "version")? == 1
            && text(&value, "rules")? == "objective-faults-v1"
            && text(&value, "selection")? == "sha256-order-policy-round-subject-v1"
            && text(&value, "member_invalidation")? == "hold-no-replacement-v1",
        "arbitration_policy_invalid",
    )?;
    for (name, max) in [
        ("funding_timeout_seconds", 86400),
        ("delivery_timeout_seconds", 2592000),
        ("case_timeout_seconds", 2592000),
        ("decision_lifetime_seconds", 604800),
        ("appeal_seconds", 86400),
    ] {
        require(
            (60..=max).contains(&integer(&value, name)?),
            "arbitration_policy_invalid",
        )?;
    }
    let size = integer(&value, "panel_size")?;
    let quorum = integer(&value, "quorum")?;
    let appeal = field(&value, "allow_appeal")?.as_bool()?;
    require(
        (1..=9).contains(&size)
            && size / 2 < quorum
            && quorum <= size
            && (!appeal
                || integer(&value, "decision_lifetime_seconds")?
                    > integer(&value, "appeal_seconds")?),
        "arbitration_policy_invalid",
    )?;
    let candidates = field(&value, "candidates")?.as_array()?;
    require(candidates.len() <= 100, "arbitration_policy_invalid")?;
    let mut seen = BTreeSet::new();
    let mut epochs = std::collections::BTreeMap::new();
    for candidate in candidates {
        let candidate = candidate.as_str()?;
        require(
            candidate.starts_with("u_") && candidate != "u_root" && seen.insert(candidate),
            "arbitration_policy_invalid",
        )?;
        if let Some(row) = tx.one(
            "SELECT epoch,active FROM arbitrator_roles WHERE subject=$1",
            &[candidate.into()],
        )? {
            if row.boolean(1)? {
                epochs.insert(candidate, Json::string(row.text(0)?));
            }
        }
    }
    encode(&json!({"policy":value,"policy_digest":hash(&value)?,"candidate_epochs":epochs}))
}
fn available(
    ctx: &mut Context<'_>,
) -> Result<(
    msg_storage::records::Resource,
    Json,
    msg_storage::records::Revision,
    i64,
)> {
    let id = ctx.text("listing_id")?.to_owned();
    let resource = store::listing(ctx, &id, false)?;
    let (body, revision) = store::body(ctx, &resource, None)?;
    require(
        text(&body, "mode")? == "sale"
            && text(&body, "state")? == "active"
            && (field(&body, "expires_at")?.is_null()
                || Timestamp::parse(text(&body, "expires_at")?)? > ctx.now),
        "listing_not_available",
    )?;
    require(
        resource.revision.as_deref() == Some(ctx.text("listing_revision")?),
        "listing_revision_conflict",
    )?;
    require(
        text(&body, "currency_id")? == "primary" && ctx.text("currency_id")? == "primary",
        "unsupported_currency",
    )?;
    let quantity = integer(ctx.request.field("arguments")?, "quantity")?;
    let total = integer(&body, "price_minor")?
        .checked_mul(quantity)
        .filter(|v| *v > 0)
        .ok_or(Error("money_overflow"))?;
    require(
        total == integer(ctx.request.field("arguments")?, "total_price_minor")?,
        "price_changed",
    )?;
    // Validate signed inventory sources before using their mutable SQL index.
    for row in ctx.tx.rows("SELECT order_id FROM order_contracts WHERE body::jsonb->>'listing_id'=$1 AND body::jsonb->>'resource_model'='1'",&[resource.id.as_str().into()])? {Mutation::begin(ctx.tx,ctx.contents,&ctx.settings.receipt_signer,row.text(0)?)?;}
    let sold:i64=ctx.tx.one("SELECT COALESCE(SUM(quantity),0)::text FROM store_orders WHERE listing_id=$1 AND state NOT IN ('cancelled','refunded')",&[resource.id.as_str().into()])?.ok_or(Error("quantity_unavailable"))?.text(0)?.parse().map_err(|_|Error("quantity_unavailable"))?;
    require(
        quantity > 0
            && sold
                .checked_add(quantity)
                .is_some_and(|n| n <= integer(&body, "quantity").unwrap_or(0)),
        "quantity_unavailable",
    )?;
    require(
        resource.owner != super::signed_subject(ctx)?,
        "self_purchase_forbidden",
    )?;
    Ok((resource, body, revision, total))
}
pub(super) fn create(ctx: &mut Context<'_>, version: i64) -> Result<Mutation> {
    let buyer = super::signed_subject(ctx)?.to_owned();
    let (listing, body, revision, total) = available(ctx)?;
    require(
        text(&body, "escrow_policy")? == "escrow-v1",
        "escrow_policy_unknown",
    )?;
    let policy = policy_snapshot(ctx.tx, text(&body, "dispute_policy")?)?;
    let mode = text(&body, "delivery_mode")?;
    let kind = text(&body, "item_kind")?;
    require(
        kind != "secret" || mode == "sealed_manual",
        "secret_requires_buyer_encryption",
    )?;
    let package = if mode == "managed_instant" {
        require(
            matches!(kind, "file" | "bundle" | "text"),
            "order_delivery_mode_unsupported",
        )?;
        let p = store::package(ctx.tx, text(&body, "package_ref")?).map_err(|e| {
            if e.0 == "package_not_found" {
                Error("package_not_available")
            } else {
                e
            }
        })?;
        require(
            text(&p, "listing_id")? == listing.id
                && text(&p, "seller")? == listing.owner
                && text(&p, "kind")? == kind
                && text(&p, "delivery_mode")? == "managed_instant",
            "package_not_available",
        )?;
        Some(p)
    } else {
        None
    };
    let recipient_key = if mode == "sealed_manual" {
        let r=ctx.tx.one("SELECT key_id,recipient,public_key FROM encryption_subkeys WHERE subject=$1 AND is_primary=1 AND retired_at IS NULL",&[buyer.as_str().into()])?.ok_or(Error("delivery_recipient_key_required"))?;
        let public = match r.value(2)? {
            Value::Bytes(bytes) => bytes,
            _ => return Err(Error("storage_column_type")),
        };
        encode(&json!({"key_id":r.text(0)?,"recipient":r.text(1)?,"fingerprint":digest(public)}))?
    } else {
        Json::parse("null")?
    };
    let target = email::target_for(
        ctx,
        &buyer,
        ctx.args()?.get("email").map(Json::as_str).transpose()?,
    )?;
    let id = order_id()?;
    let escrow = format!("esc_{}", &id[4..]);
    let quantity = integer(ctx.request.field("arguments")?, "quantity")?;
    let package_id = package.as_ref().map(|p| text(p, "id")).transpose()?;
    let package_revision = package.as_ref().map(|p| text(p, "revision")).transpose()?;
    let package_digest = package.as_ref().map(|p| text(p, "digest")).transpose()?;
    let order = Order {
        id: id.clone(),
        buyer: buyer.clone(),
        seller: listing.owner.clone(),
        listing_id: listing.id.clone(),
        listing_revision: listing.revision.clone().ok_or(Error("listing_not_found"))?,
        package_id: package_id.map(str::to_owned),
        package_revision: package_revision.map(str::to_owned),
        package_digest: package_digest.map(str::to_owned),
        quantity,
        unit_price_minor: integer(&body, "price_minor")?,
        total_price_minor: total,
        currency_id: "primary".into(),
        escrow_subject: escrow.clone(),
        escrow_policy: text(&body, "escrow_policy")?.into(),
        dispute_policy: text(&body, "dispute_policy")?.into(),
        terms_digest: hash(field(&body, "terms")?)?,
        delivery_target: target.clone(),
        payment_intent_digest: Some(ctx.request.field("payload_digest")?.as_str()?.into()),
        payment_transaction_id: None,
        state: "created".into(),
        created_at: time(ctx.now),
        funded_at: None,
        delivered_at: None,
        settled_at: None,
        receipt_refs: vec![],
    };
    insert(ctx.tx, &order)?;
    let mut locked=encode(&json!({"order_id":id,"version":version,"buyer":buyer,"seller":listing.owner,"listing":body,"listing_id":listing.id,"escrow_subject":escrow,"listing_revision":listing.revision,"listing_digest":revision.content.digest,"package_id":package_id,"package_revision":package_revision,"package_digest":package_digest,"quantity":quantity,"total_price_minor":total,"terms_digest":order.terms_digest,"policy":policy,"recipient_key":recipient_key,"handle_snapshot":field(&target,"handle_snapshot")?,"buyer_principal":ctx.principal,"created_at":time(ctx.now)}))?.into_object()?;
    if version == 4 {
        locked.insert("resource_model".into(), encode(&1)?);
        locked.insert(
            "creation_request".into(),
            Json::parse(&ctx.request.canonical()?)?,
        );
        locked.insert(
            "settlement_policy".into(),
            encode(&json!({"id":"explicit-buyer-acceptance","version":1}))?,
        );
    }
    let locked = Json::from_object(locked);
    ctx.tx.execute(
        "INSERT INTO order_contracts(order_id,body,digest) VALUES($1,$2,$3)",
        &[
            id.as_str().into(),
            locked.canonical()?.into(),
            hash(&locked)?.into(),
        ],
    )?;
    ctx.tx.execute(
        "INSERT INTO order_deadlines(order_id,expires_at) VALUES($1,$2)",
        &[
            id.as_str().into(),
            time(order::add(
                ctx.now,
                integer(field(&policy, "policy")?, "funding_timeout_seconds")?,
            )?)
            .into(),
        ],
    )?;
    let mut mutation = Mutation::new(ctx.tx, &id)?;
    email::verification_message(ctx, &mut mutation.order)?;
    Ok(mutation)
}
pub(super) fn insert(tx: &PgSession, order: &Order) -> Result<()> {
    tx.execute("INSERT INTO ledger_accounts(id,kind,subject_id,source_id) VALUES($1,'order_escrow',NULL,$2)",&[order.escrow_subject.as_str().into(),order.id.as_str().into()])?;
    tx.execute("INSERT INTO store_orders(id,buyer,seller,listing_id,listing_revision,package_id,package_revision,package_digest,quantity,unit_price_minor,total_price_minor,currency_id,escrow_subject,escrow_policy,dispute_policy,terms_digest,delivery_target,payment_intent_digest,payment_transaction_id,state,created_at,funded_at,delivered_at,settled_at,receipt_refs) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25)",&[order.id.as_str().into(),order.buyer.as_str().into(),order.seller.as_str().into(),order.listing_id.as_str().into(),order.listing_revision.as_str().into(),order.package_id.as_deref().into(),order.package_revision.as_deref().into(),order.package_digest.as_deref().into(),order.quantity.into(),order.unit_price_minor.into(),order.total_price_minor.into(),order.currency_id.as_str().into(),order.escrow_subject.as_str().into(),order.escrow_policy.as_str().into(),order.dispute_policy.as_str().into(),order.terms_digest.as_str().into(),order.delivery_target.canonical()?.into(),order.payment_intent_digest.as_deref().into(),order.payment_transaction_id.as_deref().into(),order.state.as_str().into(),order.created_at.as_str().into(),order.funded_at.as_deref().into(),order.delivered_at.as_deref().into(),order.settled_at.as_deref().into(),encode(&order.receipt_refs)?.canonical()?.into()])?;
    Ok(())
}
pub(super) fn fund(ctx: &mut Context<'_>, mutation: &mut Mutation) -> Result<Json> {
    require(mutation.order.state == "created", "order_not_fundable")?;
    let version = integer(&mutation.locked, "version")?;
    require(
        version != 5
            || (ctx.request.field("operation")?.as_str()? == "money.redeem"
                && ctx.request.field("contract_version")?.as_integer()? == "3"),
        "entitlement_funding_contract_required",
    )?;
    require(
        ctx.now
            < order::add(
                Timestamp::parse(&mutation.order.created_at)?,
                integer(
                    field(field(&mutation.locked, "policy")?, "policy")?,
                    "funding_timeout_seconds",
                )?,
            )?,
        "payment_intent_expired",
    )?;
    email::validate_target(ctx.tx, &mutation.order, false, false)?;
    let order = &mutation.order;
    let receipt = ledger::transfer(
        ctx.tx,
        &ctx.settings.receipt_signer,
        &ledger::Posting {
            sender: &order.buyer,
            recipient: &order.escrow_subject,
            amount: order.total_price_minor,
            actor: &order.buyer,
            request: ctx.request.field("request_id")?.as_str()?,
            reference: Some(&format!("order_fund:{}", order.id)),
            kind: "transfer",
            entry_key: "primary",
            now: ctx.now,
        },
    )?;
    let transaction = text(field(&receipt, "body")?, "transaction_id")?;
    ctx.tx.execute("UPDATE store_orders SET payment_transaction_id=$1,payment_intent_digest=$2,funded_at=$3,receipt_refs=$4 WHERE id=$5",&[transaction.into(),ctx.request.field("payload_digest")?.as_str()?.into(),time(ctx.now).into(),encode(&[transaction])?.canonical()?.into(),order.id.as_str().into()])?;
    mutation.order.payment_transaction_id = Some(transaction.into());
    mutation.order.payment_intent_digest =
        Some(ctx.request.field("payload_digest")?.as_str()?.into());
    mutation.order.funded_at = Some(time(ctx.now));
    mutation.order.receipt_refs = vec![transaction.into()];
    let buyer = mutation.order.buyer.clone();
    mutation.transition(
        ctx.tx,
        "funded",
        ctx.now,
        &buyer,
        ctx.request.field("request_id")?.as_str()?,
        "signed_payment_intent",
    )?;
    ctx.tx.execute(
        "UPDATE order_deadlines SET expires_at=$1 WHERE order_id=$2",
        &[
            time(order::add(
                ctx.now,
                integer(
                    field(field(&mutation.locked, "policy")?, "policy")?,
                    "delivery_timeout_seconds",
                )?,
            )?)
            .into(),
            mutation.order.id.as_str().into(),
        ],
    )?;
    if version != 5
        && text(field(&mutation.locked, "listing")?, "delivery_mode")? == "managed_instant"
    {
        super::delivery::automatic(ctx, mutation)?;
    }
    Ok(receipt)
}
pub(super) fn handle(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let operation = ctx.request.field("operation")?.as_str()?.to_owned();
    let version: u32 = ctx
        .request
        .field("contract_version")?
        .as_integer()?
        .parse()
        .map_err(|_| Error("invalid_version"))?;
    if operation == "orders.buy" && version < 3 {
        return legacy_buy(ctx, version == 2);
    }
    if operation == "orders.buy" || operation == "orders.create" {
        let contract = if (operation == "orders.buy" && version == 4)
            || (operation == "orders.create" && version == 2)
        {
            4
        } else {
            3
        };
        let mut mutation = create(ctx, contract)?;
        let payment = if operation == "orders.buy" {
            Some(fund(ctx, &mut mutation)?)
        } else {
            None
        };
        let buyer = mutation.order.buyer.clone();
        let result = view(ctx.tx, &order::read(ctx.tx, &mutation.order.id)?, &buyer)?;
        let resources = mutation.publish_context(ctx)?.into_iter().collect();
        return output(
            match payment {
                Some(payment) => json!({"order":result,"payment":payment}),
                None => json!({"order":result}),
            },
            resources,
        );
    }
    let viewer = super::viewer(ctx)?.to_owned();
    if operation == "orders.list" {
        return list(ctx, &viewer, version);
    }
    if operation == "orders.get"
        && ctx.args()?.get("source").map(Json::as_str).transpose()? == Some("legacy_purchase")
    {
        let value =
            super::offers::purchase_view(ctx, ctx.text("order_id")?, &viewer).map_err(|e| {
                if e.0 == "purchase_not_found" {
                    Error("order_not_found")
                } else {
                    e
                }
            })?;
        return output(json!({"order":value}), vec![]);
    }
    let order = owned(ctx, ctx.text("order_id")?, &viewer)?;
    match operation.as_str() {
        "orders.get" => output(json!({"order":view(ctx.tx,&order,&viewer)?}), vec![]),
        "orders.contract" => {
            let mut locked = order::get_contract(ctx.tx, &order.id)?.into_object()?;
            locked.remove("buyer_principal");
            locked.remove("handle_snapshot");
            output(
                json!({"order":view(ctx.tx,&order,&viewer)?,"contract":Json::from_object(locked)}),
                vec![],
            )
        }
        "orders.payment" => {
            let mut result=encode(&json!({"order_id":order.id,"status":field(&legacy_view(&order,&viewer)?,"payment_status")?,"amount_minor":order.total_price_minor,"currency_id":order.currency_id}))?.into_object()?;
            if viewer == order.buyer {
                let receipts = order
                    .receipt_refs
                    .iter()
                    .map(|id| {
                        ctx.tx
                            .one(
                                "SELECT receipt FROM money_ledger WHERE id=$1",
                                &[id.as_str().into()],
                            )?
                            .ok_or(Error("order_not_found"))?
                            .json(0)
                    })
                    .collect::<Result<Vec<_>>>()?;
                result.insert(
                    "receipt".into(),
                    receipts.first().cloned().unwrap_or(Json::parse("null")?),
                );
                result.insert("receipts".into(), encode(&receipts)?);
                result.insert(
                    "escrow_balance_minor".into(),
                    encode(&ledger::balance(ctx.tx, &order.escrow_subject)?)?,
                );
            }
            output(json!({"payment":Json::from_object(result)}), vec![])
        }
        "orders.cancel" if version == 1 => {
            let (receipt, decision, _) = super::legacy::settle(ctx, &order, "buyer_cancel")?;
            output(
                json!({"order":legacy_view(&order::read(ctx.tx,&order.id)?,&viewer)?,"refund":receipt,"decision":decision}),
                vec![],
            )
        }
        "orders.cancel" | "orders.fund" => {
            require(
                order.buyer == super::signed_subject(ctx)?,
                "order_not_found",
            )?;
            let mut mutation = Mutation::begin(
                ctx.tx,
                ctx.contents,
                &ctx.settings.receipt_signer,
                &order.id,
            )?;
            let payment = if operation == "orders.fund" {
                require(
                    hash(&mutation.locked)? == ctx.text("order_digest")?
                        && order.total_price_minor
                            == integer(ctx.request.field("arguments")?, "total_price_minor")?,
                    "payment_intent_mismatch",
                )?;
                Some(fund(ctx, &mut mutation)?)
            } else {
                if order.state == "created" {
                    mutation.transition(
                        ctx.tx,
                        "cancelled",
                        ctx.now,
                        &viewer,
                        ctx.request.field("request_id")?.as_str()?,
                        "buyer_cancelled",
                    )?;
                } else if !matches!(order.state.as_str(), "cancelled" | "refunded") {
                    require(
                        order.state == "funded" && order.delivered_at.is_none(),
                        "order_not_cancellable",
                    )?;
                    mutation.settle(
                        ctx.tx,
                        &ctx.settings.receipt_signer,
                        Settlement {
                            now: ctx.now,
                            actor: &viewer,
                            request: ctx.request.field("request_id")?.as_str()?,
                            reason: "buyer_cancelled",
                            refund: order.total_price_minor,
                            decision: None,
                        },
                    )?;
                }
                None
            };
            let result = view(ctx.tx, &order::read(ctx.tx, &order.id)?, &viewer)?;
            let resources = mutation.publish_context(ctx)?.into_iter().collect();
            output(
                match payment {
                    Some(payment) => json!({"order":result,"payment":payment}),
                    None => json!({"order":result}),
                },
                resources,
            )
        }
        _ => Err(Error("unknown_operation")),
    }
}
fn list(ctx: &Context<'_>, viewer: &str, version: u32) -> Result<HandlerOutput> {
    let args = ctx.request.field("arguments")?;
    let role = ctx.args()?.get("role").map(Json::as_str).transpose()?;
    let status = ctx.args()?.get("status").map(Json::as_str).transpose()?;
    let limit = if ctx.args()?.contains_key("limit") {
        integer(args, "limit")?
    } else {
        50
    };
    require((1..=100).contains(&limit), "invalid_page")?;
    let mut result = vec![];
    for row in ctx.tx.rows(
        "SELECT id FROM store_orders WHERE CASE $2::text WHEN 'buy' THEN buyer=$1 WHEN 'sell' THEN seller=$1 ELSE buyer=$1 OR seller=$1 END AND CASE $3::text WHEN 'open' THEN state IN ('created','funded','delivered','accepted') WHEN 'completed' THEN state IN ('settled','cancelled','refunded') WHEN 'disputed' THEN state='disputed' ELSE TRUE END ORDER BY created_at DESC,id DESC LIMIT $4",
        &[
            viewer.into(),
            role.unwrap_or("").into(),
            status.unwrap_or("").into(),
            limit.into(),
        ],
    )? {
        let order = owned(ctx, row.text(0)?, viewer)?;
        let mut value = legacy_view(&order, viewer)?.into_object()?;
        if version == 2 {
            value.insert("source".into(), Json::string("store_order"));
        }
        result.push(Json::from_object(value));
    }
    if version == 2 && role != Some("sell") && status != Some("disputed") {
        for row in ctx.tx.rows(
            "SELECT id FROM money_purchases WHERE subject_id=$1 AND CASE $2::text WHEN 'open' THEN state='pending' WHEN 'completed' THEN state IN ('settled','refunded') ELSE TRUE END ORDER BY created_at DESC,id DESC LIMIT $3",
            &[viewer.into(), status.unwrap_or("").into(), limit.into()],
        )? {
            result.push(super::offers::purchase_view(
                ctx,
                row.text(0)?,
                viewer,
            )?);
        }
        result.sort_by_cached_key(|v| {
            (
                text(v, "created_at").unwrap_or("").to_owned(),
                text(v, "id").unwrap_or("").to_owned(),
            )
        });
        result.reverse();
        result.truncate(limit as usize);
    }
    output(json!({"orders":result}), vec![])
}
fn legacy_buy(ctx: &mut Context<'_>, instant: bool) -> Result<HandlerOutput> {
    let buyer = super::signed_subject(ctx)?.to_owned();
    if instant {
        require(
            ctx.settings.plugins.iter().any(|p| p == "delivery"),
            "delivery_disabled",
        )?;
        if let Some(address) = ctx.args()?.get("email") {
            msg_worker::mail::validate_address(address.as_str()?)?;
        }
    }
    let (listing, body, _, total) = available(ctx)?;
    require(
        text(&body, "delivery_mode")? == "managed_instant"
            && matches!(text(&body, "item_kind")?, "file" | "bundle"),
        "order_delivery_mode_unsupported",
    )?;
    let policy = super::legacy::policy(
        text(&body, "escrow_policy")?,
        text(&body, "dispute_policy")?,
    )?;
    if !instant {
        require(
            text(&body, "escrow_policy")? == "escrow-v1",
            "escrow_policy_requires_v2",
        )?;
    }
    let auto = ctx
        .args()?
        .get("auto_accept")
        .map(Json::as_bool)
        .transpose()?
        .unwrap_or(false);
    if auto {
        require(
            field(&policy, "reasons")?
                .as_array()?
                .iter()
                .any(|r| r.as_str().ok() == Some("checkout_accept")),
            "escrow_reason_unsupported",
        )?;
    }
    require(
        ledger::balance(ctx.tx, &buyer)? >= total,
        "insufficient_funds",
    )?;
    let package = store::package(ctx.tx, text(&body, "package_ref")?).map_err(|e| {
        if e.0 == "package_not_found" {
            Error("package_not_available")
        } else {
            e
        }
    })?;
    require(
        text(&package, "listing_id")? == listing.id
            && text(&package, "seller")? == listing.owner
            && field(&package, "kind")? == field(&body, "item_kind")?
            && text(&package, "delivery_mode")? == "managed_instant",
        "package_not_available",
    )?;
    require(
        (0..=1_048_576).contains(&integer(&package, "total_size")?),
        "delivery_package_too_large",
    )?;
    if instant {
        require(
            ctx.text("package_digest")? == text(&package, "digest")?,
            "delivery_package_mismatch",
        )?;
    }
    let id = order_id()?;
    let escrow = format!("esc_{}", &id[4..]);
    let mut order = Order {
        id: id.clone(),
        buyer: buyer.clone(),
        seller: listing.owner,
        listing_id: listing.id,
        listing_revision: listing.revision.ok_or(Error("listing_not_found"))?,
        package_id: Some(text(&package, "id")?.into()),
        package_revision: Some(text(&package, "revision")?.into()),
        package_digest: Some(text(&package, "digest")?.into()),
        quantity: integer(ctx.request.field("arguments")?, "quantity")?,
        unit_price_minor: integer(&body, "price_minor")?,
        total_price_minor: total,
        currency_id: "primary".into(),
        escrow_subject: escrow,
        escrow_policy: text(&body, "escrow_policy")?.into(),
        dispute_policy: text(&body, "dispute_policy")?.into(),
        terms_digest: hash(field(&body, "terms")?)?,
        delivery_target: encode(&json!({"subject_id":buyer,"channel":"site"}))?,
        payment_intent_digest: Some(ctx.request.field("payload_digest")?.as_str()?.into()),
        payment_transaction_id: None,
        state: "funded".into(),
        created_at: time(ctx.now),
        funded_at: Some(time(ctx.now)),
        delivered_at: None,
        settled_at: None,
        receipt_refs: vec![],
    };
    let receipt = ledger::transfer(
        ctx.tx,
        &ctx.settings.receipt_signer,
        &ledger::Posting {
            sender: &buyer,
            recipient: &order.escrow_subject,
            amount: total,
            actor: &buyer,
            request: ctx.request.field("request_id")?.as_str()?,
            reference: Some(&format!("order_fund:{id}")),
            kind: "transfer",
            entry_key: "primary",
            now: ctx.now,
        },
    )?;
    let payment = text(field(&receipt, "body")?, "transaction_id")?.to_owned();
    order.payment_transaction_id = Some(payment.clone());
    order.receipt_refs = vec![payment];
    insert(ctx.tx, &order)?;
    let mut data = encode(&json!({"payment":receipt}))?.into_object()?;
    if instant {
        let delivery = super::delivery::prepare_managed(ctx, &order)?;
        data.insert("delivery".into(), super::delivery::summary(&delivery)?);
        if auto {
            let (released, decision, _) =
                super::legacy::settle(ctx, &order::read(ctx.tx, &id)?, "checkout_accept")?;
            data.insert("settlement".into(), released);
            data.insert("decision".into(), decision);
        }
        let current = order::read(ctx.tx, &id)?;
        let notification = email::initialize_legacy(
            ctx,
            &current,
            ctx.args()?.get("email").map(Json::as_str).transpose()?,
        )?;
        data.insert("notification".into(), notification);
    }
    data.insert(
        "order".into(),
        legacy_view(&order::read(ctx.tx, &id)?, &buyer)?,
    );
    output(json!(Json::from_object(data)), vec![])
}
