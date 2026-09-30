"""A vault identity binding is not a request grant or an unlimited ceiling."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from msg.core.codec import canonical, wire
from msg.core.errors import Failure
from msg.core.models import CapabilityGrant, Scope
from msg.security.oauth import require_binding, require_source

NOW = datetime(2026, 9, 27, tzinfo=UTC)


def grant(*operations, descendants=True):
    return CapabilityGrant(
        capability='content',
        version=1,
        scope=Scope(resource_id='root', descendants=descendants),
        operations=frozenset(operations),
        constraints={},
    )


class SourceSession:
    def __init__(self, *, custodial=True):
        self.parent = SimpleNamespace(
            id='key',
            subject_id='user',
            revoked_at=None,
            not_before=NOW,
            expires_at=None,
            ceiling=() if custodial else (grant('read'),),
        )
        self.user = SimpleNamespace(
            resource_id='user',
            local_only=False,
            auth_version=0,
            kind='custodial' if custodial else 'registered',
        )
        self.vault = ('key', 'active')
        self.body = {
            'parent': 'key',
            'subject': 'user',
            'auth_version': 0,
            'custodial': custodial,
            'ceiling': wire((grant('read'),)),
        }
        self.api = False

    async def credential(self, _id):
        return self.parent

    async def subject(self, _id):
        return self.user

    def one(self, query, args):
        if 'custodial_vault' in query:
            return self.vault
        if self.api and args[0] == 'api:t_api_test':
            return (canonical(self.body).decode(),)
        return None


@pytest.mark.asyncio
async def test_active_vault_with_empty_request_ceiling_preserves_captured_policy():
    tx = SourceSession()
    assert await require_source(tx, tx.body, NOW, custodial_ceiling=(grant('read'),)) is tx.user
    assert tx.parent.ceiling == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'fault', ['empty_policy', 'narrow_scope', 'revoked', 'inactive', 'foreign_vault']
)
async def test_custodial_source_still_fails_closed_on_current_authority_loss(fault):
    tx = SourceSession()
    policy = (grant('read'),)
    if fault == 'empty_policy':
        policy = ()
    elif fault == 'narrow_scope':
        policy = (grant('read', descendants=False),)
    elif fault == 'revoked':
        tx.parent.revoked_at = NOW
    elif fault == 'inactive':
        tx.vault = ('key', 'destroyed')
    else:
        tx.vault = ('other-key', 'active')
    with pytest.raises(Failure, match='invalid_grant'):
        await require_source(tx, tx.body, NOW, custodial_ceiling=policy)


@pytest.mark.asyncio
async def test_empty_registered_key_ceiling_never_uses_custodial_policy():
    tx = SourceSession(custodial=False)
    tx.parent.ceiling = ()
    with pytest.raises(Failure, match='invalid_grant'):
        await require_source(tx, tx.body, NOW, custodial_ceiling=(grant('read'),))


@pytest.mark.asyncio
async def test_registered_source_remains_independent_of_custodial_policy():
    tx = SourceSession(custodial=False)
    assert await require_source(tx, tx.body, NOW, custodial_ceiling=()) is tx.user


@pytest.mark.asyncio
@pytest.mark.parametrize('expanded', [False, True])
async def test_derived_credential_cannot_exceed_captured_custodial_grants(expanded):
    tx = SourceSession()
    tx.api = True
    credential = SimpleNamespace(
        id='t_api_test',
        source_credential_id='key',
        ceiling=(grant('read', 'write') if expanded else grant('read'),),
    )
    if expanded:
        with pytest.raises(Failure, match='invalid_grant'):
            await require_binding(
                tx, credential, NOW, None, custodial_ceiling=(grant('read', 'write'),)
            )
    else:
        await require_binding(
            tx, credential, NOW, None, custodial_ceiling=(grant('read', 'write'),)
        )
