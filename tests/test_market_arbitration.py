"""Real signed panel votes; no fake principal or direct settlement mutation."""
import asyncio
from copy import deepcopy
from datetime import timedelta

import pytest
from test_market_lifecycle import buy, market
from test_service import NOW, call, register

from msg.admin.market import apply_market
from msg.core.codec import canonical, digest, unb64, wire
from msg.core.errors import Failure
from msg.market.policy import DEFAULT_POLICY
from msg.plugins.money import _balance


async def configured(installed, *, panel_size=3, count=4, appeal=False):
    app, root = installed
    members = {}
    for i in range(count):
        key, uid, _ = await register(app, f'arbitrator-{i}')
        members[uid] = key
        await apply_market(app, root, action='grant', operator='isolated-fixture', subject=uid)
    policy = deepcopy(DEFAULT_POLICY)
    policy.update(id='test-panel-v1', candidates=list(members), panel_size=panel_size,
                  quorum=panel_size//2+1, allow_appeal=appeal)
    await apply_market(app, root, action='publish', operator='isolated-fixture', policy=policy)
    sk, seller, bk, buyer, listing, _ = await market(app, root, mode='service', kind='service',
                                                  policy=policy['id'])
    purchase = await buy(app, bk, buyer, listing)
    assert purchase.status == 'ok', wire(purchase)
    opened = await call(app, 'orders.dispute_open', {
        'order_id':purchase.data['order']['id'], 'reason':'quality'}, key=bk, subject=buyer)
    assert opened.status == 'ok', wire(opened)
    return app, root, members, sk, seller, bk, buyer, purchase.data['order'], opened.data


async def details(app, key, subject, case_id):
    result = await call(app,'orders.dispute_get',{'case_id':case_id},key=key,subject=subject)
    assert result.status == 'ok', wire(result)
    return result.data


async def vote(app, key, subject, base, *, refund=2_000_000, outcome='split'):
    proposal = {**base, 'refund_minor':refund, 'outcome':outcome}
    return await call(app,'orders.dispute_vote',{'proposal':proposal,
        'signature':wire(key.sign(canonical(proposal),purpose='arbitration-decision'))},
        key=key,subject=subject)


@pytest.mark.asyncio
async def test_fixed_panel_split_quorum_and_concurrent_execution(installed):
    app, _root, members, _sk, seller, bk, buyer, order, opened = await configured(installed)
    data = await details(app,bk,buyer,opened['case_id'])
    panel = list(opened['panel'])
    expected = sorted(members,key=lambda uid:(digest({'version':1,'order_id':order['id'],
        'policy_digest':opened['policy_digest'],'round':0,'subject':uid}),uid))[:3]
    assert panel == expected and data['panel_valid']
    base = data['proposal_base']
    # The second signed statement by the same member cannot create a new vote.
    first = await vote(app,members[panel[0]],panel[0],base)
    assert first.status=='ok' and first.data['decision'] is None
    conflict = await vote(app,members[panel[0]],panel[0],base,refund=0,outcome='release')
    assert conflict.error.code=='conflicting_vote'
    second = await vote(app,members[panel[1]],panel[1],base)
    assert second.status=='ok', wire(second)
    decision = second.data['decision']
    assert decision and decision['refund_minor']==2_000_000
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,order['escrow_subject'] if 'escrow_subject' in order else
            tx.one('SELECT escrow_subject FROM store_orders WHERE id=?',(order['id'],))[0])==5_000_000
        assert _balance(tx,seller)==0  # A vote is never a payment.
    args={'case_id':opened['case_id'],'decision_id':decision['id']}
    results=await asyncio.gather(*[call(app,'orders.dispute_execute',args,key=bk,
        subject=buyer,rid=f'execution-{i}') for i in range(3)])
    assert all(r.status=='ok' for r in results), [wire(r) for r in results]
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,buyer)==17_000_000 and _balance(tx,seller)==3_000_000
        assert tx.one('SELECT COUNT(*) FROM order_settlements WHERE order_id=?',(order['id'],))[0]==1
        assert tx.one('SELECT COUNT(*) FROM money_ledger WHERE reference LIKE ?',
                      ('order_%:'+order['id'],))[0]==3  # fund + split's two legs


@pytest.mark.asyncio
async def test_role_isolated_evidence_and_summary_has_no_private_fields(installed):
    app, root, members, sk, seller, bk, buyer, order, opened = await configured(installed)
    cid = opened['case_id']
    statement=await call(app,'orders.dispute_statement',{'case_id':cid,
        'visibility':'panel','text':'Only the panel may see the buyer testimony.'},key=bk,subject=buyer)
    assert statement.status=='ok',wire(statement)
    eid=statement.data['evidence_id']
    seller_view=await details(app,sk,seller,cid)
    assert not seller_view['evidence']
    denied=await call(app,'orders.dispute_evidence_get',{'case_id':cid,'evidence_id':eid},key=sk,subject=seller)
    missing=await call(app,'orders.dispute_evidence_get',{'case_id':cid,'evidence_id':'unknown'},key=sk,subject=seller)
    assert denied.error.code==missing.error.code=='evidence_not_found'
    arb=opened['panel'][0]
    allowed=await call(app,'orders.dispute_evidence_get',{'case_id':cid,'evidence_id':eid},key=members[arb],subject=arb)
    assert unb64(allowed.data['data'])==b'Only the panel may see the buyer testimony.'
    outsider=next(uid for uid in members if uid not in opened['panel'])
    for case_id in (cid,'case_unknown'):
        hidden=await call(app,'orders.dispute_get',{'case_id':case_id},key=members[outsider],subject=outsider)
        assert hidden.error.code=='case_not_found'
    public=await call(app,'orders.dispute_summary',{'case_id':cid})
    assert public.status=='ok'
    assert set(public.data)=={'case_id','state','policy_digest','outcome'}
    text=canonical(public.data).decode()
    assert all(private not in text for private in (buyer,seller,order['id'],eid,'testimony'))
    await apply_market(app,root,action='revoke',operator='test',subject=arb)
    after=await call(app,'orders.dispute_get',{'case_id':cid},key=members[arb],subject=arb)
    assert after.error.code=='case_not_found'


@pytest.mark.asyncio
@pytest.mark.parametrize('invalidation',['revoke','regrant','conflict','decision_revoke','expire'])
async def test_invalidated_decision_never_releases_funds(installed,invalidation):
    app, root, members, _sk, seller, bk, buyer, order, opened = await configured(
        installed,panel_size=1,count=2)
    cid, arb=opened['case_id'],opened['panel'][0]
    base=(await details(app,bk,buyer,cid))['proposal_base']
    result=await vote(app,members[arb],arb,base,refund=0,outcome='release')
    decision=result.data['decision']
    if invalidation in {'revoke','regrant'}:
        await apply_market(app,root,action='revoke',operator='test',subject=arb)
        if invalidation=='regrant':
            await apply_market(app,root,action='grant',operator='test',subject=arb)
    elif invalidation=='conflict':
        declared=await call(app,'orders.arbitrator_conflict',{'party':buyer},key=members[arb],subject=arb)
        assert declared.status=='ok'
    elif invalidation=='decision_revoke':
        revoked=await call(app,'orders.dispute_revoke',{'case_id':cid,'decision_id':decision['id']},key=members[arb],subject=arb)
        assert revoked.status=='ok'
    else:
        app.clock=lambda:NOW+timedelta(days=10)
        from msg.market.arbitration import resolve_cases
        assert await resolve_cases(app)==[cid]
    if invalidation!='expire':
        failed=await call(app,'orders.dispute_execute',{'case_id':cid,'decision_id':decision['id']},key=bk,subject=buyer)
        assert failed.status=='error',wire(failed)
    async with app.metadata.transaction(write=False) as tx:
        escrow=tx.one('SELECT escrow_subject FROM store_orders WHERE id=?',(order['id'],))[0]
        assert _balance(tx,escrow)==5_000_000 and _balance(tx,seller)==0
        assert tx.one('SELECT COUNT(*) FROM order_settlements')[0]==0


@pytest.mark.asyncio
async def test_one_appeal_disjoint_panel_and_old_decision_is_unusable(installed):
    app, _root, members, sk, seller, bk, buyer, _order, opened=await configured(
        installed,panel_size=1,count=2,appeal=True)
    cid,arb=opened['case_id'],opened['panel'][0]
    base=(await details(app,bk,buyer,cid))['proposal_base']
    result=await vote(app,members[arb],arb,base,refund=0,outcome='release')
    old=result.data['decision']['id']
    premature=await call(app,'orders.dispute_execute',{'case_id':cid,'decision_id':old},key=bk,subject=buyer)
    assert premature.error.code=='appeal_window_open'
    appealed=await call(app,'orders.dispute_appeal',{'case_id':cid},key=bk,subject=buyer)
    assert appealed.status=='ok',wire(appealed)
    assert set(appealed.data['panel']).isdisjoint(opened['panel'])
    again=await call(app,'orders.dispute_appeal',{'case_id':cid},key=sk,subject=seller)
    assert again.error.code=='appeal_not_available'
    old_attempt=await call(app,'orders.dispute_execute',{'case_id':cid,'decision_id':old},key=bk,subject=buyer)
    assert old_attempt.error.code=='decision_not_current'
    current=(await details(app,bk,buyer,cid))['proposal_base']
    reviewer=appealed.data['panel'][0]
    appeal_vote=await vote(app,members[reviewer],reviewer,current,refund=5_000_000,outcome='refund')
    assert appeal_vote.status=='ok',wire(appeal_vote)
    final=await call(app,'orders.dispute_execute',{'case_id':cid,
        'decision_id':appeal_vote.data['decision']['id']},key=bk,subject=buyer)
    assert final.status=='ok',wire(final)
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,buyer)==20_000_000 and _balance(tx,seller)==0


@pytest.mark.asyncio
async def test_split_rollback_restores_escrow_decision_and_retry(installed,monkeypatch):
    app, _root, members, _sk, _seller, bk, buyer, order, opened=await configured(installed,panel_size=1,count=1)
    cid,arb=opened['case_id'],opened['panel'][0]
    result=await vote(app,members[arb],arb,(await details(app,bk,buyer,cid))['proposal_base'])
    args={'case_id':cid,'decision_id':result.data['decision']['id']}
    from msg.market import escrow
    original=escrow._post_transfer
    def fail_after_first_leg(*args,**kwargs):
        original(*args,**kwargs)
        raise Failure('forced_split_rollback')
    monkeypatch.setattr(escrow,'_post_transfer',fail_after_first_leg)
    failed=await call(app,'orders.dispute_execute',args,key=bk,subject=buyer)
    assert failed.error.code=='forced_split_rollback'
    async with app.metadata.transaction(write=False) as tx:
        account=tx.one('SELECT escrow_subject FROM store_orders WHERE id=?',(order['id'],))[0]
        assert _balance(tx,account)==5_000_000 and _balance(tx,buyer)==15_000_000
        assert tx.one('SELECT state FROM arbitration_cases WHERE id=?',(cid,))[0]=='decided'
        assert tx.one('SELECT COUNT(*) FROM order_settlements')[0]==0
    monkeypatch.setattr(escrow,'_post_transfer',original)
    retried=await call(app,'orders.dispute_execute',args,key=bk,subject=buyer)
    assert retried.status=='ok',wire(retried)


@pytest.mark.asyncio
async def test_default_no_panel_holds_without_choosing_new_candidates(installed):
    app,root=installed
    _sk,seller,bk,buyer,listing,_=await market(app,root,mode='service',kind='service')
    order=(await buy(app,bk,buyer,listing)).data['order']
    opened=await call(app,'orders.dispute_open',{'order_id':order['id'],'reason':'quality'},key=bk,subject=buyer)
    assert opened.status=='ok' and opened.data['state']=='held' and not opened.data['panel']
    _key,uid,_=await register(app,'late-arbitrator')
    await apply_market(app,root,action='grant',operator='test',subject=uid)
    data=await details(app,bk,buyer,opened.data['case_id'])
    assert not data['case']['panels']['0'] and not data['panel_valid']
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,seller)==0 and tx.one('SELECT COUNT(*) FROM order_settlements')[0]==0
