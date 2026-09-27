"""Generate a real pre-LedgerAccount installation using the pinned old code.

Run in a subprocess with PYTHONPATH set to that checkout's src and tests. The
current Application and current schema must never initialize the source DB.
"""
import asyncio
import json
import sys
from pathlib import Path

import psycopg

from msg.application import Application
from msg.admin.root import _provision, _approve_csr
from msg.admin.money import apply_money
from msg.admin.backups import backup
from msg.config import write_example
from msg.core.codec import canonical, wire
from msg.storage import postgres
from test_service import NOW, call, register
from test_orders import _sale, _intent


async def generate(source, directory, dsn):
    assert Path(postgres.__file__).resolve().is_relative_to(source)
    assert 'ledger_accounts' not in postgres._SCHEMA
    settings = write_example(directory/'etc', directory/'data', 'http://testserver', postgres_dsn=dsn)
    app = Application(settings, clock=lambda: NOW)
    csr, root = await _provision(app, 'isolated-legacy-fixture-passphrase')
    await _approve_csr(app, csr, root, expected_digest=None, operator='isolated-test-fixture')
    try:
        seller_key, seller, _ = await register(app, 'order-seller')
        buyer_key, buyer, _ = await register(app, 'old-buyer')
        await apply_money(app, root, action='mint', operator='isolated-test-fixture', amount_minor=100_000_000)
        for subject in (seller, buyer):
            await apply_money(app, root, action='transfer', operator='isolated-test-fixture',
                              subject_id=subject, amount_minor=50_000_000)
        listing, _ = await _sale(app, seller_key, seller, quantity=5)
        orders = []
        for state in ('funded', 'settled', 'cancelled'):
            bought = await call(app, 'orders.buy', _intent(listing), key=buyer_key, subject=buyer)
            assert bought.status == 'ok', wire(bought)
            order = bought.data['order']['id']
            orders.append(order)
            if state == 'settled':
                prepared = await call(app, 'delivery.prepare', {'order_id': order}, key=buyer_key, subject=buyer)
                assert prepared.status == 'ok', wire(prepared)
                accepted = await call(app, 'delivery.accept', {'order_id': order,
                    'delivery_digest': prepared.data['delivery']['delivery_digest']}, key=buyer_key, subject=buyer)
                assert accepted.status == 'ok', wire(accepted)
            elif state == 'cancelled':
                cancelled = await call(app, 'orders.cancel', {'order_id': order}, key=buyer_key, subject=buyer)
                assert cancelled.status == 'ok', wire(cancelled)
        bounties = []
        for state in ('active', 'closed'):
            created = await call(app, 'bounty.create', {'name': 'legacy-'+state,
                'terms': 'A signature proves current key control only.',
                'reward_minor': 1_000_000, 'budget_minor': 3_000_000, 'max_claims': 3},
                key=seller_key, subject=seller)
            assert created.status == 'ok', wire(created)
            listing_id = created.data['bounty']['listing_id']
            bounties.append(listing_id)
            challenge = await call(app, 'bounty.challenge', {'listing_id': listing_id},
                                   key=buyer_key, subject=buyer)
            assert challenge.status == 'ok', wire(challenge)
            payload = challenge.data['challenge']
            claimed = await call(app, 'bounty.claim', {'challenge_id': payload['challenge_id'],
                'proof': wire(buyer_key.sign(canonical(payload), purpose='bounty-pop-v1'))},
                key=buyer_key, subject=buyer)
            assert claimed.status == 'ok', wire(claimed)
            if state == 'closed':
                closed = await call(app, 'bounty.close', {'listing_id': listing_id},
                                    key=seller_key, subject=seller)
                assert closed.status == 'ok', wire(closed)
        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT to_regclass('ledger_accounts')").fetchone()[0] is None
            assert {row[0] for row in conn.execute('SELECT state FROM store_orders')} == {'funded','settled','refunded'}
            assert {row[0] for row in conn.execute('SELECT state FROM bounty_listings')} == {'active','closed'}
            assert conn.execute('SELECT COUNT(*) FROM bounty_claims').fetchone()[0] == 2
        result = await backup(app, directory/'legacy.zip')
        assert result['status'] == 'backup_created' and not result['root_private_key_included']
        (directory/'fixture.json').write_text(json.dumps({'seller': seller, 'buyer': buyer,
            'orders': orders, 'bounties': bounties, 'backup_sha256': result['sha256']}))
    finally:
        await app.close()


if __name__ == '__main__':
    asyncio.run(generate(Path(sys.argv[1]).resolve(), Path(sys.argv[2]), sys.argv[3]))
