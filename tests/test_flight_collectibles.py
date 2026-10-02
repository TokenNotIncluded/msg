"""The shared ASCII field is reachable, deterministic, compact and exclusive."""

import base64
import hashlib
import math
import struct

import pytest

from msg.transports.flight_collectibles import (
    COUNT,
    FIELD_RADIUS,
    MAX_ANCHORS,
    MAX_PER_STEP,
    RESPAWN_SECONDS,
    CollectibleField,
    positions,
)

SEED = '0' * 32
ANCHORS = [
    {'id': 'root', 'position': [0, 0, 0], 'radius': 3},
    {'id': 'east', 'position': [120, 0, 0], 'radius': 4},
    {'id': 'north', 'position': [0, 0, 240], 'radius': 2},
]


def taken(state, glyph):
    bitmap = base64.b64decode(state['taken'], validate=True)
    return bool(bitmap[glyph >> 3] & (1 << (glyph & 7)))


def test_positions_match_the_actual_javascript_v5_generator():
    # This digest came from the JS renderer's v5 generator, including every
    # Float32 point, rather than a second Python implementation of the math.
    points = positions(SEED)
    packed = b''.join(struct.pack('<fff', *point) for point in points)
    assert hashlib.sha256(packed).hexdigest() == (
        '259f170fb59c49eb4d39e12d7302e4ccbc38a95c4356a6f804f66878068c6238'
    )
    assert len(points) == COUNT
    assert positions(SEED, 8) == points[:8]
    assert all(math.hypot(*point) <= FIELD_RADIUS + 0.0001 for point in points)
    assert positions('f' * 32) != points


def test_same_world_descriptor_is_compact_and_mirrors_between_clients():
    field = CollectibleField(SEED)
    first = field.descriptor(100)
    second = field.descriptor(100)
    assert first == second
    assert first['seed'] == SEED and first['count'] == COUNT
    assert first['radius'] == 8 and first['fuel'] == 4
    assert first['respawn_ms'] == 45000
    assert len(first['taken']) == 384
    assert base64.b64decode(first['taken']) == bytes(288)
    assert field.snapshot(100) == first


def test_first_claim_wins_and_cooldown_is_reflected_in_every_snapshot():
    field = CollectibleField(SEED, count=1)
    point = field.positions[0]
    assert field.collect(point, point, 100) == [0]
    first_client = field.snapshot(100)
    assert taken(first_client, 0) and first_client['revision'] == 1
    assert field.collect(point, point, 100) == []
    assert field.snapshot(100) == first_client
    assert field.snapshot(100 + RESPAWN_SECONDS - 0.001) == first_client
    respawned = field.snapshot(100 + RESPAWN_SECONDS)
    assert not taken(respawned, 0) and respawned['revision'] == 2
    assert field.collect(point, point, 100 + RESPAWN_SECONDS) == [0]
    assert taken(field.snapshot(100 + RESPAWN_SECONDS), 0)


def test_swept_absorption_catches_a_point_between_two_outside_endpoints():
    field = CollectibleField(SEED, count=1)
    point = field.positions[0]
    start = [point[0] - 5, point[1] + 7, point[2]]
    end = [point[0] + 5, point[1] + 7, point[2]]
    assert math.dist(point, start) > 8 and math.dist(point, end) > 8
    assert field.collect(start, end, 100) == [0]


def test_nearby_is_required_and_a_teleport_is_not_a_collection_sweep():
    field = CollectibleField(SEED, count=1)
    point = field.positions[0]
    outside = [point[0], point[1], point[2] + 8.01]
    assert field.collect(outside, outside, 100) == []
    with pytest.raises(ValueError, match='invalid_collection_step'):
        field.collect([point[0] - 100, point[1], point[2]], point, 100)
    assert not taken(field.snapshot(100), 0)


def test_pickup_includes_the_eight_unit_boundary():
    field = CollectibleField(SEED, count=1)
    point = field.positions[0]
    boundary = [point[0], point[1], point[2] + 8]
    assert field.collect(boundary, boundary, 100) == [0]


def test_highway_snapshots_carry_frozen_geometry_and_a_fresh_mask_after_reset():
    anchors = [
        {'id': anchor['id'], 'position': list(anchor['position']), 'radius': anchor['radius']}
        for anchor in ANCHORS
    ]
    field = CollectibleField(SEED, anchors=anchors)
    points = field.positions
    assert field.collect(points[0], points[0], 100)
    original = field.snapshot(100)
    assert original == field.descriptor(100)
    assert set(original) == {
        'version',
        'layout_version',
        'seed',
        'count',
        'radius',
        'fuel',
        'respawn_ms',
        'anchors',
        'revision',
        'taken',
    }
    assert original['version'] == 2 and original['layout_version'] == 1
    assert original['anchors'] == ANCHORS
    assert original['count'] == COUNT and original['radius'] == 8
    assert original['fuel'] == 4 and original['respawn_ms'] == 45000
    assert len(base64.b64decode(original['taken'], validate=True)) == 288
    assert taken(original, 0)

    anchors[0]['position'][0] = 80
    anchors[1]['id'] = 'changed'
    payload = field.snapshot(100)
    payload['anchors'][0]['position'][0] = 99
    payload['anchors'][1]['id'] = 'returned-value-mutated'
    assert field.positions == points
    assert field.snapshot(100) == original

    changed = CollectibleField('f' * 32, anchors=anchors)
    reset = changed.snapshot(100)
    assert changed.positions != points and reset['seed'] != original['seed']
    assert reset['anchors'] != original['anchors']
    assert reset['revision'] == 0 and not any(base64.b64decode(reset['taken']))
    respawned = field.snapshot(100 + RESPAWN_SECONDS)
    assert respawned['anchors'] == original['anchors']
    assert respawned['seed'] == original['seed']
    assert not any(base64.b64decode(respawned['taken']))


def test_fewer_than_two_anchors_preserves_legacy_geometry_and_can_rebuild_snapshots():
    for anchors in ((), ANCHORS[:1]):
        field = CollectibleField(SEED, anchors=anchors)
        assert field.positions == positions(SEED)
        assert field.snapshot(100) == field.descriptor(100)
        assert set(field.snapshot(100)) == {
            'version',
            'seed',
            'count',
            'radius',
            'fuel',
            'respawn_ms',
            'revision',
            'taken',
        }
        assert field.descriptor(100)['version'] == 1

    previous = CollectibleField(SEED, anchors=ANCHORS)
    previous.collect(previous.positions[0], previous.positions[0], 100)
    rebuilt = CollectibleField('f' * 32)
    state = rebuilt.snapshot(100)
    assert previous.snapshot(100)['version'] == 2 and state['version'] == 1
    assert state['seed'] != previous.seed and state['revision'] == 0
    assert not any(base64.b64decode(state['taken']))


@pytest.mark.parametrize('count', range(6))
def test_small_highway_counts_keep_finite_geometry_and_a_matching_mask(count):
    field = CollectibleField(SEED, count=count, anchors=ANCHORS)
    assert len(field.positions) == count
    assert all(math.isfinite(value) for point in field.positions for value in point)
    state = field.snapshot(100)
    assert state['version'] == 2 and state['count'] == count
    assert len(base64.b64decode(state['taken'], validate=True)) == (count + 7) // 8


def test_four_fifths_of_tokens_follow_the_curved_route_between_two_planets():
    anchors = [
        {'id': 'west', 'position': [-100, 0, 0], 'radius': 3},
        {'id': 'east', 'position': [100, 0, 0], 'radius': 3},
    ]
    points = positions(SEED, count=10, anchors=anchors)
    assert [point[0] for point in points[:8]] == [
        -87.5,
        -62.5,
        -37.5,
        -12.5,
        12.5,
        37.5,
        62.5,
        87.5,
    ]
    assert all(abs(point[1]) <= 1.5 for point in points[:8])
    assert all(abs(point[2]) <= 17.5 for point in points[:8])
    assert abs(points[3][2]) > 10 and abs(points[4][2]) > 10
    assert any(abs(point[1]) > 1.5 for point in points[8:])


def test_prim_ties_use_frozen_input_indexes_and_route_length_weights():
    anchors = [
        {'id': 'z-root', 'position': [0, 0, 0], 'radius': 3},
        {'id': 'a-peer', 'position': [100, 0, 0], 'radius': 3},
        {'id': 'm-peer', 'position': [0, 100, 0], 'radius': 3},
        {'id': 'b-peer', 'position': [100, 100, 0], 'radius': 3},
    ]
    points = positions(SEED, count=10, anchors=anchors)
    # Equal lengths select 0->1, 0->2, then 1->3 by (distance², i, j).
    assert [point[0] for point in points[:3]] == [18.75, 56.25, 93.75]
    assert [point[1] for point in points[3:5]] == [31.25, 68.75]
    assert [point[1] for point in points[5:8]] == [6.25, 43.75, 81.25]
    assert all(abs(point[0] - 100) <= 1.5 for point in points[5:8])
    assert CollectibleField(SEED, anchors=anchors).descriptor(100)['anchors'] == anchors
    assert positions(SEED, count=10, anchors=[anchors[1], anchors[0], *anchors[2:]]) != points

    unequal = [
        {'id': 'root', 'position': [0, 0, 0], 'radius': 3},
        {'id': 'short', 'position': [100, 0, 0], 'radius': 3},
        {'id': 'long', 'position': [0, 0, 200], 'radius': 3},
    ]
    weighted = positions(SEED, count=20, anchors=unequal)
    assert [point[0] for point in weighted[:5]] == [9.375, 28.125, 46.875, 65.625, 84.375]
    assert [point[2] for point in weighted[5:16]] == [
        3.125,
        21.875,
        40.625,
        59.375,
        78.125,
        96.875,
        115.625,
        134.375,
        153.125,
        171.875,
        190.625,
    ]


@pytest.mark.parametrize('horizontal', [0, 1e-9])
def test_vertical_routes_have_a_finite_perpendicular_basis(horizontal):
    anchors = [
        {'id': 'bottom', 'position': [0, 0, 0], 'radius': 3},
        {'id': 'top', 'position': [horizontal, 200, 0], 'radius': 3},
    ]
    points = positions(SEED, count=10, anchors=anchors)
    assert [point[1] for point in points[:8]] == [
        12.5,
        37.5,
        62.5,
        87.5,
        112.5,
        137.5,
        162.5,
        187.5,
    ]
    assert all(math.isfinite(value) for point in points for value in point)
    assert all(abs(point[0]) <= 1.500001 and abs(point[2]) <= 17.5 for point in points[:8])


def test_coincident_planets_and_zero_length_edges_still_make_valid_pickups():
    anchors = [
        {'id': 'first', 'position': [30, -20, 10], 'radius': 3},
        {'id': 'second', 'position': [30, -20, 10], 'radius': 3},
        {'id': 'third', 'position': [30, -20, 10], 'radius': 3},
    ]
    field = CollectibleField(SEED, count=10, anchors=anchors)
    for x, y, z in field.positions[:8]:
        assert x == 30 and abs(y + 20) <= 1.5 and abs(z - 10) <= 1.5
    assert sorted(field.collect(field.positions[0], field.positions[0], 100)) == list(range(8))
    assert field.snapshot(100)['version'] == 2
    assert all(taken(field.snapshot(100), glyph) for glyph in range(8))
    field.clear()
    assert not any(base64.b64decode(field.snapshot(100)['taken']))

    anchors[2]['position'] = [130, -20, 10]
    points = positions(SEED, count=10, anchors=anchors)
    assert [point[0] for point in points[:8]] == [
        36.25,
        48.75,
        61.25,
        73.75,
        86.25,
        98.75,
        111.25,
        123.75,
    ]


def test_highway_field_remains_inside_the_world_and_keeps_the_bounded_mask():
    field = CollectibleField(SEED, anchors=ANCHORS)
    assert len(field.positions) == COUNT
    assert all(math.hypot(*point) <= FIELD_RADIUS + 0.0001 for point in field.positions)
    assert len(field._taken) == 288 and len(field._until) == COUNT
    assert field.positions == positions(SEED, anchors=ANCHORS)


def test_anchor_count_boundary_accepts_sixty_four_public_records():
    anchors = [
        {'id': f'planet-{index}', 'position': [index, 0, 0], 'radius': 1}
        for index in range(MAX_ANCHORS)
    ]
    field = CollectibleField(SEED, count=0, anchors=anchors)
    assert field.positions == ()
    assert field.snapshot(100)['anchors'] == anchors
    assert field.snapshot(100)['taken'] == ''
    with pytest.raises(ValueError, match='invalid_collectible_anchors'):
        CollectibleField(
            SEED, anchors=[*anchors, {'id': 'extra', 'position': [0, 0, 0], 'radius': 1}]
        )


@pytest.mark.parametrize(
    'anchors',
    [
        None,
        {},
        [{'id': 'planet', 'position': [0, 0, 0]}],
        [{'id': 'planet', 'position': [0, 0, 0], 'radius': 3, 'private_home': 'secret'}],
        [{'id': '', 'position': [0, 0, 0], 'radius': 3}],
        [{'id': 'planet', 'position': [0, 0], 'radius': 3}],
        [{'id': 'planet', 'position': [True, 0, 0], 'radius': 3}],
        [{'id': 'planet', 'position': [math.nan, 0, 0], 'radius': 3}],
        [{'id': 'planet', 'position': [401, 0, 0], 'radius': 3}],
        [{'id': 'planet', 'position': [0, 0, 0], 'radius': True}],
        [{'id': 'planet', 'position': [0, 0, 0], 'radius': math.inf}],
        [{'id': 'planet', 'position': [0, 0, 0], 'radius': 0}],
        [{'id': 'planet', 'position': [0, 0, 0], 'radius': 7}],
        [ANCHORS[0], ANCHORS[0]],
    ],
)
def test_anchor_records_reject_noncanonical_or_unbounded_geometry(anchors):
    with pytest.raises(ValueError, match='invalid_collectible_anchors'):
        CollectibleField(SEED, anchors=anchors)


def test_spatial_index_queries_local_cells_instead_of_scanning_all_points():
    field = CollectibleField(SEED)
    largest_query = 0
    for point in field.positions:
        claimed = field.collect(point, point, 100)
        assert len(claimed) <= MAX_PER_STEP
        largest_query = max(largest_query, field.last_candidates)
    assert largest_query < 128
    assert len(field._respawns) <= COUNT
    assert len(field._until) == COUNT and len(field._taken) == 288
    field.clear()
    assert field.snapshot(100)['revision'] == 0
    assert not field._respawns and not any(field._until)
    assert base64.b64decode(field.snapshot(100)['taken']) == bytes(288)


@pytest.mark.parametrize('count', [True, -1, COUNT + 1, 1.5])
def test_field_size_is_bounded(count):
    with pytest.raises(ValueError, match='invalid_collectible_count'):
        CollectibleField(SEED, count=count)
