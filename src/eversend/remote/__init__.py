"""Remote (WAN) transport: design interface, not yet wired to the UI.

The LAN engine in :mod:`eversend.core` deliberately knows nothing about how a
TCP connection to a peer is obtained -- it is handed a :class:`Peer` with an
address and a port.  That is the seam a remote transport plugs into: once a
tunnel exists, it presents itself as a loopback endpoint and every existing
mechanism (chunking, resume, repair, multi-stream) works unchanged.

See ``docs/REMOTE_DESIGN.md`` for the full design, derived from studying how
RustDesk solves the same problem.
"""

from __future__ import annotations

__all__ = ["TransportKind", "Transport"]


class TransportKind:
    """How a peer connection was obtained."""

    LAN = "lan"
    DIRECT = "direct"
    PUNCHED = "punched"
    RELAYED = "relayed"


class Transport:
    """A way to reach a peer that is not on the local network.

    Implementations return a connected, already-tuned socket; the transfer
    layer treats it exactly like a LAN socket.
    """

    kind = TransportKind.LAN

    def connect(self, peer_id: str, timeout: float = 10.0):  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError
