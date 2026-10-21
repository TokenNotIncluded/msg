//! Resource and immutable Revision handlers. The executor owns authorization,
//! request replay and the durable metadata/content transaction.
use crate::context::{new_id, output, Context, HandlerOutput};
use msg_core::{digest, unb64, Error, Json, Result};
use msg_crypto::{verify, Signature};
use msg_identity::{
    authorization::{AccessRequirement, Check},
    models::{CredentialKind, ResourceRef, SubjectKind, Timestamp},
    store::{truthy, AuthorityStore},
};
use msg_storage::{
    postgres::Value,
    records::{
        decode, encode, BlobRef, Event, Relation, Resource, ResourceState, Revision,
        SignatureSource, SourceKind,
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
        | "content.template_put" => version == 1,
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
        | "content.post_rollback" => ("id", Check::Write),
        "content.topic_configure" => ("id", Check::Manage),
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
        "content.file_put" => file_put(ctx),
        "content.attach" => attach(ctx),
        "content.template_put" => template_put(ctx),
        "content.topic_configure" => topic_configure(ctx),
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
        result.insert(tag);
    }
    Ok(result.into_iter().collect())
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

enum Body {
    Bytes(Vec<u8>, String),
    Existing(BlobRef),
}
#[derive(Default)]
struct RevisionOptions {
    relations: Vec<Relation>,
    author: Option<String>,
    signature: Option<Signature>,
    id: Option<String>,
    summary: Option<String>,
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
struct Create {
    parent: String,
    kind: &'static str,
    name: Option<String>,
    id: Option<String>,
    body: Option<Body>,
    mode: Option<u16>,
    revision: RevisionOptions,
}
fn create(ctx: &mut Context<'_>, input: Create) -> Result<Resource> {
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
    require(
        parent.id != "t_store" || presentation,
        "store_controlled_resource",
    )?;
    require(
        parent.id != "t_last_will" || presentation,
        "legacy_directive_only",
    )?;
    let mut name = input.name.unwrap_or(new_id("p")?);
    if input.kind == "post" && !name.ends_with(".md") {
        name.push_str(".md");
    }
    validate_name(&name)?;
    require(
        !(parent.resource_type == "user"
            && matches!(name.as_str(), "SOUL.md" | "AGENTS.md" | "notes" | "todos")),
        "personal_managed_resource",
    )?;
    if matches!(parent.name.as_str(), "notes" | "todos") {
        if let Some(id) = &parent.parent {
            require(
                ctx.tx.resource_record(id)?.resource_type != "user",
                "personal_managed_resource",
            )?;
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
fn revise(
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
    let timestamp = argument(ctx, "content_created_at")?;
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
        require(
            (time.0 - ctx.now.0).num_milliseconds().abs() <= 300_000,
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
        change_note: None,
        source_kind: custodial.then_some(SourceKind::Operation),
        source_version: if custodial {
            Some(
                ctx.request
                    .field("contract_version")?
                    .as_integer()?
                    .parse()
                    .map_err(|_| Error("invalid_version"))?,
            )
        } else {
            None
        },
        source_digest: if custodial {
            Some(ctx.request.payload_digest()?)
        } else {
            None
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

#[cfg(test)]
mod tests;
