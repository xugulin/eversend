"""Length-prefixed binary framing with zero-copy fast paths.

Every EverSend connection is a stream of frames::

    magic(4) type(1) flags(1) stream(2) length(4) payload(length)

The codec is written so the hot path never copies bulk data in Python:

* receiving a chunk reads the header into a small ``bytearray`` and then reads
  the payload **directly into a caller supplied writable buffer** with
  :meth:`socket.recv_into`;
* sending a chunk uses :meth:`socket.sendmsg` with two iovecs (header, body),
  which the kernel gathers without an intermediate concatenation.

Both of those matter: at 10 Gbps a naive implementation spends more time in
``memcpy`` than in the network stack.
"""

from __future__ import annotations

import errno
import socket
import struct
from typing import Final

from .constants import (
    FLAG_ENCRYPTED,
    FRAME_HEADER,
    FRAME_HEADER_SIZE,
    MAGIC,
    MAX_FRAME_PAYLOAD,
    PROTOCOL_VERSION,
)

_header_struct: Final = struct.Struct(FRAME_HEADER)
_header_pack = _header_struct.pack
_header_unpack = _header_struct.unpack


class ProtocolError(Exception):
    """Raised when a peer violates the framing contract."""


class ConnectionClosed(Exception):
    """Raised when the peer closed the connection cleanly at a frame boundary."""


class GatherUnsupported(Exception):
    """Raised when a socket cannot do scatter/gather at all.

    Distinct from a real I/O error: the caller may retry the *same* bytes with
    ``sendall``.  It is only ever raised when nothing has been written yet, so
    that retrying cannot duplicate a partially sent frame.
    """


class Frame:
    """A decoded frame header plus a handle to its payload.

    ``payload`` is either ``bytes`` (control messages, small) or a
    ``memoryview`` into a reusable buffer that is only valid until the next
    frame is read on the same connection.  Callers that need to keep the data
    must copy it; the chunk receive path deliberately does not.
    """

    __slots__ = ("type", "flags", "stream", "payload", "length")

    def __init__(
        self,
        type: int,
        flags: int,
        stream: int,
        payload: bytes | memoryview,
        length: int,
    ) -> None:
        self.type = type
        self.flags = flags
        self.stream = stream
        self.payload = payload
        self.length = length

    @property
    def encrypted(self) -> bool:
        return bool(self.flags & FLAG_ENCRYPTED)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<Frame type=0x{self.type:02x} flags={self.flags} "
            f"stream={self.stream} len={self.length}>"
        )


def build_header(type: int, flags: int = 0, stream: int = 0, length: int = 0) -> bytes:
    """Encode a frame header."""
    return _header_pack(MAGIC, type, flags, stream, length)


def build_frame(type: int, payload: bytes = b"", flags: int = 0, stream: int = 0) -> bytes:
    """Encode a complete frame for small payloads (control messages)."""
    return _header_pack(MAGIC, type, flags, stream, len(payload)) + payload


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly ``n`` bytes or raise.

    Raises :class:`ConnectionClosed` if the peer closed before any byte of the
    requested block arrived, and :class:`ProtocolError` for a partial block
    (the stream can no longer be resynchronised).
    """
    if n == 0:
        return b""
    chunks: list[bytes] = []
    remaining = n
    while remaining:
        try:
            part = sock.recv(remaining)
        except (ConnectionResetError, BrokenPipeError) as exc:
            raise ConnectionClosed(str(exc)) from exc
        if not part:
            if remaining == n:
                raise ConnectionClosed("peer closed connection")
            raise ProtocolError(f"truncated frame: wanted {n}, got {n - remaining}")
        chunks.append(part)
        remaining -= len(part)
    return chunks[0] if len(chunks) == 1 else b"".join(chunks)


def read_frame(sock: socket.socket, *, allow_eof: bool = False) -> Frame:
    """Read one frame whose payload is returned as ``bytes``.

    Use :func:`read_frame_into` for the bulk data path.
    """
    header = _read_header(sock, allow_eof=allow_eof)
    if header is None:
        raise ConnectionClosed("clean close at frame boundary")
    magic, mtype, flags, stream, length = header
    payload = recv_exact(sock, length) if length else b""
    return Frame(mtype, flags, stream, payload, length)


def read_frame_into(
    sock: socket.socket,
    buffer: bytearray | memoryview,
    *,
    want_type: int | None = None,
) -> tuple[Frame, memoryview]:
    """Read one frame directly into ``buffer``.

    Returns the frame and a memoryview of the received payload.  ``buffer`` is
    grown in place if it is too small (only possible when ``want_type`` is
    ``None``, i.e. the caller did not pre-size it for a known message type).

    ``buffer`` must be a ``bytearray`` so it can be resized.
    """
    header = _read_header(sock, allow_eof=False)
    assert header is not None
    magic, mtype, flags, stream, length = header
    if length == 0:
        return Frame(mtype, flags, stream, b"", 0), memoryview(b"")

    if len(buffer) < length:
        if want_type is not None:
            # The caller sized the buffer for a specific message; a larger one
            # is a protocol violation, not something to allocate for.
            raise ProtocolError(
                f"frame of type 0x{mtype:02x} is {length} bytes, "
                f"expected at most {len(buffer)}"
            )
        buffer.extend(bytes(length - len(buffer)))

    view = memoryview(buffer)[:length]
    _recv_into_exact(sock, view)
    return Frame(mtype, flags, stream, view, length), view


def _recv_into_exact(sock: socket.socket, view: memoryview) -> None:
    """Fill ``view`` completely from ``sock``."""
    total = len(view)
    got = 0
    while got < total:
        try:
            n = sock.recv_into(view[got:], total - got)
        except (ConnectionResetError, BrokenPipeError) as exc:
            raise ConnectionClosed(str(exc)) from exc
        if n == 0:
            if got == 0:
                raise ConnectionClosed("peer closed connection")
            raise ProtocolError(f"truncated payload: wanted {total}, got {got}")
        got += n


def _read_header(sock: socket.socket, *, allow_eof: bool) -> tuple | None:
    """Read and validate a 12-byte frame header."""
    buf = bytearray(FRAME_HEADER_SIZE)
    view = memoryview(buf)
    got = 0
    while got < FRAME_HEADER_SIZE:
        try:
            n = sock.recv_into(view[got:], FRAME_HEADER_SIZE - got)
        except (ConnectionResetError, BrokenPipeError) as exc:
            raise ConnectionClosed(str(exc)) from exc
        if n == 0:
            if got == 0 and allow_eof:
                return None
            if got == 0:
                raise ConnectionClosed("peer closed connection")
            raise ProtocolError("truncated frame header")
        got += n

    magic, mtype, flags, stream, length = _header_unpack(bytes(buf))
    if magic != MAGIC:
        raise ProtocolError(f"bad magic {magic!r} (not a EverSend peer?)")
    if length > MAX_FRAME_PAYLOAD:
        raise ProtocolError(f"frame payload {length} exceeds limit {MAX_FRAME_PAYLOAD}")
    return magic, mtype, flags, stream, length


def send_frame(sock: socket.socket, type: int, payload: bytes = b"", flags: int = 0, stream: int = 0) -> None:
    """Send one complete frame (header + payload) in a single write."""
    header = _header_pack(MAGIC, type, flags, stream, len(payload))
    if payload:
        sock.sendall(header + payload)
    else:
        sock.sendall(header)


def sendmsg_all(sock: socket.socket, iovecs: list[bytes | memoryview]) -> int:
    """Write every byte of ``iovecs``, looping until it is all out.

    :meth:`socket.sendmsg` behaves like :meth:`socket.send`, **not** like
    :meth:`socket.sendall`: it returns how many bytes it managed to write and
    stops there.  Handing it a whole multi-megabyte frame therefore silently
    truncates that frame whenever the kernel send buffer cannot take all of it
    at once -- and the peer then waits forever for bytes that were never
    written.  There is no error, no short write reported anywhere, just a
    transfer that stalls.

    It hides well: with a large send buffer (the engine asks for 8 MiB) a
    chunk usually fits in one call and everything looks perfect.  It only
    appears when the buffer is smaller or the reader is slower -- a busy CI
    runner, a small `net.core.wmem_max`, a slow peer.

    Returns the total number of bytes written.
    """
    views = [v if isinstance(v, memoryview) else memoryview(v) for v in iovecs]
    index = 0
    offset = 0
    written = 0
    count = len(views)

    while index < count:
        batch = [views[index][offset:], *views[index + 1 :]]
        try:
            sent = sock.sendmsg(batch)
        except (AttributeError, OSError) as exc:
            # Windows CPython has no sendmsg at all (AttributeError), and some
            # Windows builds have one that rejects the socket outright.  Both
            # mean "this socket cannot gather", which the caller answers with
            # sendall -- but only while nothing has been written, otherwise the
            # retry would send the frame twice.
            if written == 0:
                raise GatherUnsupported(str(exc)) from exc
            raise
        if sent <= 0:
            raise OSError("sendmsg wrote nothing")
        written += sent

        # Walk the iovecs forward by ``sent`` bytes.
        while index < count:
            available = len(views[index]) - offset
            if sent < available:
                offset += sent
                break
            sent -= available
            index += 1
            offset = 0

    return written


def send_frame_gather(
    sock: socket.socket,
    type: int,
    parts: list[bytes | memoryview],
    flags: int = 0,
    stream: int = 0,
) -> None:
    """Send a frame whose payload is the concatenation of ``parts``.

    Uses scatter/gather so the kernel collects the iovecs and the payload is
    never copied in user space, but writes all of it (see :func:`sendmsg_all`).
    Falls back to ``sendall`` where ``sendmsg`` is unavailable.
    """
    total = 0
    for part in parts:
        total += len(part)
    header = _header_pack(MAGIC, type, flags, stream, total)
    iovecs: list[bytes | memoryview] = [header, *parts]
    try:
        sendmsg_all(sock, iovecs)
    except (AttributeError, OSError) as exc:
        # AttributeError: no sendmsg on this platform.  OSError with EINVAL:
        # the socket type does not support scatter/gather.
        if isinstance(exc, OSError) and getattr(exc, "errno", None) not in (
            None, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP,
        ):
            raise
        flat = b"".join(bytes(p) for p in iovecs)
        sock.sendall(flat)


def send_encrypted_frame(
    sock: socket.socket,
    type: int,
    ciphertext: bytes,
    flags: int = 0,
    stream: int = 0,
) -> None:
    """Send a frame whose payload was already sealed by the AEAD layer."""
    send_frame(sock, type, ciphertext, flags | FLAG_ENCRYPTED, stream)


def close_quietly(sock: socket.socket | None) -> None:
    """Shut a socket down without letting the teardown error escape."""
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


__all__ = [
    "ConnectionClosed",
    "Frame",
    "ProtocolError",
    "build_frame",
    "build_header",
    "close_quietly",
    "read_frame",
    "read_frame_into",
    "recv_exact",
    "send_encrypted_frame",
    "send_frame",
    "send_frame_gather",
    "sendmsg_all",
    "PROTOCOL_VERSION",
]
