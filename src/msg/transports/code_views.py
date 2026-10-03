"""Escaped code and diff surfaces with local highlighting and exact copying."""

import re
from base64 import b64encode
from hashlib import sha256
from html import escape

from markdown_it import MarkdownIt
from pygments.lexers import get_lexer_by_name
from pygments.token import Comment, Keyword, Literal, Name, Operator
from pygments.util import ClassNotFound

from msg.transports.json_layout import json_display

CODE_CSS = r"""
.code-block,.diff-view{max-width:100%;min-width:0;margin:24px 0;border:1px solid var(--line);border-radius:2px;background:var(--bg)}
.code-toolbar{display:flex;align-items:center;flex-wrap:wrap;gap:8px 16px;padding:8px 12px;border-bottom:1px solid var(--line);font:12px var(--mono);color:var(--muted)}
.code-toolbar button{margin-left:auto;min-height:44px;padding:4px 14px;border:1px solid var(--line);border-radius:2px;background:var(--bg);color:var(--fg);font:inherit;cursor:pointer}.code-toolbar button:hover{border-color:var(--fg)}
.code-copy-status:empty{display:none}.code-copy-status{font-size:11px}
.code-scroll,.diff-scroll{max-width:100%;overflow-x:auto;overscroll-behavior-inline:contain;outline-offset:3px}
.prose .code-lines,.prose .diff-lines{margin:0;padding:12px 0;border-radius:0;background:var(--bg);color:var(--fg);font:13px/1.65 var(--mono);direction:ltr;unicode-bidi:isolate;overflow:visible;min-width:max-content}
.code-lines>code,.diff-lines>code{display:block;min-width:max-content;background:none;padding:0;color:inherit;font:inherit}
.code-line{display:grid;grid-template-columns:4.5ch 1fr;min-width:max-content}.code-number{padding:0 1ch;text-align:right;color:var(--muted);border-right:1px solid var(--line);user-select:none}.code-text{padding:0 16px;white-space:pre;overflow-wrap:normal;tab-size:4}
.code-kw,.code-fn{color:#3050a0}.code-string{color:#895420}.code-comment{color:var(--muted);font-style:italic}.code-number-token{color:#895420}.code-op{color:var(--fg)}
.diff-line{display:grid;grid-template-columns:4.5ch 4.5ch 2ch 1fr;min-width:max-content}.diff-number{padding:0 1ch;text-align:right;color:var(--muted);border-right:1px solid var(--line);user-select:none}.diff-marker{padding-left:.5ch;user-select:none}.diff-text{padding:0 16px 0 4px;white-space:pre;overflow-wrap:normal;tab-size:4}.diff-insert{background:#2d823314;color:#236d2c}.diff-delete{background:#b4323212;color:#a52727}.diff-hunk{background:var(--panel);color:var(--muted);border-block:1px solid var(--line)}.diff-header,.diff-note{color:var(--muted)}
.diff-columns{display:grid;grid-template-columns:4.5ch 4.5ch 1fr;padding:8px 0;border-bottom:1px solid var(--line);font:11px var(--mono);color:var(--muted)}.diff-columns span{text-align:center}.diff-columns span:last-child{text-align:left;padding-left:16px}.diff-reference{font:12px var(--mono);overflow-wrap:anywhere}
:root[data-theme=dark] .code-block,:root[data-theme=dark] .diff-view{--code-kw:#a7b9ff;--code-string:#e8bd8b;--diff-add:#9ce4a5;--diff-del:#ffaaa4}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]) .code-block,:root:not([data-theme=light]) .diff-view{--code-kw:#a7b9ff;--code-string:#e8bd8b;--diff-add:#9ce4a5;--diff-del:#ffaaa4}}
.code-kw,.code-fn{color:var(--code-kw,#3050a0)}.code-string,.code-number-token{color:var(--code-string,#895420)}.diff-insert{color:var(--diff-add,#236d2c)}.diff-delete{color:var(--diff-del,#a52727)}
@media(max-width:520px){.code-toolbar{padding:6px 8px;gap:4px 8px}.code-text{padding-inline:12px}.diff-text{padding-right:12px}.code-lines,.diff-lines{font-size:12px}}
"""
CODE_SCRIPT = r"""(() => {
  for (const button of document.querySelectorAll('[data-copy-code]')) {
    button.addEventListener('click', async () => {
      const block = button.closest('.code-block,.diff-view');
      const source = block.querySelector('.code-source');
      const status = block.querySelector('.code-copy-status');
      const text = (en, zh) => globalThis.msgText?.(en, zh) ?? (document.documentElement.lang.startsWith('zh') ? zh : en);
      const original = new TextDecoder('utf-8', {ignoreBOM:true}).decode(Uint8Array.from(atob(source.dataset.codeSource), char => char.charCodeAt(0)));
      try {
        if (!navigator.clipboard?.writeText) throw new Error('clipboard_unavailable');
        await navigator.clipboard.writeText(original);
        status.textContent = text('Copied','已复制');
      } catch {
        source.value = original;
        source.hidden = false;
        source.focus(); source.select();
        if (!block.querySelector('[data-source-download]')) {
          const link = document.createElement('a');
          link.dataset.sourceDownload = '';
          link.href = URL.createObjectURL(new Blob([new TextEncoder().encode(original)], {type:'text/plain;charset=utf-8'}));
          link.download = block.classList.contains('diff-view') ? 'diff.patch' : 'source.txt';
          link.textContent = text('Download source','下载原文');
          block.querySelector('.code-toolbar').append(link);
        }
        status.textContent = text('Copy below or download the exact source','复制下方文字，或下载原文');
      }
    });
  }
})();"""
CODE_HASH = b64encode(sha256(CODE_SCRIPT.encode()).digest()).decode()
_LANGUAGE = re.compile(r'^[\w.+-]{1,40}$', re.ASCII)


def _token_class(token):
    if token in Comment:
        return 'code-comment'
    if token in Keyword:
        return 'code-kw'
    if token in Literal.String:
        return 'code-string'
    if token in Literal.Number:
        return 'code-number-token'
    if token in Name.Function or token in Name.Class:
        return 'code-fn'
    if token in Operator:
        return 'code-op'
    return ''


def _highlighted_lines(source, language):
    """Highlight tokens without lexer's newline, tab or whitespace preprocessing."""
    lexer = None
    if _LANGUAGE.fullmatch(language) and len(source) <= 200000:
        try:
            lexer = get_lexer_by_name(language)
        except ClassNotFound:
            pass
    pieces = (
        [(None, source)]
        if lexer is None
        else ((token, text) for _, token, text in lexer.get_tokens_unprocessed(source))
    )
    lines, current = [], ''
    for token, text in pieces:
        cls = _token_class(token) if token is not None else ''
        parts = text.split('\n')
        for index, part in enumerate(parts):
            if index:
                lines.append(current)
                current = ''
            encoded = escape(part)
            current += f'<span class="{cls}">{encoded}</span>' if cls and encoded else encoded
    if current or not lines or not source.endswith('\n'):
        lines.append(current)
    return lines, lexer is not None


def copy_toolbar(label, *, copy_label='Copy source / 复制原文'):
    return (
        '<div class="code-toolbar"><span>' + escape(label) + '</span>'
        '<button type="button" data-copy-code>' + escape(copy_label) + '</button>'
        '<span class="code-copy-status" role="status" aria-live="polite"></span></div>'
    )


def copy_source(source):
    return (
        '<textarea class="code-source" aria-label="Source / 原文" readonly hidden data-code-source="'
        + b64encode(source.encode()).decode()
        + '">'
        + escape(source)
        + '</textarea>'
    )


def render_code(source, language=''):
    language = language.split(maxsplit=1)[0] if language.strip() else ''
    display = json_display(source) if language.casefold() == 'json' else source
    lines, highlighted = _highlighted_lines(display, language)
    label = language if highlighted else (language + ' · plain text' if language else 'plain text')
    rendered = ''.join(
        f'<span class="code-line"><span class="code-number" aria-hidden="true">{number}</span>'
        f'<span class="code-text">{line}</span></span>'
        for number, line in enumerate(lines, 1)
    )
    return (
        '<section class="code-block">'
        + copy_toolbar(label)
        + '<div class="code-scroll" tabindex="0" role="region" aria-label="Code / 代码">'
        + '<pre class="code-lines"><code>'
        + rendered
        + '</code></pre></div>'
        + copy_source(source)
        + '</section>\n'
    )


def markdown_renderer():
    renderer = MarkdownIt('commonmark', {'html': False}).enable('table')

    def fence(tokens, index, options, env):
        token = tokens[index]
        return render_code(token.content, token.info)

    renderer.renderer.rules['fence'] = fence
    return renderer
