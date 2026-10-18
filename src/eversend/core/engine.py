"""The EverSend engine: the single entry point every UI talks to.

The engine owns the identity, the listeners, discovery and the live sessions,
and exposes a small, UI-agnostic API:

* :meth:`Engine.start` / :meth:`Engine.stop`
* :meth:`Engine.devices` -- who is around
* :meth:`Engine.send` -- offer files to a peer
* :meth:`Engine.cancel` -- abort a session
* :meth:`Engine.subscribe` -- receive events

Both the Qt desktop UI and the browser (mobile) UI drive exactly this API, so
the two front ends cannot drift apart in behaviour.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from . import crypto
from .connection import Connection, HandshakeError, connect_to
from .constants import (
    DEFAULT_DISCOVERY_PORT,
    DEFAULT_STREAMS,
    DEFAULT_TCP_PORT,
    DEFAULT_WEB_PORT,
    MSG_ATTACH,
    MSG_CANCEL,
    MSG_CHAT,
    MSG_CHAT_ACK,
    MSG_ERROR,
    MSG_OFFER,
    MSG_OFFER_ACK,
    MSG_OFFER_REJECT,
)
from .chat import ChatStore, direct_conversation_id, media_relpath
from .discovery import SCAN_CONCURRENCY, SCAN_RATE_PER_SECOND, DiscoveryService
from .framing import ConnectionClosed, ProtocolError
from .hashing import DigestCache
from .model import (
    DeviceInfo,
    FileEntry,
    Peer,
    TransferItem,
    TransferStats,
    default_device_name,
    new_transfer_id,
    platform_tag,
    safe_join,
)
from .server import TransferServer
from .transfer import (
    EventSink,
    OfferDecision,
    ReceiveSession,
    SendOutcome,
    SendSession,
    TransferCancelled,
    TransferFailed,
    classify_failure,
    failure_info,
    failure_text,
    is_disk_full,
)


#: Largest attachment the *chat* channel will carry.
#:
#: The chat channel is for photos, voice notes and short clips.  A 762 MB file
#: sent through it is a lie waiting to happen: it has no progress, no resume
#: and no place in the transfer table.  Above this the message is refused with
#: a reason instead of being marked sent (docs/REAL_DEVICE_FINDINGS.md §2.2).
CHAT_ATTACHMENT_MAX_BYTES = 32 * 1024 * 1024


@dataclass
class EngineConfig:
    """Everything the engine needs to run."""

    data_dir: str
    receive_dir: str
    name: str = field(default_factory=default_device_name)
    tcp_port: int = DEFAULT_TCP_PORT
    discovery_port: int = DEFAULT_DISCOVERY_PORT
    web_port: int = DEFAULT_WEB_PORT
    streams: int = DEFAULT_STREAMS
    encrypt: bool = True
    resume: bool = True
    #: PIN required from senders ('' disables it).
    pin: str = ""
    #: Accept offers from trusted devices without asking.
    auto_accept_trusted: bool = True
    #: Ask before accepting anything (ignored for trusted devices when
    #: ``auto_accept_trusted`` is set).  The headless engine always asks the
    #: listener, which decides.
    auto_accept_all: bool = False
    enable_multicast: bool = True
    enable_broadcast: bool = True
    enable_mdns: bool = True
    #: Active subnet scanning.  On by default: it is the only channel that
    #: works on a network that filters every kind of broadcast (charter §5.3).
    enable_scan: bool = True
    scan_concurrency: int = SCAN_CONCURRENCY
    scan_rate: float = SCAN_RATE_PER_SECOND
    #: Largest attachment the chat channel will carry (see
    #: :data:`CHAT_ATTACHMENT_MAX_BYTES`).
    chat_attachment_max_bytes: int = CHAT_ATTACHMENT_MAX_BYTES
    #: Whether this device can *receive* a direct push.  Advertised in the
    #: announcement (``rx``) and in the handshake (``caps.accept``) so a sender
    #: knows before dialling whether it may push or must publish-and-wait.
    accept_files: bool = True
    enable_web: bool = True


class EventBus(EventSink):
    """Fan-out event bus.

    Subscribers may be callables or queues.  A slow subscriber never blocks the
    engine: events go to a bounded queue and the oldest are dropped, because a
    lagging UI is not a reason to stall a 10 Gbps transfer.
    """

    def __init__(self, maxsize: int = 2000) -> None:
        self._subscribers: list[Callable[[dict], None]] = []
        self._queues: list[queue.Queue] = []
        self._lock = threading.RLock()
        self.maxsize = maxsize
        self.recent: list[dict] = []

    def subscribe(self, callback: Callable[[dict], None] | None = None) -> queue.Queue:
        """Subscribe.  Returns a queue that receives every event."""
        q: queue.Queue = queue.Queue(maxsize=self.maxsize)
        with self._lock:
            self._queues.append(q)
            if callback is not None:
                self._subscribers.append(callback)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._queues:
                self._queues.remove(q)

    def emit(self, kind: str, **payload: Any) -> None:
        # ``kind`` is positional; guard against a caller passing it again as a
        # keyword, which would otherwise raise inside a transfer thread and
        # take the whole session down over a logging concern.
        payload.pop("kind", None)
        event = {"kind": kind, "ts": time.time(), **payload}
        with self._lock:
            self.recent.append(event)
            if len(self.recent) > 200:
                del self.recent[: len(self.recent) - 200]
            subscribers = list(self._subscribers)
            queues = list(self._queues)

        for queue_ in queues:
            try:
                queue_.put_nowait(event)
            except queue.Full:
                try:
                    queue_.get_nowait()
                    queue_.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                pass


@dataclass
class ActiveTransfer:
    """Bookkeeping for one live transfer."""

    transfer_id: str
    direction: str  # "send" | "receive"
    peer: DeviceInfo
    stats: TransferStats
    items: dict[int, TransferItem]
    session: Any = None
    error: str = ""
    started: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# Guaranteed chat delivery (charter §5.5 / C14)
# ---------------------------------------------------------------------------

#: How many times one message is handed to a peer before we stop.
CHAT_MAX_ATTEMPTS = 5
#: Exponential backoff between attempts, in seconds: the *first* retry waits
#: 1 s (1/2/4/8, charter §5.7), then it is capped at 8 s.
CHAT_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 8.0)
#: Bounded so a peer that is switched off for a week cannot grow the queue
#: without limit; oldest failures are dropped first.
CHAT_OUTBOX_LIMIT = 200



@dataclass
class OutboxEntry:
    """One message waiting for its peer to be reachable.

    The state strings are the contract with the UI (desktop-chat reads them
    out of the chat store): ``queued`` while it is being retried, ``sent``
    once the peer acknowledged it, ``failed`` when the attempts ran out.
    """

    message_id: str
    conv_id: str
    peer_id: str
    peer_name: str
    payload: dict[str, Any]
    message: dict[str, Any] = field(default_factory=dict)
    conversation: dict[str, Any] = field(default_factory=dict)
    #: Attachment to hand over *before* the message, if any.  Retrying a
    #: message whose file never arrived would deliver a broken reference, and
    #: the transfer engine resumes, so re-sending costs only the missing part.
    media_path: str = ""
    rel_dir: str = ""
    attempts: int = 0
    created: float = field(default_factory=time.monotonic)
    next_attempt: float = 0.0
    state: str = "queued"
    reason: str = ""
    last_error: str = ""

    @property
    def due(self) -> float:
        return self.next_attempt

    def to_dict(self) -> dict[str, Any]:
        return {
            "messageId": self.message_id,
            "conv": self.conv_id,
            "peer": self.peer_id,
            "peerName": self.peer_name,
            "state": self.state,
            "attempts": self.attempts,
            "maxAttempts": CHAT_MAX_ATTEMPTS,
            "nextRetryIn": max(0.0, round(self.next_attempt - time.monotonic(), 1)),
            "reason": self.reason,
            "lastError": self.last_error,
            "hasMedia": bool(self.media_path),
            "createdAt": self.created,
        }


class ChatOutbox:
    """Thread-safe retry queue for chat messages that were not acknowledged."""

    def __init__(self, limit: int = CHAT_OUTBOX_LIMIT) -> None:
        self._entries: dict[str, OutboxEntry] = {}
        self._order: deque[str] = deque()
        self._lock = threading.RLock()
        self.limit = limit

    def put(self, entry: OutboxEntry) -> None:
        with self._lock:
            if entry.message_id in self._entries:
                existing = self._entries[entry.message_id]
                existing.next_attempt = entry.next_attempt
                existing.state = entry.state
                return
            self._entries[entry.message_id] = entry
            self._order.append(entry.message_id)
            while len(self._order) > self.limit:
                oldest = self._order.popleft()
                self._entries.pop(oldest, None)

    def get(self, message_id: str) -> OutboxEntry | None:
        with self._lock:
            return self._entries.get(str(message_id))

    def all(self) -> list[OutboxEntry]:
        with self._lock:
            return [self._entries[key] for key in self._order if key in self._entries]

    def pending(self) -> list[OutboxEntry]:
        return [e for e in self.all() if e.state == "queued"]

    def due(self, now: float | None = None) -> list[OutboxEntry]:
        moment = time.monotonic() if now is None else now
        return [e for e in self.all() if e.state == "queued" and e.next_attempt <= moment]

    def wake(self, peer_id: str = "") -> int:
        """Make entries due right now (a peer just appeared / was reconnected)."""
        count = 0
        with self._lock:
            for entry in self._entries.values():
                if entry.state != "queued":
                    continue
                if peer_id and entry.peer_id != peer_id:
                    continue
                entry.next_attempt = 0.0
                entry.reason = "对方已上线，正在补投"
                count += 1
        return count

    def remove(self, message_id: str) -> OutboxEntry | None:
        with self._lock:
            entry = self._entries.pop(str(message_id), None)
            if entry is not None:
                try:
                    self._order.remove(entry.message_id)
                except ValueError:
                    pass
            return entry

    def __len__(self) -> int:
        return len(self._entries)


class Engine:
    """The transfer engine."""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.events = EventBus()

        os.makedirs(config.data_dir, exist_ok=True)
        os.makedirs(config.receive_dir, exist_ok=True)

        self.identity = crypto.load_or_create_identity(
            os.path.join(config.data_dir, "identity.json"), config.name
        )
        # Remembers (path, size, mtime) -> digest so re-sending a file that has
        # not changed skips the hashing pass entirely.
        self.digest_cache = DigestCache(os.path.join(config.data_dir, "digests.json"))
        #: Conversations and messages.  See core/chat.py for why this is a
        #: database rather than a file, and how attachments are referenced.
        self.chat = ChatStore(config.data_dir)

        self.info = DeviceInfo(
            device_id=self.identity.device_id,
            name=config.name,
            kind=_kind(),
            platform=platform_tag(),
            version=_version(),
            tcp_port=config.tcp_port,
            web_port=config.web_port,
            x25519_pub=self.identity.x25519_pub_b64,
            ed25519_pub=self.identity.ed25519_pub_b64,
            capabilities={
                "resume": True,
                "web": config.enable_web,
                "streams": config.streams,
                # The capability the phone flow turns on: "I have a receiving
                # service, you may push files at me".  A browser client cannot
                # say this (it has no listening socket at all), which is why the
                # sender has to ask *before* offering (see can_push_files).
                "accept": bool(config.accept_files),
            },
        )

        self.server = TransferServer(
            identity=self.identity,
            device_info=lambda: self.info,
            on_offer=self._on_offer,
            on_attach=self._on_attach,
            on_chat=self._on_chat,
            on_bye=self._on_peer_bye,
            events=self.events,
            encrypt=config.encrypt,
        )
        self.discovery = DiscoveryService(
            device_info=lambda: self.info,
            events=self.events,
            port=config.discovery_port,
            enable_multicast=config.enable_multicast,
            enable_broadcast=config.enable_broadcast,
            enable_mdns=config.enable_mdns,
            enable_scan=config.enable_scan,
            scan_concurrency=config.scan_concurrency,
            scan_rate=config.scan_rate,
            # The scan can only prove that *something* listens on a port.  This
            # callback turns a reachable address into a device with an identity,
            # which is what the device list and every send path key on.
            probe_peer=self._probe_for_scan,
        )

        self._sessions: dict[str, ReceiveSession] = {}
        self._sessions_lock = threading.RLock()
        self._active: dict[str, ActiveTransfer] = {}
        self._active_lock = threading.RLock()
        self._pending_offers: dict[str, tuple[Connection, dict, ReceiveSession]] = {}
        #: Transfer ids that finished recently.  A sender whose stream
        #: supervisor reconnects in the same instant the transfer completes
        #: will attach to a session that no longer exists; answering with a
        #: protocol error there would turn a successful transfer into a
        #: reported failure, so those attaches are accepted and closed quietly.
        self._recently_finished: dict[str, float] = {}
        #: Last :class:`SendOutcome` per peer and per transfer id, so the
        #: historical ``bool`` return of ``send()`` still has a reason to read
        #: (charter E3: no silent failure).
        self._last_send_outcomes: dict[str, SendOutcome] = {}
        self._send_outcomes: dict[str, SendOutcome] = {}
        self._trusted: set[str] = set()
        self._load_trusted()
        self._started = False

        # -- guaranteed chat delivery -----------------------------------
        #: Messages the peer has not acknowledged.  Every entry is retried with
        #: 1/2/4/8 s backoff, up to CHAT_MAX_ATTEMPTS, and flushed immediately
        #: when the peer shows up again.
        self.outbox = ChatOutbox()
        self._outbox_wake = threading.Event()
        self._outbox_stop = threading.Event()
        self._outbox_thread: threading.Thread | None = None
        #: Recent user-visible problems, newest last (the diagnostic card and
        #: the "no silent failure" rule both need them; charter E2/E3).
        self._problems: deque[dict[str, Any]] = deque(maxlen=20)
        self._provider_callbacks = True
        # A discovery event is also the trigger for 补投: the moment a peer
        # reappears, its queued messages go out instead of waiting for the
        # backoff to elapse.
        self._events_subscription = self.events.subscribe(self._on_engine_event)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            port = self.server.start(self.config.tcp_port)
        except OSError as exc:
            if self.config.tcp_port == 0:
                raise
            # Fall back to an ephemeral port rather than refusing to run.
            self.events.emit(
                "warning",
                message=f"port {self.config.tcp_port} is busy ({exc}); using a random port",
            )
            port = self.server.start(0)
        self.info.tcp_port = port
        self.discovery.start()
        self.discovery.announce()
        # Retry worker for unacknowledged chat messages.  Started before the
        # startup re-enqueue below so recovered messages go out immediately.
        self._outbox_stop.clear()
        self._outbox_thread = threading.Thread(
            target=self._outbox_loop, name="chat-outbox", daemon=True
        )
        self._outbox_thread.start()
        threading.Thread(
            target=self._recover_queued_messages, name="chat-recover", daemon=True
        ).start()
        # Clean up part files left behind by transfers abandoned long ago.
        try:
            from .chunkstore import ResumeIndex

            index = ResumeIndex(self.config.data_dir)
            # The `.eversend.part`/`.journal` pair is written *next to the
            # received file*, i.e. in the receive folder, while resume state is
            # keyed off the data folder -- so the sweep has to be told about the
            # receive folder or it scans a directory that can never contain one.
            index.register_dir(self.config.receive_dir)
            index.sweep()
        except Exception:
            pass
        self.events.emit("engine_started", port=port, device_id=self.identity.device_id)

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        for session in list(self._sessions.values()):
            try:
                session.cancel("engine shutting down")
            except Exception:
                pass
        # Stop retrying first: a message that is still queued stays queued in
        # the store and is picked up again on the next start (see
        # :meth:`_recover_queued_messages`), rather than being counted as a
        # failed delivery because the application closed.
        self._outbox_stop.set()
        self._outbox_wake.set()
        thread = self._outbox_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._outbox_thread = None
        self.discovery.stop()
        self.server.stop()
        self.digest_cache.save()
        # Close the chat database too: on Windows an open handle keeps the file
        # undeletable, and callers (the packaged --cli selftest, tests, a user
        # removing the folder) do exactly that.
        try:
            self.chat.close()
        except Exception:
            pass
        self.events.emit("engine_stopped")

    @property
    def port(self) -> int:
        return self.server.port

    @property
    def receive_dir(self) -> str:
        """Where received files are written.

        A plain view onto the configuration, but a front end should not have to
        reach through ``engine.config`` for something it needs on every page.
        """
        return self.config.receive_dir

    def self_peer(self) -> Peer:
        """This device, addressable like any other.

        Discovery deliberately filters out a device's own announcements, so the
        engine never appears in :meth:`devices`.  That is right for the LAN
        list but wrong for a phone that is looking at this machine's page and
        wants to send a photo *to it* -- the obvious target would be missing.
        """
        return Peer(
            info=self.info,
            address="127.0.0.1",
            port=self.server.port,
            source="self",
            trusted=True,
        )

    def pending_offers(self) -> list[dict[str, Any]]:
        """Offers waiting for the user to accept or decline.

        Reconstructed from live state rather than from replayed events, so a
        front end that started late (or dropped an event) still sees them.
        """
        with self._sessions_lock:
            pending = list(self._pending_offers.items())
        out: list[dict[str, Any]] = []
        for request_id, (conn, offer, session) in pending:
            out.append(
                {
                    "request_id": request_id,
                    "transfer_id": session.transfer_id,
                    "peer": session.peer,
                    "total": session.total_bytes,
                    "files": [
                        {
                            "index": item.entry.index,
                            "name": item.entry.name,
                            "size": item.entry.size,
                            "resumed": item.done_bytes,
                        }
                        for item in session.items.values()
                    ],
                    "authenticated": bool(conn.peer and conn.peer.authenticated),
                    "sas": conn.peer.sas if conn.peer else "",
                }
            )
        return out

    # ------------------------------------------------------------------
    # chat
    # ------------------------------------------------------------------

    def _on_peer_bye(self, peer_info: DeviceInfo, address: str) -> None:
        """A peer said goodbye over TCP: take it offline immediately."""
        peer = self.discovery.peers.mark_offline(peer_info.device_id, from_bye=True)
        if peer is not None:
            self.events.emit(
                "device_lost",
                peer=peer,
                device_id=peer_info.device_id,
                reason="对方已断开连接",
                address=address,
            )

    def _on_chat(self, conn: Connection, payload: dict[str, Any]) -> None:
        """One chat message arrived from a peer (server thread)."""
        try:
            event = self.accept_chat(payload, source="peer")
        except Exception as exc:
            self.events.emit("chat_rejected", reason=str(exc))
            try:
                conn.send_json(MSG_ERROR, {"message": f"chat rejected: {exc}"})
            except Exception:
                pass
            return
        try:
            conn.send_json(MSG_CHAT_ACK, {"id": event["message"]["id"]})
        except Exception:
            pass
        self.events.emit("chat_message", **event)

    def relay_chat(self, conv_id: str, event: dict[str, Any]) -> int:
        """Forward a message we accepted to every *computer* in the room.

        A phone can only talk to the computer serving its page, so that computer
        is the hub: without this, a group with one phone and two laptops would
        deliver each phone message to exactly one of them.
        """
        conversation = event.get("conversation") or {}
        message = event.get("message") or {}
        payload = self.chat_payload(conversation, message)
        delivered = 0
        for member in conversation.get("members") or []:
            if member == self.info.device_id or str(member).startswith("web:"):
                continue
            if self.deliver_chat(str(member), payload):
                delivered += 1
        return delivered

    def accept_chat(self, payload: dict[str, Any], *, source: str = "peer") -> dict[str, Any]:
        """Store an incoming chat message (from a peer *or* from a phone).

        Shared by both transports on purpose: a message from another computer
        arrives as a MSG_CHAT frame, one from a phone arrives as an HTTP POST,
        and both have to end up in exactly the same conversation.
        """
        conv = dict(payload.get("conv") or {})
        msg = dict(payload.get("msg") or {})
        conv_id = str(conv.get("id") or "").strip()
        if not conv_id or not msg:
            raise ValueError("chat payload needs conv.id and msg")
        # Never let a peer invent members we did not agree to: merge, don't replace.
        self.chat.upsert_conversation(
            conv_id,
            kind=str(conv.get("kind") or "direct"),
            title=str(conv.get("title") or ""),
            members=list(conv.get("members") or []),
        )
        sender = str(msg.get("sender") or "") or (
            "web:phone" if source == "phone" else ""
        )
        if sender and sender != self.info.device_id:
            self.chat.add_member(conv_id, sender)
        message = self.chat.add_message(
            conv_id,
            message_id=str(msg.get("id") or ""),
            sender=sender,
            sender_name=str(msg.get("senderName") or ""),
            kind=str(msg.get("kind") or "text"),
            text=str(msg.get("text") or ""),
            media_name=str(msg.get("mediaName") or ""),
            media_rel=str(msg.get("mediaRel") or ""),
            media_size=int(msg.get("mediaSize") or 0),
            media_mime=str(msg.get("mediaMime") or ""),
            duration_ms=int(msg.get("durationMs") or 0),
            direction="in",
            state="received",
            ts=float(msg["ts"]) if msg.get("ts") else None,
        )
        missing = self.missing_attachment(message)
        if missing:
            # The bubble references a file that is not here: say so, or the
            # receiver shows a broken preview and concludes the app lost it.
            # (This is the mirror image of "sender says sent": on this side the
            # honest sentence is "附件没有传过来".)
            self.events.emit(
                "chat_attachment_missing",
                conversation=self.chat.conversation(conv_id) or {},
                message=message,
                peer=sender,
                mediaRel=str(message.get("mediaRel") or ""),
                reason=failure_text("attachment_missing", "对方标记为已发送，但文件没有传过来"),
                error_code="attachment_missing",
            )
        return {
            "conversation": self.chat.conversation(conv_id) or {},
            "message": message,
            "source": source,
            "attachment_missing": bool(missing),
        }

    def missing_attachment(self, message: dict[str, Any]) -> str:
        """The absolute path a received message points at, when it is absent.

        Returns ``""`` when there is nothing to check or the file is there.
        """
        rel = str((message or {}).get("mediaRel") or "")
        if not rel:
            return ""
        try:
            path = safe_join(self.config.receive_dir, os.path.dirname(rel), os.path.basename(rel))
        except Exception:
            path = os.path.join(self.config.receive_dir, rel)
        return "" if os.path.exists(path) else path

    # ------------------------------------------------------------------
    # attachment lifecycle for the publish-and-pull flow (phones)
    # ------------------------------------------------------------------

    def mark_attachment_shared(
        self,
        message_id: str,
        *,
        share_id: str = "",
        reason: str = "已发布给对方下载，等它来取",
    ) -> bool:
        """Record that an attachment was *published* and is waiting to be pulled.

        A browser/phone client cannot be pushed to, so the honest state between
        "nothing happened" and "the other side really got it" is this one:
        ``sending`` plus a sentence.  The web layer calls this when it registers
        the share; :meth:`mark_attachment_delivered` follows when the download
        actually completes.
        """
        stored = self.chat.message(str(message_id))
        if stored is None:
            return False
        self.chat.set_state(str(message_id), "sending")
        self.events.emit(
            "chat_attachment_shared",
            conversation=self.chat.conversation(str(stored.get("conv") or "")) or {},
            message=dict(stored, state="sending"),
            shareId=str(share_id or ""),
            reason=reason,
            peer=str(stored.get("sender") or ""),
        )
        return True

    def mark_attachment_delivered(self, message_id: str, *, bytes: int = 0) -> bool:
        """The other side really took the file: only now may the record say sent."""
        stored = self.chat.message(str(message_id))
        if stored is None:
            return False
        self.chat.set_state(str(message_id), "sent")
        conversation = self.chat.conversation(str(stored.get("conv") or "")) or {}
        self.events.emit(
            "chat_delivered",
            conversation=conversation,
            message=dict(stored, state="sent"),
            peer=str(stored.get("sender") or ""),
            bytes=int(bytes or 0),
            attempts=1,
            reason="对方已取走文件",
        )
        return True

    def mark_attachment_failed(
        self, message_id: str, *, reason: str, code: str = "unknown"
    ) -> bool:
        """The attachment will not arrive: record the failure with its reason."""
        stored = self.chat.message(str(message_id))
        if stored is None:
            return False
        text = failure_text(code, reason)
        self.chat.set_state(str(message_id), "failed")
        self.events.emit(
            "chat_failed",
            conversation=self.chat.conversation(str(stored.get("conv") or "")) or {},
            message=dict(stored, state="failed"),
            peer=str(stored.get("sender") or ""),
            state="failed",
            error=reason,
            reason=text,
            error_code=code,
            error_text=text,
        )
        self._note_problem("chat_failed", reason)
        return True

    def chat_payload(
        self,
        conversation: dict[str, Any],
        message: dict[str, Any],
    ) -> dict[str, Any]:
        """The wire form of one message: conversation + message, one frame."""
        return {
            "v": 1,
            "conv": {
                "id": conversation.get("id", ""),
                "kind": conversation.get("kind", "direct"),
                "title": conversation.get("title", ""),
                "members": list(conversation.get("members") or []),
            },
            "msg": {
                "id": message.get("id", ""),
                "sender": message.get("sender") or self.info.device_id,
                "senderName": message.get("senderName") or self.info.name,
                "kind": message.get("kind", "text"),
                "text": message.get("text", ""),
                "mediaName": message.get("mediaName", ""),
                "mediaRel": message.get("mediaRel", ""),
                "mediaSize": message.get("mediaSize", 0),
                "mediaMime": message.get("mediaMime", ""),
                "durationMs": message.get("durationMs", 0),
                "ts": message.get("ts", time.time()),
            },
        }

    def _resolve_chat_peer(self, device_id: str) -> tuple[Peer | None, bool]:
        """Who to dial for ``device_id``: the live device list, then memory.

        Returns ``(peer, is_live)``.  A device that is not here *right now* but
        has been seen before still has a last known address, and that is what
        makes "对方不在线 → 进重发队列 → 它一上线就补投" possible at all.
        """
        wanted = str(device_id or "")
        if not wanted:
            return None, False
        for peer in self.devices():
            if peer.info.device_id == wanted:
                return peer, True
        known = self.discovery.peers.known(wanted)
        if known is not None:
            return known.to_peer(), False
        return None, False

    def deliver_chat_ex(
        self, device_id: str, payload: dict[str, Any], *, timeout: float = 8.0
    ) -> tuple[bool, str]:
        """Try once to hand a message to a peer.

        Returns ``(delivered, reason)``: an ACK from the other side is the only
        thing that counts as delivered ("我没有收到失败" is not success), and
        when it is not delivered the reason is a Chinese sentence the UI can
        show instead of a silent ``False`` (charter §5.5, E3).
        """
        if str(device_id) == self.info.device_id:
            # Handing a message to ourselves would be *acknowledged by
            # ourselves*: the bubble would say 「已送达」 while the other device
            # never saw it -- precisely the "发出去了但对面没有" complaint.
            return False, failure_text("peer_offline", "目标就是本机")
        peer, is_live = self._resolve_chat_peer(device_id)
        if peer is None:
            return False, failure_text("peer_offline", "从未发现过这台设备")
        addresses: list[str] = []
        for candidate in [peer.address] + list(peer.addresses or []):
            if candidate and candidate not in addresses and not candidate.startswith("manual:"):
                addresses.append(candidate)
        if not addresses:
            return False, failure_text("peer_offline", "没有可用的地址")
        last_error = ""
        for address in addresses[:3]:
            conn: Connection | None = None
            try:
                sock = connect_to(address, peer.port, timeout=timeout)
                conn = Connection(sock, self.identity, stream_id=0, encrypt=self.config.encrypt)
                conn.handshake_initiator(self.info)
                conn.send_json(MSG_CHAT, payload)
                conn.sock.settimeout(timeout)
                frame = conn.recv()
                if frame.type == MSG_CHAT_ACK:
                    if not is_live:
                        # It answered although discovery has not re-seen it:
                        # that is a reconnection, and it is worth saying so.
                        self.discovery.peers.add_direct(peer.info, address, peer.port, "chat")
                        self.events.emit(
                            "peer_reconnected",
                            peer=peer.info,
                            device_id=str(device_id),
                            address=address,
                        )
                    return True, ""
                last_error = f"对方回复了意外的消息类型 {frame.type}"
            except (OSError, HandshakeError, ConnectionClosed, ProtocolError) as exc:
                last_error = str(exc)
                continue
            finally:
                if conn is not None:
                    conn.abort()
        text = failure_text(classify_failure(last_error), last_error)
        self.events.emit("chat_failed", peer=str(device_id), error=last_error or text, reason=text)
        return False, text

    def deliver_chat(self, device_id: str, payload: dict[str, Any], *, timeout: float = 8.0) -> bool:
        """Hand one chat payload to a peer, over its own short-lived connection."""
        delivered, _reason = self.deliver_chat_ex(device_id, payload, timeout=timeout)
        return delivered

    def _on_engine_event(self, event: dict[str, Any]) -> None:
        """Every engine event passes through here (subscribe callback).

        Two jobs: remember failures for the diagnostic card (E2), and use a
        peer's reappearance as the trigger to flush its queue (the "补投" half
        of charter §5.5).
        """
        try:
            kind = str(event.get("kind", ""))
            if kind in ("device_found", "device_updated"):
                peer = event.get("peer")
                device_id = str(getattr(peer, "info", None).device_id if peer is not None else "")
                if device_id and self.outbox.wake(device_id):
                    self._outbox_wake.set()
            elif kind in ("device_lost", "discovery_warning", "warning"):
                message = str(event.get("message") or event.get("reason") or "")
                if message:
                    self._note_problem(kind, message)
            elif kind in ("chat_failed", "transfer_finished", "send_finished", "file_failed"):
                error = str(event.get("error") or "")
                if error:
                    self._note_problem(kind, error, event)
        except Exception:
            pass

    def _note_problem(self, kind: str, message: str, event: dict[str, Any] | None = None) -> None:
        entry = {
            "ts": time.time(),
            "kind": kind,
            "message": message,
            "text": (event or {}).get("error_text") or "",
            "code": (event or {}).get("error_code") or "",
        }
        self._problems.append(entry)

    # -- guaranteed delivery queue -----------------------------------------

    def _outbox_loop(self) -> None:
        """Retry unacknowledged messages with 1/2/4/8 s backoff (max 5 tries)."""
        while not self._outbox_stop.is_set():
            try:
                for entry in self.outbox.due():
                    if self._outbox_stop.is_set():
                        return
                    self._attempt_delivery(entry)
            except Exception as exc:  # pragma: no cover - defensive
                self._note_problem("outbox", str(exc))
            self._outbox_wake.wait(0.25)
            self._outbox_wake.clear()

    def _attempt_delivery(self, entry: OutboxEntry) -> bool:
        """One delivery attempt for a queued message."""
        entry.attempts += 1
        reason = ""
        if entry.media_path:
            # The attachment has to land before the message that points at it,
            # or the receiver shows a broken bubble.  A retry resumes, so the
            # second attempt only moves the bytes that are still missing.
            peer, _live = self._resolve_chat_peer(entry.peer_id)
            if peer is None:
                reason = failure_text("peer_offline")
            else:
                try:
                    if not self.send(peer, [entry.media_path], rel_dir=entry.rel_dir):
                        reason = failure_text("interrupted", "附件没有送达")
                except Exception as exc:
                    reason = failure_text(classify_failure(exc), str(exc))
        if not reason:
            delivered, reason = self.deliver_chat_ex(entry.peer_id, entry.payload)
        else:
            delivered = False

        if delivered:
            entry.state = "sent"
            entry.reason = "已送达"
            try:
                self.chat.set_state(entry.message_id, "sent")
            except Exception:
                pass
            self.outbox.remove(entry.message_id)
            self.events.emit(
                "chat_delivered",
                conversation=entry.conversation,
                message=dict(entry.message, state="sent"),
                peer=entry.peer_id,
                attempts=entry.attempts,
                delivered=1,
                reason="已送达",
            )
            return True

        entry.last_error = reason
        if entry.attempts >= CHAT_MAX_ATTEMPTS:
            entry.state = "failed"
            entry.reason = f"对方不在线，重试 {entry.attempts} 次仍未送达"
            try:
                self.chat.set_state(entry.message_id, "failed")
            except Exception:
                pass
            self.events.emit(
                "chat_failed",
                conversation=entry.conversation,
                message=dict(entry.message, state="failed"),
                peer=entry.peer_id,
                attempts=entry.attempts,
                state="failed",
                error=reason,
                reason=entry.reason,
                error_code="peer_offline",
            )
            self._note_problem("chat_failed", reason)
            return False

        delay = CHAT_RETRY_DELAYS[min(entry.attempts - 1, len(CHAT_RETRY_DELAYS) - 1)]
        entry.next_attempt = time.monotonic() + delay
        entry.reason = (
            f"对方不在线，已进入重发队列（第 {entry.attempts}/{CHAT_MAX_ATTEMPTS} 次尝试，"
            f"{delay:.0f}s 后重试）"
        )
        self.events.emit(
            "chat_retry",
            conversation=entry.conversation,
            message=dict(entry.message, state="queued"),
            peer=entry.peer_id,
            attempts=entry.attempts,
            maxAttempts=CHAT_MAX_ATTEMPTS,
            nextRetryIn=delay,
            error=reason,
            reason=entry.reason,
            **failure_info(reason, code="peer_offline"),
        )
        return False

    def _enqueue_chat(
        self,
        *,
        conv_id: str,
        peer_id: str,
        peer_name: str,
        payload: dict[str, Any],
        message: dict[str, Any],
        conversation: dict[str, Any],
        reason: str,
        media_path: str = "",
        rel_dir: str = "",
    ) -> OutboxEntry:
        entry = OutboxEntry(
            message_id=str(message.get("id", "")),
            conv_id=conv_id,
            peer_id=peer_id,
            peer_name=peer_name,
            payload=payload,
            message=dict(message),
            conversation=dict(conversation),
            media_path=media_path,
            rel_dir=rel_dir,
            reason=reason,
            next_attempt=time.monotonic() + CHAT_RETRY_DELAYS[0],
        )
        self.outbox.put(entry)
        try:
            self.chat.set_state(entry.message_id, "queued")
        except Exception:
            pass
        self._outbox_wake.set()
        self.events.emit(
            "chat_queued",
            conversation=conversation,
            message=dict(message, state="queued"),
            peer=peer_id,
            peerName=peer_name,
            attempts=0,
            maxAttempts=CHAT_MAX_ATTEMPTS,
            nextRetryIn=CHAT_RETRY_DELAYS[0],
            reason=reason,
            **failure_info(reason, code="peer_offline"),
        )
        return entry

    def _recover_queued_messages(self) -> None:
        """Re-arm messages that were still queued when the app last closed.

        Without this, closing the window in the half minute after a message was
        queued would drop it for good -- and the user would have been told
        「待发送」.  Bounded to the newest conversations so startup stays fast.
        """
        try:
            conversations = []
            try:
                conversations = self.chat.conversations()[:20]
            except Exception:
                return
            for conversation in conversations:
                conv_id = str(conversation.get("id", ""))
                if not conv_id:
                    continue
                try:
                    messages = self.chat.messages(conv_id, limit=50)
                except Exception:
                    continue
                for message in messages:
                    if str(message.get("state")) != "queued" or message.get("direction") != "out":
                        continue
                    members = [
                        str(m)
                        for m in conversation.get("members") or []
                        if str(m) != self.info.device_id and not str(m).startswith("web:")
                    ]
                    if not members:
                        continue
                    target = members[0]
                    known = self.discovery.peers.known(target)
                    payload = self.chat_payload(conversation, message)
                    self._enqueue_chat(
                        conv_id=conv_id,
                        peer_id=target,
                        peer_name=(known.name if known else target),
                        payload=payload,
                        message=message,
                        conversation=conversation,
                        reason="上次退出时仍在等待对方上线，已恢复重发队列",
                    )
        except Exception as exc:  # pragma: no cover - defensive
            self._note_problem("chat_recover", str(exc))

    # -- outbox API for the UI ---------------------------------------------

    def chat_queue(self) -> list[dict[str, Any]]:
        """The pending-delivery queue, newest state included (UI reads this)."""
        return [entry.to_dict() for entry in self.outbox.all()]

    def message_state(self, message_id: str) -> dict[str, Any]:
        """State + reason for one message, without touching the network."""
        entry = self.outbox.get(str(message_id))
        if entry is None:
            stored = None
            try:
                stored = self.chat.message(str(message_id))
            except Exception:
                stored = None
            state = str((stored or {}).get("state") or "")
            if state == "queued":
                return {"state": "queued", "reason": "等待对方上线", "attempts": 0, "nextRetryIn": 0.0}
            return {"state": state or "unknown", "reason": "", "attempts": 0, "nextRetryIn": 0.0}
        data = entry.to_dict()
        data["state"] = entry.state
        return data

    def retry_message(self, message_id: str) -> bool:
        """Put a queued (or given-up) message back at the head of the queue."""
        entry = self.outbox.get(str(message_id))
        if entry is None:
            return False
        entry.state = "queued"
        entry.attempts = 0
        entry.next_attempt = 0.0
        entry.reason = "正在重新投递"
        try:
            self.chat.set_state(entry.message_id, "queued")
        except Exception:
            pass
        self._outbox_wake.set()
        self.events.emit(
            "chat_queued",
            conversation=entry.conversation,
            message=dict(entry.message, state="queued"),
            peer=entry.peer_id,
            attempts=0,
            maxAttempts=CHAT_MAX_ATTEMPTS,
            nextRetryIn=0.0,
            reason="用户手动重试",
        )
        return True

    def drop_message(self, message_id: str) -> bool:
        """Give up on one queued message (the UI's 「放弃」 action)."""
        entry = self.outbox.remove(str(message_id))
        if entry is None:
            return False
        try:
            self.chat.set_state(entry.message_id, "failed")
        except Exception:
            pass
        self.events.emit(
            "chat_dropped",
            conversation=entry.conversation,
            message=dict(entry.message, state="failed"),
            peer=entry.peer_id,
            reason="已放弃发送",
        )
        return True

    #: Aliases so a front end that guessed another spelling still works.
    retry_chat = retry_message
    discard_message = drop_message

    def send_chat(
        self,
        conv_id: str,
        *,
        kind: str = "text",
        text: str = "",
        media_path: str = "",
        media_name: str = "",
        media_mime: str = "",
        duration_ms: int = 0,
        title: str = "",
        members: Iterable[str] | None = None,
        to: str = "",
        sender_override: str = "",
        sender_name: str = "",
    ) -> dict[str, Any]:
        """Store a message and try to hand it to every computer in the room.

        Phones are members too, but they are browser clients: nothing is pushed
        to them -- the page reads the message from the store.  So a member that
        is reachable over the protocol gets a frame, and everybody else just
        finds it waiting.
        """
        member_list = list(members if members is not None else self.chat.members(conv_id))
        if to and to not in member_list:
            # A brand-new 1:1 chat has no stored members yet; the caller knows
            # who it is talking to, and that is the only place it can come from.
            member_list.append(to)
        if self.info.device_id and self.info.device_id not in member_list:
            member_list.append(self.info.device_id)
        conv = self.chat.upsert_conversation(
            conv_id,
            kind="group" if conv_id.startswith("g:") else "direct",
            title=title,
            members=member_list,
        )

        rel = ""
        if media_path:
            name = media_name or os.path.basename(media_path)
            rel = media_relpath(conv_id, name)
            media_name = os.path.basename(rel)

        message = self.chat.add_message(
            conv_id,
            sender=sender_override or self.info.device_id,
            sender_name=sender_name or self.info.name,
            kind=kind,
            text=text,
            media_name=media_name,
            media_rel=rel,
            # Local-only: lets this machine show a preview of what it sent.
            # chat_payload() deliberately drops it before anything goes on the
            # wire, so the peer never learns this computer's folder layout.
            media_source=media_path,
            media_size=os.path.getsize(media_path) if media_path and os.path.exists(media_path) else 0,
            media_mime=media_mime,
            duration_ms=duration_ms,
            direction="out",
            state="sending",
        )
        payload = self.chat_payload(conv, message)

        # An attachment changes what "sent" may mean.  The chat channel can only
        # *reference* a file: unless the file really travelled over the file
        # channel (or is queued to travel), the message is not delivered -- and
        # writing `sent` there is exactly the reported P0 (762 MB "sent", zero
        # bytes arrived; docs/REAL_DEVICE_FINDINGS.md §2.2).
        attachment = bool(media_path)
        attachment_code = ""
        if attachment:
            try:
                size = os.path.getsize(media_path)
            except OSError:
                attachment_code = "attachment_missing"
            else:
                if size > int(self.config.chat_attachment_max_bytes):
                    attachment_code = "attachment_too_large"

        delivered = 0
        reachable = 0
        queued = 0
        failed = 0
        browser_only = 0
        reasons: list[str] = []
        codes: list[str] = []
        if attachment and attachment_code:
            codes.append(attachment_code)
            reasons.append(failure_text(attachment_code))

        for member in member_list:
            if member == self.info.device_id:
                continue  # ourselves: never a destination
            if member.startswith("web:"):
                # A browser client has no receiving socket, so it cannot be
                # pushed to.  That is not a failure of *text* (the page reads
                # the store) but it is one for an attachment.
                if attachment:
                    browser_only += 1
                    failed += 1
                    codes.append("no_file_service")
                    reasons.append(failure_text("no_file_service", "手机/网页端要由它来取"))
                continue
            reachable += 1
            peer, live = self._resolve_chat_peer(member)
            if peer is None:
                # Never discovered, so there is nothing to retry against: say
                # so instead of returning a silent False (charter §5.5).
                failed += 1
                codes.append("peer_offline")
                reasons.append(f"{member[:12]}：{failure_text('peer_offline', '从未发现过这台设备')}")
                continue
            media_ok = not attachment_code
            media_code = attachment_code
            if attachment and media_ok:
                # Hand the file over first, into the folder both sides compute
                # from the conversation id; only then may the message reference
                # it.  `send_ex` says *why* when it cannot, so the queue entry
                # (and the UI) carries a real reason instead of a bare failure.
                allowed, why = self.can_push_files(member, peer=peer)
                if not allowed:
                    media_ok, media_code = False, "no_file_service"
                    self._note_problem("chat_media", why)
                else:
                    outcome = self.send_ex(peer, [media_path], rel_dir=os.path.dirname(rel))
                    if not outcome.ok:
                        media_ok, media_code = False, (outcome.error_code or "attachment_not_sent")
                        if media_code == "cancelled":
                            media_code = "attachment_not_sent"
                    else:
                        media_code = ""
            if media_ok and live and self.deliver_chat(member, payload):
                delivered += 1
                continue
            if attachment and media_code in ("attachment_missing", "attachment_too_large"):
                # Retrying cannot help: the file is wrong, not the network.
                failed += 1
                continue
            # Not delivered: either the device is known but away (queue it, it
            # goes out the moment it comes back), or it answered nothing this
            # time (still worth retrying: a peer that is mid-restart accepts
            # the connection on the next attempt).
            self._enqueue_chat(
                conv_id=conv_id,
                peer_id=member,
                peer_name=peer.info.name or member,
                payload=payload,
                message=message,
                conversation=self.chat.conversation(conv_id) or {},
                reason=(
                    failure_text(media_code)
                    if attachment and media_code
                    else failure_text("peer_offline", "等待对方上线")
                    if not live
                    else "对方没有确认收到，已进入重发队列"
                ),
                media_path=media_path if not attachment_code else "",
                rel_dir=os.path.dirname(rel) if rel else "",
            )
            if not media_ok:
                failed += 1
                codes.append(media_code or "attachment_not_sent")
                reasons.append(f"{peer.info.name or member}：{failure_text(media_code or 'attachment_not_sent')}")
            queued += 1

        if not reachable:
            # A phone-only room: text is delivered (the page reads the store),
            # an attachment is not -- nothing carried the file anywhere.
            state = "failed" if (attachment and delivered == 0) else "sent"
        elif delivered and not queued and not failed:
            state = "sent"
        elif queued:
            state = "queued"
        else:
            state = "failed"
        if attachment and state == "sent" and delivered == 0:
            state = "failed"
            codes.append("attachment_not_sent")
            reasons.append(failure_text("attachment_not_sent"))
        reason = ""
        if state == "queued":
            reason = f"已进入重发队列（{queued} 个对端），对方上线后自动补投"
        elif state == "failed":
            reason = "；".join(dict.fromkeys(reasons)) or failure_text("peer_offline")
        code = next(
            (c for c in ("attachment_missing", "attachment_too_large", "no_file_service",
                         "attachment_not_sent") if c in codes),
            codes[0] if codes else "",
        )
        text = failure_text(code, reason) if code else ""
        self.chat.set_state(message["id"], state)
        event = {
            "conversation": self.chat.conversation(conv_id) or {},
            "message": dict(
                message, state=state, mediaRel=rel, mediaName=media_name, reason=reason,
                stateReason=reason, errorCode=code, errorText=text,
            ),
            "delivered": delivered,
            "reachable": reachable,
            "queued": queued,
            "failed": failed,
            "browserOnly": browser_only,
            "attachment": attachment,
            "reason": reason,
            "error_code": code,
            "error_text": text,
        }
        self.events.emit("chat_sent", **event)
        return event

    # ------------------------------------------------------------------
    # devices
    # ------------------------------------------------------------------

    def devices(self) -> list[Peer]:
        """能收发文件的设备 —— **永远不含本机自己**。

        这里必须把本机滤掉，而且必须在**源头**滤：以前只把过滤放在网页版的
        `/api/state` 里（`peer.info.device_id == mine` 就跳过），电脑端的侧栏是
        直接吃这个列表的，于是本机一旦被登记进发现表，就会以**自己的名字**
        （截图上是 `xgl`，地址 127.0.0.1）出现在「在线设备」里冒充一台设备。

        为什么这比"多一行"更糟：侧栏会把它当成一台可发送的设备、自动恢复会话时
        也可能先选中它，用户看到的就成了"点手机却显示自己的会话""与手机的对话
        不显示"。所以这是一处会影响**会话归属**的缺陷，不是外观问题。

        本机是怎么混进来的：自己发的公告在回环上被自己收到、入站连接带着本机
        设备号、`add_manual_device("127.0.0.1")` 之类。任何一条都不该让本机
        变成"一台设备"，所以判据放在这里，而不是逐个调用方去补。
        """
        peers = self.discovery.peers.all()
        mine = str(self.info.device_id or "")
        local = self._local_addresses()
        for peer in peers:
            peer.trusted = peer.info.device_id in self._trusted
            # Two instances on one machine have different identities (separate
            # data directories) and therefore stay separate rows, correctly.
            # Labelling them makes that obvious instead of looking like the
            # list is showing duplicates.
            peer.same_host = peer.address in local
        if not mine:
            return peers
        return [peer for peer in peers if str(peer.info.device_id or "") != mine]

    def _local_addresses(self) -> set[str]:
        """Addresses belonging to this machine, for the same-host label."""
        try:
            from .sockutil import list_interfaces

            found = {i.address for i in list_interfaces(include_virtual=True, include_loopback=True)}
        except Exception:
            return set()
        found.update({"127.0.0.1", "::1", "localhost"})
        return found

    def announce(self) -> None:
        self.discovery.announce()

    def scan(self) -> int:
        """Run an active subnet scan in the background.

        The result is reported as a ``scan_finished`` event.  Without it the UI
        could only say "scanning…" and had no way to ever say anything else --
        which is exactly what users saw: a status line stuck on "正在扫描" for
        the rest of the session.
        """

        def run() -> None:
            found = 0
            registered = 0
            error = ""
            try:
                before = {p.info.device_id for p in self.devices()}
                found = self.discovery.scan_subnets(self.info.tcp_port, reason="manual")
                registered = len({p.info.device_id for p in self.devices()} - before)
            except Exception as exc:  # pragma: no cover - defensive
                error = str(exc)
            self.events.emit(
                "scan_finished",
                found=int(found),
                registered=int(registered),
                error=error,
                lastScan=dict(self.discovery._last_scan),  # noqa: SLF001 - same package
            )

        threading.Thread(target=run, name="subnet-scan", daemon=True).start()
        return 0

    def add_manual_device(self, address: str, port: int = 0, name: str = "") -> Peer:
        return self.discovery.add_manual(address, port or self.info.tcp_port, name)

    # ------------------------------------------------------------------
    # peer capabilities: can it accept a push, or must it pull?
    # ------------------------------------------------------------------

    def peer_capabilities(self, device_id: str) -> dict[str, Any]:
        """What this peer can do, as far as we can tell *before* sending.

        ``accept`` is the bit that decides the flow: a device with a receiving
        service can be pushed to (``Engine.send``/``send_ex``), a browser/phone
        client cannot and must instead be handed a published share to pull
        (docs/REAL_DEVICE_FINDINGS.md §2.3).
        """
        wanted = str(device_id or "")
        peer, live = self._resolve_chat_peer(wanted)
        if peer is None:
            return {
                "deviceId": wanted,
                "name": "",
                "known": False,
                "online": False,
                "accept": False,
                "kind": "",
                "address": "",
                "port": 0,
                "caps": {},
                "source": "",
                "reason": failure_text("peer_offline", "从未发现过这台设备"),
            }
        caps = dict(peer.info.capabilities or {})
        accept = caps.get("accept", None)
        if accept is None:
            # A peer that never advertised the bit is assumed able to receive
            # only if it looks like a protocol peer at all: ``mobile`` kind, a
            # ``web:`` member id, or no port means a browser client.
            accept = bool(peer.port) and peer.info.kind != "mobile"
        return {
            "deviceId": peer.info.device_id,
            "name": peer.info.name,
            "known": True,
            "online": bool(live),
            "accept": bool(accept),
            "kind": peer.info.kind,
            "address": peer.address,
            "port": int(peer.port or 0),
            "caps": caps,
            "source": peer.source,
            "reason": "" if accept else failure_text("no_file_service"),
        }

    def can_push_files(self, device_id: str = "", *, peer: Peer | None = None) -> tuple[bool, str]:
        """Whether files can be *pushed* to this device, with a Chinese reason.

        Returns ``(True, "")`` or ``(False, "为什么不能")``.  Callers use it to
        choose between two honest flows instead of pretending they are one:
        push over the protocol, or publish the file and let the other side pull.
        """
        wanted = str((peer.info.device_id if peer is not None else device_id) or "")
        if wanted and wanted == self.info.device_id:
            return False, failure_text("no_file_service", "目标就是本机")
        caps = self.peer_capabilities(wanted)
        if caps["known"]:
            accept, kind, port = bool(caps["accept"]), str(caps["kind"]), int(caps["port"])
        elif peer is not None:
            # The caller handed us a Peer directly (CLI, manual entry, a test):
            # trust the object's own shape rather than the discovery table, or a
            # peer discovery has not confirmed yet could never be dialled.
            explicit = dict(peer.info.capabilities or {})
            accept = bool(explicit.get("accept", bool(peer.port) and peer.info.kind != "mobile"))
            kind, port = peer.info.kind, int(peer.port or 0)
        else:
            return False, str(caps["reason"] or failure_text("peer_offline"))
        if not accept:
            detail = ""
            if kind == "mobile" or wanted.startswith("web:"):
                detail = "它是网页/手机客户端，没有接收端口：请发布给它下载"
            return False, failure_text("no_file_service", detail)
        if not port:
            return False, failure_text("no_file_service", "对方没有公布接收端口")
        return True, ""

    # ------------------------------------------------------------------
    # offline devices, diagnostics, reconnection
    # ------------------------------------------------------------------

    def known_devices(self, *, include_online: bool = False) -> list[dict[str, Any]]:
        """Devices seen before, with where and when (the 「离线设备」 list).

        The peer table only holds devices that are announcing *now*; this is
        the memory behind "对方不在线 → 排队 → 它回来就补投", and behind the
        settings page's offline list.
        """
        online = {p.info.device_id for p in self.devices()}
        out: list[dict[str, Any]] = []
        for entry in self.discovery.peers.known_all():
            if not include_online and entry.device_id in online:
                continue
            data = entry.to_dict()
            data["online"] = entry.device_id in online
            data["trusted"] = entry.device_id in self._trusted
            data["sameHost"] = entry.address in self._local_addresses()
            out.append(data)
        return out

    def forget_offline_devices(self) -> int:
        """Forget every device that is not here right now (settings action)."""
        removed = self.discovery.peers.forget_known()
        self.events.emit("offline_devices_cleared", removed=removed)
        return removed

    def reconnect(self, device_id: str, *, timeout: float = 3.0) -> bool:
        """Try to reach a device that is not announcing itself any more.

        Discovery can be blocked while TCP is not (a phone whose UDP is
        filtered, a peer whose announcements a switch drops), so the last known
        address is worth one handshake.  On success the device is back in the
        live list and its queued messages are flushed immediately -- that is
        the "恢复后自动补投" half of charter §5.7.
        """
        known = self.discovery.peers.known(device_id)
        if known is None:
            return False
        for address in [known.address] + list(known.addresses):
            if not address:
                continue
            peer = self.probe_address(address, known.port, timeout=timeout)
            if peer is not None:
                if self.outbox.wake(device_id):
                    self._outbox_wake.set()
                return True
        return False

    def set_discovery_channels(
        self,
        *,
        multicast: bool | None = None,
        broadcast: bool | None = None,
        scan: bool | None = None,
    ) -> dict[str, bool]:
        """Turn discovery channels on/off at runtime (settings page).

        Takes effect on the next :meth:`start` for the socket-based channels
        (rebinding a listener mid-flight is a restart in disguise); ``scan``
        applies immediately because it needs no socket.
        """
        if multicast is not None:
            self.config.enable_multicast = bool(multicast)
        if broadcast is not None:
            self.config.enable_broadcast = bool(broadcast)
        if scan is not None:
            self.config.enable_scan = bool(scan)
            self.discovery.enable_scan = bool(scan)
        return {
            "multicast": bool(self.config.enable_multicast),
            "broadcast": bool(self.config.enable_broadcast),
            "scan": bool(self.config.enable_scan),
        }

    def diagnostics(self) -> dict[str, Any]:
        """Everything the 诊断 card shows (charter E2), in one dict."""
        data = self.discovery.diagnostics_snapshot()
        data.update(
            {
                "app": "EverSend",
                "version": _version(),
                "deviceId": self.identity.device_id,
                "name": self.info.name,
                "platform": platform_tag(),
                "kind": self.info.kind,
                "tcpPort": self.info.tcp_port,
                "webPort": self.info.web_port,
                "discoveryPort": self.config.discovery_port,
                "receiveDir": self.config.receive_dir,
                "dataDir": self.config.data_dir,
                "streams": self.config.streams,
                "encrypt": bool(self.config.encrypt),
                "resume": bool(self.config.resume),
                "trustedDevices": len(self._trusted),
                "knownDevices": len(self.discovery.peers.known_all()),
                "onlineDevices": len(self.devices()),
                "outbox": [entry.to_dict() for entry in self.outbox.all()],
                "recentErrors": list(self._problems),
                "activeTransfers": len(self.active_transfers()),
            }
        )
        return data

    def export_diagnostics(self) -> str:
        """The 导出诊断信息 button: one JSON document a user can attach."""
        try:
            return json.dumps(self.diagnostics(), ensure_ascii=False, indent=2, default=str)
        except Exception as exc:  # pragma: no cover - defensive
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    def _probe_for_scan(self, address: str, port: int, local: str = "") -> Peer | None:
        """Discovery's callback: turn a host that answered a probe into a peer.

        This is the piece that makes the scan *useful* rather than decorative.
        The old flow emitted ``scan_hit`` and left it to the desktop window to
        handshake and register -- so the browser UI, the CLI and the Android
        page got nothing but an open port, and the user's device list stayed
        empty until they typed an IP.
        """
        return self.probe_address(address, port, local=local, timeout=4.0)

    def probe_address(
        self,
        address: str,
        port: int = 0,
        *,
        timeout: float = 4.0,
        local: str = "",
    ) -> Peer | None:
        """Introduce ourselves to whatever is listening at ``address``.

        A subnet scan can only establish that *something* accepts a TCP
        connection.  Registering that as a device would invent an identity:
        the entry would be keyed by IP, so it could never merge with the same
        machine's real record and the device list would show it twice.

        Doing the handshake costs one extra round trip on hosts that already
        answered, and it yields the peer's actual device id -- which is what
        de-duplication is keyed on.  Returns ``None`` when nothing compatible
        answers, including when the answer is this very device.
        """
        port = port or self.info.tcp_port
        source = (local, 0) if local else None
        # When the evidence was collected, not when it is applied: see
        # PeerStore.said_bye_since.
        evidence_at = time.monotonic()
        try:
            sock = connect_to(address, port, timeout=timeout, source_address=source)
        except (OSError, ConnectionError):
            return None

        conn = Connection(sock, self.identity, stream_id=0, encrypt=self.config.encrypt)
        try:
            session = conn.handshake_initiator(self.info, timeout=timeout)
        except Exception:
            conn.abort()
            return None
        finally:
            pass

        info = session.info
        conn.abort()

        if not info.device_id or info.device_id == self.identity.device_id:
            return None  # that is this device; it is not a peer

        if self.discovery.peers.said_bye_since(info.device_id, evidence_at):
            # It answered, then said goodbye while this handshake was still in
            # flight.  The goodbye is the newer fact: putting the device back
            # now is how a closed laptop stays in the list for another 90 s.
            return None

        peer, is_new = self.discovery.peers.add_direct(
            info, address, info.tcp_port or port, "scan"
        )
        self.events.emit("device_found" if is_new else "device_updated", peer=peer, is_new=is_new)
        return peer

    def trust(self, device_id: str, trusted: bool = True) -> None:
        if trusted:
            self._trusted.add(device_id)
        else:
            self._trusted.discard(device_id)
        self._save_trusted()

    def is_trusted(self, device_id: str) -> bool:
        return device_id in self._trusted

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------

    def send(
        self,
        peer: Peer,
        paths: Iterable[str],
        *,
        pin: str = "",
        streams: int | None = None,
        cancel_event: threading.Event | None = None,
        rel_dir: str = "",
    ) -> bool:
        """Offer and send ``paths`` to ``peer``.  ``True`` only on a real ACK.

        A thin wrapper over :meth:`send_ex` that keeps the historical ``bool``
        contract: every caller in the tree (desktop worker, web handler, CLI,
        tests) does ``if engine.send(...)``.  When a caller needs *why* it
        failed -- and a UI always does -- it reads the outcome back through
        :meth:`last_send_outcome` (keyed by peer and transfer id) or calls
        :meth:`send_ex` directly.
        """
        return self.send_ex(
            peer,
            paths,
            pin=pin,
            streams=streams,
            cancel_event=cancel_event,
            rel_dir=rel_dir,
        ).ok

    def send_ex(
        self,
        peer: Peer,
        paths: Iterable[str],
        *,
        pin: str = "",
        streams: int | None = None,
        cancel_event: threading.Event | None = None,
        rel_dir: str = "",
    ) -> SendOutcome:
        """Offer and send ``paths`` to ``peer``; return the *reason* as well.

        Blocks until the transfer finishes.  Runs the control connection on the
        calling thread and one thread per data stream.

        The returned :class:`SendOutcome` is the single source of truth for
        "did this work": ``ok`` is True only when the receiver explicitly
        reported the transfer done, and every failure carries an
        ``error_code`` from :data:`FAILURE_REASONS` plus its Chinese sentence.
        There is deliberately no path that returns success because the *local*
        send did not raise (docs/REAL_DEVICE_FINDINGS.md §2).

        ``rel_dir`` puts the file in a sub-folder of the receiver's receive
        directory.  Chat uses it to drop attachments into
        ``韧传聊天/<conversation>/``, a path both ends compute from the
        conversation id -- so no extra protocol field is needed for "where
        should this land".
        """
        entries, sources = build_file_entries(paths)
        if rel_dir:
            for entry in entries:
                entry.rel_dir = rel_dir
        if not entries:
            outcome = SendOutcome.failure("nothing_to_send", peer=peer.info)
            self._remember_send_outcome(outcome, peer)
            self.events.emit(
                "send_finished",
                transfer_id="",
                peer=peer.info,
                status="failed",
                bytes=0,
                error=outcome.error,
                **failure_info(outcome.error, code=outcome.error_code),
            )
            return outcome

        # Pre-flight capability check: a browser/phone client has no listening
        # socket, so there is nothing to offer to.  Dialling it anyway costs a
        # connect timeout and, worse, used to end in "已发送".  Saying it before
        # spending a round trip is what makes the result trustworthy.
        allowed, why = self.can_push_files(peer.info.device_id, peer=peer)
        if not allowed:
            outcome = SendOutcome.failure(
                "no_file_service",
                detail=why,
                peer=peer.info,
                total_files=len(entries),
            )
            self._remember_send_outcome(outcome, peer)
            self.events.emit(
                "send_finished",
                transfer_id="",
                peer=peer.info,
                status="failed",
                bytes=0,
                error=why,
                **failure_info(why, code="no_file_service"),
            )
            return outcome

        # A digest we already know shortens the handshake by a round trip and
        # lets the receiver verify a fully-resumed file without asking.
        for entry in entries:
            source = sources.get(entry.index)
            if not source:
                continue
            cached = self.digest_cache.get(source, entry.size, entry.mtime_ns)
            if cached:
                entry.digest = cached

        streams = streams or self.config.streams
        try:
            control = self._open_control(peer)
        except (TransferFailed, ConnectionClosed, ProtocolError, HandshakeError, OSError) as exc:
            # "I could not even reach it" is a *result*, not an exception: every
            # caller here is a UI of some kind, and a peer that is off, asleep
            # or gone must come back as a failure it can show.  This used to
            # escape as TransferFailed from the connect path (and as
            # ConnectionResetError when something reset the connection after it
            # was established), which the resilience test caught by killing
            # connections at random.
            code = classify_failure(exc)
            if code == "connection_refused" and not peer.info.capabilities.get("accept", True):
                # Refused *and* it never claimed to have a receiver: say what
                # that means instead of a bare "connection refused".
                code = "no_file_service"
            outcome = SendOutcome.failure(
                code, detail=str(exc), peer=peer.info, total_files=len(entries)
            )
            self._remember_send_outcome(outcome, peer)
            self.events.emit(
                "send_finished",
                transfer_id="",
                peer=peer.info,
                status="failed",
                bytes=0,
                error=str(exc),
                **failure_info(exc, code=code),
            )
            return outcome

        session = SendSession(
            control=control,
            identity=self.identity,
            entries=entries,
            sources=sources,
            events=self.events,
            pin=pin,
            streams=streams,
            cancel_event=cancel_event,
            digest_cache=self.digest_cache,
        )

        active = ActiveTransfer(
            transfer_id=session.transfer_id,
            direction="send",
            peer=control.peer.info,
            stats=session.stats,
            items=session.items,
            session=session,
        )
        self._register_active(active)
        self.events.emit(
            "send_offering",
            transfer_id=session.transfer_id,
            peer=control.peer.info,
            files=len(entries),
            total=session.total_bytes,
        )

        try:
            answer = session.offer()
        except TransferFailed as exc:
            session.error = str(exc)
            control.abort()
            outcome = SendOutcome.failure(
                "rejected",
                detail=str(exc),
                peer=control.peer.info,
                transfer_id=session.transfer_id,
                status="rejected",
                total_files=len(entries),
            )
            self._remember_send_outcome(outcome, peer)
            self.events.emit(
                "send_finished",
                transfer_id=session.transfer_id,
                peer=control.peer.info,
                status="rejected",
                bytes=0,
                error=str(exc),
                **failure_info(exc, code="rejected"),
            )
            self._unregister_active(session.transfer_id)
            return outcome
        except (ConnectionClosed, ProtocolError, HandshakeError, OSError) as exc:
            # The connection died while the offer was in flight -- the peer
            # crashed, or something on the path reset it.  That is a *failed
            # send*, not a crash: the caller (desktop window, CLI, a phone that
            # walked out of Wi-Fi range) has to get an answer it can show.
            # Found by the resilience test, which kills connections at random
            # and occasionally landed on exactly this window.
            session.error = f"the connection failed before the peer answered: {exc}"
            control.abort()
            outcome = SendOutcome.failure(
                classify_failure(exc),
                detail=session.error,
                peer=control.peer.info,
                transfer_id=session.transfer_id,
                total_files=len(entries),
            )
            self._remember_send_outcome(outcome, peer)
            self.events.emit(
                "send_finished",
                transfer_id=session.transfer_id,
                peer=control.peer.info,
                status="failed",
                bytes=0,
                error=session.error,
                **failure_info(exc),
            )
            self._unregister_active(session.transfer_id)
            return outcome

        wanted = int(answer.get("streams", streams) or streams)
        wanted = max(1, min(16, wanted))

        # Let the session open more connections later if the receiver loses
        # some: a dropped link should cost a reconnect, not the transfer.
        session.stream_opener = lambda n: self._open_data_streams(peer, n)

        # Open the data connections in parallel with the handshake already
        # done; the receiver is waiting for exactly this many ATTACHes.
        data_conns = self._open_data_streams(peer, wanted, session.transfer_id)

        try:
            for index, conn in enumerate(data_conns):
                session.attach_stream(conn, index)
        except Exception as exc:
            session.error = f"could not attach data streams: {exc}"
            for conn in data_conns:
                conn.abort()

        ok = session.serve()
        active.error = session.error
        self._unregister_active(session.transfer_id)
        control.abort()
        if ok:
            outcome = SendOutcome(
                ok=True,
                peer_id=session.peer.device_id,
                peer_name=session.peer.name,
                transfer_id=session.transfer_id,
                status="done",
                bytes=session.stats.done_bytes,
                files=session.stats.done_files,
                total_files=len(entries),
            )
        elif session.failure_code == "cancelled":
            outcome = SendOutcome(
                ok=False,
                peer_id=session.peer.device_id,
                peer_name=session.peer.name,
                transfer_id=session.transfer_id,
                status="cancelled",
                bytes=session.stats.done_bytes,
                files=session.stats.done_files,
                total_files=len(entries),
                error="",
                error_code="cancelled",
                error_text=failure_text("cancelled"),
            )
        else:
            code = session.failure_code or classify_failure(session.error) or "unknown"
            outcome = SendOutcome.failure(
                code,
                detail=session.error,
                peer=session.peer,
                transfer_id=session.transfer_id,
                total_files=len(entries),
            )
            outcome.bytes = session.stats.done_bytes
            outcome.files = session.stats.done_files
        self._remember_send_outcome(outcome, peer)
        return outcome

    def _remember_send_outcome(self, outcome: SendOutcome, peer: Peer) -> None:
        """Keep the last outcome per peer so a ``bool`` caller can still ask why."""
        with self._active_lock:
            self._last_send_outcomes[outcome.peer_id or peer.info.device_id or peer.address] = outcome
            if outcome.transfer_id:
                self._send_outcomes[outcome.transfer_id] = outcome
                if len(self._send_outcomes) > 64:
                    oldest = next(iter(self._send_outcomes))
                    self._send_outcomes.pop(oldest, None)

    def last_send_outcome(self, device_id: str = "", *, transfer_id: str = "") -> dict[str, Any]:
        """The most recent :class:`SendOutcome`, as a dict (``{}`` if none).

        A UI that called ``send()`` and got ``False`` reads this for the reason;
        a UI that has a transfer id reads it by id (a retry for the same peer
        must not overwrite the row that is still on screen).
        """
        with self._active_lock:
            outcome = (
                self._send_outcomes.get(transfer_id)
                if transfer_id
                else self._last_send_outcomes.get(device_id)
            )
            if outcome is None and device_id:
                outcome = self._last_send_outcomes.get(device_id)
        return outcome.to_dict() if outcome is not None else {}

    def _open_control(self, peer: Peer) -> Connection:
        last_error: Exception | None = None
        candidates = [peer.address] + [a for a in peer.addresses if a != peer.address]
        for address in candidates:
            if not address or address.startswith("manual:"):
                continue
            try:
                sock = connect_to(address, peer.port, timeout=6.0)
            except (OSError, ConnectionError) as exc:
                last_error = exc
                continue
            conn = Connection(sock, self.identity, stream_id=0, encrypt=self.config.encrypt)
            try:
                conn.handshake_initiator(self.info)
            except (HandshakeError, ConnectionClosed, OSError) as exc:
                conn.abort()
                last_error = exc
                continue
            self.discovery.peers.add_direct(conn.peer.info, address, peer.port)

            return conn
        raise TransferFailed(f"cannot reach {peer.info.name} ({peer.address}:{peer.port}): {last_error}")

    def _open_data_streams(self, peer: Peer, count: int, transfer_id: str = "") -> list[Connection]:
        """Open ``count`` extra connections, each already handshaken."""
        results: list[Connection | None] = [None] * count
        errors: list[str] = []

        def open_one(slot: int) -> None:
            try:
                sock = connect_to(peer.address, peer.port, timeout=6.0)
                conn = Connection(sock, self.identity, stream_id=slot + 1, encrypt=self.config.encrypt)
                conn.handshake_initiator(self.info)
                results[slot] = conn
            except Exception as exc:
                errors.append(str(exc))

        threads = [
            threading.Thread(target=open_one, args=(i,), name=f"dial-{i}", daemon=True)
            for i in range(count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15.0)

        return [c for c in results if c is not None]

    # ------------------------------------------------------------------
    # receiving
    # ------------------------------------------------------------------

    def _on_offer(self, conn: Connection, offer: dict[str, Any]) -> None:
        """Called by the server on its own thread when an OFFER arrives."""
        thread = threading.Thread(
            target=self._run_receive,
            args=(conn, offer),
            name=f"recv-{offer.get('transferId', '?')[:8]}",
            daemon=True,
        )
        thread.start()

    def _run_receive(self, conn: Connection, offer: dict[str, Any]) -> None:
        peer_info = conn.peer.info
        transfer_id = str(offer.get("transferId", ""))
        try:
            session = ReceiveSession(
                control=conn,
                offer=offer,
                identity=self.identity,
                save_dir=self.config.receive_dir,
                events=self.events,
                accept_pin=self.config.pin,
                resume=self.config.resume,
            )
        except Exception as exc:
            self.events.emit(
                "transfer_rejected",
                transfer_id=transfer_id,
                reason=str(exc),
                **failure_info(exc, code="disk_full" if is_disk_full(exc) else "io_error"),
            )
            conn.abort()
            return

        with self._sessions_lock:
            self._sessions[transfer_id] = session

        if peer_info.device_id and peer_info.device_id != self.info.device_id:
            # An inbound connection from *this* device (the browser UI sending
            # to the computer it runs on, i.e. the self peer) must not add a row
            # for ourselves: "同一台设备在列表里永远只有一行" starts with not
            # listing ourselves at all.
            #
            # The port is the peer's own (it announced it in HELLO), not ours:
            # assuming both ends listen on the same number is fine until one of
            # them fell back to a random port -- and then every message to that
            # device is dialled at the wrong port and silently fails.
            self.discovery.peers.add_direct(
                peer_info, conn.peer_address(), peer_info.tcp_port or self.info.tcp_port
            )

        try:
            decision = session.decide()
        except Exception as exc:
            session.reject(f"cannot prepare destination: {exc}")
            self._forget_session(transfer_id)
            conn.abort()
            return

        if not decision.pin_ok:
            session.reject("PIN required or incorrect", status="pin_required")
            self._forget_session(transfer_id)
            conn.abort()
            return

        # Consent: the listener decides.  Auto-accept covers trusted devices and
        # the explicit "accept everything" mode; otherwise the UI is asked and
        # answers through :meth:`Engine.resolve_offer`.
        if self._should_auto_accept(peer_info, offer, conn.peer_address()):
            decision = self._apply_auto_accept(session, decision)
        else:
            request_id = transfer_id or f"offer-{int(time.time() * 1000)}"
            with self._sessions_lock:
                self._pending_offers[request_id] = (conn, offer, session)
            self.events.emit(
                "offer_received",
                request_id=request_id,
                transfer_id=transfer_id,
                peer=peer_info,
                files=[
                    {
                        "index": item.entry.index,
                        "name": item.entry.name,
                        "size": item.entry.size,
                        "resumed": item.done_bytes,
                    }
                    for item in session.items.values()
                ],
                total=session.total_bytes,
                resume_bytes=sum(p.done_bytes for p in session.items.values()),
                authenticated=bool(conn.peer and conn.peer.authenticated),
                sas=conn.peer.sas if conn.peer else "",
            )
            return

        self._execute_receive(session, decision, conn, transfer_id)

    @staticmethod
    def _is_loopback(address: str) -> bool:
        """这个连接是不是从本机发起的（回环）。"""
        text = str(address or "").split("%")[0]
        if text.startswith("::ffff:"):
            text = text[len("::ffff:"):]
        if text in ("::1", "localhost"):
            return True
        return text.startswith("127.")

    def _should_auto_accept(
        self, peer_info: DeviceInfo, offer: dict[str, Any], address: str = ""
    ) -> bool:
        if self.config.pin and str(offer.get("pin", "")) != self.config.pin:
            # A wrong PIN must never be auto-accepted; let the UI say so.
            return False
        if self.config.auto_accept_all:
            return True
        if (
            peer_info.device_id
            and peer_info.device_id == self.info.device_id
            and self._is_loopback(address)
        ):
            # 发给"我自己"：手机连着这台电脑、在它的页面上点了发送，文件从本机
            # 回环传回这台电脑自己（web 端的 self_peer）。这是用户刚刚亲手做的
            # 动作，再弹一个"要不要接收"，文件就卡在 .part 里 —— 用户那边看到的
            # 是"手机说发出去了，电脑上什么都没收到"。
            # **必须同时是回环**：只在设备号上放行的话，局域网里任何一台机器只要
            # 谎报自己是这台电脑，就能绕开"接收前先问我"这道确认。
            return True
        return self.config.auto_accept_trusted and peer_info.device_id in self._trusted

    def _apply_auto_accept(self, session: ReceiveSession, decision: OfferDecision) -> OfferDecision:
        return decision

    def resolve_offer(
        self,
        request_id: str,
        accept: bool,
        *,
        accepted_indices: list[int] | None = None,
        trust: bool = False,
    ) -> bool:
        """Answer a pending offer (called from the UI)."""
        with self._sessions_lock:
            pending = self._pending_offers.pop(request_id, None)
        if pending is None:
            return False
        conn, offer, session = pending
        transfer_id = session.transfer_id

        if not accept:
            session.reject("declined by the user", status="declined")
            self._forget_session(transfer_id)
            conn.abort()
            return True

        decision = session.decide()
        if accepted_indices is not None:
            wanted = set(accepted_indices)
            for index in list(session._parts):  # noqa: SLF001 - same package
                if index not in wanted:
                    session._parts[index].discard()  # noqa: SLF001
                    del session._parts[index]  # noqa: SLF001
            decision.accepted = [i for i in decision.accepted if i in wanted]
        if trust:
            self.trust(session.peer.device_id, True)
        # Hand the transfer to its own thread instead of running it here.
        # ``resolve_offer`` is called *by the UI*: the Qt slot behind the
        # accept dialog, and the HTTP handler serving ``/api/offer/respond``.
        # Running the whole receive inline froze the desktop window until the
        # last byte arrived (no progress, no cancel button -- for a 10 GB file
        # that is minutes of a dead UI) and held the phone's POST open for just
        # as long, which every proxy and every doze-happy Android browser
        # eventually kills.  The caller already learns the outcome from the
        # event stream, so it only needs to know the offer was taken.
        threading.Thread(
            target=self._execute_receive,
            args=(session, decision, conn, transfer_id),
            name=f"recv-{transfer_id[:8]}",
            daemon=True,
        ).start()
        return True

    def _execute_receive(
        self,
        session: ReceiveSession,
        decision: OfferDecision,
        conn: Connection,
        transfer_id: str,
    ) -> None:
        active = ActiveTransfer(
            transfer_id=transfer_id,
            direction="receive",
            peer=session.peer,
            stats=session.stats,
            items=session.items,
            session=session,
        )
        self._register_active(active)
        try:
            session.run(decision, decision.streams)
        except TransferCancelled:
            pass
        except Exception as exc:  # pragma: no cover - defensive
            session.error = str(exc)
            self.events.emit(
                "transfer_finished",
                transfer_id=transfer_id,
                status="failed",
                error=str(exc),
                **failure_info(exc),
            )
        finally:
            active.error = session.error
            self._unregister_active(transfer_id)
            self._forget_session(transfer_id)
            conn.abort()

    def _on_attach(self, conn: Connection, info: dict[str, Any]) -> bool:
        """Bind an incoming data connection to its session."""
        transfer_id = str(info.get("t", ""))
        with self._sessions_lock:
            session = self._sessions.get(transfer_id)
            if session is None and transfer_id in self._recently_finished:
                # Raced completion: accept the connection so no error frame is
                # sent, then drop it.  The sender is already finishing up.
                threading.Thread(target=conn.abort, daemon=True).start()
                return True
        if session is None:
            return False
        claimed = str(info.get("sender", ""))
        actual = conn.peer.info.device_id if conn.peer else ""
        if claimed and actual and claimed != actual:
            # An unrelated device must not be able to inject chunks.
            return False
        session.attach(conn)
        return True

    # ------------------------------------------------------------------
    # session management
    # ------------------------------------------------------------------

    def _forget_session(self, transfer_id: str) -> None:
        with self._sessions_lock:
            self._sessions.pop(transfer_id, None)
            if transfer_id:
                now = time.monotonic()
                self._recently_finished[transfer_id] = now
                # Keep the window short; it only has to cover a reconnect that
                # was already in flight.
                if len(self._recently_finished) > 64:
                    cutoff = now - 120.0
                    for key in [k for k, v in self._recently_finished.items() if v < cutoff]:
                        del self._recently_finished[key]

    def _register_active(self, active: ActiveTransfer) -> None:
        with self._active_lock:
            self._active[active.transfer_id] = active

    def _unregister_active(self, transfer_id: str) -> None:
        with self._active_lock:
            self._active.pop(transfer_id, None)

    def active_transfers(self) -> list[ActiveTransfer]:
        with self._active_lock:
            return list(self._active.values())

    def cancel(self, transfer_id: str, reason: str = "cancelled by user") -> bool:
        """Stop a transfer because the user asked for it.

        This is the "give up" path, never the "pause" path: a receive session
        is told to *discard* its part file, because the preallocated
        ``.eversend.part`` is the size of the whole file and leaving it behind
        for a resume nobody asked for is how a cancelled 10 GB transfer used to
        sit on 10 GB of disk forever.  ``stop()`` is the path that keeps
        partial files, so an interrupted transfer still resumes.
        """
        with self._sessions_lock:
            pending = self._pending_offers.pop(transfer_id, None)
        if pending is not None:
            conn, _offer, session = pending
            session.reject(reason)
            self._forget_session(session.transfer_id)
            conn.abort()
            return True
        with self._active_lock:
            active = self._active.get(transfer_id)
        if active is None:
            return False
        try:
            active.session.cancel(reason, discard=True)
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------
    # trust persistence
    # ------------------------------------------------------------------

    @property
    def _trust_path(self) -> str:
        return os.path.join(self.config.data_dir, "trusted.json")

    def _load_trusted(self) -> None:
        import json

        try:
            with open(self._trust_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._trusted = {str(x) for x in data.get("devices", [])}
        except (OSError, ValueError):
            self._trusted = set()

    def _save_trusted(self) -> None:
        import json

        try:
            tmp = self._trust_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"devices": sorted(self._trusted)}, fh, indent=2)
            os.replace(tmp, self._trust_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _kind() -> str:
    from .model import device_kind

    return device_kind()


def _version() -> str:
    from .constants import APP_VERSION

    return APP_VERSION


def build_file_entries(paths: Iterable[str]) -> tuple[list[FileEntry], dict[int, str]]:
    """Turn a list of files/directories into offer entries.

    Directories are walked and their files offered with a relative sub-path, so
    the receiver recreates the tree.  Symlinks are followed only when they
    point at regular files, and special files (sockets, devices, FIFOs) are
    skipped -- they cannot be transferred meaningfully and would hang a reader.
    """
    entries: list[FileEntry] = []
    sources: dict[int, str] = {}
    index = 0

    for path in paths:
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(path):
            base = os.path.dirname(path)
            for root, dirs, files in os.walk(path, followlinks=False):
                dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
                for name in sorted(files):
                    full = os.path.join(root, name)
                    if not os.path.isfile(full):
                        continue
                    rel = os.path.relpath(root, base)
                    entry = _make_entry(index, name, full, "" if rel == "." else rel)
                    if entry is None:
                        continue
                    entries.append(entry)
                    sources[index] = full
                    index += 1
        elif os.path.isfile(path):
            entry = _make_entry(index, os.path.basename(path), path, "")
            if entry is None:
                continue
            entries.append(entry)
            sources[index] = path
            index += 1

    return entries, sources


def _make_entry(index: int, name: str, full: str, rel_dir: str) -> FileEntry | None:
    try:
        stat = os.stat(full)
    except OSError:
        return None
    if not os.path.isfile(full):
        return None
    from .hashing import file_digest

    digest = ""
    # Hashing is deferred: computing it here would stall the offer for as long
    # as it takes to read every byte.  The digest is filled in by
    # :func:`compute_digests` while the receiver is deciding.
    return FileEntry(
        index=index,
        name=name,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        rel_dir=rel_dir,
        mode=stat.st_mode & 0o777,
        digest=digest,
    )


def compute_digests(entries: list[FileEntry], sources: dict[int, str], on_progress=None) -> None:
    """Fill in the whole-file digests for an offer.

    Runs in a worker so the offer can be displayed immediately while the
    digests are still being computed (a 100 GB set takes a while to hash).
    """
    from .hashing import file_digest

    for entry in entries:
        path = sources.get(entry.index)
        if not path:
            continue
        try:
            entry.digest = file_digest(path)
        except OSError:
            entry.digest = ""
        if on_progress is not None:
            on_progress(entry)


def free_space(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


__all__ = [
    "ActiveTransfer",
    "Engine",
    "EngineConfig",
    "EventBus",
    "build_file_entries",
    "compute_digests",
    "free_space",
]
