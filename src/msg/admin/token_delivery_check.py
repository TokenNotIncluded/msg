"""Exercise one-use credential release only on the caller's isolated Test Root."""
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from msg.client import ClientState, MsgClient
from msg.core.errors import Failure
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


async def check_token_delivery(app, now):
    with TemporaryDirectory(prefix='msg-token-selftest-') as directory:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app))) as http:
            state = ClientState(Path(directory), server=app.settings.service_url)
            transport = HTTPTransport(state.server, http=http)
            client = MsgClient(state, transport, clock=lambda: now, retries=0)
            created = await client.temporary()
            if created.status != 'ok':
                return False
            original = state.token
            send = transport.call

            async def discard(packet):
                result = await send(packet)
                if result.status != 'ok':
                    raise Failure('selftest_issuance_failed')
                raise httpx.ReadTimeout('selftest response loss')

            transport.call = discard
            for attempt in (client.rotate_token, client.recover_token):
                try:
                    await attempt()
                    return False
                except Failure as exc:
                    if exc.code != 'transport_uncertain':
                        return False
            # Both processes can restart; refreshing only the request expiry
            # does not refresh either the token lifetime or recovery deadline.
            state = ClientState(Path(directory), server=app.settings.service_url)
            client = MsgClient(state, HTTPTransport(state.server, http=http),
                               clock=lambda: now+timedelta(seconds=1), retries=0)
            replay = await client.recover_token()
            if replay.status != 'error' or replay.error.code != 'token_delivery_unavailable':
                return False
            completed = await client.recover_token()
            if completed.status != 'ok' or not state.token or state.token[0] == original[0]:
                return False
            async with app.metadata.transaction(write=False) as tx:
                old = await tx.credential(original[0])
                current = await tx.credential(state.token[0])
            return old.revoked_at is not None and current.revoked_at is None and client._token_journal() == (None, None)
