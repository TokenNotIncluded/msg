//! Native identity verification against one current authority snapshot.
//!
//! An authenticated principal is NOT authorization to execute an operation.
//! Full resource authorization and the daemon are separate cutover gates.
pub mod authentication;
pub mod certificates;
pub mod models;
pub mod oauth;
pub mod policy;
pub mod store;

use msg_core::{Error, Result};

pub(crate) fn require(condition: bool, code: &'static str) -> Result<()> {
    if condition {
        Ok(())
    } else {
        Err(Error(code))
    }
}
