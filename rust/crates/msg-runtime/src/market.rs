//! Native market timers share the signed order and ledger boundaries.
mod arbitration;
mod ledger;
mod order;

use msg_core::{digest, Error, Json, Result};
use msg_crypto::Ed25519Signer;
use msg_identity::{
    authorization::AuthorizationService, models::Timestamp, worker_authority::WorkerAuthority,
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
