//! Native market timers share the signed order and ledger boundaries.
mod arbitration;
mod checkout;
pub(crate) mod delivery;
mod email;
mod ledger;
mod legacy;
mod money;
mod offers;
mod order;
mod store;

pub use email::MailProjection;

use crate::context::{Context, HandlerOutput};

use msg_core::{digest, Error, Json, Result};
use msg_crypto::Ed25519Signer;
use msg_identity::{
    authorization::{AccessRequirement, AuthorizationService, Check},
    models::Timestamp,
    worker_authority::WorkerAuthority,
};
use msg_storage::{content::GitContentStore, records::encode};
use msg_worker::{RuntimeContext, WorkerResult};
use serde::Serialize;

fn require(condition: bool, code: &'static str) -> Result<()> {
    if condition {
        Ok(())
    } else {
        Err(Error(code))
    }
}
fn field<'a>(value: &'a Json, name: &str) -> Result<&'a Json> {
    value
        .as_object()?
        .get(name)
        .ok_or(Error("invalid_market_record"))
}
fn text<'a>(value: &'a Json, name: &str) -> Result<&'a str> {
    field(value, name)?.as_str()
}
fn integer(value: &Json, name: &str) -> Result<i64> {
    field(value, name)?
        .as_integer()?
        .parse()
        .map_err(|_| Error("invalid_market_record"))
}
fn hash(value: &impl Serialize) -> Result<String> {
    Ok(digest(encode(value)?.canonical()?.as_bytes()))
}
fn time(now: Timestamp) -> String {
    now.0.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()
}

pub fn resolve_due(
    context: &mut RuntimeContext<'_>,
    contents: &GitContentStore,
    signer: &Ed25519Signer,
) -> WorkerResult<bool> {
    let clock = context.clock();
    context.content_transaction(contents, |unit| {
        order::resolve_due(unit, contents, signer, clock()?)
    })
}
pub fn resolve_cases(
    context: &mut RuntimeContext<'_>,
    contents: &GitContentStore,
    signer: &Ed25519Signer,
    authority: &WorkerAuthority,
    authorizer: &mut AuthorizationService,
) -> WorkerResult<bool> {
    let clock = context.clock();
    context.content_transaction(contents, |unit| {
        arbitration::resolve_cases(unit, contents, signer, authority, authorizer, clock()?)
    })
}

fn signed_subject<'a>(ctx: &'a Context<'_>) -> Result<&'a str> {
    let subject = ctx.principal.subject().ok_or(Error("signature_required"))?;
    require(
        ctx.principal.actor() == Some(subject) && ctx.principal.method() == "signature",
        "signature_required",
    )?;
    Ok(subject)
}
fn viewer<'a>(ctx: &'a Context<'_>) -> Result<&'a str> {
    let subject = ctx.principal.subject().ok_or(Error("order_not_found"))?;
    require(ctx.principal.actor() == Some(subject), "order_not_found")?;
    Ok(subject)
}
pub fn supports(name: &str, version: u32) -> bool {
    money::supports(name, version)
        || offers::supports(name, version)
        || store::supports(name, version)
        || checkout::supports(name, version)
        || delivery::supports(name, version)
        || email::supports(name, version)
}
pub fn requirements(ctx: &Context<'_>) -> Result<Vec<AccessRequirement>> {
    let name = ctx.request.field("operation")?.as_str()?;
    let own_account = (name.starts_with("orders.") && name != "orders.dispute_summary")
        || name.starts_with("delivery.")
        || matches!(
            name,
            "money.balance"
                | "money.ledger"
                | "money.transfer"
                | "money.visibility_set"
                | "money.redeem"
                | "money.purchase_get"
                | "money.purchase_settle"
                | "money.purchase_cancel"
                | "money.entitlements"
        );
    Ok(if own_account {
        ctx.principal
            .subject()
            .map(|subject| {
                ctx.operation().map(|operation| AccessRequirement {
                    resource_id: subject.into(),
                    operation,
                    check: Check::Read,
                })
            })
            .transpose()?
            .into_iter()
            .collect()
    } else {
        vec![]
    })
}
pub fn handle(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let name = ctx.request.field("operation")?.as_str()?;
    let version = ctx
        .request
        .field("contract_version")?
        .as_integer()?
        .parse()
        .map_err(|_| Error("invalid_version"))?;
    if money::supports(name, version) {
        money::handle(ctx)
    } else if offers::supports(name, version) {
        offers::handle(ctx)
    } else if store::supports(name, version) {
        store::handle(ctx)
    } else if checkout::supports(name, version) {
        checkout::handle(ctx)
    } else if delivery::supports(name, version) {
        delivery::handle(ctx)
    } else if email::supports(name, version) {
        email::handle(ctx)
    } else {
        Err(Error("unknown_operation"))
    }
}
