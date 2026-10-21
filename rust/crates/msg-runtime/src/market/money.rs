//! Account projection and signed transfers. Escrow release stays in ledger.
use super::{integer, ledger, require};
use crate::context::{output, Context, HandlerOutput};
use msg_core::{Error, Result};
use msg_identity::{models::SubjectKind, store::AuthorityStore};
use msg_storage::records::ResourceState;
use serde_json::json;

pub(super) fn supports(op: &str, version: u32) -> bool {
    version == 1 && matches!(op, "money.state" | "money.banks" | "money.balance" | "money.ledger" | "money.public_balance" | "money.public_ledger" | "money.visibility_set" | "money.transfer")
}
pub(super) fn owner<'a>(ctx: &'a Context<'_>) -> Result<&'a str> {
    let subject = ctx.principal.subject().ok_or(Error("money_subject_required"))?;
    require(ctx.principal.actor() == Some(subject), "money_subject_required")?;
    Ok(subject)
}
fn publication(ctx: &Context<'_>, subject: &str) -> Result<&'static str> {
    let resource = ctx.tx.resource_record(subject)?;
    require(resource.resource_type == "user" && resource.state == ResourceState::Active, "not_found")?;
    require(ctx.tx.one("SELECT 1 FROM identities WHERE id=$1 AND kind='subject'", &[subject.into()])?.is_some(), "not_found")?;
    let identity = ctx.tx.subject(subject)?;
    require(subject == "u_root" || matches!(identity.kind, SubjectKind::Registered | SubjectKind::Custodial), "not_found")?;
    let bank = ctx.tx.one("SELECT 1 FROM money_bank_roles WHERE subject_id=$1 AND status='active'", &[subject.into()])?.is_some();
    let policy = ctx.tx.one("SELECT visibility FROM money_visibility WHERE subject_id=$1", &[subject.into()])?;
    let opted = policy.map(|r| r.text(0).map(|s|s=="public")).transpose()?.unwrap_or(false);
    require(subject == "u_root" || bank || opted, "not_found")?;
    Ok(if subject == "u_root" { "root" } else if bank { "bank" } else { "opt_in" })
}
pub(super) fn handle(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    match ctx.request.field("operation")?.as_str()? {
        "money.state" => output(json!({"currency_id":"primary","display_name":ctx.settings.money_display_name,"code":ctx.settings.money_code,"scale":6,"total_supply_minor":ledger::supply(ctx.tx)?}),vec![]),
        "money.banks" => {
            let banks=ctx.tx.rows("SELECT subject_id,status,granted_at,revoked_at FROM money_bank_roles ORDER BY subject_id", &[])?.iter().map(|r| Ok(json!({"subject_id":r.text(0)?,"status":r.text(1)?,"granted_at":r.text(2)?,"revoked_at":r.optional_text(3)?}))).collect::<Result<Vec<_>>>()?;
            output(json!({"banks":banks}),vec![])
        }
        "money.balance" => {let subject=owner(ctx)?;output(json!({"subject_id":subject,"currency_id":"primary","balance_minor":ledger::balance(ctx.tx,subject)?}),vec![])},
        "money.public_balance" => {let subject=ctx.text("subject_id")?;let reason=publication(ctx,subject)?;output(json!({"subject_id":subject,"currency_id":"primary","scale":6,"balance_minor":ledger::balance(ctx.tx,subject)?,"publication":reason}),vec![])},
        "money.ledger" | "money.public_ledger" => ledger_view(ctx),
        "money.visibility_set" => {
            let subject=owner(ctx)?;
            let identity=ctx.tx.subject(subject)?;
            let resource=ctx.tx.resource_record(subject)?;
            require(matches!(identity.kind,SubjectKind::Registered|SubjectKind::Custodial) && !identity.local_only && resource.state==ResourceState::Active,"money_subject_required")?;
            let visibility=ctx.text("visibility")?;
            ctx.tx.execute("INSERT INTO money_visibility(subject_id,visibility,updated_at) VALUES($1,$2,$3) ON CONFLICT(subject_id) DO UPDATE SET visibility=excluded.visibility,updated_at=excluded.updated_at",&[subject.into(),visibility.into(),super::time(ctx.now).into()])?;
            let mandatory=ctx.tx.one("SELECT 1 FROM money_bank_roles WHERE subject_id=$1 AND status='active'",&[subject.into()])?.is_some();
            output(json!({"subject_id":subject,"visibility":visibility,"effective_visibility":if mandatory{"public"}else{visibility},"bank_publication_required":mandatory}),vec![])
        }
        "money.transfer" => {
            let subject=owner(ctx)?;
            let sender=ctx.args()?.get("from_subject").map(|v|v.as_str()).transpose()?.unwrap_or(subject);
            require(!matches!(sender,"u_root"|"@root"),"root_local_only")?;
            require(sender==subject,"money_sender_mismatch")?;
            let source=ctx.tx.subject(subject)?;
            let destination=ctx.tx.subject(ctx.text("to_subject")?)?;
            require(matches!(source.kind,SubjectKind::Registered|SubjectKind::Custodial)&&!source.local_only,"money_subject_required")?;
            require(matches!(destination.kind,SubjectKind::Registered|SubjectKind::Custodial)&&!destination.local_only,"invalid_money_recipient")?;
            let receipt=ledger::transfer(ctx.tx,&ctx.settings.receipt_signer,&ledger::Posting {sender:subject,recipient:&destination.resource_id,amount:integer(ctx.request.field("arguments")?,"amount_minor")?,actor:subject,request:ctx.request.field("request_id")?.as_str()?,reference:ctx.args()?.get("reference").map(|v|v.as_str()).transpose()?,kind:"transfer",entry_key:"primary",now:ctx.now})?;
            output(json!({"transfer":receipt,"balance_minor":ledger::balance(ctx.tx,subject)?}),vec![])
        }
        _ => Err(Error("unknown_operation")),
    }
}
fn ledger_view(ctx:&Context<'_>)->Result<HandlerOutput>{
    let public=ctx.request.field("operation")?.as_str()?=="money.public_ledger";
    let subject=if public{ctx.text("subject_id")?}else{owner(ctx)?};
    let publication=if public{Some(publication(ctx,subject)?)}else{None};
    let args=ctx.request.field("arguments")?;
    let cursor=if ctx.args()?.contains_key("cursor"){integer(args,"cursor")?}else{0};
    let limit=if ctx.args()?.contains_key("limit"){integer(args,"limit")?}else{50};
    require(cursor>=0&&(1..=100).contains(&limit),"invalid_page")?;
    let rows=ctx.tx.rows(if public{"SELECT seq,id,kind,amount_minor,debit_account,credit_account,committed_at FROM money_ledger WHERE seq>$1 AND (debit_account=$2 OR credit_account=$2) ORDER BY seq LIMIT $3"}else{"SELECT seq,id,kind,amount_minor,debit_account,credit_account,committed_at,policy_version,policy_digest,reference,receipt FROM money_ledger WHERE seq>$1 AND (debit_account=$2 OR credit_account=$2) ORDER BY seq LIMIT $3"},&[cursor.into(),subject.into(),(limit+1).into()])?;
    let items=rows.iter().take(limit as usize).map(|r|->Result<_>{
        if public {Ok(json!({"ledger_sequence":r.integer(0)?,"transaction_id":r.text(1)?,"kind":r.text(2)?,"amount_minor":r.integer(3)?,"from_account":r.optional_text(4)?,"to_account":r.optional_text(5)?,"committed_at":r.text(6)?}))}
        else{let outgoing=r.optional_text(4)?==Some(subject);Ok(json!({"ledger_sequence":r.integer(0)?,"transaction_id":r.text(1)?,"kind":r.text(2)?,"amount_minor":r.integer(3)?,"direction":if outgoing{"out"}else{"in"},"counterparty":r.optional_text(if outgoing{5}else{4})?,"committed_at":r.text(6)?,"policy_version":r.integer(7)?,"policy_digest":r.text(8)?,"reference":r.optional_text(9)?,"receipt":r.json(10)?}))}
    }).collect::<Result<Vec<_>>>()?;
    let next=if rows.len()>limit as usize{Some(rows[limit as usize-1].integer(0)?)}else{None};
    output(if public{json!({"subject_id":subject,"currency_id":"primary","scale":6,"publication":publication,"items":items,"next_cursor":next})}else{json!({"currency_id":"primary","items":items,"next_cursor":next})},vec![])
}
