"""Server coordinates describe real directed topology, without page dependence."""

import json
import math
from pathlib import Path

from msg.core.planet_layout import fallback_position, planet_layout


def edge(source, target, kind='explicit'):
    return {'source': source, 'target': target, 'source_type': kind}


def test_default_attachments_are_not_explicit_binary_cycles():
    nodes = ['u_root', 'u_a']
    edges = [edge('u_a', 'u_root', 'default'), edge('u_root', 'u_a')]
    layout = planet_layout(nodes, edges)
    assert layout['u_root']['position'] == [0, 0, 0]
    assert layout['u_a']['orbit']['kind'] == 'orbit'
    assert layout['u_a']['orbit']['parent_id'] == 'u_root'
    assert layout['u_a']['orbit']['source_type'] == 'default'
    assert layout['u_a']['orbit']['group_id'] is None
    assert fallback_position('u_a') == layout['u_a']['position']


def test_binary_and_multiple_systems_have_shared_real_centers():
    layout = planet_layout(
        ['u_root', 'u_a', 'u_b'],
        [edge('u_a', 'u_b'), edge('u_b', 'u_a'), edge('u_a', 'u_root', 'default')],
    )
    assert math.isclose(
        math.dist(layout['u_a']['position'], layout['u_b']['position']), 64, abs_tol=1e-5
    )
    assert layout['u_a']['orbit']['kind'] == 'binary'
    assert layout['u_a']['orbit']['group_id'] == layout['u_b']['orbit']['group_id']
    center = layout['u_a']['orbit']['center']
    assert center == layout['u_b']['orbit']['center']
    assert math.isclose(math.dist(layout['u_a']['position'], center), 32, abs_tol=1e-5)
    multiple = planet_layout(
        ['u_a', 'u_b', 'u_c'],
        [edge('u_a', 'u_b'), edge('u_b', 'u_c'), edge('u_c', 'u_a')],
    )
    assert {item['orbit']['kind'] for item in multiple.values()} == {'multi'}
    assert {item['orbit']['group_id'] for item in multiple.values()} == {
        multiple['u_a']['orbit']['group_id']
    }
    assert multiple['u_a']['orbit']['members'] == ['u_a', 'u_b', 'u_c']


def test_root_binary_center_is_translated_while_root_stays_zero():
    layout = planet_layout(['u_root', 'u_a'], [edge('u_root', 'u_a'), edge('u_a', 'u_root')])
    assert layout['u_root']['position'] == [0, 0, 0]
    center = layout['u_a']['orbit']['center']
    assert center != [0, 0, 0]
    assert math.isclose(math.dist(layout['u_a']['position'], center), 32, abs_tol=1e-5)
    assert math.isclose(math.dist(layout['u_root']['position'], center), 32, abs_tol=1e-5)


def test_world_layout_ignores_order_and_unknown_endpoints():
    nodes = ['u_root', 'u_a', 'u_b', 'u_c']
    edges = [edge('u_a', 'u_b'), edge('u_b', 'u_c'), edge('u_c', 'u_root')]
    first = planet_layout(nodes, edges)
    assert first == planet_layout(list(reversed(nodes)), list(reversed(edges)))
    assert first == planet_layout(nodes, [*edges, edge('u_private', 'u_a')])
    for item in first.values():
        assert math.hypot(*item['position']) <= 400.00001
        assert item['version'] == 4
    for source, target in [('u_a', 'u_b'), ('u_b', 'u_c')]:
        assert first[source]['orbit']['center'] == first[target]['position']


def test_shared_client_fixture_matches_authoritative_python_layout():
    fixture = json.loads((Path(__file__).parent / 'fixtures/planet-layout-v4.json').read_text())
    for scenario in fixture['scenarios']:
        assert planet_layout(scenario['nodes'], scenario['edges']) == scenario['layout']


def test_largest_bounded_scc_has_room_for_root_and_certified_planets():
    nodes = ['u_root', *[f'u_{index:03d}' for index in range(255)]]
    layout = planet_layout(
        nodes, [edge(node, nodes[(index + 1) % len(nodes)]) for index, node in enumerate(nodes)]
    )
    closest = min(
        math.dist(layout[first]['position'], layout[second]['position'])
        for index, first in enumerate(nodes)
        for second in nodes[index + 1 :]
    )
    assert closest > 8  # Root radius 5.4 plus a certified planet radius 2.05.
    assert layout['u_root']['position'] == [0, 0, 0]
    assert all(math.hypot(*item['position']) <= 400.00001 for item in layout.values())
