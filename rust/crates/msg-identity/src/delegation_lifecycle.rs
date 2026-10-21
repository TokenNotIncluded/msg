//! Client-held delegated identities and delegation resources in one transaction.
use crate::{
    authentication::Principal,
    authorization::AuthorizationService,
    lifecycle::{
        self, after, field, new_id, optional, output, resource, signature, text, Issuance,
    },
    models::*,
    mutations::{EncryptionKey, IdentityKey, IdentityStore},
    policy::grant_subset,
    require,
    store::{setting_truthy, Record},
};
use msg_core::{digest, unb64, Error, Json, Result};
use msg_crypto::{age_recipient, key_id, subject_id, verify};

pub enum Outcome {
    Complete(Json),
    Create(Box<Prepared>),
}
struct NewIdentity {
    address: String,
    grantor_address: String,
    encryption_key: String,
    encryption_recipient: String,
    max_uses: Option<u64>,
}
/// A verified grant, bound to its original request, principal and content bytes.
/// Runtime cannot construct or deserialize this authority-bearing value.
pub struct Prepared {
    fact: Json,
    body: Vec<u8>,
    name: String,
    actor: String,
    grantor: String,
    credential: String,
    auth_version: u64,
    request_id: String,
    payload_digest: String,
    grantee: String,
    key: String,
    grants: Vec<Grant>,
    depth: u32,
    expires: Timestamp,
    ttl: Option<u64>,
    new_identity: Option<NewIdentity>,
    now: Timestamp,
}
impl Prepared {
    pub fn parent(&self) -> &str {
        &self.grantor
    }
    pub fn name(&self) -> &str {
        &self.name
    }
    pub fn body(&self) -> &[u8] {
        &self.body
    }
}
pub fn supports(operation: &str, version: u32) -> bool {
    version == 1
        && matches!(
            operation,
            "identity.delegate"
                | "identity.delegation_revoke"
                | "identity.delegated_create"
                | "identity.delegated_get"
        )
}
fn covers<S: IdentityStore>(store: &S, parent: &[Grant], child: &[Grant]) -> Result<bool> {
    for requested in child {
        let mut covered = false;
        for allowed in parent {
            covered |= grant_subset(requested, allowed, store)?;
        }
        if !covered {
            return Ok(false);
        }
    }
    Ok(true)
}

pub fn handle<S: IdentityStore>(
    service: &lifecycle::Service<'_>,
    store: &mut S,
    authorizer: &mut AuthorizationService,
    principal: &Principal,
    request: &Json,
    now: Timestamp,
) -> Result<Outcome> {
    store.require_live()?;
    signature(principal)?;
    let operation = text(request, "operation")?;
    let version: u32 = decode(field(request, "contract_version")?)?;
    require(supports(operation, version), "operation_not_found")?;
    let subject = principal
        .subject()
        .ok_or(Error("authentication_required"))?;
    require(
        subject != "u_root" && !store.subject(subject)?.local_only,
        "local_only",
    )?;
    let args = field(request, "arguments")?;
    match operation {
        "identity.delegation_revoke" => {
            let id = store.resolve_identity(text(args, "id")?)?;
            let mut object = store.record(Record::Resource, &id)?;
            require(
                text(&object, "type")? == "delegation" && text(&object, "owner")? == subject,
                "delegation_owner_required",
            )?;
            authorizer.ceiling(
                principal,
                &format!("{operation}@{version}"),
                &id,
                store,
                now,
            )?;
            let generation: i64 = decode(field(&object, "generation")?)?;
            crate::custodial_migration::set(&mut object, "state", Json::string("archived"))?;
            crate::custodial_migration::set(
                &mut object,
                "generation",
                wire(
                    &generation
                        .checked_add(1)
                        .ok_or(Error("invalid_generation"))?,
                )?,
            )?;
            crate::custodial_migration::set(&mut object, "modified_at", wire(&now)?)?;
            crate::custodial_migration::set(&mut object, "modified_by", wire(&principal.actor())?)?;
            store.replace_identity_resource(&object, generation)?;
            Ok(Outcome::Complete(output(
                serde_json::json!({"revoked":true}),
                vec![ResourceRef { id, revision: None }],
            )?))
        }
        "identity.delegated_get" => {
            let id = store.resolve_identity(text(args, "id")?)?;
            let binding = store
                .setting(&format!("delegated_identity:{id}"))?
                .ok_or(Error("not_delegated_identity"))?;
            require(
                principal.actor() == Some(subject) && text(&binding, "grantor")? == subject,
                "identity_owner_required",
            )?;
            authorizer.require_base(
                principal,
                &format!("{operation}@{version}"),
                subject,
                store,
                now,
            )?;
            let delegated = store.resource(text(&binding, "delegation_id")?)?;
            let mut active = delegated.state == "active"
                && now < decode::<Timestamp>(field(&binding, "expires_at")?)?;
            if let Some(max) = optional(&binding, "max_uses")?.filter(|v| !v.is_null()) {
                active &= decode::<u64>(field(&binding, "uses")?)? < decode::<u64>(max)?;
            }
            if active {
                match service
                    .validator
                    .validate(text(&binding, "certificate_id")?, store, now)
                {
                    Ok(_) => (),
                    Err(Error(
                        "authority_source_inactive"
                        | "authority_source_expired"
                        | "certificate_revoked"
                        | "certificate_expired"
                        | "authority_source_lost",
                    )) => active = false,
                    Err(error) => return Err(error),
                }
            }
            let mut data = binding;
            crate::custodial_migration::set(&mut data, "subject_id", Json::string(id))?;
            crate::custodial_migration::set(&mut data, "active", wire(&active)?)?;
            Ok(Outcome::Complete(wire(
                &serde_json::json!({"resources":[],"data":data}),
            )?))
        }
        _ => prepare(service, store, authorizer, principal, request, now)
            .map(|plan| Outcome::Create(Box::new(plan))),
    }
}

fn prepare<S: IdentityStore>(
    service: &lifecycle::Service<'_>,
    store: &mut S,
    authorizer: &mut AuthorizationService,
    principal: &Principal,
    request: &Json,
    now: Timestamp,
) -> Result<Prepared> {
    let operation = text(request, "operation")?;
    let args = field(request, "arguments")?;
    let created = operation == "identity.delegated_create";
    let grantor = principal
        .subject()
        .ok_or(Error("authentication_required"))?;
    let actor = principal.actor().ok_or(Error("authentication_required"))?;
    let owner = store.record(Record::Resource, grantor)?;
    if created {
        require(
            [grantor, text(&owner, "name")?].contains(&text(args, "grantor")?),
            "delegated_grantor_mismatch",
        )?;
    }
    let issuer = service.issuer(store, now)?;
    let (grantee, key, new_keys) = if created {
        let public = unb64(text(args, "public_key")?, 32)?;
        require(public.len() == 32, "invalid_public_key")?;
        let recipient = text(args, "encryption_recipient")?;
        let age = age_recipient::public_from_recipient(recipient)?;
        require(
            public.as_slice() != age.as_slice(),
            "encryption_key_must_be_independent",
        )?;
        let proof = wire(
            &serde_json::json!({"target_service":service.validator.service(),"public_key":field(args,"public_key")?,"encryption_recipient":recipient,"grantor":field(args,"grantor")?}),
        )?;
        verify(
            &public,
            proof.canonical()?.as_bytes(),
            &decode(field(args, "possession_proof")?)?,
            "delegated-identity-v1",
        )?;
        let id = subject_id(&public);
        let kid = key_id(&public);
        match store.credential(&kid) {
            Err(Error("credential_not_found")) => (),
            Ok(_) => return Err(Error("key_exists")),
            Err(error) => return Err(error),
        }
        match store.resource(&id) {
            Err(Error("not_found")) => (),
            Ok(_) => return Err(Error("identity_exists")),
            Err(error) => return Err(error),
        }
        (id, kid, Some((public, age, recipient.to_owned())))
    } else {
        let grantee = store.resolve_identity(text(args, "grantee")?)?;
        let credential = store.credential(text(args, "key_id")?)?;
        require(
            credential.subject_id == grantee
                && credential.kind == CredentialKind::SigningKey
                && credential.revoked_at.is_none(),
            "invalid_delegate_key",
        )?;
        (grantee, credential.id, None)
    };
    let grants: Vec<Grant> = decode(field(args, "grants")?)?;
    require(!created || !grants.is_empty(), "delegation_grants_required")?;
    service.validate_ceiling(store, principal, &grants)?;
    let depth = optional(args, "depth")?
        .map(decode::<u32>)
        .transpose()?
        .unwrap_or(0);
    let ttl: u64 = decode(field(args, "ttl")?)?;
    require(ttl > 0 && (!created || ttl <= 86400), "invalid_ttl")?;
    let mut expires = after(now, ttl)?;
    let parent = if actor != grantor {
        let mut candidates = Vec::new();
        for id in principal.certificates() {
            let candidate = service.validator.validate(id, store, now)?;
            if candidate.kind == CertificateKind::Delegation && candidate.delegation_depth > 0 {
                candidates.push(candidate);
            }
        }
        if created {
            let mut found = None;
            for candidate in candidates {
                let mut bound = false;
                for source in &candidate.authority_sources {
                    if let Some(fact) = store.setting(&format!("delegation:{}", source.id))? {
                        bound |= text(&fact, "grantor")? == grantor;
                    }
                }
                if candidate.delegation_depth > depth
                    && expires <= candidate.expires_at
                    && covers(store, &candidate.grants, &grants)?
                    && bound
                {
                    found = Some(candidate);
                    break;
                }
            }
            Some(found.ok_or(Error("redelegation_forbidden"))?)
        } else {
            let candidate = candidates
                .into_iter()
                .next()
                .ok_or(Error("redelegation_forbidden"))?;
            require(
                depth < candidate.delegation_depth,
                "delegation_depth_exceeded",
            )?;
            require(expires <= candidate.expires_at, "delegation_ttl_escalation")?;
            require(
                covers(store, &candidate.grants, &grants)?,
                "redelegation_scope",
            )?;
            Some(candidate)
        }
    } else {
        None
    };
    for grant in &grants {
        require(
            service
                .base_grants
                .iter()
                .any(|base| base.capability == grant.capability),
            "special_delegation_requires_ca",
        )?;
        service.validator.validate_grant(grant, store)?;
        let resource = store.resource(&grant.scope.resource_id)?;
        require(resource.owner == grantor, "delegation_owner_required")?;
        authorizer.ceiling(
            principal,
            &format!("{operation}@1"),
            &resource.id,
            store,
            now,
        )?;
        if created {
            for operation in &grant.operations {
                authorizer.ceiling(principal, operation, &resource.id, store, now)?;
            }
        }
    }
    let max_uses = optional(args, "max_uses")?.map(decode::<u64>).transpose()?;
    let source_credential = store.credential(
        principal
            .credential_id()
            .ok_or(Error("authentication_required"))?,
    )?;
    let new_identity = if let Some((public, age, recipient)) = new_keys {
        require(depth <= 8, "delegation_depth_exceeded")?;
        if let Some(max) = max_uses {
            require(
                (1..=100000).contains(&max) && depth == 0,
                "limited_delegation_depth",
            )?;
            for grant in &grants {
                for operation in &grant.operations {
                    let (name, version) = operation
                        .rsplit_once('@')
                        .ok_or(Error("limited_delegation_operation"))?;
                    let version: u32 = version
                        .parse()
                        .map_err(|_| Error("limited_delegation_operation"))?;
                    let spec = service
                        .operations
                        .iter()
                        .find(|spec| spec.name == name && spec.version == version)
                        .ok_or(Error("operation_not_found"))?;
                    require(
                        spec.effect == "transaction"
                            && !name.starts_with("batch.")
                            && name != "file.batch"
                            && !name.starts_with("identity."),
                        "limited_delegation_operation",
                    )?;
                }
            }
        }
        require(
            source_credential
                .expires_at
                .is_none_or(|value| expires <= value),
            "delegation_ttl_escalation",
        )?;
        require(
            ttl <= issuer
                .issuance
                .as_ref()
                .ok_or(Error("issuer_not_ready"))?
                .max_cert_ttl_seconds,
            "certificate_ttl_escalation",
        )?;
        expires = expires.min(issuer.expires_at);
        let address = format!(
            "{}~{}",
            text(&owner, "name")?,
            crate::browser::random_identifier()?
        );
        store.insert_identity_resource(&resource(
            &grantee,
            "user",
            &address,
            service.namespace_root,
            (&grantee, actor),
            "0755",
            now,
        )?)?;
        store.update_subject(
            &Subject {
                resource_id: grantee.clone(),
                kind: SubjectKind::Temporary,
                primary_group: "g_public".into(),
                auth_version: 0,
                local_only: false,
            },
            -1,
        )?;
        store.save_credential(
            &Credential {
                id: key.clone(),
                subject_id: grantee.clone(),
                kind: CredentialKind::SigningKey,
                verifier: Verifier(public.clone()),
                ceiling: grants.clone(),
                not_before: now,
                expires_at: Some(expires),
                revoked_at: None,
                source_credential_id: None,
            },
            0,
        )?;
        store.save_identity_key(&IdentityKey {
            key_id: key.clone(),
            subject: grantee.clone(),
            public_key: Verifier(public),
            created_at: now,
            retired_at: None,
            primary: true,
        })?;
        let encryption_key = age_recipient::encryption_key_id(&age);
        require(
            !store.encryption_key_exists(&encryption_key)?,
            "encryption_key_exists",
        )?;
        store.save_encryption_key(&EncryptionKey {
            key_id: encryption_key.clone(),
            subject: grantee.clone(),
            recipient: recipient.clone(),
            public_key: Verifier(age.to_vec()),
            created_at: now,
            retired_at: None,
            primary: true,
        })?;
        Some(NewIdentity {
            address,
            grantor_address: text(&owner, "name")?.into(),
            encryption_key,
            encryption_recipient: recipient,
            max_uses,
        })
    } else {
        None
    };
    let mut fact = wire(
        &serde_json::json!({"grantor":grantor,"grantee":grantee,"grants":grants,"expires_at":expires,"parent_certificate":parent.as_ref().map(|cert|cert.resource_id.as_str())}),
    )?;
    if created || setting_truthy(store, &format!("delegated_identity:{actor}"))? {
        crate::custodial_migration::set(
            &mut fact,
            "grantor_credential_id",
            Json::string(&source_credential.id),
        )?;
    }
    let body = fact.canonical()?.into_bytes();
    let actual_ttl =
        u64::try_from((expires.0 - now.0).num_seconds()).map_err(|_| Error("invalid_ttl"))?;
    Ok(Prepared {
        fact,
        body,
        name: new_id("delegation")?,
        actor: actor.into(),
        grantor: grantor.into(),
        credential: source_credential.id,
        auth_version: store.subject(actor)?.auth_version,
        request_id: text(request, "request_id")?.into(),
        payload_digest: text(request, "payload_digest")?.into(),
        grantee,
        key,
        grants,
        depth,
        expires,
        ttl: created.then_some(actual_ttl),
        new_identity,
        now,
    })
}

pub fn finish_content<S: IdentityStore>(
    service: &lifecycle::Service<'_>,
    store: &mut S,
    principal: &Principal,
    request: &Json,
    plan: Prepared,
    reference: &ResourceRef,
    now: Timestamp,
) -> Result<Json> {
    store.require_live()?;
    require(
        principal.actor() == Some(plan.actor.as_str())
            && principal.subject() == Some(plan.grantor.as_str())
            && principal.credential_id() == Some(plan.credential.as_str())
            && text(request, "request_id")? == plan.request_id
            && text(request, "payload_digest")? == plan.payload_digest
            && now == plan.now
            && store.subject(&plan.actor)?.auth_version == plan.auth_version
            && store.credential(&plan.credential)?.current_at(now),
        "delegation_content_binding_mismatch",
    )?;
    let object = store.record(Record::Resource, &reference.id)?;
    let revision = store.identity_revision(reference)?;
    require(
        text(&object, "type")? == "delegation"
            && text(&object, "parent")? == plan.grantor
            && text(&object, "owner")? == plan.grantor
            && text(&object, "name")? == plan.name
            && text(&object, "state")? == "active"
            && text(&object, "revision")? == text(&revision, "id")?
            && reference.revision.as_deref() == Some(text(&revision, "id")?)
            && text(&revision, "actor")? == plan.actor
            && text(&revision, "subject")? == plan.grantor
            && decode::<Timestamp>(field(&object, "created_at")?)? == now
            && decode::<Timestamp>(field(&revision, "created_at")?)? == now
            && text(field(&revision, "content")?, "digest")? == digest(&plan.body)
            && decode::<u64>(field(field(&revision, "content")?, "size")?)?
                == plan.body.len() as u64,
        "delegation_content_binding_mismatch",
    )?;
    store.set_identity_setting(&format!("delegation:{}", reference.id), &plan.fact)?;
    let certificate = service.issue(
        store,
        principal,
        request,
        Issuance {
            subject: &plan.grantee,
            key: &plan.key,
            grants: Some(plan.grants.clone()),
            ttl: plan.ttl,
            authority_source: None,
            audit_source: None,
            delegation: Some((vec![reference.clone()], plan.depth)),
        },
        now,
    )?;
    if let Some(identity) = plan.new_identity {
        let binding = wire(
            &serde_json::json!({"grantor":plan.grantor,"grantor_address":identity.grantor_address,"delegation_id":reference.id,
            "certificate_id":certificate.resource_id,"expires_at":plan.expires,"address":identity.address,"max_uses":identity.max_uses,"uses":0}),
        )?;
        store.set_identity_setting(&format!("delegated_identity:{}", plan.grantee), &binding)?;
        let mut data = binding;
        for (key, value) in [
            ("subject_id", Json::string(plan.grantee)),
            ("key_id", Json::string(plan.key)),
            ("encryption_key_id", Json::string(identity.encryption_key)),
            (
                "encryption_recipient",
                Json::string(identity.encryption_recipient),
            ),
            ("target_service", Json::string(service.validator.service())),
            ("grants", wire(&plan.grants)?),
            ("depth", wire(&plan.depth)?),
        ] {
            crate::custodial_migration::set(&mut data, key, value)?;
        }
        return wire(&serde_json::json!({"resources":[{"id":reference.id}],"data":data}));
    }
    output(
        serde_json::json!({"certificate_id":certificate.resource_id,"grantee":plan.grantee}),
        vec![reference.clone()],
    )
}
