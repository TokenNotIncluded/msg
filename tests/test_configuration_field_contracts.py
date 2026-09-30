"""Every accepted configuration field has executable loading/rejection evidence."""

import json

import pytest

from msg.config import load_settings
from msg.core.errors import Failure


@pytest.mark.parametrize('value', ['"false"', '1', '[]'])
def test_mail_enable_requires_boolean_even_when_truthy(tmp_path, value):
    (tmp_path / 'msgd.toml').write_text(
        '[server]\nservice_url="https://service.example.org"\n[storage]\npostgres_dsn="service=msgd"\n'
    )
    (tmp_path / 'mail.toml').write_text(
        'enabled=' + value + '\nhost="smtp.example.test"\n'
        'tls="starttls"\nsender="sender@example.test"\n'
    )
    with pytest.raises(Failure, match='^invalid_mail_enabled$'):
        load_settings(tmp_path)


@pytest.mark.parametrize('value', ['0', '65536', 'true', '"587"'])
def test_mail_port_rejects_out_of_range_or_non_integer(tmp_path, value):
    (tmp_path / 'msgd.toml').write_text(
        '[server]\nservice_url="https://service.example.org"\n[storage]\npostgres_dsn="service=msgd"\n'
    )
    (tmp_path / 'mail.toml').write_text(
        'enabled=true\nhost="smtp.example.test"\n'
        'tls="starttls"\nsender="sender@example.test"\nport=' + value + '\n'
    )
    with pytest.raises(Failure, match='^invalid_mail_port$'):
        load_settings(tmp_path)


@pytest.mark.parametrize('section', ['server', 'storage', 'limits', 'tools'])
def test_configuration_sections_reject_non_tables_with_stable_error(tmp_path, section):
    base = '[storage]\npostgres_dsn="service=msgd"\n' if section != 'storage' else ''
    (tmp_path / 'msgd.toml').write_text(section + '=false\n' + base)
    with pytest.raises(Failure, match='^invalid_configuration_section$'):
        load_settings(tmp_path)


def test_field_matrix_runs_normal_default_and_rejection_vectors_without_secrets():
    from msg.admin.config_check import configuration_selftest
    from msg.config_contracts import CONFIGURATION_FIELDS

    result = configuration_selftest()
    assert set(result) == set(CONFIGURATION_FIELDS)
    assert all(row['status'] == 'pass' for row in result.values()), result
    assert all(
        row['default'] in {'pass', 'required', 'disabled'}
        and row['accepted'] == 'pass'
        and row['rejected'] == 'pass'
        for row in result.values()
    )
    assert 'password' not in json.dumps(result)


def test_doctor_field_status_never_substitutes_parsing_for_live_checks():
    from msg.admin.config_check import configuration_doctor

    observations = {
        'configuration': {'ok': True},
        'storage': {'ok': False},
        'valkey': {'ok': True, 'status': 'disabled'},
    }
    result = configuration_doctor(observations)
    assert result['storage.postgres_dsn']['related_check']['status'] == 'fail'
    assert result['storage.valkey_url']['related_check']['status'] == 'disabled'
    assert result['mail.host']['status'] == 'pass'
    assert result['mail.host']['scope'] == 'configuration_loading'
    assert result['mail.host']['related_check']['status'] == 'not_checked'
    assert all(row['status'] == 'fail' for row in configuration_doctor({}).values())


def test_vector_gap_is_an_error_instead_of_empty_success(monkeypatch):
    from msg.admin import config_check

    changed = dict(config_check.CONFIGURATION_FIELDS, **{'server.uncovered': {}})
    monkeypatch.setattr(config_check, 'CONFIGURATION_FIELDS', changed)
    with pytest.raises(Failure, match='^configuration_diagnostic_coverage_missing$'):
        config_check.configuration_selftest()


def test_effective_default_drift_is_reported_as_failure(monkeypatch):
    from dataclasses import replace

    import msg.config as config
    from msg.admin.config_check import configuration_selftest

    real = config.load_settings
    monkeypatch.setattr(config, 'load_settings', lambda path: replace(real(path), temporary_ttl=42))
    result = configuration_selftest()
    assert result['server.temporary_ttl'] == {
        'status': 'fail',
        'code': 'configuration_default_mismatch',
    }


@pytest.mark.asyncio
async def test_real_doctor_exposes_complete_field_matrix_read_only(installed):
    from test_service import NOW

    from msg.admin.diagnostics import doctor
    from msg.config_contracts import CONFIGURATION_FIELDS

    app, _ = installed
    config_path = app.settings.config_dir / 'msgd.toml'
    original = config_path.read_bytes()
    result = doctor(app.settings.config_dir, clock=lambda: NOW)
    assert set(result['configuration_fields']) == set(CONFIGURATION_FIELDS)
    assert all(row['status'] == 'pass' for row in result['configuration_fields'].values())
    assert config_path.read_bytes() == original
    assert 'service=' not in json.dumps(result['configuration_fields'])


def test_real_doctor_reports_bad_configuration_without_exposing_values(tmp_path):
    from msg.admin.diagnostics import doctor

    (tmp_path / 'msgd.toml').write_text('[storage]\npostgres_dsn="do-not-echo-me"\n')
    result = doctor(tmp_path)
    assert result['ok'] is False
    assert all(row['status'] == 'fail' for row in result['configuration_fields'].values())
    assert 'do-not-echo-me' not in json.dumps(result)


def test_configuration_selftest_opens_no_network_or_secret_file(monkeypatch):
    import socket
    from pathlib import Path

    import psycopg

    from msg.admin.config_check import configuration_selftest

    def forbidden(*args, **kwargs):
        raise AssertionError('configuration acceptance must not access secrets or network')

    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(psycopg, 'connect', forbidden)
    monkeypatch.setattr(Path, 'read_bytes', forbidden)
    assert all(row['status'] == 'pass' for row in configuration_selftest().values())


@pytest.mark.asyncio
async def test_official_selftest_runs_configuration_field_vectors():
    from msg.admin.diagnostics import selftest
    from msg.config_contracts import CONFIGURATION_FIELDS

    result = await selftest()
    assert result['checks']['configuration_loading'] is True, result
    assert set(result['configuration_fields']) == set(CONFIGURATION_FIELDS)
    assert all(row['status'] == 'pass' for row in result['configuration_fields'].values())
    assert result['cleaned_up'] is True
    assert result['ok'] is True, result
