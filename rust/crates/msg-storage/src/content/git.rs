//! Direct Git plumbing only: no shell, Python, hooks, inherited Git variables,
//! global configuration, stdin pipe deadlock, or unbounded in-memory output.
use super::{
    files::{self, Temporary},
    GitContentStore,
};
use msg_core::{Error, Result};
use std::{
    fs::File,
    io::{Read, Write},
    process::{Child, Command, Stdio},
    thread,
    time::Instant,
};

struct Running(Child);
impl Drop for Running {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
impl GitContentStore {
    pub(super) fn command(&self) -> Command {
        let mut command = Command::new(&self.options.git_binary);
        command
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .env("LANG", "C.UTF-8")
            .env("HOME", &self.root)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_NO_REPLACE_OBJECTS", "1")
            .env("GIT_TERMINAL_PROMPT", "0")
            .env("GIT_AUTHOR_NAME", "msg")
            .env("GIT_COMMITTER_NAME", "msg")
            .env("GIT_AUTHOR_EMAIL", "msg@localhost")
            .env("GIT_COMMITTER_EMAIL", "msg@localhost")
            .arg("--git-dir")
            .arg(&self.repo)
            .args([
                "-c",
                "core.fsync=all",
                "-c",
                "core.logAllRefUpdates=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
            ])
            .current_dir(&self.root);
        command
    }
    pub(super) fn run(
        &self,
        command: &mut Command,
        input: Option<File>,
        max_output: u64,
    ) -> Result<Temporary> {
        let mut output = Temporary::new(&self.staging)?;
        command
            .stdin(input.map(Stdio::from).unwrap_or_else(Stdio::null))
            .stdout(output.file.try_clone().map_err(files::io)?)
            .stderr(Stdio::null());
        let mut child = Running(command.spawn().map_err(files::io)?);
        let started = Instant::now();
        loop {
            if output.file.metadata().map_err(files::io)?.len() > max_output {
                return Err(Error("content_output_too_large"));
            }
            if let Some(status) = child.0.try_wait().map_err(files::io)? {
                if !status.success() {
                    return Err(Error("content_store_error"));
                }
                // The child can finish between the first size check and try_wait.
                if output.file.metadata().map_err(files::io)?.len() > max_output {
                    return Err(Error("content_output_too_large"));
                }
                output.rewind()?;
                return Ok(output);
            }
            if started.elapsed() >= self.options.git_timeout {
                return Err(Error("content_store_timeout"));
            }
            thread::sleep(std::time::Duration::from_millis(5));
        }
    }
    pub(super) fn git(&self, args: &[&str], bytes: Option<&[u8]>) -> Result<String> {
        let mut input = Temporary::new(&self.staging)?;
        if let Some(bytes) = bytes {
            input.file.write_all(bytes).map_err(files::io)?;
        }
        input.rewind()?;
        let mut result = self.run(
            self.command().args(args),
            Some(input.file.try_clone().map_err(files::io)?),
            1_048_576,
        )?;
        let mut text = String::new();
        result.file.read_to_string(&mut text).map_err(files::io)?;
        Ok(text.trim_end_matches('\n').to_owned())
    }
    pub(super) fn ref_set(&self, reference: &str, oid: &str) -> Result<()> {
        files::oid(oid)?;
        self.git(&["update-ref", "--no-deref", reference, oid], None)
            .map(|_| ())
    }
}
