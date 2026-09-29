"""Prefunded bounties with one-use identity-key proof of possession.

All account movements run inside the executor's PostgreSQL write transaction.
An escrow LedgerAccount is not a Subject and has no credential or public transfer operation.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import timedelta

from msg.core.codec import b64, canonical, decode, digest, loads, parse_time, unb64, wire
from msg.core.errors import require
from msg.core.models import HandlerOutput, ResourceRef, Signature
from msg.plugins.common import check_access, create_resource, new_id, operation_id, registration
from msg.plugins.money import CURRENCY_ID, MAX_MINOR, _balance, _post_transfer, account_requirements
from msg.plugins.schemas import IDENTIFIER, SIGNATURE, obj
from msg.security.crypto import verify

VERIFIER_ID = 'signature_pop_v1'
VERIFIER_VERSION = 1
CHALLENGE_TTL = 300


def _subject(ctx):
    require(
        ctx.principal.subject is not None
        and ctx.principal.actor == ctx.principal.subject
        and ctx.principal.method == 'signature',
        'signature_required',
    )
    return ctx.principal.subject


def _eligibility(value, subject):
    if value == {'kind': 'any'}:
        return True
    # Unknown constraints would be published but never enforced; reject them.
    require(
        isinstance(value, Mapping)
        and set(value) == {'kind', 'subjects'}
        and value.get('kind') == 'allowlist'
        and isinstance(value.get('subjects'), (list, tuple))
        and len(value['subjects']) <= 1000
        and all(isinstance(s, str) for s in value['subjects']),
        'invalid_bounty_eligibility',
    )
    return subject in value['subjects']


def _row(tx, listing_id):
    row = tx.one(
        """SELECT listing_id,publisher,escrow_subject,reward_minor,budget_minor,
        max_claims,claim_limit_per_subject,verifier_id,verifier_version,eligibility,
        state,pause_reason,expires_at,created_at FROM bounty_listings WHERE listing_id=?""",
        (listing_id,),
    )
    require(row is not None, 'bounty_not_found')
    return dict(
        zip(
            (
                'listing_id',
                'publisher',
                'escrow_subject',
                'reward_minor',
                'budget_minor',
                'max_claims',
                'claim_limit_per_subject',
                'verifier_id',
                'verifier_version',
                'eligibility',
                'state',
                'pause_reason',
                'expires_at',
                'created_at',
            ),
            (*row[:9], loads(row[9]), *row[10:]),
            strict=True,
        )
    )


def _public(row, balance, claims, now=None):
    # Derive read projections without writing lifecycle state during GET.
    row = dict(row)
    if row['state'] != 'closed':
        if (
            now is not None
            and row['expires_at'] is not None
            and parse_time(row['expires_at']) <= now
        ):
            row.update(state='paused', pause_reason='expired')
        elif balance < row['reward_minor']:
            row.update(state='paused', pause_reason='out_of_budget')
        elif claims >= row['max_claims']:
            row.update(state='paused', pause_reason='claims_exhausted')
    return {
        key: row[key]
        for key in (
            'listing_id',
            'publisher',
            'reward_minor',
            'budget_minor',
            'max_claims',
            'claim_limit_per_subject',
            'verifier_id',
            'verifier_version',
            'eligibility',
            'state',
            'pause_reason',
            'expires_at',
            'created_at',
        )
    } | {'escrow_balance_minor': balance, 'paid_claims': claims}


def _claim_count(tx, listing_id, claimant=None):
    if claimant is None:
        return int(
            tx.one(
                "SELECT COUNT(*) FROM bounty_claims WHERE listing_id=? AND status='paid'",
                (listing_id,),
            )[0]
        )
    return int(
        tx.one(
            "SELECT COUNT(*) FROM bounty_claims WHERE listing_id=? AND claimant=? AND status='paid'",
            (listing_id, claimant),
        )[0]
    )


async def validate_contract(app, tx, row):
    """Mutable accounting state cannot change a publisher's immutable terms."""
    from msg.market.catalog import read_listing_body

    resource = await tx.resource(row['listing_id'])
    body, _ = await read_listing_body(app, tx, resource)
    require(
        resource.type == 'listing'
        and resource.owner == row['publisher']
        and body.get('mode') == 'bounty'
        and body.get('currency_id') == CURRENCY_ID
        and all(
            body.get(name) == row[name]
            for name in (
                'reward_minor',
                'max_claims',
                'claim_limit_per_subject',
                'verifier_id',
                'verifier_version',
                'eligibility',
                'expires_at',
            )
        ),
        'bounty_contract_mismatch',
    )
    # Budget and state intentionally differ after funding, claims or pause.
    # Their validity is checked against the append-only ledger separately.


async def projection(app, tx, listing_id, now):
    row = _row(tx, listing_id)
    await validate_contract(app, tx, row)
    return _public(row, _balance(tx, row['escrow_subject']), _claim_count(tx, listing_id), now)


def _ready(tx, row, now, claimant):
    require(row['state'] == 'active', 'bounty_not_active')
    require(row['expires_at'] is None or parse_time(row['expires_at']) > now, 'bounty_expired')
    require(
        row['verifier_id'] == VERIFIER_ID and row['verifier_version'] == VERIFIER_VERSION,
        'bounty_verifier_unsupported',
    )
    require(_eligibility(row['eligibility'], claimant), 'bounty_ineligible')
    require(_claim_count(tx, row['listing_id']) < row['max_claims'], 'bounty_claims_exhausted')
    require(
        _claim_count(tx, row['listing_id'], claimant) < row['claim_limit_per_subject'],
        'bounty_subject_limit',
    )
    require(_balance(tx, row['escrow_subject']) >= row['reward_minor'], 'bounty_out_of_budget')


def install(app):
    op, finish = registration(app, 'bounty', ('store', 'money'))
    amount = {'type': 'integer', 'minimum': 1, 'maximum': MAX_MINOR}

    @op(
        'bounty.create',
        obj(
            {
                'name': {'type': 'string', 'minLength': 1, 'maxLength': 120},
                'terms': {'type': 'string', 'minLength': 1, 'maxLength': 16384},
                'reward_minor': amount,
                'budget_minor': amount,
                'max_claims': {'type': 'integer', 'minimum': 1, 'maximum': 1000000},
                'claim_limit_per_subject': {'type': 'integer', 'minimum': 1, 'maximum': 1000},
                'eligibility': {'type': 'object'},
                'expires_at': {'type': ['string', 'null']},
                'currency_id': {'const': CURRENCY_ID},
                'verifier_id': {'const': VERIFIER_ID},
                'verifier_version': {'const': VERIFIER_VERSION},
            },
            ('name', 'terms', 'reward_minor', 'budget_minor', 'max_claims'),
        ),
        signature=True,
        requirements=account_requirements,
    )
    async def create(ctx, request, tx):
        publisher = _subject(ctx)
        await app.authorizer.require_base(ctx.principal, operation_id(request), publisher, tx)
        await check_access(app, ctx, request, tx, 't_store', 'create')
        args = request.arguments
        require(args.get('currency_id', CURRENCY_ID) == CURRENCY_ID, 'unsupported_currency')
        require(
            args.get('verifier_id', VERIFIER_ID) == VERIFIER_ID
            and args.get('verifier_version', VERIFIER_VERSION) == VERIFIER_VERSION,
            'bounty_verifier_unsupported',
        )
        eligibility = args.get('eligibility', {'kind': 'any'})
        _eligibility(eligibility, publisher)
        expires_at = args.get('expires_at')
        if expires_at is not None:
            require(parse_time(expires_at) > ctx.now, 'bounty_expired')
        reward, budget = args['reward_minor'], args['budget_minor']
        require(budget <= MAX_MINOR and reward <= MAX_MINOR, 'invalid_money_amount')
        require(_balance(tx, publisher) >= budget, 'insufficient_funds')
        owner = await tx.subject(publisher)
        require(
            owner.kind in {'registered', 'custodial'} and not owner.local_only,
            'money_subject_required',
        )
        listing_id = new_id('bty')
        escrow = new_id('esc')
        # The prefunded escrow is an account, not a signable Subject.
        tx.execute(
            """INSERT INTO ledger_accounts(id,kind,subject_id,source_id)
            VALUES (?,'bounty_escrow',NULL,?)""",
            (escrow, listing_id),
            write=True,
        )
        body = {
            'mode': 'bounty',
            'terms': args['terms'],
            'reward_minor': reward,
            'currency_id': CURRENCY_ID,
            'budget_minor': budget,
            'max_claims': args['max_claims'],
            'claim_limit_per_subject': args.get('claim_limit_per_subject', 1),
            'verifier_id': VERIFIER_ID,
            'verifier_version': VERIFIER_VERSION,
            'eligibility': eligibility,
            'expires_at': expires_at,
            'state': 'active' if budget >= reward else 'paused',
        }
        resource = await create_resource(
            app,
            ctx,
            request,
            tx,
            parent='t_store',
            type='listing',
            name=args['name'],
            resource_id=listing_id,
            body=canonical(body),
            media_type='application/json',
            mode=0o644,
        )
        state = body['state']
        tx.execute(
            """INSERT INTO bounty_listings
            (listing_id,publisher,escrow_subject,reward_minor,budget_minor,max_claims,
             claim_limit_per_subject,verifier_id,verifier_version,eligibility,state,
             pause_reason,expires_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                listing_id,
                publisher,
                escrow,
                reward,
                budget,
                args['max_claims'],
                body['claim_limit_per_subject'],
                VERIFIER_ID,
                VERIFIER_VERSION,
                canonical(eligibility).decode(),
                state,
                'out_of_budget' if state == 'paused' else None,
                expires_at,
                wire(ctx.now),
            ),
            write=True,
        )
        funding = _post_transfer(
            tx,
            sender=publisher,
            recipient=escrow,
            amount=budget,
            actor=publisher,
            request_id=request.request_id,
            now=ctx.now,
            receipt_signer=app.receipt_signer,
            reference='bounty_fund:' + listing_id,
        )
        return HandlerOutput(
            resources=(ResourceRef(id=listing_id, revision=resource.revision),),
            data={'bounty': _public(_row(tx, listing_id), budget, 0), 'funding': funding},
        )

    @op('bounty.get', obj({'listing_id': IDENTIFIER}, ('listing_id',)), effect='read')
    async def get(ctx, request, tx):
        row = _row(tx, request.arguments['listing_id'])
        await check_access(app, ctx, request, tx, row['listing_id'], 'read')
        return HandlerOutput(data={'bounty': await projection(app, tx, row['listing_id'], ctx.now)})

    @op(
        'bounty.top_up',
        obj({'listing_id': IDENTIFIER, 'amount_minor': amount}, ('listing_id', 'amount_minor')),
        signature=True,
        requirements=account_requirements,
    )
    async def top_up(ctx, request, tx):
        publisher = _subject(ctx)
        row = _row(tx, request.arguments['listing_id'])
        require(row['publisher'] == publisher, 'bounty_not_found')
        await check_access(app, ctx, request, tx, row['listing_id'], 'write')
        await validate_contract(app, tx, row)
        require(row['state'] != 'closed', 'bounty_closed')
        require(
            row['expires_at'] is None or parse_time(row['expires_at']) > ctx.now, 'bounty_expired'
        )
        amount = request.arguments['amount_minor']
        require(row['budget_minor'] + amount <= MAX_MINOR, 'money_overflow')
        receipt = _post_transfer(
            tx,
            sender=publisher,
            recipient=row['escrow_subject'],
            amount=amount,
            actor=publisher,
            request_id=request.request_id,
            now=ctx.now,
            receipt_signer=app.receipt_signer,
            reference='bounty_top_up:' + row['listing_id'],
        )
        balance = _balance(tx, row['escrow_subject'])
        state = (
            'active'
            if (
                row['state'] == 'paused'
                and row['pause_reason'] == 'out_of_budget'
                and balance >= row['reward_minor']
                and _claim_count(tx, row['listing_id']) < row['max_claims']
            )
            else row['state']
        )
        tx.execute(
            """UPDATE bounty_listings SET budget_minor=?,state=?,pause_reason=?
            WHERE listing_id=?""",
            (
                row['budget_minor'] + amount,
                state,
                None if state == 'active' else row['pause_reason'],
                row['listing_id'],
            ),
            write=True,
        )
        return HandlerOutput(
            data={
                'bounty': _public(
                    _row(tx, row['listing_id']), balance, _claim_count(tx, row['listing_id'])
                ),
                'funding': receipt,
            }
        )

    @op(
        'bounty.challenge',
        obj({'listing_id': IDENTIFIER}, ('listing_id',)),
        signature=True,
        requirements=account_requirements,
    )
    async def challenge(ctx, request, tx):
        claimant = _subject(ctx)
        row = _row(tx, request.arguments['listing_id'])
        await check_access(app, ctx, request, tx, row['listing_id'], 'read')
        _ready(tx, row, ctx.now, claimant)
        await validate_contract(app, tx, row)
        key = tx.one(
            """SELECT key_id FROM identity_keys WHERE subject=? AND
            is_primary=1 AND retired_at IS NULL""",
            (claimant,),
        )
        require(
            key is not None and key[0] == ctx.principal.credential_id,
            'current_identity_key_required',
        )
        payload = {
            'challenge_id': new_id('bc'),
            'listing_id': row['listing_id'],
            'claimant_subject_id': claimant,
            'nonce': b64(os.urandom(24)),
            'issued_at': wire(ctx.now),
            'expires_at': wire(ctx.now + timedelta(seconds=CHALLENGE_TTL)),
            'verifier_version': VERIFIER_VERSION,
        }
        tx.execute(
            """INSERT INTO bounty_challenges
            (id,listing_id,claimant,key_id,nonce,issued_at,expires_at,
             verifier_version,payload,consumed_at) VALUES (?,?,?,?,?,?,?,?,?,NULL)""",
            (
                payload['challenge_id'],
                row['listing_id'],
                claimant,
                key[0],
                payload['nonce'],
                payload['issued_at'],
                payload['expires_at'],
                VERIFIER_VERSION,
                canonical(payload).decode(),
            ),
            write=True,
        )
        return HandlerOutput(data={'challenge': payload})

    @op(
        'bounty.close',
        obj({'listing_id': IDENTIFIER}, ('listing_id',)),
        signature=True,
        requirements=account_requirements,
    )
    async def close(ctx, request, tx):
        publisher = _subject(ctx)
        row = _row(tx, request.arguments['listing_id'])
        require(row['publisher'] == publisher, 'bounty_not_found')
        await check_access(app, ctx, request, tx, row['listing_id'], 'write')
        await validate_contract(app, tx, row)
        require(row['state'] != 'closed', 'bounty_closed')
        remaining = _balance(tx, row['escrow_subject'])
        refund = None
        if remaining:
            refund = _post_transfer(
                tx,
                sender=row['escrow_subject'],
                recipient=publisher,
                amount=remaining,
                actor=publisher,
                request_id=request.request_id,
                now=ctx.now,
                receipt_signer=app.receipt_signer,
                reference='bounty_close_return:' + row['listing_id'],
                kind='refund',
            )
        tx.execute(
            "UPDATE bounty_listings SET state='closed',pause_reason=NULL WHERE listing_id=?",
            (row['listing_id'],),
            write=True,
        )
        return HandlerOutput(
            data={
                'bounty': _public(
                    _row(tx, row['listing_id']), 0, _claim_count(tx, row['listing_id'])
                ),
                'returned': refund,
            }
        )

    @op(
        'bounty.claim',
        obj({'challenge_id': IDENTIFIER, 'proof': SIGNATURE}, ('challenge_id', 'proof')),
        signature=True,
        requirements=account_requirements,
    )
    async def claim(ctx, request, tx):
        claimant = _subject(ctx)
        row = tx.one(
            """SELECT listing_id,claimant,key_id,expires_at,
            verifier_version,payload,consumed_at,nonce,issued_at FROM bounty_challenges WHERE id=?""",
            (request.arguments['challenge_id'],),
        )
        require(row is not None and row[1] == claimant, 'bounty_challenge_not_found')
        listing = _row(tx, row[0])
        await check_access(app, ctx, request, tx, listing['listing_id'], 'read')
        _ready(tx, listing, ctx.now, claimant)
        await validate_contract(app, tx, listing)
        require(row[6] is None, 'bounty_challenge_consumed')
        require(parse_time(row[3]) > ctx.now, 'bounty_challenge_expired')
        require(row[4] == VERIFIER_VERSION, 'bounty_verifier_unsupported')
        current = tx.one(
            """SELECT key_id,public_key FROM identity_keys WHERE subject=?
            AND is_primary=1 AND retired_at IS NULL""",
            (claimant,),
        )
        require(
            current is not None
            and current[0] == row[2]
            and current[0] == ctx.principal.credential_id,
            'current_identity_key_required',
        )
        proof = decode(Signature, request.arguments['proof'])
        require(proof.key_id == current[0], 'bounty_proof_key_mismatch')
        payload = loads(row[5])
        require(
            payload
            == {
                'challenge_id': request.arguments['challenge_id'],
                'listing_id': row[0],
                'claimant_subject_id': claimant,
                'nonce': row[7],
                'issued_at': row[8],
                'expires_at': row[3],
                'verifier_version': row[4],
            },
            'bounty_challenge_mismatch',
        )
        verify(unb64(current[1], limit=32), canonical(payload), proof, purpose='bounty-pop-v1')
        proof_digest = digest({'payload': payload, 'proof': wire(proof)})
        receipt = _post_transfer(
            tx,
            sender=listing['escrow_subject'],
            recipient=claimant,
            amount=listing['reward_minor'],
            actor=claimant,
            request_id=request.request_id,
            now=ctx.now,
            receipt_signer=app.receipt_signer,
            reference='bounty_claim:' + listing['listing_id'],
        )
        transaction_id = receipt['body']['transaction_id']
        claim_id = new_id('bclaim')
        tx.execute(
            """INSERT INTO bounty_claims
            (id,listing_id,claimant,challenge_id,proof_digest,status,reward_minor,
             transaction_id,claimed_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                claim_id,
                listing['listing_id'],
                claimant,
                request.arguments['challenge_id'],
                proof_digest,
                'paid',
                listing['reward_minor'],
                transaction_id,
                wire(ctx.now),
            ),
            write=True,
        )
        consumed = tx.execute(
            """UPDATE bounty_challenges SET consumed_at=? WHERE id=? AND
            consumed_at IS NULL""",
            (wire(ctx.now), request.arguments['challenge_id']),
            write=True,
        )
        require(consumed.rowcount == 1, 'bounty_challenge_consumed')
        balance = _balance(tx, listing['escrow_subject'])
        paid_count = _claim_count(tx, listing['listing_id'])
        reason = (
            'out_of_budget'
            if balance < listing['reward_minor']
            else 'claims_exhausted'
            if paid_count >= listing['max_claims']
            else None
        )
        if reason:
            tx.execute(
                "UPDATE bounty_listings SET state='paused',pause_reason=? WHERE listing_id=?",
                (reason, listing['listing_id']),
                write=True,
            )
        from msg.plugins.communication import event_id

        notice = {
            'id': new_id('message'),
            'sender': None,
            'actor': claimant,
            'recipient': claimant,
            'resource': wire(ResourceRef(id=listing['listing_id'])),
            'time': wire(ctx.now),
            'state': 'delivered',
            'source': 'bounty_claim',
            'listing_id': listing['listing_id'],
            'claim_id': claim_id,
            'transaction_id': transaction_id,
        }
        tx.execute(
            'INSERT INTO messages VALUES (?,?,?,?,?,?)',
            (
                notice['id'],
                None,
                claimant,
                listing['listing_id'],
                event_id(request, claimant),
                canonical(notice).decode(),
            ),
            write=True,
        )
        return HandlerOutput(
            resources=(ResourceRef(id=listing['listing_id']),),
            data={
                'claim': {
                    'id': claim_id,
                    'listing_id': listing['listing_id'],
                    'claimant': claimant,
                    'challenge_id': request.arguments['challenge_id'],
                    'proof_digest': proof_digest,
                    'status': 'paid',
                    'reward_minor': listing['reward_minor'],
                    'transaction_id': transaction_id,
                    'claimed_at': wire(ctx.now),
                },
                'reward': receipt,
                'escrow_balance_minor': balance,
            },
        )

    @op(
        'bounty.pause',
        obj({'listing_id': IDENTIFIER}, ('listing_id',)),
        signature=True,
        requirements=account_requirements,
    )
    async def pause(ctx, request, tx):
        row = _row(tx, request.arguments['listing_id'])
        require(row['publisher'] == _subject(ctx), 'bounty_not_found')
        await check_access(app, ctx, request, tx, row['listing_id'], 'write')
        await validate_contract(app, tx, row)
        require(row['state'] != 'closed', 'bounty_closed')
        tx.execute(
            "UPDATE bounty_listings SET state='paused',pause_reason='publisher_paused' WHERE listing_id=?",
            (row['listing_id'],),
            write=True,
        )
        return HandlerOutput(data={'bounty': await projection(app, tx, row['listing_id'], ctx.now)})

    @op(
        'bounty.resume',
        obj({'listing_id': IDENTIFIER}, ('listing_id',)),
        signature=True,
        requirements=account_requirements,
    )
    async def resume(ctx, request, tx):
        row = _row(tx, request.arguments['listing_id'])
        require(row['publisher'] == _subject(ctx), 'bounty_not_found')
        await check_access(app, ctx, request, tx, row['listing_id'], 'write')
        await validate_contract(app, tx, row)
        require(row['state'] != 'closed', 'bounty_closed')
        require(
            row['expires_at'] is None or parse_time(row['expires_at']) > ctx.now, 'bounty_expired'
        )
        require(_claim_count(tx, row['listing_id']) < row['max_claims'], 'bounty_claims_exhausted')
        require(_balance(tx, row['escrow_subject']) >= row['reward_minor'], 'bounty_out_of_budget')
        tx.execute(
            "UPDATE bounty_listings SET state='active',pause_reason=NULL WHERE listing_id=?",
            (row['listing_id'],),
            write=True,
        )
        return HandlerOutput(data={'bounty': await projection(app, tx, row['listing_id'], ctx.now)})

    @op(
        'bounty.claims',
        obj(
            {
                'listing_id': IDENTIFIER,
                'cursor': IDENTIFIER,
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
            },
            ('listing_id',),
        ),
        effect='read',
        requirements=account_requirements,
    )
    async def claims(ctx, request, tx):
        from msg.plugins.money import _owner

        subject = _owner(ctx)
        row = _row(tx, request.arguments['listing_id'])
        await check_access(app, ctx, request, tx, row['listing_id'], 'read')
        await validate_contract(app, tx, row)
        args = request.arguments
        limit = args.get('limit', 50)
        rows = tx.rows(
            """SELECT id,claimant,challenge_id,proof_digest,status,reward_minor,
            transaction_id,claimed_at FROM bounty_claims WHERE listing_id=? AND
            (claimant=? OR ?=?) AND id>? ORDER BY id LIMIT ?""",
            (
                row['listing_id'],
                subject,
                subject,
                row['publisher'],
                args.get('cursor', ''),
                limit + 1,
            ),
        )
        items = [
            dict(
                zip(
                    (
                        'id',
                        'claimant',
                        'challenge_id',
                        'proof_digest',
                        'status',
                        'reward_minor',
                        'transaction_id',
                        'claimed_at',
                    ),
                    r,
                    strict=True,
                )
            )
            for r in rows[:limit]
        ]
        return HandlerOutput(
            data={'claims': items, 'next_cursor': items[-1]['id'] if len(rows) > limit else None}
        )

    finish()
