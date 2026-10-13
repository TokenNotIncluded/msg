"""Bounded event waits wrap, never replace, the canonical authorization pipeline."""

import asyncio
import time
from dataclasses import replace

from msg.core.executor import OperationExecutor


class EventWaitingExecutor(OperationExecutor):
    async def execute(self, request, *, entry='network'):
        result = await super().execute(request, entry=entry)
        if (
            request.operation
            not in {
                'communication.events',
                'communication.changes',
                'identity.link_collect',
                'identity.link_pending',
            }
            or request.contract_version != 2
            or result.status != 'ok'
        ):
            return result
        timeout = (
            min(
                request.arguments.get('wait_ms', 0),
                result.data.get('wait_limit_ms', 20000),
            )
            / 1000
        )
        signal = getattr(self.metadata, 'event_signal', None)
        current = getattr(self.metadata, '_current', None)
        if signal is None:
            return replace(result, data={**result.data, 'wait_supported': False})
        if not timeout or request.arguments.get('tail'):
            return result
        # A nested batch must not keep an outer transaction or writer lock open.
        if (current is not None and current.get() is not None) or signal.waiters >= 128:
            return replace(result, data={**result.data, 'wait_supported': False})

        def available(value):
            if request.operation == 'identity.link_collect':
                return value.status != 'ok' or value.data.get('state') != 'claimed'
            return (
                value.status != 'ok'
                or value.data.get('items')
                or value.data.get('has_more')
                or value.data.get('authority_changed')
            )

        if available(result):
            return result
        deadline = time.monotonic() + timeout
        signal.waiters += 1
        try:
            if not await signal.start(min(2, max(0, deadline - time.monotonic()))):
                return replace(result, data={**result.data, 'wait_supported': False})
            generation = signal.generation
            # Register before rereading: commits in the read/LISTEN gap are
            # included in this second read, not lost until a later heartbeat.
            result = await super().execute(request, entry=entry)
            checks = 0
            while not available(result) and time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if checks >= 32:
                    # Bound read amplification on a globally busy event channel.
                    # Do one final authorized read at the heartbeat deadline.
                    await asyncio.sleep(max(0, remaining))
                else:
                    await signal.wait(generation, max(0, remaining))
                    await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))
                generation = signal.generation
                # The original packet is unchanged. The canonical executor
                # reauthenticates signed owners or enforces anonymous_only and
                # re-verifies the helper's exact possession claim in the handler.
                # Revocation, expiry and current resource ACL run again.
                result = await super().execute(request, entry=entry)
                checks += 1
            return result
        finally:
            signal.waiters -= 1
