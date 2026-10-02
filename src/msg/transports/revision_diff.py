"""Readable authorized revision differences, including a real first revision."""

from html import escape

from msg.core.identifiers import hex_id
from msg.core.revision_diff import diff_rows
from msg.transports.code_views import copy_source, copy_toolbar


def _diff_markup(value):
    # The structured form keeps exact counters even when a page begins mid-hunk.
    # Legacy values without it can still render the available hunk context.
    rows = value.get('diff_lines')
    if rows is None:
        rows = list(diff_rows(value['diff'].splitlines(keepends=True)))
    rendered = []
    for row in rows:
        kind = (
            row['kind']
            if row['kind'] in {'header', 'hunk', 'context', 'insert', 'delete', 'note'}
            else 'note'
        )
        text = row['text']
        marker = {'context': ' ', 'insert': '+', 'delete': '-'}.get(kind, '')
        if marker:
            text = text[1:]
        text = text.removesuffix('\n').removesuffix('\r')
        numbers = ''.join(
            '<span class="diff-number" aria-label="'
            + label
            + '">'
            + escape(str(row.get(key) or ''))
            + '</span>'
            for key, label in [('old_line', 'Old line / 旧行'), ('new_line', 'New line / 新行')]
        )
        rendered.append(
            f'<span class="diff-line diff-{kind}">{numbers}'
            f'<span class="diff-marker" aria-hidden="true">{marker}</span>'
            f'<span class="diff-text">{escape(text)}</span></span>'
        )
    return (
        '<section class="diff-view">'
        + copy_toolbar('Unified diff / 统一差异', copy_label='Copy diff / 复制差异')
        + '<div class="diff-columns" aria-hidden="true"><span>old</span><span>new</span><span>− removed · + added / 删除 · 新增</span></div>'
        + '<div class="diff-scroll" tabindex="0" role="region" aria-label="Version diff / 版本差异">'
        + '<pre class="diff-lines"><code>'
        + ''.join(rendered)
        + '</code></pre></div>'
        + copy_source(value['diff'])
        + '</section>'
    )


def revision_diff_content(value):
    base = '/*' + hex_id(value['to']['id'])
    content = (
        f'<p><a href="{base}">Read current version / 查看当前版本</a> · '
        f'<a href="{base}/history">History / 编辑历史</a></p>'
        '<h1>Version diff / 版本差异</h1>'
    )
    if value.get('from'):
        content += (
            '<p class="diff-reference">'
            + escape(hex_id(value['from']['revision']))
            + ' → '
            + escape(hex_id(value['to']['revision']))
            + '</p>'
        )
    if value.get('reason') == 'no_previous_revision':
        content += (
            '<p role="status">This is the first version. There is no previous version to compare. '
            '/ 这是第一版，还没有可比较的上一版本。</p>'
        )
    elif not value['diff']:
        content += '<p>No text changes / 正文没有变化</p>'
    else:
        content += _diff_markup(value)
    if value.get('summary'):
        content += '<p>Summary / 摘要：' + escape(str(value['summary'])) + '</p>'
    if value.get('next'):
        content += f'<p><a href="{escape(value["next"], quote=True)}">Continue / 继续查看</a></p>'
    return content
