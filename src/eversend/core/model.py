"""Domain objects shared by every layer of EverSend."""

from __future__ import annotations

import os
import platform
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .constants import chunk_count, pick_chunk_size


def default_device_name() -> str:
    """A friendly name for this device."""
    try:
        name = socket.gethostname()
    except OSError:
        name = "unknown"
    name = name.split(".")[0].strip() or "device"
    return name


def platform_tag() -> str:
    """A short platform label used in the UI and in device records."""
    if "ANDROID_ROOT" in os.environ:
        return "android"
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    if system == "linux":
        return "linux"
    return system or "unknown"


def device_kind() -> str:
    """Coarse device category: ``desktop``, ``mobile`` or ``server``."""
    tag = platform_tag()
    if tag == "android":
        return "mobile"
    if tag in ("ios", "ipados"):
        return "mobile"
    if tag in ("linux", "windows", "macos"):
        return "desktop"
    return "server"


@dataclass(slots=True)
class DeviceInfo:
    """Identity and capabilities of a device, exchanged during discovery/HELLO."""

    device_id: str
    name: str
    kind: str = field(default_factory=device_kind)
    platform: str = field(default_factory=platform_tag)
    version: str = ""
    tcp_port: int = 0
    web_port: int = 0
    #: X25519 public key (base64), used to derive the session key.
    x25519_pub: str = ""
    #: Ed25519 public key (base64), used to authenticate the device.
    ed25519_pub: str = ""
    #: Advertised capabilities, e.g. ``{"resume": True, "web": True}``.
    capabilities: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "deviceId": self.device_id,
            "name": self.name,
            "kind": self.kind,
            "platform": self.platform,
            "version": self.version,
            "tcpPort": self.tcp_port,
            "webPort": self.web_port,
            "x25519": self.x25519_pub,
            "ed25519": self.ed25519_pub,
            "caps": self.capabilities,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceInfo:
        return cls(
            device_id=str(data.get("deviceId", "")),
            name=str(data.get("name", "unknown")),
            kind=str(data.get("kind", "desktop")),
            platform=str(data.get("platform", "unknown")),
            version=str(data.get("version", "")),
            tcp_port=int(data.get("tcpPort", 0) or 0),
            web_port=int(data.get("webPort", 0) or 0),
            x25519_pub=str(data.get("x25519", "")),
            ed25519_pub=str(data.get("ed25519", "")),
            capabilities=dict(data.get("caps") or {}),
        )


@dataclass(slots=True)
class Peer:
    """A device discovered on the network, with liveness bookkeeping."""

    info: DeviceInfo
    address: str
    port: int
    last_seen: float = field(default_factory=time.monotonic)
    #: How the peer was found: ``multicast``, ``broadcast``, ``mdns``,
    #: ``scan``, ``manual`` or ``incoming`` (it connected to us).
    source: str = "multicast"
    #: Round-trip time of the last probe, in milliseconds (0 = unknown).
    rtt_ms: float = 0.0
    trusted: bool = False
    #: True when this peer's address belongs to this very machine, i.e. it is
    #: another EverSend instance running locally rather than a device on the
    #: network.  Purely a display hint.
    same_host: bool = False
    #: Direct addresses the peer advertised for itself, if any.
    addresses: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Stable key for de-duplication across discovery channels."""
        return self.info.device_id or f"{self.address}:{self.port}"

    def is_alive(self, ttl: float) -> bool:
        return (time.monotonic() - self.last_seen) < ttl

    def touch(self, address: str | None = None, port: int | None = None) -> None:
        self.last_seen = time.monotonic()
        if address:
            self.address = address
        if port:
            self.port = port


@dataclass(slots=True)
class FileEntry:
    """One file inside an offered transfer.

    ``index`` (a.k.a. ``seq``) is the stable identifier used on the wire; it is
    simply the position in the offer's file list so both sides agree without
    exchanging ids.
    """

    index: int
    name: str
    size: int
    mtime_ns: int = 0
    #: Relative sub-directory to recreate on the receiver ('' for the root).
    rel_dir: str = ""
    mode: int = 0o644
    #: Chunk size chosen for this file.  Derived deterministically from the
    #: size, so it is recomputed rather than trusted from the peer.
    chunk_size: int = 0
    #: Whole-file digest (hex, blake2b-256) computed by the sender.
    digest: str = ""
    #: Set by the receiver when the file is skipped (already present).
    skipped: bool = False

    def __post_init__(self) -> None:
        if self.chunk_size <= 0:
            self.chunk_size = pick_chunk_size(self.size)

    @property
    def nchunks(self) -> int:
        return chunk_count(self.size, self.chunk_size)

    def to_dict(self) -> dict[str, Any]:
        return {
            "i": self.index,
            "n": self.name,
            "s": self.size,
            "m": self.mtime_ns,
            "d": self.rel_dir,
            "p": self.mode,
            "h": self.digest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileEntry:
        return cls(
            index=int(data.get("i", 0)),
            name=str(data.get("n", "")),
            size=int(data.get("s", 0)),
            mtime_ns=int(data.get("m", 0) or 0),
            rel_dir=str(data.get("d", "") or ""),
            mode=int(data.get("p", 0o644) or 0o644),
            digest=str(data.get("h", "") or ""),
        )


@dataclass(slots=True)
class TransferStats:
    """Live counters for one transfer direction."""

    total_bytes: int = 0
    done_bytes: int = 0
    total_files: int = 0
    done_files: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    started_at: float = field(default_factory=time.monotonic)
    #: Rolling samples of (timestamp, cumulative bytes) for the speed graph.
    samples: list[tuple[float, int]] = field(default_factory=list)

    @property
    def elapsed(self) -> float:
        return max(1e-6, time.monotonic() - self.started_at)

    @property
    def speed_bps(self) -> float:
        """Average speed in bytes/second since the transfer started."""
        return self.done_bytes / self.elapsed

    @property
    def instant_speed_bps(self) -> float:
        """Recent speed, smoothed enough not to be nonsense.

        Progress arrives in coarse jumps -- the receiver throttles its reports
        to a few per second, which at multi-gigabit rates means each sample can
        be a hundred megabytes.  Measuring across the two most recent samples
        then reports whatever the transfer happened to be doing for those few
        milliseconds, so the window is widened and a minimum span is required
        before a figure is trusted at all.
        """
        now = time.monotonic()
        window = [s for s in self.samples if now - s[0] <= 3.0]
        if len(window) < 2:
            return self.speed_bps
        (t0, b0), (t1, b1) = window[0], window[-1]
        dt = t1 - t0
        if dt < 0.4:
            # Too short a span to be meaningful; fall back to the average,
            # which is at least honest.
            return self.speed_bps
        return max(0.0, (b1 - b0) / dt)

    def record(self, done_bytes: int) -> None:
        self.done_bytes = done_bytes
        now = time.monotonic()
        self.samples.append((now, done_bytes))
        # Keep the history bounded (about 5 minutes at 4 samples/second).
        if len(self.samples) > 1200:
            del self.samples[: len(self.samples) - 1200]

    @property
    def eta_seconds(self) -> float | None:
        speed = self.instant_speed_bps
        if speed <= 0:
            return None
        remaining = self.total_bytes - self.done_bytes
        if remaining <= 0:
            return 0.0
        return remaining / speed

    @property
    def percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(100.0, 100.0 * self.done_bytes / self.total_bytes)


@dataclass(slots=True)
class TransferItem:
    """Progress of one file within a transfer."""

    entry: FileEntry
    done_bytes: int = 0
    status: str = "pending"  # pending|active|done|failed|skipped
    error: str = ""
    #: Path the file is being written to (receiver side) or read from (sender).
    path: str = ""

    @property
    def percent(self) -> float:
        if self.entry.size <= 0:
            return 100.0 if self.status == "done" else 0.0
        return min(100.0, 100.0 * self.done_bytes / self.entry.size)


def new_transfer_id(files: list[FileEntry], sender_id: str) -> str:
    """Deterministic transfer identifier.

    Both peers can recompute it from the offer, so a transfer that is
    interrupted and re-offered maps onto the same on-disk resume journal
    without any extra round trip.  It deliberately depends only on the file
    set (name/size/mtime) and the sender, not on wall-clock time.
    """
    import hashlib

    hasher = hashlib.blake2b(digest_size=16)
    hasher.update(sender_id.encode("utf-8", "replace"))
    hasher.update(b"\x00")
    for entry in sorted(files, key=lambda f: (f.rel_dir, f.name)):
        hasher.update(f"{entry.rel_dir}/{entry.name}\x1f{entry.size}\x1f{entry.mtime_ns}\x1e".encode("utf-8", "replace"))
    return hasher.hexdigest()


def safe_join(root: str, rel_dir: str, name: str) -> str:
    """Join ``name`` under ``root/rel_dir`` refusing to escape ``root``.

    A malicious or buggy sender must never be able to write outside the
    chosen destination directory, so every path component is sanitised and
    the result is verified to stay inside ``root``.
    """
    root_abs = os.path.abspath(root)

    parts: list[str] = []
    for raw in (rel_dir or "").replace("\\", "/").split("/"):
        cleaned = sanitize_component(raw)
        if cleaned:
            parts.append(cleaned)
    target_dir = os.path.join(root_abs, *parts) if parts else root_abs

    final = os.path.join(target_dir, sanitize_component(name))
    final_abs = os.path.abspath(final)

    if final_abs != root_abs and not final_abs.startswith(root_abs + os.sep):
        raise ValueError(f"refusing to write outside the destination: {name!r}")
    return final_abs


_UNSAFE_CHARS = '<>:"/\\|?*'


def sanitize_component(name: str) -> str:
    """Make one path component safe on every supported platform."""
    name = (name or "").strip().replace("\x00", "")
    # Strip directory separators and Windows-reserved characters.
    out = "".join(("_" if ch in _UNSAFE_CHARS else ch) for ch in name)
    out = out.rstrip(" .")  # Windows cannot have trailing dot/space
    if out in ("", ".", ".."):
        return ""
    # Windows reserved device names.
    stem = out.split(".")[0].upper()
    if stem in {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }:
        out = "_" + out
    # Keep well below the 255-byte limit of common filesystems while leaving
    # room for the ".eversend.part" suffix.
    encoded = out.encode("utf-8", "replace")
    if len(encoded) > 200:
        keep = 200
        while keep > 1 and (encoded[:keep].decode("utf-8", "ignore").encode("utf-8") != encoded[:keep]):
            keep -= 1
        out = encoded[:keep].decode("utf-8", "ignore")
    return out


def unique_path(path: str) -> str:
    """Return ``path`` or ``path (1)``, ``path (2)``... whichever is free."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for counter in range(1, 10000):
        candidate = f"{stem} ({counter}){ext}"
        if not os.path.exists(candidate):
            return candidate
    return f"{stem} ({uuid.uuid4().hex[:8]}){ext}"


def human_bytes(value: float) -> str:
    """Format a byte count for display."""
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    size = float(value)
    for unit in units:
        if abs(size) < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


def human_speed(bps: float) -> str:
    return f"{human_bytes(bps)}/s"


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


__all__ = [
    "DeviceInfo",
    "FileEntry",
    "Peer",
    "TransferItem",
    "TransferStats",
    "default_device_name",
    "device_kind",
    "human_bytes",
    "human_duration",
    "human_speed",
    "new_transfer_id",
    "platform_tag",
    "safe_join",
    "sanitize_component",
    "unique_path",
]
