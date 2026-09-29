"""Registry-derived, order-independent short codes for public GET paths."""

from __future__ import annotations

import base64
import hashlib
from importlib.resources import files
from urllib.parse import quote

from msg.core.codec import canonical, digest, loads
from msg.core.errors import Failure, require

_PREFIX = {'namespace': 'n', 'operation': 'o', 'field': 'f', 'enum': 'e'}
_SCALAR = {'string', 'integer', 'number', 'boolean'}
_DEFAULT_PUBLISHED = object()
_SECRET_OPERATIONS = frozenset({
    'identity.temporary',
    'identity.custodial_create',
    'identity.token_create',
    'identity.token_rotate',
    'identity.token_recover',
    'sharing.link_read',
})

# Versioned, published path grammar for bounded GET-only read queries. The
# meanings of these short segments must not change within version 1.
READ_QUERY_V1_SEGMENTS = {
    'root': 'r',
    'type': 't',
    'sort': 's',
    'fields': 'f',
    'first': 'n',
    'after': 'a',
}
READ_QUERY_V2_SEGMENTS = {
    **READ_QUERY_V1_SEGMENTS,
    'expand': 'x',
    'nested_first': 'nf',
    'collection': 'co',
    'parent': 'pa',
    'limit': 'l',
}
READ_QUERY_V3_SEGMENTS = {
    **READ_QUERY_V1_SEGMENTS,
    'enter': 'x',
    'leave': 'up',
    'collection': 'co',
    'parent': 'pa',
}
READ_QUERY_V1_SORT = {'id': 'i', 'time': 't', 'name': 'n'}
READ_QUERY_V1_FIELDS = {
    'id': 'i',
    'type': 't',
    'name': 'n',
    'revision': 'v',
    'generation': 'g',
    'path': 'p',
    'created_at': 'c',
    'modified_at': 'm',
    'owner': 'o',
    'group': 'u',
    'mode': 'd',
}
SEARCH_QUERY_V1_SEGMENTS = {'query': 'q', 'tag': 't', 'limit': 'n', 'cursor': 'a'}


def read_query_path_document(registry):
    require(registry.operation('discovery.read_query').effect == 'read', 'effect_mismatch')
    require(registry.operation('discovery.read_query', 2).effect == 'read', 'effect_mismatch')
    require(registry.operation('discovery.read_query', 3).effect == 'read', 'effect_mismatch')
    require(registry.operation('discovery.search').effect == 'read', 'effect_mismatch')
    return {
        'version': 1,
        'available_versions': [1, 2, 3],
        'segments': READ_QUERY_V1_SEGMENTS,
        'segments_v2': READ_QUERY_V2_SEGMENTS,
        'segments_v3': READ_QUERY_V3_SEGMENTS,
        'sort': READ_QUERY_V1_SORT,
        'fields': READ_QUERY_V1_FIELDS,
        'search_segments': SEARCH_QUERY_V1_SEGMENTS,
        'read_template': '/_read/q/1/r/{percent-encoded-root}/t/{type}/s/{sort-code}/f/{field-codes}/n/{first}',
        'read_template_v2': '/_read/q/2/r/{percent-encoded-root}/x/{collections}/n/{first}/nf/{nested-first}',
        'read_template_v3': '/_r/q/3/r/{percent-encoded-root}/n/{first}/x/children/n/{nested-first}/up/1',
        'tree_rules': {
            'enter': 'x/children or x/replies',
            'leave': 'up/1',
            'max_depth': 4,
            'each_collection_has_page_info': True,
        },
        'search_template': '/_search/q/1/q/{percent-encoded-query}/n/{limit}',
        'proof_suffix': '/p/{short-lived-signed-OperationRequest}',
        'continuation': '/_r/c/{opaque-cursor}',
        'query_ref': {
            'description': 'Sealed read-only descriptor; never an authorization credential',
            'upload': '/-/g/transfer.open -> transfer.part_put -> transfer.seal',
            'seal': '/-/g/transfer.query_seal/j/{signed-packet}',
            'descriptor': {'version': 1, 'kind': 'read', 'arguments': '{ReadQuery arguments}'},
            'read': '/_r/q/{query_ref}/p/{short-lived-signed-query_get-packet}',
        },
    }


def _code(kind: str, identity: str) -> str:
    # A code depends only on its semantic identity, never registration order.
    raw = hashlib.sha256(f'msg-dictionary-v1:{kind}:{identity}'.encode()).digest()
    return _PREFIX[kind] + base64.b32encode(raw).decode('ascii').lower()[:7]


def _field_type(schema: dict) -> str | list[str] | None:
    return schema.get('type')


def _is_scalar(schema: dict) -> bool:
    kind = _field_type(schema)
    if isinstance(kind, str) and kind in _SCALAR:
        return True
    choices = schema.get('enum', [schema['const']] if 'const' in schema else [])
    return bool(choices) and all(type(value) in {str, int, float, bool} for value in choices)


def _example(schema: dict) -> str:
    if 'enum' in schema:
        return str(schema['enum'][0])
    if 'const' in schema:
        return str(schema['const'])
    match _field_type(schema):
        case 'boolean':
            return 'true'
        case 'integer' | 'number':
            return str(schema.get('minimum', 0))
        case _:
            return 'example'


def _choices(schema: dict) -> list:
    if 'enum' in schema:
        return schema['enum']
    if 'const' in schema:
        return [schema['const']]
    return []


class ShortCodeDictionary:
    def __init__(self, registry, entry: str = 'network', *, published: dict | None = None) -> None:
        require(registry.frozen, 'registry_not_frozen')
        self.registry = registry
        self.entry = entry
        self._by_code: dict[str, dict[str, dict]] = {kind: {} for kind in _PREFIX}
        self._by_identity: dict[str, dict[str, str]] = {kind: {} for kind in _PREFIX}
        self._operations = {}
        operations = []
        namespaces = set()
        specs = sorted(registry.operations(entry), key=lambda spec: (spec.name, spec.version))
        for spec in specs:
            identity = f'{spec.name}@{spec.version}'
            namespace = spec.name.split('.', 1)[0]
            namespaces.add(namespace)
            description = registry.describe(spec)
            replacement = description.get('replaced_by')
            op_code = self._add(
                'operation',
                identity,
                value=identity,
                deprecated=description.get('deprecated', False),
                replaced_by=(_code('operation', replacement) if replacement else None),
            )
            self._operations[op_code] = spec
            schema = registry.schema(spec.input_schema)
            properties = schema.get('properties', {})
            required = list(schema.get('required', []))
            fields = []
            for name, field_schema in properties.items():
                field_identity = f'{identity}:{name}'
                field_code = self._add(
                    'field', field_identity, value=name, operation=identity, path=name
                )
                enums = []
                for value in _choices(field_schema):
                    enum_identity = f'{field_identity}:{canonical(value).decode()}'
                    enum_code = self._add(
                        'enum', enum_identity, value=value, operation=identity, path=name
                    )
                    enums.append({'code': enum_code, 'value': value})
                fields.append({
                    'name': name,
                    'code': field_code,
                    'type': _field_type(field_schema),
                    'required': name in required,
                    'order': required.index(name) if name in required else None,
                    'constraints': {
                        key: value
                        for key, value in field_schema.items()
                        if key not in {'type', 'enum'}
                    },
                    'enum': enums,
                })
            direct = (
                spec.name != 'sharing.link_read'
                and spec.effect == 'read'
                and schema.get('type') == 'object'
                and all(name in properties and _is_scalar(properties[name]) for name in required)
            )
            if spec.name in _SECRET_OPERATIONS:
                template = '/-/p/' + spec.name
                example = None
            elif direct:
                template = '/-/g/' + op_code + ''.join('/{' + name + '}' for name in required)
                example = (
                    '/-/g/'
                    + op_code
                    + ''.join(
                        '/'
                        + quote(
                            self.code_for(
                                'enum',
                                f'{identity}:{name}:'
                                f'{canonical(_choices(properties[name])[0]).decode()}',
                            )
                            if 'enum' in properties[name] or 'const' in properties[name]
                            else _example(properties[name]),
                            safe='',
                        )
                        for name in required
                    )
                )
            else:
                template = '/-/g/' + spec.name + '/j/{packet}'
                example = None
            direct_write_template = None
            direct_expected_template = None
            legacy_direct_write = (
                spec.effect != 'read'
                and schema.get('type') == 'object'
                and all(name in properties and _is_scalar(properties[name]) for name in required)
            )
            # There is no safe bearer-token or bootstrap-claim direct-write
            # grammar. Signed packet GET remains available for non-secret work.
            operations.append({
                'name': spec.name,
                'version': spec.version,
                'code': op_code,
                'namespace': namespace,
                'effect': spec.effect,
                'fields': fields,
                'required': required,
                'shortest_template': template,
                'example': example,
                'direct': direct,
                'requires_secure_channel': spec.name in _SECRET_OPERATIONS,
                'direct_write_template': direct_write_template,
                'direct_expected_template': direct_expected_template,
                'direct_write_status': (
                    'rejected_legacy_secret_url' if legacy_direct_write else None
                ),
            })
        for namespace in sorted(namespaces):
            self._add('namespace', namespace, value=namespace)
        self.document = {
            'version': 1,
            'entry': entry,
            'codes': {
                kind: sorted(values.values(), key=lambda row: row['code'])
                for kind, values in self._by_code.items()
            },
            'operations': operations,
        }
        if published is not None:
            self._preserve_published(published)
        self.etag = '"' + digest(self.document)[7:] + '"'
        self.index_document = {
            'version': 1,
            'entry': entry,
            'namespaces': [
                {
                    'name': namespace,
                    'code': self.code_for('namespace', namespace),
                    'operations': [
                        {'name': row['name'], 'code': row['code'], 'effect': row['effect']}
                        for row in operations
                        if row['namespace'] == namespace
                    ],
                }
                for namespace in sorted(namespaces)
            ],
            'retired': [
                {
                    'kind': kind,
                    'code': row['code'],
                    'identity': row['identity'],
                    'replaced_by': row.get('replaced_by'),
                }
                for kind in _PREFIX
                for row in self.document['codes'][kind]
                if row['deprecated']
            ],
        }
        self.index_etag = '"' + digest(self.index_document)[7:] + '"'
        schema_operations = []
        for spec in specs:
            schema_operations.append({
                **registry.describe(spec),
                'input': registry.schema(spec.input_schema),
                'output': registry.schema(spec.output_schema),
            })
        self.schema_document = {'version': 1, 'entry': entry, 'operations': schema_operations}
        self.schema_etag = '"' + digest(self.schema_document)[7:] + '"'

    def _preserve_published(self, published: dict) -> None:
        """Reject changed meanings and retain retired codes as visible tombstones."""
        require(
            published.get('version') == 1 and published.get('entry') == self.entry,
            'incompatible_published_dictionary',
        )
        old_operations = {row['code']: row for row in published.get('operations', [])}
        new_operations = {row['code']: row for row in self.document['operations']}
        for code, old in old_operations.items():
            if code in new_operations:
                require(
                    canonical(old) == canonical(new_operations[code]), 'published_operation_changed'
                )
        for kind in _PREFIX:
            current = self._by_code[kind]
            for old in published.get('codes', {}).get(kind, []):
                code = old['code']
                if code in current:
                    require(
                        current[code]['identity'] == old['identity'], 'published_short_code_reused'
                    )
                else:
                    retired = {**old, 'deprecated': True}
                    current[code] = retired
                    self.document['codes'][kind].append(retired)
            self.document['codes'][kind].sort(key=lambda row: row['code'])

    def _add(self, kind: str, identity: str, **data) -> str:
        code = _code(kind, identity)
        existing = self._by_code[kind].get(code)
        require(existing is None or existing['identity'] == identity, 'short_code_collision')
        row = {'code': code, 'identity': identity, 'deprecated': False, 'replaced_by': None, **data}
        self._by_code[kind][code] = row
        self._by_identity[kind][identity] = code
        return code

    def resolve(self, kind: str, code: str) -> dict:
        require(kind in _PREFIX, 'unknown_short_code_kind')
        try:
            row = self._by_code[kind][code]
        except KeyError as exc:
            raise Failure('unknown_short_code') from exc
        require(
            not row['deprecated'],
            'deprecated_short_code',
            details={'replaced_by': row.get('replaced_by')},
        )
        return row

    def code_for(self, kind: str, identity: str) -> str:
        require(kind in _PREFIX, 'unknown_short_code_kind')
        try:
            return self._by_identity[kind][identity]
        except KeyError as exc:
            raise Failure('unknown_short_code') from exc

    def resolve_operation(self, code: str):
        self.resolve('operation', code)
        return self._operations[code]

    def lookup_document(self, key: str) -> dict:
        """Return a small namespace or operation dictionary by name or code."""
        require(type(key) is str and bool(key), 'unknown_short_code')
        for kind in ('namespace', 'operation'):
            row = self._by_code[kind].get(key)
            if row is None and kind == 'operation':
                row = self._by_code[kind].get(self._by_identity[kind].get(key))
            if row is not None and row['deprecated']:
                replacement_code = row.get('replaced_by')
                replacement_row = (
                    self._by_code['operation'].get(replacement_code) if replacement_code else None
                )
                return {
                    'version': 1,
                    'entry': self.entry,
                    'scope': key,
                    'kind': kind,
                    'identity': row['identity'],
                    'deprecated': True,
                    'replaced_by': replacement_code,
                    'replacement': (
                        {'code': replacement_code, 'identity': replacement_row['identity']}
                        if replacement_row
                        else None
                    ),
                }
        if key.startswith('n') and key in self._by_code['namespace']:
            namespace = self.resolve('namespace', key)['identity']
            operations = [
                row for row in self.document['operations'] if row['namespace'] == namespace
            ]
        elif key in self._by_identity['namespace']:
            namespace = key
            operations = [
                row for row in self.document['operations'] if row['namespace'] == namespace
            ]
        else:
            if key in self._by_code['operation']:
                code = key
            else:
                identity = key if '@' in key else key + '@1'
                code = self.code_for('operation', identity)
                if '@' not in key and self._by_code['operation'][code]['deprecated']:
                    active = [
                        (spec.version, candidate)
                        for candidate, spec in self._operations.items()
                        if spec.name == key
                        and not self._by_code['operation'][candidate]['deprecated']
                    ]
                    require(active, 'deprecated_short_code')
                    code = max(active)[1]
            self.resolve_operation(code)
            operations = [row for row in self.document['operations'] if row['code'] == code]
            namespace = operations[0]['namespace']
        identities = {f'{row["name"]}@{row["version"]}' for row in operations}
        codes = {
            'namespace': [
                row for row in self.document['codes']['namespace'] if row['identity'] == namespace
            ],
            'operation': [
                row for row in self.document['codes']['operation'] if row['identity'] in identities
            ],
            'field': [
                row for row in self.document['codes']['field'] if row.get('operation') in identities
            ],
            'enum': [
                row for row in self.document['codes']['enum'] if row.get('operation') in identities
            ],
        }
        return {
            'version': 1,
            'entry': self.entry,
            'scope': key,
            'codes': codes,
            'operations': operations,
        }

    def etag_for(self, key: str) -> str:
        return '"' + digest(self.lookup_document(key))[7:] + '"'

    def decode_get_path(self, operation_code: str, segments: list[str] | tuple[str, ...]):
        """Decode a read-only path into (OperationSpec, arguments), without authentication."""
        spec = self.resolve_operation(operation_code)
        require(spec.effect == 'read', 'direct_path_read_only')
        schema = self.registry.schema(spec.input_schema)
        properties = schema.get('properties', {})
        required = list(schema.get('required', []))
        require(
            all(name in properties and _is_scalar(properties[name]) for name in required),
            'direct_path_unavailable',
        )
        require(len(segments) >= len(required), 'missing_path_argument')
        values = {}
        for name, raw in zip(required, segments[: len(required)], strict=True):
            values[name] = self._decode_value(spec, name, raw)
        rest = segments[len(required) :]
        require(len(rest) % 2 == 0, 'invalid_path_arguments')
        for field_code, raw in zip(rest[::2], rest[1::2], strict=True):
            field = self.resolve('field', field_code)
            require(
                field.get('operation') == f'{spec.name}@{spec.version}',
                'field_code_operation_mismatch',
            )
            name = field['path']
            require(
                name not in values and name in properties and _is_scalar(properties[name]),
                'invalid_path_argument',
            )
            values[name] = self._decode_value(spec, name, raw)
        self.registry.validate(spec.input_schema, values)
        return spec, values

    def decode_direct_write_path(
        self,
        operation_code: str,
        segments: list[str] | tuple[str, ...],
    ):
        """Legacy bearer/bootstrap URL grammar is intentionally disabled."""
        raise Failure('secure_channel_required')

    def _decode_value(self, spec, name: str, raw: str):
        require(type(raw) is str, 'invalid_path_argument')
        schema = self.registry.schema(spec.input_schema)['properties'][name]
        if 'enum' in schema or 'const' in schema:
            enum = self.resolve('enum', raw)
            require(
                enum.get('operation') == f'{spec.name}@{spec.version}' and enum.get('path') == name,
                'enum_code_field_mismatch',
            )
            return enum['value']
        kind = schema.get('type')
        if kind == 'string':
            return raw
        if kind == 'boolean':
            require(raw in {'true', 'false'}, 'invalid_path_boolean')
            return raw == 'true'
        if kind == 'integer':
            require(
                raw.isdecimal() and (raw == '0' or not raw.startswith('0')), 'invalid_path_integer'
            )
            return int(raw)
        if kind == 'number':
            try:
                value = float(raw)
            except ValueError as exc:
                raise Failure('invalid_path_number') from exc
            require(value == value and abs(value) != float('inf'), 'invalid_path_number')
            return value
        raise Failure('direct_path_unavailable')


def build_dictionary(
    registry, entry: str = 'network', *, published: dict | None | object = _DEFAULT_PUBLISHED
) -> ShortCodeDictionary:
    if published is _DEFAULT_PUBLISHED:
        try:
            published = loads(files('msg.data').joinpath('shortcodes.json').read_bytes())
        except FileNotFoundError:
            # A source checkout can generate its first publication snapshot.
            published = None
    return ShortCodeDictionary(registry, entry, published=published)
