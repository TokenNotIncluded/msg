//! Native identity verification against one current authority snapshot.
//!
//! An authenticated principal is NOT authorization to execute an operation.
//! Full resource authorization and the daemon are separate cutover gates.
pub mod authentication;
pub mod authorization;
pub mod browser;
pub mod certificate_lifecycle;
pub mod certificates;
pub mod custodial_lifecycle;
pub mod custodial_migration;
pub mod custodial_store;
pub mod delegation_lifecycle;
pub mod lifecycle;
pub mod models;
pub mod mutations;
pub mod policy;
mod replay;
pub mod sessions;
pub mod store;
pub mod vault;
pub mod worker_authority;

use msg_core::{Error, Result};

pub(crate) fn require(condition: bool, code: &'static str) -> Result<()> {
    if condition {
        Ok(())
    } else {
        Err(Error(code))
    }
}
