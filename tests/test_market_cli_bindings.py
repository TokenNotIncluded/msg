"""Market shortcuts resolve actual Registry operations, not a permissive mock."""
import pytest
from read_only_evidence import business_snapshot
from test_market_arbitration import configured, reasoned_base, vote
from test_market_lifecycle import market
from test_orders import _intent
from test_service import call

from msg.cli import arguments, parser
from msg.client_market import COMMANDS, run_command
from msg.core.codec import canonical, digest, loads, wire
from msg.core.errors import Failure


class ExecutorClient:
    """Use real signatures, schema validation, authorization and PostgreSQL."""

    def __init__(self, app, key, subject):
        self.app, self.key, self.subject = app, key, subject

    async def call(self, operation, params, **options):
        return await call(self.app, operation, params, key=self.key, subject=self.subject,
                          rid=options.get('request_id'), expected=options.get('expected', ()),
                          contract_version=options.get('contract_version', 1))


async def command(client, *argv):
    return await run_command(client, parser().parse_args(list(argv)), arguments)


@pytest.mark.asyncio
async def test_every_advertised_shortcut_resolves_an_actual_registered_operation(installed):
    app, _root = installed
    unknown = []
    for group, actions in COMMANDS.items():
        for action, (operation, field) in actions.items():
            if operation is None:
                assert (group, action) == ('bounty', 'prove')
                continue
            try:
                spec = app.registry.operation(operation, 2 if operation == 'money.redeem' else 1)
            except Failure:
                unknown.append((group, action, operation))
                continue
            schema = app.registry.schema(spec.input_schema)
            if field not in {None, 'json'}:
                assert field in schema['properties'], (group, action, field)
                assert set(schema.get('required', ())) <= {field}, (group, action)
    assert unknown == []


@pytest.mark.asyncio
async def test_pay_shortcut_uses_real_funding_and_idempotency(installed):
    app, root = installed
    _sk, _seller, buyer_key, buyer, listing, _package = await market(app, root)
    client = ExecutorClient(app, buyer_key, buyer)
    created = await command(client, 'orders', 'create', canonical(_intent(listing)).decode())
    assert created.status == 'ok', wire(created)
    order = created.data['order']
    params = {'order_id': order['id'], 'order_digest': order['order_digest'],
              'currency_id': 'primary', 'total_price_minor': order['total_price_minor']}
    argv = ('orders', 'pay', canonical(params).decode(), '--request-id', 'cli-fund')
    result = await command(client, *argv)
    assert result.status == 'ok', wire(result)
    before = await business_snapshot(app)
    replay = await command(client, *argv)
    assert replay.status == 'ok' and replay.replayed and replay.data == result.data
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_dispute_shortcut_preserves_reason_and_uses_real_operation(installed):
    app, root = installed
    _sk, _seller, buyer_key, buyer, listing, _package = await market(app, root, mode='service', kind='service')
    purchased = await call(app, 'orders.buy', _intent(listing), key=buyer_key,
                           subject=buyer, contract_version=3)
    assert purchased.status == 'ok', wire(purchased)
    client = ExecutorClient(app, buyer_key, buyer)
    result = await command(client, 'orders', 'dispute', canonical({
        'order_id': purchased.data['order']['id'], 'reason': 'quality'}).decode())
    assert result.status == 'ok', wire(result)
    case = await call(app, 'orders.dispute_get', {'case_id': result.data['case_id']},
                      key=buyer_key, subject=buyer)
    assert case.status == 'ok' and case.data['case']['reason'] == 'quality', wire(case)


@pytest.mark.asyncio
async def test_resolve_shortcut_requires_a_real_signed_quorum_decision(installed):
    app, _root, members, _sk, _seller, buyer_key, buyer, _order, opened = await configured(installed)
    case_id = opened['case_id']
    base = await reasoned_base(app, members[opened['panel'][0]], opened['panel'][0], case_id)
    for uid in opened['panel'][:2]:
        decision = await vote(app, members[uid], uid, base)
        assert decision.status == 'ok', wire(decision)
    proposal = decision.data['decision']
    assert proposal is not None
    args = {'case_id': case_id, 'decision_id': proposal['id']}
    client = ExecutorClient(app, buyer_key, buyer)
    result = await command(client, 'orders', 'resolve', canonical(args).decode(), '--request-id', 'cli-execute')
    assert result.status == 'ok', wire(result)
    before = await business_snapshot(app)
    replay = await command(client, 'orders', 'resolve', canonical(args).decode(), '--request-id', 'cli-execute')
    assert replay.status == 'ok' and replay.replayed and replay.data == result.data
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_contract_shortcut_reads_the_existing_locked_contract(installed):
    app, root = installed
    _sk, _seller, buyer_key, buyer, listing, _package = await market(app, root)
    created = await call(app, 'orders.create', _intent(listing), key=buyer_key, subject=buyer)
    assert created.status == 'ok'
    order = created.data['order']
    before = await business_snapshot(app)
    result = await command(ExecutorClient(app, buyer_key, buyer), 'orders', 'contract', order['id'])
    assert result.status == 'ok', wire(result)
    assert result.data['order']['order_digest'] == order['order_digest']
    async with app.metadata.transaction(write=False) as tx:
        body, committed_digest = tx.one(
            'SELECT body,digest FROM order_contracts WHERE order_id=?', (order['id'],))
    locked = loads(body)
    assert digest(locked) == committed_digest == order['order_digest']
    assert wire(result.data['contract']) == {key: value for key, value in locked.items()
                                           if key not in {'buyer_principal', 'handle_snapshot'}}
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_package_shortcut_binds_actual_id_and_preserves_seller_privacy(installed):
    app, root = installed
    seller_key, seller, buyer_key, buyer, _listing, package = await market(app, root)
    before = await business_snapshot(app)
    result = await command(ExecutorClient(app, seller_key, seller), 'store', 'package', package['id'])
    assert result.status == 'ok', wire(result)
    assert result.data['package']['id'] == package['id']
    assert result.data['package']['digest'] == package['digest']
    outsider = await command(ExecutorClient(app, buyer_key, buyer), 'store', 'package', package['id'])
    assert outsider.status == 'error' and outsider.error.code == 'package_not_found', wire(outsider)
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('version', [1, 2, 3])
async def test_explicit_buy_version_preserves_old_inputs_and_settlement_semantics(installed, version):
    app, root = installed
    _seller_key, _seller, buyer_key, buyer, listing, package = await market(app, root)
    params = _intent(listing)
    if version == 2:
        params['package_digest'] = package['digest']
    argv = ('orders', 'buy', canonical(params).decode(), '--contract-version', str(version),
            '--request-id', 'cli-versioned-buy')
    client = ExecutorClient(app, buyer_key, buyer)
    result = await command(client, *argv)
    assert result.status == 'ok', wire(result)
    assert result.data['order']['state'] == {1: 'funded', 2: 'delivered', 3: 'settled'}[version]
    before = await business_snapshot(app)
    replay = await command(client, *argv)
    assert replay.status == 'ok' and replay.replayed and replay.data == result.data
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_redeem_keeps_version_two_default_and_can_explicitly_select_one(installed):
    from test_market_redemption import setup_offer

    app, root = installed
    key, owner, quote, _fields = await setup_offer(app, root)
    client = ExecutorClient(app, key, owner)
    pending = await command(client, 'money', 'redeem', canonical({**quote, 'defer': True}).decode(),
                            '--request-id', 'cli-pending')
    assert pending.status == 'ok' and pending.data['purchase']['state'] == 'pending', wire(pending)
    immediate = await command(client, 'money', 'redeem', canonical(quote).decode(),
                              '--contract-version', '1', '--request-id', 'cli-immediate')
    assert immediate.status == 'ok' and immediate.data['purchase']['state'] == 'settled', wire(immediate)


@pytest.mark.asyncio
@pytest.mark.parametrize('version', ['0', '-1', str(2**31)])
async def test_out_of_range_version_is_rejected_before_client_or_business_writes(installed, version):
    app, _root = installed
    before = await business_snapshot(app)
    # No client at all: the local option guard must precede any client call.
    with pytest.raises(Failure, match='^invalid_contract_version$'):
        await command(None, 'money', 'state', '--contract-version', version)
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_bootstrap_order_policy_matches_the_actual_default_listing(installed):
    from importlib.resources import files

    app, root = installed
    _sk, _seller, _bk, _buyer, listing, _package = await market(app, root)
    manifest = loads(files('msg.data').joinpath('bootstrap.json').read_bytes())
    defaults = next(item['default_config'] for item in manifest['features']
                    if item['feature_id'] == 'orders')
    assert defaults['escrow_policy'] == listing['escrow_policy'] == 'escrow-v1'
    assert 'instant_policy' not in defaults
