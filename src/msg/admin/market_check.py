"""Read-only clearing inspection and the isolated, real signed market selftest."""
from msg.constants import ROOT_SUBJECT
from msg.core.codec import b64, canonical, decode, digest, loads, wire
from msg.core.errors import require
from msg.core.models import Signature
from msg.plugins.money import MAX_MINOR, _supply
from msg.security.crypto import Ed25519Signer, verify


def inspect_clearing(app, tx):
    """Never migrate, mint, seed a bank, repair a row or unlock the Root key."""
    tables = ('ledger_accounts','money_ledger','money_bank_roles','server_offers',
              'money_purchases','resource_entitlements','bounty_listings','bounty_claims',
              'store_orders','store_deliveries','store_order_events')
    for table in tables:
        require(tx.one('SELECT to_regclass(?)',(table,))[0] is not None, 'market_schema_missing')
    require(tx.one("SELECT 1 FROM information_schema.columns WHERE table_name='money_ledger' "
                   "AND column_name='entry_key'") is not None, 'market_schema_missing')
    balances = {account:int(amount) for account,amount in tx.rows('''
        SELECT account,SUM(amount) FROM (
            SELECT credit_account account,amount_minor amount FROM money_ledger WHERE credit_account IS NOT NULL
            UNION ALL
            SELECT debit_account account,-amount_minor amount FROM money_ledger WHERE debit_account IS NOT NULL
        ) entries GROUP BY account''')}
    supply = _supply(tx)
    require(0 <= supply <= MAX_MINOR and all(0 <= n <= MAX_MINOR for n in balances.values())
            and sum(balances.values()) == supply, 'money_conservation_failure')
    require(tx.one("SELECT 1 FROM ledger_accounts a JOIN identities i ON i.id=a.id "
                   "WHERE a.kind<>'subject' LIMIT 1") is None, 'escrow_has_identity')
    public = Ed25519Signer.from_bytes((app.settings.service_keys/'receipt.key').read_bytes()).public_key
    ledger = {}
    for row in tx.rows('''SELECT id,kind,amount_minor,debit_account,credit_account,seq,
        policy_version,policy_digest,receipt,actor,request_id,entry_key FROM money_ledger'''):
        receipt = loads(row[8]); body = receipt['body']
        expected = dict(zip(('transaction_id','kind','amount_minor','from_subject','to_subject',
                             'ledger_sequence','policy_version','policy_digest'),row[:8]))
        require(all(body.get(k)==v for k,v in expected.items()), 'money_receipt_mismatch')
        if row[6] >= 2:
            require((body.get('actor'),body.get('request_id'),body.get('entry_key')) == row[9:],
                    'money_receipt_mismatch')
        verify(public,canonical(body),decode(Signature,receipt['signature']),purpose='money-receipt')
        ledger[row[0]] = body
    for oid,escrow,total,state,quote,refs in tx.rows('''SELECT id,escrow_subject,total_price_minor,
        state,quote_digest,receipt_refs FROM store_orders'''):
        require(state != 'accepted', 'incomplete_order_commit')
        expected = total if state in {'funded','delivered','disputed'} else 0
        require(balances.get(escrow,0)==expected, 'order_escrow_mismatch')
        require(all(ref in ledger for ref in loads(refs)), 'order_receipt_missing')
        if quote is not None:
            last=tx.one('SELECT to_state FROM store_order_events WHERE order_id=? ORDER BY seq DESC LIMIT 1',(oid,))
            require(last is not None and last[0]==state, 'order_history_mismatch')
        if state=='settled':
            delivery=tx.one('SELECT d.state,o.escrow_policy FROM store_deliveries d '
                            'JOIN store_orders o ON o.id=d.order_id WHERE o.id=?',(oid,))
            require(delivery is not None and (delivery[0]=='claimed' or
                    delivery==('prepared','managed-instant-v1')), 'settled_delivery_missing')
    for escrow,total,state,eid in tx.rows('SELECT escrow_account,total_minor,state,entitlement_id FROM money_purchases'):
        require(balances.get(escrow,0)==(total if state=='pending' else 0),'purchase_escrow_mismatch')
        require((state=='settled') == (eid is not None), 'purchase_entitlement_mismatch')
    for lid,escrow,budget,reward,maximum,state in tx.rows('''SELECT listing_id,escrow_subject,
        budget_minor,reward_minor,max_claims,state FROM bounty_listings'''):
        claims=tx.rows('SELECT claimant,reward_minor,transaction_id,challenge_id FROM bounty_claims WHERE listing_id=?',(lid,))
        require(len(claims)<=maximum, 'bounty_limit_mismatch')
        require(balances.get(escrow,0)==(0 if state=='closed' else budget-sum(c[1] for c in claims)),
                'bounty_escrow_mismatch')
        for claimant,amount,tid,cid in claims:
            receipt=ledger.get(tid,{})
            require(amount==reward and (receipt.get('from_subject'),receipt.get('to_subject'),receipt.get('amount_minor'))
                    == (escrow,claimant,amount), 'bounty_receipt_mismatch')
            challenge=tx.one('SELECT consumed_at FROM bounty_challenges WHERE id=?',(cid,))
            require(challenge is not None and challenge[0] is not None, 'bounty_nonce_mismatch')
    return {'currency_id':'primary','scale':6,'total_supply_minor':supply,
        'banks':tx.one("SELECT COUNT(*) FROM money_bank_roles WHERE status='active'")[0],
        'enabled_offers':tx.one('SELECT COUNT(*) FROM server_offers WHERE enabled=TRUE')[0],
        'ledger_entries':len(ledger),'conserved':True,'signed_receipts_verified':True,
        'zero_default':'No mint, bank or offer is created by initialization or inspection.'}


async def check_market_e2e(app, root, call, register):
    """Only TestConsole IO is replaced; command parsing/approvals/clearing are real.

    Production MoneyAdmin still requires an OS-root physical console and hidden
    PIN. This fixture cannot be selected by configuration or a network request.
    """
    require(app.selftest_run_id is not None and app.settings.service_url=='https://selftest.invalid',
            'isolated_market_selftest_required')
    from msg.admin.money import _confirmed_money
    from msg.daemon import parser
    async with app.metadata.transaction(write=False) as tx:
        default=inspect_clearing(app,tx)
        require(default['total_supply_minor']==default['banks']==default['enabled_offers']==0,
                'market_selftest_not_empty')
    bank_key,bank=await register('bank-test')
    buyer_key,buyer=await register('market-buyer')

    async def command(argv, expected_actions):
        args=parser().parse_args(argv)
        action='bank_fund' if args.money_command=='bank' and args.bank_command=='fund' else args.money_command
        previews=[]; approvals=[]
        def emit(value): previews.append(loads(value))
        def confirm(prompt):
            approval='CONFIRM MONEY '+digest(previews[-1])
            require(prompt=='Type '+approval+' to continue: ', 'selftest_approval_unbound')
            approvals.append(previews[-1]['approval_for'])
            return approval
        def unlock():
            require(approvals==expected_actions, 'selftest_unlock_before_confirmation')
            return root  # Fresh disposable Test Root; never opens production key material.
        return await _confirmed_money(app,action,operator='isolated-TestConsole',amount=args.amount,
            subject_id=getattr(args,'subject_id',None),emit=emit,confirm=confirm,unlock=unlock)

    await command(['money','mint','20'], ['mint'])
    funding=await command(['money','bank','fund','@bank-test','20'], ['bank_add','transfer'])
    require(len(funding['audit_event_ids'])==2 and funding['root_balance_minor']==0, 'selftest_bank_fund_failed')
    async def checked(name,args,key=None,subject=None,**kwargs):
        result=await call(name,args,key,subject,**kwargs)
        require(result.status=='ok','market_selftest_'+name.replace('.','_'))
        return result
    bounty=await checked('bounty.create',{'name':'signature-reward',
        'terms':'Control of the current IdentityKey only, not human or Sybil proof.',
        'reward_minor':10_000_000,'budget_minor':10_000_000,'max_claims':1,
        'claim_limit_per_subject':1},bank_key,bank)
    lid=bounty.data['bounty']['listing_id']
    challenge=await checked('bounty.challenge',{'listing_id':lid},buyer_key,buyer)
    proof=challenge.data['challenge']
    claimed=await checked('bounty.claim',{'challenge_id':proof['challenge_id'],
        'proof':wire(buyer_key.sign(canonical(proof),purpose='bounty-pop-v1'))},buyer_key,buyer)
    require(claimed.data['reward']['body']['amount_minor']==10_000_000 and
            claimed.data['escrow_balance_minor']==0,'selftest_bounty_not_paid')
    listing=await checked('store.listing_create',{'name':'test-delivery','item_kind':'bundle',
        'price_minor':5_000_000,'currency_id':'primary','quantity':1,
        'delivery_mode':'managed_instant','escrow_policy':'managed-instant-v1',
        'dispute_policy':'dispute-v1','terms':'Fixed text and file; immediate in-site delivery.'},bank_key,bank)
    async with app.metadata.transaction(write=False) as tx:
        files=await tx.resolve((await tx.path(bank))+'/files')
    payload=await checked('content.file_put',{'parent':files,'name':'hello.txt','data':b64(b'delivery-ok\n'),
        'media_type':'text/plain'},bank_key,bank)
    listing_id=listing.resources[0].id
    package=await checked('store.package_deposit',{'listing_id':listing_id,
        'listing_revision':listing.resources[0].revision,'manifest':{'text':'msg.lmm.best store selftest'},
        'payload_refs':[wire(payload.resources[0])]},bank_key,bank)
    active=await checked('store.listing_update',{'id':listing_id,'state':'active','package_ref':package.data['package']['id']},
        bank_key,bank,expected=((listing_id,listing.data['generation']),))
    quote=active.data['listing']
    args={'listing_id':listing_id,'listing_revision':quote['listing_revision'],'quantity':1,
          'currency_id':'primary','total_price_minor':5_000_000}
    # No seller request occurs after publishing. The signed buyer request alone
    # validates the immutable deposit, delivers, then releases the exact escrow.
    bought=await checked('orders.buy',args,buyer_key,buyer,request_id='market-e2e-buy')
    require(bought.data['order']['state']=='settled','selftest_order_not_settled')
    delivery=await checked('delivery.get',{'order_id':bought.data['order']['id']},buyer_key,buyer)
    content=delivery.data['delivery']
    require(content['manifest']=={'text':'msg.lmm.best store selftest'} and
            content['payloads'][0]['data']==b64(b'delivery-ok\n') and
            content['package_digest']==package.data['package']['digest'],'selftest_delivery_mismatch')
    repeated=await checked('orders.buy',args,buyer_key,buyer,request_id='market-e2e-buy')
    require(repeated.replayed and repeated.data==bought.data,'selftest_market_replay_failed')
    buyer_balance=await checked('money.balance',{},buyer_key,buyer)
    bank_balance=await checked('money.balance',{},bank_key,bank)
    require(buyer_balance.data['balance_minor']==5_000_000 and bank_balance.data['balance_minor']==15_000_000,
            'selftest_market_balance_mismatch')
    async with app.metadata.transaction(write=False) as tx:
        checked_state=inspect_clearing(app,tx)
        require(checked_state['total_supply_minor']==20_000_000 and checked_state['banks']==1,
                'selftest_market_supply_mismatch')
    return True
