"""Real Git publication/fetch checks inside the disposable selftest installation."""
import asyncio
from datetime import timedelta
import hashlib
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.routing import Route

from msg.core.codec import b64, canonical
from msg.core.errors import Failure, require
from msg.core.requests import request_for
from msg.extensions.repositories import NativeGitStore
from msg.transports.http import create_app
from msg.transports.git_http import GitHTTPAdapter


async def check_git(app, call, register):
    require(bool(app.selftest_run_id), 'selftest_namespace_required')
    key, owner = await register('git-selftest')
    created = await call('git.create', {'parent': owner, 'name': 'selftest.git'}, key, owner)
    require(created.status == 'ok', 'selftest_git_create_failed')
    rid = created.resources[0].id
    store = NativeGitStore(app)
    env = {**store.env, 'GIT_AUTHOR_NAME': 'Selftest', 'GIT_AUTHOR_EMAIL': 'selftest@example.invalid'}

    def run(folder, *args, input=None):
        try:
            return subprocess.run(['git', '-C', str(folder), *args], input=input,
                                  capture_output=True, check=True, timeout=30, env=env).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise Failure('selftest_git_command_failed') from exc

    def pkt(data):
        return f'{len(data) + 4:04x}'.encode() + data

    async def snapshot():
        async with app.metadata.transaction(write=False) as tx:
            return {table: tuple(tx.rows(f'SELECT * FROM {table} ORDER BY 1'))
                    for table in ('resources', 'events', 'audit', 'jobs', 'results', 'transfers',
                                  'credentials', 'reactions', 'watches')}

    with tempfile.TemporaryDirectory(prefix='msg-selftest-git-') as temporary:
        work = Path(temporary)
        await asyncio.to_thread(run, work, 'init', '--initial-branch=main', '--object-format=sha1')
        payload = b'isolated Git selftest payload\n'
        (work / 'README.md').write_bytes(payload)
        await asyncio.to_thread(run, work, 'add', 'README.md')
        await asyncio.to_thread(run, work, 'commit', '-m', 'isolated selftest')
        commit = (await asyncio.to_thread(run, work, 'rev-parse', 'HEAD')).decode().strip()
        pack = await asyncio.to_thread(run, work, 'pack-objects', '--stdout', '--revs',
                                       input=(commit + '\n').encode())
        receive = pkt(f'{"0" * 40} {commit} refs/heads/main\0report-status\n'.encode()) + b'0000' + pack

        def headers(request_id):
            packet = request_for('git.http_receive', {'id': rid,
                'pack_digest': 'sha256:' + hashlib.sha256(receive).hexdigest(), 'pack_size': len(receive)},
                app.settings.service_url, signer=key, subject=owner, request_id=request_id,
                expires_at=app.clock() + timedelta(seconds=120))
            return {'X-Msg-Request': b64(canonical(packet)),
                    'Content-Type': 'application/x-git-receive-pack-request'}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                     base_url=app.settings.service_url) as http:
            endpoint = f'/-/git/{rid}/git-receive-pack'
            denied = await http.post(endpoint, content=receive,
                                    headers={'Content-Type': 'application/x-git-receive-pack-request'})
            require(denied.status_code == 401, 'selftest_git_unsigned_push_allowed')
            pushed = await http.post(endpoint, content=receive, headers=headers('selftest-git-push'))
            require(pushed.status_code == 200 and b'ok refs/heads/main\n' in pushed.content,
                    'selftest_git_push_failed')
            require(await asyncio.to_thread(run, store.path(rid), 'show', 'refs/heads/main:README.md') == payload,
                    'selftest_git_publication_mismatch')
            before = await snapshot()
            replay = await http.post(endpoint, content=receive, headers=headers('selftest-git-push'))
            require(replay.status_code == 200 and replay.content == pushed.content and
                    await snapshot() == before, 'selftest_git_replay_mutated')
            # A new request with an obsolete expected ref must not overwrite it.
            stale = await http.post(endpoint, content=receive, headers=headers('selftest-git-stale'))
            require(stale.status_code == 200 and b'ng refs/heads/main ' in stale.content,
                    'selftest_git_stale_ref_allowed')
            refs = await asyncio.to_thread(run, store.path(rid), 'for-each-ref', '--format=%(refname) %(objectname)')
            require(refs == f'refs/heads/main {commit}\n'.encode(), 'selftest_git_ref_changed')
            before = await snapshot()
            # Production's /@user/*.git router does not expose /_test/<run>.
            # Mount the real read adapter over its isolated resource path only;
            # it still executes git.refs authorization and git http-backend.
            async def read(request):
                return await GitHTTPAdapter(app).http(
                    request, urlsplit(created.data['read_url']).path, 'git-upload-pack')
            fetch_app = Starlette(routes=[Route('/fetch', read, methods=['POST'])])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fetch_app),
                                         base_url=app.settings.service_url) as reader:
                fetch = await reader.post('/fetch',
                    content=pkt(f'want {commit}\n'.encode()) + b'00000009done\n',
                    headers={'Content-Type': 'application/x-git-upload-pack-request'})
            require(fetch.status_code == 200 and fetch.content.startswith(b'0008NAK\nPACK'),
                    'selftest_git_fetch_failed')
            # Import the actual response into a fresh repository, not the source fixture.
            fetched = work / 'fetched'
            fetched.mkdir()
            await asyncio.to_thread(run, fetched, 'init', '--bare', '--object-format=sha1')
            await asyncio.to_thread(run, fetched, 'index-pack', '--stdin', input=fetch.content[8:])
            require(await asyncio.to_thread(run, fetched, 'show', commit + ':README.md') == payload,
                    'selftest_git_fetch_mismatch')
            require(await snapshot() == before and refs == await asyncio.to_thread(
                run, store.path(rid), 'for-each-ref', '--format=%(refname) %(objectname)'),
                'selftest_git_read_mutated')
    return True
