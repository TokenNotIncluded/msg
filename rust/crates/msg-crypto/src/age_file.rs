//! Bounded interoperability with the existing age executable. Secret plaintext
//! stays inside this process/pipe; callers receive ciphertext only.
use crate::age_recipient;
use msg_core::{Error, Result};
use std::{
    io::{Read, Write},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};
use zeroize::Zeroizing;

const HEADER: &[u8] = b"age-encryption.org/v1\n";
const MAX_PLAIN: usize = 1_048_576;
const MAX_CIPHER: usize = 1_114_112;

fn run(arguments: &[&str], input: &[u8], limit: usize) -> Result<Zeroizing<Vec<u8>>> {
    let mut child = Command::new("age")
        .args(arguments)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| {
            Error(if error.kind() == std::io::ErrorKind::NotFound {
                "age_dependency_unavailable"
            } else {
                "custodial_rewrap_operation_failed"
            })
        })?;
    let mut stdin = child
        .stdin
        .take()
        .ok_or(Error("custodial_rewrap_operation_failed"))?;
    let stdout = child
        .stdout
        .take()
        .ok_or(Error("custodial_rewrap_operation_failed"))?;
    thread::scope(|scope| {
        let writer = scope.spawn(move || stdin.write_all(input));
        let reader = scope.spawn(move || {
            let mut result = Zeroizing::new(Vec::new());
            stdout.take(limit as u64 + 1).read_to_end(&mut result)?;
            Ok::<_, std::io::Error>(result)
        });
        let deadline = Instant::now() + Duration::from_secs(15);
        let status = loop {
            match child.try_wait() {
                Ok(Some(status)) => break Ok(status),
                Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(10)),
                _ => {
                    let _ = child.kill();
                    let _ = child.wait();
                    break Err(Error("custodial_rewrap_operation_failed"));
                }
            }
        };
        let write = writer
            .join()
            .map_err(|_| Error("custodial_rewrap_operation_failed"))?;
        let output = reader
            .join()
            .map_err(|_| Error("custodial_rewrap_operation_failed"))?
            .map_err(|_| Error("custodial_rewrap_operation_failed"))?;
        if !status?.success() || write.is_err() || output.len() > limit {
            return Err(Error("custodial_rewrap_operation_failed"));
        }
        Ok(output)
    })
}

pub fn encrypt(recipient: &str, plaintext: &[u8]) -> Result<Vec<u8>> {
    age_recipient::public_from_recipient(recipient)?;
    if plaintext.len() > MAX_PLAIN {
        return Err(Error("custodial_rewrap_encrypt_failed"));
    }
    let output = run(
        &["--encrypt", "--recipient", recipient],
        plaintext,
        MAX_CIPHER,
    )?;
    if !output.starts_with(HEADER) {
        return Err(Error("custodial_rewrap_encrypt_failed"));
    }
    Ok(output.to_vec())
}

#[cfg(target_os = "linux")]
pub fn rewrap(identity: &str, recipient: &str, ciphertext: &[u8]) -> Result<Vec<u8>> {
    use std::{
        fs::File,
        io::{Seek, SeekFrom},
        os::fd::AsRawFd,
    };
    age_recipient::private_from_identity(identity)?;
    age_recipient::public_from_recipient(recipient)?;
    if ciphertext.len() > MAX_PLAIN || !ciphertext.starts_with(HEADER) {
        return Err(Error("custodial_rewrap_ciphertext_invalid"));
    }
    // CLOEXEC prevents another concurrently spawned worker from inheriting this
    // secret fd. age opens the parent's private, unnamed file only for this call.
    let fd = rustix::fs::memfd_create("msg-custodial-age", rustix::fs::MemfdFlags::CLOEXEC)
        .map_err(|_| Error("custodial_rewrap_operation_failed"))?;
    let mut file = File::from(fd);
    file.write_all(identity.as_bytes())
        .and_then(|_| file.write_all(b"\n"))
        .and_then(|_| file.seek(SeekFrom::Start(0)).map(|_| ()))
        .map_err(|_| Error("custodial_rewrap_operation_failed"))?;
    let path = format!("/proc/{}/fd/{}", std::process::id(), file.as_raw_fd());
    let plaintext = run(&["--decrypt", "--identity", &path], ciphertext, MAX_PLAIN)?;
    encrypt(recipient, &plaintext)
}

#[cfg(not(target_os = "linux"))]
pub fn rewrap(_identity: &str, _recipient: &str, _ciphertext: &[u8]) -> Result<Vec<u8>> {
    Err(Error("custodial_rewrap_platform_unavailable"))
}
