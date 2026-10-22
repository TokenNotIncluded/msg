//! Live certificate intersections and atomic publication of sandbox output.
use crate::{
    network::{self, NetworkPolicy},
    process::{self, Directory, Stop},
    sandbox::{self, BubblewrapRunner, Manifest},
    AttemptContext, EffectOutcome, EffectResult,
};
use msg_core::{Error, Json, Result};
use msg_identity::{
    authentication::{Entry, Principal},
    authorization::{AccessRequirement, AuthorizationService, Check},
    models::{Grant, ResourceRef, Timestamp},
    policy::grant_covers,
    worker_authority::WorkerAuthority,
};
use msg_storage::{
    content::{recovery::ContentTransaction, GitContentStore},
    postgres::PgSession,
    records::{decode, encode, BlobRef, EffectJob},
};
use serde::{Deserialize, Serialize};
use std::{collections::BTreeSet, net::IpAddr, path::PathBuf};

#[derive(Clone)]
pub struct Tool {
    pub resource: ResourceRef,
    pub executor: String,
    pub network: NetworkPolicy,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolLimits {
    pub timeout_ms: u64,
    pub max_response_bytes: u64,
    pub methods: BTreeSet<String>,
    pub ports: BTreeSet<u16>,
}
impl Default for ToolLimits {
    fn default() -> Self {
        Self {
            timeout_ms: 10000,
            max_response_bytes: 4 * 1024 * 1024,
            methods: ["GET".into(), "HEAD".into()].into(),
            ports: [80, 443].into(),
        }
    }
}
pub fn concurrency(
    contents: &GitContentStore,
    tx: &PgSession,
    reference: &ResourceRef,
) -> Result<u32> {
    let revision = tx.revision(reference)?;
    declared_concurrency(&read_definition(contents, &revision.content)?)
}
fn read_definition(contents: &GitContentStore, blob: &BlobRef) -> Result<Json> {
    let bytes = contents.read_bytes(blob, 65536)?;
    Json::parse(std::str::from_utf8(&bytes).map_err(|_| Error("invalid_tool_definition"))?)
}
fn declared_concurrency(definition: &Json) -> Result<u32> {
    let value = definition
        .as_object()?
        .get("concurrency")
        .map(|v| {
            v.as_integer()?
                .parse::<u32>()
                .map_err(|_| Error("invalid_tool_concurrency"))
        })
        .transpose()?
        .unwrap_or(1);
    if !(1..=32).contains(&value) {
        return Err(Error("invalid_tool_concurrency"));
    }
    Ok(value)
}
pub fn read_tool(
    contents: &GitContentStore,
    tx: &PgSession,
    reference: &ResourceRef,
) -> Result<Tool> {
    let resource = tx.resource_record(&reference.id)?;
    if resource.resource_type != "tool" || resource.parent.as_deref() != Some("t_tools") {
        return Err(Error("not_a_tool"));
    }
    let revision = tx.revision(reference)?;
    let value = read_definition(contents, &revision.content)?;
    declared_concurrency(&value)?;
    let value = value.as_object()?;
    let get = |key: &str| value.get(key).ok_or(Error("invalid_tool_definition"));
    let executor = match reference.id.as_str() {
        "tool_curl" => "curl",
        "tool_dns" => "dns",
        _ => return Err(Error("untrusted_tool_executor")),
    };
    if get("version")?.as_integer()? != "1"
        || get("tool_id")?.as_str()? != reference.id
        || get("executor_key")?.as_str()? != executor
    {
        return Err(Error("untrusted_tool_executor"));
    }
    for suffix in ["input", "output"] {
        let schema: ResourceRef = decode(get(&format!("{suffix}_schema"))?)?;
        if schema.id != format!("schema:tool.{executor}:{suffix}") || schema.revision.is_some() {
            return Err(Error("tool_schema_mismatch"));
        }
    }
    let network: NetworkPolicy = decode(get("network")?)?;
    network.validate()?;
    Ok(Tool {
        resource: ResourceRef {
            id: reference.id.clone(),
            revision: Some(revision.id),
        },
        executor: executor.into(),
        network,
    })
}
fn covering<'a>(
    values: &'a [Grant],
    capability: &str,
    tool: &Tool,
    tx: &PgSession,
) -> Result<Vec<&'a Grant>> {
    let mut result = Vec::new();
    for value in values {
        if grant_covers(value, capability, "tool.run@1", &tool.resource.id, tx)? {
            result.push(value);
        }
    }
    Ok(result)
}
pub fn tool_policies(
    authorizer: &mut AuthorizationService,
    limits: &ToolLimits,
    principal: &Principal,
    tool: &Tool,
    input: &Json,
    tx: &PgSession,
    now: Timestamp,
) -> Result<Vec<NetworkPolicy>> {
    sandbox::validate_input(&tool.executor, input, false)?;
    let grants = authorizer.grants(principal, tx, now)?;
    let certs = covering(&grants, "tool.use", tool, tx)?;
    let ceilings = covering(principal.ceiling(), "tool.use", tool, tx)?;
    if certs.is_empty() || ceilings.is_empty() {
        return Err(Error("tool_certificate_required"));
    }
    let private = covering(&grants, "tool.net.private", tool, tx)?;
    let private_ceilings = covering(principal.ceiling(), "tool.net.private", tool, tx)?;
    let base = tool.network.intersect(encode(limits)?.as_object()?)?;
    let mut candidates = Vec::new();
    for grant in certs {
        for ceiling in &ceilings {
            let result = (|| {
                let value = base
                    .intersect(&grant.constraints)?
                    .intersect(&ceiling.constraints)?;
                let mut public = value.clone();
                public.allow_private = false;
                candidates.push(public);
                if base.allow_private {
                    for grant in &private {
                        for ceiling in &private_ceilings {
                            let mut value = value
                                .intersect(&grant.constraints)?
                                .intersect(&ceiling.constraints)?;
                            value.allow_private = true;
                            candidates.push(value);
                        }
                    }
                }
                Ok(())
            })();
            if let Err(error) = result {
                if error != Error("network_policy_empty") {
                    return Err(error);
                }
            }
        }
    }
    let args = input.as_object()?;
    candidates.retain(|policy| {
        (|| {
            if tool.executor == "curl" {
                let target = network::validate_url(
                    args.get("url")
                        .ok_or(Error("invalid_tool_input"))?
                        .as_str()?,
                    args.get("method")
                        .map(Json::as_str)
                        .transpose()?
                        .unwrap_or("GET"),
                    policy,
                )?;
                let address = match target.host() {
                    Some(url::Host::Ipv4(v)) => Some(IpAddr::V4(v)),
                    Some(url::Host::Ipv6(v)) => Some(IpAddr::V6(v)),
                    _ => None,
                };
                if let Some(address) = address {
                    network::validate_addresses(&[address], policy)?;
                }
            } else if !policy.allows_host(
                args.get("name")
                    .ok_or(Error("invalid_tool_input"))?
                    .as_str()?,
            )? {
                return Err(Error("network_host_forbidden"));
            }
            Ok(())
        })()
        .is_ok()
    });
    if candidates.is_empty() {
        return Err(Error("network_policy_denied"));
    }
    Ok(candidates)
}

/// The domain publisher must create the ordinary private file, including its
/// quota/schema/signature rules, in the supplied recovery-backed transaction.
pub struct ToolPublication<'a> {
    pub principal: &'a Principal,
    pub job: &'a EffectJob,
    pub now: Timestamp,
    pub blob: &'a BlobRef,
    pub manifest: &'a Manifest,
}
pub struct ToolService {
    contents: GitContentStore,
    staging: PathBuf,
    runner: BubblewrapRunner,
    limits: ToolLimits,
    authority: WorkerAuthority,
    authorizer: AuthorizationService,
}
impl ToolService {
    pub fn new(
        contents: GitContentStore,
        staging: PathBuf,
        runner: BubblewrapRunner,
        limits: ToolLimits,
        authority: WorkerAuthority,
        authorizer: AuthorizationService,
    ) -> Result<Self> {
        if !staging.is_absolute()
            || limits.timeout_ms == 0
            || limits.max_response_bytes == 0
            || limits.methods.is_empty()
            || limits.ports.is_empty()
        {
            return Err(Error("invalid_tool_runner_config"));
        }
        Ok(Self {
            contents,
            staging,
            runner,
            limits,
            authority,
            authorizer,
        })
    }
    fn check(
        &mut self,
        tx: &PgSession,
        job: &EffectJob,
        now: Timestamp,
        tool: &Tool,
        input: &Json,
    ) -> Result<(Principal, Vec<NetworkPolicy>)> {
        let principal =
            self.authority
                .current(&job.id, job.attempts, &mut self.authorizer, tx, now)?;
        self.authorizer.require(
            &principal,
            Entry::Worker,
            now,
            &[AccessRequirement {
                resource_id: tool.resource.id.clone(),
                operation: "tool.run@1".into(),
                check: Check::ToolUse,
            }],
            None,
            tx,
        )?;
        let policies = tool_policies(
            &mut self.authorizer,
            &self.limits,
            &principal,
            tool,
            input,
            tx,
            now,
        )?;
        Ok((principal, policies))
    }
    pub fn execute(
        &mut self,
        context: &mut AttemptContext<'_, '_>,
        publisher: impl FnOnce(
            &mut ContentTransaction<'_, &PgSession>,
            ToolPublication<'_>,
        ) -> Result<ResourceRef>,
    ) -> EffectResult<EffectOutcome> {
        if context.job().operation != "tool.run" {
            return Err(Error("unknown_operation").into());
        }
        let directory = Directory::new(&self.staging, "tool")?;
        let stop = Stop::new(context.stop_flag());
        let (tool, input, policies, body) = context.read(|tx, job, now| {
            let reference: ResourceRef = decode(
                job.arguments
                    .get("tool")
                    .ok_or(Error("invalid_job_arguments"))?,
            )?;
            if reference.revision.is_none() {
                return Err(Error("source_revision_required"));
            }
            let tool = read_tool(&self.contents, tx, &reference)?;
            let input = job
                .arguments
                .get("input")
                .ok_or(Error("invalid_job_arguments"))?
                .clone();
            let (principal, live) = self.check(tx, job, now, &tool, &input)?;
            let captured: Vec<NetworkPolicy> = decode(
                job.arguments
                    .get("policies")
                    .ok_or(Error("invalid_job_arguments"))?,
            )?;
            let mut candidates = Vec::new();
            for current in live {
                for captured in &captured {
                    match current.captured(captured) {
                        Ok(policy) => candidates.push(policy),
                        Err(Error("network_policy_empty")) => {}
                        Err(error) => return Err(error),
                    }
                }
            }
            if candidates.is_empty() {
                return Err(Error("network_policy_denied"));
            }
            let body = if let Some(reference) = input.as_object()?.get("body_ref") {
                let reference: ResourceRef = decode(reference)?;
                self.authorizer.require(
                    &principal,
                    Entry::Worker,
                    now,
                    &[AccessRequirement {
                        resource_id: reference.id.clone(),
                        operation: "tool.run@1".into(),
                        check: Check::Read,
                    }],
                    None,
                    tx,
                )?;
                let revision = tx.revision(&reference)?;
                let path = directory.path().join("request-body");
                let mut output = directory.create("request-body")?;
                self.contents
                    .read_to(&revision.content, None, &mut output)?;
                output.sync_all().map_err(|_| Error("staging_io_error"))?;
                Some(path)
            } else {
                None
            };
            Ok((tool, input, candidates, body))
        })?;
        let max_bytes = policies
            .iter()
            .map(|p| p.max_response_bytes)
            .max()
            .ok_or(Error("network_policy_denied"))?;
        let result = self.runner.execute(
            &tool.executor,
            input.clone(),
            policies,
            body.as_deref(),
            &directory,
            &stop,
        )?;
        let contents = self.contents.clone();
        let published = context.publish_content(&contents,|unit,job,now| {
            let tx = *unit.metadata();
            let (principal,_) = self.check(tx,job,now,&tool,&input)?;
            let mut source = process::open(&result.path)?;
            let size = source.metadata().map_err(|_|Error("invalid_tool_output_path"))?.len();
            if size > max_bytes { return Err(Error("tool_response_too_large")); }
            let blob = unit.put(&mut source,&result.manifest.media_type,None,max_bytes)?;
            unit.pin(&blob,&format!("job:{}",job.id))?;
            let reference = publisher(unit,ToolPublication { principal:&principal,job,now,blob:&blob,manifest:&result.manifest })?;
            let resource = tx.resource_record(&reference.id)?;
            let subject = principal.subject().ok_or(Error("authentication_required"))?;
            // Do not let a publisher accidentally expose a result or return a
            // resource that was not created in this transaction for this subject.
            if resource.resource_type != "file" || resource.owner != subject || resource.mode != 0o600 || resource.revision != reference.revision || reference.revision.is_none() { return Err(Error("invalid_tool_output")); }
            let revision = tx.revision(&reference)?;
            if revision.content.digest != blob.digest || revision.content.size != blob.size { return Err(Error("invalid_tool_output")); }
            tx.set_setting(&format!("tool_output:{}",resource.id),&encode(&serde_json::json!({"subject":subject,"tool_id":tool.resource.id,"job_id":job.id}))?)?;
            job.result = Some(reference);
            encode(&serde_json::json!({"code":"ok","size":blob.size,"digest":blob.digest,"media_type":blob.media_type,"metadata":result.manifest.metadata}))
        });
        // Once the helper returned, an authorization or publication failure is
        // not evidence that an HTTP side effect did not happen.
        published.map_err(|_| crate::EffectFailure::from(Error("external_uncertain")))?;
        Ok(EffectOutcome::Published)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn concurrency_remains_tight_for_old_definitions() {
        assert_eq!(
            declared_concurrency(&Json::parse("{}").unwrap()).unwrap(),
            1
        );
        for value in ["true", "0", "33", "1.0", "-1"] {
            assert!(declared_concurrency(
                &Json::parse(&format!("{{\"concurrency\":{value}}}")).unwrap()
            )
            .is_err());
        }
        assert_eq!(
            declared_concurrency(&Json::parse(r#"{"concurrency":32}"#).unwrap()).unwrap(),
            32
        );
    }
}
