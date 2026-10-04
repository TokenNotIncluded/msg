"""One restrained, dependency-free visual language for browser-only views."""

from html import escape
from importlib.resources import files

from msg.transports.browser_i18n import LANGUAGES
from msg.transports.browser_palette import ACCENTS

_MARK = files('msg.data').joinpath('logo.svg').read_text()
_MARK = _MARK.replace(
    'width="96" height="96" role="img" aria-label="msg"',
    'width="28" height="28" aria-hidden="true"',
).replace('stroke="#111111"', 'stroke="currentColor"')
BRAND_LINK = '<a class="brand" href="/" aria-label="msg · Home">' + _MARK + '<span>msg</span></a>'

_THEME_ICONS = {
    'system': '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
    'light': '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5"/>',
    'dark': '<path d="M20.5 14A9 9 0 0 1 10 3.5 9 9 0 1 0 20.5 14Z"/>',
}

PREFERENCES = (
    '<details class="preferences"><summary title="Display settings / 显示设置"><svg class="settings-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/></svg><span data-i18n="display">Display settings</span><svg class="settings-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></summary>'
    '<div class="preference-fields"><div class="preference-row"><label for="msg-language" data-i18n="language">Language</label>'
    '<select id="msg-language">'
    + ''.join(
        f'<option value="{code}" lang="{tag}">{escape(label)}</option>'
        for code, label, tag, _ in LANGUAGES
    )
    + '</select></div>'
    '<div class="preference-row"><span id="accent-label" data-i18n="accent">Accent</span>'
    '<div id="msg-accent" class="accent-options" role="radiogroup" aria-labelledby="accent-label">'
    + ''.join(
        f'<label class="accent-choice" title="{label}"><input type="radio" name="msg-accent" value="{key}"'
        + (' checked' if key == 'blue' else '')
        + f'><span class="swatch" style="--swatch:{color}"></span><span class="sr-only" data-i18n="{key}">{label}</span></label>'
        for key, english, chinese, light, color in ACCENTS
        for label in [f'{english} / {chinese}']
    )
    + '</div></div><div class="preference-row"><span id="theme-label" data-i18n="theme">Theme</span>'
    '<div id="msg-theme" class="theme-options" role="radiogroup" aria-labelledby="theme-label">'
    + ''.join(
        f'<label class="theme-choice" title="{label}"><input type="radio" name="msg-theme" value="{key}"'
        + (' checked' if key == 'system' else '')
        + '><span class="theme-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">'
        + _THEME_ICONS[key]
        + f'</svg></span><span class="sr-only" data-i18n="{key}">{label}</span></label>'
        for key, label in [
            ('system', 'System / 跟随系统'),
            ('light', 'Light / 亮色'),
            ('dark', 'Dark / 暗色'),
        ]
    )
    + '</div></div><p class="settings-note" data-i18n="saved_settings">Saved on this device.</p></div></details>'
)

SKIP_LINK = '<a class="skip-link" href="#content" data-i18n="skip">Skip to content</a>'

THEME_CSS = """
.page-topic-index .prose > h2 > a,
.page-topic-index .prose > p > a[href*="post_cursor="] {
  display: inline-flex; align-items: center; min-height: 44px; min-width: 44px; max-width: 100%;
}
.thread-discussion { margin-block: 40px; min-width: 0; }
.prose .thread-heading { display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 8px 24px; padding-bottom: 20px; border-bottom: 1px solid var(--line); }
.prose .thread-heading h1, .prose .thread-heading h2 { margin: 0; font: 600 1.6rem/1.3 var(--mono); letter-spacing: -.02em; }
.prose .thread-range { margin: 8px 0 0; font-size: .8125rem; color: var(--muted); font-variant-numeric: tabular-nums; }
.thread-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 16px; }
.thread-control { display: inline-flex; align-items: center; min-height: 44px; padding-inline: 4px; font-size: .875rem; }
.prose .thread-notice, .prose .thread-empty { color: var(--muted); margin-block: 20px; }
.prose .thread-tree { list-style: none; margin: 0; padding: 0; }
.prose .thread-node { margin: 0; padding: 0; min-width: 0; scroll-margin-block: 24px; }
.thread-node > article { padding-block: 24px 20px; }
.thread-node + .thread-node > article { border-top: 1px solid var(--line); }
.thread-node:target > article { background: var(--panel); outline: 1px solid var(--line); outline-offset: 8px; }
.prose .thread-title { margin: 0 0 10px; font: 600 1rem/1.5 var(--mono); }
.thread-title a { display: inline-flex; align-items: center; min-height: 44px; color: var(--fg); text-decoration: none; }
.thread-title a:hover { text-decoration: underline; }
.prose .thread-root > article .thread-title { font-size: 1.15rem; }
.prose .thread-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 0 12px; margin: 0 0 4px; font-size: .8125rem; line-height: 1.5; color: var(--muted); }
.thread-meta a { display: inline-flex; align-items: center; min-height: 44px; }
.thread-meta .thread-author { color: var(--fg); font: 500 .875rem/1.5 var(--mono); text-decoration: none; }
.thread-meta .thread-author:hover { text-decoration: underline; }
.thread-meta .thread-permalink, .thread-meta .thread-parent { color: var(--muted); text-decoration: none; }
.thread-meta .thread-permalink:hover, .thread-meta .thread-parent:hover { color: var(--fg); text-decoration: underline; }
.thread-node-kind { border-inline-start: 1px solid var(--line); padding-inline-start: 12px; }
.prose .thread-body { max-width: 72ch; margin: 0; line-height: 1.8; }
.thread-body > :first-child, .thread-content > :first-child { margin-top: 0; }
.thread-body > :last-child, .thread-content > :last-child { margin-bottom: 0; }
.prose .thread-body h1, .prose .thread-body h2, .prose .thread-body h3 { font-size: 1rem; }
.thread-long-body > summary { min-height: 44px; cursor: pointer; list-style: none; }
.thread-long-body > summary::-webkit-details-marker { display: none; }
.thread-excerpt { display: block; color: var(--fg); margin-bottom: 8px; }
.thread-expand-label, .thread-collapse-label { display: inline-flex; align-items: center; min-height: 44px; color: var(--accent); font-size: .875rem; text-decoration: underline; text-underline-offset: .22em; }
.thread-collapse-label, .thread-long-body[open] > summary .thread-expand-label, .thread-long-body[open] > summary .thread-excerpt { display: none; }
.thread-long-body[open] > summary .thread-collapse-label { display: inline-flex; }
.prose .thread-truncation { color: var(--muted); font-size: .8125rem; }
.prose .thread-stats { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 16px; margin: 16px 0 0; min-height: 44px; font-size: .8125rem; color: var(--muted); font-variant-numeric: tabular-nums; }
.thread-branch > summary { display: flex; align-items: center; min-height: 44px; padding-block: 8px; cursor: pointer; color: var(--muted); font-size: .8125rem; border-top: 1px solid var(--line); font-variant-numeric: tabular-nums; }
.thread-branch > summary:hover { color: var(--fg); }
.thread-branch > summary::-webkit-details-marker { display: none; }
.thread-branch-count { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 16px; }
.thread-branch-chevron { flex: 0 0 auto; }
.thread-branch[open] > summary .thread-branch-chevron { transform: rotate(90deg); }
.prose .thread-branch-pending { margin: 12px 0 20px; font-size: .875rem; color: var(--muted); }
.thread-branch > [data-thread-branch-content] { margin-inline-start: 12px; padding-inline-start: 24px; border-inline-start: 1px solid var(--line); }
.thread-node[data-depth="8"] .thread-branch > [data-thread-branch-content] { margin-inline-start: 0; padding-inline-start: 0; border: 0; }
.prose .thread-more { margin-block: 24px 0; border-top: 1px solid var(--line); padding-top: 12px; }
@media (max-width: 640px) {
  .thread-discussion { margin-block: 32px; }
  .thread-branch > [data-thread-branch-content] { margin-inline-start: 4px; padding-inline-start: 12px; }
  .thread-node[data-depth="3"] .thread-branch > [data-thread-branch-content] { margin-inline-start: 0; padding-inline-start: 0; border: 0; }
  .thread-meta { column-gap: 10px; }
}
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
html { scrollbar-color: var(--muted) var(--bg); }
::selection { background: var(--fg); color: var(--bg); }
input, textarea { caret-color: var(--accent); }
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
.brand { display: inline-flex; align-items: center; gap: 10px; }
.brand svg { flex: none; width: 28px; height: 28px; transform-origin: center; }
nav { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 24px; min-width: 0; }
nav a { display: inline-flex; align-items: center; min-height: 44px; color: var(--fg); text-decoration: none; font: 13px/1.5 var(--mono); }
.current-account { overflow-wrap: anywhere; }
.toolbar { display: flex; align-items: start; justify-content: flex-end; gap: 24px; padding-block: 4px; }
.raw-link { font: 12px/44px var(--mono); color: var(--muted); white-space: nowrap; }
.preferences { color: var(--fg); font-size: 13px; max-width: 100%; }
.preferences[open] { flex: 1; max-width: 340px; }
.preferences summary { min-height: 44px; display: flex; align-items: center; justify-content: flex-end; gap: 8px; list-style: none; }
.preferences summary::-webkit-details-marker { display: none; }
.settings-icon, .settings-chevron { flex: none; }
.preferences[open] .settings-chevron { transform: rotate(180deg); }
.preference-fields { padding: 12px 0 0; }
.preference-row { display: flex; align-items: center; justify-content: space-between; gap: 20px; min-height: 52px; }
.preference-row > label, .preference-row > span { color: var(--muted); font-size: 12px; }
.preference-row select { width: 160px; min-height: 40px; border: 0; background: var(--panel); padding-inline: 12px; font-size: 13px; }
.accent-options { display: grid; grid-template-columns: repeat(4, 44px); }
.theme-options { display: flex; align-items: center; }
.accent-choice, .theme-choice { position: relative; display: grid; place-items: center; width: 44px; height: 44px; cursor: pointer; }
.accent-choice input, .theme-choice input { position: absolute; inset: 0; width: 100%; height: 100%; margin: 0; opacity: 0; cursor: pointer; }
.swatch { width: 20px; height: 20px; border-radius: 50%; background: var(--swatch); pointer-events: none; }
.accent-choice input:checked + .swatch { outline: 1px solid var(--fg); outline-offset: 4px; }
.theme-options { padding: 2px; border-radius: 6px; background: var(--panel); }
.theme-icon { display: grid; place-items: center; width: 40px; height: 36px; border-radius: 4px; color: var(--muted); pointer-events: none; }
.theme-choice input:checked + .theme-icon { background: var(--bg); color: var(--fg); box-shadow: 0 1px 3px #00000014; }
.accent-choice input:focus-visible + .swatch, .theme-choice input:focus-visible + .theme-icon { outline: 2px solid var(--accent); outline-offset: 4px; }
select { min-height: 44px; padding: 8px 28px 8px 12px; color: var(--fg); background: var(--bg); border: 1px solid var(--line); border-radius: 6px; font: inherit; cursor: pointer; }
select:hover { border-color: var(--muted); }
.settings-note { color: var(--muted); font-size: 11px; margin: 10px 0 4px; text-align: right; }
summary { cursor: pointer; }
.hero { margin: 36px 0 40px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
/* The static default is a readable MSG; only the visible hero runs. */
.token-art { position: relative; width: min(100%, 720px); margin: 12px 0 24px; }
.token-cloud { display: block; width: 100%; height: auto; overflow: hidden; }
.token { fill: var(--fg); font: 16px var(--mono); text-anchor: middle; transform-box: fill-box; transform-origin: center; }
.token-accent { fill: var(--accent); }
.token-pause { position: absolute; right: 0; bottom: 0; width: 44px; height: 44px; padding: 15px; border: 0; border-radius: 0; background: none; color: var(--muted); }
.token-pause:hover { color: var(--fg); background: none; }
.token-pause .play-mark, .token-pause[aria-pressed=true] .pause-mark { display: none; }
.token-pause[aria-pressed=true] .play-mark { display: initial; }
.eyebrow { color: var(--muted); font: 12px/1.5 var(--mono); letter-spacing: .02em; }
h1 { font-family: var(--mono); font-size: clamp(30px, 4.5vw, 46px); line-height: 1.2; letter-spacing: -.025em; font-weight: 600; margin: 20px 0; text-wrap: balance; }
h2 { font-family: var(--mono); font-size: 21px; line-height: 1.4; letter-spacing: -.015em; font-weight: 600; margin: 0 0 20px; }
h3 { line-height: 1.4; }
.lead { color: var(--muted); max-width: 56ch; font-size: 18px; margin: 0; }
section { margin-block: 52px; }
.account { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 20px 0; border-block: 1px solid var(--line); background: transparent; }
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
table { width: 100%; border-collapse: collapse; text-align: start; font-size: 14px; }
.table-scroll table { min-width: 600px; }
th, td { padding: 14px 16px; vertical-align: top; }
th { color: var(--muted); font-size: 12px; font-weight: 500; border-bottom: 1px solid var(--line); }
th:first-child, td:first-child { padding-inline-start: 0; }
th:last-child, td:last-child { padding-inline-end: 0; }
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
pre { padding: 20px; background: var(--panel); border-radius: 2px; overflow-x: auto; font-size: 13px; }
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
.page-auth input, .page-auth textarea { width: 100%; border: 1px solid var(--line); background: var(--panel); border-radius: 2px; padding: 12px 16px; margin-block: 8px 16px; }
.page-auth button { border: 0; border-radius: 2px; min-height: 44px; padding: 10px 20px; margin-block: 8px; background: var(--fg); color: var(--bg); cursor: pointer; }
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
  .account { align-items: flex-start; flex-direction: column; padding-block: 20px; }
  .stats { gap: 12px; }
  .stats strong { font-size: 30px; }
  .post-title { align-items: start; flex-direction: column; gap: 4px; }
  .preference-row { gap: 12px; }
  .preference-row select { width: 150px; }
  .preferences[open] { max-width: 100%; }
  .post-meta dl { grid-template-columns: 1fr; gap: 4px; }
  .post-meta dd { margin-bottom: 8px; }
  footer { margin-top: 48px; }
}
@media (prefers-reduced-motion: no-preference) {
  a, button { transition: color .15s ease, background-color .15s ease; }
  .brand:is(:hover, :focus-visible) > svg { animation: msg-logo-turn .7s cubic-bezier(.22, 1, .36, 1); }
  .token-art[data-running] .token {
    animation: token-assemble 12s cubic-bezier(.22, 1, .36, 1) infinite;
    animation-delay: var(--phase); animation-play-state: paused;
  }
  .token-art[data-running=true] .token { animation-play-state: running; }
}
@keyframes msg-logo-turn {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
@keyframes token-assemble {
  0%, 12%, 100% { transform: translate(var(--dx), var(--dy)) rotate(var(--turn)); opacity: .38; }
  38%, 65% { transform: translate(0, 0) rotate(0deg); opacity: 1; }
  85% { transform: translate(var(--dx), var(--dy)) rotate(var(--turn)); opacity: .38; }
}
@media (forced-colors: active) {
  select, button, .account .primary { border: 1px solid ButtonText; }
  :focus-visible { outline-color: Highlight; }
}

.document-actions { display: flex; align-items: center; gap: 14px; }
.document-actions button { padding: 4px 0; background: transparent; border: 0; cursor: pointer; color: var(--muted); font-size: .875rem; min-height: 36px; }
.document-actions button:hover { color: var(--accent); }
.document-status { color: var(--muted); font-size: .875rem; margin: 0 0 16px; }
.document-status:empty { display: none; }
#msg-document-source:not([hidden]) { width: 100%; min-height: 150px; background: var(--panel); color: var(--fg); border: 1px solid var(--line); border-radius: 8px; padding: 12px; }
.certificate-paper {
  --paper: #fbf8ee; --ink: #302b23; --paper-muted: #70624d; --paper-line: #b7a583;
  position: relative; max-width: 860px; margin: 24px auto; padding: 48px 52px 36px;
  background: var(--paper); color: var(--ink); border: 1px solid var(--paper-line);
}
.certificate-paper::before { content: ''; pointer-events: none; position: absolute; inset: 10px; border: 1px solid var(--paper-line); }
.certificate-paper a { color: inherit; text-decoration-color: var(--paper-line); }
.certificate-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 20px; padding-bottom: 24px; border-bottom: 1px solid var(--paper-line); }
.prose .certificate-heading h1, .prose .certificate-heading h2 { font-family: 'Noto Serif CJK SC', 'Songti SC', 'Noto Serif', Georgia, serif; font-size: clamp(1.4rem, 3vw, 2rem); margin: 0; font-weight: 500; letter-spacing: .02em; }
.certificate-state { font-size: .75rem; color: var(--paper-muted); white-space: nowrap; }
.certificate-paper[data-status=revoked] .certificate-state, .certificate-paper[data-status=expired] .certificate-state { color: #9b3028; font-weight: 600; }
.prose p.certificate-holder-label { margin: 32px 0 6px; color: var(--paper-muted); font-size: .875rem; }
.prose p.certificate-holder { font-family: 'Noto Serif CJK SC', 'Songti SC', Georgia, serif; font-size: clamp(1.75rem, 5vw, 2.6rem); line-height: 1.25; margin: 0 0 32px; overflow-wrap: anywhere; }
.certificate-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 22px 32px; margin: 0; }
.certificate-fields div:last-child { grid-column: 1 / -1; }
.certificate-fields dt { color: var(--paper-muted); font-size: .75rem; margin-bottom: 5px; }
.certificate-fields dd { margin: 0; font-size: .9rem; overflow-wrap: anywhere; }
.cert-identifier { font-family: var(--mono); font-size: .8em; overflow-wrap: anywhere; }
.certificate-grants { margin-top: 32px; border-top: 1px solid var(--paper-line); padding-top: 24px; }
.prose .certificate-grants h2 { margin: 0 0 12px; font-size: 1rem; }
.prose .certificate-grants > ul { list-style: none; padding: 0; margin: 0; }
.certificate-grants > ul > li { border-bottom: 1px solid var(--paper-line); padding: 8px 0; }
.certificate-grants summary { cursor: pointer; font-size: .875rem; }
.certificate-grants summary > span { color: var(--paper-muted); }
.certificate-grants p, .certificate-grants li { font-size: .8rem; overflow-wrap: anywhere; }
.certificate-paper pre, .certificate-paper code { background: transparent; color: var(--ink); }
.certificate-signature { display: flex; justify-content: flex-end; align-items: center; gap: 20px; margin-top: 32px; padding-top: 16px; }
.certificate-signature > div { max-width: 60%; overflow-wrap: anywhere; }
.certificate-signature > div > span, .certificate-signature small { color: var(--paper-muted); font-size: .75rem; }
.prose .certificate-signature p { margin: 5px 0; }
.certificate-seal { flex: 0 0 82px; width: 82px; height: 82px; fill: none; stroke: var(--paper-muted); stroke-width: 1; transform: rotate(-8deg); }
.certificate-seal text { fill: var(--paper-muted); stroke: none; font-family: Georgia, serif; font-size: 20px; letter-spacing: 2px; }
.certificate-open { display: inline-block; margin-top: 24px; font-size: .875rem; }
.prose .certificate-note { margin: 16px auto 24px; max-width: 860px; font-size: .8rem; color: var(--muted); }
.certificate-technical { max-width: 860px; margin: 0 auto 32px; }
.certificate-technical summary { cursor: pointer; color: var(--muted); font-size: .875rem; }
.certificate-technical pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 460px; overflow-y: auto; font-size: .75rem; }
.certificate-collection { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
.certificate-compact { margin: 0; padding: 32px 30px; width: 100%; }
.certificate-compact .certificate-heading { display: block; padding-bottom: 16px; }
.certificate-compact .certificate-state { display: block; margin-top: 8px; }
.certificate-compact .certificate-fields { gap: 16px; grid-template-columns: 1fr; }
.certificate-compact .certificate-holder { font-size: 1.65rem; }
.certificate-unavailable { padding: 24px; border: 1px solid var(--line); }
:root[data-theme=dark] .certificate-paper { --paper: #24211b; --ink: #eee7d7; --paper-muted: #c6b99e; --paper-line: #80745d; }
:root[data-theme=dark] .certificate-paper[data-status=revoked] .certificate-state, :root[data-theme=dark] .certificate-paper[data-status=expired] .certificate-state { color: #ffb1a7; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme=light]) .certificate-paper { --paper: #24211b; --ink: #eee7d7; --paper-muted: #c6b99e; --paper-line: #80745d; }
  :root:not([data-theme=light]) .certificate-paper[data-status=revoked] .certificate-state, :root:not([data-theme=light]) .certificate-paper[data-status=expired] .certificate-state { color: #ffb1a7; }
}
@media (max-width: 760px) {
  .certificate-collection { grid-template-columns: 1fr; }
  .certificate-paper { padding: 32px 26px 24px; }
  .certificate-heading { flex-direction: column; gap: 8px; }
  .certificate-fields { grid-template-columns: 1fr; }
  .certificate-seal { flex-basis: 66px; width: 66px; height: 66px; }
  .toolbar { flex-wrap: wrap; }
}
@media print {
  .site-header, .toolbar, .document-status, .certificate-note, .certificate-technical, .certificate-open { display: none; }
  :root[data-theme] .certificate-paper, :root:not([data-theme]) .certificate-paper { --paper: white; --ink: black; --paper-muted: #444; --paper-line: #777; break-inside: avoid; }
}

.certificate-copy-image { display: block; margin-top: 20px; padding: 8px 0; border: 0; background: transparent; color: var(--ink); cursor: pointer; font-size: .875rem; }
.certificate-copy-image:disabled { opacity: .5; cursor: wait; }
.wallet-transfer { border-top: 1px solid var(--line); margin-top: 28px; padding-top: 20px; }
.wallet-transfer-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; max-width: 640px; }
.wallet-transfer-fields label:last-child { grid-column: 1 / -1; }
.wallet-transfer-fields label > span { display: block; margin-bottom: 8px; color: var(--muted); font-size: .875rem; }
.wallet-transfer input, .wallet-transfer textarea { width: 100%; padding: 12px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
.wallet-transfer button { padding: 12px 16px; margin-top: 16px; background: var(--panel); color: var(--accent); border: 1px solid var(--line); border-radius: 8px; cursor: pointer; }
.wallet-transfer textarea { min-height: 130px; font-family: var(--mono); }
@media (max-width: 480px) { .wallet-transfer-fields { grid-template-columns: 1fr; } }
@media print { .certificate-copy-image { display: none; } }

.search-surface{max-width:760px;margin:9vh auto 5rem}.search-wordmark{font:clamp(14px,3vw,22px)/1.12 var(--mono);width:max-content;margin:0 auto 2.4rem;color:var(--accent);border:0;background:none;padding:0;overflow:visible}.search-form{display:flex;gap:.65rem;align-items:center}.search-form input{flex:1;min-width:0;border:1px solid var(--line);border-radius:2px;background:var(--bg);color:var(--fg);padding:.85rem 1rem;font:inherit;outline:none}.search-form input:focus{border-color:var(--accent);outline:2px solid var(--accent);outline-offset:3px}.search-form button{min-height:48px;background:var(--panel);border:1px solid var(--line);border-radius:2px;color:var(--fg);font:13px var(--mono);cursor:pointer;padding:.7rem 1rem}.search-form button:hover{border-color:var(--accent);color:var(--accent)}.search-help{margin:2rem auto;max-width:640px;font-size:.85rem;color:var(--muted)}.search-help summary{cursor:pointer;text-align:center}.search-help code{overflow-wrap:anywhere}.search-has-results{margin-top:2rem}.search-has-results .search-wordmark{font-size:12px;margin-bottom:1.5rem}.search-results{margin-top:2.5rem}.search-results article{margin:0;padding:1.25rem 0;border-bottom:1px solid var(--line)}.search-results article:first-child{padding-top:0}.search-results h2{font-family:inherit;font-size:1.2rem;font-weight:500;line-height:1.4;margin:.2rem 0}.search-results p{font-size:.92rem;margin:.25rem 0;line-height:1.6}.search-result-path{color:var(--muted);overflow-wrap:anywhere;font-family:var(--mono);font-size:.75rem}.search-feedback{margin:2rem 0;color:var(--muted)}

.page-document .site-header{max-width:1088px;display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:20px;align-items:center;padding-block:20px 12px}.page-document .site-header nav{margin:0;min-width:0}.account-menu{position:relative;font-size:14px}.account-menu summary{cursor:pointer;list-style:none;white-space:nowrap;max-width:180px;overflow:hidden;text-overflow:ellipsis}.account-menu summary::-webkit-details-marker{display:none}.account-menu-links{position:absolute;z-index:22;top:calc(100% + 16px);right:0;min-width:200px;padding:12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);box-shadow:0 12px 36px #0002}.account-menu-links a{display:block;padding:8px 12px;white-space:nowrap}.account-menu-links a:hover{background:var(--panel)}.document-toolbar{flex-basis:auto;align-items:center;gap:8px;padding:0;margin:0}.document-toolbar .preferences{margin-right:auto}.document-toolbar .preferences summary{justify-content:flex-start}.document-toolbar .preferences summary>span{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}.document-toolbar .settings-chevron{display:none}.document-toolbar .document-actions{gap:4px}.document-toolbar .document-actions button{display:inline-flex;align-items:center;justify-content:center;width:40px;height:40px;border-radius:8px}.document-toolbar .document-actions svg{width:17px;height:17px;fill:none;stroke:currentColor;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}.document-toolbar .document-actions button:hover{background:var(--panel)}.document-toolbar .raw-link{font:12px var(--mono);padding:12px 10px;text-decoration:none}.document-toolbar .preferences summary{padding-inline:8px;cursor:pointer}.document-toolbar .preferences summary:hover{color:var(--accent)}

.preferences{position:relative}.preferences[open]{flex:0 0 auto;max-width:100%}.preferences .preference-fields{position:absolute;z-index:20;top:100%;right:0;width:340px;max-width:calc(100vw - 44px);padding:20px;border:1px solid var(--line);border-radius:12px;background:var(--bg);box-shadow:0 12px 36px #0002}.document-toolbar .preferences .preference-fields{left:auto;right:0}.document-toolbar .preferences[open]{margin-right:0}.preferences .preference-row{gap:16px}.preferences .settings-note{margin-bottom:0}

@media(max-width:520px){.page-document .site-header{gap:8px;padding-inline:16px}.page-document .site-header .brand span{display:none}.page-document .account-menu summary{max-width:100px;font-size:12px}.page-document .document-toolbar{gap:0}.page-document .document-toolbar .document-actions button{width:36px}.page-document .document-toolbar .raw-link{padding-inline:6px}.page-document .site-header nav{gap:4px 14px}.document-toolbar{margin-left:auto}.document-toolbar .preferences .preference-fields{right:-130px}.preferences .accent-options{grid-template-columns:repeat(4,40px)}.preferences .accent-choice{width:40px}}

.document-toolbar .preferences summary{width:40px;box-sizing:border-box;justify-content:center}.document-toolbar .preferences[open]{width:40px}

.feed-filter { max-width: 640px; margin: 0 0 32px; }
.feed-filter > label { display: block; font-size: .875rem; color: var(--muted); margin-bottom: 8px; }
.feed-filter-row { display: flex; gap: 10px; }
.feed-filter input:not([type=hidden]) { flex: 1; min-width: 0; padding: 12px 14px; background: var(--panel); border: 1px solid var(--line); border-radius: 2px; }
.feed-filter button { padding: 12px 18px; border: 1px solid var(--line); border-radius: 2px; background: var(--panel); color: var(--accent); cursor: pointer; white-space: nowrap; }
.feed-filter button:hover { border-color: var(--accent); }
@media (max-width: 420px) { .feed-filter-row { flex-direction: column; } }

/* Menus stay within the visible viewport, including split windows and zoom. */
.preferences[open] { flex: 0 0 auto; max-width: 100%; }
.preferences .preference-fields, .document-toolbar .preferences .preference-fields, .account-menu-links {
  position: fixed; z-index: 20; right: auto;
  left: var(--menu-left, max(16px, calc((100vw - 340px) / 2)));
  top: var(--menu-top, 96px);
  width: var(--menu-width, 340px); max-width: calc(100vw - 32px);
  max-height: var(--menu-height, calc(100dvh - 112px)); overflow-y: auto;
  padding: 20px; border: 1px solid var(--line); border-radius: 12px;
  background: var(--bg); box-shadow: 0 12px 36px #0002;
}
.preferences .preference-row { flex-wrap: wrap; gap: 8px 16px; }
.preferences .accent-options { grid-template-columns: repeat(4, minmax(0, 40px)); max-width: 100%; }
.preferences .accent-choice { width: 40px; }
.account-menu { min-width: 0; }
.account-menu summary { display: flex; align-items: center; gap: 4px; min-height: 44px; }
.account-menu-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.account-menu summary > [aria-hidden] { flex: none; }
.account-menu-links { z-index: 22; min-width: 0; width: var(--menu-width, 280px); padding: 12px; border-radius: 8px; }
.account-menu-links a { white-space: normal; overflow-wrap: anywhere; }
.document-toolbar { flex-wrap: nowrap; }
.page-document .site-header { grid-template-columns: auto minmax(0, 1fr) auto; }
.page-document .site-header nav { justify-content: flex-end; }
.page-home .site-header > .brand { flex: none; }
.page-home .site-header nav { justify-content: flex-end; column-gap: 20px; }
@media (max-width: 640px) {
  .page-home .site-header nav { width: 100%; justify-content: flex-start; gap: 4px 18px; }
}
@media (max-width: 380px) {
  .page-document .account-menu summary { max-width: 70px; }
}

/* Touch targets: every control reaches 44px without moving the layout. Link rows
   in lists extend their hit area with padding that margin cancels. */
.brand { min-height: 44px; }
.raw-link, footer nav a { min-width: 44px; justify-content: center; }
.raw-link, .document-toolbar .raw-link { display: inline-flex; align-items: center; min-height: 44px; padding-block: 0; line-height: 1; }
.preference-row select, .preferences .preference-row select { min-height: 44px; }
.document-toolbar .document-actions button { width: 44px; height: 44px; }
.post-meta summary, .certificate-grants summary, .certificate-technical summary, .search-help summary,
.account-groups summary { display: flex; align-items: center; min-height: 44px; }
.search-help summary { justify-content: center; }
.post-title a, .search-results h2 a, .table-scroll td a { display: inline-block; padding-block: 10px; margin-block: -10px; }
.search-help button {
  min-height: 44px; padding: 8px 16px; cursor: pointer;
  color: var(--fg); background: var(--panel); border: 1px solid var(--line); border-radius: 2px;
}
.search-help button:hover { border-color: var(--accent); color: var(--accent); }
.page-auth input, .page-auth textarea, .feed-filter input:not([type=hidden]), .wallet-transfer input { min-height: 44px; }
.feed-filter button, .wallet-transfer button { min-height: 44px; }
.page-auth input:focus-visible, .page-auth textarea:focus-visible,
.feed-filter input:focus-visible, .wallet-transfer input:focus-visible, .wallet-transfer textarea:focus-visible {
  border-color: var(--accent); outline-offset: 2px;
}
@media (max-width: 520px) {
  .page-document .document-toolbar .document-actions button { width: 44px; }
  .page-document .site-header { padding-inline: 12px; }
}
"""

# Technical identifiers retain their reading order in right-to-left interfaces.
THEME_CSS += """
code, pre, .brand { direction: ltr; unicode-bidi: isolate; }
[dir=rtl] .settings-note { text-align: start; }
[dir=rtl] .document-toolbar .preferences { margin-right: 0; margin-inline-end: auto; }
"""
