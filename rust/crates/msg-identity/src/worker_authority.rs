//! A queued snapshot is evidence of prior admission, not a fresh principal.
//! Only a live, persisted attempt may be revalidated through this store port.
use crate::{
    authentication::Principal,
    authorization::AuthorizationService,
    models::{decode, CredentialKind, Grant, Timestamp},
    policy::scope_subset,
    require,
    sessions::{require_binding, BrowserPolicy},
    store::{setting_truthy, AuthorityStore},
};
use msg_core::{Error, Json, Result};
use serde::Deserialize;
use std::collections::BTreeMap;

pub trait WorkerAuthorityStore: AuthorityStore {
    fn authority(&self) -> &dyn AuthorityStore;
    /// Return the persisted snapshot only when the same attempt is running and
    /// its lease is live. Implementations must read in this authority snapshot.
    fn worker_snapshot(&self, id: &str, attempt: i64, now: Timestamp) -> Result<Json>;
}

/// An arbitration vote is read from its immutable persisted case/round/member
/// key. This port does not accept a principal supplied by a request.
pub trait ArbitrationAuthorityStore: AuthorityStore {
    fn authority(&self) -> &dyn AuthorityStore;
    fn vote_snapshot(&self, case: &str, round: u32, arbitrator: &str) -> Result<Json>;
}

pub enum SubscriptionKey {
    Webhook {
        owner: String,
        scope: String,
        generation: i64,
    },
    Watch {
        resource: String,
        revision: String,
    },
    SavedQuery {
        resource: String,
        revision: String,
    },
}
pub trait SubscriptionAuthorityStore: AuthorityStore {
    fn authority(&self) -> &dyn AuthorityStore;
    /// Read the current persisted subscription, matching every key component.
    /// Watch/query adapters must verify the pinned source and owner binding;
    /// webhook adapters must verify owner/scope/generation and enabled state.
    fn subscription_snapshot(&self, key: &SubscriptionKey) -> Result<Json>;
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Snapshot {
    actor: String,
    subject: String,
    credential_id: String,
    method: String,
    certificates: Vec<String>,
    ceiling: Vec<Grant>,
}

pub struct WorkerAuthority {
    browser_policy: Option<BrowserPolicy>,
    custodial_ceiling: Vec<Grant>,
}
impl WorkerAuthority {
    /// Use the service's temporary (custodial) ceiling, not its primary ceiling.
    pub fn new(browser_policy: Option<BrowserPolicy>, custodial_ceiling: Vec<Grant>) -> Self {
        Self {
            browser_policy,
            custodial_ceiling,
        }
    }

    pub fn current(
        &self,
        id: &str,
        attempt: i64,
        authorizer: &mut AuthorizationService,
        store: &dyn WorkerAuthorityStore,
        now: Timestamp,
    ) -> Result<Principal> {
        let original: Snapshot = decode(&store.worker_snapshot(id, attempt, now)?)
            .map_err(|_| Error("worker_identity_required"))?;
        self.revalidate(original, authorizer, store.authority(), now)
    }

    pub fn current_vote(
        &self,
        key: (&str, u32, &str),
        authorizer: &mut AuthorizationService,
        store: &dyn ArbitrationAuthorityStore,
        now: Timestamp,
    ) -> Result<Principal> {
        let (case, round, arbitrator) = key;
        let original: Snapshot = decode(&store.vote_snapshot(case, round, arbitrator)?)
            .map_err(|_| Error("decision_member_invalid"))?;
        require(
            original.actor == arbitrator
                && original.subject == arbitrator
                && original.method == "signature",
            "decision_member_invalid",
        )?;
        self.revalidate(original, authorizer, store.authority(), now)
    }

    pub fn current_subscription(
        &self,
        key: &SubscriptionKey,
        authorizer: &mut AuthorizationService,
        store: &dyn SubscriptionAuthorityStore,
        now: Timestamp,
    ) -> Result<Principal> {
        let original: Snapshot = decode(&store.subscription_snapshot(key)?)
            .map_err(|_| Error("subscription_identity_required"))?;
        if let SubscriptionKey::Webhook { owner, .. } = key {
            require(original.subject == *owner, "subscription_identity_required")?;
        }
        self.revalidate(original, authorizer, store.authority(), now)
    }

    fn revalidate(
        &self,
        original: Snapshot,
        authorizer: &mut AuthorizationService,
        store: &dyn AuthorityStore,
        now: Timestamp,
    ) -> Result<Principal> {
        let (method, kind) = match original.method.as_str() {
            "signature" => ("signature", CredentialKind::SigningKey),
            "token" => ("token", CredentialKind::Token),
            "ssh" => ("ssh", CredentialKind::SshKey),
            _ => return Err(Error("worker_identity_required")),
        };
        require(
            original.actor != "u_root" && original.subject != "u_root",
            "local_only",
        )?;
        let credential = store.credential(&original.credential_id)?;
        require(
            credential.subject_id == original.actor,
            "credential_subject_mismatch",
        )?;
        require(credential.kind == kind, "credential_kind_mismatch")?;
        require(credential.revoked_at.is_none(), "credential_revoked")?;
        require(credential.current_at(now), "credential_expired")?;
        for subject in [&original.actor, &original.subject] {
            require(!store.subject(subject)?.local_only, "local_only")?;
            require(
                !setting_truthy(store, &format!("identity_archived:{subject}"))?,
                "account_archived",
            )?;
        }
        require_binding(
            store,
            &credential,
            now,
            self.browser_policy.as_ref(),
            &self.custodial_ceiling,
        )?;
        let mut ceiling = Vec::new();
        for captured in &original.ceiling {
            for live in &credential.ceiling {
                if captured.capability != live.capability || captured.version != live.version {
                    continue;
                }
                let operations = captured
                    .operations
                    .intersection(&live.operations)
                    .cloned()
                    .collect::<std::collections::BTreeSet<_>>();
                if operations.is_empty() {
                    continue;
                }
                let scope = if scope_subset(&captured.scope, &live.scope, store)? {
                    captured.scope.clone()
                } else if scope_subset(&live.scope, &captured.scope, store)? {
                    live.scope.clone()
                } else {
                    continue;
                };
                if let Some(constraints) =
                    intersect_constraints(&captured.constraints, &live.constraints)?
                {
                    ceiling.push(Grant {
                        capability: captured.capability.clone(),
                        version: captured.version,
                        scope,
                        operations,
                        constraints,
                    });
                }
            }
        }
        require(!ceiling.is_empty(), "credential_ceiling")?;
        let principal = Principal::from_worker(
            original.actor,
            original.subject,
            original.credential_id,
            method,
            original.certificates,
            ceiling,
        );
        // Live certificate revocation, issuer authority and source revisions are
        // checked after narrowing, in the very same database snapshot.
        authorizer.grants(&principal, store, now)?;
        Ok(principal)
    }
}

fn intersect_constraints(
    captured: &BTreeMap<String, Json>,
    live: &BTreeMap<String, Json>,
) -> Result<Option<BTreeMap<String, Json>>> {
    let mut constraints = captured.clone();
    for (name, value) in live {
        let Some(previous) = constraints.get(name) else {
            constraints.insert(name.clone(), value.clone());
            continue;
        };
        let narrowed = match name.as_str() {
            "hosts" | "methods" | "ports" | "schemes" => {
                let previous = previous.as_array()?;
                let mut values = value
                    .as_array()?
                    .iter()
                    .filter(|v| previous.contains(v))
                    .cloned()
                    .collect::<Vec<_>>();
                if values.is_empty() {
                    return Ok(None);
                }
                values.sort_by_key(|v| v.canonical().unwrap_or_default());
                values.dedup();
                Json::parse(
                    &serde_json::to_string(&values)
                        .map_err(|_| Error("invalid_identity_record"))?,
                )?
            }
            "timeout_ms" | "max_response_bytes" | "max_redirects" => {
                // Network limits have an integer schema. Never narrow through
                // a floating point conversion that could enlarge the authority.
                let a: u64 = previous
                    .as_integer()?
                    .parse()
                    .map_err(|_| Error("invalid_network_limit"))?;
                let b: u64 = value
                    .as_integer()?
                    .parse()
                    .map_err(|_| Error("invalid_network_limit"))?;
                Json::parse(&a.min(b).to_string())?
            }
            _ if previous == value => previous.clone(),
            _ => return Ok(None),
        };
        constraints.insert(name.clone(), narrowed);
    }
    Ok(Some(constraints))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn constraints(raw: &str) -> BTreeMap<String, Json> {
        Json::parse(raw).unwrap().into_object().unwrap()
    }

    #[test]
    fn queued_authority_never_grows_with_live_credential() {
        let old = constraints(
            r#"{"hosts":["a.example","b.example"],"max_response_bytes":100,"custom":"x"}"#,
        );
        let live = constraints(
            r#"{"hosts":["b.example","c.example"],"max_response_bytes":500,"methods":["GET"]}"#,
        );
        let narrowed = intersect_constraints(&old, &live).unwrap().unwrap();
        assert!(
            narrowed
                == constraints(
                    r#"{"hosts":["b.example"],"max_response_bytes":100,"custom":"x","methods":["GET"]}"#
                )
        );
        assert!(
            intersect_constraints(&old, &constraints(r#"{"hosts":["c.example"]}"#))
                .unwrap()
                .is_none()
        );
        assert!(
            intersect_constraints(&old, &constraints(r#"{"custom":"y"}"#))
                .unwrap()
                .is_none()
        );
    }
}
