import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from msg.core.event_wait import EventWaitingExecutor
from msg.core.executor import OperationExecutor


@dataclass
class Result:
    status: str = 'ok'
    data: dict = field(default_factory=lambda: {'items': [], 'has_more': False})


class Signal:
    def __init__(self):
        self.generation = 0
        self.waiters = 0
        self.starts = 0
        self.waiting = asyncio.Event()
        self.changed = asyncio.Event()

    async def start(self, timeout=2):
        self.starts += 1
        return True

    async def wait(self, generation, timeout):
        self.waiting.set()
        try:
            await asyncio.wait_for(self.changed.wait(), timeout)
        except TimeoutError:
            pass


def executor(monkeypatch, read):
    monkeypatch.setattr(OperationExecutor, 'execute', read)
    service = EventWaitingExecutor.__new__(EventWaitingExecutor)
    signal = Signal()
    service.metadata = SimpleNamespace(event_signal=signal)
    packet = SimpleNamespace(
        operation='communication.events',
        contract_version=2,
        arguments={'wait_ms': 200},
    )
    return service, signal, packet


async def test_registration_race_rechecks_before_sleep(monkeypatch):
    calls = []

    async def read(self, packet, *, entry):
        calls.append(packet)
        return Result(data={'items': ['committed'] if len(calls) == 2 else [], 'has_more': False})

    service, signal, packet = executor(monkeypatch, read)
    result = await service.execute(packet)
    assert result.data['items'] == ['committed']
    assert calls == [packet, packet]
    assert signal.starts == 1 and signal.waiters == 0
    assert not signal.waiting.is_set()


async def test_permission_revoked_while_waiting_returns_error(monkeypatch):
    calls = []

    async def read(self, packet, *, entry):
        calls.append(packet)
        return Result(status='error') if signal.changed.is_set() else Result()

    service, signal, packet = executor(monkeypatch, read)
    pending = asyncio.create_task(service.execute(packet))
    await signal.waiting.wait()
    signal.changed.set()
    result = await pending
    assert result.status == 'error'
    assert len(calls) == 3 and all(value is packet for value in calls)
    assert signal.waiters == 0


async def test_cancellation_releases_waiter(monkeypatch):
    async def read(self, packet, *, entry):
        return Result()

    service, signal, packet = executor(monkeypatch, read)
    pending = asyncio.create_task(service.execute(packet))
    await signal.waiting.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert signal.waiters == 0


async def test_unauthorized_requests_do_not_open_listener(monkeypatch):
    async def read(self, packet, *, entry):
        return Result(status='error')

    service, signal, packet = executor(monkeypatch, read)
    assert (await service.execute(packet)).status == 'error'
    assert signal.starts == 0


async def test_idle_wait_has_bounded_reads_and_no_write(monkeypatch):
    calls = []

    async def read(self, packet, *, entry):
        calls.append(packet)
        return Result()

    service, signal, packet = executor(monkeypatch, read)
    packet.arguments['wait_ms'] = 20
    assert (await service.execute(packet)).status == 'ok'
    assert len(calls) == 3
    assert signal.waiters == 0


async def test_saturation_does_not_allocate_more_waiters(monkeypatch):
    async def read(self, packet, *, entry):
        return Result()

    service, signal, packet = executor(monkeypatch, read)
    signal.waiters = 128
    assert (await service.execute(packet)).data['wait_supported'] is False
    assert signal.starts == 0 and signal.waiters == 128
