"""Bounded, authoritative flight-game state, entirely separate from MSG data.

An authenticated transport supplies identities; client inputs supply only controls.
All positions, damage, cooldowns and reconnect state are owned by this RAM world.
"""

from __future__ import annotations

import math
import secrets
import time
from collections import deque
from dataclasses import dataclass, field, replace

from msg.transports.flight_collectibles import CELL_SIZE as COLLECTIBLE_CELL_SIZE, CollectibleField

TICK_SECONDS = 1 / 15
WORLD_RADIUS = 480.0
MAX_EVENTS = 256
EVENT_SECONDS = 8.0
INPUT_SECONDS = 0.5
CRUISE_SPEED = 20.0
MAX_SPEED = 60.0
THRUST_ACCELERATION = 24.0
DASH_ACCELERATION = 96.0
COAST_DRAG = 0.32
BRAKE_ACCELERATION = 48.0
STOP_SPEED = 0.12
FUEL_BURN_RATE = 1.5
FUEL_REGEN_RATE = 6.0
FUEL_EMERGENCY_REGEN_RATE = 2.0
GRAVITY_STRENGTH = 8.0
GRAVITY_SOFTENING = 8.0
GRAVITY_MAX_ACCELERATION = 8.0
SHIP_RADIUS = 1.2
MAX_GRAVITY_WELLS = 256
GRAVITY_CELL_SIZE = 48.0
SURFACE_MARGIN = 0.02
_MASK = 0xFFFFFFFF
_RANDOM = secrets.SystemRandom()


def _seeded(seed):
    """JS code-point FNV-1a and mulberry32, including unsigned 32-bit wrapping."""
    state = 2166136261
    for character in seed:
        state = ((state ^ ord(character)) * 16777619) & _MASK

    def next_value():
        nonlocal state
        state = (state + 0x6D2B79F5) & _MASK
        value = ((state ^ (state >> 15)) * (state | 1)) & _MASK
        value ^= (value + ((value ^ (value >> 7)) * (value | 61))) & _MASK
        return ((value ^ (value >> 14)) & _MASK) / 4294967296

    return next_value


def region_centers():
    """The nineteen public geometry regions; no account data or player list."""
    regions = []
    for region in range(19):
        random = _seeded('region:v3:' + str(region))
        angle = random() * math.tau
        height = (random() * 2 - 1) * 95
        distance = 75 + random() * 125
        regions.append({
            'id': region,
            'name': f'sector-{region:02}',
            'center': [math.cos(angle) * distance, height, math.sin(angle) * distance],
            'radius': 240,
        })
    return regions


def _planet_region(subject_id):
    return 0 if subject_id == 'u_root' else math.floor(_seeded('position:v3:' + subject_id)() * 19)


def planet_position(subject_id):
    """Match the browser's position:v3 geometry, also for non-BMP Unicode IDs."""
    if not isinstance(subject_id, str) or not subject_id:
        raise ValueError('invalid_identity')
    if subject_id == 'u_root':
        return [0.0, 0.0, 0.0]
    random = _seeded('position:v3:' + subject_id)
    region = math.floor(random() * 19)
    center = region_centers()[region]['center']
    azimuth = random() * math.tau
    vertical = random() * 2 - 1
    spread = 18 + random() ** 0.65 * (130 if region % 3 == 0 else 65)
    radial = math.sqrt(1 - vertical * vertical)
    position = [
        center[0] + math.cos(azimuth) * radial * spread,
        center[1] + vertical * spread,
        center[2] + math.sin(azimuth) * radial * spread,
    ]
    length = math.hypot(*position)
    return [value * 38 / max(length, 0.001) for value in position] if length < 38 else position


def _number(value):
    try:
        return type(value) in {int, float} and math.isfinite(value)
    except OverflowError:
        return False


def _vector(value):
    return (
        isinstance(value, (list, tuple))
        and len(value) == 3
        and all(_number(component) for component in value)
    )


def _event_wire(event):
    return {key: list(value) if isinstance(value, list) else value for key, value in event.items()}


def valid_input(packet):
    """Validate a complete input frame independently of its sequence/connection."""
    keys = {'v', 'type', 'seq', 'throttle', 'strafe', 'lift', 'yaw', 'pitch', 'actions'}
    return not (
        not isinstance(packet, dict)
        or not keys <= packet.keys()
        or not packet.keys() <= keys | {'brake'}
        or type(packet['v']) is not int
        or packet['v'] != 1
        or packet['type'] != 'input'
        or type(packet['seq']) is not int
        or not 0 <= packet['seq'] <= 2**53 - 1
        or any(
            not _number(packet[key]) or not -1 <= packet[key] <= 1
            for key in ('throttle', 'strafe', 'lift')
        )
        or not _number(packet['yaw'])
        or not -math.pi <= packet['yaw'] <= math.pi
        or not _number(packet['pitch'])
        or not -math.pi / 2 <= packet['pitch'] <= math.pi / 2
        or not isinstance(packet['actions'], list)
        or len(packet['actions']) > 3
        or any(type(action) is not str for action in packet['actions'])
        or len(set(packet['actions'])) != len(packet['actions'])
        or not set(packet['actions']) <= {'laser', 'shield', 'dash'}
        or type(packet.get('brake', False)) is not bool
    )


def _inertial_segment(velocity, delta, target=None, acceleration=0.0):
    """Integrate bounded linear thrust/braking or exponential free-flight drag."""
    speed = math.hypot(*velocity)
    if speed > MAX_SPEED:
        velocity = [component * MAX_SPEED / speed for component in velocity]
    if target is None:
        factor = math.exp(-COAST_DRAG * delta)
        next_velocity = [component * factor for component in velocity]
        movement = [component * (1 - factor) / COAST_DRAG for component in velocity]
    else:
        difference = [target[axis] - velocity[axis] for axis in range(3)]
        distance = math.hypot(*difference)
        if distance == 0:
            return list(velocity), [component * delta for component in velocity]
        accelerating = min(delta, distance / acceleration)
        blend = min(1.0, acceleration * delta / distance)
        next_velocity = [velocity[axis] + difference[axis] * blend for axis in range(3)]
        weighted = acceleration / distance * accelerating * accelerating / 2
        weighted += max(0.0, delta - accelerating)
        movement = [velocity[axis] * delta + difference[axis] * weighted for axis in range(3)]
    if math.hypot(*next_velocity) <= STOP_SPEED:
        next_velocity = [0.0, 0.0, 0.0]
    return next_velocity, movement


@dataclass(frozen=True, slots=True)
class GravityWell:
    id: str
    position: tuple[float, float, float]
    radius: float

    @property
    def influence(self):
        return max(36.0, self.radius * 8)

    def wire(self):
        return {
            'id': self.id,
            'position': list(self.position),
            'radius': self.radius,
            'influence': self.influence,
        }


def gravity_acceleration(position, wells):
    """Softened local attraction; a bounded sum cannot overpower normal thrust."""
    acceleration = [0.0, 0.0, 0.0]
    for well in wells:
        offset = [well.position[axis] - position[axis] for axis in range(3)]
        distance = math.hypot(*offset)
        if distance < 1e-8 or distance >= well.influence:
            continue
        strength = (
            min(
                GRAVITY_STRENGTH,
                GRAVITY_STRENGTH * (well.radius + GRAVITY_SOFTENING) ** 2 / max(distance**2, 0.01),
            )
            * (1 - distance / well.influence) ** 2
        )
        for axis in range(3):
            acceleration[axis] += offset[axis] / distance * strength
    magnitude = math.hypot(*acceleration)
    if magnitude > GRAVITY_MAX_ACCELERATION:
        acceleration = [value * GRAVITY_MAX_ACCELERATION / magnitude for value in acceleration]
    return acceleration


def planet_contact(start, end, velocity, wells):
    """Sweep to the first surface and preserve tangential/outward escape motion."""
    beginning = list(start)
    for well in wells:
        offset = [beginning[axis] - well.position[axis] for axis in range(3)]
        distance = math.hypot(*offset)
        boundary = well.radius + SHIP_RADIUS + SURFACE_MARGIN
        if distance < boundary:
            normal = [value / distance for value in offset] if distance > 1e-8 else [0.0, 1.0, 0.0]
            correction = [
                well.position[axis] + normal[axis] * boundary - beginning[axis] for axis in range(3)
            ]
            beginning = [beginning[axis] + correction[axis] for axis in range(3)]
            end = [end[axis] + correction[axis] for axis in range(3)]
            inward = sum(velocity[axis] * normal[axis] for axis in range(3))
            if inward < 0:
                velocity = [velocity[axis] - normal[axis] * inward for axis in range(3)]
    movement = [end[axis] - beginning[axis] for axis in range(3)]
    length_squared = sum(value * value for value in movement)
    first = None
    if length_squared > 1e-12:
        for well in wells:
            offset = [beginning[axis] - well.position[axis] for axis in range(3)]
            along = sum(offset[axis] * movement[axis] for axis in range(3))
            boundary = well.radius + SHIP_RADIUS
            discriminant = along**2 - length_squared * (
                sum(value * value for value in offset) - boundary**2
            )
            if along >= 0 or discriminant < 0:
                continue
            fraction = (-along - math.sqrt(discriminant)) / length_squared
            if 0 <= fraction <= 1 and (first is None or fraction < first[0]):
                first = (fraction, well)
    if first is not None:
        fraction, well = first
        touch = [beginning[axis] + movement[axis] * fraction for axis in range(3)]
        offset = [touch[axis] - well.position[axis] for axis in range(3)]
        distance = math.hypot(*offset)
        normal = [value / max(distance, 1e-8) for value in offset]
        end = [
            well.position[axis] + normal[axis] * (well.radius + SHIP_RADIUS + SURFACE_MARGIN)
            for axis in range(3)
        ]
        inward = sum(velocity[axis] * normal[axis] for axis in range(3))
        if inward < 0:
            velocity = [velocity[axis] - normal[axis] * inward for axis in range(3)]
    # Resolve the contact margin too: gravity cannot leave a parked ship sinking
    # a fraction of a tick into the surface. Repeat for nearby overlapping bodies.
    for _ in range(4):
        corrected = False
        for well in wells:
            offset = [end[axis] - well.position[axis] for axis in range(3)]
            distance = math.hypot(*offset)
            boundary = well.radius + SHIP_RADIUS + SURFACE_MARGIN
            if distance >= boundary - 1e-9:
                continue
            normal = [value / distance for value in offset] if distance > 1e-8 else [0.0, 1.0, 0.0]
            end = [well.position[axis] + normal[axis] * boundary for axis in range(3)]
            inward = sum(velocity[axis] * normal[axis] for axis in range(3))
            if inward < 0:
                velocity = [velocity[axis] - normal[axis] * inward for axis in range(3)]
            corrected = True
        if not corrected:
            break
    return end, velocity


@dataclass(slots=True)
class Ship:
    id: str
    subject_id: str | None
    handle: str
    guest: bool
    position: list[float]
    region: int
    public_subject_id: str | None = None
    velocity: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    yaw: float = 0.0
    pitch: float = 0.0
    hp: float = 100.0
    fuel: float = 100.0
    shield_until_ms: int = 0
    laser_ready_ms: int = 0
    shield_ready_ms: int = 0
    dash_ready_ms: int = 0
    dash_until_ms: int = 0
    region_ready_ms: int = 0
    respawn_at_ms: int = 0
    ack_seq: int = -1
    score: int = 0
    collected: int = 0
    _spawn: list[float] = field(default_factory=list, repr=False)
    _spawn_region: int = field(default=0, repr=False)
    _controls: tuple = field(default=(0.0, 0.0, 0.0, False), repr=False)
    _last_input: float = field(default=0.0, repr=False)
    _last_activity: float = field(default=0.0, repr=False)
    _stationary_since: float | None = field(default=None, repr=False)
    _offline_since: float | None = field(default=None, repr=False)
    _shield_until: float = field(default=0.0, repr=False)
    _laser_ready: float = field(default=0.0, repr=False)
    _shield_ready: float = field(default=0.0, repr=False)
    _dash_ready: float = field(default=0.0, repr=False)
    _dash_until: float = field(default=0.0, repr=False)
    _region_ready: float = field(default=0.0, repr=False)
    _respawn_at: float = field(default=0.0, repr=False)

    @property
    def spawn_position(self):
        return list(self._spawn)

    @property
    def spawn_region(self):
        return self._spawn_region

    def wire(self):
        return {
            'id': self.id,
            'subject_id': self.public_subject_id,
            'handle': self.handle,
            'guest': self.guest,
            'position': list(self.position),
            'velocity': list(self.velocity),
            'yaw': self.yaw,
            'pitch': self.pitch,
            'hp': self.hp,
            'fuel': self.fuel,
            'shield_until_ms': self.shield_until_ms,
            'laser_ready_ms': self.laser_ready_ms,
            'shield_ready_ms': self.shield_ready_ms,
            'dash_ready_ms': self.dash_ready_ms,
            'dash_until_ms': self.dash_until_ms,
            'region_ready_ms': self.region_ready_ms,
            'respawn_at_ms': self.respawn_at_ms,
            'region': self.region,
            'ack_seq': self.ack_seq,
            'score': self.score,
            'collected': self.collected,
        }


class FlightWorld:
    def __init__(
        self,
        max_players=96,
        max_retained=512,
        retention_seconds=300,
        clock=time.monotonic,
        collectibles=None,
    ):
        if (
            type(max_players) is not int
            or not 1 <= max_players <= 96
            or type(max_retained) is not int
            or not max_players <= max_retained <= 512
            or not _number(retention_seconds)
            or not 0 <= retention_seconds <= 86400
        ):
            raise ValueError('invalid_world_limits')
        self.max_players = max_players
        self.max_retained = max_retained
        self.retention_seconds = retention_seconds
        self.clock = clock
        self.ships: dict[str, Ship] = {}
        self.active: set[str] = set()
        self._subjects: dict[str, str] = {}
        self._tickets: dict[str, str] = {}
        self._ship_tickets: dict[str, str] = {}
        self._events = deque(maxlen=MAX_EVENTS)
        self._event_id = 0
        self._last_step = float(clock())
        self._simulated_at = self._last_step
        self._remainder = 0.0
        self._epoch_offset_ms = time.time() * 1000 - self._last_step * 1000
        self.tick = 0
        self.regions = region_centers()
        self.collectibles = CollectibleField() if collectibles is None else collectibles
        self.gravity_wells: tuple[GravityWell, ...] = ()
        self._gravity_cells = {}
        self._gravity_ready = False

    def set_gravity_wells(self, records):
        if not isinstance(records, (list, tuple)) or len(records) > MAX_GRAVITY_WELLS:
            raise ValueError('invalid_gravity_wells')
        wells = []
        seen = set()
        for value in records:
            if (
                not isinstance(value, dict)
                or set(value) != {'id', 'position', 'radius'}
                or not isinstance(value['id'], str)
                or not 1 <= len(value['id']) <= 160
                or value['id'] in seen
                or not _vector(value['position'])
                or math.hypot(*value['position']) > 400.000001
                or not _number(value['radius'])
                or not 1 <= value['radius'] <= 6
            ):
                raise ValueError('invalid_gravity_wells')
            seen.add(value['id'])
            wells.append(GravityWell(value['id'], tuple(value['position']), float(value['radius'])))
        cells = {}
        for well in wells:
            key = tuple(math.floor(value / GRAVITY_CELL_SIZE) for value in well.position)
            cells.setdefault(key, []).append(well)
        self.gravity_wells = tuple(wells)
        self._gravity_cells = cells
        self._gravity_ready = True

    def _near_wells(self, ship):
        cell = [math.floor(value / GRAVITY_CELL_SIZE) for value in ship.position]
        wells = []
        for x in range(cell[0] - 1, cell[0] + 2):
            for y in range(cell[1] - 1, cell[1] + 2):
                for z in range(cell[2] - 1, cell[2] + 2):
                    wells.extend(self._gravity_cells.get((x, y, z), ()))
        if self._gravity_ready and ship.subject_id and ship.public_subject_id is None:
            home = list(ship.spawn_position)
            home[1] -= 7
            wells.append(GravityWell('flight_home_' + ship.id, tuple(home), 3.0))
        return wells

    def gravity_descriptor(self):
        return {'version': 1, 'wells': [well.wire() for well in self.gravity_wells]}

    @property
    def active_count(self):
        return len(self.active)

    def clear(self):
        self.ships.clear()
        self.active.clear()
        self._subjects.clear()
        self._tickets.clear()
        self._ship_tickets.clear()
        self._events.clear()
        self._event_id = self.tick = 0
        self._remainder = 0.0
        self._last_step = float(self.clock())
        self._simulated_at = self._last_step
        self.collectibles.clear()
        self.gravity_wells = ()
        self._gravity_cells.clear()
        self._gravity_ready = False

    def _ms(self, now):
        return int(self._epoch_offset_ms + now * 1000)

    def _deadline(self, ship, name, at):
        setattr(ship, '_' + name, at)
        setattr(ship, name + '_ms', self._ms(at))

    def _remove(self, ship_id):
        ship = self.ships.pop(ship_id)
        if ship.subject_id:
            self._subjects.pop(ship.subject_id, None)
        ticket = self._ship_tickets.pop(ship_id)
        self._tickets.pop(ticket, None)

    def _prune(self, now):
        for ship_id, ship in tuple(self.ships.items()):
            if (
                ship_id not in self.active
                and ship._offline_since is not None
                and now - ship._offline_since >= self.retention_seconds
            ):
                self._remove(ship_id)
        while self._events and now - self._events[0][0] > EVENT_SECONDS:
            self._events.popleft()

    def _free_retained_slot(self):
        if len(self.ships) < self.max_retained:
            return
        candidates = [ship for ship in self.ships.values() if ship.id not in self.active]
        if not candidates:
            raise ValueError('world_full')
        self._remove(min(candidates, key=lambda ship: ship._offline_since).id)

    def _near_region(self, region):
        center = self.regions[region]['center']
        for _ in range(24):
            position = [component + _RANDOM.uniform(-24, 24) for component in center]
            position[1] += 7
            if all(
                math.dist(position, self.ships[ship_id].position) >= 7 for ship_id in self.active
            ):
                return position
        # Still bounded when a crowded room prevents a clear random placement.
        return [center[0], center[1] + 45, center[2]]

    def _birthpoint(self, identity, supplied_spawn):
        guest, subject_id = identity['guest'], identity['subject_id']
        region = (
            identity['spawn_region']
            if supplied_spawn
            else _RANDOM.randrange(19)
            if guest
            else _planet_region(subject_id)
        )
        position = (
            self._near_region(region)
            if guest
            else list(identity['spawn_position'])
            if supplied_spawn
            else planet_position(subject_id)
        )
        if not guest:
            position[1] += 7
        return position, region

    def _change_projection(self, old, identity, supplied_spawn, now):
        """A privacy change rotates public identifiers without resetting game state."""
        position, region = self._birthpoint(identity, supplied_spawn)
        ship = replace(
            old,
            id='ship_' + secrets.token_hex(12),
            public_subject_id=identity.get('public_subject_id', identity['subject_id']),
            handle=identity['handle'],
            position=position,
            region=region,
            velocity=[0.0, 0.0, 0.0],
            _spawn=list(position),
            _spawn_region=region,
            _controls=(0.0, 0.0, 0.0, False),
            _last_input=now,
            _last_activity=now,
            _stationary_since=now,
            _offline_since=None,
        )
        self._remove(old.id)
        # Never link an old public ship ID to its replacement through combat events.
        self._events = deque(
            (
                item
                for item in self._events
                if item[2]['player_id'] != old.id and item[2].get('target_id') != old.id
            ),
            maxlen=MAX_EVENTS,
        )
        ticket = secrets.token_urlsafe(32)
        self.ships[ship.id] = ship
        self._subjects[ship.subject_id] = ship.id
        self._ship_tickets[ship.id] = ticket
        self._tickets[ticket] = ship.id
        return ship

    def join(self, identity, resume=None):
        now = float(self.clock())
        self._prune(now)
        required = {'subject_id', 'handle', 'guest'}
        if (
            not isinstance(identity, dict)
            or not required <= identity.keys()
            or not identity.keys()
            <= required | {'spawn_position', 'spawn_region', 'public_subject_id'}
            or type(identity['guest']) is not bool
            or not isinstance(identity['handle'], str)
            or not 1 <= len(identity['handle']) <= 160
            or any(ord(character) < 32 or ord(character) == 127 for character in identity['handle'])
        ):
            raise ValueError('invalid_identity')
        subject_id = identity['subject_id']
        guest = identity['guest']
        public_subject_id = identity.get('public_subject_id', subject_id)
        if (
            guest
            and subject_id is not None
            or not guest
            and (not isinstance(subject_id, str) or not 1 <= len(subject_id) <= 160)
        ):
            raise ValueError('invalid_identity')
        if public_subject_id is not None and public_subject_id != subject_id:
            raise ValueError('invalid_identity')
        supplied_spawn = 'spawn_position' in identity or 'spawn_region' in identity
        if not guest and public_subject_id is None and not supplied_spawn:
            raise ValueError('invalid_spawn')
        if supplied_spawn and (
            guest
            or not _vector(identity.get('spawn_position'))
            or math.hypot(*identity['spawn_position']) > WORLD_RADIUS - 7
            or type(identity.get('spawn_region')) is not int
            or not 0 <= identity['spawn_region'] < 19
        ):
            raise ValueError('invalid_spawn')
        ship_id = self._subjects.get(subject_id) if subject_id else None
        if resume is not None:
            if not isinstance(resume, str) or not 20 <= len(resume) <= 128:
                raise ValueError('invalid_resume')
            resumed_id = self._tickets.get(resume)
            if resumed_id is None or self.ships[resumed_id].subject_id != subject_id:
                raise ValueError('invalid_resume')
            ship_id = resumed_id
        if ship_id in self.active:
            raise ValueError('identity_active')
        if len(self.active) >= self.max_players:
            raise ValueError('world_full')
        if not self.active:
            # Empty worlds have no live physical state to extrapolate during suspension.
            self._last_step = self._simulated_at = now
            self._remainder = 0.0
        if ship_id:
            ship = self.ships[ship_id]
            if ship.public_subject_id != public_subject_id:
                ship = self._change_projection(ship, identity, supplied_spawn, now)
            elif public_subject_id is not None:
                ship.handle = identity['handle']
            ship._offline_since = None
            ship._controls = (0.0, 0.0, 0.0, False)
            ship._last_input = now
            ship._stationary_since = now if not any(ship.velocity) else None
            self.active.add(ship.id)
            self._respawn(ship, now)
            return ship, self._ship_tickets[ship.id]
        self._free_retained_slot()
        position, region = self._birthpoint(identity, supplied_spawn)
        ship = Ship(
            id='ship_' + secrets.token_hex(12),
            subject_id=subject_id,
            handle=identity['handle'],
            guest=guest,
            position=position,
            region=region,
            public_subject_id=public_subject_id,
            _spawn=list(position),
            _spawn_region=region,
            _last_input=now,
            _last_activity=now,
            _stationary_since=now,
        )
        ticket = secrets.token_urlsafe(32)
        self.ships[ship.id] = ship
        self.active.add(ship.id)
        if subject_id:
            self._subjects[subject_id] = ship.id
        self._ship_tickets[ship.id] = ticket
        self._tickets[ticket] = ship.id
        return ship, ticket

    def leave(self, ship_id):
        if ship_id not in self.active:
            return
        self.active.remove(ship_id)
        ship = self.ships[ship_id]
        ship._offline_since = float(self.clock())
        ship._controls = (0.0, 0.0, 0.0, False)
        ship._stationary_since = None

    def input(self, ship_id, packet):
        if ship_id not in self.active or not valid_input(packet):
            return False
        ship = self.ships[ship_id]
        if packet['seq'] <= ship.ack_seq:
            return False
        ship.ack_seq = packet['seq']
        ship.yaw = float(packet['yaw'])
        ship.pitch = float(packet['pitch'])
        ship._controls = (
            float(packet['throttle']),
            float(packet['strafe']),
            float(packet['lift']),
            packet.get('brake', False),
        )
        now = float(self.clock())
        ship._last_input = now
        if ship.hp > 0:
            for action in packet['actions']:
                self._action(ship, action, now)
        return True

    def _event(self, kind, ship, now, **extra):
        self._event_id += 1
        event = {
            'id': f'evt_{self._event_id}',
            'type': kind,
            'player_id': ship.id,
            'position': list(ship.position),
            'region': ship.region,
            'at_ms': self._ms(now),
            **extra,
        }
        self._events.append((now, ship.region, event))
        return event

    @staticmethod
    def _forward(ship):
        horizontal = math.cos(ship.pitch)
        return [
            math.sin(ship.yaw) * horizontal,
            math.sin(ship.pitch),
            -math.cos(ship.yaw) * horizontal,
        ]

    def _action(self, ship, action, now):
        if action == 'shield':
            if now < ship._shield_ready or ship.fuel < 12:
                return
            ship.fuel -= 12
            self._deadline(ship, 'shield_until', now + 2)
            self._deadline(ship, 'shield_ready', now + 8)
        elif action == 'dash':
            if now < ship._dash_ready or ship.fuel < 18:
                return
            ship.fuel -= 18
            self._deadline(ship, 'dash_ready', now + 6)
            self._deadline(ship, 'dash_until', now + 1)
        else:
            if now < ship._laser_ready or ship.fuel < 1:
                return
            ship.fuel -= 1
            self._deadline(ship, 'laser_ready', now + 0.4)
            self._laser(ship, now)
        ship._last_activity = now
        ship._stationary_since = now
        if action != 'laser':
            self._event(action, ship, now)

    def _laser(self, ship, now):
        forward = self._forward(ship)
        nearest, distance = None, 60.0
        for ship_id in self.active:
            target = self.ships[ship_id]
            if ship_id == ship.id or target.hp <= 0:
                continue
            offset = [target.position[axis] - ship.position[axis] for axis in range(3)]
            along = sum(offset[axis] * forward[axis] for axis in range(3))
            perpendicular = max(0.0, sum(value * value for value in offset) - along * along)
            if along <= 0 or perpendicular > 9:
                continue
            contact = max(0.0, along - math.sqrt(9 - perpendicular))
            if contact <= distance:
                nearest, distance = target, contact
        end = [ship.position[axis] + forward[axis] * distance for axis in range(3)]
        self._event('laser', ship, now, end=end, **({'target_id': nearest.id} if nearest else {}))
        if nearest:
            nearest.hp = max(0.0, nearest.hp - (6 if now < nearest._shield_until else 15))
            nearest._last_activity = now
            self._event('hit', nearest, now, target_id=ship.id)
            if nearest.hp <= 0:
                nearest.velocity = [0.0, 0.0, 0.0]
                nearest._controls = (0.0, 0.0, 0.0, False)
                self._deadline(nearest, 'respawn_at', now + 3)
                ship.score += 1
                self._event('death', nearest, now, target_id=ship.id)

    def change_region(self, ship_id, region):
        if ship_id not in self.active or type(region) is not int or not 0 <= region < 19:
            return False
        ship = self.ships[ship_id]
        now = float(self.clock())
        if ship.hp <= 0 or ship.region == region or now < ship._region_ready or ship.fuel < 20:
            return False
        ship.region = region
        ship.position = self._near_region(region)
        ship.velocity = [0.0, 0.0, 0.0]
        ship._controls = (0.0, 0.0, 0.0, False)
        ship.fuel -= 20
        ship._last_activity = now
        ship._stationary_since = now
        self._deadline(ship, 'region_ready', now + 10)
        self._event('region', ship, now)
        return True

    def _respawn(self, ship, now):
        if ship.hp > 0 or now < ship._respawn_at:
            return
        ship.position = list(ship._spawn)
        ship.region = ship._spawn_region
        ship.velocity = [0.0, 0.0, 0.0]
        ship._controls = (0.0, 0.0, 0.0, False)
        ship.hp = ship.fuel = 100.0
        ship._respawn_at = 0.0
        ship.respawn_at_ms = 0
        ship._last_activity = now
        ship._stationary_since = now
        self._event('respawn', ship, now)

    def _advance(self, delta, now):
        for ship_id in self.active:
            ship = self.ships[ship_id]
            self._respawn(ship, now)
            if ship.hp <= 0:
                continue
            old_position = list(ship.position)
            throttle, strafe, lift, brake = ship._controls
            if now - ship._last_input > INPUT_SECONDS:
                throttle = strafe = lift = 0.0
                brake = False
            forward = self._forward(ship)
            right = [math.cos(ship.yaw), 0.0, math.sin(ship.yaw)]
            direction = [
                forward[axis] * throttle + right[axis] * strafe + (lift if axis == 1 else 0)
                for axis in range(3)
            ]
            has_controls = any(abs(component) > 1e-8 for component in (throttle, strafe, lift))
            # Split at the real action boundaries, independently of server uptime.
            start = now - delta
            boundaries = sorted({
                start,
                now,
                *(at for at in (ship._dash_until - 1, ship._dash_until) if start < at < now),
            })
            for begin, end in zip(boundaries, boundaries[1:], strict=False):
                duration = end - begin
                dashing = ship._dash_until - 1 <= (begin + end) / 2 < ship._dash_until
                thrust = direction if has_controls else forward if dashing else None
                if brake:
                    ship.velocity, travel = _inertial_segment(
                        ship.velocity, duration, [0.0, 0.0, 0.0], BRAKE_ACCELERATION
                    )
                    ship.fuel = min(100.0, ship.fuel + FUEL_REGEN_RATE * duration)
                elif thrust is not None and ship.fuel > 0:
                    powered = min(duration, ship.fuel / FUEL_BURN_RATE)
                    magnitude = max(1.0, math.hypot(*thrust))
                    speed = MAX_SPEED if dashing else CRUISE_SPEED
                    target = [component / magnitude * speed for component in thrust]
                    ship.velocity, travel = _inertial_segment(
                        ship.velocity,
                        powered,
                        target,
                        DASH_ACCELERATION if dashing else THRUST_ACCELERATION,
                    )
                    ship.fuel = max(0.0, ship.fuel - powered * FUEL_BURN_RATE)
                    ship._last_activity = now
                    if powered < duration:
                        ship.velocity, coast = _inertial_segment(ship.velocity, duration - powered)
                        travel = [travel[axis] + coast[axis] for axis in range(3)]
                        ship.fuel = min(
                            100.0, ship.fuel + FUEL_EMERGENCY_REGEN_RATE * (duration - powered)
                        )
                else:
                    ship.velocity, travel = _inertial_segment(ship.velocity, duration)
                    rate = FUEL_EMERGENCY_REGEN_RATE if thrust is not None else FUEL_REGEN_RATE
                    ship.fuel = min(100.0, ship.fuel + rate * duration)
                wells = self._near_wells(ship)
                if not brake:
                    gravity = gravity_acceleration(ship.position, wells)
                    travel = [travel[axis] + gravity[axis] * duration**2 / 2 for axis in range(3)]
                    ship.velocity = [
                        ship.velocity[axis] + gravity[axis] * duration for axis in range(3)
                    ]
                    speed = math.hypot(*ship.velocity)
                    if speed > MAX_SPEED:
                        ship.velocity = [value * MAX_SPEED / speed for value in ship.velocity]
                target_position = [ship.position[axis] + travel[axis] for axis in range(3)]
                target_position, ship.velocity = planet_contact(
                    ship.position, target_position, ship.velocity, wells
                )
                ship.position = target_position
            radius = math.hypot(*ship.position)
            if radius > WORLD_RADIUS:
                normal = [value / radius for value in ship.position]
                ship.position = [value * WORLD_RADIUS for value in normal]
                outward = sum(normal[axis] * ship.velocity[axis] for axis in range(3))
                if outward > 0:
                    ship.velocity = [
                        ship.velocity[axis] - normal[axis] * outward for axis in range(3)
                    ]
            if has_controls or now + 1e-9 < ship._dash_until or any(ship.velocity):
                ship._stationary_since = None
            else:
                if ship._stationary_since is None:
                    ship._stationary_since = now
                idle = max(ship._stationary_since, ship._last_activity)
                if now - idle > 2:
                    ship.hp = min(100.0, ship.hp + 3 * delta)
            glyph_ids = (
                self.collectibles.collect(old_position, ship.position, now)
                if math.dist(old_position, ship.position) <= COLLECTIBLE_CELL_SIZE
                else []
            )
            if glyph_ids:
                fuel_added = min(100 - ship.fuel, 4 * len(glyph_ids))
                ship.fuel += fuel_added
                ship.collected += len(glyph_ids)
                self._event(
                    'collect',
                    ship,
                    now,
                    glyph_ids=glyph_ids,
                    fuel_added=fuel_added,
                    collected=ship.collected,
                )

    def step(self, now=None):
        now = float(self.clock()) if now is None else now
        if not _number(now) or now < self._last_step:
            return []
        previous_event = self._event_id
        self._remainder += min(now - self._last_step, 0.25)
        self._last_step = now
        while self._remainder + 1e-12 >= TICK_SECONDS:
            at = now - self._remainder + TICK_SECONDS
            self._advance(TICK_SECONDS, at)
            self._simulated_at = at
            self._remainder = max(0.0, self._remainder - TICK_SECONDS)
            self.tick += 1
        self._prune(now)
        return [
            _event_wire(event)
            for _, _, event in self._events
            if int(event['id'][4:]) > previous_event
        ]

    def snapshot(self, ship_id):
        now = float(self.clock())
        self._prune(now)
        if ship_id not in self.active:
            raise ValueError('player_not_active')
        ship = self.ships[ship_id]
        others = sorted(
            (self.ships[player_id] for player_id in self.active if player_id != ship_id),
            key=lambda other: (math.dist(ship.position, other.position), other.id),
        )
        counts = {str(region): 0 for region in range(19)}
        for player_id in self.active:
            counts[str(self.ships[player_id].region)] += 1
        return {
            'v': 1,
            'type': 'snapshot',
            'tick': self.tick,
            'server_time_ms': self._ms(now),
            'state_time_ms': self._ms(self._simulated_at),
            'self_id': ship_id,
            'region': ship.region,
            'players': [ship.wire(), *(other.wire() for other in others[:95])],
            'events': [_event_wire(event) for _, _, event in list(self._events)[-64:]],
            'total_players': len(self.active),
            'region_counts': counts,
            'collectibles': self.collectibles.descriptor(now, self._ms),
            'gravity': self.gravity_descriptor(),
        }
