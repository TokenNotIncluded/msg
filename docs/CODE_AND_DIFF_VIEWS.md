# Reading code and revision differences

HTML documents render Markdown fenced code with language labels, source line numbers, local Pygments highlighting and a **Copy source** button. Unknown languages remain escaped plain text. No external script, style or highlighting service is fetched. Inline authored HTML remains disabled. Code and diff lines scroll inside their own keyboard-focusable region; long lines do not widen the document.

The copy button uses the original code content, separately encoded from the rendered line numbers and highlighting. If the browser cannot access the clipboard, the source appears in a selected text area for manual copying. The code module's script must be authorized by its exact `CODE_HASH` in the document's Content Security Policy.

Revision differences display old and new line numbers, explicit `−` / `+` markers, unchanged context and `@@` hunk headings. Read-current and history links remain available. A first revision explicitly states that no previous revision exists; a comparison with no text changes retains its normal empty state.

`discovery.diff_view` retains its existing `from`, `to`, `diff`, summary/source metadata and pagination fields. Its additive `diff_lines` array contains `kind`, `old_line`, `new_line` and `text` for each returned unified-diff record. Line numbers are computed before pagination, so a page beginning halfway through a hunk still has correct numbers. Headers, hunk headings and notes have null line numbers; deletions have only an old number, additions only a new number, and context has both. The original records preserve boundaries when either source has no final newline, even though the legacy joined `diff` text is ambiguous in that case. The special `no_previous_revision` response remains unchanged.

Continue using the returned `next` reference and retain the selected revisions. Every page is a fresh read subject to current authorization. A visible difference grants no editing permission; signed edits still require the current base revision and generation.

The shared document integration is deliberately small: use `markdown_renderer()` instead of a bare MarkdownIt renderer for Markdown, include `CODE_CSS`, add the exact `CODE_HASH` to `HOME_BROWSER_HEADERS`, and append `CODE_SCRIPT` to HTML documents. The same module exports `render_code(source, language)` for other authorized code surfaces.
