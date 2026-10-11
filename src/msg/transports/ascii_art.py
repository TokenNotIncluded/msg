"""Pinned, browser-only character art; never included in machine projections."""

from base64 import b64encode
from hashlib import sha256
from importlib.resources import files

SCRIPT = files('msg.data').joinpath('ascii-runtime.js').read_text()
HASH = b64encode(sha256(SCRIPT.encode()).digest()).decode()
TAG = '<script>' + SCRIPT + '</script>'

CONTROLS = (
    '<div class="ascii-controls" data-ascii-controls hidden role="group" '
    'aria-label="Character scene / 字符场景">'
    + ''.join(
        f'<button type="button" data-ascii-piece="{piece}" '
        f'aria-pressed="{str(piece == "community").lower()}" '
        f'aria-label="{english} / {chinese}">'
        f'<span class="ascii-piece-name" lang="en">{english}</span>'
        f'<span class="ascii-piece-label" lang="zh-CN">{chinese}</span></button>'
        for piece, english, chinese in [
            ('galaxy', 'Galaxy', '星系'),
            ('flow', 'Flow', '信号流'),
            ('aurora', 'Aurora', '极光'),
            ('community', 'Shared SVG', '公共作品'),
        ]
    )
    + '</div>'
)

CSS = """
/* An open, monochrome character stage. Text and controls never cover the art. */
.public-board {
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.12fr);
  gap: 0 64px; align-items: center; padding: 24px 0 52px;
}
.signal-copy { min-width: 0; }
.signal-meta {
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; min-height: 44px; margin-bottom: 18px;
}
.signal-mark { color: var(--muted); font: 12px/1 var(--mono); letter-spacing: .08em; }
.public-board .public-board-menu { position: relative; inset: auto; flex: none; }
.public-board .public-board-menu > summary { border-radius: 4px; }
.public-board .public-board-text {
  max-width: 18ch; margin: 0; font: 550 clamp(32px, 3.8vw, 48px)/1.28 var(--sans);
  letter-spacing: -.045em; text-wrap: balance;
}
.public-board .signal-links {
  display: flex; align-items: center; gap: 8px 24px; flex-wrap: wrap; margin: 26px 0 0;
}
.public-board .signal-links a {
  display: inline-flex; align-items: center; min-height: 44px; padding: 0;
  border-radius: 0; font: 12px/1.5 var(--mono); color: var(--muted);
  text-decoration: none; text-underline-offset: 6px;
}
.public-board .signal-links a:first-child { color: var(--fg); }
.public-board .signal-links a:hover { background: none; text-decoration: underline; }
.public-board .signal-links svg {
  width: 16px; height: 16px; margin-inline-start: 12px;
  stroke: currentColor; fill: none; stroke-width: 1.5;
}
.public-board .signal-stage { min-width: 0; color: var(--fg); }
/* Reserve the tallest scene's space; switching scenes must not move the page. */
.signal-viewport {
  position: relative; display: grid; place-items: center; min-width: 0;
  aspect-ratio: 16 / 13; isolation: isolate;
}
.signal-viewport::before, .signal-viewport::after {
  content: '+'; position: absolute; color: var(--line-strong);
  font: 14px/1 var(--mono); pointer-events: none;
}
.signal-viewport::before { inset: 0 auto auto 0; }
.signal-viewport::after { inset: auto 0 0 auto; }
.public-board .signal-viewport canvas, .public-board .signal-viewport img {
  grid-area: 1 / 1; display: block; width: 100%; min-width: 0; height: auto; margin: 0;
}
.public-board .signal-viewport img { aspect-ratio: 16 / 5; object-fit: contain; }
/* The vendor sets inline display:block; the hidden state must still win. */
.public-board .signal-viewport :is(canvas, img)[hidden] { display: none !important; }
.public-board .ascii-controls {
  display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 4px;
  margin-top: 16px; padding: 0;
}
.public-board .ascii-controls[hidden] { display: none; }
.public-board .ascii-controls button {
  position: relative; display: grid; align-content: center; justify-items: center; gap: 3px;
  min-width: 0; min-height: 56px; padding: 9px 2px; border: 0; border-radius: 2px;
  background: transparent; color: var(--muted); font: 11px/1.4 var(--mono);
}
.ascii-piece-name { white-space: nowrap; }
.ascii-piece-label { font: 11px/1.4 var(--sans); }
.public-board .ascii-controls button[aria-pressed=true] { color: var(--fg); background: var(--panel); }
.public-board .ascii-controls button[aria-pressed=true]::before,
.public-board .ascii-controls button[aria-pressed=true]::after {
  position: absolute; top: 50%; transform: translateY(-50%); color: var(--muted); font: 12px/1 var(--mono);
}
.public-board .ascii-controls button[aria-pressed=true]::before { content: '['; left: 4px; }
.public-board .ascii-controls button[aria-pressed=true]::after { content: ']'; right: 4px; }
.public-board .ascii-controls button:hover { color: var(--fg); background: var(--panel); }
.public-board .ascii-controls button:focus-visible { outline: 2px solid var(--fg); outline-offset: 2px; }
.public-board .signal-caption {
  margin: 14px 0 0; padding: 0; color: var(--muted); text-align: center;
  font: 10px/1.7 var(--mono); letter-spacing: .01em;
}
.public-board [data-success], .public-board form { grid-column: 1 / -1; }
@media (max-width: 860px) {
  .public-board { gap: 0 32px; }
  .public-board .signal-links { gap: 4px 18px; }
}
@media (max-width: 760px) {
  .public-board { grid-template-columns: minmax(0, 1fr); gap: 24px; padding: 4px 0 28px; }
  .signal-meta { margin-bottom: 12px; }
  .public-board .public-board-text { max-width: 19ch; font-size: clamp(32px, 8.5vw, 48px); }
  .public-board .signal-links { margin-top: 20px; gap: 8px 24px; }
  .public-board .signal-stage { width: min(100%, 480px); margin-inline: auto; }
  .public-board .ascii-controls { margin-top: 12px; }
}
@media (max-width: 360px) {
  .public-board .ascii-controls { gap: 2px; }
  .public-board .ascii-controls button[aria-pressed=true]::before,
  .public-board .ascii-controls button[aria-pressed=true]::after { content: none; }
}
@media print {
  .public-board .signal-stage, .public-board .signal-links, .public-board .signal-meta { display: none; }
  .public-board { display: block; }
}
"""
