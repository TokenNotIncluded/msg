"""Cross-version market ownership and unchanged real-ledger behavior."""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path

import pytest
from test_managed_checkout import buy, ok, setup_sale

from msg.core.codec import canonical, decode
from msg.core.errors import Failure
from msg.core.models import Signature
from msg.security.crypto import verify

ENTRYPOINTS = {
    'msg.plugins.orders',
    'msg.plugins.store',
    'msg.plugins.money',
    'msg.plugins.delivery',
}
OWNERS = (
    (
        'msg.plugins.orders',
        'msg.market.order_records',
        {
            '_subject': 'require_signed_subject',
            '_viewer': 'require_order_viewer',
            '_order_id': 'new_order_id',
            '_row': 'read_order',
            '_view': 'legacy_order_view',
        },
    ),
    (
        'msg.plugins.store',
        'msg.market.catalog',
        {
            '_listing': 'read_listing',
            '_body': 'read_listing_body',
            '_package_row': 'read_package_record',
        },
    ),
    (
        'msg.plugins.money',
        'msg.market.ledger',
        {
            'account_requirements': 'account_requirements',
            '_owner': 'require_money_subject',
            '_amount': 'checked_amount',
            '_balance': 'balance',
            '_supply': 'total_supply',
            'clearing_decision': 'clearing_decision',
            '_post_entry': 'append_entry',
            '_post_transfer': 'post_transfer',
        },
    ),
    (
        'msg.plugins.delivery',
        'msg.market.managed_delivery',
        {
            '_delivery': 'read_delivery',
            '_buyer_order': 'read_buyer_order',
            '_verified_delivery': 'verify_managed_delivery',
            '_package': 'read_managed_package',
            '_payload': 'read_managed_payloads',
            'prepare_managed': 'prepare_managed',
            'delivery_summary': 'delivery_summary',
        },
    ),
)


def test_market_has_no_business_entrypoint_imports_even_inside_functions():
    import msg.market

    violations = []
    for path in Path(msg.market.__file__).parent.rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [node.module]
                if isinstance(node, ast.ImportFrom)
                else [item.name for item in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            for name in names:
                if name in ENTRYPOINTS:
                    violations.append((path.name, node.lineno, name))
    assert not violations, violations


@pytest.mark.parametrize('entrypoint,owner,names', OWNERS)
def test_compatibility_imports_are_the_same_owned_functions(entrypoint, owner, names):
    adapter = importlib.import_module(entrypoint)
    shared = importlib.import_module(owner)
    assert not hasattr(shared, 'install')
    for old, new in names.items():
        function = getattr(shared, new)
        assert function is getattr(adapter, old), (old, new)
        assert function.__module__ == owner
    records = importlib.import_module('msg.market.order_records')
    store = importlib.import_module('msg.plugins.store')
    assert store._subject is records.require_signed_subject


def test_shared_market_and_versioned_use_cases_import_without_loading_entrypoints():
    code = """
import importlib, importlib.abc, sys
forbidden={'msg.plugins.orders','msg.plugins.store','msg.plugins.money','msg.plugins.delivery'}
class NoEntrypoints(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname in forbidden:
            raise AssertionError('shared market loaded business entrypoint '+fullname)
sys.meta_path.insert(0,NoEntrypoints())
for name in ('order_records','catalog','ledger','managed_delivery','orders','delivery',
             'escrow','arbitration','email','policy','rationale','targets','delivery_notifications'):
    importlib.import_module('msg.market.'+name)
assert not forbidden.intersection(sys.modules)
"""
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_ledger_guard_has_one_owner_and_does_not_depend_on_escrow_or_handlers():
    ledger = importlib.import_module('msg.market.ledger')
    escrow = importlib.import_module('msg.market.escrow')
    assert ledger._ESCROW_WRITE is escrow._ESCROW_WRITE
    imports = [
        n.module
        for n in ast.walk(ast.parse(Path(ledger.__file__).read_text()))
        if isinstance(n, ast.ImportFrom)
    ]
    assert 'msg.market.escrow' not in imports
    assert not ENTRYPOINTS.intersection(imports)
    money = importlib.import_module('msg.plugins.money')
    for name in ('CURRENCY_ID', 'CODE', 'SCALE', 'MAX_MINOR', 'POLICY_VERSION', 'POLICY_DIGEST'):
        assert getattr(money, name) == getattr(ledger, name)


async def test_shared_order_views_preserve_buyer_privacy_and_zero_write_semantics(installed):
    from msg.plugins.orders import _row, _view

    app, seller, buyer, _, args = await setup_sale(installed, policy='escrow-v1')
    args['auto_accept'] = False
    order = ok(await buy(app, buyer, args, rid='shared-order-views')).data['order']
    async with app.metadata.transaction(write=False) as tx:
        def counts():
            return tuple(
                    tx.one(f'SELECT COUNT(*) FROM {name}')[0]
                    for name in (
                        'money_ledger',
                        'store_orders',
                        'store_deliveries',
                        'results',
                        'events',
                        'jobs',
                    )
                )
        before = counts()
        record = _row(tx, order['id'], buyer[1])
        buyer_view = _view(record, buyer[1])
        seller_view = _view(_row(tx, order['id'], seller[1]), seller[1])
        assert buyer_view['delivery_target'] == {'subject_id': buyer[1], 'channel': 'site'}
        assert {'delivery_target', 'payment_intent_digest', 'receipt_refs'} <= set(buyer_view)
        assert not {'delivery_target', 'payment_intent_digest', 'receipt_refs'}.intersection(
            seller_view
        )
        assert buyer_view['state'] == seller_view['state'] == 'delivered'
        with pytest.raises(Failure, match='order_not_found'):
            _row(tx, order['id'], 'unrelated-viewer')
        with pytest.raises(Failure, match='order_not_found'):
            _row(tx, 'ord_does_not_exist', buyer[1])
        assert counts() == before


async def test_shared_posting_path_rejects_forged_escrow_authority_without_writes(installed):
    from msg.plugins.money import _balance, _post_transfer
    from msg.plugins.orders import _row

    app, seller, buyer, _, args = await setup_sale(installed, policy='escrow-v1')
    args['auto_accept'] = False
    order = ok(await buy(app, buyer, args, rid='shared-posting-guard')).data['order']
    async with app.metadata.transaction(write=True) as tx:
        record = _row(tx, order['id'], buyer[1])
        before = tuple(
            tx.one(f'SELECT COUNT(*) FROM {name}')[0]
            for name in (
                'money_ledger',
                'money_accounts',
                'order_escrow_decisions',
                'order_settlements',
            )
        )
        for authority in (None, object()):
            with pytest.raises(Failure, match='escrow_release_forbidden'):
                _post_transfer(
                    tx,
                    sender=record['escrow_subject'],
                    recipient=seller[1],
                    amount=record['total_price_minor'],
                    actor=buyer[1],
                    request_id='forged-shared-leg',
                    now=app.clock(),
                    receipt_signer=app.receipt_signer,
                    escrow_authority=authority,
                )
        after = tuple(
            tx.one(f'SELECT COUNT(*) FROM {name}')[0]
            for name in (
                'money_ledger',
                'money_accounts',
                'order_escrow_decisions',
                'order_settlements',
            )
        )
        assert before == after
        assert _balance(tx, record['escrow_subject']) == record['total_price_minor']
        assert _balance(tx, seller[1]) == 0


async def test_alias_reads_and_replay_preserve_signed_receipt_bytes(installed):
    from msg.plugins.delivery import _delivery, _verified_delivery
    from msg.plugins.orders import _row

    app, _, buyer, _, args = await setup_sale(installed)
    first = ok(await buy(app, buyer, args, rid='shared-receipt-bytes'))
    payment = first.data['payment']
    verify(
        app.receipt_signer.public_key,
        canonical(payment['body']),
        decode(Signature, payment['signature']),
        purpose='money-receipt',
    )
    async with app.metadata.transaction(write=False) as tx:
        before = tuple(tx.rows('SELECT seq,receipt FROM money_ledger ORDER BY seq'))
        record = _row(tx, first.data['order']['id'], buyer[1])
        await _verified_delivery(app, tx, record, _delivery(tx, record['id']))
    replay = ok(await buy(app, buyer, args, rid='shared-receipt-bytes'))
    assert replay.replayed and replay.data == first.data
    assert canonical(replay.data['payment']) == canonical(payment)
    async with app.metadata.transaction(write=False) as tx:
        assert tuple(tx.rows('SELECT seq,receipt FROM money_ledger ORDER BY seq')) == before
        assert canonical(payment).decode() in [row[1] for row in before]
