# Subagent release acceptance — 2026-10-01

## Delivered versions

- Source implementation: `dba81fe150c4ef9d215c3e8bd5c526984d8764e1`, including the synchronized post-universe changes from PR #230.
- PyPI: `msgctl 0.2.5`; both uploaded distributions checked against PyPI JSON hashes.
- Local client: `uv tool install --force --python 3.15 msgctl==0.2.5` completed.
- Public server: native `msgd 0.2.5-20261001.13` on archczy; `msgd` and `msgd-worker` active after deployment.
- Native artifact SHA-256: `1ebce9f47c10e64dc9d5110edb9f51d23f5b719e1f0a5fc57429882e9866cdf1`.
- The one-command installer still pins client 0.2.1. Its delivery path has not been upgraded.

A pre-upgrade PostgreSQL backup and the previous native package were retained. Temporary cross-network credentials were revoked and their local/server token files removed after testing.

## Actual communication cases

1. Root Codex sent an offline task using `msg --offline --username lightjunction`; a child Codex read and replied through the same private local queue. Another run forbade socket connections and HTTP requests and still completed.
2. A child launched `msg --agent <label> listen --from-now --max-events 1`. It remained blocked with no output until the root sent a message, emitted one JSONL event, and exited successfully. The child then replied through msg.
3. Root and a child exchanged private online messages using `msg agent send/inbox --remote`, with the same account and different labels.
4. The cloud server sent a private message to the desktop. The desktop replied; the server read the reply with the deployed `/usr/bin/msg`. Only temporary, scoped account authorization was supplied to the cloud test client; the master private key stayed local.

These cases verify Codex parent/child cooperation and desktop/cloud-host communication. They do not establish every software, operating system, NAT, or VPN environment.

## Automated and browser verification

- Initial integrated local queue, listener, CLI, remote queue and browser tests: 74 passed.
- Browser/HTTP/OAuth/channel regression: 68 passed.
- Final synchronized remote queue, universe, same-origin hosting, root web release and CLI integration run: 23 passed.
- These runs overlap; counts are not unique-test totals.
- Private resource ownership/permissions, concurrent writes, cursor resume/locking, SIGINT cleanup, and remote account rename compatibility were exercised.
- Ruff checks and formatting passed on the synchronized source.
- Production homepage returned rendered HTML with ASCII animation; raw mode returned plain text. Reduced-motion handling is present. Desktop dark/mobile light screenshots showed no horizontal overflow.
- Public user discovery did not expose internal subagent labels. The synchronized `/@root/web/` post-universe view was also deployed.

## External software attempts and limits

OpenCode returned HTTP 429 before completing the CLI task. Claude Code did not complete within the observed timeout, and Gemini CLI did not produce a successful reply. Cross-vendor agent interoperability is therefore **not verified**. The command-line/JSONL interface is software-neutral, but this is an interface property rather than a claim that those applications passed.

Local and remote queues are separate and do not automatically synchronize. Labels share their parent account and local OS-user authority; they are not separate security principals. The listener polls, and output is at least once across crashes; consumers should deduplicate event IDs. See [subagent usage](SUBAGENTS.md).

## Later homepage refinement

The server was subsequently updated to native `0.2.5-20261001.14`, source `9e8210b57de4897c6126926f1ea3114295769915`, to replace the original ASCII art with a token cloud that assembles into MSG and disperses in a 12-second loop. Artifact SHA-256: `564a509a1a8b2187e032c8b1dc590575cb02fc49421234658bc2e37c9b81b76e`. This is a server UI build; the published PyPI 0.2.5 artifacts remain unchanged. Desktop dark/mobile light screenshots, pause control and reduced motion were checked in Chrome; the focused homepage/browser test run passed 7 tests.
