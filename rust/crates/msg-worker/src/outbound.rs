//! Fixed-target HTTPS delivery. DNS is resolved once, every returned address is
//! checked, and the connection uses that exact address with TLS name validation.
//! No redirects, proxies, cookies, environment credentials, or response bodies.
use crate::{EffectOutcome, EffectResult};
use hmac::{Hmac, Mac};
use msg_core::{Error, Result};
use msg_storage::jobs::status;
use sha2::Sha256;
use std::{
    io::{Read, Write},
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, TcpStream, ToSocketAddrs},
    sync::{
        mpsc::{self, SyncSender},
        OnceLock,
    },
    thread,
    time::{Duration, Instant},
};

#[derive(Clone)]
pub struct Endpoint {
    host: String,
    target: String,
    host_header: String,
}
impl Endpoint {
    pub fn parse(url: &str) -> Result<Self> {
        if url.is_empty()
            || url.len() > 2048
            || !url.bytes().all(|c| (33..127).contains(&c))
            || url.contains(['\\', '#'])
        {
            return Err(Error("invalid_webhook_url"));
        }
        let (scheme, rest) = url.split_once("://").ok_or(Error("invalid_webhook_url"))?;
        if !scheme.eq_ignore_ascii_case("https") {
            return Err(Error("invalid_webhook_url"));
        }
        let (authority, path) = match rest.find(['/', '?']) {
            Some(i) => (&rest[..i], &rest[i..]),
            None => (rest, ""),
        };
        if authority.is_empty() || authority.contains('@') {
            return Err(Error("invalid_webhook_url"));
        }
        let (host, port) = if let Some(value) = authority.strip_prefix('[') {
            let (host, suffix) = value.split_once(']').ok_or(Error("invalid_webhook_url"))?;
            if host.parse::<Ipv6Addr>().is_err() {
                return Err(Error("invalid_webhook_url"));
            }
            let port = if suffix.is_empty() {
                None
            } else {
                Some(
                    suffix
                        .strip_prefix(':')
                        .ok_or(Error("invalid_webhook_url"))?,
                )
            };
            (host, port)
        } else if let Some((host, port)) = authority.rsplit_once(':') {
            if host.contains(':') {
                return Err(Error("invalid_webhook_url"));
            }
            (host, Some(port))
        } else {
            (authority, None)
        };
        if let Some(port) = port {
            if port.is_empty()
                || !port.bytes().all(|c| c.is_ascii_digit())
                || port.parse::<u16>() != Ok(443)
            {
                return Err(Error("webhook_port_forbidden"));
            }
        }
        let host = normalized_host(host)?;
        if let Ok(ip) = host.parse::<IpAddr>() {
            require_public(ip)?;
        }
        let host_header = if host.contains(':') {
            format!("[{host}]")
        } else {
            host.clone()
        };
        let target = if path.is_empty() {
            "/".into()
        } else if path.starts_with('?') {
            format!("/{path}")
        } else {
            path.into()
        };
        Ok(Self {
            host,
            target,
            host_header,
        })
    }
    pub fn host(&self) -> &str {
        &self.host
    }
}

fn normalized_host(host: &str) -> Result<String> {
    if host.is_empty() || host.len() > 253 || host.contains('%') || !host.is_ascii() {
        return Err(Error("invalid_host"));
    }
    if let Ok(ip) = host.parse::<IpAddr>() {
        return Ok(ip.to_string());
    }
    let host = host.trim_end_matches('.').to_ascii_lowercase();
    if !host.split('.').all(|label| {
        !label.is_empty()
            && label.len() <= 63
            && label.as_bytes()[0].is_ascii_alphanumeric()
            && label.as_bytes()[label.len() - 1].is_ascii_alphanumeric()
            && label
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'-')
    }) {
        return Err(Error("invalid_host"));
    }
    Ok(host)
}

fn in_v4(ip: Ipv4Addr, network: u32, bits: u32) -> bool {
    u32::from(ip) >> (32 - bits) == network >> (32 - bits)
}
fn in_v6(ip: Ipv6Addr, network: &str, bits: u32) -> bool {
    // Source-owned constants below, never untrusted input.
    let Ok(network) = network.parse::<Ipv6Addr>() else {
        return false;
    };
    u128::from(ip) >> (128 - bits) == u128::from(network) >> (128 - bits)
}
pub fn require_public(ip: IpAddr) -> Result<()> {
    let allowed = match ip {
        IpAddr::V4(ip) => {
            let exception = matches!(ip.octets(), [192, 0, 0, 9] | [192, 0, 0, 10]);
            exception
                || ![
                    (0x00000000, 8),
                    (0x0a000000, 8),
                    (0x64400000, 10),
                    (0x7f000000, 8),
                    (0xa9fe0000, 16),
                    (0xac100000, 12),
                    (0xc0000000, 24),
                    (0xc0000200, 24),
                    (0xc0a80000, 16),
                    (0xc6120000, 15),
                    (0xc6336400, 24),
                    (0xcb007100, 24),
                    (0xe0000000, 4),
                    (0xf0000000, 4),
                ]
                .iter()
                .any(|&(network, bits)| in_v4(ip, network, bits))
        }
        IpAddr::V6(ip) => {
            if let Some(mapped) = ip.to_ipv4_mapped() {
                return require_public(IpAddr::V4(mapped));
            }
            let exception = [
                ("2001:1::1", 128),
                ("2001:1::2", 128),
                ("2001:3::", 32),
                ("2001:4:112::", 48),
                ("2001:20::", 28),
                ("2001:30::", 28),
            ]
            .iter()
            .any(|&(n, b)| in_v6(ip, n, b));
            exception
                || ![
                    ("::", 128),
                    ("::1", 128),
                    ("64:ff9b:1::", 48),
                    ("100::", 64),
                    ("2001::", 23),
                    ("2001:db8::", 32),
                    ("2002::", 16),
                    ("3fff::", 20),
                    ("fc00::", 7),
                    ("fe80::", 10),
                    ("fec0::", 10),
                    ("ff00::", 8),
                ]
                .iter()
                .any(|&(n, b)| in_v6(ip, n, b))
        }
    };
    if allowed {
        Ok(())
    } else {
        Err(Error("private_target_forbidden"))
    }
}

type DnsRequest = (String, u16, SyncSender<Result<Vec<SocketAddr>>>);
static RESOLVER: OnceLock<Option<SyncSender<DnsRequest>>> = OnceLock::new();
pub(crate) fn remaining(deadline: Instant) -> Result<Duration> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|d| !d.is_zero())
        .ok_or(Error("external_uncertain"))
}
pub(crate) fn resolve(host: &str, port: u16, deadline: Instant) -> Result<Vec<SocketAddr>> {
    if let Ok(ip) = host.parse::<IpAddr>() {
        return Ok(vec![SocketAddr::new(ip, port)]);
    }
    let sender = RESOLVER
        .get_or_init(|| {
            let (sender, receiver) = mpsc::sync_channel::<DnsRequest>(1);
            thread::Builder::new()
                .name("msg-worker-dns".into())
                .spawn(move || {
                    while let Ok((host, port, response)) = receiver.recv() {
                        let result = (host.as_str(), port)
                            .to_socket_addrs()
                            .map(|v| v.collect::<Vec<_>>())
                            .map_err(|_| Error("external_uncertain"));
                        let _ = response.send(result);
                    }
                })
                .ok()
                .map(|_| sender)
        })
        .as_ref()
        .ok_or(Error("external_uncertain"))?;
    let (response, receiver) = mpsc::sync_channel(1);
    sender
        .try_send((host.to_owned(), port, response))
        .map_err(|_| Error("external_uncertain"))?;
    let addresses = receiver
        .recv_timeout(remaining(deadline)?)
        .map_err(|_| Error("external_uncertain"))??;
    if addresses.is_empty() {
        return Err(Error("external_uncertain"));
    }
    Ok(addresses)
}
pub(crate) fn connect(addresses: &[SocketAddr], deadline: Instant) -> Result<TcpStream> {
    for address in addresses {
        if let Ok(stream) = TcpStream::connect_timeout(address, remaining(deadline)?) {
            stream
                .set_read_timeout(Some(remaining(deadline)?))
                .map_err(|_| Error("external_uncertain"))?;
            stream
                .set_write_timeout(Some(remaining(deadline)?))
                .map_err(|_| Error("external_uncertain"))?;
            return Ok(stream);
        }
    }
    Err(Error("external_uncertain"))
}

fn post(
    endpoint: &Endpoint,
    content_type: &str,
    body: &[u8],
    headers: &[(&str, &str)],
) -> Result<u16> {
    if body.len() > msg_core::MAX_BYTES {
        return Err(Error("request_too_large"));
    }
    let deadline = Instant::now() + Duration::from_secs(8);
    let addresses = resolve(&endpoint.host, 443, deadline)?;
    // Reject a mixed public/private DNS answer before opening any connection.
    for address in &addresses {
        require_public(address.ip())?;
    }
    let socket = connect(&addresses, deadline)?;
    let tls = native_tls::TlsConnector::new().map_err(|_| Error("external_uncertain"))?;
    let mut stream = tls
        .connect(&endpoint.host, socket)
        .map_err(|_| Error("external_uncertain"))?;
    let mut head = format!("POST {} HTTP/1.1\r\nHost: {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n", endpoint.target, endpoint.host_header, content_type, body.len());
    for (name, value) in headers {
        if name.is_empty()
            || !name.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')
            || value.len() > 4096
            || !value.bytes().all(|c| (32..127).contains(&c))
        {
            return Err(Error("invalid_webhook_header"));
        }
        head.push_str(name);
        head.push_str(": ");
        head.push_str(value);
        head.push_str("\r\n");
    }
    head.push_str("\r\n");
    stream
        .get_ref()
        .set_write_timeout(Some(remaining(deadline)?))
        .map_err(|_| Error("external_uncertain"))?;
    stream
        .write_all(head.as_bytes())
        .and_then(|_| stream.write_all(body))
        .and_then(|_| stream.flush())
        .map_err(|_| Error("external_uncertain"))?;
    for _ in 0..4 {
        let mut response = Vec::new();
        while !response.ends_with(b"\r\n\r\n") {
            if response.len() >= 16_384 {
                return Err(Error("external_uncertain"));
            }
            stream
                .get_ref()
                .set_read_timeout(Some(remaining(deadline)?))
                .map_err(|_| Error("external_uncertain"))?;
            let mut byte = [0];
            stream
                .read_exact(&mut byte)
                .map_err(|_| Error("external_uncertain"))?;
            response.push(byte[0]);
        }
        let line = response
            .split(|&c| c == b'\r')
            .next()
            .ok_or(Error("external_uncertain"))?;
        let mut fields = line.split(|&c| c == b' ');
        if !matches!(fields.next(), Some(b"HTTP/1.0" | b"HTTP/1.1")) {
            return Err(Error("external_uncertain"));
        }
        let raw = fields.next().ok_or(Error("external_uncertain"))?;
        if raw.len() != 3 || !raw.iter().all(u8::is_ascii_digit) {
            return Err(Error("external_uncertain"));
        }
        let status =
            ((raw[0] - b'0') as u16) * 100 + ((raw[1] - b'0') as u16) * 10 + (raw[2] - b'0') as u16;
        if status >= 200 {
            return Ok(status);
        }
    }
    Err(Error("external_uncertain"))
}

fn form(value: &str) -> String {
    let mut output = String::new();
    for byte in value.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                output.push(char::from(byte))
            }
            b' ' => output.push('+'),
            _ => output.push_str(&format!("%{byte:02X}")),
        }
    }
    output
}
pub fn websub(hub: &str, topic: &str) -> EffectResult<EffectOutcome> {
    let endpoint = Endpoint::parse(hub)?;
    let body = format!("hub.mode=publish&hub.url={}", form(topic));
    match post(
        &endpoint,
        "application/x-www-form-urlencoded",
        body.as_bytes(),
        &[],
    ) {
        Ok(200..=299) => Ok(EffectOutcome::Done(status("websub_delivered"))),
        Ok(408 | 429 | 500..=999) | Err(Error("external_uncertain")) => Ok(EffectOutcome::Retry {
            code: "websub_retry_scheduled",
            exhausted: "websub_attempts_exhausted",
        }),
        Ok(_) => Ok(EffectOutcome::Failed("websub_failed")),
        Err(error) => Err(error.into()),
    }
}

pub fn webhook(
    url: &str,
    secret: &[u8],
    event: &str,
    delivery: &str,
    timestamp: &str,
    body: &[u8],
) -> EffectResult<EffectOutcome> {
    let endpoint = Endpoint::parse(url)?;
    if !(32..=64).contains(&secret.len()) {
        return Err(Error("invalid_webhook_secret").into());
    }
    let mut signer =
        Hmac::<Sha256>::new_from_slice(secret).map_err(|_| Error("invalid_webhook_secret"))?;
    signer.update(timestamp.as_bytes());
    signer.update(b".");
    signer.update(body);
    let signature = signer
        .finalize()
        .into_bytes()
        .iter()
        .map(|v| format!("{v:02x}"))
        .collect::<String>();
    let signature = format!("sha256={signature}");
    match post(
        &endpoint,
        "application/json",
        body,
        &[
            ("Msg-Event-Id", event),
            ("Msg-Delivery-Id", delivery),
            ("Msg-Timestamp", timestamp),
            ("Msg-Signature", &signature),
        ],
    )? {
        200..=299 => Ok(EffectOutcome::Done(status("delivered"))),
        408 | 429 | 500..=599 => Ok(EffectOutcome::Retry {
            code: "webhook_retry_scheduled",
            exhausted: "webhook_attempts_exhausted",
        }),
        _ => Ok(EffectOutcome::Failed("failed")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn public_delivery_rejects_private_mapped_and_mixed_targets() {
        for raw in [
            "127.0.0.1",
            "10.1.2.3",
            "100.64.0.1",
            "169.254.169.254",
            "192.0.0.8",
            "224.1.2.3",
            "::1",
            "::ffff:127.0.0.1",
            "fc00::1",
            "fe80::1",
            "2001:db8::1",
            "64:ff9b:1::1",
            "2002::1",
        ] {
            assert!(require_public(raw.parse().unwrap()).is_err(), "{raw}");
        }
        for raw in [
            "1.1.1.1",
            "192.0.0.9",
            "2606:4700:4700::1111",
            "::ffff:1.1.1.1",
        ] {
            assert!(require_public(raw.parse().unwrap()).is_ok(), "{raw}");
        }
    }
    #[test]
    fn endpoint_has_no_credential_redirect_or_parser_ambiguity() {
        for raw in [
            "http://example.com/",
            "https://user@example.com/",
            "https://example.com:444/",
            "https://example.com/#fragment",
            "https://127.0.0.1/",
            "https://[::1]/",
            "https://example.com\\@localhost/",
            "https://example.com/\r\nx:y",
        ] {
            assert!(Endpoint::parse(raw).is_err(), "{raw}");
        }
        let endpoint = Endpoint::parse("https://EXAMPLE.com.:443?x=1").unwrap();
        assert_eq!(endpoint.host(), "example.com");
        assert_eq!(endpoint.target, "/?x=1");
    }
}
