"""Device discovery.

Four independent channels run at once, because no single one works everywhere:

1. **UDP multicast** (239.255.83.68) -- the fast path.  One datagram reaches
   every listener on the link.
2. **UDP broadcast** (255.255.255.255 and each interface's directed broadcast)
   -- covers switched networks where IGMP snooping has pruned the multicast
   group, which is the single most common reason "the other device never
   shows up".
3. **Active subnet scan** -- a TCP connect to every host of the local /24.
   Slow, but it works when *all* broadcast traffic is filtered, which is
   exactly the situation on many corporate and hotel networks.
4. **Manual entry / QR code** -- for the cases where even that fails (client
   isolation, VPN-only paths, firewall rules).

Announcements are datagrams, never answered over UDP: a device that hears an
announcement records it and, if it wants to talk, opens a TCP connection.  That
keeps discovery stateless, so a lost packet costs nothing but a later retry.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .constants import (
    ANNOUNCE_DELAYS,
    ANNOUNCE_INTERVAL,
    DEFAULT_DISCOVERY_PORT,
    DEVICE_TTL,
    MAX_CONTROL_PAYLOAD,
    MDNS_GROUP_V4,
    MDNS_PORT,
    MDNS_SERVICE_TYPE,
    MULTICAST_GROUP_V4,
    MULTICAST_GROUP_V6,
)
from .model import DeviceInfo, Peer
from .sockutil import (
    Interface,
    create_broadcast_listener,
    create_broadcast_sender,
    create_multicast_listener,
    create_multicast_sender,
    list_interfaces,
)

#: Protocol tag so foreign traffic on our port is ignored.
DISCOVERY_TAG = "eversend/1"

#: Never build a datagram larger than this: it must survive any MTU without
#: IP fragmentation, which is unreliable for broadcast/multicast.
MAX_DATAGRAM = 1200


@dataclass(slots=True)
class Announcement:
    """The payload of one discovery datagram."""

    device_id: str
    name: str
    kind: str
    platform: str
    version: str
    port: int
    web_port: int
    timestamp: float

    @classmethod
    def from_device(cls, info: DeviceInfo) -> Announcement:
        return cls(
            device_id=info.device_id,
            name=info.name,
            kind=info.kind,
            platform=info.platform,
            version=info.version,
            port=info.tcp_port,
            web_port=info.web_port,
            timestamp=time.time(),
        )

    def to_bytes(self) -> bytes:
        payload = {
            "t": DISCOVERY_TAG,
            "id": self.device_id,
            "n": self.name[:64],
            "k": self.kind,
            "p": self.platform,
            "v": self.version,
            "port": self.port,
            "web": self.web_port,
            "ts": int(self.timestamp),
        }
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    @classmethod
    def parse(cls, data: bytes) -> Announcement | None:
        try:
            payload = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("t") != DISCOVERY_TAG:
            return None
        device_id = str(payload.get("id", ""))
        if not device_id:
            return None
        return cls(
            device_id=device_id,
            name=str(payload.get("n", "unknown"))[:64],
            kind=str(payload.get("k", "desktop")),
            platform=str(payload.get("p", "unknown")),
            version=str(payload.get("v", "")),
            port=int(payload.get("port", 0) or 0),
            web_port=int(payload.get("web", 0) or 0),
            timestamp=float(payload.get("ts", 0) or 0),
        )


class PeerStore:
    """Thread-safe registry of discovered devices."""

    def __init__(self, ttl: float = DEVICE_TTL) -> None:
        self.ttl = ttl
        self._peers: dict[str, Peer] = {}
        self._lock = threading.RLock()

    def upsert(self, announcement: Announcement, address: str, source: str) -> tuple[Peer, bool]:
        """Insert or refresh a peer.  Returns ``(peer, is_new)``."""
        key = announcement.device_id
        with self._lock:
            existing = self._peers.get(key)
            if existing is None:
                info = DeviceInfo(
                    device_id=announcement.device_id,
                    name=announcement.name,
                    kind=announcement.kind,
                    platform=announcement.platform,
                    version=announcement.version,
                    tcp_port=announcement.port,
                    web_port=announcement.web_port,
                )
                peer = Peer(info=info, address=address, port=announcement.port, source=source)
                peer.addresses = [address]
                self._peers[key] = peer
                return peer, True

            existing.touch(address, announcement.port)
            existing.source = source
            existing.info.name = announcement.name
            existing.info.kind = announcement.kind
            existing.info.platform = announcement.platform
            existing.info.version = announcement.version
            existing.info.web_port = announcement.web_port
            if address not in existing.addresses:
                existing.addresses.append(address)
                del existing.addresses[8:]  # keep the list bounded
            return existing, False

    def add_direct(
        self, info: DeviceInfo, address: str, port: int, source: str = "incoming"
    ) -> tuple[Peer, bool]:
        """Register a peer learned from an inbound connection or a probe.

        Returns ``(peer, is_new)``.  Keyed by device id like every other path,
        so a peer found by multicast, broadcast, mDNS and a subnet scan collapses
        into one entry rather than four.
        """
        with self._lock:
            peer = self._peers.get(info.device_id)
            if peer is None:
                peer = Peer(info=info, address=address, port=port, source=source)
                peer.addresses = [address]
                self._peers[info.device_id] = peer
                return peer, True
            peer.info = info
            if address and address not in peer.addresses:
                peer.addresses.append(address)
                del peer.addresses[8:]
            peer.touch(address, port)
            return peer, False

    def set_trusted(self, device_id: str, trusted: bool) -> None:
        with self._lock:
            peer = self._peers.get(device_id)
            if peer is not None:
                peer.trusted = trusted

    def get(self, device_id: str) -> Peer | None:
        with self._lock:
            return self._peers.get(device_id)

    def all(self, include_expired: bool = False) -> list[Peer]:
        with self._lock:
            peers = list(self._peers.values())
        if not include_expired:
            peers = [p for p in peers if p.is_alive(self.ttl)]
        peers.sort(key=lambda p: (not p.trusted, p.info.name.lower()))
        return peers

    def expire(self) -> list[Peer]:
        """Drop peers that have gone quiet.  Returns the ones removed."""
        with self._lock:
            gone = [p for p in self._peers.values() if not p.is_alive(self.ttl)]
            for peer in gone:
                self._peers.pop(peer.key, None)
        return gone


class DiscoveryService:
    """Runs all discovery channels for one device."""

    def __init__(
        self,
        *,
        device_info: Callable[[], DeviceInfo],
        events: Any,
        port: int = DEFAULT_DISCOVERY_PORT,
        group: str = MULTICAST_GROUP_V4,
        enable_broadcast: bool = True,
        enable_mdns: bool = True,
    ) -> None:
        self.device_info = device_info
        self.events = events
        self.port = port
        self.group = group
        self.enable_broadcast = enable_broadcast
        self.enable_mdns = enable_mdns

        self.peers = PeerStore()
        self._interfaces: list[Interface] = []
        #: address -> last time we answered it (see :meth:`_maybe_reply`).
        self._replies: dict[str, float] = {}
        self._reply_lock = threading.Lock()
        self._multicast_rx: list[socket.socket] = []
        self._broadcast_rx: socket.socket | None = None
        self._mdns_rx: socket.socket | None = None
        self._senders: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._announce_now = threading.Event()
        self._last_announce = 0.0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._interfaces = list_interfaces(include_virtual=True, include_loopback=False)

        self._multicast_rx = create_multicast_listener(
            self.group, self.port, self._interfaces, reuse_port=True
        )
        if not self._multicast_rx:
            self.events.emit(
                "discovery_warning",
                message="multicast could not be joined; falling back to broadcast and scanning",
            )

        if self.enable_broadcast:
            self._broadcast_rx = create_broadcast_listener(self.port)
            if self._broadcast_rx is None:
                self.events.emit(
                    "discovery_warning", message="the broadcast discovery port is already in use"
                )

        if self.enable_mdns:
            self._mdns_rx = self._open_mdns_socket()

        for iface in self._interfaces:
            sender = create_multicast_sender(self.group, self.port, iface)
            if sender is not None:
                self._senders.append(sender)
            if self.enable_broadcast:
                bsend = create_broadcast_sender(iface)
                if bsend is not None:
                    self._senders.append(bsend)

        for sock in self._multicast_rx:
            self._spawn(self._receive_loop, sock, "multicast")
        if self._broadcast_rx is not None:
            self._spawn(self._receive_loop, self._broadcast_rx, "broadcast")
        if self._mdns_rx is not None:
            self._spawn(self._mdns_loop, self._mdns_rx)

        self._spawn(self._announce_loop)

        self.events.emit(
            "discovery_started",
            interfaces=len(self._interfaces),
            multicast=len(self._multicast_rx),
            broadcast=self._broadcast_rx is not None,
        )

    def stop(self) -> None:
        self._stop.set()
        self._announce_now.set()
        for sock in (
            self._multicast_rx
            + ([self._broadcast_rx] if self._broadcast_rx else [])
            + ([self._mdns_rx] if self._mdns_rx else [])
            + self._senders
        ):
            try:
                sock.close()
            except OSError:
                pass
        self._multicast_rx = []
        self._broadcast_rx = None
        self._mdns_rx = None
        self._senders = []
        for thread in self._threads:
            thread.join(timeout=1.0)
        self._threads = []
        self.events.emit("discovery_stopped")

    def _spawn(self, target: Callable, *args: Any) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        self._threads.append(thread)

    # -- announcing --------------------------------------------------------

    def announce(self, burst: bool = True) -> None:
        """Send an announcement (a short burst by default)."""
        if burst:
            thread = threading.Thread(
                target=self._announce_burst, name="announce-burst", daemon=True
            )
            thread.start()
        else:
            self._send_announcement()

    def _announce_burst(self) -> None:
        started = time.monotonic()
        for delay in ANNOUNCE_DELAYS:
            wait = delay - (time.monotonic() - started)
            if wait > 0 and self._stop.wait(wait):
                return
            self._send_announcement()

    def _send_announcement(self) -> None:
        payload = Announcement.from_device(self.device_info()).to_bytes()
        if len(payload) > MAX_DATAGRAM:  # pragma: no cover - defensive
            return
        targets: list[tuple[socket.socket, tuple]] = []

        if self._interfaces:
            sender_index = 0
            for iface in self._interfaces:
                if sender_index < len(self._senders):
                    sock = self._senders[sender_index]
                    targets.append((sock, (self.group, self.port)))
                    sender_index += 1
                if self.enable_broadcast and sender_index < len(self._senders):
                    sock = self._senders[sender_index]
                    targets.append((sock, ("255.255.255.255", self.port)))
                    directed = iface.broadcast
                    if directed:
                        targets.append((sock, (directed, self.port)))
                    sender_index += 1
        else:
            for sock in self._senders:
                targets.append((sock, (self.group, self.port)))
                if self.enable_broadcast:
                    targets.append((sock, ("255.255.255.255", self.port)))

        for sock, target in targets:
            try:
                sock.sendto(payload, target)
            except OSError:
                continue

        self._last_announce = time.monotonic()
        self.events.emit("announced", targets=len(targets))

    def _announce_loop(self) -> None:
        # First burst at startup so peers see us immediately.
        self._announce_burst()
        while not self._stop.is_set():
            if self._announce_now.wait(ANNOUNCE_INTERVAL):
                self._announce_now.clear()
                continue
            if self._stop.is_set():
                return
            self._send_announcement()
            self.peers.expire()

    # -- receiving ---------------------------------------------------------

    def _receive_loop(self, sock: socket.socket, source_kind: str) -> None:
        sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(MAX_CONTROL_PAYLOAD)
            except socket.timeout:
                continue
            except OSError:
                return

            announcement = Announcement.parse(data)
            if announcement is None:
                continue
            if announcement.device_id == self.device_info().device_id:
                continue  # our own announcement, looped back

            # A multicast listener reports the *interface* address as the
            # source on some platforms; the packet's real origin is in addr.
            address = addr[0]
            if address.startswith("::ffff:"):
                address = address[7:]

            # A phone running the Android app, or any client that only speaks
            # HTTP, announces itself with no TCP port.  It is not a protocol
            # peer, so it must not enter the peer list (a peer with port 0 would
            # be un-dialable); the web layer turns this event into a paired
            # client instead.
            if announcement.port <= 0 or announcement.kind == "mobile":
                self.events.emit(
                    "mobile_found",
                    device_id=announcement.device_id,
                    name=announcement.name,
                    # 不能再叫 kind：emit(kind, **payload) 的第一个参数已经占了它。
                    device_kind=announcement.kind,
                    platform=announcement.platform,
                    version=announcement.version,
                    address=address,
                )
                self._maybe_reply(addr)
                continue

            peer, is_new = self.peers.upsert(announcement, address, source_kind)
            self.events.emit(
                "device_found" if is_new else "device_updated",
                peer=peer,
                is_new=is_new,
            )
            # Answer a device we did not know about, so its own "search for
            # computers" finishes immediately instead of waiting up to
            # ANNOUNCE_INTERVAL (30 s) for our next broadcast.
            if is_new:
                self._maybe_reply(addr)

    #: How often we answer a single address, in seconds.  A broadcast burst
    #: from several devices must not turn into an amplification loop.
    REPLY_COOLDOWN = 1.0

    def _maybe_reply(self, addr: tuple) -> None:
        """Send our announcement straight back to whoever just spoke.

        The periodic broadcast is every 30 seconds, which is far too slow for a
        phone that just tapped "搜索电脑"; a unicast answer arrives immediately,
        and because it goes to the *source port* it reaches a client that is not
        listening on our discovery port at all.
        """
        try:
            address = addr[0]
        except (IndexError, TypeError):
            return
        now = time.monotonic()
        # 两个接收线程会同时到达这里，用一个专门的锁保护这张表（原来的实现没有）。
        with self._reply_lock:
            last = self._replies.get(address, 0.0)
            if now - last < self.REPLY_COOLDOWN:
                return
            self._replies[address] = now
            if len(self._replies) > 256:
                cutoff = now - 60.0
                for key in [k for k, v in self._replies.items() if v < cutoff]:
                    del self._replies[key]
        payload = Announcement.from_device(self.device_info()).to_bytes()
        for sock in list(self._senders):
            try:
                sock.sendto(payload, addr)
                return
            except OSError:
                continue

    # -- mDNS --------------------------------------------------------------

    def _open_mdns_socket(self) -> socket.socket | None:
        """Join 224.0.0.251 so peers that only speak mDNS are still found.

        Some access points forward mDNS reliably while dropping arbitrary
        multicast groups, which makes this worth the extra socket.
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("", MDNS_PORT))
            joined = False
            for iface in self._interfaces:
                if iface.is_ipv6:
                    continue
                try:
                    membership = socket.inet_aton(MDNS_GROUP_V4) + socket.inet_aton(iface.address)
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
                    joined = True
                except OSError:
                    continue
            if not joined:
                sock.close()
                return None
            sock.settimeout(0.5)
            return sock
        except OSError:
            return None

    def _mdns_loop(self, sock: socket.socket) -> None:
        """Parse mDNS responses for our service type.

        A deliberately small implementation: we only look for PTR records
        naming :data:`MDNS_SERVICE_TYPE` and pull the SRV target plus the TXT
        record that carries our announcement fields.  Full DNS name
        compression is handled because real responders use it.
        """
        from . import mdns

        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                records = mdns.parse_records(data)
            except Exception:
                continue

            # The TCP port lives in the SRV record while the identity lives in
            # TXT, so index the SRVs by instance name first.
            srv_ports: dict[str, int] = {}
            for record in records:
                if record.rtype == mdns.TYPE_SRV and record.port:
                    srv_ports[record.name] = record.port

            for record in records:
                if record.rtype != mdns.TYPE_TXT:
                    continue
                if MDNS_SERVICE_TYPE.split(".")[0] not in record.name:
                    continue
                announcement = mdns.announcement_from_txt(record.rdata, addr[0])
                if announcement is None or announcement.device_id == self.device_info().device_id:
                    continue
                if not announcement.port:
                    announcement.port = srv_ports.get(record.name, 0)
                if not announcement.port:
                    continue  # nothing to dial: ignore the record
                peer, is_new = self.peers.upsert(announcement, addr[0], "mdns")
                self.events.emit("device_found" if is_new else "device_updated", peer=peer, is_new=is_new)

    def advertise_mdns(self) -> None:
        """Answer mDNS queries so other implementations can find us."""
        from . import mdns

        info = self.device_info()
        packet = mdns.build_announcement(
            instance=f"{info.name}-{info.device_id[:6]}",
            service_type=MDNS_SERVICE_TYPE,
            host=f"{info.name}.local",
            port=info.tcp_port,
            txt={
                "id": info.device_id,
                "n": info.name[:64],
                "k": info.kind,
                "p": info.platform,
                "v": info.version,
                "web": str(info.web_port),
            },
        )
        for iface in self._interfaces:
            if iface.is_ipv6:
                continue
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface.address))
                sock.sendto(packet, (MDNS_GROUP_V4, MDNS_PORT))
                sock.close()
            except OSError:
                continue

    # -- active probing ----------------------------------------------------

    def scan_subnets(self, port: int, *, concurrency: int = 64, timeout: float = 0.6) -> int:
        """Probe every host of every local /24 for a listening peer.

        The fallback of last resort: it costs a few hundred TCP SYNs but finds
        peers on networks that drop all broadcast and multicast traffic.
        """
        targets: list[str] = []
        for iface in self._interfaces:
            net = iface.network
            if net is None or net.num_addresses > 1024:
                continue
            for host in net.hosts():
                address = str(host)
                if host == ipaddress.ip_address(iface.address):
                    continue
                targets.append((address, iface.address))

        if not targets:
            return 0

        found = 0
        lock = threading.Lock()
        queue: list[tuple[str, str]] = list(targets)

        def worker() -> None:
            nonlocal found
            while True:
                with lock:
                    if not queue:
                        return
                    address, local = queue.pop()
                if self._probe(address, port, local, timeout):
                    with lock:
                        found += 1

        threads = [
            threading.Thread(target=worker, name=f"scan-{i}", daemon=True)
            for i in range(min(concurrency, len(targets)))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30.0)
        return found

    def _probe(self, address: str, port: int, local: str, timeout: float) -> bool:
        """TCP-connect to ``address``; if it answers like a EverSend peer, we
        learn its identity from its HTTP-free banner.

        A bare connect is not proof (any service could listen), so the peer is
        only recorded once it actually completes a handshake, which the engine
        does lazily when the user picks it.  Here we just report reachability
        by emitting an event; the UI shows it as "found by scan".
        """
        if self._stop.is_set():
            return False
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.bind((local, 0))
        except OSError:
            pass
        try:
            sock.settimeout(timeout)
            sock.connect((address, port))
        except OSError:
            try:
                sock.close()
            except OSError:
                pass
            return False
        try:
            sock.close()
        except OSError:
            pass
        self.events.emit("scan_hit", address=address, port=port)
        return True

    # -- direct ------------------------------------------------------------

    def add_manual(self, address: str, port: int, name: str = "") -> Peer:
        """Register a peer the user typed in (or scanned from a QR code)."""
        info = DeviceInfo(
            device_id=f"manual:{address}:{port}",
            name=name or address,
            tcp_port=port,
        )
        peer, _is_new = self.peers.add_direct(info, address, port, "manual")
        self.events.emit("device_found", peer=peer, is_new=True)
        return peer

    def interfaces(self) -> list[Interface]:
        return list(self._interfaces)


__all__ = ["Announcement", "DiscoveryService", "PeerStore"]
