use msg_core::{Error, Result};
use msg_runtime::{
    config::{validate_instance, Settings},
    executor::Runtime,
};
use std::path::PathBuf;

fn arguments() -> Result<(PathBuf, String)> {
    let mut args = std::env::args().skip(1);
    let mut directory = None;
    let mut instance = None;
    let mut command = None;
    while let Some(value) = args.next() {
        match value.as_str() {
            "--version" => {
                println!("msgd {} (Rust)", env!("CARGO_PKG_VERSION"));
                std::process::exit(0)
            }
            "--help" | "-h" => {
                println!("msgd [--config-dir PATH | --instance NAME] serve\n\nNative MSG daemon. The existing Python msg client remains supported.");
                std::process::exit(0)
            }
            "--config-dir" if directory.is_none() => {
                directory = Some(PathBuf::from(
                    args.next().ok_or(Error("invalid_arguments"))?,
                ))
            }
            "--instance" if instance.is_none() => {
                instance = Some(args.next().ok_or(Error("invalid_arguments"))?)
            }
            "serve" if command.is_none() => command = Some(value),
            _ => return Err(Error("invalid_arguments")),
        }
    }
    if directory.is_some() && instance.is_some() {
        return Err(Error("invalid_arguments"));
    }
    let directory = if let Some(name) = instance {
        validate_instance(&name)?;
        PathBuf::from("/etc/msgd").join(name)
    } else {
        directory.unwrap_or_else(|| PathBuf::from("/etc/msgd"))
    };
    Ok((directory, command.ok_or(Error("command_required"))?))
}
async fn run() -> Result<()> {
    let (directory, _command) = arguments()?;
    let settings = Settings::load(&directory)?;
    settings.require_network_runtime()?;
    let runtime = tokio::task::spawn_blocking(move || Runtime::open(settings))
        .await
        .map_err(|_| Error("startup_failed"))??;
    msg_server::serve(runtime).await
}
#[tokio::main]
async fn main() {
    std::panic::set_hook(Box::new(|_| eprintln!("msgd_failed")));
    if let Err(error) = run().await {
        eprintln!(
            "{{\"status\":\"error\",\"error\":{{\"code\":\"{}\"}}}}",
            error.0
        );
        std::process::exit(2);
    }
}
