# msg ingress

`nginx.conf` is a **complete, dedicated Nginx configuration**, not a `server` snippet for an existing `http {}`. This is intentional: errors raised before Host parsing must not inherit another virtual host's request/error logs. Do not overwrite a shared Nginx configuration or reload a production listener as part of a source-code verification run.

Before installation, replace `msg.example.org` consistently (including the Host comparison and certificate paths), match `service_url`, and check that the dedicated instance can own its IPv4/IPv6 listeners. Install and select this file explicitly, for example with `nginx -t -c /etc/msgd/nginx.conf`. Existing listener ownership, process arguments, default virtual hosts and any CDN/load balancer must be audited separately. Only an operator-approved deployment may change running services.

The template suppresses request/error logs at main and HTTP scope, covers default HTTP/HTTPS listeners, drops Referer upstream, and applies no-referrer/no-store to application and Nginx-generated responses. HTTP returns 400 instead of reflecting a possibly sensitive target into a redirect. Clients must select HTTPS **before** sending secrets. Unknown HTTPS Host returns 421. Keep the backend on loopback; do not expose a second unaudited ingress. The application's CSP is preserved so hosted-page isolation is not replaced by a proxy-wide policy.

## Reproducible verification

Install `nginx` and `openssl` alongside the project's test dependencies, then run:

```sh
python3.15 -m pytest tests/test_request_target_safety.py tests/test_nginx_secret_logs.py
python3.15 -m pytest tests/test_url_secrets.py tests/test_access_log_secret_redaction.py
```

The first pair tests bounded raw-target inspection plus the checked-in complete Nginx configuration with disposable TLS, actual Uvicorn/HTTP rejection paths, default Host, 400/413/414/502, upstream refusal/disconnect, GET packet encoding and no-redirect behavior. A deliberately insecure logging control proves the sentinel check can detect a leak. Missing Nginx/OpenSSL fails rather than skips; CI installs both. The second pair additionally uses the real PostgreSQL application and checks unchanged business facts and access-log behavior. Run the complete tests/conformance/build before merging.

Raw-target inspection performs at most eight percent-decoding passes **for rejection only**; it never rewrites the route. It recognizes explicit credential field names (including nested/encoded labels) and removed token/bootstrap path slots, not arbitrary natural-language secrets or opaque values. Ordinary quoted data and topics named `token` remain usable. Fragments are rejected by the client before transmission; servers cannot inspect a fragment that a browser has already discarded. Do not put secrets in unspecified fields to evade this boundary.

## Production evidence remains separate

For a shared Nginx instance, include `nginx-shared-http-logging.conf` once inside
`http {}` and set `error_log /dev/null crit;` at main scope. The inherited access
log retains time, configured server name, status and response byte count, with
no request target, client-supplied Host, header or upstream error text. Existing
log files are retained. This changes inherited logging for all shared sites;
inventory their explicit logging overrides before installation. Test with
`nginx -t` and verify the other sites after reload. Site-only logging directives
cannot protect errors occurring before Host selection. The dedicated config is
still the preferred option when independent listener ownership is available.

`test_nginx_secret_logs.py::test_shared_logging_keeps_metrics_without_request_input`
starts an actual isolated Nginx listener with this checked-in logging policy,
verifies normal and early-error status metrics, and rejects a non-live sentinel
in every captured log. It is not evidence about a CDN or external collector.

A green CI run does not establish the active production configuration or logs. Before closing an ingress acceptance issue, record the deployed commit/config digest, actual listeners and every upstream proxy, expanded configuration/process arguments, and the logging/APM/debug collectors at each hop. Do not publish credentials or raw log/config exports in an issue.

Use ordinary, credential-free `GET /healthz` on the real entry for a read-only reachability/header check; it does **not** prove log suppression. Run random non-live sentinel/error matrices only against an explicitly approved isolated/staging entry and inspect every relevant sink, including the default virtual host and edge services. Production logs/configuration require authorized read access and operator evidence. No deployment, log deletion, root operation or acceptance waiver is implied by these tests.
