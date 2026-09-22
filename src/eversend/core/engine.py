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

import os
import queue
import shutil
import threading
import time
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
from .discovery import DiscoveryService
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
    SendSession,
    TransferCancelled,
    TransferFailed,
)


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
    enable_broadcast: bool = True
    enable_mdns: bool = True
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
            capabilities={"resume": True, "web": config.enable_web, "streams": config.streams},
        )

        self.server = TransferServer(
            identity=self.identity,
            device_info=lambda: self.info,
            on_offer=self._on_offer,
            on_attach=self._on_attach,
            on_chat=self._on_chat,
            events=self.events,
            encrypt=config.encrypt,
        )
        self.discovery = DiscoveryService(
            device_info=lambda: self.info,
            events=self.events,
            port=config.discovery_port,
            enable_broadcast=config.enable_broadcast,
            enable_mdns=config.enable_mdns,
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
        self._trusted: set[str] = set()
        self._load_trusted()
        self._started = False

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
        # Clean up part files left behind by transfers abandoned long ago.
        try:
            from .chunkstore import ResumeIndex

            ResumeIndex(self.config.data_dir).sweep()
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
        return {
            "conversation": self.chat.conversation(conv_id) or {},
            "message": message,
            "source": source,
        }

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

    def deliver_chat(self, device_id: str, payload: dict[str, Any], *, timeout: float = 8.0) -> bool:
        """Hand one chat payload to a peer, over its own short-lived connection."""
        peer = next((p for p in self.devices() if p.info.device_id == device_id), None)
        if peer is None:
            return False
        conn: Connection | None = None
        try:
            sock = connect_to(peer.address, peer.port, timeout=timeout)
            conn = Connection(sock, self.identity, stream_id=0, encrypt=self.config.encrypt)
            conn.handshake_initiator(self.info)
            conn.send_json(MSG_CHAT, payload)
            conn.sock.settimeout(timeout)
            frame = conn.recv()
            return frame.type == MSG_CHAT_ACK
        except (OSError, HandshakeError, ConnectionClosed, ProtocolError) as exc:
            self.events.emit("chat_failed", peer=device_id, error=str(exc))
            return False
        finally:
            if conn is not None:
                conn.abort()

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
            media_size=os.path.getsize(media_path) if media_path and os.path.exists(media_path) else 0,
            media_mime=media_mime,
            duration_ms=duration_ms,
            direction="out",
            state="sending",
        )
        payload = self.chat_payload(conv, message)

        delivered = 0
        reachable = 0
        for member in member_list:
            if member == self.info.device_id or member.startswith("web:"):
                continue  # ourselves, or a browser client (it reads the store)
            reachable += 1
            if media_path:
                # Move the file first, into the folder both sides compute from
                # the conversation id; then the message can reference it.
                peer = next((p for p in self.devices() if p.info.device_id == member), None)
                if peer is None:
                    continue
                if not self.send(peer, [media_path], rel_dir=os.path.dirname(rel)):
                    continue
            if self.deliver_chat(member, payload):
                delivered += 1

        state = "sent" if (delivered or not reachable) else "failed"
        self.chat.set_state(message["id"], state)
        event = {
            "conversation": self.chat.conversation(conv_id) or {},
            "message": dict(message, state=state, mediaRel=rel, mediaName=media_name),
            "delivered": delivered,
            "reachable": reachable,
        }
        self.events.emit("chat_sent", **event)
        return event

    # ------------------------------------------------------------------
    # devices
    # ------------------------------------------------------------------

    def devices(self) -> list[Peer]:
        peers = self.discovery.peers.all()
        local = self._local_addresses()
        for peer in peers:
            peer.trusted = peer.info.device_id in self._trusted
            # Two instances on one machine have different identities (separate
            # data directories) and therefore stay separate rows, correctly.
            # Labelling them makes that obvious instead of looking like the
            # list is showing duplicates.
            peer.same_host = peer.address in local
        return peers

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
            error = ""
            try:
                found = self.discovery.scan_subnets(self.info.tcp_port)
            except Exception as exc:  # pragma: no cover - defensive
                error = str(exc)
            self.events.emit("scan_finished", found=int(found), error=error)

        threading.Thread(target=run, name="subnet-scan", daemon=True).start()
        return 0

    def add_manual_device(self, address: str, port: int = 0, name: str = "") -> Peer:
        return self.discovery.add_manual(address, port or self.info.tcp_port, name)

    def probe_address(self, address: str, port: int = 0, *, timeout: float = 4.0) -> Peer | None:
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
        try:
            sock = connect_to(address, port, timeout=timeout)
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
        """Offer and send ``paths`` to ``peer``.

        Blocks until the transfer finishes.  Runs the control connection on the
        calling thread and one thread per data stream.

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
            raise TransferFailed("nothing to send")

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
            self.events.emit(
                "send_finished",
                transfer_id="",
                peer=peer.info,
                status="failed",
                bytes=0,
                error=str(exc),
            )
            return False

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
            self.events.emit(
                "send_finished",
                transfer_id=session.transfer_id,
                peer=control.peer.info,
                status="rejected",
                bytes=0,
                error=str(exc),
            )
            self._unregister_active(session.transfer_id)
            return False
        except (ConnectionClosed, ProtocolError, HandshakeError, OSError) as exc:
            # The connection died while the offer was in flight -- the peer
            # crashed, or something on the path reset it.  That is a *failed
            # send*, not a crash: the caller (desktop window, CLI, a phone that
            # walked out of Wi-Fi range) has to get an answer it can show.
            # Found by the resilience test, which kills connections at random
            # and occasionally landed on exactly this window.
            session.error = f"the connection failed before the peer answered: {exc}"
            control.abort()
            self.events.emit(
                "send_finished",
                transfer_id=session.transfer_id,
                peer=control.peer.info,
                status="failed",
                bytes=0,
                error=session.error,
            )
            self._unregister_active(session.transfer_id)
            return False

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
        return ok

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
            self.events.emit("transfer_rejected", transfer_id=transfer_id, reason=str(exc))
            conn.abort()
            return

        with self._sessions_lock:
            self._sessions[transfer_id] = session

        self.discovery.peers.add_direct(peer_info, conn.peer_address(), self.info.tcp_port)

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
        if self._should_auto_accept(peer_info, offer):
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

    def _should_auto_accept(self, peer_info: DeviceInfo, offer: dict[str, Any]) -> bool:
        if self.config.pin and str(offer.get("pin", "")) != self.config.pin:
            # A wrong PIN must never be auto-accepted; let the UI say so.
            return False
        if self.config.auto_accept_all:
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
            self.events.emit("transfer_finished", transfer_id=transfer_id, status="failed", error=str(exc))
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
            active.session.cancel(reason)
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
