"""Money's fixed policy and editable labels stay separate from ledger facts."""

from dataclasses import replace

import pytest
from test_service import NOW, call

from msg.admin.diagnostics import doctor, selftest
from msg.config import MoneyConfig, load_settings, write_example
from msg.core.errors import Failure


def _config(tmp_path):
    directory = tmp_path / 'etc'
    settings = write_example(directory, tmp_path / 'data')
    return settings, directory / 'msgd.toml'


def test_money_example_has_deterministic_defaults(tmp_path):
    settings, path = _config(tmp_path)
    assert settings.money == MoneyConfig()
    content = path.read_text()
    assert '[money]' in content
    for line in (
        'enabled = true',
        'currency_id = "primary"',
        'display_name = "MSG"',
        'code = "MSG"',
        'scale = 6',
        'transfer_fee = 0',
        'allow_overdraft = false',
    ):
        assert line in content


def test_only_currency_labels_can_change(tmp_path):
    _, path = _config(tmp_path)
    path.write_text(
        path
        .read_text()
        .replace('display_name = "MSG"', 'display_name = "Community credits"')
        .replace('code = "MSG"', 'code = "CREDIT"')
    )
    money = load_settings(path.parent).money
    assert money.display_name == 'Community credits' and money.code == 'CREDIT'
    assert money.currency_id == 'primary' and money.scale == 6
    assert money.transfer_fee == 0 and money.allow_overdraft is False


@pytest.mark.parametrize(
    ('before', 'after', 'code'),
    [
        ('enabled = true', 'enabled = false', 'invalid_money_configuration'),
        ('currency_id = "primary"', 'currency_id = "secondary"', 'invalid_money_configuration'),
        ('scale = 6', 'scale = 2', 'invalid_money_configuration'),
        ('transfer_fee = 0', 'transfer_fee = 1', 'invalid_money_configuration'),
        ('allow_overdraft = false', 'allow_overdraft = true', 'invalid_money_configuration'),
        ('display_name = "MSG"', 'display_name = ""', 'invalid_money_display_name'),
        ('code = "MSG"', 'code = "msg"', 'invalid_money_code'),
        ('code = "MSG"', 'code = "MSG"\nBankRole = "alice"', 'unknown_money_configuration'),
        ('code = "MSG"', 'code = "MSG"\ntotal_supply = 100', 'unknown_money_configuration'),
        ('code = "MSG"', 'code = "MSG"\nServerOffer = "sale"', 'unknown_money_configuration'),
    ],
)
def test_money_rejects_policy_changes_and_business_facts(tmp_path, before, after, code):
    _, path = _config(tmp_path)
    path.write_text(path.read_text().replace(before, after))
    with pytest.raises(Failure) as error:
        load_settings(path.parent)
    assert error.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize('inspection_failure', [None, 'money_receipt_mismatch'])
async def test_doctor_reports_money_config_without_claiming_market_e2e(
    installed, monkeypatch, inspection_failure
):
    app, _ = installed
    before = app.settings.money
    tables = (
        'money_ledger',
        'money_bank_roles',
        'server_offers',
        'money_purchases',
        'resource_entitlements',
        'bounty_listings',
        'store_orders',
        'store_deliveries',
        'store_order_events',
        'identities',
        'events',
        'audit',
        'results',
    )

    async def snapshot():
        async with app.metadata.transaction(write=False) as tx:
            return {table: tx.rows(f'SELECT * FROM {table} ORDER BY 1') for table in tables}

    original = await snapshot()
    from msg.admin import market_check

    async def unexpected_selftest(*args, **kwargs):
        raise AssertionError('doctor must not execute a market transaction selftest')

    monkeypatch.setattr(market_check, 'check_market_e2e', unexpected_selftest)
    if inspection_failure is not None:

        def reject_inspection(*args):
            raise Failure(inspection_failure)

        monkeypatch.setattr(market_check, 'inspect_clearing', reject_inspection)
    report = doctor(app.settings.config_dir, clock=lambda: NOW)
    assert report['checks']['money_config'] == {
        'ok': True,
        'enabled': True,
        'currency_id': 'primary',
        'display_name': 'MSG',
        'code': 'MSG',
        'scale': 6,
        'transfer_fee': 0,
        'allow_overdraft': False,
    }
    assert load_settings(app.settings.config_dir).money == before
    # Doctor verifies existing ledger facts; only the disposable selftest may
    # execute mint -> bank funding -> bounty -> purchase. A configuration-only
    # pass must never hide a failed clearing inspection.
    assert 'market_e2e' not in report['checks']
    clearing = report['checks']['market_clearing']
    expected = 'fail' if inspection_failure else 'pass'
    for feature in ('money', 'bounty', 'orders'):
        assert report['features'][feature] == {'status': expected, 'check': 'market_clearing'}
    if inspection_failure is not None:
        assert clearing == {'ok': False, 'code': inspection_failure}
    else:
        assert clearing['ok'] and clearing['conserved'] and clearing['signed_receipts_verified']
        assert (
            clearing['total_supply_minor'] == clearing['banks'] == clearing['enabled_offers'] == 0
        )
        assert clearing['ledger_entries'] == 0
    assert await snapshot() == original


@pytest.mark.asyncio
async def test_public_money_state_uses_configured_labels(installed):
    app, _ = installed
    app.settings = replace(
        app.settings,
        money=replace(app.settings.money, display_name='Community credits', code='CREDIT'),
    )
    result = await call(app, 'money.state', {})
    assert result.status == 'ok'
    assert result.data['currency_id'] == 'primary'
    assert result.data['display_name'] == 'Community credits'
    assert result.data['code'] == 'CREDIT'
    assert result.data['scale'] == 6


@pytest.mark.asyncio
async def test_doctor_rejects_missing_market_schema_without_repairing_it(installed):
    app, _ = installed
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('DROP TABLE store_order_events')
    report = doctor(app.settings.config_dir, clock=lambda: NOW)
    assert not report['ok'] and report['checks']['money_config']['ok']
    assert report['checks']['market_clearing'] == {'ok': False, 'code': 'market_schema_missing'}
    for feature in ('money', 'bounty', 'orders'):
        assert report['features'][feature] == {'status': 'fail', 'check': 'market_clearing'}
    assert 'market_e2e' not in report['checks']
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT to_regclass('store_order_events')")[0] is None
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0] == 0


@pytest.mark.asyncio
async def test_market_selftest_features_require_the_actual_isolated_flow():
    result = await selftest()
    assert result['ok'] and result['cleaned_up'], result
    assert result['checks']['market_e2e'] is True
    for feature in ('money', 'bounty', 'orders'):
        assert result['features'][feature] == {'status': 'pass', 'check': 'market_e2e'}
