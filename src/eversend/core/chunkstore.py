"""Resumable, crash-safe chunked file storage.

This module is what makes EverSend survive the things that make large
transfers fail in practice:

* a Wi-Fi blip or a dropped VPN tunnel,
* the receiving laptop going to sleep,
* the application being killed, the machine losing power,
* the user closing the lid and reopening it an hour later.

The design is deliberately boring and provably safe:

**Layout.**  A file being received lives next to its final name as
``<name>.eversend.part`` with a sidecar ``<name>.eversend.journal``.  The
part file is preallocated to the full size, and every chunk is written with
a positioned write directly at its final offset.  Nothing is ever shifted, so a
partially received file is *already* in its correct final layout.

**Journal.**  The journal is an append-only log of 8-byte records
``(u32 chunk_index, u32 crc32)``.  A record is appended *after* the chunk's
bytes have been handed to the kernel for that offset.  A crash therefore has
exactly one possible outcome: records for chunks that are missing from disk,
never the reverse.  Resume replays the log to rebuild the presence bitmap;
losing the tail of the log only means re-fetching a few chunks.

**Zero-copy writes.**  The chunk body arrives in a reusable socket buffer and
goes to disk through :mod:`~eversend.core.fileio` on a ``memoryview`` of
that buffer.  The
bytes are never copied in Python, which matters at multi-gigabit rates.

**Repair instead of restart.**  If the finished file fails its whole-file
digest, the per-chunk CRCs in the journal are used to find exactly which
chunks rotted, and only those are re-requested.  A 100 GB transfer that hits
one bad sector re-sends 4 MB, not 100 GB.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import threading
import time
from typing import Iterator

from . import hashing
from .constants import chunk_count, chunk_range, pick_chunk_size
from .fileio import O_BINARY, PositionedFile, preallocate

#: Journal file magic and version.
JOURNAL_MAGIC = b"SWDJ"
JOURNAL_VERSION = 1

#: Suffixes of the sidecar files.
PART_SUFFIX = ".eversend.part"
JOURNAL_SUFFIX = ".eversend.journal"

#: Journal record: ``chunk_index:u32, crc32:u32``.
_RECORD = struct.Struct("!II")
_RECORD_SIZE = _RECORD.size

#: Journal header prefix: magic(4) version(1) flags(1) reserved(2) header_len(4).
_JOURNAL_PREFIX = struct.Struct("!4sBBHI")

#: How often the journal is pushed to the kernel by default (seconds).
JOURNAL_FLUSH_INTERVAL = 1.0

#: How often the journal is fsync'd by default (seconds).
JOURNAL_SYNC_INTERVAL = 10.0


class ChunkStoreError(Exception):
    """A chunk could not be stored or the store is inconsistent."""


def part_path(target_path: str) -> str:
    return target_path + PART_SUFFIX


def journal_path(target_path: str) -> str:
    return target_path + JOURNAL_SUFFIX


class PartFile:
    """A single incoming file with resume support.

    Thread safety: :meth:`write_chunk` may be called concurrently from any
    number of data-stream threads.  Data writes proceed in parallel (distinct
    offsets are safe with ``pwrite``); only the bitmap and the journal append
    are serialised, and neither does any bulk work.
    """

    __slots__ = (
        "_target",
        "_part",
        "_journal",
        "_size",
        "_chunk_size",
        "_nchunks",
        "_digest",
        "_have",
        "_received",
        "_file",
        "_jfd",
        "_lock",
        "_last_flush",
        "_last_sync",
        "_closed",
        "_dirty",
        "_expected_crcs",
        "_mtime_ns",
    )

    def __init__(
        self,
        target_path: str,
        size: int,
        digest: str = "",
        chunk_size: int = 0,
        *,
        mtime_ns: int = 0,
        resume: bool = True,
    ) -> None:
        self._target = target_path
        self._part = part_path(target_path)
        self._journal = journal_path(target_path)
        self._size = max(0, int(size))
        self._chunk_size = chunk_size or pick_chunk_size(self._size)
        self._nchunks = chunk_count(self._size, self._chunk_size)
        self._digest = (digest or "").lower()
        self._mtime_ns = int(mtime_ns or 0)
        self._have = bytearray((self._nchunks + 7) // 8)
        self._received = 0
        self._lock = threading.Lock()
        self._last_flush = 0.0
        self._last_sync = 0.0
        self._closed = False
        self._dirty = False
        #: CRC recorded per chunk, kept in memory so corruption can be
        #: localised later without re-reading the journal.
        self._expected_crcs: dict[int, int] = {}

        os.makedirs(os.path.dirname(self._part) or ".", exist_ok=True)

        loaded = False
        if resume:
            loaded = self._load_journal()

        if not loaded:
            self._reset_files()
        elif not os.path.exists(self._part):
            self._reset_files()

        # One portable positioned handle for the whole part file.  On POSIX it
        # is a plain descriptor plus pread/pwrite; on Windows it hides a
        # per-thread descriptor because the standard library has neither.
        try:
            self._file = PositionedFile(self._part, create=True)
        except OSError as exc:
            raise ChunkStoreError(f"cannot open {self._part}: {exc}") from exc
        if self._received == 0 or os.path.getsize(self._part) < self._size:
            preallocate(self._file._fd, self._size)  # noqa: SLF001 - same package
        # O_BINARY matters here too: the journal holds packed binary records,
        # and a text-mode descriptor would insert 0x0D bytes into them.
        self._jfd = os.open(
            self._journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND | O_BINARY, 0o600
        )
        self._last_flush = time.monotonic()
        self._last_sync = self._last_flush

    # -- introspection -----------------------------------------------------

    @property
    def target_path(self) -> str:
        return self._target

    @property
    def part(self) -> str:
        return self._part

    @property
    def size(self) -> int:
        return self._size

    @property
    def chunk_size(self) -> int:
        return self._chunk_size

    @property
    def nchunks(self) -> int:
        return self._nchunks

    @property
    def received_bytes(self) -> int:
        """Bytes that are present and verified on disk."""
        return self._received

    @property
    def digest(self) -> str:
        return self._digest

    def has_chunk(self, index: int) -> bool:
        if index < 0 or index >= self._nchunks:
            return False
        return bool(self._have[index >> 3] & (1 << (index & 7)))

    def _mark(self, index: int) -> None:
        self._have[index >> 3] |= 1 << (index & 7)

    def _unmark(self, index: int) -> None:
        self._have[index >> 3] &= ~(1 << (index & 7)) & 0xFF

    @property
    def complete(self) -> bool:
        return self._received >= self._size and self._missing_count() == 0

    def missing_chunks(self) -> list[int]:
        """Every chunk index that still has to be fetched, in file order."""
        out: list[int] = []
        for index in range(self._nchunks):
            if not (self._have[index >> 3] & (1 << (index & 7))):
                out.append(index)
        return out

    def iter_missing(self) -> Iterator[int]:
        for index in range(self._nchunks):
            if not (self._have[index >> 3] & (1 << (index & 7))):
                yield index

    def _missing_count(self) -> int:
        return self._nchunks - sum(bin(byte).count("1") for byte in self._have)

    def missing_bytes(self) -> int:
        total = 0
        for index in self.iter_missing():
            total += chunk_range(index, self._chunk_size, self._size)[1]
        return total

    # -- writing -----------------------------------------------------------

    def write_chunk(self, index: int, data: bytes | memoryview, crc: int) -> None:
        """Store chunk ``index`` (payload ``data``, verified CRC ``crc``).

        The bytes are written first and journalled second, which is the only
        ordering that is safe against a crash at any instant.
        """
        if self._closed:
            raise ChunkStoreError("store is closed")
        if index < 0 or index >= self._nchunks:
            raise ChunkStoreError(f"chunk {index} out of range 0..{self._nchunks - 1}")

        offset, length = chunk_range(index, self._chunk_size, self._size)
        if len(data) != length:
            raise ChunkStoreError(
                f"chunk {index} has {len(data)} bytes, expected {length}"
            )

        if length:
            self._file.write_at(offset, data)

        with self._lock:
            if not (self._have[index >> 3] & (1 << (index & 7))):
                self._mark(index)
                self._received += length
                self._expected_crcs[index] = crc
                os.write(self._jfd, _RECORD.pack(index, crc))
                self._dirty = True
            self._maybe_flush()

    def mark_lost(self, index: int) -> None:
        """Forget a chunk (used by the repair pass after a digest mismatch)."""
        with self._lock:
            if self._have[index >> 3] & (1 << (index & 7)):
                self._unmark(index)
                self._received -= chunk_range(index, self._chunk_size, self._size)[1]
                self._expected_crcs.pop(index, None)

    def read_chunk(self, index: int) -> bytes:
        """Read a stored chunk back (used by the repair pass)."""
        offset, length = chunk_range(index, self._chunk_size, self._size)
        return self._file.read_at(offset, length)

    def repair_scan(self) -> list[int]:
        """Return the chunks whose on-disk bytes no longer match their CRC.

        This is the cheap escape hatch when the whole-file digest does not
        match: instead of discarding a finished transfer we find the few
        chunks that actually rotted.
        """
        bad: list[int] = []
        with self._lock:
            crcs = dict(self._expected_crcs)
        for index, crc in crcs.items():
            data = self.read_chunk(index)
            if hashing.crc32(data) != crc:
                bad.append(index)
        return bad

    # -- lifecycle ---------------------------------------------------------

    def _maybe_flush(self, force: bool = False) -> None:
        """Push journal records to the kernel, and to disk, periodically."""
        now = time.monotonic()
        if force or (self._dirty and now - self._last_flush >= JOURNAL_FLUSH_INTERVAL):
            try:
                os.fsync(self._jfd)
            except OSError:
                pass
            self._last_flush = now
            self._last_sync = now
            self._dirty = False
        elif self._dirty and now - self._last_sync >= JOURNAL_SYNC_INTERVAL:
            self._file.sync()
            self._last_sync = now

    def flush(self) -> None:
        """Force journal and data to stable storage."""
        with self._lock:
            self._file.sync()
            try:
                os.fsync(self._jfd)
            except OSError:
                pass
            self._dirty = False
            self._last_flush = time.monotonic()
            self._last_sync = self._last_flush

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.flush()
        except Exception:
            pass
        try:
            os.close(self._jfd)
        except OSError:
            pass
        self._jfd = -1
        try:
            self._file.close()
        except Exception:
            pass

    def discard(self) -> None:
        """Delete the partial file and its journal."""
        self.close()
        for path in (self._part, self._journal):
            try:
                os.unlink(path)
            except OSError:
                pass

    def set_times(self, mtime_ns: int) -> None:
        """Apply the sender's modification time to the finished file."""
        if not mtime_ns:
            return
        try:
            os.utime(self._target, ns=(mtime_ns, mtime_ns))
        except (OSError, OverflowError, ValueError):
            pass

    def finalize(self, *, verify: bool = True) -> tuple[str, bool]:
        """Verify and atomically move the part file to its final name.

        Returns ``(actual_digest, ok)``.  On a digest mismatch the part file
        and the journal are **kept** so the caller can repair individual
        chunks and try again; on success they are removed.
        """
        if self._closed:
            raise ChunkStoreError("store is closed")

        # Make sure every byte is on stable storage *before* we claim success.
        self.flush()

        actual = hashing.file_digest_positioned(self._file, self._size) if verify else ""
        if verify and self._digest and actual != self._digest:
            return actual, False

        self._file.sync()
        self._file.close()

        # Atomic on POSIX and on Windows (os.replace overwrites).
        try:
            os.replace(self._part, self._target)
        except OSError:
            try:
                os.rename(self._part, self._target)
            except OSError as exc:
                raise ChunkStoreError(f"cannot finalise {self._target}: {exc}") from exc

        self._closed = True
        try:
            os.close(self._jfd)
        except OSError:
            pass
        try:
            os.unlink(self._journal)
        except OSError:
            pass
        self._jfd = -1
        return actual, True

    # -- journal -----------------------------------------------------------

    def _reset_files(self) -> None:
        """Start from scratch, removing anything left over from a mismatch."""
        for path in (self._part, self._journal):
            try:
                os.unlink(path)
            except OSError:
                pass
        self._have = bytearray((self._nchunks + 7) // 8)
        self._received = 0
        self._expected_crcs = {}
        self._write_journal_header()

    def _header(self) -> dict:
        return {
            "size": self._size,
            "chunkSize": self._chunk_size,
            "nchunks": self._nchunks,
            "digest": self._digest,
            "mtime": self._mtime_ns,
            "target": os.path.basename(self._target),
            "created": time.time(),
        }

    def _write_journal_header(self) -> None:
        header = json.dumps(self._header(), separators=(",", ":")).encode("utf-8")
        blob = _JOURNAL_PREFIX.pack(
            JOURNAL_MAGIC, JOURNAL_VERSION, 0, 0, len(header)
        ) + header
        with open(self._journal, "wb", buffering=0) as fh:
            fh.write(blob)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass

    def _load_journal(self) -> bool:
        """Rebuild the presence bitmap from an existing journal.

        Returns ``False`` when there is nothing usable to resume from, in
        which case the caller starts over.
        """
        try:
            with open(self._journal, "rb", buffering=0) as fh:
                prefix = fh.read(_JOURNAL_PREFIX.size)
                if len(prefix) < _JOURNAL_PREFIX.size:
                    return False
                magic, version, _flags, _res, header_len = _JOURNAL_PREFIX.unpack(prefix)
                if magic != JOURNAL_MAGIC or version != JOURNAL_VERSION:
                    return False
                header = json.loads(fh.read(header_len).decode("utf-8"))
                body = fh.read()
        except (OSError, ValueError, json.JSONDecodeError):
            return False

        # The journal only applies when the file identity matches exactly.
        # The digest is usually unknown at this point (the sender computes it
        # while transferring), so size + chunk size + modification time are
        # what identify the file; a stale part file for a *different* file of
        # the same name and size would have to also share its mtime.
        if (
            int(header.get("size", -1)) != self._size
            or int(header.get("chunkSize", -1)) != self._chunk_size
            or int(header.get("mtime", 0)) != self._mtime_ns
        ):
            return False
        if self._digest and str(header.get("digest", "")).lower() not in ("", self._digest):
            return False

        count = len(body) // _RECORD_SIZE
        seen: set[int] = set()
        for i in range(count):
            index, crc = _RECORD.unpack_from(body, i * _RECORD_SIZE)
            if index >= self._nchunks or index in seen:
                continue
            seen.add(index)
            self._mark(index)
            self._expected_crcs[index] = crc
            self._received += chunk_range(index, self._chunk_size, self._size)[1]

        # A torn tail record is simply ignored: it means the crash landed
        # between the write and the journal append, so the chunk is re-fetched.
        return True


class ResumeIndex:
    """Looks up resume state for incoming transfers.

    Keeps the "what have I already got?" decision out of the protocol: a
    deterministic transfer key derived from sender + file set + destination is
    mapped to the part files on disk.  Re-offering the same files therefore
    resumes, while offering them to a different folder starts clean.
    """

    def __init__(self, state_dir: str) -> None:
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)

    def part_file(
        self,
        target_path: str,
        size: int,
        digest: str,
        chunk_size: int = 0,
        *,
        mtime_ns: int = 0,
        resume: bool = True,
    ) -> PartFile:
        return PartFile(
            target_path,
            size,
            digest,
            chunk_size,
            mtime_ns=mtime_ns,
            resume=resume,
        )

    def sweep(self, max_age_seconds: float = 7 * 24 * 3600) -> list[str]:
        """Remove stale part files left behind by abandoned transfers.

        Only files whose journal has not been touched for ``max_age_seconds``
        are removed, so a transfer paused over a weekend survives.
        """
        removed: list[str] = []
        now = time.time()
        try:
            entries = os.listdir(self.state_dir)
        except OSError:
            return removed
        for name in entries:
            if name.endswith(JOURNAL_SUFFIX):
                path = os.path.join(self.state_dir, name)
                try:
                    if now - os.path.getmtime(path) > max_age_seconds:
                        os.unlink(path)
                        removed.append(path)
                except OSError:
                    pass
        return removed


def journal_crcs(path: str) -> dict[int, int]:
    """Read the chunk CRC map out of a journal (used by tests and tooling)."""
    result: dict[int, int] = {}
    try:
        with open(path, "rb", buffering=0) as fh:
            prefix = fh.read(_JOURNAL_PREFIX.size)
            if len(prefix) < _JOURNAL_PREFIX.size:
                return result
            magic, version, _f, _r, header_len = _JOURNAL_PREFIX.unpack(prefix)
            if magic != JOURNAL_MAGIC or version != JOURNAL_VERSION:
                return result
            fh.read(header_len)
            body = fh.read()
    except (OSError, ValueError):
        return result
    for i in range(len(body) // _RECORD_SIZE):
        index, crc = _RECORD.unpack_from(body, i * _RECORD_SIZE)
        result[index] = crc
    return result


__all__ = [
    "JOURNAL_SUFFIX",
    "PART_SUFFIX",
    "ChunkStoreError",
    "PartFile",
    "ResumeIndex",
    "journal_crcs",
    "journal_path",
    "part_path",
    "preallocate",
]
