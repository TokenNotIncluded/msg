"""One restrained, dependency-free visual language for browser-only views."""

from importlib.resources import files

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
    '<details class="preferences"><summary><svg class="settings-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/></svg><span data-i18n="display">Display settings</span><svg class="settings-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></summary>'
    '<div class="preference-fields"><div class="preference-row"><label for="msg-language" data-i18n="language">Language</label>'
    '<select id="msg-language"><option value="en">English</option><option value="zh">简体中文</option></select></div>'
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
.brand { display: inline-flex; align-items: center; gap: 10px; }
.brand svg { flex: none; width: 28px; height: 28px; }
nav { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 24px; min-width: 0; }
nav a { display: inline-flex; align-items: center; min-height: 44px; color: var(--fg); text-decoration: none; font-size: 14px; }
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
  .preference-row { gap: 12px; }
  .preference-row select { width: 150px; }
  .preferences[open] { max-width: 100%; }
  .post-meta dl { grid-template-columns: 1fr; gap: 4px; }
  .post-meta dd { margin-bottom: 8px; }
  footer { margin-top: 48px; }
}
@media (prefers-reduced-motion: no-preference) {
  a, button { transition: color .15s ease, background-color .15s ease; }
  .token-art[data-running] .token {
    animation: token-assemble 12s cubic-bezier(.22, 1, .36, 1) infinite;
    animation-delay: var(--phase); animation-play-state: paused;
  }
  .token-art[data-running=true] .token { animation-play-state: running; }
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
"""
