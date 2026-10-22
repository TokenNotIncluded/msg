//! Bounded local subprocesses, private staging, and a shared shutdown deadline.
use msg_core::{Error, Result};
use rand_core::{OsRng, RngCore};
use std::fmt::Write as _;
use std::{
    cell::Cell,
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    process::{Child, Command, ExitStatus, Stdio},
    sync::atomic::{AtomicBool, Ordering},
    thread,
    time::{Duration, Instant},
};

pub(crate) fn id(prefix: &str) -> Result<String> {
    let mut bytes = [0; 16];
    OsRng
        .try_fill_bytes(&mut bytes)
        .map_err(|_| Error("random_unavailable"))?;
    let mut value = String::with_capacity(prefix.len() + 1 + bytes.len() * 2);
    write!(value, "{prefix}_").expect("writing to a string cannot fail");
    for byte in bytes {
        write!(value, "{byte:02x}").expect("writing to a string cannot fail");
    }
    Ok(value)
}
pub(crate) struct Directory {
    path: PathBuf,
}
impl Directory {
    pub(crate) fn new(root: &Path, prefix: &str) -> Result<Self> {
        if !root.is_absolute() {
            return Err(Error("invalid_staging_directory"));
        }
        let metadata =
            fs::symlink_metadata(root).map_err(|_| Error("invalid_staging_directory"))?;
        if !metadata.is_dir() || metadata.file_type().is_symlink() {
            return Err(Error("invalid_staging_directory"));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            if metadata.mode() & 0o077 != 0 || metadata.uid() != rustix::process::geteuid().as_raw()
            {
                return Err(Error("invalid_staging_directory"));
            }
        }
        let path = root.join(id(prefix)?);
        let mut builder = fs::DirBuilder::new();
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            builder.mode(0o700);
        }
        builder
            .create(&path)
            .map_err(|_| Error("staging_io_error"))?;
        Ok(Self { path })
    }
    pub(crate) fn path(&self) -> &Path {
        &self.path
    }
    pub(crate) fn create(&self, name: &str) -> Result<File> {
        create(&self.path.join(name))
    }
}
impl Drop for Directory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}
pub(crate) fn create(path: &Path) -> Result<File> {
    let mut options = OpenOptions::new();
    options.read(true).write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    options.open(path).map_err(|_| Error("staging_io_error"))
}
pub(crate) fn open(path: &Path) -> Result<File> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let file = options.open(path).map_err(|_| Error("staging_io_error"))?;
    if !file
        .metadata()
        .map_err(|_| Error("staging_io_error"))?
        .is_file()
    {
        return Err(Error("invalid_tool_output_path"));
    }
    Ok(file)
}
pub(crate) struct Stop<'a> {
    flag: &'a AtomicBool,
    since: Cell<Option<Instant>>,
}
impl<'a> Stop<'a> {
    pub(crate) fn new(flag: &'a AtomicBool) -> Self {
        Self {
            flag,
            since: Cell::new(None),
        }
    }
    pub(crate) fn check(&self) -> Result<()> {
        if self.flag.load(Ordering::Acquire) {
            let since = self.since.get().unwrap_or_else(|| {
                let now = Instant::now();
                self.since.set(Some(now));
                now
            });
            if since.elapsed() >= Duration::from_secs(60) {
                return Err(Error("worker_stopping"));
            }
        }
        Ok(())
    }
}
struct Running(Child);
impl Drop for Running {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
pub(crate) fn run(
    command: &mut Command,
    input: Option<File>,
    mut output: File,
    timeout: Duration,
    max_output: u64,
    stop: &Stop<'_>,
) -> Result<(ExitStatus, Vec<u8>)> {
    stop.check()?;
    command
        .stdin(input.map(Stdio::from).unwrap_or_else(Stdio::null))
        .stdout(output.try_clone().map_err(|_| Error("staging_io_error"))?)
        .stderr(Stdio::null());
    let mut child = Running(
        command
            .spawn()
            .map_err(|_| Error("subprocess_unavailable"))?,
    );
    let started = Instant::now();
    loop {
        stop.check()?;
        if output
            .metadata()
            .map_err(|_| Error("staging_io_error"))?
            .len()
            > max_output
        {
            return Err(Error("subprocess_output_too_large"));
        }
        if let Some(status) = child.0.try_wait().map_err(|_| Error("subprocess_failed"))? {
            output
                .seek(SeekFrom::Start(0))
                .map_err(|_| Error("staging_io_error"))?;
            let mut bytes = Vec::new();
            output
                .take(max_output + 1)
                .read_to_end(&mut bytes)
                .map_err(|_| Error("staging_io_error"))?;
            if bytes.len() as u64 > max_output {
                return Err(Error("subprocess_output_too_large"));
            }
            return Ok((status, bytes));
        }
        if started.elapsed() >= timeout {
            return Err(Error("external_uncertain"));
        }
        thread::sleep(Duration::from_millis(5));
    }
}
pub(crate) fn packet_file(directory: &Directory, name: &str, bytes: &[u8]) -> Result<File> {
    let mut file = directory.create(name)?;
    file.write_all(bytes)
        .map_err(|_| Error("staging_io_error"))?;
    file.seek(SeekFrom::Start(0))
        .map_err(|_| Error("staging_io_error"))?;
    Ok(file)
}
