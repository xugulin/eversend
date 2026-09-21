"""Positioned file I/O that also works on Windows.

``os.pread`` and ``os.pwrite`` are POSIX-only.  The whole transfer core is
built on them: the receiver writes every chunk straight to its final offset
(so a partial file is already in its correct layout and needs no shifting),
and the sender reads chunks by offset so several streams can pull from one
file at once.

On Windows those functions simply do not exist -- not "are slower", they are
absent from the module.  A Windows build using them raises ``AttributeError``
on the very first chunk, which means it cannot send or receive anything at all.

The obvious emulation, ``os.lseek`` followed by ``os.read``/``os.write``, is
not thread safe: the file position lives on the descriptor, so two streams
sharing it would trample each other and silently corrupt data.  This module
therefore uses two different strategies:

* **POSIX** -- one descriptor, real ``pread``/``pwrite``.  Fully parallel, no
  locking, no copying.
* **Windows** -- one descriptor *per thread*, positioned with ``lseek``.  The
  file position is per descriptor, so threads stay independent and writes can
  still proceed in parallel; the only cost is an extra handle per stream.

Both present the same tiny interface, so the transfer layer never has to know
which one it is running on.
"""

from __future__ import annotations

import os
import threading
from typing import BinaryIO

#: Whether the platform has real positioned I/O.  Exposed for diagnostics.
HAVE_POSITIONED_IO = hasattr(os, "pwrite") and hasattr(os, "pread")

#: Windows opens files in *text* mode unless told otherwise, which silently
#: rewrites every 0x0A byte into 0x0D 0x0A.  A chunk of random data written
#: that way lands on disk a few kilobytes longer than it should be -- and the
#: corruption is invisible to the application's own checksum, because reading
#: it back through the same text-mode descriptor translates it straight back.
#: Every binary descriptor must therefore be opened with this flag.
O_BINARY = getattr(os, "O_BINARY", 0)


class PositionedFile:
    """A file that can be read and written at explicit offsets from any thread.

    Not a general-purpose file object: it exists for the two access patterns
    the transfer core actually has -- "write this chunk at this offset" and
    "read this chunk at this offset" -- and it is optimised for exactly those.
    """

    __slots__ = ("path", "_fd", "_local", "_handles", "_lock", "_closed", "_mode")

    def __init__(self, path: str, *, create: bool = False, truncate_to: int | None = None) -> None:
        self.path = path
        self._mode = "r+b"
        flags = os.O_RDWR | O_BINARY | (os.O_CREAT if create else 0)
        self._fd = os.open(path, flags, 0o600)
        if truncate_to is not None:
            os.ftruncate(self._fd, truncate_to)
        # Windows path: a descriptor per thread, tracked so close() can reach
        # them all.
        self._local = threading.local()
        self._handles: list[int] = []
        self._lock = threading.Lock()
        self._closed = False

    # -- descriptor management --------------------------------------------

    def _thread_fd(self) -> int:
        """The calling thread's descriptor.

        On POSIX this is the single shared one (``pread``/``pwrite`` carry
        their own offset, so sharing is safe and cheapest).  On Windows each
        thread gets its own, because ``lseek`` mutates the descriptor.
        """
        if HAVE_POSITIONED_IO:
            return self._fd
        fd = getattr(self._local, "fd", None)
        if fd is None:
            if self._closed:
                raise ValueError("file is closed")
            fd = os.open(self.path, os.O_RDWR | O_BINARY)
            self._local.fd = fd
            with self._lock:
                self._handles.append(fd)
        return fd

    # -- I/O ---------------------------------------------------------------

    def write_at(self, offset: int, data: bytes | bytearray | memoryview) -> int:
        """Write ``data`` at ``offset``.  Returns the number of bytes written."""
        length = len(data)
        if length == 0:
            return 0
        fd = self._thread_fd()
        if HAVE_POSITIONED_IO:
            written = 0
            view = memoryview(data)
            while written < length:
                written += os.pwrite(fd, view[written:], offset + written)
            return written

        os.lseek(fd, offset, os.SEEK_SET)
        written = 0
        view = memoryview(data)
        while written < length:
            written += os.write(fd, view[written:])
        return written

    def read_at(self, offset: int, size: int) -> bytes:
        """Read up to ``size`` bytes at ``offset``."""
        if size <= 0:
            return b""
        fd = self._thread_fd()
        if HAVE_POSITIONED_IO:
            return os.pread(fd, size, offset)
        os.lseek(fd, offset, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            block = os.read(fd, remaining)
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        return chunks[0] if len(chunks) == 1 else b"".join(chunks)

    def read_into(self, offset: int, view: memoryview) -> int:
        """Fill ``view`` from ``offset``; returns bytes read.

        Provided so the hot path can avoid an allocation where the platform
        allows it.
        """
        size = len(view)
        if size == 0:
            return 0
        fd = self._thread_fd()
        if hasattr(os, "preadv"):
            got = 0
            while got < size:
                n = os.preadv(fd, [view[got:]], offset + got)
                if n <= 0:
                    break
                got += n
            return got
        data = self.read_at(offset, size)
        view[: len(data)] = data
        return len(data)

    def sync(self) -> None:
        """Flush this file's data to stable storage."""
        try:
            if hasattr(os, "fdatasync"):
                os.fdatasync(self._fd)
            else:
                os.fsync(self._fd)
        except OSError:
            pass

    def truncate(self, size: int) -> None:
        try:
            os.ftruncate(self._fd, size)
        except OSError:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            handles = list(self._handles)
            self._handles.clear()
        # The primary descriptor is not in _handles on Windows.
        for fd in {self._fd, *handles}:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._fd = -1
        self._local = threading.local()

    def __enter__(self) -> "PositionedFile":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def preallocate(fd: int, size: int) -> None:
    """Reserve ``size`` bytes on an already-open descriptor.

    ``posix_fallocate`` does not exist on Windows, and even on POSIX plenty of
    filesystems refuse it (network shares, FUSE).  ``ftruncate`` is the
    portable fallback and is enough: it makes the file the right size so
    writes at any offset land in place, at the cost of the allocation being
    sparse on some filesystems.
    """
    if size <= 0:
        return
    fallocate = getattr(os, "posix_fallocate", None)
    if fallocate is not None:
        try:
            fallocate(fd, 0, size)
            return
        except (OSError, AttributeError):
            pass
    os.ftruncate(fd, size)


def open_path(path: str) -> BinaryIO:
    """A plain buffered handle, for sequential whole-file work."""
    return open(path, "rb", buffering=0)


__all__ = ["HAVE_POSITIONED_IO", "O_BINARY", "PositionedFile", "open_path", "preallocate"]
