from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from test_authorization import approve, scoped
from test_service import NOW, call, register

from msg.application import Application
from msg.core.codec import canonical, digest, loads, wire
from msg.core.errors import Failure
from msg.core.models import NetworkPolicy, ResourceRef, Revision
from msg.extensions.tools import descriptor, read_tool
from msg.plugins.common import new_id
from msg.security.network import intersect_policy, validate_addresses, validate_url
from msg.transports.dictionary import build_dictionary
from msg.transports.http import create_app
from msg.workers.effects import EffectWorker, ToolResult


def policy(**kw):
    return NetworkPolicy(
        schemes=frozenset({'http', 'https'}),
        hosts=(),
        ports=frozenset({80, 443}),
        methods=frozenset({'GET', 'HEAD'}),
        allow_private=kw.get('allow_private', False),
        timeout_ms=1000,
        max_response_bytes=10000,
        max_redirects=3,
    )


@pytest.mark.parametrize(
    'url',
    [
        'file:///etc/passwd',
        'http://user:pass@example.org',
        'http://example.org\\@127.0.0.1',
        'http://example.org:22/',
        'unix:///var/run/socket',
        'http://example.org/\nX:test',
    ],
)
def test_network_tool_rejects_non_http_or_ambiguous_targets(url):
    with pytest.raises(Failure):
        validate_url(url, 'GET', policy())


@pytest.mark.parametrize(
    'address',
    [
        '127.0.0.1',
        '10.2.3.4',
        '169.254.169.254',
        '::1',
        '::ffff:127.0.0.1',
        'fe80::1',
        '0.0.0.0',
        '100.64.0.1',
    ],
)
def test_public_tool_rejects_nonpublic_addresses(address):
    with pytest.raises(Failure, match='private_target_forbidden'):
        validate_addresses([address], policy())
    if address == '0.0.0.0':
        with pytest.raises(Failure, match='invalid_network_target'):
            validate_addresses([address], policy(allow_private=True))
    else:
        assert validate_addresses([address], policy(allow_private=True)) == [address]


def test_tool_policy_intersection_does_not_widen_allowlists_or_limits():
    restricted = intersect_policy(
        policy(), {'hosts': ['example.org'], 'methods': ['GET'], 'timeout_ms': 500}
    )
    assert validate_url('https://example.org/path', 'GET', restricted).hostname == 'example.org'
    assert restricted.timeout_ms == 500
    with pytest.raises(Failure):
        validate_url('https://other.example', 'GET', restricted)
    with pytest.raises(Failure):
        validate_url('https://example.org', 'HEAD', restricted)
    with pytest.raises(Failure):
        intersect_policy(policy(), {'ports': [22]})


@pytest.mark.asyncio
async def test_only_scoped_tool_is_visible_jobs_are_deduped_and_output_transfers(
    installed, tmp_path
):
    app, root = installed
    key, uid, base = await register(app, 'tool-agent')
    denied = await call(
        app,
        'tool.run',
        {'id': '/tools/dns', 'arguments': {'name': 'example.org', 'type': 'A'}},
        key=key,
        subject=uid,
    )
    assert denied.error.code == 'tool_certificate_required', wire(denied)
    cap = scoped(app, 'tool.use', 'tool_dns', app.registry.capability('tool.use').operations)
    cert = await approve(app, root, uid, key, (cap,))
    tools = await call(
        app, 'discovery.get', {'id': '/tools'}, key=key, subject=uid, certs=(cert.resource_id,)
    )
    assert [t['name'] for t in tools.data['items']] == ['dns']
    wrong = await call(
        app,
        'tool.run',
        {'id': '/tools/curl', 'arguments': {'url': 'https://example.org'}},
        key=key,
        subject=uid,
        certs=(cert.resource_id,),
    )
    assert wrong.error.code == 'tool_certificate_required'
    args = {'id': '/tools/dns', 'arguments': {'name': 'example.org', 'type': 'A'}}
    first = await call(
        app, 'tool.run', args, key=key, subject=uid, certs=(cert.resource_id,), rid='tool-once'
    )
    second = await call(
        app, 'tool.run', args, key=key, subject=uid, certs=(cert.resource_id,), rid='tool-once'
    )
    assert first.status == 'accepted' and second.replayed, wire(first)
    assert first.data['job_id'] == second.data['job_id']
    executions = []

    async def runner(tool, args, policy, outdir):
        executions.append(tool.executor_key)
        path = outdir / 'output.bin'
        path.write_bytes(canonical({'answers': ['93.184.216.34']}))
        return ToolResult(path=path, media_type='application/json', metadata={'kind': 'dns'})

    worker = EffectWorker(app, tool_runner=runner)
    assert await worker.run_once()
    assert not await worker.run_once()
    assert executions == ['dns']
    state = await call(
        app,
        'job.get',
        {'id': first.data['job_id']},
        key=key,
        subject=uid,
        certs=(cert.resource_id,),
    )
    assert state.data['state'] == 'done', wire(state)
    ref = state.data['output']
    download = await call(
        app,
        'transfer.open',
        {'direction': 'download', 'target': ref},
        key=key,
        subject=uid,
        certs=(cert.resource_id,),
    )
    assert download.status == 'ok', wire(download)


@pytest.mark.asyncio
async def test_pending_job_rechecks_revocation_and_expired_lease_is_uncertain(installed, tmp_path):
    app, root = installed
    key, uid, base = await register(app, 'worker-agent')
    cert = await approve(
        app,
        root,
        uid,
        key,
        (scoped(app, 'tool.use', 'tool_dns', app.registry.capability('tool.use').operations),),
    )
    result = await call(
        app,
        'tool.run',
        {'id': '/tools/dns', 'arguments': {'name': 'example.org', 'type': 'A'}},
        key=key,
        subject=uid,
        certs=(cert.resource_id,),
    )
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('UPDATE certificates SET revoked=1 WHERE id=?', (cert.resource_id,), write=True)
    ran = []

    async def runner(*args):
        ran.append(True)

    worker = EffectWorker(app, tool_runner=runner)
    await worker.run_once()
    assert not ran
    async with app.metadata.transaction(write=False) as tx:
        job = await tx.job(result.data['job_id'])
        assert job.state == 'failed'
    cert2 = await approve(
        app,
        root,
        uid,
        key,
        (scoped(app, 'tool.use', 'tool_dns', app.registry.capability('tool.use').operations),),
    )
    job_result = await call(
        app,
        'tool.run',
        {'id': '/tools/dns', 'arguments': {'name': 'example.org', 'type': 'A'}},
        key=key,
        subject=uid,
        certs=(cert2.resource_id,),
    )
    async with app.metadata.transaction(write=True) as tx:
        job = await tx.job(job_result.data['job_id'])
        await tx.save_job(replace(job, state='running', lease_until=NOW - timedelta(seconds=1)))
    await worker.run_once()
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.job(job.id)).state == 'uncertain'
    assert not ran


@pytest.mark.asyncio
async def test_tool_external_success_then_revocation_records_uncertain(installed):
    app, root = installed
    key, uid, _ = await register(app, 'late-revocation')
    cert = await approve(
        app,
        root,
        uid,
        key,
        (scoped(app, 'tool.use', 'tool_dns', app.registry.capability('tool.use').operations),),
    )
    result = await call(
        app,
        'tool.run',
        {'id': '/tools/dns', 'arguments': {'name': 'example.org', 'type': 'A'}},
        key=key,
        subject=uid,
        certs=(cert.resource_id,),
    )

    async def runner(tool, args, policies, outdir):
        async with app.metadata.transaction(write=True) as tx:
            tx.execute(
                'UPDATE certificates SET revoked=1 WHERE id=?', (cert.resource_id,), write=True
            )
        path = outdir / 'result.bin'
        path.write_bytes(b'[]')
        return ToolResult(path=path, media_type='application/json', metadata={})

    await EffectWorker(app, tool_runner=runner).run_once()
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.job(result.data['job_id'])).state == 'uncertain'


async def _claimed_dns_job(app, root, handle):
    key, uid, _ = await register(app, handle)
    cert = await approve(
        app,
        root,
        uid,
        key,
        (scoped(app, 'tool.use', 'tool_dns', app.registry.capability('tool.use').operations),),
    )
    result = await call(
        app,
        'tool.run',
        {'id': '/tools/dns', 'arguments': {'name': 'example.org', 'type': 'A'}},
        key=key,
        subject=uid,
        certs=(cert.resource_id,),
    )
    assert result.status == 'accepted', wire(result)
    async with app.metadata.transaction(write=False) as tx:
        return await tx.job(result.data['job_id'])


async def _enqueue_like(app, template, seconds_ago, **changes):
    job_id = new_id('job')
    job = replace(
        template,
        id=job_id,
        dedupe_key='test:' + job_id,
        state='pending',
        attempts=0,
        lease_until=None,
        next_attempt_at=NOW - timedelta(seconds=seconds_ago),
        **changes,
    )
    async with app.metadata.transaction(write=True) as tx:
        await tx.enqueue(job)
    return job


async def _tool_ref(app, rid):
    async with app.metadata.transaction(write=False) as tx:
        return wire(ResourceRef(id=rid, revision=(await tx.resource(rid)).revision))


async def _dns_revision_declaring(app, concurrency):
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource('tool_dns')
        blob = await app.contents.put_bytes(
            canonical({**descriptor('dns'), 'concurrency': concurrency}), 'application/json'
        )
        revision = Revision(
            format_version=1,
            id=new_id('v'),
            resource_id=resource.id,
            parents=(resource.revision,),
            content=blob,
            relations=(),
            actor=resource.owner,
            subject=resource.owner,
            author=resource.owner,
            created_at=app.clock(),
            manifest_digest='',
        )
        revision = replace(
            revision,
            manifest_digest=digest({
                k: v for k, v in wire(revision).items() if k not in {'manifest_digest', 'signature'}
            }),
        )
        await app.contents.pin(blob, revision.id)
        await app.contents.commit_revision(resource.parent, revision)
        await tx.append_revision(revision)
    return wire(ResourceRef(id=resource.id, revision=revision.id))


async def _state(app, job_id):
    async with app.metadata.transaction(write=False) as tx:
        return (await tx.job(job_id)).state


@pytest.mark.asyncio
async def test_historical_tool_revision_bytes_have_no_concurrency_and_mean_one(installed):
    app, _ = installed
    assert 'concurrency' not in descriptor('dns')
    async with app.metadata.transaction(write=False) as tx:
        rev = await tx.revision(ResourceRef(id='tool_dns'))
        stored = await app.contents.read_bytes(rev.content, limit=65536)
    assert stored == canonical(descriptor('dns'))
    assert 'concurrency' not in loads(stored)


@pytest.mark.asyncio
async def test_same_tool_is_not_claimed_by_a_second_worker_while_other_jobs_proceed(installed):
    app, root = installed
    held = await _claimed_dns_job(app, root, 'concurrency-holder')
    first = EffectWorker(app, tool_runner=lambda *a: None)
    claimed, execute = await first._claim()
    assert execute and claimed.id == held.id
    same_tool = await _enqueue_like(app, held, 3)
    other_tool = await _enqueue_like(
        app, held, 2, arguments={**held.arguments, 'tool': await _tool_ref(app, 'tool_curl')}
    )
    non_tool = await _enqueue_like(
        app,
        held,
        1,
        kind='mail',
        operation='communication.send',
        arguments={'recipient_subject': held.principal.subject},
    )
    peer = await Application(app.settings, clock=lambda: NOW).load()
    try:
        second = EffectWorker(peer, tool_runner=lambda *a: None)
        order = []
        for _ in range(2):
            job, execute = await second._claim()
            assert execute
            order.append(job.id)
        assert order == [other_tool.id, non_tool.id]
        assert await second._claim() == (None, False)
        assert await _state(app, same_tool.id) == 'pending'
        await first._finish(claimed, 'done', 'ok')
        job, execute = await second._claim()
        assert execute and job.id == same_tool.id
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_explicit_tool_concurrency_admits_up_to_the_declared_limit(installed):
    app, root = installed
    template = await _claimed_dns_job(app, root, 'concurrency-two')
    ref = await _dns_revision_declaring(app, 2)
    async with app.metadata.transaction(write=False) as tx:
        assert (await read_tool(app, tx, 'tool_dns', ref['revision'])).resource.revision == ref[
            'revision'
        ]
    async with app.metadata.transaction(write=True) as tx:
        await tx.save_job(replace(template, state='done'))
    jobs = [
        await _enqueue_like(app, template, 3 - i, arguments={**template.arguments, 'tool': ref})
        for i in range(3)
    ]
    peer = await Application(app.settings, clock=lambda: NOW).load()
    try:
        workers = (
            EffectWorker(app, tool_runner=lambda *a: None),
            EffectWorker(peer, tool_runner=lambda *a: None),
        )
        for worker, job in zip(workers, jobs, strict=False):
            claimed, execute = await worker._claim()
            assert execute and claimed.id == job.id
        for worker in workers:
            assert await worker._claim() == (None, False)
        assert await _state(app, jobs[2].id) == 'pending'
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_historical_limit_counts_running_jobs_across_revisions_of_one_tool(installed):
    app, root = installed
    template = await _claimed_dns_job(app, root, 'concurrency-mixed')
    ref = await _dns_revision_declaring(app, 2)
    worker = EffectWorker(app, tool_runner=lambda *a: None)
    claimed, _ = await worker._claim()
    assert claimed.id == template.id
    historical = await _enqueue_like(app, template, 2)
    declared = await _enqueue_like(app, template, 1, arguments={**template.arguments, 'tool': ref})
    job, execute = await worker._claim()
    assert execute and job.id == declared.id
    assert await worker._claim() == (None, False)
    assert await _state(app, historical.id) == 'pending'


@pytest.mark.asyncio
@pytest.mark.parametrize('declared', [0, 33, -1, True, '2', 1.5, None])
async def test_invalid_declared_tool_concurrency_is_rejected_not_unbounded(installed, declared):
    app, root = installed
    template = await _claimed_dns_job(app, root, 'concurrency-invalid')
    ref = await _dns_revision_declaring(app, declared)
    async with app.metadata.transaction(write=False) as tx:
        with pytest.raises(Failure, match='invalid_tool_concurrency'):
            await read_tool(app, tx, 'tool_dns', ref['revision'])
    async with app.metadata.transaction(write=True) as tx:
        await tx.save_job(replace(template, state='done'))
    bad = await _enqueue_like(app, template, 1, arguments={**template.arguments, 'tool': ref})
    ran = []

    async def runner(*args):
        ran.append(args)

    worker = EffectWorker(app, tool_runner=runner)
    job, execute = await worker._claim()
    assert job.id == bad.id and not execute
    assert await _state(app, bad.id) == 'failed' and not ran
    async with app.metadata.transaction(write=False) as tx:
        assert tx.setting('job_status:' + bad.id) == {'code': 'invalid_tool_concurrency'}
    assert await worker._claim() == (None, False)


@pytest.mark.asyncio
async def test_tool_run_is_the_only_public_operation_and_old_short_code_is_retired(installed):
    app, _ = installed
    dictionary = build_dictionary(app.registry)
    new_code = dictionary.code_for('operation', 'tool.run@1')
    assert dictionary.resolve_operation(new_code).name == 'tool.run'
    assert dictionary.lookup_document(new_code)['operations'][0]['name'] == 'tool.run'
    retired = dictionary.lookup_document('o7hu7ftf')
    assert retired['deprecated'] and retired['identity'] == 'tool.invoke@1'
    with pytest.raises(Failure, match='deprecated_short_code'):
        dictionary.resolve_operation('o7hu7ftf')
    with pytest.raises(Failure, match='unknown_operation'):
        app.registry.operation('tool.invoke')
    assert descriptor('dns')['operation'] == 'tool.run'
    async with app.metadata.transaction(write=False) as tx:
        assert (await read_tool(app, tx, 'tool_dns')).operation == 'tool.run'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        current = await http.get('/-/d/tool.run')
        assert current.status_code == 200
        assert current.json()['operations'][0]['code'] == new_code
        old = await http.get('/-/d/o7hu7ftf')
        assert old.status_code == 200
        assert old.json()['deprecated'] is True
        rejected = await http.get('/-/g/o7hu7ftf/j/invalid')
        assert rejected.status_code == 400
        assert rejected.json()['error']['code'] == 'deprecated_short_code'
