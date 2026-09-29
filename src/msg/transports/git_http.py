"""Native Git/LFS HTTP adaptation over the shared store and publication guard."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from starlette.requests import ClientDisconnect
from starlette.responses import Response, StreamingResponse

from msg.core.codec import canonical, unb64, wire
from msg.core.errors import Failure, require
from msg.core.models import ExecutionContext
from msg.core.requests import request_for
from msg.extensions.git_publication import guarded_command
from msg.extensions.repositories import (
    _HTTP_RECEIVE,
    _LFS_STAGED,
    MAX_GIT_PACK_BYTES,
    MAX_GIT_UPLOAD_SECONDS,
    MAX_LFS_OBJECT_BYTES,
    NativeGitStore,
    require_git_repository_capacity,
)
from msg.plugins.common import check_access, resolve
from msg.transports.http_common import body_bytes, error_status, json_response
from msg.transports.packet import path_packet

_GIT_UPLOAD_SLOTS = asyncio.Semaphore(2)
_LFS_UPLOAD_SLOTS = asyncio.Semaphore(2)


async def spool_git_pack(request, path):
    require(request.headers.get('content-encoding', 'identity') == 'identity', 'unknown_encoding')
    length = request.headers.get('content-length')
    if length is not None:
        require(length.isdecimal() and int(length) <= MAX_GIT_PACK_BYTES, 'request_too_large')
    hasher = hashlib.sha256()
    size = 0
    try:
        with path.open('xb') as stream:
            async for chunk in request.stream():
                size += len(chunk)
                require(size <= MAX_GIT_PACK_BYTES, 'request_too_large')
                hasher.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
    except ClientDisconnect as exc:
        raise Failure('request_incomplete') from exc
    return 'sha256:' + hasher.hexdigest(), size


async def spool_lfs_object(request, path, expected_size):
    require(request.headers.get('content-encoding', 'identity') == 'identity', 'unknown_encoding')
    length = request.headers.get('content-length')
    if length is not None:
        require(length.isdecimal() and int(length) == expected_size, 'lfs_size_mismatch')
    hasher = hashlib.sha256()
    size = 0
    try:
        with path.open('xb') as stream:
            async for chunk in request.stream():
                size += len(chunk)
                require(size <= expected_size, 'lfs_size_mismatch')
                hasher.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
    except ClientDisconnect as exc:
        raise Failure('request_incomplete') from exc
    require(size == expected_size, 'lfs_size_mismatch')
    return hasher.hexdigest(), size


class GitHTTPAdapter:
    def __init__(self, app, *, store: NativeGitStore | None = None):
        self.app = app
        self.store = store if store is not None else NativeGitStore(app)

    def _lfs_packet(self, request, operation, arguments):
        limits = self.app.settings.server.limits
        signed = request.headers.get('x-msg-request')
        authorization = request.headers.get('authorization', '')
        require(not (signed and authorization), 'ambiguous_proof')
        if signed:
            packet = path_packet(signed, 'j', limits.max_request_bytes)
            require(
                packet.operation == operation
                and canonical(packet.arguments) == canonical(arguments),
                'representation_mismatch',
            )
            return packet
        if authorization:
            require(authorization.startswith('Basic '), 'authentication_required')
            try:
                user, password = (
                    base64.b64decode(authorization[6:], validate=True).decode('ascii').split(':', 1)
                )
                token = unb64(password)
            except (ValueError, UnicodeDecodeError, Failure) as exc:
                raise Failure('invalid_token') from exc
            return request_for(
                operation,
                arguments,
                self.app.settings.service_url,
                token=(user, token),
                request_id=uuid4().hex if operation == 'git.lfs_publish' else None,
                expires_at=self.app.clock() + timedelta(seconds=120),
            )
        return request_for(operation, arguments, self.app.settings.service_url)

    async def _lfs_authorize(self, request, operation, arguments):
        result = await self.app.executor.execute(
            self._lfs_packet(request, operation, arguments), entry='network'
        )
        require(result.status == 'ok', result.error.code if result.error else 'permission_denied')
        return result.data['resource_id']

    async def _lfs_preflight_publish(self, request, arguments):
        """Validate the exact signed publish envelope without committing it."""
        packet = self._lfs_packet(request, 'git.lfs_publish', arguments)
        spec = self.app.registry.operation(packet.operation, packet.contract_version)
        require('network' in spec.entries, 'entry_not_allowed')
        self.app.registry.validate(spec.input_schema, packet.arguments)
        async with self.app.metadata.transaction(write=False) as tx:
            principal = await self.app.authenticator.authenticate(packet, tx, entry='network')
            require(principal.method == 'signature', 'authentication_required')
            context = ExecutionContext(
                request_id=packet.request_id,
                principal=principal,
                entry='network',
                now=self.app.clock(),
                deadline_monotonic=time.monotonic() + 30,
            )
            rid = await resolve(tx, arguments['id'])
            await check_access(self.app, context, packet, tx, rid, 'write')
            resource = await tx.resource(rid)
            require(resource.type == 'repo' and resource.state == 'active', 'not_a_repository')
        return rid

    async def http_lfs(self, request, repo_id, suffix, *, write=False):
        """LFS batch is a read/planning query; only /-/ object PUT commits bytes."""
        require(not request.query_params, 'invalid_lfs_query')
        object_match = re.fullmatch(r'objects/([0-9a-f]{64})(?:/([0-9]+))?', suffix)
        if object_match:
            oid, size_text = object_match.groups()
            if write:
                require(request.method == 'PUT' and size_text is not None, 'method_not_allowed')
                size = int(size_text)
                require(size <= MAX_LFS_OBJECT_BYTES, 'request_too_large')
                publish_args = {'id': repo_id, 'oid': oid, 'size': size}
                if request.headers.get('x-msg-request'):
                    await self._lfs_preflight_publish(request, publish_args)
                else:
                    await self._lfs_authorize(request, 'git.lfs_write_authorize', {'id': repo_id})
                self.app.settings.server.staging_dir.mkdir(parents=True, exist_ok=True)
                async with _LFS_UPLOAD_SLOTS:
                    with tempfile.TemporaryDirectory(
                        prefix='msg-lfs-', dir=self.app.settings.server.staging_dir
                    ) as temp:
                        staged = Path(temp) / 'object'
                        try:
                            async with asyncio.timeout(MAX_GIT_UPLOAD_SECONDS):
                                digest, actual = await spool_lfs_object(request, staged, size)
                        except TimeoutError as exc:
                            raise Failure('request_incomplete') from exc
                        require(digest == oid, 'lfs_digest_mismatch')
                        marker = _LFS_STAGED.set(staged)
                        try:
                            await self._lfs_authorize(request, 'git.lfs_publish', publish_args)
                        finally:
                            _LFS_STAGED.reset(marker)
                return Response(status_code=200, headers={'Cache-Control': 'no-store'})
            require(request.method in {'GET', 'HEAD'} and size_text is None, 'method_not_allowed')
            rid = await self._lfs_authorize(request, 'git.lfs_read', {'id': repo_id, 'oid': oid})
            path = self.store.lfs(rid).path(oid)
            require(path.is_file(), 'not_found')
            size = path.stat().st_size
            etag = '"sha256:' + oid + '"'
            begin, end, status = 0, size, 200
            requested = request.headers.get('range')
            if requested and request.headers.get('if-range') in {None, etag}:
                match = (
                    re.fullmatch(r'bytes=(\d*)-(\d*)', requested) if len(requested) <= 128 else None
                )
                if match and any(match.groups()):
                    left, right = match.groups()
                    if left:
                        begin = int(left)
                        end = min(size, int(right) + 1) if right else size
                        valid = begin < size and begin < end
                    else:
                        suffix = int(right)
                        begin = max(0, size - suffix)
                        end = size
                        valid = suffix > 0 and begin < end
                    if valid:
                        status = 206
                else:
                    valid = False
                if not valid:
                    return Response(
                        status_code=416,
                        headers={
                            'Content-Range': f'bytes */{size}',
                            'Accept-Ranges': 'bytes',
                            'Cache-Control': 'private, no-store',
                        },
                    )
            headers = {
                'Content-Length': str(end - begin),
                'Cache-Control': 'private, no-store',
                'X-Content-Type-Options': 'nosniff',
                'Content-Disposition': 'attachment',
                'Accept-Ranges': 'bytes',
                'ETag': etag,
            }
            if status == 206:
                headers['Content-Range'] = f'bytes {begin}-{end - 1}/{size}'
            if request.method == 'HEAD':
                return Response(status_code=status, headers=headers)

            async def stream():
                with path.open('rb') as source:
                    source.seek(begin)
                    remaining = end - begin
                    while remaining:
                        chunk = await asyncio.to_thread(source.read, min(65536, remaining))
                        require(bool(chunk), 'content_truncated')
                        remaining -= len(chunk)
                        yield chunk

            return StreamingResponse(
                stream(), status_code=status, media_type='application/octet-stream', headers=headers
            )
        require(suffix == 'objects/batch' and request.method == 'POST', 'not_found')
        require(
            request.headers.get('content-type', '').split(';', 1)[0]
            == 'application/vnd.git-lfs+json',
            'invalid_lfs_content_type',
        )
        import json

        try:
            body = json.loads(
                await body_bytes(request, self.app.settings.server.limits.max_request_bytes)
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise Failure('invalid_lfs_batch') from exc
        require(
            type(body) is dict
            and body.get('operation') in {'download', 'upload'}
            and type(body.get('objects')) is list
            and len(body['objects']) <= 100,
            'invalid_lfs_batch',
        )
        upload = body['operation'] == 'upload'
        require(not upload or write, 'method_not_allowed')
        require(not write or upload, 'method_not_allowed')
        require(
            set(body) <= {'operation', 'objects', 'transfers', 'ref', 'hash_algo'},
            'invalid_lfs_batch',
        )
        rid = await self._lfs_authorize(
            request, 'git.lfs_write_authorize' if upload else 'git.lfs_read_batch', {'id': repo_id}
        )
        read_url = self.app.settings.service_url.rstrip('/') + await self._repo_read_path(rid)
        write_url = self.app.settings.service_url.rstrip('/') + '/-/git/' + rid + '/info/lfs'
        objects = []
        for item in body['objects']:
            require(
                type(item) is dict
                and set(item) == {'oid', 'size'}
                and type(item['oid']) is str
                and re.fullmatch(r'[0-9a-f]{64}', item['oid'])
                and type(item['size']) is int
                and 0 <= item['size'] <= MAX_LFS_OBJECT_BYTES,
                'invalid_lfs_object',
            )
            oid, size = item['oid'], item['size']
            actual = self.store.lfs(rid).size(oid)
            if actual is not None and actual != size:
                objects.append({
                    'oid': oid,
                    'size': size,
                    'error': {'code': 422, 'message': 'size mismatch'},
                })
            elif upload:
                action = (
                    {}
                    if actual is not None
                    else {
                        'upload': {
                            'href': write_url + '/objects/' + oid + '/' + str(size),
                            'header': {'Authorization': request.headers.get('authorization', '')},
                        }
                    }
                )
                objects.append({'oid': oid, 'size': size, 'actions': action})
            elif actual is None:
                objects.append({
                    'oid': oid,
                    'size': size,
                    'error': {'code': 404, 'message': 'object missing'},
                })
            else:
                objects.append({
                    'oid': oid,
                    'size': size,
                    'actions': {'download': {'href': read_url + '/info/lfs/objects/' + oid}},
                })
        return json_response(
            {'transfer': 'basic', 'objects': objects},
            headers={'Cache-Control': 'no-store', 'Content-Type': 'application/vnd.git-lfs+json'},
        )

    async def _repo_read_path(self, rid):
        async with self.app.metadata.transaction(write=False) as tx:
            return await tx.path(rid)

    async def http(self, request, repo_path, suffix):
        require(suffix != 'git-receive-pack', 'method_not_allowed')
        require(suffix in {'info/refs', 'git-upload-pack', 'HEAD'}, 'not_found')
        require(
            (request.method in {'GET', 'HEAD'} and suffix in {'info/refs', 'HEAD'})
            or (request.method == 'POST' and suffix == 'git-upload-pack'),
            'method_not_allowed',
        )
        query = request.query_params
        require(
            not query or dict(query) == {'service': 'git-upload-pack'},
            'git_push_requires_authenticated_transport',
        )
        result = await self.app.executor.execute(
            request_for('git.refs', {'id': repo_path, 'limit': 1}, self.app.settings.service_url)
        )
        require(result.status == 'ok', result.error.code if result.error else 'permission_denied')
        rid = result.data['resource_id']
        body = (
            await body_bytes(request, self.app.settings.server.limits.max_request_bytes)
            if request.method == 'POST'
            else b''
        )
        env = {
            **self.store.env,
            'GIT_PROJECT_ROOT': str(self.store.root),
            'PATH_INFO': '/' + rid + '.git/' + suffix,
            'REQUEST_METHOD': 'GET' if request.method == 'HEAD' else request.method,
            'QUERY_STRING': 'service=git-upload-pack' if query else '',
            'CONTENT_TYPE': 'application/x-git-upload-pack-request' if body else '',
            'CONTENT_LENGTH': str(len(body)),
        }
        protocol = request.headers.get('git-protocol')
        if protocol:
            require(protocol in {'version=0', 'version=1', 'version=2'}, 'invalid_git_protocol')
            env['GIT_PROTOCOL'] = protocol
        process = await asyncio.create_subprocess_exec(
            'git',
            'http-backend',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            limit=65536,
        )

        async def feed():
            try:
                process.stdin.write(body)
                await process.stdin.drain()
            finally:
                process.stdin.close()

        sender = asyncio.create_task(feed())
        headers = {}
        size = 0
        status = 200
        try:
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), 30)
                size += len(line)
                require(line and size <= 8192, 'invalid_git_response')
                if line in {b'\r\n', b'\n'}:
                    break
                key, _, value = line.decode('latin1').partition(':')
                if key.lower() == 'status':
                    status = int(value.strip().split(' ', 1)[0])
                elif key.lower() in {'content-type', 'cache-control', 'expires', 'pragma'}:
                    headers[key] = value.strip()
            if request.method == 'HEAD':
                if process.returncode is None:
                    process.kill()
                await process.wait()
                await sender
                return Response(status_code=status, headers=headers)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            sender.cancel()
            raise

        async def stream():
            try:
                while chunk := await asyncio.wait_for(process.stdout.read(65536), 120):
                    yield chunk
                await sender
                require(await process.wait() == 0, 'git_operation_failed')
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                if not sender.done():
                    sender.cancel()

        return StreamingResponse(
            stream(), status_code=status, headers={**headers, 'X-Content-Type-Options': 'nosniff'}
        )

    async def http_push(self, request, rid, suffix):
        require(bool(re.fullmatch(r'[A-Za-z0-9_-]{1,128}', rid)), 'not_found')
        require(suffix in {'info/refs', 'git-receive-pack'}, 'not_found')
        advertise = suffix == 'info/refs'
        require(request.method == ('GET' if advertise else 'POST'), 'method_not_allowed')
        pairs = request.query_params.multi_items()
        require(len(pairs) == len({name for name, _ in pairs}), 'duplicate_query_parameter')
        require(
            dict(request.query_params) == ({'service': 'git-receive-pack'} if advertise else {}),
            'invalid_git_service',
        )
        if not advertise:
            require(
                request.headers.get('content-type', '').split(';', 1)[0]
                == 'application/x-git-receive-pack-request',
                'invalid_git_content_type',
            )
        limits = self.app.settings.server.limits
        operation = 'git.http_advertise' if advertise else 'git.http_receive'
        signed = request.headers.get('x-msg-request')
        authorization = request.headers.get('authorization', '')
        require(not (signed and authorization), 'ambiguous_proof')
        if not signed and not authorization:
            return json_response(
                {
                    'status': 'error',
                    'error': {'code': 'authentication_required', 'retryable': False},
                },
                401,
                headers={'WWW-Authenticate': 'Basic realm="msg Git"'},
            )
        if not advertise:
            return await self._http_push_receive(
                request, rid, operation, signed, authorization, limits
            )
        arguments = {'id': rid}
        if signed:
            packet = path_packet(signed, 'j', limits.max_request_bytes)
            require(
                packet.operation == operation
                and canonical(packet.arguments) == canonical(arguments),
                'representation_mismatch',
            )
        else:
            require(authorization.startswith('Basic '), 'authentication_required')
            try:
                user, password = (
                    base64.b64decode(authorization[6:], validate=True).decode('ascii').split(':', 1)
                )
                token = unb64(password)
            except (ValueError, UnicodeDecodeError, Failure) as exc:
                raise Failure('invalid_token') from exc
            packet = request_for(
                operation,
                arguments,
                self.app.settings.service_url,
                token=(user, token),
                expires_at=self.app.clock() + timedelta(seconds=120),
            )
        marker = _HTTP_RECEIVE.set(False)
        try:
            result = await self.app.executor.execute(packet, entry='network')
        finally:
            _HTTP_RECEIVE.reset(marker)
        if result.error:
            return json_response(
                {'status': 'error', 'error': wire(result.error)}, error_status(result.error.code)
            )
        process = await asyncio.create_subprocess_exec(
            'git',
            '--git-dir',
            str(self.store.path(rid)),
            'receive-pack',
            '--stateless-rpc',
            '--advertise-refs',
            str(self.store.path(rid)),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=self.store.env,
        )
        chunks = []
        size = 0
        try:
            while chunk := await asyncio.wait_for(process.stdout.read(65536), 30):
                size += len(chunk)
                require(size <= limits.max_response_bytes, 'response_too_large')
                chunks.append(chunk)
            require(await asyncio.wait_for(process.wait(), 30) == 0, 'git_operation_failed')
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        output = b''.join(chunks)
        prefix = b'# service=git-receive-pack\n'
        payload = f'{len(prefix) + 4:04x}'.encode() + prefix + b'0000' + output
        require(len(payload) <= limits.max_response_bytes, 'response_too_large')
        return Response(
            payload,
            media_type='application/x-git-receive-pack-advertisement',
            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'},
        )

    async def _http_push_receive(self, request, rid, operation, signed, authorization, limits):
        # Reject missing/invalid request IDs before accepting a pack. This also
        # leaves no job or staged file when a client disconnects during upload.
        if signed:
            packet = path_packet(signed, 'j', limits.max_request_bytes)
            require(
                packet.operation == operation and bool(packet.request_id), 'git_request_id_required'
            )
        else:
            require(authorization.startswith('Basic '), 'authentication_required')
            request_id = request.headers.get('x-msg-request-id')
            require(
                request_id is not None
                and re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', request_id) is not None,
                'git_request_id_required',
            )
            try:
                user, password = (
                    base64.b64decode(authorization[6:], validate=True).decode('ascii').split(':', 1)
                )
                token = unb64(password)
            except (ValueError, UnicodeDecodeError, Failure) as exc:
                raise Failure('invalid_token') from exc
        self.app.settings.server.staging_dir.mkdir(parents=True, exist_ok=True)
        async with _GIT_UPLOAD_SLOTS:
            with tempfile.TemporaryDirectory(
                prefix='msg-git-http-', dir=self.app.settings.server.staging_dir
            ) as temp:
                pack_path = Path(temp) / 'receive.pack'
                try:
                    async with asyncio.timeout(MAX_GIT_UPLOAD_SECONDS):
                        pack_digest, pack_size = await spool_git_pack(request, pack_path)
                except TimeoutError as exc:
                    raise Failure('request_incomplete') from exc
                arguments = {'id': rid, 'pack_digest': pack_digest, 'pack_size': pack_size}
                if signed:
                    require(
                        canonical(packet.arguments) == canonical(arguments),
                        'representation_mismatch',
                    )
                else:
                    packet = request_for(
                        operation,
                        arguments,
                        self.app.settings.service_url,
                        token=(user, token),
                        request_id=request_id,
                        expires_at=self.app.clock() + timedelta(seconds=120),
                    )
                if pack_size == 4 and pack_path.read_bytes() == b'0000':
                    # Git's remote-curl probe_rpc sends a complete 0000 flush
                    # before a large chunked upload. The probe is not a ref
                    # update: authenticate and recheck write ACL, but leave its
                    # request ID unused for the real receive-pack POST.
                    if signed:
                        spec = self.app.registry.operation(
                            packet.operation, packet.contract_version
                        )
                        self.app.registry.validate(spec.input_schema, packet.arguments)
                        async with self.app.metadata.transaction(write=False) as tx:
                            principal = await self.app.authenticator.authenticate(
                                packet, tx, entry='network'
                            )
                            context = ExecutionContext(
                                request_id=packet.request_id,
                                principal=principal,
                                entry='network',
                                now=self.app.clock(),
                                deadline_monotonic=time.monotonic() + 30,
                            )
                            resource = await tx.resource(rid)
                            require(
                                resource.type == 'repo' and resource.state == 'active',
                                'not_a_repository',
                            )
                            await check_access(self.app, context, packet, tx, rid, 'write')
                    else:
                        preflight = request_for(
                            'git.http_advertise',
                            {'id': rid},
                            self.app.settings.service_url,
                            token=(user, token),
                            expires_at=self.app.clock() + timedelta(seconds=120),
                        )
                        checked = await self.app.executor.execute(preflight, entry='network')
                        if checked.error:
                            return json_response(
                                {'status': 'error', 'error': wire(checked.error)},
                                error_status(checked.error.code),
                            )
                    return Response(
                        b'0000',
                        media_type='application/x-git-receive-pack-result',
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'},
                    )
                marker = _HTTP_RECEIVE.set(True)
                try:
                    result = await self.app.executor.execute(packet, entry='network')
                finally:
                    _HTTP_RECEIVE.reset(marker)
                if result.error:
                    return json_response(
                        {'status': 'error', 'error': wire(result.error)},
                        error_status(result.error.code),
                    )
                job_id = result.data['job_id']
                if result.replayed:
                    async with self.app.metadata.transaction(write=False) as tx:
                        cached = tx.setting('git_http_result:' + job_id)
                        status = tx.setting('job_status:' + job_id)
                    if cached is None:
                        raise Failure(
                            status['code']
                            if status and status['code'] not in {'ok'}
                            else 'external_uncertain'
                        )
                    return Response(
                        unb64(cached),
                        media_type='application/x-git-receive-pack-result',
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'},
                    )
                require_git_repository_capacity(self.store.path(rid), incoming=pack_size)
                code, output, _changed = await guarded_command(
                    self.app,
                    await self._job(job_id),
                    lambda store: ['receive-pack', '--stateless-rpc', str(store.path(rid))],
                    input_file=pack_path,
                    capture_output=True,
                    output_limit=limits.max_response_bytes,
                    timeout=600,
                    cache_result_key='git_http_result:' + job_id,
                )
                require(code == 0, 'git_operation_failed')
                return Response(
                    output,
                    media_type='application/x-git-receive-pack-result',
                    headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'},
                )

    async def _job(self, id):
        async with self.app.metadata.transaction(write=False) as tx:
            return await tx.job(id)
