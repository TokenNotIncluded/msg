"""A public listing must not monopolize the HTTP event loop during ACL work."""

import asyncio
import time
from datetime import timedelta

import httpx
from test_service import NOW

from msg.core.codec import b64, canonical
from msg.core.requests import request_for
from msg.transports.http import create_app


async def test_health_responds_while_listing_checks_visibility(installed, monkeypatch):
    app, _ = installed
    started = asyncio.Event()
    original = app.authorizer.require

    async def costly_acl(context, request, checks, session):
        if request.operation == 'discovery.list' and any(x.check == 'read' for x in checks):
            started.set()
            # Model the synchronous SQL/certificate work that a large listing
            # performs between actual awaits, while retaining every real ACL.
            end = time.monotonic() + 0.005
            while time.monotonic() < end:
                pass
        return await original(context, request, checks, session)

    monkeypatch.setattr(app.authorizer, 'require', costly_acl)
    packet = request_for(
        'discovery.list',
        {'limit': 200, 'fields': ['id']},
        app.settings.service_url,
        expires_at=NOW + timedelta(seconds=90),
    )
    path = '/-/g/discovery.list/j/' + b64(canonical(packet))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        assert (await http.get('/healthz')).status_code == 200
        listing = asyncio.create_task(http.get(path))
        try:
            await asyncio.wait_for(started.wait(), 5)
            assert not listing.done(), 'ACL work blocked all concurrent HTTP handling'
            health = await http.get('/healthz')
            assert health.status_code == 200
            assert not listing.done(), 'Health must respond before the listing finishes'
            response = await asyncio.wait_for(listing, 5)
            assert response.status_code == 200, response.text
            assert response.json()['status'] == 'ok'
        finally:
            if not listing.done():
                listing.cancel()
                await asyncio.gather(listing, return_exceptions=True)
