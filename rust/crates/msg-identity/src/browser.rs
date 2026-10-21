//! Signed local-key approval and digest-only browser sessions.
//!
//! Every method runs inside the caller's fenced metadata transaction. A failed
//! method must roll that transaction back. Cookies and returned token secrets
//! are delivery material: they must not enter receipts, audit bodies or logs.
use crate::{
    authentication::{OperationPolicy, Principal},
    models::*,
    policy::scope_subset,
    require,
    sessions::{require_binding, require_source, BrowserPolicy, CredentialState},
    store::{truthy, AuthorityStore},
};
use chrono::Duration;
use hmac::{Hmac, Mac};
use msg_core::{b64, Error, Json, Result};
use rand_core::{OsRng, RngCore};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use zeroize::Zeroizing;

/// Adapter implementations use the existing credential/state tables. They must
/// preserve storage's sticky failure, auth-change and writer-fence semantics.
pub trait BrowserStore: AuthorityStore {
    fn require_live(&mut self) -> Result<()>;
    fn save_credential(&mut self, credential: &Credential, auth_version: u64) -> Result<()>;
    fn put_state(&mut self, id: &str, kind: &str, expires: Timestamp, body: &Json) -> Result<()>;
    fn replace_state(&mut self, id: &str, body: &Json) -> Result<()>;
    fn expire_states(&mut self, now: Timestamp) -> Result<()>;
    fn state_count(&self) -> Result<u64>;
}

pub struct Config {
    pub enabled: bool,
    pub session_ttl: u32,
}

pub struct BrowserCredential {
    pub subject: String,
    pub credential_id: String,
    pub secret: Zeroizing<[u8; 32]>,
}

pub struct Service<'a> {
    config: Config,
    service: &'a str,
    token_secret: &'a [u8; 32],
    operations: &'a [OperationPolicy],
    custodial_ceiling: &'a [Grant],
}

const CODE_ALPHABET: &[u8] = b"ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const POST_WRITES: &[&str] = &[
    "content.post_create",
    "content.public_board_update",
    "content.post_edit",
    "discussion.like",
    "discussion.unlike",
    "discussion.bookmark",
    "discussion.unbookmark",
    "discussion.reply",
    "discussion.fork",
    "discussion.prove",
    "communication.follow",
    "communication.unfollow",
];

pub fn state_id(kind: &str, value: &str) -> String {
    format!("{kind}:{:x}", Sha256::digest(value.as_bytes()))
}

fn new_secret() -> Result<String> {
    let mut bytes = Zeroizing::new([0u8; 32]);
    OsRng
        .try_fill_bytes(bytes.as_mut())
        .map_err(|_| Error("randomness_unavailable"))?;
    Ok(b64(bytes.as_ref()))
}

fn get(store: &dyn AuthorityStore, id: &str, now: Timestamp) -> Result<CredentialState> {
    let state = store.credential_state(id)?.ok_or(Error("invalid_grant"))?;
    require(state.expires_at > now, "invalid_grant")?;
    Ok(state)
}

fn field<'a>(value: &'a Json, name: &str) -> Result<&'a Json> {
    value.as_object()?.get(name).ok_or(Error("invalid_grant"))
}

fn string<'a>(value: &'a Json, name: &str) -> Result<&'a str> {
    field(value, name)?
        .as_str()
        .map_err(|_| Error("invalid_grant"))
}

fn code_id(code: &str) -> Result<String> {
    let code = code.to_ascii_uppercase().replace('-', "");
    require(
        code.len() == 8 && code.bytes().all(|c| CODE_ALPHABET.contains(&c)),
        "invalid_grant",
    )?;
    Ok(state_id("user", &code))
}

fn update(body: Json, values: impl IntoIterator<Item = (&'static str, Json)>) -> Result<Json> {
    let mut fields = body.into_object()?;
    fields.extend(values.into_iter().map(|(k, v)| (k.to_owned(), v)));
    Ok(Json::from_object(fields))
}

impl<'a> Service<'a> {
    pub fn new(
        config: Config,
        service: &'a str,
        token_secret: &'a [u8; 32],
        operations: &'a [OperationPolicy],
        custodial_ceiling: &'a [Grant],
    ) -> Result<Self> {
        require(
            (60..=86400).contains(&config.session_ttl),
            "invalid_browser_config",
        )?;
        Ok(Self {
            config,
            service,
            token_secret,
            operations,
            custodial_ceiling,
        })
    }

    fn fence<S: BrowserStore>(&self, store: &mut S) -> Result<()> {
        require(self.config.enabled, "browser_sessions_disabled")?;
        store.require_live()
    }

    pub fn rate<S: BrowserStore>(
        &self,
        store: &mut S,
        address: &str,
        now: Timestamp,
    ) -> Result<()> {
        self.fence(store)?;
        store.expire_states(now)?;
        let id = state_id(
            "rate",
            if address.is_empty() {
                "unknown"
            } else {
                address
            },
        );
        let state = store.credential_state(&id)?;
        let count = state
            .as_ref()
            .map(|state| decode::<u64>(field(&state.body, "count")?))
            .transpose()?
            .unwrap_or(0);
        require(count < 120, "slow_down")?;
        require(store.state_count()? < 100000, "temporarily_unavailable")?;
        let expiry = state
            .map(|state| state.expires_at)
            .unwrap_or(Timestamp(now.0 + Duration::minutes(10)));
        store.put_state(
            &id,
            "rate",
            expiry,
            &wire(&serde_json::json!({"count": count + 1}))?,
        )
    }

    /// Returns a one-time polling secret and a human code, never storing either.
    pub fn pending<S: BrowserStore>(&self, store: &mut S, now: Timestamp) -> Result<Json> {
        self.fence(store)?;
        for _ in 0..8 {
            let mut bytes = [0u8; 8];
            OsRng
                .try_fill_bytes(&mut bytes)
                .map_err(|_| Error("randomness_unavailable"))?;
            let code: String = bytes
                .iter()
                .map(|b| CODE_ALPHABET[usize::from(b & 31)] as char)
                .collect();
            let id = state_id("user", &code);
            if store.credential_state(&id)?.is_some() {
                continue;
            }
            let value = new_secret()?;
            let expiry = Timestamp(now.0 + Duration::minutes(10));
            let poll = state_id("poll", &value);
            let body = wire(
                &serde_json::json!({"kind":"browser", "status":"pending", "poll":poll, "interval":5, "next_poll":now}),
            )?;
            store.put_state(&id, "pending", expiry, &body)?;
            store.put_state(
                &poll,
                "poll",
                expiry,
                &wire(&serde_json::json!({"user":id}))?,
            )?;
            return wire(&serde_json::json!({"value": value, "user_code":code, "expires":expiry}));
        }
        Err(Error("temporarily_unavailable"))
    }

    pub fn inspect<S: BrowserStore>(
        &self,
        store: &mut S,
        code: &str,
        now: Timestamp,
    ) -> Result<Json> {
        self.fence(store)?;
        let state = get(store, &code_id(code)?, now)?;
        require(string(&state.body, "kind")? == "browser", "invalid_grant")?;
        wire(
            &serde_json::json!({"kind":"browser", "status":string(&state.body,"status")?, "service":self.service, "purpose":"Approve only the browser sign-in that you started."}),
        )
    }

    fn source<S: BrowserStore>(
        &self,
        store: &S,
        principal: &Principal,
        now: Timestamp,
    ) -> Result<Json> {
        let id = principal
            .subject()
            .ok_or(Error("identity_owner_required"))?;
        require(principal.actor() == Some(id), "identity_owner_required")?;
        let subject = store.subject(id)?;
        require(
            subject.kind == SubjectKind::Registered && !subject.local_only,
            "registered_identity_required",
        )?;
        require(principal.method() == "signature", "signature_required")?;
        wire(
            &serde_json::json!({"subject":id, "parent":principal.credential_id().ok_or(Error("signature_required"))?, "auth_version":subject.auth_version, "custodial":false, "ceiling":principal.ceiling(), "auth_time":now.0.timestamp()}),
        )
    }

    pub fn approve<S: BrowserStore>(
        &self,
        store: &mut S,
        principal: &Principal,
        code: &str,
        decision: &str,
        now: Timestamp,
    ) -> Result<Json> {
        self.fence(store)?;
        require(matches!(decision, "approve" | "deny"), "invalid_request")?;
        let id = code_id(code)?;
        let state = get(store, &id, now)?;
        require(
            string(&state.body, "kind")? == "browser"
                && string(&state.body, "status")? == "pending",
            "invalid_grant",
        )?;
        let source = self.source(store, principal, now)?;
        let mut body = state.body.into_object()?;
        if decision == "approve" {
            body.extend(source.into_object()?);
        }
        let status = if decision == "approve" {
            "approved"
        } else {
            "denied"
        };
        body.insert("status".into(), wire(&status)?);
        store.replace_state(&id, &Json::from_object(body))?;
        wire(&serde_json::json!({"kind":"browser", "status":status}))
    }

    /// Pending/slow-down/denied are committed polling outcomes, not transaction
    /// failures. This preserves rate state without consuming an approved grant.
    pub fn poll<S: BrowserStore>(
        &self,
        store: &mut S,
        value: &str,
        now: Timestamp,
    ) -> Result<Json> {
        self.fence(store)?;
        require(value.len() <= 4096, "invalid_grant")?;
        let locator = get(store, &state_id("poll", value), now)?;
        let id = string(&locator.body, "user")?;
        let state = get(store, id, now)?;
        let status = string(&state.body, "status")?.to_owned();
        require(
            string(&state.body, "kind")? == "browser" && status != "consumed",
            "invalid_grant",
        )?;
        let mut interval: i64 = decode(field(&state.body, "interval")?)?;
        require((1..=86400).contains(&interval), "invalid_grant")?;
        let too_soon = Timestamp::parse(string(&state.body, "next_poll")?)? > now;
        if too_soon {
            interval = interval.checked_add(5).ok_or(Error("invalid_grant"))?;
        }
        let body = update(
            state.body,
            [
                ("interval", wire(&interval)?),
                (
                    "next_poll",
                    wire(&Timestamp(now.0 + Duration::seconds(interval)))?,
                ),
            ],
        )?;
        store.replace_state(id, &body)?;
        let error = if too_soon {
            Some("slow_down")
        } else {
            match status.as_str() {
                "pending" => Some("authorization_pending"),
                "denied" => Some("access_denied"),
                "approved" => None,
                _ => return Err(Error("invalid_grant")),
            }
        };
        if let Some(error) = error {
            return wire(&serde_json::json!({"error":error}));
        }
        require_source(
            store,
            &body,
            now,
            self.custodial_ceiling,
            &mut BTreeSet::new(),
        )?;
        let body = update(body, [("status", wire(&"consumed")?)])?;
        store.replace_state(id, &body)?;
        self.create_session(store, &body, None, None, now)
    }

    pub fn create_session<S: BrowserStore>(
        &self,
        store: &mut S,
        source: &Json,
        cookie: Option<&str>,
        ttl: Option<u32>,
        now: Timestamp,
    ) -> Result<Json> {
        self.fence(store)?;
        require_source(
            store,
            source,
            now,
            self.custodial_ceiling,
            &mut BTreeSet::new(),
        )?;
        require(
            ttl.is_none_or(|ttl| (60..=3600).contains(&ttl)),
            "invalid_session_ttl",
        )?;
        let subject = store.subject(string(source, "subject")?)?;
        let parent = store.credential(string(source, "parent")?)?;
        require(
            subject.kind == SubjectKind::Registered
                && parent.kind == CredentialKind::SigningKey
                && !source
                    .as_object()?
                    .get("custodial")
                    .map(truthy)
                    .transpose()?
                    .unwrap_or(false)
                && !source
                    .as_object()?
                    .get("login_binding")
                    .map(truthy)
                    .transpose()?
                    .unwrap_or(false),
            "signature_required",
        )?;
        let cookie = match cookie {
            Some(cookie) if !cookie.is_empty() && cookie.len() <= 4096 => cookie.to_owned(),
            Some(_) => return Err(Error("invalid_grant")),
            None => new_secret()?,
        };
        let ttl = ttl
            .unwrap_or(self.config.session_ttl)
            .min(self.config.session_ttl);
        let mut expiry = Timestamp(now.0 + Duration::seconds(i64::from(ttl)));
        if let Some(parent_expiry) = parent.expires_at {
            expiry = expiry.min(parent_expiry);
        }
        let session_id = state_id("session", &cookie);
        require(
            store.credential_state(&session_id)?.is_none(),
            "invalid_grant",
        )?;
        let credential_id = format!("t_session_{}", random_identifier()?);
        let operations: BTreeSet<String> = self
            .operations
            .iter()
            .filter(|op| {
                (op.effect == "read"
                    || (op.version == 1 && POST_WRITES.contains(&op.name.as_str())))
                    && !op.require_signature
                    && !op.anonymous_only
                    && !["identity.", "root.", "system."]
                        .iter()
                        .any(|prefix| op.name.starts_with(prefix))
            })
            .map(|op| format!("{}@{}", op.name, op.version))
            .collect();
        let wiki = BTreeSet::from([
            "content.post_create@1".to_owned(),
            "content.post_edit@1".to_owned(),
        ]);
        let wiki_scope = Scope {
            resource_id: "t_wiki".into(),
            descendants: true,
        };
        let original: Vec<Grant> = decode(field(source, "ceiling")?)?;
        let mut ceiling = Vec::new();
        for grant in original {
            let general: BTreeSet<String> = grant
                .operations
                .intersection(&operations)
                .filter(|op| !wiki.contains(*op))
                .cloned()
                .collect();
            if !general.is_empty() {
                let mut limited = grant.clone();
                limited.operations = general;
                ceiling.push(limited);
            }
            let shared: BTreeSet<String> = grant
                .operations
                .intersection(&operations)
                .filter(|op| wiki.contains(*op))
                .cloned()
                .collect();
            if !shared.is_empty() {
                let mut limited = grant;
                limited.operations = shared;
                if scope_subset(&wiki_scope, &limited.scope, store)? {
                    limited.scope = wiki_scope.clone();
                    ceiling.push(limited);
                } else if scope_subset(&limited.scope, &wiki_scope, store)? {
                    ceiling.push(limited);
                }
            }
        }
        let credential = Credential {
            id: credential_id.clone(),
            subject_id: subject.resource_id,
            kind: CredentialKind::Token,
            verifier: Verifier(Sha256::digest(self.browser_secret(&cookie)?.as_ref()).to_vec()),
            ceiling,
            not_before: now,
            expires_at: Some(expiry),
            revoked_at: None,
            source_credential_id: Some(parent.id),
        };
        store.save_credential(&credential, subject.auth_version)?;
        let body = update(
            source.clone(),
            [("browser_credential", wire(&credential_id)?)],
        )?;
        store.put_state(&session_id, "session", expiry, &body)?;
        store.put_state(
            &format!("browser:{credential_id}"),
            "browser",
            expiry,
            &update(body, [("session", wire(&session_id)?)])?,
        )?;
        wire(&serde_json::json!({"cookie":cookie,"expires":expiry}))
    }

    pub fn session<S: BrowserStore>(
        &self,
        store: &mut S,
        cookie: &str,
        now: Timestamp,
    ) -> Result<Json> {
        self.fence(store)?;
        require(!cookie.is_empty() && cookie.len() <= 4096, "invalid_grant")?;
        let id = state_id("session", cookie);
        let state = get(store, &id, now)?;
        require(
            !state
                .body
                .as_object()?
                .get("revoked")
                .map(truthy)
                .transpose()?
                .unwrap_or(false),
            "invalid_grant",
        )?;
        require_source(
            store,
            &state.body,
            now,
            self.custodial_ceiling,
            &mut BTreeSet::new(),
        )?;
        let mut source = std::collections::BTreeMap::new();
        for key in [
            "subject",
            "parent",
            "auth_version",
            "custodial",
            "ceiling",
            "auth_time",
        ] {
            source.insert(key.to_owned(), field(&state.body, key)?.clone());
        }
        source.insert("session".into(), wire(&id)?);
        wire(&serde_json::json!({"expires":state.expires_at,"source":Json::from_object(source)}))
    }

    pub fn browser_secret(&self, cookie: &str) -> Result<Zeroizing<[u8; 32]>> {
        let mut mac =
            Hmac::<Sha256>::new_from_slice(self.token_secret).map_err(|_| Error("invalid_key"))?;
        mac.update(b"msg-key-browser-v1:");
        mac.update(cookie.as_bytes());
        Ok(Zeroizing::new(mac.finalize().into_bytes().into()))
    }

    pub fn credentials<S: BrowserStore>(
        &self,
        store: &mut S,
        cookie: &str,
        now: Timestamp,
    ) -> Result<BrowserCredential> {
        let session = self.session(store, cookie, now)?;
        let source = field(&session, "source")?;
        let state = get(store, string(source, "session")?, now)?;
        let credential = store.credential(string(&state.body, "browser_credential")?)?;
        require(credential.current_at(now), "invalid_grant")?;
        require_binding(
            store,
            &credential,
            now,
            Some(&BrowserPolicy {
                enabled: self.config.enabled,
            }),
            self.custodial_ceiling,
        )?;
        Ok(BrowserCredential {
            subject: string(source, "subject")?.into(),
            credential_id: credential.id,
            secret: self.browser_secret(cookie)?,
        })
    }

    pub fn revoke<S: BrowserStore>(
        &self,
        store: &mut S,
        cookie: &str,
        now: Timestamp,
    ) -> Result<()> {
        self.session(store, cookie, now)?;
        let id = state_id("session", cookie);
        let state = get(store, &id, now)?;
        store.replace_state(&id, &update(state.body, [("revoked", wire(&true)?)])?)
    }
}

pub(crate) fn random_identifier() -> Result<String> {
    let mut bytes = [0u8; 16];
    OsRng
        .try_fill_bytes(&mut bytes)
        .map_err(|_| Error("randomness_unavailable"))?;
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}
