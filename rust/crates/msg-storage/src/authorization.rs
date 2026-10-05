//! Transaction-bound authorization facts; no policy or implicit permission here.
use crate::records::{Membership, Organization};
use crate::{database_error, Session};
use msg_core::{Error, Json, Result, MAX_BYTES};
use msg_identity::{
    authorization::{AuthorizationStore, DirectConversation, LegacyShare, ShareSource},
    models::{decode, SubjectKind},
    store::AuthorityStore,
};
use rusqlite::{params, OptionalExtension, Params, Row};
use std::collections::BTreeSet;

const MAX_FACTS: usize = 4096;
fn bounded_json(raw: &str) -> Result<Json> {
    if raw.len() > MAX_BYTES {
        return Err(Error("storage_record_too_large"));
    }
    Json::parse(raw)
}
impl<A> Session<'_, A> {
    fn authorization_rows<T>(
        &self,
        sql: &'static str,
        params: impl Params,
        decode_row: impl FnMut(&Row<'_>) -> rusqlite::Result<T>,
    ) -> Result<Vec<T>> {
        self.active()?;
        let mut stmt = self
            .transaction
            .prepare_cached(sql)
            .map_err(database_error)?;
        let rows = stmt
            .query_map(params, decode_row)
            .map_err(database_error)?
            .collect::<std::result::Result<Vec<_>, _>>()
            .map_err(database_error)?;
        if rows.len() > MAX_FACTS {
            return Err(Error("authority_work_limit"));
        }
        Ok(rows)
    }
}
impl<A> AuthorizationStore for Session<'_, A> {
    fn authority(&self) -> &dyn AuthorityStore {
        self
    }
    fn memberships(&self, subject: &str) -> Result<BTreeSet<String>> {
        let rows: Vec<(String, String)> = self.authorization_rows(
            "SELECT m.org,CAST(substr(CAST(m.body AS BLOB),1,1048577) AS TEXT) FROM memberships m JOIN resources r ON r.id=m.org WHERE m.subject=?1 AND r.type='organization' AND r.state='active' ORDER BY m.org LIMIT 4097",
            [subject], |r| Ok((r.get(0)?, r.get(1)?)))?;
        let mut groups = BTreeSet::new();
        for (org, body) in rows {
            let member: Membership = decode(&bounded_json(&body)?)?;
            member.validate()?;
            if member.status == "active"
                && member.organization_id == org
                && member.subject_id == subject
                && org != "g_public"
            {
                groups.insert(org);
            }
        }
        match self.subject(subject) {
            Ok(identity)
                if matches!(
                    identity.kind,
                    SubjectKind::Registered | SubjectKind::Custodial | SubjectKind::System
                ) && !identity.local_only =>
            {
                groups.insert("g_public".into());
            }
            Ok(_) | Err(Error("subject_not_found")) => (),
            Err(error) => return Err(error),
        }
        Ok(groups)
    }
    fn organization_exists(&self, id: &str) -> Result<bool> {
        let value = self.json("SELECT CAST(substr(CAST(body AS BLOB),1,1048577) AS TEXT) FROM identities WHERE id=?1 AND kind='organization'", id)?;
        match value {
            None => Ok(false),
            Some(value) => {
                let row: Organization = decode(&value)?;
                row.validate()?;
                if row.resource_id != id {
                    return Err(Error("storage_record_mismatch"));
                }
                Ok(true)
            }
        }
    }
    fn topic_admin(&self, topic: &str, subject: &str) -> Result<bool> {
        self.active()?;
        self.transaction.query_row("SELECT EXISTS(SELECT 1 FROM topic_memberships WHERE topic=?1 AND subject=?2 AND role='admin' AND status='active')",
            [topic, subject], |r| r.get(0)).map_err(database_error)
    }
    fn topic_ban(&self, topic: &str, subject: &str) -> Result<Option<Option<String>>> {
        self.active()?;
        self.transaction.query_row("SELECT expires_at FROM topic_bans WHERE topic=?1 AND subject=?2 AND status='active'", [topic, subject], |r| r.get(0))
            .optional().map_err(database_error)
    }
    fn direct_conversation(&self, resource: &str) -> Result<Option<DirectConversation>> {
        self.active()?;
        self.transaction.query_row("SELECT resource_id,participant_a,participant_b,state FROM dm_conversations WHERE resource_id=?1", [resource],
            |r| Ok(DirectConversation { resource_id: r.get(0)?, participant_a: r.get(1)?, participant_b: r.get(2)?, state: r.get(3)? }))
            .optional().map_err(database_error)
    }
    fn direct_blocked(&self, a: &str, b: &str) -> Result<bool> {
        self.active()?;
        self.transaction.query_row("SELECT EXISTS(SELECT 1 FROM dm_blocks WHERE (blocker=?1 AND blocked=?2) OR (blocker=?2 AND blocked=?1))",
            [a, b], |r| r.get(0)).map_err(database_error)
    }
    fn legacy_share(&self, resource: &str, grantee: &str) -> Result<Option<LegacyShare>> {
        self.active()?;
        self.transaction.query_row("SELECT grantor,expires_at,created_at FROM share_grants WHERE resource_id=?1 AND grantee=?2 AND revoked_at IS NULL",
            [resource, grantee], |r| Ok(LegacyShare { grantor: r.get(0)?, expires_at: r.get(1)?, created_at: r.get(2)? }))
            .optional().map_err(database_error)
    }
    fn share_candidates(&self, resource: &str, now: &str) -> Result<Vec<String>> {
        self.authorization_rows("SELECT id FROM share_grants_v2 WHERE resource_id=?1 AND revoked_at IS NULL AND expires_at>?2 ORDER BY id LIMIT 4097",
            params![resource, now], |r| r.get(0))
    }
    fn share_source(&self, id: &str) -> Result<Option<ShareSource>> {
        self.active()?;
        self.transaction.query_row("SELECT resource_id,grantor,grantee,grantee_kind,parent_id,substr(operations,1,1048577),substr(constraints,1,1048577),allow_reshare,expires_at,revoked_at,created_at FROM share_grants_v2 WHERE id=?1",
            [id], |r| Ok(ShareSource { resource_id: r.get(0)?, grantor: r.get(1)?, grantee: r.get(2)?, grantee_kind: r.get(3)?, parent_id: r.get(4)?,
                operations: r.get(5)?, constraints: r.get(6)?, allow_reshare: r.get::<_, i64>(7)? == 1, expires_at: r.get(8)?, revoked_at: r.get(9)?, created_at: r.get(10)? }))
            .optional().map_err(database_error)
    }
    fn csr_issuer(&self, id: &str) -> Result<String> {
        let row = self
            .json(
                "SELECT CAST(substr(CAST(body AS BLOB),1,1048577) AS TEXT) FROM csrs WHERE id=?1",
                id,
            )?
            .ok_or(Error("csr_not_found"))?;
        Ok(decode::<msg_identity::models::CertificateRequest>(&row)?.requested_issuer)
    }
}
