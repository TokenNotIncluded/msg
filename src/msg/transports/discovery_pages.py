"""Human-readable views over the existing authorized discovery operations."""

from urllib.parse import quote

from msg.transports.home_page import display_time, markdown_text


def feed_markdown(data, *, signed_in=False):
    lines = [
        '# Recommended posts / 推荐帖子',
        '',
        '[Topics / 浏览话题](/topics)',
        '',
        'Ranked by explicit follows, matching interest tags, recency and author diversity.'
        ' / 根据关注、兴趣标签、时间和作者多样性排序。',
        '',
    ]
    if not signed_in:
        lines.extend([
            '[Sign in / 登录](/login) to include your follows. / 登录后结合你的关注推荐。',
            '',
        ])
    if data['interests']:
        lines.extend(['Interests / 兴趣：' + markdown_text(', '.join(data['interests'])), ''])
    reasons = {
        'followed_author': 'Following / 已关注',
        'interest_match': 'Interest / 兴趣匹配',
        'recent': 'Recent / 最近发布',
    }
    for item in data['items']:
        title = item.get('title') or item.get('summary') or 'Untitled post / 未命名帖子'
        author = item.get('author_name') or 'Author / 作者'
        lines.extend([
            '## [' + markdown_text(title) + '](' + quote(item['path'], safe='/@*') + ')',
            '',
            '['
            + markdown_text(author)
            + '](/_id/'
            + quote(item['author'], safe='')
            + ') · '
            + display_time(item['created_at']),
            '',
        ])
        if item.get('summary') and item['summary'] != title:
            lines.extend([markdown_text(item['summary']), ''])
        lines.extend([
            ' · '.join(reasons.get(reason, markdown_text(reason)) for reason in item['reasons']),
            '',
        ])
    if not data['items']:
        lines.append('No readable recommendations yet. / 暂无可读的推荐帖子。')
    return '\n'.join(lines)


def topics_markdown(channels):
    lines = [
        '# Topics / 浏览话题',
        '',
        '[Recommended posts / 推荐帖子](/feed)',
        '',
        'Only topics you can read are listed. / 这里只列出你有权限浏览的话题。',
        '',
        '[Permission bits explained / 查看完整权限说明](/help/permissions)',
        '',
        '| Topic / 话题 | About / 说明 | Posts / 帖子数 | Mode / 权限 |',
        '| --- | --- | ---: | --- |',
    ]
    for item in channels:
        path = quote(item['path'], safe='/@*&')
        lines.append(
            '| ['
            + markdown_text(item['name'])
            + ']('
            + path
            + ') | '
            + markdown_text(item['about'])
            + ' | '
            + str(item['posts'])
            + ' | ['
            + markdown_text(item['mode'])
            + ']('
            + path
            + '/meta) |'
        )
    if not channels:
        lines.extend(['', 'No readable topics yet. / 暂无可读话题。'])
    return '\n'.join(lines)
