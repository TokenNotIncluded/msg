"""Physical-console central bank operations. Never register these as network ops."""
from __future__ import annotations

import asyncio
import getpass
import re
from decimal import Decimal
from uuid import uuid4

from msg.admin.root import require_local_console, root_envelope
from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, digest, loads, wire
from msg.core.errors import require
from msg.core.models import AuditEvent, Event, ResourceRef
from msg.plugins.money import (
    CURRENCY_ID,
    MAX_MINOR,
    POLICY_DIGEST,
    POLICY_VERSION,
    SCALE,
    _balance,
    _supply,
)
from msg.plugins.offers import validate_local_offer
from msg.security.crypto import Ed25519Signer, open_private_key


def parse_amount(value: str) -> int:
    """Parse a displayed MSG amount without binary floats or silent rounding."""
    require(isinstance(value, str) and re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?', value),
            'invalid_money_amount')
    minor = int(Decimal(value) * (10 ** SCALE))
    require(0 < minor <= MAX_MINOR, 'invalid_money_amount')
    return minor


def _root_signer(app, signer):
    require(isinstance(signer, Ed25519Signer) and
            signer.public_key == app.certificates.root_public_key, 'root_key_mismatch')


_OFFER_COLUMNS = ('offer_id', 'resource_kind', 'unit', 'price_minor', 'min_quantity',
                  'max_quantity', 'entitlement_kind', 'duration_seconds', 'enabled',
                  'price_revision')
_UNSET = object()


def _offer_snapshot(tx, offer_id):
    row = tx.one('''SELECT offer_id,resource_kind,unit,price_minor,min_quantity,max_quantity,
                         entitlement_kind,duration_seconds,enabled,price_revision
                  FROM server_offers WHERE offer_id=?''', (offer_id,))
    return dict(zip(_OFFER_COLUMNS, row)) if row else None


async def apply_offer(app, signer, *, action: str, operator: str, offer_id: str,
                      fields: dict | None = None, expected_offer=_UNSET):
    """Root-signed local offer mutation; set remains Registry/fulfiller gated."""
    _root_signer(app, signer)
    require(action in {'set', 'disable'}, 'invalid_offer_action')
    require(isinstance(offer_id, str) and
            re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,79}', offer_id), 'invalid_offer_id')
    if action == 'set':
        require(isinstance(fields, dict) and set(fields) == {
            'resource_kind', 'unit', 'price_minor', 'min_quantity',
            'max_quantity', 'entitlement_kind', 'duration_seconds'}, 'invalid_offer_fields')
        validate_local_offer(app, **fields)
    else:
        require(fields is None, 'invalid_offer_fields')
    now = app.clock()
    request_id = 'local_offer_' + uuid4().hex
    async with app.metadata.transaction(write=True) as tx:
        previous = _offer_snapshot(tx, offer_id)
        if expected_offer is not _UNSET:
            require(previous == expected_offer, 'offer_preview_stale')
        if action == 'set':
            validate_local_offer(app, **fields)
            revision = 'price_' + uuid4().hex
            tx.execute('''INSERT INTO server_offers
                (offer_id,resource_kind,unit,price_minor,min_quantity,max_quantity,
                 entitlement_kind,duration_seconds,enabled,price_revision)
                VALUES (?,?,?,?,?,?,?,?,TRUE,?)
                ON CONFLICT(offer_id) DO UPDATE SET
                  resource_kind=excluded.resource_kind,unit=excluded.unit,
                  price_minor=excluded.price_minor,min_quantity=excluded.min_quantity,
                  max_quantity=excluded.max_quantity,entitlement_kind=excluded.entitlement_kind,
                  duration_seconds=excluded.duration_seconds,enabled=TRUE,
                  price_revision=excluded.price_revision''',
                (offer_id, fields['resource_kind'], fields['unit'], fields['price_minor'],
                 fields['min_quantity'], fields['max_quantity'], fields['entitlement_kind'],
                 fields['duration_seconds'], revision), write=True)
        else:
            require(previous is not None, 'offer_not_found')
            require(previous['enabled'], 'offer_already_disabled')
            tx.execute('UPDATE server_offers SET enabled=FALSE WHERE offer_id=?',
                       (offer_id,), write=True)
        current = _offer_snapshot(tx, offer_id)
        statement = {'action': 'offer_' + action, 'offer_id': offer_id,
                     'before': previous, 'after': current, 'operator': operator,
                     'request_id': request_id, 'time': wire(now)}
        signature = wire(signer.sign(canonical(statement), purpose='money-admin'))
        event = Event(id='audit_' + uuid4().hex, type='root.money.offer.' + action,
                      time=now, request_id=request_id, actor=ROOT_SUBJECT,
                      subject=ROOT_SUBJECT, resources=(ResourceRef(id=ROOT_SUBJECT),),
                      data={**statement, 'root_signature': signature})
        await tx.append_event(event)
        await tx.append_audit(AuditEvent(event=event,
            authority=(ResourceRef(id=app.certificates.root_certificate.resource_id),),
            before_digest=digest(previous), after_digest=digest(current),
            previous_digest=None, entry_digest='', result='offer_' + action))
    return {'action': action, 'offer': current, 'audit_event_id': event.id}


async def apply_money(app, signer, *, action: str, operator: str, amount_minor: int | None = None,
                      subject_id: str | None = None, expected_state: dict | None = None):
    """Internal local use case; callers must establish the OS-console boundary.

    Each invocation creates a fresh local request id. PG serializes all writers,
    so balance and supply checks are re-evaluated within the committing transaction.
    """
    _root_signer(app, signer)
    require(action in {'mint', 'burn', 'transfer', 'bank_add', 'bank_remove'}, 'invalid_money_action')
    if action in {'mint', 'burn', 'transfer'}:
        require(type(amount_minor) is int and 0 < amount_minor <= MAX_MINOR, 'invalid_money_amount')
    else:
        require(amount_minor is None, 'invalid_money_amount')
    if action in {'transfer', 'bank_add', 'bank_remove'}:
        require(isinstance(subject_id, str) and subject_id not in {ROOT_SUBJECT, '@root'},
                'invalid_money_recipient')
    else:
        require(subject_id is None, 'invalid_money_recipient')

    request_id = 'local_money_' + uuid4().hex
    now = app.clock()
    async with app.metadata.transaction(write=True) as tx:
        if subject_id is not None:
            subject = await tx.subject(subject_id)
            require(subject.kind in {'registered', 'custodial'} and not subject.local_only,
                    'invalid_money_recipient')
        before_supply = _supply(tx)
        before_root = _balance(tx, ROOT_SUBJECT)
        row = (tx.one('SELECT status FROM money_bank_roles WHERE subject_id=?', (subject_id,))
               if action.startswith('bank_') else None)
        before_role = row[0] if row else None
        if expected_state is not None:
            current = {'supply': before_supply, 'root': before_root,
                       'subject': _balance(tx, subject_id) if subject_id else None,
                       'role': before_role}
            require(current == expected_state, 'money_preview_stale')
        if action.startswith('bank_'):
            require((action == 'bank_add' and before_role != 'active') or
                    (action == 'bank_remove' and before_role == 'active'), 'bank_role_unchanged')
            if action == 'bank_add':
                tx.execute('''INSERT INTO money_bank_roles(subject_id,status,granted_at,granted_by,revoked_at)
                    VALUES (?,?,?,?,NULL) ON CONFLICT(subject_id) DO UPDATE SET
                    status='active',granted_at=excluded.granted_at,granted_by=excluded.granted_by,
                    revoked_at=NULL''', (subject_id, 'active', wire(now), ROOT_SUBJECT), write=True)
            else:
                tx.execute("UPDATE money_bank_roles SET status='revoked',revoked_at=? WHERE subject_id=?",
                           (wire(now), subject_id), write=True)
            receipt = None
        else:
            if action == 'mint':
                require(before_supply + amount_minor <= MAX_MINOR and
                        before_root + amount_minor <= MAX_MINOR, 'money_overflow')
                debit, credit, kind = None, ROOT_SUBJECT, 'mint'
            elif action == 'burn':
                require(before_root >= amount_minor, 'insufficient_funds')
                debit, credit, kind = ROOT_SUBJECT, None, 'burn'
            else:
                require(before_root >= amount_minor, 'insufficient_funds')
                require(_balance(tx, subject_id) + amount_minor <= MAX_MINOR, 'money_overflow')
                debit, credit, kind = ROOT_SUBJECT, subject_id, 'transfer'
            for account in {debit, credit} - {None}:
                tx.execute('''INSERT INTO money_accounts(subject_id,currency_id) VALUES (?,?)
                    ON CONFLICT(subject_id,currency_id) DO NOTHING''', (account, CURRENCY_ID), write=True)
            transaction_id = 'lt_' + uuid4().hex
            sequence = tx.one("SELECT nextval(pg_get_serial_sequence('money_ledger','seq'))")[0]
            body = {'transaction_id': transaction_id, 'kind': kind, 'currency_id': CURRENCY_ID,
                    'amount_minor': amount_minor, 'from_subject': debit, 'to_subject': credit,
                    'committed_at': wire(now), 'policy_version': POLICY_VERSION,
                    'policy_digest': POLICY_DIGEST, 'ledger_sequence': sequence}
            receipt = {'body': body,
                       'signature': wire(app.receipt_signer.sign(canonical(body), purpose='money-receipt'))}
            tx.execute('''INSERT INTO money_ledger
                (seq,id,kind,currency_id,amount_minor,debit_account,credit_account,actor,request_id,
                 reference,committed_at,policy_version,policy_digest,receipt)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (sequence, transaction_id, kind, CURRENCY_ID, amount_minor, debit, credit,
                 ROOT_SUBJECT, request_id, None, wire(now), POLICY_VERSION, POLICY_DIGEST,
                 canonical(receipt).decode()), write=True)
        statement = {'action': action, 'subject_id': subject_id, 'amount_minor': amount_minor,
                     'currency_id': CURRENCY_ID, 'operator': operator, 'request_id': request_id,
                     'time': wire(now), 'receipt_digest': digest(receipt) if receipt else None,
                     'before_supply_minor': before_supply, 'after_supply_minor': _supply(tx),
                     'before_root_minor': before_root, 'after_root_minor': _balance(tx, ROOT_SUBJECT),
                     'before_role': before_role}
        proof = wire(signer.sign(canonical(statement), purpose='money-admin'))
        event = Event(id='audit_' + uuid4().hex, type='root.money.' + action,
                      time=now, request_id=request_id, actor=ROOT_SUBJECT,
                      subject=ROOT_SUBJECT,
                      resources=(ResourceRef(id=subject_id or ROOT_SUBJECT),),
                      data={**statement, 'root_signature': proof})
        await tx.append_event(event)
        await tx.append_audit(AuditEvent(event=event,
            authority=(ResourceRef(id=app.certificates.root_certificate.resource_id),),
            before_digest=digest({'supply': before_supply, 'root': before_root, 'role': before_role}),
            after_digest=digest(statement), previous_digest=None, entry_digest='', result=action))
    return {'action': action, 'receipt': receipt, 'audit_event_id': event.id,
            'root_balance_minor': statement['after_root_minor'],
            'total_supply_minor': statement['after_supply_minor'],
            'subject_id': subject_id}


class MoneyAdmin:
    def __init__(self, config_dir):
        self.config_dir = config_dir

    def execute(self, action, *, amount=None, subject_id=None):
        operator = require_local_console(self.config_dir)
        from msg.admin.root import RootAdmin
        app = RootAdmin(self.config_dir)._app()

        async def run():
            await app.load()
            try:
                amount_minor = parse_amount(amount) if amount is not None else None
                resolved_subject = subject_id
                async with app.metadata.transaction(write=False) as tx:
                    if subject_id and subject_id.startswith('@'):
                        resolved_subject = await tx.resolve('/' + subject_id)
                    if resolved_subject is not None:
                        subject = await tx.subject(resolved_subject)
                        require(subject.kind in {'registered', 'custodial'} and not subject.local_only,
                                'invalid_money_recipient')
                    root_before = _balance(tx, ROOT_SUBJECT)
                    supply_before = _supply(tx)
                    subject_before = (_balance(tx, resolved_subject)
                                      if resolved_subject is not None else None)
                    role_row = (tx.one('SELECT status FROM money_bank_roles WHERE subject_id=?',
                                       (resolved_subject,)) if action.startswith('bank_') else None)
                    role_before = role_row[0] if role_row else None
                root_delta = (amount_minor if action == 'mint' else
                              -amount_minor if action in {'burn', 'transfer'} else 0)
                supply_delta = (amount_minor if action == 'mint' else
                                -amount_minor if action == 'burn' else 0)
                # Display escaped JSON only; subject handles cannot inject terminal control codes.
                preview = {'action': action, 'amount_minor': amount_minor,
                           'currency_id': CURRENCY_ID, 'subject_id': resolved_subject,
                           'subject_handle': subject_id if subject_id != resolved_subject else None,
                           'root_balance_before_minor': root_before,
                           'root_balance_after_minor': root_before + root_delta,
                           'total_supply_before_minor': supply_before,
                           'total_supply_after_minor': supply_before + supply_delta,
                           'recipient_balance_before_minor': subject_before,
                           'recipient_balance_after_minor': (subject_before + amount_minor
                                if action == 'transfer' else subject_before),
                           'bank_role_before': role_before,
                           'bank_role_after': ('active' if action == 'bank_add' else
                                               'revoked' if action == 'bank_remove' else role_before)}
                print(canonical(preview).decode())
                approval = 'CONFIRM MONEY ' + digest(preview)
                require(input('Type ' + approval + ' to continue: ') == approval,
                        'approval_cancelled')
                pin = getpass.getpass('Root PIN/passphrase: ')
                private = open_private_key(loads(root_envelope(self.config_dir).read_bytes()), pin)
                signer = Ed25519Signer.from_bytes(private)
                return await apply_money(app, signer, action=action, operator=operator,
                                         amount_minor=amount_minor, subject_id=resolved_subject,
                                         expected_state={'supply': supply_before, 'root': root_before,
                                                         'subject': subject_before, 'role': role_before})
            finally:
                await app.close()
        return asyncio.run(run())

    def execute_offer(self, action, *, offer_id, fields=None):
        operator = require_local_console(self.config_dir)
        require(action in {'set', 'disable'}, 'invalid_offer_action')
        require(isinstance(offer_id, str) and
                re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,79}', offer_id), 'invalid_offer_id')
        from msg.admin.root import RootAdmin
        app = RootAdmin(self.config_dir)._app()

        async def run():
            await app.load()
            try:
                if action == 'set':
                    require(isinstance(fields, dict) and set(fields) == {
                        'resource_kind', 'unit', 'price', 'min_quantity',
                        'max_quantity', 'entitlement_kind', 'duration_seconds'},
                        'invalid_offer_fields')
                    fields['price_minor'] = parse_amount(fields.pop('price'))
                    validate_local_offer(app, **fields)
                async with app.metadata.transaction(write=False) as tx:
                    previous = _offer_snapshot(tx, offer_id)
                if action == 'disable':
                    require(previous is not None, 'offer_not_found')
                    require(previous['enabled'], 'offer_already_disabled')
                preview = {'action': 'offer_' + action, 'offer_id': offer_id,
                           'before': previous, 'proposed': fields if action == 'set' else
                           {**previous, 'enabled': False}}
                print(canonical(preview).decode())
                approval = 'CONFIRM MONEY ' + digest(preview)
                require(input('Type ' + approval + ' to continue: ') == approval,
                        'approval_cancelled')
                pin = getpass.getpass('Root PIN/passphrase: ')
                private = open_private_key(loads(root_envelope(self.config_dir).read_bytes()), pin)
                signer = Ed25519Signer.from_bytes(private)
                return await apply_offer(app, signer, action=action, operator=operator,
                                         offer_id=offer_id, fields=fields,
                                         expected_offer=previous)
            finally:
                await app.close()
        return asyncio.run(run())
