//! Synthetic-fixture PostgreSQL integration driver. Never expose as a service.
use msg_core::{b64, unb64, Error, Json, Result, MAX_BYTES};
use msg_crypto::Ed25519Signer;
use msg_identity::{
    authentication::{Admission, AuthenticationService, Entry, OperationPolicy},
    authorization::{AccessRequirement, AuthorizationService, Check, ResourceTypePolicy},
    browser,
    certificates::CertificateValidator,
    lifecycle,
    models::*,
    policy::{CapabilityPolicy, Registry},
    sessions::BrowserPolicy,
    store::{AuthorityStore, RuntimeGeneration},
};
use msg_protocol::Request;
use msg_storage::{
    postgres::{IdentitySession, PgStore},
    records,
};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    io::{self, BufRead, Read, Write},
};

#[derive(Deserialize)]
struct Context {
    root: Certificate,
    root_public: String,
    service: String,
    capabilities: Vec<CapabilityPolicy>,
    primary_ceiling: Vec<Grant>,
    temporary_ceiling: Vec<Grant>,
    operations: Vec<OperationPolicy>,
    resource_types: Vec<ResourceTypePolicy>,
}
impl Context {
    fn validator(&self) -> Result<CertificateValidator> {
        CertificateValidator::new(
            Registry::new(self.capabilities.clone())?,
            self.root.clone(),
            unb64(&self.root_public, 32)?
                .try_into()
                .map_err(|_| Error("invalid_test_context"))?,
            self.service.clone(),
        )
    }
}
fn text<'a>(value: &'a Value, field: &str) -> Result<&'a str> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or(Error("invalid_test_input"))
}
fn input(reader: &mut impl BufRead) -> io::Result<Option<Value>> {
    let mut text = String::new();
    if reader.take((MAX_BYTES + 1) as u64).read_line(&mut text)? == 0 {
        return Ok(None);
    }
    if text.len() > MAX_BYTES {
        return Err(io::Error::other("test_input_too_large"));
    }
    serde_json::from_str(&text)
        .map(Some)
        .map_err(|_| io::Error::other("invalid_test_input"))
}
fn output(writer: &mut impl Write, result: Result<Value>) -> io::Result<()> {
    let data = match result {
        Ok(data) => json!({"ok":true,"data":data}),
        Err(error) => json!({"ok":false,"code":error.0}),
    };
    writeln!(writer, "{data}")?;
    writer.flush()
}
fn main() -> io::Result<()> {
    let mut reader = io::stdin().lock();
    let mut writer = io::BufWriter::new(io::stdout().lock());
    let context: Context = serde_json::from_value(
        input(&mut reader)?.ok_or(io::Error::other("test_context_required"))?,
    )
    .map_err(|_| io::Error::other("invalid_test_context"))?;
    let dsn = std::env::var("MSG_PARITY_POSTGRES_DSN")
        .map_err(|_| io::Error::other("test_database_required"))?;
    let store = PgStore::open_existing(&dsn).map_err(|error| io::Error::other(error.0))?;
    let initialized = store
        .transaction(false, |tx| {
            Ok((
                AuthenticationService::new(
                    context.validator()?,
                    tx,
                    context.primary_ceiling.clone(),
                    context.temporary_ceiling.clone(),
                )?
                .with_browser_policy(BrowserPolicy { enabled: true }),
                AuthorizationService::new(
                    context.validator()?,
                    tx,
                    context.resource_types.clone(),
                    BTreeSet::from(["resource.basic".into(), "identity.basic".into()]),
                )?,
                RuntimeGeneration::capture(tx)?,
            ))
        })
        .map_err(|error| io::Error::other(error.code()))?;
    let (mut auth, mut authorizer, mut generation) = initialized;
    output(&mut writer, Ok(json!({"ready":true})))?;
    let validator = context
        .validator()
        .map_err(|error| io::Error::other(error.0))?;
    let signer = Ed25519Signer::from_seed(&[5; 32]);
    let base: Vec<_> = context
        .primary_ceiling
        .iter()
        .filter(|grant| {
            matches!(
                grant.capability.as_str(),
                "resource.basic" | "identity.basic"
            )
        })
        .cloned()
        .collect();
    let lifecycle = lifecycle::Service {
        namespace_root: "r_root",
        validator: &validator,
        online_signer: &signer,
        token_secret: &[9; 32],
        primary_ceiling: &context.primary_ceiling,
        temporary_ceiling: &context.temporary_ceiling,
        base_grants: &base,
        operations: &context.operations,
        certificate_ttl: 3600,
        temporary_ttl: 7200,
        recovery_window: 3600,
    };
    let browser = browser::Service::new(
        browser::Config {
            enabled: true,
            session_ttl: 3600,
        },
        &context.service,
        &[9; 32],
        &context.operations,
        &context.temporary_ceiling,
    )
    .map_err(|error| io::Error::other(error.0))?;
    while let Some(command) = input(&mut reader)? {
        let result = (|| -> Result<Value> {
            let action = text(&command, "action")?;
            let now = Timestamp::parse(text(&command, "now")?)?;
            let value = store.transaction(true, |tx| {
                match action {
                    "handle" | "approve" | "vault_sign" => {
                        let raw = command.get("request").ok_or(Error("invalid_test_input"))?.to_string();
                        let request = Request::parse(&raw)?;
                        let spec = context.operations.iter().find(|spec| request.field("operation").and_then(Json::as_str).ok() == Some(spec.name.as_str())
                            && request.field("contract_version").and_then(Json::as_integer).ok() == Some(spec.version.to_string().as_str())).ok_or(Error("operation_not_found"))?;
                        let principal = match auth.authenticate(&request, spec, tx, Entry::Network, now)? {
                            Admission::Fresh(principal) => principal,
                            Admission::Replay(mut replay) => {
                                replay.authorize_resources(&mut authorizer, tx, now)?;
                                return Ok(json!({"replay":replay.result()?}));
                            }
                        };
                        if action == "handle" && spec.effect != "read" {
                            if let Some(previous) = tx.request_result(principal.subject(), request.field("request_id")?.as_str()?, request.field("payload_digest")?.as_str()?)? {
                                let references: Vec<ResourceRef> = decode(previous.as_object()?.get("resources").ok_or(Error("invalid_storage_record"))?)?;
                                let checks: Vec<_> = references.into_iter().map(|reference| AccessRequirement {
                                    resource_id: reference.id, operation: format!("{}@{}", spec.name, spec.version), check: Check::Read,
                                }).collect();
                                authorizer.require(&principal, Entry::Network, now, &checks, None, tx)?;
                                return Ok(json!({"replay":previous}));
                            }
                        }
                        if matches!(action, "approve" | "vault_sign") {
                            authorizer.require_base(&principal, &format!("{}@{}",spec.name,spec.version), principal.subject().ok_or(Error("identity_owner_required"))?, tx, now)?;
                        }
                        let mut identity = IdentitySession::new(tx, &mut generation, false)?;
                        let request_json = Json::parse(&raw)?;
                        if action == "vault_sign" {
                            let signer = msg_identity::vault::open_signer(&identity, &[9;32], principal.subject().ok_or(Error("identity_owner_required"))?, now)?;
                            return Ok(json!({"signature":signer.sign(b"fixture revision", "revision")?}));
                        }
                        let handled = if action == "approve" {
                            let args = command.get("request").and_then(|v| v.get("arguments")).ok_or(Error("invalid_test_input"))?;
                            browser.approve(&mut identity, &principal, text(args,"user_code")?, text(args,"decision")?, now)?
                        } else if msg_identity::custodial_lifecycle::supports(&spec.name, spec.version) {
                            match msg_identity::custodial_lifecycle::handle(&lifecycle, &mut identity, &mut authorizer,
                                &principal, &request_json, now, &|_,_| Err(Error("fixture_content_required")))? {
                                msg_identity::custodial_lifecycle::Outcome::Complete(result) => result,
                                msg_identity::custodial_lifecycle::Outcome::Create(_) => return Err(Error("fixture_content_required")),
                            }
                        } else if msg_identity::certificate_lifecycle::Service::supports(&spec.name, spec.version) {
                            msg_identity::certificate_lifecycle::Service { validator: &validator }.handle(
                                &mut identity, &mut authorizer, &principal, &request_json, now,
                            )?
                        } else {
                            lifecycle.handle(&mut identity, &mut authorizer, &principal, &request_json, now)?
                        };
                        if command.get("fail_after").and_then(Value::as_bool) == Some(true) { return Err(Error("fixture_failure")) }
                        if action == "handle" {
                            let fields = handled.as_object()?;
                            let result: records::OperationResult = records::decode(&wire(&json!({
                                "request_id":request.field("request_id")?,"operation":spec.name,"status":"ok", "actor":principal.actor(),"subject":principal.subject(),
                                "resources":fields.get("resources"),"committed_at":now,"data":fields.get("data")
                            }))?)?;
                            tx.save_result(principal.subject(), request.field("payload_digest")?.as_str()?, &result)?;
                        }
                        Ok(json!(handled))
                    }
                    "claim" => {
                        let mut identity = IdentitySession::new(tx, &mut generation, false)?;
                        let request = wire(command.get("request").ok_or(Error("invalid_test_input"))?)?;
                        let secret = lifecycle.claim_delivery(&mut identity, &request, text(&command,"subject")?, text(&command,"credential_id")?, now)?;
                        Ok(json!({"token":b64(secret.as_ref())}))
                    }
                    _ => {
                        let mut identity = IdentitySession::new(tx, &mut generation, false)?;
                        match action {
                            "pending" => { browser.rate(&mut identity, "127.0.0.1", now)?; Ok(json!(browser.pending(&mut identity, now)?)) },
                            "poll" => Ok(json!(browser.poll(&mut identity, text(&command,"value")?, now)?)),
                            "session" => Ok(json!(browser.session(&mut identity, text(&command,"cookie")?, now)?)),
                            "logout" => { browser.revoke(&mut identity, text(&command,"cookie")?, now)?; Ok(json!({"revoked":true})) },
                            _ => Err(Error("unknown_test_action")),
                        }
                    }
                }
            }).map_err(|error| Error(error.code()))?;
            Ok(value)
        })();
        output(&mut writer, result)?;
    }
    Ok(())
}
