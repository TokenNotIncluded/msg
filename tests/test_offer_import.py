"""Explicit local offer adoption preserves signed financial history byte-for-byte."""

import asyncio
import shutil

import pytest
from test_market_redemption import setup_offer
from test_service import call

from msg.admin.money import MoneyAdmin
from msg.admin.offer_import import apply_import, preview_import
from msg.admin.root import root_envelope
from msg.core.codec import digest
from msg.core.errors import Failure
from msg.core.models import ResourceRef
from msg.market.offer_resources import authoritative_offer


async def legacy(app, root):
    key, owner, args, _ = await setup_offer(app, root)
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            """INSERT INTO server_offers SELECT 'old-offer',resource_kind,unit,price_minor,
            min_quantity,max_quantity,entitlement_kind,duration_seconds,enabled,'old-price'
            FROM server_offers WHERE offer_id='web-bytes' """,
            write=True,
        )
    return key, owner, {**args, 'offer_id': 'old-offer', 'price_revision': 'old-price'}


async def snapshot(app):
    async with app.metadata.transaction(write=False) as tx:
        return {
            table: tx.rows('SELECT * FROM ' + table + ' ORDER BY 1')
            for table in (
                'server_offers',
                'money_purchases',
                'resource_entitlements',
                'money_ledger',
                'ledger_accounts',
            )
        }


@pytest.mark.asyncio
async def test_import_preserves_offer_quote_pending_settled_and_receipt_bytes(installed):
    app, root = installed
    key, owner, args = await legacy(app, root)
    pending = await call(
        app, 'money.redeem', {**args, 'defer': True}, key=key, subject=owner, contract_version=2
    )
    settled = await call(app, 'money.redeem', args, key=key, subject=owner)
    assert pending.status == settled.status == 'ok'
    before = await snapshot(app)
    async with app.metadata.transaction(write=False) as tx:
        plan = await preview_import(app, tx, 'old-offer')
    assert plan['purchase_count'] == 2 and plan['entitlement_count'] == 1
    assert await snapshot(app) == before
    result = await apply_import(app, root, plan, operator='test')
    assert result['approved_digest'] == digest(plan)
    assert await snapshot(app) == before
    async with app.metadata.transaction(write=False) as tx:
        assert (await authoritative_offer(app, tx, 'old-offer'))['price_revision'] == 'old-price'
        revision = await tx.revision(ResourceRef(id='old-offer'))
        assert revision.id == plan['revision_id'] and revision.id != 'old-price'
        assert revision.source_digest == digest(plan)
    finished = await call(
        app,
        'money.purchase_settle',
        {'purchase_id': pending.data['purchase']['id']},
        key=key,
        subject=owner,
    )
    assert (
        finished.status == 'ok'
        and finished.data['purchase']['offer_snapshot']['price_revision'] == 'old-price'
    )
    with pytest.raises(Failure, match='offer_already_resource'):
        await apply_import(app, root, plan, operator='test')


@pytest.mark.asyncio
async def test_import_approval_rechecks_pending_purchase_and_rolls_back_publish_failure(
    installed, monkeypatch
):
    app, root = installed
    key, owner, args = await legacy(app, root)
    pending = await call(
        app, 'money.redeem', {**args, 'defer': True}, key=key, subject=owner, contract_version=2
    )
    async with app.metadata.transaction(write=False) as tx:
        plan = await preview_import(app, tx, 'old-offer')
    await call(
        app,
        'money.purchase_cancel',
        {'purchase_id': pending.data['purchase']['id']},
        key=key,
        subject=owner,
    )
    with pytest.raises(Failure, match='offer_import_preview_stale'):
        await apply_import(app, root, plan, operator='test')
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT 1 FROM resources WHERE id=?', ('old-offer',)) is None
        plan = await preview_import(app, tx, 'old-offer')
    before = await snapshot(app)
    original = app.contents.commit_revision

    async def fail(*args):
        await original(*args)
        raise Failure('injected_import_failure')

    monkeypatch.setattr(app.contents, 'commit_revision', fail)
    with pytest.raises(Failure, match='injected_import_failure'):
        await apply_import(app, root, plan, operator='test')
    assert await snapshot(app) == before
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one('SELECT 1 FROM server_offer_resources WHERE offer_id=?', ('old-offer',)) is None
        )
        assert tx.one('SELECT 1 FROM revisions WHERE id=?', (plan['revision_id'],)) is None
    monkeypatch.setattr(app.contents, 'commit_revision', original)
    assert (await apply_import(app, root, plan, operator='test'))['offer_id'] == 'old-offer'


@pytest.mark.asyncio
async def test_console_import_dry_run_needs_no_pin_and_real_import_requires_full_approval(
    installed, installation_seed, monkeypatch
):
    app, root = installed
    await legacy(app, root)
    from msg.admin import money

    monkeypatch.setattr(money, 'require_local_console', lambda _: 'test-console')
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('dry-run asked approval'))
    monkeypatch.setattr(money.getpass, 'getpass', lambda _: pytest.fail('dry-run asked PIN'))
    before = await snapshot(app)
    dry = await asyncio.to_thread(
        MoneyAdmin(app.settings.config_dir).execute_offer,
        'import',
        offer_id='old-offer',
        dry_run=True,
    )
    assert dry['approval_digest'] == digest(dry['plan'])
    assert await snapshot(app) == before
    monkeypatch.setattr('builtins.input', lambda _: 'CONFIRM MONEY')
    with pytest.raises(Failure, match='approval_cancelled'):
        await asyncio.to_thread(
            MoneyAdmin(app.settings.config_dir).execute_offer, 'import', offer_id='old-offer'
        )
    seed, _, _, _ = installation_seed
    shutil.copytree(
        root_envelope(seed / 'etc').parent, root_envelope(app.settings.config_dir).parent
    )
    monkeypatch.setattr(
        'builtins.input', lambda prompt: prompt.removeprefix('Type ').removesuffix(' to continue: ')
    )
    monkeypatch.setattr(money.getpass, 'getpass', lambda _: 'correct-horse-test-passphrase')
    result = await asyncio.to_thread(
        MoneyAdmin(app.settings.config_dir).execute_offer, 'import', offer_id='old-offer'
    )
    assert result['offer_id'] == 'old-offer' and await snapshot(app) == before


@pytest.mark.asyncio
async def test_import_rejects_inconsistent_entitlement_before_publication(installed):
    app, root = installed
    key, owner, args = await legacy(app, root)
    result = await call(app, 'money.redeem', args, key=key, subject=owner)
    assert result.status == 'ok'
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            'UPDATE resource_entitlements SET quantity=quantity+1 WHERE offer_id=?',
            ('old-offer',),
            write=True,
        )
    async with app.metadata.transaction(write=False) as tx:
        with pytest.raises(Failure, match='offer_import_inconsistent_entitlements'):
            await preview_import(app, tx, 'old-offer')
        assert tx.one('SELECT 1 FROM resources WHERE id=?', ('old-offer',)) is None


def test_daemon_import_parser_dispatches_dry_run(monkeypatch):
    from msg.daemon import main

    calls = []

    def execute(self, action, **kwargs):
        calls.append((action, kwargs))
        return {'ok': True}

    monkeypatch.setattr(MoneyAdmin, 'execute_offer', execute)
    assert main(['money', 'offer', 'import', 'old-offer', '--dry-run']) == 0
    assert calls == [('import', {'offer_id': 'old-offer', 'fields': None, 'dry_run': True})]
