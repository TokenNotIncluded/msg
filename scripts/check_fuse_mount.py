"""Exercise the real FUSE kernel boundary; missing native support is a CI failure.

Only this smoke test supplies an in-memory backend. The production FUSE adapter,
mount flags, thread bridge, offset reads and unmount lifecycle remain unchanged.
"""

import asyncio
import errno
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from msg.client_mount import Node, ReadOnlyMount, io_error, serve_mount

BODY = bytes(range(256)) * 1200


class FixtureBackend:
    async def node(self, path):
        if path not in {'/', '/data.bin'}:
            raise io_error(errno.ENOENT)
        return Node({
            'id': 'root' if path == '/' else 'file',
            'path': path,
            'container': path == '/',
            'revision': 'v_one',
            'size': len(BODY),
        })

    async def directory(self, path):
        if path != '/':
            raise io_error(errno.ENOTDIR)
        return ['.', '..', 'data.bin']

    async def query(self, operation, arguments):
        return {}

    async def read(self, node, size, offset):
        return BODY[offset : offset + size]


def inspect_mount(path):
    assert sorted(p.name for p in path.iterdir()) == ['data.bin']
    target = path / 'data.bin'
    assert target.stat().st_size == len(BODY)
    with target.open('rb') as stream:
        assert stream.read() == BODY
        stream.seek(12345)
        assert stream.read(2345) == BODY[12345:14690]
    for operation in (
        lambda: target.write_bytes(b'no'),
        target.unlink,
        lambda: (path / 'new').mkdir(),
    ):
        try:
            operation()
        except OSError as exc:
            assert exc.errno == errno.EROFS, exc
        else:
            raise AssertionError('read-only mount accepted a mutation')
    assert target.read_bytes() == BODY


async def exercise(path, *, cancel=False):
    import mfusepy

    operations = ReadOnlyMount(FixtureBackend(), asyncio.get_running_loop())
    task = asyncio.create_task(serve_mount(mfusepy, operations, path))
    try:
        for _ in range(250):
            if task.done():
                await task
                raise AssertionError('FUSE exited before mounting')
            if await asyncio.to_thread(os.path.ismount, path):
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError('FUSE mount did not appear')
        await asyncio.wait_for(asyncio.to_thread(inspect_mount, path), 15)
        if cancel:
            task.cancel()
            try:
                await asyncio.wait_for(task, 10)
            except asyncio.CancelledError:
                pass
        else:
            executable = shutil.which('fusermount3') or shutil.which('fusermount')
            if not executable:
                raise AssertionError('fusermount is required by the Linux smoke test')
            await asyncio.to_thread(
                subprocess.run, [executable, '-u', '--', str(path)], check=True, timeout=10
            )
            await asyncio.wait_for(task, 10)
        assert not await asyncio.to_thread(os.path.ismount, path)
        assert not operations.handles
    finally:
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, 10)
            except asyncio.CancelledError:
                pass


async def main():
    if not Path('/dev/fuse').exists():
        raise SystemExit('FUSE smoke requires /dev/fuse; this is not a skipped acceptance test.')
    with tempfile.TemporaryDirectory(prefix='msg-fuse-') as directory:
        path = Path(directory)
        await exercise(path)
        await exercise(path, cancel=True)
    print('FUSE kernel smoke passed: list/stat/read/seek/read-only/unmount/cancellation')


if __name__ == '__main__':
    asyncio.run(main())
