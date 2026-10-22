//! Native durable worker. Database leases do not make an external side effect
//! exactly once; expired non-idempotent work is deliberately left uncertain.
pub mod delivery;
pub mod git;
pub mod mail;
pub mod maintenance;
pub mod network;
pub mod outbound;
mod process;
pub mod sandbox;
pub mod tool;

use chrono::{DateTime, Duration as TimeDelta};
use msg_core::{Error, Json, Result};
use msg_identity::{
    models::{ResourceRef, Timestamp},
    store::{AuthorityStore, RuntimeGeneration},
};
use msg_storage::{
    content::{recovery::ContentTransaction, GitContentStore},
    jobs::{self, JobCursor, JobScan, JobStore, PAGE_SIZE},
    postgres::{PgFailure, PgSession, PgStore},
    records::{decode, EffectJob},
};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

pub type WorkerResult<T> = std::result::Result<T, PgFailure>;
pub type EffectResult<T> = std::result::Result<T, EffectFailure>;
pub type Clock = Arc<dyn Fn() -> Result<Timestamp> + Send + Sync>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EffectKind {
    Tool,
    Mail,
    MarketMail,
    MarketEmailVerify,
    WebSub,
    Webhook,
    GitPush,
    Maintenance,
    ResourceGc,
}
impl EffectKind {
    pub const ALL: [Self; 9] = [
        Self::Tool,
        Self::Mail,
        Self::MarketMail,
        Self::MarketEmailVerify,
        Self::WebSub,
        Self::Webhook,
        Self::GitPush,
        Self::Maintenance,
        Self::ResourceGc,
    ];
    pub fn parse(name: &str) -> Result<Self> {
        Ok(match name {
            "tool" => Self::Tool,
            "mail" => Self::Mail,
            "market_mail" => Self::MarketMail,
            "market_email_verify" => Self::MarketEmailVerify,
            "websub" => Self::WebSub,
            "webhook" => Self::Webhook,
            "git.push" => Self::GitPush,
            "maintenance" => Self::Maintenance,
            "gc.resource" => Self::ResourceGc,
            _ => return Err(Error("unknown_effect_kind")),
        })
    }
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Tool => "tool",
            Self::Mail => "mail",
            Self::MarketMail => "market_mail",
            Self::MarketEmailVerify => "market_email_verify",
            Self::WebSub => "websub",
            Self::Webhook => "webhook",
            Self::GitPush => "git.push",
            Self::Maintenance => "maintenance",
            Self::ResourceGc => "gc.resource",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Maintenance {
    CleanupExpired,
    CollectGarbage,
    DeliverDueTodos,
    RebuildSearch,
}
impl Maintenance {
    pub const SCHEDULED: [Self; 3] = [
        Self::CleanupExpired,
        Self::CollectGarbage,
        Self::DeliverDueTodos,
    ];
    pub fn parse(action: &str) -> Result<Self> {
        Ok(match action {
            "cleanup_expired" => Self::CleanupExpired,
            "collect_garbage" => Self::CollectGarbage,
            "deliver_due_todos" => Self::DeliverDueTodos,
            "rebuild_search" => Self::RebuildSearch,
            _ => return Err(Error("invalid_maintenance_action")),
        })
    }
    pub fn as_str(self) -> &'static str {
        match self {
            Self::CleanupExpired => "cleanup_expired",
            Self::CollectGarbage => "collect_garbage",
            Self::DeliverDueTodos => "deliver_due_todos",
            Self::RebuildSearch => "rebuild_search",
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct EffectFailure {
    pub error: Error,
    pub retryable: bool,
}
impl From<Error> for EffectFailure {
    fn from(error: Error) -> Self {
        Self {
            error,
            retryable: false,
        }
    }
}
impl From<PgFailure> for EffectFailure {
    fn from(error: PgFailure) -> Self {
        match error {
            PgFailure::Uncertain { .. } => Error("external_uncertain").into(),
            PgFailure::Rejected { error, .. } => error.into(),
        }
    }
}

pub enum EffectOutcome {
    Done(Json),
    Failed(&'static str),
    Uncertain(&'static str),
    Retry {
        code: &'static str,
        exhausted: &'static str,
    },
    /// The handler atomically published its result and job state through
    /// AttemptContext::publish. Returning this alone is rejected by the engine.
    Published,
}

/// Domain handlers are mandatory; this interface has no success/no-op defaults.
/// The daemon must provide the installed operation registry and real handlers.
pub trait EffectExecutor {
    fn operation(&self, name: &str, version: u32) -> Result<()>;
    fn tool_concurrency(&mut self, tx: &PgSession, tool: &ResourceRef) -> EffectResult<u32>;
    fn resolve_due(&mut self, context: &mut RuntimeContext<'_>) -> WorkerResult<bool>;
    fn resolve_cases(&mut self, context: &mut RuntimeContext<'_>) -> WorkerResult<bool>;
    fn maintenance(
        &mut self,
        action: Maintenance,
        context: &mut RuntimeContext<'_>,
    ) -> WorkerResult<()>;
    fn execute(
        &mut self,
        kind: EffectKind,
        context: &mut AttemptContext<'_, '_>,
    ) -> EffectResult<EffectOutcome>;
}

pub struct WorkerConfig {
    pub lease_seconds: i64,
    pub orders_enabled: bool,
    pub recovery_drill_marker: Option<PathBuf>,
    pub recovery_quarantine_marker: Option<PathBuf>,
}
impl Default for WorkerConfig {
    fn default() -> Self {
        Self {
            lease_seconds: 120,
            orders_enabled: false,
            recovery_drill_marker: None,
            recovery_quarantine_marker: None,
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tick {
    Processed,
    Idle,
    Stopped,
    RecoveryDrill,
    Quarantined,
}
impl Tick {
    pub fn processed(self) -> bool {
        self == Self::Processed
    }
}

pub struct Worker<E> {
    store: PgStore,
    executor: E,
    config: WorkerConfig,
    runtime: RuntimeGeneration,
    clock: Clock,
    last_maintenance: Option<Instant>,
}

fn utc_now() -> Result<Timestamp> {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| Error("invalid_clock"))?;
    let seconds = i64::try_from(now.as_secs()).map_err(|_| Error("invalid_clock"))?;
    DateTime::from_timestamp(seconds, now.subsec_micros() * 1000)
        .map(Timestamp)
        .ok_or(Error("invalid_clock"))
}
fn marker(path: Option<&Path>) -> Result<bool> {
    let Some(path) = path else {
        return Ok(false);
    };
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(true), // A symlink is a quarantine marker too.
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(_) => Err(Error("recovery_layout_error")),
    }
}

/// Transactions must enter through this runtime fence. A stale runtime is
/// permanently invalid, even if a recovery setting later returns to its value.
pub struct RuntimeContext<'a> {
    store: &'a PgStore,
    runtime: &'a mut RuntimeGeneration,
    config: &'a WorkerConfig,
    clock: &'a Clock,
    stopping: &'a AtomicBool,
}
impl RuntimeContext<'_> {
    pub fn clock(&self) -> Clock {
        Arc::clone(self.clock)
    }
    pub fn now(&self) -> Result<Timestamp> {
        (self.clock)()
    }
    pub fn stopping(&self) -> bool {
        self.stopping.load(Ordering::Acquire)
    }
    pub fn transaction<T>(
        &mut self,
        write: bool,
        body: impl FnOnce(&PgSession) -> Result<T>,
    ) -> WorkerResult<T> {
        if marker(self.config.recovery_drill_marker.as_deref())? {
            return Err(Error("recovery_drill_active").into());
        }
        if marker(self.config.recovery_quarantine_marker.as_deref())? {
            return Err(Error("recovery_quarantined").into());
        }
        self.store.transaction(write, |tx| {
            self.runtime.require_current(tx)?;
            if tx.has_setting("recovery_quarantine")? {
                return Err(Error("recovery_quarantined"));
            }
            body(tx)
        })
    }

    pub fn content_transaction<T>(
        &mut self,
        content: &GitContentStore,
        body: impl FnOnce(&mut ContentTransaction<'_, &PgSession>) -> Result<T>,
    ) -> WorkerResult<T> {
        if marker(self.config.recovery_drill_marker.as_deref())? {
            return Err(Error("recovery_drill_active").into());
        }
        if marker(self.config.recovery_quarantine_marker.as_deref())? {
            return Err(Error("recovery_quarantined").into());
        }
        self.store.content_transaction(content, |unit| {
            let tx = *unit.metadata();
            self.runtime.require_current(tx)?;
            if tx.has_setting("recovery_quarantine")? {
                return Err(Error("recovery_quarantined"));
            }
            body(unit)
        })
    }
}

pub struct AttemptContext<'a, 'b> {
    runtime: &'a mut RuntimeContext<'b>,
    job: &'a EffectJob,
    published: bool,
}
impl<'b> AttemptContext<'_, 'b> {
    pub fn stop_flag(&self) -> &'b AtomicBool {
        self.runtime.stopping
    }
    pub fn job(&self) -> &EffectJob {
        self.job
    }
    pub fn now(&self) -> Result<Timestamp> {
        self.runtime.now()
    }
    pub fn stopping(&self) -> bool {
        self.runtime.stopping()
    }
    /// Read and revalidate all effect-specific authority in this snapshot.
    pub fn read<T>(
        &mut self,
        body: impl FnOnce(&PgSession, &EffectJob, Timestamp) -> Result<T>,
    ) -> WorkerResult<T> {
        let clock = Arc::clone(self.runtime.clock);
        self.runtime.transaction(false, |tx| {
            let now = clock()?;
            let current = tx.job(&self.job.id)?;
            if current.state != "running"
                || current.attempts != self.job.attempts
                || current.lease_until.is_none_or(|until| until <= now)
            {
                return Err(Error("job_lease_lost"));
            }
            body(tx, &current, now)
        })
    }
    /// Reauthorize and publish a domain result in the same fenced transaction
    /// as the final job state. Network I/O must precede this transaction. A local
    /// Git ref CAS may run inside it, with failures conservatively uncertain.
    pub fn publish<T>(
        &mut self,
        details: &Json,
        body: impl FnOnce(&PgSession, &mut EffectJob, Timestamp) -> Result<T>,
    ) -> WorkerResult<Option<T>> {
        let clock = Arc::clone(self.runtime.clock);
        let value = self.runtime.transaction(true, |tx| {
            let now = clock()?;
            let Some(mut current) = jobs::current_attempt(tx, self.job, now)? else {
                return Ok(None);
            };
            let output = body(tx, &mut current, now)?;
            if current.attempts != self.job.attempts
                || current
                    .lease_until
                    .is_none_or(|until| clock().map_or(true, |now| until <= now))
            {
                return Err(Error("job_lease_lost"));
            }
            current.state = "done".into();
            current.lease_until = None;
            if !tx.save_job_if_attempt(&current, "running", self.job.attempts)? {
                return Err(Error("job_lease_lost"));
            }
            tx.job_status(&current.id, details)?;
            Ok(Some(output))
        })?;
        self.published = true;
        Ok(value)
    }

    pub fn publish_content(
        &mut self,
        content: &GitContentStore,
        body: impl FnOnce(
            &mut ContentTransaction<'_, &PgSession>,
            &mut EffectJob,
            Timestamp,
        ) -> Result<Json>,
    ) -> WorkerResult<bool> {
        let clock = Arc::clone(self.runtime.clock);
        let published = self.runtime.content_transaction(content, |unit| {
            let tx = *unit.metadata();
            let now = clock()?;
            let Some(mut current) = jobs::current_attempt(tx, self.job, now)? else {
                return Ok(false);
            };
            let details = body(unit, &mut current, now)?;
            if current.attempts != self.job.attempts
                || current
                    .lease_until
                    .is_none_or(|until| clock().map_or(true, |now| until <= now))
            {
                return Err(Error("job_lease_lost"));
            }
            current.state = "done".into();
            current.lease_until = None;
            if !tx.save_job_if_attempt(&current, "running", self.job.attempts)? {
                return Err(Error("job_lease_lost"));
            }
            tx.job_status(&current.id, &details)?;
            Ok(true)
        })?;
        self.published = true;
        Ok(published)
    }
}

impl<E: EffectExecutor> Worker<E> {
    pub fn new(store: PgStore, executor: E, config: WorkerConfig) -> WorkerResult<Self> {
        Self::with_clock(store, executor, config, Arc::new(utc_now))
    }
    pub fn with_clock(
        store: PgStore,
        executor: E,
        config: WorkerConfig,
        clock: Clock,
    ) -> WorkerResult<Self> {
        if !(1..=3600).contains(&config.lease_seconds) {
            return Err(Error("invalid_job_lease").into());
        }
        let runtime = store.transaction(false, |tx| RuntimeGeneration::capture(tx))?;
        Ok(Self {
            store,
            executor,
            config,
            runtime,
            clock,
            last_maintenance: None,
        })
    }
    pub fn run_once(&mut self, stopping: &AtomicBool) -> WorkerResult<Tick> {
        if stopping.load(Ordering::Acquire) {
            return Ok(Tick::Stopped);
        }
        // Check generation even while paused; it must latch stale permanently.
        self.store
            .transaction(false, |tx| self.runtime.require_current(tx))?;
        if marker(self.config.recovery_drill_marker.as_deref())? {
            return Ok(Tick::RecoveryDrill);
        }
        if marker(self.config.recovery_quarantine_marker.as_deref())?
            || self
                .store
                .transaction(false, |tx| tx.has_setting("recovery_quarantine"))?
        {
            return Ok(Tick::Quarantined);
        }
        let mut context = RuntimeContext {
            store: &self.store,
            runtime: &mut self.runtime,
            config: &self.config,
            clock: &self.clock,
            stopping,
        };
        if self
            .last_maintenance
            .is_none_or(|last| last.elapsed() >= Duration::from_secs(60))
        {
            for action in Maintenance::SCHEDULED {
                if context.stopping() {
                    return Ok(Tick::Stopped);
                }
                self.executor.maintenance(action, &mut context)?;
            }
            self.last_maintenance = Some(Instant::now());
        }
        if context.stopping() {
            return Ok(Tick::Stopped);
        }
        if self.config.orders_enabled {
            if self.executor.resolve_due(&mut context)? {
                return Ok(Tick::Processed);
            }
            if context.stopping() {
                return Ok(Tick::Stopped);
            }
            if self.executor.resolve_cases(&mut context)? {
                return Ok(Tick::Processed);
            }
        }
        if context.stopping() {
            return Ok(Tick::Stopped);
        }
        let clock = Arc::clone(&self.clock);
        let claimed = context.transaction(true, |tx| {
            claim(tx, clock()?, self.config.lease_seconds, stopping, |tool| {
                self.executor.tool_concurrency(tx, tool)
            })
        })?;
        let job = match claimed {
            Claim::None => {
                return Ok(if stopping.load(Ordering::Acquire) {
                    Tick::Stopped
                } else {
                    Tick::Idle
                })
            }
            Claim::Swept => return Ok(Tick::Processed),
            Claim::Job(job) => job,
        };
        // Stop after claiming leaves the lease intact; no new external call is
        // started during shutdown and a successor cannot blindly resend it.
        if context.stopping() {
            return Ok(Tick::Stopped);
        }
        let mut attempt = AttemptContext {
            runtime: &mut context,
            job: &job,
            published: false,
        };
        let result = (|| {
            let version = contract_version(&job)?;
            self.executor.operation(&job.operation, version)?;
            self.executor
                .execute(EffectKind::parse(&job.kind)?, &mut attempt)
        })();
        let published = attempt.published;
        let result = match result {
            Err(EffectFailure {
                error: Error("worker_stopping"),
                ..
            }) => return Ok(Tick::Stopped),
            Ok(EffectOutcome::Published) if published => return Ok(Tick::Processed),
            Ok(EffectOutcome::Published) => EffectOutcome::Uncertain("external_uncertain"),
            Ok(outcome) => outcome,
            Err(failure)
                if matches!(
                    job.kind.as_str(),
                    "mail" | "market_mail" | "market_email_verify"
                ) && failure.retryable
                    && failure.error.0 == "mail_connection_failed" =>
            {
                EffectOutcome::Retry {
                    code: "mail_connection_failed",
                    exhausted: "mail_connection_failed",
                }
            }
            Err(failure) if matches!(failure.error.0, "external_uncertain" | "job_lease_lost") => {
                EffectOutcome::Uncertain(failure.error.0)
            }
            Err(failure) => EffectOutcome::Failed(failure.error.0),
        };
        context.transaction(true, |tx| complete(tx, &job, clock()?, result))?;
        Ok(Tick::Processed)
    }
}

fn contract_version(job: &EffectJob) -> Result<u32> {
    let version = match job.arguments.get("contract_version") {
        None => 1,
        Some(value) => value
            .as_integer()
            .ok()
            .and_then(|s| s.parse::<u32>().ok())
            .filter(|v| *v > 0)
            .ok_or(Error("invalid_job_contract_version"))?,
    };
    Ok(version)
}
enum Claim {
    None,
    Swept,
    Job(EffectJob),
}
fn advance(now: Timestamp, seconds: i64) -> Result<Timestamp> {
    now.0
        .checked_add_signed(TimeDelta::seconds(seconds))
        .map(Timestamp)
        .ok_or(Error("invalid_datetime"))
}
fn claim(
    store: &dyn JobStore,
    now: Timestamp,
    lease: i64,
    stopping: &AtomicBool,
    mut concurrency: impl FnMut(&ResourceRef) -> EffectResult<u32>,
) -> Result<Claim> {
    if stopping.load(Ordering::Acquire) {
        return Ok(Claim::None);
    }
    let mut running = BTreeMap::<String, u32>::new();
    let mut cursor = None;
    loop {
        let page = store.job_page(JobScan::Running, cursor.as_ref())?;
        let last = page.last().map(JobCursor::from);
        let count = page.len();
        for mut job in page {
            if stopping.load(Ordering::Acquire) {
                return Ok(Claim::None);
            }
            if job.lease_until.is_none_or(|until| until <= now) {
                let mut code = "expired_execution_lease";
                job.state = "uncertain".into();
                job.lease_until = None;
                if job.kind == "websub" {
                    if job.attempts >= 8 {
                        job.state = "failed".into();
                        code = "websub_attempts_exhausted";
                    } else {
                        job.state = "pending".into();
                        job.next_attempt_at = advance(now, jobs::retry_seconds(job.attempts))?;
                    }
                }
                if store.save_job_if_attempt(&job, "running", job.attempts)? {
                    store.job_status(&job.id, &jobs::status(code))?;
                }
                return Ok(Claim::Swept);
            }
            if job.kind == "tool" {
                let tool: ResourceRef = decode(
                    job.arguments
                        .get("tool")
                        .ok_or(Error("invalid_job_arguments"))?,
                )?;
                let count = running.entry(tool.id).or_default();
                *count = count.checked_add(1).ok_or(Error("worker_work_limit"))?;
            }
        }
        if count < PAGE_SIZE {
            break;
        }
        cursor = last;
    }
    let mut limits = BTreeMap::new();
    cursor = None;
    loop {
        let page = store.job_page(JobScan::PendingDue(now), cursor.as_ref())?;
        let last = page.last().map(JobCursor::from);
        let count = page.len();
        for mut job in page {
            if stopping.load(Ordering::Acquire) {
                return Ok(Claim::None);
            }
            if job.kind == "tool" {
                let tool: ResourceRef = decode(
                    job.arguments
                        .get("tool")
                        .ok_or(Error("invalid_job_arguments"))?,
                )?;
                let key = (tool.id.clone(), tool.revision.clone());
                if !limits.contains_key(&key) {
                    match concurrency(&tool) {
                        Ok(limit) if (1..=32).contains(&limit) => {
                            limits.insert(key.clone(), limit);
                        }
                        result => {
                            let failure = result
                                .err()
                                .unwrap_or_else(|| Error("invalid_tool_concurrency").into());
                            if failure.retryable {
                                return Err(failure.error);
                            }
                            job.state = "failed".into();
                            job.lease_until = None;
                            if store.save_job_if_attempt(&job, "pending", job.attempts)? {
                                store.job_status(&job.id, &jobs::status(failure.error.0))?;
                            }
                            return Ok(Claim::Swept);
                        }
                    }
                }
                if running.get(&tool.id).copied().unwrap_or(0) >= limits[&key] {
                    continue;
                }
            }
            if stopping.load(Ordering::Acquire) {
                return Ok(Claim::None);
            }
            let previous = job.attempts;
            job.state = "running".into();
            job.attempts = previous.checked_add(1).ok_or(Error("invalid_job_state"))?;
            job.lease_until = Some(advance(now, lease)?);
            if store.save_job_if_attempt(&job, "pending", previous)? {
                return Ok(Claim::Job(job));
            }
        }
        if count < PAGE_SIZE {
            break;
        }
        cursor = last;
    }
    Ok(Claim::None)
}
fn complete(
    store: &dyn JobStore,
    job: &EffectJob,
    now: Timestamp,
    outcome: EffectOutcome,
) -> Result<()> {
    match outcome {
        EffectOutcome::Done(status) => {
            jobs::finish(store, job, now, "done", &status)?;
        }
        EffectOutcome::Failed(code) => {
            jobs::finish(store, job, now, "failed", &jobs::status(code))?;
        }
        EffectOutcome::Uncertain(code) => {
            jobs::finish(store, job, now, "uncertain", &jobs::status(code))?;
        }
        EffectOutcome::Retry { code, exhausted } => {
            let Some(mut current) = jobs::current_attempt(store, job, now)? else {
                return Ok(());
            };
            let exhausted_attempts = current.attempts >= 8;
            current.state = if exhausted_attempts {
                "failed"
            } else {
                "pending"
            }
            .into();
            current.lease_until = None;
            if !exhausted_attempts {
                current.next_attempt_at = advance(now, jobs::retry_seconds(current.attempts))?;
            }
            if store.save_job_if_attempt(&current, "running", job.attempts)? {
                store.job_status(
                    &current.id,
                    &jobs::status(if exhausted_attempts { exhausted } else { code }),
                )?;
            }
        }
        EffectOutcome::Published => return Err(Error("invalid_job_state")),
    }
    Ok(())
}

#[cfg(test)]
mod tests;
