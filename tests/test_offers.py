"""Offers never turn a catalogue row into spending power or permission."""

import pytest
from test_service import call, register

from msg.core.errors import Failure
from msg.plugins.offers import purchasable, validate_local_offer


@pytest.mark.asyncio
async def test_no_default_offers_and_no_read_side_effects(installed):
    app, _ = installed
    assert not purchasable(app, 'user', 'storage_bytes')
    assert not purchasable(app, 'file', 'storage_bytes')
    async with app.metadata.transaction(write=False) as tx:
        before = (
            tx.one('SELECT COUNT(*) FROM money_ledger')[0],
            tx.one('SELECT COUNT(*) FROM server_offers')[0],
            tx.one('SELECT COUNT(*) FROM events')[0],
        )
    result = await call(app, 'money.offers', {})
    assert result.status == 'ok' and result.data['currency_id'] == 'primary'
    assert len(result.data['offers']) == 0
    async with app.metadata.transaction(write=False) as tx:
        after = (
            tx.one('SELECT COUNT(*) FROM money_ledger')[0],
            tx.one('SELECT COUNT(*) FROM server_offers')[0],
            tx.one('SELECT COUNT(*) FROM events')[0],
        )
    assert after == before


@pytest.mark.asyncio
async def test_unlisted_redeem_fails_without_debit_or_entitlement(installed):
    app, _ = installed
    key, uid, _ = await register(app, 'offer-buyer')
    args = {
        'offer_id': 'nonexistent',
        'quantity': 1,
        'currency_id': 'primary',
        'price_revision': 'r1',
    }
    result = await call(app, 'money.redeem', args, key=key, subject=uid)
    assert result.status == 'error' and result.error.code == 'offer_not_found'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0] == 0
        assert tx.one('SELECT COUNT(*) FROM resource_entitlements')[0] == 0


@pytest.mark.asyncio
async def test_unqualified_catalogue_row_is_invisible_and_cannot_debit(installed):
    app, _ = installed
    key, uid, _ = await register(app, 'offer-hidden-buyer')
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            """INSERT INTO server_offers
            (offer_id,resource_kind,unit,price_minor,min_quantity,max_quantity,
             entitlement_kind,duration_seconds,enabled,price_revision)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ('unsafe-user', 'user', 'user', 10, 1, 1, 'create_identity', None, True, 'r1'),
            write=True,
        )
    catalogue = await call(app, 'money.offers', {})
    assert catalogue.status == 'ok' and len(catalogue.data['offers']) == 0
    result = await call(
        app,
        'money.redeem',
        {
            'offer_id': 'unsafe-user',
            'quantity': 1,
            'currency_id': 'primary',
            'price_revision': 'r1',
        },
        key=key,
        subject=uid,
    )
    assert result.error.code == 'offer_not_found'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0] == 0
        assert tx.one('SELECT COUNT(*) FROM resource_entitlements')[0] == 0


@pytest.mark.asyncio
async def test_local_offer_set_requires_registry_and_fulfiller(installed):
    app, _ = installed
    with pytest.raises(Failure) as error:
        validate_local_offer(
            app,
            resource_kind='file',
            entitlement_kind='storage_bytes',
            unit='byte',
            price_minor=1,
            min_quantity=1,
            max_quantity=100,
        )
    assert error.value.code == 'resource_not_purchasable'
