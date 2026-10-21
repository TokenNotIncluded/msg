//! Scope-first lexical search. Ranking, facets and hints see only currently
//! readable resources; relation predicates also authorize every endpoint.
use super::*;
use std::collections::BTreeSet;

fn flag(args: &BTreeMap<String, Json>, name: &str, default: bool) -> Result<bool> {
    args.get(name)
        .map(Json::as_bool)
        .transpose()
        .map(|v| v.unwrap_or(default))
}
fn strings(args: &BTreeMap<String, Json>, name: &str) -> Result<Vec<String>> {
    args.get(name)
        .map(|v| {
            v.as_array()?
                .iter()
                .map(|v| v.as_str().map(str::to_owned))
                .collect()
        })
        .transpose()
        .map(|v| v.unwrap_or_default())
}
fn version(args: &BTreeMap<String, Json>) -> Result<u32> {
    if [
        "revision",
        "source_version",
        "relation_to",
        "relation_from",
        "has_replies",
        "has_references",
        "spell",
    ]
    .iter()
    .any(|s| args.contains_key(*s))
        || args.get("scope").is_some_and(|s| s.as_object().is_ok())
        || matches!(
            arg_text(args, "relation_type")?.as_deref(),
            Some("reference" | "state" | "target" | "content")
        )
        || arg_text(args, "field")?.as_deref() == Some("title")
        || strings(args, "fields")?.iter().any(|s| s == "title")
    {
        Ok(5)
    } else if args.contains_key("suggest") {
        Ok(4)
    } else if args.contains_key("source_kind") || args.contains_key("relation_type") {
        Ok(3)
    } else if args.contains_key("facets") {
        Ok(2)
    } else {
        Ok(1)
    }
}
fn scope(ctx: &mut Context<'_>, input: &Json) -> Result<(Json, Vec<String>, Vec<String>)> {
    let (kind, supplied) = if let Ok(mapping) = input.as_object() {
        require(mapping.len() == 1, "invalid_search_scope")?;
        let (kind, value) = mapping
            .first_key_value()
            .ok_or(Error("invalid_search_scope"))?;
        let supplied = match kind.as_str() {
            "resource_refs" => {
                let refs = value.as_array()?;
                require((1..=32).contains(&refs.len()), "invalid_search_scope")?;
                refs.iter()
                    .map(|v| {
                        v.as_object()?
                            .get("id")
                            .ok_or(Error("invalid_search_scope"))?
                            .as_str()
                            .map(str::to_owned)
                    })
                    .collect::<Result<Vec<_>>>()?
            }
            "subject" | "org" => vec![value.as_str()?.to_owned()],
            _ => return Err(Error("invalid_search_scope")),
        };
        (Some(kind.clone()), supplied)
    } else {
        (None, vec![input.as_str()?.to_owned()])
    };
    let mut ids = BTreeSet::new();
    let mut owners = BTreeSet::new();
    for supplied in supplied {
        let id = ctx.resolve(&supplied, false)?;
        let resource = ctx.tx.resource_record(&id)?;
        require(
            match kind.as_deref() {
                Some("subject") => resource.resource_type == "user",
                Some("org") => resource.resource_type == "organization",
                _ => true,
            },
            "invalid_search_scope",
        )?;
        ctx.check(&id, Check::List)?;
        if matches!(resource.resource_type.as_str(), "user" | "organization") {
            owners.insert(id.clone());
        }
        ids.insert(id);
    }
    let ids: Vec<_> = ids.into_iter().collect();
    let first = ids.first().ok_or(Error("invalid_search_scope"))?;
    let normalized = match kind.as_deref() {
        Some("resource_refs") => {
            json!({"resource_refs":ids.iter().map(|id|json!({"id":id})).collect::<Vec<_>>()})
        }
        Some(kind) => json!({kind:first}),
        None => json!(first),
    };
    Ok((encode(&normalized)?, ids, owners.into_iter().collect()))
}
#[derive(PartialEq, Eq, PartialOrd, Ord)]
enum Atom {
    Number(i64),
    Text(String),
}
#[derive(PartialEq, Eq, PartialOrd, Ord)]
struct Key(Vec<Atom>);
impl Key {
    fn parse(value: &J) -> Result<Self> {
        Ok(Self(
            value
                .as_array()
                .ok_or(Error("invalid_cursor"))?
                .iter()
                .map(|v| {
                    if let Some(number) = v.as_i64() {
                        Ok(Atom::Number(number))
                    } else {
                        Ok(Atom::Text(
                            v.as_str().ok_or(Error("invalid_cursor"))?.to_owned(),
                        ))
                    }
                })
                .collect::<Result<Vec<_>>>()?,
        ))
    }
    fn json(&self) -> J {
        json!(self
            .0
            .iter()
            .map(|v| match v {
                Atom::Number(v) => json!(v),
                Atom::Text(v) => json!(v),
            })
            .collect::<Vec<_>>())
    }
}
fn timestamp_key(time: Timestamp) -> i64 {
    -(time.0.timestamp_micros() as f64 / 1_000_000.0 * 1_000_000.0) as i64
}
fn sort_key(order: &str, score: i64, resource: &Resource) -> Result<Key> {
    let mut key = match order {
        "relevance" => vec![
            Atom::Number(-score),
            Atom::Number(timestamp_key(resource.modified_at)),
        ],
        "updated" => vec![Atom::Number(timestamp_key(resource.modified_at))],
        "created" => vec![Atom::Number(timestamp_key(resource.created_at))],
        "name" => vec![Atom::Text(crate::unicode::casefold(&resource.name))],
        _ => return Err(Error("invalid_sort")),
    };
    key.push(Atom::Text(resource.id.clone()));
    Ok(Key(key))
}
fn folded_terms(text: &str) -> Vec<String> {
    crate::unicode::casefold(text)
        .split(py_whitespace)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect()
}
fn words(text: &str) -> impl Iterator<Item = &str> {
    text.split(|c| !crate::unicode::is_alphanumeric(c) && c != '_' && c != '-')
        .filter(|s| !s.is_empty())
}
fn distance(left: &str, right: &str, maximum: usize) -> usize {
    let left: Vec<char> = left.chars().collect();
    let right: Vec<char> = right.chars().collect();
    if left.len().abs_diff(right.len()) > maximum {
        return maximum + 1;
    }
    let mut previous: Vec<usize> = (0..=right.len()).collect();
    for (index, character) in left.iter().enumerate() {
        let mut row = vec![index + 1];
        for (other, target) in right.iter().enumerate() {
            row.push(
                (row[other] + 1)
                    .min(previous[other + 1] + 1)
                    .min(previous[other] + usize::from(character != target)),
            );
        }
        if row.iter().copied().min().unwrap_or(0) > maximum {
            return maximum + 1;
        }
        previous = row;
    }
    *previous.last().unwrap_or(&0)
}
fn original_span(text: &str, start: usize, end: usize) -> Result<(usize, usize)> {
    let mut offset = 0;
    let mut original_start = None;
    for (index, character) in text.chars().enumerate() {
        offset += crate::unicode::casefold(&character.to_string())
            .chars()
            .count();
        if original_start.is_none() && offset > start {
            original_start = Some(index);
        }
        if offset >= end {
            return Ok((
                original_start.ok_or(Error("invalid_search_match"))?,
                index + 1,
            ));
        }
    }
    Err(Error("invalid_search_match"))
}
fn snippet(field: &str, text: &str, folded: &str, needle: &str) -> Result<J> {
    let byte = folded.find(needle).ok_or(Error("invalid_search_match"))?;
    let start = folded[..byte].chars().count();
    let (start, end) = original_span(text, start, start + needle.chars().count())?;
    let left = start.saturating_sub(40);
    let right = text.chars().count().min(end + 40);
    Ok(
        json!({"field":field,"text":text.chars().skip(left).take(right-left).collect::<String>(),"range":[start-left,end-left]}),
    )
}
fn endpoint(ctx: &mut Context<'_>, cache: &mut BTreeMap<String, bool>, id: &str) -> Result<bool> {
    if let Some(allowed) = cache.get(id) {
        return Ok(*allowed);
    }
    let allowed = visible(ctx, id)?;
    cache.insert(id.into(), allowed);
    Ok(allowed)
}
struct Relations {
    scanned: usize,
    visible: BTreeMap<String, bool>,
}
impl Relations {
    fn allows(
        &mut self,
        ctx: &mut Context<'_>,
        args: &BTreeMap<String, Json>,
        resource: &Resource,
        revision: Option<&Revision>,
        snapshot: Timestamp,
        version: u32,
    ) -> Result<bool> {
        let kind = arg_text(args, "relation_type")?.filter(|s| !s.is_empty());
        let relation_to = arg_text(args, "relation_to")?.filter(|s| !s.is_empty());
        let relation_from = arg_text(args, "relation_from")?.filter(|s| !s.is_empty());
        let attachment = args.get("has_attachment").map(Json::as_bool).transpose()?;
        if version < 5 {
            return Ok(kind.as_ref().is_none_or(|kind| {
                revision.is_some_and(|r| r.relations.iter().any(|rel| &rel.relation_type == kind))
            }) && attachment.is_none_or(|expected| {
                revision.is_some_and(|r| {
                    r.relations
                        .iter()
                        .any(|rel| rel.relation_type == "attachment")
                }) == expected
            }));
        }
        let mut outgoing = Vec::new();
        let mut incoming = Vec::new();
        if ["relation_to", "relation_type", "has_attachment"]
            .iter()
            .any(|s| args.contains_key(*s))
        {
            for relation in revision.map(|r| r.relations.as_slice()).unwrap_or_default() {
                self.scanned += 1;
                require(self.scanned <= 4096, "query_cost_exceeded")?;
                ctx.check_deadline()?;
                if endpoint(ctx, &mut self.visible, &relation.target.id)? {
                    outgoing.push(relation);
                }
            }
        }
        if ["relation_from", "has_replies", "has_references"]
            .iter()
            .any(|s| args.contains_key(*s))
        {
            let rows=ctx.tx.rows("SELECT rel.source_id,rel.type FROM relations rel JOIN resources r ON r.id=rel.source_id AND r.revision=rel.revision_id WHERE rel.target_id=$1 AND r.state='active' AND r.created_at<=$2 ORDER BY rel.source_id,rel.type LIMIT 2049",&[resource.id.as_str().into(),encode(&snapshot)?.as_str()?.into()])?;
            require(rows.len() <= 2048, "query_cost_exceeded")?;
            for row in rows {
                self.scanned += 1;
                require(self.scanned <= 4096, "query_cost_exceeded")?;
                ctx.check_deadline()?;
                if endpoint(ctx, &mut self.visible, row.text(0)?)? {
                    incoming.push((row.text(0)?.to_owned(), row.text(1)?.to_owned()));
                }
            }
        }
        if relation_to.as_ref().is_some_and(|target| {
            !outgoing.iter().any(|r| {
                &r.target.id == target && kind.as_ref().is_none_or(|k| k == &r.relation_type)
            })
        }) {
            return Ok(false);
        }
        if relation_from.as_ref().is_some_and(|source| {
            !incoming
                .iter()
                .any(|(id, k)| id == source && kind.as_ref().is_none_or(|kind| kind == k))
        }) {
            return Ok(false);
        }
        if relation_from.is_none()
            && kind
                .as_ref()
                .is_some_and(|kind| !outgoing.iter().any(|r| &r.relation_type == kind))
        {
            return Ok(false);
        }
        if let Some(expected) = args.get("has_replies") {
            if incoming.iter().any(|(_, kind)| kind == "reply_to") != expected.as_bool()? {
                return Ok(false);
            }
        }
        if let Some(expected) = args.get("has_references") {
            if !incoming.is_empty() != expected.as_bool()? {
                return Ok(false);
            }
        }
        Ok(attachment.is_none_or(|expected| {
            outgoing.iter().any(|r| r.relation_type == "attachment") == expected
        }))
    }
}
fn buckets(values: &BTreeMap<String, i64>, limit: usize) -> Vec<J> {
    let mut sorted: Vec<_> = values.iter().collect();
    sorted.sort_by(|(a, ac), (b, bc)| bc.cmp(ac).then_with(|| a.cmp(b)));
    sorted
        .into_iter()
        .take(limit)
        .map(|(value, count)| json!({"value":value,"count":count}))
        .collect()
}

pub(super) fn handle(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let mut args = ctx.args()?.clone();
    let cursor = arg_text(&args, "cursor")?.filter(|s| !s.is_empty());
    if let Some(cursor) = &cursor {
        require(args.len() == 1, "cursor_query_mismatch")?;
        let (saved, _) = inspect_page(ctx, cursor)?;
        require(
            saved["operation"] == "discovery.lexical_search",
            "cursor_query_mismatch",
        )?;
        args = encode(&saved["arguments"])?.into_object()?;
    }
    let contract: u32 = ctx
        .request
        .field("contract_version")?
        .as_integer()?
        .parse()
        .map_err(|_| Error("invalid_version"))?;
    require(version(&args)? <= contract, "cursor_query_mismatch")?;
    let scope_arg = args
        .get("scope")
        .ok_or(Error("search_scope_required"))?
        .clone();
    let (normalized_scope, scopes, owners) = scope(ctx, &scope_arg)?;
    args.insert("scope".into(), normalized_scope);
    if let Some(tag) = args
        .get("tag")
        .filter(|v| v.as_str().is_ok_and(|v| !v.is_empty()))
        .cloned()
    {
        args.insert(
            "tag".into(),
            Json::string(crate::content::normalize_tag(&tag)?),
        );
    }
    let terms = arg_text(&args, "terms")?.unwrap_or_default();
    let exact = arg_text(&args, "exact")?.unwrap_or_default();
    let excluded = arg_text(&args, "not_terms")?.unwrap_or_default();
    require(
        [&terms, &exact, &excluded]
            .iter()
            .all(|s| s.chars().count() <= 512),
        "query_cost_exceeded",
    )?;
    let terms = folded_terms(&terms);
    let excluded = folded_terms(&excluded);
    let exact = crate::unicode::casefold(&exact);
    require(
        (!terms.is_empty() || !exact.is_empty()) && terms.len() <= 8 && excluded.len() <= 8,
        "search_query_required",
    )?;
    let limit = arg_int(&args, "limit", 50)?;
    require((1..=100).contains(&limit), "invalid_limit")?;
    let selected = if args.contains_key("fields") {
        fields(&args)?
    } else {
        [
            "id",
            "path",
            "type",
            "name",
            "revision",
            "author",
            "created_at",
        ]
        .into_iter()
        .map(str::to_owned)
        .collect()
    };
    require(
        selected.iter().all(|s| {
            matches!(
                s.as_str(),
                "id" | "path"
                    | "type"
                    | "name"
                    | "title"
                    | "revision"
                    | "author"
                    | "owner"
                    | "created_at"
                    | "modified_at"
                    | "score"
                    | "rank_reason"
                    | "snippet"
                    | "links"
            )
        }),
        "unknown_projection_field",
    )?;
    require(
        limit as usize * (selected.len() + 2) <= 1200,
        "query_cost_exceeded",
    )?;
    args.remove("cursor");
    let normalized = value(&args)?;
    let (position, snapshot) = if let Some(cursor) = &cursor {
        decode_page(ctx, cursor, "discovery.lexical_search", &normalized)?
    } else {
        (json!([]), ctx.now)
    };
    let position = Key::parse(&position)?;
    let mut cutoffs = BTreeMap::new();
    for key in [
        "created_after",
        "created_before",
        "updated_after",
        "updated_before",
    ] {
        if let Some(text) = arg_text(&args, key)? {
            cutoffs.insert(key, Timestamp::parse(&text)?);
        }
    }
    let owner = arg_text(&args, "owner")?
        .filter(|s| !s.is_empty())
        .map(|s| ctx.resolve(&s, false))
        .transpose()?;
    let author = arg_text(&args, "author")?
        .filter(|s| !s.is_empty())
        .map(|s| ctx.resolve(&s, false))
        .transpose()?;
    let kind = arg_text(&args, "type")?.filter(|s| !s.is_empty());
    let tag = arg_text(&args, "tag")?.filter(|s| !s.is_empty());
    let state = arg_text(&args, "state")?.unwrap_or("active".into());
    let field = arg_text(&args, "field")?.unwrap_or("all".into());
    let mode = arg_text(&args, "mode")?.unwrap_or("all".into());
    let order = arg_text(&args, "order")?.unwrap_or("relevance".into());
    let depth = arg_int(&args, "depth", 5)?;
    require((0..=5).contains(&depth), "query_cost_exceeded")?;
    let recursive = flag(&args, "recursive", true)?;
    let spelling = flag(&args, "spell", false)?;
    let suggest = flag(&args, "suggest", false)?;
    let suggest_prefix = if suggest {
        terms.last().cloned().unwrap_or_default()
    } else {
        String::new()
    };
    if spelling {
        require(
            (1..=4).contains(&terms.len())
                && terms.iter().all(|s| (2..=32).contains(&s.chars().count())),
            "query_cost_exceeded",
        )?;
    }
    if suggest {
        require(
            (2..=32).contains(&suggest_prefix.chars().count()),
            "query_cost_exceeded",
        )?;
    }
    let mut facets = BTreeMap::<String, BTreeMap<String, i64>>::new();
    for name in strings(&args, "facets")? {
        require(
            matches!(name.as_str(), "type" | "tag"),
            "invalid_search_facet",
        )?;
        facets.insert(name, BTreeMap::new());
    }
    let mut suggestions = BTreeMap::new();
    let mut corpus = BTreeMap::<String, i64>::new();
    let mut results = Vec::new();
    let mut scanned = 0;
    let mut relations = Relations {
        scanned: 0,
        visible: BTreeMap::new(),
    };
    let mut after = String::new();
    loop {
        ctx.check_deadline()?;
        let rows=ctx.tx.rows("WITH RECURSIVE subtree(id,depth) AS (SELECT id,0 FROM resources WHERE id=ANY($1::text[]) UNION SELECT r.id,s.depth+1 FROM resources r JOIN subtree s ON r.parent=s.id WHERE s.depth<5) SELECT body,id FROM resources WHERE created_at<=$2 AND (id IN (SELECT id FROM subtree) OR owner=ANY($3::text[]) OR grp=ANY($3::text[])) AND id>$4 ORDER BY id LIMIT 128",&[Value::TextArray(scopes.clone()),encode(&snapshot)?.as_str()?.into(),Value::TextArray(owners.clone()),after.as_str().into()])?;
        let count = rows.len();
        for row in rows {
            ctx.check_deadline()?;
            after = row.text(1)?.into();
            let resource: Resource = decode(&row.json(0)?)?;
            if value(&resource.state)? != state {
                continue;
            }
            let chain = ctx.tx.ancestors(&resource.id)?;
            let mut distances: Vec<_> = scopes
                .iter()
                .filter_map(|scope| {
                    chain
                        .iter()
                        .position(|r| &r.id == scope)
                        .map(|index| chain.len() - index)
                })
                .collect();
            if scopes.contains(&resource.id)
                || owners.contains(&resource.owner)
                || owners.contains(&resource.group)
            {
                distances.push(0);
            }
            let Some(distance) = distances.into_iter().min() else {
                continue;
            };
            if distance > depth as usize || (!recursive && distance > 1) {
                continue;
            }
            if kind.as_ref().is_some_and(|v| v != &resource.resource_type)
                || owner.as_ref().is_some_and(|v| v != &resource.owner)
                || tag.as_ref().is_some_and(|v| !resource.tags.contains(v))
            {
                continue;
            }
            if cutoffs
                .get("created_after")
                .is_some_and(|v| resource.created_at < *v)
                || cutoffs
                    .get("created_before")
                    .is_some_and(|v| resource.created_at > *v)
                || cutoffs
                    .get("updated_after")
                    .is_some_and(|v| resource.modified_at < *v)
                || cutoffs
                    .get("updated_before")
                    .is_some_and(|v| resource.modified_at > *v)
            {
                continue;
            }
            if !visible(ctx, &resource.id)? {
                continue;
            }
            scanned += 1;
            require(scanned <= 2000, "query_cost_exceeded")?;
            let revision = resource
                .revision
                .as_ref()
                .map(|_| ctx.tx.revision(&ref_for(&resource)))
                .transpose()?;
            if let Some(expected) = arg_text(&args, "revision")?.filter(|v| !v.is_empty()) {
                if revision.as_ref().is_none_or(|r| r.id != expected) {
                    continue;
                }
            }
            if args.contains_key("source_version")
                && revision.as_ref().and_then(|r| r.source_version)
                    != Some(arg_int(&args, "source_version", 0)?)
            {
                continue;
            }
            if let Some(expected) = arg_text(&args, "source_kind")?.filter(|v| !v.is_empty()) {
                let actual = revision
                    .as_ref()
                    .and_then(|r| r.source_kind.as_ref())
                    .map(value)
                    .transpose()?;
                if actual != Some(json!(expected)) {
                    continue;
                }
            }
            if !relations.allows(ctx, &args, &resource, revision.as_ref(), snapshot, contract)? {
                continue;
            }
            if author
                .as_ref()
                .is_some_and(|author| revision.as_ref().is_none_or(|r| &r.author != author))
            {
                continue;
            }
            if spelling {
                require(resource.name.chars().count() <= 256, "query_cost_exceeded")?;
                let words: BTreeSet<_> = words(&resource.name)
                    .filter(|w| (2..=32).contains(&w.chars().count()))
                    .map(crate::unicode::casefold)
                    .collect();
                for word in words {
                    if word.chars().count() <= 32 {
                        *corpus.entry(word).or_default() += 1;
                    }
                }
                require(corpus.len() <= 512, "query_cost_exceeded")?;
            }
            let mut body = String::new();
            if matches!(field.as_str(), "all" | "body") {
                if let Some(revision) = &revision {
                    if revision.content.media_type.starts_with("text/") {
                        if revision.content.size <= 65536 {
                            body = String::from_utf8(
                                ctx.contents.read_bytes(&revision.content, 65536)?,
                            )
                            .map_err(|_| Error("invalid_utf8_content"))?;
                        } else {
                            require(field != "body", "query_cost_exceeded")?;
                        }
                    }
                }
            }
            let metadata = format!(
                "{} {} {}",
                resource.resource_type, resource.owner, resource.group
            );
            let active: Vec<(&str, &str)> = match field.as_str() {
                "all" => vec![
                    ("name", &resource.name),
                    ("body", &body),
                    ("metadata", &metadata),
                ],
                "name" => vec![("name", &resource.name)],
                "title" => vec![("title", &resource.name)],
                "body" => vec![("body", &body)],
                "metadata" => vec![("metadata", &metadata)],
                _ => return Err(Error("invalid_search_field")),
            };
            let lowered: Vec<_> = active
                .iter()
                .map(|(key, text)| (*key, crate::unicode::casefold(text)))
                .collect();
            let whole = lowered
                .iter()
                .map(|(_, s)| s.as_str())
                .collect::<Vec<_>>()
                .join(" ");
            if !terms.is_empty()
                && !if mode == "all" {
                    terms.iter().all(|term| whole.contains(term))
                } else {
                    terms.iter().any(|term| whole.contains(term))
                }
            {
                continue;
            }
            if (!exact.is_empty() && !lowered.iter().any(|(_, v)| v.contains(&exact)))
                || excluded.iter().any(|term| whole.contains(term))
            {
                continue;
            }
            let score=lowered.iter().map(|(key,text)|if matches!(*key,"name"|"title"){5}else{1}*terms.iter().map(|term|text.matches(term.as_str()).count() as i64).sum::<i64>()).sum::<i64>()+if exact.is_empty(){0}else{3};
            let key = sort_key(&order, score, &resource)?;
            let mut item = json!({"ref":ref_for(&resource),"id":resource.id,"path":short_path(&ctx.tx.resource_path(&resource.id)?),"type":resource.resource_type,"name":resource.name,"revision":resource.revision,"author":revision.as_ref().map(|r|&r.author),"owner":resource.owner,"created_at":resource.created_at,"modified_at":resource.modified_at,"score":score});
            if selected.iter().any(|s| s == "title") {
                item["title"] = json!(resource.name);
            }
            if flag(&args, "snippet", false)? {
                for ((field, text), (_, low)) in active.iter().zip(&lowered) {
                    let needle = if !exact.is_empty() && low.contains(&exact) {
                        Some(exact.as_str())
                    } else {
                        terms
                            .iter()
                            .find(|term| low.contains(term.as_str()))
                            .map(String::as_str)
                    };
                    if let Some(needle) = needle {
                        item["snippet"] = snippet(field, text, low, needle)?;
                        break;
                    }
                }
            }
            if arg_text(&args, "explain")?.as_deref() == Some("compact") {
                item["rank_reason"] = json!({"matched_fields":lowered.iter().filter(|(_,text)|terms.iter().any(|term|text.contains(term))||(!exact.is_empty()&&text.contains(&exact))).map(|(key,_)|key).collect::<Vec<_>>(),"terms":terms,"order":order});
            }
            if selected.iter().any(|s| s == "links") {
                item["links"] = basic_links(ctx, &resource, revision.as_ref())?;
            }
            if args.contains_key("fields") && !selected.is_empty() {
                item.as_object_mut()
                    .ok_or(Error("invalid_projection"))?
                    .retain(|k, _| selected.contains(k));
            }
            results.push((key, item));
            if suggest {
                require(resource.name.chars().count() <= 256, "query_cost_exceeded")?;
                let words: BTreeSet<_> = words(&resource.name)
                    .map(crate::unicode::casefold)
                    .collect();
                for word in words {
                    if word != suggest_prefix
                        && word.starts_with(&suggest_prefix)
                        && word.chars().count() <= 32
                    {
                        *suggestions.entry(word).or_default() += 1;
                        require(suggestions.len() <= 100, "query_cost_exceeded")?;
                    }
                }
            }
            for (name, values) in &mut facets {
                let keys = if name == "type" {
                    std::slice::from_ref(&resource.resource_type)
                } else {
                    resource.tags.as_slice()
                };
                for key in keys {
                    *values.entry(key.clone()).or_default() += 1;
                    require(values.len() <= 20, "query_cost_exceeded")?;
                }
            }
        }
        if count < 128 {
            break;
        }
    }
    results.sort_by(|(a, _), (b, _)| a.cmp(b));
    let mut following = results
        .into_iter()
        .filter(|(key, _)| position.0.is_empty() || key > &position)
        .collect::<Vec<_>>();
    let more = following.len() > limit as usize;
    following.truncate(limit as usize);
    let last = following.last().map(|(key, _)| key.json());
    let items = following
        .into_iter()
        .map(|(_, item)| item)
        .collect::<Vec<_>>();
    let mut data = json!({"items":items});
    if !facets.is_empty() {
        data["facets"] = json!(facets
            .into_iter()
            .map(|(name, values)| (name, buckets(&values, 20)))
            .collect::<BTreeMap<_, _>>());
    }
    if suggest {
        data["suggestions"] = json!(buckets(&suggestions, 10));
    }
    if spelling {
        let mut hints = Vec::new();
        let mut seen = BTreeSet::new();
        for term in &terms {
            if !seen.insert(term) {
                continue;
            }
            let maximum = if term.chars().count() <= 4 { 1 } else { 2 };
            let mut matches = Vec::new();
            if !corpus.contains_key(term) {
                for (word, count) in &corpus {
                    ctx.check_deadline()?;
                    let distance = distance(term, word, maximum);
                    if distance <= maximum {
                        matches.push((distance, -count, word));
                    }
                }
            }
            matches.sort();
            hints.push(json!({"term":term,"suggestions":matches.into_iter().take(5).map(|(distance,count,word)|json!({"value":word,"distance":distance,"count":-count})).collect::<Vec<_>>()}));
        }
        data["spelling"] = json!({"source":"name","terms":hints});
    }
    if more {
        let cursor = encode_page(
            ctx,
            "discovery.lexical_search",
            &normalized,
            &last.ok_or(Error("invalid_cursor"))?,
            snapshot,
        )?;
        data["cursor"] = json!(cursor);
        data["next"] = json!(format!("/_r/c/{cursor}"));
        data["next_requires_auth"] = json!(ctx.principal.subject().is_some());
    }
    output(data, vec![])
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn snippets_translate_full_casefold_expansions_to_whole_source_characters() {
        let data = snippet("body", "自由 Straße İ", "自由 strasse i\u{307}", "ss").unwrap();
        assert_eq!(data["range"], json!([7, 8]));
        assert_eq!(data["text"], "自由 Straße İ");
        assert_eq!(original_span("ﬃ", 1, 2).unwrap(), (0, 1));
        assert_eq!(distance("mgs", "msg", 2), 2);
        assert_eq!(distance("自由", "自由度", 1), 1);
        assert_eq!(distance("abc", "abcdef", 1), 2);
    }
    #[test]
    fn version_selection_and_unicode_name_tokens_are_explicit() {
        let a = encode(&json!({"scope":{"subject":"u_a"},"terms":"x"}))
            .unwrap()
            .into_object()
            .unwrap();
        assert_eq!(version(&a).unwrap(), 5);
        let a = encode(&json!({"scope":"r_root","terms":"x","suggest":false}))
            .unwrap()
            .into_object()
            .unwrap();
        assert_eq!(version(&a).unwrap(), 4);
        assert_eq!(
            words("²Ⅷ free_space-自由,ok").collect::<Vec<_>>(),
            vec!["²Ⅷ", "free_space-自由", "ok"]
        );
        assert_eq!(folded_terms(" A\u{1c}Straße "), vec!["a", "strasse"]);
    }
}
