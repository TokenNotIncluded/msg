"""Pinned, case-private reasons are part of a signed arbitration allocation.

This is a reference to an ordinary immutable File revision, not executable policy.
The case grants parties/panel access to that one blob, never the author's folder.
"""
from __future__ import annotations

from msg.core.codec import canonical, decode, digest, loads, parse_time, wire
from msg.core.errors import Failure, require
from msg.core.models import BlobRef, HandlerOutput, ResourceRef
from msg.market.delivery import verify_blob
from msg.plugins.common import check_access
from msg.plugins.orders import _subject
from msg.plugins.schemas import IDENTIFIER, obj

PINNED_REF = obj({'id': IDENTIFIER, 'revision': IDENTIFIER}, ('id', 'revision'))
MAX_REASON_BYTES = 65536


def evidence_id(case, ref):
    return 'rationale_' + digest({'case_id': case['id'], 'round': case['round'],
                                  'ref': wire(ref)})[7:]


def record(tx, case, proposal):
    """Check immutable bindings without reinterpreting an old grant or policy."""
    ref = proposal['rationale_ref']
    row = tx.one('SELECT author,visibility,body FROM arbitration_evidence WHERE id=? AND case_id=?',
                 (evidence_id(case, ref), case['id']))
    require(row is not None, 'decision_rationale_missing')
    body = loads(row[2])
    require(row[0] in case['panels'][str(case['round'])] and row[1] == 'parties' and
            body['kind'] == 'rationale' and body['round'] == case['round'] and
            body['source'] == ref and body['blob']['digest'] == proposal['rationale_digest'],
            'decision_rationale_mismatch')
    return body


async def verified(app, tx, case, proposal):
    body = record(tx, case, proposal)
    try:
        await verify_blob(app, decode(BlobRef, body['blob']))
    except Failure as exc:
        if exc.code == 'package_missing':
            raise Failure('decision_rationale_missing') from None
        if exc.code == 'package_digest_mismatch':
            raise Failure('decision_rationale_mismatch') from None
        raise


def install(app, op):
    @op('orders.dispute_rationale', obj({'case_id': IDENTIFIER, 'ref': PINNED_REF},
        ('case_id', 'ref')), signature=True)
    async def deposit(ctx, request, tx):
        from msg.market.arbitration import owned_case, panel_valid
        actor = _subject(ctx)
        case = await owned_case(tx, ctx, request.arguments['case_id'])
        require(actor in case['panels'][str(case['round'])] and await panel_valid(tx, case),
                'arbitrator_required')
        require(case['state'] == 'open' and
                ctx.now < parse_time(case['round_deadlines'][str(case['round'])]),
                'case_evidence_closed')
        ref = decode(ResourceRef, request.arguments['ref'])
        resource = await tx.resource(ref.id)
        require(resource.owner == actor and resource.type in {'file', 'attachment'} and
                resource.state == 'active', 'rationale_not_owned')
        await check_access(app, ctx, request, tx, ref.id, 'read')
        blob = (await tx.revision(ref)).content
        require(0 < blob.size <= MAX_REASON_BYTES and
                blob.media_type in {'text/plain', 'text/markdown'}, 'rationale_text_required')
        await verify_blob(app, blob)
        raw = await app.contents.read_bytes(blob, limit=MAX_REASON_BYTES)
        try:
            require(bool(raw.decode('utf-8').strip()), 'rationale_text_required')
        except UnicodeDecodeError:
            raise Failure('rationale_text_required') from None
        eid = evidence_id(case, wire(ref))
        previous = tx.one('SELECT body FROM arbitration_evidence WHERE id=?', (eid,))
        if previous is None:
            require(tx.one('SELECT COUNT(*) FROM arbitration_evidence WHERE case_id=? AND author=?',
                           (case['id'], actor))[0] < 64, 'case_evidence_limit')
            await app.contents.pin(blob, eid)
            body = {'kind': 'rationale', 'blob': wire(blob), 'source': wire(ref),
                'round': case['round'], 'at': wire(ctx.now),
                'author_request_digest': request.payload_digest}
            tx.execute('INSERT INTO arbitration_evidence(id,case_id,author,visibility,body) '
                       "VALUES (?,?,?,'parties',?)",
                       (eid, case['id'], actor, canonical(body).decode()), write=True)
        binding = {'rationale_ref': wire(ref), 'rationale_digest': blob.digest}
        record(tx, case, binding)
        return HandlerOutput(data={**binding, 'evidence_id': eid})
