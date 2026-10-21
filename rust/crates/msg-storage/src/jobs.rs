//! Durable job transitions. Call inside the same fenced write transaction as
//! the protected domain update; a stale callback never owns a newer attempt.
use crate::records::EffectJob;
use msg_core::{Error, Json, Result};
use msg_identity::models::Timestamp;

pub const PAGE_SIZE: usize = 64;

#[derive(Clone)]
pub struct JobCursor {
    pub next_at: Timestamp,
    pub id: String,
}
impl From<&EffectJob> for JobCursor {
    fn from(job: &EffectJob) -> Self {
        Self {
            next_at: job.next_attempt_at,
            id: job.id.clone(),
        }
    }
}
#[derive(Clone, Copy)]
pub enum JobScan {
    Running,
    PendingDue(Timestamp),
}

pub trait JobStore {
    fn job(&self, id: &str) -> Result<EffectJob>;
    fn job_page(&self, scan: JobScan, after: Option<&JobCursor>) -> Result<Vec<EffectJob>>;
    fn save_job_if_attempt(&self, job: &EffectJob, state: &str, attempt: i64) -> Result<bool>;
    fn job_status(&self, id: &str, status: &Json) -> Result<()>;
}

pub fn status(code: &str) -> Json {
    Json::from_object(
        [("code".to_owned(), Json::string(code))]
            .into_iter()
            .collect(),
    )
}

pub fn current_attempt(
    store: &dyn JobStore,
    captured: &EffectJob,
    now: Timestamp,
) -> Result<Option<EffectJob>> {
    let mut current = store.job(&captured.id)?;
    if current.state != "running" || current.attempts != captured.attempts {
        return Ok(None);
    }
    if current.lease_until.is_none_or(|until| until <= now) {
        current.state = "uncertain".into();
        current.lease_until = None;
        if store.save_job_if_attempt(&current, "running", captured.attempts)? {
            store.job_status(&current.id, &status("expired_execution_lease"))?;
        }
        return Ok(None);
    }
    Ok(Some(current))
}

pub fn finish(
    store: &dyn JobStore,
    captured: &EffectJob,
    now: Timestamp,
    state: &str,
    details: &Json,
) -> Result<bool> {
    if !matches!(state, "done" | "failed" | "uncertain") {
        return Err(Error("invalid_job_state"));
    }
    let Some(mut current) = current_attempt(store, captured, now)? else {
        return Ok(false);
    };
    current.state = state.into();
    current.lease_until = None;
    if !store.save_job_if_attempt(&current, "running", captured.attempts)? {
        return Ok(false);
    }
    store.job_status(&current.id, details)?;
    Ok(true)
}

/// Backoff is capped before shifting, including corrupt/historical attempts.
pub fn retry_seconds(attempt: i64) -> i64 {
    30_i64
        .saturating_mul(1_i64 << attempt.saturating_sub(1).clamp(0, 7))
        .min(3600)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::records::decode;
    use std::cell::RefCell;
    struct Memory {
        job: RefCell<EffectJob>,
        status: RefCell<Option<Json>>,
    }
    impl JobStore for Memory {
        fn job(&self, _: &str) -> Result<EffectJob> {
            Ok(self.job.borrow().clone())
        }
        fn job_page(&self, _: JobScan, _: Option<&JobCursor>) -> Result<Vec<EffectJob>> {
            unreachable!()
        }
        fn save_job_if_attempt(&self, job: &EffectJob, state: &str, attempt: i64) -> Result<bool> {
            let mut current = self.job.borrow_mut();
            if current.state != state || current.attempts != attempt {
                return Ok(false);
            }
            *current = job.clone();
            Ok(true)
        }
        fn job_status(&self, _: &str, status: &Json) -> Result<()> {
            *self.status.borrow_mut() = Some(status.clone());
            Ok(())
        }
    }
    fn fixture() -> EffectJob {
        decode(&Json::parse(r#"{"id":"job_test","event_id":"evt_test","kind":"mail","dedupe_key":"x","principal":{},"operation":"communication.send","arguments":{},"state":"running","attempts":1,"next_attempt_at":"2026-01-01T00:00:00Z","lease_until":"2026-01-01T00:02:00Z"}"#).unwrap()).unwrap()
    }
    #[test]
    fn an_old_completion_cannot_change_a_new_attempt_or_status() {
        let old = fixture();
        let mut newer = old.clone();
        newer.attempts = 2;
        let store = Memory {
            job: RefCell::new(newer),
            status: RefCell::new(Some(status("newer"))),
        };
        assert!(!finish(&store, &old, old.next_attempt_at, "done", &status("ok")).unwrap());
        assert_eq!(store.job.borrow().attempts, 2);
        assert!(store.status.borrow().as_ref() == Some(&status("newer")));
    }
    #[test]
    fn an_expired_result_is_uncertain_even_without_a_sweeper() {
        let old = fixture();
        let store = Memory {
            job: RefCell::new(old.clone()),
            status: RefCell::new(None),
        };
        assert!(!finish(
            &store,
            &old,
            old.lease_until.unwrap(),
            "done",
            &status("ok")
        )
        .unwrap());
        assert_eq!(store.job.borrow().state, "uncertain");
        assert!(store.status.borrow().as_ref() == Some(&status("expired_execution_lease")));
    }
    #[test]
    fn acknowledged_result_is_terminal() {
        let old = fixture();
        let store = Memory {
            job: RefCell::new(old.clone()),
            status: RefCell::new(None),
        };
        assert!(finish(&store, &old, old.next_attempt_at, "done", &status("sent")).unwrap());
        assert_eq!(store.job.borrow().state, "done");
        assert!(store.job.borrow().lease_until.is_none());
        assert!(!finish(&store, &old, old.next_attempt_at, "failed", &status("late")).unwrap());
    }
    #[test]
    fn retry_delay_does_not_overflow() {
        assert_eq!(
            [1, 2, 7, 8, i64::MAX].map(retry_seconds),
            [30, 60, 1920, 3600, 3600]
        );
    }
}
