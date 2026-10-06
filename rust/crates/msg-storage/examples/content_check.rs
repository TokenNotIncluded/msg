//! Disposable JSONL parity/crash driver, NOT an authenticated operation API.
//! The root must contain an explicit test marker. No listener or Python adapter.
use msg_core::{b64, unb64, Error, Json, Result};
use msg_storage::{
    content::{ContentOptions, GitContentStore},
    records::{decode, encode, BlobRef},
};
use serde::de::DeserializeOwned;
use sha2::{Digest, Sha256};
use std::{
    io::{self, Read, Write},
    path::Path,
};
fn get<T: DeserializeOwned>(v: &Json, key: &str) -> Result<T> {
    decode(v.as_object()?.get(key).ok_or(Error("fixture_field"))?)
}
fn emit(value: &Json) -> Result<()> {
    println!("{}", value.canonical()?);
    io::stdout().flush().map_err(|_| Error("fixture_output"))
}
fn hold() -> Result<()> {
    emit(&Json::parse(r#"{"checkpoint":"held"}"#)?)?;
    let mut input = String::new();
    io::stdin()
        .read_line(&mut input)
        .map_err(|_| Error("fixture_input"))?;
    if input.trim() != "continue" {
        return Err(Error("fixture_input"));
    }
    Ok(())
}
struct Generated {
    remaining: u64,
    pause: bool,
    started: bool,
}
impl Read for Generated {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        if self.pause && self.started {
            self.pause = false;
            hold().map_err(io::Error::other)?;
        }
        self.started = true;
        let length = bytes.len().min(self.remaining as usize);
        bytes[..length].fill(b'x');
        self.remaining -= length as u64;
        Ok(length)
    }
}
struct HashSink {
    hash: Sha256,
    count: u64,
}
impl Write for HashSink {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.hash.update(bytes);
        self.count += bytes.len() as u64;
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
fn dispatch(store: &GitContentStore, value: &Json) -> Result<Json> {
    match get::<String>(value, "action")?.as_str() {
        "put" => {
            let bytes = unb64(&get::<String>(value, "bytes")?, 524_288)?;
            let expected: Option<String> = get(value, "expected")?;
            encode(&store.put_bytes(
                &bytes,
                &get::<String>(value, "media_type")?,
                expected.as_deref(),
            )?)
        }
        "generated" => {
            let length: u64 = get(value, "length")?;
            if length > 16_777_216 {
                return Err(Error("fixture_limit"));
            }
            let mut input = Generated {
                remaining: length,
                pause: get(value, "hold_during")?,
                started: false,
            };
            let blob = store.put(
                &mut input,
                &get::<String>(value, "media_type")?,
                None,
                length,
            )?;
            if get::<bool>(value, "hold_after")? {
                hold()?;
            }
            encode(&blob)
        }
        "read" => {
            let blob: BlobRef = get(value, "blob")?;
            let range: Option<(u64, u64)> = get(value, "range")?;
            if range
                .map(|(a, b)| b.saturating_sub(a))
                .unwrap_or(blob.size.max(0) as u64)
                > 524_288
            {
                return Err(Error("fixture_limit"));
            }
            let mut bytes = Vec::new();
            store.read_to(&blob, range, &mut bytes)?;
            Ok(Json::string(b64(&bytes)))
        }
        "hash" => {
            let mut sink = HashSink {
                hash: Sha256::new(),
                count: 0,
            };
            store.read_to(&get(value, "blob")?, None, &mut sink)?;
            encode(
                &serde_json::json!({"digest":format!("sha256:{:x}",sink.hash.finalize()),"size":sink.count}),
            )
        }
        "pin" | "unpin" | "pinned" => {
            let blob = get(value, "blob")?;
            let lease: String = get(value, "lease")?;
            match get::<String>(value, "action")?.as_str() {
                "pin" => store.pin(&blob, &lease)?,
                "unpin" => store.unpin(&blob, &lease)?,
                _ => (),
            }
            encode(&store.pinned(&blob, &lease)?)
        }
        "revision" => encode(
            &store.commit_revision(&get::<String>(value, "topic")?, &get(value, "revision")?)?,
        ),
        _ => Err(Error("fixture_action")),
    }
}
fn main() -> std::result::Result<(), Box<dyn std::error::Error>> {
    let root = std::env::args().nth(1).ok_or("fixture_root")?;
    let root = Path::new(&root);
    if std::fs::read(root.join(".msg-native-content-test"))? != b"disposable test content\n" {
        return Err("fixture_marker".into());
    }
    let store = GitContentStore::open(root, ContentOptions::default())?;
    loop {
        let mut line = String::new();
        if io::stdin().read_line(&mut line)? == 0 {
            break;
        }
        let result = Json::parse(line.trim_end()).and_then(|value| dispatch(&store, &value));
        let output = match result {
            Ok(data) => encode(&serde_json::json!({"data":data}))?,
            Err(e) => encode(&serde_json::json!({"error":e.0}))?,
        };
        emit(&output)?;
    }
    Ok(())
}
