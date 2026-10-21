//! Current key-approved browser/API bindings. No OAuth/Bearer revival or issuance.
use crate::{
    models::*,
    policy::grant_subset,
    require,
    store::{truthy, AuthorityStore},
};
use msg_core::{Error, Json, Result};
use serde::Deserialize;
use std::collections::BTreeSet;

#[derive(Clone, Deserialize)]
pub struct BrowserPolicy {
    pub enabled: bool,
}
#[derive(Clone)]
pub struct CredentialState {
    pub expires_at: Timestamp,
    pub body: Json,
}
#[derive(Deserialize)]
struct Source {
    parent: String,
    subject: String,
    auth_version: u64,
    #[serde(default)]
    custodial: bool,
    #[serde(default)]
    ceiling: Vec<Grant>,
    #[serde(default)]
    session: Option<String>,
}
fn rejected_family(id: &str) -> bool {
    id.starts_with("t_oauth_") || id.starts_with("t_browser_")
}
fn revoked(body: &Json) -> Result<bool> {
    body.as_object()?
        .get("revoked")
        .map(truthy)
        .transpose()
        .map(|v| v.unwrap_or(false))
}
fn login_binding(body: &Json) -> Result<bool> {
    body.as_object()?
        .get("login_binding")
        .map(truthy)
        .transpose()
        .map(|v| v.unwrap_or(false))
}
fn get(store: &dyn AuthorityStore, id: &str, now: Timestamp) -> Result<Json> {
    let state = store.credential_state(id)?.ok_or(Error("invalid_grant"))?;
    require(state.expires_at > now, "invalid_grant")?;
    Ok(state.body)
}
pub(crate) fn require_ceiling(
    store: &dyn AuthorityStore,
    ceiling: &[Grant],
    parent: &[Grant],
) -> Result<()> {
    for grant in ceiling {
        let mut allowed = false;
        for source in parent {
            allowed |= grant_subset(grant, source, store)?;
        }
        require(allowed, "invalid_grant")?;
    }
    Ok(())
}
pub(crate) fn require_source(
    store: &dyn AuthorityStore,
    body: &Json,
    now: Timestamp,
    custodial_ceiling: &[Grant],
    seen: &mut BTreeSet<String>,
) -> Result<()> {
    let source: Source = decode(body).map_err(|_| Error("invalid_grant"))?;
    let parent = store.credential(&source.parent)?;
    let subject = store.subject(&source.subject)?;
    require(
        parent.subject_id == subject.resource_id
            && !rejected_family(&parent.id)
            && !subject.local_only
            && subject.auth_version == source.auth_version
            && parent.current_at(now)
            && !login_binding(body)?,
        "invalid_grant",
    )?;
    if source.custodial {
        let vault = store.custodial_binding(&subject.resource_id)?;
        require(
            subject.kind == SubjectKind::Custodial
                && vault
                    .as_ref()
                    .is_some_and(|(key, status)| key == &parent.id && status == "active"),
            "invalid_grant",
        )?;
    }
    require_ceiling(
        store,
        &source.ceiling,
        if source.custodial {
            custodial_ceiling
        } else {
            &parent.ceiling
        },
    )?;
    if let Some(session) = source.session.filter(|s| !s.is_empty()) {
        // Corrupt restored cycles fail closed; they never exhaust a native stack.
        require(
            seen.len() < 32 && seen.insert(session.clone()),
            "invalid_grant",
        )?;
        let body = get(store, &session, now)?;
        require(!revoked(&body)?, "invalid_grant")?;
        require_source(store, &body, now, custodial_ceiling, seen)?;
    }
    Ok(())
}
pub fn require_binding(
    store: &dyn AuthorityStore,
    credential: &Credential,
    now: Timestamp,
    policy: Option<&BrowserPolicy>,
    custodial_ceiling: &[Grant],
) -> Result<()> {
    // Restoring historical access/family rows must not reactivate those protocols.
    require(!rejected_family(&credential.id), "invalid_grant")?;
    if let Some(browser) = store.credential_state(&format!("browser:{}", credential.id))? {
        require(
            policy.is_some_and(|p| p.enabled),
            "browser_sessions_disabled",
        )?;
        let source: Source = decode(&browser.body).map_err(|_| Error("invalid_grant"))?;
        require(
            credential.id.starts_with("t_session_")
                && credential.source_credential_id.as_deref() == Some(&source.parent)
                && credential.subject_id == source.subject
                && !source.custodial
                && !login_binding(&browser.body)?,
            "invalid_grant",
        )?;
        require_source(
            store,
            &browser.body,
            now,
            custodial_ceiling,
            &mut BTreeSet::new(),
        )?;
        return require_ceiling(store, &credential.ceiling, &source.ceiling);
    }
    if let Some(api) = store.credential_state(&format!("api:{}", credential.id))? {
        let source: Source = decode(&api.body).map_err(|_| Error("invalid_grant"))?;
        require(
            credential.source_credential_id.as_deref() == Some(&source.parent),
            "invalid_grant",
        )?;
        require_source(
            store,
            &api.body,
            now,
            custodial_ceiling,
            &mut BTreeSet::new(),
        )?;
        return require_ceiling(
            store,
            &credential.ceiling,
            &store.credential(&source.parent)?.ceiling,
        );
    }
    require(
        !credential.id.starts_with("t_session_") && credential.source_credential_id.is_none(),
        "invalid_grant",
    )
}
