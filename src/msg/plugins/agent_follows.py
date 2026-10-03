"""Effective public account follows, separate from private resource watches."""

import time
from dataclasses import replace

from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, loads, wire
from msg.core.errors import Failure, require
from msg.core.models import HandlerOutput, Principal
from msg.core.read_query import MAX_READ_SCANNED, ReadBudget
from msg.plugins.common import check_access, operation_id, resolve
from msg.plugins.discovery import short_subject_path, visible
from msg.plugins.schemas import IDENTIFIER, obj

LIMIT = {'type': 'integer', 'minimum': 1, 'maximum': 100}
MAX_FOLLOWS = 1000
MAX_ROOT_HISTORY_RESULTS = 256
MAX_ROOT_HISTORY_BYTES = 262144
MAX_ROOT_HISTORY_RESULT_BYTES = 32768
ROOT_HISTORY_CHUNK = 8
MAX_TOPOLOGY_USERS = 256
MAX_TOPOLOGY_EDGES = 2048


def blocked(tx, first, second):
    return bool(
        first
        and second
        and tx.one(
            'SELECT 1 FROM dm_blocks WHERE (blocker=? AND blocked=?) OR (blocker=? AND blocked=?)',
            (first, second, second, first),
        )
    )


class _FollowPolicy:
    """One projection's record lookups; never an ACL or authorization source."""

    def __init__(self, tx, budget=None):
        self.tx = tx
        self.budget = budget
        self.defaults = {}
        self.incomplete = False

    def scan(self):
        return self.budget is None or self.budget.scan() is not False

    def root_default(self, follower):
        if follower in self.defaults:
            return self.defaults[follower]
        self.defaults[follower] = False
        if (
            follower == ROOT_SUBJECT
            or self.tx.setting('root_follow_optout:' + follower) is True
            or not self.tx.one(
                "SELECT 1 FROM resources WHERE id=? AND type='user' AND state='active'",
                (follower,),
            )
            or not self.tx.one(
                "SELECT 1 FROM resources WHERE id=? AND type='user' AND state='active'",
                (ROOT_SUBJECT,),
            )
        ):
            return False
        # request_id has no time semantics. Any retained successful unfollow is
        # an opt-out unless a current explicit follow row supersedes it. The
        # Indexed subject history is narrowed to canonical successful Root
        # unfollow candidates before the fixed row/byte budget. Unrelated
        # signed actions never suppress a default. SQL is only a prefilter:
        # every candidate still needs the exact saved-result checks below.
        size, seen, after = 0, 0, None
        while True:
            remaining = (
                MAX_READ_SCANNED - self.budget.scanned
                if self.budget is not None
                else MAX_ROOT_HISTORY_RESULTS + 1
            )
            limit = min(ROOT_HISTORY_CHUNK, MAX_ROOT_HISTORY_RESULTS + 1 - seen, remaining)
            remaining_bytes = MAX_ROOT_HISTORY_BYTES - size
            if limit <= 0 or remaining_bytes <= 0:
                self.incomplete = True
                return False
            seek = ' AND request_id>?' if after is not None else ''
            rows = self.tx.rows(
                'SELECT request_id,CASE WHEN LENGTH(body)<=? THEN body ELSE NULL END FROM results '
                'WHERE subject=?' + seek + ' AND (body LIKE ? OR body LIKE ?) AND body LIKE ? '
                "AND body LIKE ? ESCAPE '!' AND body LIKE ? ORDER BY request_id LIMIT ?",
                (
                    min(MAX_ROOT_HISTORY_RESULT_BYTES, remaining_bytes),
                    follower,
                    *((after,) if after is not None else ()),
                    '%"operation":"communication.unfollow"%',
                    '%"operation":"communication.unfollow@1"%',
                    '%"status":"ok"%',
                    '%"id":"' + ROOT_SUBJECT.replace('_', '!_') + '"%',
                    '%"following":false%',
                    limit,
                ),
            )
            # Charge every materialized row, including unparsed rows after an
            # early definitive opt-out. Never fetch more history once the
            # shared scan budget has run out.
            for _ in rows:
                if not self.scan():
                    self.incomplete = True
                    return False
            for request_id, raw in rows:
                after = request_id
                seen += 1
                if raw is None:
                    self.incomplete = True
                    return False
                raw_size = len(raw.encode('utf-8'))
                size += raw_size
                if (
                    seen > MAX_ROOT_HISTORY_RESULTS
                    or raw_size > MAX_ROOT_HISTORY_RESULT_BYTES
                    or size > MAX_ROOT_HISTORY_BYTES
                ):
                    self.incomplete = True
                    return False
                try:
                    result = loads(raw)
                except Failure, ValueError, TypeError:
                    self.incomplete = True
                    return False
                if not isinstance(result, dict) or result.get('request_id') != request_id:
                    self.incomplete = True
                    return False
                if result.get('subject') != follower or not isinstance(
                    result.get('operation'), str
                ):
                    self.incomplete = True
                    return False
                data = result.get('data')
                if (
                    result['operation'].partition('@')[0] == 'communication.unfollow'
                    and result.get('status') == 'ok'
                    and result.get('committed_at')
                    and result.get('receipt')
                    and result.get('error') is None
                    and isinstance(data, dict)
                    and data.get('id') == ROOT_SUBJECT
                    and data.get('following') is False
                ):
                    return False
            if len(rows) < limit:
                break
        self.defaults[follower] = True
        return True

    def relation(self, follower, target):
        if not follower or follower == target:
            return None
        row = self.tx.one(
            'SELECT created_at FROM agent_follows WHERE follower=? AND target=?',
            (follower, target),
        )
        if row:
            return 'explicit', row[0]
        if target == ROOT_SUBJECT and self.root_default(follower):
            return 'default', None
        return None


async def effective_follow(tx, follower, target):
    """Return explicit/default/None from records only; callers must check access.

    A default relation is a presentation policy, never a fabricated signed
    follow, a grant, or a notification subscription.
    """
    relation = _FollowPolicy(tx).relation(follower, target)
    return relation[0] if relation else None


async def effective_targets(tx, follower):
    """Bounded, stable preference IDs; callers must filter active/visible/blocks."""
    policy = _FollowPolicy(tx)
    targets = {
        row[0]
        for row in tx.rows(
            'SELECT target FROM agent_follows WHERE follower=? AND target<>? '
            'ORDER BY target LIMIT ?',
            (follower, follower, MAX_FOLLOWS),
        )
    }
    if policy.relation(follower, ROOT_SUBJECT):
        targets.add(ROOT_SUBJECT)
    return sorted(targets)


async def effective_accounts(app, ctx, request, tx, subject, *, incoming=False, after='', budget):
    """Yield visible effective neighbors in stable ID order for pages/counts."""
    policy = _FollowPolicy(tx, budget)
    if blocked(tx, ctx.principal.subject, subject) or not tx.one(
        "SELECT 1 FROM resources WHERE id=? AND type='user' AND state='active'", (subject,)
    ):
        return
    while True:
        budget.check()
        if incoming and subject == ROOT_SUBJECT:
            rows = tx.rows(
                "SELECT id FROM resources WHERE type='user' AND state='active' AND id>? "
                'UNION SELECT follower AS id FROM agent_follows WHERE target=? AND follower>? '
                'ORDER BY id LIMIT 128',
                (after, ROOT_SUBJECT, after),
            )
        elif incoming:
            rows = tx.rows(
                'SELECT follower FROM agent_follows WHERE target=? AND follower>? '
                'ORDER BY follower LIMIT 128',
                (subject, after),
            )
        else:
            rows = tx.rows(
                'SELECT target AS id FROM agent_follows WHERE follower=? AND target>? '
                'UNION SELECT ? AS id WHERE ?>? ORDER BY id LIMIT 128',
                (subject, after, ROOT_SUBJECT, ROOT_SUBJECT, after),
            )
        for (rid,) in rows:
            budget.scan()
            after = rid
            if (
                rid == subject
                or blocked(tx, subject, rid)
                or blocked(tx, ctx.principal.subject, rid)
            ):
                continue
            resource = await tx.resource(rid)
            if resource.type != 'user' or resource.state != 'active':
                continue
            if not await visible(app, ctx, request, tx, rid):
                continue
            source, target = (rid, subject) if incoming else (subject, rid)
            if policy.relation(source, target):
                yield rid, resource, bool(policy.relation(target, source))
        if len(rows) < 128:
            break


class _TopologyBudget:
    def __init__(self, deadline):
        self.deadline = deadline
        self.scanned = 0
        self.bounded = False

    def check(self):
        require(time.monotonic() < self.deadline, 'query_cost_exceeded')

    def scan(self):
        self.check()
        if self.scanned == MAX_READ_SCANNED:
            self.bounded = True
            return False
        self.scanned += 1
        return True


async def public_topology(app, ctx, request, tx, max_users=256, max_edges=2048):
    """A fixed global slice of currently public, readable, unblocked accounts.

    Root reserves one node when visible, regardless of its position in ID order.
    Caps include that node and all derived edges. No request-persistent cache is
    kept, and the scan ceiling also includes historical opt-out evidence.
    """
    require(type(max_users) is int and 1 <= max_users <= MAX_TOPOLOGY_USERS, 'invalid_limit')
    require(type(max_edges) is int and 1 <= max_edges <= MAX_TOPOLOGY_EDGES, 'invalid_limit')
    budget = _TopologyBudget(ctx.deadline_monotonic)
    policy = _FollowPolicy(tx, budget)
    public = replace(
        ctx,
        principal=Principal(
            actor=None,
            subject=None,
            credential_id=None,
            method='anonymous',
            certificates=(),
            ceiling=(),
        ),
    )

    async def shown(rid):
        if blocked(tx, ctx.principal.subject, rid):
            return False
        if not await visible(app, public, request, tx, rid):
            return False
        # An already anonymous caller has the exact same authority as public.
        # Reuse only this decision in this transaction; signed callers still
        # need both checks, and every new projection reads current ACLs again.
        return public.principal == ctx.principal or await visible(app, ctx, request, tx, rid)

    nodes = []
    root = tx.one(
        "SELECT id FROM resources WHERE id=? AND type='user' AND state='active'",
        (ROOT_SUBJECT,),
    )
    if root and budget.scan() and await shown(ROOT_SUBJECT):
        nodes.append(ROOT_SUBJECT)
    after = ''
    done = False
    while not done:
        budget.check()
        rows = tx.rows(
            "SELECT id FROM resources WHERE type='user' AND state='active' AND id>? AND id<>? "
            'ORDER BY id LIMIT 128',
            (after, ROOT_SUBJECT),
        )
        for (rid,) in rows:
            after = rid
            if not budget.scan():
                done = True
                break
            if not await shown(rid):
                continue
            if len(nodes) == max_users:
                budget.bounded = True
                done = True
                break
            nodes.append(rid)
        if len(rows) < 128:
            done = True
    nodes.sort()
    edges = {}
    if nodes:
        placeholders = ','.join('?' for _ in nodes)
        rows = tx.rows(
            'SELECT follower,target,created_at FROM agent_follows WHERE follower IN ('
            + placeholders
            + ') AND target IN ('
            + placeholders
            + ') ORDER BY follower,target LIMIT ?',
            (*nodes, *nodes, MAX_READ_SCANNED - budget.scanned + 1),
        )
        for source, target, created_at in rows:
            if not budget.scan():
                break
            if source == target or blocked(tx, source, target):
                continue
            edges[source, target] = ('explicit', created_at)
            if len(edges) > max_edges:
                budget.bounded = True
                break
        if ROOT_SUBJECT in nodes:
            for source in nodes:
                budget.check()
                if source == ROOT_SUBJECT or blocked(tx, source, ROOT_SUBJECT):
                    continue
                relation = policy.relation(source, ROOT_SUBJECT)
                if relation:
                    edges[source, ROOT_SUBJECT] = relation
    if len(edges) > max_edges:
        budget.bounded = True
    projected = [
        {
            'source': source,
            'target': target,
            'source_type': source_type,
            'created_at': created_at,
            'mutual': bool(policy.relation(target, source)),
        }
        for (source, target), (source_type, created_at) in sorted(edges.items())[:max_edges]
    ]
    data = {
        'version': 1,
        'nodes': nodes,
        'edges': projected,
        'bounded': budget.bounded or policy.incomplete,
        # Never expose how many private accounts or receipts were inspected.
        'scanned': len(nodes) + len(projected),
    }
    budget.check()
    require(
        len(canonical(data)) <= app.settings.server.limits.max_response_bytes, 'response_too_large'
    )
    return data


async def account(tx, value, *, active=True, identity=True):
    rid = await resolve(tx, value)
    resource = await tx.resource(rid)
    require(
        resource.type == 'user' and (not active or resource.state == 'active'),
        'invalid_follow_target',
    )
    if identity:
        await tx.subject(rid)
    return resource


def install(app, op):
    async def change(ctx, request, tx):
        subject = ctx.principal.subject
        require(subject is not None, 'authentication_required')
        await account(tx, subject)
        await app.authorizer.require_base(ctx.principal, operation_id(request), subject, tx)
        enabled = request.operation == 'communication.follow'
        target = await account(tx, request.arguments['id'], active=enabled)
        require(subject != target.id, 'cannot_follow_self')
        await app.authorizer._ceiling(ctx.principal, operation_id(request), target.id, tx)
        if enabled:
            await check_access(app, ctx, request, tx, target.id, 'read')
            require(
                tx.one(
                    'SELECT 1 FROM dm_blocks WHERE (blocker=? AND blocked=?) OR (blocker=? AND blocked=?)',
                    (subject, target.id, target.id, subject),
                )
                is None,
                'follow_blocked',
            )
            existing = tx.one(
                'SELECT 1 FROM agent_follows WHERE follower=? AND target=?',
                (subject, target.id),
            )
            require(
                existing
                or tx.one(
                    'SELECT COUNT(*) FROM agent_follows WHERE follower=?',
                    (subject,),
                )[0]
                < MAX_FOLLOWS,
                'follow_limit_exceeded',
            )
            tx.execute(
                'INSERT INTO agent_follows VALUES (?,?,?) ON CONFLICT(follower,target) DO NOTHING',
                (subject, target.id, wire(ctx.now)),
                write=True,
            )
        else:
            tx.execute(
                'DELETE FROM agent_follows WHERE follower=? AND target=?',
                (subject, target.id),
                write=True,
            )
        if target.id == ROOT_SUBJECT:
            if enabled:
                tx.execute(
                    'DELETE FROM settings WHERE key=?',
                    ('root_follow_optout:' + subject,),
                    write=True,
                )
            else:
                tx.set_setting('root_follow_optout:' + subject, True)
        mutual = bool(enabled and await effective_follow(tx, target.id, subject))
        return HandlerOutput(data={'id': target.id, 'following': enabled, 'mutual': mutual})

    for name in ('communication.follow', 'communication.unfollow'):
        op(name, obj({'id': IDENTIFIER}, ('id',)))(change)

    async def listing(ctx, request, tx):
        target = await account(tx, request.arguments['subject_id'], identity=False)
        await check_access(app, ctx, request, tx, target.id, 'read')
        incoming = request.operation == 'communication.followers'
        after = request.arguments.get('after', '')
        limit = request.arguments.get('limit', 20)
        budget = ReadBudget(ctx.deadline_monotonic, app.settings.server.limits.max_response_bytes)
        items, more = [], False
        # One extra visible neighbor determines has_more. Scans (including
        # opt-out evidence) are bounded independently of the requested page.
        async for rid, resource, mutual in effective_accounts(
            app, ctx, request, tx, target.id, incoming=incoming, after=after, budget=budget
        ):
            if len(items) == limit:
                more = True
                break
            budget.node(4)
            items.append({
                'id': rid,
                'name': resource.name,
                'path': short_subject_path(await tx.path(rid)),
                'mutual': mutual,
            })
        data = {'subject_id': target.id, 'items': items, 'has_more': more}
        if more:
            data['after'] = items[-1]['id']
        budget.output(data)
        return HandlerOutput(data=data)

    schema = obj({'subject_id': IDENTIFIER, 'limit': LIMIT, 'after': IDENTIFIER}, ('subject_id',))
    for name in ('communication.agent_following', 'communication.followers'):
        op(name, schema, effect='read')(listing)
