"""Bounty funding and payout are one ledger-backed PostgreSQL transaction."""
import asyncio

import pytest

from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, wire
from msg.plugins.money import CURRENCY_ID, POLICY_DIGEST, POLICY_VERSION
from msg.security.crypto import Ed25519Signer
from test_service import NOW, call, register


async def fund(app, subject, amount):
    # Controlled fixture issue; public operations cannot mint.
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('INSERT INTO money_accounts VALUES (?,?)',
                   (subject, CURRENCY_ID), write=True)
        tx.execute('''INSERT INTO money_ledger
            (id,kind,currency_id,amount_minor,debit_account,credit_account,actor,
             request_id,reference,committed_at,policy_version,policy_digest,receipt)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            ('lt_fixture_bounty_'+subject,'mint',CURRENCY_ID,amount,None,subject,
             ROOT_SUBJECT,'fixture_bounty_'+subject,None,wire(NOW),
             POLICY_VERSION,POLICY_DIGEST,'{}'), write=True)


def terms(**changes):
    return {'name':'identity-proof-reward', 'terms':'Sign a one-use challenge.',
            'reward_minor':10, 'budget_minor':10, 'max_claims':1, **changes}


async def balance(app, key, subject):
    result = await call(app,'money.balance',{},key=key,subject=subject)
    assert result.status == 'ok', wire(result)
    return result.data['balance_minor']


async def assert_current_views(app, listing_id, *, state, budget, escrow_balance):
    bounty=await call(app,'bounty.get',{'listing_id':listing_id})
    catalog=await call(app,'store.listing_get',{'id':listing_id})
    assert bounty.status==catalog.status=='ok', (wire(bounty),wire(catalog))
    live=bounty.data['bounty']
    shown=catalog.data['listing']
    assert live['state']==shown['state']==shown['current_state']==state
    assert live['budget_minor']==shown['budget_minor']==shown['current_budget_minor']==budget
    assert live['escrow_balance_minor']==escrow_balance


@pytest.mark.asyncio
async def test_bounty_catalog_current_projection_and_historical_revision(installed):
    app,_=installed
    pubkey,publisher,_=await register(app,'bounty-projection-publisher')
    key,claimant,_=await register(app,'bounty-projection-claimant')
    await fund(app,publisher,20)
    created=await call(app,'bounty.create',terms(budget_minor=5,max_claims=2),
                       key=pubkey,subject=publisher)
    assert created.status=='ok',wire(created)
    listing_id=created.data['bounty']['listing_id']
    original_revision=created.resources[0].revision
    await assert_current_views(app,listing_id,state='paused',budget=5,escrow_balance=5)
    topped=await call(app,'bounty.top_up',{'listing_id':listing_id,'amount_minor':15},
                      key=pubkey,subject=publisher)
    assert topped.status=='ok',wire(topped)
    await assert_current_views(app,listing_id,state='active',budget=20,escrow_balance=20)
    challenge=await call(app,'bounty.challenge',{'listing_id':listing_id},
                         key=key,subject=claimant)
    assert challenge.status=='ok',wire(challenge)
    proof=wire(key.sign(canonical(challenge.data['challenge']),purpose='bounty-pop-v1'))
    claimed=await call(app,'bounty.claim',{
        'challenge_id':challenge.data['challenge']['challenge_id'],'proof':proof},
        key=key,subject=claimant)
    assert claimed.status=='ok',wire(claimed)
    await assert_current_views(app,listing_id,state='active',budget=20,escrow_balance=10)
    closed=await call(app,'bounty.close',{'listing_id':listing_id},
                      key=pubkey,subject=publisher)
    assert closed.status=='ok',wire(closed)
    assert closed.data['returned']['body']['amount_minor']==10
    await assert_current_views(app,listing_id,state='closed',budget=20,escrow_balance=0)
    historical=await call(app,'store.listing_get',
                          {'id':listing_id,'revision':original_revision})
    assert historical.status=='ok',wire(historical)
    previous=historical.data['listing']
    assert previous['state']=='paused' and previous['budget_minor']==5
    assert previous['current_state']=='closed' and previous['current_budget_minor']==20
    assert previous['terms_revision']==original_revision


@pytest.mark.asyncio
async def test_prefunded_bounty_pays_offline_publisher_and_stops_at_budget(installed):
    app,_=installed
    publisher_key,publisher,_=await register(app,'bounty-publisher')
    claimant_key,claimant,_=await register(app,'bounty-claimant')
    await fund(app,publisher,20)
    created=await call(app,'bounty.create',terms(),key=publisher_key,
                       subject=publisher,rid='bounty-create')
    assert created.status=='ok',wire(created)
    listing=created.data['bounty']['listing_id']
    assert created.data['bounty']['state']=='active'
    assert await balance(app,publisher_key,publisher)==10
    assert (await call(app,'store.listing_get',{'id':listing})).data['listing']['mode']=='bounty'
    async with app.metadata.transaction(write=False) as tx:
        escrow=created.data['funding']['body']['to_subject']
        assert tx.one('SELECT kind,subject_id,source_id FROM ledger_accounts WHERE id=?',
                      (escrow,))==('bounty_escrow',None,listing)
        assert tx.one('SELECT 1 FROM identities WHERE id=?',(escrow,)) is None
        assert tx.one('SELECT COUNT(*) FROM credentials WHERE subject=?',(escrow,))[0]==0
        assert tx.one('SELECT COUNT(*) FROM money_ledger WHERE credit_account=?',(escrow,))[0]==1
    challenge=await call(app,'bounty.challenge',{'listing_id':listing},
                         key=claimant_key,subject=claimant)
    assert challenge.status=='ok',wire(challenge)
    payload=challenge.data['challenge']
    assert payload['claimant_subject_id']==claimant and payload['listing_id']==listing
    proof=wire(claimant_key.sign(canonical(payload),purpose='bounty-pop-v1'))
    claimed=await call(app,'bounty.claim',{'challenge_id':payload['challenge_id'],
                    'proof':proof},key=claimant_key,subject=claimant,rid='bounty-claim')
    assert claimed.status=='ok',wire(claimed)
    assert claimed.data['claim']['status']=='paid'
    assert claimed.data['reward']['body']['from_subject']==escrow
    assert claimed.data['reward']['body']['to_subject']==claimant
    assert claimed.data['escrow_balance_minor']==0
    assert await balance(app,claimant_key,claimant)==10
    assert await balance(app,publisher_key,publisher)==10
    assert (await call(app,'money.state',{})).data['total_supply_minor']==20
    inbox=await call(app,'communication.inbox',{},key=claimant_key,subject=claimant)
    bounty_notices=[item for item in inbox.data['items']
                    if item.get('source')=='bounty_claim']
    assert len(bounty_notices)==1
    assert bounty_notices[0]['claim_id']==claimed.data['claim']['id']
    replay=await call(app,'bounty.claim',{'challenge_id':payload['challenge_id'],
                  'proof':proof},key=claimant_key,subject=claimant,rid='bounty-claim')
    assert replay.status=='ok' and replay.replayed
    again=await call(app,'bounty.claim',{'challenge_id':payload['challenge_id'],
                  'proof':proof},key=claimant_key,subject=claimant,rid='bounty-claim-other')
    assert again.status=='error'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM bounty_claims WHERE listing_id=?',(listing,))[0]==1
        assert tx.one('SELECT COUNT(*) FROM money_ledger WHERE debit_account=?',(escrow,))[0]==1
        assert tx.one('SELECT consumed_at FROM bounty_challenges WHERE id=?',
                      (payload['challenge_id'],))[0] is not None
    view=await call(app,'bounty.get',{'listing_id':listing})
    assert view.data['bounty']['state']=='paused'
    assert view.data['bounty']['pause_reason']=='out_of_budget'
    catalog=await call(app,'store.listing_get',{'id':listing})
    assert catalog.data['listing']['state']==view.data['bounty']['state']
    assert catalog.data['listing']['pause_reason']=='out_of_budget'
    historical=await call(app,'store.listing_get',{'id':listing,
        'revision':created.resources[0].revision})
    assert historical.data['listing']['state']=='active'
    assert historical.data['listing']['current_state']=='paused'


@pytest.mark.asyncio
async def test_bad_proof_wrong_subject_expiry_and_top_up_do_not_spend(installed):
    app,_=installed
    pubkey,publisher,_=await register(app,'bounty-negative-publisher')
    key,claimant,_=await register(app,'bounty-negative-claimant')
    other_key,other,_=await register(app,'bounty-negative-other')
    await fund(app,publisher,30)
    created=await call(app,'bounty.create',terms(budget_minor=5,max_claims=2),
                       key=pubkey,subject=publisher)
    assert created.status=='ok',wire(created)
    listing=created.data['bounty']['listing_id']
    assert created.data['bounty']['state']=='paused'
    unavailable=await call(app,'bounty.challenge',{'listing_id':listing},
                           key=key,subject=claimant)
    assert unavailable.error.code=='bounty_not_active'
    topped=await call(app,'bounty.top_up',{'listing_id':listing,'amount_minor':15},
                      key=pubkey,subject=publisher)
    assert topped.status=='ok' and topped.data['bounty']['state']=='active',wire(topped)
    ch=await call(app,'bounty.challenge',{'listing_id':listing},key=key,subject=claimant)
    assert ch.status=='ok',wire(ch)
    payload=ch.data['challenge']
    alien=wire(other_key.sign(canonical(payload),purpose='bounty-pop-v1'))
    wrong=await call(app,'bounty.claim',{'challenge_id':payload['challenge_id'],
                    'proof':alien},key=key,subject=claimant)
    assert wrong.error.code=='bounty_proof_key_mismatch'
    foreign=await call(app,'bounty.claim',{'challenge_id':payload['challenge_id'],
                    'proof':alien},key=other_key,subject=other)
    assert foreign.error.code=='bounty_challenge_not_found'
    valid=wire(key.sign(canonical(payload),purpose='bounty-pop-v1'))
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('UPDATE bounty_challenges SET expires_at=? WHERE id=?',
                   ('2026-09-26T00:00:00Z',payload['challenge_id']),write=True)
    expired=await call(app,'bounty.claim',{'challenge_id':payload['challenge_id'],
                      'proof':valid},key=key,subject=claimant)
    assert expired.error.code=='bounty_challenge_expired'
    assert await balance(app,key,claimant)==0
    restricted=await call(app,'bounty.create',terms(name='restricted-bounty',
        eligibility={'kind':'allowlist','subjects':[claimant]}),
        key=pubkey,subject=publisher)
    assert restricted.status=='ok',wire(restricted)
    excluded=await call(app,'bounty.challenge',
        {'listing_id':restricted.data['bounty']['listing_id']},
        key=other_key,subject=other)
    assert excluded.error.code=='bounty_ineligible'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM bounty_claims WHERE listing_id=?',(listing,))[0]==0
        assert tx.one('SELECT consumed_at FROM bounty_challenges WHERE id=?',
                      (payload['challenge_id'],))[0] is None
        assert tx.one("SELECT COUNT(*) FROM messages WHERE recipient=? AND body LIKE '%bounty_claim%'",
                      (claimant,))[0]==0


@pytest.mark.asyncio
async def test_concurrent_last_reward_only_one_paid_claim(installed):
    app,_=installed
    pubkey,publisher,_=await register(app,'bounty-race-publisher')
    akey,alice,_=await register(app,'bounty-race-alice')
    bkey,bob,_=await register(app,'bounty-race-bob')
    await fund(app,publisher,10)
    created=await call(app,'bounty.create',terms(max_claims=2),
                       key=pubkey,subject=publisher)
    assert created.status=='ok',wire(created)
    listing=created.data['bounty']['listing_id']
    a=await call(app,'bounty.challenge',{'listing_id':listing},key=akey,subject=alice)
    b=await call(app,'bounty.challenge',{'listing_id':listing},key=bkey,subject=bob)
    assert a.status==b.status=='ok'
    async def take(key, subject, payload, rid):
        proof=wire(key.sign(canonical(payload),purpose='bounty-pop-v1'))
        return await call(app,'bounty.claim',{'challenge_id':payload['challenge_id'],
                          'proof':proof},key=key,subject=subject,rid=rid)
    results=await asyncio.gather(take(akey,alice,a.data['challenge'],'bounty-race-a'),
                                 take(bkey,bob,b.data['challenge'],'bounty-race-b'))
    assert sorted(r.status for r in results)==['error','ok'],[wire(r) for r in results]
    assert sorted([await balance(app,akey,alice),await balance(app,bkey,bob)])==[0,10]
    async with app.metadata.transaction(write=False) as tx:
        escrow=created.data['funding']['body']['to_subject']
        assert tx.one('SELECT COUNT(*) FROM bounty_claims WHERE listing_id=?',(listing,))[0]==1
        assert tx.one('SELECT COUNT(*) FROM money_ledger WHERE debit_account=?',(escrow,))[0]==1
        assert tx.one('SELECT COUNT(*) FROM bounty_challenges WHERE consumed_at IS NOT NULL AND listing_id=?',
                      (listing,))[0]==1


@pytest.mark.asyncio
async def test_close_returns_unspent_escrow_and_blocks_pending_challenge(installed):
    app,_=installed
    pubkey,publisher,_=await register(app,'bounty-close-publisher')
    key,claimant,_=await register(app,'bounty-close-claimant')
    await fund(app,publisher,20)
    created=await call(app,'bounty.create',terms(budget_minor=20,max_claims=2),
                       key=pubkey,subject=publisher)
    assert created.status=='ok',wire(created)
    listing=created.data['bounty']['listing_id']
    challenge=await call(app,'bounty.challenge',{'listing_id':listing},
                         key=key,subject=claimant)
    assert challenge.status=='ok'
    closed=await call(app,'bounty.close',{'listing_id':listing},
                      key=pubkey,subject=publisher,rid='close-bounty')
    assert closed.status=='ok',wire(closed)
    assert closed.data['returned']['body']['kind']=='refund'
    assert closed.data['returned']['body']['amount_minor']==20
    assert closed.data['bounty']['state']=='closed'
    assert await balance(app,pubkey,publisher)==20
    proof=wire(key.sign(canonical(challenge.data['challenge']),purpose='bounty-pop-v1'))
    blocked=await call(app,'bounty.claim',{
        'challenge_id':challenge.data['challenge']['challenge_id'],'proof':proof},
        key=key,subject=claimant)
    assert blocked.error.code=='bounty_not_active'
    assert await balance(app,key,claimant)==0
    replay=await call(app,'bounty.close',{'listing_id':listing},
                      key=pubkey,subject=publisher,rid='close-bounty')
    assert replay.status=='ok' and replay.replayed
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM bounty_claims WHERE listing_id=?',(listing,))[0]==0
        escrow=created.data['funding']['body']['to_subject']
        assert tx.one('SELECT COUNT(*) FROM money_ledger WHERE debit_account=?',(escrow,))[0]==1
