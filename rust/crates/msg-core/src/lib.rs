//! MSG canonical-v1 bytes. This is deliberately not RFC 8785 / JCS.
use std::collections::BTreeMap;
use std::fmt;

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use serde::de::{self, MapAccess, Visitor};
use serde::{Deserialize, Deserializer};
use serde_json::value::RawValue;
use sha2::{Digest, Sha256};

pub const MAX_BYTES: usize = 1_048_576;
pub const MAX_DEPTH: usize = 64;

/// Errors contain stable codes only; never input, keys, tokens or parser excerpts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Error(pub &'static str);

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.0)
    }
}
impl std::error::Error for Error {}
pub type Result<T> = std::result::Result<T, Error>;

/// Private representation prevents callers from constructing non-finite values.
#[derive(Clone, PartialEq)]
pub struct Json(Value);

#[derive(Clone, PartialEq)]
enum Value {
    Null,
    Bool(bool),
    Integer(String),
    Float(f64),
    String(String),
    Array(Vec<Json>),
    Object(BTreeMap<String, Json>),
}

impl Json {
    pub fn parse(input: &str) -> Result<Self> {
        if input.len() > MAX_BYTES {
            return Err(Error("request_too_large"));
        }
        let raw: Box<RawValue> = serde_json::from_str(input).map_err(|_| Error("invalid_json"))?;
        Self::from_raw(&raw, 0)
    }

    fn from_raw(raw: &RawValue, depth: usize) -> Result<Self> {
        if depth > MAX_DEPTH {
            return Err(Error("json_too_deep"));
        }
        let text = raw.get();
        let invalid = |_| Error("invalid_json");
        let value = match text.as_bytes().first() {
            Some(b'n') => Value::Null,
            Some(b't') => Value::Bool(true),
            Some(b'f') => Value::Bool(false),
            Some(b'"') => Value::String(serde_json::from_str(text).map_err(invalid)?),
            Some(b'[') => {
                let values: Vec<Box<RawValue>> = serde_json::from_str(text).map_err(invalid)?;
                Value::Array(
                    values
                        .iter()
                        .map(|v| Self::from_raw(v, depth + 1))
                        .collect::<Result<_>>()?,
                )
            }
            Some(b'{') => {
                let members: Members = serde_json::from_str(text).map_err(|e| {
                    if e.to_string().starts_with("duplicate_json_key") {
                        Error("duplicate_json_key")
                    } else {
                        Error("invalid_json")
                    }
                })?;
                Value::Object(
                    members
                        .0
                        .into_iter()
                        .map(|(k, v)| Ok((k, Self::from_raw(&v, depth + 1)?)))
                        .collect::<Result<_>>()?,
                )
            }
            _ if text.contains(['.', 'e', 'E']) => {
                let number: f64 = text.parse().map_err(|_| Error("invalid_json"))?;
                if !number.is_finite() {
                    return Err(Error("non_finite_float"));
                }
                Value::Float(number)
            }
            _ => {
                // CPython's default integer conversion limit. Do not round through f64/u64.
                if text.trim_start_matches('-').len() > 4300 {
                    return Err(Error("invalid_json"));
                }
                Value::Integer(if text == "-0" { "0" } else { text }.to_owned())
            }
        };
        Ok(Self(value))
    }

    pub fn string(value: impl Into<String>) -> Self {
        Self(Value::String(value.into()))
    }
    pub fn empty_array() -> Self {
        Self(Value::Array(Vec::new()))
    }
    pub fn as_object(&self) -> Result<&BTreeMap<String, Json>> {
        match &self.0 {
            Value::Object(v) => Ok(v),
            _ => Err(Error("invalid_type")),
        }
    }
    pub fn into_object(self) -> Result<BTreeMap<String, Json>> {
        match self.0 {
            Value::Object(v) => Ok(v),
            _ => Err(Error("invalid_type")),
        }
    }
    pub fn from_object(value: BTreeMap<String, Json>) -> Self {
        Self(Value::Object(value))
    }
    pub fn as_array(&self) -> Result<&[Json]> {
        match &self.0 {
            Value::Array(v) => Ok(v),
            _ => Err(Error("invalid_type")),
        }
    }
    pub fn as_str(&self) -> Result<&str> {
        match &self.0 {
            Value::String(v) => Ok(v),
            _ => Err(Error("invalid_type")),
        }
    }
    pub fn as_integer(&self) -> Result<&str> {
        match &self.0 {
            Value::Integer(v) => Ok(v),
            _ => Err(Error("invalid_type")),
        }
    }
    pub fn is_null(&self) -> bool {
        matches!(self.0, Value::Null)
    }

    pub fn canonical(&self) -> Result<String> {
        let mut output = String::new();
        self.write(&mut output, 0)?;
        Ok(output)
    }

    fn write(&self, out: &mut String, depth: usize) -> Result<()> {
        if depth > MAX_DEPTH {
            return Err(Error("json_too_deep"));
        }
        match &self.0 {
            Value::Null => out.push_str("null"),
            Value::Bool(v) => out.push_str(if *v { "true" } else { "false" }),
            Value::Integer(v) => out.push_str(v),
            Value::Float(v) => out.push_str(&python_float(*v)),
            Value::String(v) => {
                out.push_str(&serde_json::to_string(v).map_err(|_| Error("invalid_json_value"))?)
            }
            Value::Array(values) => {
                out.push('[');
                for (i, value) in values.iter().enumerate() {
                    if i != 0 {
                        out.push(',');
                    }
                    value.write(out, depth + 1)?;
                }
                out.push(']');
            }
            Value::Object(values) => {
                out.push('{');
                for (i, (key, value)) in values.iter().enumerate() {
                    if i != 0 {
                        out.push(',');
                    }
                    out.push_str(
                        &serde_json::to_string(key).map_err(|_| Error("invalid_json_value"))?,
                    );
                    out.push(':');
                    value.write(out, depth + 1)?;
                }
                out.push('}');
            }
        }
        if out.len() > MAX_BYTES {
            return Err(Error("request_too_large"));
        }
        Ok(())
    }
}

struct Members(BTreeMap<String, Box<RawValue>>);
impl<'de> Deserialize<'de> for Members {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        struct Unique;
        impl<'de> Visitor<'de> for Unique {
            type Value = Members;
            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("an object without duplicate keys")
            }
            fn visit_map<M: MapAccess<'de>>(
                self,
                mut map: M,
            ) -> std::result::Result<Members, M::Error> {
                let mut values = BTreeMap::new();
                while let Some((key, value)) = map.next_entry::<String, Box<RawValue>>()? {
                    if values.insert(key, value).is_some() {
                        return Err(de::Error::custom("duplicate_json_key"));
                    }
                }
                Ok(Members(values))
            }
        }
        deserializer.deserialize_map(Unique)
    }
}

/// Ryu supplies shortest round-trip digits; Python supplies the notation rules.
/// Keep float/integer distinction, -0.0, exponent sign and two-digit exponent.
fn python_float(value: f64) -> String {
    if value == 0.0 {
        return if value.is_sign_negative() {
            "-0.0"
        } else {
            "0.0"
        }
        .to_owned();
    }
    let mut buffer = ryu::Buffer::new();
    let text = buffer.format_finite(value.abs());
    let (mantissa, exponent) = text.split_once('e').unwrap_or((text, "0"));
    let base: i32 = exponent.parse().unwrap_or(0);
    let point = mantissa.find('.').unwrap_or(mantissa.len()) as i32;
    let raw_digits = mantissa.replace('.', "");
    let leading = raw_digits.len() - raw_digits.trim_start_matches('0').len();
    let digits = raw_digits.trim_start_matches('0').trim_end_matches('0');
    let scientific = base + point - leading as i32 - 1;
    let mut output = if value.is_sign_negative() { "-" } else { "" }.to_owned();
    if !(-4..16).contains(&scientific) {
        output.push_str(&digits[..1]);
        if digits.len() > 1 {
            output.push('.');
            output.push_str(&digits[1..]);
        }
        output.push('e');
        output.push(if scientific < 0 { '-' } else { '+' });
        output.push_str(&format!("{:02}", scientific.unsigned_abs()));
    } else {
        let position = scientific + 1;
        if position <= 0 {
            output.push_str("0.");
            output.push_str(&"0".repeat((-position) as usize));
            output.push_str(digits);
        } else if position as usize >= digits.len() {
            output.push_str(digits);
            output.push_str(&"0".repeat(position as usize - digits.len()));
            output.push_str(".0");
        } else {
            let position = position as usize;
            output.push_str(&digits[..position]);
            output.push('.');
            output.push_str(&digits[position..]);
        }
    }
    output
}

pub fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}
pub fn b64(bytes: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(bytes)
}
pub fn unb64(value: &str, limit: usize) -> Result<Vec<u8>> {
    if value.len() > limit.saturating_mul(4) / 3 + 4 {
        return Err(Error("invalid_base64"));
    }
    let bytes = URL_SAFE_NO_PAD
        .decode(value)
        .map_err(|_| Error("invalid_base64"))?;
    if bytes.len() > limit || b64(&bytes) != value {
        return Err(Error("invalid_base64"));
    }
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_preserves_python_numbers() {
        for (raw, expected) in [
            ("1E20", "1e+20"),
            ("1e-7", "1e-07"),
            ("1e15", "1000000000000000.0"),
            ("1e-4", "0.0001"),
            ("-0.0", "-0.0"),
            ("-0", "0"),
            ("18446744073709551616000", "18446744073709551616000"),
            ("5e-324", "5e-324"),
            ("1.7976931348623157e308", "1.7976931348623157e+308"),
        ] {
            assert_eq!(Json::parse(raw).unwrap().canonical().unwrap(), expected);
        }
    }

    #[test]
    fn rejects_ambiguous_or_unbounded_inputs() {
        for raw in [
            r#"{"a":1,"\u0061":2}"#,
            r#"[{"x":0,"x":1}]"#,
            "NaN",
            "Infinity",
            "1e999",
            r#""\ud800""#,
            "01",
            "[1,]",
        ] {
            assert!(
                Json::parse(raw).and_then(|v| v.canonical()).is_err(),
                "{raw}"
            );
        }
        assert!(Json::parse(&" ".repeat(MAX_BYTES + 1)).is_err());
        assert!(Json::parse(&format!("{}0{}", "[".repeat(66), "]".repeat(66))).is_err());
    }

    #[test]
    fn unicode_and_base64_are_canonical() {
        assert_eq!(
            Json::parse(r#"{"😀":1,"\ue000":2,"a":"中文\n"}"#)
                .unwrap()
                .canonical()
                .unwrap(),
            "{\"a\":\"中文\\n\",\"\u{e000}\":2,\"😀\":1}"
        );
        for bad in ["YQ=", "YQ==", "YR", "+/", "Y Q", "a"] {
            assert!(unb64(bad, 10).is_err());
        }
        assert_eq!(unb64("YQ", 1).unwrap(), b"a");
        assert!(unb64("YQ", 0).is_err());
    }
}
