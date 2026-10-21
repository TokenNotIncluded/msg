//! Python 3.15's Unicode 17 full case folding, generated from the pinned oracle.
use serde::Deserialize;
use std::{collections::BTreeMap, sync::OnceLock};

#[derive(Deserialize)]
struct Table {
    version: String,
    mapping: BTreeMap<char, String>,
}
fn table() -> &'static Table {
    static TABLE: OnceLock<Table> = OnceLock::new();
    TABLE.get_or_init(|| {
        serde_json::from_str(include_str!("../../../assets/casefold.json"))
            .expect("release-owned casefold table")
    })
}
pub fn casefold(value: &str) -> String {
    let mut result = String::with_capacity(value.len());
    for ch in value.chars() {
        if let Some(replacement) = table().mapping.get(&ch) {
            result.push_str(replacement);
        } else {
            result.push(ch);
        }
    }
    result
}
pub fn version() -> &'static str {
    &table().version
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn full_folding_preserves_python_unicode_semantics() {
        assert_eq!(version(), "17.0.0");
        assert_eq!(unicode_normalization::UNICODE_VERSION, (17, 0, 0));
        assert_eq!(casefold("Straße Σς ﬃ İ"), "strasse σσ ffi i\u{307}");
        assert_eq!(casefold("\u{1c89}"), "\u{1c8a}");
    }
}
