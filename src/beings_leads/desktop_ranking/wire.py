"""Framed, lossless messages shared by the ranking owner and the Oracle service."""

import hashlib
import json
import struct
import zlib
from functools import cache
from pathlib import Path

VERSION = 1
FRAME_BYTES = 256 * 1024
MAX_MESSAGE_BYTES = 256 * 1024 * 1024
_HEADER = struct.Struct("!I")


class ProtocolError(RuntimeError):
    pass


class Wire:
    def __init__(self, reader, writer, *, compress=True):
        self.reader = reader
        self.writer = writer
        self.compress = compress
        self.bytes_received = 0

    def send(self, value):
        data = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(data) > MAX_MESSAGE_BYTES:
            raise ProtocolError("Ranking message exceeds its safety limit")
        for start in range(0, len(data), FRAME_BYTES):
            chunk = data[start : start + FRAME_BYTES]
            packed = zlib.compress(chunk, 1) if self.compress and len(chunk) >= 256 else chunk
            compressed = len(packed) < len(chunk)
            header = _HEADER.pack(len(packed) | 0x80000000 if compressed else len(chunk))
            end = _HEADER.pack(0) if start + FRAME_BYTES >= len(data) else b""
            self.writer.write(header + (packed if compressed else chunk) + end)
        self.writer.flush()

    def receive(self):
        data = bytearray()
        while True:
            header = self.reader.read(_HEADER.size)
            if len(header) != _HEADER.size:
                raise EOFError("Ranking connection closed")
            self.bytes_received += len(header)
            length = _HEADER.unpack(header)[0]
            if length == 0:
                if not data:
                    raise ProtocolError("Empty ranking message")
                return json.loads(data)
            size = length & 0x7FFFFFFF
            if not 0 < size <= FRAME_BYTES:
                raise ProtocolError("Invalid ranking frame length")
            chunk = self.reader.read(size)
            if len(chunk) != size:
                raise EOFError("Incomplete ranking frame")
            self.bytes_received += len(chunk)
            if length & 0x80000000:
                decoder = zlib.decompressobj()
                chunk = decoder.decompress(chunk, FRAME_BYTES + 1)
                if not decoder.eof or len(chunk) > FRAME_BYTES:
                    raise ProtocolError("Invalid compressed ranking frame")
            if len(data) + len(chunk) > MAX_MESSAGE_BYTES:
                raise ProtocolError("Ranking message exceeds its safety limit")
            data.extend(chunk)


@cache
def code_fingerprint():
    # A policy version alone cannot detect two builds with different scorer implementations.
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py"), key=lambda file: file.relative_to(root).parts):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).digest())
    return digest.hexdigest()
