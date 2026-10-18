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


# ---------------------------------------------------------------------------
# Device identity records (``web_clients.json`` / ``known_devices.json``)
# ---------------------------------------------------------------------------
#
# Charter C4: 同一台物理设备在列表里永远只有一行。  The registries these files
# hold are keyed by *whatever spelling of an identity the build of the day
# used* -- ``android:<deviceId>``, ``web:<deviceId>``, the bare device id, or a
# legacy ``<address>|<ua-digest>`` -- so one phone could be written twice (the
# real ``data/web_clients.json`` in this repo has exactly that: an
# ``android:instrumented`` record *and* an ``android:<uuid>`` record that the
# first one already lists as its own alias).
#
# The helpers below decide, from the records alone, which spellings denote one
# device, and fold a list of records into one row per device while keeping
# every old spelling -- and the name it displayed back then -- reachable
# (``aliases`` / ``aliasLabels``), because conversations are stored under the
# spelling that was current when they were written.

#: Prefixes a client key may carry.
DEVICE_KEY_PREFIXES = ("web:", "android:")

#: How many old identities one record keeps (the same bound ``web/server.py``
#: uses; a record must not grow without bound).
DEVICE_ALIAS_LIMIT = 8

#: Fallback display names, only used when a record carries no name at all and
#: none of its aliases remembers one.  The wording matches the three clients.
_DEVICE_KIND_LABELS = {
    "app": "安卓 App",
    "android": "安卓 App",
    "mobile": "安卓 App",
    "browser": "浏览器",
    "web": "浏览器",
    "desktop": "电脑端",
    "pc": "电脑端",
}

#: What a device with no name at all is called (same words ``chat.py`` uses).
UNKNOWN_DEVICE_LABEL = "未知设备"


def bare_device_id(value: Any) -> str:
    """``web:<id>`` / ``android:<id>`` -> ``<id>`` (anything else unchanged)."""
    text = str(value or "").strip()
    for prefix in DEVICE_KEY_PREFIXES:
        if text.startswith(prefix) and len(text) > len(prefix):
            return text[len(prefix):]
    return text


def device_record_names(record: dict[str, Any]) -> list[str]:
    """Every spelling of one device's identity that this record knows about."""
    values: list[Any] = [record.get("key"), record.get("deviceId")]
    aliases = record.get("aliases")
    if isinstance(aliases, (list, tuple, set)):
        values.extend(aliases)
    remembered = record.get("aliasLabels")
    if isinstance(remembered, dict):
        values.extend(remembered.keys())
    # ``known_devices.json`` snapshots spell the id with an underscore.
    values.append(record.get("device_id"))
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def device_record_tokens(record: dict[str, Any]) -> set[str]:
    """The identity tokens that say "this is the same device".

    A token is a *stable identity*, never an address: the same address can be
    two browsers (or a phone behind ``adb reverse``), and folding those
    together is the opposite bug (two devices shown as one).
    """
    tokens: set[str] = set()
    for name in device_record_names(record):
        bare = bare_device_id(name)
        tokens.add(name)
        if bare:
            tokens.add(bare)
            for prefix in DEVICE_KEY_PREFIXES:
                tokens.add(prefix + bare)
    return tokens


def device_record_identity(record: dict[str, Any]) -> str:
    """The canonical identity of a device record (for reports and dedup)."""
    device_id = bare_device_id(record.get("deviceId") or record.get("device_id") or "")
    if device_id:
        return f"dev:{device_id}"
    key = str(record.get("key") or "").strip()
    if key:
        return f"key:{bare_device_id(key)}"
    return ""


def device_record_aliases(record: dict[str, Any]) -> list[str]:
    """The old spellings a record already carries (tolerating a bad field)."""
    bucket = record.get("aliases")
    if not isinstance(bucket, (list, tuple)):
        return []
    out: list[str] = []
    for alias in bucket:
        text = str(alias or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def device_record_label(record: dict[str, Any]) -> str:
    """The name to show for a record -- never empty (charter C4/C7).

    Order: the record's own name, then the name it displayed under an older
    spelling (``aliasLabels``), then a name derived from its kind, and only
    then 「未知设备」.  An empty name is what the user sees as a blank device
    row, so this function is deliberately incapable of returning ``""``.
    """
    for field in ("label", "name"):
        text = str(record.get(field) or "").strip()
        if text:
            return text
    remembered = record.get("aliasLabels")
    if isinstance(remembered, dict):
        newest = ""
        for info in remembered.values():
            if isinstance(info, dict):
                text = str(info.get("label") or "").strip()
                if text:
                    newest = text
        if newest:
            return newest
    kind = str(record.get("kind") or record.get("platform") or "").strip().lower()
    return _DEVICE_KIND_LABELS.get(kind, UNKNOWN_DEVICE_LABEL)


def _record_time(record: dict[str, Any], field: str) -> float:
    try:
        return float(record.get(field) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _carry_alias(dst: dict[str, Any], alias: str, source: dict[str, Any] | None = None) -> None:
    """Remember ``alias`` on ``dst``, together with the name it showed then."""
    text = str(alias or "").strip()
    if not text or text == str(dst.get("key") or ""):
        return
    bucket = dst.get("aliases")
    if not isinstance(bucket, list):
        bucket = []
        dst["aliases"] = bucket
    if text not in bucket:
        bucket.append(text)
        del bucket[: -DEVICE_ALIAS_LIMIT]
    if source is None:
        return
    remembered = dst.get("aliasLabels")
    if not isinstance(remembered, dict):
        remembered = {}
        dst["aliasLabels"] = remembered
    remembered.setdefault(
        text,
        {
            "label": str(source.get("label") or source.get("name") or ""),
            "kind": str(source.get("kind") or ""),
            "version": str(source.get("version") or ""),
            "address": str(source.get("address") or ""),
        },
    )
    while len(remembered) > DEVICE_ALIAS_LIMIT:
        remembered.pop(next(iter(remembered)))


def merge_device_records(
    records: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fold a device registry into one record per device.

    Returns ``(records, report)``.  The rules, in the order they matter:

    * two records belong to the same device when they share any stable
      identity token (:func:`device_record_tokens`) -- same device id in any
      spelling, or one listing the other's key as an alias;
    * the survivor is the record seen most recently (it knows the current
      address and name); ``firstSeen`` keeps the *earliest* of the group and
      ``lastSeen`` the latest, so merging never makes a device look older or
      more offline than it is;
    * the survivor shows the newest name; if it has none it inherits one, and
      every folded-away record's key is stored in ``aliases`` with its
      then-current label in ``aliasLabels`` -- old conversations keep their
      title (charter C4: aliases 保留可回溯);
    * a record with no name at all still gets one (:func:`device_record_label`),
      so no empty device row can come out of here.

    ``report`` counts what happened: ``records``, ``kept``, ``merged`` (how many
    records were folded away), ``renamed`` and ``groups``.
    """
    items = [dict(record) for record in records if isinstance(record, dict)]
    report: dict[str, Any] = {"records": len(items), "kept": len(items), "merged": 0, "groups": []}
    if not items:
        return [], report

    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[max(root_left, root_right)] = min(root_left, root_right)

    owner: dict[str, int] = {}
    for index, record in enumerate(items):
        for token in device_record_tokens(record):
            if token in owner:
                union(owner[token], index)
            else:
                owner[token] = index

    groups: dict[int, list[int]] = {}
    for index in range(len(items)):
        groups.setdefault(find(index), []).append(index)

    merged_rows: list[dict[str, Any]] = []
    for members in groups.values():
        if len(members) == 1:
            row = items[members[0]]
        else:
            newest = max(
                members,
                key=lambda i: (_record_time(items[i], "lastSeen"), _record_time(items[i], "firstSeen")),
            )
            row = dict(items[newest])
            names: list[tuple[float, dict[str, Any]]] = []
            for index in members:
                source = items[index]
                if index != newest:
                    _carry_alias(row, str(source.get("key") or ""), source)
                    for alias in device_record_aliases(source):
                        _carry_alias(row, alias)
                    remembered = source.get("aliasLabels")
                    if isinstance(remembered, dict):
                        for alias, info in remembered.items():
                            if isinstance(info, dict):
                                _carry_alias(row, str(alias), info)
                    for field in ("label", "name", "kind", "version", "address", "agent",
                                  "deviceId", "device_id"):
                        if not str(row.get(field) or "").strip() and str(source.get(field) or "").strip():
                            row[field] = source[field]
                    if source.get("talked"):
                        row["talked"] = True
                    if source.get("trusted"):
                        row["trusted"] = True
                if str(source.get("label") or source.get("name") or "").strip():
                    names.append((_record_time(source, "lastSeen"), source))
            firsts = [t for t in (_record_time(items[i], "firstSeen") for i in members) if t]
            lasts = [t for t in (_record_time(items[i], "lastSeen") for i in members) if t]
            if firsts:
                row["firstSeen"] = min(firsts)
            if lasts:
                row["lastSeen"] = max(lasts)
            # One name for the row: the newest record that had one (a record that
            # was renamed by a merge elsewhere must not end up half-named).
            if names and not str(row.get("label") or "").strip():
                names.sort(key=lambda item: item[0])
                row["label"] = str(names[-1][1].get("label") or names[-1][1].get("name") or "")
            report["merged"] += len(members) - 1
            report["groups"].append(
                [str(items[i].get("key") or device_record_identity(items[i])) for i in members]
            )
        label = device_record_label(row)
        if str(row.get("label") or "") != label:
            report["renamed"] = int(report.get("renamed") or 0) + 1
        row["label"] = label
        if not str(row.get("name") or "").strip():
            row["name"] = label
        merged_rows.append(row)

    merged_rows.sort(key=lambda row: _record_time(row, "lastSeen"), reverse=True)
    report["kept"] = len(merged_rows)
    report["groups"] = [g for g in report["groups"]]
    return merged_rows, report


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
    """Format a byte count for display.

    The divisor is 1024, so the units must be the IEC ones (KiB/MiB/GiB…):
    labelling 1024-based sizes as KB/MB under-reports every number by 4.6%
    (512 MiB showed as "512.00 MB" where SI would be "536.87 MB").  All three
    front ends share this one implementation (JS mirrors it in
    ``web/assets/app.js``) so the number and the label always agree.
    """
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    size = float(value)
    for unit in units:
        if abs(size) < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PiB"


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


# ---------------------------------------------------------------------------
# Name disambiguation and ghost-device reclamation (charter C4 / C7)
# ---------------------------------------------------------------------------
#
# Two laptops can honestly be called "xgl-pc", and one machine can honestly
# produce several rows: reinstall it, or start a second instance with a
# temporary data directory, and a brand-new ``deviceId`` appears while the old
# record is still on disk.  The first is a *display* problem (three rows all
# saying "RealProbe" is what the user reports as 设备重复显示), the second is a
# *garbage* problem.  Both are solved here, once, so the desktop, the web page
# and the Android app show the same names and drop the same ghosts.

#: How a disambiguated name is spelled (``RealProbe #2``).
DISAMBIGUATED_NAME = "{name} #{index}"

#: The ordering fields a device may expose, in the spellings used across the
#: three clients (``Peer``/``DeviceInfo`` objects and plain dicts alike).
_ID_FIELDS = ("device_id", "deviceId", "id", "key")
_NAME_FIELDS = ("name", "label")
_ADDRESS_FIELDS = ("address", "ip", "host")
_PORT_FIELDS = ("port", "tcp_port", "tcpPort")
_PLATFORM_FIELDS = ("platform", "kind")
_SEEN_FIELDS = ("lastSeen", "last_seen")
_ONLINE_FIELDS = ("online",)


def device_value(device: Any, fields: Iterable[str], default: Any = None) -> Any:
    """Read one field from a device dict, a ``Peer`` or a ``DeviceInfo``.

    The three clients hand this module three different shapes, so the lookup is
    deliberately tolerant: ``Peer.info`` is consulted first (that is where a
    peer's id and name really live), then the object or dict itself.
    """
    sources: list[Any] = []
    info = getattr(device, "info", None)
    if info is not None:
        sources.append(info)
    sources.append(device)
    for source in sources:
        for field in fields:
            if isinstance(source, dict):
                value = source.get(field)
            else:
                value = getattr(source, field, None)
            if value not in (None, ""):
                return value
    return default


def _device_number(device: Any, fields: Iterable[str]) -> float:
    try:
        return float(device_value(device, fields, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def device_stable_id(device: Any) -> str:
    """The id to key a device row by (never a display name, never an address)."""
    return str(device_value(device, _ID_FIELDS, "") or "").strip()


def device_display_name(device: Any) -> str:
    """The device's own name -- never empty (charter C4)."""
    return str(device_value(device, _NAME_FIELDS, "") or "").strip() or UNKNOWN_DEVICE_LABEL


def device_is_online(device: Any) -> bool:
    """Whether a device is reachable right now."""
    value = device_value(device, _ONLINE_FIELDS, None)
    if value is None:
        caps = device_value(device, ("capabilities", "caps"), None) or {}
        if isinstance(caps, dict):
            value = caps.get("online")
        elif hasattr(caps, "get"):
            value = caps.get("online")
    return bool(value)


def disambiguate_names(devices: Iterable[Any]) -> dict[str, str]:
    """A display name for every device, with duplicates numbered.

    ``{"dev-a": "RealProbe", "dev-b": "RealProbe #2", "dev-c": "RealProbe #3"}``

    The *primary* -- the one that keeps the plain name -- is the device a user
    is most likely to mean: online first, then most recently seen, then the
    lowest id (so the answer never depends on iteration order).  Devices whose
    names already differ are returned unchanged, and one device appearing twice
    in the input keeps exactly one name.  Everything is keyed by stable id, so
    callers can look a row up without re-deriving anything.
    """
    entries: dict[str, dict[str, Any]] = {}
    for device in devices:
        device_id = device_stable_id(device)
        if not device_id:
            continue
        candidate = {
            "name": device_display_name(device),
            "online": device_is_online(device),
            "seen": _device_number(device, _SEEN_FIELDS),
        }
        current = entries.get(device_id)
        if current is None or (candidate["online"], candidate["seen"]) > (
            current["online"],
            current["seen"],
        ):
            entries[device_id] = candidate

    by_name: dict[str, list[str]] = {}
    for device_id, entry in entries.items():
        by_name.setdefault(entry["name"], []).append(device_id)

    names: dict[str, str] = {}
    for name, device_ids in by_name.items():
        if len(device_ids) == 1:
            names[device_ids[0]] = name
            continue
        ordered = sorted(
            device_ids,
            key=lambda d: (
                not entries[d]["online"],
                -entries[d]["seen"],
                d,
            ),
        )
        for index, device_id in enumerate(ordered, start=1):
            names[device_id] = name if index == 1 else DISAMBIGUATED_NAME.format(
                name=name, index=index
            )
    return names


def prune_device_records(
    records: Iterable[dict[str, Any]],
    *,
    now: float = 0.0,
    offline_ttl: float = 0.0,
    limit: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reclaim device rows that are provably ghosts of a machine we still have.

    A record is dropped only when *another* record proves it is the same
    machine, so two devices behind one NAT address survive together:

    * **same address and same port**: nothing else can be listening there, so
      the older row is the previous instance (reinstall, temporary data
      directory) -- dropped once it is offline;
    * **same address and same platform** (both non-empty) and offline for
      longer than ``offline_ttl``: the classic "the new instance reports a new
      deviceId, the old row lingers" ghost;
    * **no identity at all** (no key, no device id): un-addressable garbage;
    * beyond ``limit`` rows, the oldest *offline* ones fall off.

    A record is never dropped while it is recent, and a record whose platform
    differs from its neighbour is never treated as the same host -- two
    different machines on one address keep both rows, which is the
    over-merging mistake this rule is written to avoid.  Returns
    ``(kept, report)`` where the report names every drop and its survivor.
    """
    items = [dict(record) for record in records if isinstance(record, dict)]
    moment = float(now or 0.0)
    ttl = float(offline_ttl or 0.0)

    def stale(record: dict[str, Any]) -> bool:
        if ttl <= 0 or moment <= 0:
            return True
        return moment - _record_time(record, "lastSeen") > ttl

    def identity(record: dict[str, Any]) -> str:
        return device_record_identity(record) or str(record.get("key") or "")

    dropped: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for index, record in enumerate(items):
        if not identity(record):
            dropped.append({"key": str(record.get("key") or ""), "reason": "记录没有可寻址的身份"})
            continue
        if is_pending_record(record):
            dropped.append({"key": str(record.get("key") or ""), "reason": "__pending__ 占位记录"})
            continue
        address = str(record.get("address") or "").strip()
        port = str(record.get("port") or "").strip()
        # Only ``platform`` -- never ``kind``: every browser record on a machine
        # has the same ``kind`` ("browser") and shares 127.0.0.1, so keying the
        # "same host" rule off the kind folded two different browsers into one
        # (measured on this repo's own ``data/web_clients.json``).  A record
        # without a platform is left alone.
        platform = str(record.get("platform") or "").strip().lower()
        reason = ""
        survivor = ""
        for other in items:
            if other is record or not address:
                continue
            other_address = str(other.get("address") or "").strip()
            if other_address != address or identity(other) == identity(record):
                continue
            if _record_time(other, "lastSeen") < _record_time(record, "lastSeen"):
                continue
            other_port = str(other.get("port") or "").strip()
            other_platform = str(other.get("platform") or "").strip().lower()
            if (
                port
                and other_port
                and port == other_port
                # A conflicting platform is evidence that these are *not* the
                # same machine (our data is then inconsistent, which is not a
                # reason to delete someone's device row): keep both.  Only a
                # matching or unknown platform may be reclaimed.
                and (not platform or not other_platform or platform == other_platform)
                and stale(record)
            ):
                reason = "同一地址+端口被新实例接管"
            elif platform and platform == other_platform and stale(record):
                reason = "同一台机器的新身份（同地址同平台，旧行已离线）"
            if reason:
                survivor = str(other.get("key") or identity(other))
                break
        if reason:
            dropped.append(
                {"key": str(record.get("key") or ""), "reason": reason, "survivor": survivor}
            )
            continue
        kept.append(record)

    if limit and len(kept) > limit:
        kept.sort(key=lambda row: _record_time(row, "lastSeen"), reverse=True)
        for record in kept[limit:]:
            dropped.append({"key": str(record.get("key") or ""), "reason": f"超过 {limit} 条上限"})
        kept = kept[:limit]

    report = {"records": len(items), "kept": len(kept), "dropped": len(dropped), "reasons": dropped}
    return kept, report


def is_pending_record(record: dict[str, Any]) -> bool:
    """Whether a record is the desktop's ``__pending__`` placeholder."""
    for field in ("key", "deviceId", "device_id", "name", "label"):
        if "__pending__" in str(record.get(field) or ""):
            return True
    return False


__all__ = [
    "DEVICE_ALIAS_LIMIT",
    "DEVICE_KEY_PREFIXES",
    "DISAMBIGUATED_NAME",
    "DeviceInfo",
    "FileEntry",
    "Peer",
    "TransferItem",
    "TransferStats",
    "UNKNOWN_DEVICE_LABEL",
    "bare_device_id",
    "default_device_name",
    "device_display_name",
    "device_is_online",
    "device_kind",
    "device_record_aliases",
    "device_record_identity",
    "device_record_label",
    "device_record_names",
    "device_record_tokens",
    "device_stable_id",
    "device_value",
    "disambiguate_names",
    "human_bytes",
    "human_duration",
    "human_speed",
    "is_pending_record",
    "merge_device_records",
    "new_transfer_id",
    "platform_tag",
    "prune_device_records",
    "safe_join",
    "sanitize_component",
    "unique_path",
]
