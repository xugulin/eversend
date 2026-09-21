"""Qt bridge: turns engine events into Qt signals.

The engine runs on plain Python threads and knows nothing about Qt.  This
module is the only place where the two meet.

Emitting a Qt signal from a non-GUI thread is safe *provided* the receiving
object lives in the GUI thread: Qt then delivers the call through the event
loop (a queued connection) instead of running it on the emitting thread.  So
the bridge object is created on the GUI thread and every engine callback just
emits; the slots then run where they can safely touch widgets.
"""

from __future__ import annotations

import threading
from typing import Any

from PySide6.QtCore import QObject, Signal

from ..core.engine import Engine
from ..core.model import DeviceInfo, Peer, TransferItem, TransferStats


class EngineBridge(QObject):
    """Fan-out from :class:`~eversend.core.engine.EventBus` to Qt signals."""

    #: Any engine event, as ``{"kind": ..., ...}``.  Used for logging and for
    #: events that do not deserve a dedicated signal.
    event = Signal(dict)

    device_found = Signal(object)          # Peer
    device_updated = Signal(object)        # Peer
    scan_hit = Signal(str, int)            # address, port

    offer_received = Signal(dict)          # event payload
    transfer_started = Signal(str, int)    # transfer_id, streams
    transfer_finished = Signal(dict)       # event payload
    progress = Signal(str, int, int)       # transfer_id, done_bytes, total_bytes
    file_done = Signal(str, int, str, str) # transfer_id, index, name, path
    file_failed = Signal(str, int, str)    # transfer_id, index, error

    warning = Signal(str)
    engine_started = Signal(int)
    engine_stopped = Signal()

    def __init__(self, engine: Engine, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self._queue = engine.events.subscribe(self._on_event)
        self._last_progress: dict[str, float] = {}

    # -- engine thread -----------------------------------------------------

    def _on_event(self, event: dict[str, Any]) -> None:
        """Runs on whichever engine thread emitted the event."""
        kind = event.get("kind", "")
        try:
            if kind == "device_found":
                self.device_found.emit(event.get("peer"))
            elif kind == "device_updated":
                self.device_updated.emit(event.get("peer"))
            elif kind == "scan_hit":
                self.scan_hit.emit(str(event.get("address", "")), int(event.get("port", 0)))
            elif kind == "offer_received":
                self.offer_received.emit(event)
            elif kind == "transfer_started":
                self.transfer_started.emit(
                    str(event.get("transfer_id", "")), int(event.get("streams", 0))
                )
            elif kind == "transfer_finished":
                self.transfer_finished.emit(event)
            elif kind in ("warning", "discovery_warning"):
                self.warning.emit(str(event.get("message", "")))
            elif kind == "engine_started":
                self.engine_started.emit(int(event.get("port", 0)))
            elif kind == "engine_stopped":
                self.engine_stopped.emit()
            elif kind == "file_done":
                self.file_done.emit(
                    str(event.get("transfer_id", "")),
                    int(event.get("index", -1)),
                    str(event.get("name", "")),
                    str(event.get("path", "")),
                )
            elif kind == "file_failed":
                self.file_failed.emit(
                    str(event.get("transfer_id", "")),
                    int(event.get("index", -1)),
                    str(event.get("error", "")),
                )
        except Exception:
            # A UI signal must never be able to kill a transfer thread.
            pass

        self.event.emit(event)

    def close(self) -> None:
        try:
            self.engine.events.unsubscribe(self._queue)
        except Exception:
            pass


class SendWorker(threading.Thread):
    """Runs one ``engine.send`` off the GUI thread."""

    def __init__(self, engine: Engine, peer: Peer, paths: list[str], pin: str = "") -> None:
        super().__init__(name="qt-send", daemon=True)
        self.engine = engine
        self.peer = peer
        self.paths = paths
        self.pin = pin
        self.ok = False
        self.error = ""
        self.on_done = None  # callable(ok: bool, error: str)

    def run(self) -> None:
        try:
            self.ok = self.engine.send(self.peer, self.paths, pin=self.pin)
        except Exception as exc:
            self.error = str(exc)
            self.ok = False
        if self.on_done is not None:
            try:
                self.on_done(self.ok, self.error)
            except Exception:
                pass


def describe_peer(peer: Peer) -> str:
    """A one-line description of a device for tooltips and logs."""
    info: DeviceInfo = peer.info
    parts = [info.name, f"{peer.address}:{peer.port}"]
    if info.platform:
        parts.append(info.platform)
    if peer.trusted:
        parts.append("trusted")
    return " | ".join(parts)


def format_item(item: TransferItem) -> str:
    from ..core.model import human_bytes

    return f"{item.entry.name} ({human_bytes(item.done_bytes)}/{human_bytes(item.entry.size)})"


__all__ = ["EngineBridge", "SendWorker", "describe_peer", "format_item"]
