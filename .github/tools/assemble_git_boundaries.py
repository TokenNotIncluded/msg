"""Exact-source Git boundary extraction; imports no project module."""
from pathlib import Path
import ast
import copy
import difflib
import hashlib
import json
import re
import subprocess
import textwrap

ROOT=Path.cwd()
FILES={
    'repo':'src/msg/extensions/repositories.py',
    'ssh':'src/msg/extensions/ssh_git.py',
    'routes':'src/msg/transports/http_routes.py',
}
EXPECTED={
    'repo':'940a1a1bd987a2d97273734a8c0646097abf3c83',
    'ssh':'d6465ce37fd7e4743773942144e91e10a7c847af',
    'routes':'04a7de0431f1211893111e887ef87661ef877c0f',
}
HTTP_METHODS=('_lfs_packet','_lfs_authorize','_lfs_preflight_publish','http_lfs',
              '_repo_read_path','http','http_push','_http_push_receive','_job')
PUBLICATION=('ReferenceGuard','hook_program','relay_bounded_stdin','guarded_command')
HTTP_HELPERS=('body_bytes','json_response','error_status')
DELEGATED={'path','lfs','root','env'}
before={path:(ROOT/path).read_text() for path in FILES.values()}
after={}
audit=[]
for name,path in FILES.items():
    actual=subprocess.check_output(['git','hash-object',path],text=True).strip()
    assert actual==EXPECTED[name],(path,actual)


def definitions(text):
    return {node.name:node for node in ast.parse(text).body
            if isinstance(node,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef))}


def assignments(text):
    return {target.id:node for node in ast.parse(text).body if isinstance(node,ast.Assign)
            for target in node.targets if isinstance(target,ast.Name)}


def methods(node):
    return {item.name:item for item in node.body
            if isinstance(item,(ast.FunctionDef,ast.AsyncFunctionDef))}


def source(text,node):
    # Complete physical lines keep method indentation and decorators intact.
    begin=min([node.lineno]+[d.lineno for d in getattr(node,'decorator_list',[])])-1
    return ''.join(text.splitlines(True)[begin:node.end_lineno]).rstrip()


def delete_nodes(text,nodes):
    lines=text.splitlines(True)
    for node in sorted(nodes,key=lambda n:n.lineno,reverse=True):
        begin=min([node.lineno]+[d.lineno for d in getattr(node,'decorator_list',[])])-1
        del lines[begin:node.end_lineno]
    return ''.join(lines)


def once(text,old,new):
    assert text.count(old)==1,old
    return text.replace(old,new)


def clean(text):
    return re.sub(r'\n{4,}','\n\n\n',text).rstrip()+'\n'


repo=before[FILES['repo']]
repo_nodes=definitions(repo)
store=repo_nodes['NativeGitStore']
old_methods=methods(store)
slots=assignments(repo)
# Remove HTTP methods and stream decoding from the storage/registration module.
new_repo=delete_nodes(repo,[old_methods[name] for name in HTTP_METHODS]+[
    repo_nodes['spool_git_pack'],repo_nodes['spool_lfs_object'],
    slots['_GIT_UPLOAD_SLOTS'],slots['_LFS_UPLOAD_SLOTS']])
new_repo=once(new_repo,'from starlette.requests import ClientDisconnect\n','')
new_repo=once(new_repo,'from starlette.responses import Response,StreamingResponse\n','')
new_repo=once(new_repo,'        self.app=app\n','        self.blob_root=app.settings.server.blob_dir\n')
assert new_repo.count('self.app.settings.server.blob_dir')==2
new_repo=new_repo.replace('self.app.settings.server.blob_dir','self.blob_root')
assert 'self.app' not in source(new_repo,definitions(new_repo)['NativeGitStore'])
after[FILES['repo']]=clean(new_repo)

# Common HTTP primitives do not depend on the router or Git adapter.
routes=before[FILES['routes']]
route_defs=definitions(routes)
base_headers=assignments(routes)['BASE_HEADERS']
common='''"""Small HTTP response/body primitives shared without importing the router."""
from __future__ import annotations
from starlette.responses import Response
from msg.core.codec import canonical
from msg.core.errors import require
from msg.transports.packet import gunzip

'''+source(routes,base_headers)+'\n\n'+ '\n\n'.join(source(routes,route_defs[name]) for name in HTTP_HELPERS)+'\n'
common_path='src/msg/transports/http_common.py'
assert not (ROOT/common_path).exists()
before[common_path]=''
after[common_path]=clean(common)

# The HTTP adapter composes the store; no catch-all attribute forwarding or
# inherited storage class keeps a hidden Request/Response dependency.
http='''"""Native Git/LFS HTTP adaptation over the shared store and publication guard."""
from __future__ import annotations
import asyncio
import base64
from datetime import timedelta
import hashlib
import os
from pathlib import Path
import re
import tempfile
import time
from uuid import uuid4

from starlette.requests import ClientDisconnect
from starlette.responses import Response, StreamingResponse
from msg.core.codec import canonical, wire, unb64
from msg.core.errors import Failure, require
from msg.core.models import ExecutionContext
from msg.core.requests import request_for
from msg.plugins.common import check_access, resolve
from msg.extensions.repositories import (
    NativeGitStore, MAX_GIT_PACK_BYTES, MAX_GIT_UPLOAD_SECONDS, MAX_LFS_OBJECT_BYTES,
    require_git_repository_capacity, _HTTP_RECEIVE, _LFS_STAGED,
)
from msg.extensions.git_publication import guarded_command
from msg.transports.http_common import body_bytes, json_response, error_status
from msg.transports.packet import path_packet

'''
http+=source(repo,slots['_GIT_UPLOAD_SLOTS'])+'\n'+source(repo,slots['_LFS_UPLOAD_SLOTS'])+'\n\n'
http+='\n\n'.join(source(repo,repo_nodes[name]) for name in ('spool_git_pack','spool_lfs_object'))+'\n\n'
http+='''class GitHTTPAdapter:
    def __init__(self, app, *, store: NativeGitStore | None = None):
        self.app = app
        self.store = store if store is not None else NativeGitStore(app)

'''
for name in HTTP_METHODS:
    body=source(repo,old_methods[name])
    for line in (
        '        from msg.transports.http import body_bytes,json_response\n',
        '        from msg.transports.http import body_bytes\n',
        '        from msg.transports.http import json_response,error_status\n',
        '        from msg.transports.packet import path_packet\n',
        '                from msg.extensions.ssh_git import guarded_command\n',
    ):
        body=body.replace(line,'')
    body=re.sub(r'\bself\.(path|lfs|root|env)\b',r'self.store.\1',body)
    http+=body+'\n\n'
http_path='src/msg/transports/git_http.py'
assert not (ROOT/http_path).exists()
before[http_path]=''
after[http_path]=clean(http)

# The publication module is the old trusted guard implementation verbatim,
# minus the SSH-specific entry function. SSH exports compatible object aliases.
ssh=before[FILES['ssh']]
ssh_defs=definitions(ssh)
publication=delete_nodes(ssh,[ssh_defs['receive_pack']])
publication_path='src/msg/extensions/git_publication.py'
assert not (ROOT/publication_path).exists()
before[publication_path]=''
after[publication_path]=clean(publication)
after[FILES['ssh']]='''"""SSH receive-pack entrypoint over the shared guarded Git publication path."""
from __future__ import annotations
from msg.core.errors import require
from msg.extensions.repositories import (
    NativeGitStore, MAX_GIT_PACK_BYTES, MAX_GIT_UPLOAD_SECONDS,
    require_git_repository_capacity,
)
# Historical imports are the exact shared objects, never alternate wrappers.
from msg.extensions.git_publication import (
    ReferenceGuard, hook_program, relay_bounded_stdin, guarded_command,
)


'''+source(ssh,ssh_defs['receive_pack'])+'\n'

# Routing only selects the HTTP adapter; the outer ingress/recovery boundary
# stays in transports.http and is intentionally not modified.
new_routes=delete_nodes(routes,[base_headers]+[route_defs[name] for name in HTTP_HELPERS])
new_routes=once(new_routes,'from starlette.routing import Route\n',
    'from starlette.routing import Route\nfrom msg.transports.http_common import BASE_HEADERS, body_bytes, json_response, error_status\n')
assert new_routes.count('from msg.extensions.repositories import NativeGitStore')==3
assert new_routes.count('NativeGitStore(service)')==4
new_routes=new_routes.replace('from msg.extensions.repositories import NativeGitStore',
                             'from msg.transports.git_http import GitHTTPAdapter')
new_routes=new_routes.replace('NativeGitStore(service)','GitHTTPAdapter(service)')
after[FILES['routes']]=clean(new_routes)

# Audit all moved algorithm bodies, old registrations, worker publication and
# router dispatch, allowing only the explicitly enumerated dependency rewiring.
class NoImports(ast.NodeTransformer):
    def visit_ImportFrom(self,node): return None
    def visit_Import(self,node): return None

class DelegateStore(ast.NodeTransformer):
    def visit_Attribute(self,node):
        node=self.generic_visit(node)
        if isinstance(node.value,ast.Name) and node.value.id=='self' and node.attr in DELEGATED:
            node.value=ast.Attribute(value=ast.Name(id='self',ctx=ast.Load()),attr='store',ctx=ast.Load())
        return node

class RouteAdapter(ast.NodeTransformer):
    def visit_Name(self,node):
        if node.id=='NativeGitStore':node.id='GitHTTPAdapter'
        return node


def dump(node): return ast.dump(node,include_attributes=False)
def without_imports(node): return NoImports().visit(copy.deepcopy(node))

new_store=methods(definitions(after[FILES['repo']])['NativeGitStore'])
assert set(new_store)==set(old_methods)-set(HTTP_METHODS)
for name,node in new_store.items():
    old=source(repo,old_methods[name])
    if name=='__init__':old=old.replace('self.app=app','self.blob_root=app.settings.server.blob_dir')
    old=old.replace('self.app.settings.server.blob_dir','self.blob_root')
    assert dump(ast.parse(textwrap.dedent(old)).body[0])==dump(node),name
    audit.append('store.'+name+' algorithm unchanged except finite settings snapshot')
for name in ('register','execute_push','git_repository_bytes','require_git_repository_capacity'):
    assert dump(repo_nodes[name])==dump(definitions(after[FILES['repo']])[name]),name
    audit.append('repository.'+name+' unchanged')
http_methods=methods(definitions(after[http_path])['GitHTTPAdapter'])
for name in HTTP_METHODS:
    old=DelegateStore().visit(without_imports(old_methods[name]))
    new=without_imports(http_methods[name])
    assert dump(old)==dump(new),name
    audit.append('http.'+name+' unchanged except explicit store delegation/imports')
for name in ('spool_git_pack','spool_lfs_object'):
    assert dump(repo_nodes[name])==dump(definitions(after[http_path])[name]),name
    audit.append(name+' unchanged')
for name in PUBLICATION:
    assert dump(ssh_defs[name])==dump(definitions(after[publication_path])[name]),name
    audit.append('publication.'+name+' unchanged')
assert dump(ssh_defs['receive_pack'])==dump(definitions(after[FILES['ssh']])['receive_pack'])
audit.append('ssh.receive_pack unchanged')
for name in HTTP_HELPERS:
    assert dump(route_defs[name])==dump(definitions(after[common_path])[name]),name
    audit.append('http_common.'+name+' unchanged')
assert dump(base_headers)==dump(assignments(after[common_path])['BASE_HEADERS'])
for name,node in route_defs.items():
    if name in HTTP_HELPERS:continue
    expected=RouteAdapter().visit(without_imports(node))
    actual=without_imports(definitions(after[FILES['routes']])[name])
    assert dump(expected)==dump(actual),name
    audit.append('http_routes.'+name+' unchanged except adapter selection')
for path in (FILES['repo'],publication_path):
    imports=[node.module or '' for node in ast.walk(ast.parse(after[path])) if isinstance(node,ast.ImportFrom)]
    assert not [name for name in imports if name.startswith(('starlette','msg.transports.http','msg.transports.git_http','msg.extensions.ssh_git'))],path
for path in (http_path,common_path):
    assert not [node.module for node in ast.walk(ast.parse(after[path])) if isinstance(node,ast.ImportFrom)
                and node.module in {'msg.transports.http','msg.transports.http_routes'}],path

notes='docs/GIT_BOUNDARY_PROGRESS.md'
before[notes]=(ROOT/notes).read_text()
after[notes]=before[notes]+'''

## Implemented extraction and source audit

NativeGitStore no longer owns any HTTP handler, Request/Response conversion,
body spooler or Application property. It snapshots repository/blob paths and
keeps its original physical store algorithms. GitHTTPAdapter explicitly composes
that store and retains existing HTTP/LFS behavior. ContextVar identities used by
registered operations remain singular; upload semaphores move with HTTP spooling.

The original ReferenceGuard, hook program, bounded stdin relay and guarded Git
command are unchanged in git_publication. SSH exposes same-object aliases and its
original receive_pack entry. HTTP calls the same shared guard. The bundle worker
algorithm, including current_attempt and transaction fencing, is untouched.

HTTP helpers/base headers have one owner in http_common; routes and Git HTTP use
it. transports.http and its outer passive/recovery boundary are unchanged. The
assembly audits every moved/retained method, every existing registration and
bundle worker body, all shared publication functions, helper functions/headers,
and route functions. Only settings snapshots, explicit store attribute delegation
and import/adapter selection are permitted differences. Source-only AST evidence
is not a successful project test. Final source and patch are uploaded; one-shot
tooling is removed from the submitted tree before focused/full validation.
'''

out=Path('/tmp/git-boundary-assembly');out.mkdir(parents=True,exist_ok=True)
changed={path:clean(text) for path,text in after.items() if text!=before.get(path,'')}
patch=''.join(''.join(difflib.unified_diff(before.get(path,'').splitlines(True),text.splitlines(True),
    fromfile='a/'+path if before.get(path) else '/dev/null',tofile='b/'+path)) for path,text in sorted(changed.items()))
for path,text in changed.items():(ROOT/path).write_text(text)
(out/'changes.patch').write_text(patch)
(out/'audit.json').write_text(json.dumps({'baseline_blobs':EXPECTED,'checked_invariants':audit,
    'changed_paths':sorted(changed),'patch_sha256':hashlib.sha256(patch.encode()).hexdigest()},indent=2)+'\n')
