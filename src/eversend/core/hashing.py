"""Fast hashing helpers.

Two different integrity mechanisms are used, deliberately:

* **CRC32 per chunk** guards every byte in flight and every byte written to
  disk.  It runs in C (``zlib.crc32``), sustains several GB/s and adds 4
  bytes per chunk, so it is effectively free.
* **BLAKE2b-256 per file** provides end-to-end integrity.  BLAKE2b is
  substantially faster than SHA-256 in CPython's ``hashlib`` (it is
  implemented with a tuned C loop and does not pay SHA-256's scheduling
  overhead) while offering a comparable security margin.

``hashlib`` releases the GIL for buffers above a small threshold, so hashing
in a worker thread genuinely overlaps with socket I/O instead of serialising
behind it.
"""

from __future__ import annotations

import hashlib
import os
import zlib
from typing import BinaryIO, Iterable

#: Read size used by the whole-file hashing helpers.  4 MiB keeps the syscall
#: count low while staying inside the range where hashlib's GIL release is
#: worthwhile.
HASH_BLOCK = 4 * 1024 * 1024

#: Digest size of the whole-file hash, in bytes.
FILE_DIGEST_SIZE = 32


def crc32(data: bytes | bytearray | memoryview, value: int = 0) -> int:
    """CRC32 of ``data`` (continuing from ``value`` when given)."""
    return zlib.crc32(data, value)


def new_file_hasher() -> "hashlib._Hash":
    """A fresh whole-file hasher."""
    return hashlib.blake2b(digest_size=FILE_DIGEST_SIZE)


def file_digest(path: str) -> str:
    """BLAKE2b-256 hex digest of a file, read in large blocks."""
    hasher = new_file_hasher()
    with open(path, "rb", buffering=0) as fh:
        while True:
            block = fh.read(HASH_BLOCK)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


def file_digest_positioned(file, size: int | None = None) -> str:  # noqa: ANN001
    """Digest a :class:`~eversend.core.fileio.PositionedFile` from offset 0.

    Reads through the portable positioned interface rather than ``os.pread``
    directly, so this works on Windows too -- where the original version raised
    ``AttributeError`` and made every received file unverifiable.
    """
    hasher = new_file_hasher()
    offset = 0
    while True:
        block = file.read_at(offset, HASH_BLOCK)
        if not block:
            break
        hasher.update(block)
        offset += len(block)
        if size is not None and offset >= size:
            break
    return hasher.hexdigest()


def digest_of_stream(chunks: Iterable[bytes]) -> str:
    """Digest an iterable of byte blocks."""
    hasher = new_file_hasher()
    for block in chunks:
        hasher.update(block)
    return hasher.hexdigest()


class IncrementalFileHasher:
    """Hashes a file **as chunks arrive in order**.

    Parallel downloads deliver chunks out of order, so a naive end-to-end hash
    could only be computed by re-reading the finished file.  That extra read
    pass costs real time on multi-gigabyte transfers.

    This class removes the cost in the common case: as long as chunks arrive
    with contiguous coverage of the file's prefix, they are folded into the
    running hash immediately.  A gap simply parks the buffer until the missing
    chunk shows up.  When the transfer ends with a gap still open, the caller
    falls back to a single sequential read of the remainder (and then of the
    whole file, if the parked data was itself incomplete).

    The invariant makes it safe: :meth:`finish` only reports success when the
    hash covers the file from byte 0 continuously.
    """

    __slots__ = ("_hasher", "_next_offset", "_pending", "_pending_bytes", "_max_pending")

    def __init__(self, max_pending_bytes: int = 64 * 1024 * 1024) -> None:
        self._hasher = new_file_hasher()
        self._next_offset = 0
        self._pending: dict[int, bytes] = {}
        self._pending_bytes = 0
        #: Beyond this much parked out-of-order data we stop buffering; the
        #: caller then re-reads the file at the end instead of burning RAM.
        self._max_pending = max_pending_bytes

    @property
    def offset(self) -> int:
        """Bytes hashed continuously from the start of the file."""
        return self._next_offset

    @property
    def overflowing(self) -> bool:
        """Whether too much out-of-order data accumulated to keep buffering."""
        return self._pending_bytes > self._max_pending

    def update(self, offset: int, data: bytes | memoryview) -> None:
        """Feed the chunk that starts at ``offset``."""
        if offset == self._next_offset:
            self._hasher.update(data)
            self._next_offset += len(data)
            self._drain()
            return

        if offset < self._next_offset:
            return  # duplicate / retransmitted chunk, already folded in

        if self.overflowing:
            return  # parked data is already too large; give up on this fast path

        key = offset
        if key not in self._pending:
            copy = bytes(data)
            self._pending[key] = copy
            self._pending_bytes += len(copy)

    def _drain(self) -> None:
        """Fold in any parked chunks that are now contiguous."""
        while True:
            block = self._pending.pop(self._next_offset, None)
            if block is None:
                return
            self._pending_bytes -= len(block)
            self._hasher.update(block)
            self._next_offset += len(block)

    def can_finish_incrementally(self, total_size: int) -> bool:
        """Whether the running hash already covers the whole file."""
        return self._next_offset >= total_size

    def finish(self) -> str:
        """Hex digest of everything hashed so far."""
        return self._hasher.hexdigest()

    def reset(self) -> None:
        self._hasher = new_file_hasher()
        self._next_offset = 0
        self._pending.clear()
        self._pending_bytes = 0


def verify_file(
    path: str,
    expected_digest: str,
    *,
    progress: "callable | None" = None,
    cancel: "callable | None" = None,
) -> tuple[bool, str]:
    """Compare a file's digest against ``expected_digest``.

    Returns ``(ok, actual_digest)``.  ``progress`` is called with the number of
    bytes hashed so far; ``cancel`` is polled between blocks and aborts the
    verification when it returns true.
    """
    hasher = new_file_hasher()
    hashed = 0
    with open(path, "rb", buffering=0) as fh:
        while True:
            if cancel is not None and cancel():
                return False, ""
            block = fh.read(HASH_BLOCK)
            if not block:
                break
            hasher.update(block)
            hashed += len(block)
            if progress is not None:
                progress(hashed)
    actual = hasher.hexdigest()
    return actual == expected_digest.lower(), actual


def verify_chunk(data: bytes | memoryview, expected_crc: int) -> bool:
    """Check a received chunk body against the CRC the sender computed."""
    return zlib.crc32(data) == expected_crc


class DigestCache:
    """Persistent ``(path, size, mtime) -> digest`` cache.

    Hashing a large file is not free, and re-sending the same file is the
    common case (a backup, a media library, a project directory).  Remembering
    the digest by identity -- and identity means size *and* nanosecond mtime,
    so an edited file never matches its old entry -- makes repeat sends start
    instantly instead of spending a minute re-reading gigabytes.
    """

    def __init__(self, path: str, max_entries: int = 20000) -> None:
        self.path = path
        self.max_entries = max_entries
        self._entries: dict[str, str] = {}
        self._dirty = False
        self._lock = __import__("threading").RLock()
        self.load()

    @staticmethod
    def key(path: str, size: int, mtime_ns: int) -> str:
        return f"{os.path.abspath(path)}\x1f{size}\x1f{mtime_ns}"

    def get(self, path: str, size: int, mtime_ns: int) -> str | None:
        with self._lock:
            return self._entries.get(self.key(path, size, mtime_ns))

    def put(self, path: str, size: int, mtime_ns: int, digest: str) -> None:
        if not digest:
            return
        with self._lock:
            self._entries[self.key(path, size, mtime_ns)] = digest
            self._dirty = True
            if len(self._entries) > self.max_entries:
                # Drop the oldest half (dicts preserve insertion order).
                for old in list(self._entries)[: self.max_entries // 2]:
                    del self._entries[old]

    def load(self) -> None:
        import json

        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._entries = {str(k): str(v) for k, v in data.items()}
        except (OSError, ValueError):
            self._entries = {}

    def save(self) -> None:
        import json

        with self._lock:
            if not self._dirty:
                return
            payload = dict(self._entries)
            self._dirty = False
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def digest_for(self, path: str, size: int, mtime_ns: int) -> str:
        """Cached digest, computing and storing it on a miss."""
        cached = self.get(path, size, mtime_ns)
        if cached:
            return cached
        digest = file_digest(path)
        self.put(path, size, mtime_ns, digest)
        return digest


__all__ = [
    "FILE_DIGEST_SIZE",
    "HASH_BLOCK",
    "DigestCache",
    "IncrementalFileHasher",
    "crc32",
    "digest_of_stream",
    "file_digest",
    "file_digest_positioned",
    "new_file_hasher",
    "verify_chunk",
    "verify_file",
]
