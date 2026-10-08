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
    '<button type="button" data-ascii-piece="galaxy" aria-pressed="false">Galaxy / 星系</button>'
    '<button type="button" data-ascii-piece="flow" aria-pressed="false">Flow / 信号流</button>'
    '<button type="button" data-ascii-piece="aurora" aria-pressed="false">Aurora / 极光</button>'
    '<button type="button" data-ascii-piece="community" aria-pressed="true">Shared SVG / 公共作品</button>'
    '</div>'
)

CSS = """
/* The character field is a stage, never a raster image behind reading text. */
.public-board{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.15fr);column-gap:48px;padding:36px 0 54px;align-items:center}
.public-board .signal-stage{grid-column:2;grid-row:1 / span 2;min-width:0;position:relative;border-radius:16px;overflow:hidden;background:#080d12;color:#bedcdd}
.public-board .signal-stage[data-ascii-scene=community]{background:transparent;color:var(--fg)}
.public-board .signal-stage img{margin:0;aspect-ratio:64/48;object-fit:contain}
.public-board .signal-stage canvas{width:100%;margin:0;display:block}
.public-board .signal-stage canvas[hidden],.public-board .signal-stage img[hidden]{display:none}
.public-board .signal-stage[data-ascii-scene=flow]{color:#9bcdc3}
.public-board .signal-stage[data-ascii-scene=aurora]{color:#b7cba5}
.public-board .public-board-text{grid-column:1;grid-row:1;align-self:end;font-size:clamp(28px,3.5vw,46px);line-height:1.22;letter-spacing:-.03em;max-width:22ch;margin:0 0 24px;text-wrap:balance}
.public-board .signal-links{grid-column:1;grid-row:2;align-self:start;display:flex;align-items:center;gap:24px;flex-wrap:wrap;margin:0;padding-bottom:20px}
.public-board .signal-links a{min-height:44px;display:inline-flex;align-items:center;text-underline-offset:5px;font-size:14px}
.public-board .signal-links a:first-child{font-weight:600;color:var(--fg)}
.public-board .signal-links svg{width:16px;height:16px;margin-inline-start:8px;stroke:currentColor;fill:none;stroke-width:1.5}
.public-board .ascii-controls{display:flex;gap:0;border-top:1px solid #304047;flex-wrap:wrap;padding:4px 12px;background:#080d12;color:#bedcdd}
.public-board .ascii-controls[hidden]{display:none}
.public-board .ascii-controls button{flex:1 1 auto;border:0;background:transparent;color:inherit;padding:8px 10px;font-size:11px;border-radius:4px;min-height:44px;white-space:nowrap}
.public-board .ascii-controls button[aria-pressed=true]{color:#fff;text-decoration:underline;text-underline-offset:7px;text-decoration-thickness:2px}
.public-board .ascii-controls button:hover{background:#1c292f}
.public-board .ascii-controls button:focus-visible{outline:2px solid #bedcdd;outline-offset:-3px}
.public-board .signal-caption{font:10px/1.6 var(--mono);color:#9eb5bb;background:#080d12;margin:0;padding:0 22px 12px}
.public-board [data-success],.public-board form{grid-column:1 / -1}
.public-board-menu{top:0!important}
@media(max-width:760px){
 .public-board{grid-template-columns:1fr;gap:0;padding-top:26px;padding-bottom:32px}
 .public-board .public-board-text{grid-row:1;font-size:clamp(28px,6.7vw,40px);max-width:27ch;margin-bottom:18px}
 .public-board .signal-links{grid-row:2;padding-bottom:24px}
 .public-board .signal-stage{grid-column:1;grid-row:3;border-radius:12px}
 .public-board .ascii-controls{padding:4px 6px}.public-board .ascii-controls button{font-size:10.5px;padding:8px 7px}
}
@media print{.signal-stage{display:none}.public-board{display:block}.signal-links{display:none!important}}
"""
