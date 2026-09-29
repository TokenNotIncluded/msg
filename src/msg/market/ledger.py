"""One append-only ledger posting path in the caller's transaction.

This module grants no commit rights. Public transfers cannot supply the internal
escrow guard; Root issuance remains a separate local-console use case.
"""
from __future__ import annotations
from uuid import uuid4
from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, digest, wire
from msg.core.errors import require
from msg.core.models import AccessRequirement, SignatureProof

CURRENCY_ID = 'primary'
CODE = 'MSG'
SCALE = 6
MAX_MINOR = 2**63 - 1
POLICY_VERSION = 2
POLICY_DIGEST = digest({'version': POLICY_VERSION, 'currency_id': CURRENCY_ID,
                        'scale': SCALE, 'transfer_fee': 0, 'allow_overdraft': False,
                        'posting_identity': 'actor/request_id/entry_key'})

# Not a credential, account or public capability. Only trusted in-process market
# code can reach this token; ordinary signed money.transfer never receives it.
_ESCROW_WRITE = object()


async def account_requirements(request, tx):
    """Require the credential's scope to cover its account, even on replay.

    An operation name in a listing-scoped key is not permission to spend the
    owner's balance or read unrelated private orders. Anonymous calls still
    reach the handler's uniform not-found/signature response.
    """
    if request.proof is None:
        return ()
    key_id = (request.proof.signature.key_id if isinstance(request.proof, SignatureProof)
              else request.proof.credential_id)
    credential = await tx.credential(key_id)
    return (AccessRequirement(resource_id=request.subject or credential.subject_id,
        operation=f'{request.operation}@{request.contract_version}', check='read'),)


def require_money_subject(ctx):
    require(ctx.principal.subject is not None and
            ctx.principal.actor == ctx.principal.subject, 'money_subject_required')
    return ctx.principal.subject


def checked_amount(value):
    require(type(value) is int and 0 < value <= MAX_MINOR, 'invalid_money_amount')
    return value


def balance(tx, subject):
    debit = tx.one("SELECT COALESCE(SUM(amount_minor),0) FROM money_ledger WHERE debit_account=?",
                   (subject,))[0]
    credit = tx.one("SELECT COALESCE(SUM(amount_minor),0) FROM money_ledger WHERE credit_account=?",
                    (subject,))[0]
    return int(credit) - int(debit)


def total_supply(tx):
    issued = tx.one("SELECT COALESCE(SUM(amount_minor),0) FROM money_ledger WHERE kind='mint'")[0]
    burned = tx.one("SELECT COALESCE(SUM(amount_minor),0) FROM money_ledger WHERE kind='burn'")[0]
    return int(issued) - int(burned)


def clearing_decision(*, kind, sender, recipient, amount, sender_balance,
                      recipient_balance, supply, local_issuer=False):
    """Pure policy v2: no clock, UUID, subject privileges or discretionary input."""
    amount = checked_amount(amount)
    require(all(type(value) is int and 0 <= value <= MAX_MINOR
                for value in (sender_balance, recipient_balance, supply)), 'invalid_money_state')
    require(kind in {'mint', 'burn', 'transfer', 'redeem', 'refund'}, 'invalid_money_kind')
    if kind in {'mint', 'burn'}:
        require(local_issuer, 'root_local_only')
        require((kind == 'mint' and sender is None and recipient == ROOT_SUBJECT) or
                (kind == 'burn' and sender == ROOT_SUBJECT and recipient is None),
                'invalid_money_accounts')
    else:
        require(sender is not None and recipient is not None, 'invalid_money_accounts')
        require(sender != recipient, 'self_transfer_forbidden')
    if sender in {ROOT_SUBJECT, '@root'}:
        require(local_issuer, 'root_local_only')
    if sender is not None:
        require(sender_balance >= amount, 'insufficient_funds')
    if recipient is not None:
        require(recipient_balance + amount <= MAX_MINOR, 'money_overflow')
    if kind == 'burn':
        require(supply >= amount, 'invalid_money_state')
    if kind == 'mint':
        require(supply + amount <= MAX_MINOR, 'money_overflow')
    return {'kind': kind, 'debit_account': sender, 'credit_account': recipient,
            'amount_minor': amount, 'currency_id': CURRENCY_ID,
            'policy_version': POLICY_VERSION, 'policy_digest': POLICY_DIGEST}


def append_entry(tx, *, sender, recipient, amount, actor, request_id, now,
                receipt_signer, reference=None, kind='transfer', entry_key='primary',
                _local_issuer=False):
    """Append one named leg in the caller's serialized transaction, never commit.

    Named legs permit atomic fund+release without manufacturing request IDs.
    Historical receipts remain byte-for-byte untouched by the schema upgrade.
    Only the separate local RootAdmin use case supplies _local_issuer.
    """
    require(not _local_issuer or actor == ROOT_SUBJECT, 'root_local_only')
    clearing_decision(kind=kind, sender=sender, recipient=recipient, amount=amount,
        sender_balance=balance(tx, sender) if sender else 0,
        recipient_balance=balance(tx, recipient) if recipient else 0,
        supply=total_supply(tx), local_issuer=_local_issuer)
    for account in {sender, recipient} - {None}:
        tx.execute('INSERT INTO money_accounts(subject_id,currency_id) VALUES (?,?) '
                   'ON CONFLICT(subject_id,currency_id) DO NOTHING',
                   (account, CURRENCY_ID), write=True)
    transaction_id = 'lt_' + uuid4().hex
    sequence = tx.one("SELECT nextval(pg_get_serial_sequence('money_ledger','seq'))")[0]
    body = {'transaction_id': transaction_id, 'kind': kind, 'currency_id': CURRENCY_ID,
            'amount_minor': amount, 'from_subject': sender, 'to_subject': recipient,
            'actor': actor, 'request_id': request_id, 'entry_key': entry_key,
            'reference': reference, 'committed_at': wire(now),
            'policy_version': POLICY_VERSION, 'policy_digest': POLICY_DIGEST,
            'ledger_sequence': sequence}
    receipt = {'body': body,
               'signature': wire(receipt_signer.sign(canonical(body), purpose='money-receipt'))}
    tx.execute('''INSERT INTO money_ledger
        (seq,id,kind,currency_id,amount_minor,debit_account,credit_account,actor,request_id,
         reference,committed_at,policy_version,policy_digest,receipt,entry_key)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (sequence,transaction_id,kind,CURRENCY_ID,amount,sender,recipient,actor,
         request_id,reference,wire(now),POLICY_VERSION,POLICY_DIGEST,
         canonical(receipt).decode(),entry_key), write=True)
    return receipt


def post_transfer(tx, *, sender, recipient, amount, actor, request_id, now,
                   receipt_signer, reference=None, kind='transfer', entry_key='primary',
                   escrow_authority=None):
    require(kind in {'transfer', 'redeem', 'refund'}, 'invalid_money_kind')
    account = tx.one('SELECT kind FROM ledger_accounts WHERE id=?', (sender,))
    if account and account[0] == 'order_escrow':
        require(escrow_authority is _ESCROW_WRITE, 'escrow_release_forbidden')
    return append_entry(tx, sender=sender, recipient=recipient, amount=amount,
        actor=actor, request_id=request_id, now=now, receipt_signer=receipt_signer,
        reference=reference, kind=kind, entry_key=entry_key)
