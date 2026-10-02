"""Process-local authoritative flight playground, isolated from MSG resource writes.

Only explicitly joined players are broadcast. OAuth is checked on ingress and
again while connected; game presence never becomes an MSG online declaration.
One uvicorn worker owns one world. This is not a distributed state service.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from starlette.websockets import WebSocket, WebSocketDisconnect

from msg.core.codec import canonical, loads
from msg.core.errors import Failure, require
from msg.core.models import ExecutionContext, Principal
from msg.core.requests import request_for
from msg.plugins.discovery import visible
from msg.security.oauth import OAuthService
from msg.security.quarantine import require_live_authority
from msg.transports.flight_simulation import FlightWorld, region_centers, valid_input
from msg.transports.url_safety import require_matching_host, require_safe_request_target

MAX_CLIENTS = 96
MAX_PER_ADDRESS = 8
MAX_FRAME_BYTES = 2048
MAX_SNAPSHOT_BYTES = 192 * 1024
INPUT_HZ = 30
INPUT_BURST = 45
TICK_HZ = 15
SNAPSHOT_HZ = 5
REVALIDATE_SECONDS = 2.0
JOIN_TIMEOUT = 5.0
SEND_TIMEOUT = 1.0


@dataclass
class Peer:
    id: str
    websocket: WebSocket
    address: str
    cookie: str = ''
    subject: str | None = None
    public_identity: bool = True
    ship_id: str | None = None
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=2))
    writer: asyncio.Task | None = None
    tokens: float = INPUT_BURST
    last_input: float = field(default_factory=time.monotonic)
    bad_frames: int = 0
    pending: deque = field(default_factory=deque)


class FlightHub:
    """Bounded sockets, independent writers, and one fixed-rate shared simulation."""

    def __init__(self, service):
        self.service = service
        self.oauth = OAuthService(service)
        self.world = FlightWorld(max_players=MAX_CLIENTS, max_retained=512, retention_seconds=300)
        self.peers: dict[str, Peer] = {}
        self.addresses: dict[str, int] = {}
        self.admissions: dict[str, tuple[float, float]] = {}
        self.runner: asyncio.Task | None = None
        self.closed = False
        self.next_auth_check = 0.0
        self.auth_slots = asyncio.Semaphore(8)

    def _reserve(self, websocket):
        require(not self.closed and len(self.peers) < MAX_CLIENTS, 'server_busy')
        address = websocket.client.host if websocket.client else 'unknown'
        require(self.addresses.get(address, 0) < MAX_PER_ADDRESS, 'server_busy')
        # Connection attempts are bounded independently of incoming control frames.
        now = time.monotonic()
        if len(self.admissions) >= 1024 and address not in self.admissions:
            self.admissions = {
                key: item for key, item in self.admissions.items() if now - item[1] < 120
            }
        require(len(self.admissions) < 1024 or address in self.admissions, 'server_busy')
        tokens, checked = self.admissions.get(address, (20.0, now))
        tokens = min(20.0, tokens + (now - checked) / 3)
        require(tokens >= 1, 'server_busy')
        self.admissions[address] = (tokens - 1, now)
        peer = Peer(secrets.token_hex(16), websocket, address)
        self.peers[peer.id] = peer
        self.addresses[address] = self.addresses.get(address, 0) + 1
        return peer

    def _ingress(self, websocket):
        scope = websocket.scope
        raw = scope.get('raw_path') or scope['path'].encode('utf-8')
        require_safe_request_target(
            raw,
            scope.get('query_string', b''),
            maximum=self.service.settings.server.limits.max_path_bytes,
        )
        require(raw == b'/_flight' and not scope.get('query_string'), 'invalid_request')
        selected = require_matching_host(
            websocket.headers.getlist('host'),
            urlsplit(self.service.settings.service_url),
            aliases=getattr(self.service.settings, 'service_aliases', ()),
        )
        require(
            websocket.headers.getlist('origin') == [f'{selected.scheme}://{selected.netloc}'],
            'forbidden_origin',
        )
        require(
            'authorization' not in websocket.headers
            and 'x-msg-request' not in websocket.headers
            and 'sec-websocket-protocol' not in websocket.headers
            and 'x-http-method-override' not in websocket.headers
            and 'x-method-override' not in websocket.headers,
            'ambiguous_credentials',
        )

    def _fence(self, tx):
        self.service.runtime_generation.require_current(tx)
        require_live_authority(tx)
        require(not self.service.executor.recovery_drill_active(), 'recovery_quarantined')

    async def _public_identity(self, tx, subject):
        request = request_for(
            'discovery.get', {'id': subject}, self.service.settings.service_url, source='manual'
        )
        context = ExecutionContext(
            request_id=request.request_id,
            principal=Principal(
                actor=None,
                subject=None,
                credential_id=None,
                method='anonymous',
                certificates=(),
                ceiling=(),
            ),
            entry='network',
            now=self.service.clock(),
            deadline_monotonic=time.monotonic() + 2,
        )
        return await visible(self.service, context, request, tx, subject)

    async def _identity(self, peer):
        scheme = urlsplit(self.service.settings.service_url).scheme
        cookie_name = '__Host-msg_session' if scheme == 'https' else 'msg_session'
        raw_cookies = peer.websocket.headers.getlist('cookie')
        require(len(raw_cookies) <= 1, 'ambiguous_credentials')
        if raw_cookies:
            names = [part.partition('=')[0].strip() for part in raw_cookies[0].split(';')]
            require(names.count(cookie_name) <= 1, 'ambiguous_credentials')
        peer.cookie = peer.websocket.cookies.get(cookie_name, '')
        if raw_cookies and cookie_name in names:
            require(bool(peer.cookie), 'invalid_grant')
        require(len(peer.cookie) <= 256, 'invalid_grant')
        async with self.auth_slots:
            async with self.service.metadata.transaction(write=False) as tx:
                self._fence(tx)
                if not peer.cookie:
                    return {
                        'subject_id': None,
                        'handle': 'guest-' + secrets.token_hex(4),
                        'guest': True,
                    }
                subject, _, _ = await self.oauth.browser_credentials(tx, peer.cookie)
                resource = await tx.resource(subject)
                require(resource.type == 'user' and resource.state == 'active', 'invalid_grant')
                peer.subject = subject
                name = resource.name
                peer.public_identity = await self._public_identity(tx, subject)
        # The central anonymous authorizer preserves profile and ancestor ACLs.
        # A verified login is not permission to reveal a private profile to peers.
        if not peer.public_identity:
            region = secrets.randbelow(19)
            center = region_centers()[region]['center']
            neutral_home = [
                component + (secrets.randbelow(2001) - 1000) / 40 for component in center
            ]
            return {
                'subject_id': subject,
                'spawn_position': neutral_home,
                'spawn_region': region,
                'public_subject_id': None,
                'handle': 'pilot-' + secrets.token_hex(4),
                'guest': False,
            }
        from msg.transports.universe import subject_position

        point = await subject_position(self.service, subject)
        require(point is not None, 'invalid_grant')
        centers = region_centers()
        region = min(
            centers,
            key=lambda area: sum((point[i] - area['center'][i]) ** 2 for i in range(3)),
        )['id']
        return {
            'subject_id': subject,
            'handle': name,
            'guest': False,
            'spawn_position': point,
            'spawn_region': region,
        }

    async def _packet(self, peer, *, timeout=None):
        receive = peer.websocket.receive()
        message = await asyncio.wait_for(receive, timeout) if timeout else await receive
        if message['type'] == 'websocket.disconnect':
            raise WebSocketDisconnect(message.get('code', 1000))
        require(message.get('bytes') is None, 'flight_binary_frame')
        text = message.get('text')
        require(type(text) is str, 'invalid_request')
        require(len(text.encode('utf-8')) <= MAX_FRAME_BYTES, 'flight_frame_too_large')
        packet = loads(text)
        require(
            type(packet) is dict and type(packet.get('v')) is int and packet['v'] == 1,
            'invalid_request',
        )
        return packet

    def _allow_input(self, peer):
        now = time.monotonic()
        peer.tokens = min(INPUT_BURST, peer.tokens + (now - peer.last_input) * INPUT_HZ)
        peer.last_input = now
        require(peer.tokens >= 1, 'flight_input_rate')
        peer.tokens -= 1

    async def _send(self, websocket, value):
        payload = canonical(value)
        require(len(payload) <= MAX_SNAPSHOT_BYTES, 'response_too_large')
        await asyncio.wait_for(websocket.send_text(payload.decode()), SEND_TIMEOUT)

    async def websocket(self, websocket: WebSocket):
        peer = None
        accepted = False
        try:
            self._ingress(websocket)
            peer = self._reserve(websocket)
            identity = await self._identity(peer)
            await websocket.accept()
            accepted = True
            packet = await self._packet(peer, timeout=JOIN_TIMEOUT)
            require(
                set(packet) <= {'v', 'type', 'resume'} and packet.get('type') == 'join',
                'invalid_request',
            )
            resume = packet.get('resume')
            require(
                resume is None or (type(resume) is str and 16 <= len(resume) <= 128),
                'invalid_request',
            )
            # A logout or restoration during the join timeout must not revive a
            # stale authenticated identity when the simulation record is made.
            async with self.service.metadata.transaction(write=False) as tx:
                self._fence(tx)
                if peer.cookie:
                    subject, _, _ = await self.oauth.browser_credentials(tx, peer.cookie)
                    require(subject == peer.subject, 'invalid_grant')
                    require(
                        await self._public_identity(tx, subject) == peer.public_identity,
                        'invalid_grant',
                    )
            ship, resume = self.world.join(identity, resume=resume if identity['guest'] else None)
            peer.ship_id = ship.id
            own = ship.wire()
            home = list(ship.spawn_position)
            home[1] -= 7
            if peer.subject:
                own['home_position'] = home
            if peer.subject and not peer.public_identity:
                own['home_body'] = {
                    'id': 'flight_home_' + ship.id,
                    'position': home,
                    'radius': 3,
                    'private': True,
                    'title': '你的私有星球',
                }
            await self._send(
                websocket,
                {
                    'v': 1,
                    'type': 'hello',
                    'self': own,
                    'resume': resume,
                    'region': ship.region,
                    'regions': region_centers(),
                    'server_time_ms': self.world.snapshot(ship.id)['server_time_ms'],
                    'tick_hz': TICK_HZ,
                    'limits': {
                        'max_players': MAX_CLIENTS,
                        'max_frame_bytes': MAX_FRAME_BYTES,
                        'max_snapshot_bytes': MAX_SNAPSHOT_BYTES,
                        'input_hz': INPUT_HZ,
                        'tick_hz': TICK_HZ,
                        'snapshot_hz': SNAPSHOT_HZ,
                        'world_extent': 480,
                        'laser_range': 60,
                        'laser_cooldown_ms': 400,
                    },
                },
            )
            peer.writer = asyncio.create_task(self._writer(peer), name='flight-snapshot-writer')
            if self.runner is None or self.runner.done():
                self.runner = asyncio.create_task(self._run(), name='flight-authoritative-tick')
            while not self.closed:
                packet = await self._packet(peer)
                self._allow_input(peer)
                if packet.get('type') == 'input':
                    valid = valid_input(packet)
                    if valid and packet['seq'] <= ship.ack_seq:
                        continue
                elif packet.get('type') == 'region':
                    valid = (
                        set(packet) == {'v', 'type', 'region'}
                        and type(packet['region']) is int
                        and 0 <= packet['region'] < 19
                    )
                else:
                    valid = False
                if valid:
                    require(len(peer.pending) < 8, 'flight_input_rate')
                    peer.pending.append(packet)
                if not valid:
                    peer.bad_frames += 1
                    self._enqueue(
                        peer,
                        {
                            'v': 1,
                            'type': 'error',
                            'code': 'invalid_input',
                            'message': '控制输入无效，请重新进入飞船模式。',
                        },
                    )
                    require(peer.bad_frames < 3, 'invalid_request')
        except Failure as exc:
            code = (
                1003
                if exc.code == 'flight_binary_frame'
                else 1009
                if exc.code == 'flight_frame_too_large'
                else 4013
                if exc.code in {'invalid_grant', 'credential_not_found', 'oauth_disabled'}
                else 1013
                if exc.code in {'server_busy', 'recovery_quarantined', 'recovery_runtime_stale'}
                else 1008
            )
            with contextlib.suppress(Exception):
                await websocket.close(code=code, reason='flight_unavailable')
        except WebSocketDisconnect, TimeoutError:
            pass
        except ValueError:
            with contextlib.suppress(Exception):
                await websocket.close(code=1008, reason='flight_unavailable')
        except Exception:
            # Do not log headers, cookies, frames, resource names, or exception text.
            with contextlib.suppress(Exception):
                await websocket.close(code=1011, reason='flight_unavailable')
        finally:
            if peer:
                await self._drop(peer, close=accepted)

    def _enqueue(self, peer, packet):
        if peer.queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                peer.queue.get_nowait()
        peer.queue.put_nowait(packet)

    def _drain_inputs(self, peer):
        while peer.pending:
            packet = peer.pending.popleft()
            if packet['type'] == 'input':
                self.world.input(peer.ship_id, packet)
            elif not self.world.change_region(peer.ship_id, packet['region']):
                self._enqueue(
                    peer,
                    {
                        'v': 1,
                        'type': 'error',
                        'code': 'region_unavailable',
                        'message': '暂时无法切换区域，请检查燃油或等待冷却。',
                    },
                )

    async def _writer(self, peer):
        try:
            while not self.closed and peer.id in self.peers:
                await self._send(peer.websocket, await peer.queue.get())
        except Failure, TimeoutError, WebSocketDisconnect, RuntimeError:
            await self._drop(peer, close=True)

    async def _validate(self, *, identities):
        invalid = []
        async with self.service.metadata.transaction(write=False) as tx:
            self._fence(tx)
            if identities:
                for peer in tuple(self.peers.values()):
                    if not peer.ship_id or not peer.cookie:
                        continue
                    try:
                        subject, _, _ = await self.oauth.browser_credentials(tx, peer.cookie)
                        resource = await tx.resource(subject)
                        require(
                            subject == peer.subject
                            and resource.type == 'user'
                            and resource.state == 'active',
                            'invalid_grant',
                        )
                        require(
                            await self._public_identity(tx, subject) == peer.public_identity,
                            'invalid_grant',
                        )
                        if peer.public_identity:
                            self.world.ships[peer.ship_id].handle = resource.name
                    except Failure:
                        invalid.append(peer)
        for peer in invalid:
            await self._drop(peer, close=True, code=4013)

    async def _run(self):
        try:
            deadline = time.monotonic()
            next_snapshot = deadline
            while self.peers and not self.closed:
                now = time.monotonic()
                identities = now >= self.next_auth_check
                # Gate before moving or broadcasting. No slow socket can stall
                # the clock; each socket has its own bounded writer queue.
                await self._validate(identities=identities)
                if identities:
                    self.next_auth_check = now + REVALIDATE_SECONDS
                self.world.step()
                for peer in tuple(self.peers.values()):
                    if peer.ship_id:
                        self._drain_inputs(peer)
                if now >= next_snapshot:
                    next_snapshot = now + 1 / SNAPSHOT_HZ
                    for peer in tuple(self.peers.values()):
                        if peer.ship_id and peer.writer:
                            self._enqueue(peer, self.world.snapshot(peer.ship_id))
                deadline = max(deadline + 1 / TICK_HZ, time.monotonic())
                await asyncio.sleep(max(0, deadline - time.monotonic()))
        except asyncio.CancelledError:
            raise
        except Exception:
            for peer in tuple(self.peers.values()):
                await self._drop(peer, close=True, code=1013)
        finally:
            self.runner = None

    async def _drop(self, peer, *, close=False, code=1000):
        if self.peers.pop(peer.id, None) is None:
            return
        peer.pending.clear()
        if peer.ship_id:
            self.world.leave(peer.ship_id)
        count = self.addresses.get(peer.address, 1) - 1
        if count:
            self.addresses[peer.address] = count
        else:
            self.addresses.pop(peer.address, None)
        writer = peer.writer
        if writer and writer is not asyncio.current_task():
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer
        if close:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(peer.websocket.close(code=code), SEND_TIMEOUT)

    async def close(self):
        self.closed = True
        if self.runner:
            self.runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.runner
        for peer in tuple(self.peers.values()):
            await self._drop(peer, close=True, code=1001)
        self.world.clear()
        self.addresses.clear()
        self.admissions.clear()
