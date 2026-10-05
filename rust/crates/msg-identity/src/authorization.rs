//! Current-state resource authorization. Authentication is necessary, not sufficient.
//!
//! All facts are read through the same transaction as the operation. A request
//! cannot construct `Principal`, supply a registry, or bypass this policy with a
//! replayed result. The caller must check each replayed resource again.
use crate::{
    authentication::{Entry, Principal},
    certificates::CertificateValidator,
    models::{
        decode, CertificateKind, CredentialKind, DelegationFact, Grant, ResourceAuthority,
        ResourceRef, Timestamp,
    },
    policy::{allows, grant_covers, parse_mode, scope_contains, CERTGATE},
    require,
    store::{setting_truthy, truthy, AuthorityStore, Record, RuntimeGeneration},
};
use msg_core::{Error, Json, Result};
use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet};

const ROOT: &str = "u_root";
const TOOLS: &str = "t_tools";

/// Policy metadata is read from stored resource rows, never a caller's arguments.
#[derive(Clone, Deserialize)]
pub struct PolicyResource {
    #[serde(flatten)]
    pub authority: ResourceAuthority,
    pub name: String,
    pub type_version: u32,
}
impl std::ops::Deref for PolicyResource {
    type Target = ResourceAuthority;
    fn deref(&self) -> &Self::Target {
        &self.authority
    }
}

#[derive(Clone)]
pub struct DirectConversation {
    pub resource_id: String,
    pub participant_a: String,
    pub participant_b: String,
    pub state: String,
}
#[derive(Clone)]
pub struct LegacyShare {
    pub grantor: String,
    pub expires_at: String,
    pub created_at: String,
}
#[derive(Clone)]
pub struct ShareSource {
    pub resource_id: String,
    pub grantor: String,
    pub grantee: String,
    pub grantee_kind: String,
    pub parent_id: Option<String>,
    pub operations: String,
    pub constraints: String,
    pub allow_reshare: bool,
    pub expires_at: String,
    pub revoked_at: Option<String>,
    pub created_at: String,
}

/// The complete read boundary used by resource policy. Implementations must not
/// silently turn storage errors into absent facts or ignore unreadable rows.
pub trait AuthorizationStore: AuthorityStore {
    fn authority(&self) -> &dyn AuthorityStore;
    fn memberships(&self, subject: &str) -> Result<BTreeSet<String>>;
    fn organization_exists(&self, id: &str) -> Result<bool>;
    fn topic_admin(&self, topic: &str, subject: &str) -> Result<bool>;
    fn topic_ban(&self, topic: &str, subject: &str) -> Result<Option<Option<String>>>;
    fn direct_conversation(&self, resource: &str) -> Result<Option<DirectConversation>>;
    fn direct_blocked(&self, a: &str, b: &str) -> Result<bool>;
    fn legacy_share(&self, resource: &str, grantee: &str) -> Result<Option<LegacyShare>>;
    fn share_candidates(&self, resource: &str, now: &str) -> Result<Vec<String>>;
    fn share_source(&self, id: &str) -> Result<Option<ShareSource>>;
    fn csr_issuer(&self, id: &str) -> Result<String>;
    fn policy_resource(&self, id: &str) -> Result<PolicyResource> {
        let resource: PolicyResource = decode(&self.record(Record::Resource, id)?)?;
        require(resource.id == id, "storage_record_mismatch")?;
        Ok(resource)
    }
}

/// Constructed by trusted handlers. This is not a deserializable network proof.
pub struct AccessRequirement {
    pub resource_id: String,
    pub operation: String,
    pub check: Check,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Check {
    Read,
    List,
    Traverse,
    Write,
    Create,
    Remove,
    Chmod,
    Chgrp,
    Chown,
    Manage,
    Certgate,
    ToolUse,
    Purge,
}
impl Check {
    pub fn parse(value: &str) -> Result<Self> {
        Ok(match value {
            "read" => Self::Read,
            "list" => Self::List,
            "traverse" => Self::Traverse,
            "write" => Self::Write,
            "create" => Self::Create,
            "remove" => Self::Remove,
            "chmod" => Self::Chmod,
            "chgrp" => Self::Chgrp,
            "chown" => Self::Chown,
            "manage" => Self::Manage,
            "certgate" => Self::Certgate,
            "tool_use" => Self::ToolUse,
            "purge" => Self::Purge,
            _ => return Err(Error("invalid_permission")),
        })
    }
    fn write(self) -> bool {
        !matches!(self, Self::Read | Self::List | Self::Traverse)
    }
    fn override_capability(self) -> Option<&'static str> {
        match self {
            Self::Read | Self::List | Self::Traverse => Some("resource.read_override"),
            Self::Write => Some("resource.write_override"),
            Self::Chmod => Some("resource.chmod_override"),
            Self::Chgrp => Some("resource.chgrp_override"),
            Self::Chown => Some("resource.chown"),
            Self::Purge => Some("resource.purge"),
            Self::ToolUse => Some("tool.use"),
            _ => None,
        }
    }
}

/// Only the container flag is needed; type/version lookup is still mandatory.
#[derive(Clone, Deserialize)]
pub struct ResourceTypePolicy {
    pub name: String,
    pub version: u32,
    pub container: bool,
}

pub struct AuthorizationService {
    certificates: CertificateValidator,
    runtime: RuntimeGeneration,
    resource_types: BTreeMap<(String, u32), bool>,
    base_families: BTreeSet<String>,
}
impl AuthorizationService {
    pub fn new(
        certificates: CertificateValidator,
        store: &dyn AuthorityStore,
        resource_types: Vec<ResourceTypePolicy>,
        base_families: BTreeSet<String>,
    ) -> Result<Self> {
        let mut types = BTreeMap::new();
        for spec in resource_types {
            require(
                types
                    .insert((spec.name, spec.version), spec.container)
                    .is_none(),
                "duplicate_resource_type",
            )?;
        }
        Ok(Self {
            certificates,
            runtime: RuntimeGeneration::capture(store)?,
            resource_types: types,
            base_families,
        })
    }
    fn live(&mut self, store: &dyn AuthorityStore) -> Result<()> {
        self.runtime.require_current(store)?;
        require(
            !store.has_setting("recovery_quarantine")?,
            "recovery_quarantined",
        )
    }
    pub fn grants(
        &mut self,
        principal: &Principal,
        store: &dyn AuthorityStore,
        now: Timestamp,
    ) -> Result<Vec<Grant>> {
        self.live(store)?;
        let mut grants = Vec::new();
        for id in principal.certificates() {
            let certificate = self.certificates.validate(id, store, now)?;
            require(
                Some(certificate.key_id.as_str()) == principal.credential_id()
                    && Some(certificate.subject_id.as_str()) == principal.actor(),
                "certificate_subject_mismatch",
            )?;
            grants.extend(certificate.grants);
        }
        Ok(grants)
    }
    pub fn has(
        &mut self,
        principal: &Principal,
        capability: &str,
        operation: &str,
        resource: &str,
        store: &dyn AuthorityStore,
        now: Timestamp,
    ) -> Result<bool> {
        self.live(store)?;
        if principal.method() == "local" && principal.subject() == Some(ROOT) {
            return Ok(true);
        }
        let mut ceiling = false;
        for grant in principal.ceiling() {
            ceiling |= grant_covers(grant, capability, operation, resource, store)?;
        }
        if !ceiling {
            return Ok(false);
        }
        for grant in self.grants(principal, store, now)? {
            if grant_covers(&grant, capability, operation, resource, store)? {
                return Ok(true);
            }
        }
        Ok(false)
    }
    pub fn ceiling(
        &mut self,
        principal: &Principal,
        operation: &str,
        resource: &str,
        store: &dyn AuthorityStore,
        now: Timestamp,
    ) -> Result<()> {
        self.live(store)?;
        if principal.method() == "anonymous" {
            return Ok(());
        }
        let mut allowed = false;
        for grant in principal.ceiling() {
            allowed |= grant.operations.contains(operation)
                && scope_contains(&grant.scope, resource, store)?;
        }
        if !allowed
            && matches!(
                operation,
                "discovery.get@1"
                    | "discovery.raw@1"
                    | "job.get@1"
                    | "transfer.open@1"
                    | "transfer.part_get@1"
                    | "transfer.status@1"
                    | "transfer.seal@1"
                    | "transfer.cancel@1"
            )
        {
            allowed = self.tool_output(principal, operation, resource, store, now)?;
        }
        require(allowed, "credential_ceiling")?;
        if principal.actor() != principal.subject() {
            let mut delegated = false;
            for id in principal.certificates() {
                let certificate = self.certificates.validate(id, store, now)?;
                if certificate.kind != CertificateKind::Delegation {
                    continue;
                }
                // validate() rechecks every authority source under this exact snapshot.
                for source in &certificate.authority_sources {
                    let fact: DelegationFact = decode(
                        &store
                            .setting(&format!("delegation:{}", source.id))?
                            .ok_or(Error("authority_source_invalid"))?,
                    )?;
                    if Some(fact.grantor.as_str()) == principal.subject() {
                        for grant in &certificate.grants {
                            delegated |= grant.operations.contains(operation)
                                && scope_contains(&grant.scope, resource, store)?;
                        }
                    }
                }
            }
            require(delegated, "delegation_scope")?;
        }
        Ok(())
    }
    fn tool_output(
        &mut self,
        principal: &Principal,
        operation: &str,
        resource: &str,
        store: &dyn AuthorityStore,
        now: Timestamp,
    ) -> Result<bool> {
        if let Some(output) = store.setting(&format!("tool_output:{resource}"))? {
            if truthy(&output)? {
                let object = output.as_object()?;
                if object.get("subject").and_then(|v| v.as_str().ok()) == principal.subject() {
                    return self.has(
                        principal,
                        "tool.use",
                        operation,
                        object
                            .get("tool_id")
                            .ok_or(Error("invalid_storage_record"))?
                            .as_str()?,
                        store,
                        now,
                    );
                }
            }
        }
        Ok(false)
    }
    pub fn ordinary(
        &self,
        principal: &Principal,
        operation: &str,
        resource: &str,
        store: &dyn AuthorityStore,
    ) -> Result<bool> {
        if matches!(principal.method(), "anonymous" | "local") {
            return Ok(true);
        }
        for grant in principal.ceiling() {
            if self.base_families.contains(&grant.capability)
                && grant.operations.contains(operation)
                && scope_contains(&grant.scope, resource, store)?
            {
                return Ok(true);
            }
        }
        Ok(false)
    }
    pub fn require_base(
        &mut self,
        principal: &Principal,
        operation: &str,
        resource: &str,
        store: &dyn AuthorityStore,
        now: Timestamp,
    ) -> Result<()> {
        self.ceiling(principal, operation, resource, store, now)?;
        require(
            self.ordinary(principal, operation, resource, store)?,
            "credential_ceiling",
        )
    }
    fn chain(
        &self,
        resource: &PolicyResource,
        store: &dyn AuthorizationStore,
    ) -> Result<Vec<PolicyResource>> {
        let mut chain = Vec::new();
        for item in store.ancestors(&resource.id)? {
            chain.push(store.policy_resource(&item.id)?);
        }
        chain.push(resource.clone());
        Ok(chain)
    }
    pub fn share_target_error(
        &self,
        resource: &PolicyResource,
        chain: &[PolicyResource],
        store: &dyn AuthorizationStore,
    ) -> Result<Option<&'static str>> {
        if resource.state != "active" || chain.iter().any(|r| r.state != "active") {
            return Ok(Some("share_forbidden_resource"));
        }
        if *self
            .resource_types
            .get(&(resource.kind.clone(), resource.type_version))
            .ok_or(Error("unknown_resource_type"))?
        {
            return Ok(Some("share_container_forbidden"));
        }
        if chain.iter().any(|r| {
            matches!(r.id.as_str(), "r_agents" | "r_rules" | "t_last_will")
                || matches!(
                    r.kind.as_str(),
                    "tool"
                        | "csr"
                        | "certificate"
                        | "credential"
                        | "legacy_directive"
                        | "dm_conversation"
                )
        }) || chain.windows(2).any(|p| {
            p[0].kind == "user" && matches!(p[1].name.as_str(), "SOUL.md" | "AGENTS.md" | "todos")
        }) {
            return Ok(Some("share_forbidden_resource"));
        }
        for item in chain {
            if setting_truthy(store.authority(), &format!("hosting_preview:{}", item.id))?
                || setting_truthy(
                    store.authority(),
                    &format!("hosting_preview_file:{}", item.id),
                )?
                || store.direct_conversation(&item.id)?.is_some()
            {
                return Ok(Some("share_forbidden_resource"));
            }
        }
        Ok(None)
    }
    pub fn share_source_active(
        &self,
        resource: &PolicyResource,
        grant_id: &str,
        subject: &str,
        now: Timestamp,
        store: &dyn AuthorizationStore,
        mut reshare: bool,
    ) -> Result<bool> {
        let chain = self.chain(resource, store)?;
        if self.share_target_error(resource, &chain, store)?.is_some() {
            return Ok(false);
        }
        let (mut id, mut subject) = (grant_id.to_owned(), subject.to_owned());
        let mut seen = BTreeSet::new();
        let mut child_expiry = None;
        loop {
            if seen.len() >= 16 || !seen.insert(id.clone()) {
                return Ok(false);
            }
            let Some(row) = store.share_source(&id)? else {
                return Ok(false);
            };
            if row.resource_id != resource.id || row.revoked_at.is_some() {
                return Ok(false);
            }
            let (Ok(expires), Ok(created), Ok(operations), Ok(constraints)) = (
                Timestamp::parse(&row.expires_at),
                Timestamp::parse(&row.created_at),
                Json::parse(&row.operations),
                Json::parse(&row.constraints),
            ) else {
                return Ok(false);
            };
            let supported = operations
                .as_array()
                .is_ok_and(|a| a.len() == 1 && a[0].as_str().ok() == Some("read"))
                && constraints.as_object().is_ok_and(|a| a.is_empty());
            if !supported
                || created > now
                || expires <= now
                || expires <= created
                || child_expiry.is_some_and(|child| child > expires)
                || (reshare && !row.allow_reshare)
            {
                return Ok(false);
            }
            match row.grantee_kind.as_str() {
                "user" if row.grantee == subject => (),
                "group" => {
                    let group = match store.policy_resource(&row.grantee) {
                        Ok(group) => group,
                        Err(Error("not_found")) => return Ok(false),
                        Err(error) => return Err(error),
                    };
                    if group.state != "active"
                        || !store.organization_exists(&row.grantee)?
                        || !store.memberships(&subject)?.contains(&row.grantee)
                    {
                        return Ok(false);
                    }
                }
                _ => return Ok(false),
            }
            let Some(parent) = row.parent_id else {
                return Ok(row.grantor == resource.owner);
            };
            subject = row.grantor;
            id = parent;
            child_expiry = Some(expires);
            reshare = true;
        }
    }
    pub fn shared_read(
        &self,
        principal: &Principal,
        resource: &PolicyResource,
        chain: &[PolicyResource],
        now: Timestamp,
        store: &dyn AuthorizationStore,
    ) -> Result<bool> {
        let Some(subject) = principal.subject() else {
            return Ok(false);
        };
        if principal.actor() != Some(subject)
            || self.share_target_error(resource, chain, store)?.is_some()
        {
            return Ok(false);
        }
        if let Some(row) = store.legacy_share(&resource.id, subject)? {
            if row.grantor == resource.owner && resource.state == "active" {
                if let (Ok(expires), Ok(created)) = (
                    Timestamp::parse(&row.expires_at),
                    Timestamp::parse(&row.created_at),
                ) {
                    if created <= now && now < expires {
                        return Ok(true);
                    }
                }
            }
        }
        if resource.state != "active" {
            return Ok(false);
        }
        let time = now.0.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string();
        for candidate in store.share_candidates(&resource.id, &time)? {
            if self.share_source_active(resource, &candidate, subject, now, store, false)? {
                return Ok(true);
            }
        }
        Ok(false)
    }
    pub fn share_link_read_allowed(
        &mut self,
        grantor: &str,
        credential_id: &str,
        resource: &PolicyResource,
        chain: &[PolicyResource],
        now: Timestamp,
        store: &dyn AuthorizationStore,
    ) -> Result<bool> {
        self.runtime.require_current(store.authority())?;
        if store.has_setting("recovery_quarantine")?
            || resource.owner != grantor
            || self.share_target_error(resource, chain, store)?.is_some()
        {
            return Ok(false);
        }
        let memberships = store.memberships(grantor)?;
        if !allows(resource, Some(grantor), &memberships, "read")? {
            return Ok(false);
        }
        for ancestor in chain.iter().take(chain.len().saturating_sub(1)) {
            if !allows(ancestor, Some(grantor), &memberships, "traverse")? {
                return Ok(false);
            }
        }
        let credential = match store.credential(credential_id) {
            Ok(credential) => credential,
            Err(Error("credential_not_found")) => return Ok(false),
            Err(error) => return Err(error),
        };
        if credential.subject_id != grantor
            || credential.kind != CredentialKind::SigningKey
            || !credential.current_at(now)
        {
            return Ok(false);
        }
        for grant in &credential.ceiling {
            if grant.operations.contains("sharing.link_create@1")
                && scope_contains(&grant.scope, &resource.id, store.authority())?
            {
                return Ok(true);
            }
        }
        Ok(false)
    }
    /// Every check runs in order. `request_name` is only the literal `name`
    /// argument needed for topic presentation policy; it grants no identity.
    pub fn require(
        &mut self,
        principal: &Principal,
        entry: Entry,
        now: Timestamp,
        checks: &[AccessRequirement],
        request_name: Option<&str>,
        store: &dyn AuthorizationStore,
    ) -> Result<Vec<ResourceRef>> {
        self.live(store.authority())?;
        if principal.subject() == Some(ROOT) || principal.actor() == Some(ROOT) {
            require(
                matches!(entry, Entry::LocalAdmin) && principal.method() == "local",
                "local_only",
            )?;
        }
        let memberships = principal
            .subject()
            .map(|s| store.memberships(s))
            .transpose()?
            .unwrap_or_default();
        for check in checks {
            let resource = store.policy_resource(&check.resource_id)?;
            let operation = check.operation.as_str();
            require(resource.kind != "watch", "permission_denied")?;
            if resource.kind == "saved_query" {
                require(
                    matches!(operation, "query.save@1" | "query.saved_archive@1")
                        && principal.subject() == principal.actor()
                        && principal.subject() == Some(&resource.owner),
                    "permission_denied",
                )?;
            }
            self.ceiling(principal, operation, &resource.id, store.authority(), now)?;
            let chain = self.chain(&resource, store)?;
            let mut presentation = None;
            if resource.kind == "file"
                && matches!(resource.name.as_str(), "ABOUT.md" | "HEADER.svg")
            {
                if let Some(parent) = &resource.parent {
                    let parent = store.policy_resource(parent)?;
                    if parent.kind == "topic" {
                        presentation = Some(parent);
                    }
                }
            }
            if resource.kind == "topic"
                && check.check == Check::Create
                && matches!(request_name, Some("ABOUT.md" | "HEADER.svg"))
            {
                presentation = Some(resource.clone());
            }
            let presentation_edit = presentation.is_some() && check.check.write();
            if presentation_edit {
                let topic = presentation
                    .as_ref()
                    .ok_or(Error("invalid_storage_record"))?;
                let mut can_edit = false;
                if let Some(subject) = principal.subject() {
                    can_edit = subject == topic.owner
                        || subject == ROOT
                        || store.topic_admin(&topic.id, subject)?;
                }
                require(can_edit, "topic_admin_required")?;
            }
            if check.check.write() && principal.subject() != Some(ROOT) {
                protect_wiki(&chain, operation)?;
            }
            'preview: for item in &chain {
                for prefix in ["hosting_preview:", "hosting_preview_file:"] {
                    if let Some(marker) = store.setting(&format!("{prefix}{}", item.id))? {
                        if truthy(&marker)? {
                            let owner = marker
                                .as_object()?
                                .get("owner")
                                .ok_or(Error("invalid_storage_record"))?
                                .as_str()?;
                            require(
                                principal.subject() == Some(owner)
                                    || (principal.subject() == Some(ROOT)
                                        && matches!(entry, Entry::LocalAdmin)
                                        && principal.method() == "local"),
                                "permission_denied",
                            )?;
                            break 'preview;
                        }
                    }
                }
            }
            if check.check.write() {
                require(
                    !chain
                        .iter()
                        .any(|r| matches!(r.id.as_str(), "r_agents" | "r_rules")),
                    "system_managed_resource",
                )?;
                if !presentation_edit && chain.iter().any(|r| r.id == "t_last_will") {
                    require(
                        matches!(
                            operation,
                            "identity.legacy_put@1"
                                | "identity.legacy_put@2"
                                | "identity.legacy_archive@1"
                        ) && principal.subject() == Some(&resource.owner),
                        "legacy_directive_only",
                    )?;
                }
                let managed: Vec<_> = chain
                    .windows(2)
                    .filter(|p| p[0].kind == "user")
                    .map(|p| p[1].name.as_str())
                    .collect();
                if managed
                    .iter()
                    .any(|name| matches!(*name, "SOUL.md" | "AGENTS.md" | "notes" | "todos"))
                {
                    let sharing_notes = matches!(
                        operation,
                        "sharing.grant@1"
                            | "sharing.grant@2"
                            | "sharing.revoke@1"
                            | "sharing.revoke@2"
                            | "sharing.link_create@1"
                            | "sharing.link_revoke@1"
                    ) && managed
                        .iter()
                        .all(|name| !matches!(*name, "SOUL.md" | "AGENTS.md" | "todos"));
                    require(
                        (sharing_notes
                            || matches!(
                                operation,
                                "identity.personal_put@1"
                                    | "identity.note_put@1"
                                    | "identity.personal_put@2"
                                    | "identity.note_put@2"
                                    | "identity.soul_visibility@1"
                                    | "identity.note_archive@1"
                                    | "identity.note_restore@1"
                                    | "identity.todo_put@1"
                                    | "identity.todo_put@2"
                                    | "identity.todo_archive@1"
                                    | "identity.todo_restore@1"
                            ))
                            && principal.subject() == Some(&resource.owner),
                        "personal_managed_resource",
                    )?;
                }
                if let Some(subject) = principal.subject() {
                    for ancestor in chain.iter().rev().filter(|r| r.kind == "topic") {
                        if let Some(expiry) = store.topic_ban(&ancestor.id, subject)? {
                            require(
                                expiry
                                    .as_ref()
                                    .map(|t| Timestamp::parse(t))
                                    .transpose()?
                                    .is_some_and(|time| time <= now),
                                "topic_banned",
                            )?;
                        }
                    }
                }
            }
            let mut direct = None;
            for ancestor in &chain {
                direct = store.direct_conversation(&ancestor.id)?;
                if direct.is_some() {
                    break;
                }
            }
            if let Some(direct) = direct {
                require(
                    principal
                        .subject()
                        .is_some_and(|s| s == direct.participant_a || s == direct.participant_b),
                    "permission_denied",
                )?;
                if matches!(check.check, Check::Read | Check::List | Check::Traverse) {
                    continue;
                }
                if check.check == Check::Create
                    && resource.id == direct.resource_id
                    && matches!(
                        operation,
                        "communication.dm_send@1" | "communication.dm_send@2"
                    )
                    && direct.state == "active"
                {
                    require(
                        !store.direct_blocked(&direct.participant_a, &direct.participant_b)?,
                        "dm_blocked",
                    )?;
                    continue;
                }
                if check.check == Check::Write
                    && resource.kind == "post"
                    && principal.subject() == Some(&resource.owner)
                    && operation == "content.post_edit@1"
                {
                    require(
                        direct.state == "active"
                            && !store
                                .direct_blocked(&direct.participant_a, &direct.participant_b)?,
                        "dm_blocked",
                    )?;
                    continue;
                }
                return Err(Error("dm_controlled_resource"));
            }
            let tool_access = resource.kind == "tool"
                && self.has(
                    principal,
                    "tool.use",
                    operation,
                    &resource.id,
                    store.authority(),
                    now,
                )?;
            if resource.kind == "tool" {
                require(tool_access, "tool_certificate_required")?;
            }
            let shared_read = check.check == Check::Read
                && self.shared_read(principal, &resource, &chain, now, store)?;
            for ancestor in chain.iter().take(chain.len().saturating_sub(1)) {
                let mut readable = allows(ancestor, principal.subject(), &memberships, "traverse")?;
                let minimal_tool = tool_access && ancestor.id == TOOLS;
                if !(readable || minimal_tool || shared_read) {
                    readable = self.has(
                        principal,
                        "resource.read_override",
                        operation,
                        &ancestor.id,
                        store.authority(),
                        now,
                    )?;
                }
                require(readable || minimal_tool || shared_read, "permission_denied")?;
                require(ancestor.state == "active", "ancestor_inactive")?;
            }
            if check.check.write() {
                require(principal.subject().is_some(), "authentication_required")?;
                for ancestor in &chain {
                    if parse_mode(&ancestor.mode)? & CERTGATE != 0 {
                        require(
                            self.has(
                                principal,
                                "resource.certified_write",
                                operation,
                                &resource.id,
                                store.authority(),
                                now,
                            )?,
                            "certificate_gate",
                        )?;
                    }
                }
            }
            if resource.kind == "tool" {
                require(
                    matches!(check.check, Check::Read | Check::ToolUse),
                    "tool_read_only",
                )?;
                continue;
            }
            if resource.kind == "csr"
                && check.check == Check::Read
                && principal.subject() != Some(&resource.owner)
            {
                require(
                    principal.subject() == Some(store.csr_issuer(&resource.id)?.as_str())
                        && self.has(
                            principal,
                            "cert.issue",
                            operation,
                            &resource.id,
                            store.authority(),
                            now,
                        )?,
                    "permission_denied",
                )?;
                continue;
            }
            let mut allowed = match check.check {
                Check::Certgate => self.has(
                    principal,
                    "resource.certified_write",
                    operation,
                    &resource.id,
                    store.authority(),
                    now,
                )?,
                Check::Chmod | Check::Chgrp | Check::Manage => {
                    principal.subject() == Some(&resource.owner)
                }
                Check::Create | Check::Remove => {
                    allows(&resource, principal.subject(), &memberships, "write")?
                        && allows(&resource, principal.subject(), &memberships, "traverse")?
                }
                Check::Chown | Check::Purge | Check::ToolUse => false,
                Check::Read => {
                    allows(&resource, principal.subject(), &memberships, "read")? || shared_read
                }
                Check::List => allows(&resource, principal.subject(), &memberships, "list")?,
                Check::Traverse => {
                    allows(&resource, principal.subject(), &memberships, "traverse")?
                }
                Check::Write => allows(&resource, principal.subject(), &memberships, "write")?,
            };
            let mut ordinary =
                self.ordinary(principal, operation, &resource.id, store.authority())?;
            if !ordinary && check.check == Check::Read {
                ordinary =
                    self.tool_output(principal, operation, &resource.id, store.authority(), now)?;
            }
            if presentation_edit && matches!(check.check, Check::Write | Check::Create) {
                allowed = true;
            }
            allowed &= ordinary;
            if !allowed {
                if let Some(capability) = check.check.override_capability() {
                    allowed = self.has(
                        principal,
                        capability,
                        operation,
                        &resource.id,
                        store.authority(),
                        now,
                    )?;
                }
            }
            require(allowed, "permission_denied")?;
        }
        Ok(principal
            .certificates()
            .iter()
            .map(|id| ResourceRef {
                id: id.clone(),
                revision: None,
            })
            .collect())
    }
}
fn protect_wiki(chain: &[PolicyResource], operation: &str) -> Result<()> {
    if !chain.iter().any(|r| r.id == "t_wiki") {
        return Ok(());
    }
    let name = operation.split('@').next().unwrap_or(operation);
    require(
        !matches!(
            name,
            "content.move"
                | "content.archive"
                | "content.restore"
                | "content.purge"
                | "content.chmod"
                | "content.chgrp"
                | "content.chown"
                | "content.topic_configure"
                | "content.topic_policy_set"
                | "file.delete"
                | "content.topic_remove"
                | "content.topic_promote"
                | "content.topic_demote"
                | "content.topic_ban"
                | "content.topic_unban"
                | "content.topic_invite"
                | "content.topic_approve"
        ) && !name.starts_with("content.topic_member_"),
        "wiki_shared_resource",
    )
}
