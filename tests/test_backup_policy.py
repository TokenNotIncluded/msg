"""Restore keeps business policy while rebinding storage and disabling effects."""
import tomllib

import pytest

from msg.admin.backups import backup, restore
from msg.application import Application
from msg.config import load_settings
from msg.core.codec import wire
from msg.plugins.money import _balance, _supply
from test_service import NOW, call, register


@pytest.mark.asyncio
async def test_real_backup_restore_preserves_money_and_credential_policy(installed, tmp_path, pg_dsn):
    app, _ = installed
    config = app.settings.config_dir / 'msgd.toml'
    config.write_text(config.read_text().replace('display_name = "MSG"', 'display_name = "协作积分"')
                      .replace('code = "MSG"', 'code = "COOP"')
                      .replace('credential_delivery_recovery_window = "15m"',
                               'credential_delivery_recovery_window = "7m"'))
    app.settings = load_settings(app.settings.config_dir)
    key, subject, _ = await register(app, 'backup-policy-reader')
    original = await call(app, 'money.state', {})
    assert original.status == 'ok', wire(original)
    original_balance = await call(app, 'money.balance', {}, key=key, subject=subject)
    assert original_balance.status == 'ok', wire(original_balance)
    archive = tmp_path / 'policy.zip'
    await backup(app, archive)
    target = tmp_path / 'restored-policy'
    restore(archive, target, tmp_path / 'restored-data', postgres_dsn=pg_dsn)
    settings = load_settings(target)
    assert settings.money == app.settings.money
    assert settings.credential_delivery_recovery_window == 420
    raw = tomllib.loads((target / 'msgd.toml').read_text())
    assert raw['money']['currency_id'] == 'primary'
    assert raw['money']['scale'] == 6 and raw['money']['allow_overdraft'] is False
    assert settings.listen == '127.0.0.1'
    assert settings.server.postgres_dsn == pg_dsn
    assert settings.server.mail is None and settings.server.valkey_url is None
    assert (target / 'msgd.toml').stat().st_mode & 0o777 == 0o600
    restored = Application(settings, clock=lambda: NOW)
    await restored.load()
    try:
        current = await call(restored, 'money.state', {})
        assert current.status == 'error' and current.error.code == 'recovery_quarantined', wire(current)
        balance = await call(restored, 'money.balance', {}, key=key, subject=subject)
        assert balance.status == 'error' and balance.error.code == 'recovery_quarantined', wire(balance)
        async with restored.metadata.transaction(write=False) as tx:
            assert _supply(tx) == original.data['total_supply_minor']
            assert _balance(tx, subject) == original_balance.data['balance_minor']
            runtime = tx.setting('runtime_config')
            assert runtime['accept_writes'] is False and runtime['cleanup_enabled'] is False
    finally:
        await restored.close()
