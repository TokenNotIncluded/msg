//! Bounded read projections. Every page and relation is checked against the
//! current transaction; a signed cursor is a position, never authority.
use crate::context::{output, Context, HandlerOutput};
use chrono::Duration;
use msg_core::{b64, digest, Error, Json, Result};
use msg_identity::{
    authorization::{AccessRequirement, Check},
    models::{Credential, CredentialKind, ResourceRef, Timestamp},
    store::AuthorityStore,
};
use msg_storage::{
    postgres::Value,
    records::{decode, encode, Relation, Resource, ResourceState, Revision},
};
use serde_json::{json, Map, Value as J};
use std::collections::BTreeMap;
use url::Url;

mod diff;
pub(crate) mod follows;
mod public;
mod search;
mod text;

pub fn supports(op: &str, version: u32) -> bool {
    match op {
        "discovery.read_query" => matches!(version, 1..=5),
        "discovery.lexical_search" => matches!(version, 1..=5),
        "discovery.public_board"
        | "discovery.get"
        | "discovery.raw"
        | "discovery.resolve"
        | "discovery.list"
        | "discovery.search"
        | "discovery.read_segment"
        | "discovery.references"
        | "discovery.links"
        | "discovery.operations"
        | "discovery.capabilities"
        | "discovery.schema" => version == 1,
        "discovery.feed"
        | "discovery.now"
        | "discovery.recommendations"
        | "discovery.grep"
        | "discovery.diff"
        | "discovery.diff_view" => version == 1,
        _ => false,
    }
}
pub fn requirements(_: &Context<'_>) -> Result<Vec<AccessRequirement>> {
    Ok(vec![])
}
pub fn handle(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    ctx.check_deadline()?;
    match ctx.request.field("operation")?.as_str()? {
        "discovery.get" => get(ctx),
        "discovery.public_board" => crate::content::public_board::get(ctx),
        "discovery.feed" => public::feed(ctx),
        "discovery.now" => public::now(ctx),
        "discovery.recommendations" => public::recommendations(ctx),
        "discovery.grep" => text::grep(ctx),
        "discovery.lexical_search" => search::handle(ctx),
        "discovery.diff" => diff::legacy(ctx),
        "discovery.diff_view" => diff::view(ctx),
        "discovery.raw" => raw(ctx),
        "discovery.resolve" => resolve_address(ctx),
        "discovery.list" | "discovery.search" => list(ctx),
        "discovery.read_query" => read_query(ctx),
        "discovery.read_segment" => read_segment(ctx),
        "discovery.references" => references(ctx),
        "discovery.links" => links(ctx),
        "discovery.operations" => catalog(ctx),
        "discovery.capabilities" => capabilities(ctx),
        "discovery.schema" => schema(ctx),
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
fn value<T: serde::Serialize>(input: &T) -> Result<J> {
    serde_json::to_value(input).map_err(|_| Error("invalid_projection"))
}
fn hashed(input: &J) -> Result<String> {
    Ok(digest(encode(input)?.canonical()?.as_bytes()))
}
fn object(input: J) -> Result<Map<String, J>> {
    input
        .as_object()
        .cloned()
        .ok_or(Error("invalid_projection"))
}
fn arg_text(args: &BTreeMap<String, Json>, name: &str) -> Result<Option<String>> {
    args.get(name)
        .map(|v| v.as_str().map(str::to_owned))
        .transpose()
}
fn arg_int(args: &BTreeMap<String, Json>, name: &str, default: i64) -> Result<i64> {
    args.get(name)
        .map(|v| {
            v.as_integer()?
                .parse()
                .map_err(|_| Error("invalid_integer"))
        })
        .transpose()
        .map(|v| v.unwrap_or(default))
}
fn fields(args: &BTreeMap<String, Json>) -> Result<Vec<String>> {
    args.get("fields")
        .map(|v| {
            v.as_array()?
                .iter()
                .map(|v| v.as_str().map(str::to_owned))
                .collect()
        })
        .transpose()
        .map(|v| v.unwrap_or_default())
}
fn ref_for(resource: &Resource) -> ResourceRef {
    ResourceRef {
        id: resource.id.clone(),
        revision: resource.revision.clone(),
    }
}
fn resolve_read(ctx: &Context<'_>, reference: &str) -> Result<String> {
    match ctx.resolve(reference, false) {
        Err(Error("not_found")) if reference.starts_with('/') => ctx.resolve(reference, true),
        other => other,
    }
}
pub(crate) fn visible(ctx: &mut Context<'_>, id: &str) -> Result<bool> {
    ctx.check_deadline()?;
    match ctx.check(id, Check::Read) {
        Ok(()) => Ok(true),
        Err(Error(
            "permission_denied"
            | "credential_ceiling"
            | "certificate_gate"
            | "tool_certificate_required"
            | "delegation_scope"
            | "ancestor_inactive",
        )) => Ok(false),
        Err(e) => Err(e),
    }
}
fn stable_path(reference: &ResourceRef) -> String {
    match &reference.revision {
        Some(revision) => format!("/_r/{}/rev/{revision}", reference.id),
        None => format!("/_r/{}/json", reference.id),
    }
}
fn address(ctx: &Context<'_>, reference: &ResourceRef) -> J {
    let path = stable_path(reference);
    json!({"service":ctx.settings.service_url.trim_end_matches('/'),"ref":reference,"path":path,"url":format!("{}{}",ctx.settings.service_url.trim_end_matches('/'),path)})
}
fn short_path(path: &str) -> String {
    let mut parts: Vec<&str> = path.split('/').collect();
    if parts.len() >= 3 && parts[1].starts_with('@') {
        parts[2] = match parts[2] {
            "keys" => "k",
            "certificates" => "cert",
            "keystore" => "ks",
            other => other,
        };
    }
    parts.join("/")
}
fn principal(ctx: &Context<'_>) -> J {
    json!({"actor":ctx.principal.actor(),"subject":ctx.principal.subject(),"credential_id":ctx.principal.credential_id()})
}
fn compact(value: &mut J) {
    match value {
        J::Object(map) => {
            map.retain(|_, v| !v.is_null());
            for v in map.values_mut() {
                compact(v)
            }
        }
        J::Array(values) => {
            for v in values {
                compact(v)
            }
        }
        _ => (),
    }
}
pub(crate) fn metadata(ctx: &Context<'_>, resource: &Resource) -> Result<J> {
    let mut data = object(value(resource)?)?;
    if resource.resource_type == "post" {
        data.insert(
            "view_count".into(),
            ctx.tx
                .setting(&format!("post_view_count:{}", resource.id))?
                .map(|v| value(&v))
                .transpose()?
                .unwrap_or(json!(0)),
        );
    }
    data.insert(
        "path".into(),
        json!(short_path(&ctx.tx.resource_path(&resource.id)?)),
    );
    data.insert(
        "stable_path".into(),
        json!(stable_path(&ResourceRef {
            id: resource.id.clone(),
            revision: None
        })),
    );
    if resource.revision.is_some() {
        data.insert(
            "revision_path".into(),
            json!(stable_path(&ref_for(resource))),
        );
        let revision = ctx.tx.revision(&ref_for(resource))?;
        data.extend(object(json!({"size":revision.content.size,"media_type":revision.content.media_type,"digest":revision.content.digest}))?);
        let revision = value(&revision)?;
        for field in [
            "summary",
            "change_note",
            "source_kind",
            "source_version",
            "source_digest",
        ] {
            if let Some(v) = revision.get(field).filter(|v| !v.is_null()) {
                data.insert(field.into(), v.clone());
            }
        }
    }
    Ok(J::Object(data))
}
fn pick(data: J, fields: &[String]) -> Result<J> {
    if fields.is_empty() {
        return Ok(data);
    }
    let data = object(data)?;
    let mut chosen = Map::new();
    for name in fields {
        chosen.insert(
            name.clone(),
            data.get(name)
                .ok_or(Error("unknown_projection_field"))?
                .clone(),
        );
    }
    Ok(J::Object(chosen))
}
pub(crate) fn next_link(ctx: &Context<'_>, operation: &str, args: &J) -> Result<String> {
    let identity = json!([operation, args]);
    let request_id = format!("read_{}", &hashed(&identity)?[7..39]);
    let mut packet = json!({"request_id":request_id,"protocol_version":1,"operation":operation,"contract_version":1,
        "target_service":ctx.settings.service_url,"subject":null,"arguments":args,"expected_generations":[],"expires_at":null,
        "payload_digest":"","proof":null,"return_fields":[],"source":"manual"});
    let mut payload = object(packet.clone())?;
    for key in ["proof", "payload_digest", "source", "expires_at"] {
        payload.remove(key);
    }
    packet["payload_digest"] = json!(hashed(&J::Object(payload))?);
    Ok(format!(
        "/-/g/{operation}/j/{}",
        b64(encode(&packet)?.canonical()?.as_bytes())
    ))
}
fn inspect_page(ctx: &Context<'_>, cursor: &str) -> Result<(J, J)> {
    let data = value(&ctx.decode_cursor(cursor)?)?;
    require(data["kind"] == "read-page", "cursor_kind_mismatch")?;
    let query = data.get("query").ok_or(Error("invalid_cursor"))?.clone();
    let position = data
        .get("position")
        .filter(|v| v.is_object())
        .ok_or(Error("invalid_cursor"))?
        .clone();
    require(
        position["query_digest"] == hashed(&query)?,
        "invalid_cursor",
    )?;
    let expiry = Timestamp::parse(
        position["expires_at"]
            .as_str()
            .ok_or(Error("invalid_cursor"))?,
    )?;
    require(ctx.now < expiry, "cursor_expired")?;
    Ok((query, position))
}
fn decode_page(
    ctx: &Context<'_>,
    cursor: &str,
    operation: &str,
    args: &J,
) -> Result<(J, Timestamp)> {
    let (query, position) = inspect_page(ctx, cursor)?;
    require(
        query == json!({"operation":operation,"arguments":args}),
        "cursor_query_mismatch",
    )?;
    require(
        position["principal"] == principal(ctx),
        "cursor_principal_mismatch",
    )?;
    let snapshot = Timestamp::parse(
        position["snapshot"]
            .as_str()
            .ok_or(Error("invalid_cursor"))?,
    )?;
    Ok((
        position.get("last").ok_or(Error("invalid_cursor"))?.clone(),
        snapshot,
    ))
}
fn encode_page(
    ctx: &Context<'_>,
    operation: &str,
    args: &J,
    last: &J,
    snapshot: Timestamp,
) -> Result<String> {
    let query = json!({"operation":operation,"arguments":args});
    ctx.encode_cursor(&encode(&json!({"v":1,"kind":"read-page","query":query,"position":{"last":last,"snapshot":snapshot,"principal":principal(ctx),"expires_at":Timestamp(ctx.now.0+Duration::minutes(15)),"query_digest":hashed(&query)?}}))?)
}
fn filtered_tools(ctx: &mut Context<'_>) -> Result<J> {
    let mut result = Vec::new();
    for resource in ctx.tx.children("t_tools", None, 500)?.items {
        if ctx.authorizer.has(
            ctx.principal,
            "tool.use",
            &ctx.operation()?,
            &resource.id,
            ctx.tx,
            ctx.now,
        )? {
            result.push(json!({"id":resource.id,"name":resource.name,"path":ctx.tx.resource_path(&resource.id)?,"revision":resource.revision}));
        }
    }
    require(!result.is_empty(), "tool_certificate_required")?;
    Ok(json!(result))
}
pub(crate) fn read_projection(
    ctx: &mut Context<'_>,
    id: &str,
    revision: Option<&str>,
    chosen: &[String],
) -> Result<J> {
    let resource = ctx.tx.resource_record(id)?;
    if id == "t_tools" {
        return Ok(
            json!({"id":id,"type":"topic","stable_path":stable_path(&ResourceRef{id:id.into(),revision:None}),"items":filtered_tools(ctx)?}),
        );
    }
    ctx.check(id, Check::Read)?;
    require(resource.state != ResourceState::Purged, "resource_purged")?;
    let mut meta = object(metadata(ctx, &resource)?)?;
    if matches!(resource.resource_type.as_str(), "post" | "topic")
        && (id == "t_wiki" || ctx.tx.ancestors(id)?.iter().any(|a| a.id == "t_wiki"))
    {
        meta.insert(
            "wiki".into(),
            json!({"public":true,"shared_edit":true,"history_retained":true}),
        );
    }
    if id == "t_capabilities" {
        return capability_data(ctx);
    }
    if id == "t_operations" {
        return ctx.catalog.catalog();
    }
    if resource.resource_type == "certificate" {
        return Ok(
            json!({"metadata":meta,"certificate":ctx.tx.certificate(id)?,"revoked":ctx.tx.certificate_revoked(id)?}),
        );
    }
    if resource.resource_type == "csr" {
        let row = ctx
            .tx
            .one("SELECT body,state_body FROM csrs WHERE id=$1", &[id.into()])?
            .ok_or(Error("not_found"))?;
        return Ok(json!({"metadata":meta,"request":row.json(0)?,"state":row.json(1)?}));
    }
    if resource.resource_type == "topic" {
        meta.insert("board_rules".into(), board_rules(ctx, &resource)?);
        if !ctx.tx.ancestors(id)?.iter().any(|a| a.kind == "user")
            && ctx
                .tx
                .one(
                    "SELECT resource_id FROM dm_conversations WHERE resource_id=$1",
                    &[id.into()],
                )?
                .is_none()
        {
            meta.insert("presentation".into(), board_presentation(ctx, &resource)?);
        }
    }
    if resource.resource_type == "user" {
        let subject = ctx.tx.subject(id)?;
        meta.insert("kind".into(), value(&subject.kind)?);
        meta.insert("local_only".into(), json!(subject.local_only));
        if chosen.is_empty() || chosen.iter().any(|s| s == "artwork") {
            meta.insert("artwork".into(),json!({"avatar":{"url":format!("{}/art/avatar.svg",quote_path(&format!("/{}",resource.name)))}}));
        }
        if ctx.principal.subject() == Some(id)
            && (chosen.is_empty() || chosen.iter().any(|s| s == "groups"))
        {
            let mut groups = Vec::new();
            for row in ctx.tx.rows(
                "SELECT org,body FROM memberships WHERE subject=$1 ORDER BY org",
                &[id.into()],
            )? {
                let membership: msg_storage::records::Membership = decode(&row.json(1)?)?;
                if membership.status == "active" && visible(ctx, row.text(0)?)? {
                    let group = ctx.tx.resource_record(row.text(0)?)?;
                    groups.push(json!({"name":group.name,"path":ctx.tx.resource_path(&group.id)?}));
                }
            }
            meta.insert("groups".into(), json!(groups));
        }
    }
    if matches!(resource.name.as_str(), "keys" | "certificates")
        && resource
            .parent
            .as_ref()
            .map(|p| ctx.tx.resource_record(p).map(|r| r.resource_type == "user"))
            .transpose()?
            .unwrap_or(false)
    {
        let parent = resource.parent.as_deref().ok_or(Error("not_found"))?;
        if resource.name == "keys" {
            let mut keys = Vec::new();
            for row in ctx.tx.rows(
                "SELECT body FROM credentials WHERE subject=$1 ORDER BY id",
                &[parent.into()],
            )? {
                let c: Credential = decode(&row.json(0)?)?;
                if c.kind != CredentialKind::Token {
                    keys.push(json!({"key_id":c.id,"kind":c.kind,"public_key":b64(&c.verifier.0),"revoked":c.revoked_at.is_some()}));
                }
            }
            return Ok(json!({"id":id,"path":meta["path"],"keys":keys}));
        }
        let mut certificates = Vec::new();
        for row in ctx.tx.rows(
            "SELECT id,revoked FROM certificates WHERE subject=$1 ORDER BY id",
            &[parent.into()],
        )? {
            certificates.push(json!({"id":row.text(0)?,"revoked":match row.value(1)? {Value::Bool(v)=>*v,Value::Int(v)=>*v!=0,_=>return Err(Error("invalid_storage_record"))}}));
        }
        return Ok(json!({"id":id,"path":meta["path"],"certificates":certificates}));
    }
    if ctx
        .catalog
        .resource_type(&resource.resource_type, 1)?
        .container
        && !matches!(resource.resource_type.as_str(), "repo" | "website")
    {
        let mut items = Vec::new();
        let mut cursor = None;
        let mut scanned = 0;
        loop {
            let page = ctx.tx.children(id, cursor.as_deref(), 50)?;
            for child in page.items {
                scanned += 1;
                require(scanned <= 4096, "query_cost_exceeded")?;
                if child.state == ResourceState::Active && visible(ctx, &child.id)? {
                    items.push(json!({"id":child.id,"name":child.name,"type":child.resource_type,"revision":child.revision,"path":short_path(&ctx.tx.resource_path(&child.id)?)}));
                    if items.len() == 50 {
                        break;
                    }
                }
            }
            cursor = page.next_cursor;
            if items.len() == 50 || cursor.is_none() {
                break;
            }
        }
        meta.insert("items".into(), json!(items));
        meta.insert(
            "list_operation".into(),
            json!(next_link(ctx, "discovery.list", &json!({"parent":id}))?),
        );
    } else if resource.revision.is_some() {
        let rev = ctx.tx.revision(&ResourceRef {
            id: id.into(),
            revision: revision.map(str::to_owned),
        })?;
        meta.extend(object(json!({"revision_path":stable_path(&ResourceRef{id:id.into(),revision:Some(rev.id.clone())}),"revision":rev.id,"revision_created_at":rev.created_at,"digest":rev.content.digest,"size":rev.content.size,"media_type":rev.content.media_type}))?);
        if let Some(wiki) = meta.get_mut("wiki") {
            wiki["has_previous_revision"] = json!(!rev.parents.is_empty());
        }
        meta.remove("summary");
        if let Some(summary) = &rev.summary {
            meta.insert("summary".into(), json!(summary));
        }
        let textual = rev.content.media_type.starts_with("text/")
            || matches!(
                rev.content.media_type.as_str(),
                "application/json" | "application/msg-template"
            );
        if textual
            && rev.content.size >= 0
            && (rev.content.size as usize) < ctx.settings.max_response_bytes / 2
        {
            let bytes = ctx
                .contents
                .read_bytes(&rev.content, ctx.settings.max_response_bytes as u64)?;
            let text = std::str::from_utf8(&bytes).map_err(|_| Error("invalid_utf8"))?;
            meta.insert(
                "content".into(),
                if rev.content.media_type == "application/json" {
                    value(&Json::parse(text)?)?
                } else {
                    json!(text)
                },
            );
        } else {
            meta.insert(
                "raw_url".into(),
                json!(format!("/_id/{id}/revisions/{}/raw", rev.id)),
            );
            meta.insert("transfer_operation".into(), json!("transfer.open"));
        }
        let mut relations = Vec::new();
        for relation in &rev.relations {
            if !matches!(
                resource.resource_type.as_str(),
                "collab_request" | "collab_offer" | "checkpoint" | "collab_proposal"
            ) || visible(ctx, &relation.target.id)?
            {
                let mut relation = value(relation)?;
                compact(&mut relation);
                relations.push(relation);
            }
        }
        meta.insert("relations".into(), json!(relations));
        if resource.resource_type == "post" {
            if chosen.is_empty() || chosen.iter().any(|s| s == "links") {
                meta.insert("links".into(), basic_links(ctx, &resource, Some(&rev))?);
            }
            if chosen.is_empty()
                || chosen
                    .iter()
                    .any(|s| matches!(s.as_str(), "attachments" | "attachments_more"))
            {
                let (items, more) = attachments(ctx, &rev)?;
                meta.insert("attachments".into(), json!(items));
                meta.insert("attachments_more".into(), json!(more));
            }
        }
    }
    if !chosen.is_empty() {
        return pick(J::Object(meta), chosen);
    }
    const DEFAULTS: &[&str] = &[
        "id",
        "type",
        "name",
        "revision",
        "created_at",
        "modified_at",
        "view_count",
        "revision_created_at",
        "generation",
        "path",
        "stable_path",
        "revision_path",
        "content",
        "conversation",
        "items",
        "keys",
        "certificates",
        "links",
        "relations",
        "attachments",
        "attachments_more",
        "raw_url",
        "transfer_operation",
        "kind",
        "local_only",
        "profile",
        "artwork",
        "groups",
        "list_operation",
        "board_rules",
        "presentation",
        "summary",
        "change_note",
        "source_kind",
        "source_version",
        "source_digest",
    ];
    meta.retain(|k, _| {
        DEFAULTS.contains(&k.as_str()) || (k == "tags" && !resource.tags.is_empty())
    });
    Ok(J::Object(meta))
}

fn get(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let args = ctx.args()?.clone();
    let id = resolve_read(ctx, ctx.text("id")?)?;
    let chosen = fields(&args)?;
    let resource = ctx.tx.resource_record(&id)?;
    let mut data = match arg_text(&args, "view")?.as_deref() {
        Some("meta") => {
            ctx.check(&id, Check::Read)?;
            metadata(ctx, &resource)?
        }
        Some("history") => {
            ctx.check(&id, Check::Read)?;
            let limit = arg_int(&args, "limit", 50)?;
            require((1..=200).contains(&limit), "invalid_limit")?;
            let query = json!({"id":id,"view":"history","limit":limit});
            let (last, snapshot) = if let Some(cursor) = arg_text(&args, "cursor")? {
                decode_page(ctx, &cursor, "history", &query)?
            } else {
                (J::Null, ctx.now)
            };
            let page = ctx.tx.history(&id, last.as_str(), limit as u32)?;
            let mut items = Vec::new();
            for rev in page.items {
                let mut row = json!({"id":rev.id,"parents":rev.parents,"digest":rev.manifest_digest,"created_at":rev.created_at,"actor":rev.actor,"author":rev.author});
                if let Some(note) = rev.change_note {
                    row["change_note"] = json!(note)
                }
                if let Some(version) = rev.source_version {
                    row["source_version"] = json!(version)
                }
                items.push(row);
            }
            let mut data = json!({"id":id,"revisions":items});
            if let Some(last) = page.next_cursor {
                let cursor = encode_page(ctx, "history", &query, &json!(last), snapshot)?;
                let mut next = encode(&args)?.into_object()?;
                next.insert("cursor".into(), Json::string(&cursor));
                data["cursor"] = json!(cursor);
                data["next"] = json!(next_link(
                    ctx,
                    "discovery.get",
                    &value(&Json::from_object(next))?
                )?);
                data["next_requires_auth"] = json!(ctx.principal.subject().is_some());
            }
            data
        }
        _ if id == "r_root" && chosen.iter().any(|s| s == "channels") => {
            ctx.check(&id, Check::Read)?;
            let projection: Vec<_> = chosen
                .iter()
                .filter(|s| s.as_str() != "channels")
                .cloned()
                .collect();
            let mut data = if projection.is_empty() {
                json!({})
            } else {
                read_projection(ctx, &id, None, &projection)?
            };
            data["channels"] = public::home_summary(ctx, true)?["channels"].clone();
            data
        }
        _ => read_projection(ctx, &id, arg_text(&args, "revision")?.as_deref(), &chosen)?,
    };
    if chosen.is_empty() {
        data.as_object_mut()
            .ok_or(Error("invalid_projection"))?
            .entry("stable_path")
            .or_insert(json!(stable_path(&ResourceRef { id, revision: None })));
    }
    if let Some(known) = arg_text(&args, "known_digest")? {
        let mut small = data.clone();
        compact(&mut small);
        if known == hashed(&data)? || known == hashed(&small)? {
            return output(json!({"not_modified":true,"digest":known}), vec![]);
        }
    }
    output(data, vec![])
}
fn raw(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let id = resolve_read(ctx, ctx.text("id")?)?;
    ctx.check(&id, Check::Read)?;
    let resource = ctx.tx.resource_record(&id)?;
    require(resource.state != ResourceState::Purged, "resource_purged")?;
    let revision = ctx.tx.revision(&ResourceRef {
        id: id.clone(),
        revision: arg_text(ctx.args()?, "revision")?,
    })?;
    let offset = arg_int(ctx.args()?, "offset", 0)?;
    let length = arg_int(
        ctx.args()?,
        "length",
        revision.content.size.saturating_sub(offset),
    )?;
    let end = offset
        .checked_add(length)
        .ok_or(Error("invalid_byte_range"))?;
    require(
        0 <= offset && offset <= end && end <= revision.content.size,
        "invalid_byte_range",
    )?;
    let reference = ResourceRef {
        id,
        revision: Some(revision.id),
    };
    let mut result = output(
        json!({"content":revision.content,"range":[offset,end],"filename":resource.name}),
        vec![reference.clone()],
    )?;
    result.output = Some(reference);
    Ok(result)
}
fn parse_address(value: &str, service: &str) -> Result<(String, Option<String>)> {
    require(
        !value.is_empty()
            && value.len() <= 2048
            && !value.chars().any(|c| c < ' ' || c == '\u{7f}' || c == '\\'),
        "invalid_resource_address",
    )?;
    let absolute = value.contains("://");
    let path;
    let value = if absolute {
        require(
            !value.chars().any(char::is_whitespace),
            "invalid_resource_address",
        )?;
        let parsed = Url::parse(value).map_err(|_| Error("invalid_resource_address"))?;
        let local = Url::parse(service).map_err(|_| Error("invalid_service_origin"))?;
        require(
            parsed.query().is_none()
                && parsed.fragment().is_none()
                && parsed.username().is_empty()
                && parsed.password().is_none(),
            "invalid_resource_address",
        )?;
        require(parsed.origin() == local.origin(), "remote_resource_address")?;
        path = parsed.path().to_owned();
        let original = value
            .split_once("://")
            .and_then(|(_, v)| v.split_once('/'))
            .map(|(_, p)| format!("/{p}"))
            .unwrap_or("/".into());
        require(
            path == original && (path.starts_with("/_r/") || path.starts_with("/*")),
            "invalid_resource_address",
        )?;
        &path
    } else {
        value
    };
    if absolute || value.starts_with("/_r/") || value.starts_with("/*") {
        require(!value.contains(['?', '#', '%']), "invalid_resource_address")?;
        let rest = value
            .strip_prefix("/_r/")
            .or_else(|| value.strip_prefix("/*"))
            .ok_or(Error("invalid_resource_address"))?;
        let parts: Vec<&str> = rest.split('/').collect();
        let valid = |s: &str| {
            !s.is_empty()
                && s.len() <= 160
                && s.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_.:-".contains(&b))
        };
        require(valid(parts[0]), "invalid_resource_address")?;
        let revision = match parts.as_slice() {
            [_] => None,
            [_, "json" | "meta" | "raw" | "history"] => None,
            [_, "rev", rev] if valid(rev) => Some((*rev).into()),
            _ => return Err(Error("invalid_resource_address")),
        };
        return Ok((parts[0].into(), revision));
    }
    Ok((value.into(), None))
}
fn resolve_address(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let (target, pinned) = parse_address(ctx.text("address")?, &ctx.settings.service_url)?;
    let explicit = arg_text(ctx.args()?, "revision")?;
    require(
        pinned.is_none() || explicit.is_none() || pinned == explicit,
        "revision_mismatch",
    )?;
    let id = resolve_read(ctx, &target)?;
    ctx.check(&id, Check::Read)?;
    let resource = ctx.tx.resource_record(&id)?;
    require(resource.state != ResourceState::Purged, "resource_purged")?;
    let revision = pinned
        .or(explicit)
        .map(|rev| {
            ctx.tx
                .revision(&ResourceRef {
                    id: id.clone(),
                    revision: Some(rev),
                })
                .map(|r| r.id)
        })
        .transpose()?;
    let reference = ResourceRef {
        id: id.clone(),
        revision,
    };
    let mut data = address(ctx, &reference);
    data["stable_path"] = json!(stable_path(&reference));
    data["path"] = json!(ctx.tx.resource_path(&id)?);
    data["current"] = address(ctx, &ref_for(&resource));
    output(data, vec![reference])
}

const LIST_SQL: &str = r#"SELECT r.body,CASE $1::text WHEN 'id' THEN r.id WHEN 'time' THEN r.created_at ELSE r.name END AS ordering,r.id
FROM resources r WHERE r.state=$2 AND ($3::text IS NULL OR r.parent=$3)
AND ($4::text IS NULL OR r.type=$4) AND ($5::text IS NULL OR r.owner=$5)
AND ($6::text IS NULL OR r.name LIKE $6 ESCAPE '\' OR EXISTS(SELECT 1 FROM projections p WHERE p.resource_id=r.id AND p.text LIKE $6 ESCAPE '\'))
AND ($7::text IS NULL OR EXISTS(SELECT 1 FROM resource_tags t WHERE t.resource_id=r.id AND t.tag=$7))
AND ($8::text='all' OR (r.type='post' AND (($8='replies')=EXISTS(SELECT 1 FROM relations rel WHERE rel.source_id=r.id AND rel.revision_id=r.revision AND rel.type='reply_to'))))
AND ($9::text IS NULL OR r.created_at<=$9)
AND ($10::text IS NULL OR (NOT $12::bool AND (CASE $1 WHEN 'id' THEN r.id WHEN 'time' THEN r.created_at ELSE r.name END,r.id)>($10,$11))
OR ($12::bool AND (CASE $1 WHEN 'id' THEN r.id WHEN 'time' THEN r.created_at ELSE r.name END,r.id)<($10,$11)))
ORDER BY CASE WHEN NOT $12::bool THEN CASE $1 WHEN 'id' THEN r.id WHEN 'time' THEN r.created_at ELSE r.name END END ASC,
CASE WHEN $12::bool THEN CASE $1 WHEN 'id' THEN r.id WHEN 'time' THEN r.created_at ELSE r.name END END DESC,
CASE WHEN NOT $12::bool THEN r.id END ASC,CASE WHEN $12::bool THEN r.id END DESC LIMIT 128"#;
fn list(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let args = ctx.args()?.clone();
    list_items(ctx, args, None)
}
fn list_items(
    ctx: &mut Context<'_>,
    supplied: BTreeMap<String, Json>,
    mut budget: Option<&mut ReadBudget>,
) -> Result<HandlerOutput> {
    let operation = ctx.request.field("operation")?.as_str()?.to_owned();
    let stable = operation == "discovery.read_query";
    let mut args = supplied;
    let cursor = arg_text(&args, "cursor")?;
    if stable {
        if let Some(cursor) = &cursor {
            require(args.len() == 1, "cursor_query_mismatch")?;
            let (query, _) = inspect_page(ctx, cursor)?;
            require(query["operation"] == operation, "cursor_query_mismatch")?;
            args = encode(query.get("arguments").ok_or(Error("invalid_cursor"))?)?.into_object()?;
            let saved = read_query_version(&args);
            let version = ctx
                .request
                .field("contract_version")?
                .as_integer()?
                .parse::<u32>()
                .map_err(|_| Error("invalid_version"))?;
            require(
                saved == version || (version == 2 && saved == 1),
                "cursor_query_mismatch",
            )?;
        } else if ctx.request.field("contract_version")?.as_integer()? == "5" {
            args.insert("query_version".into(), Json::parse("5")?);
            args.entry("post_kind".into())
                .or_insert(Json::string("all"));
        }
    }
    args.remove("cursor");
    if let Some(tag) = args.get("tag").cloned() {
        let tags = crate::content::normalize_tag(&tag)?;
        args.insert("tag".into(), Json::string(tags));
    }
    let parent = arg_text(&args, "parent")?
        .map(|s| ctx.resolve(&s, false))
        .transpose()?;
    if stable {
        if let Some(parent) = &parent {
            args.insert("parent".into(), Json::string(parent));
        }
    }
    if parent.as_deref() == Some("t_tools") {
        return output(json!({"items":filtered_tools(ctx)?}), vec![]);
    }
    if let Some(parent) = &parent {
        ctx.check(parent, Check::List)?;
    }
    let limit = arg_int(&args, "limit", 50)?;
    require((1..=200).contains(&limit), "invalid_limit")?;
    let mut chosen = fields(&args)?;
    if !args.contains_key("fields") {
        chosen = ["id", "type", "name", "revision", "generation", "path"]
            .into_iter()
            .map(str::to_owned)
            .collect();
    }
    if stable {
        require(
            limit <= 100 && (limit as usize) * (chosen.len() + 1) <= 1000,
            "query_cost_exceeded",
        )?;
    }
    let sort = arg_text(&args, "sort")?.unwrap_or("id".into());
    require(
        matches!(sort.as_str(), "id" | "time" | "name"),
        "invalid_sort",
    )?;
    let direction = arg_text(&args, "direction")?.unwrap_or("asc".into());
    require(matches!(direction.as_str(), "asc" | "desc"), "invalid_sort")?;
    let query = value(&args)?;
    let (mut position, snapshot) = if let Some(cursor) = &cursor {
        if stable {
            decode_page(ctx, cursor, &operation, &query)?
        } else {
            let data = value(&ctx.decode_cursor(cursor)?)?;
            require(
                data["kind"] == "page" && data["query"] == hashed(&query)?,
                "cursor_query_mismatch",
            )?;
            (data["position"].clone(), ctx.now)
        }
    } else {
        (J::Null, ctx.now)
    };
    let kind = arg_text(&args, "type")?;
    if let Some(kind) = &kind {
        ctx.catalog.resource_type(kind, 1)?;
    }
    let author = arg_text(&args, "author")?
        .map(|s| ctx.resolve(&s, false))
        .transpose()?;
    let text = arg_text(&args, "query")?
        .filter(|s| !s.is_empty())
        .map(|s| {
            format!(
                "%{}%",
                s.replace('\\', "\\\\")
                    .replace('%', "\\%")
                    .replace('_', "\\_")
            )
        });
    let state = arg_text(&args, "state")?.unwrap_or("active".into());
    let post_kind = arg_text(&args, "post_kind")?.unwrap_or("all".into());
    require(
        matches!(post_kind.as_str(), "all" | "roots" | "replies"),
        "invalid_post_kind",
    )?;
    let mut scanned = 0;
    let mut items = Vec::new();
    let mut last = position.clone();
    let mut more = false;
    loop {
        let (order, id) = if position.is_null() {
            (None, None)
        } else {
            let pair = position
                .as_array()
                .filter(|p| p.len() == 2)
                .ok_or(Error("invalid_cursor"))?;
            (
                Some(pair[0].as_str().ok_or(Error("invalid_cursor"))?),
                Some(pair[1].as_str().ok_or(Error("invalid_cursor"))?),
            )
        };
        let rows = ctx.tx.rows(
            LIST_SQL,
            &[
                sort.as_str().into(),
                state.as_str().into(),
                parent.as_deref().into(),
                kind.as_deref().into(),
                author.as_deref().into(),
                text.as_deref().into(),
                arg_text(&args, "tag")?.into(),
                post_kind.as_str().into(),
                if stable {
                    Value::from(encode(&snapshot)?.as_str()?)
                } else {
                    Value::Null
                },
                order.into(),
                id.into(),
                (direction == "desc").into(),
            ],
        )?;
        let count = rows.len();
        for row in rows {
            scanned += 1;
            require(scanned <= 4096, "query_cost_exceeded")?;
            if let Some(budget) = budget.as_deref_mut() {
                budget.scan()?;
            }
            position = json!([row.text(1)?, row.text(2)?]);
            if visible(ctx, row.text(2)?)? {
                if items.len() == limit as usize {
                    more = true;
                    break;
                }
                let resource: Resource = decode(&row.json(0)?)?;
                if let Some(budget) = budget.as_deref_mut() {
                    budget.node(chosen.len())?;
                }
                items.push(pick(metadata(ctx, &resource)?, &chosen)?);
                last = position.clone();
            }
        }
        if more || count < 128 {
            break;
        }
    }
    let mut data = json!({"items":items});
    if stable
        && ctx.request.field("contract_version")?.as_integer()? == "3"
        && !items.is_empty()
        && !more
    {
        data["cursor"] = json!(encode_page(ctx, &operation, &query, &last, snapshot)?);
    }
    if more {
        let cursor = if stable {
            encode_page(ctx, &operation, &query, &last, snapshot)?
        } else {
            ctx.encode_cursor(&encode(
                &json!({"v":1,"kind":"page","query":hashed(&query)?,"position":last}),
            )?)?
        };
        let mut next = query.clone();
        next["cursor"] = json!(cursor);
        data["cursor"] = json!(cursor);
        data["next"] = json!(if stable {
            format!("/_r/c/{cursor}")
        } else {
            next_link(ctx, &operation, &next)?
        });
        data["next_requires_auth"] = json!(ctx.principal.subject().is_some());
    }
    output(data, vec![])
}

fn visible_link(ctx: &mut Context<'_>, reference: &ResourceRef) -> Result<Option<J>> {
    if !visible(ctx, &reference.id)? {
        return Ok(None);
    }
    if ctx.tx.resource_record(&reference.id)?.state == ResourceState::Purged {
        return Ok(None);
    }
    if reference.revision.is_some() {
        match ctx.tx.revision(reference) {
            Ok(_) => (),
            Err(Error("revision_not_found")) => return Ok(None),
            Err(e) => return Err(e),
        }
    }
    Ok(Some(
        json!({"ref":reference,"path":short_path(&ctx.tx.resource_path(&reference.id)?),"address":address(ctx,reference)}),
    ))
}
fn basic_links(
    ctx: &mut Context<'_>,
    resource: &Resource,
    revision: Option<&Revision>,
) -> Result<J> {
    let reference = ResourceRef {
        id: resource.id.clone(),
        revision: revision.map(|r| r.id.clone()).or(resource.revision.clone()),
    };
    let mut links = json!({"self":{"ref":reference,"path":short_path(&ctx.tx.resource_path(&resource.id)?),"address":address(ctx,&ResourceRef{id:resource.id.clone(),revision:None})}});
    if matches!(
        resource.resource_type.as_str(),
        "post" | "attachment" | "file"
    ) {
        if let Some(parent) = &resource.parent {
            if ctx.tx.resource_record(parent)?.resource_type == "topic" {
                if let Some(link) = visible_link(
                    ctx,
                    &ResourceRef {
                        id: parent.clone(),
                        revision: None,
                    },
                )? {
                    links["t"] = link;
                }
            }
        }
    }
    if let Some(revision) = revision {
        if let Some(author) = visible_link(
            ctx,
            &ResourceRef {
                id: revision.author.clone(),
                revision: None,
            },
        )? {
            links["a"] = author;
        }
        links["v"] = json!({"ref":reference,"path":format!("/_r/{}/rev/{}",resource.id,revision.id),"address":address(ctx,&reference)});
        if revision.parents.len() == 1 {
            links["d"] = json!({"from":{"id":resource.id,"revision":revision.parents[0]},"to":reference,"path":format!("/_r/{}/l/d",resource.id)});
        }
        let target = |kind: &str| {
            revision
                .relations
                .iter()
                .rev()
                .find(|r| r.relation_type == kind)
                .map(|r| &r.target)
        };
        if resource.resource_type == "post" {
            if let Some(link) = visible_link(ctx, target("thread_root").unwrap_or(&reference))? {
                links["r"] = link;
            }
        }
        for (kind, key) in [("fork_of", "fork_of"), ("reply_to", "p")] {
            if let Some(reference) = target(kind) {
                if let Some(link) = visible_link(ctx, reference)? {
                    links[key] = link;
                }
            }
        }
    }
    Ok(links)
}
struct RelationPage<'a> {
    rel: &'a str,
    limit: usize,
    position: &'a J,
    snapshot: Timestamp,
}
fn relation_page(
    ctx: &mut Context<'_>,
    resource: &Resource,
    revision: Option<&Revision>,
    request: RelationPage<'_>,
    mut budget: Option<&mut ReadBudget>,
) -> Result<(Vec<J>, J, bool)> {
    let RelationPage {
        rel,
        limit,
        position,
        snapshot,
    } = request;
    let mut position = position.clone();
    if matches!(rel, "c" | "b" | "f" | "q") && position == "" {
        position = json!(["", ""]);
    }
    let mut candidates = Vec::new();
    if rel == "h" {
        let after = position.as_str().ok_or(Error("invalid_cursor"))?;
        for row in ctx.tx.rows("SELECT body FROM revisions WHERE resource_id=$1 AND id>$2 AND created_at<=$3 ORDER BY id LIMIT 2049",&[resource.id.as_str().into(),after.into(),encode(&snapshot)?.as_str()?.into()])?{
            let rev:Revision=decode(&row.json(0)?)?;candidates.push((json!(rev.id),ResourceRef{id:resource.id.clone(),revision:Some(rev.id)}));
        }
    } else if matches!(rel, "c" | "b") {
        let after = position
            .as_array()
            .filter(|p| p.len() == 2)
            .ok_or(Error("invalid_cursor"))?;
        let kinds = if rel == "c" {
            vec!["reply_to".into()]
        } else {
            vec!["quote".into(), "repost".into()]
        };
        for row in ctx.tx.rows("SELECT rel.source_id,rel.type,rel.revision_id FROM relations rel JOIN resources r ON r.id=rel.source_id AND r.revision=rel.revision_id WHERE rel.target_id=$1 AND rel.type=ANY($2) AND r.state='active' AND r.created_at<=$3 AND (rel.source_id,rel.type)>($4,$5) ORDER BY rel.source_id,rel.type LIMIT 2049",&[resource.id.as_str().into(),Value::TextArray(kinds),encode(&snapshot)?.as_str()?.into(),after[0].as_str().ok_or(Error("invalid_cursor"))?.into(),after[1].as_str().ok_or(Error("invalid_cursor"))?.into()])?{
            candidates.push((json!([row.text(0)?,row.text(1)?]),ResourceRef{id:row.text(0)?.into(),revision:Some(row.text(2)?.into())}));
        }
    } else if matches!(rel, "f" | "q") {
        if let Some(rev) = revision {
            require(rev.relations.len() <= 2048, "query_cost_exceeded")?;
            let mut relations: Vec<&Relation> = rev
                .relations
                .iter()
                .filter(|r| {
                    if rel == "f" {
                        r.relation_type == "attachment"
                    } else {
                        matches!(r.relation_type.as_str(), "quote" | "repost")
                    }
                })
                .collect();
            relations.sort_by(|a, b| {
                (&a.target.id, &a.relation_type).cmp(&(&b.target.id, &b.relation_type))
            });
            let after = position
                .as_array()
                .filter(|p| p.len() == 2)
                .ok_or(Error("invalid_cursor"))?;
            let after = (
                after[0].as_str().ok_or(Error("invalid_cursor"))?,
                after[1].as_str().ok_or(Error("invalid_cursor"))?,
            );
            for relation in relations {
                if (relation.target.id.as_str(), relation.relation_type.as_str()) > after {
                    candidates.push((
                        json!([relation.target.id, relation.relation_type]),
                        relation.target.clone(),
                    ));
                }
            }
        }
    }
    let mut items = Vec::new();
    let mut last = position;
    let mut more = false;
    for (scanned, (key, reference)) in candidates.into_iter().enumerate() {
        require(scanned < 2048, "query_cost_exceeded")?;
        if let Some(budget) = budget.as_deref_mut() {
            budget.scan()?;
        }
        if let Some(mut link) = visible_link(ctx, &reference)? {
            if rel == "h" {
                let revision = ctx.tx.revision(&reference)?;
                link["author"] = json!(revision.author);
                link["created_at"] = json!(revision.created_at);
                let value = value(&revision)?;
                for field in ["change_note", "source_kind", "source_version"] {
                    if let Some(v) = value.get(field) {
                        link[field] = v.clone();
                    }
                }
            }
            if items.len() == limit {
                more = true;
                break;
            }
            if let Some(budget) = budget.as_deref_mut() {
                budget.node(5)?;
            }
            items.push(link);
            last = key;
        }
    }
    Ok((items, last, more))
}
fn links(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let mut args = ctx.args()?.clone();
    let cursor = arg_text(&args, "cursor")?;
    if let Some(cursor) = &cursor {
        require(args.len() == 1, "cursor_query_mismatch")?;
        let (query, _) = inspect_page(ctx, cursor)?;
        require(
            query["operation"] == "discovery.links",
            "cursor_query_mismatch",
        )?;
        args = encode(&query["arguments"])?.into_object()?;
    }
    let id = ctx.resolve(
        args.get("id")
            .ok_or(Error("read_resource_required"))?
            .as_str()?,
        false,
    )?;
    ctx.check(&id, Check::Read)?;
    let resource = ctx.tx.resource_record(&id)?;
    require(resource.state != ResourceState::Purged, "resource_purged")?;
    let revision = if resource.revision.is_some() {
        Some(ctx.tx.revision(&ref_for(&resource))?)
    } else {
        None
    };
    let rel = arg_text(&args, "rel")?;
    let limit = arg_int(&args, "limit", 50)?;
    require((1..=100).contains(&limit), "invalid_limit")?;
    if let Some(rel) = &rel {
        require(
            [
                "self", "t", "a", "r", "p", "c", "f", "q", "b", "h", "v", "d",
            ]
            .contains(&rel.as_str()),
            "unknown_link_relation",
        )?;
    }
    let query = json!({"id":id,"rel":rel,"limit":limit});
    let (position, snapshot) = if let Some(cursor) = &cursor {
        decode_page(ctx, cursor, "discovery.links", &query)?
    } else {
        (json!(""), ctx.now)
    };
    let mut singles = basic_links(ctx, &resource, revision.as_ref())?;
    let collections = ["c", "f", "q", "b", "h"];
    if let Some(rel) = &rel {
        if collections.contains(&rel.as_str()) {
            let (items, last, more) = relation_page(
                ctx,
                &resource,
                revision.as_ref(),
                RelationPage {
                    rel,
                    limit: limit as usize,
                    position: &position,
                    snapshot,
                },
                None,
            )?;
            let mut data = json!({"id":id,"revision":resource.revision,"rel":rel,"items":items});
            if more {
                let cursor = encode_page(ctx, "discovery.links", &query, &last, snapshot)?;
                data["cursor"] = json!(cursor);
                data["next"] = json!(format!("/_r/c/{cursor}"));
                data["next_requires_auth"] = json!(ctx.principal.subject().is_some());
            }
            return output(data, vec![]);
        }
        return output(singles.get(rel).ok_or(Error("not_found"))?.clone(), vec![]);
    }
    let now = ctx.now;
    for rel in collections {
        let (items, _, _) = relation_page(
            ctx,
            &resource,
            revision.as_ref(),
            RelationPage {
                rel,
                limit: 1,
                position: &json!(""),
                snapshot: now,
            },
            None,
        )?;
        if !items.is_empty() {
            singles[rel] = json!({"source":ref_for(&resource),"path":format!("/_r/{id}/l/{rel}"),"collection":true});
        }
    }
    output(
        json!({"id":id,"revision":resource.revision,"links":singles}),
        vec![],
    )
}
fn references(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let id = ctx.resolve(ctx.text("id")?, false)?;
    ctx.check(&id, Check::Read)?;
    let limit = arg_int(ctx.args()?, "limit", 50)?;
    require((1..=200).contains(&limit), "invalid_limit")?;
    let mut items = Vec::new();
    for row in ctx.tx.rows("SELECT rel.source_id,rel.type,rel.body FROM relations rel JOIN resources r ON r.id=rel.source_id AND r.revision=rel.revision_id WHERE rel.target_id=$1 ORDER BY rel.source_id",&[id.into()])?{
        if visible(ctx,row.text(0)?)?{let relation:Relation=decode(&row.json(2)?)?;items.push(json!({"source_id":row.text(0)?,"type":row.text(1)?,"target":relation.target}));}
        if items.len()==limit as usize{break}
    }
    output(json!({"items":items}), vec![])
}

fn catalog(ctx: &Context<'_>) -> Result<HandlerOutput> {
    let data = ctx.catalog.catalog()?;
    if arg_text(ctx.args()?, "known_digest")?.as_deref() == data["digest"].as_str() {
        output(json!({"not_modified":true,"digest":data["digest"]}), vec![])
    } else {
        output(data, vec![])
    }
}
fn capability_data(ctx: &Context<'_>) -> Result<J> {
    let capabilities = ctx.catalog.capabilities()?;
    Ok(json!({"version":1,"digest":hashed(&capabilities)?,"capabilities":capabilities}))
}
fn capabilities(ctx: &Context<'_>) -> Result<HandlerOutput> {
    let data = capability_data(ctx)?;
    if arg_text(ctx.args()?, "known_digest")?.as_deref() == data["digest"].as_str() {
        output(json!({"not_modified":true,"digest":data["digest"]}), vec![])
    } else {
        output(data, vec![])
    }
}
fn schema(ctx: &Context<'_>) -> Result<HandlerOutput> {
    let name = ctx.text("operation")?;
    let spec = if let Some((name, version)) = name.rsplit_once('@') {
        let version = version
            .parse::<u32>()
            .map_err(|_| Error("invalid_operation_version"))?;
        require(version > 0, "invalid_operation_version")?;
        ctx.catalog.operation(name, version)?
    } else {
        ctx.catalog
            .operations()
            .filter(|op| op.name == name)
            .max_by_key(|op| op.version)
            .ok_or(Error("unknown_operation"))?
    };
    require(spec.entries.contains("network"), "entry_not_allowed")?;
    let description = value(&spec.describe()?)?;
    output(
        json!({"operation":description,"input":ctx.catalog.schema(&spec.input_schema.id)?,"output":ctx.catalog.schema(&spec.output_schema.id)?,"requires_rules":description["requires_rules"]}),
        vec![],
    )
}

fn quote_path(path: &str) -> String {
    let mut result = String::new();
    for b in path.bytes() {
        if b.is_ascii_alphanumeric() || b"-_.~/@".contains(&b) {
            result.push(b as char)
        } else {
            result.push_str(&format!("%{b:02X}"));
        }
    }
    result
}
fn board_rules(ctx: &Context<'_>, resource: &Resource) -> Result<J> {
    let policy = ctx
        .tx
        .setting(&format!("policy:{}", resource.id))?
        .map(Json::into_object)
        .transpose()?
        .unwrap_or_default();
    let membership = ctx.tx.one(
        "SELECT membership_policy FROM topic_settings WHERE topic=$1",
        &[resource.id.as_str().into()],
    )?;
    Ok(
        json!({"board_id":resource.id,"generation":resource.generation,"text":policy.get("rules").cloned().unwrap_or(Json::string("")),"posting_policy":policy.get("posting_policy").cloned().unwrap_or(Json::string("open")),"membership_policy":membership.as_ref().map(|r|r.text(0)).transpose()?.unwrap_or("open"),"reply_open":policy.get("reply_open").map(|v|value(v)).transpose()?.unwrap_or(json!(true)),"editable":policy.get("editable").map(|v|value(v)).transpose()?.unwrap_or(json!(true)),"platform_rules":"/_rules/topics"}),
    )
}
fn board_presentation(ctx: &mut Context<'_>, resource: &Resource) -> Result<J> {
    let mut description = board_default(&resource.name).to_owned();
    let mut files = BTreeMap::new();
    for name in ["ABOUT.md", "HEADER.svg"] {
        if let Some(row)=ctx.tx.one("SELECT id FROM resources WHERE parent=$1 AND name=$2 AND type='file' AND state='active'",&[resource.id.as_str().into(),name.into()])?{
            let id=row.text(0)?;if !visible(ctx,id)?{continue}files.insert(name,id.to_owned());
            if name=="ABOUT.md"{let revision=ctx.tx.revision(&ResourceRef{id:id.into(),revision:None})?;if matches!(revision.content.media_type.as_str(),"text/plain"|"text/markdown")&&revision.content.size<=4096{description=String::from_utf8_lossy(&ctx.contents.read_bytes(&revision.content,4096)?).into_owned();}}
        }
    }
    let mut administrators = vec!["u_root".to_owned()];
    let mut names = vec!["@root".to_owned()];
    for row in ctx.tx.rows("SELECT subject FROM topic_memberships WHERE topic=$1 AND role='admin' AND status='active' ORDER BY subject",&[resource.id.as_str().into()])?{
        let id=row.text(0)?;if id!="u_root"{names.push(ctx.tx.resource_record(id)?.name);administrators.push(id.into());}
    }
    Ok(
        json!({"administrator_names":names,"description":description,"administrators":administrators,"header":{"url":format!("/_board/{}/header.svg",quote_path(&resource.id)),"file":files.get("HEADER.svg")},"description_file":files.get("ABOUT.md")}),
    )
}
fn board_default(name: &str) -> &'static str {
    match name{
    "main"=>"General conversation: share ideas, ask questions, and keep useful threads moving. / 自由交流、分享想法、提出问题，让有价值的讨论继续。",
    "wiki"=>"Shared knowledge, practical guides, and lessons worth keeping. Everyone can improve the articles. / 共同维护知识、实用教程和值得留下的经验，人人都能完善文章。",
    "intro"=>"Introduce yourself, your projects, and what you hope to learn or build here. / 介绍自己、正在做的项目，以及想在这里学习或创造什么。",
    "news"=>"Project updates, releases, and noteworthy changes, with sources and context. / 项目进展、版本发布和重要变化，请带上来源与背景。",
    "sos"=>"Ask for help with a clear problem, what you tried, and the result you need. / 遇到困难时说明问题、已尝试的办法和需要的帮助。",
    "relief"=>"Mutual aid and practical support: explain the need, available help, and follow-up. / 互助与实际支持：说明需求、能提供的帮助和后续进展。",
    "store"=>"Browse tools, services, and open-source bounty tasks. Keep offers clear and verifiable. / 浏览工具、服务与开源悬赏，清楚说明交付内容和可核验结果。",
    "templates"=>"Reusable prompts, structured workflows, and templates that make collaboration easier. / 分享可复用的提示词、工作流程和协作模板。",
    "tmp"=>"A temporary workspace for experiments, scratch notes, and short-lived discussions. / 临时实验、草稿记录和短期讨论的工作区。",
    "last-will"=>"Signed legacy directives and their public verification context. / 签名遗嘱指令及其公开核验信息。",
    "certified"=>"Certificate-gated discussion with explicit authority and scoped permissions. / 基于证书、明确授权范围的讨论。",
    "admins"=>"Channel stewardship, moderation decisions, and operational follow-up. / 板块维护、管理决定与运行跟进。",
    _=>"A space for focused discussion, shared work, and useful discoveries. / 专注讨论、共同工作和分享有用发现的空间。"}
}

fn shorten(text: &str, limit: usize) -> String {
    if text.chars().count() <= limit {
        text.into()
    } else {
        format!(
            "{}…",
            text.chars()
                .take(limit.saturating_sub(1))
                .collect::<String>()
                .trim_end_matches(py_whitespace)
        )
    }
}
fn plain_text(text: &str) -> Result<String> {
    let links =
        regex::Regex::new(r"!?\[([^\]]*)\]\([^)]*\)").map_err(|_| Error("invalid_pattern"))?;
    let tags = regex::Regex::new(r"<[^>]*>").map_err(|_| Error("invalid_pattern"))?;
    let text = links.replace_all(text, "$1");
    let text = tags.replace_all(&text, "");
    let text: String = text
        .chars()
        .filter(|c| !matches!(c, '`' | '*' | '_' | '~'))
        .collect();
    Ok(text
        .split(py_whitespace)
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" "))
}
fn py_whitespace(character: char) -> bool {
    character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
}
/// Python str.splitlines boundaries, including CRLF and Unicode separators.
fn split_lines(text: &str, keep_ends: bool) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((offset, character)) = chars.next() {
        if !matches!(
            character,
            '\n' | '\r' | '\u{b}' | '\u{c}' | '\u{1c}'
                ..='\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}'
        ) {
            continue;
        }
        let mut end = offset + character.len_utf8();
        if character == '\r' && chars.peek().is_some_and(|(_, c)| *c == '\n') {
            chars.next();
            end += 1;
        }
        lines.push(&text[start..if keep_ends { end } else { offset }]);
        start = end;
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}
fn attachment_kind(media: &str) -> &'static str {
    match media
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase()
        .as_str()
    {
        "image/png" | "image/jpeg" | "image/gif" | "image/webp" | "image/avif" => "image",
        "video/mp4" | "video/webm" | "video/ogg" | "video/mpeg" | "video/quicktime" => "video",
        "audio/mpeg" | "audio/mp4" | "audio/ogg" | "audio/wav" | "audio/x-wav" | "audio/webm"
        | "audio/flac" => "audio",
        "text/plain" | "text/markdown" => "text",
        _ => "file",
    }
}
fn attachments(ctx: &mut Context<'_>, revision: &Revision) -> Result<(Vec<J>, bool)> {
    let relations: Vec<_> = revision
        .relations
        .iter()
        .filter(|r| r.relation_type == "attachment")
        .collect();
    let mut items = Vec::new();
    for relation in relations.iter().take(32) {
        let unavailable = json!({"ref":relation.target,"available":false,"name":"Unavailable attachment","kind":"file"});
        let read = (|| -> Result<J> {
            require(relation.target.revision.is_some(), "revision_not_found")?;
            ctx.check(&relation.target.id, Check::Read)?;
            let resource = ctx.tx.resource_record(&relation.target.id)?;
            require(resource.state != ResourceState::Purged, "resource_purged")?;
            require(resource.resource_type == "attachment", "revision_not_found")?;
            let revision = ctx.tx.revision(&relation.target)?;
            let reference = ResourceRef {
                id: resource.id,
                revision: Some(revision.id),
            };
            let kind = if revision.content.media_type.chars().count() > 160 {
                "file"
            } else {
                attachment_kind(&revision.content.media_type)
            };
            let mut item = json!({"ref":reference,"available":true,"name":shorten(&resource.name,240),"kind":kind,"media_type":shorten(&revision.content.media_type,160),"size":revision.content.size,"digest":revision.content.digest,"raw_url":format!("/_id/{}/revisions/{}/raw",reference.id,reference.revision.as_deref().unwrap_or(""))});
            if revision.content.media_type.chars().count() > 160 {
                item["media_type_truncated"] = json!(true)
            }
            if let Some(summary) = revision.summary.filter(|s| !s.is_empty()) {
                item["summary"] = json!(shorten(
                    &plain_text(&summary.chars().take(1024).collect::<String>())?,
                    280
                ));
            }
            if kind == "text" && revision.content.size > 0 {
                let end = revision.content.size.min(512) as u64;
                let mut bytes = Vec::new();
                ctx.contents
                    .read_to(&revision.content, Some((0, end)), &mut bytes)?;
                let preview = plain_text(&String::from_utf8_lossy(&bytes))?;
                item["preview"] = json!(shorten(&preview, 180));
                item["preview_truncated"] =
                    json!(revision.content.size > end as i64 || preview.chars().count() > 180);
            }
            Ok(item)
        })();
        match read {
            Ok(item) => items.push(item),
            Err(Error(
                "permission_denied"
                | "credential_ceiling"
                | "certificate_gate"
                | "tool_certificate_required"
                | "delegation_scope"
                | "ancestor_inactive"
                | "not_found"
                | "revision_not_found"
                | "resource_purged"
                | "content_missing",
            )) => items.push(unavailable),
            Err(e) => return Err(e),
        }
    }
    Ok((items, relations.len() > 32))
}

#[derive(Clone, Copy)]
struct SegmentState {
    offset: u64,
    fence: bool,
    line_start: bool,
    continued: bool,
}
fn segment(
    ctx: &Context<'_>,
    blob: &msg_storage::records::BlobRef,
    state: SegmentState,
    max: usize,
) -> Result<(SegmentState, String)> {
    require(
        blob.size >= 0 && state.offset <= blob.size as u64,
        "invalid_byte_range",
    )?;
    let end = (blob.size as u64).min(
        state
            .offset
            .checked_add(max as u64 + 4)
            .ok_or(Error("invalid_byte_range"))?,
    );
    let mut raw = Vec::new();
    ctx.contents
        .read_to(blob, Some((state.offset, end)), &mut raw)?;
    segment_window(&raw, state, max, blob.size as u64)
}
fn segment_window(
    raw: &[u8],
    state: SegmentState,
    max: usize,
    size: u64,
) -> Result<(SegmentState, String)> {
    let decoded = match std::str::from_utf8(&raw) {
        Ok(text) => text,
        Err(e) if e.error_len().is_none() && e.valid_up_to() >= raw.len().saturating_sub(4) => {
            std::str::from_utf8(&raw[..e.valid_up_to()])
                .map_err(|_| Error("invalid_utf8_content"))?
        }
        Err(_) => return Err(Error("invalid_utf8_content")),
    };
    let mut length = decoded.len().min(max);
    while !decoded.is_char_boundary(length) {
        length -= 1
    }
    let budget = &decoded[..length];
    require(
        !budget.is_empty() || state.offset == size,
        "read_window_too_small",
    )?;
    let (mut fence, mut line_start, mut scanned) = (state.fence, state.line_start, 0);
    let mut candidate = None;
    for line in budget.split_inclusive('\n') {
        let complete = line.ends_with('\n');
        let trimmed = line.trim();
        if line_start && (trimmed.starts_with("```") || trimmed.starts_with("~~~")) {
            fence = !fence;
            if !fence && complete {
                candidate = Some((scanned + line.len(), fence, true));
            }
        } else if !fence && line_start && line.starts_with('#') && scanned > 0 {
            candidate = Some((scanned, fence, true));
        } else if !fence && trimmed.is_empty() && complete {
            candidate = Some((scanned + line.len(), fence, true));
        }
        scanned += line.len();
        line_start = complete;
    }
    let (length, fence, line_start, continued) =
        if let Some((n, f, l)) = candidate.filter(|(n, _, _)| *n > 0) {
            (n, f, l, false)
        } else {
            (
                budget.len(),
                fence,
                line_start,
                state.offset + (budget.len() as u64) < size,
            )
        };
    let offset = state.offset + length as u64;
    require(
        offset > state.offset || state.offset == size,
        "read_window_too_small",
    )?;
    Ok((
        SegmentState {
            offset,
            fence,
            line_start,
            continued,
        },
        budget[..length].into(),
    ))
}
fn segment_cursor(
    ctx: &Context<'_>,
    reference: &ResourceRef,
    max: usize,
    state: SegmentState,
    previous: Option<SegmentState>,
) -> Result<String> {
    ctx.encode_cursor(&encode(&json!({"v":1,"kind":"read-segment","query":{"ref":reference,"max_bytes":max},"position":{"offset":state.offset,"fence":state.fence,"line_start":state.line_start,"continued":state.continued,"principal":principal(ctx),"expires_at":Timestamp(ctx.now.0+Duration::minutes(15)),"previous":previous.map(|p|json!([p.offset,p.fence,p.line_start,p.continued]))}}))?)
}
fn parse_segment_state(position: &J) -> Result<SegmentState> {
    Ok(SegmentState {
        offset: position["offset"].as_u64().ok_or(Error("invalid_cursor"))?,
        fence: position["fence"].as_bool().ok_or(Error("invalid_cursor"))?,
        line_start: position["line_start"]
            .as_bool()
            .ok_or(Error("invalid_cursor"))?,
        continued: position["continued"]
            .as_bool()
            .ok_or(Error("invalid_cursor"))?,
    })
}
fn previous_state(value: &J) -> Result<SegmentState> {
    let a = value
        .as_array()
        .filter(|a| a.len() == 4)
        .ok_or(Error("invalid_cursor"))?;
    parse_segment_state(&json!({"offset":a[0],"fence":a[1],"line_start":a[2],"continued":a[3]}))
}
fn read_segment(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let args = ctx.args()?.clone();
    let (reference, max, state, mut previous) = if let Some(cursor) = arg_text(&args, "cursor")? {
        require(args.len() == 1, "cursor_query_mismatch")?;
        let data = value(&ctx.decode_cursor(&cursor)?)?;
        require(data["kind"] == "read-segment", "cursor_kind_mismatch")?;
        let position = &data["position"];
        require(
            ctx.now
                < Timestamp::parse(
                    position["expires_at"]
                        .as_str()
                        .ok_or(Error("invalid_cursor"))?,
                )?,
            "cursor_expired",
        )?;
        require(
            position["principal"] == principal(ctx),
            "cursor_principal_mismatch",
        )?;
        (
            decode::<ResourceRef>(&encode(&data["query"]["ref"])?)?,
            data["query"]["max_bytes"]
                .as_u64()
                .ok_or(Error("invalid_cursor"))? as usize,
            parse_segment_state(position)?,
            position
                .get("previous")
                .filter(|v| !v.is_null())
                .map(previous_state)
                .transpose()?,
        )
    } else {
        (
            ResourceRef {
                id: ctx.resolve(ctx.text("id")?, false)?,
                revision: arg_text(&args, "revision")?,
            },
            arg_int(&args, "max_bytes", 4096)? as usize,
            SegmentState {
                offset: 0,
                fence: false,
                line_start: true,
                continued: false,
            },
            None,
        )
    };
    require(
        (32..=8192.min(ctx.settings.max_response_bytes / 4)).contains(&max),
        "query_cost_exceeded",
    )?;
    ctx.check(&reference.id, Check::Read)?;
    let revision = ctx.tx.revision(&reference)?;
    require(
        revision.content.media_type.starts_with("text/"),
        "text_required",
    )?;
    let reference = ResourceRef {
        id: reference.id,
        revision: Some(revision.id.clone()),
    };
    let (end, text) = segment(ctx, &revision.content, state, max)?;
    let mut data = json!({"id":reference.id,"revision":revision.id,"range":[state.offset,end.offset],"text":text,"continued_block":end.continued,"continues_previous":state.continued});
    if end.offset < revision.content.size as u64 {
        data["next"] = json!(format!(
            "/_r/c/{}",
            segment_cursor(ctx, &reference, max, end, Some(state))?
        ));
    }
    if state.offset > 0 {
        if previous.is_none() {
            let mut p = SegmentState {
                offset: 0,
                fence: false,
                line_start: true,
                continued: false,
            };
            let mut scanned = 0;
            while p.offset < state.offset {
                scanned += 1;
                require(scanned <= 4096, "query_cost_exceeded")?;
                previous = Some(p);
                let (next, _) = segment(ctx, &revision.content, p, max)?;
                require(next.offset <= state.offset, "invalid_cursor")?;
                p = next;
            }
            require(p.offset == state.offset, "invalid_cursor")?;
        }
        let previous = previous.ok_or(Error("invalid_cursor"))?;
        data["prev"] = json!(format!(
            "/_r/c/{}",
            segment_cursor(ctx, &reference, max, previous, None)?
        ));
    }
    output(data, vec![reference])
}

#[derive(Default)]
struct ReadBudget {
    scanned: usize,
    nodes: usize,
    cost: usize,
}
impl ReadBudget {
    fn scan(&mut self) -> Result<()> {
        self.scanned += 1;
        require(self.scanned <= 4096, "query_cost_exceeded")
    }
    fn node(&mut self, fields: usize) -> Result<()> {
        self.nodes += 1;
        self.cost += 1 + fields;
        require(
            self.nodes <= 100 && self.cost <= 1000,
            "query_cost_exceeded",
        )
    }
    fn output(&self, ctx: &Context<'_>, data: &J) -> Result<()> {
        require(
            encode(data)?.canonical()?.len() <= ctx.settings.max_response_bytes,
            "response_too_large",
        )
    }
}
fn read_query_version(args: &BTreeMap<String, Json>) -> u32 {
    if args
        .get("query_version")
        .is_some_and(|v| v.as_integer() == Ok("5"))
    {
        5
    } else if args
        .get("query_version")
        .is_some_and(|v| v.as_integer() == Ok("3"))
        || args.get("expand").is_some_and(|v| v.as_object().is_ok())
    {
        3
    } else if ["expand", "collection", "nested_first"]
        .iter()
        .any(|k| args.contains_key(*k))
    {
        2
    } else {
        1
    }
}
fn nested_page(
    ctx: &mut Context<'_>,
    mut args: BTreeMap<String, Json>,
    mut budget: Option<&mut ReadBudget>,
) -> Result<J> {
    let cursor = arg_text(&args, "cursor")?;
    if let Some(cursor) = &cursor {
        require(args.len() == 1, "cursor_query_mismatch")?;
        let (saved, _) = inspect_page(ctx, cursor)?;
        require(
            saved["operation"] == "discovery.read_query"
                && matches!(
                    saved["arguments"]["collection"].as_str(),
                    Some("children" | "replies")
                ),
            "cursor_query_mismatch",
        )?;
        args = encode(&saved["arguments"])?.into_object()?;
    }
    let collection = arg_text(&args, "collection")?.ok_or(Error("invalid_nested_query"))?;
    require(
        matches!(collection.as_str(), "children" | "replies")
            && args.contains_key("parent")
            && ![
                "expand",
                "nested_first",
                "type",
                "author",
                "query",
                "tag",
                "state",
                "sort",
                "direction",
            ]
            .iter()
            .any(|k| args.contains_key(*k)),
        "invalid_nested_query",
    )?;
    let parent = ctx.resolve(
        args.get("parent")
            .ok_or(Error("invalid_nested_query"))?
            .as_str()?,
        false,
    )?;
    let limit = arg_int(&args, "limit", 5)?;
    let chosen = if args.contains_key("fields") {
        fields(&args)?
    } else {
        ["id", "name", "type", "path"]
            .into_iter()
            .map(str::to_owned)
            .collect()
    };
    require(
        (1..=10).contains(&limit)
            && chosen
                .iter()
                .all(|s| ["id", "name", "type", "path", "revision"].contains(&s.as_str())),
        "query_cost_exceeded",
    )?;
    let query = value(&args)?;
    let (position, snapshot) = if let Some(cursor) = &cursor {
        decode_page(ctx, cursor, "discovery.read_query", &query)?
    } else {
        (
            if collection == "children" {
                json!("")
            } else {
                json!(["", ""])
            },
            ctx.now,
        )
    };
    ctx.check(
        &parent,
        if collection == "children" {
            Check::List
        } else {
            Check::Read
        },
    )?;
    let resource = ctx.tx.resource_record(&parent)?;
    require(resource.state == ResourceState::Active, "ancestor_inactive")?;
    let (items, last, more) = if collection == "children" {
        let mut scan = position.as_str().ok_or(Error("invalid_cursor"))?.to_owned();
        let mut items = Vec::new();
        let mut last = position.clone();
        let mut more = false;
        let mut scanned = 0;
        loop {
            let rows=ctx.tx.rows("SELECT body,id FROM resources WHERE parent=$1 AND state='active' AND created_at<=$2 AND id>$3 ORDER BY id LIMIT 128",&[parent.as_str().into(),encode(&snapshot)?.as_str()?.into(),scan.as_str().into()])?;
            let count = rows.len();
            for row in rows {
                scanned += 1;
                require(scanned <= 2048, "query_cost_exceeded")?;
                if let Some(budget) = budget.as_deref_mut() {
                    budget.scan()?;
                }
                scan = row.text(1)?.into();
                if !visible(ctx, &scan)? {
                    continue;
                }
                if items.len() == limit as usize {
                    more = true;
                    break;
                }
                let resource: Resource = decode(&row.json(0)?)?;
                if let Some(budget) = budget.as_deref_mut() {
                    budget.node(chosen.len())?;
                }
                items.push(pick(metadata(ctx, &resource)?, &chosen)?);
                last = json!(scan);
            }
            if more || count < 128 {
                break;
            }
        }
        (items, last, more)
    } else {
        let revision = if resource.revision.is_some() {
            Some(ctx.tx.revision(&ref_for(&resource))?)
        } else {
            None
        };
        relation_page(
            ctx,
            &resource,
            revision.as_ref(),
            RelationPage {
                rel: "c",
                limit: limit as usize,
                position: &position,
                snapshot,
            },
            budget,
        )?
    };
    let cursor = if items.is_empty() {
        None
    } else {
        Some(encode_page(
            ctx,
            "discovery.read_query",
            &query,
            &last,
            snapshot,
        )?)
    };
    let mut data = json!({"items":items,"pageInfo":{"hasNextPage":more,"endCursor":cursor}});
    if more {
        data["next"] = json!(format!(
            "/_r/c/{}",
            cursor.as_deref().ok_or(Error("invalid_cursor"))?
        ));
        data["next_requires_auth"] = json!(ctx.principal.subject().is_some());
    }
    Ok(data)
}
fn read_query(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    match ctx.request.field("contract_version")?.as_integer()? {
        "2" => read_query_nested(ctx),
        "3" => read_query_tree(ctx),
        "4" => {
            require(
                ctx.principal.subject().is_none(),
                "public_home_summary_only",
            )?;
            output(public::home_summary(ctx, false)?, vec![])
        }
        _ => list(ctx),
    }
}
fn read_query_nested(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let supplied = ctx.args()?.clone();
    let mut args = supplied.clone();
    if let Some(cursor) = arg_text(&args, "cursor")? {
        require(args.len() == 1, "cursor_query_mismatch")?;
        let (saved, _) = inspect_page(ctx, &cursor)?;
        require(
            saved["operation"] == "discovery.read_query",
            "cursor_query_mismatch",
        )?;
        args = encode(&saved["arguments"])?.into_object()?;
        require(
            matches!(read_query_version(&args), 1 | 2),
            "cursor_query_mismatch",
        )?;
    }
    if args.contains_key("collection") {
        return output(nested_page(ctx, supplied, None)?, vec![]);
    }
    let expand = args
        .get("expand")
        .map(|v| {
            v.as_array()?
                .iter()
                .map(|v| v.as_str().map(str::to_owned))
                .collect::<Result<Vec<_>>>()
        })
        .transpose()?
        .unwrap_or_default();
    require(
        expand
            .iter()
            .all(|s| matches!(s.as_str(), "children" | "replies")),
        "invalid_nested_query",
    )?;
    require(
        expand.is_empty()
            || !args.contains_key("fields")
            || fields(&args)?.iter().any(|s| s == "id"),
        "unknown_projection_field",
    )?;
    let limit = arg_int(&args, "limit", 50)?;
    let first = arg_int(&args, "nested_first", 5)?;
    require((1..=10).contains(&first), "query_cost_exceeded")?;
    require(
        expand.is_empty()
            || (limit <= 10 && (limit as usize) * expand.len() * (first as usize + 1) <= 100),
        "query_cost_exceeded",
    )?;
    let root = if supplied.contains_key("cursor") {
        supplied
    } else {
        args.insert("expand".into(), encode(&expand)?);
        args
    };
    let result = list_items(ctx, root, None)?;
    let mut data = value(&result.data.ok_or(Error("invalid_projection"))?)?;
    for item in data["items"]
        .as_array_mut()
        .ok_or(Error("invalid_projection"))?
    {
        if expand.is_empty() {
            continue;
        }
        let id = item["id"]
            .as_str()
            .ok_or(Error("unknown_projection_field"))?
            .to_owned();
        item["collections"] = json!({});
        for collection in &expand {
            let child = encode(&json!({"parent":id,"collection":collection,"limit":first}))?
                .into_object()?;
            item["collections"][collection] = nested_page(ctx, child, None)?;
        }
    }
    data["pageInfo"] =
        json!({"hasNextPage":data.get("next").is_some(),"endCursor":data.get("cursor")});
    output(data, vec![])
}
fn tree_expand(
    ctx: &mut Context<'_>,
    mut page: J,
    plan: &Map<String, J>,
    depth: usize,
    budget: &mut ReadBudget,
) -> Result<J> {
    require(depth <= 4, "query_cost_exceeded")?;
    for item in page["items"]
        .as_array_mut()
        .ok_or(Error("invalid_projection"))?
    {
        if plan.is_empty() {
            continue;
        }
        let id = item["id"]
            .as_str()
            .ok_or(Error("unknown_projection_field"))?
            .to_owned();
        item["collections"] = json!({});
        for (collection, spec) in plan {
            require(
                matches!(collection.as_str(), "children" | "replies"),
                "invalid_nested_query",
            )?;
            let child = json!({"parent":id,"collection":collection,"limit":spec.get("limit").cloned().unwrap_or(json!(5)),"fields":spec.get("fields").cloned().unwrap_or(json!(["id","name","type","path","revision"])),"expand":spec.get("expand").cloned().unwrap_or(json!({})),"query_version":3});
            item["collections"][collection] =
                tree_collection(ctx, encode(&child)?.into_object()?, depth + 1, None, budget)?;
        }
    }
    budget.output(ctx, &page)?;
    Ok(page)
}
fn tree_collection(
    ctx: &mut Context<'_>,
    mut args: BTreeMap<String, Json>,
    depth: usize,
    boundary: Option<(J, Timestamp)>,
    budget: &mut ReadBudget,
) -> Result<J> {
    require(depth <= 4, "query_cost_exceeded")?;
    let parent = ctx.resolve(
        args.get("parent")
            .ok_or(Error("invalid_nested_query"))?
            .as_str()?,
        false,
    )?;
    args.insert("parent".into(), Json::string(parent));
    let mut leaf = args.clone();
    leaf.remove("expand");
    leaf.remove("query_version");
    let request = if let Some((last, snapshot)) = boundary {
        let cursor = encode_page(ctx, "discovery.read_query", &value(&leaf)?, &last, snapshot)?;
        BTreeMap::from([("cursor".into(), Json::string(cursor))])
    } else {
        leaf
    };
    let mut page = nested_page(ctx, request, Some(&mut *budget))?;
    if args.get("collection").and_then(|v| v.as_str().ok()) == Some("replies") {
        let chosen = if args.contains_key("fields") {
            fields(&args)?
        } else {
            ["id", "name", "type", "path", "revision"]
                .into_iter()
                .map(str::to_owned)
                .collect()
        };
        for item in page["items"]
            .as_array_mut()
            .ok_or(Error("invalid_projection"))?
        {
            let id = item["ref"]["id"]
                .as_str()
                .ok_or(Error("invalid_projection"))?;
            *item = pick(metadata(ctx, &ctx.tx.resource_record(id)?)?, &chosen)?;
        }
    }
    if let Some(end) = page["pageInfo"]["endCursor"].as_str() {
        let (query, _) = inspect_page(ctx, end)?;
        let (last, snapshot) = decode_page(ctx, end, "discovery.read_query", &query["arguments"])?;
        let outer = encode_page(ctx, "discovery.read_query", &value(&args)?, &last, snapshot)?;
        require(outer.len() <= 8192, "query_cost_exceeded")?;
        page["pageInfo"]["endCursor"] = json!(outer);
        if page["pageInfo"]["hasNextPage"] == true {
            page["next"] = json!(format!("/_r/c/{outer}"));
        }
    }
    let plan = args
        .get("expand")
        .map(|v| value(v))
        .transpose()?
        .unwrap_or(json!({}));
    tree_expand(
        ctx,
        page,
        plan.as_object().ok_or(Error("invalid_nested_query"))?,
        depth,
        budget,
    )
}
fn read_query_tree(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let supplied = ctx.args()?.clone();
    let mut args = supplied.clone();
    let mut boundary = None;
    let mut budget = ReadBudget::default();
    if let Some(cursor) = arg_text(&args, "cursor")? {
        require(args.len() == 1, "cursor_query_mismatch")?;
        let (saved, _) = inspect_page(ctx, &cursor)?;
        require(
            saved["operation"] == "discovery.read_query",
            "cursor_query_mismatch",
        )?;
        args = encode(&saved["arguments"])?.into_object()?;
        require(read_query_version(&args) == 3, "cursor_query_mismatch")?;
        boundary = Some(decode_page(
            ctx,
            &cursor,
            "discovery.read_query",
            &value(&args)?,
        )?);
        ctx.catalog.validate(
            "schema:discovery.read_query:3",
            &Json::from_object(args.clone()),
        )?;
    }
    args.insert("query_version".into(), Json::parse("3")?);
    let data = if args.contains_key("collection") {
        tree_collection(ctx, args, 0, boundary, &mut budget)?
    } else {
        let root = if supplied.contains_key("cursor") {
            supplied
        } else {
            args.clone()
        };
        let result = list_items(ctx, root, Some(&mut budget))?;
        let mut data = value(&result.data.ok_or(Error("invalid_projection"))?)?;
        data["pageInfo"] =
            json!({"hasNextPage":data.get("next").is_some(),"endCursor":data.get("cursor")});
        let plan = args
            .get("expand")
            .map(|v| value(v))
            .transpose()?
            .unwrap_or(json!({}));
        tree_expand(
            ctx,
            data,
            plan.as_object().ok_or(Error("invalid_nested_query"))?,
            0,
            &mut budget,
        )?
    };
    output(data, vec![])
}

#[cfg(test)]
mod tests;
