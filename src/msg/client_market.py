"""Small CLI adapters over the declared market contracts, never a second API."""
from collections.abc import Mapping
from uuid import uuid4

from msg.core.codec import canonical, digest, parse_time, unb64, wire
from msg.core.errors import require

# Values are (operation, positional argument). JSON is parsed by the CLI's
# existing bounded JSON/@file/stdin reader. Exact prices remain explicit input.
COMMANDS = {
    'money': {
        'state':('money.state',None), 'banks':('money.banks',None),
        'balance':('money.balance',None), 'ledger':('money.ledger','json'),
        'offers':('money.offers',None), 'entitlements':('money.entitlements',None),
        'transfer':('money.transfer','json'), 'redeem':('money.redeem','json'),
        'purchase':('money.purchase_get','purchase_id'),
        'settle':('money.purchase_settle','purchase_id'),
        'cancel':('money.purchase_cancel','purchase_id'),
    },
    'store': {
        'get':('store.listing_get','id'), 'create':('store.listing_create','json'),
        'update':('store.listing_update','json'),
        'deposit':('store.package_deposit','json'), 'package':('store.package_get','package_id'),
    },
    'bounty': {
        'get':('bounty.get','listing_id'), 'create':('bounty.create','json'),
        'top-up':('bounty.top_up','json'), 'pause':('bounty.pause','listing_id'),
        'resume':('bounty.resume','listing_id'), 'close':('bounty.close','listing_id'),
        'claims':('bounty.claims','listing_id'), 'challenge':('bounty.challenge','listing_id'),
        'claim':('bounty.claim','json'), 'prove':(None,'listing_id'),
    },
    'orders': {
        'create':('orders.create','json'), 'buy':('orders.buy','json'),
        'pay':('orders.pay','json'), 'get':('orders.get','order_id'),
        'list':('orders.list','json'), 'payment':('orders.payment','order_id'),
        'cancel':('orders.cancel','order_id'), 'refund':('orders.refund','order_id'),
        'resolve':('orders.resolve','order_id'), 'dispute':('orders.dispute','order_id'),
        'history':('orders.history','order_id'), 'recipient-key':('orders.recipient_key','order_id'),
    },
    'delivery': {
        'get':('delivery.get','order_id'), 'prepare':('delivery.prepare','order_id'),
        'accept':('delivery.accept','json'), 'submit':('delivery.submit','json'),
        'claim':('delivery.claim','json'),
    },
}


def add_commands(commands):
    for group, actions in COMMANDS.items():
        sub = commands.add_parser(group, help='Signed market operations; amounts are exact minor units.').add_subparsers(
            dest='action', required=True)
        for action, (_, field) in actions.items():
            help_text = ('Sign a current-key PoP challenge and claim; not a human/Sybil check.'
                         if group=='bounty' and action=='prove' else None)
            cmd = sub.add_parser(action, help=help_text)
            if field=='json':
                cmd.add_argument('payload', nargs='?', default='{}', help='JSON, @file or - for stdin.')
            elif field:
                cmd.add_argument('value')
            cmd.add_argument('--request-id', help='Reuse exactly this ID and input after an uncertain response.')
            if group=='store' and action=='update':
                cmd.add_argument('--generation', type=int, required=True)


async def prove_bounty(client, listing_id, request_id=None):
    require(client.state.signer is not None and client.state.subject is not None, 'signing_identity_required')
    request_id = request_id or uuid4().hex
    # Repeat the SAME challenge request after a lost claim response. Never
    # silently replace the nonce or extend TTL while retaining the claim ID.
    challenge_id = 'bounty-challenge-' + digest((client.state.subject,listing_id,request_id))[7:39]
    challenged = await client.call('bounty.challenge',{'listing_id':listing_id},request_id=challenge_id)
    if challenged.status != 'ok':
        return challenged
    payload = challenged.data.get('challenge')
    fields = {'challenge_id','listing_id','claimant_subject_id','nonce','issued_at','expires_at','verifier_version'}
    require(isinstance(payload,Mapping) and set(payload)==fields and
            payload['listing_id']==listing_id and payload['claimant_subject_id']==client.state.subject and
            payload['verifier_version']==1 and len(unb64(payload['nonce'],limit=32))==24 and
            0 < (parse_time(payload['expires_at'])-parse_time(payload['issued_at'])).total_seconds() <= 300,
            'bounty_challenge_mismatch')
    proof = client.state.signer.sign(canonical(payload),purpose='bounty-pop-v1')
    return await client.call('bounty.claim',{'challenge_id':payload['challenge_id'],'proof':wire(proof)},
                             request_id=request_id)


async def run_command(client, args, parse_arguments):
    operation, field = COMMANDS[args.command][args.action]
    if operation is None:
        return await prove_bounty(client,args.value,args.request_id)
    params = parse_arguments(args.payload) if field=='json' else {field:args.value} if field else {}
    kwargs = {'request_id':args.request_id}
    if operation=='money.redeem':
        kwargs['contract_version']=2
    if args.command=='store' and args.action=='update':
        require(isinstance(params.get('id'),str),'listing_id_required')
        kwargs['expected']=((params['id'],args.generation),)
    return await client.call(operation,params,**kwargs)
