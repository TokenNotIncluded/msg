"""按需展开正文的讨论上下文；历史补页与提交后增量互不混淆。"""

from __future__ import annotations

import hmac
import os
import time
from datetime import timedelta

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from msg.core.codec import b64, canonical, decode, digest, loads, parse_time, unb64, wire
from msg.core.errors import Failure, require
from msg.core.models import HandlerOutput, Resource, ResourceRef
from msg.plugins.common import check_access, resolve
from msg.plugins.discovery import visible
from msg.plugins.schemas import IDENTIFIER, STRING, obj

DEFAULT_LIMIT = 8
DEFAULT_PREVIEW_CHARS = 160
DEFAULT_MAX_BYTES = 16384
MAX_SCAN = 512
MAX_ANCESTORS = 256
CURSOR_KIND = 'discussion-context-v1'
POST_EVENTS = frozenset({
    'content.post_create',
    'content.post_write',
    'content.post_edit',
    'content.post_edit_metadata',
    'discussion.reply',
    'discussion.fork',
    'discussion.quote',
    'discussion.repost',
})


def _cursor_key(app):
    return hmac.digest(app.cursors.key, b'msg-discussion-context-v1', 'sha256')


def _encode(app, binding, position):
    # 扫描位置可能跨过不可见资源；签名而不加密会暴露其 ID 或事件序号。
    nonce = os.urandom(12)
    sealed = AESGCM(_cursor_key(app)).encrypt(nonce, canonical(position), binding.encode())
    return app.cursors.encode(CURSOR_KIND, binding, b64(nonce + sealed))


def _decode(app, binding, token, phase, now, epoch, floor):
    encoded = app.cursors.decode(token, CURSOR_KIND, binding)
    require(isinstance(encoded, str), 'invalid_cursor')
    payload = unb64(encoded, limit=4096)
    require(len(payload) >= 28, 'invalid_cursor')
    try:
        saved = loads(
            AESGCM(_cursor_key(app)).decrypt(payload[:12], payload[12:], binding.encode())
        )
    except InvalidTag as exc:
        raise Failure('invalid_cursor') from exc
    require(isinstance(saved, dict) and saved.get('phase') == phase, 'cursor_kind_mismatch')
    require(now < parse_time(saved['expires_at']), 'cursor_expired')
    require(saved.get('epoch') == epoch, 'resync_required')
    require(type(saved.get('fence')) is int and saved['fence'] >= floor, 'resync_required')
    if phase == 'updates':
        require(
            type(saved.get('seq')) is int
            and saved['seq'] >= floor
            and type(saved.get('offset')) is int
            and saved['offset'] >= -1,
            'resync_required',
        )
    else:
        last = saved.get('last')
        require(
            isinstance(last, list)
            and len(last) == 2
            and all(isinstance(value, str) for value in last),
            'invalid_cursor',
        )
    return saved


async def _readable(app, ctx, request, tx, rid):
    try:
        resource = await tx.resource(rid)
        return resource.state != 'purged' and await visible(app, ctx, request, tx, resource.id)
    except Failure as exc:
        if exc.code in {'not_found', 'resource_purged', 'ancestor_inactive'}:
            return False
        raise


async def _in_scope(tx, rid, focus, root, scope):
    resource = await tx.resource(rid)
    if resource.type != 'post' or resource.state != 'active':
        return False
    revision = await tx.revision(ResourceRef(id=rid))
    thread = next((r.target.id for r in revision.relations if r.type == 'thread_root'), rid)
    if thread != root:
        return False
    if scope == 'thread' or rid == focus:
        return True
    seen = {rid}
    for _ in range(MAX_ANCESTORS):
        parents = [r.target.id for r in revision.relations if r.type == 'reply_to']
        if len(parents) != 1 or parents[0] in seen:
            return False
        rid = parents[0]
        if rid == focus:
            return True
        if rid == root:
            return False
        seen.add(rid)
        try:
            parent = await tx.resource(rid)
            if parent.state == 'purged':
                return False
            revision = await tx.revision(ResourceRef(id=rid))
        except Failure as exc:
            if exc.code in {'not_found', 'resource_purged', 'revision_not_found'}:
                return False
            raise
    raise Failure('query_cost_exceeded')


async def _node(app, ctx, request, tx, rid, chars):
    # READ COMMITTED 的多次查询可能跨过编辑提交；前缀与版本必须来自同一语句。
    row = tx.one(
        'SELECT r.revision,r.body,substr(p.text,1,?) FROM resources r '
        'LEFT JOIN projections p ON p.resource_id=r.id WHERE r.id=?',
        (chars + 1, rid),
    )
    require(row is not None, 'resync_required')
    resource = decode(Resource, loads(row[1]))
    require(resource.state == 'active', 'resync_required')
    revision = await tx.revision(ResourceRef(id=rid, revision=row[0]))
    parent = None
    parents = [r.target for r in revision.relations if r.type == 'reply_to']
    if len(parents) == 1 and await _readable(app, ctx, request, tx, parents[0].id):
        try:
            await tx.revision(parents[0])
            parent = wire(parents[0])
        except Failure as exc:
            if exc.code != 'revision_not_found':
                raise
    author = revision.author if await _readable(app, ctx, request, tx, revision.author) else None
    if revision.summary:
        preview = revision.summary
        truncated = len(preview) > chars
    else:
        preview = row[2] or ''
        truncated = len(preview) > chars or (row[2] is None and revision.content.size > 0)
    return {
        'reference': wire(ResourceRef(id=resource.id, revision=revision.id)),
        'parent_reference': parent,
        'author_id': author,
        'preview': preview[:chars],
        'truncated': truncated,
    }


def _pack(nodes, base, cursor, after, more):
    refs = []
    indices = {}
    authors = []
    author_indices = {}

    def reference(ref):
        if ref is None:
            return None
        key = (ref['id'], ref.get('revision'))
        if key not in indices:
            indices[key] = len(refs)
            refs.append(ref)
        return indices[key]

    parents = {
        node['reference']['id']: node['parent_reference']['id']
        for node in nodes
        if node['parent_reference'] is not None
    }
    invalid = set()
    completed = set()
    for start in parents:
        trail = []
        positions = {}
        current = start
        while current in parents and current not in completed:
            if current in positions:
                invalid.update(trail[positions[current] :])
                break
            positions[current] = len(trail)
            trail.append(current)
            current = parents[current]
        completed.update(trail)
    root, focus = reference(base['root']), reference(base['focus'])
    items = []
    for node in nodes:
        author = node['author_id']
        if author is not None and author not in author_indices:
            author_indices[author] = len(authors)
            authors.append(author)
        items.append({
            'ref': reference(node['reference']),
            'parent': reference(
                None if node['reference']['id'] in invalid else node['parent_reference']
            ),
            'author': author_indices.get(author),
            'preview': node['preview'],
            'truncated': node['truncated'],
        })
    return {
        'root': root,
        'focus': focus,
        'refs': refs,
        'authors': authors,
        'nodes': items,
        'cursor': cursor,
        'after': after,
        'has_more': more,
    }


def _fits(data, max_bytes):
    return len(canonical(data)) <= max_bytes


def _binding(principal, root, focus, scope, limit, chars, max_bytes):
    return digest({
        'root': root,
        'focus': focus,
        'scope': scope,
        'limit': limit,
        'preview_chars': chars,
        'max_bytes': max_bytes,
        'actor': principal.actor,
        'subject': principal.subject,
        'credential': principal.credential_id,
        'method': principal.method,
        'certificates': sorted(principal.certificates),
        'ceiling': digest(principal.ceiling),
    })


def install(app, op):
    @op(
        'discussion.context',
        {
            'description': (
                '默认只给最新8个节点的160字符预览，正文按 refs 中固定版本单独读取。'
                'nodes.ref/parent 为 refs 索引，author 为 authors 索引；未授权引用省略。'
                'truncated 只描述预览源，false 不代表完整任务正文；任务必须按固定引用展开。'
                'cursor 实时补较旧历史，after 只取初始围栏后的新增/编辑，可重叠并按 id/revision 合并。'
                'has_more=null 表示源扫描预算耗尽，继续返回游标，不能视为已追平。'
                '权限或生命周期变化报 resync_required，丢弃旧视图后重新读取；读取不产生 ACK。'
                '本操作使用固定紧凑格式，不接受通用 return_fields。'
            ),
            **obj(
                {
                    'id': IDENTIFIER,
                    'scope': {'enum': ['thread', 'branch']},
                    'cursor': STRING,
                    'after': STRING,
                    'limit': {
                        'type': 'integer',
                        'minimum': 1,
                        'maximum': 50,
                        'default': DEFAULT_LIMIT,
                    },
                    'preview_chars': {
                        'type': 'integer',
                        'minimum': 0,
                        'maximum': 280,
                        'default': DEFAULT_PREVIEW_CHARS,
                    },
                    'max_bytes': {
                        'type': 'integer',
                        'minimum': 2048,
                        'maximum': 65536,
                        'default': DEFAULT_MAX_BYTES,
                        'description': 'data 的规范 JSON 字节预算；不包含外层操作结果。',
                    },
                },
                ('id',),
            ),
            'not': {'required': ['cursor', 'after']},
        },
        effect='read',
    )
    async def context(ctx, request, tx):
        # 通用展开在 handler 之后追加 data，不能绕过本操作的响应字节预算。
        require(not request.return_fields, 'context_projection_unsupported')
        args = request.arguments
        rid = await resolve(tx, args['id'])
        await check_access(app, ctx, request, tx, rid, 'read')
        resource = await tx.resource(rid)
        require(resource.type == 'post' and resource.state == 'active', 'not_a_post')
        revision = await tx.revision(ResourceRef(id=rid))
        root = next((r.target.id for r in revision.relations if r.type == 'thread_root'), rid)
        scope = args.get('scope', 'thread')
        limit = args.get('limit', DEFAULT_LIMIT)
        chars = args.get('preview_chars', DEFAULT_PREVIEW_CHARS)
        max_bytes = args.get('max_bytes', DEFAULT_MAX_BYTES)
        binding = _binding(ctx.principal, root, rid, scope, limit, chars, max_bytes)
        epoch = tx.setting('authorization_epoch', 0)
        floor = tx.setting('sync_floor', 0)
        tail = max(floor, tx.one('SELECT COALESCE(MAX(seq),0) FROM events')[0])
        phase = 'updates' if args.get('after') else 'history'
        token = args.get('after') or args.get('cursor')
        state = (
            _decode(app, binding, token, phase, ctx.now, epoch, floor)
            if token
            else {
                'phase': phase,
                'epoch': epoch,
                'fence': tail,
                'expires_at': wire(ctx.now + timedelta(minutes=15)),
                'last': [wire(ctx.now), '\uffff'],
            }
        )
        base = {'root': None, 'focus': wire(ResourceRef(id=rid, revision=revision.id))}
        if await _readable(app, ctx, request, tx, root):
            root_resource = await tx.resource(root)
            base['root'] = wire(ResourceRef(id=root, revision=root_resource.revision))
        if phase == 'updates':
            return await _updates(
                app,
                ctx,
                request,
                tx,
                rid,
                root,
                scope,
                limit,
                chars,
                max_bytes,
                binding,
                state,
                base,
                tail,
            )
        return await _history(
            app,
            ctx,
            request,
            tx,
            rid,
            root,
            scope,
            limit,
            chars,
            max_bytes,
            binding,
            state,
            base,
        )


async def _history(
    app,
    ctx,
    request,
    tx,
    rid,
    root,
    scope,
    limit,
    chars,
    max_bytes,
    binding,
    state,
    base,
):
    nodes = []
    last = state['last']
    after_state = {key: value for key, value in state.items() if key not in {'last', 'phase'}} | {
        'phase': 'updates',
        'seq': state['fence'],
        'offset': -1,
    }
    after = _encode(app, binding, after_state)
    cursor = None
    more = False
    rows = tx.rows(
        "SELECT r.id,r.created_at FROM resources r WHERE r.type='post' AND r.state='active' "
        'AND (r.id=? OR EXISTS(SELECT 1 FROM relations rel WHERE rel.source_id=r.id '
        "AND rel.revision_id=r.revision AND rel.type='thread_root' AND rel.target_id=?)) "
        'AND (r.created_at,r.id)<(?,?) ORDER BY r.created_at DESC,r.id DESC LIMIT ?',
        (root, root, *last, MAX_SCAN + 1),
    )
    for position, (candidate, created_at) in enumerate(rows):
        require(time.monotonic() < ctx.deadline_monotonic, 'query_cost_exceeded')
        if position == MAX_SCAN:
            # 未检查的源行不证明有可见后续；null 要求继续，不能声称已追平。
            more = None
            break
        if not await _readable(app, ctx, request, tx, candidate) or not await _in_scope(
            tx, candidate, rid, root, scope
        ):
            last = [created_at, candidate]
            continue
        node = await _node(app, ctx, request, tx, candidate, chars)
        next_state = {**state, 'last': [created_at, candidate]}
        trial_cursor = _encode(app, binding, next_state)
        trial = _pack([*nodes, node], base, trial_cursor, after, True)
        if len(nodes) == limit or not _fits(trial, max_bytes):
            require(bool(nodes), 'response_budget_exceeded')
            more = True
            break
        nodes.append(node)
        last = [created_at, candidate]
    if more is not False:
        cursor = _encode(app, binding, {**state, 'last': last})
    # 旧页为当前授权下的实时历史；after 始终保留初次读取的提交围栏。
    data = _pack(list(reversed(nodes)), base, cursor, after, more)
    require(tx.setting('authorization_epoch', 0) == state['epoch'], 'resync_required')
    require(_fits(data, max_bytes), 'response_budget_exceeded')
    return HandlerOutput(data=data)


async def _updates(
    app,
    ctx,
    request,
    tx,
    rid,
    root,
    scope,
    limit,
    chars,
    max_bytes,
    binding,
    state,
    base,
    tail,
):
    nodes = []
    seen = set()
    seq, offset = state['seq'], state['offset']
    rows = tx.rows(
        'SELECT seq,body FROM events WHERE seq>=? AND seq<=? ORDER BY seq LIMIT ?',
        (seq if offset >= 0 else seq + 1, tail, MAX_SCAN + 1),
    )
    more = False
    for index, (event_seq, raw) in enumerate(rows):
        require(time.monotonic() < ctx.deadline_monotonic, 'query_cost_exceeded')
        if index == MAX_SCAN:
            more = None
            break
        event = loads(raw)
        refs = event['resources'] if event['type'] in POST_EVENTS else []
        start = offset if event_seq == seq and offset >= 0 else 0
        require(start <= len(refs), 'invalid_cursor')
        for reference_index, ref in enumerate(refs[start:], start):
            require(time.monotonic() < ctx.deadline_monotonic, 'query_cost_exceeded')
            candidate = ref['id']
            if candidate in seen or not await _readable(app, ctx, request, tx, candidate):
                continue
            if not await _in_scope(tx, candidate, rid, root, scope):
                continue
            node = await _node(app, ctx, request, tx, candidate, chars)
            next_state = {**state, 'seq': event_seq, 'offset': reference_index + 1}
            trial_after = _encode(app, binding, next_state)
            trial = _pack([*nodes, node], base, None, trial_after, True)
            if len(nodes) == limit or not _fits(trial, max_bytes):
                require(bool(nodes), 'response_budget_exceeded')
                seq, offset, more = event_seq, reference_index, True
                break
            seen.add(candidate)
            nodes.append(node)
        if more:
            break
        seq, offset = event_seq, -1
    if more is False:
        seq, offset = tail, -1
    after = _encode(app, binding, {**state, 'seq': seq, 'offset': offset})
    data = _pack(nodes, base, None, after, more)
    require(tx.setting('authorization_epoch', 0) == state['epoch'], 'resync_required')
    require(_fits(data, max_bytes), 'response_budget_exceeded')
    return HandlerOutput(data=data)
