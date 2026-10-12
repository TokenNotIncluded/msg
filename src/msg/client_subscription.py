"""Authorized long-poll subscriptions using the normal signed client and cursors."""

import asyncio
import sys

from msg.core.codec import wire
from msg.core.errors import require


class Subscription:
    def __init__(self, client, *, resource=None, once=False, interval=1):
        self.client = client
        self.resource = resource
        self.once = once
        self.interval = interval

    async def _read(self, cursor, *, tail=False, wait=False, limit=50):
        args = {'limit': limit, 'wait_ms': 20000 if wait else 0}
        if self.resource is not None:
            args['resource'] = self.resource
        if cursor is not None:
            args['cursor'] = cursor
        if tail:
            args['tail'] = True
        result = self.client.checked(await self.client.call(
            'communication.events', args, contract_version=2,
        ))
        return wire(result.data)

    async def fetch(self, cursor, tail=False):
        page = await self._read(cursor, tail=tail, wait=not (self.once or tail))
        if page.get('authority_changed'):
            print(
                'msg listen: authority changed; position retained. Refresh derived resource state.',
                file=sys.stderr, flush=True,
            )
        if (
            not self.once and not tail and not page['items'] and not page['has_more']
            and not page.get('wait_supported')
        ):
            # Explicitly advertised fallback, not a silent switch of source or account.
            await asyncio.sleep(self.interval)
        return page

    async def revalidate(self, item):
        # Never output a durable pending body under yesterday's authority. The
        # protected before-cursor lets the server redo the same selection today.
        cursor = item.get('before_cursor')
        require(isinstance(cursor, str), 'invalid_listener_checkpoint')
        while True:
            page = await self._read(cursor, limit=1)
            for current in page['items']:
                if current['id'] == item['id']:
                    return current
                if current['seq'] > item['seq']:
                    return None
            if not page['has_more']:
                return None
            require(page['cursor'] != cursor, 'invalid_listener_page')
            cursor = page['cursor']
