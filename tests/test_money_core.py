"""Money writes use signed executor requests and the real PostgreSQL ledger."""

import asyncio

import pytest
from test_service import NOW, call, register

from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, decode, wire
from msg.core.errors import Failure
from msg.core.models import Signature
from msg.plugins.money import CURRENCY_ID, MAX_MINOR, POLICY_DIGEST, POLICY_VERSION
from msg.security.crypto import verify


async def _fund_fixture(app, subject, amount):
    # An explicit fixture seed stands in for future physical-console minting.
    # Public network operations cannot invoke this path.
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('INSERT INTO money_accounts VALUES (?,?)', (subject, CURRENCY_ID), write=True)
        tx.execute(
            """INSERT INTO money_ledger
            (id,kind,currency_id,amount_minor,debit_account,credit_account,actor,request_id,
             reference,committed_at,policy_version,policy_digest,receipt)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                'lt_fixture_' + subject,
                'mint',
                CURRENCY_ID,
                amount,
                None,
                subject,
                ROOT_SUBJECT,
                'fixture_' + subject,
                None,
                wire(NOW),
                POLICY_VERSION,
                POLICY_DIGEST,
                '{}',
            ),
            write=True,
        )


@pytest.mark.asyncio
async def test_zero_supply_signed_transfer_and_private_views(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'money-alice')
    bob_key, bob, _ = await register(app, 'money-bob')
    zero = await call(app, 'money.state', {})
    assert zero.status == 'ok' and zero.data['total_supply_minor'] == 0
    assert not (await call(app, 'money.banks', {})).data['banks']
    assert (await call(app, 'money.balance', {})).error.code == 'money_subject_required'
    before = await call(app, 'money.balance', {}, key=alice_key, subject=alice)
    assert before.status == 'ok' and before.data['balance_minor'] == 0
    failed = await call(
        app,
        'money.transfer',
        {'to_subject': bob, 'currency_id': 'primary', 'amount_minor': 1},
        key=alice_key,
        subject=alice,
    )
    assert failed.error.code == 'insufficient_funds'
    await _fund_fixture(app, alice, 1_000_000)
    args = {'to_subject': bob, 'currency_id': 'primary', 'amount_minor': 400_000}
    sent = await call(app, 'money.transfer', args, key=alice_key, subject=alice, rid='money-once')
    assert sent.status == 'ok', wire(sent)
    assert sent.data['transfer']['body']['policy_version'] == POLICY_VERSION == 2
    assert sent.data['transfer']['body']['policy_digest'] == POLICY_DIGEST
    assert sent.data['transfer']['body']['ledger_sequence'] > 0
    verify(
        app.receipt_signer.public_key,
        canonical(sent.data['transfer']['body']),
        decode(Signature, sent.data['transfer']['signature']),
        purpose='money-receipt',
    )
    again = await call(app, 'money.transfer', args, key=alice_key, subject=alice, rid='money-once')
    assert again.status == 'ok' and again.replayed
    clash = await call(
        app,
        'money.transfer',
        {**args, 'amount_minor': 1},
        key=alice_key,
        subject=alice,
        rid='money-once',
    )
    assert clash.error.code == 'idempotency_conflict'
    assert (await call(app, 'money.balance', {}, key=alice_key, subject=alice)).data[
        'balance_minor'
    ] == 600_000
    assert (await call(app, 'money.balance', {}, key=bob_key, subject=bob)).data[
        'balance_minor'
    ] == 400_000
    assert (await call(app, 'money.state', {})).data['total_supply_minor'] == 1_000_000
    a = (await call(app, 'money.ledger', {}, key=alice_key, subject=alice)).data['items']
    b = (await call(app, 'money.ledger', {}, key=bob_key, subject=bob)).data['items']
    assert a[-1]['direction'] == 'out' and b[-1]['direction'] == 'in'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0] == 2


@pytest.mark.asyncio
async def test_double_spend_overflow_and_root_network_guard(installed):
    app, root_key = installed
    alice_key, alice, _ = await register(app, 'money-race-alice')
    _, bob, _ = await register(app, 'money-race-bob')
    _, carol, _ = await register(app, 'money-race-carol')
    await _fund_fixture(app, alice, 100)

    async def send(to, rid):
        return await call(
            app,
            'money.transfer',
            {'to_subject': to, 'currency_id': 'primary', 'amount_minor': 80},
            key=alice_key,
            subject=alice,
            rid=rid,
        )

    results = await asyncio.gather(send(bob, 'race-bob'), send(carol, 'race-carol'))
    assert sorted(r.status for r in results) == ['error', 'ok']
    assert next(r for r in results if r.status == 'error').error.code == 'insufficient_funds'
    assert (await call(app, 'money.balance', {}, key=alice_key, subject=alice)).data[
        'balance_minor'
    ] == 20
    root = await call(
        app,
        'money.transfer',
        {
            'from_subject': ROOT_SUBJECT,
            'to_subject': bob,
            'currency_id': 'primary',
            'amount_minor': 1,
        },
        key=alice_key,
        subject=alice,
    )
    assert root.error.code == 'root_local_only'
    root_alias = await call(
        app,
        'money.transfer',
        {'from_subject': '@root', 'to_subject': bob, 'currency_id': 'primary', 'amount_minor': 1},
        key=alice_key,
        subject=alice,
    )
    assert root_alias.error.code == 'root_local_only'
    root_signed = await call(
        app,
        'money.transfer',
        {'to_subject': bob, 'currency_id': 'primary', 'amount_minor': 1},
        key=root_key,
        subject=ROOT_SUBJECT,
    )
    assert root_signed.error.code == 'local_only'
    negative = await call(
        app,
        'money.transfer',
        {'to_subject': bob, 'currency_id': 'primary', 'amount_minor': -1},
        key=alice_key,
        subject=alice,
    )
    assert negative.error.code == 'schema_validation'
    huge = await call(
        app,
        'money.transfer',
        {'to_subject': bob, 'currency_id': 'primary', 'amount_minor': MAX_MINOR + 1},
        key=alice_key,
        subject=alice,
    )
    assert huge.error.code == 'schema_validation'


@pytest.mark.asyncio
async def test_ledger_cannot_be_mutated_and_reads_do_not_write(installed):
    app, _ = installed
    key, uid, _ = await register(app, 'money-immutable')
    await _fund_fixture(app, uid, 1)
    async with app.metadata.transaction(write=False) as tx:
        initial = (
            tx.one('SELECT COUNT(*) FROM events')[0],
            tx.one('SELECT COUNT(*) FROM money_ledger')[0],
            tx.one('SELECT COUNT(*) FROM money_accounts')[0],
        )
    await call(app, 'money.state', {})
    await call(app, 'money.balance', {}, key=key, subject=uid)
    await call(app, 'money.ledger', {}, key=key, subject=uid)
    async with app.metadata.transaction(write=False) as tx:
        assert initial == (
            tx.one('SELECT COUNT(*) FROM events')[0],
            tx.one('SELECT COUNT(*) FROM money_ledger')[0],
            tx.one('SELECT COUNT(*) FROM money_accounts')[0],
        )
    with pytest.raises(Failure, match='constraint_conflict'):
        async with app.metadata.transaction(write=True) as tx:
            tx.execute(
                'UPDATE money_ledger SET amount_minor=2 WHERE id=?',
                ('lt_fixture_' + uid,),
                write=True,
            )
