"""Collaboration views obey current visibility, including after target moves."""
import pytest

from msg.core.codec import wire
from test_service import call, register


@pytest.mark.asyncio
async def test_presence_rechecks_current_subject_permissions(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'private-presence')
    created = await call(app, 'communication.presence_set',
                         {'state': 'busy', 'message': 'private activity'},
                         key=key, subject=subject)
    assert created.status == 'ok', wire(created)
    assert (await call(app, 'communication.presence_get',
                       {'subject_id': subject})).data['message'] == 'private activity'
    async with app.metadata.transaction(write=False) as tx:
        generation = (await tx.resource(subject)).generation
    changed = await call(app, 'content.chmod', {'id': subject, 'mode': '0700'},
                         key=key, subject=subject, expected=((subject, generation),))
    assert changed.status == 'ok', changed.error
    hidden = await call(app, 'communication.presence_get', {'subject_id': subject})
    assert hidden.status == 'error', wire(hidden)
    assert 'private activity' not in str(wire(hidden))
    own = await call(app, 'communication.presence_get', {'subject_id': subject},
                     key=key, subject=subject)
    assert own.data['message'] == 'private activity'


@pytest.mark.asyncio
async def test_handoff_filters_references_moved_to_forbidden_namespace(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'handoff-move-owner')
    other_key, other, _ = await register(app, 'handoff-move-other')
    post = await call(app, 'content.post_create', {'parent': '/main', 'body': 'Work'},
                      key=key, subject=subject)
    rid = post.resources[0].id
    created = await call(app, 'communication.handoff_create',
                         {'to_subject': other, 'resource_refs': [rid]},
                         key=key, subject=subject)
    hid = created.data['handoff']['id']
    moved = await call(app, 'content.move',
                       {'id': rid, 'parent': subject, 'name': 'todos'},
                       key=key, subject=subject,
                       expected=((rid, post.data['generation']),))
    assert moved.status == 'ok', moved.error
    for actor_key, actor in ((key, subject), (other_key, other)):
        got = await call(app, 'communication.handoff_get', {'id': hid},
                         key=actor_key, subject=actor)
        assert got.status == 'ok', wire(got)
        assert not got.data['handoff']['resource_refs']
        listed = await call(app, 'communication.handoff_list', {},
                            key=actor_key, subject=actor)
        assert not listed.data['items'][0]['resource_refs']
