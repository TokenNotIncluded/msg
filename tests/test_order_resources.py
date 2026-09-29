"""Ordinary Order revisions are authority, not an unchecked second write model."""
from datetime import timedelta

import pytest

from msg.core.codec import b64, canonical, loads, wire
from msg.core.errors import Failure
from msg.core.models import ResourceRef
from msg.market.order_resources import verify_source
from msg.plugins.money import _balance
from read_only_evidence import business_snapshot
from test_market_lifecycle import market
from test_orders import _intent
from test_service import NOW, call


async def buy(app, key, buyer, listing):
    result = await call(app, 'orders.buy', _intent(listing), key=key, subject=buyer,
                        contract_version=4)
    assert result.status == 'ok', wire(result)
    return result.data['order']


async def source(app, oid):
    async with app.metadata.transaction(write=False) as tx:
        resource = await verify_source(app, tx, oid)
        revision = await tx.revision(ResourceRef(id=oid))
        return resource, revision, loads(await app.contents.read_bytes(revision.content))


@pytest.mark.asyncio
async def test_order_resource_accept_appends_revision_without_rewriting_signed_origin(installed):
    app, root = installed
    sk, seller, bk, buyer, listing, _ = await market(app, root)
    order = await buy(app, bk, buyer, listing)
    oid = order['id']
    first, original, body = await source(app, oid)
    assert first.type == 'order' and first.mode == 0o400
    assert body['order']['state'] == 'delivered'
    assert body['contract']['creation_request']['contract_version'] == 4
    async with app.metadata.transaction(write=False) as tx:
        assert await tx.path(oid) == (await tx.path(buyer)) + '/orders/' + oid
    # The seller's role-trimmed market view is separate from buyer-only source bytes.
    assert (await call(app, 'orders.get', {'order_id': oid}, key=sk, subject=seller)).status == 'ok'
    assert (await call(app, 'discovery.get', {'id': oid}, key=sk, subject=seller)).status == 'error'
    for op, args in [('content.chmod', {'id': oid, 'mode': '0644'}),
                     ('file.write', {'id': oid, 'base_revision': original.id, 'data': b64(b'forged')}),
                     ('content.archive', {'id': oid})]:
        rejected = await call(app, op, args, key=bk, subject=buyer,
                              expected=((oid, first.generation),))
        assert rejected.status == 'error' and rejected.error.code in {
            'permission_denied', 'controlled_resource', 'order_controlled_resource', 'not_editable'}, wire(rejected)
    delivery = await call(app, 'delivery.get', {'order_id': oid}, key=bk, subject=buyer, contract_version=2)
    accepted = await call(app, 'delivery.accept', {'order_id': oid,
        'delivery_digest': delivery.data['delivery']['delivery_digest']}, key=bk, subject=buyer,
        contract_version=2, rid='resource-accept')
    assert accepted.status == 'ok', wire(accepted)
    second, revision, current = await source(app, oid)
    assert second.generation == first.generation + 1 and revision.parents == (original.id,)
    assert current['order']['state'] == 'settled' and current['contract'] == body['contract']
    assert loads(await app.contents.read_bytes(original.content)) == body
    replay = await call(app, 'delivery.accept', {'order_id': oid,
        'delivery_digest': delivery.data['delivery']['delivery_digest']}, key=bk, subject=buyer,
        contract_version=2, rid='resource-accept')
    assert replay.replayed and (await source(app, oid))[0] == second


@pytest.mark.asyncio
async def test_sql_state_drift_cannot_be_read_republished_or_used_to_release(installed):
    app, root = installed
    _sk, _seller, bk, buyer, listing, _ = await market(app, root)
    order = await buy(app, bk, buyer, listing)
    oid = order['id']
    resource, _, _ = await source(app, oid)
    async with app.metadata.transaction(write=True) as tx:
        tx.execute("UPDATE store_orders SET state='funded' WHERE id=?", (oid,), write=True)
    before = await business_snapshot(app)
    for operation, args, version in [('orders.get', {'order_id': oid}, 1),
            ('orders.cancel', {'order_id': oid}, 2),
            ('orders.buy', _intent(listing), 4)]:
        result = await call(app, operation, args, key=bk, subject=buyer, contract_version=version)
        assert result.error.code == 'order_resource_projection_mismatch', wire(result)
    assert await business_snapshot(app) == before
    async with app.metadata.transaction(write=False) as tx:
        assert await tx.resource(oid) == resource


@pytest.mark.asyncio
async def test_missing_order_body_blocks_reads_and_money_before_mutation(installed, monkeypatch):
    app, root = installed
    _sk, _seller, bk, buyer, listing, _ = await market(app, root)
    order = await buy(app, bk, buyer, listing)
    _, revision, _ = await source(app, order['id'])
    read = app.contents.read_bytes
    async def missing(ref):
        if ref == revision.content:
            raise FileNotFoundError('fixture')
        return await read(ref)
    monkeypatch.setattr(app.contents, 'read_bytes', missing)
    before = await business_snapshot(app)
    result = await call(app, 'orders.get', {'order_id': order['id']}, key=bk, subject=buyer)
    assert result.error.code == 'content_missing'
    result = await call(app, 'orders.cancel', {'order_id': order['id']}, key=bk, subject=buyer, contract_version=2)
    assert result.error.code == 'content_missing'
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_revision_publication_failure_rolls_back_order_delivery_and_ledger(installed, monkeypatch):
    app, root = installed
    _sk, _seller, bk, buyer, listing, _ = await market(app, root)
    before = await business_snapshot(app)
    original = app.contents.commit_revision
    async def fail(parent, revision):
        await original(parent, revision)
        if revision.resource_id.startswith('ord_'):
            raise Failure('injected_order_revision_failure')
    monkeypatch.setattr(app.contents, 'commit_revision', fail)
    result = await call(app, 'orders.buy', _intent(listing), key=bk, subject=buyer, contract_version=4)
    assert result.error.code == 'injected_order_revision_failure', wire(result)
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_timeout_internal_writer_advances_source_and_refunds_once(installed):
    app, root = installed
    _sk, seller, bk, buyer, listing, _ = await market(app, root, mode='service', kind='service')
    order = await buy(app, bk, buyer, listing)
    first, original, _ = await source(app, order['id'])
    app.clock = lambda: NOW + timedelta(days=2)
    from msg.market.escrow import resolve_due
    assert await resolve_due(app) == [order['id']]
    assert await resolve_due(app) == []
    current, revision, body = await source(app, order['id'])
    assert revision.parents == (original.id,) and current.generation == first.generation + 1
    assert body['order']['state'] == 'refunded'
    async with app.metadata.transaction(write=False) as tx:
        assert (_balance(tx, buyer), _balance(tx, seller)) == (20_000_000, 0)


@pytest.mark.asyncio
async def test_signed_arbitration_internal_entrypoint_advances_order_source(installed, monkeypatch):
    import test_market_arbitration as arb
    async def buy_v4(app, key, buyer, listing):
        return await call(app, 'orders.buy', _intent(listing), key=key, subject=buyer, contract_version=4)
    monkeypatch.setattr(arb, 'buy', buy_v4)
    app, _, members, _, seller, bk, buyer, order, opened = await arb.configured(installed)
    first, previous, body = await source(app, order['id'])
    assert body['order']['state'] == 'disputed'
    panel = opened['panel']
    base = await arb.reasoned_base(app, members[panel[0]], panel[0], opened['case_id'])
    for member in panel[:2]:
        vote = await arb.vote(app, members[member], member, base)
        assert vote.status == 'ok', wire(vote)
    from msg.market.arbitration import _read, execute
    async with app.metadata.transaction(write=True) as tx:
        case = _read(tx, opened['case_id'])
        await execute(app, tx, case, vote.data['decision']['id'], NOW, buyer, 'internal-arbitration')
    current, revision, body = await source(app, order['id'])
    assert current.generation == first.generation + 1 and revision.parents == (previous.id,)
    assert body['order']['state'] == 'settled'
    async with app.metadata.transaction(write=False) as tx:
        assert (_balance(tx, buyer), _balance(tx, seller)) == (17_000_000, 3_000_000)


@pytest.mark.asyncio
async def test_account_scoped_entitlement_replay_keeps_original_authority(installed):
    from test_market_account_scope import restricted_key
    from test_market_redemption import setup_offer
    from test_entitlement_orders import intent
    app, root = installed
    key, owner, args, _ = await setup_offer(app, root)
    scoped = await restricted_key(app, key, owner, 'money.redeem', 3, account=True)
    signed = await intent(app, args)
    first = await call(app, 'money.redeem', signed, key=scoped, subject=owner,
                       contract_version=3, rid='scoped-resource')
    assert first.status == 'ok', wire(first)
    repeated = await call(app, 'money.redeem', signed, key=scoped, subject=owner,
                          contract_version=3, rid='scoped-resource')
    assert repeated.replayed and repeated.data == first.data, wire(repeated)
    assert first.resources == () and first.data['order_resources'][0]['id'] == first.data['order']['id']


@pytest.mark.asyncio
async def test_accept_revision_failure_rolls_back_release_and_buyer_ack(installed, monkeypatch):
    app, root = installed
    _sk, _seller, bk, buyer, listing, _ = await market(app, root)
    order = await buy(app, bk, buyer, listing)
    delivery = await call(app, 'delivery.get', {'order_id': order['id']},
                          key=bk, subject=buyer, contract_version=2)
    before = await business_snapshot(app)
    original = app.contents.commit_revision
    async def fail(parent, revision):
        await original(parent, revision)
        if revision.resource_id == order['id']:
            raise Failure('injected_accept_revision_failure')
    monkeypatch.setattr(app.contents, 'commit_revision', fail)
    result = await call(app, 'delivery.accept', {'order_id': order['id'],
        'delivery_digest': delivery.data['delivery']['delivery_digest']},
        key=bk, subject=buyer, contract_version=2)
    assert result.error.code == 'injected_accept_revision_failure', wire(result)
    assert await business_snapshot(app) == before
    assert (await source(app, order['id']))[2]['order']['state'] == 'delivered'


@pytest.mark.asyncio
async def test_invalid_source_signature_cannot_authorize_lifecycle_or_read(installed):
    from dataclasses import replace
    app, root = installed
    _sk, _seller, bk, buyer, listing, _ = await market(app, root, mode='service', kind='service')
    order = await buy(app, bk, buyer, listing)
    _, revision, _ = await source(app, order['id'])
    corrupted = replace(revision, signature=replace(revision.signature, value=b'x' * 64))
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('UPDATE revisions SET body=? WHERE id=?',
                   (canonical(corrupted).decode(), revision.id), write=True)
    before = await business_snapshot(app)
    for operation, version in [('orders.get', 1), ('orders.cancel', 2)]:
        result = await call(app, operation, {'order_id': order['id']}, key=bk, subject=buyer,
                            contract_version=version)
        assert result.error.code == 'invalid_signature', wire(result)
    assert await business_snapshot(app) == before
