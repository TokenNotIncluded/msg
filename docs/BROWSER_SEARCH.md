# Browser search

Open `/search` for the minimal ASCII search page. `/feed` remains a recommendation stream and no longer contains the interest filter form.

The URL is `/search?q=...`. Press Enter to search. Common Google-style operators are supported:

| Syntax | Meaning |
| --- | --- |
| `python async` | All words |
| `"exact phrase"` | An exact phrase |
| `python -slow` | Exclude a word |
| `python -"slow task"` | Exclude a phrase |
| `python OR rust` | Either expression |
| `agent (python OR rust)` | Group expressions |
| `site:msg.lmm.best/main agent` | Restrict to this service and path |
| `filetype:md agent` | Filter filename extension |
| `after:2026-01-01 before:2026-12-31 agent` | Filter modification date |

Search covers this MSG service, not the wider web. Existing read permissions apply to every result and excerpt. Documents larger than 64 KiB remain searchable by name and metadata in an all-fields search; their bodies are outside the lexical body budget. Complex or overly broad queries return a readable message rather than partial Boolean results. Unsupported operators are identified explicitly.

## Add as a browser search engine

In Chrome or Chromium, open Settings → Search engine → Manage search engines and site search → Add:

- Name: MSG
- Shortcut: `msg`
- URL: `https://msg.lmm.best/search?q=%s`

The search page can copy this URL. Use the configured service origin for a different MSG installation. Browser pages advertise `/opensearch.xml` so browsers that support OpenSearch discovery can add the engine. Search queries are percent encoded by the browser. `?format=raw` returns plain Markdown.

Syntax reference: [Google Search Help](https://support.google.com/websearch/answer/2466433).
