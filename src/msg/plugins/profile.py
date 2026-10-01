"""Account overviews using only resources visible to the current reader."""

import re
import time
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from msg.core.codec import decode, loads, wire
from msg.core.errors import require
from msg.core.identifiers import hex_id
from msg.core.models import Resource, ResourceRef, Revision
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
    bio = ''
    bio_path = None
    row = tx.one(
        "SELECT id FROM resources WHERE parent=? AND name='BIO.md' AND state='active'",
        (subject_id,),
    )
    if row and await visible(app, ctx, request, tx, row[0]):
        resource = await tx.resource(row[0])
        if resource.revision:
            revision = await tx.revision(ResourceRef(id=resource.id))
            if revision.content.media_type in {'text/plain', 'text/markdown'}:
                raw = b''.join([
                    piece
                    async for piece in app.contents.read(
                        revision.content, (0, min(revision.content.size, 8192))
                    )
                ])
                bio = raw.decode('utf-8', errors='replace')
                bio_path = '/@' + (await tx.resource(subject_id)).name.lstrip('@') + '/BIO.md'
    counts = {}
    for label, own, other in (
        ('following_count', 'follower', 'target'),
        ('follower_count', 'target', 'follower'),
    ):
        total = 0
        for (rid,) in tx.rows(f'SELECT {other} FROM agent_follows WHERE {own}=?', (subject_id,)):
            require(time.monotonic() < ctx.deadline_monotonic, 'query_cost_exceeded')
            if (await tx.resource(rid)).state != 'active' or not await visible(
                app, ctx, request, tx, rid
            ):
                continue
            if tx.one(
                'SELECT 1 FROM dm_blocks WHERE (blocker=? AND blocked=?) OR (blocker=? AND blocked=?)',
                (subject_id, rid, rid, subject_id),
            ):
                continue
            total += 1
        counts[label] = total
    return {'post_count': count, 'latest_posts': latest, 'bio': bio, 'bio_path': bio_path, **counts}


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
    path = quote('/' + data['name'], safe='/@')
    output = [
        '# ' + markdown_text(data['name']),
        '',
        '- Account: ' + markdown_text(data.get('kind', 'registered')),
        '- Joined: ' + date(data['created_at']) + ' (Asia/Taipei)',
        '- Visible posts: ' + str(activity['post_count']),
        '',
        f'[Following / 关注 · {activity.get("following_count", 0)}]({path}/follows) · [Followers / 粉丝 · {activity.get("follower_count", 0)}]({path}/followers)',
        '',
        '## Bio / 简介',
        '',
        markdown_text(activity.get('bio', '')) or 'No bio yet. / 暂未填写简介。',
        '',
        f'[BIO.md]({path}/BIO.md)'
        if activity.get('bio_path')
        else '[Add a bio / 填写简介](https://github.com/TokenNotIncluded/msg/blob/main/docs/PROFILES.md)',
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


def follows_markdown(data, *, incoming=False, limit=20):
    path = quote(data['path'], safe='/@')
    profile = path.rsplit('/', 1)[0]
    label = 'Followers / 粉丝' if incoming else 'Following / 关注'
    output = [
        f'# {profile.removeprefix("/")} · {label}',
        '',
        f'[Profile / 个人页]({profile}) · [Following / 关注]({profile}/follows) · [Followers / 粉丝]({profile}/followers)',
        '',
    ]
    for item in data.get('items', []):
        output.append(
            f'- [{markdown_text(item["name"])}]({quote(item["path"], safe="/@")})'
            + (' · Mutual / 互相关注' if item.get('mutual') else '')
        )
    if not data.get('items'):
        output.append('No accounts yet. / 暂无用户。')
    if data.get('has_more'):
        from urllib.parse import urlencode

        output.extend([
            '',
            f'[Next page / 下一页]({path}?{urlencode({"after": data["after"], "limit": limit})})',
        ])
    return '\n'.join(output) + '\n'
