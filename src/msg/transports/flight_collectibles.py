"""Shared procedural ASCII pickups, indexed and claimed only by the RAM world."""

from __future__ import annotations

import base64
import heapq
import math
import secrets
import struct
from collections import defaultdict

COUNT = 2300
RADIUS = 4.5
FUEL = 4.0
RESPAWN_SECONDS = 45.0
CELL_SIZE = 16.0
MAX_PER_STEP = 32
FIELD_RADIUS = 475.0
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


def positions(seed, count=COUNT):
    """Match token-ribbons:v5 and its Float32 storage in the browser renderer.

    The v4 ribbons and RNG consumption are retained. Outlying decoration is
    folded inside the reachable world so every generated glyph can be picked up.
    """
    if not isinstance(seed, str) or not seed or len(seed) > 128:
        raise ValueError('invalid_collectible_seed')
    if type(count) is not int or not 0 <= count <= COUNT:
        raise ValueError('invalid_collectible_count')
    random = _random('token-ribbons:v5:' + seed)
    phases = [random() * math.tau for _ in range(3)]
    result = []
    for glyph in range(count):
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
        length = math.hypot(*point)
        if length > FIELD_RADIUS:
            point = [component * FIELD_RADIUS / length for component in point]
        # The renderer consumes these values for alpha/size after each point.
        random()
        random()
        result.append(tuple(_float32(component) for component in point))
    return tuple(result)


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

    def __init__(self, seed=None, count=COUNT):
        self.seed = secrets.token_hex(16) if seed is None else seed
        self.positions = positions(self.seed, count)
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
        return {
            'version': 1,
            'revision': self.revision,
            'taken': base64.b64encode(self._taken).decode('ascii'),
        }

    def descriptor(self, now, ms=None):
        return {
            **self.snapshot(now),
            'seed': self.seed,
            'count': self.count,
            'radius': RADIUS,
            'fuel': FUEL,
            'respawn_ms': int(RESPAWN_SECONDS * 1000),
        }

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
