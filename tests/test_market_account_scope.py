"""Signed operation names do not let a listing-scoped key use private accounts."""

import pytest
from test_market_lifecycle import market
from test_orders import _intent
from test_route_effect_matrix import database_snapshot
from test_service import call

from msg.core.codec import b64, canonical, wire
from msg.core.models import Scope
from msg.security.capabilities import grant_for
from msg.security.crypto import Ed25519Signer


async def restricted_key(app, owner_key, owner, operation, version=1, *, account=False):
    key = Ed25519Signer.generate()
    capability = operation.split('.')[0] + '.basic'
    grant = grant_for(
        app.registry.capability(capability),
        scope=Scope(resource_id=owner if account else 't_store', descendants=not account),
        operations=(f'{operation}@{version}',),
    )
    proof = key.sign(
        canonical({'subject_id': owner, 'public_key': b64(key.public_key)}), purpose='key-add'
    )
    result = await call(
        app,
        'identity.key_add',
        {
            'public_key': b64(key.public_key),
            'possession_proof': wire(proof),
            'ceiling': wire((grant,)),
        },
        key=owner_key,
        subject=owner,
    )
    assert result.status == 'ok', wire(result)
    return key


@pytest.mark.asyncio
async def test_store_scoped_funding_key_cannot_debit_account(installed):
    app, root = installed
    _, _, buyer_key, buyer, listing, _ = await market(app, root)
    created = await call(app, 'orders.create', _intent(listing), key=buyer_key, subject=buyer)
    assert created.status == 'ok', wire(created)
    order = created.data['order']
    key = await restricted_key(app, buyer_key, buyer, 'orders.fund')
    args = {
        'order_id': order['id'],
        'order_digest': order['order_digest'],
        'total_price_minor': order['total_price_minor'],
        'currency_id': 'primary',
    }
    before = await database_snapshot(app)
    denied = await call(app, 'orders.fund', args, key=key, subject=buyer)
    assert denied.status == 'error' and denied.error.code == 'credential_ceiling', wire(denied)
    assert await database_snapshot(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'operation,version',
    [
        ('orders.get', 1),
        ('orders.payment', 1),
        ('orders.contract', 1),
        ('delivery.get', 1),
        ('delivery.get', 2),
    ],
)
async def test_store_scoped_read_key_cannot_read_private_order(installed, operation, version):
    app, root = installed
    _, _, buyer_key, buyer, listing, _ = await market(app, root)
    bought = await call(
        app, 'orders.buy', _intent(listing), key=buyer_key, subject=buyer, contract_version=3
    )
    assert bought.status == 'ok', wire(bought)
    order = bought.data['order']
    key = await restricted_key(app, buyer_key, buyer, operation, version)
    before = await database_snapshot(app)
    denied = await call(
        app, operation, {'order_id': order['id']}, key=key, subject=buyer, contract_version=version
    )
    assert denied.status == 'error' and denied.error.code == 'credential_ceiling', wire(denied)
    assert await database_snapshot(app) == before


@pytest.mark.asyncio
async def test_cached_funding_result_revalidates_current_account_scope(installed):
    app, root = installed
    _, _, buyer_key, buyer, listing, _ = await market(app, root)
    created = await call(app, 'orders.create', _intent(listing), key=buyer_key, subject=buyer)
    assert created.status == 'ok', wire(created)
    order = created.data['order']
    args = {
        'order_id': order['id'],
        'order_digest': order['order_digest'],
        'total_price_minor': order['total_price_minor'],
        'currency_id': 'primary',
    }
    paid = await call(app, 'orders.fund', args, key=buyer_key, subject=buyer, rid='scope-fund-once')
    assert paid.status == 'ok', wire(paid)
    key = await restricted_key(app, buyer_key, buyer, 'orders.fund')
    before = await database_snapshot(app)
    denied = await call(app, 'orders.fund', args, key=key, subject=buyer, rid='scope-fund-once')
    assert denied.status == 'error' and denied.error.code == 'credential_ceiling', wire(denied)
    assert denied.replayed is False
    assert await database_snapshot(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'operation,version',
    [
        ('orders.get', 1),
        ('orders.payment', 1),
        ('orders.contract', 1),
        ('delivery.get', 1),
        ('delivery.get', 2),
    ],
)
async def test_account_scoped_read_key_preserves_owner_projection(installed, operation, version):
    app, root = installed
    _, _, buyer_key, buyer, listing, _ = await market(app, root)
    bought = await call(
        app, 'orders.buy', _intent(listing), key=buyer_key, subject=buyer, contract_version=3
    )
    assert bought.status == 'ok', wire(bought)
    args = {'order_id': bought.data['order']['id']}
    key = await restricted_key(app, buyer_key, buyer, operation, version, account=True)
    before = await database_snapshot(app)
    original = await call(
        app, operation, args, key=buyer_key, subject=buyer, contract_version=version
    )
    scoped = await call(app, operation, args, key=key, subject=buyer, contract_version=version)
    assert original.status == scoped.status == 'ok', wire(scoped)
    assert scoped.data == original.data
    assert await database_snapshot(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('version', [1, 2, 3, 4])
async def test_catalog_scope_alone_cannot_buy_in_any_published_version(installed, version):
    app, root = installed
    _, _, buyer_key, buyer, listing, package = await market(app, root)
    key = await restricted_key(app, buyer_key, buyer, 'orders.buy', version)
    args = _intent(listing)
    if version == 2:
        args['package_digest'] = package['digest']
    before = await database_snapshot(app)
    result = await call(app, 'orders.buy', args, key=key, subject=buyer, contract_version=version)
    assert result.status == 'error' and result.error.code == 'credential_ceiling', wire(result)
    assert await database_snapshot(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('version', [1, 2])
async def test_catalog_key_cannot_cancel_or_enumerate_private_orders(installed, version):
    app, root = installed
    _, _, buyer_key, buyer, listing, _ = await market(app, root)
    bought = await call(app, 'orders.buy', _intent(listing), key=buyer_key, subject=buyer)
    assert bought.status == 'ok', wire(bought)
    key = await restricted_key(app, buyer_key, buyer, 'orders.cancel', version)
    read_key = await restricted_key(app, buyer_key, buyer, 'orders.list')
    before = await database_snapshot(app)
    denied = await call(
        app,
        'orders.cancel',
        {'order_id': bought.data['order']['id']},
        key=key,
        subject=buyer,
        contract_version=version,
    )
    listed = await call(app, 'orders.list', {}, key=read_key, subject=buyer)
    assert denied.status == listed.status == 'error'
    assert denied.error.code == listed.error.code == 'credential_ceiling'
    assert await database_snapshot(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['http', 'path_get', 'graphql', 'mcp_http'])
async def test_real_transport_blocks_fresh_and_cached_private_payment(installed, mode):
    from datetime import timedelta

    import httpx
    from test_service import NOW

    from msg.core.requests import request_for
    from msg.transports.client import TRANSPORTS
    from msg.transports.http import create_app

    app, root = installed
    _, _, buyer_key, buyer, listing, _ = await market(app, root)
    created = await call(app, 'orders.create', _intent(listing), key=buyer_key, subject=buyer)
    assert created.status == 'ok', wire(created)
    order = created.data['order']
    args = {
        'order_id': order['id'],
        'order_digest': order['order_digest'],
        'total_price_minor': order['total_price_minor'],
        'currency_id': 'primary',
    }
    paid = await call(app, 'orders.fund', args, key=buyer_key, subject=buyer, rid='transport-fund')
    assert paid.status == 'ok', wire(paid)
    key = await restricted_key(app, buyer_key, buyer, 'orders.fund')
    before = await database_snapshot(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        adapter = TRANSPORTS[mode](app.settings.service_url, http=http)
        for request_id in ('transport-fund', 'fresh-denied-fund'):
            packet = request_for(
                'orders.fund',
                args,
                app.settings.service_url,
                signer=key,
                subject=buyer,
                request_id=request_id,
                expires_at=NOW + timedelta(seconds=90),
            )
            denied = await adapter.call(packet)
            assert denied.status == 'error' and denied.error.code == 'credential_ceiling', wire(
                denied
            )
            assert denied.replayed is False
    assert await database_snapshot(app) == before


@pytest.mark.asyncio
async def test_arbitration_account_scope_preserves_public_minimal_summary(installed):
    from test_market_arbitration import configured

    app, _, _, _, _, buyer_key, buyer, _, opened = await configured(installed)
    args = {'case_id': opened['case_id']}
    key = await restricted_key(app, buyer_key, buyer, 'orders.dispute_get')
    before = await database_snapshot(app)
    denied = await call(app, 'orders.dispute_get', args, key=key, subject=buyer)
    public = await call(app, 'orders.dispute_summary', args)
    assert denied.status == 'error' and denied.error.code == 'credential_ceiling', wire(denied)
    assert public.status == 'ok', wire(public)
    assert set(public.data) == {'case_id', 'state', 'policy_digest', 'outcome'}
    assert buyer not in canonical(public.data).decode()
    assert await database_snapshot(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('account', [False, True])
async def test_real_token_read_uses_the_same_account_scope(installed, account):
    import secrets

    from msg.core.codec import unb64

    app, root = installed
    _, _, buyer_key, buyer, listing, _ = await market(app, root)
    bought = await call(
        app, 'orders.buy', _intent(listing), key=buyer_key, subject=buyer, contract_version=3
    )
    assert bought.status == 'ok', wire(bought)
    ceiling = grant_for(
        app.registry.capability('orders.basic'),
        scope=Scope(resource_id=buyer if account else 't_store', descendants=not account),
        operations=('orders.get@1',),
    )
    issued = await call(
        app,
        'identity.token_create',
        {
            'nonce': b64(secrets.token_bytes(32)),
            'recovery_secret': b64(secrets.token_bytes(32)),
            'ceiling': wire((ceiling,)),
            'ttl': 300,
        },
        key=buyer_key,
        subject=buyer,
        contract_version=2,
    )
    assert issued.status == 'ok', wire(issued)
    token = (issued.data['credential_id'], unb64(issued.data['token']))
    before = await database_snapshot(app)
    result = await call(
        app, 'orders.get', {'order_id': bought.data['order']['id']}, subject=buyer, token=token
    )
    if account:
        assert result.status == 'ok' and result.data['order']['id'] == bought.data['order']['id']
    else:
        assert result.status == 'error' and result.error.code == 'credential_ceiling', wire(result)
    assert await database_snapshot(app) == before


@pytest.mark.asyncio
async def test_same_signer_scope_contraction_cannot_replay_previous_payment(installed):
    from dataclasses import replace

    app, root = installed
    _, _, buyer_key, buyer, listing, _ = await market(app, root)
    created = await call(app, 'orders.create', _intent(listing), key=buyer_key, subject=buyer)
    assert created.status == 'ok', wire(created)
    order = created.data['order']
    args = {
        'order_id': order['id'],
        'order_digest': order['order_digest'],
        'total_price_minor': order['total_price_minor'],
        'currency_id': 'primary',
    }
    key = await restricted_key(app, buyer_key, buyer, 'orders.fund', account=True)
    paid = await call(app, 'orders.fund', args, key=key, subject=buyer, rid='live-scope-fund')
    assert paid.status == 'ok', wire(paid)
    repeat = await call(app, 'orders.fund', args, key=key, subject=buyer, rid='live-scope-fund')
    assert repeat.status == 'ok' and repeat.replayed and repeat.data == paid.data
    # Preserve signer/request identity while changing the real persisted ceiling.
    # A cached result must not conceal this simulated current-authority update.
    async with app.metadata.transaction(write=True) as tx:
        credential = await tx.credential(key.key_id)
        narrower = replace(
            credential,
            ceiling=tuple(
                replace(g, scope=Scope(resource_id='t_store', descendants=True))
                for g in credential.ceiling
            ),
        )
        tx.execute(
            'UPDATE credentials SET body=? WHERE id=?',
            (canonical(narrower).decode(), credential.id),
            write=True,
        )
    before = await database_snapshot(app)
    denied = await call(app, 'orders.fund', args, key=key, subject=buyer, rid='live-scope-fund')
    assert denied.status == 'error' and denied.error.code == 'credential_ceiling', wire(denied)
    assert await database_snapshot(app) == before
