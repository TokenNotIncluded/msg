"""Schema contracts are self-contained; validation never loads files or URLs."""
import urllib.request

import pytest

from msg.core.errors import Failure
from msg.core.models import ResourceRef
from msg.core.registry import Registry


REF = ResourceRef(id='schema_local_references')


@pytest.mark.parametrize('keyword', ['$ref', '$dynamicRef', '$recursiveRef'])
@pytest.mark.parametrize('reference', [
    'https://example.invalid/schema', 'file:///tmp/msg-schema.json',
    'ftp://example.invalid/schema', '//example.invalid/schema',
    'other-schema.json#/$defs/value', 'urn:external:schema',
])
def test_nonlocal_references_are_rejected_before_publishing(keyword, reference):
    registry = Registry()
    with pytest.raises(Failure, match='remote_schema_reference_forbidden'):
        registry.add_schema(REF, {keyword: reference})
    with pytest.raises(Failure, match='schema_not_found'):
        registry.schema(REF)
    registry.add_schema(REF, {'type': 'integer'})
    registry.validate(REF, 1)


@pytest.mark.parametrize('schema', [
    {'properties': {'item': {'$ref': 'file:///tmp/msg-schema.json'}}},
    {'$defs': {'item': {'$dynamicRef': 'ftp://example.invalid/schema'}}},
    {'allOf': [{'$ref': 'other-schema.json'}]},
    {'items': {'$ref': '//example.invalid/schema'}},
    {'if': {'$ref': 'urn:external:schema'}},
    {'dependentSchemas': {'item': {'$ref': 'other-schema.json'}}},
    {'unevaluatedProperties': {'$ref': 'other-schema.json'}},
])
def test_nonlocal_references_in_subschemas_are_rejected(schema):
    with pytest.raises(Failure, match='remote_schema_reference_forbidden'):
        Registry().add_schema(REF, schema)


def test_local_pointer_with_http_metadata_is_not_mistaken_for_remote_reference():
    schema = {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        '$id': 'https://example.invalid/local-contract',
        'description': 'An HTTP status; this description is not a request.',
        '$defs': {'status': {'type': 'integer', 'minimum': 100, 'maximum': 599}},
        '$ref': '#/$defs/status',
    }
    registry = Registry()
    registry.add_schema(REF, schema)
    assert registry.schema(REF) == schema
    registry.validate(REF, 200)
    with pytest.raises(Failure, match='schema_validation'):
        registry.validate(REF, 99)


@pytest.mark.parametrize('keyword', ['$ref', '$dynamicRef'])
def test_local_anchors_still_validate(keyword):
    registry = Registry()
    registry.add_schema(REF, {'$defs': {'text': {'$anchor': 'text', 'type': 'string'}},
                              keyword: '#text'})
    registry.validate(REF, 'value')
    with pytest.raises(Failure, match='schema_validation'):
        registry.validate(REF, 1)


def test_reference_named_properties_and_annotation_data_are_not_schema_references():
    schema = {'type': 'object', 'properties': {
        '$ref': {'type': 'string'}, '$dynamicRef': {'type': 'string'}},
        'default': {'$ref': 'https://example.invalid/ordinary-data'},
        'examples': [{'$dynamicRef': 'file:///tmp/ordinary-data'}]}
    registry = Registry()
    registry.add_schema(REF, schema)
    registry.validate(REF, {'$ref': 'an ordinary string', '$dynamicRef': 'another'})
    with pytest.raises(Failure, match='schema_validation'):
        registry.validate(REF, {'$ref': 1})


def test_pointer_into_annotation_cannot_enable_implicit_file_retrieval(tmp_path, monkeypatch):
    external = tmp_path / 'external-schema.json'
    external.write_text('{"type":"integer"}')
    attempts = []

    def unexpected_io(*args, **kwargs):
        attempts.append(args)
        raise AssertionError('schema validation must not perform I/O')

    monkeypatch.setattr(urllib.request, 'urlopen', unexpected_io)
    registry = Registry()
    registry.add_schema(REF, {'$ref': '#/default', 'default': {'$ref': external.as_uri()}})
    with pytest.raises(Failure, match='schema_reference_unresolvable') as rejected:
        registry.validate(REF, 1)
    assert str(external) not in str(rejected.value.as_dict())
    assert attempts == []


def test_missing_local_pointer_is_a_stable_error_without_uri_disclosure():
    registry = Registry()
    registry.add_schema(REF, {'$ref': '#/$defs/missing-private-value'})
    with pytest.raises(Failure, match='schema_reference_unresolvable') as rejected:
        registry.validate(REF, {})
    assert 'missing-private-value' not in str(rejected.value.as_dict())
