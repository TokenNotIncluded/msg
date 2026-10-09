//! Policy facts from the transaction's immutable PostgreSQL snapshot.
use super::PgSession;
use crate::records::{Membership, Organization};
use msg_core::{Error, Json, Result};
use msg_identity::{
    authorization::{AuthorizationStore, DirectConversation, LegacyShare, ShareSource},
    models::{decode, SubjectKind, Timestamp},
    oauth::OAuthState,
    store::{AuthorityStore, Record, RecoveryDelivery},
};
use std::collections::BTreeSet;

impl PgSession {
    fn json_fact(&self, sql: &'static str, id: &str) -> Result<Option<Json>> {
        self.one(sql, &[id.into()])?.map(|r| r.json(0)).transpose()
    }
}
impl AuthorityStore for PgSession {
    fn record(&self, kind: Record, id: &str) -> Result<Json> {
        let (sql, missing) = match kind {
            Record::Subject => (
                "SELECT substring(body,1,1048577) FROM identities WHERE id=$1 AND kind='subject'",
                "subject_not_found",
            ),
            Record::Credential => (
                "SELECT substring(body,1,1048577) FROM credentials WHERE id=$1",
                "credential_not_found",
            ),
            Record::Certificate => (
                "SELECT substring(body,1,1048577) FROM certificates WHERE id=$1",
                "certificate_not_found",
            ),
            Record::Resource => (
                "SELECT substring(body,1,1048577) FROM resources WHERE id=$1",
                "not_found",
            ),
        };
        self.json_fact(sql, id)?.ok_or(Error(missing))
    }
    fn setting(&self, key: &str) -> Result<Option<Json>> {
        self.json_fact(
            "SELECT substring(value,1,1048577) FROM settings WHERE key=$1",
            key,
        )
    }
    fn has_setting(&self, key: &str) -> Result<bool> {
        self.one(
            "SELECT EXISTS(SELECT 1 FROM settings WHERE key=$1)",
            &[key.into()],
        )?
        .ok_or(Error("storage_error"))?
        .boolean(0)
    }
    fn certificate_revoked(&self, id: &str) -> Result<bool> {
        Ok(self
            .one("SELECT revoked FROM certificates WHERE id=$1", &[id.into()])?
            .ok_or(Error("certificate_not_found"))?
            .integer(0)?
            != 0)
    }
    fn request_result(
        &self,
        subject: Option<&str>,
        id: &str,
        digest: &str,
    ) -> Result<Option<Json>> {
        let Some(row)=self.one("SELECT digest,substring(body,1,1048577) FROM results WHERE subject=$1 AND request_id=$2",&[subject.unwrap_or("").into(),id.into()])? else {return Ok(None)};
        if row.text(0)? != digest {
            return Err(Error("idempotency_conflict"));
        }
        let result = row.json(1)?;
        let fields = result.as_object()?;
        let result_subject = fields.get("subject").and_then(|v| {
            if v.is_null() {
                Some(None)
            } else {
                v.as_str().ok().map(Some)
            }
        });
        if fields.get("request_id").and_then(|v| v.as_str().ok()) != Some(id)
            || result_subject != Some(subject)
        {
            return Err(Error("storage_record_mismatch"));
        }
        Ok(Some(result))
    }
    fn oauth_state(&self, id: &str) -> Result<Option<OAuthState>> {
        self.one(
            "SELECT expires,substring(body,1,1048577) FROM oauth_states WHERE id=$1",
            &[id.into()],
        )?
        .map(|r| {
            Ok(OAuthState {
                expires_at: Timestamp::parse(r.text(0)?)?,
                body: r.json(1)?,
            })
        })
        .transpose()
    }
    fn custodial_binding(&self, subject: &str) -> Result<Option<(String, String)>> {
        self.one(
            "SELECT signing_key_id,status FROM custodial_vault WHERE subject=$1",
            &[subject.into()],
        )?
        .map(|r| Ok((r.text(0)?.to_owned(), r.text(1)?.to_owned())))
        .transpose()
    }
    fn recovery_delivery(
        &self,
        credential: &str,
        subject: &str,
    ) -> Result<Option<RecoveryDelivery>> {
        self.one("SELECT recovery_verifier,recovery_expires_at,consumed_at,request_id FROM token_deliveries WHERE credential_id=$1 AND subject=$2",&[credential.into(),subject.into()])?
            .map(|r|Ok(RecoveryDelivery{verifier:r.text(0)?.to_owned(),expires_at:Timestamp::parse(r.text(1)?)?,consumed:r.optional_text(2)?.is_some(),request_id:r.text(3)?.to_owned()})).transpose()
    }
}
impl AuthorizationStore for PgSession {
    fn authority(&self) -> &dyn AuthorityStore {
        self
    }
    fn memberships(&self, subject: &str) -> Result<BTreeSet<String>> {
        let rows=self.rows("SELECT m.org,substring(m.body,1,1048577) FROM memberships m JOIN resources r ON r.id=m.org WHERE m.subject=$1 AND r.type='organization' AND r.state='active' ORDER BY m.org LIMIT 4097",&[subject.into()])?;
        let mut result = BTreeSet::new();
        for r in rows {
            let m: Membership = decode(&r.json(1)?)?;
            m.validate()?;
            if m.status == "active"
                && m.organization_id == r.text(0)?
                && m.subject_id == subject
                && m.organization_id != "g_public"
            {
                result.insert(m.organization_id);
            }
        }
        match self.subject(subject) {
            Ok(s)
                if matches!(
                    s.kind,
                    SubjectKind::Registered | SubjectKind::Custodial | SubjectKind::System
                ) && !s.local_only =>
            {
                result.insert("g_public".into());
            }
            Ok(_) | Err(Error("subject_not_found")) => (),
            Err(e) => return Err(e),
        }
        Ok(result)
    }
    fn organization_exists(&self, id: &str) -> Result<bool> {
        let Some(value) = self.json_fact(
            "SELECT substring(body,1,1048577) FROM identities WHERE id=$1 AND kind='organization'",
            id,
        )?
        else {
            return Ok(false);
        };
        let row: Organization = decode(&value)?;
        row.validate()?;
        if row.resource_id != id {
            return Err(Error("storage_record_mismatch"));
        }
        Ok(true)
    }
    fn topic_admin(&self, topic: &str, subject: &str) -> Result<bool> {
        self.one("SELECT EXISTS(SELECT 1 FROM topic_memberships WHERE topic=$1 AND subject=$2 AND role='admin' AND status='active')",&[topic.into(),subject.into()])?.ok_or(Error("storage_error"))?.boolean(0)
    }
    fn topic_ban(&self, topic: &str, subject: &str) -> Result<Option<Option<String>>> {
        self.one(
            "SELECT expires_at FROM topic_bans WHERE topic=$1 AND subject=$2 AND status='active'",
            &[topic.into(), subject.into()],
        )?
        .map(|r| Ok(r.optional_text(0)?.map(str::to_owned)))
        .transpose()
    }
    fn direct_conversation(&self, resource: &str) -> Result<Option<DirectConversation>> {
        self.one("SELECT resource_id,participant_a,participant_b,state FROM dm_conversations WHERE resource_id=$1",&[resource.into()])?.map(|r|Ok(DirectConversation{
            resource_id:r.text(0)?.to_owned(),participant_a:r.text(1)?.to_owned(),participant_b:r.text(2)?.to_owned(),state:r.text(3)?.to_owned()})).transpose()
    }
    fn direct_blocked(&self, a: &str, b: &str) -> Result<bool> {
        self.one("SELECT EXISTS(SELECT 1 FROM dm_blocks WHERE (blocker=$1 AND blocked=$2) OR (blocker=$2 AND blocked=$1))",&[a.into(),b.into()])?.ok_or(Error("storage_error"))?.boolean(0)
    }
    fn legacy_share(&self, resource: &str, grantee: &str) -> Result<Option<LegacyShare>> {
        self.one("SELECT grantor,expires_at,created_at FROM share_grants WHERE resource_id=$1 AND grantee=$2 AND revoked_at IS NULL",&[resource.into(),grantee.into()])?.map(|r|Ok(LegacyShare{
            grantor:r.text(0)?.to_owned(),expires_at:r.text(1)?.to_owned(),created_at:r.text(2)?.to_owned()})).transpose()
    }
    fn share_candidates(&self, resource: &str, now: &str) -> Result<Vec<String>> {
        self.rows("SELECT id FROM share_grants_v2 WHERE resource_id=$1 AND revoked_at IS NULL AND expires_at>$2 ORDER BY id LIMIT 4097",&[resource.into(),now.into()])?.iter().map(|r|Ok(r.text(0)?.to_owned())).collect()
    }
    fn share_source(&self, id: &str) -> Result<Option<ShareSource>> {
        self.one("SELECT resource_id,grantor,grantee,grantee_kind,parent_id,substring(operations,1,1048577),substring(constraints,1,1048577),allow_reshare,expires_at,revoked_at,created_at FROM share_grants_v2 WHERE id=$1",&[id.into()])?.map(|r|Ok(ShareSource{
            resource_id:r.text(0)?.to_owned(),grantor:r.text(1)?.to_owned(),grantee:r.text(2)?.to_owned(),grantee_kind:r.text(3)?.to_owned(),parent_id:r.optional_text(4)?.map(str::to_owned),
            operations:r.text(5)?.to_owned(),constraints:r.text(6)?.to_owned(),allow_reshare:r.integer(7)?==1,expires_at:r.text(8)?.to_owned(),revoked_at:r.optional_text(9)?.map(str::to_owned),created_at:r.text(10)?.to_owned()})).transpose()
    }
    fn csr_issuer(&self, id: &str) -> Result<String> {
        let value = self
            .json_fact("SELECT substring(body,1,1048577) FROM csrs WHERE id=$1", id)?
            .ok_or(Error("csr_not_found"))?;
        Ok(decode::<msg_identity::models::CertificateRequest>(&value)?.requested_issuer)
    }
}
