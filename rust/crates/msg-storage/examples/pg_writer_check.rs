//! Disposable PostgreSQL failure-injection driver, NOT an authenticated executor.
//! No network listener, arbitrary SQL, production credentials or Python process.
use msg_core::{Error, Json, Result};
use msg_identity::store::{AuthorityStore, Record};
use msg_storage::{
    postgres::{PgFailure, PgSession, PgStore},
    records::{decode, encode, IdentityRecord},
};
use serde::de::DeserializeOwned;
use std::io::{self, Write};
fn field<'a>(value: &'a Json, key: &str) -> Result<&'a Json> {
    value.as_object()?.get(key).ok_or(Error("fixture_field"))
}
fn get<T: DeserializeOwned>(value: &Json, key: &str) -> Result<T> {
    decode(field(value, key)?)
}
fn flag(value: &Json, key: &str) -> Result<bool> {
    value
        .as_object()?
        .get(key)
        .map(Json::as_bool)
        .transpose()
        .map(|v| v.unwrap_or(false))
}
fn emit(value: &Json) -> Result<()> {
    println!("{}", value.canonical()?);
    io::stdout().flush().map_err(|_| Error("fixture_output"))
}
fn hold(name: &str) -> Result<()> {
    emit(&encode(&serde_json::json!({"checkpoint":name}))?)?;
    let mut line = String::new();
    io::stdin()
        .read_line(&mut line)
        .map_err(|_| Error("fixture_input"))?;
    if line.trim() != "continue" {
        return Err(Error("fixture_input"));
    }
    Ok(())
}
fn steps(tx: &PgSession, values: &Json) -> Result<Vec<Json>> {
    let mut output = Vec::new();
    for step in values.as_array()? {
        match field(step, "action")?.as_str()? {
            "set" => tx.set_setting(&get::<String>(step, "key")?, field(step, "value")?)?,
            "read" => output.push(
                tx.setting(&get::<String>(step, "key")?)?
                    .unwrap_or(Json::parse("null")?),
            ),
            "resource" => output.push(encode(&tx.resource_record(&get::<String>(step, "id")?)?)?),
            "identity" => {
                let expected = get(step, "expected")?;
                match get::<String>(step, "record_kind")?.as_str() {
                    "subject" => tx.update_identity(
                        IdentityRecord::Subject(&get(step, "record")?),
                        expected,
                    )?,
                    "organization" => tx.update_identity(
                        IdentityRecord::Organization(&get(step, "record")?),
                        expected,
                    )?,
                    "membership" => tx.update_identity(
                        IdentityRecord::Membership(&get(step, "record")?),
                        expected,
                    )?,
                    "email" => {
                        tx.update_identity(IdentityRecord::Email(&get(step, "record")?), expected)?
                    }
                    _ => return Err(Error("fixture_action")),
                }
            }
            "subject" => output.push(tx.record(Record::Subject, &get::<String>(step, "id")?)?),
            "credential" => tx.save_credential(&get(step, "record")?, get(step, "expected")?)?,
            "get_credential" => {
                output.push(tx.record(Record::Credential, &get::<String>(step, "id")?)?)
            }
            "csr" => tx.save_csr(&get(step, "record")?)?,
            "csr_state" => output.push(encode(&tx.csr_state(&get::<String>(step, "id")?)?)?),
            "transition_csr" => tx.transition_csr(&get(step, "record")?, get(step, "expected")?)?,
            "certificate" => {
                let id: Option<String> = get(step, "csr")?;
                tx.register_certificate(
                    &get(step, "record")?,
                    id.as_deref()
                        .map(|id| Ok((id, get(step, "expected")?)))
                        .transpose()?,
                )?;
            }
            "revoke_certificate" => {
                tx.revoke_certificate(&get::<String>(step, "id")?, &get(step, "event")?)?
            }
            "get_certificate" => {
                output.push(tx.record(Record::Certificate, &get::<String>(step, "id")?)?)
            }
            "transfer" => tx.save_transfer(&get(step, "record")?, get(step, "expected")?)?,
            "get_transfer" => output.push(encode(&tx.transfer(&get::<String>(step, "id")?)?)?),
            "chunk" => tx.put_chunk(&get(step, "record")?)?,
            "missing" => output.push(encode(&tx.missing_ranges(
                &get::<String>(step, "id")?,
                get::<Option<String>>(step, "cursor")?.as_deref(),
                get(step, "limit")?,
            )?)?),
            "enqueue" => tx.enqueue(&get(step, "record")?)?,
            "job" => output.push(encode(&tx.job(&get::<String>(step, "id")?)?)?),
            "save_job" => tx.save_job(&get(step, "record")?)?,
            "path" => output.push(Json::string(tx.resource_path(&get::<String>(step, "id")?)?)),
            "resolve" => output.push(Json::string(
                tx.resolve(&get::<String>(step, "path")?, flag(step, "migrated")?)?,
            )),
            "children" => output.push(encode(&tx.children(
                &get::<String>(step, "id")?,
                get::<Option<String>>(step, "cursor")?.as_deref(),
                get(step, "limit")?,
            )?)?),
            "history" => output.push(encode(&tx.history(
                &get::<String>(step, "id")?,
                get::<Option<String>>(step, "cursor")?.as_deref(),
                get(step, "limit")?,
            )?)?),
            "get_revision" => output.push(encode(&tx.revision(&get(step, "reference")?)?)?),
            "insert" => tx.insert_resource(&get(step, "record")?)?,
            "replace" => tx.replace_resource(&get(step, "record")?, get(step, "expected")?)?,
            "revision" => tx.append_revision(&get(step, "record")?)?,
            "result" => tx.save_result(
                get::<Option<String>>(step, "subject")?.as_deref(),
                &get::<String>(step, "digest")?,
                &get(step, "record")?,
            )?,
            "lookup" => output.push(
                tx.request_result(
                    get::<Option<String>>(step, "subject")?.as_deref(),
                    &get::<String>(step, "id")?,
                    &get::<String>(step, "digest")?,
                )?
                .unwrap_or(Json::parse("null")?),
            ),
            "event" => tx.append_event(&get(step, "record")?)?,
            "audit" => tx.append_audit(&get(step, "record")?)?,
            "savepoint" => match tx.savepoint(|tx| steps(tx, field(step, "steps")?)) {
                Ok(values) => output.push(encode(&values)?),
                Err(error) if flag(step, "recover")? => {
                    output.push(encode(&serde_json::json!({"error":error.0}))?)
                }
                Err(error) => return Err(error),
            },
            "ignore" => {
                let _ = steps(tx, field(step, "steps")?);
            }
            "effect" => {
                let name: String = get(step, "name")?;
                let fail = flag(step, "fail")?;
                let panic = flag(step, "panic")?;
                let pause = flag(step, "hold")?;
                tx.on_rollback(move || {
                    emit(&encode(&serde_json::json!({"effect":name}))?)?;
                    if pause {
                        hold("compensation")?;
                    }
                    assert!(!panic, "fixture cleanup panic");
                    if fail {
                        return Err(Error("fixture_cleanup"));
                    }
                    Ok(())
                })?;
            }
            "hold" => hold("transaction")?,
            "fail" => return Err(Error("fixture_failure")),
            "panic" => panic!("fixture handler panic"),
            // This is only a test of the storage building blocks. There is NO
            // authentication or authorization here; never expose it as an API.
            "once" => {
                let subject: Option<String> = get(step, "subject")?;
                let digest: String = get(step, "digest")?;
                let record: msg_storage::records::OperationResult = get(step, "record")?;
                if let Some(result) =
                    tx.request_result(subject.as_deref(), &record.request_id, &digest)?
                {
                    output.push(result);
                } else {
                    steps(tx, field(step, "steps")?)?;
                    tx.save_result(subject.as_deref(), &digest, &record)?;
                    output.push(encode(&record)?);
                }
            }
            _ => return Err(Error("fixture_action")),
        }
    }
    Ok(output)
}
fn run(case: &Json) -> std::result::Result<Vec<Json>, PgFailure> {
    let dsn = std::env::var("MSG_PARITY_POSTGRES_DSN").map_err(|_| Error("fixture_dsn"))?;
    let store = PgStore::open_existing(&dsn)?;
    let output = store.transaction(!flag(case, "read_only")?, |tx| {
        steps(tx, field(case, "steps")?)
    })?;
    if flag(case, "hold_after_commit")? {
        hold("committed")?;
    }
    Ok(output)
}
fn main() {
    let mut line = String::new();
    let result = (|| {
        io::stdin()
            .read_line(&mut line)
            .map_err(|_| Error("fixture_input"))?;
        run(&Json::parse(&line)?)
    })();
    let output = match result {
        Ok(values) => serde_json::json!({"status":"ok","values":values}),
        Err(error) => {
            serde_json::json!({"status":"error","code":error.code(),"cleanup":if matches!(error,PgFailure::Rejected{cleanup_failed:true,..}) {vec!["storage_cleanup_failed"]} else {vec![]},"uncertain":matches!(error,PgFailure::Uncertain{..})})
        }
    };
    if emit(&encode(&output).expect("fixture result is bounded")).is_err() {
        std::process::exit(1);
    }
}
