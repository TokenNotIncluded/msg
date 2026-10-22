//! Finite text patch data: exact context, one-file unified hunks, or a
//! digest-bound Markdown heading/block. Never a command or fuzzy replacement.
use super::{py_space, require};
use msg_core::{digest, Error, Json, Result};
use regex::Regex;
use std::{collections::BTreeMap, ops::Range};

pub(super) const LIMIT: usize = 1_048_576;
const CANDIDATES: usize = 4096;
const LINES: usize = 65536;

fn text<'a>(values: &'a BTreeMap<String, Json>, key: &str) -> Result<&'a str> {
    values
        .get(key)
        .ok_or(Error("invalid_text_patch"))?
        .as_str()
        .map_err(|_| Error("invalid_text_patch"))
}
fn optional<'a>(values: &'a BTreeMap<String, Json>, key: &str) -> Result<&'a str> {
    values
        .get(key)
        .map(Json::as_str)
        .transpose()
        .map(|v| v.unwrap_or(""))
}
pub(super) fn validate(patch: &Json) -> Result<()> {
    let values = patch.as_object().map_err(|_| Error("invalid_text_patch"))?;
    let kind = text(values, "kind")?;
    let (allowed, required): (&[&str], &[&str]) = match kind {
        "exact" => (
            &["kind", "exact", "replacement", "before", "after"],
            &["exact", "replacement"],
        ),
        "unified" => (&["kind", "diff"], &["diff"]),
        "heading" => (
            &["kind", "heading", "digest", "replacement"],
            &["heading", "digest", "replacement"],
        ),
        "block" => (
            &["kind", "digest", "replacement"],
            &["digest", "replacement"],
        ),
        _ => return Err(Error("invalid_text_patch")),
    };
    require(
        values.keys().all(|k| allowed.contains(&k.as_str()))
            && required.iter().all(|k| values.contains_key(*k)),
        "invalid_text_patch",
    )?;
    let mut bytes = 0_usize;
    for (key, value) in values {
        let value = value.as_str().map_err(|_| Error("invalid_text_patch"))?;
        bytes = bytes
            .checked_add(value.len())
            .ok_or(Error("patch_too_large"))?;
        let count = value.chars().count();
        require(
            count
                <= if matches!(key.as_str(), "before" | "after" | "heading") {
                    256
                } else {
                    LIMIT
                },
            "invalid_text_patch",
        )?;
        if matches!(key.as_str(), "exact" | "heading" | "diff") {
            require(!value.is_empty(), "invalid_text_patch")?;
        }
        if key == "digest" {
            require(
                value.strip_prefix("sha256:").is_some_and(|v| {
                    v.len() == 64
                        && v.bytes()
                            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
                }),
                "invalid_text_patch",
            )?;
        }
    }
    require(bytes <= LIMIT, "patch_too_large")
}
pub(super) fn exact(
    source: &str,
    needle: &str,
    replacement: &str,
    before: &str,
    after: &str,
) -> Result<String> {
    require(!needle.is_empty(), "patch_exact_required")?;
    let mut matched = None;
    let mut start = 0;
    let mut candidates = 0;
    while let Some(index) = source[start..].find(needle).map(|i| start + i) {
        candidates += 1;
        require(candidates <= CANDIDATES, "patch_too_complex")?;
        let end = index + needle.len();
        if index >= before.len()
            && source.get(index - before.len()..index) == Some(before)
            && source[end..].starts_with(after)
        {
            require(matched.is_none(), "patch_ambiguous")?;
            matched = Some(index);
        }
        start = index
            + source[index..]
                .chars()
                .next()
                .ok_or(Error("patch_no_match"))?
                .len_utf8();
    }
    let start = matched.ok_or(Error("patch_no_match"))?;
    substitute(source, start..start + needle.len(), replacement)
}
fn substitute(source: &str, span: Range<usize>, replacement: &str) -> Result<String> {
    require(
        source.len() - (span.end - span.start) + replacement.len() <= LIMIT,
        "patch_too_large",
    )?;
    Ok(format!(
        "{}{}{}",
        &source[..span.start],
        replacement,
        &source[span.end..]
    ))
}
struct Hunk {
    old_start: usize,
    new_start: usize,
    old: Vec<String>,
    new: Vec<String>,
}
fn hunks(text: &str) -> Result<Vec<Hunk>> {
    let lines: Vec<&str> = text.split_inclusive('\n').collect();
    require(lines.len() <= LINES, "patch_too_complex")?;
    let header = Regex::new(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\r\n]*\r?\n?$")
        .map_err(|_| Error("invalid_pattern"))?;
    let mut i = 0;
    let mut result = Vec::new();
    if lines.first().is_some_and(|v| v.starts_with("--- ")) {
        require(
            lines.get(1).is_some_and(|v| v.starts_with("+++ ")),
            "invalid_unified_patch",
        )?;
        i = 2;
    }
    while i < lines.len() {
        let matches = header
            .captures(lines[i])
            .ok_or(Error("invalid_unified_patch"))?;
        require(result.len() < 256, "invalid_unified_patch")?;
        let number = |n: usize, default: &str| {
            matches
                .get(n)
                .map(|v| v.as_str())
                .unwrap_or(default)
                .parse::<usize>()
                .map_err(|_| Error("invalid_unified_patch"))
        };
        let (old_no, old_count, new_no, new_count) = (
            number(1, "0")?,
            number(2, "1")?,
            number(3, "0")?,
            number(4, "1")?,
        );
        require(
            (old_count == 0 || old_no > 0) && (new_count == 0 || new_no > 0),
            "invalid_unified_patch",
        )?;
        i += 1;
        let mut old: Vec<String> = Vec::new();
        let mut new: Vec<String> = Vec::new();
        let mut previous = None;
        while i < lines.len() && !lines[i].starts_with("@@ ") {
            let line = lines[i];
            if line.trim_end_matches(['\r', '\n']) == "\\ No newline at end of file" {
                let (a, b) = previous.take().ok_or(Error("invalid_unified_patch"))?;
                for side in [
                    if a { Some(&mut old) } else { None },
                    if b { Some(&mut new) } else { None },
                ]
                .into_iter()
                .flatten()
                {
                    let last = side.last_mut().ok_or(Error("invalid_unified_patch"))?;
                    require(last.ends_with('\n'), "invalid_unified_patch")?;
                    last.pop();
                }
            } else {
                let kind = line
                    .as_bytes()
                    .first()
                    .copied()
                    .ok_or(Error("invalid_unified_patch"))?;
                require(
                    b" -+".contains(&kind)
                        && line.ends_with('\n')
                        && (old.len() < old_count || new.len() < new_count),
                    "invalid_unified_patch",
                )?;
                let (a, b) = (kind != b'+', kind != b'-');
                if a {
                    old.push(line[1..].into());
                }
                if b {
                    new.push(line[1..].into());
                }
                previous = Some((a, b));
            }
            i += 1;
        }
        require(
            old.len() == old_count && new.len() == new_count,
            "invalid_unified_patch",
        )?;
        result.push(Hunk {
            old_start: if old_count > 0 { old_no - 1 } else { old_no },
            new_start: if new_count > 0 { new_no - 1 } else { new_no },
            old,
            new,
        });
    }
    require(!result.is_empty(), "invalid_unified_patch")?;
    Ok(result)
}
fn sequence_matches(lines: &[&str], needle: &[String]) -> Vec<usize> {
    let mut prefix = vec![0; needle.len()];
    let mut j = 0;
    for i in 1..needle.len() {
        while j > 0 && needle[i] != needle[j] {
            j = prefix[j - 1];
        }
        if needle[i] == needle[j] {
            j += 1;
        }
        prefix[i] = j;
    }
    let mut result = Vec::new();
    j = 0;
    for (i, line) in lines.iter().enumerate() {
        while j > 0 && *line != needle[j] {
            j = prefix[j - 1];
        }
        if *line == needle[j] {
            j += 1;
        }
        if j == needle.len() {
            result.push(i + 1 - j);
            if result.len() == 2 {
                return result;
            }
            j = prefix[j - 1];
        }
    }
    result
}
fn unified(source: &str, hunks: &[Hunk], rebase: bool) -> Result<String> {
    let lines: Vec<&str> = source.split_inclusive('\n').collect();
    require(lines.len() <= LINES, "patch_too_complex")?;
    require(
        !rebase || lines.len() * hunks.len() <= 1_000_000,
        "patch_too_complex",
    )?;
    let mut result = String::new();
    let mut last = 0;
    let mut delta = 0_isize;
    for hunk in hunks {
        let start = if rebase {
            require(!hunk.old.is_empty(), "patch_rebase_unsupported")?;
            let starts = sequence_matches(&lines, &hunk.old);
            require(!starts.is_empty(), "patch_no_match")?;
            require(starts.len() == 1, "patch_ambiguous")?;
            starts[0]
        } else {
            require(
                hunk.old_start.checked_add_signed(delta) == Some(hunk.new_start),
                "invalid_unified_patch",
            )?;
            hunk.old_start
        };
        require(
            last <= start && start <= lines.len(),
            "invalid_unified_patch",
        )?;
        let end = start
            .checked_add(hunk.old.len())
            .ok_or(Error("invalid_unified_patch"))?;
        require(
            lines
                .get(start..end)
                .is_some_and(|v| v.iter().copied().eq(hunk.old.iter().map(String::as_str))),
            "patch_no_match",
        )?;
        require(
            hunk.new.last().is_none_or(|v| v.ends_with('\n')) || end == lines.len(),
            "invalid_unified_patch",
        )?;
        for line in &lines[last..start] {
            result.push_str(line);
        }
        for line in &hunk.new {
            result.push_str(line);
        }
        last = end;
        delta += hunk.new.len() as isize - hunk.old.len() as isize;
        require(result.len() <= LIMIT, "patch_too_large")?;
    }
    for line in &lines[last..] {
        result.push_str(line);
    }
    require(result.len() <= LIMIT, "patch_too_large")?;
    Ok(result)
}
struct Heading {
    title: String,
    level: usize,
    start: usize,
}
struct Markdown {
    headings: Vec<Heading>,
    blocks: Vec<Range<usize>>,
}
fn markdown(source: &str) -> Result<Markdown> {
    let split =
        Regex::new(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$").map_err(|_| Error("invalid_pattern"))?;
    let lines: Vec<&str> = split.find_iter(source).map(|m| m.as_str()).collect();
    require(lines.len() <= LINES, "patch_too_complex")?;
    let atx = Regex::new(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?)|[ \t]*)$")
        .map_err(|_| Error("invalid_pattern"))?;
    let fence = Regex::new(r"^ {0,3}(`{3,}|~{3,})(.*)$").map_err(|_| Error("invalid_pattern"))?;
    let setext = Regex::new(r"^ {0,3}(=+|-+)[ \t]*$").map_err(|_| Error("invalid_pattern"))?;
    let closing = Regex::new(r"[ \t]+#+[ \t]*$").map_err(|_| Error("invalid_pattern"))?;
    let mut offsets = vec![0];
    for line in &lines {
        offsets.push(offsets.last().copied().unwrap_or(0) + line.len());
    }
    let mut result = Markdown {
        headings: vec![],
        blocks: vec![],
    };
    let mut i = 0;
    while i < lines.len() {
        let text = lines[i].trim_end_matches(['\r', '\n']);
        if text.trim_matches(py_space).is_empty() {
            i += 1;
            continue;
        }
        if let Some(start_fence) = fence
            .captures(text)
            .filter(|c| !c[1].starts_with('`') || !c[2].contains('`'))
        {
            let start = i;
            let marker = &start_fence[1];
            i += 1;
            while i < lines.len() {
                let end = fence.captures(lines[i].trim_end_matches(['\r', '\n']));
                i += 1;
                if end.as_ref().is_some_and(|end| {
                    end[1].as_bytes()[0] == marker.as_bytes()[0]
                        && end[1].len() >= marker.len()
                        && end[2].trim_matches(py_space).is_empty()
                }) {
                    break;
                }
            }
            result.blocks.push(offsets[start]..offsets[i]);
            continue;
        }
        let heading = atx.captures(text);
        let underline = lines
            .get(i + 1)
            .and_then(|s| setext.captures(s.trim_end_matches(['\r', '\n'])));
        if heading.is_some()
            || (underline.is_some() && !text.starts_with("    ") && !text.starts_with('\t'))
        {
            let (level, title) = if let Some(c) = heading.as_ref() {
                (
                    c[1].len(),
                    closing
                        .replace(
                            c.get(2)
                                .map(|v| v.as_str())
                                .unwrap_or("")
                                .trim_matches(py_space),
                            "",
                        )
                        .trim_end_matches(py_space)
                        .to_owned(),
                )
            } else {
                (
                    if underline.as_ref().is_some_and(|c| c[1].starts_with('=')) {
                        1
                    } else {
                        2
                    },
                    text.trim_matches(py_space).into(),
                )
            };
            let start = i;
            i += if heading.is_some() { 1 } else { 2 };
            result.headings.push(Heading {
                title,
                level,
                start: offsets[start],
            });
            result.blocks.push(offsets[start]..offsets[i]);
            continue;
        }
        let start = i;
        i += 1;
        while i < lines.len() && !lines[i].trim_matches(py_space).is_empty() {
            let upcoming = lines[i].trim_end_matches(['\r', '\n']);
            let underline = lines
                .get(i + 1)
                .is_some_and(|s| setext.is_match(s.trim_end_matches(['\r', '\n'])));
            if atx.is_match(upcoming) || fence.is_match(upcoming) || underline {
                break;
            }
            i += 1;
        }
        result.blocks.push(offsets[start]..offsets[i]);
    }
    require(result.blocks.len() <= CANDIDATES, "patch_too_complex")?;
    Ok(result)
}
fn structured(source: &str, values: &BTreeMap<String, Json>) -> Result<String> {
    let markdown = markdown(source)?;
    let span = if text(values, "kind")? == "heading" {
        let wanted = text(values, "heading")?;
        let matches: Vec<_> = markdown
            .headings
            .iter()
            .enumerate()
            .filter(|(_, h)| h.title == wanted)
            .collect();
        require(!matches.is_empty(), "patch_no_match")?;
        require(matches.len() == 1, "patch_ambiguous")?;
        let (index, heading) = matches[0];
        let end = markdown.headings[index + 1..]
            .iter()
            .find(|h| h.level <= heading.level)
            .map(|h| h.start)
            .unwrap_or(source.len());
        heading.start..end
    } else {
        let wanted = text(values, "digest")?;
        let matches: Vec<_> = markdown
            .blocks
            .into_iter()
            .filter(|span| digest(source[span.clone()].as_bytes()) == wanted)
            .collect();
        require(!matches.is_empty(), "patch_no_match")?;
        require(matches.len() == 1, "patch_ambiguous")?;
        matches[0].clone()
    };
    require(
        digest(source[span.clone()].as_bytes()) == text(values, "digest")?,
        "patch_no_match",
    )?;
    substitute(source, span, text(values, "replacement")?)
}
pub(super) fn apply(source: &str, patch: &Json, base: Option<&str>) -> Result<String> {
    validate(patch)?;
    require(source.len() <= LIMIT, "patch_too_large")?;
    if let Some(base) = base {
        require(base.len() <= LIMIT, "patch_too_large")?;
        apply(base, patch, None)?;
    }
    let values = patch.as_object()?;
    match text(values, "kind")? {
        "exact" => exact(
            source,
            text(values, "exact")?,
            text(values, "replacement")?,
            optional(values, "before")?,
            optional(values, "after")?,
        ),
        "unified" => {
            let hunks = hunks(text(values, "diff")?)?;
            if let Some(base) = base {
                unified(base, &hunks, true)?;
            }
            unified(source, &hunks, base.is_some())
        }
        _ => structured(source, values),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn patch(value: serde_json::Value) -> Json {
        crate::content::encode(&value).unwrap()
    }
    #[test]
    fn exact_replacements_require_one_contextual_anchor() {
        assert_eq!(exact("x界x界z", "界", "🦀", "x", "z").unwrap(), "x界x🦀z");
        assert_eq!(
            exact("aaaa", "aa", "b", "", "").unwrap_err(),
            Error("patch_ambiguous")
        );
        assert_eq!(
            exact("a界b", "界", "x", "bad", "").unwrap_err(),
            Error("patch_no_match")
        );
    }
    #[test]
    fn unified_newlines_are_byte_preserving_and_eof_cannot_join_untouched_text() {
        let p = patch(
            serde_json::json!({"kind":"unified","diff":"--- a\n+++ b\n@@ -1,2 +1,2 @@\n a\r\n-b\n+c\n\\ No newline at end of file\n"}),
        );
        assert_eq!(apply("a\r\nb\n", &p, None).unwrap(), "a\r\nc");
        assert!(apply("a\r\nb\nz\n", &p, None).is_err());
        let p = patch(serde_json::json!({"kind":"unified","diff":"@@ -1 +1 @@\n-a\n+b"}));
        assert_eq!(
            apply("a\n", &p, None).unwrap_err(),
            Error("invalid_unified_patch")
        );
    }
    #[test]
    fn rebase_validates_base_and_unique_context_in_both_revisions() {
        let p = patch(serde_json::json!({"kind":"unified","diff":"@@ -1 +1 @@\n-a\n+b\n"}));
        assert_eq!(apply("new\na\n", &p, Some("a\n")).unwrap(), "new\nb\n");
        assert!(apply("new\na\n", &p, Some("forged\n")).is_err());
        assert_eq!(
            apply("a\na\n", &p, Some("a\n")).unwrap_err(),
            Error("patch_ambiguous")
        );
        let insert = patch(serde_json::json!({"kind":"unified","diff":"@@ -0,0 +1 @@\n+a\n"}));
        assert_eq!(
            apply("x\n", &insert, Some("")).unwrap_err(),
            Error("patch_rebase_unsupported")
        );
    }
    #[test]
    fn headings_ignore_fences_and_bind_the_whole_section_digest() {
        let source = "# One\r\ntext\r\n## Child\r\nmore\r\n# Two\r\n```\r\n# One\r\n```\r\n";
        let section = source.split("# Two").next().unwrap();
        let p = patch(
            serde_json::json!({"kind":"heading","heading":"One","digest":digest(section.as_bytes()),"replacement":"# New\r\n"}),
        );
        assert_eq!(
            apply(source, &p, None).unwrap(),
            "# New\r\n# Two\r\n```\r\n# One\r\n```\r\n"
        );
        let p = patch(
            serde_json::json!({"kind":"block","digest":digest(b"same\n"),"replacement":"x\n"}),
        );
        assert_eq!(
            apply("same\n\nsame\n", &p, None).unwrap_err(),
            Error("patch_ambiguous")
        );
    }
}
