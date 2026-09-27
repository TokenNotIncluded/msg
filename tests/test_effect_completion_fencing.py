"""Every completion uses the same stored lease/attempt fence.

Maintenance doubles below isolate state transitions; the tool/Git tests exercise
signed Operations, real PostgreSQL and local Git/CAS, without external traffic.
"""
from dataclasses import replace
from datetime import timedelta

import pytest

from msg.core.models import EffectJob, Principal
from msg.workers.effects import EffectWorker, ToolResult


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['expired', 'missing', 'superseded', 'uncertain', 'live'])
async def test_maintenance_result_is_atomic_with_the_current_attempt(installed, monkeypatch, outcome):
    from msg.workers import maintenance
    app, _ = installed
    now = app.clock()
    job = EffectJob(id='job_maintenance_fence', event_id='e_maintenance_fence',
        kind='maintenance', dedupe_key='maintenance-fence',
        principal=Principal(actor=None, subject=None, credential_id=None,
                            method='anonymous', certificates=(), ceiling=()),
        operation='system.maintenance', arguments={'action': 'rebuild_search'},
        state='pending', attempts=0, next_attempt_at=now, lease_until=None)
    async with app.metadata.transaction(write=True) as tx:
        await tx.enqueue(job)
    report = {'indexed_resources': 2}
    observed = []

    async def completed_work(app, action, *, principal):
        assert action == 'rebuild_search'
        async with app.metadata.transaction(write=True) as tx:
            current = await tx.job(job.id)
            if outcome == 'expired':
                current = replace(current, lease_until=now)
            elif outcome == 'missing':
                current = replace(current, lease_until=None)
            elif outcome == 'superseded':
                current = replace(current, attempts=current.attempts + 1)
            elif outcome == 'uncertain':
                current = replace(current, state='uncertain', lease_until=None)
            await tx.save_job(current)
            tx.set_setting('job_status:' + job.id, {'code': 'existing_decision'})
            observed.append(current)
        return report

    monkeypatch.setattr(maintenance, 'run_maintenance', completed_work)
    assert await EffectWorker(app).run_once()
    async with app.metadata.transaction(write=False) as tx:
        current = await tx.job(job.id)
        status = tx.setting('job_status:' + job.id)
    if outcome == 'live':
        assert current == replace(observed[0], state='done', lease_until=None)
        assert status == report
    elif outcome in {'expired', 'missing'}:
        assert current == replace(observed[0], state='uncertain', lease_until=None)
        assert status == {'code': 'expired_execution_lease'}
    else:
        assert current == observed[0]
        assert status == {'code': 'existing_decision'}


async def expire(app, job_id, deadline):
    async with app.metadata.transaction(write=True) as tx:
        current = await tx.job(job_id)
        until = {'exact': app.clock(), 'past': app.clock() - timedelta(seconds=1),
                 'missing': None}[deadline]
        await tx.save_job(replace(current, lease_until=until))


@pytest.mark.asyncio
@pytest.mark.parametrize('deadline', ['exact', 'past', 'missing'])
async def test_real_tool_expiry_does_not_publish_a_file_without_a_sweep(installed, deadline):
    from test_service import register, call
    from test_authorization import approve, scoped
    app, root = installed
    key, uid, _ = await register(app, 'tool-completion-fence')
    grant = scoped(app, 'tool.use', 'tool_dns', app.registry.capability('tool.use').operations)
    certificate = await approve(app, root, uid, key, (grant,))
    accepted = await call(app, 'tool.run', {'id': '/tools/dns',
        'arguments': {'name': 'example.org', 'type': 'A'}}, key=key,
        subject=uid, certs=(certificate.resource_id,))
    assert accepted.status == 'accepted'
    job_id = accepted.data['job_id']
    executions = []

    async def runner(tool, arguments, policies, directory):
        executions.append(tool.executor_key)
        await expire(app, job_id, deadline)
        path = directory / 'result.json'
        path.write_bytes(b'[]')
        return ToolResult(path=path, media_type='application/json', metadata={})

    worker = EffectWorker(app, tool_runner=runner)
    assert await worker.run_once()
    async with app.metadata.transaction(write=False) as tx:
        current = await tx.job(job_id)
        assert current.state == 'uncertain' and current.result is None
        assert tx.setting('job_status:' + job_id) == {'code': 'expired_execution_lease'}
        assert tx.one('SELECT COUNT(*) FROM resources WHERE name=?', ('tool-' + job_id,))[0] == 0
    assert executions == ['dns']
    assert not await worker.run_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('deadline', ['exact', 'past', 'missing'])
async def test_real_git_expiry_does_not_move_refs_without_a_sweep(installed, tmp_path, monkeypatch, deadline):
    import subprocess
    from msg.core.codec import b64, wire
    from msg.extensions.repositories import NativeGitStore
    from test_service import register, call
    app, _ = installed
    key, uid, _ = await register(app, 'git-completion-fence')
    repo = await call(app, 'git.create', {'parent': '/@git-completion-fence',
        'name': 'code.git'}, key=key, subject=uid)
    assert repo.status == 'ok'
    work = tmp_path / 'work'
    work.mkdir()

    def git(*arguments):
        return subprocess.run(['git', '-C', str(work), *arguments], check=True,
                              capture_output=True).stdout.decode().strip()

    git('init', '--initial-branch=main')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    (work / 'README.md').write_text('public code\n')
    git('add', '.')
    git('commit', '-m', 'first')
    oid = git('rev-parse', 'HEAD')
    bundle = tmp_path / 'input.bundle'
    git('bundle', 'create', str(bundle), 'main')
    upload = await call(app, 'content.file_put', {'parent': '/@git-completion-fence/files',
        'name': 'input.bundle', 'data': b64(bundle.read_bytes()),
        'media_type': 'application/octet-stream'}, key=key, subject=uid)
    assert upload.status == 'ok'
    accepted = await call(app, 'git.push', {'id': repo.resources[0].id,
        'bundle': wire(upload.resources[0]),
        'changes': [{'ref': 'refs/heads/main', 'old': None, 'new': oid}]},
        key=key, subject=uid)
    assert accepted.status == 'accepted'
    job_id = accepted.data['job_id']
    import_bundle = NativeGitStore.import_bundle

    async def import_then_expire(store, *args, **kwargs):
        await import_bundle(store, *args, **kwargs)
        await expire(app, job_id, deadline)

    monkeypatch.setattr(NativeGitStore, 'import_bundle', import_then_expire)
    worker = EffectWorker(app)
    assert await worker.run_once()
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.job(job_id)).state == 'uncertain'
        assert tx.setting('job_status:' + job_id) == {'code': 'expired_execution_lease'}
        assert (await tx.resource(repo.resources[0].id)).generation == repo.data['generation']
    refs = await call(app, 'git.refs', {'id': repo.resources[0].id})
    assert refs.status == 'ok' and list(refs.data['refs']) == []
    assert not await worker.run_once()
