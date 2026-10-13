"""Versioned sync-feed waits without changing cursor, relevance or credential scope."""

import asyncio
import sys

from msg.core.codec import wire
from msg.core.errors import Failure


class ChangesWait:
    def __init__(self, client, *, mode='auto', once=False, interval=1, max_idle_interval=30):
        self.client = client
        self.mode = mode
        self.interval = interval
        self.idle_delay = interval
        self.idle_limit = max(interval, max_idle_interval)
        self.enabled = mode != 'poll' and (not once or mode == 'events')
        self.warned = False

    def unavailable(self, reason):
        if self.mode == 'events':
            raise Failure('listener_events_unavailable', details={'reason': reason})
        self.enabled = False
        if not self.warned:
            print(
                f'msg listen: signed changes wait unavailable ({reason}); using polling.',
                file=sys.stderr,
                flush=True,
            )
            self.warned = True

    async def read(self, arguments, *, wait=False):
        if self.enabled:
            try:
                result = self.client.checked(
                    await self.client.call(
                        'communication.changes',
                        {**arguments, 'wait_ms': 20000 if wait else 0},
                        contract_version=2,
                    )
                )
            except Failure as exc:
                # Only compatibility/scope failures permit the SAME legacy feed.
                # Revocation, expiry and ACL failures must stop the reader.
                if exc.code not in {'unknown_operation', 'credential_ceiling'}:
                    raise
                self.unavailable(exc.code)
            else:
                page = wire(result.data)
                if page.get('wait_supported') is not True:
                    self.unavailable('server_wait_unsupported')
                    if wait and not page['items'] and not page['has_more']:
                        await asyncio.sleep(self.idle_delay)
                        self.idle_delay = min(self.idle_delay * 2, self.idle_limit)
                return page
        if wait:
            await asyncio.sleep(self.idle_delay)
        page = wire(
            self.client.checked(await self.client.call('communication.changes', arguments)).data
        )
        if page['items']:
            self.idle_delay = self.interval
        elif wait and not page['has_more']:
            self.idle_delay = min(self.idle_delay * 2, self.idle_limit)
        return page
