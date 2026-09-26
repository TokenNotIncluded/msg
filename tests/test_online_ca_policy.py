"""The permanent online issuer has a fixed, ordinary-authority ceiling."""
from dataclasses import replace

import pytest

from msg.core.codec import canonical
from msg.core.errors import Failure
from msg.security.capabilities import grant_for
from msg.security.certificates import sign_certificate
from test_service import register


@pytest.mark.asyncio
@pytest.mark.parametrize('capability', (
    'cert.ca.issue', 'system.inspect', 'resource.purge',
    'tool.net.private', 'group.manage_override',
))
async def test_online_ca_rejects_forbidden_issue_grant_even_when_root_signed(installed, capability):
    app, root = installed
    async with app.metadata.transaction(write=True) as tx:
        online_id = tx.setting('online_ca_certificate')
        online = await tx.certificate(online_id)
        extra = grant_for(app.registry.capability(capability))
        issuance = replace(online.issuance,
                           issue_grants=(*online.issuance.issue_grants, extra))
        forged = sign_certificate(replace(online, issuance=issuance), root)
        tx.execute('UPDATE certificates SET body=? WHERE id=?',
                   (canonical(forged).decode(), online_id), write=True)
    async with app.metadata.transaction(write=False) as tx:
        with pytest.raises(Failure, match='online_ca_policy_exceeded'):
            await app.certificates.validate(online_id, tx)


@pytest.mark.asyncio
async def test_online_ca_rejects_extra_use_grant(installed):
    app, root = installed
    async with app.metadata.transaction(write=True) as tx:
        online_id = tx.setting('online_ca_certificate')
        online = await tx.certificate(online_id)
        extra = grant_for(app.registry.capability('cert.ca.issue'))
        forged = sign_certificate(replace(online, grants=(*online.grants, extra)), root)
        tx.execute('UPDATE certificates SET body=? WHERE id=?',
                   (canonical(forged).decode(), online_id), write=True)
    async with app.metadata.transaction(write=False) as tx:
        with pytest.raises(Failure, match='online_ca_policy_exceeded'):
            await app.certificates.validate(online_id, tx)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ('capability', 'delegation'))
async def test_online_ca_rejects_nonbasic_child_without_authority_source(installed, kind):
    app, _ = installed
    _, _, child_id = await register(app, f'online-{kind}')
    async with app.metadata.transaction(write=True) as tx:
        child = await tx.certificate(child_id)
        forged = sign_certificate(replace(child, kind=kind), app.online_signer)
        tx.execute('UPDATE certificates SET body=? WHERE id=?',
                   (canonical(forged).decode(), child_id), write=True)
    async with app.metadata.transaction(write=False) as tx:
        with pytest.raises(Failure, match='online_ca_policy_exceeded'):
            await app.certificates.validate(child_id, tx)
