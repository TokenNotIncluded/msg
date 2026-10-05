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
        + f'><span class="swatch" style="--swatch-light:{light};--swatch-dark:{color}"></span><span class="sr-only" data-i18n="{key}">{label}</span></label>'
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
  --bg: #fff; --fg: #111113; --muted: #5f5f67; --panel: #f4f4f6; --raised: #fff;
  --line: #e6e6ea; --line-strong: #c8c8d0;
  --accent-wash: color-mix(in srgb, var(--accent) 7%, var(--bg));
  --accent-soft: color-mix(in srgb, var(--accent) 20%, var(--bg));
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI Variable Text", "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", "Noto Sans", sans-serif;
  --mono: ui-monospace, "SFMono-Regular", "JetBrains Mono", Menlo, Consolas, monospace;
  --radius-control: 10px; --radius-panel: 16px;
  --shadow-pop: 0 1px 2px #0000000a, 0 18px 44px -14px #0000002e;
  --ease-out: cubic-bezier(.16, 1, .3, 1);
  --header-height: 68px;
  color-scheme: light;
}
:root[data-theme=dark] {
  --accent: var(--accent-dark); --bg: #0e0e10; --fg: #f2f2f4; --muted: #a3a3ad;
  --panel: #18181b; --raised: #161619; --line: #27272c; --line-strong: #42424a;
  --shadow-pop: 0 1px 2px #0008, 0 24px 56px -16px #000c; color-scheme: dark;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme=light]) {
    --accent: var(--accent-dark); --bg: #0e0e10; --fg: #f2f2f4; --muted: #a3a3ad;
    --panel: #18181b; --raised: #161619; --line: #27272c; --line-strong: #42424a;
    --shadow-pop: 0 1px 2px #0008, 0 24px 56px -16px #000c; color-scheme: dark;
  }
}

* { box-sizing: border-box; }
html {
  scroll-padding-block: calc(var(--header-height) + 16px) 24px;
  scrollbar-color: var(--line-strong) var(--bg); scrollbar-width: thin;
}
::selection { background: var(--fg); color: var(--bg); }
input, textarea { caret-color: var(--accent); }
body {
  margin: 0; background: var(--bg); color: var(--fg); overflow-x: clip;
  font: 16px/1.65 var(--sans); overflow-wrap: anywhere;
  -webkit-text-size-adjust: 100%; -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
}
a {
  color: var(--accent); text-decoration-thickness: 1px; text-underline-offset: .24em;
  text-decoration-color: color-mix(in srgb, currentColor 38%, transparent);
}
a:hover { text-decoration-color: currentColor; }
button, input, textarea, select { font: inherit; color: inherit; max-width: 100%; }
a, button, select, summary { -webkit-tap-highlight-color: transparent; }
summary { cursor: pointer; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; border-radius: 4px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
.skip-link {
  position: absolute; inset: 12px auto auto 16px; z-index: 40; transform: translateY(-160%);
  padding: 10px 18px; background: var(--fg); color: var(--bg); border-radius: 999px; text-decoration: none;
}
.skip-link:focus { transform: translateY(0); }

/* Header: a translucent strip that stays with the reader. The blur lives on a
   pseudo-element so fixed menus inside keep the viewport as containing block. */
.site-header, main { width: min(100%, 1128px); margin-inline: auto; padding-inline: 40px; }
.site-header {
  position: sticky; top: 0; z-index: 30; isolation: isolate;
  display: flex; align-items: center; justify-content: space-between; gap: 24px;
  min-height: var(--header-height); padding-block: 12px;
}
.site-header::before {
  content: ''; position: absolute; z-index: -1; inset: 0 calc(50% - 50vw);
  background: color-mix(in srgb, var(--bg) 82%, transparent);
  -webkit-backdrop-filter: saturate(1.6) blur(18px); backdrop-filter: saturate(1.6) blur(18px);
  border-bottom: 1px solid var(--line);
}
.brand {
  display: inline-flex; align-items: center; justify-content: center; gap: 10px; min-width: 44px; min-height: 44px;
  color: var(--fg); font: 650 21px/1 var(--mono); letter-spacing: -.04em;
  text-decoration: none; white-space: nowrap;
}
.brand svg { flex: none; width: 26px; height: 26px; transform-origin: center; }
nav { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; min-width: 0; }
nav a {
  display: inline-flex; align-items: center; min-height: 44px; padding-inline: 11px;
  border-radius: 999px; color: var(--muted); font: 500 14px/1.4 var(--sans);
  text-decoration: none; white-space: nowrap;
}
nav a:hover { color: var(--fg); background: var(--panel); }
.site-header nav .login {
  margin-inline-start: 6px; padding-inline: 18px; background: var(--fg); color: var(--bg);
}
.site-header nav .login:hover { background: color-mix(in srgb, var(--fg) 82%, var(--bg)); color: var(--bg); }
.current-account { overflow-wrap: anywhere; }

/* Toolbar, preferences and account menus. */
.toolbar { display: flex; align-items: center; justify-content: flex-end; gap: 8px; padding-block: 8px 0; }
.raw-link {
  display: inline-flex; align-items: center; justify-content: center; min-width: 44px; min-height: 44px;
  padding-inline: 10px; border-radius: 999px; color: var(--muted); font: 12px/1 var(--mono);
  text-decoration: none; white-space: nowrap;
}
.raw-link:hover { color: var(--fg); background: var(--panel); }
.preferences { position: relative; color: var(--fg); font-size: 13px; max-width: 100%; }
.preferences[open] { flex: 0 0 auto; max-width: 100%; }
.preferences summary {
  display: flex; align-items: center; justify-content: flex-end; gap: 8px; min-height: 44px;
  padding-inline: 12px; border-radius: 999px; color: var(--muted); list-style: none;
}
.preferences summary:hover, .preferences[open] summary { color: var(--fg); background: var(--panel); }
.preferences summary::-webkit-details-marker { display: none; }
.settings-icon, .settings-chevron { flex: none; }
.preferences[open] .settings-chevron { transform: rotate(180deg); }
.preferences .preference-fields, .account-menu-links {
  position: fixed; z-index: 20; right: auto;
  left: var(--menu-left, max(16px, calc((100vw - 340px) / 2)));
  top: var(--menu-top, 96px);
  width: var(--menu-width, 340px); max-width: calc(100vw - 32px);
  max-height: var(--menu-height, calc(100dvh - 112px)); overflow-y: auto;
  padding: 20px; border: 1px solid var(--line); border-radius: var(--radius-panel);
  background: var(--raised); box-shadow: var(--shadow-pop);
}
.preferences:not([open]) .preference-fields, .account-menu:not([open]) .account-menu-links { display: none; }
.preference-row { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px 16px; min-height: 52px; }
.preference-row + .preference-row { border-top: 1px solid var(--line); }
.preference-row > label, .preference-row > span { color: var(--muted); font-size: 12.5px; font-weight: 500; }
.preference-row select { width: 160px; min-height: 44px; font-size: 13px; }
.accent-options { display: grid; grid-template-columns: repeat(4, minmax(0, 40px)); max-width: 100%; }
.accent-choice, .theme-choice { position: relative; display: grid; place-items: center; width: 40px; height: 44px; cursor: pointer; }
.theme-choice { width: 44px; }
.accent-choice input, .theme-choice input { position: absolute; inset: 0; width: 100%; height: 100%; margin: 0; opacity: 0; cursor: pointer; }
.swatch { width: 18px; height: 18px; border-radius: 50%; background: var(--swatch-light); pointer-events: none; box-shadow: inset 0 0 0 1px #0000001a; }
:root[data-theme=dark] .swatch { background: var(--swatch-dark); }
@media (prefers-color-scheme: dark) { :root:not([data-theme=light]) .swatch { background: var(--swatch-dark); } }
.accent-choice input:checked + .swatch { outline: 2px solid var(--fg); outline-offset: 3px; }
.theme-options { display: flex; align-items: center; padding: 2px; border-radius: 999px; background: var(--panel); }
.theme-icon { display: grid; place-items: center; width: 40px; height: 36px; border-radius: 999px; color: var(--muted); pointer-events: none; }
.theme-choice input:checked + .theme-icon { background: var(--raised); color: var(--fg); box-shadow: 0 1px 3px #0000001f; }
.accent-choice input:focus-visible + .swatch, .theme-choice input:focus-visible + .theme-icon { outline: 2px solid var(--accent); outline-offset: 3px; }
.settings-note { color: var(--muted); font-size: 11.5px; margin: 12px 0 0; text-align: right; }
/* Gradients draw the chevron: page CSPs may forbid data: images. */
select {
  min-height: 44px; padding: 8px 34px 8px 12px; color: var(--fg); background-color: var(--bg);
  background-image: linear-gradient(45deg, transparent 50%, var(--muted) 50%), linear-gradient(135deg, var(--muted) 50%, transparent 50%);
  background-position: calc(100% - 19px) 52%, calc(100% - 14px) 52%; background-size: 5px 5px; background-repeat: no-repeat;
  border: 1px solid var(--line-strong); border-radius: var(--radius-control); font: inherit; cursor: pointer;
  -webkit-appearance: none; appearance: none;
}
[dir=rtl] select { padding: 8px 12px 8px 34px; background-position: 14px 52%, 19px 52%; }
select:hover { border-color: var(--muted); }
.account-menu { position: relative; min-width: 0; font-size: 14px; }
.account-menu summary {
  display: flex; align-items: center; gap: 4px; min-height: 44px; max-width: 180px;
  padding-inline: 12px; border-radius: 999px; color: var(--fg); font-weight: 500;
  list-style: none; white-space: nowrap; cursor: pointer;
}
.account-menu summary:hover, .account-menu[open] summary { background: var(--panel); }
.account-menu summary::-webkit-details-marker { display: none; }
.account-menu summary > [aria-hidden] { flex: none; color: var(--muted); }
.account-menu-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.account-menu-links { z-index: 22; min-width: 0; width: var(--menu-width, 280px); padding: 8px; }
.account-menu-links a {
  display: flex; align-items: center; min-height: 44px; padding: 8px 12px; border-radius: var(--radius-control);
  color: var(--fg); font-size: 14px; text-decoration: none; white-space: normal; overflow-wrap: anywhere;
}
.account-menu-links a:hover { background: var(--panel); }
.account-menu-links .current-account { font-weight: 600; }

/* Type: people's writing is sans; identifiers, time and counts are mono. */
h1 {
  font: 650 clamp(34px, 5vw, 54px)/1.1 var(--sans); letter-spacing: -.03em;
  margin: 24px 0 20px; text-wrap: balance;
}
h2 { font: 620 22px/1.3 var(--sans); letter-spacing: -.02em; margin: 0 0 16px; text-wrap: balance; }
h3 { font: 600 17px/1.4 var(--sans); letter-spacing: -.01em; }
.lead { color: var(--muted); max-width: 56ch; font-size: 18px; margin: 0; }
.muted, time, footer { color: var(--muted); }
time { flex-shrink: 0; font: 12px/1.6 var(--mono); font-variant-numeric: tabular-nums; }
section { margin-block: 52px; }
.hero { margin: 36px 0 40px; }

/* The static default is a readable MSG; only the visible hero runs. */
.token-art { position: relative; width: min(100%, 720px); margin: 12px 0 24px; }
.token-cloud { display: block; width: 100%; height: auto; overflow: hidden; }
.token { fill: var(--fg); font: 16px var(--mono); text-anchor: middle; transform-box: fill-box; transform-origin: center; }
.token-accent { fill: var(--accent); }
.token-pause { position: absolute; right: 0; bottom: 0; width: 44px; height: 44px; padding: 15px; border: 0; border-radius: 0; background: none; color: var(--muted); }
.token-pause:hover { color: var(--fg); background: none; }
.token-pause .play-mark, .token-pause[aria-pressed=true] .pause-mark { display: none; }
.token-pause[aria-pressed=true] .play-mark { display: initial; }

/* Filled action: the brand's capsule with a drawn arrow. */
.primary {
  display: inline-flex; align-items: center; justify-content: center; gap: 10px;
  min-height: 46px; padding: 0 22px; border-radius: 999px; background: var(--fg); color: var(--bg);
  font: 600 15px/1 var(--sans); text-decoration: none; white-space: nowrap;
}
.primary::after {
  content: ''; flex: none; width: 7px; height: 7px; margin-inline-end: 2px;
  border-top: 1.6px solid currentColor; border-right: 1.6px solid currentColor; transform: rotate(45deg);
}
.primary:hover { background: color-mix(in srgb, var(--fg) 84%, var(--bg)); color: var(--bg); }
.primary:hover::after { transform: translateX(3px) rotate(45deg); }

/* Home: an editorial index. Section labels hold the left column. */
.page-home #content > .public-board { margin-block: 4px 8px; }
.account {
  display: flex; align-items: center; justify-content: space-between; gap: 16px 32px;
  margin-block: 12px 56px; padding: 22px 26px; background: var(--panel); border-radius: var(--radius-panel);
}
.account:has(> .primary) { flex-direction: row-reverse; }
.account h2 { font-size: 15px; margin: 0 0 2px; }
.account p { margin: 0; color: var(--muted); font-size: 14.5px; line-height: 1.6; max-width: 62ch; }
.account p a { color: var(--fg); font: 600 15px/1.6 var(--mono); text-decoration: none; }
.account nav a { color: var(--fg); }
.account nav a:hover { background: var(--bg); }
.account-groups summary { color: var(--muted); font-size: 13px; }
.page-home #content > section:is(.activity, .latest, .channels) {
  display: grid; grid-template-columns: 200px minmax(0, 1fr); column-gap: 56px;
  margin-block: 0; padding-block: 44px 52px; border-top: 1px solid var(--line);
}
.page-home #content > section:is(.activity, .latest, .channels) > h2 {
  grid-column: 1; grid-row: 1 / span 6; align-self: start;
  position: sticky; top: calc(var(--header-height) + 24px);
  display: flex; align-items: center; gap: 10px; margin: 2px 0 24px; font-size: 15px; letter-spacing: -.01em;
}
.page-home #content > section:is(.activity, .latest, .channels) > h2::before {
  content: ''; flex: none; width: 7px; height: 7px; border-radius: 50%; border: 1.5px solid var(--accent);
}
.page-home #content > section:is(.activity, .latest, .channels) > :not(h2) { grid-column: 2; }
.stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
.stats > div { padding: 0 24px; border-inline-start: 1px solid var(--line); }
.stats > div:first-child { padding-inline-start: 0; border: 0; }
.stats strong {
  display: block; font: 620 clamp(30px, 3.6vw, 44px)/1.1 var(--sans); letter-spacing: -.03em;
  font-variant-numeric: tabular-nums;
}
.stats span { display: block; margin-top: 6px; font-size: 13px; color: var(--muted); }
.activity-note { margin: 20px 0 0; font: 12px/1.6 var(--mono); }

/* Latest posts ride one signal wire; each post is a node on it. */
.posts { position: relative; list-style: none; margin: 0; padding: 0; }
.posts li { padding-block: 16px 22px; }
.page-home .posts { padding-inline-start: 34px; }
.page-home .posts::before {
  content: ''; position: absolute; inset-inline-start: 5px; top: 30px; bottom: 0; width: 1px;
  background: linear-gradient(var(--line-strong) calc(100% - 64px), transparent);
}
.page-home .posts::after {
  content: ''; position: absolute; inset-inline-start: 5px; top: 30px; width: 1px; height: 72px;
  background: linear-gradient(transparent, var(--accent)); opacity: 0; pointer-events: none;
}
.page-home .posts li { position: relative; }
.page-home .posts li::before {
  content: ''; position: absolute; inset-inline-start: -34px; top: 24px; width: 11px; height: 11px;
  border-radius: 50%; background: var(--bg); border: 1.5px solid var(--line-strong);
}
.page-home .posts li:hover::before { background: var(--accent); border-color: var(--accent); }
.post-title { display: flex; justify-content: space-between; align-items: baseline; gap: 8px 28px; }
.post-title a {
  display: inline-block; padding-block: 10px; margin-block: -10px;
  color: var(--fg); font: 600 19px/1.4 var(--sans); letter-spacing: -.015em; text-decoration: none;
}
.post-title a:hover { color: var(--accent); }
.post-engagement { color: var(--muted); font-size: 13px; }
.post-engagement .muted { font-size: 13px; }
.posts p { color: var(--muted); font-size: 15px; line-height: 1.7; margin: 4px 0 0; max-width: 68ch; }

.empty-state {
  margin: 0; padding: 28px; border: 1px dashed var(--line-strong); border-radius: var(--radius-panel);
  color: var(--muted); font-size: 15px; text-align: center;
}
.notice {
  display: flex; align-items: flex-start; gap: 12px; margin-block: 16px; padding: 14px 18px;
  border: 1px solid var(--line); border-radius: 12px; background: var(--raised); color: var(--fg); font-size: 14.5px;
}
.notice::before { content: ''; flex: none; width: 8px; height: 8px; margin-top: .55em; border-radius: 50%; background: var(--accent); }
.scroll-hint { display: none; color: var(--muted); font: 11px/1.6 var(--mono); }

/* Tables: hairline rows, tabular numerals, mono identifiers. */
.table-scroll { max-width: 100%; overflow-x: auto; overscroll-behavior-inline: contain; border-radius: 4px; }
table { width: 100%; border-collapse: collapse; text-align: start; font-size: 14px; font-variant-numeric: tabular-nums; }
.table-scroll table { min-width: 600px; }
th, td { padding: 14px 16px; vertical-align: top; text-align: start; }
th { color: var(--muted); font-size: 12px; font-weight: 500; letter-spacing: .02em; border-bottom: 1px solid var(--line-strong); padding-block: 10px; }
td { border-bottom: 1px solid var(--line); }
th:first-child, td:first-child { padding-inline-start: 0; }
th:last-child, td:last-child { padding-inline-end: 0; }
td a { text-decoration: none; }
td a:hover { text-decoration: underline; }
.table-scroll td a, .channels .muted a { display: inline-block; min-width: 44px; padding-block: 12px; margin-block: -12px; }
.post-meta a, .thread-meta .thread-author, .account p a { min-width: 44px; }
.account p a { display: inline-flex; align-items: center; min-height: 44px; }
tbody tr:hover { background: var(--accent-wash); }
.number, th.number { text-align: right; font-variant-numeric: tabular-nums; }
.mode { font: 12.5px/1.65 var(--mono); white-space: nowrap; }
.mode a { color: var(--fg); }
.channels td:first-child a { color: var(--fg); font: 600 14px/1.5 var(--mono); }
.channels .muted { margin: 0 0 20px; font-size: 14px; max-width: 72ch; }

footer {
  display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 24px;
  margin-top: 88px; padding-block: 24px 48px; border-top: 1px solid var(--line); font-size: 13px;
}
footer nav { gap: 0 2px; margin-inline-start: -10px; }
footer nav a { min-width: 44px; justify-content: center; padding-inline: 10px; color: var(--muted); font-size: 13px; }
footer p { margin: 0; font: 12px/1.6 var(--mono); }

.page-topic-index .prose > h2 { font-size: 1.25rem; font-weight: 600; line-height: 1.45; margin-block: 44px 6px; }
.page-topic-index .prose > h2 > a, .page-topic-index .prose > p > a[href*="post_cursor="] {
  display: inline-flex; align-items: center; min-height: 44px; min-width: 44px; max-width: 100%;
}
.page-topic-index .prose > h2 > a { color: var(--fg); text-decoration: none; }
.page-topic-index .prose > h2 > a:hover { color: var(--accent); }
.page-topic-index .prose > p { line-height: 1.75; color: var(--muted); }
.page-topic-index .prose > hr { border: 0; height: 0; margin-block: 28px; }

/* Documents and posts. */
.page-document main { max-width: 880px; padding-bottom: 72px; }
.prose { padding-top: 20px; }
.prose h1 { font-size: clamp(34px, 5vw, 52px); margin: 28px 0 20px; }
.prose h2 { font-size: 24px; margin: 56px 0 14px; }
.prose h3 { font-size: 18px; margin: 36px 0 10px; }
.prose > p, .prose > ul, .prose > ol, .prose > blockquote { max-width: 68ch; }
.prose p, .prose li { line-height: 1.75; }
.prose ul, .prose ol { padding-inline-start: 1.35em; }
.prose li + li { margin-top: 4px; }
.prose li::marker { color: var(--muted); }
.prose strong { font-weight: 650; }
.prose table { display: block; overflow-x: auto; }
.prose img { border-radius: 12px; }
pre { padding: 20px; background: var(--panel); border-radius: 12px; overflow-x: auto; font-size: 13px; }
code, kbd { font-family: var(--mono); }
:not(pre) > code { padding: .12em .4em; border-radius: 6px; background: var(--panel); font-size: .875em; }
pre code { overflow-wrap: normal; white-space: pre; }
img { max-width: 100%; height: auto; }
blockquote {
  margin: 28px 0; padding: 2px 0 2px 22px; border-inline-start: 1px solid var(--fg);
  color: var(--fg); font-size: 1.125rem; line-height: 1.65;
}
blockquote p { margin: 8px 0; }
hr { border: 0; border-top: 1px solid var(--line); margin-block: 40px; }

/* The byline sits under the title as one quiet row. */
.post-meta {
  display: flex; flex-wrap: wrap; align-items: center; gap: 0 20px;
  margin: -6px 0 36px; padding-bottom: 14px; border-bottom: 1px solid var(--line);
  color: var(--muted); font-size: 13.5px;
}
.post-meta p { margin: 0; line-height: 44px; }
.post-meta p > span[data-i18n] { margin-inline-end: .15em; }
.post-meta a { display: inline-flex; align-items: center; min-height: 44px; vertical-align: top; color: var(--fg); font-weight: 550; text-decoration: none; }
.post-meta a:hover { color: var(--accent); text-decoration: underline; }
.post-meta .reply-link { font-size: 13.5px; }
.post-meta details { margin-inline-start: auto; }
.post-meta details[open] { flex-basis: 100%; margin-inline-start: 0; }
.post-meta summary { display: flex; align-items: center; min-height: 44px; font: 12px/1 var(--mono); color: var(--muted); list-style: none; }
.post-meta summary::-webkit-details-marker { display: none; }
.post-meta summary:hover { color: var(--fg); }
.post-meta dl {
  display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 8px 16px; margin: 4px 0 8px;
  padding: 14px 16px; border-radius: 12px; background: var(--panel); font: 12px/1.6 var(--mono);
}
.post-meta dt { color: var(--muted); }
.post-meta dd { margin: 0; color: var(--fg); }
.certificate-grants summary, .certificate-technical summary, .account-groups summary { display: flex; align-items: center; min-height: 44px; }

/* Forms on sign-in, approval and settings pages. */
.page-auth main { max-width: 600px; padding-block: 8px 72px; }
.page-auth h1 { font-size: clamp(34px, 5vw, 46px); margin-top: 40px; }
.page-auth main > p, .page-auth #content > p { color: var(--muted); }
.page-auth label { display: block; margin-top: 8px; font-size: 14px; font-weight: 550; }
.page-auth input, .page-auth textarea {
  width: 100%; min-height: 48px; margin-block: 8px 16px; padding: 12px 16px;
  border: 1px solid var(--line-strong); border-radius: var(--radius-control); background: var(--raised);
}
.page-auth input:hover, .page-auth textarea:hover { border-color: var(--muted); }
.page-auth button {
  min-height: 46px; margin-block: 8px; padding: 10px 24px; border: 0; border-radius: 999px;
  background: var(--fg); color: var(--bg); font-weight: 600; cursor: pointer;
}
.page-auth button:hover:not(:disabled) { background: color-mix(in srgb, var(--fg) 84%, var(--bg)); }
.page-auth button:disabled { opacity: .55; cursor: wait; }
.page-auth .approval-code {
  display: block; padding: 22px; border: 1px dashed var(--line-strong); border-radius: var(--radius-panel);
  background: var(--panel); font: 600 16px/1.7 var(--mono); letter-spacing: .04em; user-select: all;
}
.page-auth #status { min-height: 2em; font-size: 14px; }

.feed-filter { max-width: 640px; margin: 0 0 40px; }
.feed-filter > label { display: block; font-size: .875rem; color: var(--muted); margin-bottom: 8px; }
.feed-filter-row { display: flex; gap: 10px; }
.feed-filter input:not([type=hidden]) {
  flex: 1; min-width: 0; min-height: 44px; padding: 12px 14px;
  background: var(--raised); border: 1px solid var(--line-strong); border-radius: var(--radius-control);
}
.feed-filter button {
  min-height: 44px; padding: 10px 20px; border: 0; border-radius: 999px;
  background: var(--fg); color: var(--bg); font-weight: 600; cursor: pointer; white-space: nowrap;
}
.feed-filter button:hover { background: color-mix(in srgb, var(--fg) 84%, var(--bg)); }
@media (max-width: 420px) { .feed-filter-row { flex-direction: column; } }

.page-auth input:focus-visible, .page-auth textarea:focus-visible,
.feed-filter input:focus-visible, .wallet-transfer input:focus-visible, .wallet-transfer textarea:focus-visible {
  border-color: var(--accent); outline-offset: 2px;
}

/* Document toolbar: brand | account | quiet icon actions. */
.page-document .site-header { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 16px; align-items: center; }
.page-document .site-header nav { justify-content: flex-end; margin: 0; min-width: 0; }
.document-toolbar { flex-wrap: nowrap; align-items: center; gap: 2px; padding: 0; margin: 0; }
.document-toolbar .preferences summary { width: 44px; padding: 0; justify-content: center; }
.document-toolbar .preferences summary > span {
  position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%);
}
.document-toolbar .settings-chevron { display: none; }
.document-toolbar .preferences[open] { width: 44px; }
.document-actions { display: flex; align-items: center; gap: 2px; }
.document-actions button {
  display: inline-flex; align-items: center; justify-content: center; width: 44px; height: 44px;
  padding: 0; border: 0; border-radius: 999px; background: transparent; color: var(--muted); cursor: pointer;
}
.document-actions button:hover { color: var(--fg); background: var(--panel); }
.document-actions svg { width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }
.document-status { color: var(--muted); font-size: .875rem; margin: 12px 0 0; }
.document-status:empty { display: none; }
#msg-document-source:not([hidden]) {
  width: 100%; min-height: 150px; margin-top: 16px; padding: 14px; background: var(--panel); color: var(--fg);
  border: 1px solid var(--line); border-radius: 12px; font: 13px/1.6 var(--mono);
}
.page-home .site-header > .brand { flex: none; }
.page-home .site-header nav { justify-content: flex-end; }

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
@media print { .certificate-copy-image { display: none; } }

.wallet-transfer { border-top: 1px solid var(--line); margin-top: 28px; padding-top: 20px; }
.wallet-transfer-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; max-width: 640px; }
.wallet-transfer-fields label:last-child { grid-column: 1 / -1; }
.wallet-transfer-fields label > span { display: block; margin-bottom: 8px; color: var(--muted); font-size: .875rem; }
.wallet-transfer input, .wallet-transfer textarea {
  width: 100%; min-height: 44px; padding: 12px; background: var(--raised);
  border: 1px solid var(--line-strong); border-radius: var(--radius-control);
}
.wallet-transfer textarea { min-height: 130px; font-family: var(--mono); }
.wallet-transfer button {
  min-height: 44px; margin-top: 16px; padding: 10px 22px; border: 0; border-radius: 999px;
  background: var(--fg); color: var(--bg); font-weight: 600; cursor: pointer;
}
@media (max-width: 480px) { .wallet-transfer-fields { grid-template-columns: 1fr; } }

/* Search: one confident field under the wordmark. */
.search-surface { max-width: 720px; margin: 10vh auto 5rem; }
.search-wordmark {
  width: max-content; margin: 0 auto 2.4rem; padding: 0; overflow: visible; border: 0; background: none;
  color: var(--fg); font: clamp(14px, 3vw, 22px)/1.12 var(--mono);
}
.search-form { display: flex; align-items: center; gap: 6px; padding: 6px; border: 1px solid var(--line-strong); border-radius: 999px; background: var(--raised); }
.search-form:hover { border-color: var(--muted); }
.search-form:focus-within { border-color: var(--accent); box-shadow: 0 0 0 4px var(--accent-soft); }
.search-form input {
  flex: 1; min-width: 0; min-height: 44px; padding: 0 16px; border: 0; background: transparent;
  color: var(--fg); font: 17px/1.4 var(--sans); outline: none;
}
.search-form input::placeholder { color: var(--muted); }
.search-form button {
  min-height: 44px; padding: 0 22px; border: 0; border-radius: 999px; background: var(--fg); color: var(--bg);
  font: 600 14px var(--sans); cursor: pointer;
}
.search-form button:hover { background: color-mix(in srgb, var(--fg) 84%, var(--bg)); }
.search-help { margin: 2rem auto; max-width: 640px; font-size: .875rem; color: var(--muted); }
.search-help summary { display: flex; align-items: center; justify-content: center; min-height: 44px; }
.search-help summary:hover { color: var(--fg); }
.search-help code { overflow-wrap: anywhere; }
.search-help button {
  min-height: 44px; padding: 8px 18px; cursor: pointer; color: var(--fg); background: var(--panel);
  border: 1px solid var(--line); border-radius: 999px;
}
.search-help button:hover { border-color: var(--muted); }
.search-has-results { margin-top: 2rem; }
.search-has-results .search-wordmark { font-size: 11px; margin-bottom: 1.5rem; color: var(--muted); }
.search-results { margin-top: 2.5rem; }
.search-results article { margin: 0; padding: 1.25rem 0; border-bottom: 1px solid var(--line); }
.search-results article:first-child { padding-top: 0; }
.search-results h2 { font: 600 1.2rem/1.4 var(--sans); letter-spacing: -.015em; margin: .2rem 0; }
.search-results h2 a { display: inline-block; padding-block: 10px; margin-block: -10px; color: var(--fg); text-decoration: none; }
.search-results h2 a:hover { color: var(--accent); text-decoration: underline; }
.search-results p { font-size: .9375rem; margin: .25rem 0; line-height: 1.65; color: var(--muted); }
.search-result-path { color: var(--muted); overflow-wrap: anywhere; font: .75rem/1.5 var(--mono); }
.search-feedback { margin: 2rem 0; color: var(--muted); }

/* Discussion as a circuit: replies are nodes; rails and elbows show parentage. */
.thread-discussion { margin-block: 56px 40px; min-width: 0; }
.prose .thread-heading {
  display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px 24px;
  margin-bottom: 20px; padding-bottom: 14px; border-bottom: 1px solid var(--line);
}
.prose .thread-heading h1, .prose .thread-heading h2 { margin: 0; font: 650 1.75rem/1.2 var(--sans); letter-spacing: -.025em; }
.prose .thread-range { margin: 4px 0 0; color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }
.thread-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
.thread-control {
  display: inline-flex; align-items: center; min-height: 44px; padding-inline: 14px; border-radius: 999px;
  color: var(--fg); font-size: .875rem; text-decoration: none;
}
.thread-control:hover { background: var(--panel); }
.prose .thread-notice, .prose .thread-empty { color: var(--muted); margin-block: 20px; }
.prose .thread-empty { padding: 24px; border: 1px dashed var(--line-strong); border-radius: var(--radius-panel); text-align: center; }
.prose .thread-tree { list-style: none; margin: 0; padding: 0; }
.prose .thread-discussion > .thread-tree:not(:has(> .thread-root)) { position: relative; margin-inline-start: 5px; padding: 6px 0 0 25px; }
.prose .thread-discussion > .thread-tree:not(:has(> .thread-root))::before {
  content: ''; position: absolute; inset-inline-start: -5px; top: -2px; width: 11px; height: 11px;
  border-radius: 50%; background: var(--fg);
}
.prose .thread-branch > .thread-tree { margin-inline-start: -21px; padding-inline-start: 25px; }
.prose .thread-node { position: relative; margin: 0; padding: 0 0 0 26px; min-width: 0; scroll-margin-block: calc(var(--header-height) + 16px) 24px; }
.thread-node + .thread-node { margin-block-start: 14px; }
.thread-node::before {
  content: ''; position: absolute; z-index: 1; inset-inline-start: 0; top: 16px; width: 11px; height: 11px;
  border-radius: 50%; background: var(--bg); border: 1.5px solid var(--line-strong);
}
.thread-node:has(> article:hover)::before { border-color: var(--accent); }
.thread-root::before { background: var(--fg); border-color: var(--fg); }
.thread-node:target::before, .thread-focus:not(.thread-root)::before {
  background: var(--accent); border-color: var(--accent); box-shadow: 0 0 0 4px var(--accent-soft);
}
.thread-node > article { position: relative; padding-block: 0 10px; }
.thread-tree:not(:has(> .thread-root)) > .thread-node::after {
  content: ''; position: absolute; inset-inline-start: -25px; top: 0; bottom: -14px; width: 25px;
  border-inline-start: 1px solid var(--line-strong);
}
.thread-tree:not(:has(> .thread-root)) > .thread-node:not(:last-child) > article::after {
  content: ''; position: absolute; inset-inline-start: -51px; top: 21.5px; width: 25px; height: 1px; background: var(--line-strong);
}
.thread-tree:not(:has(> .thread-root)) > .thread-node:last-child::after {
  bottom: auto; height: 22px; border-bottom: 1px solid var(--line-strong); border-end-start-radius: 12px;
}
.thread-node:has(> .thread-branch) > article::before {
  content: ''; position: absolute; inset-inline-start: -21px; top: 28px; bottom: 0; border-inline-start: 1px solid var(--line-strong);
}
.thread-branch > summary { position: relative; }
.thread-branch > summary::before {
  content: ''; position: absolute; inset-inline-start: -21px; top: 0; height: 50%; width: 14px;
  border-inline-start: 1px solid var(--line-strong); border-bottom: 1px solid var(--line-strong); border-end-start-radius: 8px;
}
.thread-branch[open] > summary::before { height: auto; bottom: 0; width: 0; border-bottom: 0; border-radius: 0; }
.thread-node:target > article .thread-author, .thread-node:target > article .thread-title a { text-decoration: underline; text-underline-offset: .35em; }
.prose .thread-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 0 14px; margin: 0; font-size: .8125rem; line-height: 1.5; color: var(--muted); }
.thread-meta a { display: inline-flex; align-items: center; min-height: 44px; }
.thread-meta .thread-author { color: var(--fg); font: 600 .9375rem/1.5 var(--sans); text-decoration: none; }
.thread-meta .thread-author:hover { color: var(--accent); }
.thread-meta .thread-permalink, .thread-meta .thread-parent { color: var(--muted); text-decoration: none; }
.thread-meta .thread-permalink:hover, .thread-meta .thread-parent:hover { color: var(--fg); text-decoration: underline; }
.thread-node-kind { padding-inline-start: 0; }
.prose .thread-title { margin: -10px 0 2px; font: 600 1.0625rem/1.45 var(--sans); letter-spacing: -.01em; }
.thread-title a { display: inline-flex; align-items: center; min-height: 44px; color: var(--fg); text-decoration: none; }
.thread-title a:hover { color: var(--accent); }
.prose .thread-root > article .thread-title { font-size: 1.25rem; }
.prose .thread-body { max-width: 68ch; margin: 0; font-size: .96875rem; line-height: 1.75; }
.thread-body > :first-child, .thread-content > :first-child { margin-top: 0; }
.thread-body > :last-child, .thread-content > :last-child { margin-bottom: 0; }
.prose .thread-body h1, .prose .thread-body h2, .prose .thread-body h3 { font-size: 1rem; margin: 20px 0 8px; }
.thread-long-body > summary { min-height: 44px; cursor: pointer; list-style: none; }
.thread-long-body > summary::-webkit-details-marker { display: none; }
.thread-excerpt { display: block; color: var(--fg); margin-bottom: 8px; }
.thread-expand-label, .thread-collapse-label { display: inline-flex; align-items: center; min-height: 44px; color: var(--accent); font-size: .875rem; text-decoration: underline; text-underline-offset: .22em; }
.thread-collapse-label, .thread-long-body[open] > summary .thread-expand-label, .thread-long-body[open] > summary .thread-excerpt { display: none; }
.thread-long-body[open] > summary .thread-collapse-label { display: inline-flex; }
.prose .thread-truncation { color: var(--muted); font-size: .8125rem; }
.prose .thread-stats { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 14px; margin: 10px 0 0; min-height: 28px; color: var(--muted); font-size: 12.5px; font-variant-numeric: tabular-nums; }
.thread-branch > summary {
  display: flex; align-items: center; width: fit-content; max-width: 100%; min-height: 44px;
  color: var(--muted); font-size: 12.5px; font-variant-numeric: tabular-nums; list-style: none;
}
.thread-branch > summary:hover { color: var(--fg); }
.thread-branch > summary::-webkit-details-marker { display: none; }
.thread-branch-count { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 14px; }
.thread-branch-chevron { flex: none; box-sizing: content-box; padding: 4px; border: 1px solid var(--line-strong); border-radius: 50%; background: var(--bg); }
.thread-branch > summary:hover .thread-branch-chevron { border-color: var(--fg); }
.thread-branch[open] > summary .thread-branch-chevron { transform: rotate(90deg); }
.prose .thread-branch-pending { margin: 4px 0 16px; font-size: .875rem; color: var(--muted); }
.thread-branch [data-thread-fragment] { margin-block: 4px 16px; }
.thread-node[data-depth="8"] .thread-branch > .thread-tree { margin-inline-start: 0; padding-inline-start: 0; }
.thread-node[data-depth="8"] .thread-tree .thread-node::after, .thread-node[data-depth="8"] .thread-tree .thread-node > article::after { display: none; }
.prose .thread-more { margin-block: 28px 0; padding-top: 8px; }

@media (max-width: 860px) {
  .page-home #content > section:is(.activity, .latest, .channels) { display: block; padding-block: 36px 40px; }
  .page-home #content > section:is(.activity, .latest, .channels) > h2 { position: static; margin-bottom: 24px; }
}
@media (max-width: 640px) {
  :root { --header-height: 60px; }
  .site-header, main { padding-inline: 20px; }
  .site-header { position: relative; flex-wrap: wrap; gap: 4px 16px; padding-block: 10px 6px; }
  .site-header::before { -webkit-backdrop-filter: none; backdrop-filter: none; background: var(--bg); }
  .page-home .site-header nav {
    flex-wrap: nowrap; justify-content: flex-start; overflow-x: auto; scrollbar-width: none;
    width: calc(100% + 40px); margin-inline: -20px; padding-inline: 14px; overscroll-behavior-inline: contain;
  }
  .page-home .site-header nav::-webkit-scrollbar { display: none; }
  .hero { margin-top: 28px; }
  .scroll-hint { display: block; }
  h1 { font-size: 36px; }
  .prose h1 { font-size: 34px; }
  .lead { font-size: 17px; }
  section { margin-block: 40px; }
  .account { align-items: flex-start; flex-direction: column; padding: 20px; }
  .account:has(> .primary) { flex-direction: column-reverse; }
  .stats > div { padding-inline: 14px; }
  .stats strong { font-size: 28px; }
  .page-home .posts { padding-inline-start: 26px; }
  .page-home .posts li::before { inset-inline-start: -26px; }
  .post-title { align-items: start; flex-direction: column; gap: 2px; }
  .preference-row select { width: 150px; }
  .post-meta { gap: 0 14px; }
  .post-meta dl { grid-template-columns: 1fr; gap: 2px; }
  .post-meta dd { margin-bottom: 8px; }
  footer { margin-top: 56px; }
  .prose .thread-discussion > .thread-tree:not(:has(> .thread-root)) { padding-inline-start: 18px; }
  .thread-tree:not(:has(> .thread-root)) > .thread-node::after { inset-inline-start: -18px; width: 18px; }
  .thread-tree:not(:has(> .thread-root)) > .thread-node:not(:last-child) > article::after { inset-inline-start: -40px; width: 18px; }
  .prose .thread-branch > .thread-tree { padding-inline-start: 18px; }
  .prose .thread-node { padding-inline-start: 22px; }
  .thread-node:has(> .thread-branch) > article::before, .thread-branch > summary::before { inset-inline-start: -17px; }
  .prose .thread-branch > .thread-tree { margin-inline-start: -17px; }
  .thread-node[data-depth="3"] .thread-branch > .thread-tree { margin-inline-start: 0; padding-inline-start: 0; }
  .thread-node[data-depth="3"] .thread-tree .thread-node::after, .thread-node[data-depth="3"] .thread-tree .thread-node > article::after { display: none; }
  .thread-meta { column-gap: 10px; }
}
@media (max-width: 520px) {
  .page-document .site-header { gap: 4px; padding-inline: 12px; }
  .page-document .site-header .brand span { display: none; }
  .page-document .account-menu summary { max-width: 110px; font-size: 13px; }
  .page-document .site-header nav { gap: 4px 14px; }
  .document-toolbar { margin-left: auto; gap: 0; }
  .document-toolbar .raw-link { padding-inline: 6px; }
}
@media (max-width: 380px) {
  .page-document .account-menu summary { max-width: 76px; }
}
@media (prefers-reduced-motion: no-preference) {
  a, button, summary, select, input, textarea, .search-form {
    transition: color .18s ease, background-color .18s ease, border-color .18s ease, box-shadow .18s ease;
  }
  .primary::after, .thread-branch-chevron, .settings-chevron { transition: transform .28s var(--ease-out); }
  .thread-node::before, .page-home .posts li::before { transition: background-color .2s ease, border-color .2s ease, box-shadow .2s ease; }
  .brand:is(:hover, :focus-visible) > svg { animation: msg-logo-turn .7s cubic-bezier(.22, 1, .36, 1); }
  .preferences[open] .preference-fields, .account-menu[open] .account-menu-links { animation: msg-pop .26s var(--ease-out); }
  .page-home .posts::after { animation: msg-signal-run 1.9s linear .35s 1 both; }
  .page-home .posts li::before {
    animation: msg-node-ping 1.1s var(--ease-out) backwards;
    animation-delay: calc(.3s + 1.9s * (var(--i, 0) + .15) / var(--n, 4));
  }
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
@keyframes msg-pop {
  from { opacity: 0; transform: translateY(-6px) scale(.98); }
}
@keyframes msg-signal-run {
  0% { top: 0; opacity: 0; }
  12%, 82% { opacity: 1; }
  100% { top: calc(100% - 72px); opacity: 0; }
}
@keyframes msg-node-ping {
  0% { background: var(--bg); border-color: var(--line-strong); }
  25% { background: var(--accent); border-color: var(--accent); box-shadow: 0 0 0 5px var(--accent-soft); }
  100% { background: var(--bg); border-color: var(--line-strong); box-shadow: 0 0 0 0 transparent; }
}
@keyframes token-assemble {
  0%, 12%, 100% { transform: translate(var(--dx), var(--dy)) rotate(var(--turn)); opacity: .38; }
  38%, 65% { transform: translate(0, 0) rotate(0deg); opacity: 1; }
  85% { transform: translate(var(--dx), var(--dy)) rotate(var(--turn)); opacity: .38; }
}
@media (forced-colors: active) {
  select, button, .primary, .search-form { border: 1px solid ButtonText; }
  .site-header::before { -webkit-backdrop-filter: none; backdrop-filter: none; }
  :focus-visible { outline-color: Highlight; }
}
"""

# Technical identifiers retain their reading order in right-to-left interfaces.
THEME_CSS += """
code, pre, .brand { direction: ltr; unicode-bidi: isolate; }
[dir=rtl] .settings-note { text-align: start; }
[dir=rtl] .primary::after { transform: rotate(-135deg); }
[dir=rtl] .primary:hover::after { transform: translateX(-3px) rotate(-135deg); }
"""
