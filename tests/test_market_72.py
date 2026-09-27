"""PoP binds an actual key, not a human, and cannot spend more than escrow."""
from datetime import timedelta

import pytest

from msg.admin.backups import backup, restore
from msg.admin.money import apply_money
from msg.application import Application
from msg.config import load_settings
from msg.core.codec import canonical, wire
from msg.plugins.money import _balance, _supply
from test_service import NOW, call, register


async def fixture(app,root,**changes):
    bank_key,bank,_=await register(app,'bank-test')
    key,buyer,_=await register(app,'pop-buyer')
    await apply_money(app,root,action='mint',operator='test',amount_minor=40)
    await apply_money(app,root,action='bank_fund',operator='test',subject_id=bank,amount_minor=40)
    created=await call(app,'bounty.create',{'name':'pop','terms':'IdentityKey possession only; not human or Sybil proof.',
        'reward_minor':10,'budget_minor':20,'max_claims':1,**changes},key=bank_key,subject=bank)
    assert created.status=='ok',wire(created)
    return (bank_key,bank),(key,buyer),created


async def proof(app,identity,listing):
    key,buyer=identity
    challenge=await call(app,'bounty.challenge',{'listing_id':listing},key=key,subject=buyer)
    assert challenge.status=='ok',wire(challenge)
    payload=challenge.data['challenge']
    return {'challenge_id':payload['challenge_id'],
            'proof':wire(key.sign(canonical(payload),purpose='bounty-pop-v1'))}


@pytest.mark.asyncio
async def test_exhausted_claim_limit_is_consistent_in_both_views_and_topup_cannot_reopen(installed):
    app,root=installed
    (bk,bank),identity,created=await fixture(app,root)
    key,buyer=identity; lid=created.data['bounty']['listing_id']
    paid=await call(app,'bounty.claim',await proof(app,identity,lid),key=key,subject=buyer)
    assert paid.status=='ok',wire(paid)
    topped=await call(app,'bounty.top_up',{'listing_id':lid,'amount_minor':5},key=bk,subject=bank)
    assert topped.status=='ok',wire(topped)
    for operation,args,field in (('bounty.get',{'listing_id':lid},'bounty'),('store.listing_get',{'id':lid},'listing')):
        result=await call(app,operation,args)
        assert result.data[field]['state']=='paused'
        assert result.data[field]['pause_reason']=='claims_exhausted'
        assert result.data[field]['escrow_balance_minor']==15
        assert result.data[field]['paid_claims']==1
    denied=await call(app,'bounty.resume',{'listing_id':lid},key=bk,subject=bank)
    assert denied.error.code=='bounty_claims_exhausted'
    own=await call(app,'bounty.claims',{'listing_id':lid},key=key,subject=buyer)
    assert [item['id'] for item in own.data['claims']]==[paid.data['claim']['id']]
    outsider_key,outsider,_=await register(app,'pop-outsider')
    hidden=await call(app,'bounty.claims',{'listing_id':lid},key=outsider_key,subject=outsider)
    assert hidden.status=='ok' and len(hidden.data['claims'])==0


@pytest.mark.asyncio
async def test_pause_preserves_budget_topup_does_not_resume_and_close_returns_remainder(installed):
    app,root=installed
    (bk,bank),identity,created=await fixture(app,root,max_claims=2)
    key,buyer=identity; lid=created.data['bounty']['listing_id']
    claim=await proof(app,identity,lid)
    assert (await call(app,'bounty.pause',{'listing_id':lid},key=bk,subject=bank)).status=='ok'
    assert (await call(app,'bounty.claim',claim,key=key,subject=buyer)).error.code=='bounty_not_active'
    topped=await call(app,'bounty.top_up',{'listing_id':lid,'amount_minor':5},key=bk,subject=bank)
    assert topped.data['bounty']['state']=='paused' and topped.data['bounty']['pause_reason']=='publisher_paused'
    assert (await call(app,'bounty.resume',{'listing_id':lid},key=bk,subject=bank)).status=='ok'
    assert (await call(app,'bounty.claim',claim,key=key,subject=buyer)).status=='ok'
    closed=await call(app,'bounty.close',{'listing_id':lid},key=bk,subject=bank)
    assert closed.data['returned']['body']['amount_minor']==15
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,bank)==30 and _balance(tx,buyer)==10 and _supply(tx)==40


@pytest.mark.asyncio
async def test_expired_listing_projection_does_not_mutate_state_or_emit_events(installed):
    app,root=installed
    _,_,created=await fixture(app,root,expires_at=wire(NOW+timedelta(seconds=1)))
    lid=created.data['bounty']['listing_id']
    app.clock=lambda:NOW+timedelta(seconds=2)
    await app.load()
    async with app.metadata.transaction(write=False) as tx:
        before=tx.one('SELECT COUNT(*) FROM events')[0]
    for op,args,field in (('bounty.get',{'listing_id':lid},'bounty'),('store.listing_get',{'id':lid},'listing')):
        got=await call(app,op,args)
        assert got.data[field]['state']=='paused' and got.data[field]['pause_reason']=='expired'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT state FROM bounty_listings WHERE listing_id=?',(lid,))[0]=='active'
        assert tx.one('SELECT COUNT(*) FROM events')[0]==before


@pytest.mark.asyncio
async def test_stored_challenge_fields_must_match_canonical_signed_payload(installed):
    app,root=installed
    _,identity,created=await fixture(app,root)
    key,buyer=identity; lid=created.data['bounty']['listing_id']
    claim=await proof(app,identity,lid)
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('UPDATE bounty_challenges SET nonce=? WHERE id=?',('tampered',claim['challenge_id']),write=True)
    denied=await call(app,'bounty.claim',claim,key=key,subject=buyer)
    assert denied.error.code=='bounty_challenge_mismatch'
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,buyer)==0
        assert tx.one('SELECT consumed_at FROM bounty_challenges WHERE id=?',(claim['challenge_id'],))[0] is None
        assert tx.one('SELECT COUNT(*) FROM bounty_claims')[0]==0


@pytest.mark.asyncio
async def test_backup_restores_claim_nonce_inbox_receipt_and_replay_together(installed,tmp_path,pg_dsn):
    app,root=installed
    _,identity,created=await fixture(app,root)
    key,buyer=identity; lid=created.data['bounty']['listing_id']
    claim=await proof(app,identity,lid)
    paid=await call(app,'bounty.claim',claim,key=key,subject=buyer,rid='restore-claim')
    assert paid.status=='ok',wire(paid)
    archive=tmp_path/'market.zip'
    await backup(app,archive)
    restore(archive,tmp_path/'restored-etc',tmp_path/'restored-data',postgres_dsn=pg_dsn)
    restored=Application(load_settings(tmp_path/'restored-etc'),clock=lambda:NOW)
    await restored.load()
    try:
        repeated=await call(restored,'bounty.claim',claim,key=key,subject=buyer,rid='restore-claim')
        assert repeated.error.code == 'writes_paused'
        # Restore is deliberately read-only. Activate only this disposable copy.
        (restored.settings.config_dir/'recovery-drill.json').unlink()
        repeated=await call(restored,'bounty.claim',claim,key=key,subject=buyer,rid='restore-claim')
        assert repeated.status == 'ok', repeated.error.code if repeated.error else None
        assert repeated.replayed and repeated.data['claim']['id']==paid.data['claim']['id'], wire(repeated)
        assert (await call(restored,'bounty.claim',claim,key=key,subject=buyer,rid='new-claim')).status=='error'
        async with restored.metadata.transaction(write=False) as tx:
            assert _balance(tx,buyer)==10 and _supply(tx)==40
            assert tx.one('SELECT COUNT(*) FROM bounty_claims')[0]==1
            assert tx.one('SELECT consumed_at FROM bounty_challenges WHERE id=?',(claim['challenge_id'],))[0]
        inbox=await call(restored,'communication.inbox',{},key=key,subject=buyer)
        assert len([i for i in inbox.data['items'] if i.get('source')=='bounty_claim'])==1
    finally:
        await restored.close()
