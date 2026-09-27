"""The CI gate must reject missing, duplicate, changed or failing evidence."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest


spec = importlib.util.spec_from_file_location('ci_shards',
    Path(__file__).resolve().parents[1] / 'scripts' / 'ci_shards.py')
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)
NODES = [f'tests/sub/test_contract.py::test_case[{i}]' for i in range(100)]


def write_evidence(directory, count=4):
    for index in range(count):
        record = ci.evidence(NODES, index, count)
        (directory / f'shard-{index}.json').write_text(json.dumps(record))
        suite = ET.Element('testsuite')
        for node in record['selected']:
            case = ET.SubElement(suite, 'testcase', name=node)
            properties = ET.SubElement(case, 'properties')
            ET.SubElement(properties, 'property', name='msg.nodeid', value=node)
        ET.ElementTree(suite).write(directory / f'tests-{index}.xml')


@pytest.mark.parametrize('count', [1, 2, 4, 16])
def test_partitions_cover_every_node_exactly_once_and_are_order_independent(count):
    selected = [node for index in range(count) for node in ci.partition(NODES, index, count)]
    assert len(selected) == len(set(selected)) == len(NODES)
    assert set(selected) == set(NODES)
    for index in range(count):
        assert set(ci.partition(list(reversed(NODES)), index, count)) == set(ci.partition(NODES, index, count))


@pytest.mark.parametrize('index,count', [(-1, 4), (4, 4), (0, 0), (0, 65)])
def test_invalid_shard_fails_closed(index, count):
    with pytest.raises(ValueError):
        ci.partition(NODES, index, count)


def test_duplicate_node_ids_are_rejected():
    with pytest.raises(ValueError, match='duplicate'):
        ci.partition(NODES + NODES[:1], 0, 4)


def test_valid_complete_evidence_is_accepted(tmp_path):
    write_evidence(tmp_path)
    assert ci.verify(tmp_path, 4) == 100


@pytest.mark.parametrize('damage', ['missing_manifest', 'missing_report', 'duplicate_shard',
                                   'missing_test', 'failure', 'error', 'different_suite'])
def test_gate_rejects_incomplete_or_inconsistent_evidence(tmp_path, damage):
    write_evidence(tmp_path)
    manifest = tmp_path / 'shard-1.json'
    report = tmp_path / 'tests-1.xml'
    if damage == 'missing_manifest':
        manifest.unlink()
    elif damage == 'missing_report':
        report.unlink()
    elif damage == 'duplicate_shard':
        manifest.write_bytes((tmp_path / 'shard-0.json').read_bytes())
    elif damage == 'different_suite':
        manifest.write_text(json.dumps(ci.evidence(NODES + ['new_test'], 1, 4)))
    else:
        tree = ET.parse(report)
        case = tree.getroot().find('testcase')
        if damage == 'missing_test':
            tree.getroot().remove(case)
        else:
            ET.SubElement(case, damage)
        tree.write(report)
    with pytest.raises((ValueError, FileNotFoundError)):
        ci.verify(tmp_path, 4)


@pytest.mark.parametrize('damage', ['renamed_node', 'duplicate_node', 'missing_identity', 'duplicate_identity'])
def test_equal_test_counts_do_not_hide_wrong_execution_identities(tmp_path, damage):
    write_evidence(tmp_path)
    report = tmp_path / 'tests-1.xml'
    tree = ET.parse(report)
    cases = tree.getroot().findall('testcase')
    if damage == 'renamed_node':
        cases[0].find('./properties/property').set('value', 'tests/test_other.py::wrong')
    elif damage == 'duplicate_node':
        cases[0].find('./properties/property').set('value',
            cases[1].find('./properties/property').get('value'))
    elif damage == 'missing_identity':
        cases[0].remove(cases[0].find('properties'))
    else:
        ET.SubElement(cases[0].find('properties'), 'property', name='msg.nodeid', value='extra')
    tree.write(report)
    with pytest.raises(ValueError):
        ci.verify(tmp_path, 4)


def test_real_pytest_reports_exact_nodeids_for_parameters_and_classes(tmp_path):
    tests = tmp_path / 'tests'
    tests.mkdir()
    (tests / 'test_sample.py').write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('value', ['normal', '::[x]', '中文'])\n"
        "def test_parameter(value):\n    assert value\n"
        "class TestNested:\n    def test_child(self):\n        assert True\n")
    directory = tmp_path / 'reports'
    completed = subprocess.run(
        [sys.executable, ci.__file__, 'run', '0', '1', '--directory', str(directory)],
        cwd=tmp_path, env={**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'},
        capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert ci.verify(directory, 1) == 4
    manifest = json.loads((directory / 'shard-0.json').read_text())
    tree = ET.parse(directory / 'tests-0.xml')
    observed = [case.find('./properties/property[@name="msg.nodeid"]').get('value')
                for case in tree.getroot().findall('.//testcase')]
    assert sorted(observed) == manifest['selected']
