//! Append-only balanced posting. Only this private boundary can debit escrow.
use super::{hash, require, time};
use crate::context::new_id;
use msg_core::{Error, Json, Result};
use msg_crypto::Ed25519Signer;
use msg_identity::models::Timestamp;
use msg_storage::{postgres::PgSession, records::encode};

const CURRENCY: &str = "primary";
const ROOT: &str = "u_root";

pub(super) fn balance(tx: &PgSession, account: &str) -> Result<i64> {
    // SUM(BIGINT) is NUMERIC in PostgreSQL. Subtract there before narrowing so
    // large historical turnover cannot overflow an otherwise valid balance.
    tx.one("SELECT (COALESCE((SELECT sum(amount_minor) FROM money_ledger WHERE credit_account=$1),0)-COALESCE((SELECT sum(amount_minor) FROM money_ledger WHERE debit_account=$1),0))::text",&[account.into()])?.ok_or(Error("invalid_money_state"))?.text(0)?.parse().map_err(|_|Error("invalid_money_state"))
}
pub(super) fn supply(tx: &PgSession) -> Result<i64> {
    tx.one("SELECT (COALESCE((SELECT sum(amount_minor) FROM money_ledger WHERE kind='mint'),0)-COALESCE((SELECT sum(amount_minor) FROM money_ledger WHERE kind='burn'),0))::text",&[])?.ok_or(Error("invalid_money_state"))?.text(0)?.parse().map_err(|_|Error("invalid_money_state"))
}
fn policy_digest() -> Result<String> {
    hash(
        &serde_json::json!({"version":2,"currency_id":CURRENCY,"scale":6,"transfer_fee":0,"allow_overdraft":false,"posting_identity":"actor/request_id/entry_key"}),
    )
}
struct Clearing<'a> {
    kind: &'a str,
    sender: Option<&'a str>,
    recipient: Option<&'a str>,
    amount: i64,
    sender_balance: i64,
    recipient_balance: i64,
    supply: i64,
    local_issuer: bool,
}
fn decide(value: &Clearing<'_>) -> Result<()> {
    require(value.amount > 0, "invalid_money_amount")?;
    require(
        value.sender_balance >= 0 && value.recipient_balance >= 0 && value.supply >= 0,
        "invalid_money_state",
    )?;
    require(
        matches!(
            value.kind,
            "mint" | "burn" | "transfer" | "redeem" | "refund"
        ),
        "invalid_money_kind",
    )?;
    if matches!(value.kind, "mint" | "burn") {
        require(value.local_issuer, "root_local_only")?;
        require(
            (value.kind == "mint" && value.sender.is_none() && value.recipient == Some(ROOT))
                || (value.kind == "burn"
                    && value.sender == Some(ROOT)
                    && value.recipient.is_none()),
            "invalid_money_accounts",
        )?;
    } else {
        require(
            value.sender.is_some() && value.recipient.is_some(),
            "invalid_money_accounts",
        )?;
        require(value.sender != value.recipient, "self_transfer_forbidden")?;
    }
    if matches!(value.sender, Some("u_root" | "@root")) {
        require(value.local_issuer, "root_local_only")?;
    }
    if value.sender.is_some() {
        require(value.sender_balance >= value.amount, "insufficient_funds")?;
    }
    if value.recipient.is_some() {
        require(
            value.recipient_balance.checked_add(value.amount).is_some(),
            "money_overflow",
        )?;
    }
    if value.kind == "burn" {
        require(value.supply >= value.amount, "invalid_money_state")?;
    }
    if value.kind == "mint" {
        require(
            value.supply.checked_add(value.amount).is_some(),
            "money_overflow",
        )?;
    }
    Ok(())
}

pub(super) struct Release<'a> {
    pub order: &'a super::order::Order,
    pub recipient: &'a str,
    pub amount: i64,
    pub actor: &'a str,
    pub request: &'a str,
    pub reference: &'a str,
    pub kind: &'a str,
    pub now: Timestamp,
}
pub(super) fn release(
    tx: &PgSession,
    signer: &Ed25519Signer,
    release: Release<'_>,
) -> Result<Json> {
    let Release {
        order,
        recipient,
        amount,
        actor,
        request,
        reference,
        kind,
        now,
    } = release;
    let account = tx
        .one(
            "SELECT kind,subject_id,source_id FROM ledger_accounts WHERE id=$1",
            &[order.escrow_subject.as_str().into()],
        )?
        .ok_or(Error("escrow_account_mismatch"))?;
    require(
        account.text(0)? == "order_escrow"
            && account.optional_text(1)?.is_none()
            && account.text(2)? == order.id,
        "escrow_account_mismatch",
    )?;
    require(
        recipient == order.buyer || recipient == order.seller,
        "escrow_recipient_mismatch",
    )?;
    require(
        matches!(kind, "transfer" | "redeem" | "refund"),
        "invalid_money_kind",
    )?;
    append(
        tx,
        signer,
        &Posting {
            sender: &order.escrow_subject,
            recipient,
            amount,
            actor,
            request,
            reference: Some(reference),
            kind,
            entry_key: "primary",
            now,
        },
    )
}

pub(super) struct Posting<'a> {
    pub sender: &'a str,
    pub recipient: &'a str,
    pub amount: i64,
    pub actor: &'a str,
    pub request: &'a str,
    pub reference: Option<&'a str>,
    pub kind: &'a str,
    pub entry_key: &'a str,
    pub now: Timestamp,
}
/// Ordinary transfers cannot name a protected account as their source.
pub(super) fn transfer(
    tx: &PgSession,
    signer: &Ed25519Signer,
    value: &Posting<'_>,
) -> Result<Json> {
    if let Some(row) = tx.one(
        "SELECT kind FROM ledger_accounts WHERE id=$1",
        &[value.sender.into()],
    )? {
        require(
            !matches!(row.text(0)?, "order_escrow" | "purchase_escrow"),
            "escrow_release_forbidden",
        )?;
    }
    append(tx, signer, value)
}
fn append(tx: &PgSession, signer: &Ed25519Signer, value: &Posting<'_>) -> Result<Json> {
    let Posting {
        sender,
        recipient,
        amount,
        actor,
        request,
        reference,
        kind,
        entry_key,
        now,
    } = *value;
    require(
        matches!(kind, "transfer" | "redeem" | "refund"),
        "invalid_money_kind",
    )?;
    decide(&Clearing {
        kind,
        sender: Some(sender),
        recipient: Some(recipient),
        amount,
        sender_balance: balance(tx, sender)?,
        recipient_balance: balance(tx, recipient)?,
        supply: supply(tx)?,
        local_issuer: false,
    })?;
    for subject in [sender, recipient] {
        tx.execute("INSERT INTO money_accounts(subject_id,currency_id) VALUES($1,$2) ON CONFLICT(subject_id,currency_id) DO NOTHING",&[subject.into(),CURRENCY.into()])?;
    }
    let id = new_id("lt")?;
    let sequence = tx
        .one(
            "SELECT nextval(pg_get_serial_sequence('money_ledger','seq'))",
            &[],
        )?
        .ok_or(Error("invalid_money_state"))?
        .integer(0)?;
    let policy = policy_digest()?;
    let body = encode(
        &serde_json::json!({"transaction_id":id,"kind":kind,"currency_id":CURRENCY,"amount_minor":amount,"from_subject":sender,"to_subject":recipient,"actor":actor,"request_id":request,"entry_key":entry_key,"reference":reference,"committed_at":now,"policy_version":2,"policy_digest":policy,"ledger_sequence":sequence}),
    )?;
    let signature = signer.sign(body.canonical()?.as_bytes(), "money-receipt")?;
    let receipt = encode(&serde_json::json!({"body":body,"signature":signature}))?;
    tx.execute("INSERT INTO money_ledger(seq,id,kind,currency_id,amount_minor,debit_account,credit_account,actor,request_id,reference,committed_at,policy_version,policy_digest,receipt,entry_key) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,2,$12,$13,$14)",&[sequence.into(),id.into(),kind.into(),CURRENCY.into(),amount.into(),sender.into(),recipient.into(),actor.into(),request.into(),reference.into(),time(now).into(),policy.into(),receipt.canonical()?.into(),entry_key.into()])?;
    Ok(receipt)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn clearing_does_not_overdraft_overflow_or_spend_root() {
        let mut value = Clearing {
            kind: "transfer",
            sender: Some("escrow_order"),
            recipient: Some("buyer"),
            amount: 10,
            sender_balance: 10,
            recipient_balance: 0,
            supply: 10,
            local_issuer: false,
        };
        decide(&value).unwrap();
        value.amount = 11;
        assert_eq!(decide(&value), Err(Error("insufficient_funds")));
        value.amount = 10;
        value.recipient_balance = i64::MAX;
        assert_eq!(decide(&value), Err(Error("money_overflow")));
        value.recipient_balance = 0;
        value.sender = Some(ROOT);
        assert_eq!(decide(&value), Err(Error("root_local_only")));
        value.sender = Some("buyer");
        assert_eq!(decide(&value), Err(Error("self_transfer_forbidden")));
    }
}
