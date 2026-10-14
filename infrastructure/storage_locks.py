"""Reentrant local file mutexes for short cross-process storage operations."""
from __future__ import annotations

import errno
import os
import threading
import time
import weakref
from contextlib import contextmanager
from pathlib import Path


class LockBusy(RuntimeError):
    pass


_guard = threading.Lock()
_mutexes = weakref.WeakValueDictionary()
_local = threading.local()


@contextmanager
def file_mutex(path: Path, *, blocking: bool = True):
    key = str(Path(path).resolve())
    with _guard:
        mutex = _mutexes.get(key)
        if mutex is None:
            mutex = threading.RLock()
            _mutexes[key] = mutex
    if not mutex.acquire(blocking=blocking):
        raise LockBusy(key)
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = set()
    handle = None
    locked = False
    try:
        if key in held:
            yield
            return
        Path(key).parent.mkdir(parents=True, exist_ok=True)
        handle = open(key, "a+b")
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if not blocking:
                    raise LockBusy(key) from exc
                time.sleep(0.05)
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)
    finally:
        if handle is not None:
            try:
                if locked:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        mutex.release()
