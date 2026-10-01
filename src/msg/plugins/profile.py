"""Account overviews using only resources visible to the current reader."""

import re
import time
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from msg.core.codec import decode, loads, wire
from msg.core.errors import require
from msg.core.identifiers import hex_id
from msg.core.models import Resource, Revision
from msg.core.post_preview import post_preview


async def account_activity(app, ctx, request, tx, subject_id):
    from msg.plugins.discovery import visible

    count = 0
    latest = []
    position = None
    while True:
        require(time.monotonic() < ctx.deadline_monotonic, 'query_cost_exceeded')
        seek = ' AND (r.created_at,r.id)<(?,?)' if position is not None else ''
        rows = tx.rows(
            'SELECT r.body,r.created_at,r.id,v.body FROM resources r '
            'JOIN revisions v ON v.id=r.revision '
            "WHERE r.type='post' AND r.state='active' AND r.created_at<=?"
            + seek
            + ' ORDER BY r.created_at DESC,r.id DESC LIMIT 128',
            (wire(ctx.now), *(position or ())),
        )
        for raw, created_at, rid, revision_raw in rows:
            require(time.monotonic() < ctx.deadline_monotonic, 'query_cost_exceeded')
            position = (created_at, rid)
            resource = decode(Resource, loads(raw))
            revision = decode(Revision, loads(revision_raw))
            if revision.author != subject_id:
                continue
            if not await visible(app, ctx, request, tx, rid):
                continue
            count += 1
            if len(latest) == 5:
                continue
            body = ''
            if revision.content.media_type.startswith('text/'):
                raw = b''.join([
                    piece
                    async for piece in app.contents.read(
                        revision.content, (0, min(revision.content.size, 8192))
                    )
                ])
                body = raw.decode('utf-8', errors='ignore')
            latest.append({
                **post_preview(resource.name, body),
                'path': '/*' + hex_id(rid),
                'created_at': wire(resource.created_at),
            })
        if len(rows) < 128:
            break
    return {'post_count': count, 'latest_posts': latest}


def markdown_text(value):
    return re.sub(r'([\\`*_{}\[\]<>!|&])', r'\\\1', ' '.join(str(value).split()))


def profile_markdown(data):
    timezone = ZoneInfo('Asia/Taipei')

    def date(value):
        return (
            datetime
            .fromisoformat(value.replace('Z', '+00:00'))
            .astimezone(timezone)
            .strftime('%Y-%m-%d %H:%M')
        )

    activity = data['profile']
    output = [
        '# ' + markdown_text(data['name']),
        '',
        '- Account: ' + markdown_text(data.get('kind', 'registered')),
        '- Joined: ' + date(data['created_at']) + ' (Asia/Taipei)',
        '- Visible posts: ' + str(activity['post_count']),
        '',
        '## Latest posts',
        '',
    ]
    for item in activity['latest_posts']:
        output.append(
            f'- [{markdown_text(item["title"])}]({quote(item["path"], safe="/@*")}) — {date(item["created_at"])}'
        )
        if item['excerpt']:
            output.extend(['', '  ' + markdown_text(item['excerpt']), ''])
    if not activity['latest_posts']:
        output.append('No visible posts yet.')
    output += ['', '## Resources', '']
    for item in data.get('items', []):
        output.append(f'- [{markdown_text(item["name"])}]({quote(item["path"], safe="/@*")})')
    if not data.get('items'):
        output.append('No visible resources.')
    return '\n'.join(output) + '\n'
