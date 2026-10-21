//! A bounded native tool helper inside mandatory bubblewrap isolation.
use crate::{
    network::{self, NetworkPolicy},
    outbound,
    process::{self, Directory, Stop},
};
use hickory_resolver::{proto::rr::RecordType, Resolver};
use msg_core::{Error, Json, Result};
use msg_storage::records::{decode, encode};
use reqwest::{
    blocking::{Body, Client},
    header::{HeaderMap, HeaderName, HeaderValue},
    redirect, Method,
};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs,
    io::{Read, Write},
    net::IpAddr,
    path::{Path, PathBuf},
    process::Command,
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Packet {
    executor: String,
    arguments: Json,
    policies: Vec<NetworkPolicy>,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub media_type: String,
    pub metadata: Json,
}
pub(crate) struct ToolResult {
    pub path: PathBuf,
    pub manifest: Manifest,
}

pub struct BubblewrapRunner {
    bwrap: PathBuf,
    child: PathBuf,
    max_request_bytes: usize,
}
impl BubblewrapRunner {
    pub fn new(bwrap: PathBuf, child: PathBuf, max_request_bytes: usize) -> Result<Self> {
        if !bwrap.is_absolute() || !child.is_absolute() || max_request_bytes > msg_core::MAX_BYTES {
            return Err(Error("invalid_tool_runner_config"));
        }
        Ok(Self {
            bwrap,
            child,
            max_request_bytes,
        })
    }
    pub(crate) fn execute(
        &self,
        executor: &str,
        mut arguments: Json,
        policies: Vec<NetworkPolicy>,
        body: Option<&Path>,
        directory: &Directory,
        stop: &Stop<'_>,
    ) -> Result<ToolResult> {
        let bwrap =
            fs::canonicalize(&self.bwrap).map_err(|_| Error("tool_isolation_unavailable"))?;
        let child =
            fs::canonicalize(&self.child).map_err(|_| Error("tool_isolation_unavailable"))?;
        if !bwrap.is_file() || !child.is_file() {
            return Err(Error("tool_isolation_unavailable"));
        }
        if let Some(body) = body {
            let mut args = arguments.into_object()?;
            args.remove("body_ref");
            args.insert("_body_path".into(), Json::string("/request-body"));
            arguments = Json::from_object(args);
            process::open(body)?;
        }
        let timeout = policies
            .iter()
            .map(|p| p.timeout_ms)
            .max()
            .ok_or(Error("network_policy_denied"))?;
        let packet = encode(&Packet {
            executor: executor.into(),
            arguments,
            policies,
        })?
        .canonical()?;
        if packet.len()
            > self
                .max_request_bytes
                .saturating_add(65536)
                .min(msg_core::MAX_BYTES)
        {
            return Err(Error("tool_request_too_large"));
        }
        let output = directory.path().join("sandbox-output");
        let mut builder = fs::DirBuilder::new();
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            builder.mode(0o700);
        }
        builder
            .create(&output)
            .map_err(|_| Error("staging_io_error"))?;
        let mut command = Command::new(bwrap);
        command.env_clear().args([
            "--unshare-all",
            "--share-net",
            "--unshare-user",
            "--disable-userns",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--clearenv",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]);
        for path in ["/usr", "/bin", "/lib", "/lib64"] {
            if Path::new(path).exists() {
                command.args(["--ro-bind", path, path]);
            }
        }
        command.args(["--dir", "/etc"]);
        for path in [
            "/etc/resolv.conf",
            "/etc/hosts",
            "/etc/nsswitch.conf",
            "/etc/ssl/certs",
        ] {
            if Path::new(path).exists() {
                command.args(["--ro-bind", path, path]);
            }
        }
        command
            .arg("--ro-bind")
            .arg(child)
            .arg("/msg-tool-sandbox")
            .arg("--bind")
            .arg(&output)
            .arg("/output")
            .args(["--setenv", "HOME", "/nonexistent", "--chdir", "/tmp"]);
        if let Some(body) = body {
            command.arg("--ro-bind").arg(body).arg("/request-body");
        }
        command.arg("/msg-tool-sandbox");
        let input = process::packet_file(directory, "request.json", packet.as_bytes())?;
        let (status, bytes) = process::run(
            &mut command,
            Some(input),
            directory.create("manifest.json")?,
            Duration::from_millis(timeout.saturating_add(10000)),
            65536,
            stop,
        )
        .map_err(|e| {
            if e.0 == "subprocess_unavailable" {
                Error("tool_isolation_unavailable")
            } else {
                e
            }
        })?;
        let value = std::str::from_utf8(&bytes)
            .ok()
            .and_then(|s| Json::parse(s).ok());
        if !status.success() {
            let code = value
                .as_ref()
                .and_then(|v| v.as_object().ok())
                .and_then(|v| v.get("code"))
                .and_then(|v| v.as_str().ok());
            return Err(child_error(code));
        }
        let manifest: Manifest = decode(&value.ok_or(Error("invalid_tool_manifest"))?)
            .map_err(|_| Error("invalid_tool_manifest"))?;
        validate_manifest(&manifest)?;
        let source = output.join("result.bin");
        process::open(&source)?;
        let destination = directory.path().join("output.bin");
        fs::rename(source, &destination).map_err(|_| Error("invalid_tool_output_path"))?;
        Ok(ToolResult {
            path: destination,
            manifest,
        })
    }
}
fn child_error(code: Option<&str>) -> Error {
    for allowed in [
        "external_uncertain",
        "network_policy_denied",
        "network_scheme_forbidden",
        "network_port_forbidden",
        "network_method_forbidden",
        "network_host_forbidden",
        "private_target_forbidden",
        "invalid_network_target",
        "invalid_network_url",
        "invalid_host",
        "invalid_request_header",
        "invalid_tool_input",
        "invalid_base64",
        "unsafe_redirect_method",
        "too_many_redirects",
        "tool_response_too_large",
        "tool_request_too_large",
        "untrusted_tool_executor",
        "invalid_network_limit",
        "network_policy_empty",
        "tool_isolation_failed",
    ] {
        if code == Some(allowed) {
            return Error(allowed);
        }
    }
    Error("tool_isolation_failed")
}
fn validate_manifest(value: &Manifest) -> Result<()> {
    if value.media_type.chars().count() > 255
        || value.media_type.contains(['\r', '\n'])
        || value.metadata.as_object().is_err()
    {
        return Err(Error("invalid_tool_manifest"));
    }
    Ok(())
}

/// Checks the installed schemas again before passing the bounded packet to a child.
pub fn validate_input(executor: &str, input: &Json, internal_body: bool) -> Result<()> {
    let args = input.as_object().map_err(|_| Error("invalid_tool_input"))?;
    let field = |name: &str| args.get(name).ok_or(Error("invalid_tool_input"));
    match executor {
        "dns" => {
            if args.len() != 2 || !args.contains_key("name") || !args.contains_key("type") {
                return Err(Error("invalid_tool_input"));
            }
            network::normalized_host(field("name")?.as_str()?)?;
            record_type(field("type")?.as_str()?)?;
        }
        "curl" => {
            if args.keys().any(|k| {
                !matches!(
                    k.as_str(),
                    "url" | "method" | "headers" | "body" | "body_ref"
                ) && !(internal_body && k == "_body_path")
            }) {
                return Err(Error("invalid_tool_input"));
            }
            let url = field("url")?.as_str()?;
            if url.is_empty() || url.chars().count() > 8192 {
                return Err(Error("invalid_tool_input"));
            }
            let method = args
                .get("method")
                .map(Json::as_str)
                .transpose()?
                .unwrap_or("GET");
            if !matches!(
                method,
                "GET" | "HEAD" | "POST" | "PUT" | "PATCH" | "DELETE" | "OPTIONS"
            ) {
                return Err(Error("invalid_tool_input"));
            }
            let bodies = ["body", "body_ref", "_body_path"]
                .iter()
                .filter(|name| args.contains_key(**name))
                .count();
            if bodies > 1 {
                return Err(Error("ambiguous_request_body"));
            }
            if let Some(body) = args.get("body") {
                msg_core::unb64(body.as_str()?, msg_core::MAX_BYTES)?;
            }
            if let Some(body) = args.get("body_ref") {
                let reference: msg_identity::models::ResourceRef = decode(body)?;
                if reference.revision.is_none() {
                    return Err(Error("source_revision_required"));
                }
            }
            if let Some(path) = args.get("_body_path") {
                if path.as_str()? != "/request-body" {
                    return Err(Error("invalid_tool_input"));
                }
            }
            headers(args)?;
        }
        _ => return Err(Error("untrusted_tool_executor")),
    }
    Ok(())
}
fn headers(args: &BTreeMap<String, Json>) -> Result<HeaderMap> {
    let mut result = HeaderMap::new();
    if let Some(value) = args.get("headers") {
        let value = value.as_object()?;
        if value.len() > 30 {
            return Err(Error("invalid_request_header"));
        }
        for (name, value) in value {
            let lower = name.to_ascii_lowercase();
            let value = value.as_str()?;
            if name.is_empty()
                || matches!(
                    lower.as_str(),
                    "host"
                        | "connection"
                        | "proxy-authorization"
                        | "proxy-connection"
                        | "transfer-encoding"
                        | "content-length"
                        | "upgrade"
                        | "te"
                        | "trailer"
                        | "forwarded"
                        | "x-forwarded-for"
                        | "x-forwarded-host"
                )
                || value.chars().count() > 4096
                || value.contains(['\r', '\n'])
            {
                return Err(Error("invalid_request_header"));
            }
            result.insert(
                HeaderName::from_bytes(name.as_bytes())
                    .map_err(|_| Error("invalid_request_header"))?,
                HeaderValue::from_str(value).map_err(|_| Error("invalid_request_header"))?,
            );
        }
    }
    Ok(result)
}
fn unexpired(policies: &mut Vec<NetworkPolicy>, started: Instant) -> Result<()> {
    policies.retain(|p| started.elapsed().as_millis() <= u128::from(p.timeout_ms));
    if policies.is_empty() {
        return Err(Error("external_uncertain"));
    }
    Ok(())
}
fn maximum_timeout(policies: &[NetworkPolicy], started: Instant) -> Result<Duration> {
    let duration = Duration::from_millis(
        policies
            .iter()
            .map(|p| p.timeout_ms)
            .max()
            .ok_or(Error("network_policy_denied"))?,
    );
    duration
        .checked_sub(started.elapsed())
        .filter(|d| !d.is_zero())
        .ok_or(Error("external_uncertain"))
}
fn http_request(
    args: &Json,
    mut policies: Vec<NetworkPolicy>,
    destination: &Path,
) -> Result<Manifest> {
    let args = args.as_object()?;
    let method = args
        .get("method")
        .map(Json::as_str)
        .transpose()?
        .unwrap_or("GET");
    let mut target = args
        .get("url")
        .ok_or(Error("invalid_tool_input"))?
        .as_str()?
        .to_owned();
    let mut headers = headers(args)?;
    let body = args
        .get("body")
        .map(|v| msg_core::unb64(v.as_str()?, msg_core::MAX_BYTES))
        .transpose()?;
    let body_path = args.get("_body_path").map(Json::as_str).transpose()?;
    let started = Instant::now();
    let mut redirects = 0;
    loop {
        unexpired(&mut policies, started)?;
        policies.retain(|p| network::validate_url(&target, method, p).is_ok());
        let first = policies.first().ok_or(Error("network_policy_denied"))?;
        let parsed = network::validate_url(&target, method, first)?;
        let host = match parsed.host().ok_or(Error("invalid_network_url"))? {
            url::Host::Domain(v) => v.to_owned(),
            url::Host::Ipv4(v) => v.to_string(),
            url::Host::Ipv6(v) => v.to_string(),
        };
        let port = parsed
            .port_or_known_default()
            .ok_or(Error("invalid_network_url"))?;
        let deadline = started
            .checked_add(Duration::from_millis(
                policies
                    .iter()
                    .map(|p| p.timeout_ms)
                    .max()
                    .ok_or(Error("network_policy_denied"))?,
            ))
            .ok_or(Error("invalid_network_limit"))?;
        let addresses = outbound::resolve(&host, port, deadline)?;
        let ips: Vec<IpAddr> = addresses.iter().map(|a| a.ip()).collect();
        unexpired(&mut policies, started)?;
        policies.retain(|p| network::validate_addresses(&ips, p).is_ok());
        if policies.is_empty() {
            return Err(Error("network_policy_denied"));
        }
        let client = Client::builder()
            .no_proxy()
            .no_gzip()
            .no_brotli()
            .no_deflate()
            .no_zstd()
            .redirect(redirect::Policy::none())
            .http1_only()
            .timeout(maximum_timeout(&policies, started)?)
            .resolve_to_addrs(&host, &addresses)
            .build()
            .map_err(|_| Error("external_uncertain"))?;
        let mut request = client
            .request(
                Method::from_bytes(method.as_bytes()).map_err(|_| Error("invalid_tool_input"))?,
                parsed.clone(),
            )
            .headers(headers.clone());
        if let Some(path) = body_path {
            request = request.body(Body::from(process::open(Path::new(path))?));
        } else if let Some(body) = &body {
            request = request.body(body.clone());
        }
        let mut response = request.send().map_err(|_| Error("external_uncertain"))?;
        unexpired(&mut policies, started)?;
        let status = response.status().as_u16();
        if matches!(status, 301 | 302 | 303 | 307 | 308) {
            if let Some(location) = response.headers().get("location").filter(|v| !v.is_empty()) {
                if !matches!(method, "GET" | "HEAD") && !matches!(status, 307 | 308) {
                    return Err(Error("unsafe_redirect_method"));
                }
                redirects += 1;
                policies.retain(|p| redirects <= p.max_redirects);
                if policies.is_empty() {
                    return Err(Error("too_many_redirects"));
                }
                let next = parsed
                    .join(
                        location
                            .to_str()
                            .map_err(|_| Error("invalid_network_url"))?,
                    )
                    .map_err(|_| Error("invalid_network_url"))?;
                if next.origin() != parsed.origin() {
                    headers.remove("authorization");
                    headers.remove("cookie");
                }
                target = next.to_string();
                continue;
            }
        }
        let media_type = response
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("application/octet-stream")
            .chars()
            .take(255)
            .collect::<String>();
        let selected: BTreeMap<String, String> = response
            .headers()
            .iter()
            .filter(|(k, _)| {
                matches!(
                    k.as_str(),
                    "content-type" | "content-encoding" | "etag" | "last-modified"
                )
            })
            .map(|(k, v)| {
                Ok((
                    k.as_str().to_owned(),
                    v.to_str()
                        .map_err(|_| Error("invalid_tool_manifest"))?
                        .chars()
                        .take(4096)
                        .collect(),
                ))
            })
            .collect::<Result<_>>()?;
        let mut output = process::create(destination)?;
        let mut length: u64 = 0;
        let mut buffer = [0; 65536];
        loop {
            let count = response
                .read(&mut buffer)
                .map_err(|_| Error("external_uncertain"))?;
            if count == 0 {
                break;
            }
            length = length
                .checked_add(count as u64)
                .ok_or(Error("tool_response_too_large"))?;
            unexpired(&mut policies, started)?;
            policies.retain(|p| length <= p.max_response_bytes);
            if policies.is_empty() {
                return Err(Error("tool_response_too_large"));
            }
            output
                .write_all(&buffer[..count])
                .map_err(|_| Error("external_uncertain"))?;
        }
        output.sync_all().map_err(|_| Error("external_uncertain"))?;
        unexpired(&mut policies, started)?;
        return Ok(Manifest {
            media_type,
            metadata: encode(
                &serde_json::json!({"status":status,"headers":selected,"redirects":redirects}),
            )?,
        });
    }
}
fn record_type(value: &str) -> Result<RecordType> {
    Ok(match value {
        "A" => RecordType::A,
        "AAAA" => RecordType::AAAA,
        "TXT" => RecordType::TXT,
        "MX" => RecordType::MX,
        "NS" => RecordType::NS,
        "CNAME" => RecordType::CNAME,
        "SRV" => RecordType::SRV,
        _ => return Err(Error("invalid_tool_input")),
    })
}
fn dns_query(
    args: &Json,
    mut policies: Vec<NetworkPolicy>,
    destination: &Path,
) -> Result<Manifest> {
    let args = args.as_object()?;
    let name = network::normalized_host(
        args.get("name")
            .ok_or(Error("invalid_tool_input"))?
            .as_str()?,
    )?;
    let kind = args
        .get("type")
        .ok_or(Error("invalid_tool_input"))?
        .as_str()?;
    let rrtype = record_type(kind)?;
    policies.retain(|p| p.allows_host(&name).unwrap_or(false));
    if policies.is_empty() {
        return Err(Error("network_policy_denied"));
    }
    let started = Instant::now();
    let timeout = maximum_timeout(&policies, started)?;
    let query = name.clone();
    let (sender, receiver) = mpsc::sync_channel(1);
    thread::Builder::new()
        .name("tool-dns-query".into())
        .spawn(move || {
            let result = (|| {
                let (config, mut options) = hickory_resolver::system_conf::read_system_conf()
                    .map_err(|_| Error("external_uncertain"))?;
                options.timeout = timeout;
                options.attempts = 1;
                options.cache_size = 0;
                options.preserve_intermediates = false;
                let resolver =
                    Resolver::new(config, options).map_err(|_| Error("external_uncertain"))?;
                let answer = resolver
                    .lookup(query, rrtype)
                    .map_err(|_| Error("external_uncertain"))?;
                let records: Vec<_> = answer
                    .record_iter()
                    .filter(|r| r.record_type() == rrtype)
                    .collect();
                let ttl = records
                    .iter()
                    .map(|r| r.ttl())
                    .min()
                    .ok_or(Error("external_uncertain"))?;
                let values = records
                    .iter()
                    .map(|r| {
                        r.data()
                            .map(ToString::to_string)
                            .ok_or(Error("external_uncertain"))
                    })
                    .collect::<Result<Vec<_>>>()?;
                Ok((ttl, values))
            })();
            let _ = sender.send(result);
        })
        .map_err(|_| Error("external_uncertain"))?;
    let (ttl, values) = receiver
        .recv_timeout(timeout)
        .map_err(|_| Error("external_uncertain"))??;
    unexpired(&mut policies, started)?;
    if matches!(kind, "A" | "AAAA") {
        let ips: Vec<IpAddr> = values
            .iter()
            .map(|s| s.parse().map_err(|_| Error("invalid_network_target")))
            .collect::<Result<_>>()?;
        policies.retain(|p| network::validate_addresses(&ips, p).is_ok());
        if policies.is_empty() {
            return Err(Error("network_policy_denied"));
        }
    }
    let body = encode(&serde_json::json!({"name":name,"type":kind,"ttl":ttl,"answers":values}))?
        .canonical()?;
    if !policies
        .iter()
        .any(|p| body.len() as u64 <= p.max_response_bytes)
    {
        return Err(Error("tool_response_too_large"));
    }
    let mut output = process::create(destination)?;
    output
        .write_all(body.as_bytes())
        .map_err(|_| Error("external_uncertain"))?;
    output.sync_all().map_err(|_| Error("external_uncertain"))?;
    Ok(Manifest {
        media_type: "application/json".into(),
        metadata: Json::parse(r#"{"kind":"dns"}"#)?,
    })
}

/// Only the installed helper binary calls this; limits never affect the daemon.
pub fn child_main() -> std::process::ExitCode {
    let result = (|| {
        #[cfg(unix)]
        {
            use rustix::process::{getrlimit, setrlimit, Resource, Rlimit};
            for (resource, limit) in [
                (Resource::Core, 0),
                (Resource::Nofile, 128),
                (Resource::Cpu, 40),
                (Resource::Fsize, 64 * 1024 * 1024),
                (Resource::As, 768 * 1024 * 1024),
            ] {
                let limit = getrlimit(resource)
                    .maximum
                    .map_or(limit, |current| current.min(limit));
                setrlimit(
                    resource,
                    Rlimit {
                        current: Some(limit),
                        maximum: Some(limit),
                    },
                )
                .map_err(|_| Error("tool_isolation_failed"))?;
            }
        }
        #[cfg(not(unix))]
        {
            return Err(Error("tool_isolation_failed"));
        }
        let mut raw = Vec::new();
        std::io::stdin()
            .take(msg_core::MAX_BYTES as u64 + 1)
            .read_to_end(&mut raw)
            .map_err(|_| Error("invalid_tool_input"))?;
        if raw.len() > msg_core::MAX_BYTES {
            return Err(Error("tool_request_too_large"));
        }
        let packet: Packet = decode(&Json::parse(
            std::str::from_utf8(&raw).map_err(|_| Error("invalid_tool_input"))?,
        )?)?;
        if packet.policies.is_empty() {
            return Err(Error("network_policy_denied"));
        }
        for policy in &packet.policies {
            policy.validate()?;
        }
        validate_input(&packet.executor, &packet.arguments, true)?;
        let destination = Path::new("/output/result.bin");
        let value = match packet.executor.as_str() {
            "curl" => http_request(&packet.arguments, packet.policies, destination)?,
            "dns" => dns_query(&packet.arguments, packet.policies, destination)?,
            _ => return Err(Error("untrusted_tool_executor")),
        };
        validate_manifest(&value)?;
        encode(&value)?.canonical()
    })();
    let (code, output) = match result {
        Ok(output) => (0, output),
        Err(error) => (1, format!("{{\"code\":\"{}\"}}", error.0)),
    };
    if std::io::stdout().write_all(output.as_bytes()).is_err() {
        return std::process::ExitCode::FAILURE;
    }
    std::process::ExitCode::from(code)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn schemas_do_not_admit_shells_paths_or_ambiguous_bodies() {
        for raw in [
            r#"{"url":"https://example.org","_body_path":"/etc/passwd"}"#,
            r#"{"url":"https://example.org","body":"YQ","body_ref":{"id":"r","revision":"v"}}"#,
            r#"{"url":"https://example.org","headers":{"Host":"internal"}}"#,
            r#"{"url":"https://example.org","headers":{"X-Name":"ok\r\nHeader: bad"}}"#,
        ] {
            assert!(validate_input("curl", &Json::parse(raw).unwrap(), false).is_err());
        }
        assert!(validate_input("sh", &Json::parse("{}").unwrap(), false).is_err());
        assert!(validate_input(
            "curl",
            &Json::parse(r#"{"url":"https://example.org","body":"YQ"}"#).unwrap(),
            false
        )
        .is_ok());
    }
    #[test]
    fn http_rejects_private_target_before_connecting() {
        let policy: NetworkPolicy = decode(&Json::parse(r#"{"schemes":["http"],"hosts":[],"ports":[9],"methods":["POST"],"allow_private":false,"timeout_ms":1000,"max_response_bytes":100,"max_redirects":0}"#).unwrap()).unwrap();
        let result = http_request(
            &Json::parse(r#"{"url":"http://127.0.0.1:9","method":"POST"}"#).unwrap(),
            vec![policy],
            Path::new("/this-must-not-exist"),
        );
        assert_eq!(result.err(), Some(Error("network_policy_denied")));
    }
}
