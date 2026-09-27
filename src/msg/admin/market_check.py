"""Read-only financial invariants and a deliberately isolated Test Root scenario."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from msg.core.codec import b64, canonical, digest, loads, wire
from msg.core.errors import require
from msg.core.models import MailConfig
from msg.market.policy import contract, validate
from msg.plugins.money import _balance


async def inspect_market(tx):
    """No initialization, repair, timer, statement, event, job or network call."""
    count = 0
    for oid,buyer,total,escrow,state in tx.rows(
        'SELECT id,buyer,total_price_minor,escrow_subject,state FROM store_orders ORDER BY id'):
        expected = total if state in {'funded','delivered','accepted','disputed'} else 0
        require(_balance(tx,escrow)==expected, 'market_escrow_invariant')
        require(tx.one('SELECT 1 FROM identities WHERE id=?',(escrow,)) is None and
                tx.one('SELECT kind,subject_id FROM ledger_accounts WHERE id=?',(escrow,))==('order_escrow',None),
                'market_escrow_identity')
        snapshot = tx.one('SELECT body FROM order_contracts WHERE order_id=?',(oid,))
        if snapshot:
            locked = contract(tx,oid)
            policy = locked['policy']
            validate(policy['policy'])
            require(digest(policy['policy'])==policy['policy_digest'] and
                    locked['buyer']==buyer and locked['total_price_minor']==total, 'market_snapshot_invariant')
        row = tx.one('SELECT body FROM order_settlements WHERE order_id=?',(oid,))
        if row:
            fact = loads(row[0])
            require(state in {'settled','refunded'} and fact['refund_minor']+fact['release_minor']==total,
                    'market_settlement_invariant')
            actual = tx.one("SELECT COALESCE(SUM(amount_minor),0) FROM money_ledger WHERE debit_account=?",(escrow,))[0]
            require(actual==total,'market_settlement_invariant')
        elif snapshot:
            require(state not in {'settled','refunded'},'market_settlement_missing')
        count += 1
    for raw,expected in tx.rows('SELECT body,digest FROM arbitration_policies'):
        policy = loads(raw); validate(policy)
        require(digest(policy)==expected,'arbitration_policy_corrupt')
    for order_id,state in tx.rows('SELECT order_id,state FROM arbitration_cases'):
        settlement = tx.one('SELECT decision_id FROM order_settlements WHERE order_id=?',(order_id,))
        if state=='executed':
            require(settlement is not None and settlement[0] is not None,'market_decision_unfunded')
    return {'orders':count,'held_cases':tx.one("SELECT COUNT(*) FROM arbitration_cases WHERE state='held'")[0],
            'escrow_invariants':True,'private_reads_only':True}


async def check_market(app,root,call,register,now):
    """20 MSG issue -> bank-funded 10 reward -> 5 bundle, entirely disposable."""
    from msg.admin.market import apply_market
    from msg.admin.money import apply_money
    from msg.market.policy import DEFAULT_POLICY
    from msg.workers.effects import EffectWorker
    require(app.selftest_run_id is not None,'selftest_namespace_required')
    bank,bank_id = await register('market-test-bank')
    buyer,buyer_id = await register('market-test-buyer')
    async def invoke(name,args,key=buyer,subject=buyer_id,**kw):
        result = await call(name,args,key,subject,**kw)
        require(result.status=='ok',result.error.code if result.error else 'market_selftest_failed')
        return result
    await apply_money(app,root,action='bank_add',operator='isolated-selftest',subject_id=bank_id)
    await apply_money(app,root,action='mint',operator='isolated-selftest',amount_minor=20_000_000)
    await apply_money(app,root,action='transfer',operator='isolated-selftest',subject_id=bank_id,amount_minor=20_000_000)
    bounty = await invoke('bounty.create',{'name':'identity-test','terms':'One-use signature test',
        'reward_minor':10_000_000,'budget_minor':10_000_000,'max_claims':1},bank,bank_id)
    challenge = await invoke('bounty.challenge',{'listing_id':bounty.data['bounty']['listing_id']})
    payload = challenge.data['challenge']
    await invoke('bounty.claim',{'challenge_id':payload['challenge_id'],
        'proof':wire(buyer.sign(canonical(payload),purpose='bounty-pop-v1'))})
    listing = await invoke('store.listing_create',{'name':'store-selftest','item_kind':'bundle',
        'price_minor':5_000_000,'currency_id':'primary','quantity':1,'delivery_mode':'managed_instant',
        'escrow_policy':'escrow-v1','dispute_policy':'dispute-v1','terms':'Immutable test bundle.'},bank,bank_id)
    async with app.metadata.transaction(write=False) as tx:
        parent = tx.one("SELECT id FROM resources WHERE parent=? AND name='files'",(bank_id,))[0]
    file = await invoke('content.file_put',{'parent':parent,'name':'hello.txt','media_type':'text/plain',
        'data':b64(b'delivery-ok\n')},bank,bank_id)
    package = await invoke('store.package_deposit',{'listing_id':listing.resources[0].id,
        'listing_revision':listing.resources[0].revision,'manifest':{'text':'msg.lmm.best store selftest'},
        'payload_refs':[wire(file.resources[0])]},bank,bank_id)
    active = await invoke('store.listing_update',{'id':listing.resources[0].id,'state':'active',
        'package_ref':package.data['package']['id']},bank,bank_id,
        expected=((listing.resources[0].id,listing.data['generation']),))
    old_settings = app.settings
    class NoNetworkMail:
        def __init__(self): self.messages=[]
        async def send(self,job):
            self.messages.append(dict(job.arguments)); return 'sent'
    sender=NoNetworkMail()
    app.settings=replace(app.settings,server=replace(app.settings.server,mail=MailConfig(enabled=True,
        host='selftest.invalid',port=2525,tls='starttls',sender='test@selftest.invalid',credential_file=None)))
    try:
        sale = active.data['listing']
        bought = await invoke('orders.buy',{'listing_id':sale['listing_id'],
            'listing_revision':sale['listing_revision'],'quantity':1,'total_price_minor':5_000_000,
            'currency_id':'primary','email':'buyer@selftest.invalid'},contract_version=2)
        oid = bought.data['order']['id']
        worker=EffectWorker(app,mail_sender=sender)
        while await worker.run_once(): pass
        require(len(sender.messages)==1 and oid not in sender.messages[0]['text'],'market_email_leak')
        proof=loads(sender.messages[0]['text'])
        await invoke('orders.email_verify',{'order_id':oid,'challenge_id':proof['challenge_id'],'token':proof['token']})
        while await worker.run_once(): pass
        require(len(sender.messages)==2 and set(loads(sender.messages[1]['text']))=={'order_id','handle','pickup'},
                'market_email_leak')
        delivery=await invoke('delivery.get',{'order_id':oid},contract_version=2)
        require(delivery.data['delivery']['state']=='prepared' and
                delivery.data['delivery']['payloads'][0]['data']==b64(b'delivery-ok\n'),'market_claim_invariant')
    finally:
        app.settings=old_settings
    # A separate 1 MSG service dispute refunds the test buyer through a real
    # fixed 2-of-3 panel. It leaves the official 5/15 final balances unchanged.
    members={}
    for i in range(3):
        key,uid=await register('market-test-arbitrator-'+str(i)); members[uid]=key
        await apply_market(app,root,action='grant',operator='isolated-selftest',subject=uid)
    policy=deepcopy(DEFAULT_POLICY); policy.update(id='selftest-panel-v1',candidates=list(members))
    await apply_market(app,root,action='publish',operator='isolated-selftest',policy=policy)
    service=await invoke('store.listing_create',{'name':'case-selftest','item_kind':'service',
        'price_minor':1_000_000,'currency_id':'primary','quantity':1,'delivery_mode':'service',
        'escrow_policy':'escrow-v1','dispute_policy':policy['id'],'terms':'Test service dispute.'},bank,bank_id)
    service=await invoke('store.listing_update',{'id':service.resources[0].id,'state':'active'},bank,bank_id,
        expected=((service.resources[0].id,service.data['generation']),))
    sale=service.data['listing']
    bought=await invoke('orders.buy',{'listing_id':sale['listing_id'],'listing_revision':sale['listing_revision'],
        'quantity':1,'total_price_minor':1_000_000,'currency_id':'primary'},contract_version=2)
    opened=await invoke('orders.dispute_open',{'order_id':bought.data['order']['id'],'reason':'quality'})
    case=await invoke('orders.dispute_get',{'case_id':opened.data['case_id']})
    proposal={**case.data['proposal_base'],'outcome':'refund','refund_minor':1_000_000}
    for uid in opened.data['panel'][:2]:
        voted=await invoke('orders.dispute_vote',{'proposal':proposal,
            'signature':wire(members[uid].sign(canonical(proposal),purpose='arbitration-decision'))},members[uid],uid)
    args={'case_id':opened.data['case_id'],'decision_id':voted.data['decision']['id']}
    await invoke('orders.dispute_execute',args)
    await invoke('orders.dispute_execute',args)
    async with app.metadata.transaction(write=False) as tx:
        await inspect_market(tx)
        require(_balance(tx,buyer_id)==5_000_000 and _balance(tx,bank_id)==15_000_000 and
            tx.one("SELECT SUM(amount_minor) FROM money_ledger WHERE kind='mint'")[0]==20_000_000,
            'market_conservation_failed')
        for (escrow,) in tx.rows("SELECT id FROM ledger_accounts WHERE kind IN ('order_escrow','bounty_escrow')"):
            require(_balance(tx,escrow)==0,'market_escrow_invariant')
    return True
