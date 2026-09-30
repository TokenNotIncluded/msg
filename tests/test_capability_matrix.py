"""Pure authorization matrix with principals produced by real authentication."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_authorization import approve, scoped
from test_service import NOW, register

from msg.core.errors import Failure
from msg.core.registry import Registry
from msg.core.requests import request_for
from msg.plugins.common import registration
from msg.security.capabilities import base_grants, install_capabilities

SPECIAL = (
    'resource.certified_write',
    'resource.read_override',
    'resource.write_override',
    'resource.chmod_override',
    'resource.chgrp_override',
    'resource.chown',
    'resource.purge',
    'identity.recover',
    'group.manage_override',
    'tool.use',
    'tool.net.private',
    'system.namespace',
    'cert.issue',
    'cert.revoke',
    'cert.ca.issue',
    'system.inspect',
    'system.config',
    'system.maintenance',
)


def test_special_operation_versions_never_enter_ordinary_base_grants():
    registry = Registry()
    op, finish = registration(SimpleNamespace(registry=registry), 'content')

    async def unused(*args):
        raise AssertionError('capability assembly must not run business handlers')

    for name in ('content.purge', 'content.chown', 'identity.recover', 'content.post_create'):
        for version in (1, 2):
            op(name, {'type': 'object'}, version=version)(unused)
    finish()
    install_capabilities(registry)
    ordinary = {operation for grant in base_grants(registry) for operation in grant.operations}
    assert ordinary == {'content.post_create@1', 'content.post_create@2'}
    for name, capability in (
        ('content.purge', 'resource.purge'),
        ('content.chown', 'resource.chown'),
        ('identity.recover', 'identity.recover'),
    ):
        assert registry.capability(capability).operations == {name + '@1', name + '@2'}


@pytest.mark.asyncio
@pytest.mark.parametrize('name', SPECIAL)
async def test_special_capability_requires_exact_grant_scope_operation_and_live_chain(
    installed, name
):
    app, root = installed
    key, uid, _ = await register(app, 'matrix-agent')
    operation = sorted(app.registry.capability(name).operations)[0]
    grant = scoped(app, name, 't_main', (operation,))
    cert = await approve(app, root, uid, key, (grant,))
    expired = await approve(app, root, uid, key, (grant,))
    async with app.metadata.transaction(write=False) as tx:
        request = request_for(
            'discovery.get',
            {'id': '/main'},
            app.settings.service_url,
            signer=key,
            subject=uid,
            expires_at=NOW + timedelta(seconds=90),
        )
        ordinary = await app.authenticator.authenticate(request, tx, entry='network')
        assert not await app.authorizer.has(ordinary, name, operation, 't_main', tx)
        signed = request_for(
            'discovery.get',
            {'id': '/main'},
            app.settings.service_url,
            signer=key,
            subject=uid,
            certificates=(cert.resource_id,),
            expires_at=NOW + timedelta(seconds=90),
        )
        principal = await app.authenticator.authenticate(signed, tx, entry='network')
        assert await app.authorizer.has(principal, name, operation, 't_main', tx)
        assert not await app.authorizer.has(principal, name, operation, 't_intro', tx)
        assert not await app.authorizer.has(principal, name, 'unknown.operation@1', 't_main', tx)
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('UPDATE certificates SET revoked=1 WHERE id=?', (cert.resource_id,), write=True)
    async with app.metadata.transaction(write=False) as tx:
        with pytest.raises(Failure):
            await app.authorizer.has(principal, name, operation, 't_main', tx)
        app.certificates.clock = lambda: expired.expires_at + timedelta(seconds=1)
        with pytest.raises(Failure):
            await app.certificates.validate(expired.resource_id, tx)
