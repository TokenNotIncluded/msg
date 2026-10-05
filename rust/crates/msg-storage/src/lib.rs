//! Native SQLite sessions over existing Python-created databases.
//!
//! No schema creation/migration, network entry point or Python subprocess.
//! Read snapshots and write sessions share the same transaction-bound authority reads.
mod authorization;
pub mod postgres;
pub mod records;
#[cfg(unix)]
pub mod writer;
use msg_core::{Error, Json, Result, MAX_BYTES};
use msg_identity::{
    models::Timestamp,
    oauth::OAuthState,
    store::{AuthorityStore, Record, RecoveryDelivery},
};
use rusqlite::{Connection, ErrorCode, OpenFlags, OptionalExtension, Transaction};
use std::{cell::Cell, marker::PhantomData, path::Path, rc::Rc, time::Duration};
#[cfg(unix)]
pub use writer::{SqliteWriter, WriteFailure, WriteSession};

pub struct SqliteAuthority {
    connection: Connection,
}
pub struct ReadOnly;
pub type Snapshot<'a> = Session<'a, ReadOnly>;

/// A borrowed database session. Its transaction is private and cannot escape.
pub struct Session<'a, Access> {
    transaction: Transaction<'a>,
    access: Access,
    poison: Cell<Option<Error>>,
    // A database snapshot must stay with the task that owns it.
    _owner: PhantomData<Rc<()>>,
}
fn database_error(error: rusqlite::Error) -> Error {
    match error.sqlite_error_code() {
        Some(ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked) => Error("server_busy"),
        Some(ErrorCode::ReadOnly) => Error("read_only_transaction"),
        Some(ErrorCode::ConstraintViolation) => Error("constraint_conflict"),
        _ => Error("storage_error"),
    }
}
impl SqliteAuthority {
    pub fn open_existing(path: &Path) -> Result<Self> {
        let connection = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(database_error)?;
        connection
            .busy_timeout(Duration::from_secs(10))
            .map_err(database_error)?;
        connection
            .execute_batch(
                "PRAGMA query_only=ON; PRAGMA trusted_schema=OFF; PRAGMA foreign_keys=ON;",
            )
            .map_err(database_error)?;
        Ok(Self { connection })
    }
    pub fn snapshot(&mut self) -> Result<Snapshot<'_>> {
        let transaction = self.connection.transaction().map_err(database_error)?;
        let snapshot = Snapshot {
            transaction,
            access: ReadOnly,
            poison: Cell::new(None),
            _owner: PhantomData,
        };
        // The first read pins this snapshot before checking any authority.
        snapshot.validate_schema()?;
        Ok(snapshot)
    }
}
impl<Access> Session<'_, Access> {
    fn active(&self) -> Result<()> {
        if let Some(error) = self.poison.get() {
            return Err(error);
        }
        if self.transaction.is_autocommit() {
            return Err(Error("transaction_aborted"));
        }
        Ok(())
    }
    fn validate_schema(&self) -> Result<()> {
        self.active()?;
        let expected = [
            ("schema_version", "version"), ("settings", "key,value"),
            ("identities", "id,kind,body"), ("credentials", "id,subject,body"),
            ("certificates", "id,subject,parent,revoked,body"),
            ("resources", "id,parent,body"), ("results", "subject,request_id,digest,body"),
            ("oauth_states", "id,expires,body"),
            ("custodial_vault", "subject,signing_key_id,status"),
            ("token_deliveries", "credential_id,subject,recovery_verifier,recovery_expires_at,consumed_at,request_id"),
        ];
        for (table, columns) in expected {
            let exists: bool = self
                .transaction
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE name=?1 AND type='table')",
                    [table],
                    |r| r.get(0),
                )
                .map_err(|_| Error("unsupported_storage_schema"))?;
            if !exists {
                return Err(Error("unsupported_storage_schema"));
            }
            // Names are compile-time constants, never request strings.
            self.transaction
                .prepare(&format!("SELECT {columns} FROM {table} LIMIT 0"))
                .map_err(|_| Error("unsupported_storage_schema"))?;
        }
        let mut stmt = self
            .transaction
            .prepare("SELECT version FROM schema_version ORDER BY version LIMIT 2")
            .map_err(database_error)?;
        let versions = stmt
            .query_map([], |r| r.get::<_, i64>(0))
            .map_err(database_error)?
            .collect::<std::result::Result<Vec<_>, _>>()
            .map_err(database_error)?;
        if versions != [1] {
            return Err(Error("unsupported_storage_schema"));
        }
        Ok(())
    }
    fn json(&self, sql: &str, id: &str) -> Result<Option<Json>> {
        self.active()?;
        let mut stmt = self
            .transaction
            .prepare_cached(sql)
            .map_err(database_error)?;
        let raw: Option<String> = stmt
            .query_row([id], |r| r.get(0))
            .optional()
            .map_err(database_error)?;
        raw.map(|value| {
            if value.len() > MAX_BYTES {
                return Err(Error("storage_record_too_large"));
            }
            Json::parse(&value)
        })
        .transpose()
    }
}
impl<Access> AuthorityStore for Session<'_, Access> {
    fn record(&self, kind: Record, id: &str) -> Result<Json> {
        // A bounded SQL substring avoids allocating a corrupt multi-GB body.
        let (table, condition, missing) = match kind {
            Record::Subject => ("identities", " AND kind='subject'", "subject_not_found"),
            Record::Credential => ("credentials", "", "credential_not_found"),
            Record::Certificate => ("certificates", "", "certificate_not_found"),
            Record::Resource => ("resources", "", "not_found"),
        };
        self.json(&format!("SELECT CAST(substr(CAST(body AS BLOB),1,1048577) AS TEXT) FROM {table} WHERE id=?1{condition}"), id)?
            .ok_or(Error(missing))
    }
    fn setting(&self, key: &str) -> Result<Option<Json>> {
        self.json(
            "SELECT CAST(substr(CAST(value AS BLOB),1,1048577) AS TEXT) FROM settings WHERE key=?1",
            key,
        )
    }
    fn has_setting(&self, key: &str) -> Result<bool> {
        self.active()?;
        self.transaction
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM settings WHERE key=?1)",
                [key],
                |r| r.get(0),
            )
            .map_err(database_error)
    }
    fn certificate_revoked(&self, id: &str) -> Result<bool> {
        self.active()?;
        let revoked: Option<i64> = self
            .transaction
            .query_row("SELECT revoked FROM certificates WHERE id=?1", [id], |r| {
                r.get(0)
            })
            .optional()
            .map_err(database_error)?;
        Ok(revoked.ok_or(Error("certificate_not_found"))? != 0)
    }
    fn request_result(
        &self,
        subject: Option<&str>,
        id: &str,
        digest: &str,
    ) -> Result<Option<Json>> {
        self.active()?;
        let row: Option<(String, String)> = self.transaction.query_row(
            "SELECT digest,CAST(substr(CAST(body AS BLOB),1,1048577) AS TEXT) FROM results WHERE subject=?1 AND request_id=?2",
            [subject.unwrap_or(""), id], |r| Ok((r.get(0)?, r.get(1)?))).optional().map_err(database_error)?;
        let Some((stored_digest, raw)) = row else {
            return Ok(None);
        };
        if stored_digest != digest {
            return Err(Error("idempotency_conflict"));
        }
        if raw.len() > MAX_BYTES {
            return Err(Error("storage_record_too_large"));
        }
        let result = Json::parse(&raw)?;
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
        self.active()?;
        let row: Option<(String, String)> = self.transaction.query_row(
            "SELECT expires,CAST(substr(CAST(body AS BLOB),1,1048577) AS TEXT) FROM oauth_states WHERE id=?1",
            [id], |r| Ok((r.get(0)?, r.get(1)?))).optional().map_err(database_error)?;
        row.map(|(expires, body)| {
            if body.len() > MAX_BYTES {
                return Err(Error("storage_record_too_large"));
            }
            Ok(OAuthState {
                expires_at: Timestamp::parse(&expires)?,
                body: Json::parse(&body)?,
            })
        })
        .transpose()
    }
    fn custodial_binding(&self, subject: &str) -> Result<Option<(String, String)>> {
        self.active()?;
        self.transaction
            .query_row(
                "SELECT signing_key_id,status FROM custodial_vault WHERE subject=?1",
                [subject],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()
            .map_err(database_error)
    }
    fn recovery_delivery(
        &self,
        credential: &str,
        subject: &str,
    ) -> Result<Option<RecoveryDelivery>> {
        self.active()?;
        let row: Option<(String, String, bool, String)> = self.transaction.query_row(
            "SELECT recovery_verifier,recovery_expires_at,consumed_at IS NOT NULL,request_id FROM token_deliveries WHERE credential_id=?1 AND subject=?2",
            [credential, subject], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))).optional().map_err(database_error)?;
        row.map(|(verifier, expires, consumed, request_id)| {
            Ok(RecoveryDelivery {
                verifier,
                expires_at: Timestamp::parse(&expires)?,
                consumed,
                request_id,
            })
        })
        .transpose()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        fs,
        time::{SystemTime, UNIX_EPOCH},
    };
    fn path() -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "msg-readonly-{}-{}.db",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }
    #[test]
    fn missing_database_is_not_created() {
        let path = path();
        assert!(SqliteAuthority::open_existing(&path).is_err());
        assert!(!path.exists());
    }
    #[test]
    fn never_creates_or_repairs_a_schema() {
        let path = path();
        Connection::open(&path).unwrap().execute_batch("CREATE TABLE schema_version(version INTEGER); INSERT INTO schema_version VALUES(99);").unwrap();
        let before = fs::read(&path).unwrap();
        let mut store = SqliteAuthority::open_existing(&path).unwrap();
        assert!(matches!(
            store.snapshot(),
            Err(Error("unsupported_storage_schema"))
        ));
        assert_eq!(fs::read(&path).unwrap(), before);
        drop(store);
        fs::remove_file(path).unwrap();
    }
    #[test]
    fn connection_enforces_read_only_at_the_database_layer() {
        let path = path();
        drop(Connection::open(&path).unwrap());
        let store = SqliteAuthority::open_existing(&path).unwrap();
        assert!(store
            .connection
            .execute_batch("CREATE TABLE injected(secret TEXT);")
            .is_err());
        drop(store);
        fs::remove_file(path).unwrap();
    }
}
