//! TLS-only SMTP with an explicit uncertainty boundary. Any error after MAIL
//! starts is uncertain, and a failure closing an acknowledged DATA is ignored.
use crate::{
    outbound::{connect, remaining, resolve},
    EffectFailure, EffectOutcome, EffectResult,
};
use base64::{engine::general_purpose::STANDARD, Engine};
use msg_core::{Error, Result};
use msg_storage::jobs::status;
use serde::Deserialize;
use std::{
    collections::BTreeSet,
    fs::{File, OpenOptions},
    io::{Read, Write},
    net::TcpStream,
    path::PathBuf,
    time::{Duration, Instant},
};
use zeroize::{Zeroize, Zeroizing};

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MailConfig {
    pub enabled: bool,
    pub host: String,
    pub port: u16,
    pub tls: String,
    pub sender: String,
    pub credential_file: Option<PathBuf>,
}
pub struct MailMessage {
    pub recipient: String,
    pub subject: String,
    pub text: String,
    pub delivery_id: String,
}
pub struct SmtpSender {
    config: Option<MailConfig>,
}
impl SmtpSender {
    pub fn new(config: Option<MailConfig>) -> Self {
        Self { config }
    }
    pub fn enabled(&self) -> bool {
        self.config.as_ref().is_some_and(|c| c.enabled)
    }
    pub fn send(&self, message: &MailMessage) -> EffectResult<EffectOutcome> {
        let config = self
            .config
            .as_ref()
            .filter(|c| c.enabled)
            .ok_or(Error("mail_disabled"))?;
        // All header and recipient validation precedes connecting/secret reads.
        validate_address(&message.recipient)?;
        let (sender, from) = sender(&config.sender)?;
        let raw = render(message, &from)?;
        if !matches!(config.tls.as_str(), "tls" | "starttls")
            || config.host.is_empty()
            || config.host.chars().any(char::is_whitespace)
            || config.port == 0
        {
            return Err(Error("mail_tls_required").into());
        }
        let deadline = Instant::now() + Duration::from_secs(20);
        let mut sending = false;
        let result = (|| {
            let tls =
                native_tls::TlsConnector::new().map_err(|_| Error("mail_connection_failed"))?;
            let addresses = resolve(&config.host, config.port, deadline)?;
            let socket = connect(&addresses, deadline)?;
            let stream = if config.tls == "tls" {
                MailStream::Tls(
                    tls.connect(&config.host, socket)
                        .map_err(|_| Error("mail_connection_failed"))?,
                )
            } else {
                MailStream::Plain(socket)
            };
            let mut connection = Connection { stream, deadline };
            expect(connection.reply()?.0, 220)?;
            let mut capabilities = connection.ehlo()?;
            if config.tls == "starttls" {
                if !capabilities.contains("STARTTLS") {
                    return Err(Error("mail_connection_failed"));
                }
                expect(connection.command("STARTTLS")?.0, 220)?;
                connection.stream = match connection.stream {
                    MailStream::Plain(socket) => MailStream::Tls(
                        tls.connect(&config.host, socket)
                            .map_err(|_| Error("mail_connection_failed"))?,
                    ),
                    MailStream::Tls(_) => return Err(Error("mail_connection_failed")),
                };
                capabilities = connection.ehlo()?;
            }
            if let Some(path) = &config.credential_file {
                let credentials = credentials(path)?;
                if capabilities.contains("PLAIN") {
                    let mut bytes = Zeroizing::new(Vec::new());
                    bytes.push(0);
                    bytes.extend_from_slice(credentials.username.as_bytes());
                    bytes.push(0);
                    bytes.extend_from_slice(credentials.password.as_bytes());
                    let encoded = Zeroizing::new(STANDARD.encode(&*bytes));
                    let command = Zeroizing::new(format!("AUTH PLAIN {}", encoded.as_str()));
                    let mut response = connection.command(&command)?;
                    if response.0 == 334 {
                        response = connection.command(&encoded)?;
                    }
                    expect(response.0, 235)?;
                } else if capabilities.contains("LOGIN") {
                    expect(connection.command("AUTH LOGIN")?.0, 334)?;
                    let username = Zeroizing::new(STANDARD.encode(credentials.username.as_bytes()));
                    let password = Zeroizing::new(STANDARD.encode(credentials.password.as_bytes()));
                    expect(connection.command(&username)?.0, 334)?;
                    expect(connection.command(&password)?.0, 235)?;
                } else {
                    return Err(Error("mail_connection_failed"));
                }
            }
            let utf8 = !sender.is_ascii() || !message.recipient.is_ascii();
            if utf8 && !capabilities.contains("SMTPUTF8") {
                return Err(Error("mail_connection_failed"));
            }
            // This intentionally starts before MAIL, like smtplib.send_message:
            // a later failure is never automatically retried as an unsent mail.
            sending = true;
            let options = if utf8 { " SMTPUTF8 BODY=8BITMIME" } else { "" };
            expect(
                connection
                    .command(&format!("MAIL FROM:<{sender}>{options}"))?
                    .0,
                250,
            )?;
            let rcpt = connection
                .command(&format!("RCPT TO:<{}>", message.recipient))?
                .0;
            if !matches!(rcpt, 250 | 251) {
                return Err(Error("mail_recipient_refused"));
            }
            expect(connection.command("DATA")?.0, 354)?;
            connection.set_timeout()?;
            connection
                .stream
                .write_all(&raw)
                .and_then(|_| connection.stream.write_all(b".\r\n"))
                .and_then(|_| connection.stream.flush())
                .map_err(|_| Error("mail_connection_failed"))?;
            expect(connection.reply()?.0, 250)?;
            // Drop closes locally; QUIT cannot turn accepted DATA into a retry.
            Ok(())
        })();
        match result {
            Ok(()) => Ok(EffectOutcome::Done(status("sent"))),
            Err(_) if sending => Ok(EffectOutcome::Uncertain("external_uncertain")),
            Err(_) => Err(EffectFailure {
                error: Error("mail_connection_failed"),
                retryable: true,
            }),
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Credentials {
    username: String,
    password: String,
}
impl Drop for Credentials {
    fn drop(&mut self) {
        self.username.zeroize();
        self.password.zeroize();
    }
}
fn credentials(path: &std::path::Path) -> Result<Credentials> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let file = options
        .open(path)
        .map_err(|_| Error("invalid_smtp_credentials"))?;
    let metadata = file
        .metadata()
        .map_err(|_| Error("invalid_smtp_credentials"))?;
    if !metadata.is_file() || metadata.len() > 65_536 {
        return Err(Error("invalid_smtp_credentials"));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o077 != 0 {
            return Err(Error("invalid_smtp_credentials"));
        }
    }
    read_credentials(file)
}
fn read_credentials(file: File) -> Result<Credentials> {
    let mut raw = Zeroizing::new(Vec::new());
    file.take(65_537)
        .read_to_end(&mut raw)
        .map_err(|_| Error("invalid_smtp_credentials"))?;
    if raw.len() > 65_536 {
        return Err(Error("invalid_smtp_credentials"));
    }
    let value: Credentials =
        serde_json::from_slice(&raw).map_err(|_| Error("invalid_smtp_credentials"))?;
    if value.username.contains('\0') || value.password.contains('\0') {
        return Err(Error("invalid_smtp_credentials"));
    }
    Ok(value)
}

enum MailStream {
    Plain(TcpStream),
    Tls(native_tls::TlsStream<TcpStream>),
}
impl Read for MailStream {
    fn read(&mut self, bytes: &mut [u8]) -> std::io::Result<usize> {
        match self {
            Self::Plain(v) => v.read(bytes),
            Self::Tls(v) => v.read(bytes),
        }
    }
}
impl Write for MailStream {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        match self {
            Self::Plain(v) => v.write(bytes),
            Self::Tls(v) => v.write(bytes),
        }
    }
    fn flush(&mut self) -> std::io::Result<()> {
        match self {
            Self::Plain(v) => v.flush(),
            Self::Tls(v) => v.flush(),
        }
    }
}
struct Connection {
    stream: MailStream,
    deadline: Instant,
}
impl Connection {
    fn set_timeout(&self) -> Result<()> {
        let socket = match &self.stream {
            MailStream::Plain(v) => v,
            MailStream::Tls(v) => v.get_ref(),
        };
        socket
            .set_read_timeout(Some(remaining(self.deadline)?))
            .and_then(|_| {
                socket.set_write_timeout(Some(
                    remaining(self.deadline).map_err(std::io::Error::other)?,
                ))
            })
            .map_err(|_| Error("mail_connection_failed"))
    }
    fn command(&mut self, value: &str) -> Result<(u16, Vec<String>)> {
        if value.len() > 65_536 || value.contains(['\r', '\n']) {
            return Err(Error("mail_connection_failed"));
        }
        self.set_timeout()?;
        self.stream
            .write_all(value.as_bytes())
            .and_then(|_| self.stream.write_all(b"\r\n"))
            .and_then(|_| self.stream.flush())
            .map_err(|_| Error("mail_connection_failed"))?;
        self.reply()
    }
    fn reply(&mut self) -> Result<(u16, Vec<String>)> {
        let mut lines = Vec::new();
        let mut code = None;
        let mut total = 0;
        loop {
            let mut line = Vec::new();
            while !line.ends_with(b"\r\n") {
                if line.len() >= 4096 || total >= 65_536 {
                    return Err(Error("mail_connection_failed"));
                }
                self.set_timeout()?;
                let mut byte = [0];
                self.stream
                    .read_exact(&mut byte)
                    .map_err(|_| Error("mail_connection_failed"))?;
                line.push(byte[0]);
                total += 1;
            }
            if line.len() < 6
                || !line[..3].iter().all(u8::is_ascii_digit)
                || !matches!(line[3], b' ' | b'-')
            {
                return Err(Error("mail_connection_failed"));
            }
            let current = ((line[0] - b'0') as u16) * 100
                + ((line[1] - b'0') as u16) * 10
                + (line[2] - b'0') as u16;
            if code.is_some_and(|c| c != current) {
                return Err(Error("mail_connection_failed"));
            }
            code = Some(current);
            lines.push(String::from_utf8_lossy(&line[4..line.len() - 2]).into_owned());
            if line[3] == b' ' {
                return Ok((current, lines));
            }
        }
    }
    fn ehlo(&mut self) -> Result<BTreeSet<String>> {
        let (code, lines) = self.command("EHLO msgd.local")?;
        expect(code, 250)?;
        Ok(lines
            .iter()
            .flat_map(|line| line.split_ascii_whitespace())
            .map(str::to_ascii_uppercase)
            .collect())
    }
}
fn expect(actual: u16, expected: u16) -> Result<()> {
    if actual == expected {
        Ok(())
    } else {
        Err(Error("mail_connection_failed"))
    }
}

pub fn validate_address(address: &str) -> Result<()> {
    if address.is_empty()
        || address.len() > 254
        || address.chars().any(|c| c.is_whitespace() || c.is_control())
    {
        return Err(Error("invalid_email"));
    }
    let (local, domain) = address.rsplit_once('@').ok_or(Error("invalid_email"))?;
    if local.is_empty()
        || domain.is_empty()
        || !domain.contains('.')
        || domain.contains('@')
        || domain.starts_with('.')
        || domain.ends_with('.')
    {
        return Err(Error("invalid_email"));
    }
    let atom = |s: &str| {
        !s.is_empty()
            && s.chars().all(|c| {
                !c.is_control()
                    && !c.is_whitespace()
                    && (!c.is_ascii()
                        || c.is_ascii_alphanumeric()
                        || "!#$%&'*+-/=?^_`{|}~".contains(c))
            })
    };
    let local_ok = local.split('.').all(atom);
    // Quoted local parts are kept byte-for-byte; only the minimal form accepted
    // by the existing exact addr-spec contract is accepted here.
    let quoted = local
        .strip_prefix('"')
        .and_then(|s| s.strip_suffix('"'))
        .is_some_and(|s| {
            !s.is_empty()
                && !s.contains(['"', '\\', '@'])
                && s.chars()
                    .any(|c| !c.is_ascii_alphanumeric() && !".!#$%&'*+-/=?^_`{|}~".contains(c))
        });
    if !(local_ok || quoted)
        || !domain.split('.').all(|part| {
            !part.is_empty()
                && part.chars().all(|c| c.is_alphanumeric() || c == '-')
                && !part.starts_with('-')
                && !part.ends_with('-')
        })
    {
        return Err(Error("invalid_email"));
    }
    Ok(())
}
fn sender(value: &str) -> Result<(String, String)> {
    if value.len() > 1024 || value.chars().any(char::is_control) {
        return Err(Error("invalid_mail_sender"));
    }
    if let Some((name, rest)) = value.rsplit_once('<') {
        let address = rest.strip_suffix('>').ok_or(Error("invalid_mail_sender"))?;
        validate_address(address)?;
        if name.contains(['<', '>']) {
            return Err(Error("invalid_mail_sender"));
        }
        let name = name.trim();
        Ok((
            address.into(),
            format!("{} <{address}>", encoded_header(name)),
        ))
    } else {
        validate_address(value)?;
        Ok((value.into(), value.into()))
    }
}
fn encoded_header(value: &str) -> String {
    if value.is_ascii() && value.len() <= 72 {
        return value.into();
    }
    let mut words = Vec::new();
    let mut chunk = String::new();
    for c in value.chars() {
        if chunk.len() + c.len_utf8() > 42 {
            words.push(format!("=?UTF-8?B?{}?=", STANDARD.encode(chunk.as_bytes())));
            chunk.clear();
        }
        chunk.push(c);
    }
    if !chunk.is_empty() {
        words.push(format!("=?UTF-8?B?{}?=", STANDARD.encode(chunk.as_bytes())));
    }
    words.join("\r\n ")
}
fn render(message: &MailMessage, sender: &str) -> Result<Vec<u8>> {
    if message.subject.len() > 8192
        || message.subject.chars().any(char::is_control)
        || message.text.len() > msg_core::MAX_BYTES
        || message.delivery_id.is_empty()
        || message.delivery_id.len() > 160
        || !message
            .delivery_id
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-'))
    {
        return Err(Error("invalid_mail_message"));
    }
    let mut message_bytes = format!("From: {sender}\r\nTo: {}\r\nSubject: {}\r\nMessage-ID: <{}@msgd.local>\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n",message.recipient,encoded_header(&message.subject),message.delivery_id).into_bytes();
    let mut text = message.text.replace("\r\n", "\n").replace('\r', "\n");
    if !text.ends_with('\n') {
        text.push('\n');
    }
    let body = STANDARD.encode(text.replace('\n', "\r\n").as_bytes());
    for line in body.as_bytes().chunks(76) {
        message_bytes.extend_from_slice(line);
        message_bytes.extend_from_slice(b"\r\n");
    }
    Ok(message_bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn envelope_rejects_header_injection_and_multiple_targets() {
        for address in [
            "a@example.com\r\nBcc:b@example.com",
            "a@example.com,b@example.com",
            "Name<a@example.com>",
            "a@@example.com",
            "a..b@example.com",
            "a@example",
            "a@-example.com",
        ] {
            assert!(validate_address(address).is_err(), "{address}");
        }
        for address in ["a+b@example.com", "中文@example.com", "a.b@example.test"] {
            assert!(validate_address(address).is_ok(), "{address}");
        }
    }
    #[test]
    fn rendered_body_cannot_end_smtp_data_or_add_headers() {
        let message = MailMessage {
            recipient: "a@example.com".into(),
            subject: "中文主题".into(),
            text: "hello\r\n.\r\nBcc: attacker".into(),
            delivery_id: "job_stable".into(),
        };
        let raw = String::from_utf8(render(&message, "sender@example.com").unwrap()).unwrap();
        assert!(!raw.contains("\r\n.\r\n"));
        assert!(!raw.contains("Bcc: attacker"));
        assert!(raw.contains("Message-ID: <job_stable@msgd.local>"));
        assert!(raw.contains("=?UTF-8?B?"));
    }
    #[test]
    fn disabled_mail_never_connects_or_reads_credentials() {
        let sender = SmtpSender::new(None);
        let message = MailMessage {
            recipient: "invalid".into(),
            subject: String::new(),
            text: String::new(),
            delivery_id: String::new(),
        };
        assert!(matches!(
            sender.send(&message),
            Err(EffectFailure {
                error: Error("mail_disabled"),
                ..
            })
        ));
    }
}
