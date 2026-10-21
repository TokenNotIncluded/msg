//! Strict, bounded ingress shared by every transport. Rejection never reports
//! caller-controlled URLs, proof contents, HTTP headers or parser diagnostics.
use axum::http::HeaderMap;
use flate2::bufread::GzDecoder;
use msg_core::{unb64, Error, Json, Result};
use msg_protocol::Request;
use msg_runtime::config::Settings;
use std::{io::Read, sync::OnceLock};
use url::Url;

fn require(ok: bool, code: &'static str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(Error(code))
    }
}
pub fn header<'a>(headers: &'a HeaderMap, name: &str) -> Result<Option<&'a str>> {
    let mut values = headers.get_all(name).iter();
    let first = values.next();
    require(values.next().is_none(), "invalid_request")?;
    first
        .map(|value| value.to_str().map_err(|_| Error("invalid_request")))
        .transpose()
}
fn hex(value: u8) -> Option<u8> {
    match value {
        b'0'..=b'9' => Some(value - b'0'),
        b'a'..=b'f' => Some(value - b'a' + 10),
        b'A'..=b'F' => Some(value - b'A' + 10),
        _ => None,
    }
}
pub fn percent_decode(raw: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(raw.len());
    let mut i = 0;
    while i < raw.len() {
        if raw[i] == b'%' && i + 2 < raw.len() {
            if let (Some(a), Some(b)) = (hex(raw[i + 1]), hex(raw[i + 2])) {
                out.push(a * 16 + b);
                i += 3;
                continue;
            }
        }
        out.push(raw[i]);
        i += 1;
    }
    out
}
fn layers(raw: &[u8], mut inspect: impl FnMut(&[u8]) -> Result<()>) -> Result<()> {
    let mut value = raw.to_vec();
    for _ in 0..=8 {
        inspect(&value)?;
        let decoded = percent_decode(&value);
        if decoded == value {
            return Ok(());
        }
        value = decoded;
    }
    Err(Error("invalid_path"))
}
fn escapes(raw: &[u8]) -> bool {
    raw.iter().enumerate().all(|(i, b)| {
        *b != b'%' || i + 2 < raw.len() && hex(raw[i + 1]).is_some() && hex(raw[i + 2]).is_some()
    })
}
fn secret_name(name: &[u8]) -> Result<bool> {
    const NAMES: &[&[u8]] = &[
        b"token",
        b"accesstoken",
        b"refreshtoken",
        b"recoverysecret",
        b"newrecoverysecret",
        b"privatekey",
        b"secret",
        b"clientsecret",
        b"bootstrapclaim",
        b"password",
        b"apikey",
        b"authorization",
    ];
    let mut secret = false;
    layers(name, |layer| {
        for part in layer.split(|b| b".[]".contains(b)) {
            let normalized: Vec<u8> = part
                .trim_ascii()
                .iter()
                .filter(|b| !b"_-".contains(b))
                .map(u8::to_ascii_lowercase)
                .collect();
            secret |= NAMES.contains(&normalized.as_slice());
        }
        Ok(())
    })?;
    Ok(secret)
}
pub fn contains_secret_fields(value: &Json) -> Result<bool> {
    let mut pending = vec![value];
    let mut count = 0;
    while let Some(value) = pending.pop() {
        count += 1;
        require(count <= 16384, "request_too_large")?;
        if let Ok(fields) = value.as_object() {
            for name in fields.keys() {
                if secret_name(name.as_bytes())? {
                    return Ok(true);
                }
            }
            pending.extend(fields.values());
        } else if let Ok(items) = value.as_array() {
            pending.extend(items);
        }
    }
    Ok(false)
}
pub fn safe_target(path: &[u8], query: &[u8], maximum: usize) -> Result<()> {
    require(
        path.len()
            .saturating_add(query.len())
            .saturating_add(usize::from(!query.is_empty()))
            <= maximum,
        "path_too_large",
    )?;
    require(
        !path.contains(&b'#') && !query.contains(&b'#'),
        "secure_channel_required",
    )?;
    require(
        path.starts_with(b"/") && !path.starts_with(b"//"),
        "invalid_path",
    )?;
    require(
        !path.iter().chain(query).any(|b| *b <= b' ' || *b == 0x7f)
            && escapes(path)
            && escapes(query),
        "invalid_path",
    )?;
    layers(query, |layer| {
        for pair in layer.split(|b| b"&;".contains(b)) {
            let key = pair.split(|b| *b == b'=').next().unwrap_or_default();
            require(!secret_name(key)?, "secure_channel_required")?;
        }
        Ok(())
    })?;
    let original: Vec<&[u8]> = path.split(|b| *b == b'/').collect();
    let mut ambiguous = false;
    layers(path, |layer| {
        require(
            !layer.contains(&b'\\') && !layer.contains(&0),
            "invalid_path",
        )?;
        let segments: Vec<&[u8]> = layer.split(|b| *b == b'/').collect();
        if segments.len() >= 5 && segments[..3] == [b"".as_slice(), b"-", b"g"] {
            require(
                !segments[4].eq_ignore_ascii_case(b"token")
                    && !segments[4].eq_ignore_ascii_case(b"bootstrap"),
                "secure_channel_required",
            )?;
        }
        for segment in &segments {
            if segment.contains(&b'=') {
                require(
                    !secret_name(segment.split(|b| *b == b'=').next().unwrap_or_default())?,
                    "secure_channel_required",
                )?;
            }
        }
        if layer == b"/-" || layer.starts_with(b"/-/") {
            ambiguous |= !(path == b"/-" || path.starts_with(b"/-/"))
                || segments.iter().any(|p| *p == b"." || *p == b"..")
                || original.iter().skip(2).take(2).any(|p| p.contains(&b'%'))
                || (original.len() > 4
                    && matches!(original[2], b"g" | b"p")
                    && original[3].contains(&b'.')
                    && original[4].contains(&b'%'));
        }
        Ok(())
    })?;
    require(!ambiguous, "not_found")
}
pub fn matching_origin(headers: &HeaderMap, settings: &Settings) -> Result<String> {
    let values = headers.get_all("host").iter().collect::<Vec<_>>();
    require(values.len() == 1, "forbidden_host")?;
    let host = values[0].to_str().map_err(|_| Error("forbidden_host"))?;
    static HOST: OnceLock<regex::Regex> = OnceLock::new();
    require(
        HOST.get_or_init(|| {
            regex::Regex::new(r"^(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?$")
                .expect("fixed Host grammar")
        })
        .is_match(host),
        "forbidden_host",
    )?;
    for candidate in std::iter::once(&settings.service_url).chain(settings.service_aliases.iter()) {
        let expected = Url::parse(candidate).map_err(|_| Error("invalid_service_url"))?;
        let supplied = Url::parse(&format!("{}://{host}", expected.scheme()))
            .map_err(|_| Error("forbidden_host"))?;
        if supplied.port_or_known_default().is_some_and(|p| p > 0)
            && supplied.host_str() == expected.host_str()
            && supplied.port_or_known_default() == expected.port_or_known_default()
        {
            return Ok(candidate.trim_end_matches('/').to_owned());
        }
    }
    Err(Error("forbidden_host"))
}
pub fn policy(headers: &HeaderMap, settings: &Settings, path: &str, query: &str) -> Result<()> {
    safe_target(path.as_bytes(), query.as_bytes(), settings.max_path_bytes)?;
    let origin = matching_origin(headers, settings)?;
    require(
        !headers.contains_key("x-http-method-override")
            && !headers.contains_key("x-method-override"),
        "method_not_allowed",
    )?;
    let origins = headers.get_all("origin").iter().collect::<Vec<_>>();
    if !origins.is_empty() {
        require(
            origins.len() == 1
                && origins[0]
                    .to_str()
                    .is_ok_and(|s| s.trim_end_matches('/') == origin),
            "forbidden_origin",
        )?;
    }
    Ok(())
}
pub fn passive(headers: &HeaderMap) -> bool {
    let read = |name| {
        headers
            .get(name)
            .and_then(|h| h.to_str().ok())
            .unwrap_or("")
            .to_ascii_lowercase()
    };
    let agent = read("user-agent");
    [
        "bot",
        "crawler",
        "spider",
        "preview",
        "scanner",
        "safebrowsing",
        "facebookexternalhit",
        "slackbot",
        "discordbot",
        "whatsapp",
        "telegrambot",
        "mozilla/",
        "chrome/",
        "chromium/",
        "firefox/",
        "safari/",
        "edg/",
    ]
    .iter()
    .any(|s| agent.contains(s))
        || ["purpose", "sec-purpose", "x-moz"].iter().any(|name| {
            let value = read(name);
            value.contains("prefetch") || value.contains("prerender")
        })
        || read("sec-fetch-mode") == "navigate"
}
pub fn framing(headers: &HeaderMap, limit: usize) -> Result<(Option<usize>, bool)> {
    let length = header(headers, "content-length")?;
    let encoding = header(headers, "content-encoding")?
        .unwrap_or("identity")
        .trim_matches([' ', '\t'])
        .to_ascii_lowercase();
    require(
        length.is_none() || !headers.contains_key("transfer-encoding"),
        "invalid_request",
    )?;
    require(
        matches!(encoding.as_str(), "identity" | "gzip"),
        "unknown_encoding",
    )?;
    let expected = length
        .map(|s| {
            let s = s.trim_matches([' ', '\t']);
            require(
                !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()),
                "invalid_request",
            )?;
            let digits = s.trim_start_matches('0');
            let maximum = limit.to_string();
            require(
                digits.len() < maximum.len()
                    || digits.len() == maximum.len() && digits <= maximum.as_str(),
                "request_too_large",
            )?;
            if digits.is_empty() {
                Ok(0)
            } else {
                digits.parse().map_err(|_| Error("request_too_large"))
            }
        })
        .transpose()?;
    Ok((expected, encoding == "gzip"))
}
pub fn gunzip(raw: &[u8], limit: usize) -> Result<Vec<u8>> {
    let mut decoder = GzDecoder::new(raw);
    let mut result = Vec::new();
    decoder
        .by_ref()
        .take(limit as u64 + 1)
        .read_to_end(&mut result)
        .map_err(|_| Error("invalid_gzip"))?;
    require(result.len() <= limit, "request_too_large")?;
    require(decoder.into_inner().is_empty(), "invalid_gzip")?;
    Ok(result)
}
pub fn packet(raw: &[u8], limit: usize) -> Result<Request> {
    require(raw.len() <= limit, "request_too_large")?;
    Request::parse(std::str::from_utf8(raw).map_err(|_| Error("invalid_json"))?)
}
pub fn path_packet(encoded: &str, encoding: &str, limit: usize) -> Result<Request> {
    let bytes = unb64(encoded, limit)?;
    match encoding {
        "j" => packet(&bytes, limit),
        "gz" => packet(&gunzip(&bytes, limit)?, limit),
        _ => Err(Error("unknown_encoding")),
    }
}
pub fn url_safe(packet: &Request) -> Result<()> {
    let proof = packet.field("proof")?;
    require(
        proof.is_null() || !proof.as_object()?.contains_key("token"),
        "secure_channel_required",
    )?;
    require(
        !matches!(
            packet.field("operation")?.as_str()?,
            "identity.temporary"
                | "identity.custodial_create"
                | "identity.token_create"
                | "identity.token_rotate"
                | "identity.token_recover"
        ),
        "secure_channel_required",
    )?;
    require(
        !contains_secret_fields(packet.field("arguments")?)?,
        "secure_channel_required",
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn nested_credentials_and_ambiguous_operation_paths_are_rejected() {
        for (path, query) in [
            ("/read", "ordinary=x%26token=secret"),
            ("/read", "%2574oken=x"),
            ("/-/g/op/token/x", ""),
            ("/read/private%255fkey=x", ""),
        ] {
            assert_eq!(
                safe_target(path.as_bytes(), query.as_bytes(), 8192),
                Err(Error("secure_channel_required"))
            );
        }
        assert_eq!(
            safe_target(b"/%2d/g/content.post_create/j/abc", b"", 8192),
            Err(Error("not_found"))
        );
        assert!(safe_target(b"/_read/q/1/r/%2Fmain%2Ftopic", b"", 8192).is_ok());
        assert!(contains_secret_fields(
            &Json::parse(r#"{"nested":[{"proof[access_token]":"x"}]}"#).unwrap()
        )
        .unwrap());
    }
    #[test]
    fn gzip_must_be_one_complete_bounded_member() {
        use std::io::Write;
        let mut encoder = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        encoder.write_all(b"hello").unwrap();
        let encoded = encoder.finish().unwrap();
        assert_eq!(gunzip(&encoded, 5).unwrap(), b"hello");
        assert_eq!(gunzip(&encoded, 4), Err(Error("request_too_large")));
        assert_eq!(
            gunzip(&encoded[..encoded.len() - 1], 5),
            Err(Error("invalid_gzip"))
        );
        let mut second = encoded.clone();
        second.extend(&encoded);
        assert_eq!(gunzip(&second, 10), Err(Error("invalid_gzip")));
    }
}
