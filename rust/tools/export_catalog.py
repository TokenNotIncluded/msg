"""Export the Python compatibility reference without opening storage or keys."""

import argparse
import inspect
import json
import unicodedata
from pathlib import Path
from types import SimpleNamespace

from msg.application import Application
from msg.config_contracts import CONFIGURATION_FIELDS
from msg.core.codec import wire
from msg.plugins import BUILTINS
from msg.security.capabilities import BASE_FAMILIES


def alphanumeric_ranges():
    result = []
    for number in range(0x110000):
        if chr(number).isalnum():
            if result and result[-1][1] == number - 1:
                result[-1][1] = number
            else:
                result.append([number, number])
    return result


def catalog():
    app = Application(SimpleNamespace(server=SimpleNamespace(plugins=BUILTINS)))
    registry = app.registry
    operations = []
    schemas = dict(registry._schemas)
    for spec in registry.operations():
        row = registry.describe(spec)
        for ref in (spec.input_schema, spec.output_schema):
            schemas[ref.id] = registry.schema(ref)
        row['handler'] = spec.handler.__module__ + ':' + spec.handler.__qualname__
        row['requirements'] = spec.requirements.__module__ + ':' + spec.requirements.__qualname__
        if spec.requirements.__qualname__ == 'requirement.<locals>.requirements':
            closure = inspect.getclosurevars(spec.requirements).nonlocals
            row['access'] = {key: closure[key] for key in ('argument', 'check', 'default')}
        operations.append(row)
    return {
        'version': 1,
        'plugins': list(BUILTINS),
        'plugin_manifests': {
            name: {
                'dependencies': list(manifest.dependencies),
                'operations': [f'{spec.name}@{spec.version}' for spec in manifest.operations],
            }
            for name, manifest in registry._plugins.items()
        },
        'unicode_version': unicodedata.unidata_version,
        'alphanumeric_ranges': alphanumeric_ranges(),
        'casefold': {
            char: char.casefold()
            for number in range(0x110000)
            if not 0xD800 <= number <= 0xDFFF and (char := chr(number)).casefold() != char
        },
        'operations': operations,
        'schemas': schemas,
        'capabilities': wire(registry.capabilities()),
        'resource_types': wire(registry.resource_types()),
        'base_grants': wire(app.base_grants()),
        'primary_ceiling': wire(app.primary_ceiling()),
        'temporary_ceiling': wire(app.temporary_ceiling()),
        'base_families': sorted(BASE_FAMILIES),
        'configuration': CONFIGURATION_FIELDS,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = catalog()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n'
    )
    from graphql import get_introspection_query, graphql_sync

    from msg.core.packet import REQUEST_SCHEMA, RESULT_SCHEMA
    from msg.transports.dictionary import build_dictionary, read_query_path_document
    from msg.transports.graphql import GraphQLAdapter

    app = Application(SimpleNamespace(server=SimpleNamespace(plugins=BUILTINS)))
    dictionary = build_dictionary(app.registry)
    transport = {
        'request_schema': REQUEST_SCHEMA,
        'result_schema': RESULT_SCHEMA,
        'dictionary': dictionary.document,
        'dictionary_index': dictionary.index_document,
        'dictionary_schema': dictionary.schema_document,
        'read_query_path': read_query_path_document(app.registry),
        'graphql': graphql_sync(
            GraphQLAdapter(app).schema, get_introspection_query()
        ).data,
    }
    args.output.with_name('transport.json').write_text(
        json.dumps(transport, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n'
    )
    print(json.dumps({'operations': len(result['operations']), 'schemas': len(result['schemas'])}))


if __name__ == '__main__':
    main()
