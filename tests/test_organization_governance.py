"""Organization governance through signed requests and the real PostgreSQL store."""
import asyncio
import json

import pytest

from msg.core.codec import wire
from test_service import call, register


async def invoke(app, operation, arguments, identity):
    key, subject, _ = identity
    return await call(app, operation, arguments, key=key, subject=subject)


def ok(result):
    assert result.status == 'ok', wire(result)
    return result


@pytest.mark.asyncio
async def test_group_policies_roles_and_immediate_revocation(installed):
    app, _ = installed
    owner = await register(app, 'org-owner')
    maintainer = await register(app, 'org-maintainer')
    applicant = await register(app, 'org-applicant')
    outsider = await register(app, 'org-outsider')
    group = ok(await invoke(app, 'group.create', {'name': 'governance'}, owner)).resources[0].id
    assert (await invoke(app, 'group.policy_get', {'group': group}, outsider)).data['membership_policy'] == 'invite'

    denied = await invoke(app, 'group.join', {'group': group}, applicant)
    assert denied.error.code == 'group_invitation_required'
    ok(await invoke(app, 'group.invite', {'group': group, 'subject': maintainer[1]}, owner))
    ok(await invoke(app, 'group.join', {'group': group}, maintainer))
    ok(await invoke(app, 'group.role_set', {'group': group, 'subject': maintainer[1],
                                            'role': 'maintainer'}, owner))
    denied = await invoke(app, 'group.policy_set', {'group': group, 'policy': 'approval'}, maintainer)
    assert denied.error.code == 'group_admin_required'
    denied = await invoke(app, 'group.role_set', {'group': group, 'subject': outsider[1],
                                                   'role': 'maintainer'}, maintainer)
    assert denied.error.code == 'group_admin_required'
    ok(await invoke(app, 'group.policy_set', {'group': group, 'policy': 'approval'}, owner))
    pending = ok(await invoke(app, 'group.join', {'group': group}, applicant))
    assert pending.data['status'] == 'pending'
    denied = await invoke(app, 'content.topic_create', {'parent': group, 'name': 'before-approval'}, applicant)
    assert denied.error.code == 'permission_denied'
    ok(await invoke(app, 'group.approve', {'group': group, 'subject': applicant[1]}, maintainer))
    created = ok(await invoke(app, 'content.topic_create', {'parent': group, 'name': 'after-approval'}, applicant))
    assert created.resources[0].id
    private = ok(await invoke(app, 'content.topic_create', {'parent': group, 'name': 'members-only'}, owner))
    locked = await call(app, 'content.chmod', {'id': private.resources[0].id, 'mode': '0770'},
                        key=owner[0], subject=owner[1],
                        expected=((private.resources[0].id, private.data['generation']),))
    ok(locked)
    ok(await invoke(app, 'discovery.get', {'id': private.resources[0].id}, applicant))
    ok(await invoke(app, 'group.remove', {'group': group, 'subject': applicant[1]}, maintainer))
    denied = await invoke(app, 'content.topic_create', {'parent': group, 'name': 'after-removal'}, applicant)
    assert denied.error.code == 'permission_denied'
    denied = await invoke(app, 'discovery.get', {'id': private.resources[0].id}, applicant)
    assert denied.error.code == 'permission_denied'

    ok(await invoke(app, 'group.policy_set', {'group': group, 'policy': 'managed'}, owner))
    denied = await invoke(app, 'group.join', {'group': group}, outsider)
    assert denied.error.code == 'group_managed'
    ok(await invoke(app, 'group.invite', {'group': group, 'subject': outsider[1]}, maintainer))
    denied = await invoke(app, 'group.join', {'group': group}, outsider)
    assert denied.error.code == 'group_managed'
    ok(await invoke(app, 'group.member.add', {'group': group, 'subject': outsider[1]}, owner))
    ok(await invoke(app, 'group.leave', {'group': group}, outsider))
    ok(await invoke(app, 'group.policy_set', {'group': group, 'policy': 'open'}, owner))
    assert ok(await invoke(app, 'group.join', {'group': group}, outsider)).data['status'] == 'active'


@pytest.mark.asyncio
async def test_owner_transfer_last_owner_and_manager_boundary(installed):
    app, _ = installed
    owner = await register(app, 'owner-transfer')
    member = await register(app, 'member-transfer')
    other = await register(app, 'other-transfer')
    group = ok(await invoke(app, 'group.create', {'name': 'ownership'}, owner)).resources[0].id
    for operation, arguments in (
        ('group.leave', {'group': group}),
        ('group.remove', {'group': group, 'subject': owner[1]}),
        ('group.role_set', {'group': group, 'subject': owner[1], 'role': 'member'}),
        ('group.member.remove', {'group': group, 'subject': owner[1]}),
    ):
        denied = await invoke(app, operation, arguments, owner)
        assert denied.error.code in {'cannot_remove_group_owner', 'cannot_demote_group_owner'}
    ok(await invoke(app, 'group.member.add', {'group': group, 'subject': member[1]}, owner))
    ok(await invoke(app, 'group.join', {'group': group}, member))
    ok(await invoke(app, 'group.owner_transfer', {'group': group, 'subject': member[1]}, owner))
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource(group)).owner == member[1]
        rows = {entry.subject_id: entry.role for entry in await tx.memberships(owner[1])
                if entry.organization_id == group}
        assert rows == {owner[1]: 'maintainer'}
        assert next(entry.role for entry in await tx.memberships(member[1])
                    if entry.organization_id == group) == 'owner'
    denied = await invoke(app, 'group.policy_set', {'group': group, 'policy': 'open'}, owner)
    assert denied.error.code == 'group_admin_required'
    ok(await invoke(app, 'group.policy_set', {'group': group, 'policy': 'open'}, member))
    ok(await invoke(app, 'group.leave', {'group': group}, owner))
    denied = await invoke(app, 'group.leave', {'group': group}, member)
    assert denied.error.code == 'cannot_remove_group_owner'
    denied = await invoke(app, 'group.invite', {'group': group, 'subject': member[1]}, other)
    assert denied.error.code == 'group_admin_required'


@pytest.mark.asyncio
async def test_public_virtual_and_concurrent_decisions(installed):
    app, _ = installed
    owner = await register(app, 'public-owner')
    applicant = await register(app, 'public-applicant')
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT 1 FROM memberships WHERE org=? AND subject=?',
                      ('g_public', applicant[1])) is None
        memberships = await tx.memberships(applicant[1])
        assert [member.organization_id for member in memberships].count('g_public') == 1
    group = ok(await invoke(app, 'group.create', {'name': 'concurrent'}, owner)).resources[0].id
    ok(await invoke(app, 'group.policy_set', {'group': group, 'policy': 'approval'}, owner))
    ok(await invoke(app, 'group.join', {'group': group}, applicant))
    approve, reject = await asyncio.gather(
        invoke(app, 'group.approve', {'group': group, 'subject': applicant[1]}, owner),
        invoke(app, 'group.reject', {'group': group, 'subject': applicant[1]}, owner))
    assert sorted(result.status for result in (approve, reject)) == ['error', 'ok']
    loser = next(result for result in (approve, reject) if result.error)
    assert loser.error.code == 'group_request_not_pending'
    async with app.metadata.transaction(write=False) as tx:
        member = tx.one('SELECT body FROM memberships WHERE org=? AND subject=?',
                        (group, applicant[1]))
        assert member is not None


@pytest.mark.asyncio
async def test_legacy_add_requires_acceptance_for_new_invite_groups(installed):
    app, _ = installed
    owner = await register(app, 'legacy-add-owner')
    invitee = await register(app, 'legacy-add-invitee')
    other = await register(app, 'legacy-add-other')
    group = ok(await invoke(app, 'group.create', {'name': 'new-invite'}, owner)).resources[0].id
    added = ok(await invoke(app, 'group.member.add', {'group': group, 'subject': invitee[1]}, owner))
    assert added.data['status'] == 'invited'
    denied = await invoke(app, 'content.topic_create', {'parent': group, 'name': 'before-accept'}, invitee)
    assert denied.error.code == 'permission_denied'
    denied = await invoke(app, 'group.admin.add', {'group': group, 'subject': other[1]}, owner)
    assert denied.error.code == 'group_membership_not_active'
    ok(await invoke(app, 'group.join', {'group': group}, invitee))
    ok(await invoke(app, 'content.topic_create', {'parent': group, 'name': 'after-accept'}, invitee))

    # A pre-policy organization keeps the historical direct-add behavior until
    # its owner explicitly selects a policy. Old JSON records decode safely.
    legacy = ok(await invoke(app, 'group.create', {'name': 'pre-policy'}, owner)).resources[0].id
    async with app.metadata.transaction(write=True) as tx:
        body = json.loads(tx.one("SELECT body FROM identities WHERE id=?", (legacy,))[0])
        body.pop('membership_policy')
        tx.execute("UPDATE identities SET body=? WHERE id=?", (json.dumps(body), legacy), write=True)
    added = ok(await invoke(app, 'group.member.add', {'group': legacy, 'subject': other[1]}, owner))
    assert added.data['status'] == 'active'
