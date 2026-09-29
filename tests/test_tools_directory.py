"""Canonical /tools path and the certificate boundary on a fresh install."""

from dataclasses import replace

import pytest
from test_authorization import approve, scoped
from test_service import NOW, call, register

from msg.bootstrap import bootstrap
from msg.core.codec import wire
from msg.core.errors import Failure


@pytest.mark.asyncio
async def test_new_install_tools_path_keeps_permissions_and_capability_gate(installed):
    app, root = installed
    async with app.metadata.transaction(write=False) as tx:
        assert await tx.resolve('/tools') == 't_tools'
        assert await tx.resolve('/tools/dns') == 'tool_dns'
        with pytest.raises(Failure, match='not_found'):
            await tx.resolve('/_tools')
        directory = await tx.resource('t_tools')
        dns = await tx.resource('tool_dns')
        assert (directory.owner, directory.group, directory.mode) == ('u_root', 'g_admins', 0o500)
        assert (dns.owner, dns.group, dns.mode) == ('u_root', 'g_admins', 0o400)

    key, uid, _ = await register(app, 'tools-directory')
    args = {'id': '/tools/dns', 'arguments': {'name': 'example.org', 'type': 'A'}}
    denied = await call(app, 'tool.run', args, key=key, subject=uid)
    assert denied.error.code == 'tool_certificate_required', wire(denied)
    cap = scoped(app, 'tool.use', 'tool_dns', app.registry.capability('tool.use').operations)
    cert = await approve(app, root, uid, key, (cap,))
    listing = await call(
        app, 'discovery.get', {'id': '/tools'}, key=key, subject=uid, certs=(cert.resource_id,)
    )
    assert [tool['name'] for tool in listing.data['items']] == ['dns']
    accepted = await call(app, 'tool.run', args, key=key, subject=uid, certs=(cert.resource_id,))
    assert accepted.status == 'accepted', wire(accepted)


@pytest.mark.asyncio
async def test_legacy_tools_path_is_read_only_and_new_alias_still_checks_certificate(installed):
    app, root = installed
    async with app.metadata.transaction(write=True) as tx:
        current = await tx.resource('t_tools')
        await tx.replace(
            replace(current, name='_tools', generation=current.generation + 1), current.generation
        )
    await bootstrap(app.metadata, app.contents, app.registry, NOW)
    key, uid, _ = await register(app, 'legacy-tools-directory')
    cap = scoped(app, 'tool.use', 'tool_dns', app.registry.capability('tool.use').operations)
    cert = await approve(app, root, uid, key, (cap,))
    legacy_read = await call(
        app, 'discovery.get', {'id': '/_tools'}, key=key, subject=uid, certs=(cert.resource_id,)
    )
    assert [tool['name'] for tool in legacy_read.data['items']] == ['dns']
    args = {'id': '/_tools/dns', 'arguments': {'name': 'example.org', 'type': 'A'}}
    rejected = await call(app, 'tool.run', args, key=key, subject=uid, certs=(cert.resource_id,))
    assert rejected.error.code == 'legacy_tool_path_read_only', wire(rejected)
    canonical = {**args, 'id': '/tools/dns'}
    allowed = await call(
        app, 'tool.run', canonical, key=key, subject=uid, certs=(cert.resource_id,)
    )
    assert allowed.status == 'accepted', wire(allowed)
