"""Transparent channel headers from compact, authorized presentation metadata."""

from html import escape

CSS = """
.board-heading { position:relative; isolation:isolate; overflow:hidden; min-height:200px;
  margin:24px 0 32px; padding:28px 0; border-block:1px solid var(--line); }
.board-heading img { position:absolute; inset:0 0 0 auto; width:46%; height:100%;
  object-fit:contain; z-index:-1; opacity:.6; pointer-events:none; }
.prose .board-heading h1 { max-width:55%; margin:0 0 12px; font:600 clamp(26px,3.5vw,36px)/1.25 var(--mono); letter-spacing:-.025em; }
.prose .board-description { max-width:55%; color:var(--muted); font-size:14px;
  white-space:pre-line; margin:0; }
.board-administrators { margin-top:20px; font:12px/1.8 var(--mono); color:var(--muted); }
@media(max-width:640px) {
  .board-heading { min-height:188px; padding:24px 0; }
  .prose .board-heading h1 { max-width:100%; }
  .prose .board-description { max-width:72%; }
  .board-heading img { width:40%; opacity:.24; }
}
"""


def header_html(resource):
    presentation = resource['presentation']
    url = escape(presentation['header']['url'], quote=True)
    return (
        '<header class="board-heading">'
        f'<img src="{url}" data-motion-src="" data-still-src="{url}?still=1" alt="" aria-hidden="true" decoding="async">'
        f'<h1>{escape(resource["name"])}</h1>'
        f'<p class="board-description">{escape(presentation["description"])}</p>'
        '<p class="board-administrators">Administrators / 管理员: '
        + ', '.join(
            escape(admin)
            for admin in presentation.get('administrator_names', presentation['administrators'])
        )
        + '</p></header>'
    )
