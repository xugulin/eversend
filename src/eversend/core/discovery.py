"""Device discovery.

Four independent channels run at once, because no single one works everywhere:

1. **UDP multicast** (239.255.83.68) -- the fast path.  One datagram reaches
   every listener on the link.
2. **UDP broadcast** (255.255.255.255 and each interface's directed broadcast)
   -- covers switched networks where IGMP snooping has pruned the multicast
   group, which is the single most common reason "the other device never
   shows up".
3. **Active subnet scan** -- a TCP connect to hosts of the local subnets.  It
   runs *by itself* when the passive channels have produced nothing, which is
   what makes "零手动" true on networks that filter every kind of broadcast
   (corporate, hotel, guest Wi-Fi with client isolation).
4. **Manual entry / QR code** -- for the cases where even that fails (client
   isolation, VPN-only paths, firewall rules).

Announcements are datagrams, never answered over UDP: a device that hears an
announcement records it and, if it wants to talk, opens a TCP connection.  That
keeps discovery stateless, so a lost packet costs nothing but a later retry.

Design notes that matter (each of these was a measured user-visible bug):

* **The first announcement goes out before anything else.**  Interface
  enumeration, listener setup and the mDNS advertisement all used to happen
  first, so the packet that lets the other side see us was hostage to work
  that has nothing to do with it.  Now: create the senders, shout, *then*
  build the listeners.  Charter §5.1 wants that packet within 300 ms.
* **The burst is spread out (0/1/2/5 s, then every 15 s).**  A front-loaded
  burst (0/0.25/1/2.5 s + every 30 s) is heard by nobody if the peer starts
  half a second after us: measured, the earliest a peer that booted 300 ms
  later could see us was *the 1-second packet*.  The steady interval is what
  bounds how long a device that joins late stays invisible, so it is 15 s.
* **``bye`` on shutdown.**  Waiting for a 90-second TTL to notice a closed
  laptop is not "offline", it is "haunted".
* **The scan registers peers, it does not just report open ports.**  A TCP
  connect proves *something* is listening; only a handshake yields the device
  id that the device list de-duplicates on.  Reporting "3 hosts answered" and
  leaving the user to type an IP is exactly the bug this file fixes.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import random
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
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

# ---------------------------------------------------------------------------
# Cadence (charter §5.1)
# ---------------------------------------------------------------------------

#: Announcement burst offsets in seconds, measured from the first packet.
#:
#: The values are a *discovery-layer policy*, not a wire agreement: two peers
#: never have to agree on when the other shouts, only on what a shout looks
#: like (see PROTOCOL.md §9.1).  The numbers themselves live in
#: ``constants.ANNOUNCE_DELAYS`` -- the documented single source of truth --
#: and are re-exported here because this is where the cadence is implemented.
#:
#: A late-booting peer is the reason the tail is long: it misses the first
#: packets entirely, so the 5-second one is what actually introduces us.
ANNOUNCE_SCHEDULE: tuple[float, ...] = tuple(ANNOUNCE_DELAYS)

#: How often a running device re-announces itself, in seconds
#: (``constants.ANNOUNCE_INTERVAL``: 15 s, charter §5.1).
ANNOUNCE_STEADY_INTERVAL: float = float(ANNOUNCE_INTERVAL)

#: After the "扫描" button (or a phone tapping "搜索电脑") we send a short
#: follow-up burst, so a peer that is *also* scanning does not have to wait for
#: the steady interval to hear us.
ANNOUNCE_BURST_ON_DEMAND: tuple[float, ...] = (0.0, 0.35, 1.0)

# ---------------------------------------------------------------------------
# Scan tuning
# ---------------------------------------------------------------------------

#: Parallel TCP connects during a subnet scan.  A /24 finishes in under a
#: second; the pool is what turns a 2 500-probe /8 pass into ~4 seconds.
#: Configurable through ``EngineConfig.scan_concurrency`` (a phone wants less).
SCAN_CONCURRENCY = 160

#: Upper bound on probes started per second, so a scan cannot flood a switch
#: (or trip an IDS) even on a /16.  Charter §5.2: throttle, do not flood.
#: 800/s for the ~4 seconds a pass lasts is a few thousand SYNs in total --
#: less than one ``nmap -sn`` of a /20 -- and the pass budget below is the hard
#: ceiling, so "throttled" never degrades into "unbounded".
SCAN_RATE_PER_SECOND = 800.0

#: Connect timeout for one probe.  A same-L2 host answers in microseconds; a
#: host that does not answer at all is what this bounds, so it is short.
SCAN_TIMEOUT = 0.30

#: Grace period after startup before the first automatic scan.  Multicast and
#: broadcast are faster when they work at all, so they get the first shot --
#: but a short one: the whole point is to be visible within 5 s when they do
#: not work, and both channels keep running while the scan does.
SCAN_START_GRACE = 0.6

#: Probe budget for one automatic pass.  The plan is *ordered by priority*
#: (our own /24 first), so a budget is not a coverage limit for the case that
#: matters -- it is what keeps a /8 from becoming a million SYNs.
SCAN_TIGHT_BUDGET = 2500
#: Deeper pass, run when the cheap one has come up empty more than once.
SCAN_DEEP_BUDGET = 6000

#: How long to wait before the next pass when nothing has been found yet.
SCAN_INTERVAL_ALONE = 20.0
#: ... and when at least one peer is already visible (keeps the list fresh
#: without hammering the network).
SCAN_INTERVAL_WITH_PEERS = 120.0
#: Backoff ceiling for repeated empty scans with no peers at all.
SCAN_INTERVAL_MAX = 180.0

#: Host offsets probed inside a foreign /24, in this order.  The low range is
#: where DHCP pools and static device addresses actually live, so it is
#: sampled densely; the rest of the /24 sparsely.
SCAN_SAMPLE_OFFSETS: tuple[int, ...] = (
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16,
    20, 24, 32, 48, 64, 96, 128, 160, 192, 224, 240, 250, 254,
)

#: Foreign /24s that get the dense sample (they are the nearest ones, i.e. the
#: ones a device on the same switch is most likely to be in).
SCAN_NEAR_SEGMENTS = 8
#: Foreign /24s sampled per pass in the sparse sweep.  Capped so that even a
#: /8 finishes its whole pass inside the 5-second discovery promise.
SCAN_SWEEP_SEGMENTS = 384
#: Offsets sampled in a near /24 (the low range, where DHCP pools and static
#: addresses live) and in a far one.
SCAN_DENSE_SAMPLES = 16
SCAN_SPARSE_SAMPLES = 4
#: Foreign /24s walked completely, in the deep pass only.
SCAN_DEEP_SEGMENTS = 4
#: Ceiling on the *far* stage of one pass.  Without it the far stage is the
#: whole budget on a /8, and the pass stops fitting in five seconds -- the
#: near stage is what actually finds a peer, the far stage is insurance.
SCAN_FAR_PROBE_BUDGET = 1000

#: Cap on how many peers we remember after they go offline ("离线设备" and the
#: destination of a message queued for a device that is not there right now).
KNOWN_PEER_LIMIT = 256

#: How long after a ``bye`` an announcement for that device is ignored.
#:
#: Not superstition: a reply that was *already on the wire* when the peer shut
#: down arrives microseconds later, and applying it re-adds a laptop that just
#: left for a full 90-second TTL.  Two seconds cannot hide a genuine restart --
#: a restarting client announces at 0/1/2/5 s, so at worst the 5-second packet
#: introduces it again.
BYE_IGNORE_WINDOW = 2.0

#: How long a remembered offline peer stays eligible for an automatic retry.
KNOWN_PEER_TTL = 7 * 24 * 3600.0


# ---------------------------------------------------------------------------
# errors, in words a user can act on
# ---------------------------------------------------------------------------


def explain_socket_error(exc: BaseException | str) -> str:
    """Turn a socket error into a Chinese sentence a user can act on.

    The diagnostic card (charter E2) has to say *why* nothing was found; an
    English errno string is not an answer to "为什么我看不到对方".
    """
    text = str(exc or "")
    code = getattr(exc, "errno", None)
    if code is None:
        for name, number in errno.__dict__.items():
            if name.startswith("E") and isinstance(number, int) and name in text:
                code = number
                break
    if code == errno.ENETUNREACH or "unreachable" in text.lower():
        return "网络不可达（网卡没连上，或缺少到该网段的路由）"
    if code in (errno.EACCES, errno.EPERM) or "permission" in text.lower():
        return "权限不足（被防火墙或安全软件拦下了）"
    if code == errno.EADDRINUSE or "in use" in text.lower():
        return "端口被占用（可能已有一个韧传在运行）"
    if code == errno.ECONNREFUSED or "refused" in text.lower():
        return "连接被拒绝（对方没在监听这个端口）"
    if code == errno.EHOSTUNREACH:
        return "主机不可达（对方不在线，或被客户端隔离挡住）"
    if isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in text.lower():
        return "等待超时（对方防火墙在静默丢包）"
    if "network is down" in text.lower() or code == errno.ENETDOWN:
        return "网卡未启用"
    return text or "未知错误"


# ---------------------------------------------------------------------------
# announcements
# ---------------------------------------------------------------------------


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
    #: True for the "I am leaving" datagram sent on shutdown.  Receivers take
    #: the device out of the list immediately instead of waiting for the TTL.
    bye: bool = False
    #: Other addresses this device can be reached at (multi-homed hosts: an
    #: Ethernet box with Wi-Fi, a VPN and Docker bridges).  The datagram's
    #: source address is authoritative; these are fallbacks, and they are what
    #: stops "it announced itself from the wrong NIC" from becoming "type the
    #: IP by hand".
    addresses: tuple[str, ...] = ()
    #: Whether this device has a service that can *receive* files (``rx`` on the
    #: wire).  A browser/phone client has no listening socket at all, so there
    #: is nothing to push to: the sender must publish and wait instead
    #: (docs/REAL_DEVICE_FINDINGS.md §2.3).  Defaults to True for old peers,
    #: which had no way to say otherwise.
    accepts_files: bool = True

    @classmethod
    def from_device(
        cls,
        info: DeviceInfo,
        *,
        bye: bool = False,
        addresses: Iterable[str] = (),
        accepts_files: bool | None = None,
    ) -> "Announcement":
        seen: list[str] = []
        for address in addresses:
            text = str(address or "").strip()
            if text and ":" not in text and text not in seen:
                seen.append(text)
            if len(seen) >= 6:
                break
        return cls(
            device_id=info.device_id,
            name=info.name,
            kind=info.kind,
            platform=info.platform,
            version=info.version,
            port=info.tcp_port,
            web_port=info.web_port,
            timestamp=time.time(),
            bye=bye,
            addresses=tuple(seen),
            accepts_files=(
                bool((info.capabilities or {}).get("accept", True))
                if accepts_files is None
                else bool(accepts_files)
            ),
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
        if self.bye:
            payload["bye"] = 1
        if self.addresses:
            payload["a"] = list(self.addresses)
        # Additive field: "I can receive files".  An old peer ignores it and is
        # assumed able to receive, which is what it used to be.
        payload["rx"] = 1 if self.accepts_files else 0
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    @classmethod
    def parse(cls, data: bytes) -> "Announcement | None":
        try:
            payload = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("t") != DISCOVERY_TAG:
            return None
        device_id = str(payload.get("id", ""))
        if not device_id:
            return None
        raw_addresses = payload.get("a")
        addresses: tuple[str, ...] = ()
        if isinstance(raw_addresses, list):
            addresses = tuple(
                str(item) for item in raw_addresses[:6] if isinstance(item, str) and item
            )
        return cls(
            device_id=device_id,
            name=str(payload.get("n", "unknown"))[:64],
            kind=str(payload.get("k", "desktop")),
            platform=str(payload.get("p", "unknown")),
            version=str(payload.get("v", "")),
            port=int(payload.get("port", 0) or 0),
            web_port=int(payload.get("web", 0) or 0),
            timestamp=float(payload.get("ts", 0) or 0),
            bye=bool(payload.get("bye")),
            addresses=addresses,
            accepts_files=bool(payload.get("rx", 1)),
        )


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------


@dataclass
class ChannelStats:
    """Attempts / successes / last error for one discovery channel.

    The settings page (charter E2) shows these so a user can screenshot "组播
    收不到、广播收到 3 次、主动扫描成功 1 次" instead of guessing.
    """

    name: str
    label: str
    attempts: int = 0
    successes: int = 0
    errors: int = 0
    sent: int = 0
    received: int = 0
    last_error: str = ""
    last_error_text: str = ""
    last_error_at: float = 0.0
    last_success_at: float = 0.0
    last_peer: str = ""
    enabled: bool = True

    def note_attempt(self, count: int = 1) -> None:
        self.attempts += count

    def note_sent(self, count: int = 1) -> None:
        self.sent += count
        self.attempts += count

    def note_received(self) -> None:
        self.received += 1

    def note_success(self, peer: Peer | None = None) -> None:
        self.successes += 1
        self.last_success_at = time.time()
        if peer is not None:
            self.last_peer = f"{peer.info.name} ({peer.address})"

    def note_error(self, exc: BaseException | str) -> None:
        self.errors += 1
        self.last_error = str(exc)
        self.last_error_text = explain_socket_error(exc)
        self.last_error_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "enabled": self.enabled,
            "attempts": self.attempts,
            "successes": self.successes,
            "errors": self.errors,
            "sent": self.sent,
            "received": self.received,
            "lastError": self.last_error,
            "lastErrorText": self.last_error_text,
            "lastErrorAt": self.last_error_at,
            "lastSuccessAt": self.last_success_at,
            "lastPeer": self.last_peer,
            "healthy": bool(self.successes) or self.attempts == 0,
        }


CHANNEL_LABELS = {
    "multicast": "组播",
    "broadcast": "广播",
    "mdns": "mDNS（224.0.0.251）",
    "scan": "主动扫描",
    "manual": "手动 / 二维码",
}


class Diagnostics:
    """Per-channel counters plus a "为什么看不到对方" checklist."""

    def __init__(self) -> None:
        self.channels: dict[str, ChannelStats] = {
            key: ChannelStats(key, label) for key, label in CHANNEL_LABELS.items()
        }
        self._lock = threading.RLock()
        self.notes: list[dict[str, Any]] = []

    def channel(self, name: str) -> ChannelStats:
        with self._lock:
            stats = self.channels.get(name)
            if stats is None:
                stats = ChannelStats(name, CHANNEL_LABELS.get(name, name))
                self.channels[name] = stats
            return stats

    def note(self, message: str, *, level: str = "info") -> None:
        with self._lock:
            self.notes.append({"ts": time.time(), "level": level, "message": message})
            if len(self.notes) > 40:
                del self.notes[: len(self.notes) - 40]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {name: stats.to_dict() for name, stats in self.channels.items()}


# ---------------------------------------------------------------------------
# peer store
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class KnownPeer:
    """A device we have seen before, even if it is not here right now.

    Kept separately from the live peer table because two features need it: the
    "离线设备" section, and the send queue for a message addressed to a device
    that is momentarily away (charter §5.5: queue it, do not silently drop it).
    """

    device_id: str
    name: str
    kind: str
    platform: str
    version: str
    address: str
    port: int
    source: str
    last_seen: float
    addresses: list[str] = field(default_factory=list)

    def to_peer(self) -> Peer:
        info = DeviceInfo(
            device_id=self.device_id,
            name=self.name,
            kind=self.kind,
            platform=self.platform,
            version=self.version,
            tcp_port=self.port,
        )
        peer = Peer(info=info, address=self.address, port=self.port, source="offline")
        peer.addresses = list(self.addresses) or [self.address]
        peer.last_seen = self.last_seen
        return peer

    def to_dict(self) -> dict[str, Any]:
        return {
            "deviceId": self.device_id,
            "name": self.name,
            "kind": self.kind,
            "platform": self.platform,
            "address": self.address,
            "port": self.port,
            "source": self.source,
            "lastSeen": self.last_seen,
        }


class PeerStore:
    """Thread-safe registry of discovered devices."""

    def __init__(self, ttl: float = DEVICE_TTL) -> None:
        self.ttl = ttl
        self._peers: dict[str, Peer] = {}
        self._known: dict[str, KnownPeer] = {}
        #: device id -> when it said goodbye.  Used to reject *older* evidence:
        #: a subnet-scan handshake that was already in flight when the peer
        #: quit can complete afterwards, and applying it would put a laptop
        #: that just shut down back in the list for another full TTL.
        self._bye_at: dict[str, float] = {}
        self._lock = threading.RLock()

    def upsert(
        self, announcement: Announcement, address: str, source: str
    ) -> tuple[Peer, bool] | None:
        """Insert or refresh a peer.  Returns ``(peer, is_new)``.

        ``None`` means the datagram was *older* than the device's goodbye and
        had to be ignored: it is the reply that was already in flight when the
        peer shut down (see :data:`BYE_IGNORE_WINDOW`).
        """
        key = announcement.device_id
        with self._lock:
            if self.said_bye_recently(key):
                return None
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
                info.capabilities["accept"] = bool(announcement.accepts_files)
                peer = Peer(info=info, address=address, port=announcement.port, source=source)
                peer.addresses = [address]
                self._peers[key] = peer
                self._remember(peer, source)
                return peer, True

            existing.touch(address, announcement.port)
            existing.source = source
            existing.info.name = announcement.name
            existing.info.kind = announcement.kind
            existing.info.platform = announcement.platform
            existing.info.version = announcement.version
            existing.info.web_port = announcement.web_port
            # The capability is advertised, so it is authoritative here: a
            # device that says "I cannot receive" must not be offered files.
            existing.info.capabilities["accept"] = bool(announcement.accepts_files)
            if address not in existing.addresses:
                existing.addresses.append(address)
                del existing.addresses[8:]  # keep the list bounded
            for extra in announcement.addresses:
                if extra not in existing.addresses:
                    existing.addresses.append(extra)
            del existing.addresses[8:]
            self._remember(existing, source)
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
                self._remember(peer, source)
                return peer, True
            peer.info = info
            if address and address not in peer.addresses:
                peer.addresses.append(address)
                del peer.addresses[8:]
            peer.touch(address, port)
            self._remember(peer, source)
            return peer, False

    # -- remembering devices that went away --------------------------------

    def _remember(self, peer: Peer, source: str) -> None:
        """Record where a device was last reachable (call with the lock held)."""
        key = peer.info.device_id
        if not key:
            return
        entry = self._known.get(key)
        if entry is None:
            entry = KnownPeer(
                device_id=key,
                name=peer.info.name,
                kind=peer.info.kind,
                platform=peer.info.platform,
                version=peer.info.version,
                address=peer.address,
                port=peer.port,
                source=source,
                last_seen=time.time(),
            )
            self._known[key] = entry
        entry.name = peer.info.name or entry.name
        entry.kind = peer.info.kind or entry.kind
        entry.platform = peer.info.platform or entry.platform
        entry.version = peer.info.version or entry.version
        if peer.address:
            entry.address = peer.address
            if peer.address not in entry.addresses:
                entry.addresses.insert(0, peer.address)
                del entry.addresses[6:]
        if peer.port:
            entry.port = peer.port
        entry.source = source
        entry.last_seen = time.time()
        if len(self._known) > KNOWN_PEER_LIMIT:
            oldest = sorted(self._known.items(), key=lambda item: item[1].last_seen)
            for old_key, _value in oldest[: len(self._known) - KNOWN_PEER_LIMIT]:
                self._known.pop(old_key, None)

    def known(self, device_id: str) -> KnownPeer | None:
        with self._lock:
            entry = self._known.get(str(device_id or ""))
            if entry is None:
                return None
            if time.time() - entry.last_seen > KNOWN_PEER_TTL:
                return None
            return entry

    def known_all(self) -> list[KnownPeer]:
        """Devices seen before, newest first (the "离线设备" list)."""
        with self._lock:
            entries = list(self._known.values())
        now = time.time()
        entries = [e for e in entries if now - e.last_seen <= KNOWN_PEER_TTL]
        entries.sort(key=lambda e: e.last_seen, reverse=True)
        return entries

    def forget_known(self) -> int:
        with self._lock:
            count = len(self._known)
            self._known.clear()
        return count

    # -- liveness ----------------------------------------------------------

    def mark_offline(self, device_id: str, *, from_bye: bool = False) -> Peer | None:
        """Take a peer out of the *live* table, keeping it as a known device.

        Returns the peer that was removed, or ``None`` when it was not there.
        ``from_bye`` records *when* the device said it was leaving, so evidence
        older than that (an in-flight probe) cannot resurrect it.
        """
        key = str(device_id or "")
        with self._lock:
            if from_bye and key:
                self._bye_at[key] = time.monotonic()
                if len(self._bye_at) > 512:
                    cutoff = time.monotonic() - 300.0
                    for old in [k for k, v in self._bye_at.items() if v < cutoff]:
                        del self._bye_at[old]
            peer = self._peers.pop(key, None)
            if peer is not None:
                self._remember(peer, peer.source)
            return peer

    def said_bye_recently(self, device_id: str, *, window: float = BYE_IGNORE_WINDOW) -> bool:
        """Whether this device said goodbye less than ``window`` seconds ago."""
        with self._lock:
            stamp = self._bye_at.get(str(device_id or ""))
        return stamp is not None and (time.monotonic() - stamp) < window

    def said_bye_since(self, device_id: str, since: float) -> bool:
        """Whether this device said goodbye after ``since`` (monotonic seconds).

        The caller passes the moment it *started collecting its evidence* (the
        probe), not the moment it finished: a handshake that began before the
        goodbye proves the peer was alive then, which is not a reason to list
        it now.
        """
        with self._lock:
            stamp = self._bye_at.get(str(device_id or ""))
        return stamp is not None and stamp >= since

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
                self._remember(peer, peer.source)
        return gone


# ---------------------------------------------------------------------------
# scan planning
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScanTarget:
    """One host to probe, together with the local address to probe it from."""

    address: str
    local: str
    segment: str


def plan_scan_targets(
    interfaces: list[Interface],
    *,
    tight: bool = True,
    budget: int | None = None,
) -> list[ScanTarget]:
    """Hosts to probe, covering **every** local subnet without flooding.

    Subnets that are /24 or smaller are probed host by host: that is 254
    connects, which a LAN does not even notice.

    Bigger subnets (/23 ... /8) are *segmented samples* instead.  Walking a
    /16 host by host is 65 534 SYNs -- slow, and indistinguishable from a port
    scan to any IDS -- and walking a /8 is 16 million.  So the plan is built as
    a **priority ladder** instead, and it is consumed in that order:

    1. our own /24, walked completely (a device on the same switch is almost
       always in it);
    2. the nearest foreign /24s, with a dense sample of low offsets -- where
       DHCP pools and hand-configured addresses live;
    3. every remaining /24 of the subnet, sampled sparsely (nearest first), so
       coverage is spread over all segments instead of exhausted on the first
       few;
    4. only in the deep pass: the nearest foreign /24s walked completely.

    Ordering is what makes the 5-second promise hold on a large subnet: the
    worker pool walks the list front to back, so a peer that is one segment
    away is probed a few dozen connects in, not after 60 000 of them.  The
    budget then caps the *total*, so a /8 costs the same as a /16.
    """
    targets: list[ScanTarget] = []
    seen: set[str] = set()
    limit = int(budget if budget is not None else (SCAN_TIGHT_BUDGET if tight else SCAN_DEEP_BUDGET))

    def add(address: str, local: str, segment: str) -> None:
        if address in seen or len(targets) >= limit:
            return
        seen.add(address)
        targets.append(ScanTarget(address, local, segment))

    for iface in interfaces:
        if iface.is_ipv6:
            continue
        # Loopback is this machine; probing it finds us, not a peer.
        if iface.address.startswith("127."):
            continue
        net = iface.network
        if net is None:
            try:
                net = ipaddress.ip_network(f"{iface.address}/24", strict=False)
            except ValueError:
                continue

        own = ipaddress.ip_address(iface.address)
        if net.prefixlen >= 24:
            for host in net.hosts():
                if host == own:
                    continue
                add(str(host), iface.address, str(net))
            continue

        # --- 1. our own /24, every host ---------------------------------
        try:
            own_net = ipaddress.ip_network(f"{iface.address}/24", strict=False)
        except ValueError:
            continue
        for host in own_net.hosts():
            if host == own:
                continue
            add(str(host), iface.address, str(own_net))

        # The sibling list is computed arithmetically: a /8 holds 65 536 /24s
        # and materialising them costs more than the scan itself.
        base = int(net.network_address)
        count = 1 << max(0, 24 - net.prefixlen)
        own_index = (int(own) - base) // 256
        candidates: list[int] = []
        step = 1
        while len(candidates) < min(count - 1, SCAN_SWEEP_SEGMENTS):
            progressed = False
            up = own_index + step
            down = own_index - step
            if up < count:
                candidates.append(up)
                progressed = True
            if down >= 0:
                candidates.append(down)
                progressed = True
            if not progressed:
                break
            step += 1

        # --- 2. the nearest /24s, sampled densely at low offsets --------
        near = candidates[:SCAN_NEAR_SEGMENTS]
        for index in near:
            if len(targets) >= limit:
                break
            segment_base = base + index * 256
            for offset in SCAN_SAMPLE_OFFSETS[:SCAN_DENSE_SAMPLES]:
                add(str(ipaddress.ip_address(segment_base + offset)), iface.address, f"{index}")

        # --- 3. every other /24, sampled sparsely ----------------------
        far_budget = SCAN_FAR_PROBE_BUDGET
        for index in candidates[SCAN_NEAR_SEGMENTS:]:
            if len(targets) >= limit or far_budget <= 0:
                break
            segment_base = base + index * 256
            for offset in SCAN_SAMPLE_OFFSETS[:SCAN_SPARSE_SAMPLES]:
                if far_budget <= 0:
                    break
                before = len(targets)
                add(str(ipaddress.ip_address(segment_base + offset)), iface.address, f"{index}")
                if len(targets) > before:
                    far_budget -= 1

        # --- 4. deep pass only: walk the nearest /24s completely --------
        if not tight:
            for index in near[:SCAN_DEEP_SEGMENTS]:
                if len(targets) >= limit:
                    break
                segment_base = base + index * 256
                for offset in range(1, 255):
                    add(str(ipaddress.ip_address(segment_base + offset)), iface.address, f"{index}")

    return targets


# ---------------------------------------------------------------------------
# discovery service
# ---------------------------------------------------------------------------


class DiscoveryService:
    """Runs all discovery channels for one device."""

    #: How often we answer a single address, in seconds.  A broadcast burst
    #: from several devices must not turn into an amplification loop.
    REPLY_COOLDOWN = 1.0
    #: How often one address may make us answer an mDNS query, in seconds.
    MDNS_REPLY_COOLDOWN = 1.0

    def __init__(
        self,
        *,
        device_info: Callable[[], DeviceInfo],
        events: Any,
        port: int = DEFAULT_DISCOVERY_PORT,
        group: str = MULTICAST_GROUP_V4,
        enable_multicast: bool = True,
        enable_broadcast: bool = True,
        enable_mdns: bool = True,
        enable_scan: bool = True,
        scan_concurrency: int = SCAN_CONCURRENCY,
        scan_rate: float = SCAN_RATE_PER_SECOND,
        probe_peer: Callable[[str, int, str], Peer | None] | None = None,
    ) -> None:
        self.device_info = device_info
        self.events = events
        self.port = port
        self.group = group
        self.enable_multicast = enable_multicast
        self.enable_broadcast = enable_broadcast
        self.enable_mdns = enable_mdns
        self.enable_scan = enable_scan
        self.scan_concurrency = max(1, int(scan_concurrency))
        self.scan_rate = max(1.0, float(scan_rate))
        #: Set by the engine: performs a real handshake against a host that
        #: answered a probe and registers the peer it learns.  Without it a
        #: scan can only say "something is listening there".
        self.probe_peer = probe_peer

        self.peers = PeerStore()
        self.diagnostics = Diagnostics()
        self._interfaces: list[Interface] = []
        #: address -> last time we answered it (see :meth:`_maybe_reply`).
        self._replies: dict[str, float] = {}
        self._reply_lock = threading.Lock()
        self._multicast_rx: list[socket.socket] = []
        self._broadcast_rx: socket.socket | None = None
        self._mdns_rx: socket.socket | None = None
        self._senders: list[socket.socket] = []
        self._sender_targets: list[tuple[socket.socket, tuple, str]] = []
        self._mdns_senders: list[tuple[socket.socket, Interface]] = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._announce_now = threading.Event()
        self._last_announce = 0.0
        #: Monotonic time the current burst started (schedule is relative to it).
        self._burst_started = 0.0
        self._pending_burst: list[float] = []
        self._next_steady = 0.0
        self._scan_lock = threading.Lock()
        self._scan_running = False
        self._scan_due = 0.0
        self._scan_failures = 0
        self._last_scan: dict[str, Any] = {}
        self._scan_errors: list[str] = []
        #: Set while a scan is in progress, so the periodic scheduler does not
        #: pile a second one on top.
        self._started_at = 0.0
        self._first_announce_at = 0.0
        self._announce_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._started_at = time.monotonic()
        self._interfaces = list_interfaces(include_virtual=True, include_loopback=False)

        # 1. Senders first, then shout -- *before* any listener or mDNS work.
        #    The other side can only see us when this packet leaves, so nothing
        #    that is not on the critical path is allowed in front of it.
        self._build_senders()
        self._send_announcement()

        # 2. Now the receivers, so the answer to that shout (and everybody
        #    else's) has somewhere to land.
        self._multicast_rx = (
            create_multicast_listener(self.group, self.port, self._interfaces, reuse_port=True)
            if self.enable_multicast
            else []
        )
        if self.enable_multicast and not self._multicast_rx:
            self.diagnostics.channel("multicast").note_error("组播组加入失败")
            self.events.emit(
                "discovery_warning",
                message="multicast could not be joined; falling back to broadcast and scanning",
            )

        if self.enable_broadcast:
            self._broadcast_rx = create_broadcast_listener(self.port)
            if self._broadcast_rx is None:
                self.diagnostics.channel("broadcast").note_error("发现端口被占用")
                self.events.emit(
                    "discovery_warning", message="the broadcast discovery port is already in use"
                )

        if self.enable_mdns:
            self._mdns_rx = self._open_mdns_socket()

        for sock in self._multicast_rx:
            self._spawn(self._receive_loop, sock, "multicast")
        if self._broadcast_rx is not None:
            self._spawn(self._receive_loop, self._broadcast_rx, "broadcast")
        if self._mdns_rx is not None:
            self._spawn(self._mdns_loop, self._mdns_rx)

        # 3. The scheduler finishes the burst (1/2/5 s), keeps the steady
        #    15-second heartbeat going, expires dead peers and drives the
        #    automatic subnet scan.
        self._burst_started = self._first_announce_at or time.monotonic()
        self._pending_burst = list(ANNOUNCE_SCHEDULE[1:])
        self._next_steady = self._burst_started + ANNOUNCE_SCHEDULE[-1] + ANNOUNCE_STEADY_INTERVAL
        self._scan_due = time.monotonic() + SCAN_START_GRACE
        self._spawn(self._scheduler_loop)

        self.events.emit(
            "discovery_started",
            interfaces=len(self._interfaces),
            multicast=len(self._multicast_rx),
            broadcast=self._broadcast_rx is not None,
            scan=self.enable_scan,
        )

    def _build_senders(self) -> None:
        """Create one sender per interface and remember what to shout at."""
        self._senders = []
        self._sender_targets = []
        self._mdns_senders = []
        ifaces = list(self._interfaces)
        if not ifaces:
            # No interface list: shout on the default route only.
            sock = create_multicast_sender(self.group, self.port, Interface("default", "0.0.0.0"))
            if sock is None:
                sock = create_broadcast_sender(Interface("default", "0.0.0.0"))
            if sock is not None:
                self._senders.append(sock)
                self._sender_targets.append((sock, (self.group, self.port), "multicast"))
                if self.enable_broadcast:
                    self._sender_targets.append((sock, ("255.255.255.255", self.port), "broadcast"))
            return

        for iface in ifaces:
            if iface.is_ipv6:
                if self.enable_multicast:
                    sender = create_multicast_sender(MULTICAST_GROUP_V6, self.port, iface)
                    if sender is not None:
                        self._senders.append(sender)
                        self._sender_targets.append(
                            (sender, (MULTICAST_GROUP_V6, self.port), "multicast")
                        )
                continue
            sender = (
                create_multicast_sender(self.group, self.port, iface)
                if self.enable_multicast
                else create_broadcast_sender(iface)
            )
            if sender is not None:
                self._senders.append(sender)
                if self.enable_multicast:
                    self._sender_targets.append((sender, (self.group, self.port), "multicast"))
                # The mDNS advertisement travels over the same interface, so it
                # reuses this socket: one socket per interface, not one per send.
                self._mdns_senders.append((sender, iface))
            if self.enable_broadcast:
                bsend = create_broadcast_sender(iface)
                if bsend is not None:
                    self._senders.append(bsend)
                    self._sender_targets.append((bsend, ("255.255.255.255", self.port), "broadcast"))
                    directed = iface.broadcast
                    if directed:
                        self._sender_targets.append((bsend, (directed, self.port), "broadcast"))

    def stop(self, *, announce_bye: bool = True) -> None:
        if self._first_announce_at and announce_bye:
            # "Leaving" is a datagram like any other: peers drop us from their
            # list at once instead of showing a laptop that was shut down an
            # hour ago (charter §5.2).
            try:
                self.send_bye()
            except Exception:
                pass
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
        self._sender_targets = []
        self._mdns_senders = []
        for thread in self._threads:
            if thread is threading.current_thread():
                continue
            thread.join(timeout=1.5)
        self._threads = []
        self.events.emit("discovery_stopped")

    def _spawn(self, target: Callable, *args: Any) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        self._threads.append(thread)

    # -- announcing --------------------------------------------------------

    def announce(self, burst: bool = True) -> None:
        """Send an announcement now (and a short burst by default).

        Called by the 「扫描」 button and by the engine at startup; it is the
        "make me visible immediately" path, so it does not wait for the
        scheduler's next tick.
        """
        if burst:
            self._pending_burst = list(ANNOUNCE_BURST_ON_DEMAND[1:])
            self._burst_started = time.monotonic()
        self._send_announcement()
        if self.enable_mdns:
            # ... and *ask* who is there.  ``_mdns_loop`` answers such a query
            # immediately, so two devices that both just started find each
            # other in one round trip instead of waiting for a periodic
            # announcement (charter §5.1: ≤5 s, and the 「扫描」 button has to
            # be an accelerator, not a necessary step).
            threading.Thread(target=self._mdns_query_safe, name="mdns-query", daemon=True).start()

    def _mdns_query_safe(self) -> None:
        try:
            self.advertise_mdns(query_only=True)
        except Exception as exc:  # pragma: no cover - defensive
            self.diagnostics.channel("mdns").note_error(exc)

    def _local_addresses(self) -> list[str]:
        return [iface.address for iface in self._interfaces if not iface.is_ipv6]

    def _send_announcement(self, *, bye: bool = False) -> int:
        """Put our announcement on the wire.  Returns how many sockets took it."""
        payload = Announcement.from_device(
            self.device_info(), bye=bye, addresses=self._local_addresses()
        ).to_bytes()
        if len(payload) > MAX_DATAGRAM:  # pragma: no cover - defensive
            return 0
        sent = 0
        for sock, target, channel in list(self._sender_targets):
            try:
                sock.sendto(payload, target)
                sent += 1
                self.diagnostics.channel(channel).note_sent()
            except OSError as exc:
                self.diagnostics.channel(channel).note_error(exc)
                continue
        self._last_announce = time.monotonic()
        if not self._first_announce_at:
            self._first_announce_at = self._last_announce
        self.events.emit("announced", targets=len(self._sender_targets), bye=bye)
        # mDNS is a *second* channel for the same information, and a slow one
        # (one packet per interface); it must never delay the UDP packet above.
        if self.enable_mdns and not bye:
            threading.Thread(
                target=self._advertise_mdns_safe, name="mdns-advertise", daemon=True
            ).start()
        return sent

    def send_bye(self) -> None:
        """Tell everybody we are leaving (multicast, broadcast and unicast)."""
        info = self.device_info()
        payload = Announcement.from_device(
            info, bye=True, addresses=self._local_addresses()
        ).to_bytes()
        targets: list[tuple[socket.socket, tuple]] = [
            (sock, target) for sock, target, _channel in list(self._sender_targets)
        ]
        # Unicast to every device we know about, so a peer whose multicast and
        # broadcast are filtered still notices immediately.
        for entry in self.peers.known_all()[:32]:
            for sock in list(self._senders):
                targets.append((sock, (entry.address, self.port)))
                break
        for _round in range(3):
            for sock, target in targets:
                try:
                    sock.sendto(payload, target)
                except OSError:
                    continue
            if self._stop.is_set():
                break
            time.sleep(0.02)

    def _scheduler_loop(self) -> None:
        """One tick loop for cadence, expiry and the automatic scan.

        A single thread on a 0.1-second tick instead of one thread per job:
        the interactions (a burst restarts the steady timer, a scan is due
        after a burst) are orderings, and orderings are easier to get right in
        one place than spread over four timers.
        """
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                while self._pending_burst and now - self._burst_started >= self._pending_burst[0]:
                    self._pending_burst.pop(0)
                    self._send_announcement()
                    now = time.monotonic()
                if self._announce_now.is_set():
                    self._announce_now.clear()
                    self._send_announcement()
                    now = time.monotonic()
                if now >= self._next_steady:
                    self._send_announcement()
                    self._next_steady = now + ANNOUNCE_STEADY_INTERVAL
                self._expire_peers()
                if self.enable_scan and now >= self._scan_due and not self._scan_running:
                    self._run_scan_pass()
            except Exception as exc:  # pragma: no cover - defensive
                self.diagnostics.note(f"发现调度出错：{exc}", level="error")
            self._stop.wait(0.1)

    def _expire_peers(self) -> None:
        gone = self.peers.expire()
        for peer in gone:
            self.events.emit(
                "device_lost", peer=peer, device_id=peer.info.device_id, reason="长时间没有收到公告"
            )

    # -- receiving ---------------------------------------------------------

    def _receive_loop(self, sock: socket.socket, source_kind: str) -> None:
        sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(MAX_CONTROL_PAYLOAD)
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    self.diagnostics.channel(source_kind).note_error(exc)
                return

            announcement = Announcement.parse(data)
            if announcement is None:
                continue
            if announcement.device_id == self.device_info().device_id:
                continue  # our own announcement, looped back

            stats = self.diagnostics.channel(source_kind)
            stats.note_received()

            # A multicast listener reports the *interface* address as the
            # source on some platforms; the packet's real origin is in addr.
            address = addr[0]
            if address.startswith("::ffff:"):
                address = address[7:]

            if announcement.bye:
                self._handle_bye(announcement, source_kind)
                continue

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
                    # A phone that announces itself with no port is a browser
                    # client by construction: it cannot receive a push.
                    accepts_files=bool(announcement.accepts_files and announcement.port > 0),
                    # 不能再叫 kind：emit(kind, **payload) 的第一个参数已经占了它。
                    device_kind=announcement.kind,
                    platform=announcement.platform,
                    version=announcement.version,
                    address=address,
                )
                self._maybe_reply(addr)
                continue

            found = self.peers.upsert(announcement, address, source_kind)
            if found is None:
                continue  # older than the peer's goodbye
            peer, is_new = found
            stats.note_success(peer)
            self.events.emit(
                "device_found" if is_new else "device_updated",
                peer=peer,
                is_new=is_new,
            )
            # Answer a device we did not know about, so its own "search for
            # computers" finishes immediately instead of waiting up to
            # ANNOUNCE_STEADY_INTERVAL for our next broadcast.
            if is_new:
                self._maybe_reply(addr)

    def _handle_bye(self, announcement: Announcement, source_kind: str) -> None:
        """A peer told us it is leaving: take it out of the list now."""
        peer = self.peers.mark_offline(announcement.device_id, from_bye=True)
        if peer is not None:
            self.events.emit(
                "device_lost",
                peer=peer,
                device_id=announcement.device_id,
                reason="对方已退出",
                source=source_kind,
            )

    def _maybe_reply(self, addr: tuple) -> None:
        """Send our announcement straight back to whoever just spoke.

        Two destinations on purpose:

        * the **source port**, which is what a phone's or another
          implementation's custom socket is listening on, and
        * our **discovery port** on that address, because a peer that
          announced from an unbound sender socket has an ephemeral source port
          that nobody ever reads -- measured in the lab harness, a reply that
          only targets the source port lands in a socket buffer that is
          discarded, and the peer waits for the next periodic announcement.
        """
        try:
            address = addr[0]
            source_port = int(addr[1])
        except (IndexError, TypeError, ValueError):
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
        payload = Announcement.from_device(
            self.device_info(), addresses=self._local_addresses()
        ).to_bytes()
        targets = [(address, source_port), (address, self.port)]
        for target in targets:
            if target[1] <= 0:
                continue
            for sock in list(self._senders):
                try:
                    sock.sendto(payload, target)
                    break
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
                except OSError as exc:
                    self.diagnostics.channel("mdns").note_error(exc)
                    continue
            if not joined:
                sock.close()
                self.diagnostics.channel("mdns").note_error("mDNS 组加入失败")
                return None
            sock.settimeout(0.5)
            return sock
        except OSError as exc:
            self.diagnostics.channel("mdns").note_error(exc)
            return None

    def _mdns_loop(self, sock: socket.socket) -> None:
        """Parse mDNS traffic for our service type.

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
            # A query for our service type gets an answer straight away, the
            # same way a UDP probe does.  Without this, a phone that just
            # tapped "search for the computer" waits for the next steady
            # broadcast -- and Android's NsdManager, which is what the app
            # uses, gives up long before that.
            try:
                questions = mdns.parse_questions(data)
            except Exception:
                questions = []
            if any(
                qtype in (mdns.TYPE_PTR, mdns.TYPE_ANY) and MDNS_SERVICE_TYPE.split(".")[0] in name
                for name, qtype in questions
            ):
                self.diagnostics.channel("mdns").note_received()
                self._answer_mdns(addr[0])

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
                found = self.peers.upsert(announcement, addr[0], "mdns")
                if found is None:
                    continue
                peer, is_new = found
                self.diagnostics.channel("mdns").note_received()
                self.diagnostics.channel("mdns").note_success(peer)
                self.events.emit("device_found" if is_new else "device_updated", peer=peer, is_new=is_new)

    def _answer_mdns(self, address: str) -> None:
        """Answer one querier, rate-limited (see :meth:`_maybe_reply`)."""
        now = time.monotonic()
        with self._reply_lock:
            last = self._replies.get(f"mdns:{address}", 0.0)
            if now - last < self.MDNS_REPLY_COOLDOWN:
                return
            self._replies[f"mdns:{address}"] = now
        self._advertise_mdns_safe()

    def _advertise_mdns_safe(self) -> None:
        try:
            self.advertise_mdns()
        except Exception as exc:  # pragma: no cover - defensive
            self.diagnostics.channel("mdns").note_error(exc)

    def advertise_mdns(self, *, query_only: bool = False) -> None:
        """Answer mDNS queries so other implementations can find us.

        ``query_only`` sends the PTR *question* instead of the answer: that is
        the "who is there?" poke, which is how a device that just started makes
        every mDNS-aware peer answer immediately rather than on its own
        schedule.
        """
        from . import mdns

        info = self.device_info()
        txt = {
            "id": info.device_id,
            "n": info.name[:64],
            "k": info.kind,
            "p": info.platform,
            "v": info.version,
            "web": str(info.web_port),
            # Same capability as in the UDP announcement, for peers that only
            # speak mDNS (Android's NsdManager among them).
            "rx": "1" if (info.capabilities or {}).get("accept", True) else "0",
        }
        query = mdns.build_query(MDNS_SERVICE_TYPE)
        for sock, iface in list(self._mdns_senders):
            try:
                if query_only:
                    sock.sendto(query, (MDNS_GROUP_V4, MDNS_PORT))
                    continue
                # One packet per interface, each carrying that interface's own A
                # record.  Android's NsdManager resolves the host from it; without
                # an A record it has nothing to dial.
                packet = mdns.build_announcement(
                    instance=f"{info.name}-{info.device_id[:6]}",
                    service_type=MDNS_SERVICE_TYPE,
                    host=f"{info.name}.local",
                    port=info.tcp_port,
                    txt=txt,
                    address=iface.address,
                )
                sock.sendto(packet, (MDNS_GROUP_V4, MDNS_PORT))
                self.diagnostics.channel("mdns").note_sent()
            except OSError as exc:
                self.diagnostics.channel("mdns").note_error(exc)
                continue

    # -- active probing ----------------------------------------------------

    def scan_plan(self, *, tight: bool = True) -> list[ScanTarget]:
        return plan_scan_targets(self._interfaces, tight=tight)

    def scan_subnets(
        self,
        port: int,
        *,
        concurrency: int | None = None,
        timeout: float = SCAN_TIMEOUT,
        tight: bool = True,
        reason: str = "manual",
    ) -> int:
        """Probe the local subnets for listening peers.

        Returns the number of hosts that answered.  Answering is not enough to
        be a device: each hit gets a real handshake (``probe_peer``) and only
        that registers a peer, keyed by device id, in the shared device list --
        which is the difference between "3 addresses responded" and "华为 Mate
        出现在设备列表里，可以直接发文件".
        """
        port = int(port or 0)
        if port <= 0:
            return 0
        targets = self.scan_plan(tight=tight)
        if not targets:
            self._last_scan = {
                "reason": reason,
                "targets": 0,
                "hits": 0,
                "registered": 0,
                "elapsed": 0.0,
                "pass": "tight" if tight else "sweep",
            }
            return 0

        workers = max(1, min(concurrency or self.scan_concurrency, len(targets)))
        started = time.monotonic()
        hits = 0
        probes = 0
        lock = threading.Lock()
        #: (address, local interface) of every host that answered, so the
        #: handshake afterwards is sent out of the interface it was probed
        #: from -- on a multi-homed host the kernel would otherwise pick the
        #: default route and the peer would see a connection from an address it
        #: never spoke to.
        hit_addresses: list[tuple[str, str]] = []
        index = [0]
        #: Token bucket: no more than ``scan_rate`` probes per second, whatever
        #: the concurrency is.  A /16 sweep is thousands of SYNs, and a switch
        #: (or an intrusion detector) should see a trickle, not a storm.
        budget = [float(self.scan_rate)]
        last_refill = [time.monotonic()]

        def take_token() -> None:
            while True:
                now = time.monotonic()
                with lock:
                    budget[0] = min(
                        float(self.scan_rate),
                        budget[0] + (now - last_refill[0]) * self.scan_rate,
                    )
                    last_refill[0] = now
                    if budget[0] >= 1.0:
                        budget[0] -= 1.0
                        return
                time.sleep(0.005)

        def worker() -> None:
            nonlocal hits, probes
            while not self._stop.is_set():
                with lock:
                    if index[0] >= len(targets):
                        return
                    target = targets[index[0]]
                    index[0] += 1
                take_token()
                with lock:
                    probes += 1
                if self._probe(target, port, timeout, hit_addresses, lock):
                    with lock:
                        hits += 1

        threads = [
            threading.Thread(target=worker, name=f"scan-{i}", daemon=True)
            for i in range(workers)
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + max(60.0, len(targets) / self.scan_rate * 4 + 30.0)
        for thread in threads:
            thread.join(timeout=max(1.0, deadline - time.monotonic()))

        # Register the hosts that answered.  Done after the sweep so a slow
        # handshake cannot hold up the remaining probes.
        registered = self._register_hits(hit_addresses, port)
        elapsed = time.monotonic() - started
        stats = self.diagnostics.channel("scan")
        stats.note_attempt(probes)
        self._last_scan = {
            "reason": reason,
            "targets": len(targets),
            "probes": probes,
            "hits": hits,
            "registered": registered,
            "elapsed": round(elapsed, 2),
            "pass": "tight" if tight else "sweep",
            "finished_at": time.time(),
        }
        self.diagnostics.note(
            f"主动扫描（{reason}）：探测 {probes} 个地址，{hits} 个有响应，登记 {registered} 台设备，用时 {elapsed:.1f}s"
        )
        return hits

    def _register_hits(self, hits: list[tuple[str, str]] | list[str], port: int) -> int:
        """Handshake with the hosts that answered and register the peers.

        Accepts either ``(address, local)`` pairs or bare addresses: the pair
        carries the interface the probe went out of, so the handshake leaves
        from the same one; a bare string is what a test stub naturally appends,
        and that shape was published as part of the probe interface.
        """
        if self.probe_peer is None or not hits:
            return 0
        registered = 0
        seen: set[str] = set()
        for hit in hits:
            if isinstance(hit, tuple):
                address, local = str(hit[0]), str(hit[1] if len(hit) > 1 else "")
            else:
                address, local = str(hit), ""
            if self._stop.is_set():
                break
            if address in seen:
                continue
            seen.add(address)
            try:
                peer = self.probe_peer(address, port, local)
            except Exception as exc:  # pragma: no cover - defensive
                self.diagnostics.channel("scan").note_error(exc)
                continue
            if peer is not None:
                registered += 1
                self.diagnostics.channel("scan").note_success(peer)
                # Answer the host that just answered us.  Discovery has to be
                # symmetric or it is half useless: when only *one* side can
                # scan (the other is a phone, or its own sweep of a /16 never
                # landed on our segment), the finder must introduce itself.
                # One unicast datagram and the peer appears on the other side
                # in the same second.
                self._announce_to(address)
                self.events.emit("device_found", peer=peer, is_new=True, via="scan")
        return registered

    def _announce_to(self, address: str) -> None:
        """Send our announcement straight to one address's discovery port."""
        payload = Announcement.from_device(
            self.device_info(), addresses=self._local_addresses()
        ).to_bytes()
        for sock in list(self._senders):
            try:
                sock.sendto(payload, (address, self.port))
                self.diagnostics.channel("multicast").note_sent()
                return
            except OSError:
                continue

    def _probe(
        self,
        target: ScanTarget,
        port: int,
        timeout: float,
        hit_addresses: list[str],
        lock: threading.Lock,
    ) -> bool:
        """TCP-connect to one host; a bare connect only proves *something* listens.

        The identity comes from the handshake in :meth:`_register_hits`, so a
        printer or a NAS on the same port does not turn into a fake device row.
        """
        if self._stop.is_set():
            return False
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            try:
                sock.bind((target.local, 0))
            except OSError:
                pass
            sock.connect((target.address, port))
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
        with lock:
            hit_addresses.append((target.address, target.local))
        # Frozen interface: the desktop shell listens for this to show
        # "扫描发现" and (before this version) to probe the address itself.
        self.events.emit("scan_hit", address=target.address, port=port)
        return True

    def _run_scan_pass(self) -> None:
        """One automatic scan pass, scheduled by the tick loop.

        One pass, not a "tight then sweep" pair: the plan is already ordered by
        priority (our own /24 first, then the nearest segments), so the worker
        pool discovers a nearby peer within a second or two while the far end
        of a /16 keeps being sampled behind it.  Escalating to the deep plan
        happens on a *later* pass, once the cheap one has come up empty twice
        -- widening within a pass would delay the probe that matters.
        """
        with self._scan_lock:
            if self._scan_running:
                return
            self._scan_running = True
        try:
            live = list(self.peers.all())
            port = int(getattr(self.device_info(), "tcp_port", 0) or 0)
            if port <= 0:
                return
            deep = self._scan_failures >= 2
            hits = self.scan_subnets(port, tight=not deep, reason="auto-deep" if deep else "auto")
            self._scan_failures = 0 if hits else self._scan_failures + 1
            if live or hits:
                interval = SCAN_INTERVAL_WITH_PEERS if live else SCAN_INTERVAL_ALONE
            else:
                interval = min(
                    SCAN_INTERVAL_MAX,
                    SCAN_INTERVAL_ALONE * (1.5 ** min(self._scan_failures, 4)),
                )
            self._scan_due = time.monotonic() + interval
        except Exception as exc:  # pragma: no cover - defensive
            self.diagnostics.channel("scan").note_error(exc)
            self._scan_due = time.monotonic() + SCAN_INTERVAL_ALONE
        finally:
            with self._scan_lock:
                self._scan_running = False

    # -- diagnostics -------------------------------------------------------

    def diagnostics_snapshot(self) -> dict[str, Any]:
        """Everything the settings page's 诊断 card needs (charter E2)."""
        peers = self.peers.all()
        channels = self.diagnostics.snapshot()
        for name, stats in channels.items():
            stats["enabled"] = self.channel_enabled(name)
        suggestions = self.suggestions(peers)
        return {
            "port": self.port,
            "interfaces": [
                {"name": i.name, "address": i.address, "netmask": i.netmask} for i in self._interfaces
            ],
            "addresses": self._local_addresses(),
            "peers": len(peers),
            "known": len(self.peers.known_all()),
            "channels": channels,
            "lastScan": dict(self._last_scan),
            "scanErrors": list(self._scan_errors[-5:]),
            "notes": list(self.diagnostics.notes[-20:]),
            "suggestions": suggestions,
            "firstAnnounceMs": (
                round((self._first_announce_at - self._started_at) * 1000, 1)
                if self._first_announce_at and self._started_at
                else None
            ),
        }

    def channel_enabled(self, name: str) -> bool:
        if name == "multicast":
            return bool(self.enable_multicast and self._senders)
        if name == "broadcast":
            return bool(self.enable_broadcast and self._broadcast_rx)
        if name == "mdns":
            return bool(self.enable_mdns and self._mdns_rx)
        if name == "scan":
            return bool(self.enable_scan)
        return True

    def suggestions(self, peers: list[Peer] | None = None) -> list[dict[str, str]]:
        """A short "为什么看不到对方" checklist, in Chinese (charter C13/E2)."""
        peers = self.peers.all() if peers is None else peers
        out: list[dict[str, str]] = []
        if not self._interfaces:
            out.append(
                {
                    "code": "no-interface",
                    "text": "没有找到可用的网卡地址：请确认已连上 Wi-Fi 或网线。",
                }
            )
            return out
        if peers:
            names = "、".join(p.info.name for p in peers[:3])
            out.append({"code": "ok", "text": f"已发现 {len(peers)} 台设备：{names}。"})
        channels = self.diagnostics.channels
        if self.enable_broadcast and channels["broadcast"].received == 0 and channels["broadcast"].sent > 0:
            out.append(
                {
                    "code": "broadcast-blocked",
                    "text": "广播公告没有收到任何回应：路由器/AP 可能在拦广播（很多企业网络这样）。",
                }
            )
        if channels["multicast"].received == 0 and channels["multicast"].sent > 0:
            out.append(
                {
                    "code": "multicast-blocked",
                    "text": "组播公告没有收到任何回应：AP 的 IGMP snooping 常把自定义组播组剪掉。",
                }
            )
        if self.enable_mdns and channels["mdns"].received == 0 and channels["mdns"].sent > 0:
            out.append(
                {
                    "code": "mdns-blocked",
                    "text": "mDNS 没有回应：部分 AP 会拦 224.0.0.251:5353。",
                }
            )
        scan = channels["scan"]
        if scan.attempts and not scan.successes:
            out.append(
                {
                    "code": "scan-empty",
                    "text": (
                        "主动扫描扫遍本机网段也没有找到设备：可能开了客户端隔离（同一 Wi-Fi 的设备互相不可见），"
                        "或对方在另一个网段。可以手动输入 IP 或扫码连接。"
                    ),
                }
            )
        if not peers and self._interfaces:
            out.append(
                {
                    "code": "firewall",
                    "text": (
                        f"本机监听 TCP {self.device_info().tcp_port} / UDP {self.port}；"
                        "如果对方也开着韧传仍看不到，请在这台电脑和对方的防火墙上放行这两个端口。"
                    ),
                }
            )
        return out

    # -- direct ------------------------------------------------------------

    def add_manual(self, address: str, port: int, name: str = "") -> Peer:
        """Register a peer the user typed in (or scanned from a QR code)."""
        info = DeviceInfo(
            device_id=f"manual:{address}:{port}",
            name=name or address,
            tcp_port=port,
        )
        peer, _is_new = self.peers.add_direct(info, address, port, "manual")
        self.diagnostics.channel("manual").note_success(peer)
        self.events.emit("device_found", peer=peer, is_new=True)
        return peer

    def interfaces(self) -> list[Interface]:
        return list(self._interfaces)

    def last_announce_at(self) -> float:
        return self._last_announce

    def first_announce_delay_ms(self) -> float | None:
        if not self._first_announce_at or not self._started_at:
            return None
        return (self._first_announce_at - self._started_at) * 1000.0


__all__ = [
    "ANNOUNCE_SCHEDULE",
    "ANNOUNCE_STEADY_INTERVAL",
    "Announcement",
    "ChannelStats",
    "Diagnostics",
    "DiscoveryService",
    "KnownPeer",
    "PeerStore",
    "ScanTarget",
    "explain_socket_error",
    "plan_scan_targets",
]
