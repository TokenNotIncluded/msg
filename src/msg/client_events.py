"""Local database change hints. The mailbox, not the hint, is the durable queue."""

from __future__ import annotations

import asyncio
import ctypes
import os
import struct
import sys
from pathlib import Path


class EventsUnavailable(OSError):
    """The platform cannot provide native local file notifications."""


class LocalEvents:
    """Subscribe before reading, then wait without a timer or database polling.

    Linux inotify watches the private mailbox directory. Notifications carry no
    message bodies. A lost/overflowed hint causes a fresh mailbox read; a removed
    watch fails closed. Every reader owns its descriptor, so readers cannot steal
    notifications from each other. Other platforms use explicit polling fallback.
    """

    def __init__(self, database: Path):
        if sys.platform != 'linux':
            raise EventsUnavailable('Native local events require Linux.')
        library = ctypes.CDLL(None, use_errno=True)
        try:
            initialize = library.inotify_init1
            add_watch = library.inotify_add_watch
        except AttributeError:
            raise EventsUnavailable('inotify is not available.') from None
        initialize.argtypes, initialize.restype = [ctypes.c_int], ctypes.c_int
        add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add_watch.restype = ctypes.c_int
        self._loop = asyncio.get_running_loop()
        self._changed = asyncio.Event()
        self._name = os.fsencode(database.name)
        self._lost = False
        self._descriptor = initialize(os.O_NONBLOCK | os.O_CLOEXEC)
        if self._descriptor < 0:
            raise EventsUnavailable(ctypes.get_errno(), 'Cannot initialize local events.')
        # MODIFY, ATTRIB, MOVED_FROM/TO, CREATE, DELETE, DELETE_SELF, MOVE_SELF.
        # Do not watch OPEN/CLOSE: a read-only mailbox scan also opens SQLite.
        mask = 0x2 | 0x4 | 0x40 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800
        try:
            if add_watch(self._descriptor, os.fsencode(database.parent), mask) < 0:
                raise OSError(ctypes.get_errno(), 'Cannot watch the local mailbox.')
            self._loop.add_reader(self._descriptor, self._ready)
        except BaseException:
            os.close(self._descriptor)
            self._descriptor = -1
            raise

    def _ready(self):
        try:
            while True:
                data = os.read(self._descriptor, 65536)
                if not data:
                    self._lost = True
                    self._changed.set()
                    return
                offset = 0
                while offset < len(data):
                    _, mask, _, length = struct.unpack_from('=iIII', data, offset)
                    offset += 16
                    name = data[offset : offset + length].rstrip(b'\0')
                    offset += length
                    # UNMOUNT/IGNORED or a removed/moved directory invalidates
                    # the subscription. Never silently sleep on a dead watch.
                    if mask & (0x2000 | 0x8000 | 0x400 | 0x800):
                        self._lost = True
                    if self._lost or mask & 0x4000 or name == self._name:
                        self._changed.set()
        except BlockingIOError:
            pass
        except OSError:
            self._lost = True
            self._changed.set()

    async def wait(self):
        await self._changed.wait()
        self._changed.clear()
        if self._lost:
            raise OSError('Local event subscription was lost; restart the listener.')

    def close(self):
        if self._descriptor >= 0:
            self._loop.remove_reader(self._descriptor)
            os.close(self._descriptor)
            self._descriptor = -1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
