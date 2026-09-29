"""Single-currency, append-only clearing for signed subject transfers.

The PostgreSQL ledger is authoritative. There is deliberately no public issuer
operation: root funding and bank administration require a separate local-console
entry point, and are not reachable through this plugin.
"""

from __future__ import annotations

from msg.constants import ROOT_SUBJECT
from msg.core.codec import loads
from msg.core.errors import require
from msg.core.models import HandlerOutput

# Compatibility imports; shared implementation has one market owner.
from msg.market.ledger import (
    CODE as CODE,
    CURRENCY_ID as CURRENCY_ID,
    MAX_MINOR as MAX_MINOR,
    POLICY_DIGEST as POLICY_DIGEST,
    POLICY_VERSION as POLICY_VERSION,
    SCALE as SCALE,
    account_requirements as account_requirements,
    append_entry as _post_entry,
    balance as _balance,
    checked_amount as _amount,
    clearing_decision as clearing_decision,
    post_transfer as _post_transfer,
    require_money_subject as _owner,
    total_supply as _supply,
)
from msg.plugins.common import registration
from msg.plugins.schemas import IDENTIFIER, obj

__all__ = [
    'CODE',
    'CURRENCY_ID',
    'MAX_MINOR',
    'POLICY_DIGEST',
    'POLICY_VERSION',
    'SCALE',
    '_amount',
    '_balance',
    '_owner',
    '_post_entry',
    '_post_transfer',
    '_supply',
    'account_requirements',
    'clearing_decision',
    'install',
]


def install(app):
    op, finish = registration(app, 'money', ('identity',))
    amount_schema = {'type': 'integer', 'minimum': 1, 'maximum': MAX_MINOR}

    @op('money.state', obj(), effect='read')
    async def state(ctx, request, tx):
        return HandlerOutput(
            data={
                'currency_id': CURRENCY_ID,
                'display_name': app.settings.money.display_name,
                'code': app.settings.money.code,
                'scale': SCALE,
                'total_supply_minor': _supply(tx),
            }
        )

    @op('money.banks', obj(), effect='read')
    async def banks(ctx, request, tx):
        rows = tx.rows(
            'SELECT subject_id,status,granted_at,revoked_at '
            'FROM money_bank_roles ORDER BY subject_id'
        )
        return HandlerOutput(
            data={
                'banks': [
                    {'subject_id': r[0], 'status': r[1], 'granted_at': r[2], 'revoked_at': r[3]}
                    for r in rows
                ]
            }
        )

    @op('money.balance', obj(), effect='read', requirements=account_requirements)
    async def balance(ctx, request, tx):
        subject = _owner(ctx)
        return HandlerOutput(
            data={
                'subject_id': subject,
                'currency_id': CURRENCY_ID,
                'balance_minor': _balance(tx, subject),
            }
        )

    @op(
        'money.ledger',
        obj({
            'cursor': {'type': 'integer', 'minimum': 0},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
        }),
        effect='read',
        requirements=account_requirements,
    )
    async def ledger(ctx, request, tx):
        subject = _owner(ctx)
        cursor = request.arguments.get('cursor', 0)
        limit = request.arguments.get('limit', 50)
        rows = tx.rows(
            """SELECT seq,id,kind,amount_minor,debit_account,credit_account,
                              committed_at,policy_version,policy_digest,reference,receipt
                         FROM money_ledger WHERE seq>? AND
                              (debit_account=? OR credit_account=?) ORDER BY seq LIMIT ?""",
            (cursor, subject, subject, limit + 1),
        )
        items = [
            {
                'ledger_sequence': r[0],
                'transaction_id': r[1],
                'kind': r[2],
                'amount_minor': r[3],
                'direction': 'out' if r[4] == subject else 'in',
                'counterparty': r[5] if r[4] == subject else r[4],
                'committed_at': r[6],
                'policy_version': r[7],
                'policy_digest': r[8],
                'reference': r[9],
                'receipt': loads(r[10]),
            }
            for r in rows[:limit]
        ]
        return HandlerOutput(
            data={
                'currency_id': CURRENCY_ID,
                'items': items,
                'next_cursor': rows[limit - 1][0] if len(rows) > limit else None,
            }
        )

    @op(
        'money.transfer',
        obj(
            {
                'from_subject': IDENTIFIER,
                'to_subject': IDENTIFIER,
                'currency_id': {'const': CURRENCY_ID},
                'amount_minor': amount_schema,
                'reference': {'type': 'string', 'maxLength': 160},
            },
            ('to_subject', 'currency_id', 'amount_minor'),
        ),
        signature=True,
        requirements=account_requirements,
    )
    async def transfer(ctx, request, tx):
        args = request.arguments
        require(args.get('from_subject') not in {ROOT_SUBJECT, '@root'}, 'root_local_only')
        sender = _owner(ctx)
        require(args.get('from_subject', sender) == sender, 'money_sender_mismatch')
        source = await tx.subject(sender)
        destination = await tx.subject(args['to_subject'])
        require(
            source.kind in {'registered', 'custodial'} and not source.local_only,
            'money_subject_required',
        )
        require(
            destination.kind in {'registered', 'custodial'} and not destination.local_only,
            'invalid_money_recipient',
        )
        receipt = _post_transfer(
            tx,
            sender=sender,
            recipient=destination.resource_id,
            amount=args['amount_minor'],
            actor=ctx.principal.actor,
            request_id=request.request_id,
            now=ctx.now,
            receipt_signer=app.receipt_signer,
            reference=args.get('reference'),
        )
        return HandlerOutput(data={'transfer': receipt, 'balance_minor': _balance(tx, sender)})

    finish()
