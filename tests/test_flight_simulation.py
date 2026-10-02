"""Game rules, adversarial inputs, reconnects and the browser geometry contract."""

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from msg.transports.flight_collectibles import CollectibleField
from msg.transports.flight_simulation import (
    FlightWorld,
    planet_position,
    region_centers,
    valid_input,
)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def identity(subject='u_pilot', **extra):
    return {'subject_id': subject, 'handle': '@' + subject, 'guest': False, **extra}


def packet(seq=0, **extra):
    return {
        'v': 1,
        'type': 'input',
        'seq': seq,
        'throttle': 0,
        'strafe': 0,
        'lift': 0,
        'yaw': 0,
        'pitch': 0,
        'actions': [],
        **extra,
    }


@pytest.fixture
def world():
    return FlightWorld(
        clock=Clock(), collectibles=CollectibleField(seed='simulation-rules', count=0)
    )


def advance(world, seconds, ship=None, **controls):
    for _ in range(round(seconds * 15)):
        world.clock.now += 1 / 15
        if ship is not None:
            assert world.input(ship.id, packet(ship.ack_seq + 1, **controls))
        world.step()


def combat(world):
    attacker, _ = world.join(identity('u_attacker', spawn_position=[0, 0, 0], spawn_region=0))
    target, _ = world.join(identity('u_target', spawn_position=[0, 0, -20], spawn_region=1))
    return attacker, target


def test_signed_spawn_matches_planet_and_server_layout_takes_precedence(world):
    ship, _ = world.join(identity())
    assert ship.position == [
        planet_position('u_pilot')[0],
        planet_position('u_pilot')[1] + 7,
        planet_position('u_pilot')[2],
    ]
    root, _ = world.join(identity('u_root'))
    assert root.position == [0, 7, 0]
    custom, _ = world.join(identity('u_custom', spawn_position=[120, -30, 60], spawn_region=7))
    assert custom.position == [120, -23, 60]
    assert custom.region == 7
    assert len(region_centers()) == 19
    assert all(region['id'] == index for index, region in enumerate(region_centers()))


@pytest.mark.parametrize(
    'extra',
    [
        {'spawn_position': [0, 0, 0]},
        {'spawn_region': 1},
        {'spawn_position': [math.nan, 0, 0], 'spawn_region': 1},
        {'spawn_position': [474, 0, 0], 'spawn_region': 1},
        {'spawn_position': [0, 0, 0], 'spawn_region': True},
        {'spawn_position': [0, 0, 0], 'spawn_region': 19},
    ],
)
def test_server_spawn_is_bounded_and_requires_both_fields(world, extra):
    with pytest.raises(ValueError, match='^invalid_spawn$'):
        world.join(identity(**extra))


def test_js_position_is_exact_for_unicode_and_many_region_seeds():
    node = shutil.which('node')
    if not node:
        pytest.skip('geometry comparison requires Node.js')
    module = Path(__file__).parents[1] / 'src/msg/data/root-web-model.js'
    ids = ['u_root', 'u_pilot', 'u_汉字🙂', 'u_𐐀🌕'] + [f'u_geometry_{i}' for i in range(100)]
    source = (
        'require(process.argv[1]);'
        'console.log(JSON.stringify(JSON.parse(process.argv[2]).map(MSGUniverse.position)));'
    )
    result = subprocess.run(
        [node, '-e', source, str(module), json.dumps(ids)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    for subject, expected in zip(ids, json.loads(result.stdout), strict=True):
        assert planet_position(subject) == pytest.approx(expected, abs=2e-12)


def test_guests_have_random_safe_spawn_and_secret_resume(world):
    guest = {'subject_id': None, 'handle': 'guest', 'guest': True}
    first, ticket = world.join(guest)
    second, other_ticket = world.join(guest)
    assert ticket != other_ticket and len(ticket) >= 40
    assert first.id != second.id and first.position != second.position
    assert 0 <= first.region < 19 and math.hypot(*first.position) < 480
    with pytest.raises(ValueError, match='^identity_active$'):
        world.join(guest, ticket)
    world.leave(first.id)
    first.hp, first.fuel = 47, 32
    first.position = [1, 2, 3]
    resumed, same_ticket = world.join(guest, ticket)
    assert resumed is first and same_ticket == ticket
    assert (resumed.hp, resumed.fuel, resumed.position) == (47, 32, [1, 2, 3])
    with pytest.raises(ValueError, match='^invalid_resume$'):
        world.join(identity('u_impersonator'), other_ticket)


def test_signed_reconnect_preserves_location_health_cooldowns_and_sequence(world):
    ship, ticket = world.join(identity())
    assert world.input(ship.id, packet(10, actions=['shield', 'dash', 'laser']))
    deadlines = (ship.shield_ready_ms, ship.dash_ready_ms, ship.laser_ready_ms)
    ship.hp = 27
    ship.position = [3, 4, 5]
    with pytest.raises(ValueError, match='^identity_active$'):
        world.join(identity())
    world.leave(ship.id)
    world.clock.now += 1
    rejoined, same_ticket = world.join(identity())
    assert rejoined is ship and same_ticket == ticket
    assert rejoined.hp == 27 and rejoined.fuel == 69
    assert rejoined.position == [3, 4, 5]
    assert deadlines == (ship.shield_ready_ms, ship.dash_ready_ms, ship.laser_ready_ms)
    assert rejoined.ack_seq == 10
    assert not world.input(ship.id, packet(10, actions=['laser']))


@pytest.mark.parametrize(
    'extra',
    [
        {'position': [0, 0, 0]},
        {'hp': 100},
        {'v': True},
        {'type': 'region'},
        {'seq': True},
        {'seq': -1},
        {'seq': 2**53},
        {'throttle': True},
        {'throttle': 2},
        {'yaw': 4},
        {'pitch': math.pi},
        {'yaw': float('nan')},
        {'lift': float('inf')},
        {'yaw': 10**1000},
        {'actions': ['laser', 'laser']},
        {'actions': [['laser']]},
        {'actions': ['teleport']},
        {'actions': 'laser'},
        {'brake': 1},
    ],
)
def test_untrusted_input_cannot_change_state_or_consume_sequence(world, extra):
    ship, _ = world.join(identity())
    before = ship.wire()
    assert not valid_input(packet(**extra))
    assert not world.input(ship.id, packet(**extra))
    assert ship.wire() == before
    assert world.input(ship.id, packet())


def test_incomplete_input_and_replays_are_rejected_without_reexecuting_actions(world):
    ship, _ = world.join(identity())
    incomplete = packet()
    del incomplete['yaw']
    assert not world.input(ship.id, incomplete)
    attack = packet(4, actions=['laser'])
    assert valid_input(attack)
    assert world.input(ship.id, attack)
    assert ship.fuel == 99
    world.clock.now += 1
    assert not world.input(ship.id, attack)
    assert valid_input(attack)
    assert ship.fuel == 99 and len(world.snapshot(ship.id)['events']) == 1
    assert world.input(ship.id, packet(5, actions=['laser']))
    assert ship.fuel == 98


def test_duplicate_sequence_is_harmless_only_for_an_otherwise_valid_frame(world):
    ship, _ = world.join(identity())
    assert world.input(ship.id, packet(8))
    assert not world.input(ship.id, packet(8)) and valid_input(packet(8))
    assert not world.input(ship.id, packet(7)) and valid_input(packet(7))
    assert not valid_input(packet(-1, position=[9000, 9000, 9000]))
    assert not valid_input(packet(7, hp=100))
    assert not valid_input(packet(7, throttle=math.nan))
    assert not valid_input([]) and not valid_input(None)


def test_ship_space_motion_normalizes_diagonals_and_uses_fuel(world):
    forward, _ = world.join(identity('u_forward', spawn_position=[0, 0, 0], spawn_region=0))
    diagonal, _ = world.join(identity('u_diagonal', spawn_position=[50, 0, 0], spawn_region=0))
    first_start, other_start = list(forward.position), list(diagonal.position)
    for _ in range(15):
        world.clock.now += 1 / 15
        assert world.input(forward.id, packet(forward.ack_seq + 1, throttle=1))
        assert world.input(diagonal.id, packet(diagonal.ack_seq + 1, throttle=1, strafe=1, lift=1))
        world.step()
    forward_distance = math.dist(first_start, forward.position)
    assert forward.position[0:2] == [0, 7]
    assert 8 < forward_distance < 20  # It accelerates rather than jumping to full cruise.
    assert math.dist(other_start, diagonal.position) == pytest.approx(forward_distance)
    assert math.hypot(*forward.velocity) == pytest.approx(20)
    assert math.hypot(*diagonal.velocity) == pytest.approx(20)
    assert forward.fuel == pytest.approx(92)
    old = list(forward.velocity)
    advance(world, 1 / 15, forward, throttle=1, yaw=math.pi / 2)
    assert forward.velocity[0] > 0 and forward.velocity[2] < 0
    assert math.dist(old, forward.velocity) <= 24 / 15 + 1e-8
    advance(world, 2, forward, throttle=1, yaw=math.pi / 2)
    assert forward.velocity == pytest.approx([20, 0, 0])


def test_lost_input_clears_thrust_but_coasting_cannot_regenerate(world):
    ship, _ = world.join(identity())
    ship.hp, ship.fuel = 40, 50
    assert world.input(ship.id, packet(throttle=1))
    advance(world, 1)
    assert 0 < math.hypot(*ship.velocity) < 20
    coasting = list(ship.position)
    health, fuel = ship.hp, ship.fuel
    advance(world, 2.5)
    assert ship.position != coasting and ship.hp == health and ship.fuel == fuel
    advance(world, 1, ship, brake=True)
    assert ship.velocity == [0, 0, 0]
    stopped = list(ship.position)
    advance(world, 1, ship)
    assert ship.hp == health and ship.fuel == fuel
    advance(world, 2, ship)
    assert ship.position == stopped and ship.hp > health and ship.fuel > fuel
    attacker, target = combat(world)
    advance(world, 2.1)
    target.hp = 80
    assert world.input(attacker.id, packet(actions=['laser']))
    assert target.hp == 65


def test_exhaustion_preserves_momentum_and_brake_is_not_a_free_refill(world):
    ship, _ = world.join(identity('u_fuel', spawn_position=[0, 0, 0], spawn_region=0))
    ship.fuel = 0.1
    advance(world, 1, ship, throttle=1)
    assert ship.position[2] < 0
    assert math.hypot(*ship.velocity) > 0 and ship.fuel == 0
    drifting = ship.position[2]
    advance(world, 1, ship, throttle=1)
    assert ship.position[2] < drifting and ship.fuel == 0
    advance(world, 1, ship, throttle=1, brake=True)
    assert ship.fuel == 0 and ship.velocity == [0, 0, 0]
    stopped = list(ship.position)
    advance(world, 2.2, ship, brake=True)
    assert ship.fuel > 0
    assert ship.position == stopped


def test_laser_is_hitscan_with_range_nearest_target_and_cross_region_hits(world):
    attacker, target = combat(world)
    blocker, _ = world.join(identity('u_blocker', spawn_position=[0, 0, -10], spawn_region=2))
    assert world.input(attacker.id, packet(actions=['laser']))
    assert blocker.hp == 85 and target.hp == 100
    assert world.snapshot(attacker.id)['events'][0]['end'] == pytest.approx([0, 7, -7])
    world.clock.now += 0.41
    blocker.position[0] = 4
    assert world.input(attacker.id, packet(1, actions=['laser']))
    assert target.hp == 85
    world.clock.now += 0.41
    target.position = [0, 7, -64]
    assert world.input(attacker.id, packet(2, actions=['laser']))
    assert target.hp == 85
    world.clock.now += 0.41
    target.position = [0, 7, 1]
    assert world.input(attacker.id, packet(3, actions=['laser']))
    assert target.hp == 85


def test_shield_reduces_damage_and_expiry_cooldown_and_cost_are_server_owned(world):
    attacker, target = combat(world)
    assert world.input(target.id, packet(actions=['shield']))
    assert target.fuel == 88
    assert target.shield_until_ms - world.snapshot(target.id)['server_time_ms'] == 2000
    assert target.shield_ready_ms - world.snapshot(target.id)['server_time_ms'] == 8000
    assert world.input(attacker.id, packet(actions=['laser']))
    assert target.hp == 94
    assert world.input(target.id, packet(1, actions=['shield']))
    assert target.fuel == 88
    world.clock.now += 2.01
    assert world.input(attacker.id, packet(1, actions=['laser']))
    assert target.hp == 79
    target.fuel = 11
    world.clock.now += 6
    assert world.input(target.id, packet(2, actions=['shield']))
    assert target.fuel == 11


def test_dash_has_one_second_thrust_cost_and_six_second_cooldown(world):
    ship, _ = world.join(identity('u_dash', spawn_position=[0, 0, 0], spawn_region=0))
    assert world.input(ship.id, packet(actions=['dash'], throttle=1))
    assert ship.fuel == 82
    advance(world, 0.8, ship, throttle=1)
    assert -48 < ship.position[2] < -20
    assert math.hypot(*ship.velocity) == pytest.approx(60)
    assert world.input(ship.id, packet(ship.ack_seq + 1, actions=['dash']))
    assert ship.dash_ready_ms - world.snapshot(ship.id)['server_time_ms'] == 5200
    advance(world, 0.4, ship, throttle=1)
    assert 20 < math.hypot(*ship.velocity) < 60  # Dash release softly returns to cruise.
    advance(world, 2, ship, throttle=1)
    assert math.hypot(*ship.velocity) == pytest.approx(20)
    assert (
        len([event for event in world.snapshot(ship.id)['events'] if event['type'] == 'dash']) == 1
    )


def test_death_is_three_second_respawn_at_original_spawn_and_reconnect_cannot_revive(world):
    attacker, target = combat(world)
    target.hp = 15
    original = list(target.position)
    assert world.change_region(target.id, 3)
    target.position = [0, 7, -20]
    assert world.input(attacker.id, packet(actions=['laser']))
    assert target.hp == 0 and attacker.score == 1
    remaining = target.respawn_at_ms - world.snapshot(target.id)['server_time_ms']
    assert remaining == 3000
    cooldown = target.region_ready_ms
    world.leave(target.id)
    resumed, _ = world.join(identity('u_target'))
    assert resumed is target and target.hp == 0 and target.region_ready_ms == cooldown
    assert not world.change_region(target.id, 5)
    advance(world, 2.9)
    assert target.hp == 0
    advance(world, 0.2)
    assert target.hp == target.fuel == 100
    assert target.position == original and target.region == 1
    assert target.respawn_at_ms == 0 and target.region_ready_ms == cooldown


def test_region_switch_cost_and_cooldown_do_not_reset_game_health(world):
    ship, _ = world.join(identity())
    ship.hp = 47
    assert world.input(ship.id, packet(actions=['shield']))
    shield = ship.shield_ready_ms
    assert world.change_region(ship.id, (ship.region + 1) % 19)
    assert ship.hp == 47 and ship.fuel == 68 and ship.shield_ready_ms == shield
    assert math.hypot(*ship.position) < 480
    assert not world.change_region(ship.id, (ship.region + 1) % 19)
    assert not world.change_region(ship.id, True)
    assert not world.change_region(ship.id, 19)
    world.clock.now += 10.01
    assert world.change_region(ship.id, (ship.region + 1) % 19)
    assert ship.fuel == 48
    ship.fuel = 19
    world.clock.now += 10.01
    assert not world.change_region(ship.id, (ship.region + 1) % 19)


def test_global_snapshot_contains_real_online_players_and_copies_public_vectors(world):
    first, second = combat(world)
    assert world.input(first.id, packet(actions=['laser']))
    snapshot = world.snapshot(first.id)
    assert snapshot['self_id'] == first.id
    assert snapshot['players'][0]['id'] == first.id
    assert {player['id'] for player in snapshot['players']} == {first.id, second.id}
    assert snapshot['total_players'] == sum(snapshot['region_counts'].values()) == 2
    assert snapshot['region_counts']['0'] == snapshot['region_counts']['1'] == 1
    snapshot['players'][0]['position'][0] = 9000
    assert first.position[0] == 0
    snapshot['events'][0]['position'][0] = 9000
    snapshot['events'][0]['end'][0] = 9000
    assert world.snapshot(first.id)['events'][0]['position'][0] == 0
    assert world.snapshot(first.id)['events'][0]['end'][0] == 0
    world.leave(second.id)
    assert world.snapshot(first.id)['total_players'] == 1
    with pytest.raises(ValueError, match='^player_not_active$'):
        world.snapshot(second.id)


def test_capacity_retention_expiry_and_eviction_keep_all_indexes_bounded():
    clock = Clock()
    world = FlightWorld(max_players=2, max_retained=3, retention_seconds=5, clock=clock)
    first, ticket = world.join(identity('u_first'))
    second, _ = world.join(identity('u_second'))
    with pytest.raises(ValueError, match='^world_full$'):
        world.join(identity('u_third'))
    world.leave(first.id)
    third, _ = world.join(identity('u_third'))
    world.leave(second.id)
    world.join(identity('u_fourth'))
    assert first.id not in world.ships
    assert len(world.ships) == len(world._tickets) == len(world._subjects) == 3
    with pytest.raises(ValueError, match='^invalid_resume$'):
        world.join(identity('u_first'), ticket)
    clock.now += 6
    world.step()
    assert second.id not in world.ships and third.id in world.ships
    assert len(world.ships) == len(world._tickets) == len(world._subjects) == 2


def test_event_buffer_expiry_fixed_ticks_and_long_stall_movement_are_bounded(world):
    ship, _ = world.join(identity('u_events', spawn_position=[0, 0, 0], spawn_region=0))
    for _ in range(400):
        world._event('laser', ship, world.clock(), end=[0, 0, 0])
    assert len(world._events) == 256
    assert len(world.snapshot(ship.id)['events']) == 64
    assert world.input(ship.id, packet(throttle=1))
    world.clock.now += 0.01
    assert world.step() == [] and world.tick == 0
    world.clock.now += 0.06
    world.step()
    assert world.tick == 1
    start = list(ship.position)
    world.clock.now += 10000
    # Refresh a legitimate input after the stall. Physics still caps catch-up.
    assert world.input(ship.id, packet(1, throttle=1))
    world.step()
    assert math.dist(ship.position, start) <= 20 * 0.3
    assert world.snapshot(ship.id)['events'] == []
    before = ship.wire(), world.tick
    for invalid in [float('nan'), float('inf'), -1]:
        assert world.step(invalid) == []
    assert (ship.wire(), world.tick) == before


def test_world_bounds_cancel_only_outward_velocity(world):
    ship, _ = world.join(identity('u_edge', spawn_position=[0, 0, 0], spawn_region=0))
    ship.position = [479.9, 0, 0]
    advance(world, 1, ship, strafe=1)
    assert math.hypot(*ship.position) == pytest.approx(480)
    assert ship.velocity == pytest.approx([0, 0, 0])
    advance(world, 1, ship, strafe=-1)
    assert 460 < ship.position[0] < 480
    assert ship.velocity[0] == pytest.approx(-20)


def test_private_signed_ship_keeps_identity_for_rejoin_without_exposing_it(world):
    private = identity(
        'u_private_uuid',
        handle='pilot-72',
        public_subject_id=None,
        spawn_position=[30, 40, 50],
        spawn_region=3,
    )
    ship, _ = world.join(private)
    other, _ = world.join(identity('u_public'))
    assert ship.subject_id == 'u_private_uuid' and not ship.guest
    assert ship.wire()['subject_id'] is None
    assert ship.spawn_position == [30, 47, 50] and ship.spawn_region == 3
    assert 'u_private_uuid' not in json.dumps(world.snapshot(other.id))
    assert 'spawn_position' not in ship.wire()
    assert world.input(ship.id, packet(actions=['shield']))
    ship.hp = 18
    cooldown = ship.shield_ready_ms
    world.leave(ship.id)
    resumed, _ = world.join(private)
    assert resumed is ship and resumed.hp == 18 and resumed.shield_ready_ms == cooldown
    with pytest.raises(ValueError, match='^invalid_spawn$'):
        world.join(identity('u_hidden', public_subject_id=None))


def test_clear_discards_every_game_identity_ticket_and_event(world):
    ship, _ = world.join(identity())
    assert world.input(ship.id, packet(actions=['laser']))
    world.leave(ship.id)
    world.clear()
    assert not world.ships and not world.active and not world._events
    assert not world._subjects and not world._tickets and not world._ship_tickets
    assert world.tick == 0 and world.active_count == 0


def test_private_reconnect_retains_temporary_handle_and_home(world):
    private = identity(
        'u_private',
        handle='pilot-first',
        public_subject_id=None,
        spawn_position=[10, 20, 30],
        spawn_region=2,
    )
    first, ticket = world.join(private)
    world.leave(first.id)
    resumed, resumed_ticket = world.join({
        **private,
        'handle': 'pilot-new',
        'spawn_position': [100, 110, 120],
        'spawn_region': 3,
    })
    assert resumed is first and resumed_ticket == ticket
    assert resumed.handle == 'pilot-first'
    assert resumed.spawn_position == [10, 27, 30] and resumed.spawn_region == 2


def test_privacy_change_rotates_identifiers_home_and_events_without_healing(world):
    public = identity('u_changing', spawn_position=[5, 15, 25], spawn_region=2)
    first, old_ticket = world.join(public)
    old_id = first.id
    assert world.input(first.id, packet(12, actions=['shield', 'dash', 'laser']))
    first.hp, first.score = 32, 7
    before = first.wire()
    world.leave(first.id)
    private = {
        **public,
        'public_subject_id': None,
        'handle': 'pilot-anonymous',
        'spawn_position': [-40, -50, -60],
        'spawn_region': 6,
    }
    hidden, new_ticket = world.join(private, old_ticket)
    assert hidden is not first and hidden.id != old_id and new_ticket != old_ticket
    assert old_id not in world.ships and old_ticket not in world._tickets
    assert world._subjects['u_changing'] == hidden.id
    assert first.id == old_id  # An old socket's finally/leave cannot remove the new ship.
    assert hidden.position == hidden.spawn_position == [-40, -43, -60]
    assert hidden.region == hidden.spawn_region == 6
    for key in [
        'hp',
        'fuel',
        'score',
        'ack_seq',
        'laser_ready_ms',
        'shield_ready_ms',
        'dash_ready_ms',
    ]:
        assert hidden.wire()[key] == before[key]
    assert hidden.wire()['subject_id'] is None
    assert world.snapshot(hidden.id)['events'] == []
    world.leave(old_id)
    assert hidden.id in world.active
    world.leave(hidden.id)
    revealed, revealed_ticket = world.join(public)
    assert revealed.id != hidden.id and revealed_ticket != new_ticket
    assert revealed.position == [5, 22, 25] and revealed.wire()['subject_id'] == 'u_changing'
    assert revealed.hp == 32 and revealed.fuel == before['fuel'] and revealed.ack_seq == 12


def test_privacy_change_preserves_death_deadline_and_respawns_at_new_home(world):
    attacker, target = combat(world)
    assert world.input(target.id, packet(8, actions=['shield', 'dash']))
    target.hp = 6
    assert world.input(attacker.id, packet(actions=['laser']))
    assert target.hp == 0
    deadlines = target.respawn_at_ms, target.shield_ready_ms, target.dash_ready_ms
    world.leave(target.id)
    hidden, _ = world.join(
        identity(
            'u_target',
            public_subject_id=None,
            handle='private-pilot',
            spawn_position=[80, 90, 100],
            spawn_region=5,
        )
    )
    assert hidden.hp == 0
    assert (hidden.respawn_at_ms, hidden.shield_ready_ms, hidden.dash_ready_ms) == deadlines
    advance(world, 3.1)
    assert hidden.hp == hidden.fuel == 100
    assert hidden.position == [80, 97, 100] and hidden.region == 5
    assert hidden.shield_ready_ms == deadlines[1] and hidden.dash_ready_ms == deadlines[2]


@pytest.mark.parametrize('start', [100.0, 1000.0, 1000000.0])
def test_dash_duration_is_independent_of_monotonic_clock_uptime(start):
    clock = Clock()
    clock.now = start
    world = FlightWorld(clock=clock, collectibles=CollectibleField(seed='dash-timing', count=0))
    ship, _ = world.join(identity('u_dash_timing', spawn_position=[0, 0, 0], spawn_region=0))
    for seq in range(16):
        clock.now = start + seq / 15
        assert world.input(ship.id, packet(seq, throttle=1, actions=['dash'] if seq == 0 else []))
        world.step()
    assert world.tick == 15
    assert ship.position[2] == pytest.approx(-41.25, abs=1e-7)
    assert ship.fuel == pytest.approx(74)
    assert ship.velocity == pytest.approx([0, 0, -60])


def test_neutral_turning_preserves_world_momentum_without_burning_fuel(world):
    ship, _ = world.join(identity('u_drift', spawn_position=[0, 0, 0], spawn_region=0))
    advance(world, 1, ship, throttle=1)
    before = list(ship.position)
    fuel = ship.fuel
    advance(world, 1 / 15, ship, yaw=math.pi / 2, pitch=0.8)
    assert ship.velocity[0] == ship.velocity[1] == 0
    assert 19 < -ship.velocity[2] < 20
    assert ship.position[0:2] == before[0:2] and ship.position[2] < before[2]
    assert ship.fuel == fuel
    # Exhaustion disables the engine rather than deleting existing momentum.
    ship.fuel = 0
    speed = math.hypot(*ship.velocity)
    advance(world, 1, ship, throttle=1, yaw=math.pi / 2)
    assert ship.velocity[0] == ship.velocity[1] == 0
    assert 0 < math.hypot(*ship.velocity) < speed
    assert ship.fuel == 0


def test_braking_is_gradual_cannot_reverse_and_expires_with_missing_input(world):
    ship, _ = world.join(identity('u_brake', spawn_position=[0, 0, 0], spawn_region=0))
    advance(world, 1, ship, throttle=1)
    assert world.input(ship.id, packet(ship.ack_seq + 1, brake=True))
    advance(world, 1 / 15)
    assert 16 < -ship.velocity[2] < 18
    advance(world, 1, ship, brake=True)
    assert ship.velocity == [0, 0, 0]
    ship.velocity = [60, 0, 0]
    assert world.input(ship.id, packet(ship.ack_seq + 1, brake=True))
    advance(world, 0.8)
    # A stale brake must stop applying its strong drag after half a second.
    assert 30 < ship.velocity[0] < 40


def test_dash_release_preserves_speed_and_neutral_drift_does_not_heal(world):
    ship, _ = world.join(identity('u_release', spawn_position=[0, 0, 0], spawn_region=0))
    assert world.input(ship.id, packet(actions=['dash'], throttle=1))
    advance(world, 1, ship, throttle=1)
    assert math.hypot(*ship.velocity) == pytest.approx(60)
    assert ship.dash_until_ms - world.snapshot(ship.id)['state_time_ms'] <= 1
    fuel = ship.fuel
    ship.hp = 40
    advance(world, 1 / 15, ship)
    assert 58 < math.hypot(*ship.velocity) < 60
    advance(world, 3, ship)
    assert math.hypot(*ship.velocity) > 15 and ship.hp == 40
    assert ship.fuel == pytest.approx(fuel)


def test_throttle_intent_prevents_idle_refill_even_when_directions_cancel(world):
    ship, _ = world.join(identity('u_cancel', spawn_position=[0, 0, 0], spawn_region=0))
    ship.hp, ship.fuel = 40, 0
    advance(world, 3, ship, throttle=1, lift=-1, pitch=math.pi / 2)
    assert ship.velocity == [0, 0, 0]
    assert ship.hp == 40 and ship.fuel == 0
    advance(world, 1.5, ship)
    assert ship.hp == 40 and ship.fuel == 0
    advance(world, 1, ship)
    assert ship.hp > 40 and ship.fuel > 0


def test_reconnect_preserves_coasting_and_does_not_count_offline_time_as_idle(world):
    ship, _ = world.join(identity('u_reconnect_drift', spawn_position=[0, 0, 0], spawn_region=0))
    advance(world, 1, ship, throttle=1)
    velocity, position = list(ship.velocity), list(ship.position)
    fuel = ship.fuel
    world.leave(ship.id)
    world.clock.now += 100
    resumed, _ = world.join(identity('u_reconnect_drift'))
    assert resumed is ship and resumed.velocity == velocity and resumed.position == position
    advance(world, 1 / 15, resumed)
    assert resumed.position[2] < position[2]
    assert resumed.fuel == fuel


def test_collectibles_are_shared_and_credit_one_authoritative_player(world):
    world.collectibles = CollectibleField(seed='world-pickup', count=1)
    first, _ = world.join(identity('u_collector_1'))
    second, _ = world.join(identity('u_collector_2'))
    point = list(world.collectibles.positions[0])
    first.position, second.position = list(point), list(point)
    first.fuel = second.fuel = 80
    advance(world, 1 / 15)
    assert first.collected + second.collected == 1
    assert first.fuel + second.fuel == 164
    snapshot = world.snapshot(first.id)
    events = [event for event in snapshot['events'] if event['type'] == 'collect']
    assert len(events) == 1 and events[0]['glyph_ids'] == [0]
    assert events[0]['fuel_added'] == 4 and events[0]['collected'] == 1
    assert snapshot['collectibles']['revision'] == 1
    assert snapshot['collectibles'] == world.snapshot(second.id)['collectibles']


def test_full_tank_still_collects_without_interrupting_stationary_timers(world):
    world.collectibles = CollectibleField(seed='world-pickup', count=1)
    ship, _ = world.join(identity('u_full_tank'))
    ship.position = list(world.collectibles.positions[0])
    idle = ship._stationary_since, ship._last_activity
    advance(world, 1 / 15)
    assert ship.collected == 1 and ship.fuel == 100
    event = next(event for event in world.snapshot(ship.id)['events'] if event['type'] == 'collect')
    assert event['fuel_added'] == 0
    assert (ship._stationary_since, ship._last_activity) == idle
    collected = ship.collected
    world.leave(ship.id)
    resumed, _ = world.join(identity('u_full_tank'))
    assert resumed.collected == collected
    world.leave(resumed.id)
    hidden, _ = world.join(
        identity(
            'u_full_tank',
            public_subject_id=None,
            handle='pilot-hidden',
            spawn_position=[0, 0, 0],
            spawn_region=0,
        )
    )
    assert hidden.collected == collected and hidden.wire()['collected'] == collected
    hidden.hp = 0
    world._deadline(hidden, 'respawn_at', world.clock() + 0.1)
    advance(world, 0.2)
    assert hidden.hp == 100 and hidden.collected == collected


def test_respawn_and_region_relocation_do_not_collect_along_a_teleport(world):
    world.collectibles = CollectibleField(seed='world-pickup', count=1)
    ship, _ = world.join(identity('u_teleport', spawn_position=[0, 0, 0], spawn_region=0))
    point = list(world.collectibles.positions[0])
    assert math.hypot(*point) > 30
    ship.position = [component * 1.008 for component in point]
    ship.hp = 0
    world._deadline(ship, 'respawn_at', world.clock() + 0.1)
    advance(world, 0.2)
    assert ship.position == [0, 7, 0] and ship.collected == 0
    ship.position = list(point)
    region = max(
        range(1, 19), key=lambda index: math.dist(point, region_centers()[index]['center'])
    )
    assert world.change_region(ship.id, region)
    advance(world, 1 / 15)
    assert ship.collected == 0 and world.collectibles.revision == 0


def test_snapshot_physics_time_advances_only_on_ticks_and_resets_after_empty_pause(world):
    first, _ = world.join(identity('u_state_clock'))
    initial = world.snapshot(first.id)
    world.clock.now += 0.01
    world.step()
    pending = world.snapshot(first.id)
    assert pending['state_time_ms'] == initial['state_time_ms']
    assert pending['server_time_ms'] > pending['state_time_ms']
    advance(world, 1 / 15)
    assert world.snapshot(first.id)['state_time_ms'] > initial['state_time_ms']
    world.leave(first.id)
    world.clock.now += 120
    resumed, _ = world.join(identity('u_state_clock'))
    current = world.snapshot(resumed.id)
    assert current['state_time_ms'] == current['server_time_ms']
