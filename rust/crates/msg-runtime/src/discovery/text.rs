//! Finite text searches; offsets are Unicode character positions on the wire.
use super::*;
use std::{collections::BTreeSet, sync::OnceLock};

#[derive(Debug)]
enum Token {
    Literal(char),
    Any,
    Start,
    End,
    Class {
        negative: bool,
        ranges: Vec<(char, char)>,
    },
}
struct GrepPattern {
    tokens: Vec<Token>,
    sensitive: bool,
}
fn equivalents(character: char) -> Vec<char> {
    static GROUPS: OnceLock<BTreeMap<String, BTreeSet<char>>> = OnceLock::new();
    let groups = GROUPS.get_or_init(|| {
        #[derive(serde::Deserialize)]
        struct Table {
            mapping: BTreeMap<char, String>,
        }
        let table: Table = serde_json::from_str(include_str!("../../../../assets/casefold.json"))
            .expect("release-owned casefold table");
        let mut groups = BTreeMap::<String, BTreeSet<char>>::new();
        for (character, folded) in table.mapping {
            let group = groups.entry(folded.clone()).or_default();
            group.insert(character);
            let mut chars = folded.chars();
            if let (Some(first), None) = (chars.next(), chars.next()) {
                group.insert(first);
            }
        }
        // Python re.IGNORECASE's dotted/dotless I equivalence is wider than
        // Unicode default full case folding. Each match still consumes one char.
        for character in ['i', 'I', '\u{130}', '\u{131}'] {
            groups.entry("i".into()).or_default().insert(character);
        }
        groups
    });
    let key = if matches!(character, 'i' | 'I' | '\u{130}' | '\u{131}') {
        "i".into()
    } else {
        crate::unicode::casefold(&character.to_string())
    };
    groups
        .get(&key)
        .map(|v| v.iter().copied().collect())
        .unwrap_or_else(|| vec![character])
}
impl GrepPattern {
    fn compile(pattern: &str, regular: bool, sensitive: bool) -> Result<Self> {
        require(
            (1..=128).contains(&pattern.chars().count()),
            "invalid_grep_pattern",
        )?;
        if !regular {
            return Ok(Self {
                tokens: pattern.chars().map(Token::Literal).collect(),
                sensitive,
            });
        }
        require(
            !pattern.chars().any(|c| "(){}|\\*+?".contains(c)),
            "invalid_grep_pattern",
        )?;
        let characters: Vec<char> = pattern.chars().collect();
        let mut offset = 0;
        let mut tokens = Vec::new();
        while offset < characters.len() {
            let character = characters[offset];
            offset += 1;
            tokens.push(match character {
                '.' => Token::Any,
                '^' => Token::Start,
                '$' => Token::End,
                '[' => {
                    let negative = characters.get(offset) == Some(&'^');
                    if negative {
                        offset += 1;
                    }
                    let mut members = Vec::new();
                    if characters.get(offset) == Some(&']') {
                        members.push(']');
                        offset += 1;
                    }
                    while offset < characters.len() && characters[offset] != ']' {
                        members.push(characters[offset]);
                        offset += 1;
                    }
                    require(
                        offset < characters.len() && !members.is_empty(),
                        "invalid_grep_pattern",
                    )?;
                    offset += 1;
                    let mut ranges = Vec::new();
                    let mut index = 0;
                    while index < members.len() {
                        if index + 2 < members.len() && members[index + 1] == '-' {
                            require(members[index] <= members[index + 2], "invalid_grep_pattern")?;
                            ranges.push((members[index], members[index + 2]));
                            index += 3;
                        } else {
                            ranges.push((members[index], members[index]));
                            index += 1;
                        }
                    }
                    Token::Class { negative, ranges }
                }
                other => Token::Literal(other),
            });
        }
        Ok(Self { tokens, sensitive })
    }
    fn positions(&self, line: &str, limit: usize) -> Vec<(usize, usize)> {
        let characters: Vec<char> = line.chars().collect();
        let folded: Vec<Vec<char>> = if self.sensitive {
            characters.iter().map(|c| vec![*c]).collect()
        } else {
            characters.iter().copied().map(equivalents).collect()
        };
        let mut result = Vec::new();
        let mut start = 0;
        while start <= characters.len() && result.len() < limit {
            let mut end = start;
            let mut matched = true;
            for token in &self.tokens {
                let ok = match token {
                    Token::Start => end == 0,
                    Token::End => end == characters.len(),
                    Token::Any => end < characters.len() && characters[end] != '\n',
                    Token::Literal(expected) => folded
                        .get(end)
                        .is_some_and(|group| group.contains(expected)),
                    Token::Class { negative, ranges } => folded.get(end).is_some_and(|group| {
                        group.iter().any(|character| {
                            ranges
                                .iter()
                                .any(|(first, last)| first <= character && character <= last)
                        }) != *negative
                    }),
                };
                if !ok {
                    matched = false;
                    break;
                }
                if !matches!(token, Token::Start | Token::End) {
                    end += 1;
                }
            }
            if matched {
                result.push((start, end));
                start = if end > start { end } else { end + 1 };
            } else {
                start += 1;
            }
        }
        result
    }
}

/// Python fnmatchcase's full-path wildcards. Rust's finite regex engine needs
/// no atomic/backtracking groups; invalid descending ranges are empty sets.
fn glob(pattern: &str) -> Result<regex::Regex> {
    let chars: Vec<char> = pattern.chars().collect();
    let mut at = 0;
    let mut translated = String::from("(?s)\\A");
    while at < chars.len() {
        let character = chars[at];
        at += 1;
        match character {
            '*' => translated.push_str(".*"),
            '?' => translated.push('.'),
            '[' => {
                let begin = at;
                let mut end = at;
                if chars.get(end) == Some(&'!') {
                    end += 1;
                }
                if chars.get(end) == Some(&']') {
                    end += 1;
                }
                while end < chars.len() && chars[end] != ']' {
                    end += 1;
                }
                if end == chars.len() {
                    translated.push_str("\\[");
                    continue;
                }
                let mut chunks: Vec<Vec<char>> = Vec::new();
                let mut segment = begin;
                let mut search = begin + if chars.get(begin) == Some(&'!') { 2 } else { 1 };
                while search < end {
                    let Some(next) = (search..end).find(|n| chars[*n] == '-') else {
                        break;
                    };
                    chunks.push(chars[segment..next].to_vec());
                    segment = next + 1;
                    search = next + 3;
                }
                if segment < end {
                    chunks.push(chars[segment..end].to_vec());
                } else if let Some(last) = chunks.last_mut() {
                    last.push('-');
                }
                let mut index = chunks.len();
                while index > 1 {
                    index -= 1;
                    if chunks[index - 1].last() > chunks[index].first() {
                        let next = chunks.remove(index);
                        chunks[index - 1].pop();
                        chunks[index - 1].extend(next.into_iter().skip(1));
                    }
                }
                let mut stuff = String::new();
                for (index, chunk) in chunks.iter().enumerate() {
                    if index > 0 {
                        stuff.push('-');
                    }
                    for character in chunk {
                        if matches!(character, '\\' | '-' | '&' | '~' | '|' | '[' | ']') {
                            stuff.push('\\');
                        }
                        stuff.push(*character);
                    }
                }
                at = end + 1;
                if stuff.is_empty() {
                    translated.push_str("\\b\\B");
                } else if stuff == "!" {
                    translated.push('.');
                } else {
                    if let Some(rest) = stuff.strip_prefix('!') {
                        stuff = format!("^{rest}");
                    } else if stuff.starts_with('^') {
                        stuff.insert(0, '\\');
                    }
                    translated.push('[');
                    translated.push_str(&stuff);
                    translated.push(']');
                }
            }
            other => translated.push_str(&regex::escape(&other.to_string())),
        }
    }
    translated.push_str("\\z");
    regex::RegexBuilder::new(&translated)
        .size_limit(1_048_576)
        .build()
        .map_err(|_| Error("query_cost_exceeded"))
}
pub(super) fn grep(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let args = ctx.args()?.clone();
    let scope = ctx.resolve(ctx.text("scope")?, false)?;
    ctx.check(&scope, Check::List)?;
    let boolean = |key: &str, default: bool| {
        args.get(key)
            .map(Json::as_bool)
            .transpose()
            .map(|v| v.unwrap_or(default))
    };
    let pattern = GrepPattern::compile(
        ctx.text("pattern")?,
        boolean("regex", false)?,
        boolean("case_sensitive", true)?,
    )?;
    let include = arg_text(&args, "glob")?
        .filter(|v| !v.is_empty())
        .map(|v| glob(&v))
        .transpose()?;
    let exclude = arg_text(&args, "exclude_glob")?
        .filter(|v| !v.is_empty())
        .map(|v| glob(&v))
        .transpose()?;
    let max_files = arg_int(&args, "max_files", 50)?;
    let max_matches = arg_int(&args, "max_matches", 50)?;
    let before = arg_int(&args, "before", 0)?;
    let after = arg_int(&args, "after", 0)?;
    require(
        (1..=100).contains(&max_files)
            && (1..=100).contains(&max_matches)
            && (0..=3).contains(&before)
            && (0..=3).contains(&after),
        "query_cost_exceeded",
    )?;
    let count_only = boolean("count_only", false)?;
    let files_only = boolean("files_with_matches", false)?;
    let mut seen = 0;
    let mut bytes_read = 0_i64;
    let mut scanned = 0;
    let mut count = 0;
    let mut truncated = false;
    let mut matches = Vec::new();
    let mut files = Vec::new();
    let mut last = String::new();
    'pages: loop {
        let rows=ctx.tx.rows("WITH RECURSIVE subtree(id) AS (SELECT id FROM resources WHERE id=$1 UNION SELECT r.id FROM resources r JOIN subtree s ON r.parent=s.id) SELECT body,id FROM resources WHERE state='active' AND id IN (SELECT id FROM subtree) AND id>$2 ORDER BY id LIMIT 128",&[scope.as_str().into(),last.as_str().into()])?;
        let page_size = rows.len();
        for row in rows {
            let id = row.text(1)?;
            last = id.into();
            if id != scope && !ctx.tx.ancestors(id)?.iter().any(|r| r.id == scope) {
                continue;
            }
            if !visible(ctx, id)? {
                continue;
            }
            let resource: Resource = decode(&row.json(0)?)?;
            if resource.revision.is_none() {
                continue;
            }
            let path = short_path(&ctx.tx.resource_path(id)?);
            if include.as_ref().is_some_and(|p| !p.is_match(&path))
                || exclude.as_ref().is_some_and(|p| p.is_match(&path))
            {
                continue;
            }
            scanned += 1;
            require(scanned <= 2000, "query_cost_exceeded")?;
            let revision = ctx.tx.revision(&ref_for(&resource))?;
            if !revision.content.media_type.starts_with("text/") {
                continue;
            }
            require(revision.content.size <= 65536, "query_cost_exceeded")?;
            if seen >= max_files {
                truncated = true;
                break 'pages;
            }
            seen += 1;
            bytes_read += revision.content.size;
            require(bytes_read <= 1_048_576, "query_cost_exceeded")?;
            let bytes = ctx.contents.read_bytes(&revision.content, 65536)?;
            let text = std::str::from_utf8(&bytes).map_err(|_| Error("invalid_utf8_content"))?;
            let lines = split_lines(text, false);
            let mut found = false;
            for (line_no, line) in lines.iter().enumerate() {
                for (start, end) in pattern.positions(line, (max_matches - count) as usize) {
                    count += 1;
                    if !found {
                        files.push(json!({"ref":{"id":id,"revision":revision.id},"path":path}));
                        found = true;
                    }
                    if !(count_only || files_only) {
                        let left = line_no.saturating_sub(before as usize);
                        let right = lines.len().min(line_no + 1 + after as usize);
                        let context = lines[left..right]
                            .join("\n")
                            .chars()
                            .take(512)
                            .collect::<String>();
                        matches.push(json!({"ref":{"id":id,"revision":revision.id},"path":path,"line_hint":line_no+1,"range":[start,end],"context":context}));
                    }
                    if count >= max_matches {
                        truncated = true;
                        break 'pages;
                    }
                }
            }
        }
        if page_size < 128 {
            break;
        }
    }
    output(
        if count_only {
            json!({"count":count,"truncated":truncated})
        } else if files_only {
            json!({"files":files,"truncated":truncated})
        } else {
            json!({"matches":matches,"truncated":truncated})
        },
        vec![],
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn bounded_regex_uses_original_unicode_positions_and_python_case_equivalence() {
        assert_eq!(
            GrepPattern::compile("i", false, false)
                .unwrap()
                .positions("自由İıI🦀", 100),
            vec![(2, 3), (3, 4), (4, 5)]
        );
        assert_eq!(
            GrepPattern::compile("[A-Z]", true, false)
                .unwrap()
                .positions("İıſK", 100),
            vec![(0, 1), (1, 2), (2, 3), (3, 4)]
        );
        assert_eq!(
            GrepPattern::compile("ß", false, false)
                .unwrap()
                .positions("ẞ ss ß", 100),
            vec![(0, 1), (5, 6)]
        );
        assert_eq!(
            GrepPattern::compile("^a.$", true, true)
                .unwrap()
                .positions("a🦀", 100),
            vec![(0, 2)]
        );
        for pattern in ["a*", "[a-z]+", "(x)", "\\d", "[z-a]", "[]"] {
            assert!(
                GrepPattern::compile(pattern, true, true).is_err(),
                "{pattern}"
            );
        }
    }
    #[test]
    fn globs_match_whole_paths_and_python_class_edges() {
        assert!(glob("/@*/**.md").unwrap().is_match("/@alice/notes/a.md"));
        assert!(!glob("*.md").unwrap().is_match("a.md/extra"));
        assert!(glob("[!a-c]?").unwrap().is_match("自由"));
        assert!(glob("[[]x[]]").unwrap().is_match("[x]"));
        assert!(!glob("[z-a]").unwrap().is_match("z"));
        assert!(glob("[!z-a]").unwrap().is_match("z"));
        assert!(glob("a[").unwrap().is_match("a["));
    }
}
