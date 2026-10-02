"""One bounded recommendation page, using effective preferences and public posts."""

import re
from dataclasses import replace

from msg.algorithm import ALGORITHM_VERSION, Candidate, rank
from msg.core.codec import wire
from msg.core.identifiers import hex_id
from msg.core.models import HandlerOutput, Principal, ResourceRef
from msg.core.post_preview import post_preview
from msg.core.read_query import ReadBudget
from msg.core.tags import normalize_tag
from msg.plugins.agent_follows import blocked, effective_targets
from msg.plugins.common import operation_id
from msg.plugins.discovery import visible
from msg.plugins.schemas import obj

FEED_SCHEMA = obj({
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
    'interests': {
        'type': 'array',
        'maxItems': 10,
        'uniqueItems': True,
        'items': {'type': 'string', 'minLength': 1, 'maxLength': 64},
    },
})


def install(app, op):
    @op('discovery.recommendations', FEED_SCHEMA, effect='read')
    async def feed(ctx, request, tx):
        subject = ctx.principal.subject
        following = set()
        budget = ReadBudget(ctx.deadline_monotonic, app.settings.server.limits.max_response_bytes)
        if subject:
            await app.authorizer.require_base(ctx.principal, operation_id(request), subject, tx)
            for target in await effective_targets(tx, subject):
                budget.scan()
                resource = await tx.resource(target)
                if (
                    resource.type == 'user'
                    and resource.state == 'active'
                    and not blocked(tx, subject, target)
                    and await visible(app, ctx, request, tx, target)
                ):
                    following.add(target)
            # Old subscriptions remain private, but are useful to their owner's
            # feed. They are never turned into public account-follow relations.
            following.update(
                row[0]
                for row in tx.rows(
                    'SELECT w.resource FROM watches w JOIN resources r ON r.id=w.resource '
                    "WHERE w.subject=? AND r.type='user' AND r.state='active' "
                    'ORDER BY w.resource LIMIT 1000',
                    (subject,),
                )
            )
        interests = tuple(normalize_tag(tag) for tag in request.arguments.get('interests', ()))
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
        sources = [
            tx.rows(
                "SELECT id FROM resources WHERE type='post' AND state='active' AND created_at<=? "
                'ORDER BY created_at DESC,id DESC LIMIT 128',
                (wire(ctx.now),),
            )
        ]
        followed = sorted(following)
        if followed:
            sources.append(
                tx.rows(
                    "SELECT id FROM resources WHERE type='post' AND state='active' AND created_at<=? "
                    'AND owner IN (' + ','.join('?' for _ in followed) + ') '
                    'ORDER BY created_at DESC,id DESC LIMIT 128',
                    (wire(ctx.now), *followed),
                )
            )
        candidates, hydrated = [], {}
        for source in sources:
            for (rid,) in source:
                budget.scan()
                if rid in hydrated:
                    continue
                if not await visible(app, public, request, tx, rid):
                    continue
                if subject and not await visible(app, ctx, request, tx, rid):
                    continue
                resource = await tx.resource(rid)
                revision = await tx.revision(ResourceRef(id=rid))
                if subject and tx.one(
                    'SELECT 1 FROM dm_blocks WHERE '
                    '(blocker=? AND blocked IN (?,?)) OR (blocked=? AND blocker IN (?,?))',
                    (
                        subject,
                        resource.owner,
                        revision.author,
                        subject,
                        resource.owner,
                        revision.author,
                    ),
                ):
                    continue
                candidates.append(
                    Candidate(
                        rid,
                        revision.author,
                        resource.created_at,
                        resource.tags,
                    )
                )
                hydrated[rid] = (resource, revision)
        recommendations = rank(
            candidates,
            now=ctx.now,
            following=following,
            interests=interests,
            limit=request.arguments.get('limit', 20),
        )
        items = []
        for recommendation in recommendations:
            budget.node(8)
            resource, revision = hydrated[recommendation.candidate.id]
            item = {
                'id': resource.id,
                'revision': revision.id,
                'author': revision.author,
                'path': '/*' + hex_id(resource.id),
                'created_at': wire(resource.created_at),
                'score': recommendation.score,
                'reasons': list(recommendation.reasons),
            }
            title = resource.name.removesuffix('.md')
            if not re.fullmatch(r'p_[0-9a-f]{32}', title):
                item['title'] = title
            if await visible(app, public, request, tx, revision.author):
                item['author_name'] = (await tx.resource(revision.author)).name
            if revision.summary:
                item['summary'] = revision.summary
            elif revision.content.media_type.startswith('text/') and revision.content.size <= 32768:
                body = (await app.contents.read_bytes(revision.content, limit=32768)).decode(
                    'utf-8'
                )
                preview = post_preview(resource.name, body)
                item.setdefault('title', preview['title'])
                item['summary'] = preview['excerpt'] or body[:180]
            item.setdefault('title', post_preview(resource.name, revision.summary or '')['title'])
            items.append(item)
        data = {
            'algorithm': 'msg for bot need',
            'algorithm_version': ALGORITHM_VERSION,
            'items': items,
            'interests': list(interests),
        }
        budget.output(data)
        return HandlerOutput(data=data)
