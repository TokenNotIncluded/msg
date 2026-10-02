"""The shared ASCII field is reachable, deterministic, compact and exclusive."""

import base64
import hashlib
import math
import struct

import pytest

from msg.transports.flight_collectibles import (
    COUNT,
    FIELD_RADIUS,
    MAX_PER_STEP,
    RESPAWN_SECONDS,
    CollectibleField,
    positions,
)

SEED = '0' * 32


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
    assert first['radius'] == 4.5 and first['fuel'] == 4
    assert first['respawn_ms'] == 45000
    assert len(first['taken']) == 384
    assert base64.b64decode(first['taken']) == bytes(288)
    assert set(field.snapshot(100)) == {'version', 'revision', 'taken'}


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
    start = [point[0] - 5, point[1], point[2]]
    end = [point[0] + 5, point[1], point[2]]
    assert field.collect(start, end, 100) == [0]


def test_nearby_is_required_and_a_teleport_is_not_a_collection_sweep():
    field = CollectibleField(SEED, count=1)
    point = field.positions[0]
    outside = [point[0], point[1], point[2] + 5]
    assert field.collect(outside, outside, 100) == []
    with pytest.raises(ValueError, match='invalid_collection_step'):
        field.collect([point[0] - 100, point[1], point[2]], point, 100)
    assert not taken(field.snapshot(100), 0)


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
