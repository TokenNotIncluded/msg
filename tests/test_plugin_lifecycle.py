from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_manifest_feature_linkage import plugin

from msg.config import load_settings, write_example
from msg.core.errors import Failure
from msg.core.registry import Registry
from msg.plugins import BUILTINS, install_registry


@pytest.mark.parametrize(
    'value,code',
    [
        ('"identity"', 'invalid_plugin_list'),
        ('{}', 'invalid_plugin_list'),
        ('["identity", "identity"]', 'invalid_plugin_list'),
        ('["identity", "external.py"]', 'unknown_plugin'),
        ('[]', 'identity_plugin_required'),
    ],
)
def test_invalid_plugin_configuration_rejected_before_application(tmp_path, value, code):
    settings = write_example(tmp_path / 'etc', tmp_path / 'data')
    path = settings.config_dir / 'msgd.toml'
    with path.open('a') as stream:
        stream.write('\n[plugins]\nenabled = ' + value + '\n')
    with pytest.raises(Failure, match=code):
        load_settings(settings.config_dir)


def test_configuration_defaults_are_installed_defaults(tmp_path):
    settings = write_example(tmp_path / 'etc', tmp_path / 'data')
    assert settings.server.plugins == BUILTINS
    assert settings.credential_delivery_recovery_window == 15 * 60
    assert settings.money.enabled is True
    assert settings.money.currency_id == 'primary'
    assert settings.money.display_name == settings.money.code == 'MSG'
    assert (settings.money.scale, settings.money.transfer_fee, settings.money.allow_overdraft) == (
        6,
        0,
        False,
    )


@pytest.mark.parametrize(
    'plugins,code',
    [
        ('identity', 'invalid_plugin_list'),
        (('identity', 'identity'), 'invalid_plugin_list'),
        (('external.py',), 'unknown_plugin'),
        ((), 'identity_plugin_required'),
    ],
)
def test_invalid_direct_install_is_atomic(plugins, code):
    context = SimpleNamespace(registry=Registry())
    with pytest.raises(Failure, match=code):
        install_registry(context, plugins)
    assert not context.registry._plugins
    assert not context.registry._schemas


def test_unsupported_migration_is_rejected_before_contract_registration():
    registry = Registry()
    with pytest.raises(Failure, match='unsupported_plugin_migration'):
        registry.add(replace(plugin(), migrations=('run:untrusted.module',)))
    assert not registry._plugins
    assert not registry.operations()


@pytest.mark.asyncio
@pytest.mark.parametrize('version', [1, 99, True])
async def test_unavailable_queued_operation_never_runs_effect(installed, monkeypatch, version):
    from test_effect_retry_state import job_for

    from msg.workers.effects import EffectWorker

    app, _ = installed
    job = replace(
        job_for('webhook', state='pending', attempts=0),
        lease_until=None,
        arguments={'contract_version': version},
    )
    if version == 1:
        # A restarted application has omitted the communication plugin.
        app.registry._operations.pop(('communication.send', 1))
    async with app.metadata.transaction(write=True) as tx:
        await tx.enqueue(job)
    worker = EffectWorker(app)

    async def forbidden(*args):
        raise AssertionError('disabled or unregistered operation reached external dispatch')

    monkeypatch.setattr(worker, '_webhook', forbidden)
    assert await worker.run_once() is True
    async with app.metadata.transaction(write=False) as tx:
        current = await tx.job(job.id)
        assert current.state == 'failed'
        assert current.result is None
        assert tx.setting('job_status:' + job.id)['code'] == (
            'invalid_job_contract_version' if version is True else 'unknown_operation'
        )
        assert current.arguments == job.arguments


@pytest.mark.asyncio
@pytest.mark.parametrize('installed_version,queued_version', [(1, 1), (2, 2), (1, 2), (2, 1)])
async def test_worker_uses_exact_queued_contract_version(
    installed, monkeypatch, installed_version, queued_version
):
    from test_effect_retry_state import job_for

    from msg.workers.effects import EffectWorker

    app, _ = installed
    spec = app.registry._operations.pop(('communication.send', 1))
    app.registry._operations[('communication.send', installed_version)] = replace(
        spec, version=installed_version
    )
    job = replace(
        job_for('webhook', state='pending', attempts=0),
        lease_until=None,
        arguments={'contract_version': queued_version},
    )
    async with app.metadata.transaction(write=True) as tx:
        await tx.enqueue(job)
    worker = EffectWorker(app)
    dispatched = []

    async def dispatch(current):
        dispatched.append(current.id)
        await worker._finish(current, 'done', 'test_dispatched')

    monkeypatch.setattr(worker, '_webhook', dispatch)
    assert await worker.run_once()
    async with app.metadata.transaction(write=False) as tx:
        current = await tx.job(job.id)
        assert current.state == ('done' if installed_version == queued_version else 'failed')
        assert dispatched == ([job.id] if installed_version == queued_version else [])


@pytest.mark.asyncio
async def test_disabling_plugin_preserves_stored_data_and_pending_job(installed, monkeypatch):
    from test_effect_retry_state import job_for

    from msg.application import Application

    app, _ = installed
    job = replace(job_for('webhook', state='pending', attempts=0), lease_until=None)
    async with app.metadata.transaction(write=True) as tx:
        await tx.enqueue(job)
    async with app.metadata.transaction(write=False) as tx:
        before = tx.rows('SELECT id,body FROM resources ORDER BY id')
    settings = replace(
        app.settings,
        server=replace(
            app.settings.server,
            plugins=tuple(name for name in app.settings.server.plugins if name != 'communication'),
        ),
    )
    disabled = Application(settings)

    async def forbidden_notification(*args):
        raise AssertionError('disabled plugin produced a notification')

    monkeypatch.setattr('msg.plugins.communication.enqueue_domain_webhooks', forbidden_notification)
    await disabled._event_notifications(None, None)
    with pytest.raises(Failure, match='unknown_operation'):
        disabled.registry.operation('communication.send')
    # Constructing the disabled registry neither rewrites facts nor completes jobs.
    async with app.metadata.transaction(write=False) as tx:
        assert tx.rows('SELECT id,body FROM resources ORDER BY id') == before
        assert await tx.job(job.id) == job


@pytest.mark.asyncio
async def test_version_two_maintenance_producer_persists_exact_version(installed, monkeypatch):
    from test_effect_retry_state import job_for
    from test_service import NOW

    app, _ = installed
    captured = []

    async def allow(*args):
        return True

    async def enqueue(job):
        captured.append(job)

    monkeypatch.setattr(app.authorizer, 'has', allow)
    request = SimpleNamespace(
        operation='system.maintenance',
        contract_version=2,
        request_id='versioned-maintenance',
        arguments={'action': 'deliver_due_todos'},
    )
    context = SimpleNamespace(principal=job_for('maintenance').principal, now=NOW)
    await app.registry.operation('system.maintenance', 2).handler(
        context, request, SimpleNamespace(enqueue=enqueue)
    )
    assert len(captured) == 1
    assert captured[0].arguments['contract_version'] == 2
    assert captured[0].arguments['action'] == 'deliver_due_todos'


def test_disabled_plugin_diagnostics_cannot_claim_feature_success():
    from msg.admin.diagnostics import feature_results
    from msg.bootstrap import feature_manifest

    result = feature_results(
        feature_manifest(),
        {'market_clearing': {'ok': True}},
        'doctor_check',
        enabled_plugins=('identity',),
    )
    assert result['money'] == {'status': 'disabled', 'check': 'market_clearing'}
    assert result['root_trust'] == {
        'status': 'fail',
        'check': 'root_trust',
        'reason': 'check_missing',
    }
