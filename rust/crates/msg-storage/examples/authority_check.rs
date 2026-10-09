//! Local JSONL differential-test helper. Never register this as an HTTP/MCP tool.
//! Inputs are synthetic public fixtures; errors contain codes, never proof bytes.
use msg_core::{digest, unb64, Error, Json, Result, MAX_BYTES};
use msg_identity::{
    authentication::{Admission, AuthenticationService, Entry, OperationPolicy},
    authorization::{
        AccessRequirement, AuthorizationService, AuthorizationStore, Check, ResourceTypePolicy,
    },
    certificates::CertificateValidator,
    models::*,
    policy::*,
    store::Record,
};
use msg_protocol::Request;
use msg_storage::{postgres::PgStore, SqliteAuthority};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    io::{self, BufRead, Read, Write},
    path::Path,
};

#[derive(Deserialize)]
struct Context {
    root: Certificate,
    root_public: String,
    service: String,
    capabilities: Vec<CapabilityPolicy>,
    primary_ceiling: Vec<Grant>,
    temporary_ceiling: Vec<Grant>,
    oauth: msg_identity::oauth::OAuthPolicy,
    #[serde(default)]
    resource_types: Vec<ResourceTypePolicy>,
    #[serde(default)]
    base_families: BTreeSet<String>,
}
impl Context {
    fn validator(&self) -> Result<CertificateValidator> {
        let public: [u8; 32] = unb64(&self.root_public, 32)?
            .try_into()
            .map_err(|_| Error("invalid_test_input"))?;
        CertificateValidator::new(
            Registry::new(self.capabilities.clone())?,
            self.root.clone(),
            public,
            self.service.clone(),
        )
    }
}
fn text<'a>(input: &'a Value, key: &str) -> Result<&'a str> {
    input
        .get(key)
        .and_then(Value::as_str)
        .ok_or(Error("invalid_test_input"))
}
fn value(input: &Value, key: &str) -> Result<Json> {
    Json::parse(
        &input
            .get(key)
            .ok_or(Error("invalid_test_input"))?
            .to_string(),
    )
}
fn run(
    input: Value,
    context: &Context,
    auth: &mut AuthenticationService,
    authorization: &mut AuthorizationService,
    session: &dyn AuthorizationStore,
) -> Result<Value> {
    let store = session.authority();
    let now = || Timestamp::parse(text(&input, "now")?);
    match text(&input, "action")? {
        "authenticate" => {
            let request = Request::parse(text(&input, "raw")?)?;
            let policy: OperationPolicy = decode(&value(&input, "policy")?)?;
            let entry = match text(&input, "entry")? {
                "network" => Entry::Network,
                "local_admin" => Entry::LocalAdmin,
                "worker" => Entry::Worker,
                _ => return Err(Error("invalid_test_input")),
            };
            match auth.authenticate_for_resource(
                &request,
                &policy,
                store,
                entry,
                now()?,
                input.get("resource").and_then(Value::as_str),
            )? {
                Admission::Fresh(p) => Ok(json!({"kind":"fresh", "principal":p})),
                Admission::Replay(result) => Ok(json!({"kind":"replay", "result":result})),
            }
        }
        "authorize" | "base" | "has" => {
            let request = Request::parse(text(&input, "raw")?)?;
            let policy: OperationPolicy = decode(&value(&input, "policy")?)?;
            let entry = match text(&input, "entry")? {
                "network" => Entry::Network,
                "local_admin" => Entry::LocalAdmin,
                "worker" => Entry::Worker,
                _ => return Err(Error("invalid_test_input")),
            };
            let Admission::Fresh(principal) =
                auth.authenticate(&request, &policy, store, entry, now()?)?
            else {
                return Err(Error("test_fresh_principal_required"));
            };
            match text(&input, "action")? {
                "base" => {
                    authorization.require_base(
                        &principal,
                        text(&input, "operation")?,
                        text(&input, "id")?,
                        store,
                        now()?,
                    )?;
                    Ok(json!({"allowed":true}))
                }
                "has" => Ok(
                    json!({"allowed":authorization.has(&principal, text(&input,"capability")?, text(&input,"operation")?, text(&input,"id")?, store, now()?)?}),
                ),
                _ => {
                    let mut checks = Vec::new();
                    for item in input
                        .get("checks")
                        .and_then(Value::as_array)
                        .ok_or(Error("invalid_test_input"))?
                    {
                        checks.push(AccessRequirement {
                            resource_id: text(item, "resource_id")?.to_owned(),
                            operation: text(item, "operation")?.to_owned(),
                            check: Check::parse(text(item, "check")?)?,
                        });
                    }
                    let refs = authorization.require(
                        &principal,
                        entry,
                        now()?,
                        &checks,
                        input.get("request_name").and_then(Value::as_str),
                        session,
                    )?;
                    Ok(json!({"refs":refs}))
                }
            }
        }
        "memberships" => Ok(json!({"groups":session.memberships(text(&input,"subject")?)?})),
        "share_source" => {
            let resource = session.policy_resource(text(&input, "id")?)?;
            Ok(
                json!({"allowed":authorization.share_source_active(&resource,text(&input,"grant")?,text(&input,"subject")?,now()?,session,input.get("reshare").and_then(Value::as_bool).unwrap_or(false))?}),
            )
        }
        "share_link" => {
            let resource = session.policy_resource(text(&input, "id")?)?;
            let mut chain = store
                .ancestors(&resource.id)?
                .iter()
                .map(|r| session.policy_resource(&r.id))
                .collect::<Result<Vec<_>>>()?;
            chain.push(resource.clone());
            Ok(
                json!({"allowed":authorization.share_link_read_allowed(text(&input,"grantor")?,text(&input,"credential")?,&resource,&chain,now()?,session)?}),
            )
        }
        "certificate" => {
            let cert = context
                .validator()?
                .validate(text(&input, "id")?, store, now()?)?;
            Ok(
                json!({"canonical":wire(&cert)?.canonical()?, "signing_digest":digest(&cert.signing_bytes()?)}),
            )
        }
        "publication" => {
            let cert: Certificate = decode(&value(&input, "certificate")?)?;
            let csr: CertificateRequest = decode(&value(&input, "csr")?)?;
            let validated =
                context
                    .validator()?
                    .validate_publication(&cert, &csr, store, now()?)?;
            Ok(json!({"canonical":wire(&validated)?.canonical()?}))
        }
        "result" => Ok(
            json!({"result":store.request_result(input.get("subject").and_then(Value::as_str), text(&input,"id")?, text(&input,"digest")?)?}),
        ),
        "setting" => Ok(
            json!({"exists":store.has_setting(text(&input,"key")?)?, "value":store.setting(text(&input,"key")?)?}),
        ),
        "record_digest" => {
            let kind = match text(&input, "kind")? {
                "subject" => Record::Subject,
                "credential" => Record::Credential,
                "certificate" => Record::Certificate,
                "resource" => Record::Resource,
                _ => return Err(Error("invalid_test_input")),
            };
            let raw = store.record(kind, text(&input, "id")?)?;
            let normalized = match kind {
                Record::Subject => wire(&decode::<Subject>(&raw)?)?,
                Record::Credential => wire(&decode::<Credential>(&raw)?)?,
                Record::Certificate => wire(&decode::<Certificate>(&raw)?)?,
                Record::Resource => raw,
            };
            Ok(json!({"digest":digest(normalized.canonical()?.as_bytes())}))
        }
        "scope" => Ok(
            json!({"contains":scope_contains(&decode(&value(&input,"scope")?)?, text(&input,"id")?, store)?}),
        ),
        "subset" => Ok(
            json!({"subset":grant_subset(&decode(&value(&input,"child")?)?, &decode(&value(&input,"parent")?)?, store)?}),
        ),
        "modes" => {
            let mut decisions = Vec::new();
            let mut resource = ResourceAuthority {
                id: "r".into(),
                kind: "file".into(),
                parent: None,
                owner: "owner".into(),
                group: "group".into(),
                mode: String::new(),
                state: "active".into(),
                revision: None,
            };
            let group = BTreeSet::from(["group".to_owned()]);
            for mode in 0..=0o7777 {
                resource.mode = format!("{mode:04o}");
                for subject in [Some("owner"), Some("member"), None] {
                    for permission in ["read", "write", "execute", "list", "traverse"] {
                        decisions.push(allows(&resource, subject, &group, permission)?);
                    }
                }
            }
            Ok(
                json!({"count":decisions.len(), "digest":digest(wire(&decisions)?.canonical()?.as_bytes())}),
            )
        }
        _ => Err(Error("unknown_test_action")),
    }
}
fn read_input(reader: &mut impl BufRead) -> io::Result<Option<Value>> {
    let mut raw = String::new();
    if reader
        .take((MAX_BYTES * 8 + 1) as u64)
        .read_line(&mut raw)?
        == 0
    {
        return Ok(None);
    }
    if raw.len() > MAX_BYTES * 8 {
        return Err(io::Error::other("test_input_too_large"));
    }
    serde_json::from_str(&raw)
        .map(Some)
        .map_err(|_| io::Error::other("invalid_test_input"))
}
fn output(writer: &mut impl Write, result: Result<Value>) -> io::Result<()> {
    let value = match result {
        Ok(data) => json!({"ok":true,"data":data}),
        Err(e) => json!({"ok":false,"code":e.0}),
    };
    writeln!(writer, "{value}")?;
    writer.flush()
}
fn main() -> io::Result<()> {
    let path = std::env::args()
        .nth(1)
        .ok_or(io::Error::other("test_database_required"))?;
    let mut reader = io::stdin().lock();
    let mut writer = io::BufWriter::new(io::stdout().lock());
    let input = read_input(&mut reader)?.ok_or(io::Error::other("test_context_required"))?;
    let context: Context =
        serde_json::from_value(input).map_err(|_| io::Error::other("invalid_test_context"))?;
    enum Backend {
        Sqlite(SqliteAuthority),
        Postgres(Box<PgStore>),
    }
    impl Backend {
        fn read<T>(
            &mut self,
            body: impl FnOnce(&dyn AuthorizationStore) -> Result<T>,
        ) -> Result<T> {
            match self {
                Self::Sqlite(store) => body(&store.snapshot()?),
                Self::Postgres(store) => store
                    .transaction(false, |tx| body(tx))
                    .map_err(|e| Error(e.code())),
            }
        }
    }
    let mut store = match std::env::var("MSG_PARITY_POSTGRES_DSN") {
        Ok(dsn) => PgStore::open_existing(&dsn).map(|store| Backend::Postgres(Box::new(store))),
        Err(_) => SqliteAuthority::open_existing(Path::new(&path)).map(Backend::Sqlite),
    };
    let store = match &mut store {
        Ok(store) => store,
        Err(e) => {
            output(&mut writer, Err(*e))?;
            return Ok(());
        }
    };
    let (mut auth, mut authorization) = store
        .read(|snapshot| {
            let auth = AuthenticationService::new(
                context.validator()?,
                snapshot.authority(),
                context.primary_ceiling.clone(),
                context.temporary_ceiling.clone(),
            )?
            .with_oauth_policy(context.oauth.clone());
            let authorization = AuthorizationService::new(
                context.validator()?,
                snapshot.authority(),
                context.resource_types.clone(),
                context.base_families.clone(),
            )?;
            Ok((auth, authorization))
        })
        .map_err(io::Error::other)?;
    output(&mut writer, Ok(json!({"ready":true})))?;
    while let Some(input) = read_input(&mut reader)? {
        if input.get("action").and_then(Value::as_str) == Some("hold_snapshot") {
            let result = store.read(|snapshot| {
                output(
                    &mut writer,
                    snapshot
                        .setting("snapshot_probe")
                        .map(|v| json!({"before":v})),
                )
                .map_err(|_| Error("test_io_error"))?;
                read_input(&mut reader)
                    .map_err(|_| Error("test_io_error"))?
                    .ok_or(Error("test_resume_required"))?;
                Ok(json!({"after":snapshot.setting("snapshot_probe")?}))
            });
            output(&mut writer, result)?;
        } else {
            let result = store
                .read(|snapshot| run(input, &context, &mut auth, &mut authorization, snapshot));
            output(&mut writer, result)?;
        }
    }
    Ok(())
}
