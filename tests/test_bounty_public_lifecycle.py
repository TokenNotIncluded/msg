"""Public discovery locates immutable terms; market reads report live accounting."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from read_only_evidence import business_snapshot, readonly_evidence
from test_market_72 import fixture, proof
from test_market_cli_bindings import ExecutorClient, command
from test_service import NOW, call

from msg.application import Application
from msg.core.codec import canonical, wire


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'transition,state,reason,budget,balance,claims',
    [
        ('underfunded', 'paused', 'out_of_budget', 5, 5, 0),
        ('top_up', 'active', None, 20, 20, 0),
        ('pause', 'paused', 'publisher_paused', 20, 20, 0),
        ('exhausted', 'paused', 'claims_exhausted', 20, 10, 1),
        ('expire', 'paused', 'expired', 20, 20, 0),
        ('close', 'closed', None, 20, 0, 0),
    ],
)
async def test_search_and_real_cli_preserve_terms_and_current_accounting(
    installed, monkeypatch, transition, state, reason, budget, balance, claims
):
    app, root = installed
    changes = {'budget_minor': 5} if transition in {'underfunded', 'top_up'} else {}
    if transition == 'expire':
        changes['expires_at'] = wire(NOW + timedelta(seconds=1))
    (key, bank), identity, created = await fixture(app, root, **changes)
    lid = created.data['bounty']['listing_id']
    ref = created.resources[0]
    original = dict(created.data['bounty'])
    async with app.metadata.transaction(write=False) as tx:
        revision_before = wire(await tx.revision(ref))
        content_before = await app.contents.read_bytes((await tx.revision(ref)).content)
    publisher = ExecutorClient(app, key, bank)
    if transition == 'top_up':
        result = await command(
            publisher,
            'bounty',
            'top-up',
            canonical({'listing_id': lid, 'amount_minor': 15}).decode(),
        )
        assert result.status == 'ok', wire(result)
    elif transition in {'pause', 'close'}:
        result = await command(publisher, 'bounty', transition, lid)
        assert result.status == 'ok', wire(result)
    elif transition == 'exhausted':
        claimant_key, claimant = identity
        result = await call(
            app,
            'bounty.claim',
            await proof(app, identity, lid),
            key=claimant_key,
            subject=claimant,
        )
        assert result.status == 'ok', wire(result)
    elif transition == 'expire':
        app.clock = lambda: NOW + timedelta(seconds=1)
        await app.load()

    anonymous = ExecutorClient(app, None, None)
    async with readonly_evidence(app, monkeypatch):
        for operation, parameters in (
            ('discovery.search', {'query': 'pop', 'type': 'listing'}),
            ('discovery.lexical_search', {'scope': '/store', 'terms': 'pop', 'field': 'name'}),
        ):
            found = await call(app, operation, parameters)
            assert found.status == 'ok', wire(found)
            assert [item['id'] for item in found.data['items']] == [lid]
            # Resource.state describes publication, not Bounty accounting.
            # Search returns a reference, never fabricated budget/award facts.
            assert found.data['items'][0]['revision'] == ref.revision
            assert 'budget_minor' not in found.data['items'][0]
        bounty = await command(anonymous, 'bounty', 'get', lid)
        catalog = await command(anonymous, 'store', 'get', lid)
        assert bounty.status == catalog.status == 'ok', (wire(bounty), wire(catalog))
        for shown in (bounty.data['bounty'], catalog.data['listing']):
            assert (shown['state'], shown['pause_reason']) == (state, reason)
            assert shown['budget_minor'] == budget
            assert shown['escrow_balance_minor'] == balance
            assert shown['paid_claims'] == claims
        history = await call(app, 'store.listing_get', {'id': lid, 'revision': ref.revision})
        assert history.status == 'ok', wire(history)
        shown = history.data['listing']
        assert shown['state'] == original['state']
        assert shown['budget_minor'] == original['budget_minor']
        assert shown['current_state'] == state and shown['current_budget_minor'] == budget
        assert shown['terms_revision'] == ref.revision
        async with app.metadata.transaction(write=False) as tx:
            revision = await tx.revision(ref)
            assert wire(revision) == revision_before
            assert await app.contents.read_bytes(revision.content) == content_before


@pytest.mark.asyncio
async def test_real_cli_prove_replays_after_restart_without_second_nonce_or_reward(installed):
    app, root = installed
    _, (key, claimant), created = await fixture(app, root)
    lid = created.data['bounty']['listing_id']
    client = ExecutorClient(app, key, claimant)
    client.state = SimpleNamespace(signer=key, subject=claimant)
    argv = ('bounty', 'prove', lid, '--request-id', 'cli-pop-restart')
    paid = await command(client, *argv)
    assert paid.status == 'ok' and not paid.replayed, wire(paid)
    before = await business_snapshot(app)
    restarted = Application(app.settings, clock=lambda: NOW)
    await restarted.load()
    try:
        client = ExecutorClient(restarted, key, claimant)
        client.state = SimpleNamespace(signer=key, subject=claimant)
        replay = await command(client, *argv)
        assert replay.status == 'ok' and replay.replayed, wire(replay)
        assert replay.data == paid.data and replay.receipt == paid.receipt
        assert await business_snapshot(restarted) == before
        async with restarted.metadata.transaction(write=False) as tx:
            assert tx.one('SELECT COUNT(*) FROM bounty_challenges')[0] == 1
            assert tx.one('SELECT COUNT(*) FROM bounty_claims')[0] == 1
    finally:
        await restarted.close()
