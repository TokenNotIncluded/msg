//! A committed result is not a reusable authorization decision.
use crate::{
    authentication::{Entry, Principal},
    authorization::{AccessRequirement, AuthorizationService, AuthorizationStore, Check},
    lifecycle::{field, text},
    models::{decode, ResourceRef, Timestamp},
    require,
};
use msg_core::{Error, Json, Result};

/// The historic principal is deliberately inaccessible. Even a valid replay
/// admission cannot be passed to a mutation handler or a token-delivery claim.
pub struct ReplayAdmission {
    principal: Principal,
    result: Json,
    operation: String,
    entry: Entry,
    authorized: bool,
}

impl ReplayAdmission {
    pub(crate) fn new(
        principal: Principal,
        result: Json,
        operation: &str,
        version: u32,
        entry: Entry,
        metadata_only: bool,
    ) -> Result<Self> {
        require(
            text(&result, "operation")? == operation
                && field(&result, "actor")?.as_str().ok() == principal.actor()
                && field(&result, "subject")?.as_str().ok() == principal.subject(),
            "replay_result_mismatch",
        )?;
        let result = if metadata_only {
            require(
                matches!(
                    operation,
                    "identity.token_rotate"
                        | "identity.token_recover"
                        | "identity.custodial_upgrade_finish"
                ) && principal.actor() == principal.subject(),
                "replay_result_mismatch",
            )?;
            let references: Vec<ResourceRef> = decode(field(&result, "resources")?)?;
            require(
                references.iter().all(|reference| {
                    Some(reference.id.as_str()) == principal.subject()
                        && reference.revision.is_none()
                }),
                "replay_result_mismatch",
            )?;
            let mut body = result.into_object()?;
            let mut data = body
                .remove("data")
                .ok_or(Error("replay_result_mismatch"))?
                .into_object()?;
            // Response-only secrets are never recovered from a stored result.
            // In particular this also protects old persisted result formats.
            data.retain(|name, _| {
                matches!(
                    name.as_str(),
                    "status"
                        | "subject_id"
                        | "credential_id"
                        | "previous_credential"
                        | "expires_at"
                        | "identity_key_id"
                        | "encryption_key_id"
                        | "encryption_recipient"
                        | "certificate_id"
                        | "signature_source"
                        | "completion_scope"
                        | "phase"
                        | "history_recoverable"
                        | "verification_scope"
                        | "server_tracked_age_revisions"
                        | "vault_destroyed"
                        | "external_migration"
                        | "external_ciphertexts_verified"
                        | "retirement"
                        | "resolution"
                        | "challenge_id"
                        | "finalization_request_id"
                )
            });
            body.retain(|name, _| {
                matches!(
                    name.as_str(),
                    "request_id"
                        | "operation"
                        | "status"
                        | "actor"
                        | "subject"
                        | "resources"
                        | "committed_at"
                        | "receipt"
                        | "replayed"
                        | "error"
                        | "output"
                        | "prefer_cli"
                        | "cli_url"
                )
            });
            require(
                body.get("output").is_none_or(Json::is_null),
                "replay_result_mismatch",
            )?;
            body.insert("data".into(), Json::from_object(data));
            Json::from_object(body)
        } else {
            result
        };
        Ok(Self {
            principal,
            result,
            operation: format!("{operation}@{version}"),
            entry,
            authorized: false,
        })
    }

    /// Recheck every returned resource against this transaction's current ACLs,
    /// shares, certificate sources and ceiling. An exhausted delegation cannot
    /// bypass a later access revocation by replaying an old successful result.
    pub fn authorize_resources(
        &mut self,
        authorizer: &mut AuthorizationService,
        store: &dyn AuthorizationStore,
        now: Timestamp,
    ) -> Result<()> {
        self.authorized = false;
        let references: Vec<ResourceRef> = decode(field(&self.result, "resources")?)?;
        let checks: Vec<_> = references
            .into_iter()
            .map(|reference| AccessRequirement {
                resource_id: reference.id,
                operation: self.operation.clone(),
                check: Check::Read,
            })
            .collect();
        authorizer.require(&self.principal, self.entry, now, &checks, None, store)?;
        self.authorized = true;
        Ok(())
    }

    /// Only available after current resource authorization succeeds.
    pub fn result(&self) -> Result<&Json> {
        require(self.authorized, "replay_authorization_required")?;
        Ok(&self.result)
    }
}
