"""Bounded universe projections through the existing read executor.

Public geometry always uses anonymous authority, even for a signed-in visitor.
Private mail is a separate response derived only from the current browser session.
No writes, persistent projections, guessed subjects, or alternative ACL checks.
"""

from __future__ import annotations

import asyncio
import re

from starlette.responses import Response

from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, wire
from msg.core.errors import Failure, require
from msg.core.post_preview import post_preview
from msg.core.requests import request_for
from msg.transports.http_common import BASE_HEADERS

PAGE_FIELDS = {
    'users': ['id', 'name', 'path', 'star'],
    'posts': ['id', 'name', 'path', 'owner', 'parent', 'created_at'],
}
HIDDEN = {
    'permission_denied',
    'authentication_required',
    'not_found',
    'resource_purged',
    'local_only',
    'credential_ceiling',
    'certificate_gate',
    'ancestor_inactive',
}


def page_arguments(kind, author=None):
    return {
        'type': 'user' if kind == 'users' else 'post',
        'limit': 100 if kind == 'users' else 24,
        'sort': 'id' if kind == 'users' else 'time',
        'direction': 'asc' if kind == 'users' else 'desc',
        'fields': PAGE_FIELDS[kind],
        **({'author': author} if author else {}),
    }


async def public_page(service, query):
    kind = query.get('kind', 'users')
    require(kind in PAGE_FIELDS, 'invalid_universe_kind')
    require(set(query) <= {'kind', 'cursor', 'author', 'ids'}, 'unknown_query_parameter')
    author = query.get('author')
    require(author is None or kind == 'posts' and 0 < len(author) <= 160, 'invalid_universe_author')
    ids = query.get('ids')
    if ids is not None:
        require(kind == 'users' and not author and 'cursor' not in query, 'invalid_universe_ids')
        require(0 < len(ids) <= 16099, 'invalid_universe_ids')
        ids = ids.split(',')
        require(
            0 < len(ids) <= 100
            and len(set(ids)) == len(ids)
            and all(re.fullmatch(r'u_[A-Za-z0-9_-]{1,158}', rid) for rid in ids),
            'invalid_universe_ids',
        )
    args = page_arguments(kind, author)
    if 'cursor' in query:
        saved, _ = service.cursors.inspect_page(query['cursor'], service.clock())
        require(
            saved.get('operation') == 'discovery.read_query'
            and canonical(saved.get('arguments')) == canonical(args),
            'cursor_query_mismatch',
        )
        args = {'cursor': query['cursor']}

    async def read(operation, arguments, *, optional=False):
        require(service.registry.operation(operation).effect == 'read', 'effect_mismatch')
        result = await service.executor.execute(
            request_for(operation, arguments, service.settings.service_url, source='manual')
        )
        if result.error:
            if optional and result.error.code in HIDDEN:
                return None
            raise Failure(result.error.code)
        return wire(result.data)

    if ids is not None:
        semaphore = asyncio.Semaphore(4)

        async def refresh(rid):
            async with semaphore:
                data = await read(
                    'discovery.get', {'id': rid, 'fields': PAGE_FIELDS['users']}, optional=True
                )
                return data

        refreshed = await asyncio.gather(*(refresh(rid) for rid in ids))
        page = {'items': [item for item in refreshed if item]}
    else:
        page = await read('discovery.read_query', args)
    anchor = None
    if kind == 'users' and ids is None and 'cursor' not in query:
        # Root is a real readable identity, not a decorative fake account, and
        # remains reachable even when it sorts beyond the first user page.
        anchor = await read(
            'discovery.get', {'id': ROOT_SUBJECT, 'fields': PAGE_FIELDS['users']}, optional=True
        )
    items = []
    references = {}

    async def reference(rid):
        if rid not in references:
            references[rid] = await read(
                'discovery.get', {'id': rid, 'fields': ['id', 'name', 'path']}, optional=True
            )
        return references[rid]

    for item in page['items']:
        if kind == 'users':
            items.append(item)
            continue
        detail = await read('discovery.get', {'id': item['id']}, optional=True)
        if detail is None:
            continue
        author = await reference(item['owner'])
        content = detail.get('content', '')
        preview = post_preview(item['name'], content[:8192] if isinstance(content, str) else '')
        reply = next(
            (r['target']['id'] for r in detail.get('relations', ()) if r['type'] == 'reply_to'),
            None,
        )
        # Never expose a private target ID merely because a public post refers to it.
        reply_to = await reference(reply) if reply else None
        items.append({
            'id': item['id'],
            'path': item['path'],
            'created_at': item['created_at'],
            'author': author,
            'reply_to': reply_to,
            **preview,
        })
    return {
        'version': 1,
        'kind': kind,
        'generated_at': wire(service.clock()),
        **({'anchor': anchor} if anchor is not None else {}),
        'author': query.get('author'),
        'items': items,
        'cursor': page.get('cursor'),
        'service': service.settings.service_url,
    }


async def universe_response(service, request, browser_account, execute_packet):
    require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
    require(
        not request.headers.get('x-msg-request') and not request.headers.get('authorization'),
        'ambiguous_credentials',
    )
    pairs = request.query_params.multi_items()
    query = dict(pairs)
    require(len(pairs) == len(query), 'duplicate_query_parameter')
    if request.url.path == '/_universe/me':
        require(not query, 'unknown_query_parameter')
        account = await browser_account()
        conversations = []
        if account:
            result = await execute_packet(
                request_for(
                    'communication.dm_list', {}, service.settings.service_url, source='manual'
                )
            )
            if result.error:
                raise Failure(result.error.code)
            require(result.subject == account['id'], 'permission_denied')
            conversations = wire(result.data)['items']
            balance_result = await execute_packet(
                request_for('money.balance', {}, service.settings.service_url, source='manual')
            )
            own_balance = {'visibility': 'unavailable'}
            if balance_result.error is None:
                from msg.plugins.star_projection import reserve_projection

                money = wire(balance_result.data)
                require(
                    balance_result.subject == account['id']
                    and money.get('subject_id') == account['id'],
                    'permission_denied',
                )
                own_balance = reserve_projection(service, money['balance_minor'], 'self')
            public_facts = await service.executor.execute(
                request_for(
                    'discovery.get',
                    {'id': account['id'], 'fields': ['star']},
                    service.settings.service_url,
                    source='manual',
                )
            )
            facts = wire(public_facts.data).get('star', {}) if public_facts.error is None else {}
            account = {
                **account,
                'star': {**facts, 'balance': own_balance, 'checked_at': wire(service.clock())},
            }
        value = {
            'version': 1,
            'account': account,
            'conversations': conversations,
            'service': service.settings.service_url,
        }
    else:
        value = await public_page(service, query)
    body = canonical(value)
    require(len(body) <= service.settings.server.limits.max_response_bytes, 'response_too_large')
    return Response(
        b'' if request.method == 'HEAD' else body,
        media_type='application/json',
        headers={
            **BASE_HEADERS,
            'Cache-Control': 'private, no-store',
            'Vary': 'Cookie',
            'Content-Length': str(len(body)),
            'X-Robots-Tag': 'noindex',
        },
    )
