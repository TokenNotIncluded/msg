"""Small CLI adapters over the declared market contracts, never a second API."""

from collections.abc import Mapping
from uuid import uuid4

from msg.core.codec import canonical, digest, parse_time, unb64, wire
from msg.core.errors import require

# Values are (operation, positional argument). JSON is parsed by the CLI's
# existing bounded JSON/@file/stdin reader. Exact prices remain explicit input.
COMMANDS = {
    'money': {
        'state': ('money.state', None),
        'banks': ('money.banks', None),
        'balance': ('money.balance', None),
        'ledger': ('money.ledger', 'json'),
        'offers': ('money.offers', None),
        'entitlements': ('money.entitlements', None),
        'transfer': ('money.transfer', 'json'),
        'redeem': ('money.redeem', 'json'),
        'purchase': ('money.purchase_get', 'purchase_id'),
        'settle': ('money.purchase_settle', 'purchase_id'),
        'cancel': ('money.purchase_cancel', 'purchase_id'),
    },
    'store': {
        'get': ('store.listing_get', 'id'),
        'create': ('store.listing_create', 'json'),
        'update': ('store.listing_update', 'json'),
        'deposit': ('store.package_deposit', 'json'),
        'package': ('store.package_get', 'id'),
    },
    'bounty': {
        'get': ('bounty.get', 'listing_id'),
        'create': ('bounty.create', 'json'),
        'top-up': ('bounty.top_up', 'json'),
        'pause': ('bounty.pause', 'listing_id'),
        'resume': ('bounty.resume', 'listing_id'),
        'close': ('bounty.close', 'listing_id'),
        'claims': ('bounty.claims', 'listing_id'),
        'challenge': ('bounty.challenge', 'listing_id'),
        'claim': ('bounty.claim', 'json'),
        'prove': (None, 'listing_id'),
    },
    'orders': {
        'create': ('orders.create', 'json'),
        'buy': ('orders.buy', 'json'),
        'pay': ('orders.fund', 'json'),
        'get': ('orders.get', 'order_id'),
        'list': ('orders.list', 'json'),
        'payment': ('orders.payment', 'order_id'),
        'cancel': ('orders.cancel', 'order_id'),
        'contract': ('orders.contract', 'order_id'),
        'resolve': ('orders.dispute_execute', 'json'),
        'dispute': ('orders.dispute_open', 'json'),
        'case': ('orders.dispute_get', 'case_id'),
    },
    'delivery': {
        'get': ('delivery.get', 'order_id'),
        'prepare': ('delivery.prepare', 'order_id'),
        'accept': ('delivery.accept', 'json'),
        'submit': ('delivery.submit', 'json'),
        'claim': ('delivery.claim', 'json'),
    },
}


def add_commands(commands):
    for group, actions in COMMANDS.items():
        sub = commands.add_parser(
            group, help='Signed market operations; amounts are exact minor units.'
        ).add_subparsers(dest='action', required=True)
        for action, (operation, field) in actions.items():
            help_text = (
                'Sign a current-key PoP challenge and claim; not a human/Sybil check.'
                if group == 'bounty' and action == 'prove'
                else None
            )
            if operation == 'money.redeem':
                help_text = 'Sign current Resource terms and synchronous grant/settlement; legacy needs --contract-version 1 or 2.'
            cmd = sub.add_parser(action, help=help_text, description=help_text)
            if field == 'json':
                cmd.add_argument(
                    'payload', nargs='?', default='{}', help='JSON, @file or - for stdin.'
                )
            elif field:
                cmd.add_argument('value')
            cmd.add_argument(
                '--request-id', help='Reuse exactly this ID and input after an uncertain response.'
            )
            if operation is not None:
                cmd.add_argument(
                    '--contract-version',
                    type=int,
                    help='Explicit published operation version; never changes the signed input silently.',
                )
            if group == 'store' and action == 'update':
                cmd.add_argument('--generation', type=int, required=True)


async def prove_bounty(client, listing_id, request_id=None):
    require(
        client.state.signer is not None and client.state.subject is not None,
        'signing_identity_required',
    )
    request_id = request_id or uuid4().hex
    # Repeat the SAME challenge request after a lost claim response. Never
    # silently replace the nonce or extend TTL while retaining the claim ID.
    challenge_id = (
        'bounty-challenge-' + digest((client.state.subject, listing_id, request_id))[7:39]
    )
    challenged = await client.call(
        'bounty.challenge', {'listing_id': listing_id}, request_id=challenge_id
    )
    if challenged.status != 'ok':
        return challenged
    payload = challenged.data.get('challenge')
    fields = {
        'challenge_id',
        'listing_id',
        'claimant_subject_id',
        'nonce',
        'issued_at',
        'expires_at',
        'verifier_version',
    }
    require(
        isinstance(payload, Mapping)
        and set(payload) == fields
        and payload['listing_id'] == listing_id
        and payload['claimant_subject_id'] == client.state.subject
        and payload['verifier_version'] == 1
        and len(unb64(payload['nonce'], limit=32)) == 24
        and 0
        < (parse_time(payload['expires_at']) - parse_time(payload['issued_at'])).total_seconds()
        <= 300,
        'bounty_challenge_mismatch',
    )
    proof = client.state.signer.sign(canonical(payload), purpose='bounty-pop-v1')
    return await client.call(
        'bounty.claim',
        {'challenge_id': payload['challenge_id'], 'proof': wire(proof)},
        request_id=request_id,
    )


async def entitlement_intent(client, params):
    """Bind the current ordinary Resource quote; legacy requires explicit @1/@2."""
    require(
        isinstance(params, Mapping) and isinstance(params.get('offer_id'), str), 'offer_id_required'
    )
    require(
        type(params.get('quantity')) is int and params['quantity'] > 0, 'invalid_offer_quantity'
    )
    require('defer' not in params, 'explicit_legacy_contract_required')
    result = await client.call('store.listing_get', {'id': params['offer_id']}, contract_version=2)
    require(result.status == 'ok', 'offer_resource_required_use_explicit_legacy_contract')
    listing = result.data['listing']
    source = listing.get('server_offer')
    require(
        listing.get('server_offer_model') == 1
        and isinstance(source, Mapping)
        and source.get('enabled') is True
        and listing['listing_id'] == params['offer_id'],
        'offer_resource_required_use_explicit_legacy_contract',
    )
    quote = {k: v for k, v in source.items() if k != 'enabled'}
    quote.update(currency_id='primary', provider_version=1)
    bound = {
        'currency_id': 'primary',
        'price_revision': quote['price_revision'],
        'listing_revision': listing['listing_revision'],
        'offer_snapshot_digest': digest(quote),
        'total_price_minor': quote['price_minor'] * params['quantity'],
        'settlement_policy': 'deterministic-entitlement-v1',
    }
    require(
        all(key not in params or params[key] == value for key, value in bound.items()),
        'offer_intent_changed',
    )
    return {**params, **bound}


async def run_command(client, args, parse_arguments):
    operation, field = COMMANDS[args.command][args.action]
    if operation is None:
        return await prove_bounty(client, args.value, args.request_id)
    params = (
        parse_arguments(args.payload) if field == 'json' else {field: args.value} if field else {}
    )
    kwargs = {'request_id': args.request_id}
    version = getattr(args, 'contract_version', None)
    if version is not None:
        require(type(version) is int and 1 <= version <= 2**31 - 1, 'invalid_contract_version')
        kwargs['contract_version'] = version
    elif operation == 'orders.buy':
        kwargs['contract_version'] = 4
    elif operation == 'orders.create':
        kwargs['contract_version'] = 2
    elif operation == 'money.redeem':
        kwargs['contract_version'] = 3
    if operation == 'money.redeem' and kwargs.get('contract_version') == 3:
        params = await entitlement_intent(client, params)
    if args.command == 'store' and args.action == 'update':
        require(isinstance(params.get('id'), str), 'listing_id_required')
        kwargs['expected'] = ((params['id'], args.generation),)
    return await client.call(operation, params, **kwargs)
