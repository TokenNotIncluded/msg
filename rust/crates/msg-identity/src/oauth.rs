//! Current-state browser/API/OAuth bindings; no issuance or refresh writes.
//! The resource audience is a trusted adapter value, never a packet field.
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
pub struct OAuthClientPolicy {
    pub client_id: String,
    pub scopes: BTreeSet<String>,
}
#[derive(Clone, Deserialize)]
pub struct OAuthPolicy {
    pub enabled: bool,
    pub clients: Vec<OAuthClientPolicy>,
}
#[derive(Clone)]
pub struct OAuthState {
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
#[derive(Deserialize)]
struct Family {
    parent: String,
    subject: String,
    client_id: String,
    scopes: BTreeSet<String>,
    #[serde(default)]
    resource: Option<String>,
    ceiling: Vec<Grant>,
}

fn field<'a>(body: &'a Json, name: &str) -> Result<&'a Json> {
    body.as_object()?.get(name).ok_or(Error("invalid_grant"))
}
fn revoked(body: &Json) -> Result<bool> {
    body.as_object()?
        .get("revoked")
        .map(truthy)
        .transpose()
        .map(|v| v.unwrap_or(false))
}
fn get(store: &dyn AuthorityStore, id: &str, now: Timestamp) -> Result<Json> {
    let state = store.oauth_state(id)?.ok_or(Error("invalid_grant"))?;
    require(state.expires_at > now, "invalid_grant")?;
    Ok(state.body)
}
fn require_ceiling(store: &dyn AuthorityStore, ceiling: &[Grant], parent: &[Grant]) -> Result<()> {
    for grant in ceiling {
        let mut allowed = false;
        for source in parent {
            allowed |= grant_subset(grant, source, store)?;
        }
        require(allowed, "invalid_grant")?;
    }
    Ok(())
}
fn require_source(
    store: &dyn AuthorityStore,
    source: &Json,
    now: Timestamp,
    custodial_ceiling: &[Grant],
    seen: &mut BTreeSet<String>,
) -> Result<()> {
    let source: Source = decode(source).map_err(|_| Error("invalid_grant"))?;
    let parent = store.credential(&source.parent)?;
    let subject = store.subject(&source.subject)?;
    require(
        parent.subject_id == subject.resource_id
            && !subject.local_only
            && subject.auth_version == source.auth_version
            && parent.current_at(now),
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
        // The Python path currently recurses without a bound. Restored cyclic
        // or oversized chains fail closed rather than exhausting the stack.
        require(
            seen.len() < 32 && seen.insert(session.clone()),
            "oauth_source_cycle",
        )?;
        let body = get(store, &session, now)?;
        require(!revoked(&body)?, "invalid_grant")?;
        require_source(store, &body, now, custodial_ceiling, seen)?;
    }
    Ok(())
}
fn enabled(policy: Option<&OAuthPolicy>) -> Result<&OAuthPolicy> {
    policy.filter(|p| p.enabled).ok_or(Error("oauth_disabled"))
}

pub fn require_binding(
    store: &dyn AuthorityStore,
    credential: &Credential,
    now: Timestamp,
    policy: Option<&OAuthPolicy>,
    custodial_ceiling: &[Grant],
    resource: Option<&str>,
) -> Result<()> {
    if let Some(browser) = store.oauth_state(&format!("browser:{}", credential.id))? {
        enabled(policy)?;
        let source: Source = decode(&browser.body).map_err(|_| Error("invalid_grant"))?;
        require(
            credential.source_credential_id.as_deref() == Some(&source.parent)
                && credential.subject_id == source.subject,
            "invalid_grant",
        )?;
        require_source(
            store,
            &browser.body,
            now,
            custodial_ceiling,
            &mut BTreeSet::new(),
        )?;
        return require_ceiling(
            store,
            &credential.ceiling,
            &decode::<Vec<Grant>>(field(&browser.body, "ceiling")?)?,
        );
    }
    let api = store.oauth_state(&format!("api:{}", credential.id))?;
    if let Some(api) = &api {
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
        require_ceiling(
            store,
            &credential.ceiling,
            &store.credential(&source.parent)?.ceiling,
        )?;
    }
    let access = store.oauth_state(&format!("access:{}", credential.id))?;
    let Some(access) = access else {
        return require(
            !credential.id.starts_with("t_oauth_")
                && !credential.id.starts_with("t_browser_")
                && (credential.source_credential_id.is_none() || api.is_some()),
            "invalid_grant",
        );
    };
    let body = get(store, field(&access.body, "family")?.as_str()?, now)?;
    let family: Family = decode(&body).map_err(|_| Error("invalid_grant"))?;
    require(
        credential.source_credential_id.as_deref() == Some(&family.parent),
        "invalid_grant",
    )?;
    let client = enabled(policy)?
        .clients
        .iter()
        .find(|c| c.client_id == family.client_id)
        .ok_or(Error("invalid_client"))?;
    require(
        family.scopes.is_subset(&client.scopes) && !revoked(&body)?,
        "invalid_grant",
    )?;
    let has_mcp = family.scopes.iter().any(|s| {
        matches!(
            s.as_str(),
            "msg.mcp.read" | "msg.mcp.message" | "msg.mcp.post"
        )
    });
    if let Some(audience) = &family.resource {
        require(
            credential.subject_id == family.subject && resource == Some(audience),
            "invalid_grant",
        )?;
        require(
            has_mcp && !family.scopes.contains("msg.read") && !family.scopes.contains("msg.write"),
            "invalid_grant",
        )?;
        for grant in &credential.ceiling {
            require(
                grant
                    .operations
                    .iter()
                    .all(|op| family.scopes.iter().any(|scope| mcp_operation(scope, op))),
                "invalid_grant",
            )?;
        }
    } else {
        require(!has_mcp, "invalid_grant")?;
    }
    require_source(store, &body, now, custodial_ceiling, &mut BTreeSet::new())?;
    require_ceiling(store, &credential.ceiling, &family.ceiling)
}

fn mcp_operation(scope: &str, operation: &str) -> bool {
    match scope {
        "msg.mcp.read" => matches!(
            operation,
            "discovery.get@1"
                | "discovery.resolve@1"
                | "discovery.read_segment@1"
                | "discovery.read_query@3"
                | "discovery.lexical_search@5"
                | "communication.inbox@1"
                | "communication.dm_list@1"
                | "communication.conversation_get@1"
                | "communication.changes@1"
        ),
        "msg.mcp.message" => matches!(
            operation,
            "communication.dm_request@3"
                | "communication.dm_send@2"
                | "communication.dm_accept@2"
                | "communication.dm_reject@2"
                | "discussion.ack@1"
        ),
        "msg.mcp.post" => matches!(operation, "content.post_create@2" | "discussion.reply@2"),
        _ => false,
    }
}
