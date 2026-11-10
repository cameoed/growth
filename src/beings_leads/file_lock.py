"""Cross-process ownership for SQLite maintenance and scan resource slots."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import ctypes
    import errno
    import msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("internal", ctypes.c_size_t),
            ("internal_high", ctypes.c_size_t),
            ("offset", wintypes.DWORD),
            ("offset_high", wintypes.DWORD),
            ("event", wintypes.HANDLE),
        ]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _lock_file = _kernel.LockFileEx
    _lock_file.argtypes = [wintypes.HANDLE, *[wintypes.DWORD] * 4, ctypes.POINTER(_Overlapped)]
    _lock_file.restype = wintypes.BOOL
    _unlock_file = _kernel.UnlockFileEx
    _unlock_file.argtypes = [wintypes.HANDLE, *[wintypes.DWORD] * 3, ctypes.POINTER(_Overlapped)]
    _unlock_file.restype = wintypes.BOOL
    _FAIL_IMMEDIATELY = 1
    _EXCLUSIVE = 2
    _LOCK_VIOLATION = 33

    def _lock_byte(fd: int, slot: int, *, shared: bool, blocking: bool) -> None:
        region = _Overlapped(offset=slot)
        flags = (0 if shared else _EXCLUSIVE) | (0 if blocking else _FAIL_IMMEDIATELY)
        if not _lock_file(msvcrt.get_osfhandle(fd), flags, 0, 1, 0, ctypes.byref(region)):
            error = ctypes.get_last_error()
            if not blocking and error == _LOCK_VIOLATION:
                raise BlockingIOError(errno.EAGAIN, "File lock is held")
            raise ctypes.WinError(error)

    def unlock_slot(fd: int, slot: int) -> None:
        region = _Overlapped(offset=slot)
        if not _unlock_file(msvcrt.get_osfhandle(fd), 0, 1, 0, ctypes.byref(region)):
            raise ctypes.WinError(ctypes.get_last_error())

    def lock_slot(fd: int, slot: int) -> None:
        _lock_byte(fd, slot, shared=False, blocking=False)

else:
    import fcntl

    def lock_slot(fd: int, slot: int) -> None:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, slot)

    def unlock_slot(fd: int, slot: int) -> None:
        fcntl.lockf(fd, fcntl.LOCK_UN, 1, slot)


def open_directory_lock(path: Path, *, shared: bool = False, blocking: bool = True) -> int:
    """Hold a directory lock until its descriptor closes."""
    # Windows cannot use a directory fd with LockFileEx. Keep one stable lock file
    # per directory, independent of database replacements.
    if os.name == "nt":
        fd = os.open(path / ".beings-growth.lock", os.O_CREAT | os.O_RDWR, 0o600)
    else:
        fd = os.open(path, os.O_RDONLY)
    try:
        if os.name == "nt":
            _lock_byte(fd, 0, shared=shared, blocking=blocking)
        else:
            flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(fd, flags | (0 if blocking else fcntl.LOCK_NB))
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def directory_lock(path: Path):
    fd = open_directory_lock(path)
    try:
        yield
    finally:
        os.close(fd)
