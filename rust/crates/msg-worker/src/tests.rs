use super::*;
use std::cell::RefCell;

struct Memory {
    jobs: RefCell<BTreeMap<String, EffectJob>>,
    statuses: RefCell<BTreeMap<String, Json>>,
}
impl Memory {
    fn new(jobs: Vec<EffectJob>) -> Self {
        Self {
            jobs: RefCell::new(jobs.into_iter().map(|j| (j.id.clone(), j)).collect()),
            statuses: RefCell::new(BTreeMap::new()),
        }
    }
}
impl JobStore for Memory {
    fn job(&self, id: &str) -> Result<EffectJob> {
        self.jobs
            .borrow()
            .get(id)
            .cloned()
            .ok_or(Error("job_not_found"))
    }
    fn job_page(&self, scan: JobScan, after: Option<&JobCursor>) -> Result<Vec<EffectJob>> {
        let mut jobs = self
            .jobs
            .borrow()
            .values()
            .filter(|job| match scan {
                JobScan::Running => job.state == "running",
                JobScan::PendingDue(now) => job.state == "pending" && job.next_attempt_at <= now,
            })
            .filter(|job| {
                after.is_none_or(|c| {
                    (job.next_attempt_at, job.id.as_str()) > (c.next_at, c.id.as_str())
                })
            })
            .cloned()
            .collect::<Vec<_>>();
        jobs.sort_by(|a, b| (a.next_attempt_at, &a.id).cmp(&(b.next_attempt_at, &b.id)));
        jobs.truncate(PAGE_SIZE);
        Ok(jobs)
    }
    fn save_job_if_attempt(&self, job: &EffectJob, state: &str, attempt: i64) -> Result<bool> {
        let mut jobs = self.jobs.borrow_mut();
        let current = jobs.get_mut(&job.id).ok_or(Error("job_not_found"))?;
        if current.state != state || current.attempts != attempt {
            return Ok(false);
        }
        *current = job.clone();
        Ok(true)
    }
    fn job_status(&self, id: &str, status: &Json) -> Result<()> {
        self.statuses.borrow_mut().insert(id.into(), status.clone());
        Ok(())
    }
}
fn fixture(id: &str, kind: &str, state: &str) -> EffectJob {
    let mut job: EffectJob = decode(&Json::parse(r#"{"id":"job_test","event_id":"evt_test","kind":"mail","dedupe_key":"x","principal":{},"operation":"communication.send","arguments":{},"state":"pending","attempts":0,"next_attempt_at":"2026-01-01T00:00:00Z","lease_until":null}"#).unwrap()).unwrap();
    job.id = id.into();
    job.kind = kind.into();
    job.state = state.into();
    if kind == "tool" {
        job.arguments.insert(
            "tool".into(),
            Json::parse(r#"{"id":"tool_http","revision":"rev_1"}"#).unwrap(),
        );
    }
    if state == "running" {
        job.attempts = 1;
        job.lease_until = Some(advance(job.next_attempt_at, 120).unwrap());
    }
    job
}

#[test]
fn shutdown_during_policy_read_never_claims_or_starts_an_attempt() {
    let job = fixture("job_1", "tool", "pending");
    let now = job.next_attempt_at;
    let store = Memory::new(vec![job]);
    let stopping = AtomicBool::new(false);
    let result = claim(&store, now, 120, &stopping, |_| {
        stopping.store(true, Ordering::Release);
        Ok(1)
    })
    .unwrap();
    assert!(matches!(result, Claim::None));
    assert_eq!(store.job("job_1").unwrap().attempts, 0);
    assert_eq!(store.job("job_1").unwrap().state, "pending");
}
#[test]
fn orphaned_external_work_is_never_requeued_but_websub_hints_are() {
    for (kind, expected) in [
        ("mail", "uncertain"),
        ("webhook", "uncertain"),
        ("git.push", "uncertain"),
        ("websub", "pending"),
    ] {
        let mut job = fixture("job_1", kind, "running");
        let now = job.lease_until.unwrap();
        job.lease_until = Some(now);
        let store = Memory::new(vec![job]);
        let result = claim(&store, now, 120, &AtomicBool::new(false), |_| {
            panic!("not a tool")
        })
        .unwrap();
        assert!(matches!(result, Claim::Swept));
        let current = store.job("job_1").unwrap();
        assert_eq!(current.state, expected);
        assert!(current.lease_until.is_none());
        if kind == "websub" {
            assert!(current.next_attempt_at == advance(now, 30).unwrap());
        }
    }
}
#[test]
fn tool_limit_counts_other_workers_before_claiming_due_jobs() {
    let running = fixture("job_running", "tool", "running");
    let mut blocked = fixture("job_1", "tool", "pending");
    blocked.arguments.insert(
        "tool".into(),
        Json::parse(r#"{"id":"tool_http","revision":"rev_2"}"#).unwrap(),
    );
    let mail = fixture("job_2", "mail", "pending");
    let now = mail.next_attempt_at;
    let store = Memory::new(vec![running, blocked, mail]);
    let result = claim(&store, now, 120, &AtomicBool::new(false), |reference| {
        assert_eq!(reference.revision.as_deref(), Some("rev_2"));
        Ok(1)
    })
    .unwrap();
    let Claim::Job(job) = result else {
        panic!("a different effect must remain claimable")
    };
    assert_eq!(job.id, "job_2");
    assert_eq!(job.attempts, 1);
    assert_eq!(store.job("job_1").unwrap().state, "pending");
}
#[test]
fn scans_continue_past_one_page_before_claiming() {
    let mut jobs = (0..65)
        .map(|i| fixture(&format!("job_running_{i:03}"), "mail", "running"))
        .collect::<Vec<_>>();
    let now = jobs[0].next_attempt_at;
    jobs[64].lease_until = None;
    jobs.push(fixture("job_pending", "mail", "pending"));
    let store = Memory::new(jobs);
    assert!(matches!(
        claim(
            &store,
            now,
            120,
            &AtomicBool::new(false),
            |_| unreachable!()
        )
        .unwrap(),
        Claim::Swept
    ));
    assert_eq!(store.job("job_running_064").unwrap().state, "uncertain");
    assert_eq!(store.job("job_pending").unwrap().state, "pending");
}
#[test]
fn exhausted_websub_and_live_retry_are_bounded() {
    let mut job = fixture("job_1", "websub", "running");
    job.attempts = 8;
    let now = job.lease_until.unwrap();
    let store = Memory::new(vec![job]);
    claim(
        &store,
        now,
        120,
        &AtomicBool::new(false),
        |_| unreachable!(),
    )
    .unwrap();
    assert_eq!(store.job("job_1").unwrap().state, "failed");
    assert!(store.statuses.borrow()["job_1"] == jobs::status("websub_attempts_exhausted"));
    let job = fixture("job_2", "webhook", "running");
    let now = job.next_attempt_at;
    let store = Memory::new(vec![job.clone()]);
    complete(
        &store,
        &job,
        now,
        EffectOutcome::Retry {
            code: "retry",
            exhausted: "exhausted",
        },
    )
    .unwrap();
    let current = store.job("job_2").unwrap();
    assert_eq!(current.state, "pending");
    assert!(current.next_attempt_at == advance(now, 30).unwrap());
    complete(&store, &job, now, EffectOutcome::Done(jobs::status("late"))).unwrap();
    assert_eq!(store.job("job_2").unwrap().state, "pending");
}
#[test]
fn queued_contract_version_rejects_boolean_zero_and_fraction() {
    let mut job = fixture("job_1", "mail", "pending");
    assert_eq!(contract_version(&job).unwrap(), 1);
    for raw in ["true", "0", "1.0", "-1", "4294967296"] {
        job.arguments
            .insert("contract_version".into(), Json::parse(raw).unwrap());
        assert_eq!(
            contract_version(&job),
            Err(Error("invalid_job_contract_version"))
        );
    }
}
