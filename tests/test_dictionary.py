"""Public short-code dictionary and direct read-path contract."""

from copy import deepcopy
from importlib.resources import files
from types import SimpleNamespace

import pytest

from msg.application import Application
from msg.core.codec import b64, loads
from msg.core.errors import Failure
from msg.transports.dictionary import build_dictionary


@pytest.fixture
def registry():
    settings = SimpleNamespace(
        server=SimpleNamespace(
            plugins=('identity', 'content', 'discussion', 'communication', 'discovery')
        )
    )
    return Application(settings).registry


def test_registry_drives_stable_public_documents(registry):
    first = build_dictionary(registry)
    second = build_dictionary(registry)
    assert first.document == second.document
    assert first.etag == second.etag
    assert first.schema_document == second.schema_document
    assert first.schema_etag == second.schema_etag
    assert first.etag.startswith('"') and first.etag.endswith('"')
    assert first.document['codes'].keys() == {'namespace', 'operation', 'field', 'enum'}
    assert first.document['operations']
    for operation in first.document['operations']:
        assert {'required', 'shortest_template', 'example', 'fields'} <= operation.keys()
        for field in operation['fields']:
            assert {'type', 'required', 'order', 'constraints', 'code'} <= field.keys()

    class ReorderedRegistry:
        frozen = True

        def operations(self, entry):
            return tuple(reversed(registry.operations(entry)))

        def schema(self, ref):
            return registry.schema(ref)

        def describe(self, spec):
            return registry.describe(spec)

        def validate(self, ref, value):
            return registry.validate(ref, value)

    reordered = build_dictionary(ReorderedRegistry())
    assert reordered.document == first.document
    assert reordered.etag == first.etag


def test_direct_read_decodes_required_and_optional_short_fields(registry):
    dictionary = build_dictionary(registry)
    operation = 'discovery.get@1'
    op_code = dictionary.code_for('operation', operation)
    limit_code = dictionary.code_for('field', operation + ':limit')
    spec, arguments = dictionary.decode_get_path(op_code, ['/main', limit_code, '12'])
    assert spec.name == 'discovery.get'
    assert arguments == {'id': '/main', 'limit': 12}
    assert dictionary.resolve_operation(op_code) is spec


def test_unknown_or_cross_operation_codes_fail_closed(registry):
    dictionary = build_dictionary(registry)
    read = dictionary.code_for('operation', 'discovery.get@1')
    other_field = dictionary.code_for('field', 'discovery.list@1:limit')
    mutation = dictionary.code_for('operation', 'content.post_create@1')
    with pytest.raises(Failure, match='unknown_short_code'):
        dictionary.resolve_operation('ounknown')
    with pytest.raises(Failure, match='unknown_short_code'):
        dictionary.decode_get_path(read, ['/main', 'funknown', '1'])
    with pytest.raises(Failure, match='field_code_operation_mismatch'):
        dictionary.decode_get_path(read, ['/main', other_field, '1'])
    with pytest.raises(Failure, match='direct_path_read_only'):
        dictionary.decode_get_path(mutation, ['/main', 'hello'])
    with pytest.raises(Failure, match='missing_path_argument'):
        dictionary.decode_get_path(read, [])
    with pytest.raises(Failure, match='invalid_path_arguments'):
        dictionary.decode_get_path(read, ['/main', 'fextra'])


def test_enum_codes_are_scoped_to_field(registry):
    dictionary = build_dictionary(registry)
    operation = 'discovery.list@1'
    op_code = dictionary.code_for('operation', operation)
    state_code = dictionary.code_for('field', operation + ':state')
    state_field = next(
        field for field in dictionary.document['operations'] if field['name'] == 'discovery.list'
    )
    state = next(field for field in state_field['fields'] if field['name'] == 'state')
    value = state['enum'][0]
    _, arguments = dictionary.decode_get_path(op_code, [state_code, value['code']])
    assert arguments == {'state': value['value']}
    with pytest.raises(Failure, match='unknown_short_code'):
        dictionary.decode_get_path(op_code, [state_code, str(value['value'])])


def test_published_dictionary_rejects_changed_meanings_and_keeps_retired_codes(registry):
    original = build_dictionary(registry).document
    changed = deepcopy(original)
    changed['operations'][0]['effect'] = 'read'
    with pytest.raises(Failure, match='published_operation_changed'):
        build_dictionary(registry, published=changed)

    class ReducedRegistry:
        frozen = True

        def operations(self, entry):
            return tuple(
                spec for spec in registry.operations(entry) if spec.name != 'discovery.get'
            )

        def schema(self, ref):
            return registry.schema(ref)

        def describe(self, spec):
            return registry.describe(spec)

    reduced = build_dictionary(ReducedRegistry(), published=original)
    old_code = build_dictionary(registry).code_for('operation', 'discovery.get@1')
    retired = next(row for row in reduced.document['codes']['operation'] if row['code'] == old_code)
    assert retired['deprecated'] and retired['replaced_by'] is None
    assert reduced.lookup_document(old_code)['deprecated']
    assert any(row['code'] == old_code for row in reduced.index_document['retired'])
    with pytest.raises(Failure, match='deprecated_short_code'):
        reduced.resolve_operation(old_code)


def test_scoped_dictionary_keeps_only_requested_namespace_or_operation(registry):
    dictionary = build_dictionary(registry)
    assert dictionary.index_document['namespaces']
    assert all(
        set(operation) == {'name', 'code', 'effect'}
        for namespace in dictionary.index_document['namespaces']
        for operation in namespace['operations']
    )
    assert dictionary.index_etag != dictionary.etag
    namespace_code = dictionary.code_for('namespace', 'discovery')
    namespace = dictionary.lookup_document(namespace_code)
    assert namespace['operations']
    assert all(row['namespace'] == 'discovery' for row in namespace['operations'])
    assert len(namespace['operations']) < len(dictionary.document['operations'])
    assert len(dictionary.lookup_document('discovery')['operations']) == len(
        namespace['operations']
    )
    op_code = dictionary.code_for('operation', 'discovery.get@1')
    operation = dictionary.lookup_document(op_code)
    assert len(operation['operations']) == 1
    assert operation['operations'][0]['name'] == 'discovery.get'
    assert len(dictionary.lookup_document('discovery.get')['operations']) == 1
    assert len(operation['codes']['operation']) == 1
    assert all(row['operation'] == 'discovery.get@1' for row in operation['codes']['field'])
    assert dictionary.etag_for(op_code) == dictionary.etag_for(op_code)
    assert dictionary.etag_for(op_code) != dictionary.etag
    with pytest.raises(Failure, match='unknown_short_code'):
        dictionary.lookup_document('unknown.operation')


def test_non_direct_template_uses_full_operation_packet_route(registry):
    dictionary = build_dictionary(registry)
    operation = next(
        row for row in dictionary.document['operations'] if row['name'] == 'content.post_create'
    )
    assert not operation['direct']
    assert operation['shortest_template'] == '/-/g/content.post_create/j/{packet}'


def test_default_build_checks_packaged_publication_snapshot(registry):
    snapshot = loads(files('msg.data').joinpath('shortcodes.json').read_bytes())
    automatic = build_dictionary(registry)
    # The publication is historical: new operations can outnumber its rows.
    # Every published short code must still retain exactly the same identity.
    for kind, rows in snapshot['codes'].items():
        current = {row['code']: row['identity'] for row in automatic.document['codes'][kind]}
        assert all(current[row['code']] == row['identity'] for row in rows)
    explicit = build_dictionary(registry, published=snapshot)
    assert automatic.document == explicit.document
    assert automatic.document != build_dictionary(registry, published=None).document


@pytest.mark.parametrize(
    ('old_name', 'old_version', 'new_name', 'new_version'),
    [
        ('identity.temporary', 1, 'identity.temporary', 3),
        ('identity.temporary', 2, 'identity.temporary', 3),
        ('identity.custodial_create', 1, 'identity.custodial_create', 2),
        ('identity.token_rotate', 1, 'identity.token_rotate', 2),
        ('identity.token_create', 1, 'identity.token_create', 2),
    ],
)
def test_disabled_credential_versions_are_published_as_deprecated(
    registry, old_name, old_version, new_name, new_version
):
    dictionary = build_dictionary(registry)
    old_identity = f'{old_name}@{old_version}'
    new_identity = f'{new_name}@{new_version}'
    old_code = dictionary.code_for('operation', old_identity)
    new_code = dictionary.code_for('operation', new_identity)
    old_row = next(
        row for row in dictionary.document['codes']['operation'] if row['code'] == old_code
    )
    new_spec = dictionary.resolve_operation(new_code)
    assert old_row['deprecated'] is True
    assert old_row['replaced_by'] == new_code
    assert new_spec.name == new_name and new_spec.version == new_version
    scoped = dictionary.lookup_document(old_code)
    assert scoped['deprecated'] is True
    assert scoped['replaced_by'] == new_code
    assert scoped['replacement'] == {'code': new_code, 'identity': new_identity}
    with pytest.raises(Failure, match='deprecated_short_code'):
        dictionary.resolve_operation(old_code)

    old_description = next(
        row
        for row in dictionary.schema_document['operations']
        if row['name'] == old_name and row['version'] == old_version
    )
    assert old_description['deprecated'] is True
    assert old_description['replaced_by'] == new_identity


def test_deprecation_metadata_does_not_rewrite_published_operation_rows(registry):
    snapshot = loads(files('msg.data').joinpath('shortcodes.json').read_bytes())
    dictionary = build_dictionary(registry)
    for operation in snapshot['operations']:
        if operation['name'] in {
            'identity.temporary',
            'identity.custodial_create',
            'identity.token_rotate',
            'identity.token_create',
        }:
            identity = f'{operation["name"]}@{operation["version"]}'
            code = dictionary.code_for('operation', identity)
            actual = next(
                row
                for row in dictionary.document['operations']
                if row['name'] == operation['name'] and row['version'] == operation['version']
            )
            assert actual == operation
            if operation['version'] == 1 or (
                operation['name'] == 'identity.temporary' and operation['version'] == 2
            ):
                code_row = next(
                    row for row in dictionary.document['codes']['operation'] if row['code'] == code
                )
                assert code_row['deprecated'] is True


def test_bare_operation_discovery_uses_active_version_after_v1_retirement(registry):
    dictionary = build_dictionary(registry)
    active = dictionary.lookup_document('identity.temporary')
    assert [row['version'] for row in active['operations']] == [3]
    retired = dictionary.lookup_document('identity.temporary@1')
    assert retired['deprecated'] is True
    assert retired['replacement']['identity'] == 'identity.temporary@3'


def test_legacy_direct_write_codes_stay_stable_but_are_rejected(registry):
    dictionary = build_dictionary(registry)
    operation = 'content.post_create@1'
    code = dictionary.code_for('operation', operation)
    parent = dictionary.code_for('field', operation + ':parent')
    assert dictionary.resolve_operation(code).name == 'content.post_create'
    assert dictionary.resolve('field', parent)['path'] == 'parent'
    token = b64(b't' * 32)
    with pytest.raises(Failure, match='secure_channel_required'):
        dictionary.decode_direct_write_path(
            code,
            [
                'token',
                't_test',
                token,
                'u_test',
                'request_1',
                '2026-09-27T00:05:00Z',
                'args',
                parent,
                '/main',
            ],
        )
    row = next(
        row for row in dictionary.document['operations'] if row['name'] == 'content.post_create'
    )
    assert row['direct_write_template'] is None
    assert row['direct_expected_template'] is None
    assert row['direct_write_status'] == 'rejected_legacy_secret_url'


def test_legacy_bootstrap_claim_and_transfer_token_paths_are_rejected(registry):
    dictionary = build_dictionary(registry)
    bootstrap = dictionary.code_for('operation', 'identity.temporary@1')
    nonce_code = dictionary.code_for('field', 'identity.temporary@1:nonce')
    with pytest.raises(Failure, match='secure_channel_required'):
        dictionary.decode_direct_write_path(
            bootstrap,
            [
                'bootstrap',
                'request_1',
                '2026-09-27T00:05:00Z',
                'args',
                nonce_code,
                b64(b'n' * 32),
            ],
        )
    row = next(
        row for row in dictionary.document['operations'] if row['name'] == 'identity.temporary'
    )
    assert row['requires_secure_channel'] is True
    assert row['shortest_template'] == '/-/p/identity.temporary'


@pytest.mark.parametrize(
    'name',
    [
        'identity.temporary',
        'identity.custodial_create',
        'identity.token_create',
        'identity.token_rotate',
        'identity.token_recover',
    ],
)
def test_credential_secret_operations_have_no_argument_paths(registry, name):
    dictionary = build_dictionary(registry)
    rows = [row for row in dictionary.document['operations'] if row['name'] == name]
    assert rows
    for row in rows:
        assert row['requires_secure_channel'] is True
        assert row['example'] is None
        assert row['direct'] is False
        assert row['shortest_template'] == '/-/p/' + name
        assert row['direct_write_template'] is None
        assert row['direct_expected_template'] is None


def test_sharing_link_secret_has_no_argument_path():
    settings = SimpleNamespace(
        server=SimpleNamespace(
            plugins=('identity', 'content', 'discussion', 'communication', 'discovery', 'sharing')
        )
    )
    dictionary = build_dictionary(Application(settings).registry)
    row = next(
        row for row in dictionary.document['operations'] if row['name'] == 'sharing.link_read'
    )
    assert row['requires_secure_channel'] is True
    assert row['example'] is None
    assert row['shortest_template'] == '/-/p/sharing.link_read'
