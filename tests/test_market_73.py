"""Versioned fulfillment, objective refunds, inventory races and crash recovery."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import subprocess

import pytest

from msg.admin.backups import backup, restore
from msg.admin.money import apply_money
from msg.application import Application
from msg.config import load_settings
from msg.core.codec import b64, canonical, digest, wire
from msg.core.errors import Failure
from msg.core.models import ResourceRef
from msg.core.requests import request_for
from msg.plugins import delivery, order_engine
from msg.plugins.money import _balance, _supply
from test_orders import _intent, _sale
from test_service import NOW, call, register


async def market(installed, *, mode='managed_instant', policy='managed-instant-v1', quantity=2):
    app, root = installed
    sk, seller, _ = await register(app, 'order-seller')
    bk, buyer, _ = await register(app, 'order-buyer')
    await apply_money(app, root, action='mint', operator='test', amount_minor=20_000_000)
    await apply_money(app, root, action='transfer', operator='test', subject_id=buyer, amount_minor=20_000_000)
    if mode == 'managed_instant':
        listing, _ = await _sale(app, sk, seller, quantity=quantity, policy=policy)
    else:
        created = await call(app, 'store.listing_create', {
            'name':'manual', 'item_kind':'service' if mode=='service' else 'secret',
            'price_minor':5_000_000, 'quantity':quantity, 'currency_id':'primary',
            'delivery_mode':mode, 'escrow_policy':policy, 'dispute_policy':'dispute-v1',
            'terms':'Explicit buyer acceptance, no automatic quality judgement.'}, key=sk, subject=seller)
        assert created.status == 'ok', wire(created)
        lid = created.resources[0].id
        active = await call(app, 'store.listing_update', {'id':lid, 'state':'active'},
            key=sk, subject=seller, expected=((lid,created.data['generation']),))
        assert active.status == 'ok', wire(active)
        listing = active.data['listing']
    return app, (sk,seller), (bk,buyer), listing


async def buy(app, identity, listing, *, op='orders.buy', rid=None):
    key, buyer = identity
    result = await call(app, op, _intent(listing), key=key, subject=buyer, rid=rid)
    assert result.status == 'ok', wire(result)
    return result


async def facts(app):
    async with app.metadata.transaction(write=False) as tx:
        return {t:tx.one('SELECT COUNT(*) FROM '+t)[0] for t in
            ('money_ledger','store_orders','store_order_events','store_deliveries','messages','events','results','ledger_accounts')}


@pytest.mark.asyncio
async def test_instant_delivery_settles_named_legs_once_and_records_real_policy_consent(installed):
    app, (sk,seller), identity, listing = await market(installed)
    bk,buyer = identity
    result = await buy(app,identity,listing,rid='instant-one')
    order = result.data['order']; oid = order['id']
    assert order['state'] == order['payment_status'] == 'settled'
    received = await call(app,'delivery.get',{'order_id':oid},key=bk,subject=buyer)
    data = received.data['delivery']
    assert data['manifest'] == {'text':'msg.lmm.best store selftest'}
    assert data['payloads'][0]['data'] == b64(b'delivery-ok\n')
    assert data['payloads'][0]['digest'] == digest(b'delivery-ok\n')
    assert data['package_digest'] == order['package_digest']
    assert data['state']=='prepared' and data['claimed_at'] is None
    assert data['delivery_digest'] == result.data['delivery']['delivery_digest']
    history = await call(app,'orders.history',{'order_id':oid},key=bk,subject=buyer)
    events = history.data['events']
    assert [e['to_state'] for e in events] == ['created','funded','delivered','accepted','settled']
    assert events[-2]['reason'] == 'policy_delivery'  # NOT a fabricated buyer inspection signature.
    before = await facts(app)
    replay = await buy(app,identity,listing,rid='instant-one')
    assert replay.replayed and replay.data == result.data and await facts(app) == before
    assert (await call(app,'delivery.accept',{'order_id':oid,'delivery_digest':data['delivery_digest']},
                       key=bk,subject=buyer)).error.code == 'delivery_not_acceptable'
    assert (await call(app,'delivery.get',{'order_id':oid},key=sk,subject=seller)).error.code == 'order_not_found'
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,buyer)==15_000_000 and _balance(tx,seller)==5_000_000 and _supply(tx)==20_000_000
        assert tx.rows('SELECT entry_key FROM money_ledger WHERE actor=? AND request_id=? ORDER BY entry_key',
                       (buyer,'instant-one')) == [('order_fund',),('order_release',)]
        assert len(tx.rows('SELECT body FROM messages WHERE recipient=?',(buyer,))) == 1


@pytest.mark.asyncio
async def test_unpaid_quote_keeps_old_price_but_does_not_reserve_last_quantity(installed):
    app, (sk,seller), identity, listing = await market(installed,quantity=1)
    bk,buyer = identity
    a = (await buy(app,identity,listing,op='orders.create')).data['order']
    b = (await buy(app,identity,listing,op='orders.create')).data['order']
    assert a['state']=='created' and a['payment_status']=='pending'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0] == 2
        resource = await tx.resource(listing['listing_id'])
    changed = await call(app,'store.listing_update',{'id':resource.id,'price_minor':9_000_000,'terms':'New terms'},
        key=sk,subject=seller,expected=((resource.id,resource.generation),))
    assert changed.status=='ok',wire(changed)
    async def pay(order, **changes):
        return await call(app,'orders.pay',{'order_id':order['id'],'quote_digest':order['quote_digest'],
            'currency_id':'primary','total_price_minor':5_000_000,**changes},key=bk,subject=buyer)
    denied = await pay(a,total_price_minor=4_000_000)
    assert denied.error.code=='order_quote_mismatch'
    results = await asyncio.gather(pay(a),pay(b))
    assert sorted(r.status for r in results)==['error','ok']
    assert next(r for r in results if r.status=='error').error.code=='quantity_unavailable'
    old = next(r for r in results if r.status=='ok').data['order']
    assert old['unit_price_minor']==5_000_000 and old['listing_revision']==listing['listing_revision']
    assert old['terms_digest']==listing['terms_digest'] and old['state']=='settled'
    unpaid = b if old['id']==a['id'] else a
    cancelled = await call(app,'orders.cancel',{'order_id':unpaid['id']},key=bk,subject=buyer)
    assert cancelled.data['refund'] is None and cancelled.data['order']['payment_status']=='cancelled'
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,buyer)==15_000_000 and _balance(tx,seller)==5_000_000
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0]==4


@pytest.mark.asyncio
@pytest.mark.parametrize('stage',['delivery','release'])
async def test_auto_checkout_failure_after_each_irreversible_step_rolls_back_all_facts(installed,monkeypatch,stage):
    app, _, identity, listing = await market(installed)
    before = await facts(app)
    if stage=='delivery':
        original=delivery._insert_delivery
        def fail(*args,**kwargs):
            original(*args,**kwargs)
            raise Failure('injected_after_delivery')
        monkeypatch.setattr(delivery,'_insert_delivery',fail)
    else:
        original=delivery._post_transfer
        def fail(*args,**kwargs):
            original(*args,**kwargs)
            raise Failure('injected_after_release')
        monkeypatch.setattr(delivery,'_post_transfer',fail)
    key,buyer=identity
    result=await call(app,'orders.buy',_intent(listing),key=key,subject=buyer)
    assert result.error.code=='injected_after_'+stage
    assert await facts(app)==before
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,buyer)==20_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['package','delivery','content'])
async def test_objective_corruption_refunds_once_never_releases_seller(installed,fault,monkeypatch):
    app, (_,seller), (bk,buyer), listing=await market(installed,policy='escrow-v1')
    order=(await buy(app,(bk,buyer),listing)).data['order']; oid=order['id']
    if fault=='delivery':
        prepared=await call(app,'delivery.prepare',{'order_id':oid},key=bk,subject=buyer)
        assert prepared.status=='ok',wire(prepared)
    async with app.metadata.transaction(write=True) as tx:
        if fault=='package':
            pid=tx.one('SELECT package_id FROM store_orders WHERE id=?',(oid,))[0]
            tx.execute('UPDATE store_packages SET manifest=? WHERE id=?',('{"tampered":true}',pid),write=True)
        elif fault=='delivery':
            tx.execute('DELETE FROM store_deliveries WHERE order_id=?',(oid,),write=True)
    if fault=='content':
        original=app.contents.read_bytes
        async def missing(*args,**kwargs):
            raise Failure('content_missing')
        monkeypatch.setattr(app.contents,'read_bytes',missing)
    result=await call(app,'orders.resolve',{'order_id':oid},key=bk,subject=buyer,rid='objective-one')
    assert result.status=='ok',wire(result)
    assert result.data['order']['state']=='refunded'
    if fault=='content':monkeypatch.setattr(app.contents,'read_bytes',original)
    repeat=await call(app,'orders.resolve',{'order_id':oid},key=bk,subject=buyer,rid='objective-one')
    assert repeat.replayed and repeat.data==result.data
    twice=await call(app,'orders.resolve',{'order_id':oid},key=bk,subject=buyer)
    assert twice.error.code=='order_not_refundable'
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,buyer)==20_000_000 and _balance(tx,seller)==0 and _supply(tx)==20_000_000
        assert tx.one("SELECT COUNT(*) FROM money_ledger WHERE reference=?",('order_refund:'+oid,))[0]==1
        assert tx.one("SELECT COUNT(*) FROM money_ledger WHERE reference=?",('order_release:'+oid,))[0]==0


@pytest.mark.asyncio
async def test_expired_undelivered_order_refunds_but_transient_storage_error_does_not(installed,monkeypatch):
    app, _, (bk,buyer), listing=await market(installed,policy='escrow-v1')
    order=(await buy(app,(bk,buyer),listing)).data['order']; args={'order_id':order['id']}
    async def transient(*args,**kwargs):raise Failure('content_store_error')
    with monkeypatch.context() as scoped:
        scoped.setattr(app.contents,'read_bytes',transient)
        assert (await call(app,'orders.resolve',args,key=bk,subject=buyer)).error.code=='content_store_error'
    now=NOW+timedelta(seconds=order_engine.DELIVERY_TTL+1)
    app.clock=lambda:now
    await app.load()
    result=await app.executor.execute(request_for('orders.resolve',args,app.settings.service_url,
        subject=buyer,signer=bk,expires_at=now+timedelta(seconds=120)))
    assert result.status=='ok' and result.data['order']['state']=='refunded',wire(result)


@pytest.mark.asyncio
async def test_service_assertion_needs_buyer_acceptance_or_seller_refund_no_quality_oracle(installed):
    app,(sk,seller),(bk,buyer),listing=await market(installed,mode='service',policy='service-accept-v1')
    order=(await buy(app,(bk,buyer),listing)).data['order']; args={'order_id':order['id']}
    submitted=await call(app,'delivery.submit',{**args,'completion_statement':'Completed the work.'},key=sk,subject=seller)
    assert submitted.status=='ok',wire(submitted)
    assert (await call(app,'orders.resolve',args,key=bk,subject=buyer)).error.code=='no_objective_refund'
    assert (await call(app,'orders.dispute',args,key=bk,subject=buyer)).data['order']['state']=='disputed'
    accept=await call(app,'delivery.accept',{**args,'delivery_digest':submitted.data['delivery']['delivery_digest']},key=bk,subject=buyer)
    assert accept.error.code=='delivery_not_acceptable'
    refunded=await call(app,'orders.refund',args,key=sk,subject=seller)
    assert refunded.data['order']['state']=='refunded'
    second=(await buy(app,(bk,buyer),listing)).data['order']; args={'order_id':second['id']}
    submitted=await call(app,'delivery.submit',{**args,'completion_statement':'Another completed task.'},key=sk,subject=seller)
    accepted=await call(app,'delivery.accept',{**args,'delivery_digest':submitted.data['delivery']['delivery_digest']},key=bk,subject=buyer)
    assert accepted.status=='ok' and accepted.data['state']=='settled',wire(accepted)
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,buyer)==15_000_000 and _balance(tx,seller)==5_000_000


@pytest.mark.asyncio
async def test_sealed_manual_binds_current_buyer_key_and_never_releases_before_accept(installed):
    app,(sk,seller),(bk,buyer),listing=await market(installed,mode='sealed_manual',policy='sealed-manual-v1')
    order=(await buy(app,(bk,buyer),listing)).data['order']; args={'order_id':order['id']}
    key=(await call(app,'orders.recipient_key',args,key=sk,subject=seller)).data
    encrypted=subprocess.run(['age','-r',key['recipient']],input=b'only-the-buyer',stdout=subprocess.PIPE,check=True).stdout
    put=await call(app,'content.file_put',{'parent':'/@order-seller/files','name':'sealed.age',
        'data':b64(encrypted),'media_type':'application/octet-stream'},key=sk,subject=seller)
    assert put.status=='ok',wire(put)
    packet={**args,'payload_ref':wire(put.resources[0]),'recipient_key_id':'wrong-key'}
    assert (await call(app,'delivery.submit',packet,key=sk,subject=seller)).error.code=='recipient_key_changed'
    packet['recipient_key_id']=key['key_id']
    submitted=await call(app,'delivery.submit',packet,key=sk,subject=seller)
    assert submitted.status=='ok',wire(submitted)
    received=await call(app,'delivery.get',args,key=bk,subject=buyer)
    assert received.data['delivery']['payloads'][0]['data']==b64(encrypted)
    assert received.data['delivery']['manifest']['verification']=='buyer_decrypts'
    assert (await call(app,'delivery.get',args,key=sk,subject=seller)).error.code=='order_not_found'
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,seller)==0
    accepted=await call(app,'delivery.accept',{**args,'delivery_digest':submitted.data['delivery']['delivery_digest']},key=bk,subject=buyer)
    assert accepted.status=='ok',wire(accepted)


@pytest.mark.asyncio
async def test_refund_accept_race_can_move_escrow_only_once(installed):
    app,(sk,seller),(bk,buyer),listing=await market(installed,policy='escrow-v1')
    order=(await buy(app,(bk,buyer),listing)).data['order']; args={'order_id':order['id']}
    prepared=await call(app,'delivery.prepare',args,key=bk,subject=buyer)
    results=await asyncio.gather(
        call(app,'delivery.accept',{**args,'delivery_digest':prepared.data['delivery']['delivery_digest']},key=bk,subject=buyer),
        call(app,'orders.refund',args,key=sk,subject=seller))
    assert sorted(r.status for r in results)==['error','ok']
    async with app.metadata.transaction(write=False) as tx:
        row=tx.one('SELECT escrow_subject,state FROM store_orders WHERE id=?',(order['id'],))
        assert row[1] in {'settled','refunded'} and _balance(tx,row[0])==0
        assert tx.one('SELECT COUNT(*) FROM money_ledger WHERE debit_account=?',(row[0],))[0]==1
        assert _balance(tx,buyer)+_balance(tx,seller)==20_000_000


@pytest.mark.asyncio
async def test_backup_restores_pending_and_settled_orders_with_history_and_payloads(installed,tmp_path,pg_dsn):
    app,_,identity,listing=await market(installed)
    bk,buyer=identity
    settled=await buy(app,identity,listing,rid='restored-instant')
    unpaid=await buy(app,identity,listing,op='orders.create')
    archive=tmp_path/'orders.zip'
    await backup(app,archive)
    restore(archive,tmp_path/'restored-etc',tmp_path/'restored-data',postgres_dsn=pg_dsn)
    restored=Application(load_settings(tmp_path/'restored-etc'),clock=lambda:NOW)
    await restored.load()
    try:
        oid=settled.data['order']['id']
        got=await call(restored,'delivery.get',{'order_id':oid},key=bk,subject=buyer)
        assert got.data['delivery']['payloads'][0]['data']==b64(b'delivery-ok\n')
        current=await call(restored,'orders.get',{'order_id':unpaid.data['order']['id']},key=bk,subject=buyer)
        assert current.data['order']['state']=='created'
        assert (await call(restored,'orders.buy',_intent(listing),key=bk,subject=buyer,rid='restored-instant')).error.code=='writes_paused'
        (restored.settings.config_dir/'recovery-drill.json').unlink()  # Only this disposable restore.
        before=await facts(restored)
        replay=await call(restored,'orders.buy',_intent(listing),key=bk,subject=buyer,rid='restored-instant')
        assert replay.replayed and replay.data==settled.data and await facts(restored)==before
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_funded_order_escrow_survives_restore_and_refunds_once(installed,tmp_path,pg_dsn):
    from msg.admin.market_check import inspect_clearing
    app,_,(key,buyer),listing=await market(installed,policy='escrow-v1')
    bought=await buy(app,(key,buyer),listing,rid='funded-backup')
    oid=bought.data['order']['id']; args={'order_id':oid}
    archive=tmp_path/'funded.zip'
    await backup(app,archive)
    restore(archive,tmp_path/'funded-etc',tmp_path/'funded-data',postgres_dsn=pg_dsn)
    restored=Application(load_settings(tmp_path/'funded-etc'),clock=lambda:NOW)
    await restored.load()
    try:
        got=await call(restored,'orders.get',args,key=key,subject=buyer)
        assert got.data['order']['state']=='funded'
        async with restored.metadata.transaction(write=False) as tx:
            assert inspect_clearing(restored,tx)['conserved']
            escrow=tx.one('SELECT escrow_subject FROM store_orders WHERE id=?',(oid,))[0]
            assert _balance(tx,escrow)==5_000_000
        (restored.settings.config_dir/'recovery-drill.json').unlink()  # Disposable restore only.
        first=await call(restored,'orders.cancel',args,key=key,subject=buyer,rid='funded-cancel')
        retry=await call(restored,'orders.cancel',args,key=key,subject=buyer,rid='funded-cancel')
        assert first.status=='ok' and retry.replayed and retry.data==first.data
        async with restored.metadata.transaction(write=False) as tx:
            assert inspect_clearing(restored,tx)['conserved'] and _balance(tx,buyer)==20_000_000
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_explicit_claim_after_policy_settlement_records_receipt_without_second_payment(installed):
    app,_,(key,buyer),listing=await market(installed)
    bought=await buy(app,(key,buyer),listing)
    args={'order_id':bought.data['order']['id'],
          'delivery_digest':bought.data['delivery']['delivery_digest']}
    async with app.metadata.transaction(write=False) as tx:
        count=tx.one('SELECT COUNT(*) FROM money_ledger')[0]
    first=await call(app,'delivery.claim',args,key=key,subject=buyer,rid='explicit-received')
    retry=await call(app,'delivery.claim',args,key=key,subject=buyer,rid='explicit-received')
    assert first.status=='ok' and first.data['state']=='claimed' and retry.replayed
    duplicate=await call(app,'delivery.claim',args,key=key,subject=buyer)
    assert duplicate.error.code=='delivery_already_claimed'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0]==count
