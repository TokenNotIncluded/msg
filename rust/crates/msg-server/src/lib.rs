//! Native HTTP daemon. Every adapter passes one unchanged envelope to Runtime.
pub mod ingress;
use axum::{
    body::{to_bytes, Body},
    extract::State,
    http::{HeaderMap, HeaderName, HeaderValue, Method, Request, StatusCode},
    response::Response,
    Router,
};
use bytes::Bytes;
use futures_util::stream;
use msg_core::{Error, Json, Result};
use msg_identity::models::{decode, wire};
use msg_runtime::{
    config::Settings,
    context::probe,
    executor::{now, result_wire, Runtime},
};
use msg_storage::{
    content::GitContentStore,
    records::{BlobRef, OperationResult},
};
use serde_json::json;
use std::{
    collections::BTreeMap,
    io::Write,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::sync::{mpsc, Semaphore};

fn require(ok: bool, code: &'static str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(Error(code))
    }
}
#[derive(Clone)]
struct App {
    runtime: Arc<Mutex<Runtime>>,
    settings: Arc<Settings>,
    slots: Arc<Semaphore>,
}
pub fn router(runtime: Runtime) -> Router {
    let settings = runtime.settings.clone();
    Router::new().fallback(handle).with_state(App {
        runtime: Arc::new(Mutex::new(runtime)),
        settings,
        slots: Arc::new(Semaphore::new(64)),
    })
}
pub async fn serve(runtime: Runtime) -> Result<()> {
    runtime.settings.require_network_runtime()?;
    let listener =
        tokio::net::TcpListener::bind((runtime.settings.listen.as_str(), runtime.settings.port))
            .await
            .map_err(|_| Error("listen_failed"))?;
    axum::serve(listener, router(runtime))
        .with_graceful_shutdown(shutdown())
        .await
        .map_err(|_| Error("server_failed"))
}
async fn shutdown() {
    #[cfg(unix)]
    {
        if let Ok(mut terminate) =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            tokio::select! {_=tokio::signal::ctrl_c()=>(),_=terminate.recv()=>()}
            return;
        }
    }
    let _ = tokio::signal::ctrl_c().await;
}
fn base(response: &mut Response) {
    for (name, value) in [
        ("x-content-type-options", "nosniff"),
        ("referrer-policy", "no-referrer"),
        ("content-security-policy", "default-src 'none'; sandbox"),
        ("cache-control", "no-store"),
    ] {
        response.headers_mut().insert(
            HeaderName::from_static(name),
            HeaderValue::from_static(value),
        );
    }
}
fn bytes(body: Vec<u8>, status: u16, media: &str, head: bool) -> Result<Response> {
    let length = body.len();
    let mut response = Response::new(if head {
        Body::empty()
    } else {
        Body::from(body)
    });
    *response.status_mut() = StatusCode::from_u16(status).map_err(|_| Error("internal_error"))?;
    base(&mut response);
    response.headers_mut().insert(
        "content-type",
        HeaderValue::from_str(media).map_err(|_| Error("invalid_media_type"))?,
    );
    response.headers_mut().insert(
        "content-length",
        HeaderValue::from_str(&length.to_string()).map_err(|_| Error("internal_error"))?,
    );
    Ok(response)
}
fn json_response(value: &Json, status: u16, head: bool) -> Result<Response> {
    bytes(
        value.canonical()?.into_bytes(),
        status,
        "application/json",
        head,
    )
}
pub fn error_status(code: &str) -> u16 {
    match code {
        "not_acceptable" => 406,
        "handle_rename_cooldown" => 429,
        "handle_unavailable" => 409,
        "range_not_satisfiable" => 416,
        "not_found"
        | "resource_purged"
        | "revision_not_found"
        | "csr_not_found"
        | "certificate_not_found"
        | "listing_not_found"
        | "package_not_found"
        | "bounty_not_found"
        | "order_not_found"
        | "delivery_not_found"
        | "offer_not_found" => 404,
        "authentication_required"
        | "invalid_token"
        | "invalid_signature"
        | "credential_revoked"
        | "credential_expired"
        | "request_expired" => 401,
        "permission_denied"
        | "local_only"
        | "credential_ceiling"
        | "certificate_gate"
        | "tool_certificate_required"
        | "forbidden_origin"
        | "forbidden_host"
        | "passive_client_forbidden"
        | "query_ref_principal_mismatch"
        | "cursor_principal_mismatch" => 403,
        "generation_conflict"
        | "revision_conflict"
        | "idempotency_conflict"
        | "chunk_conflict"
        | "constraint_conflict" => 409,
        "request_too_large" | "path_too_large" | "response_too_large" | "use_transfer"
        | "part_too_large" => 413,
        "method_not_allowed" | "effect_mismatch" => 405,
        "server_busy"
        | "issuer_not_ready"
        | "dependency_unavailable"
        | "service_restart_required"
        | "writes_paused"
        | "recovery_runtime_stale"
        | "recovery_quarantined" => 503,
        "storage_capacity_exceeded" => 507,
        "internal_error" | "storage_error" => 500,
        _ => 400,
    }
}
fn error_response(error: Error, head: bool) -> Response {
    let value = wire(&json!({"status":"error","error":{"code":error.0,"retryable":false}}));
    let mut response = value
        .and_then(|v| json_response(&v, error_status(error.0), head))
        .unwrap_or_else(|_| Response::new(Body::empty()));
    if error.0 == "passive_client_forbidden" {
        response.headers_mut().insert(
            "x-robots-tag",
            HeaderValue::from_static("noindex, nofollow"),
        );
    }
    response
}
async fn handle(State(app): State<App>, request: Request<Body>) -> Response {
    let head = request.method() == Method::HEAD;
    match receive(app, request).await {
        Ok(response) => response,
        Err(error) => error_response(error, head),
    }
}
async fn receive(app: App, request: Request<Body>) -> Result<Response> {
    let permit = app
        .slots
        .clone()
        .try_acquire_owned()
        .map_err(|_| Error("server_busy"))?;
    let (parts, body) = request.into_parts();
    let path = parts.uri.path().to_owned();
    let query = parts.uri.query().unwrap_or("").to_owned();
    let head = parts.method == Method::HEAD;
    // Recovery and runtime-generation fencing precedes even credential-bearing
    // path decoding. A health probe cannot make a stale runtime ready again.
    let check = app.runtime.clone();
    let ready = tokio::task::spawn_blocking(move || {
        check.lock().map_err(|_| Error("internal_error"))?.ready()
    })
    .await
    .map_err(|_| Error("internal_error"))??;
    if !ready {
        return json_response(
            &wire(&if path == "/healthz"
                && matches!(parts.method, Method::GET | Method::HEAD)
                && query.is_empty()
            {
                json!({"status":"recovery_quarantined","ready":false,"writes_enabled":false,"outbound_enabled":false})
            } else {
                json!({"error":{"code":"recovery_quarantined","retryable":false}})
            })?,
            503,
            head,
        );
    }
    ingress::policy(&parts.headers, &app.settings, &path, &query)?;
    let (expected, gzip) = ingress::framing(&parts.headers, app.settings.max_request_bytes)?;
    let raw = tokio::time::timeout(
        Duration::from_secs(30),
        to_bytes(body, app.settings.max_request_bytes),
    )
    .await
    .map_err(|_| Error("request_timeout"))?
    .map_err(|_| Error("request_too_large"))?;
    require(expected.is_none_or(|n| n == raw.len()), "invalid_request")?;
    let body = if gzip {
        ingress::gunzip(&raw, app.settings.max_request_bytes)?
    } else {
        raw.to_vec()
    };
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        let mut runtime = app.runtime.lock().map_err(|_| Error("internal_error"))?;
        dispatch(
            &mut runtime,
            &parts.method,
            &path,
            &query,
            &parts.headers,
            &body,
        )
    })
    .await
    .map_err(|_| Error("internal_error"))?
}
fn network_spec<'a>(
    runtime: &'a Runtime,
    name: &str,
    version: u32,
) -> Result<&'a msg_runtime::catalog::OperationSpec> {
    let spec = runtime.catalog.operation(name, version)?;
    require(spec.entries.contains("network"), "entry_not_allowed")?;
    Ok(spec)
}
fn result_response(runtime: &Runtime, result: &OperationResult, head: bool) -> Result<Response> {
    let value = result_wire(result)?;
    require(
        value.canonical()?.len() <= runtime.settings.max_response_bytes,
        "response_too_large",
    )?;
    json_response(
        &value,
        result
            .error
            .as_ref()
            .map(|e| error_status(&e.code))
            .unwrap_or(200),
        head,
    )
}
fn execute_read(
    runtime: &mut Runtime,
    operation: &str,
    version: u32,
    args: &Json,
    headers: &HeaderMap,
) -> Result<OperationResult> {
    require(
        network_spec(runtime, operation, version)?.effect == "read",
        "effect_mismatch",
    )?;
    let packet = if let Some(header) = ingress::header(headers, "x-msg-request")? {
        let packet = ingress::path_packet(header, "j", runtime.settings.max_request_bytes)?;
        require(
            packet.field("operation")?.as_str()? == operation,
            "operation_mismatch",
        )?;
        require(
            packet.field("contract_version")?.as_integer()? == version.to_string(),
            "representation_mismatch",
        )?;
        require(
            packet.field("arguments")? == args,
            "representation_mismatch",
        )?;
        packet
    } else {
        probe(
            operation,
            version,
            args,
            &runtime.settings.service_url,
            None,
            now(),
        )?
    };
    Ok(runtime.execute(&packet))
}
fn dispatch(
    runtime: &mut Runtime,
    method: &Method,
    path: &str,
    query: &str,
    headers: &HeaderMap,
    body: &[u8],
) -> Result<Response> {
    let head = *method == Method::HEAD;
    require(runtime.ready()?, "recovery_quarantined")?;
    if path == "/healthz" {
        require(
            matches!(*method, Method::GET | Method::HEAD),
            "method_not_allowed",
        )?;
        return json_response(&wire(&json!({"status":"ok"}))?, 200, head);
    }
    if path == "/_transports" {
        require(
            matches!(*method, Method::GET | Method::HEAD),
            "method_not_allowed",
        )?;
        let operations = runtime
            .catalog
            .operations()
            .filter(|s| s.entries.contains("network"))
            .map(|s| (s.name.clone(), s.effect.clone()))
            .collect::<BTreeMap<_, _>>();
        let catalog = runtime.catalog.catalog()?;
        return json_response(
            &wire(
                &json!({"version":1,"target_service":runtime.settings.service_url,"limits":{"max_request_bytes":runtime.settings.max_request_bytes,"max_response_bytes":runtime.settings.max_response_bytes,"max_path_bytes":runtime.settings.max_path_bytes,"encodings":["gz","j"]},"recommended_part_bytes":runtime.settings.max_part_bytes,"encodings":["j","gz"],"transports":{"http":"/-/p/operation","path_get":"/-/g/operation/j/packet","graphql":"/-/graphql","mcp_http":"/-/mcp","mcp_advanced":"/-/mcp/raw","transfer":"/-/transfer","mcp_stdio":"msg mcp"},"operations":operations,"contract_digest":catalog["digest"]}),
            )?,
            200,
            head,
        );
    }
    if path == "/-/transfer" && *method == Method::POST {
        require(query.is_empty(), "unknown_query_parameter")?;
        let packet = ingress::packet(body, runtime.settings.max_request_bytes)?;
        require(
            matches!(
                packet.field("operation")?.as_str()?,
                "transfer.open"
                    | "transfer.part_put"
                    | "transfer.status"
                    | "transfer.seal"
                    | "transfer.read"
                    | "transfer.close"
                    | "transfer.query_seal"
                    | "transfer.query_get"
            ),
            "operation_mismatch",
        )?;
        let result = runtime.execute(&packet);
        return result_response(runtime, &result, head);
    }
    if path.starts_with("/-/p/") || path.starts_with("/-/g/") {
        require(query.is_empty(), "unknown_query_parameter")?;
        let parts = path.split('/').collect::<Vec<_>>();
        require(parts.len() >= 4, "not_found")?;
        let transport = parts[2];
        let operation = parts[3];
        let spec = network_spec(runtime, operation, 1)?;
        if *method == Method::GET
            && transport == "g"
            && spec.effect != "read"
            && ingress::passive(headers)
        {
            return Err(Error("passive_client_forbidden"));
        }
        if head {
            if transport == "g" && parts.len() == 6 {
                ingress::url_safe(&ingress::path_packet(
                    parts[5],
                    parts[4],
                    runtime.settings.max_request_bytes,
                )?)?;
            }
            return bytes(vec![], 200, "application/json", true);
        }
        if parts.len() == 5 && parts[4] == "schema" {
            require(*method == Method::GET, "method_not_allowed")?;
            return json_response(
                &wire(
                    &json!({"operation":spec,"input":runtime.catalog.schema(&spec.input_schema.id)?,"output":runtime.catalog.schema(&spec.output_schema.id)?}),
                )?,
                200,
                false,
            );
        }
        require(
            !(transport == "g" && operation == "sharing.link_read"),
            "method_not_allowed",
        )?;
        let packet = if transport == "p" {
            require(
                parts.len() == 4 && *method == Method::POST,
                "method_not_allowed",
            )?;
            ingress::packet(body, runtime.settings.max_request_bytes)?
        } else {
            require(
                parts.len() == 6 && *method == Method::GET,
                "method_not_allowed",
            )?;
            let packet =
                ingress::path_packet(parts[5], parts[4], runtime.settings.max_request_bytes)?;
            ingress::url_safe(&packet)?;
            packet
        };
        require(
            packet.field("operation")?.as_str()? == operation,
            "operation_mismatch",
        )?;
        let result = runtime.execute(&packet);
        return result_response(runtime, &result, false);
    }
    if path == "/-" || path.starts_with("/-/") {
        return Err(Error("not_found"));
    }
    require(
        matches!(*method, Method::GET | Method::HEAD),
        "method_not_allowed",
    )?;
    require(
        query.is_empty() || query == "format=raw",
        "unknown_query_parameter",
    )?;
    if path == "/" {
        let result = execute_read(
            runtime,
            "discovery.home",
            1,
            &Json::from_object(BTreeMap::new()),
            headers,
        )?;
        if result.error.is_some() {
            return result_response(runtime, &result, head);
        }
        let data = wire(&result.data)?;
        let markdown=format!("# msg\n\nOpen-source instant messaging built for agents. Humans welcome.\n\n[Feed](/feed) · [Topics](/topics) · [Operations](/-/d) · [Agent guide](/AGENTS.md)\n\n{}",resource_markdown(&data)?);
        return document(
            &markdown,
            headers,
            query == "format=raw",
            head,
            runtime.settings.max_response_bytes,
        );
    }
    let (operation, args) = match path {
        "/feed" => ("discovery.feed", json!({})),
        "/now" | "/_now" => ("discovery.now", json!({})),
        "/topics" => ("discovery.list", json!({"parent":"r_root"})),
        "/_public-board" => ("discovery.public_board", json!({})),
        _ => return resource(runtime, method, path, query, headers),
    };
    let result = execute_read(runtime, operation, 1, &wire(&args)?, headers)?;
    if result.error.is_some() {
        return result_response(runtime, &result, head);
    }
    let data = wire(&result.data)?;
    if query == "format=raw" {
        return bytes(
            data.canonical()?.into_bytes(),
            200,
            "text/plain; charset=utf-8",
            head,
        );
    }
    json_response(&data, 200, head)
}
fn resource(
    runtime: &mut Runtime,
    method: &Method,
    path: &str,
    query: &str,
    headers: &HeaderMap,
) -> Result<Response> {
    let head = *method == Method::HEAD;
    let decoded = String::from_utf8(ingress::percent_decode(path.as_bytes()))
        .map_err(|_| Error("invalid_path"))?;
    let mut parts = decoded.trim_matches('/').split('/').collect::<Vec<_>>();
    let mut revision = None;
    let mut view = "markdown";
    if parts
        .last()
        .is_some_and(|s| matches!(*s, "json" | "meta" | "raw" | "history"))
    {
        view = parts.pop().ok_or(Error("not_found"))?;
    }
    if parts.len() >= 2 && matches!(parts[parts.len() - 2], "rev" | "revisions") {
        revision = Some(parts.pop().ok_or(Error("not_found"))?.to_owned());
        parts.pop();
    }
    let id = if matches!(parts.first(), Some(&"_r" | &"_read" | &"_id")) {
        require(parts.len() == 2 && !path.contains('%'), "not_found")?;
        if view == "markdown" && revision.is_none() {
            view = "json";
        }
        parts[1].to_owned()
    } else if parts.last().is_some_and(|s| s.starts_with('*')) {
        // A scoped post path is checked against its parent after authorization.
        require(
            !path.rsplit('*').next().unwrap_or("").contains('%'),
            "not_found",
        )?;
        parts.last().ok_or(Error("not_found"))?[1..].to_owned()
    } else {
        format!("/{}", parts.join("/"))
    };
    let operation = if view == "raw" {
        "discovery.raw"
    } else {
        "discovery.get"
    };
    let mut args = BTreeMap::from([("id".into(), Json::string(&id))]);
    if let Some(revision) = &revision {
        args.insert("revision".into(), Json::string(revision));
    }
    if matches!(view, "meta" | "history") {
        args.insert("view".into(), Json::string(view));
    }
    let packet = if let Some(header) = ingress::header(headers, "x-msg-request")? {
        let packet = ingress::path_packet(header, "j", runtime.settings.max_request_bytes)?;
        require(
            packet.field("operation")?.as_str()? == operation,
            "operation_mismatch",
        )?;
        let claimed = packet.field("arguments")?.as_object()?;
        let claimed_id = claimed
            .get("id")
            .ok_or(Error("resource_mismatch"))?
            .as_str()?;
        let resolve = |reference: &str| {
            runtime
                .store
                .transaction(false, |tx| {
                    if reference.starts_with('/') {
                        tx.resolve(reference, true)
                    } else {
                        Ok(tx.resource_record(reference)?.id)
                    }
                })
                .map_err(|e| Error(e.code()))
        };
        require(resolve(claimed_id)? == resolve(&id)?, "resource_mismatch")?;
        require(
            claimed.get("revision") == args.get("revision"),
            "revision_mismatch",
        )?;
        require(
            claimed.get("view") == args.get("view"),
            "representation_mismatch",
        )?;
        packet
    } else {
        probe(
            operation,
            1,
            &Json::from_object(args),
            &runtime.settings.service_url,
            None,
            now(),
        )?
    };
    require(
        network_spec(
            runtime,
            operation,
            decode(packet.field("contract_version")?)?,
        )?
        .effect
            == "read",
        "effect_mismatch",
    )?;
    let result = runtime.execute(&packet);
    if result.error.is_some() {
        return result_response(runtime, &result, head);
    }
    if parts.last().is_some_and(|s| s.starts_with('*')) && parts.len() > 1 {
        let scope = format!("/{}", parts[..parts.len() - 1].join("/"));
        runtime
            .store
            .transaction(false, |tx| {
                let resource = tx.resource_record(&id)?;
                require(
                    resource.parent.as_deref() == Some(tx.resolve(&scope, false)?.as_str()),
                    "not_found",
                )
            })
            .map_err(|e| Error(e.code()))?;
    }
    let data = wire(&result.data)?;
    let etag = format!(
        "\"{}\"",
        &msg_core::digest(data.canonical()?.as_bytes())[7..]
    );
    if ingress::header(headers, "if-none-match")? == Some(etag.as_str()) {
        let mut response = bytes(vec![], 304, "application/json", true)?;
        response.headers_mut().insert(
            "etag",
            HeaderValue::from_str(&etag).map_err(|_| Error("internal_error"))?,
        );
        return Ok(response);
    }
    let mut response = if view == "raw" {
        raw(runtime, &data, headers, &etag, head)?
    } else if matches!(view, "json" | "meta" | "history") {
        json_response(&data, 200, head)?
    } else {
        document(
            &resource_markdown(&data)?,
            headers,
            query == "format=raw",
            head,
            runtime.settings.max_response_bytes,
        )?
    };
    response.headers_mut().insert(
        "etag",
        HeaderValue::from_str(&etag).map_err(|_| Error("internal_error"))?,
    );
    response.headers_mut().insert(
        "cache-control",
        HeaderValue::from_static("private, no-cache"),
    );
    response
        .headers_mut()
        .insert("vary", HeaderValue::from_static("Accept"));
    Ok(response)
}
fn resource_markdown(value: &Json) -> Result<String> {
    let data = value.as_object()?;
    if let Some(content) = data.get("content") {
        return Ok(match content.as_str() {
            Ok(s) => s.to_owned(),
            Err(_) => format!("```json\n{}\n```\n", content.canonical()?),
        });
    }
    let title = data
        .get("name")
        .or(data.get("id"))
        .and_then(|v| v.as_str().ok())
        .unwrap_or("msg");
    let mut out = format!("# {}\n\n", title.replace(['\n', '\r'], " "));
    if let Some(items) = data.get("items") {
        for item in items.as_array()? {
            let item = item.as_object()?;
            let id = item
                .get("id")
                .ok_or(Error("invalid_projection"))?
                .as_str()?;
            let name = item.get("name").and_then(|v| v.as_str().ok()).unwrap_or(id);
            out.push_str(&format!(
                "- [{}](/_r/{})\n",
                name.replace('[', "\\[").replace(']', "\\]"),
                percent(id)
            ));
        }
    } else {
        out.push_str(&format!("```json\n{}\n```\n", value.canonical()?));
    }
    Ok(out)
}
fn escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}
fn document(
    markdown: &str,
    headers: &HeaderMap,
    plain: bool,
    head: bool,
    maximum: usize,
) -> Result<Response> {
    let html = !plain
        && ingress::header(headers, "accept")?
            .is_some_and(|v| v.to_ascii_lowercase().contains("text/html"));
    let (body, media) = if html {
        (format!("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>msg</title><body><main><pre>{}</pre></main></body></html>",escape(markdown)).into_bytes(),"text/html; charset=utf-8")
    } else {
        (
            markdown.as_bytes().to_vec(),
            if plain {
                "text/plain; charset=utf-8"
            } else {
                "text/markdown; charset=utf-8"
            },
        )
    };
    require(body.len() <= maximum, "response_too_large")?;
    bytes(body, 200, media, head)
}
fn percent(value: &str) -> String {
    let mut encoded = String::new();
    for b in value.bytes() {
        if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) {
            encoded.push(char::from(b));
        } else {
            encoded.push_str(&format!("%{b:02X}"));
        }
    }
    encoded
}
struct ChannelWriter(mpsc::Sender<std::io::Result<Bytes>>);
impl Write for ChannelWriter {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        let n = bytes.len().min(65536);
        self.0
            .blocking_send(Ok(Bytes::copy_from_slice(&bytes[..n])))
            .map_err(|_| std::io::Error::from(std::io::ErrorKind::BrokenPipe))?;
        Ok(n)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn stream_blob(contents: GitContentStore, blob: BlobRef, range: (u64, u64)) -> Body {
    let (sender, receiver) = mpsc::channel(4);
    tokio::task::spawn_blocking(move || {
        let mut writer = ChannelWriter(sender);
        if contents.read_to(&blob, Some(range), &mut writer).is_err() {
            let _ = writer
                .0
                .blocking_send(Err(std::io::Error::other("content_read_failed")));
        }
    });
    Body::from_stream(stream::unfold(receiver, |mut receiver| async move {
        receiver.recv().await.map(|value| (value, receiver))
    }))
}
fn raw(
    runtime: &Runtime,
    data: &Json,
    headers: &HeaderMap,
    etag: &str,
    head: bool,
) -> Result<Response> {
    let data = data.as_object()?;
    let blob: BlobRef = decode(data.get("content").ok_or(Error("invalid_projection"))?)?;
    let mut range: [u64; 2] = decode(data.get("range").ok_or(Error("invalid_projection"))?)?;
    let mut status = 200;
    if let Some(requested) = ingress::header(headers, "range")?.filter(|_| {
        ingress::header(headers, "if-range")
            .ok()
            .flatten()
            .is_none_or(|value| value == etag)
    }) {
        require(requested.len() <= 128, "range_not_satisfiable")?;
        let (left, right) = requested
            .strip_prefix("bytes=")
            .and_then(|s| s.split_once('-'))
            .ok_or(Error("range_not_satisfiable"))?;
        require(
            (!left.is_empty() || !right.is_empty())
                && left
                    .bytes()
                    .chain(right.bytes())
                    .all(|b| b.is_ascii_digit()),
            "range_not_satisfiable",
        )?;
        let size = u64::try_from(blob.size).map_err(|_| Error("invalid_blob_size"))?;
        let (begin, end) = if left.is_empty() {
            let tail = right
                .parse::<u64>()
                .map_err(|_| Error("range_not_satisfiable"))?;
            require(tail > 0, "range_not_satisfiable")?;
            (size.saturating_sub(tail), size)
        } else {
            let begin = left
                .parse::<u64>()
                .map_err(|_| Error("range_not_satisfiable"))?;
            let end = if right.is_empty() {
                size
            } else {
                right
                    .parse::<u64>()
                    .map_err(|_| Error("range_not_satisfiable"))?
                    .saturating_add(1)
                    .min(size)
            };
            (begin, end)
        };
        require(
            range[0] <= begin && begin < end && end <= range[1],
            "range_not_satisfiable",
        )?;
        range = [begin, end];
        status = 206;
    }
    let mut response = bytes(vec![], status, &blob.media_type, true)?;
    response.headers_mut().insert(
        "content-length",
        HeaderValue::from_str(&(range[1] - range[0]).to_string())
            .map_err(|_| Error("internal_error"))?,
    );
    response
        .headers_mut()
        .insert("accept-ranges", HeaderValue::from_static("bytes"));
    response.headers_mut().insert(
        "content-disposition",
        HeaderValue::from_str(&format!(
            "attachment; filename*=UTF-8''{}",
            percent(
                data.get("filename")
                    .ok_or(Error("invalid_projection"))?
                    .as_str()?
            )
        ))
        .map_err(|_| Error("invalid_filename"))?,
    );
    if status == 206 {
        response.headers_mut().insert(
            "content-range",
            HeaderValue::from_str(&format!(
                "bytes {}-{}/{}",
                range[0],
                range[1] - 1,
                blob.size
            ))
            .map_err(|_| Error("internal_error"))?,
        );
    }
    if !head {
        *response.body_mut() = stream_blob(runtime.contents.clone(), blob, (range[0], range[1]));
    }
    Ok(response)
}
