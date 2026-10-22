//! Public feeds use the same current authority as every other resource read.
use super::*;
use msg_identity::authentication::{Entry, Principal};
use std::collections::{BTreeSet, HashMap};

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
    let budget = ReadBudget::default();
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
            ctx.check_deadline()?;
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

fn prefix(ctx: &Context<'_>, revision: &Revision, limit: u64) -> Result<Vec<u8>> {
    let size = u64::try_from(revision.content.size).map_err(|_| Error("invalid_blob_size"))?;
    let mut bytes = Vec::new();
    ctx.contents
        .read_to(&revision.content, Some((0, size.min(limit))), &mut bytes)?;
    Ok(bytes)
}
fn utf8_ignore(mut bytes: &[u8]) -> String {
    let mut text = String::new();
    while !bytes.is_empty() {
        match std::str::from_utf8(bytes) {
            Ok(rest) => {
                text.push_str(rest);
                break;
            }
            Err(error) => {
                // The prefix is checked by from_utf8; no unchecked conversion.
                if let Ok(part) = std::str::from_utf8(&bytes[..error.valid_up_to()]) {
                    text.push_str(part);
                }
                match error.error_len() {
                    Some(length) => bytes = &bytes[error.valid_up_to() + length..],
                    None => break,
                }
            }
        }
    }
    text
}
fn taipei_time(time: Timestamp) -> String {
    let local = time.0 + Duration::hours(8);
    let mut text = local.format("%Y-%m-%dT%H:%M:%S").to_string();
    if local.timestamp_subsec_micros() != 0 {
        text.push_str(&format!(".{:06}", local.timestamp_subsec_micros()));
    }
    text.push_str("+08:00");
    text
}
pub(super) fn home_summary(ctx: &mut Context<'_>, channels_only: bool) -> Result<J> {
    let today = (ctx.now.0 + Duration::hours(8)).date_naive();
    let stamp = encode(&ctx.now)?.as_str()?.to_owned();
    let mut posts = 0_u64;
    let mut posts_today = 0_u64;
    let mut users = 0_u64;
    let mut latest = Vec::new();
    let mut channel_posts = BTreeMap::<String, u64>::new();
    let mut position: Option<(String, String)> = None;
    loop {
        let rows=ctx.tx.rows("SELECT r.body,r.created_at,r.id FROM resources r WHERE r.state='active' AND r.created_at<=$1 AND (r.type='post' OR (r.type='user' AND EXISTS (SELECT 1 FROM identities i WHERE i.id=r.id AND i.kind='subject'))) AND ($2::text IS NULL OR (r.created_at,r.id)<($2,$3)) ORDER BY r.created_at DESC,r.id DESC LIMIT 128",&[stamp.as_str().into(),position.as_ref().map(|p|p.0.as_str()).into(),position.as_ref().map(|p|p.1.as_str()).into()])?;
        let count = rows.len();
        for row in rows {
            ctx.check_deadline()?;
            position = Some((row.text(1)?.into(), row.text(2)?.into()));
            let id = row.text(2)?;
            if !visible(ctx, id)? {
                continue;
            }
            let resource: Resource = decode(&row.json(0)?)?;
            if resource.resource_type == "user" {
                users += u64::from(
                    ctx.tx.subject(id)?.kind != msg_identity::models::SubjectKind::System,
                );
                continue;
            }
            posts += 1;
            for ancestor in ctx.tx.ancestors(id)? {
                if ancestor.kind == "topic" && ancestor.parent.as_deref() == Some("r_root") {
                    *channel_posts.entry(ancestor.id).or_default() += 1;
                }
            }
            posts_today +=
                u64::from((resource.created_at.0 + Duration::hours(8)).date_naive() == today);
            if !channels_only && latest.len() < 5 {
                let mut body = String::new();
                if resource.revision.is_some() {
                    let revision = ctx.tx.revision(&ref_for(&resource))?;
                    if revision.content.media_type.starts_with("text/") {
                        body = utf8_ignore(&prefix(ctx, &revision, 8192)?);
                    }
                }
                let preview = post_preview(&resource.name, &body)?;
                latest.push(json!({"name":resource.name,"title":preview["title"],"excerpt":preview["excerpt"],"view_count":metadata(ctx,&resource)?["view_count"],"path":format!("/*{}",hex_id(id)),"created_at":taipei_time(resource.created_at)}));
            }
        }
        if count < 128 {
            break;
        }
    }
    let mut channels = Vec::new();
    for row in ctx.tx.rows("SELECT body FROM resources WHERE type='topic' AND state='active' AND parent='r_root' AND created_at<=$1 ORDER BY name,id",&[stamp.into()])? {
        let resource:Resource=decode(&row.json(0)?)?;
        if resource.name.starts_with(['.','_']) || ctx.tx.one("SELECT 1 FROM dm_conversations WHERE resource_id=$1",&[resource.id.as_str().into()])?.is_some() || !visible(ctx,&resource.id)?{continue;}
        let policy=ctx.tx.setting(&format!("policy:{}",resource.id))?.unwrap_or(Json::from_object(BTreeMap::new()));
        let editable=policy.as_object()?.get("editable").map(Json::as_bool).transpose()?.unwrap_or(true);
        let mode=resource.mode;
        let mut posting=if resource.id=="t_last_will"{"legacy directive"}else if !editable{"frozen"}else if mode&0o003==0o003{"identity"}else if mode&0o030==0o030{if mode&0o300==0o300{"owner / group"}else{"group"}}else if mode&0o300==0o300{"owner"}else{"closed"}.to_owned();
        if mode&0o4000!=0 || ctx.tx.ancestors(&resource.id)?.iter().any(|a|u16::from_str_radix(&a.mode,8).is_ok_and(|m|m&0o4000!=0)){posting.push_str(" +cert");}
        channels.push(json!({"name":resource.name,"path":short_path(&ctx.tx.resource_path(&resource.id)?),"read":if ctx.principal.subject().is_some(){"Current identity access."}else{"Public; no login required."},"mode":format!("{mode:04o}"),"about":board_presentation(ctx,&resource)?["description"],"posting":posting,"posts":channel_posts.get(&resource.id).copied().unwrap_or(0)}));
    }
    Ok(
        json!({"channels":channels,"posts":posts,"posts_today":posts_today,"users":users,"date":today.to_string(),"timezone":"Asia/Taipei","latest":latest}),
    )
}

pub(super) fn post_preview(name: &str, body: &str) -> Result<J> {
    let marker = regex::Regex::new(r"^(`{3,}|~{3,})").map_err(|_| Error("invalid_pattern"))?;
    let rule = regex::Regex::new(r"^[-=*_ ]{3,}$").map_err(|_| Error("invalid_pattern"))?;
    let heading =
        regex::Regex::new(r"^#{1,6}\s+(.+?)\s*#*$").map_err(|_| Error("invalid_pattern"))?;
    let bullet = regex::Regex::new(r"^(?:>\s*|[-+]\s+)").map_err(|_| Error("invalid_pattern"))?;
    let mut title = String::new();
    let mut prose = Vec::new();
    let mut fence = None;
    let mut frontmatter = body.starts_with("---\n") || body.starts_with("---\r\n");
    for (index, line) in split_lines(body, false).into_iter().enumerate() {
        let line = line.trim_matches(py_whitespace);
        if frontmatter {
            if index != 0 && matches!(line, "---" | "...") {
                frontmatter = false;
            }
            continue;
        }
        if let Some(marker) = marker.find(line) {
            let character = marker.as_str().chars().next();
            if fence.is_none() {
                fence = character;
            } else if fence == character {
                fence = None;
            }
            continue;
        }
        if fence.is_some() || line.is_empty() || rule.is_match(line) {
            continue;
        }
        let matched = heading.captures(line);
        let text = if let Some(matched) = &matched {
            plain_text(matched.get(1).ok_or(Error("invalid_pattern"))?.as_str())?
        } else {
            plain_text(&bullet.replace(line, ""))?
        };
        if text.is_empty() {
            continue;
        }
        if matched.is_some() {
            if title.is_empty() {
                title = text;
            }
        } else {
            prose.push(text);
        }
    }
    if title.is_empty() {
        if !prose.is_empty() {
            title = prose.remove(0);
        } else {
            let generated = regex::Regex::new(r"^p_[0-9a-f]{32}(?:\.md)?$")
                .map_err(|_| Error("invalid_pattern"))?;
            if !generated.is_match(name) {
                title = plain_text(name.strip_suffix(".md").unwrap_or(name))?;
            }
            if title.is_empty() {
                title = "Untitled post".into();
            }
        }
    }
    let excerpt = prose
        .into_iter()
        .filter(|v| v != &title)
        .collect::<Vec<_>>()
        .join(" ");
    Ok(json!({"title":shorten(&title,96),"excerpt":shorten(&excerpt,180)}))
}

fn public_visible(ctx: &mut Context<'_>, anonymous: &Principal, id: &str) -> Result<bool> {
    ctx.check_deadline()?;
    let operation = ctx.operation()?;
    match ctx.authorizer.require(
        anonymous,
        Entry::Network,
        ctx.now,
        &[AccessRequirement {
            resource_id: id.into(),
            operation,
            check: Check::Read,
        }],
        None,
        ctx.tx,
    ) {
        Ok(_) => Ok(true),
        Err(Error(
            "permission_denied"
            | "credential_ceiling"
            | "certificate_gate"
            | "tool_certificate_required"
            | "delegation_scope"
            | "ancestor_inactive",
        )) => Ok(false),
        Err(error) => Err(error),
    }
}
struct Recommendation {
    resource: Resource,
    revision: Revision,
    score: f64,
    reasons: Vec<&'static str>,
}
fn recommendation_score(
    created: Timestamp,
    now: Timestamp,
    followed: bool,
    tags: &[String],
    interests: &BTreeSet<String>,
) -> Result<(f64, Vec<&'static str>)> {
    let age = (now.0 - created.0)
        .num_microseconds()
        .ok_or(Error("invalid_time"))? as f64
        / 1_000_000.0
        / 3600.0;
    let mut score = (-age.max(0.0) / 24.0).exp2();
    let mut reasons = vec!["recent"];
    if followed {
        score += 3.0;
        reasons.push("followed_author");
    }
    let tags: BTreeSet<_> = tags.iter().map(|t| crate::unicode::casefold(t)).collect();
    let overlap = tags.intersection(interests).count();
    if overlap > 0 {
        score += 2.0 * overlap as f64 / interests.len() as f64;
        reasons.push("interest_match");
    }
    Ok((score, reasons))
}
fn next_recommendation(
    pending: &[Recommendation],
    counts: &HashMap<String, usize>,
) -> Option<usize> {
    (0..pending.len()).min_by(|left, right| {
        let a = &pending[*left];
        let b = &pending[*right];
        let a_score = -a.score / (1 + counts.get(&a.revision.author).copied().unwrap_or(0)) as f64;
        let b_score = -b.score / (1 + counts.get(&b.revision.author).copied().unwrap_or(0)) as f64;
        a_score
            .total_cmp(&b_score)
            .then_with(|| a.resource.id.cmp(&b.resource.id))
    })
}
pub(super) fn recommendations(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let args = ctx.args()?.clone();
    let subject = ctx.principal.subject().map(str::to_owned);
    let mut following = BTreeSet::new();
    let mut budget = ReadBudget::default();
    if let Some(subject) = &subject {
        let operation = ctx.operation()?;
        ctx.authorizer
            .require_base(ctx.principal, &operation, subject, ctx.tx, ctx.now)?;
        for target in follows::effective_targets(ctx.tx, subject)? {
            budget.scan()?;
            let resource = ctx.tx.resource_record(&target)?;
            if resource.resource_type == "user"
                && resource.state == ResourceState::Active
                && !follows::blocked(ctx.tx, Some(subject), Some(&target))?
                && visible(ctx, &target)?
            {
                following.insert(target);
            }
        }
        for row in ctx.tx.rows("SELECT w.resource FROM watches w JOIN resources r ON r.id=w.resource WHERE w.subject=$1 AND r.type='user' AND r.state='active' ORDER BY w.resource LIMIT 1000",&[subject.as_str().into()])?{following.insert(row.text(0)?.into());}
    }
    let interests: Vec<String> = args
        .get("interests")
        .map(|v| {
            v.as_array()?
                .iter()
                .map(crate::content::normalize_tag)
                .collect::<Result<Vec<_>>>()
        })
        .transpose()?
        .unwrap_or_default();
    let folded_interests: BTreeSet<String> = interests
        .iter()
        .map(|t| crate::unicode::casefold(t))
        .collect();
    let anonymous = crate::context::anonymous_principal(ctx)?;
    let stamp = encode(&ctx.now)?.as_str()?.to_owned();
    let mut sources=vec![ctx.tx.rows("SELECT id FROM resources WHERE type='post' AND state='active' AND created_at<=$1 ORDER BY created_at DESC,id DESC LIMIT 128",&[stamp.as_str().into()])?];
    if !following.is_empty() {
        sources.push(ctx.tx.rows("SELECT id FROM resources WHERE type='post' AND state='active' AND created_at<=$1 AND owner=ANY($2::text[]) ORDER BY created_at DESC,id DESC LIMIT 128",&[stamp.into(),Value::TextArray(following.iter().cloned().collect())])?);
    }
    let mut pending = Vec::new();
    let mut hydrated = BTreeSet::new();
    for source in sources {
        for row in source {
            budget.scan()?;
            let id = row.text(0)?;
            if hydrated.contains(id)
                || !public_visible(ctx, &anonymous, id)?
                || (subject.is_some() && !visible(ctx, id)?)
            {
                continue;
            }
            let resource = ctx.tx.resource_record(id)?;
            let revision = ctx.tx.revision(&ResourceRef {
                id: id.into(),
                revision: None,
            })?;
            if let Some(subject) = &subject {
                if follows::blocked(ctx.tx, Some(subject), Some(&resource.owner))?
                    || follows::blocked(ctx.tx, Some(subject), Some(&revision.author))?
                {
                    continue;
                }
            }
            let (score, reasons) = recommendation_score(
                resource.created_at,
                ctx.now,
                following.contains(&revision.author),
                &resource.tags,
                &folded_interests,
            )?;
            hydrated.insert(id.to_owned());
            pending.push(Recommendation {
                resource,
                revision,
                score,
                reasons,
            });
        }
    }
    let limit = arg_int(&args, "limit", 20)?;
    require((1..=100).contains(&limit), "invalid_limit")?;
    let generated = regex::Regex::new(r"^p_[0-9a-f]{32}$").map_err(|_| Error("invalid_pattern"))?;
    let mut counts = HashMap::<String, usize>::new();
    let mut items = Vec::new();
    while !pending.is_empty() && items.len() < limit as usize {
        let index = next_recommendation(&pending, &counts).ok_or(Error("invalid_projection"))?;
        let selected = pending.remove(index);
        let resource = selected.resource;
        let revision = selected.revision;
        *counts.entry(revision.author.clone()).or_default() += 1;
        budget.node(8)?;
        let mut item = json!({"id":resource.id,"revision":revision.id,"author":revision.author,"path":format!("/*{}",hex_id(&resource.id)),"created_at":resource.created_at,"score":selected.score,"reasons":selected.reasons});
        let title = resource.name.strip_suffix(".md").unwrap_or(&resource.name);
        if !generated.is_match(title) {
            item["title"] = json!(title);
        }
        if public_visible(ctx, &anonymous, &revision.author)? {
            item["author_name"] = json!(ctx.tx.resource_record(&revision.author)?.name);
        }
        if let Some(summary) = revision.summary.as_ref().filter(|s| !s.is_empty()) {
            item["summary"] = json!(summary);
        } else if revision.content.media_type.starts_with("text/") && revision.content.size <= 32768
        {
            let bytes = ctx.contents.read_bytes(&revision.content, 32768)?;
            let body = std::str::from_utf8(&bytes).map_err(|_| Error("invalid_utf8_content"))?;
            let preview = post_preview(&resource.name, body)?;
            if item.get("title").is_none() {
                item["title"] = preview["title"].clone();
            }
            item["summary"] = if preview["excerpt"].as_str().is_some_and(|v| !v.is_empty()) {
                preview["excerpt"].clone()
            } else {
                json!(body.chars().take(180).collect::<String>())
            };
        }
        if item.get("title").is_none() {
            item["title"] =
                post_preview(&resource.name, revision.summary.as_deref().unwrap_or(""))?["title"]
                    .clone();
        }
        items.push(item);
    }
    let data = json!({"algorithm":"msg for bot need","algorithm_version":"0.1.0","items":items,"interests":interests});
    budget.output(ctx, &data)?;
    output(data, vec![])
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
        assert_eq!(hex_id("u_root"), "e2ac31d8d6d49e8259c98203a2d7d4a8");
    }
}
