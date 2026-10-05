//! Native PostgreSQL transactions over the existing, Python-migrated schema.
//!
//! One dedicated connection owns each transaction. Writers use the same
//! session advisory lock as Python and retain it through all compensations.
//! Transaction pooling is unsupported. No schema is created or upgraded here.
mod authority;
mod query;
mod records;
use msg_core::{Error, Result};
use postgres::{fallible_iterator::FallibleIterator, types::ToSql, Client, Config};
use postgres_native_tls::MakeTlsConnector;
pub use query::{Row, Value};
use std::{
    cell::{Cell, RefCell},
    fmt,
    marker::PhantomData,
    panic::{catch_unwind, resume_unwind, AssertUnwindSafe},
    rc::Rc,
    str::FromStr,
    time::Duration,
};

const MAX_ROWS: usize = 4096;
const MAX_QUERY_BYTES: usize = 8 * 1024 * 1024;
type Cleanup = Box<dyn FnOnce() -> Result<()>>;

#[derive(Clone)]
pub struct PgStore {
    config: Config,
    tls: native_tls::TlsConnector,
}
impl fmt::Debug for PgStore {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("PgStore(<private connection configuration>)")
    }
}
#[derive(Debug)]
pub enum PgFailure {
    Rejected {
        error: Error,
        cleanup_failed: bool,
    },
    /// Do not retry under a new request ID, delete blobs, or issue secrets.
    Uncertain {
        error: Error,
    },
}
impl PgFailure {
    pub fn code(&self) -> &'static str {
        match self {
            Self::Rejected { error, .. } | Self::Uncertain { error } => error.0,
        }
    }
}
impl From<Error> for PgFailure {
    fn from(error: Error) -> Self {
        Self::Rejected {
            error,
            cleanup_failed: false,
        }
    }
}
pub struct PgSession {
    client: RefCell<Client>,
    write: bool,
    closed: Cell<bool>,
    poisoned: Cell<Option<Error>>,
    rollback: RefCell<Vec<Cleanup>>,
    savepoints: Cell<u64>,
    _owner: PhantomData<Rc<()>>,
}
fn database_error(error: &postgres::Error) -> Error {
    let code = error.code().map(|c| c.code()).unwrap_or("");
    if code.starts_with("23") {
        Error("constraint_conflict")
    } else if matches!(
        code,
        "40P01" | "40001" | "55P03" | "57014" | "53300" | "57P03"
    ) {
        Error("server_busy")
    } else if code == "25006" {
        Error("read_only_transaction")
    } else {
        Error("storage_error")
    }
}
impl PgStore {
    pub fn open_existing(dsn: &str) -> Result<Self> {
        let mut config = Config::from_str(dsn).map_err(|_| Error("invalid_postgres_config"))?;
        config.connect_timeout(Duration::from_secs(10));
        config.application_name("msgd-native");
        let local = config.get_hosts().iter().all(|host| match host {
            postgres::config::Host::Tcp(host) => {
                host == "localhost"
                    || host
                        .parse::<std::net::IpAddr>()
                        .is_ok_and(|ip| ip.is_loopback())
            }
            #[cfg(unix)]
            postgres::config::Host::Unix(_) => true,
        }) && config
            .get_hostaddrs()
            .iter()
            .all(std::net::IpAddr::is_loopback);
        if !local {
            if config.get_ssl_mode() == postgres::config::SslMode::Disable {
                return Err(Error("postgres_tls_required"));
            }
            // A remote connection must not fall back to plaintext when the peer
            // rejects TLS. Certificate/name validation is still enforced below.
            config.ssl_mode(postgres::config::SslMode::Require);
        }
        // native-tls validates server certificates and names whenever TLS is used.
        // There is no dangerous_accept_invalid_certs/hostnames setting.
        let tls = native_tls::TlsConnector::builder()
            .build()
            .map_err(|_| Error("postgres_tls_error"))?;
        let store = Self { config, tls };
        store
            .transaction(false, |tx| tx.validate_schema())
            .map_err(|e| Error(e.code()))?;
        Ok(store)
    }
    fn connect(&self) -> Result<Client> {
        self.config
            .connect(MakeTlsConnector::new(self.tls.clone()))
            .map_err(|e| database_error(&e))
    }
    pub fn transaction<T>(
        &self,
        write: bool,
        body: impl FnOnce(&PgSession) -> Result<T>,
    ) -> std::result::Result<T, PgFailure> {
        let mut client = self.connect()?;
        client.batch_execute("SET search_path = pg_catalog, public; SET statement_timeout='30s'; SET lock_timeout='10s'; SET idle_in_transaction_session_timeout='60s';")
            .map_err(|e|PgFailure::from(database_error(&e)))?;
        if write {
            client
                .query_one("SELECT pg_advisory_lock(725274758,1886265951)", &[])
                .map_err(|e| PgFailure::from(database_error(&e)))?;
        }
        // Acquire the writer fence before taking the snapshot. Read-only requests
        // also see one committed generation, not a mixture of policy revisions.
        client
            .batch_execute(if write {
                "BEGIN ISOLATION LEVEL REPEATABLE READ"
            } else {
                "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
            })
            .map_err(|e| PgFailure::from(database_error(&e)))?;
        let session = PgSession {
            client: RefCell::new(client),
            write,
            closed: Cell::new(false),
            poisoned: Cell::new(None),
            rollback: RefCell::new(Vec::new()),
            savepoints: Cell::new(0),
            _owner: PhantomData,
        };
        let outcome = catch_unwind(AssertUnwindSafe(|| body(&session)));
        match outcome {
            Ok(Ok(value)) if session.poisoned.get().is_none() => {
                let commit = session.client.borrow_mut().batch_execute("COMMIT");
                match commit {
                    Ok(()) => {
                        session.closed.set(true);
                        session.rollback.borrow_mut().clear();
                        Ok(value)
                    }
                    Err(error) => {
                        let definitely_aborted = error.code().is_some_and(|c| {
                            c.code().starts_with("23")
                                || matches!(c.code(), "40001" | "40P01" | "25P02")
                        });
                        if definitely_aborted
                            && session
                                .client
                                .borrow_mut()
                                .batch_execute("ROLLBACK")
                                .is_ok()
                        {
                            let failed = session.compensate(0);
                            session.closed.set(true);
                            Err(PgFailure::Rejected {
                                error: database_error(&error),
                                cleanup_failed: failed,
                            })
                        } else {
                            session.closed.set(true);
                            session.rollback.borrow_mut().clear();
                            Err(PgFailure::Uncertain {
                                error: Error("storage_commit_uncertain"),
                            })
                        }
                    }
                }
            }
            result => {
                let confirmed = session
                    .client
                    .borrow_mut()
                    .batch_execute("ROLLBACK")
                    .is_ok();
                let cleanup_failed = if confirmed {
                    session.compensate(0)
                } else {
                    session.rollback.borrow_mut().clear();
                    false
                };
                session.closed.set(true);
                match result {
                    Err(panic) => resume_unwind(panic),
                    result => {
                        let error = match result {
                            Ok(Err(error)) => error,
                            _ => session
                                .poisoned
                                .get()
                                .unwrap_or(Error("transaction_aborted")),
                        };
                        if confirmed {
                            Err(PgFailure::Rejected {
                                error,
                                cleanup_failed,
                            })
                        } else {
                            Err(PgFailure::Uncertain {
                                error: Error("storage_rollback_uncertain"),
                            })
                        }
                    }
                }
            }
        }
        // Closing this dedicated connection releases the advisory lock only after
        // compensation completes. A transaction-pool proxy breaks that guarantee.
    }
}
impl PgSession {
    fn active(&self, write: bool) -> Result<()> {
        if self.closed.get() {
            return Err(Error("transaction_closed"));
        }
        if let Some(e) = self.poisoned.get() {
            return Err(e);
        }
        if write && !self.write {
            return Err(Error("read_only_transaction"));
        }
        Ok(())
    }
    fn capture<T>(&self, result: Result<T>, write: bool) -> Result<T> {
        if let Err(e) = result {
            if write {
                self.poisoned.set(Some(e));
            }
            return Err(e);
        }
        result
    }
    pub fn require_write(&self) -> Result<()> {
        self.active(true)
    }
    /// SQL is a compiled, trusted string; only values may originate in a request.
    pub fn rows(&self, sql: &'static str, params: &[Value]) -> Result<Vec<Row>> {
        self.active(false)?;
        let refs: Vec<&(dyn ToSql + Sync)> =
            params.iter().map(|p| p as &(dyn ToSql + Sync)).collect();
        let result = (|| {
            let mut client = self.client.borrow_mut();
            let mut iter = client
                .query_raw(sql, refs)
                .map_err(|e| database_error(&e))?;
            let mut rows = Vec::new();
            let mut bytes = 0;
            while let Some(row) = iter.next().map_err(|e| database_error(&e))? {
                let row = Row::decode(row)?;
                bytes += row.bytes();
                if rows.len() >= MAX_ROWS || bytes > MAX_QUERY_BYTES {
                    return Err(Error("storage_query_limit"));
                }
                rows.push(row);
            }
            Ok(rows)
        })();
        self.capture(result, self.write)
    }
    pub fn one(&self, sql: &'static str, params: &[Value]) -> Result<Option<Row>> {
        let mut rows = self.rows(sql, params)?;
        if rows.len() > 1 {
            return self.capture(Err(Error("storage_record_ambiguous")), self.write);
        }
        Ok(rows.pop())
    }
    pub fn execute(&self, sql: &'static str, params: &[Value]) -> Result<u64> {
        self.active(true)?;
        let refs: Vec<&(dyn ToSql + Sync)> =
            params.iter().map(|p| p as &(dyn ToSql + Sync)).collect();
        let result = self
            .client
            .borrow_mut()
            .execute(sql, &refs)
            .map_err(|e| database_error(&e));
        self.capture(result, true)
    }
    pub fn on_rollback(&self, effect: impl FnOnce() -> Result<()> + 'static) -> Result<()> {
        self.active(true)?;
        self.rollback.borrow_mut().push(Box::new(effect));
        Ok(())
    }
    fn compensate(&self, start: usize) -> bool {
        let effects: Vec<_> = self.rollback.borrow_mut().drain(start..).collect();
        let mut failed = false;
        for effect in effects.into_iter().rev() {
            failed |= !matches!(catch_unwind(AssertUnwindSafe(effect)), Ok(Ok(())));
        }
        failed
    }
    pub fn savepoint<T>(&self, body: impl FnOnce(&Self) -> Result<T>) -> Result<T> {
        self.active(false)?;
        let id = self
            .savepoints
            .get()
            .checked_add(1)
            .ok_or(Error("storage_savepoint_limit"))?;
        self.savepoints.set(id);
        // Names contain only an internal integer. They never contain user strings.
        self.client
            .borrow_mut()
            .batch_execute(&format!("SAVEPOINT msg_native_{id}"))
            .map_err(|e| database_error(&e))?;
        let start = self.rollback.borrow().len();
        let result = catch_unwind(AssertUnwindSafe(|| body(self)));
        if matches!(&result, Ok(Ok(_))) && self.poisoned.get().is_none() {
            let release = self
                .client
                .borrow_mut()
                .batch_execute(&format!("RELEASE SAVEPOINT msg_native_{id}"))
                .map_err(|e| database_error(&e));
            self.capture(release, self.write)?;
            return match result {
                Ok(value) => value,
                Err(p) => resume_unwind(p),
            };
        }
        let rollback = self.client.borrow_mut().batch_execute(&format!(
            "ROLLBACK TO SAVEPOINT msg_native_{id}; RELEASE SAVEPOINT msg_native_{id}"
        ));
        if let Err(error) = rollback {
            let error = database_error(&error);
            self.poisoned.set(Some(error));
            return Err(error);
        }
        let poison = self.poisoned.take();
        let cleanup_failed = self.compensate(start);
        if cleanup_failed {
            self.poisoned.set(Some(Error("storage_cleanup_failed")));
        }
        match result {
            Err(p) => resume_unwind(p),
            Ok(Err(e)) => Err(e),
            Ok(Ok(_)) => Err(poison.unwrap_or(Error("transaction_aborted"))),
        }
    }
    fn validate_schema(&self) -> Result<()> {
        let versions = self
            .rows(
                "SELECT version FROM public.schema_version ORDER BY version LIMIT 2",
                &[],
            )
            .map_err(|_| Error("unsupported_storage_schema"))?;
        if versions.len() != 1 || versions[0].integer(0)? != 1 {
            return Err(Error("unsupported_storage_schema"));
        }
        let required = [
            (
                "resources",
                vec![
                    "id",
                    "type",
                    "name",
                    "parent",
                    "owner",
                    "grp",
                    "mode",
                    "generation",
                    "revision",
                    "state",
                    "created_at",
                    "modified_at",
                    "body",
                ],
            ),
            ("identities", vec!["id", "kind", "generation", "body"]),
            ("credentials", vec!["id", "subject", "body"]),
            (
                "certificates",
                vec!["id", "subject", "parent", "revoked", "body"],
            ),
            ("settings", vec!["key", "value"]),
            ("results", vec!["subject", "request_id", "digest", "body"]),
            ("oauth_states", vec!["id", "kind", "expires", "body"]),
            ("revisions", vec!["id", "resource_id", "created_at", "body"]),
            (
                "relations",
                vec![
                    "revision_id",
                    "source_id",
                    "type",
                    "target_id",
                    "target_revision",
                    "body",
                ],
            ),
            (
                "custodial_vault",
                vec!["subject", "signing_key_id", "status"],
            ),
            (
                "token_deliveries",
                vec![
                    "credential_id",
                    "subject",
                    "request_id",
                    "recovery_verifier",
                    "recovery_expires_at",
                    "consumed_at",
                ],
            ),
            ("memberships", vec!["org", "subject", "generation", "body"]),
            ("emails", vec!["subject", "generation", "body"]),
            (
                "csrs",
                vec!["id", "generation", "state", "body", "state_body"],
            ),
            (
                "transfers",
                vec!["id", "subject", "generation", "body", "limits"],
            ),
            ("chunks", vec!["transfer_id", "offset", "length", "body"]),
            (
                "jobs",
                vec!["id", "dedupe", "kind", "state", "next_at", "body"],
            ),
            ("resource_tags", vec!["resource_id", "tag"]),
            (
                "resource_path_aliases",
                vec!["parent_id", "name", "resource_id"],
            ),
            ("events", vec!["seq", "id", "body"]),
            ("audit", vec!["seq", "digest", "previous", "body"]),
        ];
        for (table, columns) in required {
            let row=self.one("SELECT count(*) FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname=$1 AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped AND a.attname::text=ANY($2)",&[table.into(),Value::TextArray(columns.iter().map(|s|s.to_string()).collect())])?.ok_or(Error("unsupported_storage_schema"))?;
            if row.integer(0)? != columns.len() as i64 {
                return Err(Error("unsupported_storage_schema"));
            }
        }
        for (table, columns) in [
            ("resources", vec!["id"]),
            ("revisions", vec!["id"]),
            ("identities", vec!["id"]),
            ("credentials", vec!["id"]),
            ("certificates", vec!["id"]),
            ("csrs", vec!["id"]),
            ("results", vec!["subject", "request_id"]),
            ("settings", vec!["key"]),
            ("memberships", vec!["org", "subject"]),
            ("emails", vec!["subject"]),
            ("transfers", vec!["id"]),
            ("chunks", vec!["transfer_id", "offset"]),
            ("jobs", vec!["id"]),
            ("events", vec!["seq"]),
            ("audit", vec!["seq"]),
            ("resource_path_aliases", vec!["parent_id", "name"]),
            ("resource_tags", vec!["tag", "resource_id"]),
        ] {
            self.require_constraint(table, "p", &columns)?;
        }
        for (table, columns) in [
            ("resources", vec!["parent", "name"]),
            ("events", vec!["id"]),
            ("audit", vec!["digest"]),
            ("jobs", vec!["dedupe"]),
        ] {
            self.require_constraint(table, "u", &columns)?;
        }
        for (table, source, target, column) in [
            ("resources", "parent", "resources", "id"),
            ("revisions", "resource_id", "resources", "id"),
            ("relations", "revision_id", "revisions", "id"),
            ("resource_tags", "resource_id", "resources", "id"),
            ("resource_path_aliases", "resource_id", "resources", "id"),
            ("resource_path_aliases", "parent_id", "resources", "id"),
        ] {
            let row=self.one("SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_constraint c JOIN pg_catalog.pg_class t ON t.oid=c.conrelid JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace JOIN pg_catalog.pg_class r ON r.oid=c.confrelid JOIN pg_catalog.pg_namespace rn ON rn.oid=r.relnamespace WHERE n.nspname='public' AND rn.nspname='public' AND t.relname=$1 AND r.relname=$3 AND c.contype='f' AND c.convalidated AND ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY k(attnum,ord) JOIN pg_catalog.pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum ORDER BY k.ord)=ARRAY[$2]::text[] AND ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY k(attnum,ord) JOIN pg_catalog.pg_attribute a ON a.attrelid=r.oid AND a.attnum=k.attnum ORDER BY k.ord)=ARRAY[$4]::text[])",&[table.into(),source.into(),target.into(),column.into()])?.ok_or(Error("unsupported_storage_schema"))?;
            if !row.boolean(0)? {
                return Err(Error("unsupported_storage_schema"));
            }
        }
        let root=self.one("SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class t ON t.oid=i.indrelid JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname='public' AND t.relname='resources' AND i.indisunique AND i.indisvalid AND i.indisready AND i.indnkeyatts=1 AND pg_catalog.pg_get_expr(i.indexprs,i.indrelid)='1' AND pg_catalog.pg_get_expr(i.indpred,i.indrelid)='(parent IS NULL)')",&[])?.ok_or(Error("unsupported_storage_schema"))?;
        if !root.boolean(0)? {
            return Err(Error("unsupported_storage_schema"));
        }
        Ok(())
    }
    fn require_constraint(&self, table: &str, kind: &str, columns: &[&str]) -> Result<()> {
        let row=self.one("SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_constraint c JOIN pg_catalog.pg_class t ON t.oid=c.conrelid JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname='public' AND t.relname=$1 AND c.contype::text=$2 AND c.convalidated AND ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY k(attnum,ord) JOIN pg_catalog.pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum ORDER BY k.ord)=$3)",&[table.into(),kind.into(),Value::TextArray(columns.iter().map(|s|s.to_string()).collect())])?.ok_or(Error("unsupported_storage_schema"))?;
        if !row.boolean(0)? {
            return Err(Error("unsupported_storage_schema"));
        }
        Ok(())
    }
}
