"""Real TCP range acceptance and actual JS parsing of authoritative beam frames."""

import json
import math
import subprocess
from pathlib import Path

import pytest
from test_flight_websocket import controls, flight_server, oauth, player, snapshot

from msg.transports.flight_simulation import LASER_RANGE

__all__ = ['flight_server', 'oauth']


def freeze_positions(flight, source_id, target_id, source, target):
    # Seed authoritative fixture geometry; clients still supply controls only.
    # Drain the final elapsed physics before positioning the boundary cases.
    world = flight.hub.world
    now = world.clock()
    world.clock = lambda: now
    world.step()
    world.ships[source_id].position = list(source)
    world.ships[target_id].position = list(target)


def replay_actual_client(hello, state, command):
    module = Path(__file__).parents[1] / 'src/msg/data/root-web-flight-client.js'
    replay = """
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const {hello,state,command} = JSON.parse(fs.readFileSync(0,'utf8'));
let socket,pump; const sent=[];
class Socket {
  constructor(){ socket=this; this.readyState=0; this.bufferedAmount=0; }
  send(raw){ sent.push(JSON.parse(raw)); }
  close(){ this.readyState=3; }
}
const context=vm.createContext({WebSocket:Socket,location:{protocol:'http:',host:'testserver'},
  document:{hidden:false,visibilityState:'visible',addEventListener(){},removeEventListener(){}},
  setTimeout(){return 1;},clearTimeout(){},setInterval(fn){pump=fn;return 1;},clearInterval(){},
  performance:{now(){return 0;}},Date});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const client=new context.MSGFlightClient.Client(); client.connect(); socket.readyState=1; socket.onopen();
socket.onmessage({data:JSON.stringify(hello)});
assert.equal(client.setInput(command),true); pump();
assert.equal(sent.at(-1).seq,0); assert.equal(sent.at(-1).actions[0],'laser');
socket.onmessage({data:JSON.stringify(state)});
assert.equal(client.connected,true); assert.equal(client.snapshot.tick,state.tick);
assert.equal(client.limits.laser_range,180); assert.equal(client.self.ack_seq,0);
const beam=client.snapshot.events.find(event=>event.type==='laser');
assert.ok(beam); assert.ok(Math.hypot(...beam.end.map((x,i)=>x-beam.position[i]))<=180+1e-6);
assert.equal(JSON.stringify(beam.end),JSON.stringify(state.events.find(e=>e.type==='laser').end));
process.stdout.write(JSON.stringify({connected:true,laser_range:client.limits.laser_range}));
client.disconnect();
"""
    result = subprocess.run(
        ['node', '-e', replay, str(module)],
        input=json.dumps({'hello': hello, 'state': state, 'command': command}),
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'connected': True, 'laser_range': LASER_RANGE}


@pytest.mark.parametrize(
    ('distance', 'expected_hp', 'expected_length'),
    [(100, 85, 97), (183, 85, 180), (183.001, 100, 180)],
)
async def test_actual_tcp_laser_range_and_surface_boundary(
    flight_server, distance, expected_hp, expected_length
):
    attacker, hello = await flight_server.join()
    target, target_hello = await flight_server.join()
    source_id, target_id = hello['self']['id'], target_hello['self']['id']
    assert hello['limits']['laser_range'] == LASER_RANGE == 180
    freeze_positions(flight_server, source_id, target_id, [0, 7, 0], [0, 7, -distance])
    await attacker.send_json(controls(0, actions=['laser']))
    state = await snapshot(attacker, lambda body: player(body, source_id)['ack_seq'] == 0)
    assert player(state, target_id)['hp'] == expected_hp
    assert player(state, source_id)['fuel'] == 99
    beam = next(event for event in state['events'] if event['type'] == 'laser')
    assert math.dist(beam['position'], beam['end']) == pytest.approx(expected_length)
    assert beam.get('target_id') == (target_id if expected_hp < 100 else None)
    assert not attacker.closed and not target.closed
    replay_actual_client(hello, state, {'actions': ['laser']})


async def test_actual_tcp_outward_world_edge_ray_stays_connected_in_actual_client(flight_server):
    attacker, hello = await flight_server.join()
    target, target_hello = await flight_server.join()
    source_id, target_id = hello['self']['id'], target_hello['self']['id']
    freeze_positions(flight_server, source_id, target_id, [470, 0, 0], [0, 0, 0])
    await attacker.send_json(controls(0, yaw=math.pi / 2, actions=['laser']))
    state = await snapshot(attacker, lambda body: player(body, source_id)['ack_seq'] == 0)
    beam = next(event for event in state['events'] if event['type'] == 'laser')
    assert beam['position'] == [470, 0, 0]
    assert beam['end'] == pytest.approx([650, 0, 0], abs=1e-10)
    assert beam['end'][0] > hello['limits']['world_extent']
    assert player(state, target_id)['hp'] == 100
    assert 'target_id' not in beam and not attacker.closed and not target.closed
    replay_actual_client(hello, state, {'yaw': math.pi / 2, 'actions': ['laser']})
