"""The independent authenticated Git URL is the only HTTP push path."""

import asyncio
import base64
import os
import subprocess
from datetime import timedelta

import httpx
import pytest
import uvicorn
from starlette.requests import ClientDisconnect
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, unb64, wire
from msg.core.requests import request_for
from msg.extensions.repositories import _HTTP_RECEIVE, MAX_GIT_PACK_BYTES, NativeGitStore
from msg.extensions.ssh_git import guarded_command
from msg.security.capabilities import grant_for
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_git_push_advertisement_requires_token_and_ordinary_path_stays_read_only(installed):
    app, _ = installed
    key, user, _ = await register(app, 'git-http-owner')
    created = await call(
        app, 'git.create', {'parent': '/@git-http-owner', 'name': 'code.git'}, key=key, subject=user
    )
    assert created.status == 'ok', wire(created)
    rid = created.resources[0].id
    grant = grant_for(app.registry.capability('git.basic'))
    issued = await call(
        app,
        'identity.token_create',
        {
            'nonce': b64(os.urandom(32)),
            'recovery_secret': b64(os.urandom(32)),
            'ceiling': wire((grant,)),
            'ttl': 3600,
        },
        key=key,
        subject=user,
        contract_version=2,
    )
    assert issued.status == 'ok', wire(issued)
    direct = await call(
        app,
        'git.http_receive',
        {'id': rid, 'pack_digest': 'sha256:' + '0' * 64, 'pack_size': 0},
        key=key,
        subject=user,
    )
    assert direct.error.code == 'git_http_transport_required', wire(direct)
    basic = base64.b64encode(
        (issued.data['credential_id'] + ':' + issued.data['token']).encode()
    ).decode()
    headers = {'Authorization': 'Basic ' + basic}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        unauthenticated = await http.get(f'/-/git/{rid}/info/refs?service=git-receive-pack')
        assert unauthenticated.status_code == 401
        advertised = await http.get(
            f'/-/git/{rid}/info/refs?service=git-receive-pack', headers=headers
        )
        assert advertised.status_code == 200, advertised.text
        assert advertised.headers['content-type'].startswith(
            'application/x-git-receive-pack-advertisement'
        )
        assert b'git-receive-pack' in advertised.content
        signed = request_for(
            'git.http_advertise',
            {'id': rid},
            app.settings.service_url,
            signer=key,
            subject=user,
            expires_at=NOW + timedelta(seconds=120),
        )
        signature = await http.get(
            f'/-/git/{rid}/info/refs?service=git-receive-pack',
            headers={'X-Msg-Request': b64(canonical(signed))},
        )
        assert signature.status_code == 200 and signature.content == advertised.content
        ordinary = await http.post(
            '/@git-http-owner/code.git/git-receive-pack', headers=headers, content=b'0000'
        )
        assert ordinary.status_code in {401, 403, 405}
        ordinary_advertisement = await http.get(
            '/@git-http-owner/code.git/info/refs?service=git-receive-pack', headers=headers
        )
        assert ordinary_advertisement.status_code != 200
    assert created.data['read_url'].endswith('/@git-http-owner/code.git')
    assert created.data['push_url'].endswith('/-/git/' + rid)
    assert created.data['push_max_bytes'] == MAX_GIT_PACK_BYTES
    refs = await call(app, 'git.refs', {'id': rid})
    assert refs.data['read_url'] == created.data['read_url']
    assert refs.data['push_url'] == created.data['push_url']


@pytest.mark.asyncio
async def test_git_http_receive_requires_explicit_request_id(installed):
    app, _ = installed
    key, user, _ = await register(app, 'git-http-id')
    created = await call(
        app, 'git.create', {'parent': '/@git-http-id', 'name': 'code.git'}, key=key, subject=user
    )
    rid = created.resources[0].id
    grant = grant_for(app.registry.capability('git.basic'))
    issued = await call(
        app,
        'identity.token_create',
        {
            'nonce': b64(os.urandom(32)),
            'recovery_secret': b64(os.urandom(32)),
            'ceiling': wire((grant,)),
            'ttl': 3600,
        },
        key=key,
        subject=user,
        contract_version=2,
    )
    basic = base64.b64encode(
        (issued.data['credential_id'] + ':' + issued.data['token']).encode()
    ).decode()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        rejected = await http.post(
            f'/-/git/{rid}/git-receive-pack',
            content=b'0000',
            headers={'Content-Type': 'application/x-git-receive-pack-request'},
        )
        assert rejected.status_code in {400, 401}
        assert rejected.json()['error']['code'] in {
            'git_request_id_required',
            'authentication_required',
        }
        no_id = await http.post(
            f'/-/git/{rid}/git-receive-pack',
            content=b'0000',
            headers={
                'Authorization': 'Basic ' + basic,
                'Content-Type': 'application/x-git-receive-pack-request',
            },
        )
        assert no_id.status_code == 400
        assert no_id.json()['error']['code'] == 'git_request_id_required'
        noop = await http.post(
            f'/-/git/{rid}/git-receive-pack',
            content=b'0000',
            headers={
                'Authorization': 'Basic ' + basic,
                'X-Msg-Request-Id': 'noop-one',
                'Content-Type': 'application/x-git-receive-pack-request',
            },
        )
        assert noop.status_code == 200, noop.text
        oversized = await http.post(
            f'/-/git/{rid}/git-receive-pack',
            content=b'x' * (MAX_GIT_PACK_BYTES + 1),
            headers={
                'Authorization': 'Basic ' + basic,
                'X-Msg-Request-Id': 'too-large',
                'Content-Type': 'application/x-git-receive-pack-request',
            },
        )
        assert oversized.status_code == 413
        assert oversized.json()['error']['code'] == 'request_too_large'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='git.receive'")[0] == 0
        assert tx.one("SELECT COUNT(*) FROM results WHERE request_id='noop-one'")[0] == 0


@pytest.mark.asyncio
async def test_interrupted_pack_is_removed_and_request_id_can_retry(installed):
    app, _ = installed
    key, user, _ = await register(app, 'git-interrupted')
    created = await call(
        app,
        'git.create',
        {'parent': '/@git-interrupted', 'name': 'code.git'},
        key=key,
        subject=user,
    )
    rid = created.resources[0].id
    grant = grant_for(app.registry.capability('git.basic'))
    issued = await call(
        app,
        'identity.token_create',
        {
            'nonce': b64(os.urandom(32)),
            'recovery_secret': b64(os.urandom(32)),
            'ceiling': wire((grant,)),
            'ttl': 3600,
        },
        key=key,
        subject=user,
        contract_version=2,
    )
    basic = base64.b64encode(
        (issued.data['credential_id'] + ':' + issued.data['token']).encode()
    ).decode()
    headers = {
        'Authorization': 'Basic ' + basic,
        'X-Msg-Request-Id': 'interrupted-once',
        'Content-Type': 'application/x-git-receive-pack-request',
    }

    async def interrupted():
        yield b'0000'
        raise ClientDisconnect

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        aborted = await http.post(
            f'/-/git/{rid}/git-receive-pack', content=interrupted(), headers=headers
        )
        assert aborted.status_code == 400
        assert aborted.json()['error']['code'] == 'request_incomplete'
        assert not list(app.settings.server.staging_dir.glob('msg-git-http-*'))
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='git.receive'")[0] == 0
        retried = await http.post(
            f'/-/git/{rid}/git-receive-pack', content=b'0000', headers=headers
        )
        assert retried.status_code == 200, retried.text
    refs = await call(app, 'git.refs', {'id': rid})
    assert not refs.data['refs']


@pytest.mark.asyncio
async def test_standard_git_push_uses_guarded_http_endpoint_once(installed, tmp_path):
    app, _ = installed
    key, user, _ = await register(app, 'real-http-push')
    created = await call(
        app, 'git.create', {'parent': '/@real-http-push', 'name': 'code.git'}, key=key, subject=user
    )
    assert created.status == 'ok', wire(created)
    rid = created.resources[0].id
    grant = grant_for(app.registry.capability('git.basic'))
    issued = await call(
        app,
        'identity.token_create',
        {
            'nonce': b64(os.urandom(32)),
            'recovery_secret': b64(os.urandom(32)),
            'ceiling': wire((grant,)),
            'ttl': 3600,
        },
        key=key,
        subject=user,
        contract_version=2,
    )
    basic = base64.b64encode(
        (issued.data['credential_id'] + ':' + issued.data['token']).encode()
    ).decode()
    application = create_app(app)
    captured = []
    observed = []

    async def host_bridge(scope, receive, send):
        if scope['type'] == 'http':
            scope = {
                **scope,
                'headers': [
                    (name, b'testserver' if name == b'host' else value)
                    for name, value in scope['headers']
                ],
            }
        chunks = []

        async def record():
            message = await receive()
            if scope['type'] == 'http' and scope['path'].endswith('/git-receive-pack'):
                chunks.append(message.get('body', b''))
            return message

        async def report(message):
            if (
                scope['type'] == 'http'
                and scope['path'].endswith('/git-receive-pack')
                and message['type'] == 'http.response.start'
            ):
                observed.append({
                    'status': message['status'],
                    'headers': {
                        name.decode(): value.decode()
                        for name, value in scope['headers']
                        if name
                        in {b'content-length', b'transfer-encoding', b'expect', b'git-protocol'}
                    },
                })
            await send(message)

        await application(scope, record, report)
        if chunks:
            captured.append(b''.join(chunks))

    server = uvicorn.Server(
        uvicorn.Config(host_bridge, host='127.0.0.1', port=0, log_level='error', lifespan='off')
    )
    server_task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started and server.servers:
                break
            await asyncio.sleep(0.05)
        assert server.started and server.servers
        port = server.servers[0].sockets[0].getsockname()[1]
        work = tmp_path / 'work'
        work.mkdir()

        def git(*args):
            return subprocess.run(
                ['git', '-C', str(work), *args], capture_output=True, text=True, timeout=30
            )

        assert git('init', '--initial-branch=main').returncode == 0
        assert git('config', 'user.name', 'Test').returncode == 0
        assert git('config', 'user.email', 'test@example.invalid').returncode == 0
        (work / 'README.md').write_text('real HTTP push\n')
        (work / 'payload.bin').write_bytes(os.urandom(1_300_000))
        assert git('add', '.').returncode == 0
        assert git('commit', '-m', 'initial').returncode == 0
        command = [
            'git',
            '-C',
            str(work),
            '-c',
            'http.extraHeader=Authorization: Basic ' + basic,
            '-c',
            'http.extraHeader=X-Msg-Request-Id: real-push-once',
            '-c',
            'http.postBuffer=1048576',
            'push',
            f'http://127.0.0.1:{port}/-/git/{rid}',
            'main',
        ]
        pushed = await asyncio.to_thread(
            subprocess.run, command, capture_output=True, text=True, timeout=60
        )
        assert pushed.returncode == 0, (pushed.stderr, observed, [len(c) for c in captured])
        assert len(captured) == 2 and captured[0] == b'0000'
        assert len(captured[1]) > app.settings.server.limits.max_request_bytes
        assert [item['status'] for item in observed] == [200, 200]
        assert observed[0]['headers'].get('content-length') == '4'
        assert observed[1]['headers'].get('transfer-encoding') == 'chunked'
        refs = await call(app, 'git.refs', {'id': rid})
        assert refs.status == 'ok' and refs.data['refs'][0]['name'] == 'refs/heads/main', wire(refs)
        async with app.metadata.transaction(write=False) as tx:
            generation = (await tx.resource(rid)).generation
            jobs = tx.one("SELECT COUNT(*) FROM jobs WHERE kind='git.receive'")[0]
        headers = {
            'Authorization': 'Basic ' + basic,
            'X-Msg-Request-Id': 'real-push-once',
            'Content-Type': 'application/x-git-receive-pack-request',
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
        ) as http:
            replay = await http.post(
                f'/-/git/{rid}/git-receive-pack', content=captured[1], headers=headers
            )
            assert replay.status_code == 200, replay.text
            conflict = await http.post(
                f'/-/git/{rid}/git-receive-pack', content=captured[1] + b'x', headers=headers
            )
            assert conflict.status_code == 409, conflict.text
            assert conflict.json()['error']['code'] == 'idempotency_conflict'
        async with app.metadata.transaction(write=False) as tx:
            assert (await tx.resource(rid)).generation == generation
            assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='git.receive'")[0] == jobs
    finally:
        server.should_exit = True
        await server_task


@pytest.mark.asyncio
async def test_http_git_reference_hook_rechecks_revoked_token(installed):
    app, _ = installed
    key, user, _ = await register(app, 'hook-http-owner')
    created = await call(
        app,
        'git.create',
        {'parent': '/@hook-http-owner', 'name': 'code.git'},
        key=key,
        subject=user,
    )
    rid = created.resources[0].id
    grant = grant_for(app.registry.capability('git.basic'))
    issued = await call(
        app,
        'identity.token_create',
        {
            'nonce': b64(os.urandom(32)),
            'recovery_secret': b64(os.urandom(32)),
            'ceiling': wire((grant,)),
            'ttl': 3600,
        },
        key=key,
        subject=user,
        contract_version=2,
    )
    packet = request_for(
        'git.http_receive',
        {'id': rid, 'pack_digest': 'sha256:' + '0' * 64, 'pack_size': 0},
        app.settings.service_url,
        token=(issued.data['credential_id'], unb64(issued.data['token'])),
        request_id='revoked-before-hook',
        expires_at=NOW + timedelta(seconds=120),
    )
    marker = _HTTP_RECEIVE.set(True)
    try:
        accepted = await app.executor.execute(packet)
    finally:
        _HTTP_RECEIVE.reset(marker)
    assert accepted.status == 'accepted', wire(accepted)
    store = NativeGitStore(app)
    tree = (await asyncio.to_thread(store._run, rid, 'mktree', input=b'')).decode().strip()
    commit = (
        subprocess
        .run(
            ['git', '--git-dir', str(store.path(rid)), 'commit-tree', tree],
            input=b'initial\n',
            capture_output=True,
            check=True,
            env={
                **store.env,
                'GIT_AUTHOR_NAME': 'Test',
                'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            },
        )
        .stdout.decode()
        .strip()
    )
    revoked = await call(
        app, 'identity.key_revoke', {'key_id': issued.data['credential_id']}, key=key, subject=user
    )
    assert revoked.status == 'ok', wire(revoked)
    async with app.metadata.transaction(write=False) as tx:
        job = await tx.job(accepted.data['job_id'])
    code = await guarded_command(
        app, job, lambda _: ['update-ref', 'refs/heads/main', commit, '0' * 40]
    )
    assert code != 0
    refs, _ = await store.refs(rid)
    assert refs == []
