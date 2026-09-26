"""Native Git HTTP fetch must leave repository and business state unchanged."""
import subprocess

import httpx
import pytest

from msg.extensions.repositories import NativeGitStore
from msg.transports.http import create_app
from test_service import call, register


TABLES = ('resources', 'events', 'audit', 'jobs', 'results', 'transfers',
          'credentials', 'reactions', 'watches')


async def business_snapshot(app):
    async with app.metadata.transaction(write=False) as tx:
        return {table: tuple(tx.rows(f'SELECT * FROM {table} ORDER BY 1'))
                for table in TABLES}


def refs_snapshot(repo):
    return subprocess.run(['git', '-C', str(repo), 'for-each-ref',
                           '--format=%(refname) %(objectname)'],
                          check=True, capture_output=True).stdout


def seed_ref(repo):
    tree = subprocess.run(['git', '-C', str(repo), 'mktree'], input=b'',
                          check=True, capture_output=True).stdout.decode().strip()
    commit = subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test',
                             '-c', 'user.email=test@example.invalid', 'commit-tree', tree,
                             '-m', 'fetch fixture'], check=True, capture_output=True).stdout.decode().strip()
    subprocess.run(['git', '-C', str(repo), 'update-ref', 'refs/heads/main', commit],
                   check=True, capture_output=True)
    return commit


@pytest.mark.asyncio
async def test_native_git_upload_pack_is_read_only(installed):
    app, _ = installed
    key, uid, _ = await register(app, 'git-fetch-owner')
    created = await call(app, 'git.create', {'parent': '/@git-fetch-owner', 'name': 'read.git'},
                         key=key, subject=uid)
    assert created.status == 'ok'
    repo = NativeGitStore(app).path(created.resources[0].id)
    commit = seed_ref(repo)
    before_refs = refs_snapshot(repo)
    assert commit.encode() in before_refs
    before_business = await business_snapshot(app)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        want = f'want {commit}\n'.encode()
        request_body = f'{len(want) + 4:04x}'.encode() + want + b'00000009done\n'
        fetch = await http.post('/@git-fetch-owner/read.git/git-upload-pack',
                                content=request_body,
                                headers={'Content-Type': 'application/x-git-upload-pack-request'})
        assert fetch.status_code == 200, fetch.text
        assert b'PACK' in fetch.content
        receive = await http.post('/@git-fetch-owner/read.git/git-receive-pack',
                                  content=b'0000')
        assert receive.status_code == 405, receive.text
        for method in ('PUT', 'DELETE'):
            response = await http.request(method, '/@git-fetch-owner/read.git/git-upload-pack',
                                          content=b'0000')
            assert response.status_code == 405, (method, response.text)

    assert refs_snapshot(repo) == before_refs
    assert await business_snapshot(app) == before_business
