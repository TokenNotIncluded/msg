"""Publication is explicit, current-role based, and excludes private receipt data."""

from dataclasses import replace

import httpx
import pytest
from test_service import call, register

from msg.admin.accounts import archive_account, archive_preview
from msg.admin.money import apply_money
from msg.constants import ROOT_SUBJECT
from msg.core.codec import digest, wire
from msg.transports.http import create_app


async def bank_fixture(app, root):
    key, bank, _ = await register(app, 'public-bank')
    await apply_money(app, root, action='mint', operator='test', amount_minor=10_000_000_001)
    await apply_money(app, root, action='bank_add', operator='test', subject_id=bank)
    await apply_money(
        app,
        root,
        action='transfer',
        operator='test',
        subject_id=bank,
        amount_minor=10_000_000_001,
    )
    return key, bank


@pytest.mark.asyncio
async def test_public_bank_root_ledger_exact_pagination_and_private_fields(installed):
    app, root = installed
    key, bank = await bank_fixture(app, root)
    _, other, _ = await register(app, 'public-recipient')
    sent = await call(
        app,
        'money.transfer',
        {
            'to_subject': other,
            'currency_id': 'primary',
            'amount_minor': 1,
            'reference': 'secret private business information',
        },
        key=key,
        subject=bank,
    )
    assert sent.status == 'ok', wire(sent)
    for uid in (ROOT_SUBJECT, bank):
        balance = await call(app, 'money.public_balance', {'subject_id': uid})
        assert balance.status == 'ok', wire(balance)
        assert balance.data['scale'] == 6
    assert (await call(app, 'money.public_balance', {'subject_id': bank})).data[
        'balance_minor'
    ] == 10_000_000_000
    page = await call(app, 'money.public_ledger', {'subject_id': bank, 'limit': 1})
    assert page.status == 'ok', wire(page)
    assert len(page.data['items']) == 1 and page.data['next_cursor'] is not None
    following = await call(
        app,
        'money.public_ledger',
        {'subject_id': bank, 'limit': 1, 'cursor': page.data['next_cursor']},
    )
    assert following.data['next_cursor'] is None
    item = following.data['items'][0]
    assert item['amount_minor'] == 1 and item['from_account'] == bank
    assert item['to_account'] == other
    assert set(item) == {
        'ledger_sequence',
        'transaction_id',
        'kind',
        'amount_minor',
        'from_account',
        'to_account',
        'committed_at',
    }
    assert 'secret' not in str(following.data)
    # Old contracts remain owner-only and keep their original full projection.
    assert (await call(app, 'money.balance', {})).error.code == 'money_subject_required'
    private = await call(app, 'money.ledger', {}, key=key, subject=bank)
    assert private.data['items'][-1]['reference'] == 'secret private business information'
    assert 'receipt' in private.data['items'][-1]
    assert (
        await call(app, 'money.public_balance', {'subject_id': other})
    ).error.code == 'not_found'
    assert (await call(app, 'money.public_ledger', {'subject_id': other})).error.code == 'not_found'


@pytest.mark.asyncio
async def test_visibility_self_only_revoke_bank_and_archive(installed):
    app, root = installed
    key, bank = await bank_fixture(app, root)
    other_key, other, _ = await register(app, 'public-volunteer')
    denied = await call(
        app,
        'money.visibility_set',
        {'visibility': 'public'},
        key=other_key,
        subject=bank,
    )
    assert denied.status == 'error'
    assert (await call(app, 'money.visibility_set', {'visibility': 'public'})).status == 'error'
    opted = await call(
        app,
        'money.visibility_set',
        {'visibility': 'public'},
        key=other_key,
        subject=other,
    )
    assert opted.status == 'ok', wire(opted)
    assert (await call(app, 'money.public_balance', {'subject_id': other})).status == 'ok'
    assert not (await call(app, 'money.public_ledger', {'subject_id': other})).data['items']
    for target in ('t_main', 'missing-public-account'):
        for operation in ('money.public_balance', 'money.public_ledger'):
            result = await call(app, operation, {'subject_id': target})
            assert result.error.code == 'not_found'
    assert (
        await call(
            app,
            'money.visibility_set',
            {'visibility': 'private'},
            key=other_key,
            subject=other,
        )
    ).status == 'ok'
    assert (
        await call(app, 'money.public_balance', {'subject_id': other})
    ).error.code == 'not_found'
    bank_private = await call(
        app,
        'money.visibility_set',
        {'visibility': 'private'},
        key=key,
        subject=bank,
    )
    assert bank_private.data['effective_visibility'] == 'public'
    await apply_money(app, root, action='bank_remove', operator='test', subject_id=bank)
    assert (await call(app, 'money.public_balance', {'subject_id': bank})).error.code == 'not_found'
    await call(app, 'money.visibility_set', {'visibility': 'public'}, key=key, subject=bank)
    drained = await call(
        app,
        'money.transfer',
        {'to_subject': other, 'currency_id': 'primary', 'amount_minor': 10_000_000_001},
        key=key,
        subject=bank,
    )
    assert drained.status == 'ok', wire(drained)
    async with app.metadata.transaction(write=False) as tx:
        preview = await archive_preview(tx, bank)
    await archive_account(app, bank, root, expected_digest=digest(preview), operator='test')
    for op in ('money.public_balance', 'money.public_ledger'):
        assert (await call(app, op, {'subject_id': bank})).error.code == 'not_found'


@pytest.mark.asyncio
async def test_visibility_does_not_expand_existing_ceiling(installed):
    app, _ = installed
    key, uid, _ = await register(app, 'old-money-credential')
    async with app.metadata.transaction(write=True) as tx:
        credential = await tx.credential(key.key_id)
        subject = await tx.subject(uid)
        await tx.save_credential(
            replace(
                credential,
                ceiling=tuple(
                    replace(grant, operations=grant.operations - {'money.visibility_set@1'})
                    for grant in credential.ceiling
                ),
            ),
            subject.auth_version,
        )
    result = await call(
        app,
        'money.visibility_set',
        {'visibility': 'public'},
        key=key,
        subject=uid,
    )
    assert result.error.code == 'credential_ceiling'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT 1 FROM money_visibility WHERE subject_id=?', (uid,)) is None


@pytest.mark.asyncio
async def test_public_http_read_only_aliases_and_effect_fence(installed):
    app, root = installed
    _, bank = await bank_fixture(app, root)
    async with app.metadata.transaction(write=False) as tx:
        before = tuple(
            tx.one(f'SELECT COUNT(*) FROM {table}')[0]
            for table in ('money_ledger', 'money_visibility', 'events', 'audit')
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url=app.settings.service_url,
    ) as http:
        for path in (
            '/@root/public-balance',
            '/@root/public-ledger',
            '/@public-bank/public-balance',
            '/@public-bank/public-ledger?limit=1',
        ):
            response = await http.get(path)
            assert response.status_code == 200, response.text
            head = await http.head(path)
            assert head.status_code == 200 and head.content == b''
            assert head.headers['etag'] == response.headers['etag']
            assert (await http.post(path, json={})).status_code in {404, 405}
        assert (await http.get('/@public-bank/public-ledger?limit=101')).status_code != 200
        assert (await http.get('/@public-bank/public-ledger?limit=1&limit=2')).status_code != 200
        assert (await http.get('/@public-bank/public-balance?limit=1')).status_code != 200
        assert (await http.get('/@public-bank/public-balance')).json()['subject_id'] == bank
    async with app.metadata.transaction(write=False) as tx:
        assert before == tuple(
            tx.one(f'SELECT COUNT(*) FROM {table}')[0]
            for table in ('money_ledger', 'money_visibility', 'events', 'audit')
        )
