"""Published schemas must not drift through aliases after registration/freeze."""

from copy import deepcopy

import pytest

from msg.core.codec import canonical, digest
from msg.core.errors import Failure
from msg.core.models import PluginManifest, ResourceRef
from msg.core.registry import Registry

REF = ResourceRef(id='schema_test_ownership')
SCHEMA = {
    'type': 'object',
    'properties': {'mode': {'enum': ['read']}},
    'required': ['mode'],
    'additionalProperties': False,
}


def registry(schema):
    result = Registry()
    result.add_schema(REF, schema)
    result.add(
        PluginManifest(
            name='identity',
            version='1',
            dependencies=(),
            resource_types=(),
            capabilities=(),
            operations=(),
            migrations=(),
        )
    )
    result.freeze()
    return result


@pytest.mark.parametrize('source', ['caller', 'lookup'])
def test_nested_schema_mutation_cannot_expand_a_registered_contract(source):
    supplied = deepcopy(SCHEMA)
    installed = registry(supplied)
    original = canonical(installed.schema(REF))
    exposed = supplied if source == 'caller' else installed.schema(REF)
    exposed['properties']['mode']['enum'].append('write')
    exposed['additionalProperties'] = True
    exposed['required'].clear()
    assert canonical(installed.schema(REF)) == original
    for invalid in ({'mode': 'write'}, {'mode': 'read', 'extra': 'injected'}, {}):
        with pytest.raises(Failure, match='schema_validation'):
            installed.validate(REF, invalid)
    installed.validate(REF, {'mode': 'read'})


def test_each_lookup_returns_an_independent_snapshot_with_identical_digest():
    installed = registry(deepcopy(SCHEMA))
    first, second = installed.schema(REF), installed.schema(REF.id)
    assert digest(first) == digest(second) == digest(SCHEMA)
    assert first is not second
    assert first['properties'] is not second['properties']
    first.clear()
    assert second == SCHEMA and installed.schema(REF) == SCHEMA


@pytest.mark.parametrize('schema', [True, False])
def test_boolean_json_schemas_keep_their_existing_validation_semantics(schema):
    installed = registry(schema)
    assert installed.schema(REF) is schema
    if schema:
        installed.validate(REF, {'arbitrary': ['value']})
    else:
        with pytest.raises(Failure, match='schema_validation'):
            installed.validate(REF, {})


def test_failed_remote_reference_registration_has_no_partial_contract():
    installed = Registry()
    with pytest.raises(Failure, match='remote_schema_reference_forbidden'):
        installed.add_schema(REF, {'$ref': 'https://example.invalid/never-fetch'})
    with pytest.raises(Failure, match='schema_not_found'):
        installed.schema(REF)
    installed.add_schema(REF, deepcopy(SCHEMA))
    installed.validate(REF, {'mode': 'read'})


def test_freeze_still_rejects_new_or_replacement_schemas():
    installed = registry(deepcopy(SCHEMA))
    for ref in (REF, ResourceRef(id='schema_new')):
        with pytest.raises(Failure, match='schema_conflict'):
            installed.add_schema(ref, {})
    assert installed.schema(REF) == SCHEMA
