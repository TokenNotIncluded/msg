//! Native unified diffs with Python SequenceMatcher's stable tie-breaking and
//! popular-line heuristic. Record boundaries survive missing final newlines.
use super::*;
use std::{collections::HashMap, time::Instant};

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Equal,
    Replace,
    Insert,
    Delete,
}
#[derive(Clone, Copy)]
struct Code {
    kind: Kind,
    a: usize,
    end_a: usize,
    b: usize,
    end_b: usize,
}
#[derive(serde::Serialize)]
struct DiffLine {
    kind: &'static str,
    old_line: Option<usize>,
    new_line: Option<usize>,
    text: String,
}
fn deadline(deadline: Instant) -> Result<()> {
    require(Instant::now() < deadline, "query_cost_exceeded")
}
fn blocks(a: &[&str], b: &[&str], expires: Instant) -> Result<Vec<(usize, usize, usize)>> {
    let mut occurrences = HashMap::<&str, Vec<usize>>::new();
    for (index, line) in b.iter().enumerate() {
        occurrences.entry(line).or_default().push(index);
    }
    if b.len() >= 200 {
        let threshold = b.len() / 100 + 1;
        occurrences.retain(|_, positions| positions.len() <= threshold);
    }
    let mut pending = vec![(0, a.len(), 0, b.len())];
    let mut matches = Vec::new();
    while let Some((low_a, high_a, low_b, high_b)) = pending.pop() {
        let (mut best_a, mut best_b, mut size) = (low_a, low_b, 0);
        let mut previous = HashMap::<usize, usize>::new();
        for (index, line) in a.iter().enumerate().take(high_a).skip(low_a) {
            deadline(expires)?;
            let mut next = HashMap::new();
            if let Some(positions) = occurrences.get(line) {
                for &other in positions {
                    if other < low_b {
                        continue;
                    }
                    if other >= high_b {
                        break;
                    }
                    let length = other
                        .checked_sub(1)
                        .and_then(|p| previous.get(&p))
                        .copied()
                        .unwrap_or(0)
                        + 1;
                    next.insert(other, length);
                    if length > size {
                        best_a = index + 1 - length;
                        best_b = other + 1 - length;
                        size = length;
                    }
                }
            }
            previous = next;
        }
        while best_a > low_a && best_b > low_b && a[best_a - 1] == b[best_b - 1] {
            best_a -= 1;
            best_b -= 1;
            size += 1;
        }
        while best_a + size < high_a
            && best_b + size < high_b
            && a[best_a + size] == b[best_b + size]
        {
            size += 1;
        }
        if size > 0 {
            matches.push((best_a, best_b, size));
            if low_a < best_a && low_b < best_b {
                pending.push((low_a, best_a, low_b, best_b));
            }
            if best_a + size < high_a && best_b + size < high_b {
                pending.push((best_a + size, high_a, best_b + size, high_b));
            }
        }
    }
    matches.sort_unstable();
    let mut combined: Vec<(usize, usize, usize)> = Vec::new();
    for (i, j, length) in matches {
        if let Some((previous_a, previous_b, previous_length)) = combined.last_mut() {
            if *previous_a + *previous_length == i && *previous_b + *previous_length == j {
                *previous_length += length;
                continue;
            }
        }
        combined.push((i, j, length));
    }
    combined.push((a.len(), b.len(), 0));
    Ok(combined)
}
fn groups(a: &[&str], b: &[&str], expires: Instant) -> Result<Vec<Vec<Code>>> {
    let (mut i, mut j) = (0, 0);
    let mut codes = Vec::new();
    for (ai, bj, size) in blocks(a, b, expires)? {
        let kind = if i < ai && j < bj {
            Some(Kind::Replace)
        } else if i < ai {
            Some(Kind::Delete)
        } else if j < bj {
            Some(Kind::Insert)
        } else {
            None
        };
        if let Some(kind) = kind {
            codes.push(Code {
                kind,
                a: i,
                end_a: ai,
                b: j,
                end_b: bj,
            });
        }
        i = ai + size;
        j = bj + size;
        if size > 0 {
            codes.push(Code {
                kind: Kind::Equal,
                a: ai,
                end_a: i,
                b: bj,
                end_b: j,
            });
        }
    }
    if codes.is_empty() {
        return Ok(Vec::new());
    }
    if let Some(first) = codes.first_mut() {
        if first.kind == Kind::Equal {
            first.a = first.a.max(first.end_a.saturating_sub(3));
            first.b = first.b.max(first.end_b.saturating_sub(3));
        }
    }
    if let Some(last) = codes.last_mut() {
        if last.kind == Kind::Equal {
            last.end_a = last.end_a.min(last.a + 3);
            last.end_b = last.end_b.min(last.b + 3);
        }
    }
    let mut groups = Vec::new();
    let mut current = Vec::new();
    for mut code in codes {
        if code.kind == Kind::Equal && code.end_a - code.a > 6 {
            current.push(Code {
                end_a: code.end_a.min(code.a + 3),
                end_b: code.end_b.min(code.b + 3),
                ..code
            });
            groups.push(std::mem::take(&mut current));
            code.a = code.a.max(code.end_a - 3);
            code.b = code.b.max(code.end_b - 3);
        }
        current.push(code);
    }
    if !(current.is_empty() || current.len() == 1 && current[0].kind == Kind::Equal) {
        groups.push(current);
    }
    Ok(groups)
}
fn range(start: usize, end: usize) -> String {
    match end - start {
        0 => format!("{start},0"),
        1 => (start + 1).to_string(),
        length => format!("{},{length}", start + 1),
    }
}
fn unified(a: &str, b: &str, from: &str, to: &str, expires: Instant) -> Result<Vec<DiffLine>> {
    let a = split_lines(a, true);
    let b = split_lines(b, true);
    let mut lines = Vec::new();
    for group in groups(&a, &b, expires)? {
        deadline(expires)?;
        if lines.is_empty() {
            for text in [format!("--- {from}\n"), format!("+++ {to}\n")] {
                lines.push(DiffLine {
                    kind: "header",
                    old_line: None,
                    new_line: None,
                    text,
                });
            }
        }
        let first = group.first().ok_or(Error("invalid_diff"))?;
        let last = group.last().ok_or(Error("invalid_diff"))?;
        lines.push(DiffLine {
            kind: "hunk",
            old_line: None,
            new_line: None,
            text: format!(
                "@@ -{} +{} @@\n",
                range(first.a, last.end_a),
                range(first.b, last.end_b)
            ),
        });
        for code in group {
            if code.kind == Kind::Equal {
                for (offset, text) in a[code.a..code.end_a].iter().enumerate() {
                    lines.push(DiffLine {
                        kind: "context",
                        old_line: Some(code.a + offset + 1),
                        new_line: Some(code.b + offset + 1),
                        text: format!(" {text}"),
                    });
                }
            } else {
                if matches!(code.kind, Kind::Replace | Kind::Delete) {
                    for (offset, text) in a[code.a..code.end_a].iter().enumerate() {
                        lines.push(DiffLine {
                            kind: "delete",
                            old_line: Some(code.a + offset + 1),
                            new_line: None,
                            text: format!("-{text}"),
                        });
                    }
                }
                if matches!(code.kind, Kind::Replace | Kind::Insert) {
                    for (offset, text) in b[code.b..code.end_b].iter().enumerate() {
                        lines.push(DiffLine {
                            kind: "insert",
                            old_line: None,
                            new_line: Some(code.b + offset + 1),
                            text: format!("+{text}"),
                        });
                    }
                }
            }
        }
    }
    Ok(lines)
}
fn text(ctx: &Context<'_>, revision: &Revision) -> Result<String> {
    require(
        revision.content.media_type.starts_with("text/")
            || revision.content.media_type == "application/json",
        "text_diff_required",
    )?;
    let maximum = ctx
        .settings
        .max_response_bytes
        .checked_mul(4)
        .ok_or(Error("diff_requires_transfer"))?;
    require(
        revision.content.size >= 0 && revision.content.size as u64 <= maximum as u64,
        "diff_requires_transfer",
    )?;
    String::from_utf8(ctx.contents.read_bytes(&revision.content, maximum as u64)?)
        .map_err(|_| Error("invalid_utf8_content"))
}
fn window(args: &BTreeMap<String, Json>, default: i64) -> Result<(usize, usize)> {
    let offset = arg_int(args, "offset", 0)?;
    let limit = arg_int(args, "limit", default)?;
    require(
        offset >= 0 && (1..=500).contains(&limit),
        "invalid_diff_range",
    )?;
    Ok((
        usize::try_from(offset).map_err(|_| Error("invalid_diff_range"))?,
        limit as usize,
    ))
}
pub(super) fn legacy(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let left: ResourceRef = decode(ctx.arg("left")?)?;
    let right: ResourceRef = decode(ctx.arg("right")?)?;
    let mut contents = Vec::new();
    for reference in [&left, &right] {
        ctx.check(&reference.id, Check::Read)?;
        contents.push(text(ctx, &ctx.tx.revision(reference)?)?);
    }
    let from = format!("{}@{}", left.id, left.revision.as_deref().unwrap_or("None"));
    let to = format!(
        "{}@{}",
        right.id,
        right.revision.as_deref().unwrap_or("None")
    );
    let lines = unified(&contents[0], &contents[1], &from, &to, ctx.deadline)?;
    let (offset, limit) = window(ctx.args()?, 100)?;
    let end = offset.saturating_add(limit);
    output(
        json!({"diff":lines.iter().skip(offset).take(limit).map(|r|r.text.as_str()).collect::<String>(),"next_offset":if end<lines.len(){Some(end)}else{None}}),
        vec![],
    )
}
pub(super) fn view(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let args = ctx.args()?.clone();
    let id = ctx.resolve(ctx.text("id")?, false)?;
    ctx.check(&id, Check::Read)?;
    let resource = ctx.tx.resource_record(&id)?;
    require(
        resource.state != ResourceState::Purged && resource.revision.is_some(),
        "revision_not_found",
    )?;
    let previous = args
        .get("previous")
        .map(Json::as_bool)
        .transpose()?
        .unwrap_or(false);
    let known = arg_text(&args, "known_revision")?.filter(|s| !s.is_empty());
    let old = arg_text(&args, "old_revision")?.filter(|s| !s.is_empty());
    let new = arg_text(&args, "new_revision")?.filter(|s| !s.is_empty());
    require(
        u8::from(previous) + u8::from(known.is_some()) + u8::from(old.is_some() || new.is_some())
            == 1,
        "invalid_diff_range",
    )?;
    let (old, new) = if previous {
        let current = ctx.tx.revision(&ref_for(&resource))?;
        if current.parents.is_empty() {
            return output(
                json!({"from":null,"to":{"id":id,"revision":current.id},"diff":"","reason":"no_previous_revision"}),
                vec![],
            );
        }
        require(current.parents.len() == 1, "previous_revision_not_found")?;
        (current.parents[0].clone(), current.id)
    } else if let Some(old) = known {
        (old, resource.revision.ok_or(Error("revision_not_found"))?)
    } else {
        (
            old.ok_or(Error("invalid_diff_range"))?,
            new.ok_or(Error("invalid_diff_range"))?,
        )
    };
    let before = ctx.tx.revision(&ResourceRef {
        id: id.clone(),
        revision: Some(old),
    })?;
    let after = ctx.tx.revision(&ResourceRef {
        id: id.clone(),
        revision: Some(new),
    })?;
    let prior = text(ctx, &before)?;
    let current = text(ctx, &after)?;
    require(
        prior.bytes().filter(|b| *b == b'\n').count()
            + current.bytes().filter(|b| *b == b'\n').count()
            <= 20000,
        "query_cost_exceeded",
    )?;
    let path = short_path(&ctx.tx.resource_path(&id)?);
    let lines = unified(
        &prior,
        &current,
        &format!("{path}@{}", before.id),
        &format!("{path}@{}", after.id),
        ctx.deadline,
    )?;
    let (offset, limit) = window(&args, 500)?;
    let end = offset.saturating_add(limit);
    let mut data = json!({"from":{"id":id,"revision":before.id},"to":{"id":id,"revision":after.id},"diff":lines.iter().skip(offset).take(limit).map(|r|r.text.as_str()).collect::<String>(),"diff_lines":lines.iter().skip(offset).take(limit).collect::<Vec<_>>()});
    if before.summary != after.summary {
        data["summary"] = json!({"from":before.summary,"to":after.summary});
    }
    for (label, revision) in [("from_source", &before), ("to_source", &after)] {
        let mut source = object(value(revision)?)?;
        source.retain(|key, value| {
            matches!(
                key.as_str(),
                "change_note" | "source_kind" | "source_version" | "source_digest"
            ) && !value.is_null()
        });
        if !source.is_empty() {
            data[label] = json!(source);
        }
    }
    if end < lines.len() {
        data["next_offset"] = json!(end);
        data["next"] = json!(format!("/_r/{id}/diff/{}/{}/o/{end}", before.id, after.id));
    }
    output(data, vec![])
}

#[cfg(test)]
mod tests {
    use super::*;
    fn compare(a: &str, b: &str) -> Vec<DiffLine> {
        unified(
            a,
            b,
            "old",
            "new",
            Instant::now() + std::time::Duration::from_secs(2),
        )
        .unwrap()
    }
    #[test]
    fn missing_newlines_keep_record_boundaries_for_paged_diff_rows() {
        let lines = compare("a\r\nold", "a\r\nnew");
        assert_eq!(
            lines.iter().map(|r| r.text.as_str()).collect::<Vec<_>>(),
            [
                "--- old\n",
                "+++ new\n",
                "@@ -1,2 +1,2 @@\n",
                " a\r\n",
                "-old",
                "+new"
            ]
        );
        assert_eq!(lines[4].old_line, Some(2));
        assert_eq!(lines[5].new_line, Some(2));
        assert_eq!(lines[5].old_line, None);
        assert!(compare("equal\n", "equal\n").is_empty());
        assert_eq!(compare("", "new")[2].text, "@@ -0,0 +1 @@\n");
    }
    #[test]
    fn matcher_preserves_earliest_ties_and_popular_line_heuristic() {
        let lines = compare("a\nb\na\n", "b\na\nb\n");
        assert_eq!(
            lines.iter().map(|r| r.text.as_str()).collect::<String>(),
            "--- old\n+++ new\n@@ -1,3 +1,3 @@\n+b\n a\n b\n-a\n"
        );
        let old = format!("first\n{}last\n", "same\n".repeat(200));
        let new = format!("changed\n{}last\n", "same\n".repeat(200));
        let result = compare(&old, &new);
        assert_eq!(result[2].text, "@@ -1,4 +1,4 @@\n");
        assert_eq!(result.len(), 8);
    }
}
