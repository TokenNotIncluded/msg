"""Small human views of already-authorized post branches and claims."""

from urllib.parse import quote, urlencode

from msg.transports.home_page import display_time, markdown_text


def post_read_markdown(path, query, data=None, *, public_fallback=False, error=None):
    proofs = path == '/_post/proofs'
    title = 'Proof records / 证明记录' if proofs else 'Thread branches / 帖子分叉'
    lines = [
        '# ' + title,
        '',
        '[Back to post / 返回帖子](/_id/' + quote(query['id'], safe='') + ')',
        '',
    ]
    if error:
        if error == 'credential_ceiling':
            message = 'Current authorization does not include this read. / 当前授权未包含此项读取。'
        elif error in {'permission_denied', 'certificate_gate'}:
            message = 'You cannot read these records. / 你没有读取这些记录的权限。'
        elif error in {'not_found', 'revision_not_found', 'resource_purged'}:
            message = 'These records are unavailable. / 找不到可读取的记录。'
        else:
            message = 'Could not load these records. / 暂时无法加载这些记录。'
        lines.extend([message, '', 'Error / 错误：`' + markdown_text(error) + '`'])
        return '\n'.join(lines)
    if public_fallback:
        lines.extend(['Showing public records only. / 当前仅显示公开可读的记录。', ''])
    if proofs:
        lines.extend([
            'These are participant claims about a specific revision. / 这些是参与者对此版本的声明。',
            '',
        ])
        for item in data['items']:
            lines.extend(['## ' + markdown_text(item['kind']), '', display_time(item['time']), ''])
            if item.get('note'):
                lines.extend([markdown_text(item['note']), ''])
        cursor_key = 'cursor'
        empty = 'No claims for this revision yet. / 此版本还没有证明声明。'
    else:
        for item in data['items']:
            lines.extend([
                '- [' + markdown_text(item['name']) + '](/_id/' + quote(item['id'], safe='') + ')'
            ])
        cursor_key = 'after'
        empty = 'No readable branches yet. / 暂无可读的分叉。'
    if not data['items']:
        lines.append(empty)
    if data.get(cursor_key):
        next_query = {key: value for key, value in query.items() if key != cursor_key}
        next_query[cursor_key] = data[cursor_key]
        lines.extend(['', '[More / 更多](' + path + '?' + urlencode(next_query) + ')'])
    return '\n'.join(lines)
