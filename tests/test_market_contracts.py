"""Finance scope, revoked proofs, atomic receipts and production selftest guard."""
import asyncio
from types import SimpleNamespace

import pytest

from msg.admin.diagnostics import doctor
from msg.admin.market_check import check_market_e2e, inspect_clearing
from msg.cli import arguments, parser
from msg.client_market import prove_bounty, run_command
from msg.core.codec import b64, canonical, wire
from msg.core.errors import Failure
from msg.core.models import Scope
from msg.plugins.money import _balance, _supply
from msg.security.capabilities import grant_for
from msg.security.crypto import Ed25519Signer
from test_market_72 import fixture, proof
from test_market_redemption import setup_offer
from test_service import NOW, call, register


@pytest.mark.parametrize('argv,op,params',[
    (['money','balance'],'money.balance',{}),
    (['money','purchase','pur_one'],'money.purchase_get',{'purchase_id':'pur_one'}),
    (['orders','list','{"status":"open"}'],'orders.list',{'status':'open'}),
    (['orders','history','ord_one'],'orders.history',{'order_id':'ord_one'}),
    (['delivery','submit','{"order_id":"ord_one","completion_statement":"done"}'],
     'delivery.submit',{'order_id':'ord_one','completion_statement':'done'}),
    (['store','update','{"id":"listing","state":"paused"}','--generation','2'],
     'store.listing_update',{'id':'listing','state':'paused'}),
])
@pytest.mark.asyncio
async def test_cli_routes_exact_contract_without_silent_price_or_recipient_changes(argv,op,params):
    class Client:
        async def call(self,operation,parameters,**kwargs):return operation,parameters,kwargs
    parsed=parser().parse_args(argv+['--request-id','retry-one'])
    operation,parameters,kwargs=await run_command(Client(),parsed,arguments)
    assert (operation,parameters)==(op,params) and kwargs['request_id']=='retry-one'
    if argv[:2]==['store','update']:assert kwargs['expected']==(('listing',2),)


@pytest.mark.asyncio
async def test_limited_key_operation_name_does_not_grant_owner_account_scope_even_on_replay(installed):
    app,root=installed
    key,owner,args,_=await setup_offer(app,root)
    bought=await call(app,'money.redeem',args,key=key,subject=owner,rid='scoped-redeem')
    assert bought.status=='ok',wire(bought)
    limited=Ed25519Signer.generate()
    public=b64(limited.public_key)
    grant=grant_for(app.registry.capability('money.basic'),scope=Scope(resource_id='t_store',descendants=True),
                    operations=('money.redeem@1','money.balance@1','money.purchase_get@1'))
    added=await call(app,'identity.key_add',{'public_key':public,
        'possession_proof':wire(limited.sign(canonical({'subject_id':owner,'public_key':public}),purpose='key-add')),
        'ceiling':[wire(grant)]},key=key,subject=owner)
    assert added.status=='ok',wire(added)
    for op,params,rid in [('money.balance',{},None),('money.redeem',args,'scoped-redeem'),
                          ('money.purchase_get',{'purchase_id':bought.data['purchase']['id']},None)]:
        denied=await call(app,op,params,key=limited,subject=owner,rid=rid)
        assert denied.error.code=='credential_ceiling',wire(denied)
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,owner)==90 and tx.one('SELECT COUNT(*) FROM money_purchases')[0]==1


@pytest.mark.asyncio
async def test_revoked_or_replaced_identity_key_cannot_spend_pending_challenge(installed):
    app,root=installed
    _,(key,claimant),created=await fixture(app,root)
    args=await proof(app,(key,claimant),created.data['bounty']['listing_id'])
    new=Ed25519Signer.generate(); public=b64(new.public_key)
    added=await call(app,'identity.key_add',{'public_key':public,
        'possession_proof':wire(new.sign(canonical({'subject_id':claimant,'public_key':public}),purpose='key-add')),
        'ceiling':wire(app.primary_ceiling())},key=key,subject=claimant)
    assert added.status=='ok',wire(added)
    revoked=await call(app,'identity.key_revoke',{'key_id':key.key_id},key=key,subject=claimant)
    assert revoked.status=='ok',wire(revoked)
    assert (await call(app,'bounty.claim',args,key=key,subject=claimant)).error.code=='credential_revoked'
    assert (await call(app,'bounty.claim',args,key=new,subject=claimant)).error.code=='current_identity_key_required'
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,claimant)==0
        assert tx.one('SELECT consumed_at FROM bounty_challenges WHERE id=?',(args['challenge_id'],))[0] is None


@pytest.mark.asyncio
async def test_claim_close_race_and_event_failure_never_lose_or_duplicate_budget(installed,monkeypatch):
    app,root=installed
    (bk,bank),(key,buyer),created=await fixture(app,root)
    listing=created.data['bounty']['listing_id']; args=await proof(app,(key,buyer),listing)
    from msg.storage.postgres import PostgresSession
    original=PostgresSession.append_event
    async def fail_after_event(self,event):
        await original(self,event)
        if event.type=='bounty.claim':raise Failure('injected_claim_event_failure')
    with monkeypatch.context() as scoped:
        scoped.setattr(PostgresSession,'append_event',fail_after_event)
        failed=await call(app,'bounty.claim',args,key=key,subject=buyer)
        assert failed.error.code=='injected_claim_event_failure'
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,buyer)==0 and tx.one('SELECT COUNT(*) FROM bounty_claims')[0]==0
        assert tx.one('SELECT consumed_at FROM bounty_challenges WHERE id=?',(args['challenge_id'],))[0] is None
        assert tx.one("SELECT COUNT(*) FROM messages WHERE body LIKE '%bounty_claim%'")[0]==0
    results=await asyncio.gather(call(app,'bounty.claim',args,key=key,subject=buyer),
                                 call(app,'bounty.close',{'listing_id':listing},key=bk,subject=bank))
    assert results[1].status=='ok'
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,bank)+_balance(tx,buyer)==_supply(tx)==40
        assert _balance(tx,created.data['funding']['body']['to_subject'])==0
        assert tx.one('SELECT COUNT(*) FROM bounty_claims')[0] in {0,1}


@pytest.mark.asyncio
async def test_cli_pop_reuses_the_challenge_and_claim_request_ids(installed):
    app,root=installed
    _,(key,buyer),created=await fixture(app,root)
    class Client:
        state=SimpleNamespace(subject=buyer,signer=key)
        async def call(self,op,params,**kwargs):
            return await call(app,op,params,key=key,subject=buyer,rid=kwargs.get('request_id'))
    lid=created.data['bounty']['listing_id']
    a=await prove_bounty(Client(),lid,'cli-proof')
    b=await prove_bounty(Client(),lid,'cli-proof')
    assert a.status==b.status=='ok' and b.replayed and a.data==b.data
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM bounty_challenges')[0]==1
        assert tx.one('SELECT COUNT(*) FROM bounty_claims')[0]==1


@pytest.mark.asyncio
async def test_default_doctor_is_read_only_and_selftest_cannot_seed_an_installed_server(installed):
    app,root=installed
    async with app.metadata.transaction(write=False) as tx:
        before=tx.one('SELECT COUNT(*) FROM events')[0]
        facts=inspect_clearing(app,tx)
        assert facts['total_supply_minor']==facts['banks']==facts['enabled_offers']==facts['ledger_entries']==0
    result=doctor(app.settings.config_dir,clock=lambda:NOW)
    assert result['checks']['market_clearing']['ok'] and result['features']['money']['status']=='pass',result
    with pytest.raises(Failure,match='isolated_market_selftest_required'):
        await check_market_e2e(app,root,None,None)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM events')[0]==before
        assert _supply(tx)==0


@pytest.mark.asyncio
async def test_existing_short_codes_keep_their_published_contract(installed):
    from importlib.resources import files
    from msg.core.codec import loads
    from msg.transports.dictionary import build_dictionary
    app,_=installed
    published=loads(files('msg.data').joinpath('shortcodes.json').read_bytes())
    fresh=build_dictionary(app.registry,published=None).document
    old={row['code']:row for row in published['operations']}
    changed=[row['name']+'@'+str(row['version']) for row in fresh['operations']
             if row['code'] in old and canonical(row)!=canonical(old[row['code']])]
    assert changed==[],changed
    build_dictionary(app.registry)
