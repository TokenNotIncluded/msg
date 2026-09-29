"""The catalog has immutable facts, while buying and settlement remain absent."""

from types import SimpleNamespace

import pytest
from test_service import call, register

from msg.core.codec import b64, canonical, wire
from msg.plugins.store import _body


def listing_args(**changes):
    return {
        'name': 'sample.bin',
        'item_kind': 'file',
        'price_minor': 1250000,
        'currency_id': 'primary',
        'quantity': 2,
        'delivery_mode': 'managed_instant',
        'escrow_policy': 'escrow-v1',
        'dispute_policy': 'dispute-v1',
        'terms': 'The file is delivered after settlement.',
        **changes,
    }


@pytest.mark.asyncio
async def test_store_starts_empty_and_listing_requires_seller_signature(installed):
    app, _ = installed
    anonymous = await call(app, 'discovery.get', {'id': '/store'})
    assert anonymous.status == 'ok', wire(anonymous)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM resources WHERE parent='t_store'")[0] == 0
        assert tx.one('SELECT COUNT(*) FROM store_packages')[0] == 0
    unsigned = await call(app, 'store.listing_create', listing_args())
    assert unsigned.status == 'error'
    key, seller, _ = await register(app, 'catalog-seller')
    created = await call(app, 'store.listing_create', listing_args(), key=key, subject=seller)
    assert created.status == 'ok', wire(created)
    listing = created.data['listing']
    assert listing['seller'] == seller and listing['state'] == 'draft' and listing['mode'] == 'sale'
    assert listing['package_ref'] is None
    assert listing['listing_revision'] == listing['terms_revision']
    assert (await call(app, 'store.listing_get', {'id': listing['listing_id']})).status == 'ok'
    public_resource = await call(app, 'discovery.get', {'id': listing['listing_id']})
    assert public_resource.status == 'ok' and listing['listing_id'] in str(public_resource.data)
    # A draft catalog entry is not a purchase API.
    assert not any(
        s.name in {'store.buy', 'store.order_create', 'store.escrow_fund'}
        for s in app.registry.operations()
    )
    explicit = await call(
        app,
        'store.listing_create',
        listing_args(mode='sale', name='sale.bin'),
        key=key,
        subject=seller,
    )
    assert explicit.status == 'ok' and explicit.data['listing']['mode'] == 'sale'
    unsupported = await call(
        app,
        'store.listing_create',
        listing_args(mode='bounty', name='bounty.bin'),
        key=key,
        subject=seller,
    )
    assert unsupported.error.code == 'listing_mode_unsupported'
    async with app.metadata.transaction(write=False) as tx:
        revision = await tx.revision(created.resources[0])
        persisted = await app.contents.read_bytes(revision.content)
        assert b'"mode":"sale"' in persisted
        assert tx.one("SELECT COUNT(*) FROM resources WHERE parent='t_store'")[0] == 2


@pytest.mark.asyncio
async def test_legacy_listing_body_without_mode_reads_as_sale():
    old = {
        'currency_id': 'primary',
        'price_minor': 1,
        'quantity': 1,
        'item_kind': 'file',
        'delivery_mode': 'managed_instant',
        'package_ref': None,
        'escrow_policy': 'v1',
        'dispute_policy': 'v1',
        'terms': 'old',
        'expires_at': None,
        'state': 'draft',
    }
    revision = SimpleNamespace(id='v_legacy', content=object())

    class LegacyTx:
        async def revision(self, ref):
            assert ref.id == 'r_legacy'
            return revision

    class LegacyContent:
        async def read_bytes(self, blob):
            assert blob is revision.content
            return canonical(old)

    body, found = await _body(
        SimpleNamespace(contents=LegacyContent()), LegacyTx(), SimpleNamespace(id='r_legacy')
    )
    assert found is revision and body == {'mode': 'sale', **old}


@pytest.mark.asyncio
async def test_deposit_is_immutable_and_only_seller_can_read_payload(installed):
    app, _ = installed
    key, seller, _ = await register(app, 'deposit-seller')
    other_key, other, _ = await register(app, 'deposit-other')
    listing = await call(app, 'store.listing_create', listing_args(), key=key, subject=seller)
    rid = listing.resources[0].id
    version = listing.resources[0].revision
    file = await call(
        app,
        'content.file_put',
        {
            'parent': '/@deposit-seller/files',
            'name': 'private.bin',
            'data': b64(b'package secret'),
            'media_type': 'application/octet-stream',
        },
        key=key,
        subject=seller,
    )
    assert file.status == 'ok', wire(file)
    payload = {'id': file.resources[0].id, 'revision': file.resources[0].revision}
    deposit = await call(
        app,
        'store.package_deposit',
        {
            'listing_id': rid,
            'listing_revision': version,
            'manifest': {'format': 1, 'name': 'private.bin'},
            'payload_refs': [payload],
        },
        key=key,
        subject=seller,
        rid='deposit-once',
    )
    assert deposit.status == 'ok', wire(deposit)
    replay = await call(
        app,
        'store.package_deposit',
        {
            'listing_id': rid,
            'listing_revision': version,
            'manifest': {'format': 1, 'name': 'private.bin'},
            'payload_refs': [payload],
        },
        key=key,
        subject=seller,
        rid='deposit-once',
    )
    assert replay.status == 'ok' and replay.replayed
    package_id = deposit.data['package']['id']
    first = await call(app, 'store.package_get', {'id': package_id}, key=key, subject=seller)
    assert first.status == 'ok' and first.data['package']['payload_refs'][0]['id'] == payload['id']
    hidden = await call(app, 'store.package_get', {'id': package_id}, key=other_key, subject=other)
    absent = await call(
        app, 'store.package_get', {'id': 'pkg_does_not_exist'}, key=other_key, subject=other
    )
    assert hidden.error.code == absent.error.code == 'package_not_found'
    assert 'package secret' not in str(hidden.data)
    update = await call(
        app,
        'store.listing_update',
        {'id': rid, 'package_ref': package_id, 'state': 'active'},
        key=key,
        subject=seller,
        expected=((rid, listing.data['generation']),),
    )
    assert update.status == 'ok', wire(update)
    old = await call(app, 'store.listing_get', {'id': rid, 'revision': version})
    assert old.status == 'ok' and old.data['listing']['package_ref'] is None
    assert old.data['listing']['price_minor'] == 1250000
    newer = await call(
        app,
        'store.listing_update',
        {'id': rid, 'price_minor': 1500000, 'terms': 'Revised terms.'},
        key=key,
        subject=seller,
        expected=((rid, update.data['generation']),),
    )
    assert newer.status == 'ok', wire(newer)
    again = await call(app, 'store.package_get', {'id': package_id}, key=key, subject=seller)
    assert again.data['package']['digest'] == first.data['package']['digest']
    assert again.data['package']['listing_revision'] == version
    assert old.data['listing']['terms_digest'] != newer.data['listing']['terms_digest']


@pytest.mark.asyncio
async def test_listing_cas_payload_ownership_and_no_secret_leak(installed):
    app, _ = installed
    seller_key, seller, _ = await register(app, 'catalog-owner')
    other_key, other, _ = await register(app, 'catalog-intruder')
    listing = await call(
        app, 'store.listing_create', listing_args(), key=seller_key, subject=seller
    )
    rid = listing.resources[0].id
    generation = listing.data['generation']
    stale = await call(
        app,
        'store.listing_update',
        {'id': rid, 'price_minor': 2},
        key=seller_key,
        subject=seller,
        expected=((rid, generation + 1),),
    )
    assert stale.error.code == 'generation_conflict'
    forbidden = await call(
        app,
        'store.listing_update',
        {'id': rid, 'price_minor': 2},
        key=other_key,
        subject=other,
        expected=((rid, generation),),
    )
    missing = await call(
        app,
        'store.listing_update',
        {'id': 'r_absent', 'price_minor': 2},
        key=other_key,
        subject=other,
    )
    assert forbidden.error.code == missing.error.code == 'listing_not_found'
    foreign = await call(
        app,
        'content.file_put',
        {'parent': '/@catalog-intruder/files', 'name': 'private.txt', 'data': b64(b'not yours')},
        key=other_key,
        subject=other,
    )
    deposit = await call(
        app,
        'store.package_deposit',
        {
            'listing_id': rid,
            'listing_revision': listing.resources[0].revision,
            'manifest': {},
            'payload_refs': [
                {'id': foreign.resources[0].id, 'revision': foreign.resources[0].revision}
            ],
        },
        key=seller_key,
        subject=seller,
    )
    assert deposit.error.code == 'payload_not_owned'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM store_packages')[0] == 0


@pytest.mark.asyncio
async def test_store_namespace_and_listing_metadata_need_store_operations(installed):
    app, _ = installed
    key, seller, _ = await register(app, 'catalog-boundary')
    for operation, args in (
        ('content.post_create', {'parent': '/store', 'body': 'unrelated post'}),
        ('content.file_put', {'parent': '/store', 'name': 'unrelated.bin', 'data': b64(b'x')}),
        ('content.topic_create', {'parent': '/store', 'name': 'unrelated'}),
    ):
        denied = await call(app, operation, args, key=key, subject=seller)
        assert denied.error.code == 'store_controlled_resource', wire(denied)
    ordinary = await call(
        app, 'content.post_create', {'parent': '/main', 'body': 'ordinary'}, key=key, subject=seller
    )
    assert ordinary.status == 'ok', wire(ordinary)
    moved = await call(
        app,
        'content.move',
        {'id': ordinary.resources[0].id, 'parent': '/store'},
        key=key,
        subject=seller,
        expected=((ordinary.resources[0].id, ordinary.data['generation']),),
    )
    assert moved.error.code == 'store_controlled_resource'
    listing = await call(app, 'store.listing_create', listing_args(), key=key, subject=seller)
    assert listing.status == 'ok', wire(listing)
    rid = listing.resources[0].id
    changed = await call(
        app,
        'content.chmod',
        {'id': rid, 'mode': '0777'},
        key=key,
        subject=seller,
        expected=((rid, listing.data['generation']),),
    )
    assert changed.error.code == 'controlled_resource'
    changed_group = await call(
        app,
        'content.chgrp',
        {'id': rid, 'group': '/&public'},
        key=key,
        subject=seller,
        expected=((rid, listing.data['generation']),),
    )
    assert changed_group.error.code == 'controlled_resource'
