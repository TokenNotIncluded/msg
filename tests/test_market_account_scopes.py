"""A store-scoped credential cannot use its owner's private market account.

All requests use real registered Ed25519 keys and the normal executor against
PostgreSQL. Denied calls must preserve every authoritative row, not just balance.
"""
from dataclasses import replace

import pytest
from read_only_evidence import business_snapshot
from test_market_lifecycle import market
from test_orders import _intent
from test_service import call

from msg.core.codec import b64, canonical, wire
from msg.core.models import Scope
from msg.security.capabilities import grant_for
from msg.security.crypto import Ed25519Signer


async def account_key(app, owner_key, owner, *, include_account=False):
    signer = Ed25519Signer.generate()
    scopes = [Scope(resource_id='t_store', descendants=True)]
    if include_account:
        scopes.append(Scope(resource_id=owner))
    grants = [grant_for(app.registry.capability(family), scope=scope)
              for scope in scopes for family in ('orders.basic', 'delivery.basic')]
    public = b64(signer.public_key)
    added = await call(app, 'identity.key_add', {
        'public_key': public,
        'possession_proof': wire(signer.sign(canonical({
            'subject_id': owner, 'public_key': public}), purpose='key-add')),
        'ceiling': wire(grants)}, key=owner_key, subject=owner)
    assert added.status == 'ok', wire(added)
    return signer


def assert_ceiling(result):
    assert result.status == 'error', wire(result)
    assert result.error.code == 'credential_ceiling', wire(result)
    assert not result.replayed and result.data is None
    assert not result.resources and result.receipt is None


@pytest.mark.asyncio
@pytest.mark.parametrize('version', [1, 2, 3])
async def test_store_key_cannot_buy_or_replay_an_owner_purchase(installed, version):
    app, root = installed
    _seller_key, _seller, buyer_key, buyer, listing, package = await market(app, root)
    limited = await account_key(app, buyer_key, buyer)
    permitted = await account_key(app, buyer_key, buyer, include_account=True)
    args = _intent(listing)
    if version == 2:
        args['package_digest'] = package['digest']
    request_id = 'private-buy-' + str(version)
    before = await business_snapshot(app)
    denied = await call(app, 'orders.buy', args, key=limited, subject=buyer,
                        rid=request_id, contract_version=version)
    assert_ceiling(denied)
    assert await business_snapshot(app) == before

    accepted = await call(app, 'orders.buy', args, key=permitted, subject=buyer,
                          rid=request_id, contract_version=version)
    assert accepted.status == 'ok', wire(accepted)
    before = await business_snapshot(app)
    denied = await call(app, 'orders.buy', args, key=limited, subject=buyer,
                        rid=request_id, contract_version=version)
    assert_ceiling(denied)
    assert await business_snapshot(app) == before
    replay = await call(app, 'orders.buy', args, key=permitted, subject=buyer,
                        rid=request_id, contract_version=version)
    assert replay.status == 'ok' and replay.replayed and replay.data == accepted.data
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_quote_and_fund_need_account_scope_before_mutation_and_replay(installed):
    app, root = installed
    _sk, _seller, buyer_key, buyer, listing, _package = await market(app, root)
    limited = await account_key(app, buyer_key, buyer)
    permitted = await account_key(app, buyer_key, buyer, include_account=True)
    args = _intent(listing)
    before = await business_snapshot(app)
    denied = await call(app, 'orders.create', args, key=limited, subject=buyer, rid='private-quote')
    assert_ceiling(denied)
    assert await business_snapshot(app) == before
    created = await call(app, 'orders.create', args, key=permitted, subject=buyer, rid='private-quote')
    assert created.status == 'ok', wire(created)
    order = created.data['order']
    params = {'order_id': order['id'], 'order_digest': order['order_digest'],
              'currency_id': 'primary', 'total_price_minor': order['total_price_minor']}
    before = await business_snapshot(app)
    assert_ceiling(await call(app, 'orders.create', args, key=limited, subject=buyer, rid='private-quote'))
    assert_ceiling(await call(app, 'orders.fund', params, key=limited, subject=buyer, rid='private-fund'))
    assert await business_snapshot(app) == before
    funded = await call(app, 'orders.fund', params, key=permitted, subject=buyer, rid='private-fund')
    assert funded.status == 'ok', wire(funded)
    before = await business_snapshot(app)
    assert_ceiling(await call(app, 'orders.fund', params, key=limited, subject=buyer, rid='private-fund'))
    replay = await call(app, 'orders.fund', params, key=permitted, subject=buyer, rid='private-fund')
    assert replay.status == 'ok' and replay.replayed and replay.data == funded.data
    assert await business_snapshot(app) == before


READS = [
    ('orders.get', 1), ('orders.list', 1), ('orders.payment', 1), ('orders.contract', 1),
    ('delivery.get', 1), ('delivery.get', 2), ('delivery.part_get', 1),
]


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,version', READS)
async def test_private_order_and_delivery_views_require_account_scope(installed, operation, version):
    app, root = installed
    _sk, _seller, buyer_key, buyer, listing, _package = await market(app, root)
    limited = await account_key(app, buyer_key, buyer)
    permitted = await account_key(app, buyer_key, buyer, include_account=True)
    bought = await call(app, 'orders.buy', _intent(listing), key=buyer_key, subject=buyer,
                        contract_version=3)
    assert bought.status == 'ok', wire(bought)
    order_id = bought.data['order']['id']
    args = {} if operation == 'orders.list' else {'order_id': order_id}
    if operation == 'delivery.part_get':
        read = await call(app, 'delivery.get', {'order_id': order_id}, key=buyer_key,
                          subject=buyer, contract_version=2)
        args.update(delivery_digest=read.data['delivery']['delivery_digest'],
                    payload_index=0, offset=0, length=8)
    before = await business_snapshot(app)
    assert_ceiling(await call(app, operation, args, key=limited, subject=buyer, contract_version=version))
    assert await business_snapshot(app) == before
    allowed = await call(app, operation, args, key=permitted, subject=buyer, contract_version=version)
    assert allowed.status == 'ok', wire(allowed)
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,version', [('delivery.accept', 2), ('delivery.claim', 1)])
async def test_claim_cannot_be_forged_by_a_store_scoped_key(installed, operation, version):
    app, root = installed
    if operation == 'delivery.claim':
        from test_managed_checkout import setup_sale
        app, _seller, (buyer_key, buyer), _package, purchase_args = await setup_sale(installed)
        purchase_version, read_version = 2, 1
    else:
        _sk, _seller, buyer_key, buyer, listing, _package = await market(app, root)
        purchase_args, purchase_version, read_version = _intent(listing), 3, 2
    limited = await account_key(app, buyer_key, buyer)
    permitted = await account_key(app, buyer_key, buyer, include_account=True)
    bought = await call(app, 'orders.buy', purchase_args, key=buyer_key, subject=buyer,
                        contract_version=purchase_version)
    assert bought.status == 'ok', wire(bought)
    order_id = bought.data['order']['id']
    read = await call(app, 'delivery.get', {'order_id': order_id}, key=buyer_key,
                      subject=buyer, contract_version=read_version)
    assert read.status == 'ok', wire(read)
    args = {'order_id': order_id, 'delivery_digest': read.data['delivery']['delivery_digest']}
    before = await business_snapshot(app)
    assert_ceiling(await call(app, operation, args, key=limited, subject=buyer,
                              contract_version=version, rid='private-claim'))
    assert await business_snapshot(app) == before
    accepted = await call(app, operation, args, key=permitted, subject=buyer,
                          contract_version=version, rid='private-claim')
    assert accepted.status == 'ok', wire(accepted)
    before = await business_snapshot(app)
    assert_ceiling(await call(app, operation, args, key=limited, subject=buyer,
                              contract_version=version, rid='private-claim'))
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_same_key_losing_account_scope_cannot_replay_its_own_cached_result(installed):
    app, root = installed
    _sk, _seller, buyer_key, buyer, listing, _package = await market(app, root)
    key = await account_key(app, buyer_key, buyer, include_account=True)
    args = _intent(listing)
    bought = await call(app, 'orders.buy', args, key=key, subject=buyer,
                        contract_version=3, rid='same-key-scope-change')
    assert bought.status == 'ok', wire(bought)
    # Deliberately change the current authority row to model a narrowed ceiling.
    # The signer, public key, request ID and signed business payload stay identical.
    async with app.metadata.transaction(write=True) as tx:
        credential = await tx.credential(key.key_id)
        narrowed = replace(credential, ceiling=tuple(grant for grant in credential.ceiling
                           if grant.scope.resource_id == 't_store'))
        tx.execute('UPDATE credentials SET body=? WHERE id=?',
                   (canonical(narrowed).decode(), key.key_id), write=True)
    before = await business_snapshot(app)
    assert_ceiling(await call(app, 'orders.buy', args, key=key, subject=buyer,
                              contract_version=3, rid='same-key-scope-change'))
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_all_private_market_registrations_share_the_pre_replay_requirement(installed):
    from msg.plugins.common import no_requirements
    from msg.plugins.money import account_requirements

    app, _root = installed
    covered = set()
    for spec in app.registry.operations():
        if spec.name.startswith(('orders.', 'delivery.')):
            public = spec.name == 'orders.dispute_summary'
            assert spec.requirements is (no_requirements if public else account_requirements), spec.name
            covered.add((spec.name, spec.version))
    assert {('orders.buy', 1), ('orders.buy', 2), ('orders.buy', 3),
            ('orders.fund', 1), ('delivery.get', 2), ('delivery.transfer_open', 1),
            ('orders.dispute_summary', 1), ('orders.dispute_vote', 1)} <= covered


@pytest.mark.asyncio
async def test_public_case_summary_does_not_gain_a_private_account_requirement(installed, monkeypatch):
    from read_only_evidence import readonly_evidence
    from test_market_arbitration import configured

    app, _root, _members, _sk, _seller, buyer_key, buyer, order, opened = await configured(installed)
    limited = await account_key(app, buyer_key, buyer)
    args = {'case_id': opened['case_id']}
    async with readonly_evidence(app, monkeypatch):
        public = await call(app, 'orders.dispute_summary', args)
        signed = await call(app, 'orders.dispute_summary', args, key=limited, subject=buyer)
        assert public.status == signed.status == 'ok'
        assert public.data == signed.data
        assert set(public.data) == {'case_id', 'state', 'policy_digest', 'outcome'}
        assert buyer not in canonical(public.data).decode()
        assert order['id'] not in canonical(public.data).decode()
        assert_ceiling(await call(app, 'orders.dispute_get', args, key=limited, subject=buyer))
        own = await call(app, 'orders.dispute_get', args, key=buyer_key, subject=buyer)
        assert own.status == 'ok'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['http', 'path_get', 'graphql', 'mcp_http'])
async def test_actual_read_adapters_recheck_account_scope_after_warming_cache(installed, monkeypatch, mode):
    from datetime import timedelta

    import httpx
    from read_only_evidence import readonly_evidence
    from test_service import NOW

    from msg.core.requests import request_for
    from msg.transports.client import TRANSPORTS
    from msg.transports.http import create_app

    app, root = installed
    _sk, _seller, buyer_key, buyer, listing, _package = await market(app, root)
    key = await account_key(app, buyer_key, buyer, include_account=True)
    bought = await call(app, 'orders.buy', _intent(listing), key=buyer_key, subject=buyer,
                        contract_version=3)
    assert bought.status == 'ok'
    args = {'order_id': bought.data['order']['id']}
    packets = [request_for(op, args, app.settings.service_url, signer=key, subject=buyer,
                           expires_at=NOW + timedelta(seconds=120))
               for op in ('orders.get', 'delivery.get')]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        adapter = TRANSPORTS[mode](app.settings.service_url, http=http)
        async with readonly_evidence(app, monkeypatch):
            for packet in packets:
                assert (await adapter.call(packet)).status == 'ok'
        async with app.metadata.transaction(write=True) as tx:
            credential = await tx.credential(key.key_id)
            limited = replace(credential, ceiling=tuple(grant for grant in credential.ceiling
                              if grant.scope.resource_id == 't_store'))
            tx.execute('UPDATE credentials SET body=? WHERE id=?',
                       (canonical(limited).decode(), key.key_id), write=True)
        async with readonly_evidence(app, monkeypatch):
            for packet in packets:
                assert_ceiling(await adapter.call(packet))
        await adapter.close()


@pytest.mark.asyncio
async def test_get_head_and_conditional_order_aliases_recheck_narrowed_scope(installed, monkeypatch):
    import httpx
    from read_only_evidence import readonly_evidence
    from test_order_paths import _headers

    from msg.transports.http import create_app

    app, root = installed
    _sk, _seller, buyer_key, buyer, listing, _package = await market(app, root)
    key = await account_key(app, buyer_key, buyer, include_account=True)
    bought = await call(app, 'orders.buy', _intent(listing), key=buyer_key, subject=buyer,
                        contract_version=3)
    assert bought.status == 'ok'
    order_id = bought.data['order']['id']
    queries = [(base + tail, operation) for base in
               (f'/@order-buyer/orders/{order_id}', f'/_orders/{order_id}')
               for tail, operation in (('', 'orders.get'), ('/_payment', 'orders.payment'),
                                        ('/_delivery', 'delivery.get'))]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        cached = []
        async with readonly_evidence(app, monkeypatch):
            for path, operation in queries:
                headers = _headers(app, operation, {'order_id': order_id}, key, buyer)
                response = await http.get(path, headers=headers)
                assert response.status_code == 200, response.text
                etag = response.headers['etag']
                cached.append((path, dict(headers, **{'If-None-Match': etag})))
        async with app.metadata.transaction(write=True) as tx:
            credential = await tx.credential(key.key_id)
            limited = replace(credential, ceiling=tuple(grant for grant in credential.ceiling
                              if grant.scope.resource_id == 't_store'))
            tx.execute('UPDATE credentials SET body=? WHERE id=?',
                       (canonical(limited).decode(), key.key_id), write=True)
        async with readonly_evidence(app, monkeypatch):
            for path, headers in cached:
                response = await http.get(path, headers=headers)
                assert response.status_code == 403, response.text
                assert response.json()['error']['code'] == 'credential_ceiling'
                assert 'etag' not in response.headers
                assert 'delivery-ok' not in response.text
                head = await http.head(path, headers=headers)
                assert head.status_code == 403 and not head.content
                assert 'etag' not in head.headers
