//! One authenticated, authorized transaction for every network operation.
use crate::{catalog::{Catalog, OperationSpec}, config::Settings, content, context::{new_id, Context, ContentWrite, HandlerOutput}, discovery};
use hmac::{Hmac, Mac};
use msg_core::{b64, digest, Error, Json, Result};
use msg_identity::{
    authentication::{Admission, AuthenticationService, Entry, OperationPolicy},
    authorization::{AccessRequirement, AuthorizationService, Check},
    browser, certificate_lifecycle, certificates::CertificateValidator, lifecycle,
    models::{decode, wire, ResourceRef, Timestamp}, sessions::BrowserPolicy,
    store::{truthy, AuthorityStore, RuntimeGeneration},
};
use msg_protocol::Request;
use msg_storage::{content::{ContentOptions, GitContentStore}, postgres::{IdentitySession, PgFailure, PgSession, PgStore}, records::{AuditEvent, Event, OperationError, OperationResult, ResultStatus}};
use sha2::Sha256;
use std::{collections::BTreeMap, sync::Arc, time::SystemTime};

pub fn now() -> Timestamp { Timestamp(SystemTime::now().into()) }

pub struct Runtime {
    pub settings: Arc<Settings>,
    pub catalog: Arc<Catalog>,
    pub store: PgStore,
    pub contents: GitContentStore,
    validator: CertificateValidator,
    policies: Vec<OperationPolicy>,
    state: State,
    cursor_key: [u8; 32],
}
struct State {
    generation: RuntimeGeneration,
    authenticator: AuthenticationService,
    authorizer: AuthorizationService,
}
fn validator(settings: &Settings, catalog: &Catalog) -> Result<CertificateValidator> {
    CertificateValidator::new(catalog.capability_registry()?, settings.root_certificate.clone(), settings.root_public_key, settings.service_url.clone())
}
fn identity<'a>(settings: &'a Settings, catalog: &'a Catalog, validator: &'a CertificateValidator, policies: &'a [OperationPolicy]) -> lifecycle::Service<'a> {
    lifecycle::Service { namespace_root:"r_root", validator, online_signer:&settings.online_signer,
        token_secret:&settings.token_secret, primary_ceiling:&catalog.primary_ceiling,
        temporary_ceiling:&catalog.temporary_ceiling, base_grants:&catalog.base_grants,
        operations:policies, certificate_ttl:settings.base_certificate_ttl,
        temporary_ttl:settings.temporary_ttl, recovery_window:settings.credential_delivery_recovery_window }
}
pub fn supports(name: &str, version: u32) -> bool {
    lifecycle::Service::supports(name, version) || certificate_lifecycle::Service::supports(name, version) ||
        content::supports(name, version) || discovery::supports(name, version) ||
        (version == 1 && matches!(name, "identity.session_request" | "identity.session_approve"))
}

impl Runtime {
    pub fn open(settings: Settings) -> Result<Self> {
        let catalog = Catalog::load(Some(&settings.plugins))?;
        let store = PgStore::open_existing(&settings.postgres_dsn)?;
        let contents = GitContentStore::open(&settings.content_dir, ContentOptions {
            binary_dir:Some(settings.blob_dir.clone()), staging_dir:Some(settings.staging_dir.clone()),
            group_read:settings.hosting_content_group_read, ..ContentOptions::default()
        })?;
        let validator = validator(&settings, &catalog)?;
        let state = store.transaction(false, |tx| {
            let root = tx.subject("u_root")?;
            require(root.local_only, "root_policy_corrupt")?;
            validator.validate(validator.root_id(), tx, now())?;
            let generation = RuntimeGeneration::capture(tx)?;
            let authenticator = AuthenticationService::new(crate::executor::validator(&settings, &catalog)?, tx,
                catalog.primary_ceiling.clone(), catalog.temporary_ceiling.clone())?
                .with_browser_policy(BrowserPolicy { enabled:settings.browser.enabled });
            let authorizer = AuthorizationService::new(crate::executor::validator(&settings, &catalog)?, tx,
                catalog.resource_policies(), catalog.base_families.clone())?;
            Ok(State { generation, authenticator, authorizer })
        }).map_err(|e| Error(e.code()))?;
        let mut mac = Hmac::<Sha256>::new_from_slice(settings.token_secret.as_ref()).map_err(|_| Error("invalid_service_key"))?;
        mac.update(b"cursor-key-v1");
        let cursor_key = mac.finalize().into_bytes().into();
        let policies = catalog.policies();
        let mut runtime = Self { settings:Arc::new(settings), catalog:Arc::new(catalog), store, contents, validator, policies, state, cursor_key };
        if runtime.ready()? {
            // Recover an unfinished native content transaction before accepting work.
            runtime.store.content_transaction(&runtime.contents, |unit| {
                runtime.state.generation.require_current(*unit.metadata())?;
                Ok(())
            }).map_err(|e| Error(e.code()))?;
        }
        Ok(runtime)
    }

    pub fn ready(&mut self) -> Result<bool> {
        self.store.transaction(false, |tx| {
            self.state.generation.require_current(tx)?;
            Ok(!self.settings.quarantined() && !tx.has_setting("recovery_quarantine")?)
        }).map_err(|e| Error(e.code()))
    }

    pub fn execute(&mut self, request: &Request) -> OperationResult {
        let mut actor = None;
        let mut subject = None;
        let result = (|| -> std::result::Result<(OperationResult, bool), PgFailure> {
            require(request.canonical()?.len() <= self.settings.max_request_bytes, "request_too_large")?;
            let name = request.field("operation")?.as_str()?;
            let version: u32 = request.field("contract_version")?.as_integer()?.parse().map_err(|_| Error("unknown_operation"))?;
            let spec = self.catalog.operation(name, version)?.clone();
            if name == "identity.temporary" && version < 3 { return Err(Error("temporary_dual_keys_required").into()); }
            if secret_version(name).is_some_and(|v| version < v) { return Err(Error("credential_delivery_upgrade_required").into()); }
            require(spec.entries.contains("network"), "entry_not_allowed")?;
            require(spec.enabled, "operation_disabled")?;
            require(supports(name, version), "operation_not_migrated")?;
            self.catalog.validate(&spec.input_schema.id, request.field("arguments")?)?;
            if self.settings.quarantined() { return Err(Error(if spec.effect == "read" { "recovery_quarantined" } else { "writes_paused" }).into()); }
            let Self { settings, catalog, store, contents, validator, policies, state, cursor_key } = self;
            let mut run = |tx: &PgSession, write: Option<&mut dyn ContentWrite>| {
                state.generation.require_current(tx)?;
                let admitted = state.authenticator.authenticate(request, &spec.policy(), tx, Entry::Network, now())?;
                let principal = match admitted {
                    Admission::Fresh(principal) => principal,
                    Admission::Replay(mut replay) => {
                        replay.authorize_resources(&mut state.authorizer, tx, now())?;
                        let mut result: OperationResult = decode(replay.result()?)?;
                        result.replayed = true;
                        return Ok((result, false));
                    }
                };
                actor = principal.actor().map(str::to_owned); subject = principal.subject().map(str::to_owned);
                if spec.anonymous_only { require(principal.method() == "anonymous" && actor.is_none() && subject.is_none() && principal.credential_id().is_none(), "anonymous_only")?; }
                let mut context = Context { tx, contents, catalog, principal:&principal, request, now:now(), settings,
                    authorizer:&mut state.authorizer, content_write:write, cursor_key };
                let checks = requirements(&context, &spec)?;
                let request_name = context.args()?.get("name").and_then(|v| v.as_str().ok()).map(str::to_owned);
                context.authorizer.require(&principal, Entry::Network, context.now, &checks, request_name.as_deref(), tx)?;
                if spec.effect != "read" {
                    if let Some(previous) = tx.request_result(principal.subject(), request.field("request_id")?.as_str()?, request.field("payload_digest")?.as_str()?)? {
                        let mut result: OperationResult = decode(&previous)?;
                        for reference in &result.resources { context.check(&reference.id, Check::Read)?; }
                        result.replayed = true;
                        return Ok((result, true));
                    }
                    if capacity_write(name) {
                        if let Some(config) = tx.setting("runtime_config")? {
                            if let Some(value) = config.as_object()?.get("accept_writes") { require(truthy(value)?, "writes_paused")?; }
                        }
                    }
                }
                for pair in request.field("expected_generations")?.as_array()? {
                    let pair = pair.as_array()?;
                    let id = pair[0].as_str()?;
                    let current = tx.resource_record(id)?;
                    if !checks.iter().any(|check| check.resource_id == id) { context.check(id, if Some(current.owner.as_str()) == principal.subject() { Check::Manage } else { Check::Read })?; }
                    require(pair[1].as_integer()? == current.generation.to_string(), "generation_conflict")?;
                }
                let audited = audited(&spec);
                let mut before = Vec::new();
                if audited {
                    let mut ids = checks.iter().map(|c| c.resource_id.clone()).collect::<std::collections::BTreeSet<_>>();
                    if let Some(id) = principal.subject() { ids.insert(id.to_owned()); }
                    for id in ids {
                        match tx.resource_record(&id) { Ok(r) => before.push(r), Err(Error("not_found")) => (), Err(e) => return Err(e) }
                    }
                }
                let mut output = dispatch(&mut context, &mut state.generation, validator, policies)?;
                let chosen: Vec<String> = decode(request.field("return_fields")?)?;
                if !chosen.is_empty() {
                    require(!output.resources.is_empty(), "projection_unavailable")?;
                    let mut projections = Vec::new();
                    for reference in &output.resources { projections.push(discovery::read_projection(&mut context, &reference.id, reference.revision.as_deref(), &chosen)?); }
                    output.data.get_or_insert_with(BTreeMap::new).insert("projection".into(), wire(&projections)?);
                }
                if audited {
                    tx.append_audit(&AuditEvent { event:Event { id:new_id("a")?, event_type:name.to_owned(), time:now(), request_id:request.field("request_id")?.as_str()?.to_owned(), actor:actor.clone(), subject:subject.clone(), resources:output.resources.clone(), data:BTreeMap::from([("request_digest".into(), request.field("payload_digest")?.clone())]) },
                        authority:principal.certificates().iter().map(|id|ResourceRef { id:id.clone(), revision:None }).collect(),
                        before_digest:Some(digest(wire(&before)?.canonical()?.as_bytes())), after_digest:Some(digest(wire(&output)?.canonical()?.as_bytes())), previous_digest:None, entry_digest:String::new(), result:"committed".into() })?;
                }
                catalog.validate(&spec.output_schema.id, &wire(&output)?)?;
                if let Some(id) = principal.actor() {
                    let key = format!("delegated_identity:{id}");
                    if let Some(binding) = tx.setting(&key)? {
                        let mut binding = binding.into_object()?;
                        if let Some(maximum) = binding.get("max_uses").filter(|v| !v.is_null()) {
                            require(spec.effect == "transaction", "limited_delegation_operation")?;
                            let maximum: u64 = decode(maximum)?;
                            let uses: u64 = decode(binding.get("uses").ok_or(Error("invalid_delegation"))?)?;
                            require(uses < maximum, "delegation_exhausted")?;
                            binding.insert("uses".into(), wire(&(uses + 1))?);
                            tx.set_setting(&key, &Json::from_object(binding))?;
                        }
                    }
                }
                let mut result = OperationResult { request_id:request.field("request_id")?.as_str()?.to_owned(), operation:name.to_owned(),
                    status:if spec.effect == "external" { ResultStatus::Accepted } else { ResultStatus::Ok }, actor:actor.clone(), subject:subject.clone(),
                    resources:output.resources, committed_at:(spec.effect != "read").then(now), replayed:false, receipt:None, error:None, data:output.data, output:output.output, prefer_cli:false, cli_url:None };
                if let Some(time) = result.committed_at {
                    let eid = digest(wire(&(principal.subject(), &result.request_id))?.canonical()?.as_bytes());
                    let event = Event { id:format!("e_{}", &eid[7..39]), event_type:name.to_owned(), time, request_id:result.request_id.clone(), actor:actor.clone(), subject:subject.clone(), resources:result.resources.clone(), data:BTreeMap::from([("operation".into(), wire(&name)?)]) };
                    tx.append_event(&event)?;
                    crate::notifications::enqueue(&mut context, &event)?;
                    result.receipt = Some(settings.receipt_signer.sign(&receipt_bytes(&result)?, "receipt")?);
                    require(wire(&result)?.canonical()?.len() <= settings.max_response_bytes, "response_too_large")?;
                    tx.save_result(principal.subject(), request.field("payload_digest")?.as_str()?, &result)?;
                }
                Ok((result, true))
            };
            if spec.effect == "read" { store.transaction(false, |tx| run(tx, None)) }
            else { store.content_transaction(contents, |unit| { let tx = *unit.metadata(); run(tx, Some(unit)) }) }
        })();
        match result {
            Ok((mut result, ordinary)) => {
                result.prefer_cli = request.field("source").and_then(Json::as_str).unwrap_or("unknown") != "msg";
                result.cli_url = result.prefer_cli.then(|| "/AGENTS.md".to_owned());
                if ordinary && matches!(result.status, ResultStatus::Ok) && secret_version(&result.operation).is_some() {
                    let released = (|| {
                        let subject = result.subject.as_deref().ok_or(Error("invalid_token_result"))?;
                        let credential_id = result.data.as_ref().and_then(|m| m.get("credential_id")).ok_or(Error("invalid_token_result"))?.as_str()?;
                        let request = Json::parse(&request.canonical()?)?;
                        let service = identity(&self.settings, &self.catalog, &self.validator, &self.policies);
                        self.store.transaction(true, |tx| {
                            let mut store = IdentitySession::new(tx, &mut self.state.generation, self.settings.quarantined())?;
                            service.claim_delivery(&mut store, &request, subject, credential_id, now())
                        })
                    })();
                    match released {
                        Ok(secret) => { result.data.get_or_insert_with(BTreeMap::new).insert("token".into(), wire(&b64(secret.as_ref())).expect("bounded token")); }
                        Err(error) => {
                            let mut failed = failure(request, actor, subject, &error);
                            if error.code() == "token_delivery_unavailable" { failed.data = Some(BTreeMap::from([("committed_result".into(), wire(&result).expect("validated result"))])); }
                            return failed;
                        }
                    }
                }
                result
            }
            Err(error) => failure(request, actor, subject, &error),
        }
    }
}

fn requirements(context: &Context<'_>, spec: &OperationSpec) -> Result<Vec<AccessRequirement>> {
    if content::supports(&spec.name, spec.version) { return content::requirements(context); }
    if discovery::supports(&spec.name, spec.version) { return discovery::requirements(context); }
    if spec.requirements == "msg.plugins.common:no_requirements" { return Ok(vec![]); }
    if let Some(access) = &spec.access {
        let reference = context.args()?.get(&access.argument).map(Json::as_str).transpose()?.or(access.default.as_deref()).ok_or(Error("missing_argument"))?;
        let check = match access.check.as_str() { "read"=>Check::Read,"list"=>Check::List,"traverse"=>Check::Traverse,"write"=>Check::Write,"create"=>Check::Create,"remove"=>Check::Remove,"chmod"=>Check::Chmod,"chgrp"=>Check::Chgrp,"chown"=>Check::Chown,"manage"=>Check::Manage,"certgate"=>Check::Certgate,"purge"=>Check::Purge,"tool_use"=>Check::ToolUse,_=>return Err(Error("invalid_registry")) };
        return Ok(vec![AccessRequirement { resource_id:context.resolve(reference, false)?, operation:context.operation()?, check }]);
    }
    Err(Error("operation_not_migrated"))
}
fn dispatch(context: &mut Context<'_>, generation: &mut RuntimeGeneration, validator: &CertificateValidator, policies: &[OperationPolicy]) -> Result<HandlerOutput> {
    let name = context.request.field("operation")?.as_str()?;
    let version: u32 = decode(context.request.field("contract_version")?)?;
    if content::supports(name, version) { return content::handle(context); }
    if discovery::supports(name, version) { return discovery::handle(context); }
    let mut store = IdentitySession::new(context.tx, generation, context.settings.quarantined())?;
    let request = Json::parse(&context.request.canonical()?)?;
    if lifecycle::Service::supports(name, version) {
        return decode(&identity(context.settings, context.catalog, validator, policies).handle(&mut store, context.authorizer, context.principal, &request, context.now)?);
    }
    if certificate_lifecycle::Service::supports(name, version) {
        return decode(&certificate_lifecycle::Service {validator}.handle(&mut store, context.authorizer, context.principal, &request, context.now)?);
    }
    if matches!(name, "identity.session_request" | "identity.session_approve") && version == 1 {
        let browser = browser::Service::new(browser::Config { enabled:context.settings.browser.enabled, session_ttl:context.settings.browser.session_ttl }, &context.settings.service_url, &context.settings.token_secret, policies, &context.catalog.temporary_ceiling)?;
        let code = context.text("user_code")?;
        let data = if name == "identity.session_request" { browser.inspect(&mut store, code, context.now)? }
            else {
                context.authorizer.require_base(context.principal, &context.operation()?, context.subject()?, context.tx, context.now)?;
                browser.approve(&mut store, context.principal, code, context.text("decision")?, context.now)?
            };
        return Ok(HandlerOutput { data:Some(data.into_object()?), ..HandlerOutput::default() });
    }
    Err(Error("operation_not_migrated"))
}
pub fn receipt_bytes(result: &OperationResult) -> Result<Vec<u8>> {
    let mut value = wire(result)?.into_object()?;
    for name in ["receipt", "replayed", "prefer_cli", "cli_url"] { value.remove(name); }
    if secret_version(&result.operation).is_some() {
        if let Some(data) = value.get_mut("data").filter(|v| !v.is_null()) { let mut fields = data.clone().into_object()?; fields.remove("token"); *data = Json::from_object(fields); }
    }
    Ok(Json::from_object(value).canonical()?.into_bytes())
}
pub fn result_wire(result: &OperationResult) -> Result<Json> {
    fn compact(value: serde_json::Value) -> serde_json::Value {
        match value {
            serde_json::Value::Object(map) => serde_json::Value::Object(map.into_iter().filter(|(_,v)| !v.is_null()).map(|(k,v)|(k,compact(v))).collect()),
            serde_json::Value::Array(list) => serde_json::Value::Array(list.into_iter().map(compact).collect()),
            value => value,
        }
    }
    let mut value = serde_json::to_value(result).map_err(|_|Error("invalid_result"))?;
    let object = value.as_object_mut().ok_or(Error("invalid_result"))?;
    for key in ["replayed", "prefer_cli"] { if object.get(key).and_then(serde_json::Value::as_bool) == Some(false) { object.remove(key); } }
    if result.resources.is_empty() { object.remove("resources"); }
    Json::parse(&compact(value).to_string())
}
fn failure(request: &Request, actor: Option<String>, subject: Option<String>, error: &PgFailure) -> OperationResult {
    OperationResult { request_id:request.field("request_id").and_then(Json::as_str).unwrap_or("").to_owned(), operation:request.field("operation").and_then(Json::as_str).unwrap_or("").to_owned(),
        status:if matches!(error, PgFailure::Uncertain {..}) { ResultStatus::Uncertain } else { ResultStatus::Error }, actor,subject,
        resources:vec![], committed_at:None,replayed:false,receipt:None,
        error:Some(OperationError {code:error.code().to_owned(),retryable:false,field_path:None,retry_after_seconds:None,message:Some(crate::errors::public_message(error.code()).to_owned())}),
        data:None,output:None,prefer_cli:false,cli_url:None }
}
fn secret_version(name: &str) -> Option<u32> { match name { "identity.temporary"=>Some(3),"identity.custodial_create"|"identity.token_rotate"|"identity.token_create"=>Some(2),"identity.token_recover"=>Some(1),_=>None } }
fn audited(spec: &OperationSpec) -> bool {
    matches!(spec.name.as_str(), "content.chmod"|"content.chgrp"|"content.chown"|"content.purge"|"content.move"|"file.move"|"identity.key_add"|"identity.key_revoke"|"identity.recover"|"identity.delegate"|"identity.delegation_revoke"|"identity.ssh_key_add"|"identity.ssh_key_revoke"|"identity.ssh_certificates"|"cert.publish"|"cert.request") || (spec.name.starts_with("group.") && spec.effect != "read")
}
fn capacity_write(name: &str) -> bool {
    matches!(name,"identity.register"|"identity.delegated_create"|"identity.temporary"|"identity.custodial_create"|"content.topic_create"|"content.post_create"|"content.post_edit"|"content.file_put"|"content.attach"|"file.create"|"file.copy"|"file.write"|"file.patch"|"file.mkdir"|"content.template_put"|"discussion.reply"|"discussion.quote"|"discussion.repost"|"transfer.part_put"|"git.create"|"git.push"|"git.receive"|"hosting.deploy"|"hosting.preview"|"keystore.put"|"achievement.start")
}
fn require(ok: bool, code: &'static str) -> Result<()> { if ok {Ok(())} else {Err(Error(code))} }
