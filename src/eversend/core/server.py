"""TCP listener that routes incoming connections to transfers.

Every connection starts the same way: a handshake, then one message that says
what the connection is for.

* ``OFFER``  -- this is the control connection of a new transfer.
* ``ATTACH`` -- this connection joins an existing transfer as a data stream.
* anything else -- rejected and closed.

Keeping the routing this small means a peer can open its control connection and
all of its data connections concurrently (they are indistinguishable until the
first message), which removes a full round trip from the start of every
transfer.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any, Callable

from . import crypto
from .connection import Connection, HandshakeError
from .constants import (
    LISTEN_BACKLOG,
    MAX_PENDING_HANDSHAKES,
    MSG_ATTACH,
    MSG_CHAT,
    MSG_OFFER,
    MSG_PING,
)
from .framing import ConnectionClosed, ProtocolError, close_quietly
from .model import DeviceInfo
from .sockutil import create_listener, tune_socket


class TransferServer:
    """Accepts peer connections and dispatches them."""

    def __init__(
        self,
        *,
        identity: crypto.Identity,
        device_info: Callable[[], DeviceInfo],
        on_offer: Callable[[Connection, dict[str, Any]], None],
        on_attach: Callable[[Connection, dict[str, Any]], bool],
        events: Any,
        encrypt: bool = True,
        bind_host: str = "0.0.0.0",
        on_chat: Callable[[Connection, dict[str, Any]], None] | None = None,
    ) -> None:
        self.identity = identity
        self.device_info = device_info
        self.on_offer = on_offer
        self.on_attach = on_attach
        #: Optional: older callers (tests) may not care about chat.
        self.on_chat = on_chat or (lambda conn, payload: None)
        self.events = events
        self.encrypt = encrypt
        self.bind_host = bind_host

        self.port = 0
        self._listeners: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._conn_threads: set[threading.Thread] = set()
        self._stop = threading.Event()
        self._pending = threading.Semaphore(MAX_PENDING_HANDSHAKES)
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return not self._stop.is_set() and bool(self._listeners)

    def start(self, port: int) -> int:
        """Bind and start accepting.  Returns the port actually bound."""
        self._listeners = create_listener(self.bind_host, port)
        self.port = self._listeners[0].getsockname()[1]
        self._stop.clear()
        for listener in self._listeners:
            thread = threading.Thread(
                target=self._accept_loop,
                args=(listener,),
                name=f"accept-{listener.fileno()}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        self.events.emit("server_started", port=self.port)
        return self.port

    def stop(self) -> None:
        self._stop.set()
        for listener in self._listeners:
            close_quietly(listener)
        self._listeners = []
        for thread in self._threads:
            thread.join(timeout=1.0)
        self._threads = []
        self.events.emit("server_stopped")

    # -- accept ------------------------------------------------------------

    def _accept_loop(self, listener: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                sock, addr = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(0.05)
                continue

            if not self._pending.acquire(blocking=False):
                # Too many half-open handshakes: shed load rather than
                # accumulate threads a slow peer could pin.
                close_quietly(sock)
                continue

            thread = threading.Thread(
                target=self._serve,
                args=(sock, addr),
                name=f"peer-{addr[0]}",
                daemon=True,
            )
            with self._lock:
                self._conn_threads.add(thread)
            thread.start()

    def _serve(self, sock: socket.socket, addr: tuple) -> None:
        conn: Connection | None = None
        try:
            tune_socket(sock, nodelay=True, keepalive=True)
            sock.settimeout(20.0)
            conn = Connection(sock, self.identity, stream_id=0, encrypt=self.encrypt)
            peer = conn.handshake_responder(self.device_info())

            sock.settimeout(30.0)
            frame = conn.recv()
            sock.settimeout(None)

            if frame.type == MSG_OFFER:
                offer = json.loads(frame.payload.decode("utf-8"))
                self.events.emit(
                    "peer_connected",
                    peer=peer.info,
                    address=addr[0],
                    purpose="offer",
                    authenticated=peer.authenticated,
                )
                self.on_offer(conn, offer)
                conn = None  # ownership passed to the receive session
                return

            if frame.type == MSG_ATTACH:
                info = json.loads(frame.payload.decode("utf-8"))
                if self.on_attach(conn, info):
                    conn = None  # ownership passed to the session
                    return
                try:
                    conn.send_json(0x41, {"message": "unknown transfer"})
                except Exception:
                    pass
                return

            if frame.type == MSG_CHAT:
                payload = json.loads(frame.payload.decode("utf-8"))
                self.events.emit(
                    "peer_connected",
                    peer=peer.info,
                    address=addr[0],
                    purpose="chat",
                    authenticated=peer.authenticated,
                )
                # The handler answers with an ack (or an error) and the
                # connection is closed either way: a chat line is one shot.
                self.on_chat(conn, payload)
                return

            if frame.type == MSG_PING:
                return

            self.events.emit("peer_rejected", address=addr[0], reason="unexpected first message")
        except HandshakeError as exc:
            self.events.emit("peer_rejected", address=addr[0], reason=str(exc))
        except (ConnectionClosed, ProtocolError, OSError, ValueError, UnicodeDecodeError):
            pass
        except Exception as exc:  # pragma: no cover - defensive
            self.events.emit("peer_rejected", address=addr[0], reason=f"internal error: {exc}")
        finally:
            self._pending.release()
            if conn is not None:
                conn.abort()
            with self._lock:
                self._conn_threads.discard(threading.current_thread())


def list_local_addresses(port: int) -> list[str]:
    """Every address this host can be reached at on ``port``."""
    from .sockutil import list_interfaces

    out = []
    for iface in list_interfaces(include_virtual=False):
        host = f"[{iface.address}]" if iface.is_ipv6 else iface.address
        out.append(f"{host}:{port}")
    return out


__all__ = ["TransferServer", "list_local_addresses"]
