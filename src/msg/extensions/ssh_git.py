"""Bridge Git's reference transaction to a current-authority metadata transaction.

Pack upload happens without holding a PostgreSQL write transaction. The
reference hook reauthorizes at ref preparation. Git and PostgreSQL are not one
atomic storage engine: a lost commit acknowledgement is recorded as uncertain.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import shlex
import sys
import tempfile

from msg.core.codec import b64, canonical, loads, wire
from msg.core.errors import Failure, require
from msg.extensions.repositories import (NativeGitStore, MAX_GIT_PACK_BYTES, MAX_GIT_UPLOAD_SECONDS,
    require_git_repository_capacity)
from msg.plugins.common import check_access
from msg.storage.git import durable_write
from msg.workers.effects import current_principal, worker_context, effect_request


class ReferenceGuard:
    def __init__(self, app, job):
        self.app, self.job = app, job
        self.queue = asyncio.Queue()
        self.changed = False
        self.uncertain = False
        self.failures = []

    async def authorize(self, tx):
        current = await tx.job(self.job.id)
        require(current.state == 'running' and current.attempts == self.job.attempts, 'job_lease_lost')
        principal = await current_principal(self.app, self.job.principal, tx)
        context, request = worker_context(self.app, self.job, principal), effect_request(self.app, self.job, principal)
        await check_access(self.app, context, request, tx, self.job.arguments['id'], 'write')
        require(tx.setting('runtime_config', {}).get('accept_writes', True), 'writes_paused')
        return principal

    @staticmethod
    def validate_changes(text):
        require(isinstance(text, str) and len(text.encode()) <= 65536, 'git_ref_batch_too_large')
        rows = []
        import re
        for line in text.splitlines():
            fields = line.split(' ')
            require(len(fields) == 3, 'invalid_git_change')
            old, new, ref = fields
            require(re.fullmatch(r'[0-9a-f]{40}', old) is not None and re.fullmatch(r'[0-9a-f]{40}', new) is not None,
                    'invalid_git_change')
            require(ref == 'HEAD' or re.fullmatch(r'refs/(heads|tags)/[^\s]+', ref) is not None, 'invalid_git_ref')
            rows.append({'ref':ref, 'old':old, 'new':new})
        require(0 < len(rows) <= 128 and len({r['ref'] for r in rows}) == len(rows), 'invalid_git_change')
        return rows

    async def run(self):
        """One task owns both halves of each PostgreSQL transaction."""
        manager = tx = None
        preparing = prepared = None
        while True:
            message, answer = await self.queue.get()
            try:
                state = message['state']
                if state == 'stop':
                    if manager is not None:
                        self.uncertain = True
                        await manager.__aexit__(Failure, Failure('external_uncertain'), None)
                    answer.set_result({'ok':True})
                    return
                changes = self.validate_changes(message['changes'])
                if state == 'preparing':
                    require(manager is None, 'git_transaction_already_prepared')
                    # Git 2.54+ invokes this phase before taking ref locks.
                    # Preflight current authority without holding a PostgreSQL write
                    # transaction, then re-check inside the prepared phase.
                    async with self.app.metadata.transaction(write=False) as preflight:
                        await self.authorize(preflight)
                    preparing = changes
                elif state == 'prepared':
                    require(manager is None, 'git_transaction_already_prepared')
                    require(preparing is None or changes == preparing, 'git_transaction_mismatch')
                    manager = self.app.metadata.transaction(write=True)
                    tx = await manager.__aenter__()
                    try:
                        await self.authorize(tx)
                    except BaseException:
                        await manager.__aexit__(*sys.exc_info())
                        manager = tx = None
                        raise
                    prepared = changes
                elif state in {'committed', 'aborted'}:
                    require(manager is not None and changes == prepared, 'git_transaction_mismatch')
                    active = manager
                    manager = None
                    if state == 'aborted':
                        await active.__aexit__(Failure, Failure('git_transaction_aborted'), None)
                    else:
                        self.changed = True
                        try:
                            resource = await tx.resource(self.job.arguments['id'])
                            await tx.replace(replace(resource, generation=resource.generation + 1,
                                modified_at=self.app.clock(), modified_by=self.job.principal.actor), resource.generation)
                            history = tx.setting('git_receive:' + self.job.id, [])
                            history.append({'time':wire(self.app.clock()), 'refs':changes})
                            tx.set_setting('git_receive:' + self.job.id, history)
                            from msg.core.models import Event, AuditEvent, ResourceRef
                            from msg.core.codec import digest
                            from msg.plugins.common import new_id
                            event = Event(id=new_id('event'), type='git.refs_committed', time=self.app.clock(),
                                request_id=self.job.arguments['request_id'], actor=self.job.principal.actor,
                                subject=self.job.principal.subject, resources=(), data={'job_id':self.job.id, 'refs':changes})
                            await tx.append_event(event)
                            await tx.append_audit(AuditEvent(event=event,
                                authority=tuple(ResourceRef(id=c) for c in self.job.principal.certificates),
                                before_digest=digest(resource), after_digest=digest(changes),
                                previous_digest=None, entry_digest='', result='refs_committed'))
                            await active.__aexit__(None, None, None)
                        except BaseException:
                            self.uncertain = True
                            if not tx.closed:
                                await active.__aexit__(*sys.exc_info())
                            raise
                    tx = preparing = prepared = None
                else:
                    raise Failure('invalid_git_hook_state')
                answer.set_result({'ok':True})
            except BaseException as exc:
                code = exc.code if isinstance(exc, Failure) else 'git_guard_failed'
                self.failures.append(code)
                if not answer.done():
                    answer.set_result({'ok':False, 'code':code})

    async def message(self, value):
        answer = asyncio.get_running_loop().create_future()
        await self.queue.put((value, answer))
        return await asyncio.shield(answer)


def hook_program(socket_path, secret):
    """Generated from trusted code, not from a repository or user command."""
    return ('#!' + sys.executable + '\n' +
        'import json,socket,sys\n'
        'data=sys.stdin.buffer.read(65537)\n'
        'if len(data)>65536: sys.exit(1)\n'
        's=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.settimeout(45)\n' +
        's.connect(' + repr(str(socket_path)) + ')\n' +
        's.sendall((json.dumps({"secret":' + repr(secret) + ',"state":sys.argv[1],"changes":data.decode("ascii")})+"\\n").encode())\n'
        'r=s.makefile("rb").readline(8193)\n'
        'sys.exit(0 if r and len(r)<=8192 and json.loads(r).get("ok") else 1)\n').encode()


async def relay_bounded_stdin(process, stream, *, limit, timeout):
    """Copy stream to process stdin, rejecting once limit bytes have been seen."""
    loop=asyncio.get_running_loop()
    total=0
    async def pump():
        nonlocal total
        while True:
            chunk=await loop.run_in_executor(None, stream.read, 65536)
            if not chunk:
                break
            total+=len(chunk)
            require(total<=limit,'request_too_large')
            process.stdin.write(chunk)
            await process.stdin.drain()
        process.stdin.close()
    await asyncio.wait_for(pump(), timeout)
    return await asyncio.wait_for(process.wait(), timeout)


async def guarded_command(app, job, command_factory, *, stdin=None, stdout=None, stderr=None, timeout=600,
                          input_data=None,input_file=None,capture_output=False,output_limit=None,cache_result_key=None,
                          stdin_byte_limit=None,stdin_stream=None):
    """Run a fixed Git command with a private reference-transaction hook.

    command_factory is installed adapter code, never a value accepted from wire.
    Tests exercise this bridge with real update-ref; the SSH adapter uses receive-pack.
    """
    store = NativeGitStore(app)
    require(input_data is None or input_file is None,'ambiguous_git_input')
    require(stdin_byte_limit is None or (input_data is None and input_file is None),'ambiguous_git_input')
    app.settings.server.staging_dir.mkdir(parents=True, exist_ok=True)
    # Unix socket path has a small OS limit; the random private directory is not
    # an account directory and contains no application credentials.
    with tempfile.TemporaryDirectory(prefix='msg-git-') as temporary:
        directory = Path(temporary)
        socket_path = directory/'guard.sock'
        secret = secrets.token_hex(32)
        guard = ReferenceGuard(app, job)
        task = asyncio.create_task(guard.run())
        async def connection(reader, writer):
            try:
                raw = await asyncio.wait_for(reader.readline(), 45)
                require(len(raw) <= 70000, 'git_hook_message_too_large')
                value = loads(raw)
                require(hmac.compare_digest(value.get('secret',''), secret), 'git_hook_authentication_failed')
                result = await guard.message(value)
                writer.write(canonical(result) + b'\n')
                await writer.drain()
            except Exception:
                writer.write(b'{"ok":false}\n')
                try:await writer.drain()
                except (BrokenPipeError, ConnectionError):pass
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_unix_server(connection, str(socket_path), limit=70001)
        durable_write(directory/'reference-transaction', hook_program(socket_path, secret), mode=0o700)
        env = dict(store.env)
        command = ['git','--git-dir',str(store.path(job.arguments['id'])),'-c','core.fsync=all',
            '-c','core.hooksPath='+str(directory),'-c','receive.fsckObjects=true',
            '-c','receive.denyNonFastForwards=true',*command_factory(store)]
        process = None
        code = None
        output = b''
        try:
            process = await asyncio.create_subprocess_exec(*command,
                stdin=asyncio.subprocess.PIPE if input_data is not None or input_file is not None or stdin_byte_limit is not None else stdin,
                stdout=asyncio.subprocess.PIPE if capture_output else stdout,
                stderr=asyncio.subprocess.DEVNULL if capture_output else stderr,
                env=env,start_new_session=True)
            if input_data is not None or input_file is not None:
                async def exchange():
                    async def feed():
                        try:
                            if input_file is None:
                                process.stdin.write(input_data)
                                await process.stdin.drain()
                            else:
                                with open(input_file,'rb') as source:
                                    while chunk:=source.read(65536):
                                        process.stdin.write(chunk)
                                        await process.stdin.drain()
                        except (BrokenPipeError,ConnectionResetError):
                            pass
                        finally:
                            process.stdin.close()
                    sender=asyncio.create_task(feed())
                    try:
                        chunks=[]
                        size=0
                        while chunk:=await process.stdout.read(65536):
                            size+=len(chunk)
                            require(output_limit is None or size<=output_limit,'response_too_large')
                            chunks.append(chunk)
                        await sender
                        await process.wait()
                        return b''.join(chunks)
                    finally:
                        if not sender.done():sender.cancel()
                        await asyncio.gather(sender,return_exceptions=True)
                output=await asyncio.wait_for(exchange(),timeout)
                code=process.returncode
            else:
                if stdin_byte_limit is not None:
                    code = await relay_bounded_stdin(process, sys.stdin.buffer if stdin_stream is None else stdin_stream,
                                                     limit=stdin_byte_limit, timeout=timeout)
                else:
                    code = await asyncio.wait_for(process.wait(), timeout)
        finally:
            if process is not None and process.returncode is None:
                import signal
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
                guard.uncertain = True
            await guard.message({'state':'stop'})
            await task
            server.close()
            await server.wait_closed()
            async with app.metadata.transaction(write=True) as tx:
                current = await tx.job(job.id)
                if current.state == 'running':
                    # Git ref transactions may have partially succeeded without
                    # --atomic. Audit the actual committed refs, never invent rollback.
                    uncertain=guard.uncertain or (guard.changed and (code != 0 or bool(guard.failures)))
                    state = 'uncertain' if uncertain else 'done' if code == 0 and not guard.failures else 'failed'
                    if state=='done' and cache_result_key is not None:
                        require(capture_output,'git_result_cache_requires_output')
                        tx.set_setting(cache_result_key,b64(output))
                    await tx.save_job(replace(current, state=state, lease_until=None))
                    tx.set_setting('job_status:'+job.id, {'code':'external_uncertain' if uncertain else
                        guard.failures[0] if guard.failures else 'ok' if state=='done' else 'git_operation_failed',
                        'refs_changed':guard.changed})
        result=code if code is not None and not guard.uncertain and not guard.failures else 1
        return (result,output,guard.changed) if capture_output else result


async def receive_pack(app, job_id, *, stdin_stream=None):
    async with app.metadata.transaction(write=False) as tx:
        job = await tx.job(job_id)
        require(job.kind == 'git.receive' and job.state == 'running', 'invalid_git_session')
    require_git_repository_capacity(NativeGitStore(app).path(job.arguments['id']))
    return await guarded_command(app, job, lambda store:['receive-pack',str(store.path(job.arguments['id']))],
        timeout=MAX_GIT_UPLOAD_SECONDS, stdin_byte_limit=MAX_GIT_PACK_BYTES, stdin_stream=stdin_stream)
