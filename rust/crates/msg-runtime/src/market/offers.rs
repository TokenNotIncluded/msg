//! Root-issued capacity offers, historical purchases, and synchronous Orders.
use super::{checkout, field, hash, integer, ledger, money, order, require, text, time};
use crate::context::{new_id, output, Context, HandlerOutput};
use msg_core::{b64, digest, Error, Json, Result};
use msg_crypto::verify;
use msg_identity::{
    models::{ResourceRef, Timestamp},
    store::AuthorityStore,
};
use msg_storage::{
    postgres::{PgSession, Row, Value},
    records::{encode, ResourceState},
};
use serde_json::json;
use std::collections::{BTreeMap, BTreeSet};

const MAX_CAPACITY: i64 = 1_099_511_627_776;
const ENTITLEMENT: &str = "hosting_bytes_v1";
const QUOTE_KEYS: &[&str] = &[
    "offer_id",
    "resource_kind",
    "unit",
    "price_minor",
    "min_quantity",
    "max_quantity",
    "entitlement_kind",
    "duration_seconds",
    "price_revision",
];

pub(super) fn supports(op: &str, version: u32) -> bool {
    match op {
        "money.offers" | "money.purchase_get" => (1..=2).contains(&version),
        "money.redeem" => (1..=3).contains(&version),
        "money.purchase_settle" | "money.purchase_cancel" | "money.entitlements" => version == 1,
        _ => false,
    }
}
fn columns(row: &Row, keys: &[&str]) -> Result<Json> {
    require(row.len() == keys.len(), "storage_column_missing")?;
    let mut result = BTreeMap::new();
    for (i, key) in keys.iter().enumerate() {
        let value = match row.value(i)? {
            Value::Null => Json::parse("null")?,
            Value::Text(v) => Json::string(v),
            Value::Int(v) => encode(v)?,
            Value::Bool(v) => encode(v)?,
            _ => return Err(Error("storage_column_type")),
        };
        result.insert((*key).into(), value);
    }
    Ok(Json::from_object(result))
}
fn public_offer(row: &Row) -> Result<Json> {
    let mut value = columns(row, QUOTE_KEYS)?.into_object()?;
    value.insert("currency_id".into(), Json::string("primary"));
    value.insert("provider_version".into(), encode(&1)?);
    Ok(Json::from_object(value))
}
fn purchasable(ctx: &Context<'_>, offer: &Json) -> bool {
    text(offer, "resource_kind").ok() == Some("website")
        && text(offer, "entitlement_kind").ok() == Some(ENTITLEMENT)
        && ctx
            .catalog
            .resource_type("website", 1)
            .is_ok_and(|s| s.purchasable)
        && ctx.catalog.operation("hosting.deploy", 1).is_ok()
}
fn valid_offer(ctx: &Context<'_>, offer: &Json) -> Result<()> {
    require(purchasable(ctx, offer), "resource_not_purchasable")?;
    require(text(offer, "unit")? == "byte", "invalid_offer_unit")?;
    let price = integer(offer, "price_minor")?;
    let minimum = integer(offer, "min_quantity")?;
    let maximum = integer(offer, "max_quantity")?;
    require(price > 0, "invalid_offer_price")?;
    require(
        0 < minimum
            && minimum <= maximum
            && maximum <= MAX_CAPACITY
            && price.checked_mul(maximum).is_some(),
        "invalid_offer_quantity",
    )?;
    let duration = field(offer, "duration_seconds")?;
    require(
        duration.is_null() || (1..=315_360_000).contains(&integer(offer, "duration_seconds")?),
        "invalid_offer_duration",
    )
}
pub(super) fn listing_body(offer: &Json) -> Result<Json> {
    encode(
        &json!({"mode":"sale","item_kind":"entitlement","price_minor":field(offer,"price_minor")?,"currency_id":"primary","quantity":field(offer,"max_quantity")?,"delivery_mode":"managed_instant","package_ref":null,"escrow_policy":"legacy-entitlement-v1","dispute_policy":"local-root-refund-v1","terms":"Server-issued entitlement; settled Root payments require local refund authority.","expires_at":null,"state":if field(offer,"enabled")?.as_bool()? {"active"}else{"paused"},"server_offer":offer,"server_offer_model":1}),
    )
}
/// A mapped SQL quote is an index of this signed source, never independent authority.
fn authoritative_offer(ctx: &Context<'_>, id: &str) -> Result<Option<Json>> {
    let Some(row) = ctx.tx.one(
        "SELECT listing_id FROM server_offer_resources WHERE offer_id=$1",
        &[id.into()],
    )?
    else {
        return Ok(None);
    };
    let resource = ctx.tx.resource_record(row.text(0)?)?;
    require(
        resource.id == id
            && resource.resource_type == "listing"
            && resource.parent.as_deref() == Some("t_store")
            && resource.owner == "u_root"
            && resource.state == ResourceState::Active
            && resource.mode == 0o444
            && resource.group == "g_public",
        "offer_resource_invalid",
    )?;
    for ancestor in ctx.tx.ancestors(id)? {
        require(
            ancestor.state == "active"
                && u32::from_str_radix(&ancestor.mode, 8).is_ok_and(|m| m & 1 != 0),
            "offer_resource_invalid",
        )?;
    }
    let revision = ctx.tx.revision(&ResourceRef {
        id: id.into(),
        revision: resource.revision,
    })?;
    require(
        revision.actor == "u_root" && revision.subject == "u_root" && revision.signature.is_some(),
        "offer_resource_invalid",
    )?;
    let mut manifest = encode(&revision)?.into_object()?;
    manifest.remove("manifest_digest");
    manifest.remove("signature");
    let manifest = Json::from_object(manifest);
    require(
        hash(&manifest)? == revision.manifest_digest,
        "offer_resource_invalid",
    )?;
    verify(
        &ctx.settings.root_public_key,
        manifest.canonical()?.as_bytes(),
        revision
            .signature
            .as_ref()
            .ok_or(Error("offer_resource_invalid"))?,
        "revision",
    )?;
    let bytes = ctx
        .contents
        .read_bytes(&revision.content, msg_core::MAX_BYTES as u64)?;
    require(
        digest(&bytes) == revision.content.digest,
        "offer_resource_invalid",
    )?;
    let body =
        Json::parse(std::str::from_utf8(&bytes).map_err(|_| Error("offer_resource_invalid"))?)?;
    require(
        integer(&body, "server_offer_model")? == 1,
        "offer_resource_invalid",
    )?;
    let offer = field(&body, "server_offer")?;
    let keys: BTreeSet<_> = offer.as_object()?.keys().map(String::as_str).collect();
    let mut expected: BTreeSet<_> = QUOTE_KEYS.iter().copied().collect();
    expected.insert("enabled");
    require(
        keys == expected && text(offer, "offer_id")? == id && body == listing_body(offer)?,
        "offer_resource_invalid",
    )?;
    Ok(Some(offer.clone()))
}
fn verify_projection(ctx: &Context<'_>, offer: &Json) -> Result<Option<Json>> {
    let source = authoritative_offer(ctx, text(offer, "offer_id")?)?;
    if let Some(source) = &source {
        require(
            field(source, "enabled")?.as_bool()?,
            "offer_projection_mismatch",
        )?;
        for name in QUOTE_KEYS {
            require(
                field(source, name)? == field(offer, name)?,
                "offer_projection_mismatch",
            )?;
        }
    }
    Ok(source)
}
fn offer_listing(offer: &Json) -> Result<Json> {
    encode(
        &json!({"listing_id":field(offer,"offer_id")?,"listing_revision":field(offer,"price_revision")?,"source":"server_offer","mode":"sale","seller":"u_root","item_kind":"entitlement","state":"active","price_minor":field(offer,"price_minor")?,"currency_id":"primary","unit":field(offer,"unit")?,"min_quantity":field(offer,"min_quantity")?,"max_quantity":field(offer,"max_quantity")?,"resource_kind":field(offer,"resource_kind")?,"entitlement_kind":field(offer,"entitlement_kind")?,"duration_seconds":field(offer,"duration_seconds")?,"escrow_policy":"legacy-entitlement-v1","purchase_operation":"money.redeem","purchase_versions":[1,2],"offer_snapshot":offer}),
    )
}
fn read_offer(ctx: &Context<'_>, id: &str, error: &'static str) -> Result<Json> {
    let row = ctx.tx.one("SELECT offer_id,resource_kind,unit,price_minor,min_quantity,max_quantity,entitlement_kind,duration_seconds,price_revision FROM server_offers WHERE offer_id=$1 AND enabled=TRUE", &[id.into()])?.ok_or(Error(error))?;
    let offer = public_offer(&row)?;
    valid_offer(ctx, &offer).map_err(|_| Error(error))?;
    Ok(offer)
}
pub(super) fn listing(ctx: &Context<'_>) -> Result<HandlerOutput> {
    let offer = read_offer(ctx, ctx.text("id")?, "listing_not_found")?;
    verify_projection(ctx, &offer)?;
    require(
        ctx.args()?
            .get("revision")
            .map(Json::as_str)
            .transpose()?
            .unwrap_or(text(&offer, "price_revision")?)
            == text(&offer, "price_revision")?,
        "listing_not_found",
    )?;
    output(json!({"listing":offer_listing(&offer)?}), vec![])
}
fn purchase(tx: &PgSession, id: &str, subject: &str) -> Result<Json> {
    let row = tx.one("SELECT id,subject_id,request_id,offer_snapshot,quantity,total_minor,escrow_account,state,created_at,expires_at,funding_transaction_id,final_transaction_id,entitlement_id,reason FROM money_purchases WHERE id=$1 AND subject_id=$2", &[id.into(), subject.into()])?.ok_or(Error("purchase_not_found"))?;
    let mut value = columns(
        &row,
        &[
            "id",
            "subject_id",
            "request_id",
            "offer_snapshot",
            "quantity",
            "total_minor",
            "escrow_account",
            "state",
            "created_at",
            "expires_at",
            "funding_transaction_id",
            "final_transaction_id",
            "entitlement_id",
            "reason",
        ],
    )?
    .into_object()?;
    value.insert("offer_snapshot".into(), row.json(3)?);
    Ok(Json::from_object(value))
}
pub(super) fn purchase_view(ctx: &Context<'_>, id: &str, viewer: &str) -> Result<Json> {
    purchase_order(ctx.tx, &purchase(ctx.tx, id, viewer)?)
}
fn purchase_order(tx: &PgSession, p: &Json) -> Result<Json> {
    let quote = field(p, "offer_snapshot")?;
    let state = text(p, "state")?;
    let buyer = text(p, "subject_id")?;
    let escrow = text(p, "escrow_account")?;
    let total = integer(p, "total_minor")?;
    require(
        matches!(state, "pending" | "settled" | "refunded")
            && text(quote, "currency_id")? == "primary"
            && integer(quote, "provider_version")? == 1
            && integer(quote, "price_minor")? > 0
            && integer(p, "quantity")?.checked_mul(integer(quote, "price_minor")?) == Some(total),
        "legacy_purchase_invalid",
    )?;
    let account = tx
        .one(
            "SELECT kind,subject_id,source_id FROM ledger_accounts WHERE id=$1",
            &[escrow.into()],
        )?
        .ok_or(Error("legacy_purchase_invalid"))?;
    require(
        account.text(0)? == "purchase_escrow"
            && account.optional_text(1)?.is_none()
            && account.text(2)? == text(p, "id")?,
        "legacy_purchase_invalid",
    )?;
    let funding = tx.one("SELECT debit_account,credit_account,amount_minor,committed_at FROM money_ledger WHERE id=$1", &[text(p,"funding_transaction_id")?.into()])?.ok_or(Error("legacy_purchase_invalid"))?;
    require(
        funding.text(0)? == buyer && funding.text(1)? == escrow && funding.integer(2)? == total,
        "legacy_purchase_invalid",
    )?;
    let mut final_at = None;
    let mut refs = vec![field(p, "funding_transaction_id")?.clone()];
    if state == "pending" {
        require(
            field(p, "final_transaction_id")?.is_null() && field(p, "entitlement_id")?.is_null(),
            "legacy_purchase_invalid",
        )?;
    } else {
        let final_row = tx.one("SELECT debit_account,credit_account,amount_minor,committed_at FROM money_ledger WHERE id=$1", &[text(p,"final_transaction_id")?.into()])?.ok_or(Error("legacy_purchase_invalid"))?;
        require(
            final_row.text(0)? == escrow
                && final_row.text(1)? == if state == "settled" { "u_root" } else { buyer }
                && final_row.integer(2)? == total,
            "legacy_purchase_invalid",
        )?;
        if state == "settled" {
            let grant = tx.one("SELECT subject_id,purchase_request_id,redeem_transaction_id FROM resource_entitlements WHERE id=$1", &[text(p,"entitlement_id")?.into()])?.ok_or(Error("legacy_purchase_invalid"))?;
            require(
                grant.text(0)? == buyer
                    && grant.text(1)? == text(p, "request_id")?
                    && grant.text(2)? == text(p, "final_transaction_id")?,
                "legacy_purchase_invalid",
            )?;
        } else {
            require(
                field(p, "entitlement_id")?.is_null(),
                "legacy_purchase_invalid",
            )?;
        }
        final_at = Some(final_row.text(3)?.to_owned());
        refs.push(field(p, "final_transaction_id")?.clone());
    }
    encode(
        &json!({"id":field(p,"id")?,"source":"legacy_purchase","buyer":buyer,"seller":"u_root","listing_id":field(quote,"offer_id")?,"listing_revision":field(quote,"price_revision")?,"listing_source":"server_offer","quantity":field(p,"quantity")?,"unit_price_minor":field(quote,"price_minor")?,"total_price_minor":total,"currency_id":field(quote,"currency_id")?,"escrow_account_id":escrow,"escrow_policy":"legacy-entitlement-v1","state":if state=="pending"{"funded"}else{state},"legacy_state":state,"created_at":field(p,"created_at")?,"funded_at":funding.text(3)?,"settled_at":if state=="settled"{final_at.as_deref()}else{None},"refunded_at":if state=="refunded"{final_at.as_deref()}else{None},"accepted_at":null,"delivery_id":null,"delivered_at":null,"entitlement_id":field(p,"entitlement_id")?,"receipt_refs":refs,"offer_snapshot":quote}),
    )
}
pub(crate) fn extra_capacity(tx: &PgSession, subject: &str, now: Timestamp) -> Result<i64> {
    tx.one("SELECT COALESCE(SUM(quantity),0)::text FROM resource_entitlements WHERE subject_id=$1 AND entitlement_kind=$2 AND (expires_at IS NULL OR expires_at>$3)", &[subject.into(), ENTITLEMENT.into(), time(now).into()])?.ok_or(Error("entitlement_capacity_exceeded"))?.text(0)?.parse().map_err(|_| Error("entitlement_capacity_exceeded"))
}
fn capacity(tx: &PgSession, subject: &str, now: Timestamp, quantity: i64, base: u64) -> Result<()> {
    let sum =
        i128::from(extra_capacity(tx, subject, now)?) + i128::from(quantity) + i128::from(base);
    require(
        sum <= i128::from(MAX_CAPACITY),
        "entitlement_capacity_exceeded",
    )
}
fn grant(tx: &PgSession, p: &Json, now: Timestamp, base: u64, transaction: &str) -> Result<String> {
    let buyer = text(p, "subject_id")?;
    let request = text(p, "request_id")?;
    let quote = field(p, "offer_snapshot")?;
    let quantity = integer(p, "quantity")?;
    capacity(tx, buyer, now, quantity, base)?;
    let expires = if field(quote, "duration_seconds")?.is_null() {
        None
    } else {
        Some(time(order::add(now, integer(quote, "duration_seconds")?)?))
    };
    let id = format!("ent_{}", &hash(&[buyer, request])?[7..39]);
    tx.execute("INSERT INTO resource_entitlements(id,subject_id,offer_id,purchase_request_id,quantity,entitlement_kind,granted_at,expires_at,redeem_transaction_id) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)", &[id.as_str().into(),buyer.into(),text(quote,"offer_id")?.into(),request.into(),quantity.into(),text(quote,"entitlement_kind")?.into(),time(now).into(),expires.as_deref().into(),transaction.into()])?;
    Ok(id)
}
fn finalize(ctx: &Context<'_>, p: &Json, cancel: bool) -> Result<Json> {
    require(text(p, "state")? == "pending", "purchase_not_pending")?;
    let quote = field(p, "offer_snapshot")?;
    let reason = if cancel {
        Some("cancelled")
    } else if Timestamp::parse(text(p, "expires_at")?)? <= ctx.now {
        Some("expired")
    } else if !purchasable(ctx, quote) || integer(quote, "provider_version")? != 1 {
        Some("provider_unavailable")
    } else {
        None
    };
    let total = integer(p, "total_minor")?;
    let escrow = text(p, "escrow_account")?;
    let buyer = text(p, "subject_id")?;
    let id = text(p, "id")?;
    require(
        ledger::balance(ctx.tx, escrow)? == total,
        "escrow_balance_mismatch",
    )?;
    let receipt = ledger::release_protected(
        ctx.tx,
        &ctx.settings.receipt_signer,
        &ledger::EscrowAccount {
            id: escrow,
            source: id,
            kind: "purchase_escrow",
            buyer,
            seller: "u_root",
        },
        &ledger::Posting {
            sender: escrow,
            recipient: if reason.is_some() { buyer } else { "u_root" },
            amount: total,
            actor: buyer,
            request: ctx.request.field("request_id")?.as_str()?,
            reference: Some(&format!(
                "purchase_{}:{id}",
                if reason.is_some() { "refund" } else { "settle" }
            )),
            kind: if reason.is_some() { "refund" } else { "redeem" },
            entry_key: "purchase_final",
            now: ctx.now,
        },
    )?;
    let transaction = text(field(&receipt, "body")?, "transaction_id")?;
    let entitlement = if reason.is_some() {
        None
    } else {
        Some(grant(
            ctx.tx,
            p,
            ctx.now,
            ctx.settings.hosting_base_capacity_bytes,
            transaction,
        )?)
    };
    require(ctx.tx.execute("UPDATE money_purchases SET state=$1,final_transaction_id=$2,entitlement_id=$3,reason=$4 WHERE id=$5 AND state='pending'", &[if reason.is_some(){"refunded".into()}else{"settled".into()},transaction.into(),entitlement.as_deref().into(),reason.into(),id.into()])? == 1, "purchase_not_pending")?;
    Ok(receipt)
}
fn redeem(ctx: &mut Context<'_>, version: u32) -> Result<HandlerOutput> {
    let buyer = money::owner(ctx)?.to_owned();
    let offer = read_offer(ctx, ctx.text("offer_id")?, "offer_not_found")?;
    let source = verify_projection(ctx, &offer)?;
    require(
        text(&offer, "price_revision")? == ctx.text("price_revision")?,
        "offer_price_changed",
    )?;
    let quantity = integer(ctx.request.field("arguments")?, "quantity")?;
    let total = integer(&offer, "price_minor")?
        .checked_mul(quantity)
        .ok_or(Error("invalid_offer_quantity"))?;
    require(
        integer(&offer, "min_quantity")? <= quantity
            && quantity <= integer(&offer, "max_quantity")?,
        "invalid_offer_quantity",
    )?;
    capacity(
        ctx.tx,
        &buyer,
        ctx.now,
        quantity,
        ctx.settings.hosting_base_capacity_bytes,
    )?;
    if version == 3 {
        return redeem_order(
            ctx,
            &buyer,
            &offer,
            &source.ok_or(Error("offer_requires_import"))?,
            quantity,
            total,
        );
    }
    let id = new_id("pur")?;
    let escrow = new_id("esc")?;
    ctx.tx.execute("INSERT INTO ledger_accounts(id,kind,subject_id,source_id) VALUES($1,'purchase_escrow',NULL,$2)", &[escrow.as_str().into(),id.as_str().into()])?;
    let funding = ledger::transfer(
        ctx.tx,
        &ctx.settings.receipt_signer,
        &ledger::Posting {
            sender: &buyer,
            recipient: &escrow,
            amount: total,
            actor: &buyer,
            request: ctx.request.field("request_id")?.as_str()?,
            reference: Some(&format!("purchase_fund:{id}")),
            kind: "transfer",
            entry_key: "purchase_fund",
            now: ctx.now,
        },
    )?;
    ctx.tx.execute("INSERT INTO money_purchases(id,subject_id,request_id,offer_snapshot,quantity,total_minor,escrow_account,state,created_at,expires_at,funding_transaction_id) VALUES($1,$2,$3,$4,$5,$6,$7,'pending',$8,$9,$10)", &[id.as_str().into(),buyer.as_str().into(),ctx.request.field("request_id")?.as_str()?.into(),offer.canonical()?.into(),quantity.into(),total.into(),escrow.as_str().into(),time(ctx.now).into(),time(order::add(ctx.now,900)?).into(),text(field(&funding,"body")?,"transaction_id")?.into()])?;
    let settlement = if ctx
        .args()?
        .get("defer")
        .map(Json::as_bool)
        .transpose()?
        .unwrap_or(false)
    {
        None
    } else {
        Some(finalize(ctx, &purchase(ctx.tx, &id, &buyer)?, false)?)
    };
    output(
        json!({"purchase":purchase(ctx.tx,&id,&buyer)?,"funding":funding,"settlement":settlement}),
        vec![],
    )
}
fn redeem_order(
    ctx: &mut Context<'_>,
    buyer: &str,
    offer: &Json,
    source: &Json,
    quantity: i64,
    total: i64,
) -> Result<HandlerOutput> {
    let resource = ctx.tx.resource_record(text(offer, "offer_id")?)?;
    require(
        resource.owner == "u_root"
            && resource.revision.as_deref() == Some(ctx.text("listing_revision")?),
        "offer_listing_changed",
    )?;
    require(
        ctx.text("offer_snapshot_digest")? == hash(offer)?,
        "offer_snapshot_changed",
    )?;
    require(
        total == integer(ctx.request.field("arguments")?, "total_price_minor")?,
        "price_changed",
    )?;
    let listing = listing_body(source)?;
    let id = checkout::order_id()?;
    let target = encode(
        &json!({"subject_id":buyer,"channel":"site","handle_snapshot":ctx.tx.resource_record(buyer)?.name,"email":{"state":"disabled"}}),
    )?;
    let order = order::Order {
        id: id.clone(),
        buyer: buyer.into(),
        seller: "u_root".into(),
        listing_id: resource.id.clone(),
        listing_revision: resource
            .revision
            .clone()
            .ok_or(Error("offer_listing_changed"))?,
        package_id: None,
        package_revision: None,
        package_digest: None,
        quantity,
        unit_price_minor: integer(offer, "price_minor")?,
        total_price_minor: total,
        currency_id: "primary".into(),
        escrow_subject: format!("esc_{}", &id[4..]),
        escrow_policy: text(&listing, "escrow_policy")?.into(),
        dispute_policy: text(&listing, "dispute_policy")?.into(),
        terms_digest: hash(field(&listing, "terms")?)?,
        delivery_target: target,
        payment_intent_digest: Some(ctx.request.field("payload_digest")?.as_str()?.into()),
        payment_transaction_id: None,
        state: "created".into(),
        created_at: time(ctx.now),
        funded_at: None,
        delivered_at: None,
        settled_at: None,
        receipt_refs: vec![],
    };
    checkout::insert(ctx.tx, &order)?;
    let credential = ctx.tx.credential(
        ctx.principal
            .credential_id()
            .ok_or(Error("signature_required"))?,
    )?;
    let policy = json!({"id":"deterministic-entitlement-v1","version":1,"funding_timeout_seconds":900,"delivery_timeout_seconds":900});
    let locked = encode(
        &json!({"order_id":id,"version":5,"resource_model":1,"buyer":buyer,"seller":"u_root","listing":listing,"listing_id":resource.id,"listing_revision":resource.revision,"escrow_subject":order.escrow_subject,"package_id":null,"package_revision":null,"package_digest":null,"quantity":quantity,"total_price_minor":total,"terms_digest":order.terms_digest,"policy":{"policy":policy,"policy_digest":hash(&policy)?},"settlement_policy":{"id":"deterministic-entitlement-v1","version":1},"offer_snapshot":offer,"redemption_request_id":ctx.request.field("request_id")?,"redemption_request":Json::parse(&ctx.request.canonical()?)?,"redemption_signer":b64(&credential.verifier.0),"recipient_key":null,"created_at":time(ctx.now)}),
    )?;
    ctx.tx.execute(
        "INSERT INTO order_contracts(order_id,body,digest) VALUES($1,$2,$3)",
        &[
            id.as_str().into(),
            locked.canonical()?.into(),
            hash(&locked)?.into(),
        ],
    )?;
    let mut mutation = order::Mutation::new(ctx.tx, &id)?;
    let funding = checkout::fund(ctx, &mut mutation)?;
    let p = encode(
        &json!({"subject_id":buyer,"request_id":ctx.request.field("request_id")?,"quantity":quantity,"offer_snapshot":offer}),
    )?;
    let now = ctx.now;
    let base = ctx.settings.hosting_base_capacity_bytes;
    let settlement = mutation
        .add_entitlement_settlement(ctx, |tx, transaction| grant(tx, &p, now, base, transaction))?;
    let result = checkout::view(ctx.tx, &order::read(ctx.tx, &id)?, buyer)?;
    let resources = mutation.publish_context(ctx)?.into_iter().collect();
    output(
        json!({"order":result,"funding":funding,"settlement":settlement}),
        resources,
    )
}
pub(super) fn handle(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let operation = ctx.request.field("operation")?.as_str()?.to_owned();
    let version = ctx
        .request
        .field("contract_version")?
        .as_integer()?
        .parse::<u32>()
        .map_err(|_| Error("invalid_version"))?;
    if operation == "money.offers" {
        let mut result = vec![];
        let mut after = String::new();
        loop {
            ctx.check_deadline()?;
            let rows = ctx.tx.rows("SELECT offer_id,resource_kind,unit,price_minor,min_quantity,max_quantity,entitlement_kind,duration_seconds,price_revision FROM server_offers WHERE enabled=TRUE AND offer_id>$1 ORDER BY offer_id LIMIT 128", &[after.as_str().into()])?;
            for row in &rows {
                after = row.text(0)?.into();
                let offer = public_offer(row)?;
                if valid_offer(ctx, &offer).is_ok() {
                    verify_projection(ctx, &offer)?;
                    result.push(offer);
                }
            }
            if rows.len() < 128 {
                break;
            }
        }
        let mut data = json!({"currency_id":"primary","offers":result});
        if version == 2 {
            data["listings"] = json!(result
                .iter()
                .map(offer_listing)
                .collect::<Result<Vec<_>>>()?);
        }
        return output(data, vec![]);
    }
    if operation == "money.redeem" {
        return redeem(ctx, version);
    }
    let owner = money::owner(ctx)?.to_owned();
    if operation == "money.entitlements" {
        let mut values = vec![];
        let mut after = String::new();
        loop {
            ctx.check_deadline()?;
            let rows = ctx.tx.rows("SELECT id,offer_id,quantity,entitlement_kind,granted_at,expires_at,redeem_transaction_id FROM resource_entitlements WHERE subject_id=$1 AND id>$2 ORDER BY id LIMIT 128", &[owner.as_str().into(),after.as_str().into()])?;
            for row in &rows {
                after = row.text(0)?.into();
                values.push(columns(
                    row,
                    &[
                        "id",
                        "offer_id",
                        "quantity",
                        "entitlement_kind",
                        "granted_at",
                        "expires_at",
                        "transaction_id",
                    ],
                )?);
            }
            if rows.len() < 128 {
                break;
            }
        }
        return output(
            json!({"entitlements":values,"hosting_extra_bytes":extra_capacity(ctx.tx,&owner,ctx.now)?}),
            vec![],
        );
    }
    let id = ctx.text("purchase_id")?.to_owned();
    let p = purchase(ctx.tx, &id, &owner)?;
    match operation.as_str() {
        "money.purchase_get" => output(
            if version == 2 {
                json!({"purchase":p,"order":purchase_order(ctx.tx,&p)?})
            } else {
                json!({"purchase":p})
            },
            vec![],
        ),
        "money.purchase_settle" | "money.purchase_cancel" => {
            let receipt = finalize(ctx, &p, operation == "money.purchase_cancel")?;
            output(
                if operation == "money.purchase_cancel" {
                    json!({"purchase":purchase(ctx.tx,&id,&owner)?,"refund":receipt})
                } else {
                    json!({"purchase":purchase(ctx.tx,&id,&owner)?,"settlement":receipt})
                },
                vec![],
            )
        }
        _ => Err(Error("unknown_operation")),
    }
}
