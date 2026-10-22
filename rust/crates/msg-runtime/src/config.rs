//! Read the installed msgd configuration and public trust. Never initialize keys.
use msg_core::{unb64, Error, Json, Result};
use msg_crypto::{age_recipient, Ed25519Signer};
use msg_identity::{
    browser,
    models::{decode, Certificate},
};
use msg_worker::mail::MailConfig;
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use std::{
    collections::BTreeSet,
    fs::{self, File, OpenOptions},
    io::Read,
    path::{Component, Path, PathBuf},
};
use url::Url;
use zeroize::Zeroizing;

pub const BUILTINS: &[&str] = &[
    "identity",
    "content",
    "discussion",
    "communication",
    "discovery",
    "achievements",
    "recovery",
    "sharing",
    "money",
    "offers",
    "store",
    "bounty",
    "orders",
    "delivery",
    "transfer",
    "extensions",
    "system",
    "batch",
];

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RecoveryCustodian {
    pub id: String,
    pub name: String,
    pub recipient: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub policy_ref: Option<String>,
    #[serde(skip_deserializing)]
    pub fingerprint: String,
}
pub struct Settings {
    pub config_dir: PathBuf,
    pub service_url: String,
    pub service_aliases: Vec<String>,
    pub listen: String,
    pub port: u16,
    pub postgres_dsn: String,
    pub valkey_url: Option<String>,
    pub content_dir: PathBuf,
    pub repositories_dir: PathBuf,
    pub blob_dir: PathBuf,
    pub staging_dir: PathBuf,
    pub service_keys_dir: PathBuf,
    pub root_private_dir: PathBuf,
    pub recovery_marker: PathBuf,
    pub plugins: Vec<String>,
    pub max_request_bytes: usize,
    pub max_response_bytes: usize,
    pub max_path_bytes: usize,
    pub max_part_bytes: usize,
    pub temporary_ttl: u64,
    pub transfer_ttl: u64,
    pub base_certificate_ttl: u64,
    pub credential_delivery_recovery_window: u64,
    pub handle_rename_enabled: bool,
    pub browser: browser::Config,
    pub mail: Option<MailConfig>,
    pub websub_hubs: Vec<String>,
    pub recovery_custodians: Vec<RecoveryCustodian>,
    pub public_web_origin: Option<String>,
    pub hosting_base_capacity_bytes: u64,
    pub hosting_content_group_read: bool,
    pub hosting_recovery_marker: Option<PathBuf>,
    pub money_display_name: String,
    pub money_code: String,
    pub tool_timeout_ms: u64,
    pub tool_max_response_bytes: usize,
    pub tool_methods: Vec<String>,
    pub tool_ports: Vec<u16>,
    pub token_secret: Zeroizing<[u8; 32]>,
    pub online_signer: Ed25519Signer,
    pub receipt_signer: Ed25519Signer,
    pub root_certificate: Certificate,
    pub root_public_key: [u8; 32],
}

struct Section(toml::Table);
impl Section {
    fn get<T: DeserializeOwned>(&self, key: &str, default: T) -> Result<T> {
        self.0
            .get(key)
            .map(|v| {
                v.clone()
                    .try_into()
                    .map_err(|_| Error("invalid_configuration"))
            })
            .unwrap_or(Ok(default))
    }
    fn optional<T: DeserializeOwned>(&self, key: &str) -> Result<Option<T>> {
        self.0
            .get(key)
            .map(|v| {
                v.clone()
                    .try_into()
                    .map_err(|_| Error("invalid_configuration"))
            })
            .transpose()
    }
    fn required<T: DeserializeOwned>(&self, key: &str, code: &'static str) -> Result<T> {
        self.optional(key)?.ok_or(Error(code))
    }
}
fn section(root: &toml::Table, name: &str, keys: &[&str], error: &'static str) -> Result<Section> {
    let value = match root.get(name) {
        Some(toml::Value::Table(table)) => table.clone(),
        None => toml::Table::new(),
        _ => return Err(Error(error)),
    };
    require(value.keys().all(|k| keys.contains(&k.as_str())), error)?;
    Ok(Section(value))
}
fn require(value: bool, code: &'static str) -> Result<()> {
    if value {
        Ok(())
    } else {
        Err(Error(code))
    }
}
fn exists(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok()
}
fn read(path: &Path, maximum: u64, private: bool) -> Result<Zeroizing<Vec<u8>>> {
    #[cfg(unix)]
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    options.custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK);
    let mut file: File = options
        .open(path)
        .map_err(|_| Error("configuration_missing"))?;
    let meta = file
        .metadata()
        .map_err(|_| Error("configuration_missing"))?;
    require(
        meta.is_file() && meta.len() <= maximum,
        "unsafe_configuration_file",
    )?;
    #[cfg(unix)]
    if private {
        require(
            meta.mode() & 0o007 == 0 && meta.mode() & 0o022 == 0,
            "unsafe_service_key",
        )?;
    }
    let mut data = Zeroizing::new(Vec::new());
    file.by_ref()
        .take(maximum + 1)
        .read_to_end(&mut data)
        .map_err(|_| Error("configuration_missing"))?;
    require(data.len() as u64 <= maximum, "unsafe_configuration_file")?;
    Ok(data)
}
fn key(directory: &Path, name: &str) -> Result<Zeroizing<[u8; 32]>> {
    let value = read(&directory.join(name), 32, true)?;
    Ok(Zeroizing::new(
        value
            .as_slice()
            .try_into()
            .map_err(|_| Error("invalid_service_key"))?,
    ))
}
fn path(value: PathBuf) -> Result<PathBuf> {
    require(
        value.is_absolute()
            && !value.components().any(|c| c == Component::ParentDir)
            && !value.to_string_lossy().chars().any(char::is_control),
        "storage_paths_must_be_absolute",
    )?;
    Ok(value)
}
fn absolute(value: &Path) -> Result<PathBuf> {
    path(if value.is_absolute() {
        value.to_path_buf()
    } else {
        std::env::current_dir()
            .map_err(|_| Error("configuration_missing"))?
            .join(value)
    })
}
pub fn service_origin(value: &str) -> Result<String> {
    require(
        !value.is_empty()
            && !value
                .chars()
                .any(|c| c <= ' ' || c == '\u{7f}' || c == '\\'),
        "invalid_server_url",
    )?;
    let mut url =
        Url::parse(value.trim_end_matches('/')).map_err(|_| Error("invalid_server_url"))?;
    require(
        matches!(url.scheme(), "https" | "http")
            && url.host_str().is_some()
            && url.username().is_empty()
            && url.password().is_none()
            && (url.path().is_empty() || url.path() == "/")
            && url.query().is_none()
            && url.fragment().is_none()
            && url.port() != Some(0),
        "invalid_server_url",
    )?;
    if let Some(url::Host::Domain(domain)) = url.host() {
        let name = domain.trim_end_matches('.').to_owned();
        require(
            name.len() <= 253
                && name.split('.').all(|part| {
                    !part.is_empty()
                        && part.len() <= 63
                        && part.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
                        && !part.starts_with('-')
                        && !part.ends_with('-')
                }),
            "invalid_server_url",
        )?;
        url.set_host(Some(&name))
            .map_err(|_| Error("invalid_server_url"))?;
    }
    Ok(url.origin().ascii_serialization())
}
fn protected_root(directory: &Path) -> Result<PathBuf> {
    if directory == Path::new("/etc/msgd") {
        return Ok(PathBuf::from("/var/lib/msgd-root"));
    }
    let name = directory
        .file_name()
        .and_then(|v| v.to_str())
        .ok_or(Error("invalid_configuration"))?;
    let parent = directory.parent().ok_or(Error("invalid_configuration"))?;
    let legacy = parent.join(format!("{name}-root"));
    if parent != Path::new("/etc/msgd") {
        return Ok(legacy);
    }
    let current = PathBuf::from("/var/lib/private/msgd")
        .join(name)
        .join("root");
    let has_state = |p: &Path| {
        [
            "key.json",
            "initialization.pending",
            "rotation.pending.json",
            "history",
            ".rotation.lock",
        ]
        .iter()
        .any(|n| exists(&p.join(n)))
    };
    let old = has_state(&legacy);
    let new = has_state(&current);
    require(!(old && new), "ambiguous_root_private_directory")?;
    if old {
        return Ok(legacy);
    }
    validate_instance(name)?;
    Ok(current)
}
pub fn validate_instance(name: &str) -> Result<()> {
    require(
        !name.is_empty()
            && name.len() <= 26
            && name.as_bytes()[0].is_ascii_lowercase()
            && name
                .bytes()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || b"_-".contains(&c)),
        "invalid_instance_name",
    )
}

impl Settings {
    pub fn load(directory: &Path) -> Result<Self> {
        let directory = absolute(directory)?;
        let plugins_dir = directory.join("plugins.d");
        if let Ok(meta) = fs::symlink_metadata(&plugins_dir) {
            require(
                meta.is_dir() && !meta.file_type().is_symlink(),
                "unsafe_plugins_directory",
            )?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                require(
                    meta.permissions().mode() & 0o022 == 0,
                    "unsafe_plugins_directory",
                )?;
            }
        }
        let current = directory.join("msgd.toml");
        let legacy = directory.join("server.toml");
        require(
            !(exists(&current) && exists(&legacy)),
            "ambiguous_server_configuration",
        )?;
        let raw = read(
            if exists(&legacy) { &legacy } else { &current },
            1_048_576,
            false,
        )?;
        let root: toml::Table =
            toml::from_str(std::str::from_utf8(&raw).map_err(|_| Error("invalid_configuration"))?)
                .map_err(|_| Error("invalid_configuration"))?;
        require(
            root.keys().all(|k| {
                [
                    "server", "storage", "limits", "identity", "tools", "money", "hosting",
                    "recovery", "plugins", "websub", "browser",
                ]
                .contains(&k.as_str())
            }),
            "unknown_configuration_section",
        )?;
        let server = section(
            &root,
            "server",
            &[
                "service_url",
                "service_aliases",
                "listen",
                "port",
                "public_web_origin",
                "temporary_ttl",
                "transfer_ttl",
            ],
            "unknown_server_configuration",
        )?;
        let storage = section(
            &root,
            "storage",
            &[
                "postgres_dsn",
                "valkey_url",
                "content",
                "repositories",
                "blobs",
                "staging",
                "service_keys",
            ],
            "unknown_storage_configuration",
        )?;
        let limits = section(
            &root,
            "limits",
            &[
                "request_bytes",
                "response_bytes",
                "path_bytes",
                "part_bytes",
            ],
            "unknown_limit",
        )?;
        let identity = section(
            &root,
            "identity",
            &[
                "credential_delivery_recovery_window",
                "handle_rename_enabled",
            ],
            "unknown_identity_configuration",
        )?;
        let tools = section(
            &root,
            "tools",
            &[
                "isolation",
                "timeout_ms",
                "max_response_bytes",
                "methods",
                "ports",
            ],
            "unknown_tool_configuration",
        )?;
        let money = section(
            &root,
            "money",
            &[
                "enabled",
                "currency_id",
                "display_name",
                "code",
                "scale",
                "transfer_fee",
                "allow_overdraft",
            ],
            "unknown_money_configuration",
        )?;
        let hosting = section(
            &root,
            "hosting",
            &[
                "base_capacity_bytes",
                "content_group_read",
                "recovery_marker",
            ],
            "unknown_hosting_configuration",
        )?;
        let recovery = section(
            &root,
            "recovery",
            &["custodians"],
            "unknown_recovery_configuration",
        )?;
        let plugins = section(
            &root,
            "plugins",
            &["enabled"],
            "unknown_plugin_configuration",
        )?;
        let websub = section(&root, "websub", &["hubs"], "unknown_websub_configuration")?;
        let browser = section(
            &root,
            "browser",
            &["enabled", "session_ttl"],
            "invalid_browser_config",
        )?;
        let service_url: String = server.required("service_url", "invalid_service_url")?;
        let canonical = service_origin(&service_url).map_err(|_| Error("invalid_service_url"))?;
        // Target service strings retain the installed spelling, as Python does.
        let service_url = service_url.trim_end_matches('/').to_owned();
        let aliases: Vec<String> = server.get("service_aliases", Vec::new())?;
        require(aliases.len() <= 32, "invalid_service_aliases")?;
        let service_aliases = aliases
            .iter()
            .map(|s| service_origin(s))
            .collect::<Result<Vec<_>>>()?;
        let unique: BTreeSet<_> = service_aliases.iter().collect();
        require(
            unique.len() == service_aliases.len()
                && !unique.contains(&canonical)
                && service_aliases
                    .iter()
                    .all(|s| s.split(':').next() == service_url.split(':').next()),
            "invalid_service_aliases",
        )?;
        let listen: String = server.get("listen", "127.0.0.1".into())?;
        require(!listen.is_empty(), "invalid_listen_address")?;
        let port: u16 = server.get("port", 8042)?;
        require(port > 0, "invalid_listen_port")?;
        let temporary_ttl = server.get("temporary_ttl", 3600_u64)?;
        let transfer_ttl = server.get("transfer_ttl", 86400_u64)?;
        require(temporary_ttl > 0 && transfer_ttl > 0, "invalid_ttl")?;
        let postgres_dsn: String = storage.required("postgres_dsn", "invalid_postgres_dsn")?;
        require(
            !postgres_dsn.trim().is_empty() && !postgres_dsn.chars().any(char::is_control),
            "invalid_postgres_dsn",
        )?;
        if postgres_dsn.starts_with("service=") {
            require(
                postgres_dsn.len() > 8 && !postgres_dsn.chars().any(char::is_whitespace),
                "invalid_postgres_dsn",
            )?;
        } else {
            let dsn = Url::parse(&postgres_dsn).map_err(|_| Error("invalid_postgres_dsn"))?;
            require(
                matches!(dsn.scheme(), "postgres" | "postgresql")
                    && dsn.host_str().is_some()
                    && dsn.fragment().is_none(),
                "invalid_postgres_dsn",
            )?;
        }
        let valkey_url: Option<String> = storage.optional("valkey_url")?;
        if let Some(value) = &valkey_url {
            let value = Url::parse(value).map_err(|_| Error("invalid_valkey_url"))?;
            require(
                matches!(value.scheme(), "redis" | "rediss" | "unix")
                    && value.fragment().is_none()
                    && if value.scheme() == "unix" {
                        !value.path().is_empty()
                    } else {
                        value.host_str().is_some()
                    },
                "invalid_valkey_url",
            )?;
        }
        let content_dir =
            path(storage.get("content", PathBuf::from("/var/lib/msgd/git/content"))?)?;
        let parent = content_dir
            .parent()
            .ok_or(Error("storage_paths_must_be_absolute"))?;
        let modern = content_dir.file_name().is_some_and(|n| n == "content")
            && parent.file_name().is_some_and(|n| n == "git");
        let data_dir = if modern {
            parent
                .parent()
                .ok_or(Error("storage_paths_must_be_absolute"))?
        } else {
            parent
        };
        let repositories_dir = path(storage.get(
            "repositories",
            parent.join(if modern { "repos" } else { "repositories" }),
        )?)?;
        let blob_dir = path(storage.get(
            "blobs",
            if modern {
                data_dir.join("blobs/sha256")
            } else {
                content_dir.join("binary")
            },
        )?)?;
        let service_keys_dir = path(storage.get(
            "service_keys",
            if !modern && exists(&directory.join("service")) {
                directory.join("service")
            } else {
                data_dir.join("service")
            },
        )?)?;
        let staging_dir = path(storage.get("staging", data_dir.join("transfers/staging"))?)?;
        let limit = |name, default| -> Result<usize> {
            let value = limits.get(name, default)?;
            require(value >= 256, "invalid_limit")?;
            Ok(value)
        };
        let max_request_bytes = limit("request_bytes", 1_048_576)?;
        let max_response_bytes = limit("response_bytes", 1_048_576)?;
        let max_path_bytes = limit("path_bytes", 8192)?;
        let max_part_bytes = limit("part_bytes", 65536)?;
        let window: String = identity.get("credential_delivery_recovery_window", "15m".into())?;
        let minutes = window
            .strip_suffix('m')
            .and_then(|s| {
                if !s.starts_with('0') {
                    s.parse::<u64>().ok()
                } else {
                    None
                }
            })
            .ok_or(Error("invalid_credential_delivery_recovery_window"))?;
        require(
            window.len() <= 3 && (1..=60).contains(&minutes),
            "invalid_credential_delivery_recovery_window",
        )?;
        let enabled = browser.get("enabled", true)?;
        let session_ttl = browser.get("session_ttl", 3600_u32)?;
        require(
            (60..=86400).contains(&session_ttl),
            "invalid_browser_config",
        )?;
        let origin = Url::parse(&canonical).map_err(|_| Error("invalid_service_url"))?;
        require(
            !enabled
                || origin.scheme() == "https"
                || matches!(
                    origin.host_str(),
                    Some("localhost" | "127.0.0.1" | "[::1]" | "testserver")
                ),
            "secure_channel_required",
        )?;
        let public_web_origin: Option<String> = server.optional("public_web_origin")?;
        if let Some(value) = &public_web_origin {
            service_origin(value).map_err(|_| Error("invalid_hosting_origin"))?;
            require(
                value.trim_end_matches('/') == service_url,
                "hosting_origin_must_match_service",
            )?;
        }
        let hosting_base_capacity_bytes = hosting.get("base_capacity_bytes", 10_485_760_u64)?;
        require(
            (1..=1_099_511_627_776).contains(&hosting_base_capacity_bytes),
            "invalid_hosting_capacity",
        )?;
        let hosting_recovery_marker = hosting
            .optional::<PathBuf>("recovery_marker")?
            .map(path)
            .transpose()?;
        require(
            money.get("enabled", true)?
                && money.get("currency_id", "primary".to_owned())? == "primary"
                && money.get("scale", 6_i64)? == 6
                && money.get("transfer_fee", 0_i64)? == 0
                && !money.get("allow_overdraft", false)?,
            "invalid_money_configuration",
        )?;
        let money_display_name: String = money.get("display_name", "MSG".into())?;
        let money_code: String = money.get("code", "MSG".into())?;
        require(
            (1..=80).contains(&money_display_name.chars().count())
                && money_display_name.trim() == money_display_name
                && !money_display_name.chars().any(|c| c < ' '),
            "invalid_money_display_name",
        )?;
        require(
            (2..=16).contains(&money_code.len())
                && money_code.as_bytes()[0].is_ascii_uppercase()
                && money_code
                    .bytes()
                    .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit()),
            "invalid_money_code",
        )?;
        require(
            tools.get("isolation", "bubblewrap".to_owned())? == "bubblewrap",
            "unsafe_tool_worker",
        )?;
        let tool_timeout_ms = tools.get("timeout_ms", 10000_u64)?;
        let tool_max_response_bytes = tools.get("max_response_bytes", 4194304_usize)?;
        require(
            tool_timeout_ms > 0 && tool_max_response_bytes > 0,
            "invalid_tool_limit",
        )?;
        let tool_methods: Vec<String> = tools.get("methods", vec!["GET".into(), "HEAD".into()])?;
        require(
            tool_methods.iter().all(|s| {
                ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"].contains(&s.as_str())
            }),
            "invalid_tool_methods",
        )?;
        let tool_ports: Vec<u16> = tools.get("ports", vec![80, 443])?;
        require(tool_ports.iter().all(|p| *p > 0), "invalid_tool_ports")?;
        let plugins: Vec<String> =
            plugins.get("enabled", BUILTINS.iter().map(|s| (*s).into()).collect())?;
        require(
            plugins.iter().collect::<BTreeSet<_>>().len() == plugins.len()
                && plugins.iter().any(|s| s == "identity")
                && plugins.iter().all(|s| BUILTINS.contains(&s.as_str())),
            "invalid_plugin_list",
        )?;
        let websub_hubs: Vec<String> = websub.get("hubs", Vec::new())?;
        require(websub_hubs.len() <= 16, "invalid_websub_hubs")?;
        for value in &websub_hubs {
            msg_worker::outbound::Endpoint::parse(value)?;
        }
        let mut seen = BTreeSet::new();
        let websub_hubs = websub_hubs
            .into_iter()
            .filter(|s| seen.insert(s.clone()))
            .collect();
        let mut recovery_custodians: Vec<RecoveryCustodian> =
            recovery.get("custodians", Vec::new())?;
        require(
            recovery_custodians.len() <= 16,
            "invalid_recovery_custodians",
        )?;
        let mut ids = BTreeSet::new();
        for c in &mut recovery_custodians {
            require(
                (2..=64).contains(&c.id.len())
                    && c.id.as_bytes()[0].is_ascii_lowercase()
                    && c.id
                        .bytes()
                        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
                    && (1..=100).contains(&c.name.chars().count())
                    && [&c.description, &c.policy_ref]
                        .iter()
                        .all(|s| s.as_ref().is_none_or(|s| s.chars().count() <= 500)),
                "invalid_recovery_custodian",
            )?;
            require(ids.insert(c.id.clone()), "duplicate_recovery_custodian")?;
            c.fingerprint = age_recipient::encryption_key_id(
                &age_recipient::public_from_recipient(&c.recipient)?,
            );
        }
        let mail_file = directory.join("mail.toml");
        let mail = if exists(&mail_file) {
            let bytes = read(&mail_file, 65536, false)?;
            let table: toml::Table = toml::from_str(
                std::str::from_utf8(&bytes).map_err(|_| Error("invalid_mail_configuration"))?,
            )
            .map_err(|_| Error("invalid_mail_configuration"))?;
            require(
                table.keys().all(|s| {
                    [
                        "enabled",
                        "host",
                        "port",
                        "sender",
                        "tls",
                        "credential_file",
                    ]
                    .contains(&s.as_str())
                }),
                "unknown_mail_configuration",
            )?;
            let section = Section(table);
            if !section.get("enabled", false)? {
                None
            } else {
                let host: String = section.required("host", "invalid_mail_host")?;
                let sender: String = section.required("sender", "invalid_mail_sender")?;
                let tls: String = section.required("tls", "mail_tls_required")?;
                let port = section.get("port", 587_u16)?;
                require(
                    !host.is_empty() && !host.chars().any(|c| c <= ' '),
                    "invalid_mail_host",
                )?;
                require(
                    !sender.trim().is_empty() && !sender.chars().any(|c| c < ' '),
                    "invalid_mail_sender",
                )?;
                require(
                    matches!(tls.as_str(), "tls" | "starttls") && port > 0,
                    "mail_tls_required",
                )?;
                Some(MailConfig {
                    enabled: true,
                    host,
                    port,
                    tls,
                    sender,
                    credential_file: section
                        .optional::<PathBuf>("credential_file")?
                        .map(path)
                        .transpose()?,
                })
            }
        } else {
            None
        };
        let current = data_dir.join("recovery-drill.json");
        let legacy = directory.join("recovery-drill.json");
        require(
            !(exists(&current) && exists(&legacy)),
            "ambiguous_recovery_marker",
        )?;
        let recovery_marker = if exists(&legacy) { legacy } else { current };
        let root_private_dir = protected_root(&directory)?;
        let trust_dir = directory.join("trust");
        require(
            !fs::symlink_metadata(&trust_dir)
                .map_err(|_| Error("root_trust_missing"))?
                .file_type()
                .is_symlink(),
            "unsafe_root_trust_path",
        )?;
        let mut trust: Option<Json> = None;
        for name in ["root.crt", "root.json"] {
            let file = trust_dir.join(name);
            if !exists(&file) {
                continue;
            }
            let bytes = read(&file, 1_048_576, false)?;
            let value =
                Json::parse(std::str::from_utf8(&bytes).map_err(|_| Error("invalid_root_trust"))?)?;
            if let Some(previous) = &trust {
                require(
                    previous.canonical()? == value.canonical()?,
                    "root_trust_alias_mismatch",
                )?;
            }
            trust = Some(value);
        }
        let trust = trust.ok_or(Error("root_trust_missing"))?;
        let object = trust.as_object()?;
        require(
            object.len() == 3
                && object.get("version").and_then(|v| v.as_integer().ok()) == Some("1"),
            "invalid_root_trust",
        )?;
        let root_public_key = unb64(
            object
                .get("public_key")
                .ok_or(Error("invalid_root_trust"))?
                .as_str()?,
            32,
        )?
        .try_into()
        .map_err(|_| Error("invalid_root_trust"))?;
        let root_certificate = decode(
            object
                .get("certificate")
                .ok_or(Error("invalid_root_trust"))?,
        )?;
        let token_secret = key(&service_keys_dir, "tokens.key")?;
        let online_signer = Ed25519Signer::from_seed(&*key(&service_keys_dir, "online.key")?);
        let receipt_signer = Ed25519Signer::from_seed(&*key(&service_keys_dir, "receipt.key")?);
        Ok(Self {
            config_dir: directory,
            service_url,
            service_aliases,
            listen,
            port,
            postgres_dsn,
            valkey_url,
            content_dir,
            repositories_dir,
            blob_dir,
            staging_dir,
            service_keys_dir,
            root_private_dir,
            recovery_marker,
            plugins,
            max_request_bytes,
            max_response_bytes,
            max_path_bytes,
            max_part_bytes,
            temporary_ttl,
            transfer_ttl,
            base_certificate_ttl: 2592000,
            credential_delivery_recovery_window: minutes * 60,
            handle_rename_enabled: identity.get("handle_rename_enabled", true)?,
            browser: browser::Config {
                enabled,
                session_ttl,
            },
            mail,
            websub_hubs,
            recovery_custodians,
            public_web_origin,
            hosting_base_capacity_bytes,
            hosting_content_group_read: hosting.get("content_group_read", false)?,
            hosting_recovery_marker,
            money_display_name,
            money_code,
            tool_timeout_ms,
            tool_max_response_bytes,
            tool_methods,
            tool_ports,
            token_secret,
            online_signer,
            receipt_signer,
            root_certificate,
            root_public_key,
        })
    }
    pub fn quarantined(&self) -> bool {
        exists(&self.recovery_marker)
    }
    pub fn require_network_runtime(&self) -> Result<()> {
        require(
            !rustix::process::geteuid().is_root(),
            "network_service_must_not_run_as_root",
        )?;
        for directory in [&self.root_private_dir, &self.config_dir.join("root")] {
            use rustix::fs::{access, Access};
            require(
                access(directory, Access::READ_OK).is_err()
                    && access(directory, Access::EXEC_OK).is_err(),
                "root_material_accessible_to_service",
            )?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn origins_and_instances_cannot_escape_their_boundaries() {
        assert_eq!(
            service_origin("HTTPS://Example.COM:443/").unwrap(),
            "https://example.com"
        );
        assert_eq!(
            service_origin("http://[::1]:8042").unwrap(),
            "http://[::1]:8042"
        );
        for s in [
            "https://x@host",
            "https://host/a",
            "https://host?x",
            "https://host#x",
            "https://host\\x",
            " https://host",
            "https://host:0",
        ] {
            assert!(service_origin(s).is_err(), "{s}");
        }
        for name in ["..", "/root", "a/b", "UPPER", "a b"] {
            assert!(validate_instance(name).is_err());
        }
        assert!(validate_instance("my-server_2").is_ok());
    }
}
