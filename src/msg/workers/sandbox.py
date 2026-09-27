"""Mandatory OS-isolated tool subprocess; no unsandboxed fallback."""
from __future__ import annotations
import asyncio
from pathlib import Path
import shutil
import sys
from msg.core.codec import canonical, loads, wire
from msg.core.errors import Failure, require


class BubblewrapRunner:
    def __init__(self, app):
        self.app = app

    async def __call__(self, tool, arguments, policies, directory):
        from msg.workers.effects import ToolResult
        bwrap = shutil.which('bwrap')
        require(bwrap is not None, 'tool_isolation_unavailable')
        source = Path(__file__).resolve().parents[2]
        # Only trusted interpreter/library trees and this package are visible.
        command = [bwrap, '--unshare-all', '--share-net', '--unshare-user', '--disable-userns',
            '--die-with-parent', '--new-session', '--cap-drop', 'ALL', '--clearenv',
            '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
        for path in ('/usr', '/bin', '/lib', '/lib64'):
            if Path(path).exists():
                command += ['--ro-bind', path, path]
        prefix = str(Path(sys.prefix).resolve())
        if not prefix.startswith('/usr') and prefix not in {'/','/etc','/home','/mnt'}:
            command += ['--ro-bind', prefix, prefix]
        command += ['--dir','/etc']
        for path in ('/etc/resolv.conf', '/etc/hosts', '/etc/nsswitch.conf', '/etc/ssl/certs'):
            if Path(path).exists():
                command += ['--ro-bind', path, path]
        output = directory/'sandbox-output'
        output.mkdir(mode=0o700)
        command += ['--ro-bind',str(source),'/app', '--bind',str(output),'/output',
            '--setenv','PYTHONPATH','/app','--setenv','PYTHONDONTWRITEBYTECODE','1',
            '--setenv','HOME','/nonexistent','--chdir','/tmp']
        args = dict(arguments)
        body = args.pop('_body_path', None)
        if body:
            command += ['--ro-bind', body, '/request-body']
            args['_body_path'] = '/request-body'
        command += [sys.executable, '-m', 'msg.workers.sandbox_child']
        packet = canonical({'executor':tool.executor_key, 'arguments':args, 'policies':wire(policies)})
        require(len(packet) <= self.app.settings.server.limits.max_request_bytes+65536, 'tool_request_too_large')
        process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=65536)
        timeout = max(p.timeout_ms for p in policies)/1000+10
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(packet), timeout)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise Failure('external_uncertain')
        if process.returncode != 0:
            if stdout:
                try:
                    detail = loads(stdout)
                    raise Failure(detail.get('code', 'external_uncertain'))
                except (ValueError, TypeError) as exc:
                    if isinstance(exc, Failure):
                        raise
            raise Failure('tool_isolation_failed')
        require(len(stdout) <= 65536, 'invalid_tool_manifest')
        data = loads(stdout)
        source_file = output/'result.bin'
        require(source_file.is_file() and not source_file.is_symlink(), 'invalid_tool_output_path')
        destination = directory/'output.bin'
        source_file.rename(destination)
        return ToolResult(path=destination, media_type=data['media_type'], metadata=data['metadata'])
