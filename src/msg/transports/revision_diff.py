"""Readable authorized revision differences, including a real first revision."""

from html import escape

from msg.core.identifiers import hex_id


def revision_diff_content(value):
    base = '/*' + hex_id(value['to']['id'])
    content = (
        f'<p><a href="{base}">Read current version / 查看当前版本</a> · '
        f'<a href="{base}/history">History / 编辑历史</a></p>'
        '<h1>Version diff / 版本差异</h1>'
    )
    if value.get('reason') == 'no_previous_revision':
        content += (
            '<p role="status">This is the first version. There is no previous version to compare. '
            '/ 这是第一版，还没有可比较的上一版本。</p>'
        )
    elif not value['diff']:
        content += '<p>No text changes / 正文没有变化</p>'
    else:
        content += '<pre style="overflow:auto">'
        for line in value['diff'].splitlines(keepends=True):
            color = (
                '#22863a'
                if line.startswith('+')
                else '#cb2431'
                if line.startswith('-')
                else 'inherit'
            )
            content += f'<span style="color:{color}">{escape(line)}</span>'
        content += '</pre>'
    if value.get('summary'):
        content += '<p>Summary / 摘要：' + escape(str(value['summary'])) + '</p>'
    if value.get('next'):
        content += f'<p><a href="{escape(value["next"], quote=True)}">Continue / 继续查看</a></p>'
    return content
