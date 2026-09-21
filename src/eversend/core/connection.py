"""Connection layer: framing + encryption + a mutually authenticated handshake.

A :class:`Connection` wraps one TCP socket and turns it into a stream of typed
frames.  It is the only place that knows about both the wire format and the
crypto, which keeps the transfer logic above it readable.

Handshake (2 round trips, mutual authentication)
------------------------------------------------

1. ``I -> R  HELLO``       device info, X25519 public key, ``nonce_i``
2. ``R -> I  HELLO_ACK``   device info, X25519 public key, ``nonce_r``,
                           ``sig_r`` over the full transcript
3. ``I -> R  AUTH``        ``sig_i`` over the same transcript
4. ``R -> I  AUTH_OK``     accept/reject

Both sides derive the session key after step 2, so steps 3 and 4 are already
encrypted.  Step 2 proves the responder's identity to the initiator; step 3
proves the initiator's identity to the responder.  A party that relays the
handshake without holding a key cannot produce either signature, and the
6-digit SAS both users see will differ, so the relay is visible.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import crypto
from .constants import (
    CHUNK_HEADER,
    CHUNK_HEADER_SIZE,
    FLAG_ENCRYPTED,
    FRAME_HEADER_SIZE,
    HANDSHAKE_TIMEOUT,
    MAX_CONTROL_PAYLOAD,
    MSG_AUTH,
    MSG_AUTH_OK,
    MSG_BYE,
    MSG_CHUNK,
    MSG_ERROR,
    MSG_HELLO,
    MSG_HELLO_ACK,
    MSG_NAMES,
    MSG_PING,
    MSG_PONG,
    PROTOCOL_VERSION,
    STREAM_IDLE_TIMEOUT,
)
from .framing import (
    ConnectionClosed,
    Frame,
    ProtocolError,
    build_header,
    close_quietly,
    read_frame,
    send_frame,
)
from .model import DeviceInfo
from .sockutil import tune_socket

_chunk_struct = struct.Struct(CHUNK_HEADER)


class HandshakeError(Exception):
    """The peer failed authentication or spoke a different protocol."""


@dataclass(slots=True)
class PeerSession:
    """Everything learned about the peer during the handshake."""

    info: DeviceInfo
    session: crypto.SessionCrypto
    #: True when the peer proved possession of its Ed25519 key.
    authenticated: bool = False
    #: True when the payload stream is encrypted.
    encrypted: bool = False
    #: Set by the application once the user trusts this device.
    address: str = ""

    @property
    def sas(self) -> str:
        return self.session.sas if self.session else ""


class Connection:
    """One framed, optionally encrypted TCP connection to a peer."""

    __slots__ = (
        "sock",
        "_identity",
        "_session",
        "_peer",
        "_recv_buffer",
        "_chunk_buffer",
        "_send_lock",
        "_closed",
        "_stream_id",
        "_role",
        "created",
        "last_activity",
        "_bytes_sent",
        "_bytes_received",
        "_encrypt_enabled",
    )

    def __init__(
        self,
        sock: socket.socket,
        identity: crypto.Identity,
        *,
        stream_id: int = 0,
        encrypt: bool = True,
    ) -> None:
        self.sock = sock
        self._identity = identity
        self._session: crypto.SessionCrypto | None = None
        self._peer: PeerSession | None = None
        self._recv_buffer = bytearray()
        self._chunk_buffer = bytearray()
        self._send_lock = threading.Lock()
        self._closed = False
        self._stream_id = stream_id
        self._role = "data" if stream_id else "control"
        self._encrypt_enabled = encrypt and crypto.CRYPTO_AVAILABLE
        self.created = time.monotonic()
        self.last_activity = self.created
        self._bytes_sent = 0
        self._bytes_received = 0

        tune_socket(sock, keepalive=True, nodelay=True)

    # -- properties --------------------------------------------------------

    @property
    def peer(self) -> PeerSession | None:
        return self._peer

    @property
    def encrypted(self) -> bool:
        return bool(self._session and self._session.algorithm != "none")

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def bytes_sent(self) -> int:
        return self._bytes_sent

    @property
    def bytes_received(self) -> int:
        return self._bytes_received

    @property
    def stream_id(self) -> int:
        return self._stream_id

    def peer_address(self) -> str:
        try:
            return self.sock.getpeername()[0]
        except OSError:
            return ""

    # -- handshake ---------------------------------------------------------

    def handshake_initiator(self, info: DeviceInfo, *, timeout: float = HANDSHAKE_TIMEOUT) -> PeerSession:
        """Perform the initiator side of the handshake."""
        self.sock.settimeout(timeout)
        nonce_i = crypto.new_nonce()
        hello = {
            "proto": PROTOCOL_VERSION,
            "device": info.to_dict(),
            "x25519": self._identity.x25519_pub_b64,
            "ed25519": self._identity.ed25519_pub_b64,
            "nonce": crypto.b64e(nonce_i),
            "encrypt": self._encrypt_enabled,
        }
        send_frame(self.sock, MSG_HELLO, json.dumps(hello).encode("utf-8"))

        frame = read_frame(self.sock)
        if frame.type == MSG_ERROR:
            raise HandshakeError(_error_text(frame.payload))
        if frame.type != MSG_HELLO_ACK:
            raise HandshakeError(f"expected HELLO_ACK, got {MSG_NAMES.get(frame.type, frame.type)}")

        ack = json.loads(frame.payload.decode("utf-8"))
        if int(ack.get("proto", -1)) != PROTOCOL_VERSION:
            raise HandshakeError(
                f"protocol mismatch: peer speaks {ack.get('proto')}, we speak {PROTOCOL_VERSION}"
            )

        peer_info = DeviceInfo.from_dict(ack.get("device") or {})
        nonce_r = crypto.b64d(ack["nonce"])
        peer_x25519 = str(ack.get("x25519", ""))

        encrypt = bool(ack.get("encrypt", False)) and self._encrypt_enabled

        # Verify the responder's signature over the transcript.
        authenticated = False
        if crypto.CRYPTO_AVAILABLE and peer_info.ed25519_pub and ack.get("sig"):
            transcript = crypto.handshake_transcript(
                info.device_id,
                peer_info.device_id,
                self._identity.x25519_public,
                crypto.b64d(peer_x25519),
                nonce_i,
                nonce_r,
            )
            authenticated = crypto.verify_signature(
                crypto.b64d(peer_info.ed25519_pub), crypto.b64d(ack["sig"]), transcript
            )

        self._install_session(
            peer_info,
            peer_x25519,
            nonce_i,
            nonce_r,
            initiator=True,
            encrypt=encrypt,
            authenticated=authenticated,
        )

        # Prove our own identity.
        sig_i = b""
        if crypto.CRYPTO_AVAILABLE:
            transcript = crypto.handshake_transcript(
                info.device_id,
                peer_info.device_id,
                self._identity.x25519_public,
                crypto.b64d(peer_x25519),
                nonce_i,
                nonce_r,
            )
            sig_i = self._identity.sign(transcript)
        self.send_json(MSG_AUTH, {"sig": crypto.b64e(sig_i)})

        # AUTH_OK is already inside the encrypted session.
        frame = self.recv()
        if frame.type == MSG_ERROR:
            raise HandshakeError(_error_text(frame.payload))
        if frame.type != MSG_AUTH_OK:
            raise HandshakeError(f"expected AUTH_OK, got {MSG_NAMES.get(frame.type, frame.type)}")
        result = json.loads(frame.payload.decode("utf-8"))
        if not result.get("ok", False):
            raise HandshakeError(str(result.get("reason", "rejected")))

        self.sock.settimeout(None)
        return self._peer  # type: ignore[return-value]

    def handshake_responder(self, info: DeviceInfo, *, timeout: float = HANDSHAKE_TIMEOUT) -> PeerSession:
        """Perform the responder side of the handshake."""
        self.sock.settimeout(timeout)
        frame = read_frame(self.sock)
        if frame.type != MSG_HELLO:
            raise HandshakeError(f"expected HELLO, got {MSG_NAMES.get(frame.type, frame.type)}")

        hello = json.loads(frame.payload.decode("utf-8"))
        if int(hello.get("proto", -1)) != PROTOCOL_VERSION:
            raise HandshakeError(
                f"protocol mismatch: peer speaks {hello.get('proto')}, we speak {PROTOCOL_VERSION}"
            )

        peer_info = DeviceInfo.from_dict(hello.get("device") or {})
        nonce_i = crypto.b64d(hello["nonce"])
        peer_x25519 = str(hello.get("x25519", ""))
        nonce_r = crypto.new_nonce()

        encrypt = bool(hello.get("encrypt", False)) and self._encrypt_enabled

        self._install_session(
            peer_info,
            peer_x25519,
            nonce_i,
            nonce_r,
            initiator=False,
            encrypt=encrypt,
            authenticated=False,
        )

        sig_r = b""
        if crypto.CRYPTO_AVAILABLE and peer_x25519:
            transcript = crypto.handshake_transcript(
                peer_info.device_id,
                info.device_id,
                crypto.b64d(peer_x25519),
                self._identity.x25519_public,
                nonce_i,
                nonce_r,
            )
            sig_r = self._identity.sign(transcript)

        ack = {
            "proto": PROTOCOL_VERSION,
            "device": info.to_dict(),
            "x25519": self._identity.x25519_pub_b64,
            "ed25519": self._identity.ed25519_pub_b64,
            "nonce": crypto.b64e(nonce_r),
            "encrypt": self.encrypted,
            "sig": crypto.b64e(sig_r),
        }
        send_frame(self.sock, MSG_HELLO_ACK, json.dumps(ack).encode("utf-8"))

        # AUTH arrives encrypted: the session was installed before HELLO_ACK.
        frame = self.recv()
        if frame.type != MSG_AUTH:
            raise HandshakeError(f"expected AUTH, got {MSG_NAMES.get(frame.type, frame.type)}")
        auth = json.loads(frame.payload.decode("utf-8"))

        authenticated = False
        if crypto.CRYPTO_AVAILABLE and peer_info.ed25519_pub and auth.get("sig"):
            transcript = crypto.handshake_transcript(
                peer_info.device_id,
                info.device_id,
                crypto.b64d(peer_x25519),
                self._identity.x25519_public,
                nonce_i,
                nonce_r,
            )
            authenticated = crypto.verify_signature(
                crypto.b64d(peer_info.ed25519_pub), crypto.b64d(auth["sig"]), transcript
            )
        self._peer.authenticated = authenticated  # type: ignore[union-attr]

        self.send_json(MSG_AUTH_OK, {"ok": True, "authenticated": authenticated})
        self.sock.settimeout(None)
        return self._peer  # type: ignore[return-value]

    def _install_session(
        self,
        peer_info: DeviceInfo,
        peer_x25519: str,
        nonce_i: bytes,
        nonce_r: bytes,
        *,
        initiator: bool,
        encrypt: bool,
        authenticated: bool,
    ) -> None:
        if encrypt and peer_x25519:
            session = crypto.establish_session(
                self._identity,
                peer_info.device_id,
                peer_x25519,
                nonce_i,
                nonce_r,
                initiator=initiator,
            )
        else:
            session = crypto.SessionCrypto(
                crypto.NullCipher(), crypto.NullCipher(), b"", peer_info.device_id, "none"
            )
        self._session = session
        self._peer = PeerSession(
            info=peer_info,
            session=session,
            authenticated=authenticated,
            encrypted=session.algorithm != "none",
            address=self.peer_address(),
        )

    # -- control frames ----------------------------------------------------

    def send_json(self, type: int, payload: dict[str, Any]) -> None:
        self.send(type, json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def send(self, type: int, payload: bytes | memoryview = b"") -> None:
        """Send one frame, encrypting it when a session is established."""
        if self._closed:
            raise ConnectionClosed("connection is closed")
        raw = bytes(payload) if not isinstance(payload, bytes) else payload
        flags = 0
        if self._session is not None and self._session.algorithm != "none":
            raw = self._session.send.seal(raw, _aad(type))
            flags = FLAG_ENCRYPTED
        header = build_header(type, flags, self._stream_id, len(raw))
        with self._send_lock:
            try:
                if raw:
                    self.sock.sendall(header + raw)
                else:
                    self.sock.sendall(header)
                self._bytes_sent += len(header) + len(raw)
            except (OSError, BrokenPipeError) as exc:
                self._closed = True
                raise ConnectionClosed(str(exc)) from exc
        self.last_activity = time.monotonic()

    def send_chunk(
        self,
        seq: int,
        index: int,
        offset: int,
        data: bytes | memoryview,
        crc: int,
    ) -> None:
        """Send one file chunk.

        The chunk sub-header stays in the clear and is authenticated as
        associated data, so a tamperer cannot move a chunk to a different
        offset.  The body is gathered from its buffer by the kernel when no
        encryption is in play, i.e. with no Python-level copy at all.
        """
        if self._closed:
            raise ConnectionClosed("connection is closed")

        chunk_header = _chunk_struct.pack(seq, index, offset, len(data), crc)
        flags = 0

        if self._session is not None and self._session.algorithm != "none":
            sealed = self._session.send.seal(data, _aad(MSG_CHUNK) + chunk_header)
            flags = FLAG_ENCRYPTED
            body: bytes | memoryview = sealed
        else:
            body = data if isinstance(data, memoryview) else memoryview(data)

        frame_header = build_header(
            MSG_CHUNK, flags, self._stream_id, CHUNK_HEADER_SIZE + len(body)
        )
        with self._send_lock:
            try:
                if hasattr(self.sock, "sendmsg"):
                    try:
                        self.sock.sendmsg([frame_header, chunk_header, body])
                    except (OSError, TypeError):
                        self.sock.sendall(frame_header + chunk_header + bytes(body))
                else:
                    self.sock.sendall(frame_header + chunk_header + bytes(body))
                self._bytes_sent += len(frame_header) + CHUNK_HEADER_SIZE + len(body)
            except (OSError, BrokenPipeError) as exc:
                self._closed = True
                raise ConnectionClosed(str(exc)) from exc
        self.last_activity = time.monotonic()

    def recv(self) -> Frame:
        """Read the next frame, decrypting the payload if needed."""
        frame = read_frame(self.sock)
        self.last_activity = time.monotonic()
        self._bytes_received += frame.length
        if frame.encrypted:
            if self._session is None:
                raise ProtocolError("encrypted frame before the session was established")
            try:
                plain = self._session.recv.open(frame.payload, _aad(frame.type))
            except Exception as exc:
                raise ProtocolError(f"frame authentication failed: {exc}") from exc
            frame.payload = plain
            frame.length = len(plain)
            frame.flags &= ~FLAG_ENCRYPTED
        # Answer keepalives here so no caller has to; loop rather than recurse
        # so a peer that only sends PINGs cannot grow the stack.
        while frame.type in (MSG_PING, MSG_PONG):
            if frame.type == MSG_PING:
                try:
                    self.send(MSG_PONG, frame.payload)
                except ConnectionClosed:
                    pass
            frame = read_frame(self.sock)
            self.last_activity = time.monotonic()
            self._bytes_received += frame.length
            if frame.encrypted:
                if self._session is None:
                    raise ProtocolError("encrypted frame before the session was established")
                try:
                    plain = self._session.recv.open(frame.payload, _aad(frame.type))
                except Exception as exc:
                    raise ProtocolError(f"frame authentication failed: {exc}") from exc
                frame.payload = plain
                frame.length = len(plain)
                frame.flags &= ~FLAG_ENCRYPTED
        return frame

    def recv_json(self) -> tuple[int, dict[str, Any]]:
        frame = self.recv()
        if frame.length > MAX_CONTROL_PAYLOAD:
            raise ProtocolError("control frame too large")
        try:
            return frame.type, json.loads(frame.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProtocolError(f"malformed control payload: {exc}") from exc

    # -- bulk receive path -------------------------------------------------

    def recv_data(self) -> "ChunkFrame | Frame":
        """Receive one frame from a data stream.

        Returns a :class:`ChunkFrame` for bulk data or a plain :class:`Frame`
        for anything else (a control message interleaved on the stream).

        For an unencrypted session, ``ChunkFrame.data`` is a view straight
        into a reusable internal buffer -- the bytes are never copied.  That
        view is only valid until the next call, which is exactly the lifetime
        the caller needs to hand it to a positioned write.
        """
        from .framing import _recv_into_exact, _read_header  # private fast path

        header = _read_header(self.sock, allow_eof=False)
        assert header is not None
        _magic, mtype, flags, stream, length = header
        encrypted = bool(flags & FLAG_ENCRYPTED)

        if mtype != MSG_CHUNK:
            payload = bytearray(length)
            if length:
                _recv_into_exact(self.sock, memoryview(payload))
            self.last_activity = time.monotonic()
            self._bytes_received += length + FRAME_HEADER_SIZE
            raw: bytes = bytes(payload)
            if encrypted:
                if self._session is None:
                    raise ProtocolError("encrypted frame before session")
                raw = self._session.recv.open(raw, _aad(mtype, stream))
            return Frame(mtype, flags & ~FLAG_ENCRYPTED, stream, raw, len(raw))

        chunk_header = bytearray(CHUNK_HEADER_SIZE)
        _recv_into_exact(self.sock, memoryview(chunk_header))
        seq, index, offset, data_len, crc = _chunk_struct.unpack(bytes(chunk_header))

        expected = CHUNK_HEADER_SIZE + data_len + (crypto.TAG_SIZE if encrypted else 0)
        if length != expected:
            raise ProtocolError(
                f"chunk frame length {length} disagrees with its header "
                f"(data {data_len}, encrypted {encrypted}, expected {expected})"
            )

        body_len = data_len + (crypto.TAG_SIZE if encrypted else 0)
        buf = self._chunk_buffer
        if len(buf) < body_len:
            buf.extend(bytes(body_len - len(buf)))
        view = memoryview(buf)[:body_len]
        _recv_into_exact(self.sock, view)

        self.last_activity = time.monotonic()
        self._bytes_received += length + FRAME_HEADER_SIZE

        if encrypted:
            if self._session is None:
                raise ProtocolError("encrypted chunk before session")
            try:
                plain = self._session.recv.open(
                    view, _aad(MSG_CHUNK, stream) + bytes(chunk_header)
                )
            except Exception as exc:
                raise ProtocolError(f"chunk authentication failed: {exc}") from exc
            return ChunkFrame(seq, index, offset, memoryview(plain), crc)

        return ChunkFrame(seq, index, offset, view[:data_len], crc)

    # -- lifecycle ---------------------------------------------------------

    def ping(self) -> None:
        try:
            self.send(MSG_PING, struct.pack("!d", time.monotonic()))
        except ConnectionClosed:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            send_frame(self.sock, MSG_BYE)
        except Exception:
            pass
        close_quietly(self.sock)

    def abort(self) -> None:
        self._closed = True
        close_quietly(self.sock)


@dataclass(slots=True)
class ChunkFrame:
    """One received file chunk.

    ``data`` is a view into the connection's reusable buffer for unencrypted
    sessions: consume it before the next :meth:`Connection.recv_data` call.
    """

    seq: int
    index: int
    offset: int
    data: memoryview
    crc: int

    def __len__(self) -> int:
        return len(self.data)


def _aad(frame_type: int, stream_id: int = 0) -> bytes:
    """Associated data binding a ciphertext to its frame type.

    Without it an attacker could take a sealed CHUNK frame and replay it as a
    control frame, and the AEAD would still verify.

    The stream id is deliberately *not* included.  Streams are already
    separated by being separate TCP connections, whereas the id is assigned by
    the initiator and the responder only learns it later -- binding it would
    make every frame undecryptable until then, for no security gain.
    """
    return bytes([frame_type & 0xFF])


def _error_text(payload: bytes) -> str:
    try:
        return str(json.loads(payload.decode("utf-8")).get("message", "error"))
    except Exception:
        return "error"


def connect_to(
    host: str,
    port: int,
    *,
    timeout: float = 5.0,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Open a tuned TCP connection, trying every address the host resolves to.

    ``source_address`` pins the local address, which matters when a peer
    announced itself over one interface and the OS would otherwise pick
    another (the classic multi-homed "it connects but nothing arrives" bug).
    """
    last_error: Exception | None = None
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP, type=socket.SOCK_STREAM)
    # IPv4 first: it is what discovery advertises, and it avoids a needless
    # AAAA lookup penalty on networks with broken IPv6.
    infos.sort(key=lambda i: 0 if i[0] == socket.AF_INET else 1)

    for family, socktype, proto, _canon, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            tune_socket(sock, nodelay=True, keepalive=True)
            sock.settimeout(timeout)
            if source_address:
                try:
                    sock.bind(source_address)
                except OSError:
                    pass
            sock.connect(sockaddr)
            sock.settimeout(None)
            return sock
        except OSError as exc:
            last_error = exc
            try:
                sock.close()
            except OSError:
                pass
    raise ConnectionError(f"cannot connect to {host}:{port}: {last_error}")


__all__ = [
    "Connection",
    "HandshakeError",
    "PeerSession",
    "connect_to",
]
