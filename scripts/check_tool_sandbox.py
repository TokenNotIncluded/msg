"""Fail-fast local/CI preflight using the real runner and a non-network packet.

Only this operator-invoked probe captures subprocess stderr. The packet contains
no user input or credentials, and production errors keep their stable safe code.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from tempfile import TemporaryDirectory, TemporaryFile
from types import SimpleNamespace
from unittest.mock import patch

from msg.core.errors import Failure
from msg.core.models import NetworkPolicy
from msg.workers.sandbox import BubblewrapRunner


async def check():
    print(f'Python executable: {sys.executable}', flush=True)
    print(f'Python prefix: {sys.prefix}; base prefix: {sys.base_prefix}', flush=True)
    setting = Path('/proc/sys/kernel/apparmor_restrict_unprivileged_userns')
    if setting.exists():
        print(f'AppArmor unprivileged userns restriction: {setting.read_text().strip()}', flush=True)
    policy = NetworkPolicy(schemes=frozenset({'https'}), hosts=(), ports=frozenset({443}),
                           methods=frozenset({'GET'}), allow_private=False,
                           timeout_ms=1000, max_response_bytes=1024, max_redirects=0)
    app = SimpleNamespace(settings=SimpleNamespace(server=SimpleNamespace(
        limits=SimpleNamespace(max_request_bytes=1024))))
    spawn = asyncio.create_subprocess_exec
    with TemporaryDirectory(prefix='msg-sandbox-preflight-') as directory, TemporaryFile() as diagnostics:
        async def capture(*args, **kwargs):
            kwargs['stderr'] = diagnostics
            return await spawn(*args, **kwargs)

        try:
            with patch('asyncio.create_subprocess_exec', capture):
                try:
                    await BubblewrapRunner(app)(SimpleNamespace(executor_key='curl'),
                        {'url': 'file:///not-a-network-target'}, (policy,), Path(directory))
                except Failure as exc:
                    if exc.code != 'network_policy_denied':
                        raise
                else:
                    raise AssertionError('Sandbox child accepted an invalid scheme')
        finally:
            diagnostics.seek(0)
            stderr = diagnostics.read(16384).decode(errors='replace')
            if stderr:
                print('Sandbox preflight stderr:\n' + stderr, file=sys.stderr, flush=True)
    print('Sandbox child started and enforced network policy.', flush=True)


if __name__ == '__main__':
    asyncio.run(check())
