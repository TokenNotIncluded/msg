"""Transparent channel headers from compact, authorized presentation metadata."""

from html import escape

CSS = """
.board-heading { position:relative; isolation:isolate; overflow:hidden; min-height:240px;
  margin:24px 0 32px; padding:36px 0; }
.board-heading img { position:absolute; inset:0; width:100%; height:100%;
  object-fit:cover; z-index:-1; opacity:.6; pointer-events:none; }
.prose .board-heading h1 { margin:0 0 16px; }
.prose .board-description { max-width:52%; color:var(--muted); font-size:14px;
  white-space:pre-line; margin:0; }
.board-administrators { margin-top:20px; font-size:12px; color:var(--muted); }
@media(max-width:640px) {
  .board-heading { min-height:260px; padding:24px 0; }
  .prose .board-description { max-width:75%; }
  .board-heading img { opacity:.24; }
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
