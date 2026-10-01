"""One restrained, dependency-free visual language for browser-only views."""

PREFERENCES = (
    '<details class="preferences"><summary data-i18n="display">Display</summary>'
    '<div class="preference-fields"><label for="msg-language">'
    '<span data-i18n="language">Language</span><select id="msg-language">'
    '<option value="en">English</option><option value="zh">简体中文</option></select></label>'
    '<label for="msg-accent"><span data-i18n="accent">Accent</span><select id="msg-accent">'
    + ''.join(
        f'<option value="{key}" data-i18n="{key}">{label}</option>'
        for key, label in [
            ('blue', 'Blue'),
            ('green', 'Green'),
            ('violet', 'Violet'),
            ('orange', 'Orange'),
        ]
    )
    + '</select></label><label for="msg-theme"><span data-i18n="theme">Theme</span>'
    '<select id="msg-theme"><option value="system" data-i18n="system">System</option>'
    '<option value="light" data-i18n="light">Light</option>'
    '<option value="dark" data-i18n="dark">Dark</option></select></label></div></details>'
)

SKIP_LINK = '<a class="skip-link" href="#content" data-i18n="skip">Skip to content</a>'

THEME_CSS = """
:root {
  --accent-light: #205ba7; --accent-dark: #94bfff; --accent: var(--accent-light);
  --bg: #fff; --fg: #1d1d1f; --muted: #68686d; --panel: #f5f5f7; --line: #dedee3;
  --mono: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
  color-scheme: light;
}
:root[data-theme=dark] {
  --accent: var(--accent-dark); --bg: #111113; --fg: #f5f5f7;
  --muted: #a5a5ae; --panel: #1d1d20; --line: #39393f; color-scheme: dark;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme=light]) {
    --accent: var(--accent-dark); --bg: #111113; --fg: #f5f5f7;
    --muted: #a5a5ae; --panel: #1d1d20; --line: #39393f; color-scheme: dark;
  }
}
* { box-sizing: border-box; }
html { scroll-padding-block: 24px; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  overflow-wrap: anywhere; -webkit-text-size-adjust: 100%;
}
a { color: var(--accent); text-underline-offset: .22em; text-decoration-thickness: 1px; }
a:hover { text-decoration: underline; }
button, input, textarea, select { font: inherit; color: inherit; max-width: 100%; }
a, button, select, summary { -webkit-tap-highlight-color: transparent; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 5px; border-radius: 3px; }
.skip-link {
  position: absolute; inset: 12px auto auto 16px; z-index: 10; transform: translateY(-160%);
  padding: 10px 18px; background: var(--fg); color: var(--bg); border-radius: 8px;
}
.skip-link:focus { transform: translateY(0); }
.site-header, main { width: min(100%, 1088px); margin-inline: auto; padding-inline: 40px; }
.site-header { display: flex; align-items: center; justify-content: space-between; gap: 24px; padding-block: 24px; }
.brand { color: var(--fg); font: 600 24px/1 var(--mono); letter-spacing: -.06em; text-decoration: none; white-space: nowrap; }
.brand::after { content: "_"; color: var(--muted); }
nav { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 24px; min-width: 0; }
nav a { display: inline-flex; align-items: center; min-height: 44px; color: var(--fg); text-decoration: none; font-size: 14px; }
.current-account { overflow-wrap: anywhere; }
.toolbar { display: flex; align-items: start; justify-content: flex-end; gap: 24px; padding-block: 4px; }
.raw-link { font: 12px/44px var(--mono); color: var(--muted); white-space: nowrap; }
.preferences { color: var(--muted); font-size: 13px; max-width: 100%; }
.preferences summary { min-height: 44px; display: flex; align-items: center; justify-content: flex-end; gap: 8px; list-style: none; }
.preferences summary::-webkit-details-marker { display: none; }
.preferences summary::before { content: "[+]"; font: 11px var(--mono); }
.preferences[open] summary::before { content: "[-]"; }
.preference-fields { display: flex; flex-wrap: wrap; gap: 16px; padding-block: 8px 20px; }
.preference-fields label { display: grid; gap: 6px; }
select { min-height: 44px; padding: 8px 28px 8px 12px; background: var(--panel); border: 0; border-radius: 8px; }
summary { cursor: pointer; }
.hero { margin: 36px 0 40px; }
.eyebrow { color: var(--muted); font: 12px/1.5 var(--mono); letter-spacing: .02em; }
h1 { font-size: clamp(36px, 5.8vw, 64px); line-height: 1.09; letter-spacing: -.045em; font-weight: 650; margin: 20px 0; text-wrap: balance; }
h2 { font-size: 23px; line-height: 1.3; letter-spacing: -.025em; font-weight: 600; margin: 0 0 20px; }
h3 { line-height: 1.4; }
.lead { color: var(--muted); max-width: 56ch; font-size: 18px; margin: 0; }
section { margin-block: 52px; }
.account { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 22px 24px; border-radius: 14px; background: var(--panel); }
.account h2 { font-size: 14px; margin: 0 0 4px; }
.account p { margin: 0; color: var(--muted); font-size: 14px; max-width: 65ch; }
.account .primary { display: inline-flex; align-items: center; justify-content: center; min-height: 44px; padding: 8px 18px; border-radius: 8px; background: var(--fg); color: var(--bg); text-decoration: none; white-space: nowrap; }
.stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 24px; }
.stats strong { display: block; font-size: 34px; font-weight: 500; letter-spacing: -.04em; font-variant-numeric: tabular-nums; }
.stats span, .muted, time, footer { font-size: 13px; color: var(--muted); }
.activity-note { margin-top: 18px; font: 11px/1.6 var(--mono); }
.posts { list-style: none; margin: 0; padding: 0; }
.posts li { padding-block: 18px; }
.posts li:first-child { padding-top: 0; }
.post-title { display: flex; justify-content: space-between; align-items: baseline; gap: 24px; }
.post-title a { color: var(--fg); font-size: 18px; line-height: 1.4; font-weight: 550; text-decoration: none; }
.post-title a:hover { color: var(--accent); text-decoration: underline; }
.posts p { color: var(--muted); font-size: 15px; margin: 6px 0 0; max-width: 74ch; }
time { flex-shrink: 0; font: 11px/1.6 var(--mono); }
.empty-state, .notice { padding: 20px 0; color: var(--muted); font-size: 15px; }
.empty-state::before { content: "[ ] "; font-family: var(--mono); }
.notice::before { content: "[!] "; font-family: var(--mono); }
.scroll-hint { display: none; color: var(--muted); font: 11px/1.6 var(--mono); }
.table-scroll { max-width: 100%; overflow-x: auto; overscroll-behavior-inline: contain; }
table { width: 100%; border-collapse: collapse; text-align: left; font-size: 14px; }
.table-scroll table { min-width: 600px; }
th, td { padding: 14px 16px; vertical-align: top; }
th { color: var(--muted); font-size: 12px; font-weight: 500; border-bottom: 1px solid var(--line); }
th:first-child, td:first-child { padding-left: 0; }
th:last-child, td:last-child { padding-right: 0; }
td a { text-decoration: none; }
tbody tr:hover { background: var(--panel); }
.number { text-align: right; font-variant-numeric: tabular-nums; }
.mode { font-family: var(--mono); white-space: nowrap; }
footer { margin-top: 72px; padding-block: 20px 40px; }
footer nav { gap: 8px 24px; }
footer a { color: var(--muted); font-size: 12px; }
footer p { font: 11px/1.6 var(--mono); }
.page-document main { max-width: 840px; padding-bottom: 56px; }
.prose { padding-top: 28px; }
.prose h1 { font-size: clamp(32px, 4.5vw, 44px); margin-top: 16px; }
.prose h2 { margin-top: 40px; }
.prose > p, .prose > ul, .prose > ol { max-width: 72ch; }
.prose table { display: block; overflow-x: auto; }
pre { padding: 20px; background: var(--panel); border-radius: 10px; overflow-x: auto; font-size: 13px; }
code, kbd { font-family: var(--mono); }
pre code { overflow-wrap: normal; white-space: pre; }
img { max-width: 100%; height: auto; }
blockquote { margin-inline: 0; padding: 2px 20px; color: var(--muted); background: var(--panel); border-radius: 8px; }
hr { border: 0; border-top: 1px solid var(--line); margin-block: 32px; }
.post-meta { color: var(--muted); font-size: 13px; margin: 4px 0 32px; }
.post-meta p { margin: 4px 0; }
.post-meta details { margin-top: 12px; }
.post-meta summary { min-height: 32px; }
.post-meta dl { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 8px 16px; font: 12px/1.6 var(--mono); }
.post-meta dd { margin: 0; }
.page-auth main { max-width: 656px; padding-block: 16px 64px; }
.page-auth h1 { font-size: clamp(32px, 5vw, 42px); margin-top: 40px; }
.page-auth main > p, .page-auth #content > p { color: var(--muted); }
.page-auth input, .page-auth textarea { width: 100%; border: 1px solid var(--line); background: var(--panel); border-radius: 8px; padding: 12px 16px; margin-block: 8px 16px; }
.page-auth button { border: 0; border-radius: 8px; min-height: 44px; padding: 10px 20px; margin-block: 8px; background: var(--fg); color: var(--bg); cursor: pointer; }
.page-auth button:disabled { opacity: .55; cursor: wait; }
.page-auth .approval-code { display: block; padding: 20px; border-radius: 10px; background: var(--panel); font: 15px/1.7 var(--mono); user-select: all; }
.page-auth #status { min-height: 2em; font-size: 14px; }
@media (max-width: 640px) {
  .site-header, main { padding-inline: 22px; }
  .site-header { flex-wrap: wrap; gap: 8px 24px; padding-block: 20px 8px; }
  .site-header nav { column-gap: 20px; }
  .hero { margin-top: 28px; }
  .scroll-hint { display: block; }
  h1 { font-size: 40px; }
  .lead { font-size: 17px; }
  section { margin-block: 40px; }
  .account { align-items: flex-start; flex-direction: column; padding: 20px; }
  .stats { gap: 12px; }
  .stats strong { font-size: 30px; }
  .post-title { align-items: start; flex-direction: column; gap: 4px; }
  .preference-fields { flex-direction: column; }
  .post-meta dl { grid-template-columns: 1fr; gap: 4px; }
  .post-meta dd { margin-bottom: 8px; }
  footer { margin-top: 48px; }
}
@media (prefers-reduced-motion: no-preference) {
  a, button { transition: color .15s ease, background-color .15s ease; }
}
@media (forced-colors: active) {
  select, button, .account .primary { border: 1px solid ButtonText; }
  :focus-visible { outline-color: Highlight; }
}
"""