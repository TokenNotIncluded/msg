from copy import deepcopy
from dataclasses import replace

import pytest

from msg.bootstrap import feature_manifest, manifest
from msg.core.errors import Failure
from msg.core.models import PluginManifest
from msg.core.registry import Registry


def plugin(name='identity', **changes):
    return PluginManifest(
        name=name,
        version='1',
        dependencies=(),
        resource_types=(),
        capabilities=(),
        operations=(),
        migrations=(),
        **changes,
    )


def test_legacy_manifest_keeps_empty_feature_claims():
    assert plugin().feature_ids == ()


def test_bootstrap_rejects_unknown_feature_and_wrong_check_owner():
    for changes in (
        {'feature_id': 'unowned'},
        {'doctor_check': 'import:evil'},
        {'selftest_case': 'root_network_rejected'},
    ):
        definition = deepcopy(manifest())
        definition['features'][0].update(changes)
        with pytest.raises(Failure, match='feature_source_mismatch'):
            feature_manifest(definition)


def test_feature_claims_reject_other_owner_and_duplicates_atomically():
    registry = Registry()
    for claims in (('money',), ('root_trust', 'root_trust'), ('absent',)):
        with pytest.raises(Failure, match='feature_source_mismatch'):
            registry.add(plugin(feature_ids=claims))
        assert not registry._plugins
    registry.add(plugin(feature_ids=('root_trust',)))
    registry.freeze()


def test_plugin_defaults_resolve_from_bootstrap_and_are_isolated():
    registry = Registry()
    registry.add(plugin())
    registry.add(replace(plugin('money', feature_ids=('money',)), dependencies=('identity',)))
    registry.freeze()
    (row,) = registry.features('money')
    assert row == next(row for row in feature_manifest() if row['feature_id'] == 'money')
    row['default_config']['banks'].append('mutated')
    assert registry.features('money')[0]['default_config']['banks'] == []
    with pytest.raises(Failure, match='unknown_plugin'):
        registry.features('absent')


def test_missing_feature_source_is_rejected_at_freeze(monkeypatch):
    definition = deepcopy(manifest())
    definition['features'].pop()
    monkeypatch.setattr('msg.bootstrap.manifest', lambda: definition)
    registry = Registry()
    registry.add(plugin())
    with pytest.raises(Failure, match='feature_source_mismatch'):
        registry.freeze()
    assert not registry.frozen


def test_features_do_not_bypass_plugin_dependencies():
    registry = Registry()
    dependent = replace(plugin('money', feature_ids=('money',)), dependencies=('identity',))
    with pytest.raises(Failure, match='missing_plugin_dependency'):
        registry.add(dependent)
    assert not registry._plugins


@pytest.mark.asyncio
async def test_installed_manifests_link_entire_inventory(installed):
    app, _ = installed
    from msg.plugins.features import FEATURE_SOURCES

    rows = [row for name in app.registry._plugins for row in app.registry.features(name)]
    assert len(rows) == len(FEATURE_SOURCES)
    assert {row['feature_id'] for row in rows} == set(FEATURE_SOURCES)
