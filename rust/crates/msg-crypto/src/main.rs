//! Small, offline encryption CLI. No account, network or server dependency.
use msg_core::{Error, Result};
use std::io::Write;

const HELP: &str = "msg-crypto — local MSG encryption\n\
Usage:\n\
  msg-crypto keygen PRIVATE_KEY_FILE\n\
  msg-crypto encrypt RECIPIENT_BASE64 INPUT OUTPUT\n\
  msg-crypto decrypt PRIVATE_KEY_FILE INPUT OUTPUT\n\n\
Outputs are new private files; existing paths are never overwritten.\n\
Keep the private key locally. Share only the recipient public key.\n\
Encryption does not authenticate the sender. Unix only.";

fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            let _ = writeln!(std::io::stderr().lock(), "msg-crypto: {error}");
            std::process::ExitCode::FAILURE
        }
    }
}

fn run() -> Result<()> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.is_empty() || matches!(args.as_slice(), [arg] if arg == "--help" || arg == "-h") {
        return print_output(HELP);
    }
    #[cfg(unix)]
    {
        local::run(&args)
    }
    #[cfg(not(unix))]
    {
        Err(Error("unix_required_for_private_files"))
    }
}

fn print_output(text: &str) -> Result<()> {
    writeln!(std::io::stdout().lock(), "{text}").map_err(|_| Error("stdout_write_failed"))
}

#[cfg(unix)]
mod local {
    use super::*;
    use msg_core::{b64, unb64};
    use msg_crypto::sealed_box::{
        decrypt, encrypt, generate_key, FORMAT, MAX_ENVELOPE_BYTES, MAX_PLAINTEXT_BYTES,
    };
    use std::{
        ffi::OsString,
        fs::{self, DirBuilder, OpenOptions},
        io::{Read, Write},
        os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt},
        path::Path,
    };
    use zeroize::Zeroizing;

    pub fn run(args: &[OsString]) -> Result<()> {
        match args {
            [command, output] if command == "keygen" => {
                let (private, public) = generate_key()?;
                let result = serde_json::to_string(&serde_json::json!({
                    "format": FORMAT,
                    "recipient": b64(&public),
                    "private_key_file": output.to_str().ok_or(Error("invalid_output_path"))?,
                }))
                .map_err(|_| Error("invalid_output_path"))?;
                write_new(Path::new(output), &private[..])?;
                print_output(&result)
            }
            [command, recipient, input, output] if command == "encrypt" => {
                let recipient = unb64(
                    recipient
                        .to_str()
                        .ok_or(Error("invalid_encryption_recipient"))?,
                    32,
                )?;
                let plaintext = read(Path::new(input), MAX_PLAINTEXT_BYTES, false)?;
                let envelope = encrypt(&plaintext, &recipient)?;
                write_new(Path::new(output), &envelope)
            }
            [command, private, input, output] if command == "decrypt" => {
                let private = read(Path::new(private), 32, true)?;
                let envelope = read(Path::new(input), MAX_ENVELOPE_BYTES, false)?;
                let plaintext = decrypt(&envelope, &private)?;
                write_new(Path::new(output), &plaintext)
            }
            _ => Err(Error("invalid_arguments; use --help")),
        }
    }

    fn read(path: &Path, limit: usize, private: bool) -> Result<Zeroizing<Vec<u8>>> {
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
            .open(path)
            .map_err(|_| Error("input_file_unavailable"))?;
        // Check the opened file, not a separate path lookup that can race it.
        let meta = file
            .metadata()
            .map_err(|_| Error("input_file_unavailable"))?;
        if !meta.is_file() {
            return Err(Error("regular_file_required"));
        }
        if private
            && (meta.mode() & 0o077 != 0 || meta.uid() != rustix::process::geteuid().as_raw())
        {
            return Err(Error("unsafe_encryption_key_permissions"));
        }
        let mut bytes = Zeroizing::new(Vec::with_capacity(limit + 1));
        file.take(limit as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| Error("input_file_unavailable"))?;
        if bytes.len() > limit {
            return Err(Error("input_file_too_large"));
        }
        Ok(bytes)
    }

    fn write_new(path: &Path, bytes: &[u8]) -> Result<()> {
        if let Some(parent) = path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(parent)
                .map_err(|_| Error("output_directory_unavailable"))?;
        }
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)
            .map_err(|_| Error("output_must_be_new_file"))?;
        if file.write_all(bytes).and_then(|_| file.sync_all()).is_err() {
            let _ = fs::remove_file(path);
            return Err(Error("output_write_failed"));
        }
        Ok(())
    }
}
