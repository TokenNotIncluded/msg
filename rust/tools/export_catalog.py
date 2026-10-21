"""Export the Python compatibility reference without opening storage or keys."""

import argparse
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

from msg.application import Application
from msg.config_contracts import CONFIGURATION_FIELDS
from msg.core.codec import wire
from msg.plugins import BUILTINS
from msg.security.capabilities import BASE_FAMILIES


def catalog():
    app = Application(SimpleNamespace(server=SimpleNamespace(plugins=BUILTINS)))
    registry = app.registry
    operations = []
    schemas = {}
    for spec in registry.operations():
        row = registry.describe(spec)
        for ref in (spec.input_schema, spec.output_schema):
            schemas[ref.id] = registry.schema(ref)
        row['handler'] = spec.handler.__module__ + ':' + spec.handler.__qualname__
        row['requirements'] = (
            spec.requirements.__module__ + ':' + spec.requirements.__qualname__
        )
        if spec.requirements.__qualname__ == 'requirement.<locals>.requirements':
            closure = inspect.getclosurevars(spec.requirements).nonlocals
            row['access'] = {key: closure[key] for key in ('argument', 'check', 'default')}
        operations.append(row)
    return {
        'version': 1,
        'plugins': list(BUILTINS),
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
    print(json.dumps({'operations': len(result['operations']), 'schemas': len(result['schemas'])}))


if __name__ == '__main__':
    main()
