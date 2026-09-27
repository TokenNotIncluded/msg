"""Single-currency, append-only clearing for signed subject transfers.

The PostgreSQL ledger is authoritative. There is deliberately no public issuer
operation: root funding and bank administration require a separate local-console
entry point, and are not reachable through this plugin.
"""
from __future__ import annotations

from uuid import uuid4

from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, digest, loads, wire
from msg.core.errors import require
from msg.core.models import HandlerOutput
from msg.plugins.common import registration
from msg.plugins.schemas import IDENTIFIER, obj


CURRENCY_ID = 'primary'
CODE = 'MSG'
SCALE = 6
MAX_MINOR = 2**63 - 1
POLICY_VERSION = 1
POLICY_DIGEST = digest({'version': POLICY_VERSION, 'currency_id': CURRENCY_ID,
                        'scale': SCALE, 'transfer_fee': 0, 'allow_overdraft': False})


def _owner(ctx):
    require(ctx.principal.subject is not None and
            ctx.principal.actor == ctx.principal.subject, 'money_subject_required')
    return ctx.principal.subject


def _amount(value):
    require(type(value) is int and 0 < value <= MAX_MINOR, 'invalid_money_amount')
    return value


def _balance(tx, subject):
    debit = tx.one("SELECT COALESCE(SUM(amount_minor),0) FROM money_ledger WHERE debit_account=?",
                   (subject,))[0]
    credit = tx.one("SELECT COALESCE(SUM(amount_minor),0) FROM money_ledger WHERE credit_account=?",
                    (subject,))[0]
    return int(credit) - int(debit)


def _supply(tx):
    issued = tx.one("SELECT COALESCE(SUM(amount_minor),0) FROM money_ledger WHERE kind='mint'")[0]
    burned = tx.one("SELECT COALESCE(SUM(amount_minor),0) FROM money_ledger WHERE kind='burn'")[0]
    return int(issued) - int(burned)


def _post_transfer(tx, *, sender, recipient, amount, actor, request_id, now,
                   receipt_signer, reference=None, kind='transfer', escrow_authority=None):
    """Called only within the executor's serialized write transaction."""
    require(kind in {'transfer', 'refund'}, 'invalid_money_kind')
    amount = _amount(amount)
    account = tx.one('SELECT kind FROM ledger_accounts WHERE id=?', (sender,))
    if account and account[0] == 'order_escrow':
        from msg.market.escrow import _ESCROW_WRITE
        require(escrow_authority is _ESCROW_WRITE, 'escrow_release_forbidden')
    require(sender != recipient, 'self_transfer_forbidden')
    require(sender not in {ROOT_SUBJECT,'@root'}, 'root_local_only')
    require(_balance(tx, sender) >= amount, 'insufficient_funds')
    require(_balance(tx, recipient) + amount <= MAX_MINOR, 'money_overflow')
    tx.execute('INSERT INTO money_accounts(subject_id,currency_id) VALUES (?,?) '
               'ON CONFLICT(subject_id,currency_id) DO NOTHING', (sender,CURRENCY_ID), write=True)
    tx.execute('INSERT INTO money_accounts(subject_id,currency_id) VALUES (?,?) '
               'ON CONFLICT(subject_id,currency_id) DO NOTHING', (recipient,CURRENCY_ID), write=True)
    transaction_id = 'lt_' + uuid4().hex
    sequence = tx.one("SELECT nextval(pg_get_serial_sequence('money_ledger','seq'))")[0]
    body = {'transaction_id': transaction_id, 'kind': kind, 'currency_id': CURRENCY_ID,
            'amount_minor': amount, 'from_subject': sender, 'to_subject': recipient,
            'committed_at': wire(now), 'policy_version': POLICY_VERSION,
            'policy_digest': POLICY_DIGEST, 'ledger_sequence': sequence}
    receipt = {'body':body, 'signature':wire(receipt_signer.sign(canonical(body),purpose='money-receipt'))}
    tx.execute('''INSERT INTO money_ledger
        (seq,id,kind,currency_id,amount_minor,debit_account,credit_account,actor,request_id,
         reference,committed_at,policy_version,policy_digest,receipt)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (sequence,transaction_id,kind,CURRENCY_ID,amount,sender,recipient,actor,
         request_id,reference,wire(now),POLICY_VERSION,POLICY_DIGEST,
         canonical(receipt).decode()),write=True)
    return receipt


def install(app):
    op, finish = registration(app, 'money', ('identity',))
    amount_schema = {'type':'integer','minimum':1,'maximum':MAX_MINOR}

    @op('money.state', obj(), effect='read')
    async def state(ctx, request, tx):
        return HandlerOutput(data={'currency_id': CURRENCY_ID,
                                   'display_name': app.settings.money.display_name,
                                   'code': app.settings.money.code, 'scale': SCALE,
                                   'total_supply_minor': _supply(tx)})

    @op('money.banks', obj(), effect='read')
    async def banks(ctx, request, tx):
        rows = tx.rows('SELECT subject_id,status,granted_at,revoked_at '
                       'FROM money_bank_roles ORDER BY subject_id')
        return HandlerOutput(data={'banks':[{'subject_id':r[0], 'status':r[1],
                                             'granted_at':r[2], 'revoked_at':r[3]}
                                            for r in rows]})

    @op('money.balance', obj(), effect='read')
    async def balance(ctx, request, tx):
        subject = _owner(ctx)
        return HandlerOutput(data={'subject_id': subject, 'currency_id': CURRENCY_ID,
                                   'balance_minor': _balance(tx, subject)})

    @op('money.ledger', obj({'cursor': {'type':'integer','minimum':0},
                             'limit': {'type':'integer','minimum':1,'maximum':100}}), effect='read')
    async def ledger(ctx, request, tx):
        subject = _owner(ctx)
        cursor = request.arguments.get('cursor', 0)
        limit = request.arguments.get('limit', 50)
        rows = tx.rows('''SELECT seq,id,kind,amount_minor,debit_account,credit_account,
                              committed_at,policy_version,policy_digest,reference,receipt
                         FROM money_ledger WHERE seq>? AND
                              (debit_account=? OR credit_account=?) ORDER BY seq LIMIT ?''',
                       (cursor, subject, subject, limit+1))
        items = [{'ledger_sequence':r[0], 'transaction_id':r[1], 'kind':r[2],
                  'amount_minor':r[3], 'direction':'out' if r[4]==subject else 'in',
                  'counterparty':r[5] if r[4]==subject else r[4],
                  'committed_at':r[6], 'policy_version':r[7],
                  'policy_digest':r[8], 'reference':r[9], 'receipt':loads(r[10])}
                 for r in rows[:limit]]
        return HandlerOutput(data={'currency_id':CURRENCY_ID, 'items':items,
                                   'next_cursor':rows[limit-1][0] if len(rows)>limit else None})

    @op('money.transfer', obj({'from_subject': IDENTIFIER, 'to_subject': IDENTIFIER,
                               'currency_id': {'const':CURRENCY_ID},
                               'amount_minor': amount_schema,
                               'reference': {'type':'string','maxLength':160}},
                              ('to_subject','currency_id','amount_minor')),
        signature=True)
    async def transfer(ctx, request, tx):
        args = request.arguments
        require(args.get('from_subject') not in {ROOT_SUBJECT,'@root'}, 'root_local_only')
        sender = _owner(ctx)
        require(args.get('from_subject',sender)==sender, 'money_sender_mismatch')
        source = await tx.subject(sender)
        destination = await tx.subject(args['to_subject'])
        require(source.kind in {'registered','custodial'} and not source.local_only,
                'money_subject_required')
        require(destination.kind in {'registered','custodial'} and not destination.local_only,
                'invalid_money_recipient')
        receipt = _post_transfer(tx, sender=sender, recipient=destination.resource_id,
                                 amount=args['amount_minor'], actor=ctx.principal.actor,
                                 request_id=request.request_id, now=ctx.now,
                                 receipt_signer=app.receipt_signer,
                                 reference=args.get('reference'))
        return HandlerOutput(data={'transfer':receipt,
                                   'balance_minor':_balance(tx,sender)})

    finish()
