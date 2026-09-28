"""A purchased entitlement must be consumed by real hosting checks, not an IOU."""
import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from msg.admin.money import apply_money, apply_offer
from msg.application import Application
from msg.constants import ROOT_SUBJECT
from msg.core.codec import b64, wire
from msg.core.errors import Failure
from msg.core.requests import request_for
from msg.plugins import offers
from msg.plugins.hosting_capacity import ENTITLEMENT_KIND, extra_capacity
from msg.plugins.money import _balance, _supply, clearing_decision, MAX_MINOR
from test_service import NOW, call, register


async def setup_offer(app, root, *, duration=None):
    key, owner, _ = await register(app, 'capacity-buyer')
    await apply_money(app,root,action='mint',operator='test',amount_minor=100)
    await apply_money(app,root,action='transfer',operator='test',amount_minor=100,subject_id=owner)
    fields={'resource_kind':'website','unit':'byte','price_minor':2,'min_quantity':1,
            'max_quantity':40,'entitlement_kind':ENTITLEMENT_KIND,'duration_seconds':duration}
    published=await apply_offer(app,root,action='set',operator='test',offer_id='web-bytes',fields=fields)
    quote=published['offer']
    args={'offer_id':'web-bytes','quantity':5,'currency_id':'primary','price_revision':quote['price_revision']}
    return key,owner,args,fields


@pytest.mark.asyncio
async def test_real_quote_redeem_and_named_legs_are_atomic_and_replay_safe(installed):
    app,root=installed
    key,owner,args,_=await setup_offer(app,root)
    assert app.registry.resource_type('website').purchasable
    assert not offers.purchasable(app,'file',ENTITLEMENT_KIND)
    result=await call(app,'money.redeem',args,key=key,subject=owner,rid='capacity-one')
    assert result.status=='ok',wire(result)
    purchase=result.data['purchase']
    assert purchase['state']=='settled' and purchase['offer_snapshot']['price_minor']==2
    replay=await call(app,'money.redeem',args,key=key,subject=owner,rid='capacity-one')
    assert replay.replayed and replay.data==result.data
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,owner)==90 and _balance(tx,ROOT_SUBJECT)==10
        assert _balance(tx,purchase['escrow_account'])==0 and _supply(tx)==100
        assert extra_capacity(tx,owner,NOW)==5
        assert tx.one('SELECT COUNT(*) FROM resource_entitlements')[0]==1
        legs=tx.rows('SELECT entry_key FROM money_ledger WHERE actor=? AND request_id=? ORDER BY entry_key',
                     (owner,'capacity-one'))
        assert legs==[('purchase_final',),('purchase_fund',)]
        assert tx.one('SELECT COUNT(*) FROM identities WHERE id=?',(purchase['escrow_account'],))[0]==0
    denied=await call(app,'money.purchase_cancel',{'purchase_id':purchase['id']},key=key,subject=owner)
    assert denied.error.code=='purchase_not_pending'
    # A restart validates the new system-account source without reissuing funds.
    restarted=Application(app.settings,clock=lambda:NOW)
    await restarted.load()
    try:
        assert (await call(restarted,'money.purchase_get',{'purchase_id':purchase['id']},key=key,subject=owner)).data['purchase']['state']=='settled'
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_pending_locks_quote_then_cancel_or_settle_with_one_final_posting(installed):
    app,root=installed
    key,owner,args,fields=await setup_offer(app,root)
    args={**args,'defer':True}
    pending=await call(app,'money.redeem',args,key=key,subject=owner,contract_version=2)
    assert pending.status=='ok',wire(pending)
    purchase=pending.data['purchase']; pid=purchase['id']
    await apply_offer(app,root,action='set',operator='test',offer_id='web-bytes',fields={**fields,'price_minor':3})
    await apply_offer(app,root,action='disable',operator='test',offer_id='web-bytes')
    async with app.metadata.transaction(write=False) as tx:
        assert extra_capacity(tx,owner,NOW)==0 and _balance(tx,purchase['escrow_account'])==10
    # Editing/disabling the catalogue cannot retroactively change a pending quote.
    results=await asyncio.gather(*[call(app,'money.purchase_settle',{'purchase_id':pid},key=key,subject=owner,rid=r)
                                   for r in ('settle-a','settle-b')])
    assert sorted(r.status for r in results)==['error','ok']
    good=next(r for r in results if r.status=='ok')
    assert good.data['purchase']['total_minor']==10
    assert next(r for r in results if r.status=='error').error.code=='purchase_not_pending'
    other_key,other,_=await register(app,'capacity-outsider')
    for id_ in (pid,'not-an-order'):
        assert (await call(app,'money.purchase_get',{'purchase_id':id_},key=other_key,subject=other)).error.code=='purchase_not_found'


@pytest.mark.asyncio
async def test_fulfiller_failure_rolls_back_postings_purchase_entitlement_and_event(installed,monkeypatch):
    app,root=installed
    key,owner,args,_=await setup_offer(app,root)
    provider=offers.ENTITLEMENT_FULFILLERS[ENTITLEMENT_KIND]
    def fail_after_post(*a):
        provider[3](*a)
        raise Failure('injected_grant_failure')
    monkeypatch.setitem(offers.ENTITLEMENT_FULFILLERS,ENTITLEMENT_KIND,(*provider[:3],fail_after_post))
    async with app.metadata.transaction(write=False) as tx:
        before={t:tx.one('SELECT COUNT(*) FROM '+t)[0] for t in ('money_ledger','events','audit','money_purchases','resource_entitlements','ledger_accounts')}
    result=await call(app,'money.redeem',args,key=key,subject=owner)
    assert result.error.code=='injected_grant_failure'
    async with app.metadata.transaction(write=False) as tx:
        assert before=={t:tx.one('SELECT COUNT(*) FROM '+t)[0] for t in before}
        assert _balance(tx,owner)==100


@pytest.mark.asyncio
async def test_expired_reservation_refunds_exactly_and_does_not_extend_its_quote(installed):
    app,root=installed
    key,owner,args,_=await setup_offer(app,root)
    result=await call(app,'money.redeem',{**args,'defer':True},key=key,subject=owner,contract_version=2)
    assert result.status=='ok',wire(result)
    pid=result.data['purchase']['id']
    app.clock=lambda:NOW+timedelta(seconds=offers.PURCHASE_TTL)
    await app.load()
    packet=request_for('money.purchase_settle',{'purchase_id':pid},app.settings.service_url,
        signer=key,subject=owner,expires_at=app.clock()+timedelta(seconds=120))
    expired=await app.executor.execute(packet)
    assert expired.status=='ok',wire(expired)
    assert expired.data['purchase']['state']=='refunded' and expired.data['purchase']['reason']=='expired'
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,owner)==100 and extra_capacity(tx,owner,app.clock())==0
        assert _supply(tx)==100


@pytest.mark.asyncio
async def test_capacity_changes_real_deployment_without_granting_access(installed):
    app,root=installed
    app.settings=replace(app.settings,hosting_base_capacity_bytes=10)
    key,owner,args,_=await setup_offer(app,root)
    website=await call(app,'hosting.create',{'parent':'/@capacity-buyer','name':'web'},key=key,subject=owner)
    assert website.status=='ok',wire(website)
    file=await call(app,'content.file_put',{'parent':'/@capacity-buyer/files','name':'index.html',
        'data':b64(b'hello website!'),'media_type':'text/html'},key=key,subject=owner)
    assert file.status=='ok',wire(file)
    wid=website.resources[0].id
    payload={'id':wid,'entries':[{'path':'index.html','source':wire(file.resources[0])}]}
    async def deploy():
        return await call(app,'hosting.deploy',payload,key=key,subject=owner,
                          expected=((wid,website.data['generation']),))
    assert (await deploy()).error.code=='hosting_capacity_exceeded'
    bought=await call(app,'money.redeem',args,key=key,subject=owner)
    assert bought.status=='ok',wire(bought)
    deployed=await deploy()
    assert deployed.status=='ok',wire(deployed)
    other_key,other,_=await register(app,'capacity-outsider')
    denied=await call(app,'hosting.deploy',payload,key=other_key,subject=other,
                      expected=((wid,deployed.data['generation']),))
    assert denied.status=='error' and denied.error.code!='hosting_capacity_exceeded'


def test_clearing_policy_is_pure_and_has_no_bank_or_role_exceptions():
    values=dict(kind='transfer',sender='alice',recipient='bob',amount=5,
                sender_balance=10,recipient_balance=4,supply=14)
    assert clearing_decision(**values)==clearing_decision(**values)
    for change,code in (({'amount':0},'invalid_money_amount'),({'amount':True},'invalid_money_amount'),
        ({'amount':11},'insufficient_funds'),({'recipient_balance':MAX_MINOR},'money_overflow'),
        ({'sender':ROOT_SUBJECT},'root_local_only'),({'kind':'mint','sender':None},'root_local_only')):
        with pytest.raises(Failure,match=code):
            clearing_decision(**(values|change))


@pytest.mark.asyncio
async def test_backup_restores_pending_purchase_and_settled_entitlement_without_repayment(installed,tmp_path,pg_dsn):
    from msg.admin.backups import backup, restore
    from msg.admin.market_check import inspect_clearing
    from msg.config import load_settings
    app,root=installed
    key,owner,args,_=await setup_offer(app,root)
    settled=await call(app,'money.redeem',args,key=key,subject=owner,rid='backup-settled')
    pending=await call(app,'money.redeem',{**args,'defer':True},key=key,subject=owner,rid='backup-pending',contract_version=2)
    assert settled.status==pending.status=='ok'
    async with app.metadata.transaction(write=False) as tx:
        original = {
            'settled_purchase': tx.one('SELECT * FROM money_purchases WHERE id=?',(settled.data['purchase']['id'],)),
            'pending_purchase': tx.one('SELECT * FROM money_purchases WHERE id=?',(pending.data['purchase']['id'],)),
            'settled_result': tx.one('SELECT digest,body FROM results WHERE subject=? AND request_id=?',(owner,'backup-settled')),
            'ledger': tx.rows('SELECT * FROM money_ledger ORDER BY seq'),
            'entitlements': tx.rows('SELECT * FROM resource_entitlements ORDER BY id'),
        }
    archive=tmp_path/'purchases.zip'
    await backup(app,archive)
    restore(archive,tmp_path/'restored-etc',tmp_path/'restored-data',postgres_dsn=pg_dsn)
    restored=Application(load_settings(tmp_path/'restored-etc'),clock=lambda:NOW)
    await restored.load()
    try:
        async with restored.metadata.transaction(write=False) as tx:
            assert inspect_clearing(restored,tx)['conserved']
            assert _balance(tx,owner)==80 and extra_capacity(tx,owner,NOW)==5
            assert tx.one('SELECT * FROM money_purchases WHERE id=?',(settled.data['purchase']['id'],))==original['settled_purchase']
            assert tx.one('SELECT * FROM money_purchases WHERE id=?',(pending.data['purchase']['id'],))==original['pending_purchase']
            assert tx.one('SELECT digest,body FROM results WHERE subject=? AND request_id=?',(owner,'backup-settled'))==original['settled_result']
            assert tx.rows('SELECT * FROM money_ledger ORDER BY seq')==original['ledger']
            assert tx.rows('SELECT * FROM resource_entitlements ORDER BY id')==original['entitlements']
        blocked=await call(restored,'money.redeem',args,key=key,subject=owner,rid='backup-settled')
        assert blocked.error.code=='writes_paused'
        (restored.settings.config_dir/'recovery-drill.json').unlink()
        replay=await call(restored,'money.redeem',args,key=key,subject=owner,rid='backup-settled')
        assert replay.error.code=='writes_paused'
        cancel_args={'purchase_id':pending.data['purchase']['id']}
        cancelled=await call(restored,'money.purchase_cancel',cancel_args,key=key,subject=owner,rid='backup-cancel')
        assert cancelled.error.code=='writes_paused'
        async with restored.metadata.transaction(write=False) as tx:
            assert _balance(tx,owner)==80 and extra_capacity(tx,owner,NOW)==5
            assert inspect_clearing(restored,tx)['conserved']
            assert tx.rows('SELECT * FROM money_ledger ORDER BY seq')==original['ledger']
    finally:
        await restored.close()
