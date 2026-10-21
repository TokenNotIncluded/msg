//! Current panel, vote, rationale and decision checks before any escrow write.
use super::{
    field, hash, integer,
    order::{self, Mutation, Order, Settlement},
    require, text, time,
};
use crate::context::new_id;
use msg_core::{Error, Json, Result};
use msg_crypto::{verify, Ed25519Signer, Signature};
use msg_identity::{
    authorization::AuthorizationService,
    models::{CredentialKind, SubjectKind, Timestamp},
    store::{truthy, AuthorityStore},
    worker_authority::WorkerAuthority,
};
use msg_storage::{
    content::{recovery::ContentTransaction, GitContentStore},
    postgres::PgSession,
    records::{decode, encode, BlobRef, ResourceState},
};
use std::collections::BTreeSet;

fn read_case(tx: &PgSession, id: &str) -> Result<Json> {
    let row = tx
        .one(
            "SELECT body,round,state,deadline,order_id FROM arbitration_cases WHERE id=$1",
            &[id.into()],
        )?
        .ok_or(Error("case_not_found"))?;
    let value = row.json(0)?;
    require(
        text(&value, "id")? == id && text(&value, "order_id")? == row.text(4)?,
        "decision_context_mismatch",
    )?;
    let mut value = value.into_object()?;
    value.insert("round".into(), encode(&row.integer(1)?)?);
    value.insert("state".into(), Json::string(row.text(2)?));
    value.insert("deadline".into(), Json::string(row.text(3)?));
    Ok(Json::from_object(value))
}
fn policy(case: &Json) -> Result<&Json> {
    field(field(case, "policy")?, "policy")
}
fn panel(case: &Json) -> Result<Vec<&str>> {
    field(field(case, "panels")?, &integer(case, "round")?.to_string())?
        .as_array()?
        .iter()
        .map(Json::as_str)
        .collect()
}
fn member_valid(tx: &PgSession, candidate: &str, case: &Json) -> Result<bool> {
    if candidate == text(case, "buyer")? || candidate == text(case, "seller")? {
        return Ok(false);
    }
    let epochs = field(field(case, "policy")?, "candidate_epochs")?.as_object()?;
    let Some(epoch) = epochs.get(candidate).filter(|v| truthy(v)) else {
        return Ok(false);
    };
    let Some(row) = tx.one(
        "SELECT epoch,active FROM arbitrator_roles WHERE subject=$1",
        &[candidate.into()],
    )?
    else {
        return Ok(false);
    };
    if !row.boolean(1)?
        || row.text(0)? != epoch.as_str()?
        || tx
            .one(
                "SELECT 1 FROM arbitrator_conflicts WHERE arbitrator=$1 AND party IN ($2,$3)",
                &[
                    candidate.into(),
                    text(case, "buyer")?.into(),
                    text(case, "seller")?.into(),
                ],
            )?
            .is_some()
    {
        return Ok(false);
    }
    let subject = match tx.subject(candidate) {
        Ok(value) => value,
        Err(Error("not_found")) => return Ok(false),
        Err(error) => return Err(error),
    };
    let resource = match tx.resource_record(candidate) {
        Ok(value) => value,
        Err(Error("not_found")) => return Ok(false),
        Err(error) => return Err(error),
    };
    Ok(!subject.local_only
        && subject.kind == SubjectKind::Registered
        && resource.state == ResourceState::Active)
}
fn panel_valid(tx: &PgSession, case: &Json) -> Result<bool> {
    let panel = panel(case)?;
    if panel.len() as i64 != integer(policy(case)?, "panel_size")? {
        return Ok(false);
    }
    for candidate in panel {
        if !member_valid(tx, candidate, case)? {
            return Ok(false);
        }
    }
    Ok(true)
}
fn proposal_valid(case: &Json, proposal: &Json, total: i64) -> Result<()> {
    let base = encode(
        &serde_json::json!({"case_id":field(case,"id")?,"order_id":field(case,"order_id")?,"round":field(case,"round")?,"policy_digest":field(field(case,"policy")?,"policy_digest")?,"expires_at":order::add(Timestamp::parse(field(field(case,"round_deadlines")?,&integer(case,"round")?.to_string())?.as_str()?)?,integer(policy(case)?,"decision_lifetime_seconds")?)?}),
    )?;
    let fields = proposal.as_object()?;
    require(
        fields.len() == 9
            && fields.keys().all(|k| {
                matches!(
                    k.as_str(),
                    "case_id"
                        | "order_id"
                        | "round"
                        | "policy_digest"
                        | "expires_at"
                        | "outcome"
                        | "refund_minor"
                        | "rationale_ref"
                        | "rationale_digest"
                )
            }),
        "decision_context_mismatch",
    )?;
    for (key, value) in base.as_object()? {
        require(fields.get(key) == Some(value), "decision_context_mismatch")?;
    }
    let refund = integer(proposal, "refund_minor")?;
    require(
        match text(proposal, "outcome")? {
            "release" => refund == 0,
            "refund" => refund == total,
            "split" => 0 < refund && refund < total,
            _ => false,
        },
        "decision_amount_mismatch",
    )
}
fn verify_rationale(
    tx: &PgSession,
    contents: &GitContentStore,
    case: &Json,
    proposal: &Json,
) -> Result<()> {
    let reference = field(proposal, "rationale_ref")?;
    let id = format!(
        "rationale_{}",
        &hash(
            &serde_json::json!({"case_id":field(case,"id")?,"round":field(case,"round")?,"ref":reference})
        )?[7..]
    );
    let row = tx
        .one(
            "SELECT author,visibility,body FROM arbitration_evidence WHERE id=$1 AND case_id=$2",
            &[id.into(), text(case, "id")?.into()],
        )?
        .ok_or(Error("decision_rationale_missing"))?;
    let body = row.json(2)?;
    require(
        panel(case)?.contains(&row.text(0)?)
            && row.text(1)? == "parties"
            && text(&body, "kind")? == "rationale"
            && field(&body, "round")? == field(case, "round")?
            && field(&body, "source")? == reference
            && field(field(&body, "blob")?, "digest")? == field(proposal, "rationale_digest")?,
        "decision_rationale_mismatch",
    )?;
    let blob: BlobRef = decode(field(&body, "blob")?)?;
    require(
        blob.size > 0 && blob.size <= 65536,
        "decision_rationale_mismatch",
    )?;
    contents
        .read_to(&blob, None, &mut std::io::sink())
        .map_err(|error| match error.0 {
            "content_missing" => Error("decision_rationale_missing"),
            "content_truncated" | "content_size_mismatch" | "content_digest_mismatch" => {
                Error("decision_rationale_mismatch")
            }
            _ => error,
        })
}
fn verify_vote(
    tx: &PgSession,
    authority: &WorkerAuthority,
    authorizer: &mut AuthorizationService,
    case: &Json,
    arbitrator: &str,
    vote: &Json,
    now: Timestamp,
) -> Result<()> {
    require(
        text(vote, "arbitrator")? == arbitrator && member_valid(tx, arbitrator, case)?,
        "decision_member_invalid",
    )?;
    let round =
        u32::try_from(integer(case, "round")?).map_err(|_| Error("decision_not_current"))?;
    let principal =
        authority.current_vote((text(case, "id")?, round, arbitrator), authorizer, tx, now)?;
    authorizer.require_base(&principal, "orders.dispute_vote@1", arbitrator, tx, now)?;
    let credential = tx.credential(
        principal
            .credential_id()
            .ok_or(Error("decision_signer_mismatch"))?,
    )?;
    let signature: Signature = decode(field(vote, "signature")?)?;
    require(
        signature.key_id == credential.id && credential.kind == CredentialKind::SigningKey,
        "decision_signer_mismatch",
    )?;
    verify(
        &credential.verifier.0,
        field(vote, "proposal")?.canonical()?.as_bytes(),
        &signature,
        "arbitration-decision",
    )
}

/// Only successful current-state validation can construct this settlement proof.
pub(super) struct ValidatedDecision(Json);
impl ValidatedDecision {
    pub(super) fn value(&self) -> &Json {
        &self.0
    }
}
fn validate(
    tx: &PgSession,
    contents: &GitContentStore,
    authority: &WorkerAuthority,
    authorizer: &mut AuthorizationService,
    order: &Order,
    locked: &Json,
    decision: Json,
    now: Timestamp,
) -> Result<ValidatedDecision> {
    let row = tx
        .one(
            "SELECT body,revoked FROM arbitration_decisions WHERE id=$1",
            &[text(&decision, "id")?.into()],
        )?
        .ok_or(Error("decision_revoked_or_unknown"))?;
    require(
        !row.boolean(1)? && hash(&row.json(0)?)? == hash(&decision)?,
        "decision_revoked_or_unknown",
    )?;
    let case = read_case(tx, text(&decision, "case_id")?)?;
    require(
        text(&case, "order_id")? == order.id
            && field(&case, "round")? == field(&decision, "round")?
            && text(&case, "state")? == "decided"
            && order.state == "disputed",
        "decision_not_current",
    )?;
    require(
        text(&case, "buyer")? == order.buyer
            && text(&case, "seller")? == order.seller
            && field(&case, "policy")? == field(locked, "policy")?,
        "decision_context_mismatch",
    )?;
    let proposal = field(&decision, "proposal")?;
    proposal_valid(&case, proposal, order.total_price_minor)?;
    require(
        field(&decision, "refund_minor")? == field(proposal, "refund_minor")?
            && field(&decision, "policy_digest")?
                == field(field(&case, "policy")?, "policy_digest")?,
        "decision_context_mismatch",
    )?;
    require(
        Timestamp::parse(text(&decision, "expires_at")?)? > now,
        "decision_expired",
    )?;
    require(
        Timestamp::parse(text(&decision, "not_before")?)? <= now,
        "appeal_window_open",
    )?;
    require(panel_valid(tx, &case)?, "decision_panel_invalid")?;
    require(
        field(&decision, "rationale_ref")? == field(proposal, "rationale_ref")?
            && field(&decision, "rationale_digest")? == field(proposal, "rationale_digest")?,
        "decision_rationale_mismatch",
    )?;
    verify_rationale(tx, contents, &case, proposal)?;
    let voters: Vec<_> = field(&decision, "voters")?
        .as_array()?
        .iter()
        .map(Json::as_str)
        .collect::<Result<_>>()?;
    let unique: BTreeSet<_> = voters.iter().copied().collect();
    let panel: BTreeSet<_> = panel(&case)?.into_iter().collect();
    require(
        voters.len() as i64 == integer(policy(&case)?, "quorum")?
            && unique.len() == voters.len()
            && unique.is_subset(&panel),
        "decision_quorum_invalid",
    )?;
    let mut latest = None;
    for arbitrator in voters {
        let row = tx.one("SELECT body FROM arbitration_votes WHERE case_id=$1 AND round=$2 AND arbitrator=$3",&[text(&case,"id")?.into(),integer(&case,"round")?.into(),arbitrator.into()])?.ok_or(Error("decision_quorum_invalid"))?;
        let vote = row.json(0)?;
        require(
            field(&vote, "proposal")? == proposal,
            "decision_conflicting_vote",
        )?;
        verify_vote(tx, authority, authorizer, &case, arbitrator, &vote, now)?;
        let cast = Timestamp::parse(text(&vote, "cast_at")?)?;
        latest = Some(latest.map_or(cast, |previous: Timestamp| previous.max(cast)));
    }
    let delay =
        if integer(&case, "round")? == 0 && field(policy(&case)?, "allow_appeal")?.as_bool()? {
            integer(policy(&case)?, "appeal_seconds")?
        } else {
            0
        };
    require(
        Timestamp::parse(text(&decision, "not_before")?)?
            == order::add(latest.ok_or(Error("decision_quorum_invalid"))?, delay)?
            && field(&decision, "expires_at")? == field(proposal, "expires_at")?,
        "decision_context_mismatch",
    )?;
    Ok(ValidatedDecision(decision))
}
fn notice(tx: &PgSession, case: &Json, state: &str, now: Timestamp) -> Result<()> {
    let recipients: BTreeSet<_> = [text(case, "buyer")?, text(case, "seller")?].into();
    for recipient in recipients {
        let id = new_id("message")?;
        let record = encode(
            &serde_json::json!({"id":id,"sender":recipient,"actor":recipient,"recipient":recipient,"resource":{"id":recipient,"revision":null},"order_id":field(case,"order_id")?,"case_id":field(case,"id")?,"time":now,"state":state,"source":"system"}),
        )?;
        tx.execute("INSERT INTO messages(id,sender,recipient,resource,event_id,body) VALUES($1,$2,$2,$2,$3,$4)",&[id.into(),recipient.into(),format!("case:{}",text(case,"id")?).into(),record.canonical()?.into()])?;
    }
    Ok(())
}
fn may_hold(error: Error) -> bool {
    matches!(
        error.0,
        "decision_revoked_or_unknown"
            | "decision_expired"
            | "decision_panel_invalid"
            | "decision_member_invalid"
            | "credential_revoked"
            | "credential_expired"
            | "invalid_signature"
            | "permission_denied"
            | "credential_ceiling"
            | "decision_not_current"
            | "decision_quorum_invalid"
            | "decision_signer_mismatch"
            | "decision_conflicting_vote"
            | "decision_context_mismatch"
            | "decision_rationale_missing"
            | "decision_rationale_mismatch"
    )
}
pub(super) fn resolve_cases(
    unit: &mut ContentTransaction<'_, &PgSession>,
    contents: &GitContentStore,
    signer: &Ed25519Signer,
    authority: &WorkerAuthority,
    authorizer: &mut AuthorizationService,
    now: Timestamp,
) -> Result<bool> {
    let tx = *unit.metadata();
    let rows = tx.rows("SELECT id FROM arbitration_cases WHERE state IN ('open','decided') AND deadline<=$1 ORDER BY deadline,id LIMIT 100",&[time(now).into()])?;
    let mut changed = false;
    for row in rows {
        let id = row.text(0)?;
        let case = read_case(tx, id)?;
        let mut reason = "quorum_timeout";
        if text(&case, "state")? == "decided" {
            // The narrow 'held' fallback surrounds only validation. Once a
            // ledger write starts, every error aborts the entire transaction.
            let preparation: Result<(Mutation, Option<ValidatedDecision>)> = (|| {
                let mutation = Mutation::begin(tx, contents, signer, text(&case, "order_id")?)?;
                let row = tx
                    .one(
                        "SELECT id,body FROM arbitration_decisions WHERE case_id=$1 AND round=$2",
                        &[id.into(), integer(&case, "round")?.into()],
                    )?
                    .ok_or(Error("decision_not_current"))?;
                if let Some(existing) = tx.one(
                    "SELECT body FROM order_settlements WHERE order_id=$1",
                    &[mutation.order.id.as_str().into()],
                )? {
                    require(
                        text(&existing.json(0)?, "decision_id")? == row.text(0)?,
                        "order_already_settled",
                    )?;
                    return Ok((mutation, None));
                }
                let decision = row.json(1)?;
                require(
                    text(&decision, "id")? == row.text(0)?,
                    "decision_not_current",
                )?;
                let decision = validate(
                    tx,
                    contents,
                    authority,
                    authorizer,
                    &mutation.order,
                    &mutation.locked,
                    decision,
                    now,
                )?;
                Ok((mutation, Some(decision)))
            })();
            match preparation {
                Ok((mut mutation, decision)) => {
                    let actor = text(&case, "buyer")?;
                    let request = format!("case-resolve:{id}");
                    if let Some(decision) = decision {
                        let refund = integer(decision.value(), "refund_minor")?;
                        mutation.settle(
                            tx,
                            signer,
                            Settlement {
                                now,
                                actor,
                                request: &request,
                                reason: "arbitration_decision",
                                refund,
                                decision: Some(&decision),
                            },
                        )?;
                        notice(tx, &case, "executed", now)?;
                    }
                    mutation.publish(unit, signer, now, actor, &request)?;
                    changed = true;
                    continue;
                }
                Err(error) if may_hold(error) => reason = error.0,
                Err(error) => return Err(error),
            }
        }
        tx.execute(
            "UPDATE arbitration_cases SET state='held' WHERE id=$1",
            &[id.into()],
        )?;
        tx.set_setting(
            &format!("case_hold:{id}"),
            &encode(&serde_json::json!({"reason":reason,"at":now}))?,
        )?;
        notice(tx, &case, "held", now)?;
        changed = true;
    }
    Ok(changed)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn storage_or_balance_errors_never_become_an_arbitration_rule() {
        for code in [
            "storage_io_error",
            "database_unavailable",
            "money_overflow",
            "escrow_balance_mismatch",
            "appeal_window_open",
            "order_resource_projection_mismatch",
        ] {
            assert!(!may_hold(Error(code)));
        }
        assert!(may_hold(Error("credential_revoked")));
        assert!(may_hold(Error("decision_quorum_invalid")));
    }
}
