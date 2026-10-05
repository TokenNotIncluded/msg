//! Disposable local failure-injection driver, NOT an authenticated executor.
//! No network listener, arbitrary SQL, production credentials or Python process.
use msg_core::{Error, Json, Result};
use msg_identity::store::{AuthorityStore, Record};
use msg_storage::{
    records::{decode, encode},
    SqliteWriter, WriteFailure, WriteSession,
};
use serde::de::DeserializeOwned;
use std::{
    io::{self, Write},
    path::Path,
    time::Duration,
};
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
fn steps(tx: &mut WriteSession<'_>, values: &Json) -> Result<Vec<Json>> {
    let mut output = Vec::new();
    for step in values.as_array()? {
        match field(step, "action")?.as_str()? {
            "set" => tx.set_setting(&get::<String>(step, "key")?, field(step, "value")?)?,
            "read" => output.push(
                tx.setting(&get::<String>(step, "key")?)?
                    .unwrap_or(Json::parse("null")?),
            ),
            "resource" => output.push(tx.record(Record::Resource, &get::<String>(step, "id")?)?),
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
fn run(path: &Path, case: &Json) -> std::result::Result<Vec<Json>, WriteFailure> {
    let timeout = case
        .as_object()?
        .get("timeout_ms")
        .map(decode::<u64>)
        .transpose()?
        .unwrap_or(2000);
    let mut writer = SqliteWriter::open_existing(path, Duration::from_millis(timeout))?;
    let output = writer.transaction(|tx| steps(tx, field(case, "steps")?))?;
    if flag(case, "hold_after_commit")? {
        hold("committed")?;
    }
    Ok(output)
}
fn main() {
    let mut line = String::new();
    let result = (|| {
        let path = std::env::args().nth(1).ok_or(Error("fixture_path"))?;
        io::stdin()
            .read_line(&mut line)
            .map_err(|_| Error("fixture_input"))?;
        run(Path::new(&path), &Json::parse(&line)?)
    })();
    let output = match result {
        Ok(values) => serde_json::json!({"status":"ok","values":values}),
        Err(error) => {
            serde_json::json!({"status":"error","code":error.cause.0,"cleanup":error.cleanup_errors.iter().map(|e| e.0).collect::<Vec<_>>()})
        }
    };
    if emit(&encode(&output).expect("fixture result is bounded")).is_err() {
        std::process::exit(1);
    }
}
