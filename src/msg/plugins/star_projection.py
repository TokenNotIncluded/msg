"""Opt-in visual facts; nothing here grants identity or reveals private activity.

Only an explicit anonymous ``star`` projection pays this cost. Certificate and
post scans are bounded, money uses the existing publication policy, and absence
is unknown rather than an invented zero/offline observation.
"""

from __future__ import annotations

import time
from dataclasses import replace

from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, loads, wire
from msg.core.errors import Failure, require
from msg.core.models import Principal
from msg.core.planet_layout import fallback_position, planet_layout
from msg.market.ledger import SCALE, balance

MAX_CERTIFICATES = 32
MAX_ACTIVITY_CANDIDATES = 64
MAX_POST_COUNT_CANDIDATES = 4096


async def public_layout(app, ctx, request, tx):
    """Only cache within one read transaction, never across ACL epochs."""
    value = getattr(tx, '_planet_public_layout', None)
    if value is None:
        from msg.plugins.agent_follows import public_topology

        topology = await public_topology(app, ctx, request, tx)
        value = {
            'topology': topology,
            'layouts': planet_layout(topology['nodes'], topology['edges']),
        }
        tx._planet_public_layout = value
    return value


async def public_star_batch(app, ctx, request, tx):
    """One bounded anonymous snapshot; every summary still checks current ACL."""
    require(ctx.principal.subject is None, 'public_star_summary_only')
    projection = await public_layout(app, ctx, request, tx)
    summaries = {}
    bounded = projection['topology']['bounded']
    output_bytes = len(canonical(projection['topology']))
    output_limit = app.settings.server.limits.max_response_bytes // 2
    require(output_bytes < output_limit, 'response_too_large')
    for rid in projection['topology']['nodes']:
        if time.monotonic() + 0.02 >= ctx.deadline_monotonic:
            bounded = True
            break
        try:
            summary = await star_projection(app, ctx, request, tx, rid)
        except Failure as exc:
            if exc.code == 'query_cost_exceeded':
                bounded = True
                break
            if exc.code in {
                'permission_denied',
                'authentication_required',
                'not_found',
                'resource_purged',
                'local_only',
                'ancestor_inactive',
                'certificate_gate',
                'credential_ceiling',
            }:
                bounded = True
                continue
            raise
        size = len(canonical({rid: summary}))
        if output_bytes + size > output_limit:
            bounded = True
            break
        output_bytes += size
        summaries[rid] = summary
    return {
        'topology': projection['topology'],
        'stars': summaries,
        'bounded': bounded,
        'checked_at': wire(ctx.now),
    }


async def post_counts(app, ctx, request, tx):
    """Count current authored revisions after each post's current read check.

    A globally bounded scan is shared by stars in this transaction. Raw scan
    counts and private author totals never become public metadata.
    """
    from msg.plugins.discovery import visible

    principal = ctx.principal.subject
    cached = getattr(tx, '_planet_post_counts', {})
    if principal in cached:
        return cached[principal]
    rows = tx.rows(
        'SELECT r.id,r.created_at,v.body FROM resources r JOIN revisions v ON v.id=r.revision '
        "WHERE r.type='post' AND r.state='active' AND r.created_at<=? ORDER BY r.id LIMIT ?",
        (wire(ctx.now), MAX_POST_COUNT_CANDIDATES + 1),
    )
    counts, last, ids = {}, {}, {}
    for rid, created_at, raw in rows[:MAX_POST_COUNT_CANDIDATES]:
        require(time.monotonic() < ctx.deadline_monotonic, 'query_cost_exceeded')
        if not await visible(app, ctx, request, tx, rid):
            continue
        author = loads(raw)['author']
        counts[author] = counts.get(author, 0) + 1
        ids.setdefault(author, set()).add(rid)
        last[author] = max(last.get(author, created_at), created_at)
    value = {
        'counts': counts,
        'ids': ids,
        'last': last,
        'exact': len(rows) <= MAX_POST_COUNT_CANDIDATES,
    }
    cached[principal] = value
    tx._planet_post_counts = cached
    return value


async def private_post_counts(app, ctx, request, tx, subject_id):
    require(
        ctx.principal.subject == subject_id and ctx.principal.method != 'anonymous',
        'permission_denied',
    )
    own = await post_counts(app, ctx, request, tx)
    public = await post_counts(
        app,
        replace(
            ctx,
            principal=Principal(
                actor=None,
                subject=None,
                credential_id=None,
                method='anonymous',
                certificates=(),
                ceiling=(),
            ),
        ),
        request,
        tx,
    )
    exact = own['exact'] and public['exact']
    count = own['counts'].get(subject_id, 0)
    private_count = len(own['ids'].get(subject_id, set()) - public['ids'].get(subject_id, set()))
    return {
        'own': count if exact else None,
        'private_visible': private_count if exact else None,
        'exact': exact,
    }


def reserve_projection(app, amount, visibility):
    # The ledger is int64. Transport it as a string, never a lossy JS Number.
    return {
        'visibility': visibility,
        'amount_minor': str(amount),
        'scale': SCALE,
        'code': app.settings.money.code,
    }


async def star_projection(app, ctx, request, tx, subject_id):
    from msg.plugins.common import check_access
    from msg.plugins.communication import presence_record
    from msg.plugins.discovery import visible
    from msg.plugins.money import public_account

    require(ctx.principal.subject is None, 'public_star_summary_only')
    resource = await tx.resource(subject_id)
    require(resource.type == 'user' and resource.state == 'active', 'not_found')
    await check_access(app, ctx, request, tx, subject_id, 'read')

    def budget():
        require(time.monotonic() < ctx.deadline_monotonic, 'query_cost_exceeded')

    certificate = {'state': 'none'}
    rows = tx.rows(
        'SELECT id FROM certificates WHERE subject=? AND revoked=0 ORDER BY id LIMIT ?',
        (subject_id, MAX_CERTIFICATES + 1),
    )
    if len(rows) > MAX_CERTIFICATES:
        certificate = {'state': 'unknown'}
    for (cid,) in rows[:MAX_CERTIFICATES]:
        budget()
        if not await visible(app, ctx, request, tx, cid):
            continue
        try:
            cert = await app.certificates.validate(cid, tx)
        except Failure:
            # Fail closed, including revoked issuers/keys, not just leaf expiry.
            continue
        if cert.subject_id == subject_id:
            certificate = {
                'state': 'valid',
                'id': cert.resource_id,
                'path': await tx.path(cert.resource_id),
                'kind': cert.kind,
                'expires_at': wire(cert.expires_at),
            }
            break

    # Same TTL interpretation as communication.presence_get, with private text
    # and capability hints deliberately excluded from the visual projection.
    presence = presence_record(tx, subject_id, ctx.now)
    presence = {
        key: presence[key]
        for key in ('state', 'updated_at', 'expires_at', 'self_reported')
        if key in presence
    }
    posts = await post_counts(app, ctx, request, tx)
    last_post = posts['last'].get(subject_id)
    projection = await public_layout(app, ctx, request, tx)
    layout = projection['layouts'].get(subject_id)
    if layout is None:
        layout = {
            'version': 4,
            'position': fallback_position(subject_id),
            'orbit': {'kind': 'isolated', 'source_type': None, 'parent_id': None, 'members': []},
            'bounded': True,
        }

    reserve = {'visibility': 'private'}
    try:
        await public_account(tx, subject_id)
    except Failure as exc:
        if exc.code != 'not_found':
            raise
    else:
        reserve = reserve_projection(app, balance(tx, subject_id), 'public')
    return {
        'role': 'root' if subject_id == ROOT_SUBJECT else 'user',
        'certificate': certificate,
        'presence': presence,
        'last_public_post_at': last_post,
        'post_count': {
            'public': posts['counts'].get(subject_id, 0) if posts['exact'] else None,
            'exact': posts['exact'],
            'scanned': posts['counts'].get(subject_id, 0),
        },
        'layout': layout,
        'balance': reserve,
        'checked_at': wire(ctx.now),
    }
