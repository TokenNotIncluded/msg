"""Run disjoint pytest partitions and verify their complete evidence at the CI gate.

Every runner collects the entire suite. Partitioning is by stable node ID, not a
hand-maintained filename list, so new/parameterized/nested tests cannot disappear.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def partition(nodeids, index, count):
    if not 1 <= count <= 64 or not 0 <= index < count:
        raise ValueError('invalid shard coordinates')
    if len(nodeids) != len(set(nodeids)):
        raise ValueError('duplicate test node ID')
    return [node for node in nodeids
            if int.from_bytes(hashlib.sha256(node.encode()).digest()[:8], 'big') % count == index]


def evidence(nodeids, index, count):
    all_nodes = sorted(nodeids)
    return {'version': 2, 'index': index, 'count': count, 'all': all_nodes,
            'selected': partition(all_nodes, index, count)}


def verify(directory, count):
    directory = Path(directory)
    expected = None
    seen = set()
    for index in range(count):
        manifest = json.loads((directory / f'shard-{index}.json').read_text())
        if manifest != evidence(manifest['all'], index, count):
            raise ValueError('invalid shard manifest')
        if expected is None:
            expected = set(manifest['all'])
        elif expected != set(manifest['all']):
            raise ValueError('runners collected different test suites')
        selected = set(manifest['selected'])
        if seen & selected:
            raise ValueError('test executed in multiple shards')
        seen.update(selected)
        root = ET.parse(directory / f'tests-{index}.xml').getroot()
        cases = root.findall('.//testcase')
        if len(cases) != len(selected) or root.findall('.//error') or root.findall('.//failure'):
            raise ValueError('JUnit result is incomplete or failed')
        observed = []
        for case in cases:
            identities = case.findall('./properties/property[@name="msg.nodeid"]')
            if len(identities) != 1 or identities[0].get('value') is None:
                raise ValueError('JUnit test identity is missing or ambiguous')
            observed.append(identities[0].get('value'))
        if sorted(observed) != sorted(selected):
            raise ValueError('JUnit test identities do not match the selected shard')
    if not expected or seen != expected:
        raise ValueError('test coverage is incomplete')
    return len(seen)


def run(index, count, directory):
    import pytest

    partition([], index, count)  # Validate before starting tests or writing files.
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    class Collector:
        def pytest_collection_modifyitems(self, config, items):
            manifest = evidence([item.nodeid for item in items], index, count)
            chosen = set(manifest['selected'])
            rejected = [item for item in items if item.nodeid not in chosen]
            items[:] = [item for item in items if item.nodeid in chosen]
            for item in items:
                # Preserve the exact pytest identity, including class/parameter
                # punctuation, instead of reverse-engineering JUnit display names.
                item.user_properties.append(('msg.nodeid', item.nodeid))
            config.hook.pytest_deselected(items=rejected)
            (directory / f'shard-{index}.json').write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')

    return pytest.main(['tests', f'--junitxml={directory / f"tests-{index}.xml"}'],
                       plugins=[Collector()])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    execute = commands.add_parser('run')
    execute.add_argument('index', type=int)
    execute.add_argument('count', type=int)
    check = commands.add_parser('check')
    check.add_argument('count', type=int)
    for command in (execute, check):
        command.add_argument('--directory', type=Path, default=Path('artifacts'))
    args = parser.parse_args()
    if args.command == 'run':
        return run(args.index, args.count, args.directory)
    print(f'Complete non-overlapping coverage: {verify(args.directory, args.count)} tests')
    return 0


if __name__ == '__main__':
    sys.exit(main())
