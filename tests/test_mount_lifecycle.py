"""Native negotiation and cancellation must not block the SDK event loop."""

import asyncio
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from msg.client_mount import READ_BYTES, ReadOnlyMount, serve_mount


def test_fuse3_read_limit_handshake():
    mount = ReadOnlyMount(None, None)
    connection = SimpleNamespace(max_read=0)
    mount.init_with_config(connection, None)
    assert connection.max_read == READ_BYTES
    mount.init_with_config(SimpleNamespace(), None)
    mount.init_with_config(None, None)


@pytest.mark.asyncio
async def test_cancellation_checks_mount_off_the_sdk_loop(monkeypatch, tmp_path):
    loop_thread = threading.get_ident()
    started, stopped = threading.Event(), threading.Event()
    checks = []

    def native_mount(operations, path, **options):
        assert threading.get_ident() != loop_thread
        assert options['ro'] and options['direct_io']
        assert 'allow_other' not in options
        started.set()
        assert stopped.wait(5), 'unmount did not stop the native worker'

    def ismount(path):
        checks.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return started.is_set() and not stopped.is_set()

    async def unmount(*command, **kwargs):
        assert command == ('/usr/bin/fusermount3', '-u', '--', str(tmp_path))
        stopped.set()
        return SimpleNamespace(wait=AsyncMock(return_value=0))

    monkeypatch.setattr(os.path, 'ismount', ismount)
    monkeypatch.setattr('shutil.which', lambda name: '/usr/bin/fusermount3')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', unmount)
    operations = ReadOnlyMount(None, asyncio.get_running_loop())
    operations.handles[1] = object()
    task = asyncio.create_task(serve_mount(SimpleNamespace(FUSE=native_mount), operations, tmp_path))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert checks and not operations.handles
    finally:
        stopped.set()
        if not task.done():
            task.cancel()
