from copy import deepcopy

import pytest
from test_service import NOW

from msg.admin.diagnostics import doctor, feature_results, selftest
from msg.bootstrap import feature_manifest, manifest
from msg.core.errors import Failure


def test_feature_inventory_has_unique_real_samples_and_explicit_checks():
    rows = feature_manifest()
    assert {
        'bootstrap',
        'postgres',
        'root_trust',
        'online_ca',
        'authorization',
        'audit',
        'content',
        'git_content',
    } <= {row['feature_id'] for row in rows}
    assert all(
        set(row)
        == {
            'feature_id',
            'enabled_by_default',
            'default_config',
            'sample_resource',
            'doctor_check',
            'selftest_case',
        }
        for row in rows
    )
    assert (
        next(row for row in rows if row['feature_id'] == 'git_content')['selftest_case']
        == 'git_push_read_cas'
    )
    mapped = {row['feature_id']: (row['doctor_check'], row['selftest_case']) for row in rows}
    assert {
        key: mapped[key]
        for key in (
            'bootstrap',
            'postgres',
            'root_trust',
            'online_ca',
            'authorization',
            'audit',
            'content',
            'git_content',
        )
    } == {
        'bootstrap': ('bootstrap', 'isolated_namespace'),
        'postgres': ('storage', 'stable_id_read'),
        'root_trust': ('root_trust', 'root_network_rejected'),
        'online_ca': ('online_ca', 'online_registration'),
        'authorization': ('authority_snapshot', 'certgate'),
        'audit': ('audit', 'online_delegation'),
        'content': ('content_layout', 'idempotency'),
        'git_content': ('git', 'git_push_read_cas'),
    }
    assert {key: mapped[key] for key in ('recovery', 'search', 'hosting')} == {
        'recovery': ('recovery_checkpoint', 'recovery_checkpoint_replay'),
        'search': ('lexical_search', 'lexical_search_access'),
        'hosting': ('hosting', 'hosting_deploy'),
    }


def test_recovery_checkpoint_inspection_detects_vocabulary_drift(monkeypatch):
    from msg.admin import recovery_replay
    from msg.admin.diagnostics import inspect_recovery_checkpoint

    assert inspect_recovery_checkpoint() == {
        'fact_kinds': len(recovery_replay.SUPPORTED_FACTS),
        'promotion': 'blocked',
    }
    monkeypatch.setattr(
        recovery_replay, 'SUPPORTED_FACTS', recovery_replay.SUPPORTED_FACTS - {'vault.destroy'}
    )
    with pytest.raises(Failure, match='^recovery_checkpoint_schema_drift$'):
        inspect_recovery_checkpoint()


def test_disabled_plugins_are_reported_as_skipped_not_passed():
    rows = feature_manifest()
    observations = {
        'lexical_search': {'ok': True, 'status': 'disabled'},
        'hosting': {'ok': False, 'code': 'unknown_operation'},
    }
    result = feature_results(rows, observations, 'doctor_check')
    assert result['search'] == {'status': 'skip', 'check': 'lexical_search'}
    assert result['hosting'] == {'status': 'fail', 'check': 'hosting'}
    assert result['recovery'] == {
        'status': 'fail',
        'check': 'recovery_checkpoint',
        'reason': 'check_missing',
    }


@pytest.mark.parametrize(
    'change',
    [
        lambda row: row.update(sample_resource='r_nonexistent'),
        lambda row: row.update(feature_id='BAD'),
        lambda row: row.update(enabled_by_default='true'),
        lambda row: row.pop('doctor_check'),
    ],
)
def test_feature_inventory_rejects_invalid_declarations(change):
    definition = deepcopy(manifest())
    change(definition['features'][0])
    with pytest.raises(Failure, match='feature_manifest_invalid'):
        feature_manifest(definition)


def test_feature_results_do_not_invent_success():
    rows = feature_manifest()
    doctor_result = feature_results(rows, {'bootstrap': {'ok': True}}, 'doctor_check')
    assert doctor_result['bootstrap'] == {'status': 'pass', 'check': 'bootstrap'}
    assert doctor_result['postgres'] == {
        'status': 'fail',
        'check': 'storage',
        'reason': 'check_missing',
    }
    assert doctor_result['valkey_signal']['status'] == 'disabled'
    selftest_result = feature_results(rows, {'isolated_namespace': True}, 'selftest_case')
    assert selftest_result['bootstrap']['status'] == 'pass'
    assert selftest_result['git_content'] == {
        'status': 'fail',
        'check': 'git_push_read_cas',
        'reason': 'check_missing',
    }
    assert selftest_result['postgres']['status'] == 'fail'


@pytest.mark.asyncio
async def test_doctor_feature_results_are_grounded_in_read_only_checks(installed):
    app, _ = installed
    result = doctor(app.settings.config_dir, clock=lambda: NOW)
    for feature_id in (
        'bootstrap',
        'postgres',
        'root_trust',
        'online_ca',
        'authorization',
        'audit',
        'content',
        'git_content',
        'identity_upgrade',
        'recovery',
        'search',
        'hosting',
        'honors',
    ):
        row = result['features'][feature_id]
        assert row['status'] == 'pass', result
        assert result['checks'][row['check']]['ok'] is True
    assert result['checks']['lexical_search']['read_only'] is True
    assert result['checks']['recovery_checkpoint']['promotion'] == 'blocked'


@pytest.mark.asyncio
async def test_selftest_feature_results_are_grounded_in_isolated_operations():
    result = await selftest()
    assert result['ok'] and result['cleaned_up'], result['checks'].get('failure', result)
    for feature_id in (
        'bootstrap',
        'postgres',
        'root_trust',
        'online_ca',
        'authorization',
        'audit',
        'content',
        'identity_upgrade',
        'recovery',
        'search',
        'hosting',
        'honors',
    ):
        row = result['features'][feature_id]
        assert row['status'] == 'pass', result
        assert result['checks'][row['check']] is True
    assert result['features']['git_content'] == {'status': 'pass', 'check': 'git_push_read_cas'}
    assert result['checks']['git_push_read_cas'] is True
