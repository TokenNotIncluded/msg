# Public terminal

`/terminal` is a minimal, anonymous, read-only view of the MSG service. The public home page links to it. The public service runs this feature in native package `msgd 0.2.14-20261002.31`, built from `bef3fd1d92a5a5332a57ed0f314471f071c4026e` and deployed on 2026-10-02 (Asia/Taipei).

| Command | Output |
| --- | --- |
| `help` | Fixed command list |
| `status` | Service loaded / not ready |
| `time` | Service clock in UTC |
| `server` | Configured public service URL |
| `stats` | Public post/user counts and the counting date |
| `feed` | Up to five recent public post paths, titles and excerpts |
| `clear` | Clear the local transcript |

Arrow keys recall command history, Tab completes an unambiguous prefix, and Ctrl+L clears the transcript. History stays in memory for this tab, capped at 100 commands; output is capped at 160 transcript entries. The interface uses no external dependencies, fonts, persistent browser storage or cookies. Requests time out after 15 seconds and can be retried.

## Read boundary

`GET /_terminal?command=<name>` accepts exactly one `command` parameter from the table. `HEAD` is supported. Other methods, arguments, unknown commands, authorization headers and signed request headers are rejected. Neither route executes shell commands, accesses host files, nor reads process environments or host logs.

`stats` and `feed` call `discovery.read_query@4` with the fixed `home_summary` argument through the existing executor. The request has no subject or proof, regardless of the browser session, and the operation must declare a read effect. The existing anonymous projection filters access to resources; the terminal does not query storage directly. Output is rendered as text, including post titles and excerpts. Responses retain configured size limits, no-store caching, and a hash-pinned script CSP.

## Surface design

The terminal extends the current monochrome website: black canvas, light text, readable gray supporting text, and a system monospace face for commands/output. A thin header rule separates the title/home link from the transcript. One labeled prompt follows the output; a native Run button supports touch and keyboard. The layout narrows to the viewport without horizontal overflow. Busy state disables duplicate submissions, errors remain in the transcript, and an empty feed says there are no public posts.

Keep this direction local to `/terminal`; it does not replace the global design system. Commands and their real text output are the visual content. Do not add dashboard cards, simulated shell output or decorative terminal effects. The initial viewport exposes the complete fixed command list and the prompt together.

The built surface uses a black ground (`#000`), primary text (`#e7e7e7`), supporting text (`#aaa`) and error text (`#ffb4aa`). All text shares the command/output monospace stack at 14px with a 1.7 line height; the title stays at transcript scale. The centered column caps at 900px. Outer padding is 32px vertically and 24px horizontally, becoming 20px and 16px at widths up to 540px. Output preserves line breaks and wraps long content; the prompt input shrinks while its label and Run button remain visible.

Focus is visible on every control: an underline for the command input and an offset light outline for the home link and Run button. Selection reverses the text/background colors, and the caret and scrollbar retain the monochrome palette. Submissions append output without entrance animation; a visible “Reading…” state lasts only while the request is pending. Keep this immediate transcript behavior when extending the command vocabulary.

## Validation

`tests/test_public_terminal.py` exercises the HTTP routes against a real disposable MSG installation, including fixed commands, prohibited methods, argument injection, credentials and private-post exclusion. Browser screenshots cover desktop/mobile layout using a local transport preview; they do not prove deployment or production health.

## Production verification (2026-10-02)

The public `/terminal` returned HTTP 200 with bytes matching the bundled asset and its hash-pinned CSP. All seven commands succeeded; `status` returned `ready`. Unknown commands/injection and credentials returned 400, POST returned 405, and HEAD returned 200 without a body. Health, home, search, now, feed and the public profile returned 200. Both native systemd services were active with zero restarts; package verification reported zero altered files.

Native package SHA-256: `b076f1cc9e32205dfbb9cf83a6e4540025bfeea858eb7d4ca5db4793b566bf9e`. Before installation, the previous package, code overlays, configuration/data and PostgreSQL dump were saved under the protected server directory `/var/backups/msgd/public-terminal-20261002-215128`; the service v4 backup completed. Root private material was not changed.

Deployment rehearsal, recovery safety, issue recovery, Python quality and integration regression CI passed for the initial deployed source `c6be208`. The complete Rewrite contracts workflow identified stale homepage, browser-grant, wallet-routing, event-fixture and cache expectations, plus an oversized topic rule. Follow-up changes updated the fixtures and bounded the topic rule; the corrected cache checks passed (12), CLI/rule checks passed (8), and the remaining regression checks passed (184). A complete rerun is queued on the immutable `codex/public-terminal-deploy-20261002` branch; no full-suite pass is claimed. Local focused checks passed (52 compatibility checks, 31 JavaScript checks, 8 conformance cases and 4 terminal tests). Existing CA authority remains outdated for newer signed operations; doctor reports that expected limitation, and the anonymous terminal does not depend on expanding authority.

The final native package also includes the version 4 bounded topic rule. Public browser validation confirmed `status` → `ready` and a 390px mobile viewport without horizontal overflow.
