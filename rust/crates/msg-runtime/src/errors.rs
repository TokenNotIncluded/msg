//! Fixed public contract text. Never expose exception messages or credentials.
use std::{collections::BTreeMap, sync::OnceLock};
pub fn public_message(code: &str) -> &'static str {
    static MESSAGES: OnceLock<BTreeMap<String, String>> = OnceLock::new();
    MESSAGES
        .get_or_init(|| {
            serde_json::from_str(include_str!("../../../assets/errors.json"))
                .expect("release-owned error messages")
        })
        .get(code)
        .map(String::as_str)
        .unwrap_or("The operation could not be completed.")
}
