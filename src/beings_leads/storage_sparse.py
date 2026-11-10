"""Offline macOS deallocation of already-zero bytes; never edit SQLite structures.

Caller must hold the app maintenance lock and SQLite's exclusive lock, with WAL
checkpointed. F_PUNCHHOLE is documented by the installed macOS fcntl(2) manual:
reads of a deallocated range return zeros, and the logical file size is unchanged.
Only bytes proved to be zero are eligible, regardless of database page ownership.
"""

from __future__ import annotations

import hashlib
import math
import os
import stat
import struct
import sys


def _punch(fd, offset, length):
    if sys.platform != "darwin":
        raise OSError("Zero-block deallocation requires the macOS F_PUNCHHOLE interface")
    import fcntl

    # fpunchhole_t from the installed SDK's sys/fcntl.h: uint32,uint32,off_t,off_t.
    fcntl.fcntl(fd, 99, struct.pack("=IIqq", 0, 0, offset, length))


def release_zero_blocks(path, *, progress=lambda value: None, punch=_punch):
    """Make zero ranges sparse, with constant memory and complete byte/hash checks.

    No copy, truncation, guessed free-page map or nonzero overwrite is permitted.
    Cancellation after any range is safe: every operation is byte-identical.
    """
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Sparse maintenance requires a regular file")
        fs = os.fstatvfs(fd)
        block = math.lcm(fs.f_frsize or fs.f_bsize, before.st_blksize)
        chunk_size = max(block, (8 * 1024 * 1024 // block) * block)
        zero_block = bytes(block)
        original = hashlib.sha256()
        released = ranges = offset = 0
        next_report = 1024 * 1024 * 1024
        while offset < before.st_size:
            size = min(chunk_size, before.st_size - offset)
            data = os.pread(fd, size, offset)
            if len(data) != size:
                raise ValueError("Database size changed during zero-block maintenance")
            original.update(data)
            aligned = size // block * block
            start = None
            for position in range(0, aligned + block, block):
                zero = position < aligned and data[position:position + block] == zero_block
                if zero and start is None:
                    start = position
                elif not zero and start is not None:
                    length = position - start
                    punch(fd, offset + start, length)
                    if os.pread(fd, length, offset + start) != bytes(length):
                        raise ValueError("Filesystem did not preserve zero bytes")
                    released += length
                    ranges += 1
                    start = None
            offset += size
            if offset >= next_report:
                progress({"stage": "zero_blocks", "checked_bytes": offset,
                          "zero_bytes_deallocated": released})
                next_report += 1024 * 1024 * 1024
        os.fsync(fd)
        actual = hashlib.sha256()
        for position in range(0, before.st_size, chunk_size):
            size = min(chunk_size, before.st_size - position)
            data = os.pread(fd, size, position)
            if len(data) != size:
                raise ValueError("Database size changed during sparse verification")
            actual.update(data)
        after, named = os.fstat(fd), os.stat(path, follow_symlinks=False)
        if ((after.st_size, after.st_dev, after.st_ino)
                != (before.st_size, before.st_dev, before.st_ino)
                or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)
                or original.digest() != actual.digest()):
            raise ValueError("Zero-block release failed whole-file byte identity")
        result = {"byte_identity": True, "sha256": actual.hexdigest(),
                  "logical_bytes": after.st_size, "zero_bytes_deallocated": released,
                  "ranges": ranges, "allocated_before_bytes": before.st_blocks * 512,
                  "allocated_after_bytes": after.st_blocks * 512}
        progress({"stage": "zero_blocks_verified", **result})
        return result
    finally:
        os.close(fd)
