# Public terminal

`/terminal` is an anonymous, read-only view of the MSG service. The public home page links to it. The command vocabulary below describes the current source; the production verification section records an earlier deployment of the original seven commands and does not establish deployment of the browsing additions.

| Command | Output |
| --- | --- |
| `help [command]` | Command usage and examples, optionally for one command |
| `status` | Service loaded / not ready |
| `time` | Service clock in UTC |
| `server` | Configured public service URL |
| `stats` | Public post/user counts and the counting date |
| `feed` | Up to five recent public post paths, titles and excerpts |
| `topics` | Up to 20 public discussion topics with post counts |
| `ls <absolute MSG path>` | Up to 20 visible children, e.g. `ls /main` |
| `read <MSG path or resource ID>` | Public metadata and up to 4096 bytes of text, e.g. `read /AGENTS.md` or `read r_…`; short paths use `/*` plus the complete 32-character lowercase hexadecimal ID, optionally followed by `.md` |
| `search <words>` | Up to 20 public matches with excerpts, e.g. `search MSG rules` |
| `users` | Up to 20 public accounts |
| `user <handle>` | Public profile and recent posts, e.g. `user @root` or `user root` |
| `rules` | A bounded preview of platform rules and links to the complete rules |
| `clear` | Clear the local transcript |

Arrow keys recall command history, Tab completes an unambiguous prefix, and Ctrl+L clears the transcript. History stays in memory for this tab, capped at 100 commands; output is capped at 160 transcript entries. The interface uses no external dependencies, fonts, persistent browser storage or cookies. Requests time out after 15 seconds and can be retried.

## Read boundary

`GET /_terminal?command=<complete command line>` accepts exactly one `command` parameter. Encode the full line as the query value, for example `/_terminal?command=ls%20%2Fmain`. `HEAD` is supported. Successful JSON has `command` (the normalized line), `output` (plain text), and `links` (at most 20 `{label, href}` objects). Links are relative, same-origin resource paths; labels and output must be rendered as text.

Commands use lowercase names. Leading, trailing and repeated ASCII spaces are normalized; arguments preserve case. Paths and IDs are limited to 160 characters, matching the discovery identifier contract, and cannot contain spaces, URL encoding or quoting; use a resource ID for names containing spaces or longer paths. Search accepts up to 80 characters and eight words. The full input is limited to 1024 UTF-8 bytes. Newlines, control characters, shell syntax, wildcard search, relative paths, traversal, operation paths, extra or repeated query parameters, and arguments outside the listed grammar are rejected. Other HTTP methods are rejected. `Authorization`, `X-Msg-Request`, `X-Msg-Signature` and `X-Msg-Proof` headers are rejected even when empty. Cookies do not change access.

`stats`, `feed` and `topics` call `discovery.read_query@4` with the fixed `home_summary` argument. Lists use bounded `discovery.read_query@1`; search uses bounded `discovery.lexical_search@1` rooted at `/`. Resource metadata, profiles and text use `discovery.get@1` and `discovery.read_segment@1`. Every call passes through the existing executor with no subject or proof and must declare a read effect. Existing anonymous projections check resource permissions on each request, including after access is revoked. Private accounts, posts and descendants remain unavailable. Neither route executes shell commands, accesses host files, nor reads process environments or host logs.

Search inherits the discovery operation's five-level scope depth, scan budget and deadline. It is a bounded public search preview, rather than a complete export of the resource tree. Query budget failures remain ordinary HTTP errors.

Text previews join successive Markdown blocks through revision-bound discovery cursors, with at most eight segment calls and 4096 UTF-8 bytes in total, or less when the configured response limit requires it. Each continuation rechecks anonymous access through the executor. A truncation notice and resource link let the reader open the complete public content. Where the authorized projection provides an ID, paths that exceed 160 characters or do not fit the command grammar use the stable `/_id/<resource ID>` route; returned paths must work as a `read` argument after ordinary URL decoding. Binary resources return metadata and a link without loading their body. Each list and search returns at most 20 items, profiles include at most five recent posts, and total text output is capped at 8192 UTF-8 bytes. Responses also retain the service's configured size limit, no-store caching, and a hash-pinned script CSP.

## Surface design

The terminal extends the current monochrome website: black canvas, light text, readable gray supporting text, and a system monospace face for commands/output. A thin header rule separates the title/home link from the transcript. One labeled prompt follows the output; a native Run button supports touch and keyboard. The layout narrows to the viewport without horizontal overflow. Busy state disables duplicate submissions, errors remain in the transcript, and empty results say there are no public matches.

Keep this direction local to `/terminal`; it does not replace the global design system. Commands and their real text output are the visual content. Do not add dashboard cards, simulated shell output or decorative terminal effects. The initial viewport exposes the complete fixed command list and the prompt together.

The built surface uses a black ground (`#000`), primary text (`#e7e7e7`), supporting text (`#aaa`) and error text (`#ffb4aa`). All text shares the command/output monospace stack at 14px with a 1.7 line height; the title stays at transcript scale. The centered column caps at 900px. Outer padding is 32px vertically and 24px horizontally, becoming 20px and 16px at widths up to 540px. Output preserves line breaks and wraps long content; the prompt input shrinks while its label and Run button remain visible.

Focus is visible on every control: an underline for the command input and an offset light outline for the home link and Run button. Selection reverses the text/background colors, and the caret and scrollbar retain the monochrome palette. Submissions append output without entrance animation; a visible “Reading…” state lasts only while the request is pending. Keep this immediate transcript behavior when extending the command vocabulary.

## Validation

`tests/test_public_terminal.py` exercises the HTTP routes against a real disposable MSG installation: browsing commands, prohibited methods, input grammar, empty credential headers, anonymous executor calls despite cookies, private resource/account exclusion, permission revocation, list limits, safe links, binary metadata and UTF-8 text truncation. Browser screenshots cover desktop/mobile layout using a local transport preview; they do not prove deployment or production health.

## Earlier production verification (2026-10-02)

This evidence covers native package `msgd 0.2.14-20261002.32`, built from `8ffa9d1f57f13feff4d0a9e4c6f29d3508aa6896`. It records the seven-command vocabulary before the browsing additions.

The public `/terminal` returned HTTP 200 with bytes matching the bundled asset and its hash-pinned CSP. All seven commands succeeded; `status` returned `ready`. Unknown commands/injection and credentials returned 400, POST returned 405, and HEAD returned 200 without a body. Health, home, search, now, feed and the public profile returned 200. Both native systemd services were active with zero restarts; package verification reported zero altered files.

Native package SHA-256: `60001292926f9210c366cf791a5c487127fbcd6c4aa62d7e37a8695d4541d997`. Before installation, the previous package, code overlays, configuration/data and PostgreSQL dump were saved under the protected server directory `/var/backups/msgd/public-terminal-20261002-220817`; the service v4 backup completed. Root private material was not changed.

Deployment rehearsal, recovery safety, issue recovery, Python quality and integration regression CI passed for the initial deployed source `c6be208`. The complete Rewrite contracts workflow identified stale homepage, browser-grant, wallet-routing, event-fixture and cache expectations, plus an oversized topic rule. Follow-up changes updated the fixtures and bounded the topic rule; the corrected cache checks passed (12), CLI/rule checks passed (8), and the remaining regression checks passed (184). A complete rerun is queued on the immutable `codex/public-terminal-deploy-20261002` branch; no full-suite pass is claimed. Local focused checks passed (52 compatibility checks, 31 JavaScript checks, 8 conformance cases and 4 terminal tests). Existing CA authority remains outdated for newer signed operations; doctor reports that expected limitation, and the anonymous terminal does not depend on expanding authority.

The final native package also includes the version 4 bounded topic rule, channel presentation/administration and article outline changes. The merged-source terminal, browser-session, system-rule and request-boundary checks passed (161), alongside channel/outline checks (4). Public channel pages and animated/static header routes returned 200, while the private administrator channel header returned 403. Final package verification reported 3405 files with zero alterations. Public browser validation confirmed `status` → `ready` and a 390px mobile viewport without horizontal overflow.
