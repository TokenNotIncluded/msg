//! Local bundle import and atomic compare-and-swap ref updates. Public
//! repositories never share the internal content store's bare repository.
use crate::{
    process::{self, Directory, Stop},
    AttemptContext, EffectOutcome, EffectResult,
};
use msg_core::{Error, Json, Result};
use msg_identity::{
    authentication::{Entry, Principal},
    authorization::{AccessRequirement, AuthorizationService, Check},
    models::ResourceRef,
    store::{truthy, AuthorityStore},
    worker_authority::WorkerAuthority,
};
use msg_storage::{
    content::GitContentStore,
    postgres::PgSession,
    records::{decode, encode, EffectJob},
};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

const REPOSITORY_BYTES: u64 = 2 * 1024 * 1024 * 1024;
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RefChange {
    #[serde(rename = "ref")]
    pub reference: String,
    pub old: Option<String>,
    pub new: Option<String>,
}
pub struct GitPushService {
    pub contents: GitContentStore,
    root: PathBuf,
    staging: PathBuf,
    git: PathBuf,
    authority: WorkerAuthority,
    authorizer: AuthorizationService,
}
impl GitPushService {
    pub fn new(
        contents: GitContentStore,
        root: PathBuf,
        staging: PathBuf,
        git: PathBuf,
        authority: WorkerAuthority,
        authorizer: AuthorizationService,
    ) -> Result<Self> {
        if !root.is_absolute() || !staging.is_absolute() || !git.is_absolute() {
            return Err(Error("invalid_git_configuration"));
        }
        Ok(Self {
            contents,
            root,
            staging,
            git,
            authority,
            authorizer,
        })
    }
    fn path(&self, id: &str) -> Result<PathBuf> {
        if id.is_empty()
            || id.len() > 128
            || !id
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-'))
        {
            return Err(Error("invalid_repository_id"));
        }
        let path = self.root.join(format!("{id}.git"));
        for path in [&self.root, &path] {
            let metadata = fs::symlink_metadata(path).map_err(|_| Error("repository_missing"))?;
            if !metadata.is_dir() || metadata.file_type().is_symlink() {
                return Err(Error("repository_missing"));
            }
        }
        Ok(path)
    }
    fn command(&self, repo: &Path) -> Command {
        let mut command = Command::new(&self.git);
        command
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .env("LANG", "C.UTF-8")
            .env("HOME", "/nonexistent")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_NO_REPLACE_OBJECTS", "1")
            .env("GIT_TERMINAL_PROMPT", "0")
            .env("GIT_COMMITTER_NAME", "msg")
            .env("GIT_COMMITTER_EMAIL", "msg@localhost")
            .arg("--git-dir")
            .arg(repo)
            .args([
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsync=all",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "protocol.file.allow=always",
            ]);
        command
    }
    fn run(
        &self,
        repo: &Path,
        args: &[&str],
        bytes: Option<&[u8]>,
        directory: &Directory,
        stop: &Stop<'_>,
    ) -> Result<Vec<u8>> {
        let sequence = process::id("git")?;
        let input = bytes
            .map(|bytes| process::packet_file(directory, &format!("{sequence}.in"), bytes))
            .transpose()?;
        let output = directory.create(&format!("{sequence}.out"))?;
        let (status, output) = process::run(
            self.command(repo).args(args),
            input,
            output,
            Duration::from_secs(120),
            1_048_576,
            stop,
        )?;
        if !status.success() {
            return Err(Error("git_operation_failed"));
        }
        Ok(output)
    }
    fn check(
        &mut self,
        tx: &PgSession,
        job: &EffectJob,
        now: msg_identity::models::Timestamp,
        source: Option<&ResourceRef>,
    ) -> Result<Principal> {
        let principal =
            self.authority
                .current(&job.id, job.attempts, &mut self.authorizer, tx, now)?;
        let id = argument(job, "id")?.as_str()?;
        let mut checks = vec![AccessRequirement {
            resource_id: id.into(),
            operation: "git.push@1".into(),
            check: Check::Write,
        }];
        if let Some(source) = source {
            checks.push(AccessRequirement {
                resource_id: source.id.clone(),
                operation: "git.push@1".into(),
                check: Check::Read,
            });
        }
        self.authorizer
            .require(&principal, Entry::Worker, now, &checks, None, tx)?;
        if tx.resource_record(id)?.resource_type != "repo" {
            return Err(Error("not_a_repository"));
        }
        Ok(principal)
    }
    pub fn execute(&mut self, context: &mut AttemptContext<'_, '_>) -> EffectResult<EffectOutcome> {
        let job = context.job().clone();
        if job.operation != "git.push" {
            return Err(Error("unknown_operation").into());
        }
        let source: ResourceRef = decode(argument(&job, "bundle")?)?;
        if source.revision.is_none() {
            return Err(Error("source_revision_required").into());
        }
        let changes: Vec<RefChange> = decode(argument(&job, "changes")?)?;
        validate_changes(&changes)?;
        let force = job
            .arguments
            .get("force")
            .map(Json::as_bool)
            .transpose()?
            .unwrap_or(false);
        let repo = self.path(argument(&job, "id")?.as_str()?)?;
        let directory = Directory::new(&self.staging, "git-push")?;
        let bundle_path = directory.path().join("input.bundle");
        context.read(|tx, job, now| {
            self.check(tx, job, now, Some(&source))?;
            let revision = tx.revision(&source)?;
            let mut output = directory.create("input.bundle")?;
            if revision.content.size < 0 || revision.content.size as u64 > REPOSITORY_BYTES {
                return Err(Error("storage_capacity_exceeded"));
            }
            self.contents
                .read_to(&revision.content, None, &mut output)?;
            Ok(())
        })?;
        let size = fs::metadata(&bundle_path)
            .map_err(|_| Error("staging_io_error"))?
            .len();
        if repository_bytes(&repo)?
            .checked_add(size)
            .is_none_or(|n| n > REPOSITORY_BYTES)
        {
            return Err(Error("storage_capacity_exceeded").into());
        }
        let bundle = bundle_path
            .to_str()
            .ok_or(Error("invalid_staging_directory"))?;
        let stop = Stop::new(context.stop_flag());
        self.run(
            &repo,
            &["bundle", "verify", bundle],
            None,
            &directory,
            &stop,
        )?;
        self.run(
            &repo,
            &["bundle", "unbundle", bundle],
            None,
            &directory,
            &stop,
        )?;
        self.run(
            &repo,
            &["fsck", "--no-reflogs", "--connectivity-only"],
            None,
            &directory,
            &stop,
        )?;
        for change in &changes {
            self.run(
                &repo,
                &["check-ref-format", &change.reference],
                None,
                &directory,
                &stop,
            )?;
            if let Some(new) = &change.new {
                let suffix = if change.reference.starts_with("refs/heads/") {
                    "^{commit}"
                } else {
                    "^{object}"
                };
                self.run(
                    &repo,
                    &["rev-parse", "--verify", &format!("{new}{suffix}")],
                    None,
                    &directory,
                    &stop,
                )?;
            }
            if !force && change.reference.starts_with("refs/heads/") {
                if let (Some(old), Some(new)) = (&change.old, &change.new) {
                    self.run(
                        &repo,
                        &["merge-base", "--is-ancestor", old, new],
                        None,
                        &directory,
                        &stop,
                    )?;
                }
            }
        }
        let details = encode(&serde_json::json!({"code":"ok","refs":changes}))?;
        let mut moved = false;
        let result = context.publish(&details, |tx, current, now| {
            let principal = self.check(tx, current, now, None)?;
            if let Some(config) = tx.setting("runtime_config")? {
                if let Some(enabled) = config.as_object()?.get("accept_writes") {
                    if !truthy(enabled)? {
                        return Err(Error("writes_paused"));
                    }
                }
            }
            let commands = ref_commands(&changes)?;
            // Keep the metadata writer fence while refs move. A crash between
            // Git commit and SQL commit is reported as uncertain, never retried.
            moved = true;
            self.run(
                &repo,
                &["update-ref", "--stdin"],
                Some(commands.as_bytes()),
                &directory,
                &stop,
            )?;
            let mut resource = tx.resource_record(argument(current, "id")?.as_str()?)?;
            let expected = resource.generation;
            resource.generation = resource
                .generation
                .checked_add(1)
                .ok_or(Error("invalid_generation"))?;
            resource.modified_at = now;
            resource.modified_by = principal
                .actor()
                .ok_or(Error("worker_identity_required"))?
                .into();
            tx.replace_resource(&resource, expected)?;
            Ok(())
        });
        match result {
            Ok(_) => Ok(EffectOutcome::Published),
            Err(error) if error.code() == "worker_stopping" => Err(Error("worker_stopping").into()),
            Err(_) if moved => Err(Error("external_uncertain").into()),
            Err(error) => Err(error.into()),
        }
    }
}
fn argument<'a>(job: &'a EffectJob, key: &str) -> Result<&'a Json> {
    job.arguments.get(key).ok_or(Error("invalid_job_arguments"))
}
fn oid(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}
fn validate_changes(changes: &[RefChange]) -> Result<()> {
    if changes.is_empty() || changes.len() > 128 {
        return Err(Error("invalid_git_change"));
    }
    let mut seen = BTreeSet::new();
    for change in changes {
        if !seen.insert(&change.reference) {
            return Err(Error("duplicate_git_ref"));
        }
        if !(change.reference.starts_with("refs/heads/")
            || change.reference.starts_with("refs/tags/"))
            || change.reference.len() > 1024
            || change
                .reference
                .chars()
                .any(|c| c.is_whitespace() || c.is_control())
            || change.old.as_deref().is_some_and(|v| !oid(v))
            || change.new.as_deref().is_some_and(|v| !oid(v))
            || (change.old.is_none() && change.new.is_none())
        {
            return Err(Error("invalid_git_change"));
        }
    }
    Ok(())
}
fn ref_commands(changes: &[RefChange]) -> Result<String> {
    validate_changes(changes)?;
    let mut lines = vec!["start".into()];
    for change in changes {
        lines.push(match &change.new {
            Some(new) => format!(
                "update {} {new} {}",
                change.reference,
                change
                    .old
                    .as_deref()
                    .unwrap_or("0000000000000000000000000000000000000000")
            ),
            None => format!(
                "delete {} {}",
                change.reference,
                change.old.as_deref().ok_or(Error("invalid_git_change"))?
            ),
        });
    }
    lines.extend(["prepare".into(), "commit".into(), String::new()]);
    Ok(lines.join("\n"))
}
fn repository_bytes(root: &Path) -> Result<u64> {
    let mut pending = vec![root.to_owned()];
    let mut total = 0_u64;
    while let Some(path) = pending.pop() {
        for entry in fs::read_dir(path).map_err(|_| Error("repository_missing"))? {
            let entry = entry.map_err(|_| Error("repository_missing"))?;
            let metadata =
                fs::symlink_metadata(entry.path()).map_err(|_| Error("repository_missing"))?;
            if metadata.is_dir() && !metadata.file_type().is_symlink() {
                pending.push(entry.path());
            } else {
                total = total
                    .checked_add(metadata.len())
                    .ok_or(Error("storage_capacity_exceeded"))?;
                if total > REPOSITORY_BYTES {
                    return Err(Error("storage_capacity_exceeded"));
                }
            }
        }
    }
    Ok(total)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ref_updates_have_expected_old_ids_and_one_atomic_commit() {
        let changes = vec![
            RefChange {
                reference: "refs/heads/main".into(),
                old: None,
                new: Some("a".repeat(40)),
            },
            RefChange {
                reference: "refs/tags/old".into(),
                old: Some("b".repeat(40)),
                new: None,
            },
        ];
        let command = ref_commands(&changes).unwrap();
        assert!(command.starts_with("start\n"));
        assert!(command.ends_with("prepare\ncommit\n"));
        assert!(command.contains(&"0".repeat(40)));
        assert!(command.contains(&format!("delete refs/tags/old {}", "b".repeat(40))));
    }
    #[test]
    fn queued_refs_cannot_inject_extra_git_commands() {
        let change = RefChange {
            reference: "refs/heads/main\ncommit".into(),
            old: None,
            new: Some("a".repeat(40)),
        };
        assert!(validate_changes(&[change]).is_err());
        let change = RefChange {
            reference: "refs/heads/main".into(),
            old: None,
            new: Some("a".repeat(40)),
        };
        assert!(validate_changes(&[change.clone(), change]).is_err());
    }
}
