"""Git owns files/publication; protocol adapters own requests and responses."""
from __future__ import annotations

import ast
import importlib
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from msg.core.errors import Failure
from msg.core.codec import wire
from msg.extensions.repositories import NativeGitStore
from msg.extensions.ssh_git import ReferenceGuard
from msg.transports.http import create_app
from test_service import call, register
from test_native_git_read_only import business_snapshot, refs_snapshot, seed_ref


def dependencies(module):
    tree=ast.parse(Path(importlib.import_module(module).__file__).read_text())
    return [node.module or '' for node in ast.walk(tree) if isinstance(node,ast.ImportFrom)] + [
        alias.name for node in ast.walk(tree) if isinstance(node,ast.Import) for alias in node.names]


def test_shared_repository_module_does_not_import_http_or_ssh_adapters():
    forbidden=('starlette','msg.transports.http','msg.transports.git_http','msg.extensions.ssh_git')
    assert not [name for name in dependencies('msg.extensions.repositories')
                if any(name==prefix or name.startswith(prefix+'.') for prefix in forbidden)]


def test_repository_store_has_no_http_handlers_or_request_conversion_methods():
    assert not {'http','http_push','http_lfs','_http_push_receive','_lfs_packet',
                '_lfs_authorize','_lfs_preflight_publish','_repo_read_path','_job'}.intersection(vars(NativeGitStore))
    assert {'path','lfs','lfs_capacity','create','refs','import_bundle','update_refs'}<=set(vars(NativeGitStore))


def test_shared_publication_has_no_protocol_adapter_dependency():
    module=importlib.import_module('msg.extensions.git_publication')
    assert not hasattr(module,'receive_pack')
    forbidden=('starlette','msg.transports.http','msg.transports.git_http','msg.extensions.ssh_git')
    assert not [name for name in dependencies(module.__name__)
                if any(name==prefix or name.startswith(prefix+'.') for prefix in forbidden)]


def test_both_adapters_use_the_same_publication_implementation():
    owner=importlib.import_module('msg.extensions.git_publication')
    ssh=importlib.import_module('msg.extensions.ssh_git')
    http=importlib.import_module('msg.transports.git_http')
    for name in ('ReferenceGuard','hook_program','relay_bounded_stdin','guarded_command'):
        assert getattr(ssh,name) is getattr(owner,name)
        assert getattr(owner,name).__module__==owner.__name__
    assert http.guarded_command is owner.guarded_command
    assert 'msg.extensions.ssh_git' not in dependencies(http.__name__)


def test_http_helpers_have_one_owner_without_a_router_import_cycle():
    common=importlib.import_module('msg.transports.http_common')
    routes=importlib.import_module('msg.transports.http_routes')
    public=importlib.import_module('msg.transports.http')
    git=importlib.import_module('msg.transports.git_http')
    for name in ('body_bytes','json_response','error_status'):
        assert getattr(common,name) is getattr(routes,name) is getattr(public,name) is getattr(git,name)
        assert getattr(common,name).__module__==common.__name__
    assert routes.BASE_HEADERS is common.BASE_HEADERS
    assert not {'msg.transports.http','msg.transports.http_routes'}.intersection(dependencies(git.__name__))
    assert not {'msg.transports.http','msg.transports.http_routes','msg.transports.git_http'}.intersection(dependencies(common.__name__))


def test_git_storage_and_publication_import_without_loading_protocol_adapters():
    code='''
import importlib.abc,sys
class NoAdapters(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if any(fullname==name or fullname.startswith(name+'.') for name in
               ('starlette','msg.transports.http','msg.transports.http_routes',
                'msg.transports.git_http','msg.extensions.ssh_git')):
            raise AssertionError('shared Git imported adapter '+fullname)
sys.meta_path.insert(0,NoAdapters())
from msg.extensions.repositories import NativeGitStore
from msg.extensions.git_publication import ReferenceGuard,guarded_command
'''
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


async def test_http_uses_explicit_store_composition_not_storage_inheritance(installed):
    app,_=installed
    adapter=importlib.import_module('msg.transports.git_http').GitHTTPAdapter
    store=NativeGitStore(app)
    assert not hasattr(store,'app')
    assert store.root==app.settings.server.repositories_dir
    assert store.blob_root==app.settings.server.blob_dir
    http=adapter(app,store=store)
    assert http.store is store and http.app is app
    assert not isinstance(http,NativeGitStore)
    assert '__getattr__' not in vars(adapter)


async def test_public_git_read_and_rejected_push_preserve_all_business_state(installed):
    app,_=installed
    key,uid,_=await register(app,'git-boundary-read')
    created=await call(app,'git.create',{'parent':'/@git-boundary-read','name':'code.git'},
                       key=key,subject=uid)
    assert created.status=='ok',wire(created)
    rid=created.resources[0].id
    repo=NativeGitStore(app).path(rid)
    seed_ref(repo)
    before=await business_snapshot(app)
    refs=refs_snapshot(repo)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as client:
        path='/@git-boundary-read/code.git'
        head=await client.get(path+'/HEAD')
        assert head.status_code==200 and b'refs/heads/main' in head.content
        advertised=await client.get(path+'/info/refs?service=git-upload-pack')
        assert advertised.status_code==200 and b'git-upload-pack' in advertised.content
        rejected=await client.post(path+'/git-receive-pack',content=b'0000')
        assert rejected.status_code in {401,403,405}
        unauthenticated=await client.get('/-/git/'+rid+'/info/refs?service=git-receive-pack')
        assert unauthenticated.status_code==401
    assert refs_snapshot(repo)==refs
    assert await business_snapshot(app)==before


def test_reference_batch_validation_preserves_exact_git_change_contract():
    old,new='0'*40,'1'*40
    rows=ReferenceGuard.validate_changes(f'{old} {new} refs/heads/main\n{old} {new} refs/tags/v1\n')
    assert rows==[{'ref':'refs/heads/main','old':old,'new':new},
                  {'ref':'refs/tags/v1','old':old,'new':new}]
    for value in ('',f'{old} {new} refs/heads/main\n{old} {new} refs/heads/main',
                  f'bad {new} refs/heads/main',f'{old} {new} refs/remotes/private'):
        with pytest.raises(Failure):ReferenceGuard.validate_changes(value)
