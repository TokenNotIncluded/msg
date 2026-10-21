//! Resource and immutable Revision handlers. The executor owns authorization,
//! request replay and the durable metadata/content transaction.
mod patch;
pub(crate) mod public_board;
use crate::context::{new_id, output, Context, HandlerOutput};
use msg_core::{digest, unb64, Error, Json, Result};
use msg_crypto::{verify, Signature};
use msg_identity::{
    authorization::{AccessRequirement, AuthorizationStore, Check},
    models::{CredentialKind, ResourceRef, SubjectKind, Timestamp},
    store::{truthy, AuthorityStore},
};
use msg_storage::{
    postgres::Value,
    records::{
        decode, encode, BlobRef, EffectJob, Event, JobDraft, Relation, Resource, ResourceState,
        Revision, SignatureSource, SourceKind,
    },
};
use serde_json::{json, Value as J};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{self, Write},
};
use unicode_normalization::UnicodeNormalization;

const ROOT: &str = "u_root";
const ROOT_SPACE: &str = "r_root";
const SETGID: u16 = 0o2000;
const STICKY: u16 = 0o1000;

pub fn supports(op: &str, version: u32) -> bool {
    match op {
        "content.post_patch" | "content.text_patch_batch" => (1..=2).contains(&version),
        "content.text_patch" => (1..=3).contains(&version),
        "content.post_create"
        | "content.post_write"
        | "content.post_edit"
        | "content.topic_configure" => (1..=2).contains(&version),
        "content.topic_create"
        | "content.tags_set"
        | "content.post_edit_metadata"
        | "content.post_rollback"
        | "content.file_put"
        | "content.attach"
        | "content.template_put"
        | "content.archive"
        | "content.restore"
        | "content.move"
        | "content.chmod"
        | "content.chgrp"
        | "content.chown"
        | "content.purge"
        | "content.topic_join"
        | "content.topic_leave"
        | "content.topic_invite"
        | "content.topic_approve"
        | "content.topic_remove"
        | "content.topic_promote"
        | "content.topic_demote"
        | "content.topic_ban"
        | "content.topic_unban"
        | "content.topic_policy_set"
        | "content.topic_members"
        | "content.topic_events" => version == 1,
        "content.public_board_update" => version == 1,
        _ => false,
    }
}

pub fn requirements(ctx: &Context<'_>) -> Result<Vec<AccessRequirement>> {
    let op = ctx.request.field("operation")?.as_str()?;
    let (argument, check) = match op {
        "content.topic_create"
        | "content.post_create"
        | "content.file_put"
        | "content.template_put" => ("parent", Check::Create),
        "content.post_write"
        | "content.post_edit"
        | "content.post_edit_metadata"
        | "content.post_rollback"
        | "content.post_patch"
        | "content.text_patch" => ("id", Check::Write),
        "content.topic_configure" => ("id", Check::Manage),
        "content.chmod" => ("id", Check::Chmod),
        "content.chgrp" => ("id", Check::Chgrp),
        "content.chown" => ("id", Check::Chown),
        "content.purge" => ("id", Check::Purge),
        _ => return Ok(vec![]),
    };
    Ok(vec![AccessRequirement {
        resource_id: ctx.resolve(ctx.text(argument)?, false)?,
        operation: ctx.operation()?,
        check,
    }])
}

pub fn handle(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    match ctx.request.field("operation")?.as_str()? {
        "content.topic_create" => topic_create(ctx),
        "content.post_create" => post_create(ctx),
        "content.post_write" | "content.post_edit" => post_edit(ctx),
        "content.tags_set" => tags_set(ctx),
        "content.post_edit_metadata" => post_metadata(ctx),
        "content.post_rollback" => post_rollback(ctx),
        "content.post_patch" | "content.text_patch" => text_patch(ctx),
        "content.text_patch_batch" => text_patch_batch(ctx),
        "content.file_put" => file_put(ctx),
        "content.attach" => attach(ctx),
        "content.template_put" => template_put(ctx),
        "content.topic_configure" => topic_configure(ctx),
        "content.public_board_update" => public_board::update(ctx),
        "content.archive" | "content.restore" => lifecycle(ctx),
        "content.move" => move_resource(ctx),
        "content.chmod" | "content.chgrp" | "content.chown" => permissions(ctx),
        "content.purge" => purge(ctx),
        "content.topic_members" => topic_members(ctx),
        "content.topic_events" => topic_events(ctx),
        "content.topic_join"
        | "content.topic_leave"
        | "content.topic_invite"
        | "content.topic_approve"
        | "content.topic_remove"
        | "content.topic_promote"
        | "content.topic_demote"
        | "content.topic_ban"
        | "content.topic_unban"
        | "content.topic_policy_set" => topic_membership(ctx),
        _ => Err(Error("unknown_operation")),
    }
}

fn require(ok: bool, code: &'static str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(Error(code))
    }
}
fn argument(ctx: &Context<'_>, key: &str) -> Result<Option<String>> {
    ctx.args()?
        .get(key)
        .map(|v| v.as_str().map(str::to_owned))
        .transpose()
}
fn reference(resource: &Resource) -> ResourceRef {
    ResourceRef {
        id: resource.id.clone(),
        revision: resource.revision.clone(),
    }
}
fn result(resource: &Resource, extra: J) -> Result<HandlerOutput> {
    let mut data = extra
        .as_object()
        .cloned()
        .ok_or(Error("invalid_handler_output"))?;
    data.insert("generation".into(), json!(resource.generation));
    output(J::Object(data), vec![reference(resource)])
}
pub(crate) fn assert_generation(ctx: &Context<'_>, resource: &Resource) -> Result<()> {
    assert_expected(
        ctx.request.field("expected_generations")?,
        &resource.id,
        resource.generation,
    )
}
fn assert_expected(values: &Json, id: &str, generation: i64) -> Result<()> {
    for value in values.as_array()? {
        let pair = value.as_array()?;
        require(pair.len() == 2, "invalid_expected_generation")?;
        if pair[0].as_str()? == id {
            return require(
                pair[1].as_integer()? == generation.to_string(),
                "generation_conflict",
            );
        }
    }
    Err(Error("expected_generation_required"))
}
fn valid_id(id: &str, code: &'static str) -> Result<()> {
    require(
        !id.is_empty()
            && id.len() <= 128
            && id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b)),
        code,
    )
}
pub(crate) fn validate_name(name: &str) -> Result<()> {
    require(
        (1..=120).contains(&name.chars().count())
            && !matches!(
                name,
                "." | ".." | "json" | "meta" | "raw" | "history" | "revisions"
            )
            && name
                .chars()
                .all(|c| c >= ' ' && c != '\u{7f}' && c != '/' && c != '\\'),
        "invalid_name",
    )?;
    require(!name.starts_with(['@', '&', '!', '~']), "reserved_name")
}
fn normalize_tags(value: &Json) -> Result<Vec<String>> {
    let values = value.as_array().map_err(|_| Error("invalid_tags"))?;
    require(values.len() <= 16, "invalid_tags")?;
    let mut result = BTreeSet::new();
    for value in values {
        result.insert(normalize_tag(value)?);
    }
    Ok(result.into_iter().collect())
}
pub(crate) fn normalize_tag(value: &Json) -> Result<String> {
    let normalized: String = value
        .as_str()
        .map_err(|_| Error("invalid_tag"))?
        .nfkc()
        .collect();
    let tag: String = crate::unicode::casefold(normalized.trim_matches(py_space))
        .nfc()
        .collect();
    require(
        (1..=48).contains(&tag.chars().count())
            && tag.len() <= 96
            && tag.chars().next().is_some_and(char::is_alphanumeric)
            && tag
                .chars()
                .all(|c| c.is_alphanumeric() || c == '-' || c == '_'),
        "invalid_tag",
    )?;
    Ok(tag)
}
fn summary(value: Option<String>) -> Result<Option<String>> {
    require(
        value.as_ref().is_none_or(|s| s.chars().count() <= 280),
        "invalid_summary",
    )?;
    Ok(value.filter(|s| !s.is_empty()))
}
fn setting(ctx: &Context<'_>, name: &str) -> Result<BTreeMap<String, Json>> {
    ctx.tx
        .setting(name)?
        .map(Json::into_object)
        .transpose()
        .map(|v| v.unwrap_or_default())
}
fn policy(ctx: &Context<'_>, resource: &Resource) -> Result<BTreeMap<String, Json>> {
    let id = if resource.resource_type == "topic" {
        &resource.id
    } else {
        resource.parent.as_ref().ok_or(Error("not_a_topic"))?
    };
    setting(ctx, &format!("policy:{id}"))
}
fn editable(ctx: &Context<'_>, resource: &Resource) -> Result<()> {
    require(
        policy(ctx, resource)?
            .get("editable")
            .map(truthy)
            .transpose()?
            .unwrap_or(true),
        "content_frozen",
    )
}
fn in_wiki(ctx: &Context<'_>, resource: &Resource) -> Result<bool> {
    Ok(resource.id == "t_wiki"
        || ctx
            .tx
            .ancestors(&resource.id)?
            .iter()
            .any(|r| r.id == "t_wiki"))
}
fn is_admin(ctx: &Context<'_>, topic: &str) -> Result<bool> {
    if ctx.principal.subject() == Some(ROOT) {
        return Ok(true);
    }
    let Some(subject) = ctx.principal.subject() else {
        return Ok(false);
    };
    let row = ctx.tx.one(
        "SELECT role,status FROM topic_memberships WHERE topic=$1 AND subject=$2",
        &[topic.into(), subject.into()],
    )?;
    Ok(row
        .as_ref()
        .is_some_and(|row| row.text(0) == Ok("admin") && row.text(1) == Ok("active")))
}
fn can_present(ctx: &Context<'_>, topic: &Resource) -> Result<bool> {
    Ok(ctx.principal.subject() == Some(topic.owner.as_str()) || is_admin(ctx, &topic.id)?)
}
fn presentation(ctx: &Context<'_>, resource: &Resource) -> Result<bool> {
    if resource.resource_type != "file"
        || !matches!(resource.name.as_str(), "ABOUT.md" | "HEADER.svg")
    {
        return Ok(false);
    }
    let Some(parent) = resource.parent.as_ref() else {
        return Ok(false);
    };
    let topic = ctx.tx.resource_record(parent)?;
    if topic.resource_type != "topic" {
        return Ok(false);
    }
    require(can_present(ctx, &topic)?, "topic_admin_required")?;
    Ok(true)
}
fn protect_namespace(ctx: &mut Context<'_>, parent: &Resource, name: &str) -> Result<()> {
    if parent.resource_type == "topic" && matches!(name, "ABOUT.md" | "HEADER.svg") {
        require(can_present(ctx, parent)?, "topic_admin_required")?;
    }
    if name.starts_with('_') {
        let operation = ctx.operation()?;
        require(
            ctx.authorizer.has(
                ctx.principal,
                "system.namespace",
                &operation,
                &parent.id,
                ctx.tx,
                ctx.now,
            )?,
            "protected_namespace",
        )?;
    }
    Ok(())
}
pub(crate) fn unmanaged(ctx: &Context<'_>, resource: &Resource) -> Result<()> {
    require(
        !matches!(
            resource.resource_type.as_str(),
            "order" | "order_collection"
        ),
        "order_controlled_resource",
    )?;
    let ancestors = ctx.tx.ancestors(&resource.id)?;
    let mut parent_user = false;
    for id in ancestors
        .iter()
        .map(|r| r.id.as_str())
        .chain(std::iter::once(resource.id.as_str()))
    {
        let child = ctx.tx.resource_record(id)?;
        require(
            !(parent_user
                && matches!(
                    child.name.as_str(),
                    "SOUL.md" | "AGENTS.md" | "notes" | "todos"
                )),
            "personal_managed_resource",
        )?;
        parent_user = child.resource_type == "user";
    }
    Ok(())
}
fn removable(ctx: &mut Context<'_>, resource: &Resource) -> Result<Resource> {
    unmanaged(ctx, resource)?;
    let ancestors = ctx.tx.ancestors(&resource.id)?;
    require(
        resource.id != "t_last_will" && !ancestors.iter().any(|r| r.id == "t_last_will"),
        "legacy_directive_only",
    )?;
    require(
        !matches!(resource.id.as_str(), "r_agents" | "r_rules")
            && !ancestors.iter().any(|r| r.id == "r_rules"),
        "system_managed_resource",
    )?;
    require(resource.id != ROOT, "protected_resource")?;
    let parent = ctx.tx.resource_record(
        resource
            .parent
            .as_deref()
            .ok_or(Error("protected_resource"))?,
    )?;
    ctx.check(&parent.id, Check::Remove)?;
    require(
        parent.mode & STICKY == 0
            || ctx.principal.subject() == Some(resource.owner.as_str())
            || ctx.principal.subject() == Some(parent.owner.as_str()),
        "sticky_denied",
    )?;
    Ok(parent)
}
fn enforce_rules(ctx: &Context<'_>, parent: &str) -> Result<()> {
    let policy = setting(ctx, &format!("policy:{parent}"))?;
    require(
        policy
            .get("editable")
            .map(truthy)
            .transpose()?
            .unwrap_or(true),
        "content_frozen",
    )?;
    let posting = policy
        .get("posting_policy")
        .map(Json::as_str)
        .transpose()?
        .unwrap_or("open");
    if posting == "open" {
        return Ok(());
    }
    let row = ctx
        .tx
        .one(
            "SELECT role,status FROM topic_memberships WHERE topic=$1 AND subject=$2",
            &[parent.into(), ctx.principal.subject().into()],
        )?
        .ok_or(Error("board_members_only"))?;
    require(row.text(1)? == "active", "board_members_only")?;
    require(
        posting != "admins" || row.text(0)? == "admin",
        "board_admins_only",
    )
}
fn direct_message(ctx: &Context<'_>, id: &str) -> Result<bool> {
    let ids: Vec<String> = ctx
        .tx
        .ancestors(id)?
        .into_iter()
        .map(|r| r.id)
        .chain(std::iter::once(id.to_owned()))
        .collect();
    Ok(ctx
        .tx
        .one(
            "SELECT resource_id FROM dm_conversations WHERE resource_id=ANY($1) LIMIT 1",
            &[Value::TextArray(ids)],
        )?
        .is_some())
}

pub(crate) enum Body {
    Bytes(Vec<u8>, String),
    Existing(BlobRef),
}
#[derive(Default)]
pub(crate) struct RevisionOptions {
    pub relations: Vec<Relation>,
    pub author: Option<String>,
    pub signature: Option<Signature>,
    pub id: Option<String>,
    pub summary: Option<String>,
    pub change_note: Option<String>,
    pub source_kind: Option<SourceKind>,
    pub source_version: Option<i64>,
    pub source_digest: Option<String>,
    pub content_created_at: Option<String>,
}
fn revision_options(ctx: &Context<'_>) -> Result<RevisionOptions> {
    Ok(RevisionOptions {
        signature: ctx
            .args()?
            .get("content_signature")
            .map(decode)
            .transpose()?,
        id: argument(ctx, "revision_id")?,
        summary: summary(argument(ctx, "summary")?)?,
        ..RevisionOptions::default()
    })
}
pub(crate) struct Create {
    pub parent: String,
    pub kind: &'static str,
    pub name: Option<String>,
    pub id: Option<String>,
    pub body: Option<Body>,
    pub mode: Option<u16>,
    pub revision: RevisionOptions,
}
pub(crate) fn create(ctx: &mut Context<'_>, input: Create) -> Result<Resource> {
    let parent = ctx.tx.resource_record(&input.parent)?;
    require(
        !matches!(input.kind, "order" | "order_collection")
            && parent.resource_type != "order_collection",
        "order_controlled_resource",
    )?;
    require(parent.state == ResourceState::Active, "ancestor_inactive")?;
    require(
        ctx.catalog
            .resource_type(&parent.resource_type, parent.type_version as u32)?
            .container,
        "not_a_container",
    )?;
    let container = ctx.catalog.resource_type(input.kind, 1)?.container;
    let presentation = parent.resource_type == "topic"
        && input.kind == "file"
        && matches!(input.name.as_deref(), Some("ABOUT.md" | "HEADER.svg"));
    let operation = ctx.request.field("operation")?.as_str()?;
    if (parent.id == "t_store" && !presentation) || input.kind == "listing" {
        require(
            parent.id == "t_store"
                && input.kind == "listing"
                && matches!(operation, "store.listing_create" | "bounty.create"),
            "store_controlled_resource",
        )?;
    }
    if parent.id == "t_last_will" && !presentation {
        require(
            operation == "identity.legacy_put" && input.kind == "legacy_directive",
            "legacy_directive_only",
        )?;
    }
    let mut name = input.name.unwrap_or(new_id("p")?);
    if input.kind == "post" && !name.ends_with(".md") {
        name.push_str(".md");
    }
    validate_name(&name)?;
    require(
        !(parent.resource_type == "user"
            && matches!(name.as_str(), "SOUL.md" | "AGENTS.md" | "notes" | "todos"))
            || matches!(
                operation,
                "identity.personal_put" | "identity.note_put" | "identity.todo_put"
            ),
        "personal_managed_resource",
    )?;
    if matches!(parent.name.as_str(), "notes" | "todos") {
        if let Some(id) = &parent.parent {
            if ctx.tx.resource_record(id)?.resource_type == "user" {
                require(
                    if parent.name == "notes" {
                        operation == "identity.note_put"
                    } else {
                        operation == "identity.todo_put" && input.kind == "todo"
                    },
                    "personal_managed_resource",
                )?;
            }
        }
    }
    protect_namespace(ctx, &parent, &name)?;
    let subject = ctx.tx.subject(ctx.subject()?)?;
    let policy = setting(ctx, &format!("policy:{}", parent.id))?;
    let default = policy
        .get(&format!("{}_mode", input.kind))
        .or_else(|| policy.get(if container { "topic_mode" } else { "post_mode" }))
        .map(Json::as_str)
        .transpose()?
        .unwrap_or(if container { "1777" } else { "0644" });
    let mut mode = input
        .mode
        .unwrap_or(u16::from_str_radix(default, 8).map_err(|_| Error("invalid_mode"))?);
    if container && parent.mode & SETGID != 0 {
        mode |= SETGID;
    }
    if in_wiki(ctx, &parent)? {
        require(
            matches!(input.kind, "post" | "topic" | "file" | "attachment"),
            "wiki_content_only",
        )?;
        mode = if container { 0o1777 } else { 0o666 };
    }
    let id = input.id.unwrap_or(new_id("r")?);
    valid_id(&id, "invalid_resource_id")?;
    let actor = ctx
        .principal
        .actor()
        .ok_or(Error("authentication_required"))?
        .to_owned();
    let resource = Resource {
        id,
        resource_type: input.kind.into(),
        type_version: 1,
        name,
        parent: Some(parent.id),
        owner: subject.resource_id,
        group: if parent.mode & SETGID != 0 {
            parent.group
        } else {
            subject.primary_group
        },
        mode,
        generation: 0,
        revision: None,
        state: ResourceState::Active,
        created_at: ctx.now,
        created_by: actor.clone(),
        modified_at: ctx.now,
        modified_by: actor,
        tags: vec![],
    };
    ctx.tx.insert_resource(&resource)?;
    match input.body {
        Some(body) => revise(ctx, resource, body, input.revision),
        None => Ok(resource),
    }
}
fn manifest(revision: &Revision) -> Result<Json> {
    let mut value = encode(revision)?.into_object()?;
    value.remove("manifest_digest");
    value.remove("signature");
    Ok(Json::from_object(value))
}
pub(crate) fn revise(
    ctx: &mut Context<'_>,
    resource: Resource,
    body: Body,
    options: RevisionOptions,
) -> Result<Resource> {
    let presented = presentation(ctx, &resource)?;
    require(
        !matches!(
            resource.resource_type.as_str(),
            "order" | "order_collection"
        ),
        "order_controlled_resource",
    )?;
    let timestamp = options
        .content_created_at
        .or(argument(ctx, "content_created_at")?);
    require(
        options.signature.is_some() || (options.id.is_none() && timestamp.is_none()),
        "content_signature_required",
    )?;
    require(
        options.signature.is_none() || options.id.is_some(),
        "revision_id_required",
    )?;
    let blob = match body {
        Body::Existing(blob) => {
            ctx.contents.read_to(&blob, None, &mut io::sink())?;
            blob
        }
        Body::Bytes(bytes, media) => {
            require(
                bytes.len() <= ctx.settings.max_request_bytes,
                "use_transfer",
            )?;
            ctx.put_bytes(&bytes, &media, None)?
        }
    };
    if presented {
        if resource.name == "HEADER.svg" {
            require(
                blob.media_type == "image/svg+xml" && blob.size <= 98_304,
                "invalid_board_svg",
            )?;
            safe_svg(&ctx.contents.read_bytes(&blob, 98_304)?)?;
        } else {
            require(
                matches!(blob.media_type.as_str(), "text/plain" | "text/markdown")
                    && blob.size <= 4096,
                "invalid_board_description",
            )?;
        }
    }
    for relation in &options.relations {
        require(
            ctx.catalog
                .resource_type(&resource.resource_type, resource.type_version as u32)?
                .relations
                .contains(&relation.relation_type),
            "invalid_relation_type",
        )?;
        ctx.check(&relation.target.id, Check::Read)?;
        if relation.target.revision.is_some() {
            ctx.tx.revision(&relation.target)?;
        }
    }
    let id = options.id.unwrap_or(new_id("v")?);
    valid_id(&id, "invalid_revision_id")?;
    let created_at = if options.signature.is_some() {
        let time = Timestamp::parse(
            timestamp
                .as_deref()
                .ok_or(Error("content_timestamp_required"))?,
        )?;
        let drift = time.0 - ctx.now.0;
        require(
            drift >= chrono::Duration::seconds(-300) && drift <= chrono::Duration::seconds(300),
            "content_timestamp_out_of_range",
        )?;
        time
    } else {
        ctx.now
    };
    let subject = ctx.subject()?.to_owned();
    let custodial = ctx.principal.method() == "token"
        && ctx.tx.subject(&subject)?.kind == SubjectKind::Custodial;
    require(
        !custodial || options.signature.is_none(),
        "custodial_content_signature_forbidden",
    )?;
    let mut revision = Revision {
        format_version: 1,
        id,
        resource_id: resource.id.clone(),
        parents: resource.revision.iter().cloned().collect(),
        content: blob,
        relations: options.relations,
        actor: ctx
            .principal
            .actor()
            .ok_or(Error("authentication_required"))?
            .into(),
        subject: subject.clone(),
        author: options.author.unwrap_or(subject.clone()),
        created_at,
        manifest_digest: String::new(),
        signature: None,
        summary: summary(options.summary)?,
        change_note: options.change_note,
        source_kind: if custodial {
            Some(SourceKind::Operation)
        } else {
            options.source_kind
        },
        source_version: if custodial {
            Some(
                ctx.request
                    .field("contract_version")?
                    .as_integer()?
                    .parse()
                    .map_err(|_| Error("invalid_version"))?,
            )
        } else {
            options.source_version
        },
        source_digest: if custodial {
            Some(ctx.request.payload_digest()?)
        } else {
            options.source_digest
        },
        signature_source: custodial.then_some(SignatureSource::Custodial),
    };
    let signed = manifest(&revision)?.canonical()?;
    revision.manifest_digest = digest(signed.as_bytes());
    if let Some(signature) = options.signature {
        let credential = ctx.tx.credential(
            ctx.principal
                .credential_id()
                .ok_or(Error("content_signer_mismatch"))?,
        )?;
        require(signature.key_id == credential.id, "content_signer_mismatch")?;
        verify(
            &credential.verifier.0,
            signed.as_bytes(),
            &signature,
            "revision",
        )?;
        revision.signature = Some(signature);
    } else if custodial {
        let signer = msg_identity::vault::open_signer(
            ctx.tx,
            &ctx.settings.token_secret,
            &subject,
            ctx.now,
        )?;
        revision.signature = Some(signer.sign(signed.as_bytes(), "revision")?);
    }
    ctx.pin(&revision.content, &revision.id)?;
    let ancestors = ctx.tx.ancestors(&resource.id)?;
    let topic = ancestors
        .iter()
        .rev()
        .find(|r| r.kind == "topic")
        .map(|r| r.id.as_str())
        .unwrap_or(ROOT_SPACE);
    ctx.commit_revision(topic, &revision)?;
    ctx.tx.append_revision(&revision)?;
    let mut updated = resource.clone();
    updated.generation = resource
        .generation
        .checked_add(1)
        .ok_or(Error("generation_exhausted"))?;
    updated.revision = Some(revision.id);
    updated.modified_at = ctx.now;
    updated.modified_by = revision.actor;
    ctx.tx.replace_resource(&updated, resource.generation)?;
    if revision.content.media_type.starts_with("text/") && revision.content.size <= 1_048_576 {
        let bytes = ctx.contents.read_bytes(&revision.content, 1_048_576)?;
        let text = String::from_utf8_lossy(&bytes).into_owned();
        ctx.tx.execute("INSERT INTO projections(resource_id,text) VALUES($1,$2) ON CONFLICT(resource_id) DO UPDATE SET text=excluded.text", &[resource.id.into(), text.into()])?;
    } else {
        ctx.tx.execute(
            "DELETE FROM projections WHERE resource_id=$1",
            &[resource.id.into()],
        )?;
    }
    Ok(updated)
}

// The snapshot reader verifies digest/length before this bounded incremental
// UTF-8 validator sees any bytes. Source imports never rewrite signed content.
#[derive(Default)]
struct Utf8 {
    tail: Vec<u8>,
    invalid: bool,
}
impl Write for Utf8 {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let mut data = std::mem::take(&mut self.tail);
        data.extend_from_slice(bytes);
        match std::str::from_utf8(&data) {
            Ok(_) => (),
            Err(e) if e.error_len().is_none() => {
                self.tail.extend_from_slice(&data[e.valid_up_to()..])
            }
            Err(_) => {
                self.invalid = true;
                return Err(io::Error::new(io::ErrorKind::InvalidData, "invalid_utf8"));
            }
        }
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
fn source_content(ctx: &mut Context<'_>, value: &Json) -> Result<Body> {
    let reference: ResourceRef = decode(value)?;
    require(reference.revision.is_some(), "source_revision_required")?;
    ctx.check(&reference.id, Check::Read)?;
    require(!direct_message(ctx, &reference.id)?, "dm_reference_private")?;
    let revision = ctx.tx.revision(&reference)?;
    require(
        matches!(
            revision.content.media_type.as_str(),
            "text/plain" | "text/markdown"
        ),
        "text_source_required",
    )?;
    let mut validator = Utf8::default();
    let result = ctx
        .contents
        .read_to(&revision.content, None, &mut validator);
    require(
        !validator.invalid && validator.tail.is_empty(),
        "invalid_utf8",
    )?;
    result?;
    Ok(Body::Existing(revision.content))
}

fn topic_create(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let input = Create {
        parent: ctx.resolve(ctx.text("parent")?, false)?,
        kind: "topic",
        name: Some(ctx.text("name")?.into()),
        id: None,
        body: None,
        mode: None,
        revision: RevisionOptions::default(),
    };
    let resource = create(ctx, input)?;
    ctx.tx.execute(
        "INSERT INTO topic_settings(topic,membership_policy) VALUES($1,'open')",
        &[resource.id.as_str().into()],
    )?;
    let role = if in_wiki(ctx, &resource)? {
        "member"
    } else {
        "admin"
    };
    ctx.tx.execute("INSERT INTO topic_memberships(topic,subject,role,status,joined_at,invited_by) VALUES($1,$2,$3,'active',$4,NULL)", &[resource.id.as_str().into(), ctx.subject()?.into(), role.into(), encode(&ctx.now)?.as_str()?.into()])?;
    governance_event(ctx, &resource.id, "create", ctx.principal.subject(), None)?;
    result(&resource, json!({}))
}
fn post_body(
    ctx: &mut Context<'_>,
    parent: &str,
    old: Option<&Revision>,
    options: &mut RevisionOptions,
) -> Result<(Body, J)> {
    let args = ctx.args()?.clone();
    if let Some(template) = args.get("template").filter(|v| !v.is_null()) {
        require(
            !args.contains_key("body") && !args.contains_key("source"),
            "ambiguous_content",
        )?;
        let (body, metadata, relation) =
            template_content(ctx, parent, template, args.get("values"))?;
        options.relations.push(relation);
        Ok((body, metadata))
    } else if let Some(source) = args.get("source").filter(|v| !v.is_null()) {
        require(!args.contains_key("body"), "ambiguous_content")?;
        Ok((source_content(ctx, source)?, json!({})))
    } else if args.contains_key("summary") && !args.contains_key("body") && old.is_some() {
        let old = old.ok_or(Error("body_required"))?;
        options.relations = old.relations.clone();
        Ok((Body::Existing(old.content.clone()), json!({})))
    } else {
        let body = args.get("body").ok_or(Error("body_required"))?.as_str()?;
        Ok((
            Body::Bytes(body.as_bytes().to_vec(), "text/markdown".into()),
            json!({}),
        ))
    }
}
fn post_create(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let parent = ctx.resolve(ctx.text("parent")?, false)?;
    enforce_rules(ctx, &parent)?;
    let mut revision = revision_options(ctx)?;
    let (body, metadata) = post_body(ctx, &parent, None, &mut revision)?;
    let input = Create {
        parent,
        kind: "post",
        name: argument(ctx, "name")?,
        id: argument(ctx, "resource_id")?,
        body: Some(body),
        mode: None,
        revision,
    };
    let resource = create(ctx, input)?;
    result(&resource, metadata)
}
fn post_edit(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let resource = ctx
        .tx
        .resource_record(&ctx.resolve(ctx.text("id")?, false)?)?;
    require(
        resource.resource_type == "post" && resource.state == ResourceState::Active,
        "not_editable",
    )?;
    assert_generation(ctx, &resource)?;
    require(
        resource.revision.as_deref() == Some(ctx.text("expected_revision")?),
        "revision_conflict",
    )?;
    editable(ctx, &resource)?;
    let old = ctx.tx.revision(&reference(&resource))?;
    let mut options = revision_options(ctx)?;
    if !ctx.args()?.contains_key("summary") {
        options.summary = old.summary.clone();
    }
    options.author = Some(old.author.clone());
    options.relations = old
        .relations
        .iter()
        .filter(|r| r.relation_type != "template")
        .cloned()
        .collect();
    let (body, _) = post_body(
        ctx,
        resource.parent.as_deref().ok_or(Error("not_a_topic"))?,
        Some(&old),
        &mut options,
    )?;
    let updated = revise(ctx, resource, body, options)?;
    result(&updated, json!({}))
}
fn change(ctx: &Context<'_>, mut resource: Resource) -> Result<Resource> {
    let generation = resource.generation;
    resource.generation = generation
        .checked_add(1)
        .ok_or(Error("generation_exhausted"))?;
    resource.modified_at = ctx.now;
    resource.modified_by = ctx
        .principal
        .actor()
        .ok_or(Error("authentication_required"))?
        .into();
    ctx.tx.replace_resource(&resource, generation)?;
    Ok(resource)
}
fn tags_set(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let mut resource = ctx
        .tx
        .resource_record(&ctx.resolve(ctx.text("id")?, false)?)?;
    unmanaged(ctx, &resource)?;
    ctx.check(
        &resource.id,
        if matches!(resource.resource_type.as_str(), "topic" | "repo") {
            Check::Manage
        } else {
            Check::Write
        },
    )?;
    require(
        ctx.catalog
            .resource_type(&resource.resource_type, resource.type_version as u32)?
            .taggable,
        "resource_not_taggable",
    )?;
    require(resource.state == ResourceState::Active, "resource_inactive")?;
    assert_generation(ctx, &resource)?;
    let tags = normalize_tags(ctx.arg("tags")?)?;
    if resource.tags != tags {
        resource.tags = tags;
        resource = change(ctx, resource)?;
    }
    result(&resource, json!({"tags":resource.tags}))
}
fn post_metadata(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let mut resource = ctx
        .tx
        .resource_record(&ctx.resolve(ctx.text("id")?, false)?)?;
    require(
        ctx.args()?.contains_key("name") || ctx.args()?.contains_key("tags"),
        "metadata_change_required",
    )?;
    require(
        resource.resource_type == "post" && resource.state == ResourceState::Active,
        "not_editable",
    )?;
    unmanaged(ctx, &resource)?;
    assert_generation(ctx, &resource)?;
    editable(ctx, &resource)?;
    let mut name = resource.name.clone();
    if let Some(supplied) = argument(ctx, "name")? {
        let parent = removable(ctx, &resource)?;
        name = supplied;
        if !name.ends_with(".md") {
            name.push_str(".md");
        }
        validate_name(&name)?;
        protect_namespace(ctx, &parent, &name)?;
    }
    let tags = ctx
        .args()?
        .get("tags")
        .map(normalize_tags)
        .transpose()?
        .unwrap_or(resource.tags.clone());
    if name != resource.name || tags != resource.tags {
        resource.name = name;
        resource.tags = tags;
        resource = change(ctx, resource)?;
    }
    result(
        &resource,
        json!({"name":resource.name,"tags":resource.tags}),
    )
}
fn post_rollback(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let resource = ctx
        .tx
        .resource_record(&ctx.resolve(ctx.text("id")?, false)?)?;
    require(
        resource.resource_type == "post" && resource.state == ResourceState::Active,
        "not_editable",
    )?;
    unmanaged(ctx, &resource)?;
    assert_generation(ctx, &resource)?;
    editable(ctx, &resource)?;
    require(
        resource.revision.as_deref() == Some(ctx.text("base_revision")?),
        "revision_conflict",
    )?;
    require(
        resource.revision.as_deref() != Some(ctx.text("target_revision")?),
        "rollback_target_current",
    )?;
    let current = ctx.tx.revision(&reference(&resource))?;
    let old = ctx.tx.revision(&ResourceRef {
        id: resource.id.clone(),
        revision: Some(ctx.text("target_revision")?.into()),
    })?;
    require(old.author == current.author, "rollback_author_mismatch")?;
    let signed = manifest(&old)?.canonical()?;
    require(
        digest(signed.as_bytes()) == old.manifest_digest,
        "revision_manifest_mismatch",
    )?;
    if let Some(signature) = &old.signature {
        let credential = ctx.tx.credential(&signature.key_id)?;
        require(
            credential.subject_id == old.subject && credential.kind == CredentialKind::SigningKey,
            "revision_signer_mismatch",
        )?;
        verify(
            &credential.verifier.0,
            signed.as_bytes(),
            signature,
            "revision",
        )?;
    }
    let mut options = revision_options(ctx)?;
    options.author = Some(current.author);
    options.summary = old.summary;
    options.relations = old.relations;
    let updated = revise(ctx, resource, Body::Existing(old.content), options)?;
    result(&updated, json!({"rolled_back_from":old.id}))
}
fn file_put(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let args = ctx.args()?.clone();
    require(
        args.contains_key("data") != args.contains_key("source"),
        "one_content_source_required",
    )?;
    let media = argument(ctx, "media_type")?.unwrap_or("application/octet-stream".into());
    require(!media.contains(['\r', '\n']), "invalid_media_type")?;
    let body = if let Some(source) = args.get("source") {
        let reference: ResourceRef = decode(source)?;
        ctx.check(&reference.id, Check::Read)?;
        Body::Existing(ctx.tx.revision(&reference)?.content)
    } else {
        Body::Bytes(
            unb64(ctx.text("data")?, ctx.settings.max_request_bytes)?,
            media,
        )
    };
    let input = Create {
        parent: ctx.resolve(ctx.text("parent")?, false)?,
        kind: "file",
        name: Some(ctx.text("name")?.into()),
        id: None,
        body: Some(body),
        mode: None,
        revision: RevisionOptions::default(),
    };
    let resource = create(ctx, input)?;
    result(&resource, json!({}))
}
fn attach(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let post = ctx
        .tx
        .resource_record(&ctx.resolve(ctx.text("post")?, false)?)?;
    require(
        post.resource_type == "post" && post.state == ResourceState::Active,
        "not_a_post",
    )?;
    assert_generation(ctx, &post)?;
    ctx.check(&post.id, Check::Write)?;
    let source: ResourceRef = decode(ctx.arg("source")?)?;
    ctx.check(&source.id, Check::Read)?;
    let revision = ctx.tx.revision(&source)?;
    let parent = post.parent.clone().ok_or(Error("not_a_topic"))?;
    ctx.check(&parent, Check::Create)?;
    let input = Create {
        parent,
        kind: "attachment",
        name: Some(argument(ctx, "name")?.unwrap_or(new_id("attachment")?)),
        id: None,
        body: Some(Body::Existing(revision.content)),
        mode: Some(post.mode & 0o777),
        revision: RevisionOptions::default(),
    };
    let attached = create(ctx, input)?;
    ctx.tx.set_setting(
        &format!("publication:{}", attached.id),
        &encode(&json!({"source":source,"post":post.id,"publisher":ctx.subject()?}))?,
    )?;
    let mut old = ctx.tx.revision(&reference(&post))?;
    old.relations.push(Relation {
        relation_type: "attachment".into(),
        target: reference(&attached),
        excerpt: None,
    });
    let updated = revise(
        ctx,
        post,
        Body::Existing(old.content),
        RevisionOptions {
            relations: old.relations,
            author: Some(old.author),
            summary: old.summary,
            ..RevisionOptions::default()
        },
    )?;
    result(&updated, json!({"attachment":reference(&attached)}))
}
fn topic_configure(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let resource = ctx
        .tx
        .resource_record(&ctx.resolve(ctx.text("id")?, false)?)?;
    unmanaged(ctx, &resource)?;
    require(resource.resource_type == "topic", "not_a_topic")?;
    assert_generation(ctx, &resource)?;
    ctx.tx
        .set_setting(&format!("policy:{}", resource.id), ctx.arg("policy")?)?;
    let updated = change(ctx, resource)?;
    governance_event(ctx, &updated.id, "configure", ctx.principal.subject(), None)?;
    result(&updated, json!({}))
}
fn governance_event(
    ctx: &Context<'_>,
    topic: &str,
    action: &str,
    target: Option<&str>,
    reason: Option<&str>,
) -> Result<()> {
    let mut data = json!({"topic_id":topic,"action":action,"target_subject":target});
    if let Some(reason) = reason.filter(|r| !r.is_empty()) {
        data["reason"] = json!(reason);
    }
    let event = Event {
        id: new_id("te")?,
        event_type: format!("topic.{action}"),
        time: ctx.now,
        request_id: ctx.request.field("request_id")?.as_str()?.into(),
        actor: ctx.principal.actor().map(str::to_owned),
        subject: ctx.principal.subject().map(str::to_owned),
        resources: vec![ResourceRef {
            id: topic.into(),
            revision: None,
        }],
        data: encode(&data)?.into_object()?,
    };
    ctx.tx.append_event(&event)?;
    ctx.tx.execute(
        "INSERT INTO topic_event_projection(seq,topic) SELECT seq,$1 FROM events WHERE id=$2",
        &[topic.into(), event.id.as_str().into()],
    )?;
    if let Some(target) = target {
        let id = new_id("message")?;
        let notice = encode(
            &json!({"id":id,"sender":ctx.principal.subject(),"actor":ctx.principal.actor(),"recipient":target,"resource":{"id":target,"revision":null},"time":ctx.now,"state":"delivered","source":"topic_governance","topic_id":topic,"action":action}),
        )?;
        ctx.tx.execute("INSERT INTO messages(id,sender,recipient,resource,event_id,body) VALUES($1,$2,$3,$3,$4,$5)", &[id.into(),ctx.principal.subject().into(),target.into(),event.id.into(),notice.canonical()?.into()])?;
    }
    Ok(())
}

fn safe_svg(bytes: &[u8]) -> Result<()> {
    const NS: &str = "http://www.w3.org/2000/svg";
    let error = Error("invalid_board_svg");
    require(!bytes.is_empty() && bytes.len() <= 98_304, error.0)?;
    let text = std::str::from_utf8(bytes).map_err(|_| error)?;
    let lower = crate::unicode::casefold(&text.replace(NS, ""));
    require(
        ![
            "<!",
            "@import",
            "javascript:",
            "data:",
            "http:",
            "https:",
            "//",
        ]
        .iter()
        .any(|s| lower.contains(s))
            && !lower
                .match_indices("url")
                .any(|(i, _)| lower[i + 3..].trim_start().starts_with('(')),
        error.0,
    )?;
    let document = roxmltree::Document::parse(text).map_err(|_| error)?;
    let root = document.root_element();
    require(
        root.tag_name().namespace() == Some(NS) && root.tag_name().name() == "svg",
        error.0,
    )?;
    for node in document.descendants().filter(|n| n.is_element()) {
        require(
            node.tag_name().namespace() == Some(NS)
                && matches!(
                    node.tag_name().name(),
                    "svg"
                        | "g"
                        | "defs"
                        | "title"
                        | "desc"
                        | "text"
                        | "tspan"
                        | "rect"
                        | "circle"
                        | "ellipse"
                        | "path"
                        | "line"
                        | "polyline"
                        | "polygon"
                        | "style"
                        | "animate"
                        | "animateTransform"
                        | "animateMotion"
                        | "set"
                ),
            error.0,
        )?;
        for attr in node.attributes() {
            let name = attr.name().to_ascii_lowercase();
            require(
                !name.starts_with("on") && !matches!(name.as_str(), "href" | "src"),
                error.0,
            )?;
            if name == "attributename" {
                require(
                    matches!(
                        attr.value().to_ascii_lowercase().as_str(),
                        "opacity"
                            | "fill"
                            | "stroke"
                            | "transform"
                            | "x"
                            | "y"
                            | "cx"
                            | "cy"
                            | "r"
                            | "rx"
                            | "ry"
                            | "d"
                            | "visibility"
                            | "stroke-width"
                            | "font-size"
                    ),
                    error.0,
                )?;
            }
        }
    }
    Ok(())
}

struct Field {
    name: String,
    kind: String,
    required: bool,
    choices: Vec<String>,
    default: Option<Json>,
}
struct Template {
    name: String,
    version: i64,
    fields: Vec<Field>,
}
fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}
fn template_name(name: &str) -> bool {
    name.chars().next().is_some_and(|c| c.is_ascii_alphabetic())
        && name
            .chars()
            .all(|c| c.is_alphanumeric() || matches!(c, '_' | '.' | '-'))
}
fn check_field(field: &Field, value: &Json) -> Result<()> {
    if value.is_null() && !field.required {
        return Ok(());
    }
    match field.kind.as_str() {
        "str" | "text" | "enum" => {
            value.as_str().map_err(|_| Error("template_type_error"))?;
        }
        "int" => {
            value
                .as_integer()
                .map_err(|_| Error("template_type_error"))?;
        }
        "bool" => {
            value.as_bool().map_err(|_| Error("template_type_error"))?;
        }
        "ref" | "file" => {
            if let Ok(id) = value.as_str() {
                require(
                    !id.is_empty()
                        && id.len() <= 128
                        && id
                            .bytes()
                            .all(|b| b.is_ascii_alphanumeric() || b"_.:-".contains(&b)),
                    "template_reference_error",
                )?;
            } else {
                let _: ResourceRef = decode(value)?;
            }
        }
        _ => return Err(Error("unknown_field_type")),
    }
    if field.kind == "enum" {
        require(
            field
                .choices
                .iter()
                .any(|c| value.as_str() == Ok(c.as_str())),
            "template_enum_error",
        )?;
    }
    Ok(())
}
fn parse_template(source: &str) -> Result<Template> {
    let mut lines = source
        .split([
            '\n', '\r', '\u{b}', '\u{c}', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}', '\u{2028}',
            '\u{2029}',
        ])
        .map(|s| s.trim_matches(py_space))
        .filter(|s| !s.is_empty() && !s.starts_with('#'));
    let header = lines.next().ok_or(Error("empty_template"))?;
    let (name, version) = header
        .split_once('@')
        .ok_or(Error("invalid_template_header"))?;
    require(
        template_name(name)
            && !version.starts_with('0')
            && version.bytes().all(|b| b.is_ascii_digit()),
        "invalid_template_header",
    )?;
    let version = version
        .parse::<i64>()
        .map_err(|_| Error("invalid_template_header"))?;
    require(version > 0, "invalid_template_header")?;
    let mut fields = Vec::new();
    let mut names = BTreeSet::new();
    for line in lines {
        let (name, rest) = line
            .split_once(':')
            .ok_or(Error("invalid_template_field"))?;
        require(template_name(name), "invalid_template_field")?;
        require(names.insert(name.to_owned()), "duplicate_field")?;
        let default_at = if rest.starts_with("enum(") {
            rest.find(')')
                .and_then(|end| rest[end + 1..].find('=').map(|i| end + 1 + i))
        } else {
            rest.find('=')
        };
        let (mut kind, default) = default_at
            .map(|i| (&rest[..i], Some(&rest[i + 1..])))
            .unwrap_or((rest, None));
        let required = kind.ends_with('!');
        if kind.ends_with(['!', '?']) {
            kind = &kind[..kind.len() - 1];
        }
        require(!required || default.is_none(), "required_field_has_default")?;
        let choices =
            if let Some(values) = kind.strip_prefix("enum(").and_then(|s| s.strip_suffix(')')) {
                kind = "enum";
                let values: Vec<String> = values.split(',').map(|s| s.trim().to_owned()).collect();
                require(
                    values.iter().all(|s| !s.is_empty())
                        && values.iter().collect::<BTreeSet<_>>().len() == values.len(),
                    "invalid_enum",
                )?;
                values
            } else {
                vec![]
            };
        require(
            matches!(
                kind,
                "str" | "text" | "int" | "bool" | "enum" | "ref" | "file"
            ),
            "unknown_field_type",
        )?;
        let mut field = Field {
            name: name.into(),
            kind: kind.into(),
            required,
            choices,
            default: None,
        };
        if let Some(default) = default {
            let value = match Json::parse(default) {
                Ok(v) => v,
                Err(_) => {
                    require(
                        matches!(kind, "str" | "text" | "enum" | "ref" | "file"),
                        "invalid_default",
                    )?;
                    Json::string(default)
                }
            };
            check_field(&field, &value)?;
            field.default = Some(value);
        }
        fields.push(field);
    }
    Ok(Template {
        name: name.into(),
        version,
        fields,
    })
}
fn template_values(template: &Template, supplied: Option<&Json>) -> Result<Json> {
    let empty = BTreeMap::new();
    let supplied = supplied
        .map(Json::as_object)
        .transpose()
        .map_err(|_| Error("invalid_template_values"))?
        .unwrap_or(&empty);
    require(
        supplied
            .keys()
            .all(|k| template.fields.iter().any(|f| &f.name == k)),
        "unknown_template_field",
    )?;
    let mut result = BTreeMap::new();
    for field in &template.fields {
        if let Some(value) = supplied.get(&field.name) {
            check_field(field, value)?;
            result.insert(field.name.clone(), value.clone());
        } else if let Some(value) = &field.default {
            result.insert(field.name.clone(), value.clone());
        } else {
            require(!field.required, "missing_required")?;
        }
    }
    Ok(Json::from_object(result))
}
fn template_content(
    ctx: &mut Context<'_>,
    parent: &str,
    value: &Json,
    values: Option<&Json>,
) -> Result<(Body, J, Relation)> {
    let reference = if let Ok(value) = value.as_str() {
        let id = if value.contains('/') || value.starts_with("tpl_") {
            ctx.resolve(value, false)?
        } else {
            let path = format!(
                "{}/templates/{value}",
                ctx.tx.resource_path(parent)?.trim_end_matches('/')
            );
            match ctx.resolve(&path, false) {
                Ok(id) => id,
                Err(Error("not_found")) => ctx.resolve(&format!("/templates/{value}"), false)?,
                Err(e) => return Err(e),
            }
        };
        ResourceRef {
            revision: ctx.tx.resource_record(&id)?.revision,
            id,
        }
    } else {
        let args = value.as_object()?;
        let id = ctx.resolve(
            args.get("id").ok_or(Error("invalid_template"))?.as_str()?,
            false,
        )?;
        let revision = if let Some(version) = args.get("version") {
            ctx.tx
                .setting(&format!("template_version:{id}:{}", version.as_integer()?))?
                .map(|v| v.as_str().map(str::to_owned))
                .transpose()?
        } else {
            ctx.tx.resource_record(&id)?.revision
        };
        require(revision.is_some(), "template_version_not_found")?;
        ResourceRef { id, revision }
    };
    ctx.check(&reference.id, Check::Read)?;
    require(
        ctx.tx.resource_record(&reference.id)?.resource_type == "template",
        "not_a_template",
    )?;
    let revision = ctx.tx.revision(&reference)?;
    let bytes = ctx
        .contents
        .read_bytes(&revision.content, ctx.settings.max_request_bytes as u64)?;
    let source = std::str::from_utf8(&bytes).map_err(|_| Error("invalid_utf8"))?;
    let template = parse_template(source)?;
    let values = template_values(&template, values)?;
    let meta = json!({"template_id":reference.id,"template_version":template.version,"template_digest":revision.content.digest});
    let mut content = encode(&meta)?.into_object()?;
    content.insert("values".into(), values);
    Ok((
        Body::Bytes(
            Json::from_object(content).canonical()?.into_bytes(),
            "application/json".into(),
        ),
        meta,
        Relation {
            relation_type: "template".into(),
            target: reference,
            excerpt: None,
        },
    ))
}
fn template_put(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let parent = ctx.resolve(ctx.text("parent")?, false)?;
    let source = ctx.text("source")?.to_owned();
    let template = parse_template(&source)?;
    let existing = ctx.tx.one(
        "SELECT id FROM resources WHERE parent=$1 AND name=$2",
        &[parent.as_str().into(), template.name.as_str().into()],
    )?;
    let body = Body::Bytes(source.into_bytes(), "application/msg-template".into());
    let resource = if let Some(row) = existing {
        let resource = ctx.tx.resource_record(row.text(0)?)?;
        require(resource.resource_type == "template", "name_conflict")?;
        ctx.check(&resource.id, Check::Write)?;
        assert_generation(ctx, &resource)?;
        require(
            ctx.tx
                .setting(&format!(
                    "template_version:{}:{}",
                    resource.id, template.version
                ))?
                .is_none(),
            "template_version_exists",
        )?;
        revise(ctx, resource, body, RevisionOptions::default())?
    } else {
        create(
            ctx,
            Create {
                parent,
                kind: "template",
                name: Some(template.name),
                id: None,
                body: Some(body),
                mode: None,
                revision: RevisionOptions::default(),
            },
        )?
    };
    ctx.tx.set_setting(
        &format!("template_version:{}:{}", resource.id, template.version),
        &encode(&resource.revision)?,
    )?;
    let revision = ctx.tx.revision(&reference(&resource))?;
    result(
        &resource,
        json!({"template_id":resource.id,"template_version":template.version,"template_digest":revision.content.digest}),
    )
}

fn ensure_public_repositories(
    ctx: &Context<'_>,
    resource: &Resource,
    mode: Option<u16>,
    parent: Option<&str>,
) -> Result<()> {
    let mut require_parent = resource.resource_type == "repo" && parent.is_some();
    for row in ctx.tx.rows(
        "SELECT id FROM resources WHERE type='repo' AND state='active'",
        &[],
    )? {
        let id = row.text(0)?;
        if id == resource.id {
            require(
                mode.unwrap_or(resource.mode) & 4 != 0,
                "repo_public_read_required",
            )?;
        }
        if ctx.tx.ancestors(id)?.iter().any(|r| r.id == resource.id) {
            if let Some(mode) = mode {
                require(mode & 1 != 0, "repo_public_read_required")?;
            }
            require_parent |= parent.is_some();
        }
    }
    if require_parent {
        let parent = ctx.tx.resource_record(parent.ok_or(Error("not_found"))?)?;
        require(parent.mode & 1 != 0, "repo_public_read_required")?;
        for ancestor in ctx.tx.ancestors(&parent.id)? {
            require(
                u16::from_str_radix(&ancestor.mode, 8).map_err(|_| Error("invalid_mode"))? & 1 != 0,
                "repo_public_read_required",
            )?;
        }
    }
    Ok(())
}
fn website_size(ctx: &Context<'_>, resource: &Resource) -> Result<u64> {
    if resource.revision.is_none() {
        return Ok(0);
    }
    let revision = ctx.tx.revision(&reference(resource))?;
    let bytes = ctx
        .contents
        .read_bytes(&revision.content, 16 * 1024 * 1024)?;
    let manifest =
        Json::parse(std::str::from_utf8(&bytes).map_err(|_| Error("invalid_hosting_manifest"))?)?;
    let entries = manifest
        .as_object()?
        .get("entries")
        .ok_or(Error("invalid_hosting_manifest"))?
        .as_object()?;
    let mut total = 0_u64;
    for value in entries.values() {
        let revision = ctx.tx.revision(&decode(value)?)?;
        total = total
            .checked_add(
                u64::try_from(revision.content.size)
                    .map_err(|_| Error("invalid_hosting_manifest"))?,
            )
            .ok_or(Error("hosting_capacity_exceeded"))?;
        require(total <= 1_u64 << 40, "hosting_capacity_exceeded")?;
    }
    Ok(total)
}
fn website_capacity(ctx: &Context<'_>, resource: &Resource, owner: &str) -> Result<()> {
    let mut used = website_size(ctx, resource)?;
    for row in ctx.tx.rows(
        "SELECT id FROM resources WHERE type='website' AND owner=$1 AND state='active' AND id<>$2",
        &[owner.into(), resource.id.as_str().into()],
    )? {
        used = used
            .checked_add(website_size(ctx, &ctx.tx.resource_record(row.text(0)?)?)?)
            .ok_or(Error("hosting_capacity_exceeded"))?;
    }
    // PostgreSQL SUM(bigint) is numeric; text preserves the exact bounded integer.
    let extra=ctx.tx.one("SELECT COALESCE(SUM(quantity),0)::text FROM resource_entitlements WHERE subject_id=$1 AND entitlement_kind='hosting_bytes_v1' AND (expires_at IS NULL OR expires_at>$2)",&[owner.into(),encode(&ctx.now)?.as_str()?.into()])?.ok_or(Error("storage_error"))?.text(0)?.parse::<u64>().map_err(|_|Error("invalid_hosting_capacity"))?;
    let capacity = ctx
        .settings
        .hosting_base_capacity_bytes
        .saturating_add(extra)
        .min(1_u64 << 40);
    require(used <= capacity, "hosting_capacity_exceeded")
}
fn ordinary_content(resource: &Resource) -> Result<()> {
    require(
        matches!(
            resource.resource_type.as_str(),
            "post"
                | "file"
                | "attachment"
                | "topic"
                | "template"
                | "skill"
                | "repo"
                | "website"
                | "keystore"
        ),
        "controlled_resource",
    )
}
pub(crate) fn lifecycle(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let mut resource = ctx
        .tx
        .resource_record(&ctx.resolve(ctx.text("id")?, false)?)?;
    ordinary_content(&resource)?;
    assert_generation(ctx, &resource)?;
    removable(ctx, &resource)?;
    require(resource.state != ResourceState::Purged, "resource_purged")?;
    let archive = matches!(
        ctx.request.field("operation")?.as_str()?,
        "content.archive" | "file.delete"
    );
    if !archive {
        if resource.resource_type == "post" {
            enforce_rules(ctx, resource.parent.as_deref().ok_or(Error("not_a_topic"))?)?;
        }
        if resource.resource_type == "website" {
            website_capacity(ctx, &resource, &resource.owner)?;
        }
        if resource.resource_type == "repo" {
            ensure_public_repositories(
                ctx,
                &resource,
                Some(resource.mode),
                resource.parent.as_deref(),
            )?;
        }
    }
    resource.state = if archive {
        ResourceState::Archived
    } else {
        ResourceState::Active
    };
    let updated = change(ctx, resource)?;
    if updated.resource_type == "topic" {
        governance_event(
            ctx,
            &updated.id,
            if archive { "archive" } else { "restore" },
            ctx.principal.subject(),
            None,
        )?;
    }
    result(
        &updated,
        json!({"state":if archive{"archived"}else{"active"}}),
    )
}
pub(crate) fn move_resource(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let mut resource = ctx
        .tx
        .resource_record(&ctx.resolve(ctx.text("id")?, false)?)?;
    ordinary_content(&resource)?;
    assert_generation(ctx, &resource)?;
    removable(ctx, &resource)?;
    require(
        !direct_message(ctx, &resource.id)?,
        "dm_controlled_resource",
    )?;
    let target = ctx.resolve(ctx.text("parent")?, false)?;
    require(target != "t_store", "store_controlled_resource")?;
    let parent = ctx.tx.resource_record(&target)?;
    require(
        parent.resource_type != "order_collection",
        "order_controlled_resource",
    )?;
    ctx.check(&target, Check::Create)?;
    if resource.resource_type == "post" {
        enforce_rules(ctx, &target)?;
    }
    require(
        ctx.catalog
            .resource_type(&parent.resource_type, 1)?
            .container,
        "not_a_container",
    )?;
    ensure_public_repositories(ctx, &resource, None, Some(&target))?;
    let name = argument(ctx, "name")?.unwrap_or(resource.name.clone());
    validate_name(&name)?;
    require(
        !(parent.resource_type == "user"
            && matches!(name.as_str(), "SOUL.md" | "AGENTS.md" | "notes")),
        "personal_managed_resource",
    )?;
    if parent.name == "notes" {
        if let Some(grand) = &parent.parent {
            require(
                ctx.tx.resource_record(grand)?.resource_type != "user",
                "personal_managed_resource",
            )?;
        }
    }
    protect_namespace(ctx, &parent, &name)?;
    resource.name = name;
    resource.parent = Some(target);
    let updated = change(ctx, resource)?;
    if updated.resource_type == "topic" {
        governance_event(ctx, &updated.id, "move", ctx.principal.subject(), None)?;
    }
    result(&updated, json!({}))
}
fn permissions(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let mut resource = ctx
        .tx
        .resource_record(&ctx.resolve(ctx.text("id")?, false)?)?;
    unmanaged(ctx, &resource)?;
    assert_generation(ctx, &resource)?;
    require(
        resource.resource_type != "listing" && resource.id != "t_store",
        "controlled_resource",
    )?;
    let action = ctx
        .request
        .field("operation")?
        .as_str()?
        .strip_prefix("content.")
        .ok_or(Error("unknown_operation"))?;
    let extra = match action {
        "chmod" => {
            require(
                !matches!(
                    resource.resource_type.as_str(),
                    "csr" | "certificate" | "tool" | "delegation"
                ),
                "controlled_resource",
            )?;
            let mode = ctx.text("mode")?;
            require(
                mode.len() == 4 && mode.bytes().all(|b| (b'0'..=b'7').contains(&b)),
                "invalid_mode",
            )?;
            let mode = u16::from_str_radix(mode, 8).map_err(|_| Error("invalid_mode"))?;
            ensure_public_repositories(ctx, &resource, Some(mode), None)?;
            resource.mode = mode;
            json!({"mode":format!("{mode:04o}")})
        }
        "chgrp" => {
            let group = ctx.resolve(ctx.text("group")?, false)?;
            let row = ctx
                .tx
                .one(
                    "SELECT body FROM identities WHERE id=$1 AND kind='organization'",
                    &[group.as_str().into()],
                )?
                .ok_or(Error("group_not_found"))?;
            let _: msg_storage::records::Organization = decode(&row.json(0)?)?;
            let member = ctx.tx.memberships(ctx.subject()?)?.contains(&group);
            let operation = ctx.operation()?;
            require(
                member
                    || ctx.authorizer.has(
                        ctx.principal,
                        "resource.chgrp_override",
                        &operation,
                        &resource.id,
                        ctx.tx,
                        ctx.now,
                    )?,
                "group_membership_required",
            )?;
            resource.group = group.clone();
            json!({"group":group})
        }
        "chown" => {
            require(
                !matches!(
                    resource.resource_type.as_str(),
                    "user" | "organization" | "certificate" | "csr" | "tool" | "delegation"
                ),
                "controlled_resource",
            )?;
            let owner = ctx.resolve(ctx.text("owner")?, false)?;
            require(!ctx.tx.subject(&owner)?.local_only, "local_only")?;
            if resource.resource_type == "website" && resource.state == ResourceState::Active {
                website_capacity(ctx, &resource, &owner)?;
            }
            resource.owner = owner.clone();
            json!({"owner":owner})
        }
        _ => return Err(Error("unknown_operation")),
    };
    let updated = change(ctx, resource)?;
    if updated.resource_type == "topic" {
        governance_event(ctx, &updated.id, action, ctx.principal.subject(), None)?;
    }
    result(&updated, extra)
}
fn purge(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let mut resource = ctx
        .tx
        .resource_record(&ctx.resolve(ctx.text("id")?, false)?)?;
    unmanaged(ctx, &resource)?;
    assert_generation(ctx, &resource)?;
    require(
        matches!(
            resource.resource_type.as_str(),
            "post" | "file" | "attachment" | "template" | "skill" | "keystore"
        ),
        "purge_leaf_only",
    )?;
    let revisions: Vec<Revision> = ctx
        .tx
        .rows(
            "SELECT body FROM revisions WHERE resource_id=$1",
            &[resource.id.as_str().into()],
        )?
        .into_iter()
        .map(|r| decode(&r.json(0)?))
        .collect::<Result<_>>()?;
    resource.state = ResourceState::Purged;
    resource.revision = None;
    let updated = change(ctx, resource)?;
    ctx.tx.execute(
        "DELETE FROM revisions WHERE resource_id=$1",
        &[updated.id.as_str().into()],
    )?;
    ctx.tx.execute(
        "DELETE FROM projections WHERE resource_id=$1",
        &[updated.id.as_str().into()],
    )?;
    let request_id = ctx.request.field("request_id")?.as_str()?;
    let event = digest(
        encode(&json!([ctx.subject()?, request_id]))?
            .canonical()?
            .as_bytes(),
    );
    let job=EffectJob::new(ctx.principal,JobDraft{id:new_id("job")?,event_id:format!("e_{}",&event[7..39]),kind:"gc.resource".into(),dedupe_key:format!("purge:{}:{request_id}",ctx.subject()?),operation:ctx.request.field("operation")?.as_str()?.into(),arguments:encode(&json!({"contract_version":ctx.request.field("contract_version")?,"id":updated.id,"revisions":revisions}))?.into_object()?},ctx.now)?;
    ctx.tx.enqueue(&job)?;
    result(
        &updated,
        json!({"state":"purged","physical_cleanup":job.id,"backup_scope":"backups_require_separate_retention"}),
    )
}

fn governed_topic(ctx: &mut Context<'_>, admin: bool) -> Result<String> {
    let id = ctx.resolve(ctx.text("id")?, false)?;
    let resource = ctx.tx.resource_record(&id)?;
    require(
        resource.resource_type == "topic" && resource.state == ResourceState::Active,
        "topic_not_active",
    )?;
    let operation = ctx.operation()?;
    if ctx.principal.subject() != Some(ROOT) && in_wiki(ctx, &resource)? {
        require(
            !matches!(
                ctx.request.field("operation")?.as_str()?,
                "content.topic_policy_set"
                    | "content.topic_remove"
                    | "content.topic_promote"
                    | "content.topic_demote"
                    | "content.topic_ban"
                    | "content.topic_unban"
                    | "content.topic_invite"
                    | "content.topic_approve"
            ),
            "wiki_shared_resource",
        )?;
    }
    unmanaged(ctx, &resource)?;
    require(
        ctx.principal.subject().is_some() && ctx.principal.actor() == ctx.principal.subject(),
        "topic_subject_required",
    )?;
    ctx.authorizer
        .require_base(ctx.principal, &operation, &id, ctx.tx, ctx.now)?;
    ctx.check(&id, Check::Read)?;
    if admin {
        require(is_admin(ctx, &id)?, "topic_admin_required")?;
    }
    Ok(id)
}
fn topic_banned(ctx: &Context<'_>, topic: &str, subject: &str) -> Result<bool> {
    let row = ctx.tx.one(
        "SELECT expires_at FROM topic_bans WHERE topic=$1 AND subject=$2 AND status='active'",
        &[topic.into(), subject.into()],
    )?;
    match row {
        None => Ok(false),
        Some(row) => Ok(row
            .optional_text(0)?
            .map(Timestamp::parse)
            .transpose()?
            .is_none_or(|t| t > ctx.now)),
    }
}
fn last_admin(ctx: &Context<'_>, topic: &str) -> Result<()> {
    let count=ctx.tx.one("SELECT COUNT(*) FROM topic_memberships WHERE topic=$1 AND role='admin' AND status='active'",&[topic.into()])?.ok_or(Error("storage_error"))?.integer(0)?;
    require(count > 1, "last_topic_admin")
}
fn topic_membership(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let action = ctx
        .request
        .field("operation")?
        .as_str()?
        .strip_prefix("content.topic_")
        .ok_or(Error("unknown_operation"))?
        .to_owned();
    let topic = governed_topic(ctx, !matches!(action.as_str(), "join" | "leave"))?;
    let subject = ctx.subject()?.to_owned();
    let target = if matches!(action.as_str(), "join" | "leave" | "policy_set") {
        subject.clone()
    } else {
        ctx.resolve(ctx.text("subject_id")?, false)?
    };
    let previous=ctx.tx.one("SELECT role,status,joined_at,invited_by FROM topic_memberships WHERE topic=$1 AND subject=$2",&[topic.as_str().into(),target.as_str().into()])?;
    let role = previous.as_ref().map(|r| r.text(0)).transpose()?;
    let status = previous.as_ref().map(|r| r.text(1)).transpose()?;
    let now = encode(&ctx.now)?.as_str()?.to_owned();
    let (mut data, event) = match action.as_str() {
        "join" => {
            require(!topic_banned(ctx, &topic, &target)?, "topic_banned")?;
            require(
                !matches!(status, Some("active" | "pending")),
                "already_topic_member",
            )?;
            let row = ctx.tx.one(
                "SELECT membership_policy FROM topic_settings WHERE topic=$1",
                &[topic.as_str().into()],
            )?;
            let policy = row
                .as_ref()
                .map(|r| r.text(0))
                .transpose()?
                .unwrap_or("open");
            require(
                matches!(policy, "open" | "approval")
                    || (policy == "invite" && status == Some("invited")),
                "topic_join_closed",
            )?;
            let next = if policy == "approval" && status != Some("invited") {
                "pending"
            } else {
                "active"
            };
            ctx.tx.execute("INSERT INTO topic_memberships(topic,subject,role,status,joined_at,invited_by) VALUES($1,$2,'member',$3,$4,NULL) ON CONFLICT(topic,subject) DO UPDATE SET role='member',status=excluded.status,joined_at=excluded.joined_at",&[topic.as_str().into(),target.as_str().into(),next.into(),if next=="active"{now.as_str().into()}else{Value::Null}])?;
            (
                json!({"status":next,"role":"member"}),
                if next == "active" {
                    "member.join"
                } else {
                    "member.request"
                },
            )
        }
        "leave" => {
            require(
                matches!(status, Some("active" | "pending" | "invited")),
                "not_topic_member",
            )?;
            if role == Some("admin") && status == Some("active") {
                last_admin(ctx, &topic)?;
            }
            ctx.tx.execute("UPDATE topic_memberships SET role='member',status='left' WHERE topic=$1 AND subject=$2",&[topic.as_str().into(),target.as_str().into()])?;
            (json!({"status":"left"}), "member.leave")
        }
        "invite" => {
            ctx.tx.subject(&target)?;
            require(!topic_banned(ctx, &topic, &target)?, "topic_banned")?;
            require(
                !matches!(status, Some("active" | "pending" | "invited")),
                "already_topic_member",
            )?;
            ctx.tx.execute("INSERT INTO topic_memberships(topic,subject,role,status,joined_at,invited_by) VALUES($1,$2,'member','invited',NULL,$3) ON CONFLICT(topic,subject) DO UPDATE SET role='member',status='invited',joined_at=NULL,invited_by=excluded.invited_by",&[topic.as_str().into(),target.as_str().into(),subject.as_str().into()])?;
            (json!({"status":"invited"}), "member.invite")
        }
        "approve" => {
            require(status == Some("pending"), "topic_request_not_pending")?;
            require(!topic_banned(ctx, &topic, &target)?, "topic_banned")?;
            ctx.tx.execute("UPDATE topic_memberships SET status='active',joined_at=$1 WHERE topic=$2 AND subject=$3",&[now.into(),topic.as_str().into(),target.as_str().into()])?;
            (json!({"status":"active"}), "member.approve")
        }
        "remove" | "promote" | "demote" => {
            require(status == Some("active"), "not_topic_member")?;
            if matches!(action.as_str(), "remove" | "demote") && role == Some("admin") {
                last_admin(ctx, &topic)?;
            }
            if action == "remove" {
                ctx.tx.execute("UPDATE topic_memberships SET role='member',status='removed' WHERE topic=$1 AND subject=$2",&[topic.as_str().into(),target.as_str().into()])?;
                (json!({"status":"removed"}), "member.remove")
            } else {
                let next = if action == "promote" {
                    "admin"
                } else {
                    "member"
                };
                require(role != Some(next), "topic_role_unchanged")?;
                ctx.tx.execute(
                    "UPDATE topic_memberships SET role=$1 WHERE topic=$2 AND subject=$3",
                    &[next.into(), topic.as_str().into(), target.as_str().into()],
                )?;
                (
                    json!({"role":next}),
                    if action == "promote" {
                        "member.promote"
                    } else {
                        "member.demote"
                    },
                )
            }
        }
        "ban" => {
            ctx.tx.subject(&target)?;
            if role == Some("admin") && status == Some("active") {
                last_admin(ctx, &topic)?;
            }
            let expires = argument(ctx, "expires_at")?;
            if let Some(expires) = expires.as_deref().filter(|v| !v.is_empty()) {
                require(Timestamp::parse(expires)? > ctx.now, "topic_ban_expired")?;
            }
            ctx.tx.execute("INSERT INTO topic_bans(topic,subject,actor,created_at,expires_at,reason,status) VALUES($1,$2,$3,$4,$5,$6,'active') ON CONFLICT(topic,subject) DO UPDATE SET actor=excluded.actor,created_at=excluded.created_at,expires_at=excluded.expires_at,reason=excluded.reason,status='active'",&[topic.as_str().into(),target.as_str().into(),subject.as_str().into(),now.into(),expires.into(),argument(ctx,"reason")?.into()])?;
            if previous.is_some() {
                ctx.tx.execute("UPDATE topic_memberships SET role='member',status='removed' WHERE topic=$1 AND subject=$2",&[topic.as_str().into(),target.as_str().into()])?;
            }
            (json!({"banned_subject":target}), "member.ban")
        }
        "unban" => {
            let changed=ctx.tx.execute("UPDATE topic_bans SET status='lifted' WHERE topic=$1 AND subject=$2 AND status='active'",&[topic.as_str().into(),target.as_str().into()])?;
            require(changed == 1, "topic_ban_not_found")?;
            (
                json!({"unbanned_subject":target,"membership_restored":false}),
                "member.unban",
            )
        }
        "policy_set" => {
            let policy = ctx.text("membership_policy")?;
            ctx.tx.execute("INSERT INTO topic_settings(topic,membership_policy) VALUES($1,$2) ON CONFLICT(topic) DO UPDATE SET membership_policy=excluded.membership_policy",&[topic.as_str().into(),policy.into()])?;
            (json!({"membership_policy":policy}), "policy.change")
        }
        _ => return Err(Error("unknown_operation")),
    };
    governance_event(
        ctx,
        &topic,
        event,
        Some(&target),
        argument(ctx, "reason")?.as_deref(),
    )?;
    data["topic_id"] = json!(topic);
    output(
        data,
        vec![ResourceRef {
            id: topic,
            revision: None,
        }],
    )
}
fn topic_members(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let topic = ctx.resolve(ctx.text("id")?, false)?;
    ctx.check(&topic, Check::Read)?;
    require(
        ctx.tx.resource_record(&topic)?.resource_type == "topic",
        "not_a_topic",
    )?;
    let mut items = Vec::new();
    for row in ctx.tx.rows("SELECT subject,role,status,joined_at FROM topic_memberships WHERE topic=$1 AND status='active' ORDER BY subject",&[topic.as_str().into()])?{items.push(json!({"subject_id":row.text(0)?,"role":row.text(1)?,"status":row.text(2)?,"joined_at":row.optional_text(3)?}));}
    let policy = ctx.tx.one(
        "SELECT membership_policy FROM topic_settings WHERE topic=$1",
        &[topic.as_str().into()],
    )?;
    output(
        json!({"topic_id":topic,"membership_policy":policy.as_ref().map(|r|r.text(0)).transpose()?.unwrap_or("open"),"items":items}),
        vec![],
    )
}
fn event_codes() -> J {
    json!({"topic.create":"tc","topic.member.join":"mj","topic.member.request":"mr","topic.member.leave":"ml","topic.member.invite":"mi","topic.member.approve":"ma","topic.member.remove":"mx","topic.member.promote":"mp","topic.member.demote":"md","topic.member.ban":"mb","topic.member.unban":"mu","topic.policy.change":"pc","topic.archive":"ta","topic.restore":"tr","topic.move":"tm","topic.chmod":"tmo","topic.chgrp":"tgr","topic.chown":"tow","topic.configure":"tcf"})
}
fn topic_events(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let topic = ctx.resolve(ctx.text("id")?, false)?;
    ctx.check(&topic, Check::Read)?;
    require(
        ctx.tx.resource_record(&topic)?.resource_type == "topic",
        "not_a_topic",
    )?;
    let view = argument(ctx, "view")?.unwrap_or("compact".into());
    let admin = is_admin(ctx, &topic)?;
    let binding = digest(
        encode(
            &json!({"topic":topic,"view":view,"subject":ctx.principal.subject(),"admin":admin}),
        )?
        .canonical()?
        .as_bytes(),
    );
    let position = if let Some(cursor) = argument(ctx, "cursor")? {
        let cursor = ctx.decode_cursor(&cursor)?;
        let c = cursor.as_object()?;
        require(
            c.get("kind").and_then(|v| v.as_str().ok()) == Some("topic_events")
                && c.get("query").and_then(|v| v.as_str().ok()) == Some(&binding),
            "cursor_query_mismatch",
        )?;
        c.get("position")
            .ok_or(Error("invalid_cursor"))?
            .as_integer()?
            .parse::<i64>()
            .map_err(|_| Error("invalid_cursor"))?
    } else {
        i64::MAX
    };
    let limit = ctx
        .args()?
        .get("limit")
        .map(|v| {
            v.as_integer()?
                .parse::<u32>()
                .map_err(|_| Error("invalid_limit"))
        })
        .transpose()?
        .unwrap_or(10);
    require((1..=50).contains(&limit), "invalid_limit")?;
    let rows=ctx.tx.rows("SELECT p.seq,e.body FROM (SELECT seq FROM topic_event_projection WHERE topic=$1 AND seq<$2 ORDER BY seq DESC LIMIT $3) p JOIN events e ON e.seq=p.seq ORDER BY p.seq DESC",&[topic.as_str().into(),position.into(),(limit+1).into()])?;
    let mut items = Vec::new();
    let mut base = J::Null;
    let codes = event_codes();
    for row in rows.iter().take(limit as usize) {
        let event: Event = decode(&row.json(1)?)?;
        if base.is_null() {
            base = json!(event.time);
        }
        let mut data = event.data.clone();
        if !admin {
            data.remove("reason");
        }
        let target = data
            .get("target_subject")
            .filter(|v| !v.is_null())
            .map(Json::as_str)
            .transpose()?;
        let item = match view.as_str() {
            "compact" => {
                json!({"s":row.integer(0)?,"c":codes.get(&event.event_type).ok_or(Error("invalid_topic_event"))?,"a":event.actor,"u":target,"t":event.time})
            }
            "normal" => {
                let mut line = format!(
                    "{} {} {} {}",
                    encode(&event.time)?.as_str()?,
                    event.actor.as_deref().unwrap_or("None"),
                    data.get("action")
                        .ok_or(Error("invalid_topic_event"))?
                        .as_str()?,
                    target.unwrap_or("")
                );
                if let Some(reason) = data
                    .get("reason")
                    .map(Json::as_str)
                    .transpose()?
                    .filter(|s| !s.is_empty())
                {
                    line.push_str(" — ");
                    line.push_str(reason);
                }
                json!({"seq":row.integer(0)?,"text":line})
            }
            "proof" => {
                let mut proof =
                    serde_json::to_value(&event).map_err(|_| Error("invalid_topic_event"))?;
                proof["data"] =
                    serde_json::to_value(&data).map_err(|_| Error("invalid_topic_event"))?;
                if let Some(result) = ctx.tx.one(
                    "SELECT body FROM results WHERE subject=$1 AND request_id=$2",
                    &[
                        event.subject.as_deref().into(),
                        event.request_id.as_str().into(),
                    ],
                )? {
                    proof["receipt"] =
                        serde_json::to_value(result.json(0)?.as_object()?.get("receipt"))
                            .map_err(|_| Error("invalid_topic_event"))?;
                }
                json!({"seq":row.integer(0)?,"event":proof})
            }
            _ => return Err(Error("invalid_view")),
        };
        items.push(item);
    }
    let mut data = json!({"topic_id":topic,"view":view,"items":items});
    if view == "compact" {
        data["schema_version"] = json!(1);
        data["event_codes"] = codes;
        data["base_time"] = base;
    }
    if rows.len() > limit as usize {
        let cursor=ctx.encode_cursor(&encode(&json!({"v":1,"kind":"topic_events","query":binding,"position":rows[limit as usize-1].integer(0)?}))?)?;
        let mut args = serde_json::to_value(ctx.args()?).map_err(|_| Error("invalid_cursor"))?;
        args["cursor"] = json!(cursor);
        data["cursor"] = json!(cursor);
        data["next"] = json!(crate::discovery::next_link(
            ctx,
            "content.topic_events",
            &args
        )?);
        data["next_requires_auth"] = json!(ctx.principal.subject().is_some());
    }
    output(data, vec![])
}

fn patch_text<'a>(args: &'a BTreeMap<String, Json>, name: &str) -> Result<&'a str> {
    args.get(name).ok_or(Error("missing_argument"))?.as_str()
}
fn patch_optional(args: &BTreeMap<String, Json>, name: &str) -> Result<Option<String>> {
    args.get(name)
        .map(|v| v.as_str().map(str::to_owned))
        .transpose()
}
struct PreparedPatch {
    resource: Resource,
    body: Body,
    options: RevisionOptions,
}
fn prepare_patch(
    ctx: &mut Context<'_>,
    args: &BTreeMap<String, Json>,
    post_only: bool,
) -> Result<PreparedPatch> {
    let id = ctx.resolve(patch_text(args, "id")?, false)?;
    let resource = ctx.tx.resource_record(&id)?;
    require(
        matches!(resource.resource_type.as_str(), "post" | "file")
            && resource.state == ResourceState::Active,
        "not_editable",
    )?;
    if post_only {
        require(resource.resource_type == "post", "not_editable")?;
    }
    let presented = presentation(ctx, &resource)?;
    let ancestors = ctx.tx.ancestors(&id)?;
    require(
        presented || !(id == "t_last_will" || ancestors.iter().any(|r| r.id == "t_last_will")),
        "legacy_directive_only",
    )?;
    require(
        id != "r_agents" && id != "r_rules" && !ancestors.iter().any(|r| r.id == "r_rules"),
        "system_managed_resource",
    )?;
    unmanaged(ctx, &resource)?;
    ctx.check(&id, Check::Write)?;
    assert_generation(ctx, &resource)?;
    if !presented {
        editable(ctx, &resource)?;
    }
    let old = ctx.tx.revision(&reference(&resource))?;
    require(
        matches!(
            old.content.media_type.as_str(),
            "text/plain" | "text/markdown" | "image/svg+xml"
        ),
        "text_patch_required",
    )?;
    let structured = args.get("patch");
    if let Some(patch) = structured {
        patch::validate(patch)?;
    } else {
        let size = patch_text(args, "exact")?.len()
            + patch_text(args, "replacement")?.len()
            + patch_optional(args, "before")?
                .map(|v| v.len())
                .unwrap_or(0)
            + patch_optional(args, "after")?.map(|v| v.len()).unwrap_or(0);
        require(size <= patch::LIMIT, "patch_too_large")?;
    }
    require(old.content.size <= patch::LIMIT as i64, "patch_too_large")?;
    let source = String::from_utf8(ctx.contents.read_bytes(&old.content, patch::LIMIT as u64)?)
        .map_err(|_| Error("invalid_utf8"))?;
    let base_id = patch_text(args, "base_revision")?;
    let base_generation = args
        .get("base_generation")
        .map(|v| {
            v.as_integer()?
                .parse::<i64>()
                .map_err(|_| Error("invalid_generation"))
        })
        .transpose()?;
    let base = if old.id != base_id {
        require(
            args.get("rebase").is_some_and(|v| v.as_bool() == Ok(true)),
            "revision_conflict",
        )?;
        let base_generation = base_generation.ok_or(Error("base_generation_required"))?;
        require(
            base_generation < resource.generation,
            "base_generation_conflict",
        )?;
        let base = ctx.tx.revision(&ResourceRef {
            id: id.clone(),
            revision: Some(base_id.into()),
        })?;
        let mut ancestor = old.clone();
        let mut seen = BTreeSet::new();
        while ancestor.id != base.id {
            require(seen.len() < 4096, "patch_too_complex")?;
            require(
                seen.insert(ancestor.id.clone()) && ancestor.parents.len() == 1,
                "revision_conflict",
            )?;
            ancestor = ctx.tx.revision(&ResourceRef {
                id: id.clone(),
                revision: Some(ancestor.parents[0].clone()),
            })?;
        }
        require(
            base.content.media_type == old.content.media_type
                && base.content.size <= patch::LIMIT as i64,
            "text_patch_required",
        )?;
        let base = String::from_utf8(
            ctx.contents
                .read_bytes(&base.content, patch::LIMIT as u64)?,
        )
        .map_err(|_| Error("invalid_utf8"))?;
        if structured.is_none() {
            patch::exact(
                &base,
                patch_text(args, "exact")?,
                patch_text(args, "replacement")?,
                patch_optional(args, "before")?.as_deref().unwrap_or(""),
                patch_optional(args, "after")?.as_deref().unwrap_or(""),
            )?;
        }
        Some(base)
    } else {
        require(
            base_generation.is_none_or(|v| v == resource.generation),
            "base_generation_conflict",
        )?;
        None
    };
    let body = if let Some(patch) = structured {
        patch::apply(&source, patch, base.as_deref())?
    } else {
        patch::exact(
            &source,
            patch_text(args, "exact")?,
            patch_text(args, "replacement")?,
            patch_optional(args, "before")?.as_deref().unwrap_or(""),
            patch_optional(args, "after")?.as_deref().unwrap_or(""),
        )?
    };
    let mut options = RevisionOptions {
        relations: old.relations,
        author: Some(old.author),
        summary: old.summary,
        ..RevisionOptions::default()
    };
    if let Some(patch) = structured {
        options.change_note = patch_optional(args, "change_note")?;
        options.source_kind = Some(SourceKind::User);
        options.source_version = Some(1);
        options.source_digest = Some(digest(patch.canonical()?.as_bytes()));
        options.signature = args.get("content_signature").map(decode).transpose()?;
        options.id = patch_optional(args, "revision_id")?;
        options.content_created_at = patch_optional(args, "content_created_at")?;
    }
    Ok(PreparedPatch {
        resource,
        body: Body::Bytes(body.into_bytes(), old.content.media_type),
        options,
    })
}
fn text_patch(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let args = ctx.args()?.clone();
    let post = ctx.request.field("operation")?.as_str()? == "content.post_patch";
    let prepared = prepare_patch(ctx, &args, post)?;
    let updated = revise(ctx, prepared.resource, prepared.body, prepared.options)?;
    result(&updated, json!({}))
}
fn text_patch_batch(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let items = ctx.arg("patches")?.as_array()?.to_vec();
    require((1..=16).contains(&items.len()), "invalid_patch_batch")?;
    let mut targets = BTreeSet::new();
    let mut prepared = Vec::new();
    for item in items {
        let args = item.as_object()?;
        let id = ctx.resolve(patch_text(args, "id")?, false)?;
        require(targets.insert(id), "duplicate_patch_target")?;
        prepared.push(prepare_patch(ctx, args, false)?);
    }
    // No blob or Revision is published until every target, precondition and
    // exact edit has validated. A later signature failure rolls back the unit.
    let mut updated = Vec::new();
    for prepared in prepared {
        updated.push(revise(
            ctx,
            prepared.resource,
            prepared.body,
            prepared.options,
        )?);
    }
    let generations: BTreeMap<_, _> = updated
        .iter()
        .map(|r| (r.id.clone(), r.generation))
        .collect();
    let mut data = json!({"generations":generations});
    if ctx.request.field("contract_version")?.as_integer()? == "2" {
        data["items"] = json!(updated
            .iter()
            .map(|r| json!({"id":r.id,"revision":r.revision,"generation":r.generation}))
            .collect::<Vec<_>>());
    }
    output(data, updated.iter().map(reference).collect())
}

#[cfg(test)]
mod tests;
