# Multiple domains, one authority

Refs #216, #221, #219, #80. Aliases select ingress; the canonical `service_url`
continues to identify the installation in signatures, certificates, receipts,
discovery, advertised links and local client storage.

```toml
[server]
service_url = "https://primary.example.org"
service_aliases = ["https://backup.example.org", "https://other.example.org:8443"]
```

The default alias list is empty. At most 32 unique HTTP(S) origins are accepted,
with the canonical scheme. URL credentials, paths, queries, fragments, invalid
ports, duplicates after normalization and the canonical origin itself are
rejected. Alias entries are normalized; the existing canonical authority is not
rewritten, so upgrading does not invalidate its signed history.

Both the ordinary HTTP application and the database read-only hosting application
check the exact Host and port before reaching business data. Missing, repeated,
malformed and unknown Hosts remain forbidden. Removing an alias removes its
ingress permission when configuration is reloaded. For platform operations,
browser Origin must match the selected ingress origin; listing another alias does
not grant cross-origin access. Published static pages retain their existing CSP
sandbox and opaque-origin behavior, without cookies or server write authority.

Clients explicitly keep the original `--server` while selecting `--endpoint`, or
use `ServiceURL` in their SSH-style host configuration. All four operation
transports, OAuth endpoints and signed hosting previews use the selected endpoint.
The discovery response must still report the original authority. Requests signed
for the alias itself fail `wrong_service`; another installation cannot accept the
original installation's packets or certificates. Endpoint selection never moves
keys or rewrites pending requests. See [client connections](CLIENT_CONNECTIONS.md).

## Reverse proxy and TLS

DNS, certificate coverage and reverse-proxy configuration are separate operator
steps. Preserve the incoming Host to the backend and allow only the configured
names and ports. Do not replace it with the canonical Host, reflect a request
target into a redirect, loosen the application Host check, or enable wildcard
origins. The existing dedicated Nginx template admits only its canonical hostname.
For two HTTPS names on port 443, an explicit replacement allowlist can be:

```nginx
# In the dedicated http block.
map $http_host $msg_host_allowed {
    default 0;
    primary.example.org 1;
    primary.example.org:443 1;
    backup.example.org 1;
    backup.example.org:443 1;
}
```

List both names in the TLS server's `server_name`, install certificates covering
both names, and replace the template's single `$host` condition with
`if ($msg_host_allowed = 0) { return 421; }`. Retain its missing-Host condition,
suppressed request/error logs, `proxy_set_header Host $http_host`, no-referrer,
no-store and upstream loopback restriction. Explicit nondefault ports require
their own listener, TLS and matching entries. Validate the complete configuration
before an approved reload. This document does not change a running proxy.

## Recovery and verification

Backup restoration retains the alias policy while binding restored storage and
listeners to the isolated target. The policy participates in the existing full
configuration digest for recovery proofs. It does not bypass recovery quarantine,
current authorization or promotion requirements.

`tests/test_service_aliases.py` covers parsing, rejection, canonical signed
publication/replay, wrong-service rejection, all four transports, identity
reuse, Origin/Host denial, no redirect, OAuth endpoint security and configuration.
`tests/test_hosting_alias_ingress.py` runs against the real read-only PostgreSQL
hosting role. `tests/test_backup_daemon.py` checks alias preservation in a real
backup/restore with quarantine. The connection tests reject unsafe IdentityFile
inputs before state or network access. Cloud workflow `Service alias contracts`
records its exact head/checkout/tree and JUnit report; complete acceptance still
uses the existing eight-shard, conformance, installed-package and specialist gates.

No target-host alias, DNS, TLS certificate, deployment, Root operation, production
snapshot restore, funds or cleanup is implied by source tests.
