"""Shared procedural ASCII pickups, indexed and claimed only by the RAM world."""

from __future__ import annotations

import base64
import heapq
import math
import secrets
import struct
from collections import defaultdict

COUNT = 2300
RADIUS = 8.0
FUEL = 4.0
RESPAWN_SECONDS = 45.0
CELL_SIZE = 16.0
MAX_PER_STEP = 32
FIELD_RADIUS = 475.0
MAX_ANCHORS = 64
_MASK = 0xFFFFFFFF


def _random(seed):
    state = 2166136261
    for character in seed:
        state = ((state ^ ord(character)) * 16777619) & _MASK

    def value():
        nonlocal state
        state = (state + 0x6D2B79F5) & _MASK
        mixed = ((state ^ (state >> 15)) * (state | 1)) & _MASK
        mixed ^= (mixed + ((mixed ^ (mixed >> 7)) * (mixed | 61))) & _MASK
        return ((mixed ^ (mixed >> 14)) & _MASK) / 4294967296

    return value


def _float32(value):
    return struct.unpack('!f', struct.pack('!f', value))[0]


def _freeze_anchors(anchors):
    if not isinstance(anchors, (list, tuple)) or len(anchors) > MAX_ANCHORS:
        raise ValueError('invalid_collectible_anchors')
    result, seen = [], set()
    for anchor in anchors:
        if (
            not isinstance(anchor, dict)
            or set(anchor) != {'id', 'position', 'radius'}
            or not isinstance(anchor['id'], str)
            or not 1 <= len(anchor['id']) <= 160
            or anchor['id'] in seen
            or not isinstance(anchor['position'], (list, tuple))
            or len(anchor['position']) != 3
            or any(
                type(value) not in {int, float} or not math.isfinite(value)
                for value in anchor['position']
            )
            or math.hypot(*anchor['position']) > 400.000001
            or type(anchor['radius']) not in {int, float}
            or not math.isfinite(anchor['radius'])
            or not 1 <= anchor['radius'] <= 6
        ):
            raise ValueError('invalid_collectible_anchors')
        seen.add(anchor['id'])
        result.append((
            anchor['id'],
            tuple(float(v) for v in anchor['position']),
            float(anchor['radius']),
        ))
    return tuple(result)


def _cross(first, second):
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )


def _length(vector):
    return math.sqrt(vector[0] * vector[0] + vector[1] * vector[1] + vector[2] * vector[2])


def _lane_edges(seed, anchors):
    """Prim's ordered tree is part of the shared Python/JS layout contract."""
    visited, edges = {0}, []
    while len(visited) < len(anchors):
        candidates = []
        for first in visited:
            for second in range(len(anchors)):
                if second in visited:
                    continue
                delta = tuple(
                    anchors[second][1][axis] - anchors[first][1][axis] for axis in range(3)
                )
                squared = delta[0] * delta[0] + delta[1] * delta[1] + delta[2] * delta[2]
                candidates.append((squared, first, second))
        squared, first, second = min(candidates)
        visited.add(second)
        start = anchors[first][1]
        delta = tuple(anchors[second][1][axis] - start[axis] for axis in range(3))
        length = math.sqrt(squared)
        direction = tuple(value / length for value in delta) if length else (1.0, 0.0, 0.0)
        normal = _cross(direction, (0.0, 1.0, 0.0))
        normal_length = _length(normal)
        if normal_length <= 1e-6:
            normal = _cross(direction, (1.0, 0.0, 0.0))
            normal_length = _length(normal)
        normal = tuple(value / normal_length for value in normal)
        binormal = _cross(direction, normal)
        sign = (
            1
            if _random(f'token-lane:v1:{seed}:{anchors[first][0]}:{anchors[second][0]}')() < 0.5
            else -1
        )
        edges.append((length, start, delta, normal, binormal, min(18.0, length * 0.08) * sign))
    return tuple(edges)


def _ambient_point(random, glyph, phases):
    # Keep the v5 loop and its RNG consumption, also for the v6 ambient suffix.
    fraction, band = random(), glyph % 3
    phase = phases[band]
    point = [
        (fraction - 0.5) * 940,
        math.sin(fraction * 8 + phase) * (35 + band * 30) + (band - 1) * 44,
        math.cos(fraction * 6 + phase) * 115 + (band - 1) * 160,
    ]
    width = 5 + 22 * (1 + math.sin(fraction * 13 + phase))
    for axis in range(3):
        point[axis] += (random() + random() + random() - 1.5) * width
    if glyph % 5 == 0:
        point = [(random() - 0.5) * (750 if axis == 1 else 1500) for axis in range(3)]
    return point


def _positions(seed, count, anchors):
    if not isinstance(seed, str) or not seed or len(seed) > 128:
        raise ValueError('invalid_collectible_seed')
    if type(count) is not int or not 0 <= count <= COUNT:
        raise ValueError('invalid_collectible_count')
    highways = len(anchors) >= 2
    random = _random(('token-highways:v6:' if highways else 'token-ribbons:v5:') + seed)
    phases = [random() * math.tau for _ in range(3)]
    edges = _lane_edges(seed, anchors) if highways else ()
    total = 0.0
    for edge in edges:
        total += edge[0]
    lane_count = count * 4 // 5 if highways else 0
    result = []
    for glyph in range(count):
        if glyph < lane_count:
            remaining = (glyph + 0.5) / lane_count * total
            edge = edges[0]
            if total:
                for candidate in edges:
                    length = candidate[0]
                    if length and remaining <= length:
                        edge = candidate
                        break
                    remaining -= length
            length, start, delta, normal, binormal, bend = edge
            along = max(0.0, min(1.0, remaining / length)) if length else 0.0
            curve = math.sin(math.pi * along) * bend
            jitter_normal, jitter_binormal = (random() - 0.5) * 3, (random() - 0.5) * 3
            point = [
                start[axis]
                + delta[axis] * along
                + normal[axis] * curve
                + normal[axis] * jitter_normal
                + binormal[axis] * jitter_binormal
                for axis in range(3)
            ]
        else:
            point = _ambient_point(random, glyph, phases)
        length = math.hypot(*point)
        if length > FIELD_RADIUS:
            point = [component * FIELD_RADIUS / length for component in point]
        # The renderer consumes these values for alpha/size after each point.
        random()
        random()
        result.append(tuple(_float32(component) for component in point))
    return tuple(result)


def positions(seed, count=COUNT, anchors=()):
    """Legacy v5 ribbons or v6 public-planet lanes, stored as Float32 points."""
    return _positions(seed, count, _freeze_anchors(anchors))


def _segment_distance_squared(point, start, end):
    movement = [end[axis] - start[axis] for axis in range(3)]
    length = sum(component * component for component in movement)
    along = (
        max(
            0.0,
            min(
                1.0,
                sum((point[axis] - start[axis]) * movement[axis] for axis in range(3)) / length,
            ),
        )
        if length
        else 0.0
    )
    return sum((point[axis] - start[axis] - movement[axis] * along) ** 2 for axis in range(3))


class CollectibleField:
    """Fixed geometry, 288-byte availability mask, and at most 2300 timers."""

    def __init__(self, seed=None, count=COUNT, anchors=()):
        self.seed = secrets.token_hex(16) if seed is None else seed
        self._anchors = _freeze_anchors(anchors)
        self.positions = _positions(self.seed, count, self._anchors)
        self.count = count
        self.revision = 0
        self._taken = bytearray((count + 7) // 8)
        self._until = [0.0] * count
        self._respawns = []
        self._cells = defaultdict(list)
        self.last_candidates = 0
        for glyph, point in enumerate(self.positions):
            self._cells[tuple(math.floor(value / CELL_SIZE) for value in point)].append(glyph)

    def clear(self):
        self._taken[:] = bytes(len(self._taken))
        self._until[:] = [0.0] * self.count
        self._respawns.clear()
        self.revision = 0
        self.last_candidates = 0

    def refresh(self, now):
        while self._respawns and self._respawns[0][0] <= now:
            until, glyph = heapq.heappop(self._respawns)
            if self._until[glyph] == until:
                self._until[glyph] = 0.0
                self._taken[glyph >> 3] &= ~(1 << (glyph & 7))
                self.revision += 1

    def snapshot(self, now, ms=None):
        self.refresh(now)
        state = {
            'version': 2 if len(self._anchors) >= 2 else 1,
            'seed': self.seed,
            'count': self.count,
            'radius': RADIUS,
            'fuel': FUEL,
            'respawn_ms': int(RESPAWN_SECONDS * 1000),
            'revision': self.revision,
            'taken': base64.b64encode(self._taken).decode('ascii'),
        }
        if state['version'] == 2:
            state.update(
                layout_version=1,
                anchors=[
                    {'id': rid, 'position': list(position), 'radius': radius}
                    for rid, position, radius in self._anchors
                ],
            )
        return state

    def descriptor(self, now, ms=None):
        return self.snapshot(now, ms)

    def collect(self, start, end, now, maximum=MAX_PER_STEP):
        """Sweep a single legal physics tick; relocation never supplies a sweep."""
        if (
            type(maximum) is not int
            or not 1 <= maximum <= MAX_PER_STEP
            or not math.isfinite(now)
            or any(
                len(point) != 3 or any(not math.isfinite(v) for v in point)
                for point in (start, end)
            )
            or math.dist(start, end) > CELL_SIZE
        ):
            raise ValueError('invalid_collection_step')
        self.refresh(now)
        bounds = [
            (
                math.floor((min(start[axis], end[axis]) - RADIUS) / CELL_SIZE),
                math.floor((max(start[axis], end[axis]) + RADIUS) / CELL_SIZE),
            )
            for axis in range(3)
        ]
        candidates = []
        for x in range(bounds[0][0], bounds[0][1] + 1):
            for y in range(bounds[1][0], bounds[1][1] + 1):
                for z in range(bounds[2][0], bounds[2][1] + 1):
                    candidates.extend(self._cells.get((x, y, z), ()))
        self.last_candidates = len(candidates)
        nearby = []
        for glyph in candidates:
            if self._until[glyph]:
                continue
            distance = _segment_distance_squared(self.positions[glyph], start, end)
            if distance <= RADIUS * RADIUS:
                nearby.append((distance, glyph))
        claimed = []
        for _, glyph in sorted(nearby)[:maximum]:
            until = now + RESPAWN_SECONDS
            self._until[glyph] = until
            self._taken[glyph >> 3] |= 1 << (glyph & 7)
            heapq.heappush(self._respawns, (until, glyph))
            self.revision += 1
            claimed.append(glyph)
        return claimed
