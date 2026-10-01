"""Opt-in visual facts; nothing here grants identity or reveals private activity.

Only an explicit anonymous ``star`` projection pays this cost. Certificate and
post scans are bounded, money uses the existing publication policy, and absence
is unknown rather than an invented zero/offline observation.
"""

from __future__ import annotations

import time

from msg.constants import ROOT_SUBJECT
from msg.core.codec import wire
from msg.core.errors import Failure, require
from msg.market.ledger import SCALE, balance

MAX_CERTIFICATES = 32
MAX_ACTIVITY_CANDIDATES = 64


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
    last_post = None
    for rid, created_at in tx.rows(
        "SELECT id,created_at FROM resources WHERE owner=? AND type='post' "
        "AND state='active' AND created_at<=? ORDER BY created_at DESC,id DESC LIMIT ?",
        (subject_id, wire(ctx.now), MAX_ACTIVITY_CANDIDATES),
    ):
        budget()
        if await visible(app, ctx, request, tx, rid):
            last_post = created_at
            break

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
        'balance': reserve,
        'checked_at': wire(ctx.now),
    }
