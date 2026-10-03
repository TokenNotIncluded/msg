"""Public topology avoids duplicate anonymous checks, retaining current ACLs."""

import time
from collections import Counter
from dataclasses import replace
from datetime import timedelta

from test_service import call, register

from msg.core.codec import canonical, wire
from msg.core.models import ExecutionContext
from msg.core.requests import request_for
from msg.plugins import agent_follows


async def project(app, *, signer=None, subject=None):
    request = request_for(
        'discovery.get',
        {'id': 'u_root', 'fields': ['id']},
        app.settings.service_url,
        signer=signer,
        subject=subject,
        expires_at=app.clock() + timedelta(seconds=120),
    )
    async with app.metadata.transaction(write=False) as tx:
        principal = await app.executor.authenticator.authenticate(request, tx, entry='network')
        context = ExecutionContext(
            request_id=request.request_id,
            principal=principal,
            entry='network',
            now=app.clock(),
            deadline_monotonic=time.monotonic() + 30,
        )
        return await agent_follows.public_topology(app, context, request, tx)


def record_visibility(monkeypatch):
    entries = []
    original = agent_follows.visible

    async def recorded(app, context, request, tx, rid):
        decision = await original(app, context, request, tx, rid)
        entries.append((rid, context.principal.subject, decision))
        return decision

    monkeypatch.setattr(agent_follows, 'visible', recorded)
    return entries


async def test_anonymous_topology_checks_each_candidate_once_and_rechecks_new_acl(
    installed, monkeypatch
):
    app, _ = installed
    _, first, _ = await register(app, 'topology-first')
    _, private, _ = await register(app, 'topology-private')
    entries = record_visibility(monkeypatch)
    before = await project(app)
    assert {first, private} <= set(before['nodes'])
    assert all(count == 1 for count in Counter(rid for rid, _, _ in entries).values())
    assert {subject for _, subject, _ in entries} == {None}

    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(private)
        await tx.replace(
            replace(resource, mode=0o700, generation=resource.generation + 1), resource.generation
        )
    entries.clear()
    after = await project(app)
    assert first in after['nodes']
    assert private not in canonical(after).decode()
    assert all(count == 1 for count in Counter(rid for rid, _, _ in entries).values())
    assert (private, None, False) in entries


async def test_signed_topology_keeps_public_and_callers_own_checks_and_blocks(
    installed, monkeypatch
):
    app, _ = installed
    key, reader, _ = await register(app, 'topology-reader')
    _, other, _ = await register(app, 'topology-other')
    entries = record_visibility(monkeypatch)
    public = await project(app)
    entries.clear()
    signed = await project(app, signer=key, subject=reader)
    assert canonical(signed) == canonical(public)
    for rid in signed['nodes']:
        assert [(subject, shown) for item, subject, shown in entries if item == rid] == [
            (None, True),
            (reader, True),
        ]

    result = await call(
        app, 'communication.dm_block', {'subject_id': other}, key=key, subject=reader
    )
    assert result.status == 'ok', wire(result)
    entries.clear()
    blocked = await project(app, signer=key, subject=reader)
    assert other not in canonical(blocked).decode()
    assert not any(rid == other for rid, _, _ in entries)
    assert other in (await project(app))['nodes']
