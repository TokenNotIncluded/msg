"""浏览器讨论投影是额外的只读请求，不改动既有帖子和首页协议。"""

import asyncio
from urllib.parse import urlencode

from msg.core.codec import wire
from msg.core.errors import Failure
from msg.core.identifiers import hex_id
from msg.core.requests import request_for
from msg.transports.post_read_pages import thread_fragment_html, thread_preview
from msg.transports.thread_panel import discussion_note, discussion_panel_html, reply_label


async def branch_items(execute, page, service_url):
    """只读当前展开层；长正文使用既有固定版本分段读取。"""

    async def item(ref):
        meta = await execute(
            request_for(
                'discovery.get',
                {
                    **ref,
                    'fields': [
                        'id',
                        'revision',
                        'name',
                        'path',
                        'type',
                        'state',
                        'created_at',
                        'links',
                        'relations',
                        'view_count',
                    ],
                },
                service_url,
                source='manual',
            )
        )
        if meta.error:
            if meta.error.code in {
                'permission_denied',
                'not_found',
                'ancestor_inactive',
                'resource_purged',
            }:
                return None
            raise Failure(meta.error.code)
        value = wire(meta.data)
        if value['state'] != 'active':
            return None
        pieces, remaining, more = [], 8192, False
        arguments = {**ref, 'max_bytes': remaining}
        # 分段接口按 Markdown 块停下；字节与段数都设上限，避免短标题挤掉正文。
        for _ in range(8):
            body = await execute(
                request_for('discovery.read_segment', arguments, service_url, source='manual')
            )
            if body.error:
                if body.error.code in {
                    'permission_denied',
                    'not_found',
                    'ancestor_inactive',
                    'resource_purged',
                    'revision_not_found',
                }:
                    return None
                raise Failure(body.error.code)
            segment = wire(body.data)
            encoded = segment['text'].encode('utf-8')
            pieces.append(encoded[:remaining].decode('utf-8', errors='ignore'))
            more = bool(segment.get('next')) or len(encoded) > remaining
            remaining -= min(len(encoded), remaining)
            if not more or not remaining:
                break
            next_path = segment.get('next')
            if not isinstance(next_path, str) or not next_path.startswith('/_r/c/'):
                raise Failure('invalid_cursor')
            arguments = {'cursor': next_path.removeprefix('/_r/c/')}
        return {**value, 'content': ''.join(pieces), '_body_more': more}

    values = await asyncio.gather(*(item(ref) for ref in page['items']))
    return [value for value in values if value is not None]


def root_index_arguments(*, parent=None, limit=20):
    return {
        'type': 'post',
        'post_kind': 'roots',
        'sort': 'time',
        'direction': 'desc',
        'limit': limit,
        'fields': ['id', 'revision', 'name', 'type', 'path', 'created_at', 'view_count'],
        **({'parent': parent} if parent else {}),
    }


async def root_post_index(
    execute, service_url, *, parent=None, cursor=None, limit=20, public_execute=None
):
    arguments = {'cursor': cursor} if cursor else root_index_arguments(parent=parent, limit=limit)
    packet = request_for(
        'discovery.read_query', arguments, service_url, source='manual', contract_version=5
    )
    result = await execute(packet)
    public_only = False
    reader = execute
    if result.error and result.error.code == 'credential_ceiling' and public_execute:
        result = await public_execute(packet)
        public_only = True
        reader = public_execute
    if result.error:
        return {'error': result.error.code, 'items': []}
    page = wire(result.data)
    semaphore = asyncio.Semaphore(4)

    async def titled(item):
        async with semaphore:
            response = await reader(
                request_for(
                    'discovery.read_segment',
                    {'id': item['id'], 'revision': item['revision'], 'max_bytes': 1024},
                    service_url,
                    source='manual',
                )
            )
        preview = {'title': item['name'], 'excerpt': ''}
        if response.error and response.error.code in {
            'permission_denied',
            'not_found',
            'ancestor_inactive',
            'resource_purged',
            'revision_not_found',
        }:
            return None
        if not response.error:
            preview = thread_preview({**item, 'content': wire(response.data)['text']})
            if preview['title'] == 'Untitled post':
                preview['title'] = '未命名主帖'
        return {**item, 'path': '/*' + hex_id(item['id']), **preview}

    return {
        **page,
        'items': [
            item
            for item in await asyncio.gather(*(titled(item) for item in page['items']))
            if item is not None
        ],
        'public_only': public_only,
    }


async def reply_statuses(execute, ids, service_url, *, public_execute=None):
    if not ids:
        return {}
    result = await execute(
        request_for('discussion.reply_status', {'ids': ids}, service_url, source='manual')
    )
    public_only = False
    if result.error and result.error.code == 'credential_ceiling' and public_execute:
        result = await public_execute(
            request_for('discussion.reply_status', {'ids': ids}, service_url, source='manual')
        )
        public_only = True
    if result.error:
        return {}
    return {
        item['id']: {**item, **({'public_only': True} if public_only else {})}
        for item in wire(result.data)['items']
    }


def fragment_html(data, *, focus_id, query, status=None, statuses=None):
    data = dict(data)
    if data.get('cursor'):
        data['next'] = '/_post/thread-fragment?' + urlencode({**query, 'cursor': data['cursor']})
    else:
        data.pop('next', None)
    markup = thread_fragment_html(
        data['items'], data, focus_id=focus_id, embedded=True, reply_statuses=statuses
    )
    from html import escape

    return markup.replace(
        'data-thread-fragment',
        'data-thread-fragment data-reply-label="'
        + escape(reply_label(status), quote=True)
        + '" data-thread-note="'
        + escape(discussion_note(status), quote=True)
        + '"',
        1,
    )


async def post_discussion(execute, resource, service_url, *, public_execute=None):
    statuses = await reply_statuses(
        execute, [resource['id']], service_url, public_execute=public_execute
    )
    status = statuses.get(resource['id'])
    return discussion_panel_html(resource, status), status
