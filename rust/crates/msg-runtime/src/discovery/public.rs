//! Public feeds use the same current authority as every other resource read.
use super::*;

pub(super) fn feed(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let args = ctx.args()?.clone();
    let parent = arg_text(&args, "parent")?
        .filter(|s| !s.is_empty())
        .map(|s| ctx.resolve(&s, false))
        .transpose()?;
    if let Some(parent) = &parent {
        ctx.check(parent, Check::List)?;
    }
    let limit = arg_int(&args, "limit", 30)?;
    require((1..=100).contains(&limit), "invalid_limit")?;
    let binding = hashed(&json!({"parent":parent,"limit":limit}))?;
    let mut position = if let Some(cursor) = arg_text(&args, "cursor")?.filter(|s| !s.is_empty()) {
        let data = value(&ctx.decode_cursor(&cursor)?)?;
        require(
            data["kind"] == "rss" && data["query"] == binding,
            "cursor_query_mismatch",
        )?;
        data.get("position")
            .cloned()
            .ok_or(Error("invalid_cursor"))?
    } else {
        J::Null
    };
    let mut last = position.clone();
    let mut items = Vec::new();
    let mut more = false;
    let mut budget = ReadBudget::default();
    loop {
        let (stamp, id) = if position.is_null() {
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
        let rows = ctx.tx.rows("SELECT body,created_at,id FROM resources WHERE type='post' AND state='active' AND ($1::text IS NULL OR parent=$1) AND ($2::text IS NULL OR (created_at,id)<($2,$3)) ORDER BY created_at DESC,id DESC LIMIT 128", &[parent.as_deref().into(), stamp.into(), id.into()])?;
        let count = rows.len();
        for row in rows {
            budget.scan()?;
            position = json!([row.text(1)?, row.text(2)?]);
            if !visible(ctx, row.text(2)?)? {
                continue;
            }
            if items.len() == limit as usize {
                more = true;
                break;
            }
            let resource: Resource = decode(&row.json(0)?)?;
            let revision = ctx.tx.revision(&ResourceRef {
                id: resource.id.clone(),
                revision: None,
            })?;
            items.push(json!({"id":resource.id,"revision":revision.id,"title":resource.name,"url":format!("{}/*{}",ctx.settings.service_url,hex_id(&resource.id)),"created_at":resource.created_at,"digest":revision.manifest_digest}));
            last = position.clone();
        }
        if more || count < 128 {
            break;
        }
    }
    let service =
        Url::parse(&ctx.settings.service_url).map_err(|_| Error("invalid_service_url"))?;
    let authority = &service[url::Position::BeforeHost..url::Position::AfterPort];
    let mut data =
        json!({"title":format!("msg · {authority}"),"home":ctx.settings.service_url,"items":items});
    if more {
        let cursor = ctx.encode_cursor(&encode(
            &json!({"v":1,"kind":"rss","query":binding,"position":last}),
        )?)?;
        let mut next = args;
        next.insert("cursor".into(), Json::string(&cursor));
        data["cursor"] = json!(cursor);
        data["next"] = json!(next_link(ctx, "discovery.feed", &value(&next)?)?);
        data["next_requires_auth"] = json!(ctx.principal.subject().is_some());
    }
    budget.output(ctx, &data)?;
    output(data, vec![])
}

pub(super) fn hex_id(id: &str) -> String {
    let is_hex = |s: &str| {
        s.len() == 32
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    };
    if is_hex(id) {
        return id.to_owned();
    }
    if let Some((prefix, tail)) = id.rsplit_once('_') {
        if is_hex(tail)
            && prefix
                .bytes()
                .next()
                .is_some_and(|b| b.is_ascii_alphabetic())
            && prefix
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_')
        {
            return tail.to_owned();
        }
    }
    let mut input = b"msg.hex-reference/v1\0".to_vec();
    input.extend_from_slice(id.as_bytes());
    digest(&input)[7..39].to_owned()
}

pub(super) fn presence(ctx: &Context<'_>, subject: &str) -> Result<J> {
    let row = ctx.tx.one(
        "SELECT expires_at,body FROM presence WHERE subject=$1",
        &[subject.into()],
    )?;
    match row {
        Some(row) if Timestamp::parse(row.text(0)?)? > ctx.now => value(&row.json(1)?),
        _ => Ok(json!({"subject_id":subject,"state":"unknown"})),
    }
}
fn live_node(ctx: &mut Context<'_>, id: &str, nodes: &mut Vec<J>) -> Result<bool> {
    if nodes.iter().any(|v| v["id"] == id) {
        return Ok(true);
    }
    let resource = ctx.tx.resource_record(id)?;
    if resource.resource_type != "user"
        || resource.state != ResourceState::Active
        || !visible(ctx, id)?
    {
        return Ok(false);
    }
    let mut state = object(presence(ctx, id)?)?;
    state.retain(|k, _| {
        matches!(
            k.as_str(),
            "state" | "updated_at" | "expires_at" | "self_reported"
        )
    });
    nodes.push(json!({"id":id,"name":resource.name,"path":ctx.tx.resource_path(id)?,"presence":state,"last_public_activity_at":null}));
    Ok(true)
}
pub(super) fn now(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    require(ctx.principal.subject().is_none(), "anonymous_only")?;
    let stamp = encode(&ctx.now)?.as_str()?.to_owned();
    let cutoff = encode(&Timestamp(ctx.now.0 - Duration::minutes(15)))?
        .as_str()?
        .to_owned();
    let rows=ctx.tx.rows("SELECT r.id FROM presence p JOIN resources r ON r.id=p.subject WHERE p.expires_at>$1 AND r.state='active' ORDER BY p.expires_at DESC,r.id LIMIT 201",&[stamp.as_str().into()])?;
    let mut nodes = Vec::new();
    let mut events = Vec::new();
    let mut edges = Vec::new();
    for row in rows.iter().take(200) {
        let id = row.text(0)?;
        if matches!(
            presence(ctx, id)?["state"].as_str(),
            Some("available" | "busy")
        ) {
            live_node(ctx, id, &mut nodes)?;
        }
    }
    let posts=ctx.tx.rows("SELECT id,created_at,parent FROM resources WHERE type='post' AND state='active' AND created_at>=$1 AND created_at<=$2 ORDER BY created_at DESC,id DESC LIMIT 121",&[cutoff.into(),stamp.as_str().into()])?;
    for row in posts.iter().take(120) {
        let id = row.text(0)?;
        let created = row.text(1)?;
        let parent = row.text(2)?;
        if !visible(ctx, id)? || !visible(ctx, parent)? {
            continue;
        }
        let revision = ctx.tx.revision(&ResourceRef {
            id: id.into(),
            revision: None,
        })?;
        if !live_node(ctx, &revision.author, &mut nodes)? {
            continue;
        }
        let node = nodes
            .iter_mut()
            .find(|n| n["id"] == revision.author)
            .ok_or(Error("invalid_projection"))?;
        if node["last_public_activity_at"].as_str().unwrap_or("") < created {
            node["last_public_activity_at"] = json!(created);
        }
        events.push(json!({"id":id,"author":revision.author,"time":created,"path":ctx.tx.resource_path(id)?,"board":{"id":parent,"path":ctx.tx.resource_path(parent)?}}));
        for relation in &revision.relations {
            if !matches!(
                relation.relation_type.as_str(),
                "reply_to" | "quote" | "repost"
            ) {
                continue;
            }
            let target = ctx.tx.resource_record(&relation.target.id)?;
            if target.state != ResourceState::Active || !visible(ctx, &target.id)? {
                continue;
            }
            let target_revision = ctx.tx.revision(&relation.target)?;
            if !live_node(ctx, &target_revision.author, &mut nodes)? {
                continue;
            }
            edges.push(json!({"source":revision.author,"target":target_revision.author,"kind":relation.relation_type,"post":id,"time":created}));
        }
    }
    output(
        json!({"generated_at":stamp,"window_seconds":900,"refresh_seconds":5,"nodes":nodes,"edges":edges,"events":events,"bounded":rows.len()>200||posts.len()>120}),
        vec![],
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn compact_identifiers_preserve_signed_ids_without_prefix_guessing() {
        assert_eq!(
            hex_id("p_0123456789abcdef0123456789abcdef"),
            "0123456789abcdef0123456789abcdef"
        );
        assert_eq!(
            hex_id("Ab_c_0123456789abcdef0123456789abcdef"),
            "0123456789abcdef0123456789abcdef"
        );
        assert_eq!(
            hex_id("0123456789abcdef0123456789abcdef"),
            "0123456789abcdef0123456789abcdef"
        );
        assert_ne!(
            hex_id("_0123456789abcdef0123456789abcdef"),
            "0123456789abcdef0123456789abcdef"
        );
        assert_eq!(hex_id("u_root"), "39ab3632c17d38099bf0ee6d1d26c9eb");
    }
}
