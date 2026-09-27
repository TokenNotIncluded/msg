"""Run complete, disjoint pytest partitions and verify each executed node ID.

Adapted from PR #104. Every runner collects the complete suite; the gate rejects
skips, duplicate IDs, count-only substitutions and missing or failed evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def partition(nodeids, index, count):
    if not 1 <= count <= 64 or not 0 <= index < count:
        raise ValueError('invalid shard coordinates')
    if len(nodeids) != len(set(nodeids)):
        raise ValueError('duplicate test node ID')
    return [node for node in nodeids
            if int.from_bytes(hashlib.sha256(node.encode()).digest()[:8], 'big') % count == index]


def evidence(nodeids, index, count):
    all_nodes = sorted(nodeids)
    return {'version': 1, 'index': index, 'count': count, 'all': all_nodes,
            'selected': partition(all_nodes, index, count)}


def report_nodes(path):
    root = ET.parse(path).getroot()
    if any(root.findall('.//' + tag) for tag in ('error', 'failure', 'skipped')):
        raise ValueError('JUnit result contains failed or skipped tests')
    nodes = []
    for case in root.findall('.//testcase'):
        ids = [p.get('value') for p in case.findall('./properties/property')
               if p.get('name') == 'msg_nodeid']
        if len(ids) != 1 or not ids[0]:
            raise ValueError('JUnit result is missing an unambiguous test node ID')
        nodes.append(ids[0])
    if len(nodes) != len(set(nodes)):
        raise ValueError('JUnit result contains duplicate test node IDs')
    return set(nodes)


def verify(directory, count):
    partition([], 0, count)
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
        if report_nodes(directory / f'tests-{index}.xml') != selected:
            raise ValueError('JUnit result does not match selected test node IDs')
    if not expected or seen != expected:
        raise ValueError('test coverage is incomplete')
    return len(seen)


def run(index, count, directory):
    import pytest

    partition([], index, count)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    class Collector:
        def pytest_collection_modifyitems(self, config, items):
            manifest = evidence([item.nodeid for item in items], index, count)
            chosen = set(manifest['selected'])
            rejected = [item for item in items if item.nodeid not in chosen]
            items[:] = [item for item in items if item.nodeid in chosen]
            for item in items:
                item.user_properties.append(('msg_nodeid', item.nodeid))
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
