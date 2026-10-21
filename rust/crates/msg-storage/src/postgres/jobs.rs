use super::PgSession;
use crate::{
    jobs::{JobCursor, JobScan, JobStore, PAGE_SIZE},
    records::{encode, EffectJob},
};
use msg_core::{Error, Json, Result};
use msg_identity::{
    models::Timestamp,
    store::AuthorityStore,
    worker_authority::{ArbitrationAuthorityStore, WorkerAuthorityStore},
};

fn time(value: Timestamp) -> String {
    value.0.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()
}

impl JobStore for PgSession {
    fn job(&self, id: &str) -> Result<EffectJob> {
        PgSession::job(self, id)
    }

    fn job_page(&self, scan: JobScan, after: Option<&JobCursor>) -> Result<Vec<EffectJob>> {
        let cursor = after
            .map(|c| time(c.next_at))
            .unwrap_or_else(|| "0001-01-01T00:00:00.000000Z".into());
        let id = after.map(|c| c.id.as_str()).unwrap_or("");
        // Paging IDs first keeps individual 1 MiB jobs below the aggregate SQL
        // limit; each full row is validated by the existing typed reader.
        let rows = match scan {
            JobScan::Running => self.rows("SELECT id,kind,dedupe FROM jobs WHERE state='running' AND (next_at,id)>($1,$2) ORDER BY next_at,id LIMIT $3", &[cursor.into(), id.into(), (PAGE_SIZE as i64).into()])?,
            JobScan::PendingDue(now) => self.rows("SELECT id,kind,dedupe FROM jobs WHERE state='pending' AND next_at<=$1 AND (next_at,id)>($2,$3) ORDER BY next_at,id LIMIT $4", &[time(now).into(), cursor.into(), id.into(), (PAGE_SIZE as i64).into()])?,
        };
        rows.iter()
            .map(|row| {
                let job = PgSession::job(self, row.text(0)?)?;
                if job.kind != row.text(1)? || job.dedupe_key != row.text(2)? {
                    return Err(Error("storage_record_mismatch"));
                }
                Ok(job)
            })
            .collect()
    }

    fn save_job_if_attempt(&self, job: &EffectJob, state: &str, attempt: i64) -> Result<bool> {
        self.require_write()?;
        let result = (|| {
            if job.attempts < 0
                || attempt < 0
                || !matches!(
                    job.state.as_str(),
                    "pending" | "running" | "done" | "failed" | "uncertain"
                )
            {
                return Err(Error("invalid_job_state"));
            }
            Ok(self.execute("UPDATE jobs SET state=$1,next_at=$2,body=$3 WHERE id=$4 AND state=$5 AND (body::jsonb->>'attempts')::bigint=$6", &[job.state.as_str().into(), time(job.next_attempt_at).into(), encode(job)?.canonical()?.into(), job.id.as_str().into(), state.into(), attempt.into()])? == 1)
        })();
        self.capture(result, true)
    }

    fn job_status(&self, id: &str, status: &Json) -> Result<()> {
        self.set_setting(&format!("job_status:{id}"), status)
    }
}

impl WorkerAuthorityStore for PgSession {
    fn authority(&self) -> &dyn AuthorityStore {
        self
    }
    fn worker_snapshot(&self, id: &str, attempt: i64, now: Timestamp) -> Result<Json> {
        let job = self.job(id)?;
        if job.state != "running"
            || job.attempts != attempt
            || job.lease_until.is_none_or(|until| until <= now)
        {
            return Err(Error("job_lease_lost"));
        }
        Ok(job.principal_snapshot().clone())
    }
}

impl ArbitrationAuthorityStore for PgSession {
    fn authority(&self) -> &dyn AuthorityStore {
        self
    }
    fn vote_snapshot(&self, case: &str, round: u32, arbitrator: &str) -> Result<Json> {
        let row = self.one("SELECT body FROM arbitration_votes WHERE case_id=$1 AND round=$2 AND arbitrator=$3",&[case.into(),round.into(),arbitrator.into()])?.ok_or(Error("decision_quorum_invalid"))?;
        let value = row.json(0)?;
        let fields = value.as_object()?;
        if fields.get("arbitrator").and_then(|v| v.as_str().ok()) != Some(arbitrator) {
            return Err(Error("decision_member_invalid"));
        }
        let proposal = fields
            .get("proposal")
            .ok_or(Error("decision_context_mismatch"))?
            .as_object()?;
        if proposal.get("case_id").and_then(|v| v.as_str().ok()) != Some(case)
            || proposal
                .get("round")
                .and_then(|v| v.as_integer().ok())
                .and_then(|v| v.parse::<u32>().ok())
                != Some(round)
        {
            return Err(Error("decision_context_mismatch"));
        }
        fields
            .get("principal")
            .cloned()
            .ok_or(Error("decision_member_invalid"))
    }
}
