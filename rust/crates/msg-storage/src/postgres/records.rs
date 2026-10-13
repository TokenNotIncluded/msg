//! Typed durable records. These methods do not grant business authority.
use super::{PgSession, Value};
use crate::records::*;
use msg_core::{Error, Json, Result};
use msg_identity::{
    models::{Certificate, CertificateRequest, Credential, ResourceRef, Timestamp},
    store::{AuthorityStore, Record},
};
use serde::Serialize;
use std::collections::BTreeSet;
fn time(value: Timestamp) -> String {
    value.0.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()
}
fn raw(value: &impl Serialize) -> Result<Value> {
    Ok(encode(value)?.canonical()?.into())
}
fn require(yes: bool, code: &'static str) -> Result<()> {
    if yes {
        Ok(())
    } else {
        Err(Error(code))
    }
}
fn is_hex(id: &str) -> bool {
    id.len() == 32
        && id
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
impl PgSession {
    fn mutate<T>(&self, body: impl FnOnce() -> Result<T>) -> Result<T> {
        self.require_write()?;
        self.capture(body(), true)
    }
    pub fn resource_record(&self, id: &str) -> Result<Resource> {
        if is_hex(id) {
            let rows=self.rows("SELECT substring(body,1,1048577) FROM resources WHERE CASE WHEN id ~ '^[0-9a-f]{32}$' THEN id WHEN id ~ '^[A-Za-z][A-Za-z0-9_]*_([0-9a-f]{32})$' THEN right(id,32) ELSE left(encode(sha256(convert_to('msg.hex-reference/v1','UTF8')||decode('00','hex')||convert_to(id,'UTF8')),'hex'),32) END=$1 LIMIT 2",&[id.into()])?;
            require(rows.len() <= 1, "ambiguous_resource_id")?;
            return decode(&rows.first().ok_or(Error("not_found"))?.json(0)?);
        }
        let resource: Resource = decode(&self.record(Record::Resource, id)?)?;
        require(resource.id == id, "storage_record_mismatch")?;
        Ok(resource)
    }
    pub fn resolve(&self, path: &str, migrated: bool) -> Result<String> {
        require(
            path.starts_with('/') && !path.contains(['\\', '\0']),
            "invalid_path",
        )?;
        let parts: Vec<_> = path.trim_end_matches('/').split('/').skip(1).collect();
        require(
            parts.len() <= 256
                && parts
                    .iter()
                    .all(|p| !p.is_empty() && !matches!(*p, "." | "..")),
            "invalid_path",
        )?;
        if !migrated {
            if parts.len() == 2 && parts[0] == "_id" {
                return Ok(self.resource_record(parts[1])?.id);
            }
            if let Some(last) = parts.last().and_then(|p| p.strip_prefix('*')) {
                let resource = self.resource_record(last)?;
                if parts.len() > 1 {
                    let parent =
                        self.resolve(&format!("/{}", parts[..parts.len() - 1].join("/")), false)?;
                    require(resource.parent.as_deref() == Some(&parent), "not_found")?;
                }
                return Ok(resource.id);
            }
        }
        let mut id = self
            .one("SELECT id FROM resources WHERE parent IS NULL", &[])?
            .ok_or(Error("not_initialized"))?
            .text(0)?
            .to_owned();
        if path == "/" {
            return Ok(id);
        }
        require(!parts.is_empty(), "invalid_path")?;
        for part in parts {
            let mut found = self.one(
                "SELECT id FROM resources WHERE parent=$1 AND name=$2",
                &[id.clone().into(), part.into()],
            )?;
            if found.is_none() && migrated {
                found = self.one(
                    "SELECT resource_id FROM resource_path_aliases WHERE parent_id=$1 AND name=$2",
                    &[id.clone().into(), part.into()],
                )?;
            }
            id = found.ok_or(Error("not_found"))?.text(0)?.to_owned();
        }
        Ok(self.resource_record(&id)?.id)
    }
    pub fn resource_path(&self, id: &str) -> Result<String> {
        let mut current = self.resource_record(id)?;
        let mut names = Vec::new();
        let mut seen = BTreeSet::new();
        while let Some(parent) = &current.parent {
            require(seen.insert(current.id.clone()), "parent_cycle")?;
            require(seen.len() <= 1024, "authority_work_limit")?;
            names.push(current.name.clone());
            current = self.resource_record(parent)?;
        }
        names.reverse();
        Ok(format!("/{}", names.join("/")))
    }
    pub fn children(
        &self,
        parent: &str,
        cursor: Option<&str>,
        limit: u32,
    ) -> Result<Page<Resource>> {
        require((1..=500).contains(&limit), "invalid_limit")?;
        let rows=self.rows("SELECT substring(body,1,1048577) FROM resources WHERE parent=$1 AND id>$2 ORDER BY id LIMIT $3",&[parent.into(),cursor.unwrap_or("").into(),(limit+1).into()])?;
        let items: Vec<Resource> = rows
            .iter()
            .take(limit as usize)
            .map(|r| decode(&r.json(0)?))
            .collect::<Result<_>>()?;
        let next_cursor = if rows.len() > limit as usize {
            items.last().map(|v| v.id.clone())
        } else {
            None
        };
        Ok(Page { items, next_cursor })
    }
    pub fn revision(&self, reference: &ResourceRef) -> Result<Revision> {
        let resource = self.resource_record(&reference.id)?;
        let id = reference
            .revision
            .as_ref()
            .or(resource.revision.as_ref())
            .ok_or(Error("revision_not_found"))?;
        let rows = if is_hex(id) {
            self.rows("SELECT substring(body,1,1048577) FROM revisions WHERE resource_id=$1 AND CASE WHEN id ~ '^[0-9a-f]{32}$' THEN id WHEN id ~ '^[A-Za-z][A-Za-z0-9_]*_([0-9a-f]{32})$' THEN right(id,32) ELSE left(encode(sha256(convert_to('msg.hex-reference/v1','UTF8')||decode('00','hex')||convert_to(id,'UTF8')),'hex'),32) END=$2 LIMIT 2",&[resource.id.clone().into(),id.as_str().into()])?
        } else {
            self.rows(
                "SELECT substring(body,1,1048577) FROM revisions WHERE resource_id=$1 AND id=$2",
                &[resource.id.clone().into(), id.as_str().into()],
            )?
        };
        require(rows.len() <= 1, "ambiguous_revision_id")?;
        let revision: Revision =
            decode(&rows.first().ok_or(Error("revision_not_found"))?.json(0)?)?;
        require(
            revision.resource_id == resource.id && (is_hex(id) || revision.id == *id),
            "storage_record_mismatch",
        )?;
        Ok(revision)
    }
    pub fn history(&self, id: &str, cursor: Option<&str>, limit: u32) -> Result<Page<Revision>> {
        require((1..=500).contains(&limit), "invalid_limit")?;
        let rows=self.rows("SELECT substring(body,1,1048577) FROM revisions WHERE resource_id=$1 AND id>$2 ORDER BY id LIMIT $3",&[id.into(),cursor.unwrap_or("").into(),(limit+1).into()])?;
        let items: Vec<Revision> = rows
            .iter()
            .take(limit as usize)
            .map(|r| decode(&r.json(0)?))
            .collect::<Result<_>>()?;
        let next_cursor = if rows.len() > limit as usize {
            items.last().map(|v| v.id.clone())
        } else {
            None
        };
        Ok(Page { items, next_cursor })
    }
    pub fn set_setting(&self, key: &str, value: &Json) -> Result<()> {
        self.mutate(||{
        self.execute("INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=excluded.value",&[key.into(),value.canonical()?.into()])?;Ok(())
    })
    }
    pub fn bump_authorization_epoch(&self) -> Result<()> {
        self.mutate(|| {
            let old = self
                .setting("authorization_epoch")?
                .map(|v| {
                    v.as_integer()?
                        .parse::<i64>()
                        .map_err(|_| Error("invalid_authorization_epoch"))
                })
                .transpose()?
                .unwrap_or(0);
            let next = old
                .checked_add(1)
                .filter(|_| old >= 0)
                .ok_or(Error("invalid_authorization_epoch"))?;
            self.set_setting("authorization_epoch", &Json::parse(&next.to_string())?)
        })
    }
    pub fn insert_resource(&self, r: &Resource) -> Result<()> {
        self.mutate(||{
        r.validate()?;if let Some(parent)=&r.parent{self.resource_record(parent)?;}
        self.execute("INSERT INTO resources(id,type,name,parent,owner,grp,mode,generation,revision,state,created_at,modified_at,body) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",&[
            r.id.as_str().into(),r.resource_type.as_str().into(),r.name.as_str().into(),r.parent.as_deref().into(),r.owner.as_str().into(),r.group.as_str().into(),(r.mode as i64).into(),r.generation.into(),r.revision.as_deref().into(),r.state.sql().into(),time(r.created_at).into(),time(r.modified_at).into(),raw(r)?])?;
        for tag in &r.tags{self.execute("INSERT INTO resource_tags(resource_id,tag) VALUES($1,$2)",&[r.id.as_str().into(),tag.as_str().into()])?;}Ok(())
    })
    }
    pub fn replace_resource(&self, r: &Resource, expected: i64) -> Result<()> {
        self.mutate(||{
        r.validate()?;let old=self.resource_record(&r.id)?;
        require(old.generation==expected,"generation_conflict")?;require(expected.checked_add(1)==Some(r.generation),"invalid_generation")?;
        require(old.resource_type==r.resource_type&&old.created_at==r.created_at&&old.created_by==r.created_by,"immutable_creation_fact")?;
        if let Some(parent)=&r.parent{require(parent!=&r.id&&!self.ancestors(parent)?.iter().any(|a|a.id==r.id),"parent_cycle")?;}
        if old.parent!=r.parent||old.owner!=r.owner||old.group!=r.group||old.mode!=r.mode||old.state!=r.state{self.bump_authorization_epoch()?;}
        if let Some(parent)=&old.parent{if old.parent!=r.parent||old.name!=r.name{self.execute("INSERT INTO resource_path_aliases(parent_id,name,resource_id) VALUES($1,$2,$3) ON CONFLICT(parent_id,name) DO UPDATE SET resource_id=excluded.resource_id",&[parent.as_str().into(),old.name.as_str().into(),old.id.as_str().into()])?;}}
        let changed=self.execute("UPDATE resources SET name=$1,parent=$2,owner=$3,grp=$4,mode=$5,generation=$6,revision=$7,state=$8,modified_at=$9,body=$10 WHERE id=$11 AND generation=$12",&[
            r.name.as_str().into(),r.parent.as_deref().into(),r.owner.as_str().into(),r.group.as_str().into(),(r.mode as i64).into(),r.generation.into(),r.revision.as_deref().into(),r.state.sql().into(),time(r.modified_at).into(),raw(r)?,r.id.as_str().into(),expected.into()])?;
        require(changed==1,"generation_conflict")?;
        if old.tags!=r.tags{self.execute("DELETE FROM resource_tags WHERE resource_id=$1",&[r.id.as_str().into()])?;for tag in &r.tags{self.execute("INSERT INTO resource_tags(resource_id,tag) VALUES($1,$2)",&[r.id.as_str().into(),tag.as_str().into()])?;}}Ok(())
    })
    }
    pub fn append_revision(&self, r: &Revision) -> Result<()> {
        self.mutate(||{
        require(r.format_version>=1,"invalid_version")?;require(r.content.size>=0,"invalid_nonnegative_integer")?;
        for parent in &r.parents{self.revision(&ResourceRef{id:r.resource_id.clone(),revision:Some(parent.clone())})?;}
        self.execute("INSERT INTO revisions(id,resource_id,created_at,body) VALUES($1,$2,$3,$4)",&[r.id.as_str().into(),r.resource_id.as_str().into(),time(r.created_at).into(),raw(r)?])?;
        for rel in &r.relations{
            require(!rel.excerpt.is_some_and(|[a,b]|a<0||a>b),"invalid_byte_range")?;
            self.execute("INSERT INTO relations(revision_id,source_id,type,target_id,target_revision,body) VALUES($1,$2,$3,$4,$5,$6)",&[r.id.as_str().into(),r.resource_id.as_str().into(),rel.relation_type.as_str().into(),rel.target.id.as_str().into(),rel.target.revision.as_deref().into(),raw(rel)?])?;
        }Ok(())
    })
    }
    pub fn save_result(
        &self,
        subject: Option<&str>,
        digest: &str,
        result: &OperationResult,
    ) -> Result<()> {
        self.mutate(|| {
            require(
                subject == result.subject.as_deref() && subject != Some(""),
                "storage_record_mismatch",
            )?;
            require(
                digest.strip_prefix("sha256:").is_some_and(|s| {
                    s.len() == 64
                        && s.bytes()
                            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                }),
                "invalid_digest",
            )?;
            require(
                self.one("SELECT count(*) FROM results", &[])?
                    .ok_or(Error("storage_error"))?
                    .integer(0)?
                    < 100_000,
                "storage_capacity_exceeded",
            )?;
            self.execute(
                "INSERT INTO results(subject,request_id,digest,body) VALUES($1,$2,$3,$4)",
                &[
                    subject.unwrap_or("").into(),
                    result.request_id.as_str().into(),
                    digest.into(),
                    raw(result)?,
                ],
            )?;
            Ok(())
        })
    }
    pub fn append_event(&self, event: &Event) -> Result<()> {
        self.mutate(|| {
            self.execute(
                "INSERT INTO events(id,body) VALUES($1,$2)",
                &[event.id.as_str().into(), raw(event)?],
            )?;
            Ok(())
        })
    }
    pub fn append_audit(&self, event: &AuditEvent) -> Result<()> {
        self.mutate(|| {
            let previous = self
                .one("SELECT digest FROM audit ORDER BY seq DESC LIMIT 1", &[])?
                .map(|r| Ok(r.text(0)?.to_owned()))
                .transpose()?;
            let mut event = event.clone();
            event.previous_digest = previous;
            event.entry_digest.clear();
            let mut fields = encode(&event)?.into_object()?;
            fields.remove("entry_digest");
            event.entry_digest =
                msg_core::digest(Json::from_object(fields).canonical()?.as_bytes());
            self.execute(
                "INSERT INTO audit(digest,previous,body) VALUES($1,$2,$3)",
                &[
                    event.entry_digest.as_str().into(),
                    event.previous_digest.as_deref().into(),
                    raw(&event)?,
                ],
            )?;
            Ok(())
        })
    }
    pub fn update_identity(&self, record: IdentityRecord<'_>, expected: i64) -> Result<()> {
        self.mutate(|| {
            let (select, insert, update, mut keys, generation, body, kind) = match record {
                IdentityRecord::Subject(s) => (
                    "SELECT generation FROM identities WHERE id=$1",
                    "INSERT INTO identities(id,kind,generation,body) VALUES($1,$2,$3,$4)",
                    "UPDATE identities SET generation=$1,body=$2 WHERE id=$3",
                    vec![s.resource_id.as_str().into()],
                    i64::try_from(s.auth_version).map_err(|_| Error("invalid_generation"))?,
                    raw(s)?,
                    Some("subject"),
                ),
                IdentityRecord::Organization(g) => {
                    require(
                        g.membership_version >= 0
                            && matches!(
                                g.membership_policy.as_str(),
                                "open" | "approval" | "invite" | "managed"
                            )
                            && g.builtin
                                .as_ref()
                                .is_none_or(|s| matches!(s.as_str(), "public" | "admins")),
                        "invalid_identity_record",
                    )?;
                    (
                        "SELECT generation FROM identities WHERE id=$1",
                        "INSERT INTO identities(id,kind,generation,body) VALUES($1,$2,$3,$4)",
                        "UPDATE identities SET generation=$1,body=$2 WHERE id=$3",
                        vec![g.resource_id.as_str().into()],
                        g.membership_version,
                        raw(g)?,
                        Some("organization"),
                    )
                }
                IdentityRecord::Membership(m) => {
                    require(
                        m.version >= 0
                            && matches!(
                                m.role.as_str(),
                                "owner" | "maintainer" | "member" | "admin"
                            )
                            && matches!(
                                m.status.as_str(),
                                "active" | "pending" | "invited" | "rejected"
                            ),
                        "invalid_identity_record",
                    )?;
                    self.bump_authorization_epoch()?;
                    (
                        "SELECT generation FROM memberships WHERE org=$1 AND subject=$2",
                        "INSERT INTO memberships(org,subject,generation,body) VALUES($1,$2,$3,$4)",
                        "UPDATE memberships SET generation=$1,body=$2 WHERE org=$3 AND subject=$4",
                        vec![
                            m.organization_id.as_str().into(),
                            m.subject_id.as_str().into(),
                        ],
                        m.version,
                        raw(m)?,
                        None,
                    )
                }
                IdentityRecord::Email(e) => (
                    "SELECT generation FROM emails WHERE subject=$1",
                    "INSERT INTO emails(subject,generation,body) VALUES($1,$2,$3)",
                    "UPDATE emails SET generation=$1,body=$2 WHERE subject=$3",
                    vec![e.subject_id.as_str().into()],
                    expected.checked_add(1).ok_or(Error("invalid_generation"))?,
                    raw(e)?,
                    None,
                ),
            };
            let old = self.one(select, &keys)?;
            require(
                match &old {
                    None => expected == -1,
                    Some(row) => row.integer(0)? == expected,
                },
                "generation_conflict",
            )?;
            if old.is_none() {
                if let Some(kind) = kind {
                    keys.push(kind.into());
                }
                keys.push(generation.into());
                keys.push(body);
                self.execute(insert, &keys)?;
            } else {
                let mut params = vec![generation.into(), body];
                params.extend(keys);
                self.execute(update, &params)?;
            }
            Ok(())
        })
    }
    pub fn save_credential(&self, c: &Credential, expected_auth_version: u64) -> Result<()> {
        self.mutate(||{
        require(self.subject(&c.subject_id)?.auth_version==expected_auth_version,"auth_version_conflict")?;
        if let Some(row)=self.one("SELECT substring(body,1,1048577) FROM credentials WHERE id=$1",&[c.id.as_str().into()])?{
            let old:Credential=msg_identity::models::decode(&row.json(0)?)?;
            require(old.subject_id==c.subject_id&&old.kind==c.kind&&old.verifier.0==c.verifier.0&&old.source_credential_id==c.source_credential_id,"credential_identity_immutable")?;
        }
        self.execute("INSERT INTO credentials(id,subject,body) VALUES($1,$2,$3) ON CONFLICT(id) DO UPDATE SET body=excluded.body",&[c.id.as_str().into(),c.subject_id.as_str().into(),raw(c)?])?;Ok(())
    })
    }
    pub fn csr(&self, id: &str) -> Result<CertificateRequest> {
        msg_identity::models::decode(
            &self
                .one(
                    "SELECT substring(body,1,1048577) FROM csrs WHERE id=$1",
                    &[id.into()],
                )?
                .ok_or(Error("csr_not_found"))?
                .json(0)?,
        )
    }
    pub fn csr_state(&self, id: &str) -> Result<CertificateRequestState> {
        decode(
            &self
                .one(
                    "SELECT substring(state_body,1,1048577) FROM csrs WHERE id=$1",
                    &[id.into()],
                )?
                .ok_or(Error("csr_not_found"))?
                .json(0)?,
        )
    }
    pub fn save_csr(&self, request: &CertificateRequest) -> Result<()> {
        self.mutate(||{
        let state=CertificateRequestState{request_id:request.resource_id.clone(),status:"pending".into(),generation:0,certificate_id:None};
        self.execute("INSERT INTO csrs(id,generation,state,body,state_body) VALUES($1,0,'pending',$2,$3)",&[request.resource_id.as_str().into(),raw(request)?,raw(&state)?])?;Ok(())
    })
    }
    pub fn transition_csr(&self, state: &CertificateRequestState, expected: i64) -> Result<()> {
        self.mutate(||{
        require(matches!(state.status.as_str(),"pending"|"issued"|"rejected"|"cancelled"|"expired"),"invalid_csr_state")?;
        require(expected.checked_add(1)==Some(state.generation),"invalid_generation")?;
        let old=self.csr_state(&state.request_id)?;require(old.status=="pending","csr_not_pending")?;require(old.generation==expected,"generation_conflict")?;
        require(self.execute("UPDATE csrs SET generation=$1,state=$2,state_body=$3 WHERE id=$4 AND generation=$5",&[state.generation.into(),state.status.as_str().into(),raw(state)?,state.request_id.as_str().into(),expected.into()])?==1,"generation_conflict")
    })
    }
    pub fn register_certificate(&self, cert: &Certificate, csr: Option<(&str, i64)>) -> Result<()> {
        self.mutate(|| {
            self.execute(
                "INSERT INTO certificates(id,subject,parent,revoked,body) VALUES($1,$2,$3,0,$4)",
                &[
                    cert.resource_id.as_str().into(),
                    cert.subject_id.as_str().into(),
                    cert.parent_certificate_id.as_deref().into(),
                    raw(cert)?,
                ],
            )?;
            if let Some((id, generation)) = csr {
                self.set_setting(
                    &format!("certificate_request:{}", cert.resource_id),
                    &Json::string(id),
                )?;
                self.transition_csr(
                    &CertificateRequestState {
                        request_id: id.into(),
                        status: "issued".into(),
                        generation: generation
                            .checked_add(1)
                            .ok_or(Error("invalid_generation"))?,
                        certificate_id: Some(cert.resource_id.clone()),
                    },
                    generation,
                )?;
            }
            Ok(())
        })
    }
    pub fn revoke_certificate(&self, id: &str, event: &AuditEvent) -> Result<()> {
        self.mutate(|| {
            self.certificate(id)?;
            self.execute(
                "UPDATE certificates SET revoked=1 WHERE id=$1",
                &[id.into()],
            )?;
            self.bump_authorization_epoch()?;
            self.append_audit(event)
        })
    }
    pub fn transfer(&self, id: &str) -> Result<TransferSession> {
        decode(
            &self
                .one(
                    "SELECT substring(body,1,1048577) FROM transfers WHERE id=$1",
                    &[id.into()],
                )?
                .ok_or(Error("transfer_not_found"))?
                .json(0)?,
        )
    }
    pub fn save_transfer(&self, t: &TransferSession, expected: Option<i64>) -> Result<()> {
        self.mutate(|| {
            require(
                matches!(t.direction.as_str(), "upload" | "download")
                    && matches!(
                        t.state.as_str(),
                        "open" | "sealed" | "cancelled" | "expired"
                    )
                    && t.generation >= 0
                    && t.expected_size.is_none_or(|n| n >= 0),
                "invalid_transfer",
            )?;
            if let Some(expected) = expected {
                require(
                    expected.checked_add(1) == Some(t.generation),
                    "invalid_generation",
                )?;
                require(
                    self.execute(
                        "UPDATE transfers SET generation=$1,body=$2 WHERE id=$3 AND generation=$4",
                        &[
                            t.generation.into(),
                            raw(t)?,
                            t.id.as_str().into(),
                            expected.into(),
                        ],
                    )? == 1,
                    "generation_conflict",
                )?;
            } else {
                self.execute(
                    "INSERT INTO transfers(id,subject,generation,body) VALUES($1,$2,$3,$4)",
                    &[
                        t.id.as_str().into(),
                        t.subject_id.as_str().into(),
                        t.generation.into(),
                        raw(t)?,
                    ],
                )?;
            }
            Ok(())
        })
    }
    pub fn put_chunk(&self, c: &TransferChunk) -> Result<()> {
        self.mutate(||{
        require(c.offset>=0&&c.content.size>=0,"invalid_nonnegative_integer")?;
        let end=c.offset.checked_add(c.content.size).ok_or(Error("invalid_byte_range"))?;
        let rows=self.rows("SELECT \"offset\",length,substring(body,1,1048577) FROM chunks WHERE transfer_id=$1 AND \"offset\"<$2 AND \"offset\"+length>$3",&[c.transfer_id.as_str().into(),end.into(),c.offset.into()])?;
        if !rows.is_empty(){require(rows.len()==1&&rows[0].integer(0)?==c.offset&&rows[0].integer(1)?==c.content.size,"chunk_conflict")?;
            let old:TransferChunk=decode(&rows[0].json(2)?)?;require(encode(&old.content)?.canonical()?==encode(&c.content)?.canonical()?,"chunk_conflict")?;return Ok(());}
        self.execute("INSERT INTO chunks(transfer_id,\"offset\",length,body) VALUES($1,$2,$3,$4)",&[c.transfer_id.as_str().into(),c.offset.into(),c.content.size.into(),raw(c)?])?;Ok(())
    })
    }
    pub fn missing_ranges(
        &self,
        id: &str,
        cursor: Option<&str>,
        limit: u32,
    ) -> Result<Page<[i64; 2]>> {
        let size = self
            .transfer(id)?
            .expected_size
            .ok_or(Error("size_required"))?;
        require((1..=500).contains(&limit), "invalid_limit")?;
        let mut start = cursor
            .unwrap_or("0")
            .parse::<i64>()
            .map_err(|_| Error("invalid_cursor"))?;
        require(start >= 0 && start <= size, "invalid_cursor")?;
        let rows=self.rows("SELECT \"offset\",length FROM chunks WHERE transfer_id=$1 AND \"offset\"+length>$2 ORDER BY \"offset\" LIMIT 4097",&[id.into(),start.into()])?;
        let mut gaps = Vec::new();
        for row in rows {
            let offset = row.integer(0)?;
            let length = row.integer(1)?;
            require(offset >= 0 && length >= 0, "invalid_byte_range")?;
            if offset > start {
                gaps.push([start, offset]);
                if gaps.len() > limit as usize {
                    break;
                }
            }
            start = start.max(
                offset
                    .checked_add(length)
                    .ok_or(Error("invalid_byte_range"))?,
            );
        }
        if gaps.len() <= limit as usize && start < size {
            gaps.push([start, size]);
        }
        let next_cursor = gaps.get(limit as usize).map(|v| v[0].to_string());
        gaps.truncate(limit as usize);
        Ok(Page {
            items: gaps,
            next_cursor,
        })
    }
    pub fn job(&self, id: &str) -> Result<EffectJob> {
        let row = self
            .one(
                "SELECT state,next_at,substring(body,1,1048577) FROM jobs WHERE id=$1",
                &[id.into()],
            )?
            .ok_or(Error("job_not_found"))?;
        let job: EffectJob = decode(&row.json(2)?)?;
        require(
            job.id == id
                && job.state == row.text(0)?
                && time(job.next_attempt_at) == row.text(1)?,
            "storage_record_mismatch",
        )?;
        Ok(job)
    }
    pub fn enqueue(&self, job: &EffectJob) -> Result<()> {
        self.mutate(|| {
            require(
                job.attempts == 0 && job.state == "pending" && job.lease_until.is_none(),
                "invalid_job_state",
            )?;
            self.execute(
                "INSERT INTO jobs(id,dedupe,kind,state,next_at,body) VALUES($1,$2,$3,$4,$5,$6)",
                &[
                    job.id.as_str().into(),
                    job.dedupe_key.as_str().into(),
                    job.kind.as_str().into(),
                    job.state.as_str().into(),
                    time(job.next_attempt_at).into(),
                    raw(job)?,
                ],
            )?;
            Ok(())
        })
    }
    pub fn save_job(&self, job: &EffectJob) -> Result<()> {
        self.mutate(|| {
            require(
                job.attempts >= 0
                    && matches!(
                        job.state.as_str(),
                        "pending" | "running" | "done" | "failed" | "uncertain"
                    ),
                "invalid_job_state",
            )?;
            require(
                self.execute(
                    "UPDATE jobs SET state=$1,next_at=$2,body=$3 WHERE id=$4",
                    &[
                        job.state.as_str().into(),
                        time(job.next_attempt_at).into(),
                        raw(job)?,
                        job.id.as_str().into(),
                    ],
                )? == 1,
                "job_not_found",
            )
        })
    }
}
