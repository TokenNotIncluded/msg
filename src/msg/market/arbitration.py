"""Deterministic panels and signed, quorum-bound escrow decisions.

A Case never holds a key or balance. A panel can attest only one allocation per
member/round. The EscrowEngine consumes the current, revalidated Decision once.
"""
from __future__ import annotations

from datetime import timedelta

from msg.core.codec import canonical, decode, digest, loads, parse_time, wire
from msg.core.errors import Failure, require
from msg.core.models import BlobRef, HandlerOutput, Principal, ResourceRef, Signature
from msg.market.orders import HASH, view
from msg.market.policy import contract
from msg.market.rationale import PINNED_REF
from msg.market.rationale import verified as verify_rationale
from msg.plugins.common import check_access, new_id, no_requirements
from msg.market.order_records import read_order as _row, require_signed_subject as _subject
from msg.plugins.schemas import IDENTIFIER, REF, SIGNATURE, obj
from msg.security.crypto import verify

REASONS = ('quality','not_as_described','wrong_ciphertext','delivery_missing',
           'delivery_digest_mismatch','package_missing','package_digest_mismatch',
           'delivery_timeout','other')
CASE = obj({'case_id': IDENTIFIER}, ('case_id',))
PROPOSAL = obj({'case_id': IDENTIFIER, 'order_id': IDENTIFIER,
    'round': {'type':'integer','minimum':0,'maximum':1}, 'policy_digest': HASH,
    'outcome': {'enum':['release','refund','split']},
    'refund_minor': {'type':'integer','minimum':0,'maximum':2**63-1},
    'expires_at': {'type':'string','maxLength':40},
    'rationale_ref': PINNED_REF, 'rationale_digest': HASH},
    ('case_id','order_id','round','policy_digest','outcome','refund_minor','expires_at',
     'rationale_ref','rationale_digest'))


def _read(tx, case_id):
    row = tx.one('SELECT body,round,state,deadline FROM arbitration_cases WHERE id=?', (case_id,))
    require(row is not None, 'case_not_found')
    case = loads(row[0])
    case.update(round=row[1], state=row[2], deadline=row[3])
    return case


def _conflict(tx, candidate, case):
    return candidate in {case['buyer'],case['seller']} or tx.one('''SELECT 1
        FROM arbitrator_conflicts WHERE arbitrator=? AND party IN (?,?)''',
        (candidate,case['buyer'],case['seller'])) is not None


async def member_valid(tx, candidate, case):
    epoch = case['policy']['candidate_epochs'].get(candidate)
    row = tx.one('SELECT epoch,active FROM arbitrator_roles WHERE subject=?', (candidate,))
    if not epoch or not row or not row[1] or row[0] != epoch or _conflict(tx, candidate, case):
        return False
    try:
        subject, resource = await tx.subject(candidate), await tx.resource(candidate)
        return not subject.local_only and subject.kind == 'registered' and resource.state == 'active'
    except Failure:
        return False


async def panel_for(tx, case, round_number):
    eligible = []
    previous = set(case.get('panels', {}).get('0', ())) if round_number else set()
    for candidate in case['policy']['policy']['candidates']:
        if candidate not in previous and await member_valid(tx, candidate, case):
            eligible.append(candidate)
    def score(candidate):
        return digest({'version': 1, 'order_id': case['order_id'],
            'policy_digest': case['policy']['policy_digest'], 'round': round_number,
            'subject': candidate})
    return sorted(eligible, key=lambda candidate: (score(candidate),candidate))[
        :case['policy']['policy']['panel_size']]


async def panel_valid(tx, case):
    panel = case['panels'][str(case['round'])]
    return (len(panel) == case['policy']['policy']['panel_size'] and
            all([await member_valid(tx, candidate, case) for candidate in panel]))


async def owned_case(tx, ctx, case_id, *, parties=False):
    viewer = ctx.principal.subject
    require(viewer is not None and ctx.principal.actor == viewer, 'case_not_found')
    case = _read(tx, case_id)
    if viewer in {case['buyer'],case['seller']}:
        return case
    if not parties and viewer in case['panels'][str(case['round'])] and await member_valid(tx, viewer, case):
        return case
    raise Failure('case_not_found')


def proposal_base(case):
    policy = case['policy']['policy']
    deadline = parse_time(case['round_deadlines'][str(case['round'])])
    return {'case_id': case['id'], 'order_id': case['order_id'], 'round': case['round'],
        'policy_digest': case['policy']['policy_digest'],
        'expires_at': wire(deadline+timedelta(seconds=policy['decision_lifetime_seconds']))}


def proposal_valid(case, proposal, total):
    base = proposal_base(case)
    require(set(proposal) == {*base,'outcome','refund_minor','rationale_ref','rationale_digest'} and
            all(proposal[k] == v for k,v in base.items()), 'decision_context_mismatch')
    refund, outcome = proposal['refund_minor'], proposal['outcome']
    require(type(refund) is int and ((outcome=='release' and refund==0) or
        (outcome=='refund' and refund==total) or (outcome=='split' and 0<refund<total)),
        'decision_amount_mismatch')


async def verify_vote(app, tx, case, vote):
    from msg.workers.effects import current_principal
    principal = decode(Principal, vote['principal'])
    require(principal.subject == vote['arbitrator'] and principal.actor == principal.subject and
            principal.method == 'signature' and await member_valid(tx, principal.subject, case),
            'decision_member_invalid')
    current = await current_principal(app, principal, tx)
    await app.authorizer.require_base(current, 'orders.dispute_vote@1', principal.subject, tx)
    credential = await tx.credential(principal.credential_id)
    signature = decode(Signature, vote['signature'])
    require(signature.key_id == credential.id and credential.kind == 'signing_key',
            'decision_signer_mismatch')
    verify(credential.verifier, canonical(vote['proposal']), signature, purpose='arbitration-decision')


async def validate_decision(app, tx, order, decision, now):
    row = tx.one('SELECT body,revoked FROM arbitration_decisions WHERE id=?', (decision['id'],))
    require(row is not None and not row[1] and digest(loads(row[0])) == digest(decision),
            'decision_revoked_or_unknown')
    case = _read(tx, decision['case_id'])
    require(case['order_id'] == order['id'] and case['round'] == decision['round'] and
            case['state'] == 'decided' and order['state'] == 'disputed', 'decision_not_current')
    proposal_valid(case, decision['proposal'], order['total_price_minor'])
    require(decision['refund_minor'] == decision['proposal']['refund_minor'] and
            decision['policy_digest'] == case['policy']['policy_digest'], 'decision_context_mismatch')
    require(parse_time(decision['expires_at']) > now, 'decision_expired')
    require(parse_time(decision['not_before']) <= now, 'appeal_window_open')
    require(await panel_valid(tx, case), 'decision_panel_invalid')
    require(decision['rationale_ref'] == decision['proposal']['rationale_ref'] and
            decision['rationale_digest'] == decision['proposal']['rationale_digest'],
            'decision_rationale_mismatch')
    await verify_rationale(app, tx, case, decision['proposal'])
    quorum = case['policy']['policy']['quorum']
    require(len(decision['voters']) == quorum and len(set(decision['voters'])) == quorum and
            set(decision['voters']) <= set(case['panels'][str(case['round'])]), 'decision_quorum_invalid')
    cast_times = []
    for arbitrator in decision['voters']:
        vote_row = tx.one('SELECT body FROM arbitration_votes WHERE case_id=? AND round=? AND arbitrator=?',
                         (case['id'],case['round'],arbitrator))
        require(vote_row is not None, 'decision_quorum_invalid')
        vote = loads(vote_row[0])
        require(vote['proposal'] == decision['proposal'], 'decision_conflicting_vote')
        await verify_vote(app, tx, case, vote)
        cast_times.append(parse_time(vote['cast_at']))
    delay = (case['policy']['policy']['appeal_seconds']
             if case['round']==0 and case['policy']['policy']['allow_appeal'] else 0)
    require(parse_time(decision['not_before']) == max(cast_times)+timedelta(seconds=delay) and
            decision['expires_at'] == decision['proposal']['expires_at'], 'decision_context_mismatch')


def notice(tx, case, state, now):
    # Private inbox references only. No testimony, vote, email or payload escapes.
    for recipient in {case['buyer'],case['seller']}:
        record = {'id': new_id('message'), 'sender': recipient, 'actor': recipient,
            'recipient': recipient, 'resource': wire(ResourceRef(id=recipient)),
            'order_id': case['order_id'], 'case_id': case['id'],
            'time': wire(now), 'state': state, 'source': 'system'}
        tx.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',
            (record['id'],recipient,recipient,recipient,'case:'+case['id'],canonical(record).decode()), write=True)


async def objective_fault(app, tx, order, now):
    locked = contract(tx, order['id'])
    if order['state'] == 'funded':
        deadline = parse_time(order['funded_at']) + timedelta(
            seconds=locked['policy']['policy']['delivery_timeout_seconds'])
        if now >= deadline:
            return 'delivery_timeout'
        if locked['listing']['delivery_mode'] == 'managed_instant':
            from msg.market.delivery import package
            try:
                await package(app, tx, order)
            except Failure as exc:
                if exc.code in {'package_missing','package_digest_mismatch'}:
                    return exc.code
                raise
    if order['state'] == 'delivered':
        from msg.market.delivery import verified
        try:
            await verified(app, tx, order)
        except Failure as exc:
            if exc.code == 'delivery_not_found':
                return 'delivery_missing'
            if exc.code in {'delivery_digest_mismatch','package_missing','package_digest_mismatch'}:
                return exc.code
            raise
    return None


async def execute(app, tx, case, decision_id, now, actor, request_id):
    from msg.market.escrow import settle
    order = _row(tx, case['order_id'], case['buyer'])
    row = tx.one('SELECT body FROM arbitration_decisions WHERE id=? AND case_id=? AND round=?',
                 (decision_id,case['id'],case['round']))
    require(row is not None, 'decision_not_current')
    decision = loads(row[0])
    existing = tx.one('SELECT body FROM order_settlements WHERE order_id=?', (order['id'],))
    if existing:
        settlement = loads(existing[0])
        require(settlement['decision_id'] == decision_id, 'order_already_settled')
        return settlement['receipts']
    receipts = await settle(app, tx, order, now=now, actor=actor, request_id=request_id,
        reason='arbitration_decision', refund_minor=decision['refund_minor'], decision=decision)
    notice(tx, case, 'executed', now)
    return receipts


async def resolve_cases(app, *, limit=100):
    changed, now = [], app.clock()
    async with app.metadata.transaction(write=True) as tx:
        app.runtime_generation.require_current(tx)
        for (case_id,) in tx.rows('''SELECT id FROM arbitration_cases
            WHERE state IN ('open','decided') AND deadline<=? ORDER BY deadline,id LIMIT ?''',
            (wire(now),limit)):
            case = _read(tx, case_id)
            reason = 'quorum_timeout'
            if case['state'] == 'decided':
                row = tx.one('SELECT id FROM arbitration_decisions WHERE case_id=? AND round=?',
                             (case_id,case['round']))
                try:
                    require(row is not None, 'decision_not_current')
                    await execute(app, tx, case, row[0], now, case['buyer'], 'case-resolve:'+case_id)
                    changed.append(case_id)
                    continue
                except Failure as exc:
                    # No unexpected SQL/IO failure is converted into a money rule.
                    if exc.code not in {'decision_revoked_or_unknown','decision_expired',
                        'decision_panel_invalid','decision_member_invalid','credential_revoked',
                        'credential_expired','invalid_signature','permission_denied',
                        'credential_ceiling','decision_not_current','decision_quorum_invalid',
                        'decision_signer_mismatch','decision_conflicting_vote','decision_context_mismatch',
                        'decision_rationale_missing','decision_rationale_mismatch'}:
                        raise
                    reason = exc.code
            tx.execute("UPDATE arbitration_cases SET state='held' WHERE id=?", (case_id,), write=True)
            tx.set_setting('case_hold:'+case_id, {'reason': reason, 'at': wire(now)})
            notice(tx, case, 'held', now)
            changed.append(case_id)
    return changed


def install(app, op):
    from msg.market.rationale import install as install_rationale
    install_rationale(app, op)

    @op('orders.dispute_open', obj({'order_id': IDENTIFIER,
        'reason': {'enum': list(REASONS)}}, ('order_id','reason')), signature=True)
    async def open_case(ctx, request, tx):
        from msg.market.escrow import settle, transition
        actor = _subject(ctx)
        order = _row(tx, request.arguments['order_id'], actor)
        locked = contract(tx, order['id'])
        existing = tx.one('SELECT id FROM arbitration_cases WHERE order_id=?', (order['id'],))
        if existing:
            return HandlerOutput(data={'case_id': existing[0], 'order_id': order['id']})
        require(order['state'] in {'funded','delivered'}, 'order_not_disputable')
        objective = await objective_fault(app, tx, order, ctx.now)
        if objective:
            receipts = await settle(app, tx, order, now=ctx.now, actor=actor,
                request_id=request.request_id, reason=objective, refund_minor=order['total_price_minor'])
            return HandlerOutput(data={'resolution': 'objective_refund', 'reason': objective,
                'order': view(tx, order, actor), 'receipts': receipts})
        case_id = new_id('case')
        deadline = wire(ctx.now+timedelta(seconds=locked['policy']['policy']['case_timeout_seconds']))
        case = {'id': case_id, 'order_id': order['id'], 'buyer': order['buyer'],
            'seller': order['seller'], 'opened_by': actor, 'opened_at': wire(ctx.now),
            'reason': request.arguments['reason'], 'policy': locked['policy'],
            'panels': {}, 'round_deadlines': {'0': deadline}, 'round': 0}
        case['panels']['0'] = await panel_for(tx, case, 0)
        state = 'open' if await panel_valid(tx, case) else 'held'
        tx.execute('INSERT INTO arbitration_cases(id,order_id,round,state,deadline,body) VALUES (?,?,0,?,?,?)',
                   (case_id,order['id'],state,deadline,canonical(case).decode()), write=True)
        await transition(tx, order, 'disputed', now=ctx.now, actor=actor,
                         request_id=request.request_id, reason=request.arguments['reason'])
        notice(tx, case, state, ctx.now)
        return HandlerOutput(data={'case_id': case_id, 'order_id': order['id'], 'state': state,
            'panel': case['panels']['0'], 'policy_digest': locked['policy']['policy_digest'],
            'deadline': deadline})

    @op('orders.dispute_get', CASE, effect='read')
    async def get(ctx, request, tx):
        case = await owned_case(tx, ctx, request.arguments['case_id'])
        viewer = ctx.principal.subject
        panel = viewer in case['panels'][str(case['round'])]
        evidence = []
        for eid,author,visibility,body in tx.rows('''SELECT id,author,visibility,body
            FROM arbitration_evidence WHERE case_id=? ORDER BY id''', (case['id'],)):
            if panel or viewer == author or visibility == 'parties':
                record = loads(body)
                evidence.append({'id': eid,'author': author,'visibility': visibility,
                    'kind': record['kind'],'round': record['round'],
                    'digest': record['blob']['digest'],'size': record['blob']['size'],
                    **({'rationale_ref': record['source']} if record['kind']=='rationale' else {})})
        decisions = [loads(row[0]) for row in tx.rows(
            'SELECT body FROM arbitration_decisions WHERE case_id=? ORDER BY round', (case['id'],))]
        votes = [loads(row[0]) for row in tx.rows(
            'SELECT body FROM arbitration_votes WHERE case_id=? AND round=? ORDER BY arbitrator',
            (case['id'],case['round']))]
        return HandlerOutput(data={'case': {k:v for k,v in case.items() if k!='policy'},
            'policy': case['policy'], 'panel_valid': await panel_valid(tx, case),
            'proposal_base': proposal_base(case), 'evidence': evidence,
            'votes': [{'arbitrator':v['arbitrator'],'proposal':v['proposal'],'signature':v['signature']}
                      for v in votes], 'decisions': decisions})

    @op('orders.dispute_summary', CASE, effect='read', requirements=no_requirements)
    async def summary(ctx, request, tx):
        case = _read(tx, request.arguments['case_id'])
        row = tx.one('SELECT body FROM arbitration_decisions WHERE case_id=? AND round=?',
                     (case['id'],case['round']))
        return HandlerOutput(data={'case_id': case['id'], 'state': case['state'],
            'policy_digest': case['policy']['policy_digest'],
            'outcome': loads(row[0])['proposal']['outcome'] if row and case['state']=='executed' else None})

    evidence_fields = {'case_id': IDENTIFIER, 'visibility': {'enum':['panel','parties']}}
    @op('orders.dispute_statement', obj({**evidence_fields,
        'text': {'type':'string','minLength':1,'maxLength':4096}}, ('case_id','visibility','text')),
        signature=True)
    @op('orders.dispute_evidence', obj({**evidence_fields, 'ref': REF},
        ('case_id','visibility','ref')), signature=True)
    async def add_evidence(ctx, request, tx):
        actor = _subject(ctx)
        case = await owned_case(tx, ctx, request.arguments['case_id'], parties=True)
        require(case['state'] in {'open','held'} and
                ctx.now < parse_time(case['round_deadlines'][str(case['round'])]), 'case_evidence_closed')
        require(tx.one('SELECT COUNT(*) FROM arbitration_evidence WHERE case_id=? AND author=?',
                       (case['id'],actor))[0] < 64, 'case_evidence_limit')
        from msg.market.delivery import verify_blob
        if 'text' in request.arguments:
            blob = await app.contents.put_bytes(request.arguments['text'].encode(), 'text/plain')
            kind, source = 'statement', None
        else:
            ref = decode(ResourceRef, request.arguments['ref'])
            require(ref.revision is not None, 'payload_revision_required')
            resource = await tx.resource(ref.id)
            require(resource.owner == actor and resource.type in {'file','attachment'} and
                    resource.state == 'active', 'evidence_not_owned')
            await check_access(app, ctx, request, tx, ref.id, 'read')
            blob = (await tx.revision(ref)).content
            kind, source = 'resource', wire(ref)
        await verify_blob(app, blob)
        eid = new_id('evidence')
        await app.contents.pin(blob, eid)
        record = {'kind': kind, 'blob': wire(blob), 'source': source, 'round': case['round'],
            'at': wire(ctx.now), 'author_request_digest': request.payload_digest}
        tx.execute('INSERT INTO arbitration_evidence(id,case_id,author,visibility,body) VALUES (?,?,?,?,?)',
            (eid,case['id'],actor,request.arguments['visibility'],canonical(record).decode()), write=True)
        return HandlerOutput(data={'evidence_id': eid, 'digest': blob.digest})

    @op('orders.dispute_evidence_get', obj({'case_id': IDENTIFIER,'evidence_id': IDENTIFIER,
        'offset': {'type':'integer','minimum':0},
        'length': {'type':'integer','minimum':1,'maximum':65536}},
        ('case_id','evidence_id')), effect='read')
    async def evidence_get(ctx, request, tx):
        case = await owned_case(tx, ctx, request.arguments['case_id'])
        row = tx.one('SELECT author,visibility,body FROM arbitration_evidence WHERE id=? AND case_id=?',
                     (request.arguments['evidence_id'],case['id']))
        viewer = ctx.principal.subject
        require(row is not None and (viewer==row[0] or row[1]=='parties' or
                viewer in case['panels'][str(case['round'])]), 'evidence_not_found')
        record = loads(row[2])
        blob = decode(BlobRef, record['blob'])
        start = request.arguments.get('offset', 0)
        end = min(blob.size, start+request.arguments.get('length', 65536))
        require(0 <= start <= end <= blob.size, 'invalid_byte_range')
        raw = b''.join([part async for part in app.contents.read(blob,(start,end))])
        from msg.core.codec import b64
        return HandlerOutput(data={'evidence_id': request.arguments['evidence_id'], 'digest': blob.digest,
            'size': blob.size, 'offset': start, 'data': b64(raw), 'chunk_digest': digest(raw),
            'next_offset': end if end<blob.size else None})

    @op('orders.dispute_vote', obj({'proposal': PROPOSAL, 'signature': SIGNATURE},
        ('proposal','signature')), signature=True)
    async def vote(ctx, request, tx):
        actor = _subject(ctx)
        proposal = wire(request.arguments['proposal'])
        case = await owned_case(tx, ctx, proposal['case_id'])
        require(actor in case['panels'][str(case['round'])] and await panel_valid(tx, case),
                'arbitrator_required')
        require(case['state']=='open' and
                ctx.now < parse_time(case['round_deadlines'][str(case['round'])]), 'case_voting_closed')
        order = _row(tx, case['order_id'], case['buyer'])
        proposal_valid(case, proposal, order['total_price_minor'])
        await verify_rationale(app, tx, case, proposal)
        record = {'arbitrator': actor, 'proposal': proposal, 'signature': wire(request.arguments['signature']),
                  'principal': wire(ctx.principal), 'cast_at': wire(ctx.now)}
        await verify_vote(app, tx, case, record)
        previous = tx.one('SELECT body FROM arbitration_votes WHERE case_id=? AND round=? AND arbitrator=?',
                          (case['id'],case['round'],actor))
        if previous:
            require(loads(previous[0])['proposal'] == proposal, 'conflicting_vote')
        else:
            tx.execute('INSERT INTO arbitration_votes(case_id,round,arbitrator,body) VALUES (?,?,?,?)',
                       (case['id'],case['round'],actor,canonical(record).decode()), write=True)
        votes = [loads(r[0]) for r in tx.rows('SELECT body FROM arbitration_votes WHERE case_id=? AND round=?',
                                             (case['id'],case['round']))]
        matching = []
        for cast in votes:
            if cast['proposal'] == proposal:
                await verify_vote(app, tx, case, cast)
                matching.append(cast)
        quorum = case['policy']['policy']['quorum']
        decision = None
        if len(matching) >= quorum:
            voters = sorted(matching, key=lambda v:v['arbitrator'])[:quorum]
            delay = (case['policy']['policy']['appeal_seconds']
                if case['round']==0 and case['policy']['policy']['allow_appeal'] else 0)
            decision = {'id': 'dec_'+digest(proposal)[7:39], 'case_id': case['id'],
                'order_id': order['id'], 'round': case['round'], 'proposal': proposal,
                'refund_minor': proposal['refund_minor'], 'policy_digest': proposal['policy_digest'],
                'rationale_ref': proposal['rationale_ref'], 'rationale_digest': proposal['rationale_digest'],
                'voters': [v['arbitrator'] for v in voters], 'expires_at': proposal['expires_at'],
                'not_before': wire(max(parse_time(v['cast_at']) for v in voters)+timedelta(seconds=delay))}
            tx.execute('INSERT INTO arbitration_decisions(id,case_id,round,body) VALUES (?,?,?,?)',
                       (decision['id'],case['id'],case['round'],canonical(decision).decode()), write=True)
            tx.execute("UPDATE arbitration_cases SET state='decided',deadline=? WHERE id=?",
                       (decision['not_before'],case['id']), write=True)
            notice(tx, case, 'decided', ctx.now)
        return HandlerOutput(data={'case_id': case['id'], 'votes_for_proposal': len(matching), 'decision': decision})

    @op('orders.dispute_execute', obj({'case_id': IDENTIFIER,'decision_id': IDENTIFIER},
        ('case_id','decision_id')), signature=True)
    async def execute_decision(ctx, request, tx):
        actor = _subject(ctx)
        case = await owned_case(tx, ctx, request.arguments['case_id'])
        receipts = await execute(app, tx, case, request.arguments['decision_id'], ctx.now, actor, request.request_id)
        return HandlerOutput(data={'case_id': case['id'],'state':'executed','receipts':receipts})

    @op('orders.dispute_appeal', CASE, signature=True)
    async def appeal(ctx, request, tx):
        _subject(ctx)
        case = await owned_case(tx, ctx, request.arguments['case_id'], parties=True)
        policy = case['policy']['policy']
        require(policy['allow_appeal'] and case['round']==0 and case['state'] in {'decided','held'},
                'appeal_not_available')
        row = tx.one('SELECT body FROM arbitration_decisions WHERE case_id=? AND round=0', (case['id'],))
        until = (parse_time(loads(row[0])['not_before']) if row else
                 parse_time(case['round_deadlines']['0'])+timedelta(seconds=policy['appeal_seconds']))
        require(ctx.now < until, 'appeal_window_closed')
        case['panels']['1'] = await panel_for(tx, case, 1)
        case['round'] = 1
        deadline = wire(ctx.now+timedelta(seconds=policy['case_timeout_seconds']))
        case['round_deadlines']['1'] = deadline
        state = 'open' if await panel_valid(tx, case) else 'held'
        tx.execute('UPDATE arbitration_cases SET round=1,state=?,deadline=?,body=? WHERE id=?',
                   (state,deadline,canonical(case).decode(),case['id']), write=True)
        notice(tx, case, 'appealed', ctx.now)
        return HandlerOutput(data={'case_id':case['id'],'round':1,'state':state,'panel':case['panels']['1']})

    @op('orders.dispute_revoke', obj({'case_id': IDENTIFIER,'decision_id': IDENTIFIER},
        ('case_id','decision_id')), signature=True)
    async def revoke_decision(ctx, request, tx):
        actor = _subject(ctx)
        case = await owned_case(tx, ctx, request.arguments['case_id'])
        row = tx.one('SELECT body FROM arbitration_decisions WHERE id=? AND case_id=? AND round=?',
                     (request.arguments['decision_id'],case['id'],case['round']))
        require(row is not None and actor in loads(row[0])['voters'], 'decision_signer_required')
        require(case['state'] in {'decided','held'}, 'decision_already_executed')
        tx.execute('UPDATE arbitration_decisions SET revoked=TRUE WHERE id=?',
                   (request.arguments['decision_id'],), write=True)
        tx.execute("UPDATE arbitration_cases SET state='held' WHERE id=?", (case['id'],), write=True)
        notice(tx, case, 'held', ctx.now)
        return HandlerOutput(data={'case_id':case['id'],'state':'held','decision_revoked':True})

    @op('orders.arbitrator_conflict', obj({'party': IDENTIFIER}, ('party',)), signature=True)
    async def declare_conflict(ctx, request, tx):
        actor = _subject(ctx)
        role = tx.one('SELECT active FROM arbitrator_roles WHERE subject=?', (actor,))
        require(role is not None and role[0], 'arbitrator_required')
        party = (await tx.subject(request.arguments['party'])).resource_id
        tx.execute('INSERT INTO arbitrator_conflicts(arbitrator,party) VALUES (?,?) ON CONFLICT DO NOTHING',
                   (actor,party), write=True)
        return HandlerOutput(data={'declared':True})
