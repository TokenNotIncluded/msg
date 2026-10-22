//! Effective follows are presentation preferences, never authorization grants.
use super::*;
use msg_storage::postgres::PgSession;
use std::collections::BTreeSet;

pub(crate) fn blocked(tx: &PgSession, first: Option<&str>, second: Option<&str>) -> Result<bool> {
    match (first,second) {
        (Some(first),Some(second)) if !first.is_empty()&&!second.is_empty()=>Ok(tx.one("SELECT 1 FROM dm_blocks WHERE (blocker=$1 AND blocked=$2) OR (blocker=$2 AND blocked=$1)",&[first.into(),second.into()])?.is_some()),
        _=>Ok(false),
    }
}

#[derive(Default)]
struct FollowPolicy {
    defaults: BTreeMap<String, bool>,
    incomplete: bool,
}
impl FollowPolicy {
    fn root_default(
        &mut self,
        tx: &PgSession,
        follower: &str,
        budget: &mut ReadBudget,
    ) -> Result<bool> {
        if let Some(value) = self.defaults.get(follower) {
            return Ok(*value);
        }
        self.defaults.insert(follower.into(), false);
        if follower == "u_root"
            || tx
                .setting(&format!("root_follow_optout:{follower}"))?
                .is_some_and(|v| v.as_bool() == Ok(true))
            || tx
                .one(
                    "SELECT 1 FROM resources WHERE id=$1 AND type='user' AND state='active'",
                    &[follower.into()],
                )?
                .is_none()
            || tx
                .one(
                    "SELECT 1 FROM resources WHERE id='u_root' AND type='user' AND state='active'",
                    &[],
                )?
                .is_none()
        {
            return Ok(false);
        }
        let mut size = 0_usize;
        let mut seen = 0_usize;
        let mut after = None::<String>;
        loop {
            let limit = 8_usize.min(257 - seen).min(4096 - budget.scanned);
            let remaining = 262144 - size;
            if limit == 0 || remaining == 0 {
                self.incomplete = true;
                return Ok(false);
            }
            let rows=tx.rows("SELECT request_id,CASE WHEN LENGTH(body)<=$1 THEN body ELSE NULL END FROM results WHERE subject=$2 AND ($3::text IS NULL OR request_id>$3) AND (body LIKE '%\"operation\":\"communication.unfollow\"%' OR body LIKE '%\"operation\":\"communication.unfollow@1\"%') AND body LIKE '%\"status\":\"ok\"%' AND body LIKE '%\"id\":\"u!_root\"%' ESCAPE '!' AND body LIKE '%\"following\":false%' ORDER BY request_id LIMIT $4",&[(32768_usize.min(remaining) as i64).into(),follower.into(),after.as_deref().into(),(limit as i64).into()])?;
            for _ in &rows {
                budget.scan()?;
            }
            let count = rows.len();
            for row in rows {
                let id = row.text(0)?;
                after = Some(id.into());
                seen += 1;
                let Some(raw) = row.optional_text(1)? else {
                    self.incomplete = true;
                    return Ok(false);
                };
                size += raw.len();
                if seen > 256 || raw.len() > 32768 || size > 262144 {
                    self.incomplete = true;
                    return Ok(false);
                }
                let result = match Json::parse(raw).and_then(|v| value(&v)) {
                    Ok(value) => value,
                    Err(_) => {
                        self.incomplete = true;
                        return Ok(false);
                    }
                };
                if !result.is_object()
                    || result["request_id"] != id
                    || result["subject"] != follower
                    || !result["operation"].is_string()
                {
                    self.incomplete = true;
                    return Ok(false);
                }
                if successful_root_unfollow(&result) {
                    return Ok(false);
                }
            }
            if count < limit {
                break;
            }
        }
        self.defaults.insert(follower.into(), true);
        Ok(true)
    }
    fn relation(
        &mut self,
        tx: &PgSession,
        follower: &str,
        target: &str,
        budget: &mut ReadBudget,
    ) -> Result<Option<&'static str>> {
        if follower.is_empty() || follower == target {
            return Ok(None);
        }
        if tx
            .one(
                "SELECT created_at FROM agent_follows WHERE follower=$1 AND target=$2",
                &[follower.into(), target.into()],
            )?
            .is_some()
        {
            return Ok(Some("explicit"));
        }
        if target == "u_root" && self.root_default(tx, follower, budget)? {
            return Ok(Some("default"));
        }
        Ok(None)
    }
}
fn successful_root_unfollow(result: &J) -> bool {
    let truthy = |value: &J| match value {
        J::Null => false,
        J::Bool(v) => *v,
        J::Number(v) => v.as_f64().is_some_and(|v| v != 0.0),
        J::String(v) => !v.is_empty(),
        J::Array(v) => !v.is_empty(),
        J::Object(v) => !v.is_empty(),
    };
    result["operation"]
        .as_str()
        .is_some_and(|v| v.split('@').next() == Some("communication.unfollow"))
        && result["status"] == "ok"
        && truthy(&result["committed_at"])
        && truthy(&result["receipt"])
        && result["error"].is_null()
        && result["data"].is_object()
        && result["data"]["id"] == "u_root"
        && result["data"]["following"] == false
}
pub(crate) fn effective_follow(
    tx: &PgSession,
    follower: &str,
    target: &str,
) -> Result<Option<&'static str>> {
    FollowPolicy::default().relation(tx, follower, target, &mut ReadBudget::default())
}
pub(crate) fn effective_targets(tx: &PgSession, follower: &str) -> Result<BTreeSet<String>> {
    let mut targets = BTreeSet::new();
    for row in tx.rows("SELECT target FROM agent_follows WHERE follower=$1 AND target<>$1 ORDER BY target LIMIT 1000",&[follower.into()])?{targets.insert(row.text(0)?.to_owned());}
    if effective_follow(tx, follower, "u_root")?.is_some() {
        targets.insert("u_root".into());
    }
    Ok(targets)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_exact_successful_committed_root_unfollow_suppresses_default() {
        let record = json!({"request_id":"r_a","operation":"communication.unfollow@1","status":"ok","subject":"u_a","committed_at":"2026-01-01T00:00:00Z","receipt":{"signature":"sig"},"error":null,"data":{"id":"u_root","following":false}});
        assert!(successful_root_unfollow(&record));
        for field in ["committed_at", "receipt"] {
            let mut changed = record.clone();
            changed[field] = J::Null;
            assert!(!successful_root_unfollow(&changed));
        }
        for changed in [
            json!({"id":"u_root","following":true}),
            json!({"id":"u_other","following":false}),
        ] {
            let mut result = record.clone();
            result["data"] = changed;
            assert!(!successful_root_unfollow(&result));
        }
    }
}
