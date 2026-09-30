"""The database read-only hosting runtime shares the strict alias ingress boundary."""

from dataclasses import replace

import httpx
import pytest
from test_hosting_runtime import reader_settings as reader_settings, website

from msg.extensions.hosting import hosting_app
from msg.hosting_runtime import HostingRuntime

ALIAS = 'http://alias.example.org:8042'


@pytest.mark.asyncio
async def test_readonly_hosting_alias_preserves_sandbox_and_rejects_ambiguous_hosts(
    installed, reader_settings
):
    app, _ = installed
    await website(app)
    runtime = HostingRuntime(replace(reader_settings, service_aliases=(ALIAS,)))
    try:
        await runtime.load()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=hosting_app(runtime)), base_url=ALIAS
        ) as http:
            response = await http.get('/@readonly-host/web/')
            assert response.status_code == 200 and response.content == b'<h1>published</h1>', (
                response.text
            )
            assert 'sandbox' in response.headers['content-security-policy']
            assert 'allow-same-origin' not in response.headers['content-security-policy']
            assert 'set-cookie' not in response.headers
            for headers in (
                {'Host': 'testserver:9999'},
                {'Host': 'alias.example.org'},
                {'Host': 'unknown.example.org:8042'},
                [('Host', 'alias.example.org:8042'), ('Host', 'testserver')],
            ):
                denied = await http.get('/@readonly-host/web/', headers=headers)
                assert denied.status_code == 403, denied.text
                assert b'published' not in denied.content
            allowed = await http.get('/@readonly-host/web/', headers={'Origin': 'null'})
            assert allowed.status_code == 200
            unsafe = await http.get('/@readonly-host/web/?token=must-not-echo')
            assert unsafe.status_code != 200 and b'must-not-echo' not in unsafe.content
    finally:
        await runtime.close()
