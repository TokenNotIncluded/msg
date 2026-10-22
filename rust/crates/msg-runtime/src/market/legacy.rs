//! Published v1/v2 checkout decisions. They never adopt v3/v4 policy implicitly.
use super::{field, hash, integer, ledger, order::Order, require, text, time};
use crate::context::Context;
use msg_core::{b64, Error, Json, Result};
use msg_crypto::{verify, Signature};
use msg_identity::models::Timestamp;
use msg_storage::{
    postgres::PgSession,
    records::{decode, encode},
};
use serde_json::json;

pub(super) fn policy(escrow: &str, dispute: &str) -> Result<Json> {
    require(
        dispute == "dispute-v1" && matches!(escrow, "escrow-v1" | "escrow-instant-v1"),
        "escrow_policy_unsupported",
    )?;
    let mut value=encode(&json!({"escrow_policy":"escrow-v1","dispute_policy":"dispute-v1","version":1,"reasons":["buyer_cancel","buyer_accept"],"arbitration_enabled":false,"automatic_timeout_enabled":false,"allow_split":false}))?.into_object()?;
    if escrow == "escrow-instant-v1" {
        value.insert("escrow_policy".into(), Json::string(escrow));
        value.insert(
            "reasons".into(),
            encode(&["buyer_cancel", "buyer_accept", "checkout_accept"])?,
        );
        value.insert("checkout_contract".into(), Json::string("orders.buy@2"));
        value.insert("requires_package_digest".into(), encode(&true)?);
        value.insert("checkout_claims_delivery".into(), encode(&false)?);
    }
    Ok(Json::from_object(value))
}
pub(super) fn settle(
    ctx: &mut Context<'_>,
    order: &Order,
    reason: &str,
) -> Result<(Json, Json, Option<Json>)> {
    let owned_buyer = super::signed_subject(ctx)?.to_owned();
    let buyer = owned_buyer.as_str();
    require(
        !super::checkout::has_contract(ctx.tx, &order.id)?,
        "order_contract_requires_v3",
    )?;
    require(
        ctx.tx
            .one(
                "SELECT 1 FROM order_settlements WHERE order_id=$1",
                &[order.id.as_str().into()],
            )?
            .is_none(),
        "order_already_resolved",
    )?;
    require(order.buyer == buyer, "order_not_found")?;
    let policy = policy(&order.escrow_policy, &order.dispute_policy)?;
    require(
        field(&policy, "reasons")?
            .as_array()?
            .iter()
            .any(|v| v.as_str().ok() == Some(reason)),
        "escrow_reason_unsupported",
    )?;
    ctx.authorizer
        .require_base(ctx.principal, &ctx.operation()?, buyer, ctx.tx, ctx.now)?;
    let operation = ctx.request.field("operation")?.as_str()?;
    let version = ctx.request.field("contract_version")?.as_integer()?;
    let delivery = super::delivery::read(ctx.tx, &order.id)?;
    let (recipient, outcome, state) = if reason == "buyer_cancel" {
        require(
            operation == "orders.cancel" && version == "1",
            "escrow_decision_mismatch",
        )?;
        require(
            order.state == "funded" && order.delivered_at.is_none() && delivery.is_none(),
            "order_not_cancellable",
        )?;
        (buyer, "refund", "refunded")
    } else if matches!(reason, "buyer_accept" | "checkout_accept") {
        if reason == "checkout_accept" {
            require(
                operation == "orders.buy"
                    && version == "2"
                    && ctx.arg("auto_accept")?.as_bool()?
                    && Some(ctx.text("package_digest")?) == order.package_digest.as_deref()
                    && Some(ctx.request.field("payload_digest")?.as_str()?)
                        == order.payment_intent_digest.as_deref(),
                "escrow_decision_mismatch",
            )?;
        } else {
            require(
                operation == "delivery.accept" && version == "1",
                "escrow_decision_mismatch",
            )?;
        }
        let delivery = delivery.as_ref().ok_or(Error("delivery_mismatch"))?;
        super::delivery::verify_managed(ctx.tx, ctx.contents, order, delivery)?;
        if reason != "checkout_accept" {
            require(
                text(delivery, "delivery_digest")? == ctx.text("delivery_digest")?,
                "delivery_mismatch",
            )?;
        }
        require(
            order.state == "delivered"
                && order.delivered_at.is_some()
                && text(delivery, "state")? == "prepared",
            "delivery_not_acceptable",
        )?;
        require(
            order.delivery_target == encode(&json!({"subject_id":buyer,"channel":"site"}))?,
            "delivery_recipient_mismatch",
        )?;
        (order.seller.as_str(), "release", "settled")
    } else {
        return Err(Error("escrow_reason_unsupported"));
    };
    require(
        order.currency_id == "primary"
            && ledger::balance(ctx.tx, &order.escrow_subject)? == order.total_price_minor,
        "escrow_balance_mismatch",
    )?;
    require(
        ctx.tx
            .one(
                "SELECT 1 FROM order_escrow_decisions WHERE order_id=$1",
                &[order.id.as_str().into()],
            )?
            .is_none(),
        "order_already_resolved",
    )?;
    let request = ctx.request.field("request_id")?.as_str()?;
    let payload = ctx.request.field("payload_digest")?.as_str()?;
    let ledger_request = if reason == "checkout_accept" {
        hash(&json!(["checkout-settlement-v1", buyer, request, order.id]))?
    } else {
        request.into()
    };
    let mut body=encode(&json!({"id":format!("ed_{}",&hash(&json!([order.id,payload,reason]))?[7..]),"order_id":order.id,"outcome":outcome,"reason":reason,"recipient":recipient,"currency_id":"primary","amount_minor":order.total_price_minor,"policy_version":field(&policy,"version")?,"policy_digest":hash(&policy)?,"order_digest":hash(order)?,"evidence_digest":hash(&delivery)?,"request_id":request,"request_digest":payload,"actor":buyer,"issued_at":ctx.now,"expires_at":ctx.request.field("expires_at")?}))?.into_object()?;
    if reason == "checkout_accept" {
        body.insert("ledger_request_id".into(), Json::string(&ledger_request));
    }
    let body = Json::from_object(body);
    let signature = ctx
        .settings
        .receipt_signer
        .sign(body.canonical()?.as_bytes(), "escrow-decision-v1")?;
    let decision = encode(&json!({"body":body,"signature":signature}))?;
    require(
        Timestamp::parse(text(&body, "expires_at")?)? > ctx.now,
        "escrow_decision_expired",
    )?;
    let receipt = ledger::release(
        ctx.tx,
        &ctx.settings.receipt_signer,
        ledger::Release {
            order,
            recipient,
            amount: order.total_price_minor,
            actor: buyer,
            request: &ledger_request,
            reference: &format!("order_{outcome}:{}", order.id),
            kind: if outcome == "refund" {
                "refund"
            } else {
                "transfer"
            },
            now: ctx.now,
        },
    )?;
    let transaction = text(field(&receipt, "body")?, "transaction_id")?;
    if reason == "checkout_accept" {
        require(ctx.tx.execute("UPDATE store_deliveries SET receipt=$1 WHERE order_id=$2 AND recipient_subject=$3 AND state='prepared'",&[receipt.canonical()?.into(),order.id.as_str().into(),buyer.into()])?==1,"delivery_not_acceptable")?;
    } else if outcome == "release" {
        require(ctx.tx.execute("UPDATE store_deliveries SET state='claimed',claimed_at=$1,receipt=$2 WHERE order_id=$3 AND recipient_subject=$4 AND state='prepared'",&[time(ctx.now).into(),receipt.canonical()?.into(),order.id.as_str().into(),buyer.into()])?==1,"delivery_not_acceptable")?;
    }
    let mut refs = order.receipt_refs.clone();
    refs.push(transaction.into());
    require(ctx.tx.execute("UPDATE store_orders SET state=$1,settled_at=$2,receipt_refs=$3 WHERE id=$4 AND buyer=$5 AND state=$6",&[state.into(),if outcome=="release"{Some(time(ctx.now))}else{order.settled_at.clone()}.into(),encode(&refs)?.canonical()?.into(),order.id.as_str().into(),buyer.into(),order.state.as_str().into()])?==1,"escrow_state_conflict")?;
    ctx.tx.execute("INSERT INTO order_escrow_decisions(order_id,id,body,signature,source_proof,transaction_id) VALUES($1,$2,$3,$4,$5,$6)",&[order.id.as_str().into(),text(&body,"id")?.into(),body.canonical()?.into(),encode(&signature)?.canonical()?.into(),encode(&json!({"signed_envelope":b64(&ctx.request.signing_bytes()?),"proof":ctx.request.field("proof")?}))?.canonical()?.into(),transaction.into()])?;
    Ok((receipt, decision, delivery))
}
pub(super) fn verify_checkout(
    tx: &PgSession,
    ctx: &Context<'_>,
    order: &Order,
    delivery: &Json,
) -> Result<()> {
    require(
        order.state == "settled"
            && order.settled_at.is_some()
            && !field(delivery, "receipt")?.is_null(),
        "delivery_not_acceptable",
    )?;
    let policy = policy(&order.escrow_policy, &order.dispute_policy)?;
    require(
        order.escrow_policy == "escrow-instant-v1",
        "delivery_not_acceptable",
    )?;
    let row = tx
        .one(
            "SELECT body,signature,transaction_id FROM order_escrow_decisions WHERE order_id=$1",
            &[order.id.as_str().into()],
        )?
        .ok_or(Error("escrow_decision_mismatch"))?;
    let body = row.json(0)?;
    require(
        text(&body, "order_id")? == order.id
            && text(&body, "reason")? == "checkout_accept"
            && text(&body, "outcome")? == "release"
            && text(&body, "actor")? == order.buyer
            && text(&body, "recipient")? == order.seller
            && text(&body, "currency_id")? == "primary"
            && integer(&body, "amount_minor")? == order.total_price_minor
            && field(&body, "policy_version")? == field(&policy, "version")?
            && text(&body, "policy_digest")? == hash(&policy)?
            && Some(text(&body, "request_digest")?) == order.payment_intent_digest.as_deref()
            && order.receipt_refs
                == vec![
                    order
                        .payment_transaction_id
                        .clone()
                        .ok_or(Error("escrow_decision_mismatch"))?,
                    row.text(2)?.into(),
                ]
            && order.settled_at.as_deref() == Some(text(&body, "issued_at")?),
        "escrow_decision_mismatch",
    )?;
    let mut issued = order.clone();
    issued.state = "delivered".into();
    issued.settled_at = None;
    issued.receipt_refs = order.payment_transaction_id.clone().into_iter().collect();
    let mut evidence = delivery.as_object()?.clone();
    evidence.insert("state".into(), Json::string("prepared"));
    evidence.insert("claimed_at".into(), Json::parse("null")?);
    evidence.insert("receipt".into(), Json::parse("null")?);
    let request = hash(&json!([
        "checkout-settlement-v1",
        order.buyer,
        text(&body, "request_id")?,
        order.id
    ]))?;
    require(
        text(&body, "order_digest")? == hash(&issued)?
            && text(&body, "evidence_digest")? == hash(&Json::from_object(evidence))?
            && text(&body, "ledger_request_id")? == request,
        "escrow_decision_mismatch",
    )?;
    require(
        Timestamp::parse(text(&body, "expires_at")?)?
            > Timestamp::parse(text(&body, "issued_at")?)?,
        "escrow_decision_expired",
    )?;
    let signature: Signature = decode(&row.json(1)?)?;
    verify(
        &ctx.settings.receipt_signer.public_key(),
        body.canonical()?.as_bytes(),
        &signature,
        "escrow-decision-v1",
    )?;
    let ledger=tx.one("SELECT debit_account,credit_account,amount_minor,currency_id,reference,receipt,actor,request_id FROM money_ledger WHERE id=$1",&[row.text(2)?.into()])?.ok_or(Error("escrow_decision_mismatch"))?;
    require(
        ledger.text(0)? == order.escrow_subject
            && ledger.text(1)? == order.seller
            && ledger.integer(2)? == order.total_price_minor
            && ledger.text(3)? == "primary"
            && ledger.text(4)? == format!("order_release:{}", order.id)
            && ledger.json(5)? == *field(delivery, "receipt")?
            && ledger.text(6)? == order.buyer
            && ledger.text(7)? == request
            && ledger::balance(tx, &order.escrow_subject)? == 0,
        "escrow_decision_mismatch",
    )
}
