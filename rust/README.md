# Native MSG migration — phase 1

**This is a wire-compatibility foundation, not a replacement daemon.** No Python
entry point, live database, configuration, route or deployment is changed.

## Ownership boundary

The retained Python implementation owns `msg` CLI, administration/migration
scripts and development tools. The target Rust implementation will own
`msgd serve`, `msgd worker`, domain/identity, storage, HTTP/MCP and server-side
crypto/protocol. Shared wire semantics remain compatible with the retained CLI.
Do not rename endpoints or introduce a second signature protocol during this work.

The baseline inspected for this phase is
`7e16ca75ff6af4313365814c23912b5997b222c5`. The conformance test imports the actual
Python implementation from the same checkout, so later drift is detectable.

## Implemented here

| Crate | Native implementation | Python reference |
|---|---|---|
| `msg-core` | Bounded strict JSON, duplicate-key rejection, Python-style canonical numbers/Unicode ordering, SHA-256 and canonical unpadded base64url | `core/codec.py` |
| `msg-crypto` | Existing key/subject IDs, purpose-framed Ed25519 signing and verification | `security/crypto.py` |
| `msg-protocol` | Strict request shape, defaults, normalized UTC timestamps, business digest and signed request bytes | `core/packet.py`, `core/requests.py`, `core/models.py` |

`wire_check` is a JSONL **test example**, not a server or client replacement. Its
fixed seed is public test material. The library does not log or implement Debug
for private keys, requests or bearer proofs. Unsafe code is forbidden in these
crates; standard cryptographic libraries implement cryptographic primitives.

Business digests exclude only `proof`, `payload_digest`, `source` and
`expires_at`. Signatures cover everything except `proof`. Retry tests ensure
changing a request ID, business argument or precondition is not mistaken for a
same-content retry. Native signatures are also verified by the Python verifier.

## Validation

From the repository root, with Python 3.15 and Rust 1.85.0:

```sh
python -m pip install cryptography==46.0.3 jsonschema==4.25.1
cargo +1.85.0 test --locked --manifest-path rust/Cargo.toml --workspace --all-targets
cargo +1.85.0 clippy --locked --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo +1.85.0 build --locked --manifest-path rust/Cargo.toml --example wire_check
PYTHONPATH=src python rust/tests/test_python_parity.py \
  --binary rust/target/debug/examples/wire_check --report rust/artifacts/parity.json
```

The differential corpus includes 16,384 deterministic random IEEE-754 bit
patterns (non-finite values are excluded), decimal exponent neighbors, Unicode,
large integers, malformed JSON/base64/envelopes and bidirectional Ed25519 checks.
The report records the actual executed count, not the random input budget.
CI checks formatting without modifying sources, tests debug and optimized
release builds, and preserves the exact revision, toolchain versions, differential
reports and dependency lock as artifacts.

## Explicit cutover blockers

- `verify_signature_bytes` is not authentication or authorization. Credential
  revocation, subject binding, expiry, service binding, certificate chains,
  permission checks, idempotency storage and transactional execution are pending.
- Resource/result/receipt/certificate models, encrypted-key wrapping and recovery
  are not ported. Existing Python code remains the authoritative runtime.
- No native `serve`/`worker`, database writer, HTTP/MCP adapter or release artifact
  is provided yet. An empty loop or Python subprocess would not count as a port.
- The first parser is deliberately bounded to 1 MiB and 64 nested levels, and
  accepts RFC3339 UTC timestamps only. Python `datetime.fromisoformat` accepts
  additional aliases. These differences must be resolved or explicitly versioned
  before routing production requests to Rust; they are not silent fallback paths.
- Dalek strict verification refuses weak-key signature forgeries. Compatibility
  with historical keys/signatures must be characterized before any cutover;
  normal valid signatures must match Python byte-for-byte.
- No memory-reduction or throughput claim has been measured. A benchmark must
  compare equivalent functionality, data, concurrency and process trees.

## Next implementation slices

1. Freeze identity/result/receipt fixtures and add the credential, certificate and
   authorization model, including expired/revoked/cross-service negative cases.
2. Port storage transactions and the request-id/digest replay ledger against
   disposable databases created by the Python migrations. Verify rollback,
   restart/retry, concurrent writers and schema/version gates. No dual writes.
3. Integrate a bounded native executor and a read-only `msgd serve` slice, then
   HTTP/MCP adapters with exactly the existing operation registry and envelopes.
4. Port worker claiming/leases/retries and side effects. Enable write operations
   only after same-fixture differential tests and failure/recovery tests pass.
5. Perform isolated deployment rehearsals, workload/RSS measurements and an
   explicit rollback/cutover review. Keep the current Python service available.

## Coordination and evidence

Repository `AGENTS.md`, local entry skill and the deployed `/AGENTS.md` were read.
The current editing runtime has no selected MSG signing identity or worker
runtime; it cannot create an authorized private coordination thread. No new
production identity was registered and no public coordination content was posted.
This bounded work is therefore tracked in the migration branch/PR rather than
claiming MSG coordination occurred. Return to private MSG coordination when an
existing authorized identity is available.

The editing container has no Rust toolchain and cannot resolve GitHub for a git
clone. Consequently native compilation and the actual Python 3.15 differential
run must be evidenced by this PR's cloud CI, not claimed from local inspection.
