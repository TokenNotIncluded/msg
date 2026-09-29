"""Stage 1: new local offers have one signed Resource/Revision writer."""
import pytest

from msg.admin.money import apply_offer
from msg.core.codec import canonical, digest, wire
from msg.core.errors import Failure
from msg.core.models import ResourceRef
from msg.market.offer_resources import authoritative_offer
from msg.security.crypto import verify
from test_market_redemption import setup_offer
from test_service import call


@pytest.mark.asyncio
async def test_new_offer_is_root_signed_listing_with_immutable_price_history(installed):
    app, root = installed
    key, owner, args, fields = await setup_offer(app, root)
    async with app.metadata.transaction(write=False) as tx:
        resource = await tx.resource(args['offer_id'])
        assert resource.type == 'listing' and resource.parent == 't_store'
        assert resource.owner == 'u_root' and resource.mode == 0o444
        revision = await tx.revision(ResourceRef(id=resource.id))
        assert revision.id == args['price_revision']
        manifest = {k: v for k, v in wire(revision).items() if k not in {'manifest_digest', 'signature'}}
        verify(root.public_key, canonical(manifest), revision.signature, purpose='revision')
        assert digest(manifest) == revision.manifest_digest
        original = await app.contents.read_bytes(revision.content)
        assert (await authoritative_offer(app, tx, resource.id))['price_minor'] == 2
    ordinary = await call(app, 'store.listing_get', {'id': resource.id})
    assert ordinary.status == 'ok', wire(ordinary)
    assert ordinary.data['listing']['server_offer']['offer_id'] == args['offer_id']
    assert ordinary.data['listing']['listing_revision'] == args['price_revision']
    pending = await call(app, 'money.redeem', {**args, 'defer': True}, key=key, subject=owner,
                         contract_version=2)
    assert pending.status == 'ok', wire(pending)
    updated = await apply_offer(app, root, action='set', operator='test',
                                offer_id=args['offer_id'], fields={**fields, 'price_minor': 3})
    assert updated['offer']['price_revision'] != args['price_revision']
    async with app.metadata.transaction(write=False) as tx:
        assert await app.contents.read_bytes(revision.content) == original
        current = await tx.revision(ResourceRef(id=resource.id))
        assert current.parents == (revision.id,)
        assert (await authoritative_offer(app, tx, resource.id)) == updated['offer']
    settled = await call(app, 'money.purchase_settle', {'purchase_id': pending.data['purchase']['id']},
                         key=key, subject=owner)
    assert settled.status == 'ok' and settled.data['purchase']['total_minor'] == 10
    disabled = await apply_offer(app, root, action='disable', operator='test', offer_id=args['offer_id'])
    assert disabled['offer']['price_revision'] == updated['offer']['price_revision']
    ordinary = await call(app, 'store.listing_get', {'id': resource.id})
    assert ordinary.data['listing']['state'] == 'paused'
    assert len((await call(app, 'money.offers', {})).data['offers']) == 0


@pytest.mark.asyncio
async def test_projected_price_tampering_cannot_change_signed_offer_or_spend(installed):
    app, root = installed
    key, owner, args, _ = await setup_offer(app, root)
    async with app.metadata.transaction(write=True) as tx:
        original = await authoritative_offer(app, tx, args['offer_id'])
        tx.execute('UPDATE server_offers SET price_minor=1 WHERE offer_id=?',
                   (args['offer_id'],), write=True)
        count = tx.one('SELECT COUNT(*) FROM money_ledger')[0]
    denied = await call(app, 'money.redeem', args, key=key, subject=owner)
    assert denied.error.code == 'offer_projection_mismatch'
    assert (await call(app, 'money.offers', {})).error.code == 'offer_projection_mismatch'
    with pytest.raises(Failure, match='offer_projection_mismatch'):
        await apply_offer(app, root, action='disable', operator='test', offer_id=args['offer_id'])
    async with app.metadata.transaction(write=False) as tx:
        assert await authoritative_offer(app, tx, args['offer_id']) == original
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0] == count
        assert tx.one('SELECT COUNT(*) FROM money_purchases')[0] == 0


@pytest.mark.asyncio
async def test_legacy_offer_is_not_silently_adopted_on_read_or_edit(installed):
    app, root = installed
    _, _, _, fields = await setup_offer(app, root)
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('''INSERT INTO server_offers
            SELECT 'legacy-offer',resource_kind,unit,price_minor,min_quantity,max_quantity,
                   entitlement_kind,duration_seconds,enabled,'price_legacy'
            FROM server_offers WHERE offer_id='web-bytes' ''', write=True)
    assert (await call(app, 'money.offers', {})).status == 'ok'
    await apply_offer(app, root, action='set', operator='test', offer_id='legacy-offer', fields=fields)
    native = await call(app, 'store.listing_get', {'id': 'legacy-offer'}, contract_version=2)
    assert native.error.code == 'listing_not_found'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT 1 FROM server_offer_resources WHERE offer_id=?', ('legacy-offer',)) is None
        assert tx.one('SELECT 1 FROM resources WHERE id=?', ('legacy-offer',)) is None


@pytest.mark.asyncio
async def test_local_offer_revision_failure_rolls_back_projection_and_resource(installed, monkeypatch):
    app, root = installed
    _, _, _, fields = await setup_offer(app, root)
    async with app.metadata.transaction(write=False) as tx:
        before = {table: tx.one('SELECT COUNT(*) FROM ' + table)[0] for table in
                  ('resources', 'revisions', 'server_offers', 'server_offer_resources', 'events', 'audit')}
    commit = app.contents.commit_revision
    async def fail_after_commit(*args):
        await commit(*args)
        raise Failure('injected_offer_revision_failure')
    monkeypatch.setattr(app.contents, 'commit_revision', fail_after_commit)
    with pytest.raises(Failure, match='injected_offer_revision_failure'):
        await apply_offer(app, root, action='set', operator='test', offer_id='rollback-offer', fields=fields)
    async with app.metadata.transaction(write=False) as tx:
        assert {table: tx.one('SELECT COUNT(*) FROM ' + table)[0] for table in before} == before
    monkeypatch.setattr(app.contents, 'commit_revision', commit)
    retried = await apply_offer(app, root, action='set', operator='test', offer_id='rollback-offer', fields=fields)
    assert retried['offer']['enabled']


@pytest.mark.asyncio
async def test_remote_seller_cannot_reprice_or_purchase_root_offer_via_store(installed):
    app, root = installed
    key, owner, args, _ = await setup_offer(app, root)
    async with app.metadata.transaction(write=False) as tx:
        resource = await tx.resource(args['offer_id'])
        before = await authoritative_offer(app, tx, resource.id)
    denied = await call(app, 'store.listing_update', {'id': resource.id, 'price_minor': 1},
                        key=key, subject=owner, expected=((resource.id, resource.generation),))
    assert denied.error.code == 'listing_not_found'
    denied = await call(app, 'orders.buy', {'listing_id': resource.id,
        'listing_revision': resource.revision, 'quantity': 1, 'currency_id': 'primary',
        'total_price_minor': 2}, key=key, subject=owner, contract_version=4)
    assert denied.status == 'error'
    async with app.metadata.transaction(write=False) as tx:
        assert await authoritative_offer(app, tx, resource.id) == before
        assert tx.one('SELECT COUNT(*) FROM store_orders')[0] == 0


@pytest.mark.asyncio
async def test_existing_resource_id_collision_cannot_be_overwritten_by_local_offer(installed):
    from dataclasses import replace
    app, root = installed
    _, owner, args, fields = await setup_offer(app, root)
    async with app.metadata.transaction(write=True) as tx:
        template = await tx.resource(args['offer_id'])
        collision = replace(template, id='collision-offer', name='collision-offer', owner=owner,
                            revision=None, generation=0)
        await tx.insert(collision)
    with pytest.raises(Failure, match='offer_resource_conflict'):
        await apply_offer(app, root, action='set', operator='test', offer_id=collision.id, fields=fields)
    async with app.metadata.transaction(write=False) as tx:
        assert await tx.resource(collision.id) == collision
        assert tx.one('SELECT 1 FROM server_offers WHERE offer_id=?', (collision.id,)) is None
        assert tx.one('SELECT 1 FROM server_offer_resources WHERE offer_id=?', (collision.id,)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['web-bytes', 't_store'])
async def test_private_or_inactive_source_cannot_leak_through_legacy_offer_view(installed, target):
    from dataclasses import replace
    app, root = installed
    key, owner, args, _ = await setup_offer(app, root)
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(target)
        await tx.replace(replace(resource, mode=0o700, generation=resource.generation + 1),
                         resource.generation)
        count = tx.one('SELECT COUNT(*) FROM money_ledger')[0]
    assert (await call(app, 'money.offers', {})).error.code == 'offer_resource_invalid'
    denied = await call(app, 'money.redeem', args, key=key, subject=owner)
    assert denied.error.code == 'offer_resource_invalid'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0] == count
        assert tx.one('SELECT COUNT(*) FROM money_purchases')[0] == 0
