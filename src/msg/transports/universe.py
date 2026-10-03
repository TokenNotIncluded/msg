"""Bounded universe projections through the existing read executor.

Public geometry always uses anonymous authority, even for a signed-in visitor.
Private mail is a separate response derived only from the current browser session.
No writes, persistent projections, guessed subjects, or alternative ACL checks.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from datetime import timedelta

from starlette.responses import Response

from msg.constants import ROOT_SPACE, ROOT_SUBJECT
from msg.core.codec import canonical, wire
from msg.core.errors import Failure, require
from msg.core.planet_layout import fallback_position, planet_layout
from msg.core.post_preview import post_preview
from msg.core.requests import request_for
from msg.transports.http_common import BASE_HEADERS

PAGE_FIELDS = {
    'users': ['id', 'name', 'path', 'star', 'artwork'],
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


async def subject_position(service, subject_id):
    """An authoritative public birth point, or None for a hidden identity.

    The flight server assigns private identities a separate neutral home. This
    helper never projects hidden follow labels or derives a private UUID point.
    """
    result = await service.executor.execute(
        request_for(
            'discovery.get',
            {'id': subject_id, 'fields': ['star_topology']},
            service.settings.service_url,
            source='manual',
        )
    )
    if result.error:
        if result.error.code in HIDDEN:
            return None
        raise Failure(result.error.code)
    graph = wire(result.data)['star_topology']
    layout = planet_layout(graph['nodes'], graph['edges'])
    return layout[subject_id]['position'] if subject_id in layout else fallback_position(subject_id)


def page_arguments(kind, author=None):
    if kind == 'posts' and author:
        return {
            'scope': ROOT_SPACE,
            'type': 'post',
            'terms': 'post',
            'field': 'metadata',
            'author': author,
            'recursive': True,
            'depth': 5,
            'order': 'created',
            'limit': 24,
            'fields': [field for field in PAGE_FIELDS['posts'] if field != 'parent'],
        }
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
    require(set(query) <= {'kind', 'cursor', 'author', 'ids', 'shuffle'}, 'unknown_query_parameter')
    shuffle = query.get('shuffle')
    if shuffle is not None:
        require(
            shuffle == '1' and kind == 'users' and not {'cursor', 'author', 'ids'} & query.keys(),
            'invalid_universe_shuffle',
        )
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
    page_operation = 'discovery.lexical_search' if author else 'discovery.read_query'
    if 'cursor' in query:
        saved, _ = service.cursors.inspect_page(query['cursor'], service.clock())
        require(
            saved.get('operation') == page_operation
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

    if author:
        require(
            await read('discovery.get', {'id': author, 'fields': ['id']}, optional=True)
            is not None,
            'not_found',
        )

    batch = None
    if ids is not None:
        # The first current readable account authorizes one bounded anonymous
        # batch. Individual current reads below intersect its summaries, so a
        # newly hidden account never survives from that earlier snapshot.
        for rid in ids:
            if await read('discovery.get', {'id': rid, 'fields': ['id']}, optional=True):
                value = await read(
                    'discovery.get', {'id': rid, 'fields': ['star_batch']}, optional=True
                )
                batch = value['star_batch'] if value else None
                break
        semaphore = asyncio.Semaphore(4)

        async def refresh(rid):
            async with semaphore:
                data = await read(
                    'discovery.get',
                    {'id': rid, 'fields': ['id', 'name', 'path', 'artwork']},
                    optional=True,
                )
                return data

        refreshed = await asyncio.gather(*(refresh(rid) for rid in ids))
        page = {'items': [item for item in refreshed if item]}
        graph = (
            batch['topology']
            if batch
            else {'version': 1, 'nodes': [], 'edges': [], 'bounded': True, 'scanned': 0}
        )
        hidden = set(ids) - {item['id'] for item in page['items']}
        graph = {
            **graph,
            'nodes': [rid for rid in graph['nodes'] if rid not in hidden],
            'edges': [
                edge
                for edge in graph['edges']
                if edge['source'] not in hidden and edge['target'] not in hidden
            ],
        }
        graph['scanned'] = len(graph['nodes']) + len(graph['edges'])
        positions = planet_layout(graph['nodes'], graph['edges'])
        for item in page['items']:
            summary = batch['stars'].get(item['id']) if batch else None
            if summary is not None and item['id'] in positions:
                summary = {**summary, 'layout': positions[item['id']]}
            item['star'] = summary or {
                'role': 'root' if item['id'] == ROOT_SUBJECT else 'user',
                'certificate': {'state': 'unknown'},
                'presence': {'state': 'unknown'},
                'last_public_post_at': None,
                'post_count': {'public': None, 'exact': False, 'scanned': 0},
                'activity': {
                    'window_days': 14,
                    'window_end': batch['checked_at'] if batch else wire(service.clock()),
                    'recent_posts': None,
                    'previous_posts': None,
                    'active_days': None,
                    'previous_active_days': None,
                    'exact': False,
                },
                'layout': positions.get(item['id'])
                or {
                    'version': 4,
                    'position': fallback_position(item['id']),
                    'orbit': {
                        'kind': 'isolated',
                        'source_type': None,
                        'parent_id': None,
                        'members': [],
                    },
                    'bounded': True,
                },
                'balance': {'visibility': 'unknown'},
                'checked_at': batch['checked_at'] if batch else wire(service.clock()),
                'bounded': True,
            }
    else:
        if shuffle:
            # Random keyset seek, not ORDER BY RANDOM() or an OFFSET scan.
            # The cursor is not authority: every row still goes through the
            # existing anonymous executor, snapshot and visibility checks.
            snapshot = service.clock()
            start = 'u_' + secrets.token_hex(16)
            cursor = service.cursors.encode_page(
                'discovery.read_query',
                args,
                [start, start],
                snapshot,
                {'actor': None, 'subject': None, 'credential_id': None},
                snapshot + timedelta(minutes=15),
            )
            page = await read('discovery.read_query', {'cursor': cursor})
            # The root often sorts after hashed IDs; wrap even if only root
            # remains beyond the random seek. At most one extra bounded read.
            if not any(re.fullmatch(r'u_[0-9a-f]{32}', item['id']) for item in page['items']):
                page = await read('discovery.read_query', args)
        else:
            page = await read(page_operation, args)
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
        author_link = detail.get('links', {}).get('a')
        author = await reference(author_link['ref']['id']) if author_link else None
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
    topology = graph if ids is not None else None
    if kind == 'users' and ids is None:
        readable = anchor or next(iter(items), None)
        if readable is not None:
            graph = await read(
                'discovery.get', {'id': readable['id'], 'fields': ['star_topology']}, optional=True
            )
            if graph is not None:
                topology = graph['star_topology']
    return {
        'version': 1,
        'kind': kind,
        'generated_at': wire(service.clock()),
        **({'anchor': anchor} if anchor is not None else {}),
        **({'topology': topology} if topology is not None else {}),
        'author': query.get('author'),
        **({'bounded': True, 'post_scope_depth': 5} if query.get('author') else {}),
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
    if request.url.path == '/_universe/account':
        require(not query, 'unknown_query_parameter')
        account = await browser_account(fields=('id', 'name'))
        value = {'version': 1, 'account': account}
    elif request.url.path == '/_universe/me':
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
            counts_result = await execute_packet(
                request_for(
                    'discovery.get',
                    {'id': account['id'], 'fields': ['star_private']},
                    service.settings.service_url,
                    source='manual',
                )
            )
            private_counts = {'own': None, 'private_visible': None, 'exact': False}
            if counts_result.error is None:
                require(counts_result.subject == account['id'], 'permission_denied')
                private_counts = wire(counts_result.data)['star_private']
            account = {
                **account,
                'star': {
                    **facts,
                    'post_count': {**facts.get('post_count', {}), **private_counts},
                    'balance': own_balance,
                    'checked_at': wire(service.clock()),
                },
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
