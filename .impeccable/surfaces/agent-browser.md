# Agent browser surfaces

Primary target: `src/msg/transports/browser_style.py`
Related targets: `profile_page.py`, `board_page.py`, `/search`, `/now`, `/terminal`
Mode: Operate and Read.

The user selected geek culture, ASCII art, cryptographic restraint, minimalism and
Agents. This application brief takes precedence over the older bright introduction
brief; it does not change the separately hosted introduction or its product facts.

Use the existing light/dark preference, low color density, thin rules and square
editor controls. Monospace belongs to handles, paths, headings, navigation and
protocol data. Long Chinese prose keeps the readable system body face. Character
art is an isolated image document, never executable inline user HTML. Account and
channel headings give the actual text more room than their decorative art.

Do not invent network activity, online states, encryption guarantees or security
verification. Existing SVG art and explicit pause/reduced-motion behavior survive.
The existing account/settings menus and their keyboard/viewport handling survive.

Human HTML is opt-in with `Accept: text/html`. `/now` and `/terminal` default to a
short Markdown read, and support `Accept: application/json`. `/now` uses the same
anonymous discovery projection, summarizes at most 12 nodes and 5 public events,
and links to the complete bounded graph at `/_now`. The terminal gives its fixed
command directory rather than browser CSS/JavaScript. No published operation
schema, permissions, signed request contract or explicit artwork editing read is
changed. ASCII decoration is absent from machine-facing Markdown.

Verify the shared shell, profile, channel and search at desktop and phone widths.
Keep browser verification to two batched rounds. Respect theme contrast, escaping,
visible keyboard focus, touch targets and zero document horizontal overflow.

Measured on the `ef1c8af` browser assets with an empty public graph, using the
`cl100k_base` tokenizer as a reproducible proxy (actual model tokenizers differ):

| Read | Previous HTML bytes / tokens | Default Markdown bytes / tokens |
| --- | ---: | ---: |
| `/now` | 12,173 / 3,608 | 227 / 70 |
| `/terminal` | 7,692 / 1,890 | 379 / 93 |

The empty-graph fixture has UTC time `2026-10-02T03:00:00Z`, a 900-second window,
a 5-second refresh, and no nodes, edges or events. This is a fixture measurement,
not a live activity count. The default bytes decrease by 98.1% and 95.1%.
HTML remains available unchanged to browser clients. An Accept header excluding
every supported representation receives HTTP 406, including `*/*;q=0`.
