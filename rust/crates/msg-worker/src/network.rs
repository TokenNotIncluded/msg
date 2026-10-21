//! Tool network constraints remain separate alternatives throughout a request.
use crate::outbound::require_public;
use msg_core::{Error, Json, Result};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    net::IpAddr,
};
use url::{Host, Url};

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NetworkPolicy {
    pub schemes: BTreeSet<String>,
    pub hosts: BTreeSet<String>,
    pub ports: BTreeSet<u16>,
    pub methods: BTreeSet<String>,
    pub allow_private: bool,
    pub timeout_ms: u64,
    pub max_response_bytes: u64,
    pub max_redirects: u32,
}
impl NetworkPolicy {
    pub fn validate(&self) -> Result<()> {
        if self.timeout_ms == 0 || self.max_response_bytes == 0 {
            return Err(Error("invalid_network_limit"));
        }
        if self.schemes.is_empty() || self.ports.is_empty() || self.methods.is_empty() {
            return Err(Error("network_policy_empty"));
        }
        for host in &self.hosts {
            normalized_host(host)?;
        }
        Ok(())
    }

    pub fn intersect(&self, constraints: &BTreeMap<String, Json>) -> Result<Self> {
        if constraints.keys().any(|key| {
            !matches!(
                key.as_str(),
                "hosts"
                    | "schemes"
                    | "ports"
                    | "methods"
                    | "timeout_ms"
                    | "max_response_bytes"
                    | "max_redirects"
            )
        }) {
            return Err(Error("unknown_network_constraint"));
        }
        let mut narrowed = self.clone();
        for name in ["hosts", "schemes", "methods"] {
            let Some(value) = constraints.get(name) else {
                continue;
            };
            let supplied: BTreeSet<String> = value
                .as_array()?
                .iter()
                .map(|v| {
                    let text = v.as_str()?;
                    if name == "hosts" {
                        normalized_host(text)
                    } else {
                        Ok(text.to_owned())
                    }
                })
                .collect::<Result<_>>()?;
            let current = match name {
                "hosts" => &mut narrowed.hosts,
                "schemes" => &mut narrowed.schemes,
                _ => &mut narrowed.methods,
            };
            if name == "hosts" {
                *current = current
                    .iter()
                    .map(|s| normalized_host(s))
                    .collect::<Result<_>>()?;
            }
            *current = if name == "hosts" && current.is_empty() {
                supplied
            } else {
                current.intersection(&supplied).cloned().collect()
            };
            if current.is_empty() {
                return Err(Error("network_policy_empty"));
            }
        }
        if let Some(value) = constraints.get("ports") {
            let supplied: BTreeSet<u16> = value
                .as_array()?
                .iter()
                .map(|v| {
                    v.as_integer()?
                        .parse()
                        .map_err(|_| Error("invalid_network_limit"))
                })
                .collect::<Result<_>>()?;
            narrowed.ports = narrowed.ports.intersection(&supplied).copied().collect();
            if narrowed.ports.is_empty() {
                return Err(Error("network_policy_empty"));
            }
        }
        for name in ["timeout_ms", "max_response_bytes", "max_redirects"] {
            if let Some(value) = constraints.get(name) {
                let value = value
                    .as_integer()?
                    .parse::<u64>()
                    .map_err(|_| Error("invalid_network_limit"))?;
                if name != "max_redirects" && value == 0 {
                    return Err(Error("invalid_network_limit"));
                }
                match name {
                    "timeout_ms" => narrowed.timeout_ms = narrowed.timeout_ms.min(value),
                    "max_response_bytes" => {
                        narrowed.max_response_bytes = narrowed.max_response_bytes.min(value)
                    }
                    _ => {
                        narrowed.max_redirects = u64::from(narrowed.max_redirects).min(value) as u32
                    }
                }
            }
        }
        narrowed.validate()?;
        Ok(narrowed)
    }

    /// Intersect queued authority with current authority; private access needs both.
    pub fn captured(&self, captured: &Self) -> Result<Self> {
        captured.validate()?;
        let mut restrictions = msg_storage::records::encode(captured)?.into_object()?;
        restrictions.remove("allow_private");
        if captured.hosts.is_empty() {
            restrictions.remove("hosts");
        }
        let mut result = self.intersect(&restrictions)?;
        result.allow_private &= captured.allow_private;
        Ok(result)
    }
    pub fn allows_host(&self, host: &str) -> Result<bool> {
        let host = normalized_host(host)?;
        Ok(self.hosts.is_empty()
            || self
                .hosts
                .iter()
                .map(|h| normalized_host(h))
                .collect::<Result<BTreeSet<_>>>()?
                .contains(&host))
    }
}

pub fn normalized_host(host: &str) -> Result<String> {
    if host.is_empty()
        || host.chars().count() > 253
        || host.contains('%')
        || host.chars().any(|c| c <= ' ' || c == '\u{7f}')
    {
        return Err(Error("invalid_host"));
    }
    let host = host.trim_end_matches('.').to_lowercase();
    if let Ok(ip) = host.parse::<IpAddr>() {
        return Ok(ip.to_string());
    }
    // URL libraries also accept legacy numeric IPv4 spellings. Reject those:
    // they must not silently change a certificate's exact host constraint.
    let Host::Domain(domain) = Host::parse(&host).map_err(|_| Error("invalid_host"))? else {
        return Err(Error("invalid_host"));
    };
    if domain.len() > 253
        || domain.split('.').any(|label| {
            label.is_empty()
                || label.len() > 63
                || label.starts_with('-')
                || label.ends_with('-')
                || !label
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        })
    {
        return Err(Error("invalid_host"));
    }
    Ok(domain)
}

pub fn validate_url(value: &str, method: &str, policy: &NetworkPolicy) -> Result<Url> {
    if value.is_empty()
        || value.chars().count() > 8192
        || value.contains('\\')
        || value.chars().any(|c| c <= ' ' || c == '\u{7f}')
    {
        return Err(Error("invalid_network_url"));
    }
    let mut parsed = Url::parse(value).map_err(|_| Error("invalid_network_url"))?;
    if !matches!(parsed.scheme(), "http" | "https") || !policy.schemes.contains(parsed.scheme()) {
        return Err(Error("network_scheme_forbidden"));
    }
    // Require explicit HTTP authority syntax, including an absent userinfo field.
    let authority = value
        .split_once("://")
        .ok_or(Error("invalid_network_url"))?
        .1
        .split(['/', '?', '#'])
        .next()
        .ok_or(Error("invalid_network_url"))?;
    if authority.contains('@') || parsed.fragment().is_some_and(|v| !v.is_empty()) {
        return Err(Error("invalid_network_url"));
    }
    let raw_host = if authority.starts_with('[') {
        authority
            .strip_prefix('[')
            .and_then(|v| v.split_once(']'))
            .map(|(h, _)| h)
            .ok_or(Error("invalid_network_url"))?
    } else {
        authority
            .split(':')
            .next()
            .ok_or(Error("invalid_network_url"))?
    };
    let host = normalized_host(raw_host)?;
    if !policy.ports.contains(
        &parsed
            .port_or_known_default()
            .ok_or(Error("invalid_network_url"))?,
    ) {
        return Err(Error("network_port_forbidden"));
    }
    if !policy.methods.contains(method) {
        return Err(Error("network_method_forbidden"));
    }
    if !policy.allows_host(&host)? {
        return Err(Error("network_host_forbidden"));
    }
    let url_host = if host.contains(':') {
        format!("[{host}]")
    } else {
        host
    };
    parsed
        .set_host(Some(&url_host))
        .map_err(|_| Error("invalid_network_url"))?;
    Ok(parsed)
}

pub fn validate_addresses(addresses: &[IpAddr], policy: &NetworkPolicy) -> Result<()> {
    if addresses.is_empty() {
        return Err(Error("dns_no_address"));
    }
    for address in addresses {
        let actual = match address {
            IpAddr::V6(ip) => ip.to_ipv4_mapped().map(IpAddr::V4).unwrap_or(*address),
            _ => *address,
        };
        if actual.is_unspecified() || actual.is_multicast() {
            return Err(Error("invalid_network_target"));
        }
        if !policy.allow_private {
            require_public(actual)?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    pub(crate) fn policy() -> NetworkPolicy {
        serde_json::from_value(serde_json::json!({"schemes":["http","https"],"hosts":[],"ports":[80,443],"methods":["GET","POST"],"allow_private":false,"timeout_ms":10000,"max_response_bytes":4096,"max_redirects":3})).unwrap()
    }
    #[test]
    fn intersection_never_combines_alternative_hosts_or_private_access() {
        let mut a = policy();
        a.hosts.insert("a.example".into());
        let mut b = policy();
        b.hosts.insert("b.example".into());
        b.allow_private = true;
        assert_eq!(a.captured(&b).err(), Some(Error("network_policy_empty")));
        b.hosts.clear();
        b.timeout_ms = 100;
        let result = a.captured(&b).unwrap();
        assert_eq!(result.timeout_ms, 100);
        assert!(!result.allow_private);
        assert!(result.hosts.contains("a.example"));
        assert!(a
            .intersect(
                Json::parse(r#"{"timeout_ms":true}"#)
                    .unwrap()
                    .as_object()
                    .unwrap()
            )
            .is_err());
    }
    #[test]
    fn targets_cannot_hide_private_ips_or_credentials() {
        let policy = policy();
        for url in [
            "https://@example.org/",
            "https://user:pw@example.org/",
            "https://2130706433/",
            "https://127.1/",
            "https://example.org\\@127.0.0.1/",
            "https://example.org/#secret",
        ] {
            assert!(validate_url(url, "GET", &policy).is_err(), "{url}");
        }
        assert!(validate_url("https://EXAMPLE.org./", "GET", &policy).is_ok());
        for ip in ["127.0.0.1", "::ffff:127.0.0.1", "224.0.0.1", "0.0.0.0"] {
            assert!(validate_addresses(&[ip.parse().unwrap()], &policy).is_err());
        }
        let mut private = policy;
        private.allow_private = true;
        assert!(validate_addresses(&["127.0.0.1".parse().unwrap()], &private).is_ok());
        assert!(validate_addresses(&["224.0.0.1".parse().unwrap()], &private).is_err());
    }
}
