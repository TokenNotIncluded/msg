"""The signed decision must commit to one immutable, case-private explanation."""

import pytest
from test_market_arbitration import configured, details, vote
from test_service import call

from msg.core.codec import b64, canonical, digest, unb64, wire
from msg.plugins.money import _balance


async def unchanged_money(app, order, buyer, seller):
    async with app.metadata.transaction(write=False) as tx:
        account = tx.one('SELECT escrow_subject FROM store_orders WHERE id=?', (order['id'],))[0]
        assert _balance(tx, account) == 5_000_000
        assert _balance(tx, buyer) == 15_000_000 and _balance(tx, seller) == 0
        assert tx.one('SELECT COUNT(*) FROM order_settlements')[0] == 0


async def source_file(app, key, subject, raw, *, media='text/plain'):
    from uuid import uuid4

    async with app.metadata.transaction(write=False) as tx:
        parent = tx.one("SELECT id FROM resources WHERE parent=? AND name='files'", (subject,))[0]
    result = await call(
        app,
        'content.file_put',
        {
            'parent': parent,
            'name': 'reason-' + uuid4().hex + '.txt',
            'data': b64(raw),
            'media_type': media,
        },
        key=key,
        subject=subject,
    )
    assert result.status == 'ok', wire(result)
    return result


async def bound(app, members, opened, raw=b'Only the specified work was delivered.'):
    member = opened['panel'][0]
    source = await source_file(app, members[member], member, raw)
    args = {'case_id': opened['case_id'], 'ref': wire(source.resources[0])}
    result = await call(app, 'orders.dispute_rationale', args, key=members[member], subject=member)
    assert result.status == 'ok', wire(result)
    base = dict((await details(app, members[member], member, opened['case_id']))['proposal_base'])
    base.update({k: result.data[k] for k in ('rationale_ref', 'rationale_digest')})
    return source, result.data, base


@pytest.mark.asyncio
async def test_signed_allocation_without_rationale_is_rejected(installed):
    app, _root, members, _sk, seller, bk, buyer, order, opened = await configured(
        installed, panel_size=1, count=1
    )
    member = opened['panel'][0]
    base = (await details(app, bk, buyer, opened['case_id']))['proposal_base']
    result = await vote(app, members[member], member, base)
    assert result.status == 'error', (
        'An allocation without an immutable rationale must never form a decision'
    )
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM arbitration_votes')[0] == 0
        assert tx.one('SELECT COUNT(*) FROM arbitration_decisions')[0] == 0
    await unchanged_money(app, order, buyer, seller)


@pytest.mark.asyncio
async def test_rationale_is_private_pinned_and_survives_source_edit(installed):
    app, _root, members, sk, seller, bk, buyer, _order, opened = await configured(
        installed, panel_size=1, count=2
    )
    original = b'Only the specified work was delivered.'
    source, binding, base = await bound(app, members, opened, original)
    cid, member = opened['case_id'], opened['panel'][0]
    # Repeated explicit deposits do not duplicate evidence or alter the pinned fact.
    repeated = await call(
        app,
        'orders.dispute_rationale',
        {'case_id': cid, 'ref': wire(source.resources[0])},
        key=members[member],
        subject=member,
    )
    assert repeated.status == 'ok' and repeated.data == binding, wire(repeated)
    changed = await call(
        app,
        'content.text_patch',
        {
            'id': source.resources[0].id,
            'base_revision': source.resources[0].revision,
            'exact': original.decode(),
            'replacement': 'A later draft is not the signed rationale.',
        },
        key=members[member],
        subject=member,
        expected=((source.resources[0].id, source.data['generation']),),
    )
    assert changed.status == 'ok', wire(changed)
    args = {'case_id': cid, 'evidence_id': binding['evidence_id']}
    for key, subject in ((bk, buyer), (sk, seller)):
        view = await call(app, 'orders.dispute_evidence_get', args, key=key, subject=subject)
        assert view.status == 'ok' and unb64(view.data['data']) == original, wire(view)
        forbidden = await call(
            app, 'discovery.get', {'id': source.resources[0].id}, key=key, subject=subject
        )
        assert forbidden.status == 'error'  # The case does not share the author's file tree.
    outsider = next(uid for uid in members if uid != member)
    denied = await call(
        app, 'orders.dispute_evidence_get', args, key=members[outsider], subject=outsider
    )
    missing = await call(
        app,
        'orders.dispute_evidence_get',
        {'case_id': 'unknown', 'evidence_id': binding['evidence_id']},
        key=members[outsider],
        subject=outsider,
    )
    assert denied.error.code == missing.error.code == 'case_not_found'
    public = await call(app, 'orders.dispute_summary', {'case_id': cid})
    assert original.decode() not in str(wire(public))
    assert binding['rationale_digest'] not in str(wire(public))
    cast = await vote(app, members[member], member, base)
    assert cast.status == 'ok', wire(cast)
    decision = cast.data['decision']
    assert decision['rationale_ref'] == wire(source.resources[0])
    assert decision['rationale_digest'] == digest(original)
    executed = await call(
        app,
        'orders.dispute_execute',
        {'case_id': cid, 'decision_id': decision['id']},
        key=bk,
        subject=buyer,
    )
    assert executed.status == 'ok', wire(executed)


@pytest.mark.asyncio
async def test_valid_signature_cannot_be_reused_with_another_rationale(installed):
    app, _root, members, _sk, seller, _bk, buyer, order, opened = await configured(
        installed, panel_size=1, count=1
    )
    member = opened['panel'][0]
    _source, _binding, first = await bound(app, members, opened, b'First proposed explanation.')
    _source, _binding, second = await bound(app, members, opened, b'A different explanation.')
    original = {**first, 'refund_minor': 2_000_000, 'outcome': 'split'}
    altered = {**second, 'refund_minor': 2_000_000, 'outcome': 'split'}
    signature = members[member].sign(canonical(original), purpose='arbitration-decision')
    result = await call(
        app,
        'orders.dispute_vote',
        {'proposal': altered, 'signature': wire(signature)},
        key=members[member],
        subject=member,
    )
    assert result.error.code == 'invalid_signature', wire(result)
    await unchanged_money(app, order, buyer, seller)


@pytest.mark.asyncio
async def test_rationale_from_another_case_does_not_authorize_a_vote(installed):
    app, _root, members, _sk, seller, bk, buyer, order, opened = await configured(
        installed, panel_size=1, count=1
    )
    member = opened['panel'][0]
    _source, _binding, first = await bound(app, members, opened)
    # The frozen listing snapshot supplies the same advertised checkout terms.
    async with app.metadata.transaction(write=False) as tx:
        from msg.market.policy import contract

        locked = contract(tx, order['id'])
    purchased = await call(
        app,
        'orders.buy',
        {
            'listing_id': order['listing_id'],
            'listing_revision': order['listing_revision'],
            'quantity': 1,
            'currency_id': 'primary',
            'total_price_minor': locked['total_price_minor'],
        },
        key=bk,
        subject=buyer,
        contract_version=3,
    )
    assert purchased.status == 'ok', wire(purchased)
    other = await call(
        app,
        'orders.dispute_open',
        {'order_id': purchased.data['order']['id'], 'reason': 'quality'},
        key=bk,
        subject=buyer,
    )
    assert other.status == 'ok', wire(other)
    base = dict((await details(app, bk, buyer, other.data['case_id']))['proposal_base'])
    base.update({k: first[k] for k in ('rationale_ref', 'rationale_digest')})
    result = await vote(app, members[member], member, base)
    assert result.error.code == 'decision_rationale_missing', wire(result)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM arbitration_votes')[0] == 0
        assert _balance(tx, seller) == 0 and _balance(tx, buyer) == 10_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['missing_bytes', 'wrong_digest'])
async def test_unavailable_or_mismatched_reason_cannot_release_escrow(installed, mutation):
    app, _root, members, _sk, seller, bk, buyer, order, opened = await configured(
        installed, panel_size=1, count=1
    )
    member = opened['panel'][0]
    _source, binding, base = await bound(app, members, opened)
    if mutation == 'wrong_digest':
        base['rationale_digest'] = digest(b'Not the deposited reason.')
        rejected = await vote(app, members[member], member, base)
        assert rejected.error.code == 'decision_rationale_mismatch', wire(rejected)
    else:
        cast = await vote(app, members[member], member, base)
        assert cast.status == 'ok', wire(cast)
        # Exercise actual referenced-content loss, not a pretend worker exception.
        (app.contents.index / binding['rationale_digest'][7:]).unlink()
        rejected = await call(
            app,
            'orders.dispute_execute',
            {'case_id': opened['case_id'], 'decision_id': cast.data['decision']['id']},
            key=bk,
            subject=buyer,
        )
        assert rejected.error.code == 'decision_rationale_missing', wire(rejected)
        from msg.market.arbitration import resolve_cases

        assert await resolve_cases(app) == [opened['case_id']]
        async with app.metadata.transaction(write=False) as tx:
            assert (
                tx.one('SELECT state FROM arbitration_cases WHERE id=?', (opened['case_id'],))[0]
                == 'held'
            )
    await unchanged_money(app, order, buyer, seller)


@pytest.mark.asyncio
async def test_only_current_panel_can_deposit_owned_nonempty_text(installed):
    app, _root, members, _sk, _seller, bk, buyer, _order, opened = await configured(
        installed, panel_size=1, count=1
    )
    member, cid = opened['panel'][0], opened['case_id']
    buyer_file = await source_file(app, bk, buyer, b'A party is not the deciding panel.')
    args = {'case_id': cid, 'ref': wire(buyer_file.resources[0])}
    party = await call(app, 'orders.dispute_rationale', args, key=bk, subject=buyer)
    assert party.error.code == 'arbitrator_required', wire(party)
    stolen = await call(app, 'orders.dispute_rationale', args, key=members[member], subject=member)
    assert stolen.error.code == 'rationale_not_owned', wire(stolen)
    for raw, media in (
        (b'   \n', 'text/plain'),
        (b'\xff', 'text/plain'),
        (b'explanation', 'application/octet-stream'),
    ):
        source = await source_file(app, members[member], member, raw, media=media)
        invalid = await call(
            app,
            'orders.dispute_rationale',
            {'case_id': cid, 'ref': wire(source.resources[0])},
            key=members[member],
            subject=member,
        )
        assert invalid.error.code == 'rationale_text_required', wire(invalid)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM arbitration_evidence')[0] == 0
