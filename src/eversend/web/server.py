"""The mobile/Web front end of EverSend: a stdlib-only HTTP server.

PySide6 cannot run on Android, so an Android phone drives the engine through
its browser instead.  This module serves a small single-page app plus a JSON
API on port :data:`~eversend.core.constants.DEFAULT_WEB_PORT` and bridges it
to exactly the same :class:`~eversend.core.engine.Engine` API the desktop UI
uses -- the two front ends therefore cannot drift apart in behaviour.

Design constraints, in order of importance
------------------------------------------

1. **No third-party imports.**  The portable build ships two vendored wheels
   (``cryptography``, PySide6); a phone-facing server must not add a third.
   Everything here is ``http.server`` + ``json`` + ``hmac``.
2. **The LAN is hostile.**  Anyone on the Wi-Fi can reach this port, so: the
   asset path is confined to one directory by ``realpath``, every mutating
   request must carry a per-run CSRF token compared with
   :func:`hmac.compare_digest`, request bodies are capped, and a ``Host``
   header that is not an IP literal or ``localhost`` is refused (DNS
   rebinding).
3. **A phone falls asleep mid-request.**  Every streaming loop is bounded,
   every socket that can block forever is registered so :meth:`WebUI.stop`
   can unblock it, and every reader/unsubscriber runs in a ``finally``.
4. **Uploads must not be buffered.**  A phone can push gigabytes; the body is
   streamed straight to a spool file in chunks and then handled by the
   ordinary transfer engine.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import queue
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, ClassVar, Iterable, Iterator

from ..core.constants import APP_NAME, APP_NAME_CN, APP_VERSION, DEFAULT_WEB_PORT
from ..core.chat import (
    client_aliases,
    conversation_people,
    conversation_title,
    direct_conversation_id,
    media_relpath,
)
from ..core.engine import Engine, build_file_entries, free_space
from ..core.model import (
    DeviceInfo,
    Peer,
    disambiguate_names,
    new_transfer_id,
    sanitize_component,
    unique_path,
)
from . import qr

log = logging.getLogger("eversend.web")

#: Header carrying the CSRF token.  A header is preferred over a cookie
#: because the page is served from the same origin and never needs the token
#: to survive a navigation.
TOKEN_HEADER = "X-EverSend-Token"

#: Bodies of the JSON endpoints are tiny; the cap is what stops a hostile
#: client from making the server allocate a gigabyte.
MAX_JSON_BODY = 64 * 1024

#: Heartbeat comment interval on the event stream.  Well under the idle
#: timeout of every common reverse proxy and of Android's doze mode.
SSE_HEARTBEAT = 15.0

#: Read size when draining a request body.  Small enough that a slow phone
#: never makes us hold more than this, large enough to keep syscalls cheap.
IO_CHUNK = 256 * 1024

#: Default ceiling for one uploaded file: large enough for a 4K video,
#: small enough that a hostile LAN client cannot fill the disk unnoticed.
DEFAULT_MAX_UPLOAD = 32 * 1024 * 1024 * 1024

#: Port for the HTTPS interface (voice messages need a secure context).
DEFAULT_WEB_TLS_PORT = DEFAULT_WEB_PORT + 1

#: Hidden working directory for in-flight uploads, inside the receive dir.
_SPOOL_DIRNAME = ".eversend-uploads"

#: How long a resolved local-address list is reused, in seconds.
_ADDRESS_TTL = 30.0

#: How many files one session may keep published for the browser.  Bounded so
#: a long-running desktop does not accumulate entries (and so the phone's list
#: stays readable); the oldest hand-offs fall off first.
MAX_SHARES = 50

#: How long an open page counts as "connected" after its last request.  The
#: page polls ``/api/state`` every three seconds and keeps an event stream
#: open, so this survives a few dropped polls without leaving a phone listed
#: forever after it walks out of Wi-Fi range.
CLIENT_TTL = 15.0


def _as_float(value: Any) -> float:
    """A timestamp that survives a hand-edited / older ``web_clients.json``.

    The registry is loaded straight off disk, so a record may be missing fields
    or carry a string where a number belongs.  One bad row must never take
    ``/api/state`` (and with it the whole page) down.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def agent_digest(agent: str) -> str:
    """The short hash of a ``User-Agent``, i.e. the second half of a legacy key.

    Kept in one place because "is this the same browser?" is asked in three
    places (the registry key, the duplicate cleanup, the goodbye path) and two
    different digests would silently stop matching each other.
    """
    return hashlib.sha1((agent or "").encode("utf-8", "replace")).hexdigest()[:8]


def bare_device_id(device_id: str) -> str:
    """A client's device id without a ``web:`` / ``android:`` prefix.

    The prefix belongs to the *key* (``web:<id>``), not to the id: the page
    stores a bare UUID and the app posts a bare UUID, but a client that copied
    a key out of a listing must not end up registering as ``web:web:<id>``.
    """
    value = str(device_id or "").strip()
    for prefix in ("web:", "android:"):
        if value.startswith(prefix) and len(value) > len(prefix):
            return value[len(prefix):]
    return value


def client_label(client: dict[str, Any]) -> str:
    """「名字（安卓 App/网页版 · 韧传 版本 · IP）」—— 三个客户端显示的同一句话。

    只写一份：会话列表要显示的名字、以及归并掉的旧身份当时显示过的名字，都
    由它算出来，两边不许各写一套。
    """
    is_app = str(client.get("kind") or "") == "app"
    parts = ["安卓 App" if is_app else "网页版"]
    if client.get("version"):
        parts.append(f"韧传 {client['version']}")
    if client.get("address"):
        parts.append(str(client["address"]))
    return f"{client.get('label') or '手机'}（{' · '.join(parts)}）"


def is_app_agent(agent: str) -> bool:
    """Is this User-Agent our own Android app?

    Both the current build (``EverSend-Android/1.0``) and any future one are
    recognised, because the point is to tell "the app" apart from "a browser on
    the phone" when the two entries describe the same handset.
    """
    return "eversend-android" in (agent or "").lower()


def describe_agent(agent: str) -> str:
    """A short human label for a browser's ``User-Agent``.

    The desktop shows this next to the connected client, because "已连接手机：
    1 台" is much easier to trust when it also says *which* phone.
    """
    text = (agent or "").lower()
    if not text:
        return "未知客户端"
    # Scripts and health checks are not "a browser someone is holding": saying
    # so keeps the desktop's list from showing a mystery client.
    if any(token in text for token in ("urllib", "curl/", "wget", "http-client", "python-requests", "okhttp", "axios")):
        return "命令行/脚本"
    if is_app_agent(text):
        # The Android app identifies itself; saying "安卓 App" instead of
        # "浏览器" matters because the desktop lists it as a paired device and
        # the user has to recognise it there.
        return "安卓 App"
    if "micromessenger" in text:
        browser = "微信内置浏览器"
    elif "edg" in text:
        browser = "Edge"
    elif "firefox" in text:
        browser = "Firefox"
    elif "chrome" in text or "crios" in text:
        browser = "Chrome"
    elif "safari" in text:
        browser = "Safari"
    else:
        browser = "浏览器"

    if "android" in text:
        system = "Android"
    elif "iphone" in text:
        system = "iPhone"
    elif "ipad" in text:
        system = "iPad"
    elif "windows" in text:
        system = "Windows"
    elif "mac os" in text or "macintosh" in text:
        system = "macOS"
    elif "linux" in text:
        system = "Linux"
    else:
        system = ""

    if system:
        return f"{system} 上的 {browser}"
    return browser


def is_mobile_client(client: dict[str, Any]) -> bool:
    """True when a connected client looks like a phone or tablet.

    Not simply "not loopback": a phone that arrives through a port forward (an
    emulator, a reverse proxy, a hotspot) presents itself as 127.0.0.1, and the
    desktop should still recognise it as a phone.
    """
    label = str(client.get("label") or "")
    if any(word in label for word in ("Android", "iPhone", "iPad")):
        return True
    return not client.get("isLocal")

#: Content type per extension.  Spelled out rather than left to
#: :mod:`mimetypes` because the registry differs wildly between platforms and
#: a wrong type silently breaks the app (a ``.js`` served as ``text/plain`` is
#: refused by every modern browser).
_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
    ".apk": "application/vnd.android.package-archive",
}

#: Routes that only accept POST, so a GET can be answered with 405.
_POST_ONLY_ROUTES = frozenset(
    {"/api/announce", "/api/scan", "/api/upload", "/api/share", "/api/leave", "/api/hello", "/api/chat/send", "/api/chat/upload", "/api/offer/respond", "/api/cancel", "/api/trust", "/api/peer", "/api/devices/forget-offline"}
)

_JSON_TYPE = "application/json; charset=utf-8"


class _BodyTooLarge(Exception):
    """The request body exceeded the configured cap."""


class _BadRequest(Exception):
    """The request body could not be parsed."""


# ---------------------------------------------------------------------------
# serialisation helpers
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Convert engine objects into JSON-safe structures.

    The engine hands out live dataclasses (:class:`Peer`, :class:`DeviceInfo`,
    :class:`TransferStats`) rather than dicts.  Converting them generically
    here means a new field added to the core shows up in the API automatically
    instead of being silently dropped.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


def device_dict(info: DeviceInfo, **extra: Any) -> dict[str, Any]:
    """The public shape of a device, as every endpoint reports it."""
    data: dict[str, Any] = {
        "id": info.device_id,
        # 与 knownClients 一样给一个**稳定身份**字段：手机端拿它当设备行的 id
        # 去和会话成员（存的是设备号）对号入座。以前协议对端没有这个字段，
        # 安卓端 ``id = peer.identityKey`` 就拿到空串 —— 会话永远匹配不上，
        # 退化成按显示名猜（真机实测：标题显示成"与 e4233004… 的对话"）。
        "identityKey": info.device_id,
        "name": info.name,
        "kind": info.kind,
        "platform": info.platform,
        "version": info.version,
        "tcpPort": info.tcp_port,
        "webPort": info.web_port,
        "caps": dict(info.capabilities or {}),
    }
    data.update(extra)
    return data


def display_address(address: str) -> str:
    """Present an address the way a human expects to read it.

    A peer that reaches us over the IPv4 listener is recorded with an
    IPv4-mapped IPv6 address (``::ffff:192.168.1.50``) because that is what the
    socket reports.  It is the same host, but ``192.168.1.50`` is what belongs
    in the UI and in a URL a phone might open.
    """
    text = (address or "").strip()
    if text.lower().startswith("::ffff:") and "." in text:
        candidate = text[7:]
        try:
            ipaddress.IPv4Address(candidate.split("%")[0])
        except ValueError:
            return text
        return candidate
    return text


#: How an Android installer may be named to be picked up automatically.
_APK_NAMES = ("eversend-android.apk", "eversend.apk")


def _find_android_apk(directories: Iterable[str]) -> str | None:
    """Look for an Android installer in the given directories.

    Only names that are clearly ours are accepted.  A blanket ``*.apk`` would
    pick up whatever unrelated installer happens to sit in the working
    directory and offer it to a phone as "the app", which is worse than
    offering nothing.
    """
    for directory in directories:
        if not directory or not os.path.isdir(directory):
            continue
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            continue
        lowered = {name.lower(): name for name in entries}
        for wanted in _APK_NAMES:
            name = lowered.get(wanted)
            if name:
                return os.path.join(directory, name)
        for name in entries:  # versioned names like EverSend-1.0.0.apk
            low = name.lower()
            if low.endswith(".apk") and "eversend" in low:
                return os.path.join(directory, name)
        for name in entries:  # …and the Chinese name, 韧传-1.0.0.apk
            if name.lower().endswith(".apk") and "韧传" in name:
                return os.path.join(directory, name)
    return None


def peer_dict(peer: Peer) -> dict[str, Any]:
    """The public shape of a discovered peer."""
    primary = display_address(peer.address)
    addresses = [display_address(a) for a in (peer.addresses or []) if a]
    if primary and primary not in addresses:
        addresses.insert(0, primary)
    web_port = peer.info.web_port or 0
    web_url = ""
    if web_port and primary:
        host = f"[{primary}]" if ":" in primary else primary
        web_url = f"http://{host}:{web_port}/"
    return device_dict(
        peer.info,
        address=primary,
        port=peer.port,
        addresses=addresses,
        trusted=bool(peer.trusted),
        source=peer.source,
        rttMs=round(float(peer.rtt_ms), 1),
        lastSeen=round(time.time() - max(0.0, time.monotonic() - peer.last_seen), 1),
        webUrl=web_url,
    )


def transfer_dict(active: Any) -> dict[str, Any]:
    """Flatten one :class:`~eversend.core.engine.ActiveTransfer` for the UI."""
    stats = active.stats
    items = []
    for index in sorted(active.items):
        item = active.items[index]
        items.append(
            {
                "index": index,
                "name": item.entry.name,
                "size": item.entry.size,
                "done": item.done_bytes,
                "percent": round(item.percent, 2),
                "status": item.status,
                "error": item.error,
                "path": item.path,
            }
        )
    return {
        "transferId": active.transfer_id,
        "direction": active.direction,
        "peer": device_dict(active.peer),
        "status": "failed" if active.error else "active",
        "error": active.error,
        "bytes": stats.done_bytes,
        "total": stats.total_bytes,
        "percent": round(stats.percent, 2),
        "speed": round(stats.instant_speed_bps, 1),
        "eta": stats.eta_seconds,
        "files": stats.total_files,
        "doneFiles": stats.done_files,
        "failedFiles": stats.failed_files,
        "skippedFiles": stats.skipped_files,
        "elapsed": round(stats.elapsed, 2),
        "items": items,
    }


def event_dict(event: dict[str, Any]) -> dict[str, Any]:
    """Normalise one bus event so the browser never sees a raw dataclass."""
    kind = str(event.get("kind", ""))
    payload: dict[str, Any] = {"kind": kind, "ts": event.get("ts", time.time())}
    for key, value in event.items():
        if key in ("kind", "ts"):
            continue
        if key == "peer" and isinstance(value, DeviceInfo):
            payload["peer"] = device_dict(value)
        else:
            payload[key] = _jsonable(value)
    return payload


def _usable_address(address: str) -> bool:
    """True for an address a phone on the LAN could actually dial."""
    try:
        parsed = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    return not (parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified)


#: How long a caller waits for the resolver before giving up on it.
_RESOLVER_WAIT = 0.35

_resolver_lock = threading.Lock()
_resolver_done = threading.Event()
_resolver_found: list[str] = []
_resolver_launched = False


def _resolve_own_name() -> None:
    """Look our own host name up, on a thread nobody waits for."""
    found: list[str] = []
    try:
        for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = entry[4][0]
            if _usable_address(address):
                found.append(address)
    except OSError:
        pass
    with _resolver_lock:
        for address in found:
            if address not in _resolver_found:
                _resolver_found.append(address)
    _resolver_done.set()


def resolved_addresses(wait: float = _RESOLVER_WAIT) -> list[str]:
    """Addresses the system resolver has for our host name, maybe.

    The lookup runs on a daemon thread and this only waits ``wait`` seconds for
    it -- because ``getaddrinfo`` on our own name can block for tens of seconds
    and there is no way to time it out.  Measured on a macOS runner, where the
    very first ``/api/state`` request (a phone opening the page) took longer
    than the browser was willing to wait; on Linux the same call returns in
    microseconds, which is why only one platform ever showed it.

    A slow answer is not thrown away: the thread keeps running and the *next*
    call -- the page polls every three seconds -- picks it up.
    """
    global _resolver_launched
    if not _resolver_done.is_set():
        with _resolver_lock:
            if not _resolver_launched:
                _resolver_launched = True
                threading.Thread(
                    target=_resolve_own_name, name="eversend-resolver", daemon=True
                ).start()
        _resolver_done.wait(wait)
    with _resolver_lock:
        return list(_resolver_found)


def local_addresses() -> list[str]:
    """IPv4 addresses a phone on the same LAN could use to reach us.

    The first entry is the one the routing table would pick, which is right in
    every normal setup (one Wi-Fi/Ethernet interface).  The rest come from the
    resolver and cover the multi-homed case.  No packet is ever sent.
    """
    found: list[str] = []

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.5)
            # Connecting a UDP socket only selects a source address and route;
            # nothing is transmitted, so this works with no internet at all.
            probe.connect(("10.255.255.255", 1))
            candidate = probe.getsockname()[0]
        if _usable_address(candidate):
            found.append(candidate)
    except OSError:
        pass

    for address in resolved_addresses():
        if address not in found:
            found.append(address)

    return found


# ---------------------------------------------------------------------------
# the server
# ---------------------------------------------------------------------------


class WebUI:
    """Serves the phone-facing single-page app and its JSON API.

    One instance owns one HTTP listener plus a small amount of session state
    (the CSRF token, the pending-offer cache and the in-flight uploads).  It
    never owns the engine: the caller starts and stops that.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        host: str = "0.0.0.0",
        port: int = DEFAULT_WEB_PORT,
        asset_dir: str | None = None,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD,
        keep_uploads: bool = False,
        extra_hosts: Iterable[str] = (),
        advertise: bool = True,
        log_requests: bool = False,
        ssl_context: Any = None,
    ) -> None:
        self.engine = engine
        #: When set, the interface is served over HTTPS.  Voice messages need it
        #: (a browser only hands over the microphone in a secure context).
        self.ssl_context = ssl_context
        self.host = host
        self.port = int(port)
        self.asset_dir = os.path.realpath(asset_dir or os.path.join(os.path.dirname(__file__), "assets"))
        self.max_upload_bytes = int(max_upload_bytes)
        #: Keeping the spool file turns the receive directory into an upload
        #: inbox; the default is to delete it once the transfer is over so a
        #: phone upload does not silently consume disk twice.
        self.keep_uploads = bool(keep_uploads)
        self.extra_hosts = {h.strip().lower() for h in extra_hosts if h and h.strip()}
        self.advertise = bool(advertise)
        self.log_requests = bool(log_requests)

        #: Per-run CSRF secret.  Regenerated on every start, so a token
        #: swiped from an old page is worthless after a restart.
        self._token = secrets.token_urlsafe(32)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state_lock = threading.RLock()
        #: request_id -> offer snapshot, so a phone that connects *after* an
        #: offer arrived still sees it (the engine only emits an event).
        self._offers: dict[str, dict[str, Any]] = {}
        self._uploads: dict[str, dict[str, Any]] = {}
        self._uploads_done: list[dict[str, Any]] = []
        self._sse_sockets: set[socket.socket] = set()
        #: Who has a page open, keyed by IP address.  A phone is a *browser*
        #: client, so it can never show up in the peer list the way another
        #: computer does -- without this the desktop had no way at all to tell
        #: the user "your phone is connected", and clicking 「手机连接」 looked
        #: like it had done nothing.
        self._clients: dict[str, dict[str, Any]] = {}
        #: Every browser this desktop has paired with, keyed like ``_clients``.
        #: A phone is a *device you own*, not a session: its page gets frozen
        #: the moment the screen goes off or the user switches apps, so a list
        #: that forgot it after fifteen seconds would force the user to keep
        #: the phone awake and in the browser to stay paired.  It stays listed
        #: until the phone says goodbye (「断开连接」) or the user removes it.
        self._known: dict[str, dict[str, Any]] = {}
        self._known_path = os.path.join(engine.config.data_dir, "web_clients.json")
        #: device_id -> 最后一次见到的设备快照。引擎的发现表是有 TTL 的（几分钟
        #: 没消息就丢），于是"刚才还在、现在走了"的电脑在页面上会彻底消失 ——
        #: 用户要求设备列表分「在线 / 离线」，离线那一栏就得有东西可放。
        #: 这里只记身份与最后在线时间，不参与任何寻址。
        self._remembered: dict[str, dict[str, Any]] = {}
        #: Cached answer to "is there an Android installer to offer?" (see
        #: :meth:`android_apk`); the page asks on every state poll.
        self._apk_path: str | None = None
        self._apk_checked = 0.0
        self._apk_lock = threading.Lock()
        self._load_known()
        #: Files the desktop handed to the browser, by share id.  A browser has
        #: no receiving service, so "send to phone" cannot be a push: the
        #: desktop publishes the file and the phone picks it up with one tap.
        #: 分享列表会落盘（``shares.json``）：opt-1017 真机实测"电脑端重启后手机
        #: 下载到一半的分享 404"——手机想用 Range 续传，服务端却已经不认这个 id 了。
        #: 持久化后重启/崩溃都能接着取；加载时只保留文件仍然存在的条目。
        self._shares: dict[str, dict[str, Any]] = {}
        self._shares_path = os.path.join(engine.config.data_dir, "shares.json")
        self._load_shares()
        self._workers: set[threading.Thread] = set()
        self._upload_lock = threading.Lock()
        #: 可续传上传（opt-1017）：uploadId -> 该 id 的写锁，防止同一 id 并发写串。
        #: 键是客户端生成并校验过的十六进制串（见 ``valid_resume_id``）。
        self._resume_locks: dict[str, threading.Lock] = {}
        #: uploadId -> 完成响应快照。断线后客户端可能重发最后一段（幂等重放）：
        #: 已经完成的 id 直接回原响应，而不是把同一文件再落一次盘。
        self._resume_done: dict[str, dict[str, Any]] = {}
        self._index_cache: tuple[float, str] | None = None
        self._address_cache: tuple[float, list[str]] | None = None
        self._bound_port = 0

        # Tracking offers through the bus is the only way to support a client
        # that attaches late; the engine keeps pending offers private because
        # only a UI is ever interested in them.
        engine.events.subscribe(self._on_engine_event)

    # -- lifecycle ---------------------------------------------------------

    @property
    def token(self) -> str:
        """The CSRF token the served page must echo back on every POST."""
        return self._token

    def addresses(self) -> list[str]:
        """Local addresses, cached for a few seconds.

        :func:`local_addresses` may consult the resolver, and ``/api/state`` is
        polled every three seconds by every open tab; caching keeps a slow DNS
        lookup off the polling path without hiding an interface change for
        long.
        """
        with self._state_lock:
            cached = self._address_cache
            if cached is not None and time.monotonic() - cached[0] < _ADDRESS_TTL:
                return list(cached[1])
        found = local_addresses()
        with self._state_lock:
            self._address_cache = (time.monotonic(), found)
        return found

    @property
    def receive_dir(self) -> str:
        """Where received files land.

        The core exposes this only as ``config.receive_dir`` -- there is no
        ``Engine.receive_dir`` property -- so the lookup is tolerant: if the
        core ever grows one, this picks it up instead of shadowing it with the
        config value.
        """
        directory = getattr(self.engine, "receive_dir", None)
        if directory:
            return str(directory)
        return str(getattr(getattr(self.engine, "config", None), "receive_dir", "") or "")

    @property
    def data_dirs(self) -> tuple[str, ...]:
        """Where to look for an ``.apk`` to hand to a phone.

        Order matters: the data directory first (it is where this program
        already writes), then the folder the program lives in -- the green
        package unpacks to one directory, so "drop the apk next to run.sh" is
        the instruction that is easiest to follow -- then the working
        directory, which is what a source checkout looks like.
        """
        data_dir = str(getattr(getattr(self.engine, "config", None), "data_dir", "") or "")
        # .../app/eversend/web/server.py -> .../  (the unpacked package root)
        package_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        return tuple(dict.fromkeys(d for d in (data_dir, package_root, os.getcwd()) if d))

    # -- connected browsers ------------------------------------------------

    def touch_client(self, address: str, agent: str = "", device_id: str = "") -> None:
        """Remember that a client just did something.

        One physical device == one record, and the record is keyed by a *stable
        identity*, never by where the request came from:

        * a client that sends ``X-EverSend-Device`` (the app on every request,
          the page on every ``fetch``) is keyed ``android:<id>`` / ``web:<id>``;
          the address is just a field on that record, updated in place, so
          roaming between Wi-Fi and a hotspot does not mint a second device;
        * a request *without* that header -- a browser cannot add custom headers
          to ``GET /`` or ``<script src>`` -- is folded into the device record
          at the same address with the same User-Agent when one exists
          (:meth:`_touch_device_at_address`), which is what used to produce two
          rows called 「Android 上的 Chrome」 for one handset;
        * only a client that really has no stable identity (a computer's browser
          that never sends the header, a script) still falls back to the legacy
          ``address|UA-digest`` key.

        Keyed by address **and** User-Agent.  Address alone is not enough: a
        phone reaching us through a port forward (``adb reverse``, a reverse
        proxy, an SSH tunnel) arrives from the same 127.0.0.1 as a local health
        check, and the two would collapse into one entry -- which is exactly
        how the Android CI check first "saw" a client that was really its own
        probe.  One phone in a normal LAN still gets exactly one entry.
        """
        if not address and not device_id:
            return
        now = time.time()
        if device_id:
            # 原生 App 用 android:<id>（/api/hello 登记过），网页版用 web:<id>：
            # 两者都是"设备号"，跟地址无关 —— 换 Wi-Fi、换 IP 都还是同一台。
            if self._touch_known_device(device_id, address, agent, now):
                return
            if self._touch_web_device(device_id, address, agent, now):
                return
        elif self._touch_device_at_address(address, agent, now):
            # 没有设备号，但这个地址上已经有一台**带设备号**的设备，UA 也对得上：
            # 是它自己。 浏览器不会给地址栏导航（``GET /``）或 ``<script src>``
            # 加自定义头，所以"打开页面"这一下永远没有 ``X-EverSend-Device``；
            # 按地址再建一条，"一台手机"就变成了用户看到的两行
            # 「Android 上的 Chrome」（一行是设备号记录，一行是地址记录）。
            return
        if is_app_agent(agent) and self._touch_known_app(address, agent, now):
            return
        key = self.client_key(address, agent)
        remember = False
        label = describe_agent(agent)
        meaningful = label not in ("命令行/脚本", "未知客户端")
        with self._state_lock:
            entry = self._known.get(key)
            if entry is None and meaningful:
                self._known[key] = {
                    "key": key,
                    "address": address,
                    "agent": agent[:200],
                    "label": describe_agent(agent),
                    "firstSeen": now,
                    "lastSeen": now,
                }
                remember = True
            elif entry is not None:
                entry["lastSeen"] = now
                # It made a real HTTP request: it is not a mere broadcast any more.
                if entry.pop("provisional", None) is not None:
                    entry["talked"] = True
                    remember = True
                if label != entry.get("label"):
                    # describe_agent() learns new clients over time (the app
                    # used to be filed as "浏览器"), and a remembered device
                    # keeps its label forever otherwise -- which is how the
                    # user keeps seeing last week's wording.
                    entry["label"] = label
                    remember = True
            known = self._clients.get(key)
            if known is None:
                self._clients[key] = {
                    "address": address,
                    "agent": agent[:200],
                    "label": describe_agent(agent),
                    "since": now,
                    "lastSeen": now,
                }
            else:
                known["lastSeen"] = now
                known["label"] = describe_agent(agent)
        if remember:
            self._save_known()

    def _touch_known_device(self, device_id: str, address: str, agent: str, now: float) -> bool:
        """Keep an app-registered device alive, whichever address it came from."""
        key = f"android:{bare_device_id(device_id)}"
        with self._state_lock:
            client = self._known.get(key)
            if client is None:
                return False
            self._touch_record(key, address, agent, now)
            if agent and not client.get("agent"):
                client["agent"] = agent[:200]
        return True

    def _touch_web_device(self, device_id: str, address: str, agent: str, now: float) -> bool:
        """A browser that identifies itself with a stable id.

        The page generates one UUID once (localStorage) and sends it on every
        request, so a phone that roams between networks stays *one* device here
        -- and therefore keeps *one* conversation -- instead of turning into a
        new client every time its address changes.
        """
        device_id = bare_device_id(device_id)
        key = f"web:{device_id}"
        remember = False
        with self._state_lock:
            client = self._known.get(key)
            if client is None:
                client = {
                    "key": key,
                    "address": address,
                    "agent": agent[:200],
                    "label": describe_agent(agent),
                    "firstSeen": now,
                    "deviceId": device_id,
                    "kind": "browser",
                }
                self._known[key] = client
                # 新配对的手机必须马上落盘：以前这条只在别的动作顺手保存时
                # 才写文件，进程重启一次它就"从没出现过"。
                remember = True
            elif address and str(client.get("address") or "") != address:
                remember = True
            self._touch_record(key, address, agent, now)

            # 历史遗留：网页版以前的身份是"地址 + User-Agent"，浏览器升级一次
            # （或者换内核）就多出一条 —— 用户看到"同一个浏览器两台设备"。
            # 现在它有了稳定设备号，同一个地址上那些旧记录就是它自己：
            #   * 已经不在说话的，直接收掉；
            #   * 还在说话的，只要 UA 摘要和这次请求一模一样，也是它自己
            #     ——「打开页面」（无自定义头）刚建的那条正是这样，不收掉就
            #     正好是用户报的两行「Android 上的 Chrome」。
            if address:
                for other in [
                    legacy
                    for legacy, entry in self._known.items()
                    if legacy != key
                    and self._is_legacy_key(legacy)
                    and entry.get("address") == address
                    and (
                        legacy not in self._clients
                        or (
                            bool(agent)
                            and agent_digest(str(entry.get("agent") or "")) == agent_digest(agent)
                        )
                    )
                ]:
                    self._merge_record(key, other)
                    remember = True
        if remember:
            self._save_known()
        return True

    def _touch_device_at_address(self, address: str, agent: str, now: float) -> bool:
        """碰一下"这个地址上那台带设备号的设备"，如果有的话。

        浏览器**不可能**给地址栏导航（``GET /``）或者 ``<script src>`` 加自定义
        头，所以打开一台已经配对过的手机上的页面时，总会有一次没有
        ``X-EverSend-Device`` 的请求先到。按"地址 + UA"给它建一条记录，就多出
        用户看到的那一行「Android 上的 Chrome」，而且两行都算在线。

        判定条件（三条都要满足，缺一不并）：
          1. 那条记录有稳定设备号（``web:`` / ``android:``），不是地址键记录；
          2. 记录的 ``address`` 就是这个地址；
          3. UA 摘要一模一样。

        确实没有设备号的普通浏览器（电脑上直接打开页面、老缓存的旧页面）在这个
        地址上没有"带设备号的自己"，所以照旧按老办法建档，不会被藏掉。
        同一地址 + 同 UA + 两台设备号不同（NAT 后面两台同款手机）时只会挑最近
        说话的那条碰一下 lastSeen，**不会**把两条设备记录并成一条：下一次带设备
        号的请求仍然各回各家。
        """
        if not address or not agent:
            return False
        digest = agent_digest(agent)
        with self._state_lock:
            best = ""
            for key, client in self._known.items():
                if self._is_legacy_key(key) or not str(client.get("deviceId") or ""):
                    continue
                if str(client.get("address") or "") != address:
                    continue
                known_agent = str(client.get("agent") or "")
                if not known_agent or agent_digest(known_agent) != digest:
                    continue
                if not best or float(client.get("lastSeen") or 0) > float(self._known[best].get("lastSeen") or 0):
                    best = key
            if not best:
                return False
            self._touch_record(best, address, agent, now)
        return True

    def _touch_record(self, key: str, address: str, agent: str, now: float) -> None:
        """把一条已存在的记录碰活，并保证它在"正在说话"的表里。"""
        client = self._known.get(key)
        if client is None:
            return
        client["lastSeen"] = now
        client.pop("provisional", None)
        client["talked"] = True
        if address:
            client["address"] = address
        live = self._clients.get(key)
        if live is None:
            self._clients[key] = {
                "address": address or client.get("address", ""),
                "agent": agent[:200] or str(client.get("agent") or ""),
                "label": client.get("label") or describe_agent(agent),
                "since": client.get("firstSeen", now),
                "lastSeen": now,
            }
        else:
            live["lastSeen"] = now
            if address:
                live["address"] = address

    @staticmethod
    def _is_legacy_key(key: str) -> bool:
        """老格式的键：``地址|UA摘要``，只有"网络位置"，没有稳定身份。"""
        return "|" in str(key)

    def _merge_record(self, target: str, source: str) -> bool:
        """把 ``source`` 折进 ``target``：同一台设备的第二条记录。

        保留规则：``lastSeen`` 取最新、``firstSeen`` 取最早、地址/UA 取"更近的
        那条"的（换过网络就用新地址，用户看到的就是它现在在哪）。``source``
        若正在说话，这份"在线"要转给 ``target`` —— 合并绝不能把"刚刚还连着"
        变成"离线"，也不能让一条已经配对的手机凭空掉出列表。

        被并掉那条的键会记进 ``target["aliases"]``：会话里存的成员 id 是**当时**
        那个写法（``web:地址|UA摘要``），记录没了不等于那段聊天里的名字也该没
        —— 289 条消息的老会话会从「安卓 App」掉成「未知设备」。
        """
        dst = self._known.get(target)
        src = self._known.pop(source, None)
        if dst is None or src is None:
            return False
        src_live = self._clients.pop(source, None)
        self._carry_aliases(dst, src)
        src_last = _as_float(src.get("lastSeen"))
        dst_last = _as_float(dst.get("lastSeen"))
        if src_last > dst_last:
            if src.get("address"):
                dst["address"] = src["address"]
            if src.get("agent"):
                dst["agent"] = src["agent"]
        dst["lastSeen"] = max(dst_last, src_last)
        firsts = [_as_float(c.get("firstSeen")) for c in (dst, src) if c.get("firstSeen")]
        if firsts:
            dst["firstSeen"] = min(firsts)
        if not dst.get("label") and src.get("label"):
            dst["label"] = src["label"]
        if src_live is not None and target not in self._clients:
            self._clients[target] = {
                "address": str(dst.get("address") or ""),
                "agent": str(dst.get("agent") or ""),
                "label": str(dst.get("label") or ""),
                "since": dst.get("firstSeen", src_live.get("lastSeen")),
                "lastSeen": max(_as_float(src_live.get("lastSeen")), dst["lastSeen"]),
            }
        return True

    @staticmethod
    def _record_aliases(client: dict[str, Any]) -> list[str]:
        """一条记录身上攒下来的旧身份（并进来的那些键）。

        只在 ``aliases`` 真的是个列表时认它：注册表从磁盘读回来，字段被手工
        改坏（比如写成一个字符串）时按"没有别名"处理，而不是逐字符迭代。
        """
        bucket = client.get("aliases")
        if not isinstance(bucket, list):
            return []
        out: list[str] = []
        for alias in bucket:
            text = str(alias or "").strip()
            if text and text not in out:
                out.append(text)
        return out

    def _add_alias(self, client: dict[str, Any], alias: str, source: dict[str, Any] | None = None) -> None:
        """把 ``alias`` 记到 ``client`` 上，供显示层把老会话的成员 id 认回来。

        ``aliases`` 是给**调用方**读的：``core.chat.client_aliases()`` 会把它一起
        展开成"这台设备可能出现的几种 id"，三个客户端算会话名字时都认。至于旧
        写法**当时**叫什么名字，记在 ``aliasLabels`` 里，由本文件的
        :meth:`_Handler._member_label_map` 取用（它会盖过"现在的名字"）。

        ``source`` 是被并掉的那条记录：连同它**当时显示的名字**一起记下来
        （``aliasLabels``）。会话标题用的是旧名字，归并后换成新记录的名字会
        让那段 289 条消息的老会话"改名"；记着旧名字，标题就一字不变。
        """
        text = str(alias or "").strip()
        if not text or text == str(client.get("key") or ""):
            return
        bucket = client.get("aliases")
        if not isinstance(bucket, list):
            bucket = []
            client["aliases"] = bucket
        if text not in bucket:
            bucket.append(text)
            del bucket[: -self.ALIAS_LIMIT]      # 只留最近几条，记录不该无限长
        if source is None:
            return
        remembered = client.get("aliasLabels")
        if not isinstance(remembered, dict):
            remembered = {}
            client["aliasLabels"] = remembered
        remembered.setdefault(
            text,
            {
                "label": str(source.get("label") or ""),
                "kind": str(source.get("kind") or ""),
                "version": str(source.get("version") or ""),
                "address": str(source.get("address") or ""),
            },
        )
        while len(remembered) > self.ALIAS_LIMIT:
            remembered.pop(next(iter(remembered)))

    def _carry_aliases(self, dst: dict[str, Any], src: dict[str, Any]) -> None:
        """把 ``src`` 攒下的旧身份（连同它们当时显示的名字）搬到 ``dst`` 上。"""
        self._add_alias(dst, str(src.get("key") or ""), src)
        for alias in self._record_aliases(src):
            self._add_alias(dst, alias)
        remembered = src.get("aliasLabels")
        if not isinstance(remembered, dict):
            return
        mine = dst.get("aliasLabels")
        if not isinstance(mine, dict):
            mine = {}
            dst["aliasLabels"] = mine
        for alias, info in remembered.items():
            if isinstance(info, dict):
                mine.setdefault(str(alias), info)
        while len(mine) > self.ALIAS_LIMIT:
            mine.pop(next(iter(mine)))

    def _alias_label(self, client: dict[str, Any], alias: str) -> str:
        """旧身份 ``alias`` 当时显示的名字（没有记录就退回这条记录的名字）。"""
        remembered = client.get("aliasLabels")
        info = remembered.get(alias) if isinstance(remembered, dict) else None
        if isinstance(info, dict):
            return client_label(info)
        return client_label(client)

    #: 一条记录最多带几个旧身份（老会话认得回来就行）。
    ALIAS_LIMIT = 8

    def _heal_duplicate_devices(self) -> bool:
        """把已经落盘的重复记录并掉（老数据也要治）。

        只并"无设备号的地址键记录 + 有设备号的记录，且 UA 摘要一致"这一对：

        * 地址也一样时无条件并 —— 这正是"页面首次加载建了一条、页面里的
          fetch 又建了一条"留下的痕迹，两条都还算在线；
        * 地址已经不一样时，只并那条**不在说话**的老记录。一个还在轮询的
          浏览器不会被藏掉，而"上周换过 IP 留下的幽灵行"会被收掉。
        """
        with self._state_lock:
            merged = False
            for legacy in [k for k in self._known if self._is_legacy_key(k)]:
                entry = self._known.get(legacy)
                if entry is None or not entry.get("agent"):
                    continue
                digest = agent_digest(str(entry.get("agent") or ""))
                target = ""
                for key, client in self._known.items():
                    if self._is_legacy_key(key) or not str(client.get("deviceId") or ""):
                        continue
                    known_agent = str(client.get("agent") or "")
                    if not known_agent or agent_digest(known_agent) != digest:
                        continue
                    same_address = bool(entry.get("address")) and str(client.get("address") or "") == str(
                        entry.get("address") or ""
                    )
                    if not same_address and legacy in self._clients:
                        continue
                    if not target or float(client.get("lastSeen") or 0) > float(
                        self._known[target].get("lastSeen") or 0
                    ):
                        target = key
                if target and self._merge_record(target, legacy):
                    merged = True
            return merged

    def _touch_known_app(self, address: str, agent: str, now: float) -> bool:
        """Keep an app that already introduced itself on its own entry.

        The app is remembered by device id (``android:<id>``), but it also polls
        every few seconds, and every poll used to *re-create* the browser entry
        (address + User-Agent) that its ``/api/hello`` had just removed.  The
        result was exactly the duplicate the user complained about: one phone,
        two rows, one of them called "浏览器".  Returns whether a known app
        matched -- if not (an older build that never says hello), the normal
        browser path still applies, so nothing becomes invisible.
        """
        with self._state_lock:
            for key, client in self._known.items():
                if not key.startswith("android:") or client.get("address") != address:
                    continue
                client["lastSeen"] = now
                live = self._clients.get(key)
                if live is None:
                    self._clients[key] = {
                        "address": address,
                        "agent": agent[:200],
                        "label": client.get("label") or describe_agent(agent),
                        "since": client.get("firstSeen", now),
                        "lastSeen": now,
                    }
                else:
                    live["lastSeen"] = now
                return True
        return False

    def share_files(self, paths: Iterable[str], only_key: str = "") -> list[dict[str, Any]]:
        """Publish local files for the connected browser to download.

        Called by the desktop window, in this process: the paths never travel
        over the network, so `/api/share/<id>` cannot be turned into a file
        read primitive.  The phone gets an opaque id.

        ``only_key`` addresses the hand-off to one paired client (its registry
        key).  Without it every connected phone sees the file -- right for the
        desktop's 「发送给手机」, wrong for "this upload was meant for that one".
        """
        added: list[dict[str, Any]] = []
        for raw in paths:
            path = os.path.abspath(str(raw))
            if not os.path.isfile(path):
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            share_id = secrets.token_urlsafe(9)
            entry = {
                "id": share_id,
                "name": os.path.basename(path),
                "size": size,
                "path": path,
                "added": time.time(),
                "downloaded": False,
                #: 手机已经取走多少字节。会话里那条记录的进度条就靠它 ——
                #: 手机是通过 HTTP 来取的，电脑这一侧是服务端，所以"传到哪了"
                #: 只有这里知道（发送方没有可推的连接，也就没有 active_transfers）。
                "sentBytes": 0,
            }
            if only_key:
                entry["forKey"] = only_key
            with self._state_lock:
                self._shares[share_id] = entry
                # A long-running session shares file after file; keep the
                # registry bounded (and drop entries whose file is gone, so a
                # stale row never sits on the phone's page forever).
                if len(self._shares) > MAX_SHARES:
                    for key in sorted(self._shares, key=lambda k: self._shares[k]["added"])[
                        : len(self._shares) - MAX_SHARES
                    ]:
                        del self._shares[key]
            added.append(entry)
            self.engine.events.emit(
                "share_added",
                share_id=share_id,
                name=entry["name"],
                size=size,
                total=len(added),
            )
        if added:
            self._save_shares()
        return added

    def shares(self, for_key: str = "") -> list[dict[str, Any]]:
        """Shares as the *phone* may see them: no filesystem paths.

        ``for_key`` filters to what was addressed to that client (plus anything
        addressed to nobody, i.e. the desktop's broadcast 「发送给手机」).
        """
        with self._state_lock:
            found = [
                {
                    "id": s["id"],
                    "name": s["name"],
                    "size": s["size"],
                    "added": s["added"],
                    "downloaded": bool(s.get("downloaded")),
                    "sentBytes": int(s.get("sentBytes") or 0),
                }
                for s in self._shares.values()
                if not for_key or not s.get("forKey") or s.get("forKey") == for_key
            ]
        found.sort(key=lambda s: s["added"], reverse=True)
        return found

    def share_path(self, share_id: str) -> str | None:
        """The local path behind a share id, or ``None`` if it is unknown."""
        with self._state_lock:
            entry = self._shares.get(str(share_id))
            if entry is None:
                return None
            path = str(entry["path"])
        return path if os.path.isfile(path) else None

    def share_lookup(self, share_id: str) -> tuple[str, str, str]:
        """``(state, path, name)`` —— 让下载接口说得出**为什么**给不了。

        ``state`` 三种，对应三句不同的中文（用户看的不是状态码）：

        * ``ok``      —— 有这条分享，文件也在；
        * ``missing`` —— 分享列表里根本没有它：条目过期、被顶掉、或者链接抄错了；
        * ``gone``    —— 条目还在，但它指向的文件被删掉/移走了。

        以前这两种都是同一个 ``None``，手机只会看到一句笼统的"不在分享列表里"，
        明明是文件被删了却让人去翻分享列表。
        """
        with self._state_lock:
            entry = self._shares.get(str(share_id))
            if entry is None:
                return "missing", "", ""
            name = str(entry.get("name") or "")
            path = str(entry.get("path") or "")
        if not path or not os.path.isfile(path):
            return "gone", path, name
        return "ok", path, name

    def find_share_for_message(self, message: dict[str, Any]) -> str:
        """这条聊天消息对应哪一条分享（找不到就空串）。

        桌面发布文件与写聊天记录用的是**同一个绝对路径**（分享条目的 ``path``
        ↔ 消息的 ``mediaSource``），所以先按路径比；路径丢了（老记录、文件挪过
        位置）再按 名字 + 大小 比。不需要改数据库结构，也不需要桌面端先加字段。

        桌面端新版本会把分享 id **直接写进消息记录**（``shareId`` 字段，见
        ``core/chat.py`` 的 share_id 列）：这时只认它 —— 记录里写明了就是精确
        关联；如果它已经过期（分享表被清空/条目被顶掉），返回空而不是拿同一个
        文件的**新**分享去顶替，否则老气泡会跟着新下载显示进度。
        """
        direct = str(message.get("shareId") or message.get("share_id") or "")
        if direct:
            with self._state_lock:
                if direct in self._shares:
                    return direct
            return ""
        source = str(message.get("mediaSource") or "")
        wanted_path = os.path.realpath(source) if source else ""
        name = str(message.get("mediaName") or "")
        try:
            size = int(message.get("mediaSize") or 0)
        except (TypeError, ValueError):
            size = 0
        with self._state_lock:
            entries = [(key, dict(value)) for key, value in self._shares.items()]
        if wanted_path:
            for share_id, entry in entries:
                candidate = str(entry.get("path") or "")
                if candidate and os.path.realpath(candidate) == wanted_path:
                    return share_id
        if name:
            for share_id, entry in entries:
                if str(entry.get("name") or "") != name:
                    continue
                if not size or int(entry.get("size") or 0) == size:
                    return share_id
        return ""

    def mark_share_downloaded(self, share_id: str) -> None:
        """手机把整个文件取走了 —— 这是"回执"，电脑端据此才敢写「已取走」。

        只在**第一次**转成已下载时发事件：同一条分享可能被重复取（用户又点了一次
        下载），事件发一次就够，事件流上不必跟着重复。
        """
        became = False
        name = ""
        size = 0
        with self._state_lock:
            entry = self._shares.get(str(share_id))
            if entry is not None:
                became = not bool(entry.get("downloaded"))
                entry["downloaded"] = True
                entry["sentBytes"] = int(entry.get("size") or entry.get("sentBytes") or 0)
                name = str(entry.get("name") or "")
                size = int(entry.get("size") or 0)
        if became:
            self.engine.events.emit(
                "share_taken", share_id=str(share_id), name=name, size=size
            )
            self._save_shares()

    def note_share_progress(self, share_id: str, sent_bytes: int) -> None:
        """手机正在取这个文件，已经发出去多少字节。

        只增不减：Range 续传会分成几次请求，界面要看到的是"总体取到哪了"。
        """
        with self._state_lock:
            entry = self._shares.get(str(share_id))
            if entry is not None:
                entry["sentBytes"] = max(int(entry.get("sentBytes") or 0), int(sent_bytes))

    def clear_shares(self) -> None:
        with self._state_lock:
            self._shares.clear()
        self._save_shares()

    # -- 分享列表持久化（opt-1017） ----------------------------------------
    #
    # 为什么必须落盘：手机下载大文件走 HTTP Range 续传，电脑端进程重启后
    # 分享表原来在内存里 → 手机带着 Range 回来续传，服务端回 404「这个文件已经
    # 不在分享列表里了」，用户看到的是"传到一半白传"。落盘后重启/崩溃都能接着取。
    #
    # 安全边界：文件路径只存在本机 data 目录（与 web_clients.json 同级），网络
    # 侧永远只见不透明 id；加载时再校验一次"文件仍然存在"，不存在的条目直接丢弃。

    def _load_shares(self) -> None:
        try:
            with open(self._shares_path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        loaded: dict[str, dict[str, Any]] = {}
        entries = data.get("shares") if isinstance(data, dict) else None
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            share_id = str(entry.get("id") or "")
            path = str(entry.get("path") or "")
            if not share_id or not path or not os.path.isfile(path):
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            loaded[share_id] = {
                "id": share_id,
                "name": str(entry.get("name") or os.path.basename(path)),
                "size": size,
                "path": path,
                "added": float(entry.get("added") or 0.0),
                "downloaded": bool(entry.get("downloaded")),
                # 续传进度由下一次 Range 请求重新上报；这里清零，免得界面上
                # 显示一个"已经下载了 80%"却还要重新取的历史数字。
                "sentBytes": 0,
            }
            if entry.get("forKey"):
                loaded[share_id]["forKey"] = str(entry["forKey"])
        if not loaded:
            return
        with self._state_lock:
            # 内存里可能已经有本次进程新发的（一般不会，构造期还没开始服务）。
            for share_id, entry in loaded.items():
                self._shares.setdefault(share_id, entry)
        log.info("从 %s 恢复了 %d 条分享（电脑端重启后仍可续传）", self._shares_path, len(loaded))

    def _save_shares(self) -> None:
        """原子落盘（tmp+rename）；只读介质/磁盘错误不能影响服务。"""
        try:
            with self._state_lock:
                entries = [dict(entry) for entry in self._shares.values()]
            payload = {"shares": entries}
            tmp = self._shares_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self._shares_path)
        except (OSError, ValueError, TypeError):
            log.debug("share list could not be persisted", exc_info=True)

    @staticmethod
    def client_key(address: str, agent: str = "") -> str:
        return f"{address}|{agent_digest(agent)}"

    def identity_key(self, client: dict[str, Any]) -> str:
        """这台设备的稳定身份 —— 三个客户端拿它做防御性去重。

        有设备号就是 ``web:<deviceId>`` / ``android:<deviceId>``（跟地址无关，
        换网络、换 IP 都不变）；没有设备号的老记录只能退回它自己的键
        （``地址|UA摘要``），那种记录本来就没有稳定身份可言。
        """
        key = str(client.get("key") or "")
        device_id = str(client.get("deviceId") or "")
        if not device_id:
            return key
        return key if not self._is_legacy_key(key) else f"web:{device_id}"

    def known_clients(self) -> list[dict[str, Any]]:
        """Every phone this desktop has paired with, newest activity first.

        Unlike :meth:`clients` this does not expire: the phone keeps its place
        in the device list while its screen is off, while it is in another app,
        or while its page is closed.  ``online`` says whether it is talking to
        us *right now* (``key in self._clients``).

        一台物理设备只有一条记录：记录以稳定设备号（``web:<id>`` /
        ``android:<id>``）为键，地址只是它身上会变的一个字段。老数据里同一台
        设备的第二条（``地址|UA摘要``）在这里被并掉，所以升级后名单也不会
        继续显示两行。

        注册表是直接从磁盘读回来的，字段可能残缺（手工改过、旧版本写的）：
        这里全部按 ``.get`` + 默认值读，一行坏数据不能让 ``/api/state`` 整页
        500 —— 那样连"哪台设备坏了"都看不见。
        """
        now = time.time()
        dropped = False
        with self._state_lock:
            # 活跃记录按 TTL 过期：它们是"正在说话"的凭证，不是历史。
            # 以前这里只读不清理，于是换过网络的旧记录会永远显示"在线"。
            for key in [
                key
                for key, client in self._clients.items()
                if now - _as_float(client.get("lastSeen")) > CLIENT_TTL
            ]:
                self._clients.pop(key, None)
            for key in [
                key
                for key, client in self._known.items()
                if client.get("provisional")
                and now - _as_float(client.get("lastSeen")) > self.PROVISIONAL_TTL
            ]:
                self._known.pop(key, None)
                self._clients.pop(key, None)
                dropped = True
            if self._heal_duplicate_devices():
                dropped = True
            # 连着注册表的键一起取出来：记录里万一没有 key 字段（手工改坏的
            # 文件），也得有一条能用的键，否则界面上那一行点了没反应。
            found = [(key, dict(c)) for key, c in self._known.items()]
            live = {k: _as_float(c.get("lastSeen")) for k, c in self._clients.items()}
        if dropped:
            self._save_known()
        for record_key, client in found:
            key = str(client.get("key") or record_key or "")
            address = str(client.get("address") or "")
            last_seen = _as_float(client.get("lastSeen"))
            client["key"] = key
            client["address"] = address
            client["lastSeen"] = last_seen
            client["firstSeen"] = _as_float(client.get("firstSeen")) or last_seen
            client["isLocal"] = address in ("127.0.0.1", "::1")
            client["secondsAgo"] = round(max(0.0, now - last_seen), 1)
            client["online"] = key in live
            # One word for "what is this": the phone lists its peers with a
            # status chip, and it must not have to guess from the User-Agent.
            client["clientKind"] = "app" if str(client.get("kind") or "") == "app" else "browser"
            client["platform"] = "android" if client["clientKind"] == "app" else "browser"
            # 稳定身份：同一台设备的两条记录（见 _merge_record）靠它对上号。
            # 桌面端 / 网页版 / 安卓都可以拿它再兜一层去重，别只信行数。
            client["identityKey"] = self.identity_key(client)
        found.sort(key=lambda item: _as_float(item[1].get("lastSeen")), reverse=True)
        return [client for _record_key, client in found]

    def register_app(
        self,
        device_id: str,
        name: str = "",
        version: str = "",
        address: str = "",
        agent: str = "",
    ) -> dict[str, Any]:
        """A native app introduces itself with a stable id.

        The browser registry keys a phone by *address + User-Agent*, which is
        fine for a page but wrong for an app: the address changes when the phone
        moves between networks, and then the desktop would show a second device
        and every conversation would point at the old one.  An app that sends
        its own device id gets one stable entry instead.
        """
        device_id = str(device_id or "").strip()
        if not device_id:
            raise ValueError("缺少 deviceId")
        key = f"android:{device_id}"
        now = time.time()
        with self._state_lock:
            entry = self._known.get(key) or {
                "key": key,
                "firstSeen": now,
            }
            entry.update(
                {
                    "address": address or entry.get("address", ""),
                    "agent": agent[:200] if agent else entry.get("agent", ""),
                    "label": name or entry.get("label") or "安卓 App",
                    "name": name or entry.get("name", ""),
                    "deviceId": device_id,
                    "version": version or entry.get("version", ""),
                    "kind": "app",
                    "lastSeen": now,
                }
            )
            entry.pop("provisional", None)
            entry["talked"] = True
            self._known[key] = entry
            # 同一台手机重装 App 会换设备号（新安装 = 新的 UUID），于是列表里
            # 出现两条同名记录。同名、且旧的那条**不在线**时，就认定是它自己
            # 重装过 —— 收掉旧的。两台真机同名又都在线的情况不会被误合并。
            for stale in [
                other
                for other, client in self._known.items()
                if other.startswith("android:")
                and other != key
                and client.get("label") == entry.get("label")
                and str(client.get("name") or "") == str(entry.get("name") or "")
                and other not in self._clients
            ]:
                # 收掉旧记录，但它的键（和它当时显示的名字）要留在新记录上：
                # 老会话的成员 id 用的是当时那个写法
                # （``web:地址|摘要`` / ``android:<旧uuid>``）。
                self._carry_aliases(entry, self._known.get(stale) or {})
                self._known.pop(stale, None)
                self._clients.pop(stale, None)
            # An earlier build of the app did not introduce itself at all, so it
            # entered the registry under the browser key (address + User-Agent)
            # and now shows up as a second, duplicate device next to the real
            # one.  Same address and clearly the same app: drop the old entry.
            if address:
                for stale in [
                    other
                    for other, client in self._known.items()
                    if other != key
                    and client.get("address") == address
                    and is_app_agent(str(client.get("agent", "")))
                ]:
                    self._carry_aliases(entry, self._known.get(stale) or {})
                    self._known.pop(stale, None)
                    self._clients.pop(stale, None)
            # Serve it as a live client too, so everything that reads
            # ``clients()`` (the device list, the QR dialog) sees it right away.
            self._clients[key] = {
                "address": entry["address"],
                "agent": entry["agent"],
                "label": entry["label"],
                "since": entry.get("firstSeen", now),
                "lastSeen": now,
            }
        self._save_known()
        return dict(entry)

    def register_from_discovery(self, event: dict[str, Any]) -> None:
        """A device announced itself over UDP (before opening any page).

        Marked provisional: anything that broadcasts on our discovery port gets
        remembered, including one-off probes (the self-test sends them, and so
        does every phone that merely looks for the computer once).  A real app
        keeps talking -- its ``/api/hello`` and its polls clear the flag -- so a
        device that only ever broadcast disappears again by itself instead of
        sitting in the user's paired list forever.
        """
        device_id = str(event.get("device_id") or "")
        if not device_id:
            return
        try:
            entry = self.register_app(
                device_id,
                name=str(event.get("name") or "") or "手机",
                version=str(event.get("version") or ""),
                address=str(event.get("address") or ""),
            )
        except ValueError:
            return
        with self._state_lock:
            stored = self._known.get(str(entry.get("key", "")))
            if stored is not None and not stored.get("talked"):
                stored["provisional"] = True

    def forget_client(self, address: str, agent: str = "", device_id: str = "") -> bool:
        """Forget one browser entirely —— the phone's 「断开连接」 button.

        Both the live list and the remembered one: the user asked to be
        disconnected, so the desktop must stop listing it as its phone.

        Since a paired browser is remembered under its device id (``web:<id>``)
        and no longer under ``地址|UA摘要``, saying goodbye has to remove the
        device record too -- otherwise 「断开连接」 looks like it did nothing and
        the phone is back in the list on the next poll.  When the caller has no
        device id (an old cached page), fall back to "the records at this
        address whose User-Agent is this one".
        """
        device_id = bare_device_id(device_id)
        keys = [self.client_key(address, agent)]
        if device_id:
            keys += [f"web:{device_id}", f"android:{device_id}"]
        else:
            digest = agent_digest(agent)
            with self._state_lock:
                keys += [
                    key
                    for key, client in self._known.items()
                    if not self._is_legacy_key(key)
                    and bool(agent)
                    and str(client.get("address") or "") == address
                    and agent_digest(str(client.get("agent") or "")) == digest
                ]
        with self._state_lock:
            gone = False
            for key in dict.fromkeys(keys):
                gone = self._clients.pop(key, None) is not None or gone
                gone = self._known.pop(key, None) is not None or gone
        if gone:
            self._save_known()
        return gone

    def forget_offline_devices(self) -> int:
        """清空"以前见过、现在不在线"的设备名单（桌面端的「清空离线设备」）。

        只清这一份记忆，不动聊天记录、不动配对过的手机 —— 用户要的是"列表干净"，
        不是"把历史删掉"。在线设备不受影响（它们下一次被发现又会回来）。
        """
        live = {peer.info.device_id for peer in self.engine.devices()}
        with self._state_lock:
            gone = [key for key in self._remembered if key not in live]
            for key in gone:
                self._remembered.pop(key, None)
        return len(gone)

    def remove_client(self, key: str) -> bool:
        """Forget a remembered browser by key (the desktop's 移除 action)."""
        with self._state_lock:
            self._clients.pop(key, None)
            gone = self._known.pop(key, None) is not None
        if gone:
            self._save_known()
        return gone

    def _load_known(self) -> None:
        try:
            with open(self._known_path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        for entry in data.get("clients", []):
            if not isinstance(entry, dict) or not entry.get("key"):
                continue
            # Health checks and scripts are not devices; a file written by an
            # older build may still contain them.
            if str(entry.get("label")) in ("命令行/脚本", "未知客户端"):
                continue
            self._known[str(entry["key"])] = entry

    def _save_known(self) -> None:
        import json as _json

        try:
            payload = {"clients": list(self._known.values())}
            tmp = self._known_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                _json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self._known_path)
        except OSError:
            pass  # a read-only medium must not break the UI

    def clients(self, ttl: float = CLIENT_TTL) -> list[dict[str, Any]]:
        """Browsers with the page open right now, newest activity first."""
        cutoff = time.time() - ttl
        with self._state_lock:
            for key in [k for k, c in self._clients.items() if _as_float(c.get("lastSeen")) < cutoff]:
                del self._clients[key]
            # 「正在说话」的表同样只能有一条：老数据里同一台设备的第二条记录
            # 在这里被并掉，否则 /api/state 的第一份 webClients 会先于
            # knownClients 把它露出去（state() 就是先取 clients() 的）。
            healed = self._heal_duplicate_devices()
            # ``_clients`` 的行本来没有 key 字段，算身份时按注册表的键补上。
            found = [
                dict(c, identityKey=self.identity_key(self._known.get(key) or dict(c, key=key)))
                for key, c in self._clients.items()
            ]
        if healed:
            self._save_known()
        now = time.time()
        for client in found:
            address = str(client.get("address") or "")
            client["address"] = address
            client["isLocal"] = address in ("127.0.0.1", "::1")
            client["secondsAgo"] = round(max(0.0, now - _as_float(client.get("lastSeen"))), 1)
        found.sort(key=lambda c: _as_float(c.get("lastSeen")), reverse=True)
        return found

    @property
    def port_bound(self) -> int:
        """The port actually listening (differs from ``port`` when it was 0)."""
        return self._bound_port

    def start(self, port: int | None = None) -> int:
        """Bind and serve.  Returns the port that was actually bound."""
        if self._httpd is not None:
            return self._bound_port
        wanted = self.port if port is None else int(port)
        self._stop.clear()

        ui = self

        class Handler(_Handler):
            # A per-instance subclass keeps two WebUIs in one process (the
            # self-test does exactly that) from sharing a token.
            server_ui: ClassVar[WebUI] = ui

        httpd = _ThreadingServer((self.host, wanted), Handler)
        httpd.daemon_threads = True
        httpd.request_queue_size = 128
        if self.ssl_context is not None:
            # Wrapping the *listening* socket is the documented way to put
            # http.server behind TLS: every accepted connection is then already
            # an SSLSocket, and the handler code stays exactly the same.
            httpd.socket = self.ssl_context.wrap_socket(httpd.socket, server_side=True)
        self._httpd = httpd
        self._bound_port = int(httpd.server_address[1])
        self.port = self._bound_port

        # Advertise the real port: the engine's configured web_port is only a
        # wish until the socket is bound, and a peer that reads the wrong port
        # from discovery shows a dead link.
        #
        # Only the plain-HTTP copy of the page owns this field.  The HTTPS copy
        # is started right after it (the microphone needs a secure context) and
        # used to overwrite it, which sent every discovery announcement out with
        # ``"web": 52120`` -- a TLS port that anyone building "http://<host>:<web>/"
        # from it, the Android app included, cannot open.
        if self.ssl_context is None:
            try:
                self.engine.info.web_port = self._bound_port
                self.engine.info.capabilities["web"] = True
            except Exception:  # pragma: no cover - DeviceInfo is a plain dataclass
                pass

        self._thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, name="eversend-web", daemon=True
        )
        self._thread.start()
        log.info("web UI listening on http://%s:%d/", self.host, self._bound_port)
        # 启动时清一次过期续传 spool（opt-1017）。失败不影响服务。
        try:
            self.sweep_resume_spools()
        except Exception:  # pragma: no cover - 清理是 best effort
            log.debug("sweep_resume_spools failed", exc_info=True)
        if self.advertise:
            try:
                self.engine.announce()
            except Exception:
                log.debug("announce after web start failed", exc_info=True)
        return self._bound_port

    def stop(self, timeout: float = 3.0) -> None:
        """Stop serving and release every socket and thread we own."""
        self._stop.set()
        for sock in list(self._sse_sockets):
            # An SSE response blocks in send(); shutting the socket down is
            # what wakes that thread instead of waiting for the next event.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
        deadline = time.monotonic() + timeout
        for worker in list(self._workers):
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        self._sse_sockets.clear()

    def __enter__(self) -> "WebUI":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    @property
    def running(self) -> bool:
        return self._httpd is not None

    # -- URLs --------------------------------------------------------------

    def urls(self) -> list[str]:
        """Every URL a phone could open to reach this UI."""
        port = self._bound_port or self.port
        hosts = self.addresses() or ["127.0.0.1"]
        return [f"http://{host}:{port}/" for host in hosts]

    @property
    def url(self) -> str:
        """The URL to put in the QR code."""
        return self.urls()[0]

    # -- engine bookkeeping ------------------------------------------------

    def _on_engine_event(self, event: dict[str, Any]) -> None:
        """Cache pending offers so a late client can still answer them."""
        kind = event.get("kind")
        with self._state_lock:
            if kind == "offer_received":
                request_id = str(event.get("request_id", ""))
                if request_id:
                    self._offers[request_id] = event_dict(event)
            elif kind in ("transfer_accepted", "transfer_rejected", "transfer_finished", "transfer_cancelled"):
                transfer_id = str(event.get("transfer_id", ""))
                for key, offer in list(self._offers.items()):
                    if offer.get("transfer_id") == transfer_id or key == transfer_id:
                        del self._offers[key]
            elif kind == "engine_stopped":
                self._offers.clear()
            elif kind == "mobile_found":
                # A phone (the Android app, or any HTTP-only client) announced
                # itself: remember it as a paired device so the desktop lists it
                # even before it opens a page.
                self.register_from_discovery(event)
            # Offers the user never answers expire inside the engine after
            # OFFER_TIMEOUT; prune them so the list cannot grow forever.
            if len(self._offers) > 32:
                cutoff = time.time() - 330
                for key, offer in list(self._offers.items()):
                    if float(offer.get("ts", 0)) < cutoff:
                        del self._offers[key]

    def _pending_offers(self) -> list[dict[str, Any]]:
        with self._state_lock:
            return list(self._offers.values())

    # -- views -------------------------------------------------------------

    def _self_device(self) -> dict[str, Any]:
        info = self.engine.info
        self_addresses = self.addresses()
        return device_dict(
            info,
            tcpPort=self.engine.port,
            webPort=self._bound_port or info.web_port,
            addresses=self_addresses,
            urls=self.urls(),
        )

    def self_peer(self) -> Peer:
        """A :class:`Peer` that points at this very engine over loopback.

        The engine's own device is deliberately absent from
        :meth:`Engine.devices` (discovery drops its own announcements), yet
        "send this photo to the computer I am looking at" is *the* headline
        flow for a phone.  Addressing ourselves over loopback makes the local
        machine an ordinary target: the file goes through the real protocol
        and lands in the receive directory exactly like a remote transfer.
        """
        return Peer(info=self.engine.info, address="127.0.0.1", port=self.engine.port, source="local")

    def _devices(self) -> list[dict[str, Any]]:
        mine = self.engine.info.device_id
        local = device_dict(
            self.engine.info,
            address="127.0.0.1",
            port=self.engine.port,
            addresses=self.addresses(),
            trusted=True,
            source="local",
            rttMs=0.0,
            lastSeen=0.0,
            webUrl=self.url,
            tcpPort=self.engine.port,
            webPort=self._bound_port or self.engine.info.web_port,
            isSelf=True,
            local=True,
            label="本机",
            # 连本机也带这个字段：页面按 online 分「在线 / 离线」两段，
            # 少一个键就得靠 `!== false` 兜，谁少写一处谁那一段就错。
            online=True,
        )
        out: list[dict[str, Any]] = [local]
        now = time.time()
        live: set[str] = set()
        for peer in self.engine.devices():
            # A self-send makes the engine discover itself; never list it
            # twice, and never let it masquerade as a remote device.
            if peer.info.device_id == mine:
                continue
            entry = peer_dict(peer)
            entry["isSelf"] = False
            entry["local"] = False
            # Everything in the peer store was heard from within DEVICE_TTL, so
            # it is online by construction -- but the *phone* needs that said
            # out loud, and it needs "how long ago", so it can show the same
            # 在线 / 未连接 distinction the desktop shows.
            entry["online"] = True
            entry["secondsAgo"] = round(max(0.0, now - float(entry.get("lastSeen") or 0)), 1)
            live.add(str(entry.get("id") or ""))
            out.append(entry)
            self._remember_device(entry, now)
        # 离线：以前见过、现在不在发现表里的设备。页面把它们放在「离线」那一栏，
        # 点开还能看到"最后在线什么时候"。
        for device_id, snapshot in self._offline_devices(live, now):
            out.append(snapshot)
        # 手动添加过的地址一旦握手成功，引擎会用**真实设备号**再登记一条；那条
        # `manual:<地址>:<端口>` 占位记录就成了幽灵行 —— 用户看到的是"同一台设备
        # 两行"（C4）。被真实行覆盖的占位行在这里丢掉，并记进 live，免得它又从
        # 「离线」那份记忆里冒出来。
        out, dropped = self._drop_manual_phantoms(out)
        live.update(dropped)
        # 两台设备真的重名时给个编号（RealProbe / RealProbe #2）——两行一模一样的
        # 名字，用户分不清点的是哪一台（C4 / E1，和电脑端共用 core.model 里的
        # disambiguate_names）。
        names = disambiguate_names(out)
        for entry in out:
            label = names.get(str(entry.get("id") or ""))
            if label:
                entry["name"] = label
        # Remote devices with a reachable web UI first, then by name: the list
        # is a picker, and the wanted entry is almost always near the top.
        out[1:] = sorted(out[1:], key=lambda d: (not d.get("webUrl", ""), d.get("name", "").lower()))
        return out

    @staticmethod
    def _drop_manual_phantoms(
        rows: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """丢掉被真实设备记录覆盖掉的「手动添加」占位行。

        判定要求**同端口**，并且手动那条的地址是回环地址、或者就出现在真实那条的
        地址里 —— 一个端口不可能同时属于两台机器；加上地址这一层，局域网里两台
        设备碰巧都用同一个端口也不会被误并（宁可留两行，也不能把两台设备并成一台）。
        """
        real: dict[int, set[str]] = {}
        for row in rows:
            if str(row.get("source") or "") == "manual":
                continue
            port = int(row.get("tcpPort") or row.get("port") or 0)
            if not port:
                continue
            bucket = real.setdefault(port, set())
            bucket.add(str(row.get("address") or ""))
            for extra in row.get("addresses") or []:
                bucket.add(str(extra))
        kept: list[dict[str, Any]] = []
        dropped: set[str] = set()
        for row in rows:
            if str(row.get("source") or "") == "manual":
                port = int(row.get("tcpPort") or row.get("port") or 0)
                address = str(row.get("address") or "")
                peers = real.get(port) or set()
                loopback = address in ("127.0.0.1", "::1", "localhost", "")
                if peers and (loopback or address in peers):
                    dropped.add(str(row.get("id") or ""))
                    continue
            kept.append(row)
        return kept, dropped

    #: 离线设备最多记这么多台、最多记这么久（内存里的一点点历史，不是数据库）。
    REMEMBER_LIMIT = 24
    REMEMBER_TTL = 24 * 3600.0

    def _remember_device(self, entry: dict[str, Any], now: float) -> None:
        device_id = str(entry.get("id") or "")
        if not device_id:
            return
        with self._state_lock:
            self._remembered[device_id] = {
                "entry": dict(entry),
                "lastSeen": now,
            }
            if len(self._remembered) > self.REMEMBER_LIMIT:
                oldest = sorted(self._remembered.items(), key=lambda item: item[1]["lastSeen"])
                for key, _value in oldest[: len(self._remembered) - self.REMEMBER_LIMIT]:
                    self._remembered.pop(key, None)

    def _offline_devices(self, live: set[str], now: float) -> list[tuple[str, dict[str, Any]]]:
        found: list[tuple[str, dict[str, Any]]] = []
        with self._state_lock:
            for device_id, record in list(self._remembered.items()):
                age = now - float(record.get("lastSeen") or 0)
                if age > self.REMEMBER_TTL:
                    self._remembered.pop(device_id, None)
                    continue
                if device_id in live:
                    continue
                entry = dict(record["entry"])
                entry["online"] = False
                entry["secondsAgo"] = round(max(0.0, age), 1)
                found.append((device_id, entry))
        found.sort(key=lambda item: item[1].get("secondsAgo", 0.0))
        return found

    def state(self) -> dict[str, Any]:
        """Everything the app needs to draw a full frame."""
        receive_dir = self.receive_dir
        try:
            active = [transfer_dict(t) for t in self.engine.active_transfers()]
        except Exception:  # pragma: no cover - defensive
            log.debug("active_transfers failed", exc_info=True)
            active = []
        active.sort(key=lambda t: t["transferId"])
        with self._state_lock:
            uploads = [dict(u) for u in self._uploads.values()]
            recent_uploads = list(self._uploads_done[-20:])
        return {
            "ok": True,
            "app": {
                "name": APP_NAME,
                "nameCn": APP_NAME_CN,
                "version": APP_VERSION,
                # The browser page can only do so much: it is frozen when the
                # phone screen goes off and it cannot ask for the microphone
                # over plain HTTP.  The native app has neither limit, so the
                # page offers it whenever the installer is on this machine.
                "apk": self.apk_info(),
            },
            "device": self._self_device(),
            # 能力宣告：手机 App 据此决定是否启用「断点续传上传」（opt-1017）。
            # 旧客户端忽略未知字段，旧服务端没有这个字段时新 App 退回一次性上传。
            "caps": {
                "uploadResume": True,
                "uploadMaxBytes": self.max_upload_bytes,
                "downloadRange": True,
            },
            "devices": self._devices(),
            "transfers": active,
            "offers": self._pending_offers(),
            "uploads": uploads,
            "recentUploads": recent_uploads,
            "receiveDir": receive_dir,
            "freeSpace": free_space(receive_dir),
            # Who has this page open.  The phone sees itself here, and the
            # desktop reads the same list to show "手机已连接".
            "webClients": self.clients(),
            # Chat summary: the phone renders its conversation list from this,
            # and needs to know which member id is *itself*.
            "chat": {
                "selfId": "",
                "conversations": self.engine.chat.conversations(),
                "unread": self.engine.chat.unread_total(),
            },
            # The remembered ones too: the phone stays in the list while its
            # screen is off, so the desktop never has to be told "keep the
            # browser open" to stay paired.
            "knownClients": self.known_clients(),
            "shares": self.shares(),
            "serverTime": time.time(),
        }

    def list_files(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Files under the receive directory, newest first.

        Bounded in both depth and count: a receive directory can be a whole
        mounted disk, and the phone only ever shows the tail of it.
        """
        root = os.path.realpath(self.receive_dir)
        out: list[dict[str, Any]] = []
        if not os.path.isdir(root):
            return out
        stop = False
        for current, dirs, names in os.walk(root, followlinks=False):
            if stop:
                break
            depth = current[len(root) :].count(os.sep)
            if depth >= 6:
                dirs[:] = []
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in names:
                # Our own spool/resume files are implementation detail.
                if name.startswith(".") or ".eversend." in name:
                    continue
                full = os.path.join(current, name)
                try:
                    stat = os.stat(full)
                except OSError:
                    continue
                if not os.path.isfile(full):
                    continue
                out.append(
                    {
                        "path": os.path.relpath(full, root).replace(os.sep, "/"),
                        "name": name,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    }
                )
                if len(out) >= limit * 2:
                    stop = True
                    break
        out.sort(key=lambda f: f["mtime"], reverse=True)
        return out[:limit]

    def qr_url(self, override: str = "") -> str:
        """The URL encoded into the QR code."""
        if override:
            return override
        port = self._bound_port or self.port
        hosts = self.addresses()
        host = hosts[0] if hosts else "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{port}/"

    # -- uploads -----------------------------------------------------------

    def _find_peer(self, device_id: str, address: str = "", port: int = 0) -> Peer | None:
        """Resolve the target of an upload.

        A discovered peer is preferred because it carries the full identity
        (and its alternate addresses).  Falling back to a literal address lets
        the UI work even when discovery is blocked by the access point.
        """
        if device_id:
            if device_id == self.engine.info.device_id:
                return self.self_peer()
            for peer in self.engine.devices():
                if peer.info.device_id == device_id:
                    return peer
        if address:
            return self.engine.add_manual_device(address, int(port or 0))
        return None

    @property
    def spool_dir(self) -> str:
        """Where an upload waits between the HTTP body and ``Engine.send``.

        A hidden sub-directory rather than the receive directory itself: the
        spool file carries the *user's* file name, and if it sat next to the
        destination it would make the receiver's collision handling rename the
        incoming file to ``photo (1).jpg`` while the original was deleted a
        moment later.  Dot-prefixed, so the resume sweep, ``/api/files`` and
        the user never see it.
        """
        directory = os.path.join(self.receive_dir, _SPOOL_DIRNAME)
        os.makedirs(directory, exist_ok=True)
        return directory

    def spool_path(self, name: str) -> str:
        """A fresh spool file, already carrying the real file name."""
        clean = sanitize_component(name) or "upload.bin"
        with self._upload_lock:
            return unique_path(os.path.join(self.spool_dir, clean))

    # -- 可续传上传（opt-1017） ---------------------------------------------
    #
    # 手机→电脑的大文件以前是"一次 POST 传完"：传到 70% 断网就全废，重发从 0
    # 开始。这里给 spool 加一个**固定名字**（由客户端生成的 uploadId 决定），
    # 断线重连后带 offset 继续写同一个文件；传满 X-EverSend-Total 字节才定稿。
    # 没有 uploadId 的旧客户端仍然走一次性路径，行为完全不变。

    #: uploadId 只接受十六进制，长度有界 —— 它直接进文件名，必须防路径穿越。
    _RESUME_ID_RE = re.compile(r"^[0-9a-f]{16,64}$")

    def valid_resume_id(self, raw: str) -> str:
        candidate = (raw or "").strip().lower()
        return candidate if self._RESUME_ID_RE.match(candidate) else ""

    def resume_spool(self, upload_id: str) -> tuple[str, int]:
        """续传 spool 的路径 + 已经收到的字节数（不存在则 0）。"""
        path = os.path.join(self.spool_dir, "resume-" + upload_id)
        try:
            have = os.path.getsize(path)
        except OSError:
            have = 0
        return path, have

    def resume_lock(self, upload_id: str) -> threading.Lock:
        with self._upload_lock:
            lock = self._resume_locks.get(upload_id)
            if lock is None:
                lock = threading.Lock()
                self._resume_locks[upload_id] = lock
            return lock

    def promote_resume_spool(self, spool: str, name: str) -> str:
        """收满之后把 spool 改回用户文件名，交给既有 finish/land 流程。"""
        clean = sanitize_component(name) or "upload.bin"
        with self._upload_lock:
            final = unique_path(os.path.join(self.spool_dir, clean))
            os.replace(spool, final)
        return final

    def remember_resume_done(self, upload_id: str, payload: dict[str, Any]) -> None:
        with self._upload_lock:
            self._resume_done[upload_id] = dict(payload)
            while len(self._resume_done) > 64:  # 有界，别无限攒
                self._resume_done.pop(next(iter(self._resume_done)))

    def resume_completed(self, upload_id: str) -> dict[str, Any] | None:
        with self._upload_lock:
            payload = self._resume_done.get(upload_id)
            return dict(payload) if payload is not None else None

    def sweep_resume_spools(self, max_age: float = 24 * 3600) -> int:
        """删掉超龄的续传 spool（断线后再没回来续的那些）。"""
        now = time.time()
        removed = 0
        try:
            entries = list(os.scandir(self.spool_dir))
        except OSError:
            return 0
        for entry in entries:
            if not entry.name.startswith("resume-") or not entry.is_file():
                continue
            try:
                if now - entry.stat().st_mtime > max_age:
                    os.unlink(entry.path)
                    removed += 1
            except OSError:
                continue
        if removed:
            log.info("清理了 %d 个过期的续传 spool", removed)
        return removed

    def finish_upload(self, spool: str) -> str:
        """Return the path the engine should send, keeping ``keep_uploads``.

        With ``keep_uploads`` the file is moved into the receive directory so
        the user actually finds it; otherwise it stays in the spool directory
        and is deleted once the transfer is over.
        """
        if not self.keep_uploads:
            return spool
        with self._upload_lock:
            final = unique_path(os.path.join(self.receive_dir, os.path.basename(spool)))
            os.replace(spool, final)
        return final

    def land_upload(
        self,
        spool: str,
        name: str,
        device_id: str = "",
        *,
        from_id: str = "",
        from_name: str = "",
    ) -> dict[str, Any]:
        """把一次"发给自己"的上传落到接收目录，并记成已完成。

        这条路上**没有对端**：不生成传输、不调 ``engine.send``。以前它会掉进
        ``_run_send`` → ``engine.send(self_peer)`` 被"对端没有接收服务"拒掉 →
        ``finally`` 把文件删掉，用户那边是"手机说发出去了，电脑上一个文件都没有"。

        ``from_id`` / ``from_name`` 是**发件人**（那台手机）。以前这里不带发件人，
        电脑端的「接收」列表与会话都看不到"谁给我发了什么" —— 文件落了盘却查无此人。
        """
        os.makedirs(self.receive_dir, exist_ok=True)
        with self._upload_lock:
            final = unique_path(os.path.join(self.receive_dir, os.path.basename(name)))
            os.replace(spool, final)
        upload = {
            "id": uuid.uuid4().hex,
            "name": os.path.basename(final),
            "size": os.path.getsize(final),
            "deviceId": device_id,
            "deviceName": self.engine.info.name,
            "fromId": str(from_id or ""),
            "fromName": str(from_name or ""),
            "status": "done",
            "error": "",
            "path": final,
            "started": time.time(),
            "finished": time.time(),
        }
        self._complete_upload(upload)
        try:
            self.engine.events.emit(
                "web_upload_finished",
                upload_id=upload["id"],
                name=upload["name"],
                path=final,
                size=upload["size"],
                from_id=str(from_id or ""),
                from_name=str(from_name or ""),
            )
        except Exception:  # pragma: no cover - best effort
            pass
        return upload

    def start_send(self, peer: Peer, path: str, pin: str, upload: dict[str, Any]) -> str:
        """Hand a spooled file to the engine and return its transfer id.

        The engine's transfer id is deterministic (it hashes the file set and
        the sender id), so it can be computed here and returned while the
        transfer is still starting -- which is what lets the browser attach
        its progress bar to the right session before the first byte moves.
        """
        transfer_id = ""
        try:
            entries, _sources = build_file_entries([path])
            if entries:
                transfer_id = new_transfer_id(entries, self.engine.info.device_id)
        except Exception:  # pragma: no cover - defensive
            log.debug("could not pre-compute the transfer id", exc_info=True)

        upload["transferId"] = transfer_id
        upload["status"] = "sending"
        self._spawn(self._run_send, peer, path, pin, upload)
        return transfer_id

    def _run_send(self, peer: Peer, path: str, pin: str, upload: dict[str, Any]) -> None:
        """Background leg: spooled file -> the chosen device."""
        try:
            ok = self.engine.send(peer, [path], pin=pin)
            upload["status"] = "done" if ok else "failed"
            if not ok:
                upload["error"] = (
                    "对方没有接收这次传输：确认它正在运行韧传、和你在同一局域网；"
                    "如果它是一台手机，请用「传输 → 接收」里的分享条目等它来取。"
                )
                self._emit_upload_failed(upload)
        except Exception as exc:
            upload["status"] = "failed"
            upload["error"] = str(exc)
            # Surface the failure on the event stream: without it the browser
            # would only see that its own upload succeeded.
            try:
                self.engine.events.emit(
                    "web_upload_failed",
                    upload_id=upload.get("id", ""),
                    transfer_id=upload.get("transferId", ""),
                    name=upload.get("name", ""),
                    error=str(exc),
                )
            except Exception:
                pass
        finally:
            upload["finished"] = time.time()
            if not self.keep_uploads:
                if upload.get("status") == "done":
                    # 送达了：spool 只是过程文件，删掉不留痕。
                    try:
                        os.unlink(path)
                        os.rmdir(os.path.dirname(path))
                    except OSError:
                        pass
                else:
                    # **失败不许销毁证据**（宪章 E3）：以前这里失败也 unlink，
                    # 用户看到的是"上传成功但对面没收到、文件也没了"。
                    # 现在把文件挪进接收目录，记录里写明它落在哪，用户能找到、能重试。
                    kept = self.keep_failed_upload(path)
                    upload["path"] = kept
                    upload["hint"] = "文件已留在电脑的接收目录里：" + kept
                    self._emit_upload_failed(upload)
            self._complete_upload(upload)

    def _emit_upload_failed(self, upload: dict[str, Any]) -> None:
        """把失败推到事件流上（页面据此弹提示）——失败必须看得见。"""
        try:
            self.engine.events.emit(
                "web_upload_failed",
                upload_id=upload.get("id", ""),
                transfer_id=upload.get("transferId", ""),
                name=upload.get("name", ""),
                error=str(upload.get("error") or ""),
                path=str(upload.get("path") or ""),
            )
        except Exception:  # pragma: no cover - event bus is best effort
            pass

    def _complete_upload(self, upload: dict[str, Any]) -> None:
        """一条上传记录的收尾：从"进行中"挪进"最近记录"（页面读它画那一行）。"""
        with self._state_lock:
            self._uploads.pop(upload.get("id", ""), None)
            self._uploads_done.append(dict(upload))
            del self._uploads_done[:-50]

    def keep_failed_upload(self, path: str) -> str:
        """发送失败时把文件从 spool 挪进接收目录，返回最终路径。

        spool 目录是隐藏的、会被清理的临时区；失败的文件留在那里等于丢了。
        """
        try:
            os.makedirs(self.receive_dir, exist_ok=True)
            with self._upload_lock:
                final = unique_path(os.path.join(self.receive_dir, os.path.basename(path)))
                os.replace(path, final)
            return final
        except OSError:
            return path

    def classify_upload_target(self, device_id: str, address: str = "", port: int = 0) -> dict[str, Any]:
        """这次上传是发给谁的：``self`` / ``client``（手机）/ ``peer``（协议对端）/ ``unknown``。

        三条路以前挤在一条上，结果"发给自己"和"发给手机"都会掉进
        ``engine.send``，被拒之后 spool 又被删 —— 用户看到的正是"电脑说发了、
        手机那边什么都没有"。
        """
        wanted = bare_device_id(device_id)
        mine = str(self.engine.info.device_id or "")
        if wanted and wanted == bare_device_id(mine):
            return {"kind": "self", "name": self.engine.info.name}
        if wanted:
            for client in list(self.known_clients()) + list(self.clients()):
                if bare_device_id(client.get("deviceId")) != wanted:
                    continue
                return {
                    "kind": "client",
                    "key": str(client.get("key") or ""),
                    "name": str(client.get("label") or client.get("name") or "手机"),
                }
        if device_id.startswith(("web:", "android:")):
            # 有设备号但注册表里还没有它（刚打开页面）：按客户端处理，
            # 发布时不指定接收方 —— 让所有已连接设备都能看到，总比文件丢了强。
            return {"kind": "client", "key": "", "name": "手机"}
        peer = self._find_peer(device_id, address, port)
        if peer is not None:
            return {"kind": "peer", "peer": peer, "name": peer.info.name}
        return {"kind": "unknown"}

    def register_upload(self, upload: dict[str, Any]) -> None:
        with self._state_lock:
            self._uploads[str(upload["id"])] = upload

    def _spawn(self, target: Callable[..., Any], *args: Any) -> threading.Thread:
        """Start a worker we can join in :meth:`stop` instead of leaking it."""
        thread = threading.Thread(target=target, args=args, daemon=True, name="eversend-upload")
        with self._state_lock:
            self._workers = {t for t in self._workers if t.is_alive()}
            self._workers.add(thread)
        thread.start()
        return thread

    # -- assets ------------------------------------------------------------

    def index_html(self) -> str:
        """The SPA with the CSRF token substituted in.

        Re-read only when the file changes so that editing the assets during
        development shows up on the next reload while a production run does no
        disk I/O at all.
        """
        path = os.path.join(self.asset_dir, "index.html")
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return _MISSING_ASSETS
        cached = self._index_cache
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            with open(path, "r", encoding="utf-8") as handle:
                html = handle.read()
        except OSError:
            return _MISSING_ASSETS
        html = html.replace("__EVERSEND_TOKEN__", self._token)
        self._index_cache = (mtime, html)
        return html

    def resolve_asset(self, relative: str) -> str | None:
        """Map a URL path to a file inside the asset directory, or ``None``.

        Three independent gates, because one is never enough for a path that
        arrives straight off the network: a NUL byte or ``..`` segment is
        rejected outright, and the *resolved* path (symlinks included) must
        still be inside the asset root.
        """
        if not relative or "\x00" in relative:
            return None
        relative = relative.replace("\\", "/")
        if ".." in relative.split("/"):
            return None
        candidate = os.path.realpath(os.path.join(self.asset_dir, relative.lstrip("/")))
        root = self.asset_dir
        if candidate != root and not candidate.startswith(root + os.sep):
            return None
        if not os.path.isfile(candidate):
            return None
        return candidate

    def resolve_download(self, relative: str) -> str | None:
        """Map ``?path=`` to a file inside the receive directory, or ``None``."""
        if not relative or "\x00" in relative:
            return None
        relative = relative.replace("\\", "/").lstrip("/")
        parts = [part for part in relative.split("/") if part not in ("", ".")]
        if ".." in parts:
            return None
        root = os.path.realpath(self.receive_dir)
        candidate = os.path.realpath(os.path.join(root, *parts))
        if candidate != root and not candidate.startswith(root + os.sep):
            return None
        # Never hand out internal state: the resume journal and the upload
        # spool are both dot-prefixed, and a phone that kept a copy of a
        # half-written part file would corrupt a later resume.
        if any(part.startswith(".") or ".eversend." in part for part in parts):
            return None
        if not os.path.isfile(candidate):
            return None
        return candidate

    #: How long a "no installer here" answer is trusted, in seconds.
    APK_CACHE_SECONDS = 5.0

    #: How long a device that was only *overheard* (a UDP announcement, nothing
    #: else) is remembered.  Long enough that a phone which announced and then
    #: opened its page stays listed; short enough that a stray probe does not
    #: sit in the paired list for the rest of the day.
    PROVISIONAL_TTL = 90.0

    def android_apk(self) -> str | None:
        """The Android installer to hand to a phone, or ``None``.

        The app is distributed through the release page, but a phone in China
        often cannot reach GitHub, while it is already talking to this computer
        over the hotspot.  Dropping the ``.apk`` next to the program (or into
        the data directory) is therefore the delivery route that actually
        works, and the page offers it only when the file is really there.

        Checked with a short cache: the phone polls ``/api/state`` every few
        seconds and neither the answer nor the directory changes often.
        """
        now = time.monotonic()
        with self._apk_lock:
            if self._apk_checked and now - self._apk_checked < self.APK_CACHE_SECONDS:
                return self._apk_path
            self._apk_checked = now
            self._apk_path = _find_android_apk(self.data_dirs)
            return self._apk_path

    def apk_info(self) -> dict[str, Any]:
        """What the page needs to offer the download (or hide the card)."""
        path = self.android_apk()
        if not path:
            return {"available": False}
        try:
            size = os.path.getsize(path)
        except OSError:
            return {"available": False}
        return {
            "available": True,
            "name": os.path.basename(path),
            "size": size,
            "url": "/apk",
        }

    def check_host(self, header: str) -> bool:
        """Reject ``Host`` headers that could be a DNS-rebinding attack.

        A browser can be pointed at ``evil.example`` which resolves to this
        LAN address; the page then talks to us with that name in ``Host``.
        Requiring an IP literal or ``localhost`` breaks that trick while
        staying invisible to a phone that scanned the QR code -- which always
        carries an IP literal.

        Someone who genuinely reaches this machine by name (``desk.lan``) can
        say so once with the ``extra_hosts`` constructor argument.
        """
        if not header:
            return False
        host = header.strip()
        if host.startswith("["):  # IPv6 literal
            end = host.find("]")
            host = host[1:end] if end > 0 else host
        elif host.count(":") == 1:
            host = host.split(":", 1)[0]
        host = host.strip().lower().rstrip(".")
        if not host:
            return False
        if host in ("localhost",) or host.endswith(".localhost"):
            return True
        if host in self.extra_hosts:
            return True
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return False
        return True


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


class _ThreadingServer(ThreadingHTTPServer):
    """A threading server that never lets a slow client hold up a shutdown."""

    daemon_threads = True
    allow_reuse_address = True
    #: Match the engine's own timeout so a phone that walks out of Wi-Fi
    #: range does not leave a handler thread parked on a dead socket.
    request_queue_size = 128

    def handle_error(self, request: Any, client_address: Any) -> None:  # noqa: D102
        """客户端中途断连**不是错误**，别把 traceback 打到用户终端。

        `socketserver` 默认在 `handle_error` 里 `traceback.print_exc()`，于是
        "手机把请求取消了""手机切走了 Wi-Fi"这类完全正常的事，会在用户终端里
        刷出一整段二十行的红字 —— 用户会以为程序坏了（真实反馈："终端里有很多报错"）。

        这里把三类**正常的连接结束**降成一行 debug：连接被对方重置、管道断开、
        读写超时（安卓的 HTTP 客户端在后台被冻结时就是这个样子）。其余异常仍走
        我们的 logger 记 warning + traceback，方便真出问题时排查 ——
        但**不再直接吐到 stderr**，用户终端保持干净。
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            log.debug("客户端断开（正常）：%s", client_address)
            return
        if isinstance(exc, (TimeoutError, socket.timeout)):
            log.debug("连接超时（正常）：%s", client_address)
            return
        log.warning("处理请求时出错（%s）：%s", client_address, exc, exc_info=True)


class _Handler(BaseHTTPRequestHandler):
    """One request.

    Note that a handler instance lives for a whole keep-alive *connection*, not
    for a single request, so every per-request flag is reset in
    :meth:`_dispatch` rather than in ``__init__``.
    """

    server_ui: ClassVar[WebUI]
    server_version = f"EverSend/{APP_VERSION}"
    sys_version = ""  # do not advertise the interpreter version
    protocol_version = "HTTP/1.1"
    timeout = 70.0
    #: True while answering HEAD: GET's headers, but no body.
    _head_only = False
    #: Body parsed by the CSRF gate, reused by the route handler.
    _json_cache: dict[str, Any] | None = None
    #: Body bytes consumed so far, so a rejection knows how much is still on
    #: the wire and can swallow it before closing (see ``_drain_unread_body``).
    _body_read = 0

    # -- logging -----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if self.server_ui.log_requests:
            log.info("%s - %s", self.address_string(), fmt % args)
        else:
            log.debug("%s - %s", self.address_string(), fmt % args)

    def log_error(self, fmt: str, *args: Any) -> None:
        log.warning("%s - %s", self.address_string(), fmt % args)

    # -- verbs -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_bytes(
            HTTPStatus.NO_CONTENT,
            b"",
            "text/plain; charset=utf-8",
            extra={"Allow": "GET, HEAD, POST, OPTIONS"},
        )

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    do_DELETE = do_PATCH = do_TRACE = do_PUT  # noqa: N815

    def _method_not_allowed(self) -> None:
        self._send_bytes(
            HTTPStatus.METHOD_NOT_ALLOWED,
            b"method not allowed",
            "text/plain; charset=utf-8",
            extra={"Allow": "GET, HEAD, POST, OPTIONS"},
        )

    # -- routing -----------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        """Validate, gate and route one request, turning any failure into a response."""
        ui = self.server_ui
        # Reset per-request state: a keep-alive connection reuses this object,
        # so a leftover body cache would let one request read the previous
        # request's JSON, and a leftover HEAD flag would blank the next body.
        self._head_only = self.command == "HEAD"
        self._json_cache = None
        self._body_read = 0
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path or "/", errors="replace")
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        except ValueError:
            self._fail(HTTPStatus.BAD_REQUEST, "无法解析的请求地址")
            return

        # 1. DNS-rebinding gate.  Cheap, and it protects every route below.
        if not ui.check_host(self.headers.get("Host", "")):
            self._fail(
                HTTPStatus.FORBIDDEN,
                "这个访问地址不被信任：请扫二维码或改用本机的 IP 地址访问"
                "（如需用主机名访问，请在启动时把它加入 extra_hosts）",
            )
            return

        # 2. CSRF gate for everything that changes state.  The token normally
        #    rides in the header; a JSON or form client may also put it in the
        #    body, which is why this can consume (and cache) the body.
        if method == "POST" and not self._token_ok(path, query) and not self._token_from_body():
            self._fail(HTTPStatus.FORBIDDEN, "缺少或错误的防跨站令牌，请刷新页面")
            return

        # Any accepted request means a browser is there.  Recorded *after* the
        # gates, so a rejected request cannot make the desktop announce a
        # connection that never happened.
        ui.touch_client(
            self.client_address[0] if self.client_address else "",
            self.headers.get("User-Agent", ""),
            device_id=self.headers.get("X-EverSend-Device", ""),
        )

        try:
            self._route(method, path, query)
        except _BadRequest as exc:
            self._fail(HTTPStatus.BAD_REQUEST, str(exc) or "请求体格式错误")
        except _BodyTooLarge as exc:
            self._fail(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc) or "请求体过大")
        except (BrokenPipeError, ConnectionResetError):
            # The phone went to sleep or the tab was closed mid-response.
            self.close_connection = True
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("unhandled error serving %s", path)
            self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, f"服务器内部错误: {exc}")

    def _route(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        """Hand a validated request to its endpoint (or answer 404/405)."""
        ui = self.server_ui
        if path == "/" or path == "/index.html":
            self._send_index()
            return
        if path in ("/favicon.ico", "/assets/favicon.ico"):
            self._send_asset("icon.svg")
            return
        if path.startswith("/assets/"):
            self._send_asset(path[len("/assets/") :])
            return
        if path == "/api/emoji":
            # One palette for all three clients: the desktop imports the module,
            # the page and the app fetch it here.  A face you can send from the
            # phone but not from the desktop would otherwise be a bug report.
            from ..core.emoji import emoji_payload

            self._send_json(HTTPStatus.OK, {"ok": True, "groups": emoji_payload()})
            return
        if path == "/api/state":
            state = ui.state()
            # ``state()`` is also used by the desktop, which has no HTTP client
            # identity; only here do we know which phone is asking.
            state["chat"]["selfId"] = self._self_client_id(ui)
            # 会话列表要有"人能读"的名字：手机 App 就是从这里读列表的，
            # 以前它只能退回会话 id（"d:c96ec2dd94"）。
            state["chat"]["conversations"] = [
                dict(item, **self._conversation_labels(ui, item, state["chat"]["selfId"]))
                for item in state["chat"]["conversations"]
            ]
            # A file addressed to one phone must not show up on another's page.
            state["shares"] = ui.shares(for_key=self._self_client_key(ui))
            self._send_json(HTTPStatus.OK, state)
            return
        if path == "/api/devices":
            self._send_json(HTTPStatus.OK, {"ok": True, "devices": ui._devices()})
            return
        if path == "/api/shares":
            # 手机（网页版与 App）的取文件入口：一份只说"有哪些文件等你取"的小响应。
            # forKey 语义与 /api/state 一致 —— 指定给某台设备的分享只有它看得见，
            # 没指定接收方的（电脑端「发送给手机」）所有已连接设备都能看到。
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "shares": ui.shares(for_key=self._self_client_key(ui)),
                    "total": len(ui._shares),
                },
            )
            return
        if path == "/api/upload/status":
            # 续传辅助接口（opt-1017）：手机断线重连后先问"服务端已经有几字节"。
            # 只读、无副作用；uploadId 是客户端生成的 128 位随机串，猜不到。
            upload_id = ui.valid_resume_id((query.get("uploadId") or [""])[0])
            if not upload_id:
                raise _BadRequest("uploadId 非法（应为 16-64 位十六进制）")
            done = ui.resume_completed(upload_id)
            if done is not None:
                done_size = int(done.get("size") or 0)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "complete": True,
                        "resumeId": upload_id,
                        "offset": done_size,
                        "total": done_size,
                    },
                )
                return
            _spool, have = ui.resume_spool(upload_id)
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "complete": False, "resumeId": upload_id, "offset": have},
            )
            return
        if path == "/api/files":
            files = ui.list_files()
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "dir": ui.receive_dir, "files": files, "free": free_space(ui.receive_dir)},
            )
            return
        if path in ("/api/qr", "/api/qr.svg", "/api/qr.png"):
            self._send_qr(path, query)
            return
        if path == "/apk" or path == "/EverSend.apk":
            # The installer itself, so a phone can get the app from the
            # computer it is already talking to instead of from the internet.
            self._send_apk()
            return
        if path == "/api/download":
            self._send_download(query)
            return
        if path.startswith("/api/share/"):
            # What the desktop handed over for this phone, by opaque id.
            self._send_share(path[len("/api/share/") :])
            return
        if path == "/api/chat":
            self._send_chat(query)
            return
        if path.startswith("/api/chat/media/"):
            self._send_chat_media(path[len("/api/chat/media/") :])
            return
        if path == "/api/events":
            # A stream has no end, so it can never be answered with HEAD, and
            # any other verb would leave a thread parked on an open response.
            if method != "GET" or self._head_only:
                self._fail(HTTPStatus.METHOD_NOT_ALLOWED, "事件流只支持 GET")
                return
            self._send_events()
            return
        if method == "POST":
            if path == "/api/announce":
                ui.engine.announce()
                self._send_json(HTTPStatus.OK, {"ok": True})
                return
            if path == "/api/scan":
                ui.engine.scan()
                self._send_json(HTTPStatus.ACCEPTED, {"ok": True, "scanning": True})
                return
            if path == "/api/share":
                self._share_files()
                return
            if path == "/api/hello":
                self._hello()
                return
            if path == "/api/chat/send":
                self._chat_send()
                return
            if path == "/api/chat/upload":
                self._chat_upload(query)
                return
            if path == "/api/leave":
                # The page is going away on purpose.  Drop it now; the desktop
                # would otherwise keep showing it for another 15 seconds.
                forgotten = self.server_ui.forget_client(
                    self.client_address[0] if self.client_address else "",
                    self.headers.get("User-Agent", ""),
                    device_id=self.headers.get("X-EverSend-Device", ""),
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "forgotten": forgotten})
                return
            if path == "/api/devices/forget-offline":
                forgotten = ui.forget_offline_devices()
                self._send_json(HTTPStatus.OK, {"ok": True, "forgotten": forgotten})
                return
            if path == "/api/upload":
                self._receive_upload(query)
                return
            if path == "/api/offer/respond":
                self._respond_offer()
                return
            if path == "/api/cancel":
                self._cancel_transfer()
                return
            if path == "/api/trust":
                self._set_trust()
                return
            if path == "/api/peer":
                self._add_peer()
                return
            self._fail(HTTPStatus.NOT_FOUND, "没有这个接口")
            return
        if path in _POST_ONLY_ROUTES:
            # Right path, wrong verb: say so instead of pretending it is absent.
            self._method_not_allowed()
            return
        self._fail(HTTPStatus.NOT_FOUND, "没有这个接口")

    # -- security helpers --------------------------------------------------

    def _token_ok(self, path: str, query: dict[str, list[str]]) -> bool:
        """Constant-time comparison of the CSRF token from header or query.

        The header is the normal path; the query parameter exists for
        ``<form>``-style posts and for ``/api/upload``, whose body is raw file
        bytes and therefore cannot carry a field.
        """
        token = self.server_ui.token
        supplied = self.headers.get(TOKEN_HEADER, "")
        if not supplied:
            supplied = (query.get("token") or [""])[0]
        if not supplied:
            return False
        return hmac.compare_digest(supplied, token)

    def _token_from_body(self) -> bool:
        """Check a token carried inside a JSON or form-encoded body.

        The body is consumed here and cached, so the route handler that runs
        next still sees it.  Small JSON bodies make this cheap; the raw-bytes
        upload endpoint never takes this path (it has no parseable body).
        """
        kind = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if kind == "application/json":
            try:
                raw = self._read_body()
                parsed = json.loads(raw.decode("utf-8"))
            except (_BadRequest, _BodyTooLarge, ValueError, UnicodeDecodeError):
                return False
            if not isinstance(parsed, dict):
                return False
            self._json_cache = parsed
            supplied = parsed.get("token", "")
        elif kind == "application/x-www-form-urlencoded":
            try:
                raw = self._read_body()
            except (_BadRequest, _BodyTooLarge):
                return False
            fields = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
            supplied = (fields.get("token") or [""])[0]
        else:
            return False
        return bool(supplied) and hmac.compare_digest(str(supplied), self.server_ui.token)

    # -- request body ------------------------------------------------------

    def _body_length(self) -> int | None:
        """Declared body length, or ``None`` for a chunked body."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            encoding = (self.headers.get("Transfer-Encoding") or "").lower()
            if "chunked" in encoding:
                return None
            raise _BadRequest("缺少 Content-Length")
        try:
            length = int(raw)
        except (TypeError, ValueError):
            raise _BadRequest("Content-Length 非法") from None
        if length < 0:
            raise _BadRequest("Content-Length 非法")
        return length

    def _iter_body(self, limit: int) -> Iterator[bytes]:
        """Yield the request body in bounded pieces, never buffering it all."""
        length = self._body_length()
        total = 0
        if length is None:
            yield from self._iter_chunked(limit)
            return
        if length > limit:
            raise _BodyTooLarge(f"请求体超过上限 {limit} 字节")
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(IO_CHUNK, remaining))
            if not chunk:
                raise _BadRequest("请求体不完整")
            remaining -= len(chunk)
            self._body_read += len(chunk)
            total += len(chunk)
            if total > limit:
                raise _BodyTooLarge(f"请求体超过上限 {limit} 字节")
            yield chunk

    def _iter_chunked(self, limit: int) -> Iterator[bytes]:
        """Decode ``Transfer-Encoding: chunked`` bodies.

        Mobile Safari occasionally streams a large ``FormData`` upload this
        way, and a naive server would either reject it or read forever.
        """
        total = 0
        while True:
            line = self.rfile.readline(1024)
            if not line:
                raise _BadRequest("请求体不完整")
            size_field = line.split(b";", 1)[0].strip()
            try:
                size = int(size_field, 16)
            except ValueError:
                raise _BadRequest("分块长度非法") from None
            if size == 0:
                while True:  # consume optional trailers
                    trailer = self.rfile.readline(1024)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                return
            total += size
            if total > limit:
                raise _BodyTooLarge(f"请求体超过上限 {limit} 字节")
            remaining = size
            while remaining > 0:
                chunk = self.rfile.read(min(IO_CHUNK, remaining))
                if not chunk:
                    raise _BadRequest("请求体不完整")
                remaining -= len(chunk)
                self._body_read += len(chunk)
                yield chunk
            self.rfile.read(2)  # trailing CRLF

    def _read_body(self, limit: int = MAX_JSON_BODY) -> bytes:
        """Read a whole (small) body, refusing anything above ``limit``."""
        return b"".join(self._iter_body(limit))

    def _json_body(self) -> dict[str, Any]:
        """The request body as a JSON object, empty when there is none."""
        if self._json_cache is not None:
            return self._json_cache
        raw = self._read_body()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise _BadRequest("请求体不是合法的 JSON") from None
        if not isinstance(parsed, dict):
            raise _BadRequest("请求体必须是 JSON 对象")
        return parsed

    # -- responses ---------------------------------------------------------

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        extra: dict[str, str] | None = None,
        cache: str = "no-store",
    ) -> None:
        """Send a complete, length-delimited response."""
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        # HEAD reports the headers a GET would produce, but no body.
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        # The page is same-origin only; these headers cost nothing and stop a
        # stray browser extension from framing or sniffing it.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body and not self._head_only:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                # The phone locked its screen mid-response.  Nothing to do
                # but drop the connection without a traceback.
                self.close_connection = True

    def _send_json(self, status: int, payload: Any) -> None:
        """Send ``payload`` as JSON, serialising engine objects on the way."""
        body = json.dumps(payload, ensure_ascii=False, default=_jsonable).encode("utf-8")
        self._send_bytes(status, body, _JSON_TYPE)

    def _fail(self, status: int, message: str, extra: dict[str, Any] | None = None) -> None:
        """Errors are JSON for the API and text for everything else.

        ``extra`` 是给客户端的**可执行信息**（宪章 E3：失败要带下一步）：
        手机端拿它把一句"失败"变成用户看得懂、能照做的一句话。``error`` 这个键
        本身一个字都不改 —— 老客户端读的就是它。

        A failed request may still have an unread body; keeping the connection
        alive would make the next parse read those bytes as a request, so the
        connection is closed instead.
        """
        if self.command == "POST":
            self.close_connection = True
        payload: dict[str, Any] = {"ok": False, "error": message}
        if extra:
            payload.update(extra)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        content_type = _JSON_TYPE
        if not (self.path or "").startswith("/api/"):
            body = message.encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        self._send_bytes(
            status,
            body,
            content_type,
            extra={"Connection": "close"} if self.close_connection else None,
        )
        if self.close_connection:
            self._drain_unread_body()

    #: How much of a rejected body is swallowed before the socket is closed.
    #: Comfortably above the JSON endpoints' own cap, far below an upload.
    DRAIN_LIMIT = 8 * 1024 * 1024

    def _drain_unread_body(self, limit: int = DRAIN_LIMIT) -> None:
        """Read and discard what is left of a rejected request body.

        Closing a socket that still has unread data in its receive buffer makes
        the kernel answer with a RST rather than a FIN -- and a RST *throws the
        response away*, so the client reports "connection reset" instead of the
        413 (or 403) that was just sent.  Which of the two happens is a race
        between the response and the rest of the body, so the same request
        passes on Linux and fails on Windows/Wine: exactly what the browser
        self-test showed, once in every few runs.

        Only what the client *declared* is drained, and never more than
        ``limit``: a hostile or merely oversized upload must not be read to the
        end just to deliver an error page.
        """
        try:
            length = self._body_length()
        except _BadRequest:
            return
        if length is None:  # chunked: the framing is unusable once we bail out
            return
        remaining = min(length - self._body_read, limit)
        if remaining <= 0:
            return
        try:
            self.connection.settimeout(2.0)
        except OSError:
            return
        try:
            while remaining > 0:
                block = self.rfile.read(min(IO_CHUNK, remaining))
                if not block:
                    break
                remaining -= len(block)
                self._body_read += len(block)
        except (OSError, ValueError):
            # A client that stopped sending (or a half-closed socket) leaves
            # nothing more to drain; closing now is the only option anyway.
            pass

    # -- static content ----------------------------------------------------

    def _send_index(self) -> None:
        """Serve the app shell, never from cache: it carries the CSRF token."""
        html = self.server_ui.index_html().encode("utf-8")
        self._send_bytes(
            HTTPStatus.OK,
            html,
            "text/html; charset=utf-8",
            extra={
                # A stale page would carry a stale CSRF token, so the shell is
                # never cached; the assets it pulls in are.
                #
                # media-src / img-src 的 blob: 必须显式写出来（Chromium 不把
                # blob: 算进 'self'）：待发文件是本机选的，缩略图、视频封面帧、
                # 预览都走 blob URL。不写这两条时 <video>/<audio>/<img> 全都加载
                # 不了，浏览器只报 "Media load rejected by URL safety check"。
                "Content-Security-Policy": (
                    "default-src 'none'; img-src 'self' data: blob:; "
                    "media-src 'self' blob:; "
                    "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                    "connect-src 'self'; base-uri 'none'; form-action 'none'; "
                    "frame-ancestors 'none'"
                ),
            },
            cache="no-store, must-revalidate",
        )

    def _send_asset(self, relative: str) -> None:
        path = self.server_ui.resolve_asset(relative)
        if path is None:
            self._fail(HTTPStatus.NOT_FOUND, "资源不存在")
            return
        try:
            stat = os.stat(path)
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            self._fail(HTTPStatus.NOT_FOUND, "资源无法读取")
            return
        # "no-cache" means revalidate, not "do not store": with an ETag, a
        # phone reloading a page it already has costs one 304 instead of 30 KB
        # of JavaScript over Wi-Fi, while an edited asset still shows up.
        etag = f'W/"{int(stat.st_mtime)}-{stat.st_size:x}"'
        if self.headers.get("If-None-Match", "").strip() == etag:
            self._send_bytes(
                HTTPStatus.NOT_MODIFIED, b"", _MIME_TYPES[".txt"], extra={"ETag": etag}, cache="no-cache"
            )
            return
        extension = os.path.splitext(path)[1].lower()
        content_type = _MIME_TYPES.get(extension, "application/octet-stream")
        self._send_bytes(HTTPStatus.OK, body, content_type, extra={"ETag": etag}, cache="no-cache")

    def _send_qr(self, path: str, query: dict[str, list[str]]) -> None:
        ui = self.server_ui
        override = (query.get("url") or [""])[0]
        url = ui.qr_url(override)
        try:
            if path.endswith(".png"):
                body = qr.png(url, scale=6)
                self._send_bytes(HTTPStatus.OK, body, "image/png")
                return
            body = qr.svg(url, scale=8, title="EverSend").encode("utf-8")
        except qr.QrError as exc:
            self._fail(HTTPStatus.BAD_REQUEST, f"无法生成二维码: {exc}")
            return
        self._send_bytes(
            HTTPStatus.OK, body, _MIME_TYPES[".svg"], extra={"X-EverSend-URL": url}
        )

    # -- downloads ---------------------------------------------------------

    def _send_share(self, share_id: str) -> None:
        """手机来取一个电脑发布出来的文件（`GET /api/share/<id>`）。

        **进度语义**（这一条是本轮真机问题的核心）：手机是纯 HTTP 客户端，
        电脑推不过去，所以"传到哪了"只有服务端知道。这里把
        ``start + 本次已发出的字节数`` 报给 `note_share_progress` —— 分几次
        Range 请求续传时，它是**总体进度**，不是某一段的进度；而且
        `note_share_progress` 本身只增不减（乱序重试、从头重下都不会让进度倒退）。

        **什么时候算"取完了"**：只有整个文件都发出去过才算 —— 一次只发了
        中间一段（Range 206）就写 `downloaded=True` 是错的，电脑端会显示
        "手机已取走"，而手机只拿到半截。
        """
        ui = self.server_ui
        share_id = urllib.parse.unquote(share_id)
        state, path, name = ui.share_lookup(share_id)
        if state == "missing":
            self._fail(
                HTTPStatus.NOT_FOUND,
                "这个文件已经不在分享列表里了（分享条目过期或被顶掉），让电脑重新发一次。",
            )
            return
        if state == "gone":
            self._fail(
                HTTPStatus.NOT_FOUND,
                "文件「" + (name or "这个文件") + "」已经不在了：它在电脑上被删掉或移走了，让电脑重新发一次。",
            )
            return

        try:
            total = int(os.path.getsize(path))
        except OSError:
            self._fail(HTTPStatus.NOT_FOUND, "文件读不到了：请让电脑重新发一次。")
            return

        # 这次请求要发的是哪一段（和 _stream_file 用同一个解析器，判定不会打架）。
        start, end = 0, max(0, total - 1)
        range_header = self.headers.get("Range", "")
        if range_header and _range_unsatisfiable(range_header, total):
            # 这一段要不到：让 _stream_file 回 416 + 中文说明（含 Content-Range）。
            self._stream_file(path, on_progress=None)
            return
        if range_header:
            parsed = _parse_range(range_header, total)
            if parsed is not None:
                start, end = parsed
            # 解析不出来（多段、写坏了）就当整文件发：whole=True，进度照样对。

        def note(sent_now: int) -> None:
            # 总体进度 = 这一段的起点 + 这一段已经发出去多少。
            ui.note_share_progress(share_id, min(total, start + sent_now) if total else start + sent_now)
            if total and start + sent_now >= total:
                # 续传把最后一段补齐了：这才是真的取完。
                ui.mark_share_downloaded(share_id)

        whole = start == 0 and end >= max(0, total - 1)
        self._stream_file(
            path,
            on_complete=(lambda: ui.mark_share_downloaded(share_id)) if whole else None,
            on_progress=note,
        )

    def _send_apk(self) -> None:
        """``GET /apk``: hand the Android installer to a phone.

        A plain link, not a fetch with the CSRF header: the browser has to own
        the download (it shows progress, and on Android it hands the file to
        the package installer).  Receiving files is already open to the LAN by
        design, so serving this one file adds nothing to the trust model.
        """
        path = self.server_ui.android_apk()
        if not path:
            self._fail(
                HTTPStatus.NOT_FOUND,
                "本机没有安卓安装包。把 EverSend-android.apk 放到韧传目录（或 data 目录）里，"
                "手机刷新本页就能下载安装。",
            )
            return
        self._stream_file(path)

    def _send_download(self, query: dict[str, list[str]]) -> None:
        """Stream a received file, honouring a single-range ``Range`` header."""
        relative = (query.get("path") or [""])[0]
        path = self.server_ui.resolve_download(relative)
        if path is None:
            self._fail(HTTPStatus.NOT_FOUND, "文件不存在或不在接收目录内")
            return
        self._stream_file(path)

    def _stream_file(self, path: str, on_complete=None, on_progress=None) -> None:
        """Send one local file, honouring a single-range ``Range`` header."""
        try:
            size = os.path.getsize(path)
        except OSError:
            self._fail(HTTPStatus.NOT_FOUND, "文件无法读取")
            return

        start, end = 0, size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range", "")
        if range_header and _range_unsatisfiable(range_header, size):
            # 规则上要不到的那一段：416 + 一句中文 + Content-Range（标准做法）。
            # 以前这里回的是空 body，手机只看到状态码，日志里也说不出所以然。
            message = (
                "Range " + range_header + " 要不到：这个文件一共 " + str(size) + " 字节，"
                "请从 0 到 " + str(max(0, size - 1)) + " 之间取，或者不带 Range 直接下整个文件。"
            )
            self._send_bytes(
                HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                message.encode("utf-8"),
                "text/plain; charset=utf-8",
                extra={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
            )
            return
        if range_header:
            parsed = _parse_range(range_header, size)
            if parsed is not None:
                start, end = parsed
                status = HTTPStatus.PARTIAL_CONTENT
            # 解析不出来（多段 / 写坏了）就忽略这个头、整个文件照发 —— HTTP 的规矩，
            # 也是下载管理器（它们爱发多段）不被打回 416 的原因。

        length = max(0, end - start + 1)
        filename = os.path.basename(path)
        disposition = (
            "attachment; filename=\""
            + filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
            + "\"; filename*=UTF-8''"
            + urllib.parse.quote(filename, safe="")
        )
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": disposition,
        }
        if status == HTTPStatus.PARTIAL_CONTENT:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"

        self.send_response(int(status))
        self.send_header("Content-Type", _MIME_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self._head_only or length == 0:
            return

        # Read in bounded blocks so a 4 GB video never lands in RAM, and stop
        # the instant the phone stops reading (which is what happens when it
        # sleeps): the socket write raises and we release the handle.
        remaining = length
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                while remaining > 0:
                    block = handle.read(min(IO_CHUNK, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
                    if on_progress is not None:
                        # 报进度本身不能把传输搞挂：手机那边断了就算了。
                        try:
                            on_progress(length - remaining)
                        except Exception:
                            on_progress = None
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        if remaining == 0 and on_complete is not None:
            on_complete()

    # -- event stream ------------------------------------------------------

    def _send_events(self) -> None:
        """Server-Sent Events: one long-lived response per open tab."""
        ui = self.server_ui
        # A stream has no length, so it can only be delimited by closing the
        # connection; that also keeps proxies from buffering it forever.
        self.close_connection = True
        try:
            self.connection.settimeout(None)
        except OSError:
            pass
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        with ui._state_lock:
            ui._sse_sockets.add(self.connection)

        mailbox = ui.engine.events.subscribe()
        try:
            self._sse_write("retry: 2000\n\n")
            self._sse_write(
                "event: hello\ndata: "
                + json.dumps(
                    {
                        "kind": "hello",
                        "device": ui._self_device(),
                        "serverTime": time.time(),
                    },
                    ensure_ascii=False,
                    default=_jsonable,
                )
                + "\n\n"
            )
            last_beat = time.monotonic()
            # 一条**开着的**事件流就说明"这台设备还在"。
            #
            # 以前只有离散的 HTTP 请求才会 touch_client，于是手机端那个保活服务
            # 一旦把 SSE 挂上（它设计上就该一直挂着），主循环就阻塞在事件流里、
            # 心跳请求再也发不出去；而这边 15 秒没收到请求就判离线 —— 用户看到的
            # 是"手机明明连着、电脑端却显示未连接 / 最后在线 X 分钟前"，发给它的
            # 文件被当成离线留着。安卓仪器测试 keepAliveServiceStaysOnline 抓的
            # 就是这个。
            #
            # 每 CLIENT_TTL 的一半碰一次，留一倍余量：不能只在 ping 的时候碰，
            # SSE_HEARTBEAT(15s) 并不比 CLIENT_TTL(15s) 短。
            last_touch = 0.0
            while not ui._stop.is_set():
                try:
                    event = mailbox.get(timeout=1.0)
                except queue.Empty:
                    event = None
                now = time.monotonic()
                if now - last_touch >= CLIENT_TTL / 2:
                    last_touch = now
                    ui.touch_client(
                        self.client_address[0] if self.client_address else "",
                        self.headers.get("User-Agent", ""),
                        device_id=self.headers.get("X-EverSend-Device", ""),
                    )
                if event is not None:
                    self._sse_write(
                        "data: " + json.dumps(event_dict(event), ensure_ascii=False, default=_jsonable) + "\n\n"
                    )
                    continue
                if now - last_beat >= SSE_HEARTBEAT:
                    last_beat = now
                    # The heartbeat is what keeps NAT tables and Android's
                    # doze mode from silently dropping an idle stream.
                    self._sse_write(": ping\n\n")
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            # Both of these are leaks if they are skipped, and both are
            # skipped by every early return above without a ``finally``.
            ui.engine.events.unsubscribe(mailbox)
            with ui._state_lock:
                ui._sse_sockets.discard(self.connection)

    def _sse_write(self, text: str) -> None:
        """Write and flush one SSE frame; an exception here ends the stream."""
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()

    # -- mutating endpoints ------------------------------------------------

    def _accept_upload_body(self, ui, spool: str, declared: int | None) -> int | None:
        """把上传的请求体流式写进 ``spool``；失败时清理并抛出。

        三类目标（self / client / peer）都要先把字节收下来，所以只写这一份。
        返回写入的字节数；``declared`` 是 Content-Length（可能没有）。
        """
        written = 0
        try:
            with open(spool, "wb", buffering=0) as handle:
                for chunk in self._iter_body(ui.max_upload_bytes):
                    handle.write(chunk)
                    written += len(chunk)
            if written == 0 and declared != 0:
                raise _BadRequest("请求体为空")
        except BaseException:
            # Any failure -- a sleeping phone, a disk error, a refused body --
            # must not leave a spool file behind.
            try:
                os.unlink(spool)
            except OSError:
                pass
            raise
        return written

    def _append_upload_body(self, ui, spool: str, total: int, have: int) -> int:
        """把**续传**请求体追加进 spool；断线时保留已写部分（这正是续传的意义）。

        和 ``_accept_upload_body`` 的区别：失败时**不删** spool —— 已经收到的
        字节就是下次续传的起点；只有收满 ``total`` 才由调用方定稿/改名。
        """
        written = 0
        remaining = max(1, total - have)
        with open(spool, "ab", buffering=0) as handle:
            for chunk in self._iter_body(remaining):
                handle.write(chunk)
                written += len(chunk)
        if written == 0:
            raise _BadRequest("请求体为空")
        return written

    def _resume_conflict(self, upload_id: str, have: int, total: int) -> None:
        """续传偏移对不上：把服务端真实偏移告诉客户端，让它从那里继续。

        HTTP 409 是"请求与当前资源状态冲突"的标准语义；手机端收到后读
        ``offset`` 字段重新发剩余字节即可，不需要用户重选文件。

        用 ``_fail`` 而不是裸 ``_send_json``：请求体还没读，keep-alive 会把
        残余字节当成下一个请求解析 —— ``_fail`` 会关连接并排掉未读 body。
        """
        self._fail(
            HTTPStatus.CONFLICT,
            f"偏移不匹配：服务端已有 {have} 字节，请从该偏移继续",
            extra={
                "offset": have,
                "total": total,
                "resumeId": upload_id,
                "complete": False,
            },
        )

    def _receive_upload(self, query: dict[str, list[str]]) -> None:
        """``POST /api/upload``: the body *is* the file.

        The bytes are streamed to a spool file and then handed to the engine,
        so an upload costs one file handle and a fixed amount of memory no
        matter whether it is 2 KB or 20 GB.
        """
        ui = self.server_ui
        name = (query.get("name") or [""])[0]
        device_id = (query.get("deviceId") or [""])[0]
        address = (query.get("address") or [""])[0]
        pin = (query.get("pin") or [""])[0]

        # A phone cannot receive a protocol transfer, so "send to that phone"
        # means "hand my file to its page".  The file is the one just uploaded
        # -- the caller never names a path on this machine, which is what keeps
        # this safe to expose to the LAN (unlike /api/share, which is loopback
        # only for exactly that reason).
        try:
            port = int((query.get("port") or ["0"])[0] or 0)
        except ValueError:
            port = 0

        clean = sanitize_component(name)
        if not clean:
            raise _BadRequest("缺少合法的文件名 (name)")

        # 目标分三类，路由完全不同（以前只有"对端"这一类，另外两类都掉进
        # engine.send 里，然后 spool 被 finally 删掉 —— 用户看到"202 之后文件蒸发"）：
        #   self   → 发给自己 = **本地落地**，不走网络；
        #   client → 发给手机（App / 浏览器）：它没有接收服务，只能发布到分享表等它来取；
        #   peer   → 协议对端，才是真正的一次 engine.send。
        target = ui.classify_upload_target(device_id, address, port)
        if target["kind"] == "unknown":
            self._fail(
                HTTPStatus.NOT_FOUND,
                "未找到目标设备，请让对方保持在线后重新扫描",
                extra={"hint": "让对方打开韧传（或手机页面）再点一次「扫描」；也可以用「扫描 ▾ › 手动输入地址」直接填它的 IP。"},
            )
            return

        # 可续传上传（opt-1017）：带 uploadId 的请求，body 是「从 offset 起的剩余
        # 字节」，写进固定名字的 spool；断线重连后从服务端已有字节继续。收满
        # X-EverSend-Total 才走定稿流程。**没有 uploadId 的旧客户端完全走原路径。**
        raw_upload_id = (query.get("uploadId") or [""])[0]
        resumable_id = ui.valid_resume_id(raw_upload_id)
        if raw_upload_id and not resumable_id:
            raise _BadRequest("uploadId 非法（应为 16-64 位十六进制）")
        resumable = bool(resumable_id)
        resume_total = 0
        resume_offset = 0
        if resumable:
            try:
                resume_offset = int((query.get("offset") or ["0"])[0] or 0)
            except ValueError:
                raise _BadRequest("offset 非法") from None
            raw_total = self.headers.get("X-EverSend-Total", "")
            try:
                resume_total = int(raw_total)
            except (TypeError, ValueError):
                raise _BadRequest("续传上传缺少合法的 X-EverSend-Total 头") from None
            if resume_offset < 0 or resume_total <= 0 or resume_offset > resume_total:
                raise _BadRequest("offset / X-EverSend-Total 越界")
            if resume_total > ui.max_upload_bytes:
                raise _BodyTooLarge(f"这个文件超过服务端上限 {ui.max_upload_bytes} 字节")

        written = 0
        if resumable:
            replay = ui.resume_completed(resumable_id)
            if replay is not None:
                # 幂等重放：客户端断线重发最后一段时不能把同一文件再落一次盘。
                replay.update(
                    {
                        "ok": True,
                        "complete": True,
                        "resumeId": resumable_id,
                        "offset": replay.get("size", resume_total),
                        "total": resume_total,
                    }
                )
                self._send_json(HTTPStatus.OK, replay)
                return
            spool, have = ui.resume_spool(resumable_id)
            if resume_offset != have:
                self._resume_conflict(resumable_id, have, resume_total)
                return
            if have >= resume_total:
                # 上次收满了但没来得及定稿（例如进程在收尾时崩溃）：直接定稿。
                spool_final = ui.promote_resume_spool(spool, clean)
                written = have
            else:
                expected = resume_total - have
                declared = self._body_length()
                if declared is not None and declared > expected:
                    raise _BadRequest(
                        f"Content-Length 超过剩余字节数（本次最多 {expected} 字节）"
                    )
                available = free_space(ui.receive_dir)
                if available and expected > available:
                    self._fail(
                        HTTPStatus.INSUFFICIENT_STORAGE,
                        f"接收目录剩余空间不足（还需要 {expected} 字节，可用 {available} 字节）",
                    )
                    return
                with ui.resume_lock(resumable_id):
                    spool, have = ui.resume_spool(resumable_id)
                    if resume_offset != have:
                        self._resume_conflict(resumable_id, have, resume_total)
                        return
                    got = self._append_upload_body(ui, spool, resume_total, have)
                    written = have + got
                if written < resume_total:
                    # 还没收满（客户端主动分段/连接被切）：保留 spool 等下一次续传。
                    self._send_json(
                        HTTPStatus.ACCEPTED,
                        {
                            "ok": True,
                            "complete": False,
                            "offset": written,
                            "total": resume_total,
                            "resumeId": resumable_id,
                            "name": clean,
                        },
                    )
                    return
                spool_final = ui.promote_resume_spool(spool, clean)
        else:
            # 空间预检与 spool 准备对所有目标都一样：**先拒绝再写**，比写一半失败好。
            declared = self._body_length()
            if declared is not None:
                available = free_space(ui.receive_dir)
                if available and declared > available:
                    self._fail(
                        HTTPStatus.INSUFFICIENT_STORAGE,
                        f"接收目录剩余空间不足（需要 {declared} 字节，可用 {available} 字节）",
                    )
                    return
            spool_final = ui.spool_path(clean)
            written = self._accept_upload_body(ui, spool_final, declared)

        def finish_resumable(payload: dict[str, Any]) -> dict[str, Any]:
            """续传上传的完成响应：登记幂等快照并补齐 complete/resumeId 字段。"""
            if not resumable:
                return payload
            ui.remember_resume_done(resumable_id, payload)
            merged = dict(payload)
            merged.update(
                {
                    "complete": True,
                    "resumeId": resumable_id,
                    "offset": written,
                    "total": written,
                }
            )
            return merged

        if target["kind"] == "self":
            # "发给我自己" = 本地落地：不生成传输、不调 engine.send。
            # 带上发件人（那台手机），否则电脑端「接收」列表里这一行没有来源。
            sender_id = self._self_client_id(ui)
            sender_name = self._self_client_name(ui)
            upload = ui.land_upload(
                spool_final,
                clean,
                device_id=str(ui.engine.info.device_id),
                from_id=sender_id,
                from_name=sender_name,
            )
            self._send_json(
                HTTPStatus.ACCEPTED,
                finish_resumable(
                    {
                        "ok": True,
                        "uploadId": upload["id"],
                        "name": upload["name"],
                        "size": upload["size"],
                        "path": upload.get("path", ""),
                        "local": True,
                    }
                ),
            )
            return

        peer = target.get("peer")
        final = ui.finish_upload(spool_final)

        upload = {
            "id": uuid.uuid4().hex,
            "name": os.path.basename(final),
            "size": written,
            "deviceId": peer.info.device_id if peer is not None else device_id,
            "deviceName": peer.info.name if peer is not None else str(target.get("name") or "手机"),
            "status": "queued",
            "error": "",
            "started": time.time(),
            "finished": 0.0,
        }
        if target["kind"] == "client":
            # 手机没有接收服务：发布到分享表，等它自己来取（进度、回执都在那条路上）。
            shared = ui.share_files([final], only_key=str(target.get("key") or ""))
            if not shared:
                self._fail(
                    HTTPStatus.NOT_FOUND,
                    "文件没能发布给这台手机：" + str(target.get("name") or device_id),
                    extra={"hint": "让它在设备列表里显示为「已连接」，然后重发一次。"},
                )
                return
            upload["status"] = "handed-off"
            upload["deviceName"] = str(target.get("name") or "手机")
            upload["shareId"] = shared[0]["id"]
            upload["finished"] = time.time()
            ui.register_upload(upload)
            self._send_json(
                HTTPStatus.ACCEPTED,
                finish_resumable(
                    {"ok": True, "upload": upload, "handedOff": True, "shareId": shared[0]["id"]}
                ),
            )
            return
        ui.register_upload(upload)
        transfer_id = ui.start_send(peer, final, pin, upload)
        self._send_json(
            HTTPStatus.ACCEPTED,
            finish_resumable(
                {
                    "ok": True,
                    "uploadId": upload["id"],
                    "transferId": transfer_id,
                    "name": upload["name"],
                    "size": written,
                    "device": device_dict(peer.info),
                }
            ),
        )

    # -- chat --------------------------------------------------------------

    def _self_client_name(self, ui) -> str:
        """这台手机自称的名字（``V2408A``），拿不到才退回"安卓 App/网页版"。

        以前消息署名直接用 ``describe_agent(User-Agent)``，于是手机发来的每一句
        都写着「安卓 App」—— 用户看到的却是设备列表里的 V2408A，同一个人两个名字。
        """
        try:
            clients = ui.known_clients()
        except Exception:
            clients = []
        address = self.client_address[0] if self.client_address else ""
        device_id = bare_device_id(self.headers.get("X-EverSend-Device", ""))
        agent = self.headers.get("User-Agent", "")
        for client in clients:
            same_device = device_id and str(client.get("deviceId") or "") == device_id
            same_address = bool(address) and str(client.get("address") or "") == address
            if not (same_device or same_address):
                continue
            name = str(client.get("name") or "").strip()
            if name and name not in ("未知设备",):
                return name
        return describe_agent(agent) or "手机"

    def _self_client_id(self, ui) -> str:
        """A stable id for *this* phone inside conversations.

        The browser registry already keys a phone by address + User-Agent, and
        that key survives a screen-off freeze, so a conversation with a phone
        keeps pointing at the same member.
        """
        address = self.client_address[0] if self.client_address else ""
        agent = self.headers.get("User-Agent", "")
        device_id = bare_device_id(self.headers.get("X-EverSend-Device", ""))
        if device_id:
            # 稳定身份：换网络、换 IP 都还是这台设备，会话不会分身。
            # （原生 App 走 /api/hello 登记成 android:<id>，网页版这里就是
            #   web:<id>，两边各自稳定。）前缀只留一个，不能拼成 web:web:<id>。
            return "web:" + device_id
        # 没带设备号的请求：如果这个地址上登记过一台 App，就用它的设备号 ——
        # 否则同一个手机在这一条请求上会写成"地址:空摘要"，聊天记录又多一段。
        try:
            for client in ui.known_clients():
                if str(client.get("address") or "") != address:
                    continue
                known_id = str(client.get("deviceId") or "")
                if known_id:
                    return "web:" + known_id
        except Exception:
            pass
        key = ui.client_key(address, agent)
        return "web:" + key

    def _self_client_key(self, ui) -> str:
        """This phone's registry key (``address|digest``), for addressed shares."""
        address = self.client_address[0] if self.client_address else ""
        agent = self.headers.get("User-Agent", "")
        # A native app registers under its device id, so its key is that one;
        # a browser is keyed by address + User-Agent.  Either way the reply must
        # name the same key the registry stored.
        for client in ui.known_clients():
            if bool(client.get("online")) and str(client.get("address") or "") == address:
                if str(client.get("kind") or "") == "app" or client.get("agent") == agent:
                    return str(client.get("key") or "")
        return ui.client_key(address, agent)

    def _hello(self) -> None:
        """``POST /api/hello``: a native app says who it is."""
        body = self._json_body()
        device_id = str(body.get("deviceId") or body.get("id") or "")
        name = str(body.get("name") or "")
        version = str(body.get("version") or "")
        address = self.client_address[0] if self.client_address else ""
        try:
            entry = self.server_ui.register_app(
                device_id,
                name=name,
                version=version,
                address=address,
                agent=self.headers.get("User-Agent", ""),
            )
        except ValueError as exc:
            raise _BadRequest(str(exc)) from None
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "device": {
                    "id": entry.get("deviceId", ""),
                    "name": entry.get("label", ""),
                    "address": entry.get("address", ""),
                },
            },
        )

    def _member_label_map(self, ui) -> dict[str, str]:
        """成员 id → 「名字（系统 · 韧传 版本 · IP）」。

        手机（``web:<key>``）在已配对列表里，电脑在对端列表里，本机用本机名。
        这段以前只有电脑端自己算，于是手机 App 的会话列表里显示的是一串
        ``d:c96ec2dd94`` —— 数据库主键。现在三个客户端都从服务端拿结果。
        """
        engine = ui.engine
        labels: dict[str, str] = {}
        for client in ui.known_clients():
            if not client.get("key"):
                continue
            label = client_label(client)
            # 会话里的成员 id 可能是 android:<id>、web:<id> 或 地址|UA摘要，
            # 都指同一台手机 —— 见 core.chat.client_aliases 的注释。
            # （``client_aliases()`` 现在也会把记录里的 ``aliases`` 一起展开，
            #   所以这一轮会把老键也写成"现在的名字"。）
            for alias in client_aliases(client):
                labels[alias] = label
            # 归并掉的旧身份（``aliases``）：那条记录已经不在了，会话里存的还是
            # 当时的写法（``web:127.0.0.1|d2445430``）。名字要用**当时**那个
            # （``aliasLabels``），那段 289 条消息的老会话才不会因为一次归并就
            # 改名。当前身份（key/deviceId）仍然用现在的名字。
            #
            # 必须**盖在**上面那一轮之后，而且用赋值不能用 ``setdefault``：
            # ``client_aliases()`` 已经把老键写进 labels 了，setdefault 永远
            # 不会生效 —— 老会话就会显示成这台设备现在的名字。
            for alias in ui._record_aliases(client):
                alias_label = ui._alias_label(client, alias)
                for name in client_aliases({"key": alias}):
                    labels[name] = alias_label
        for peer in engine.devices():
            info = peer.info
            parts = [str(info.platform or "未知系统")]
            if info.version:
                parts.append(f"韧传 {info.version}")
            if peer.address:
                parts.append(str(peer.address))
            labels[info.device_id] = f"{info.name}（{' · '.join(parts)}）"
        labels[engine.info.device_id] = f"{engine.info.name}（本机）"
        return labels

    def _conversation_labels(
        self, ui, conversation: dict[str, Any], viewer: str = ""
    ) -> dict[str, str]:
        """会话列表要显示的两个字段（名字、成员），都由服务端算好。

        ``viewer`` 是"谁在看这个列表"：电脑端看一对一，看到的是对方（手机）
        的名字；手机看同一个会话，该看到的是电脑的名字。以前两边都按电脑的
        视角算，于是手机上那条会话写着手机自己的名字。
        """
        labels = self._member_label_map(ui)
        self_id = viewer or ui.engine.info.device_id
        return {
            "displayTitle": conversation_title(conversation, labels, self_id),
            "displayPeople": conversation_people(conversation, labels, self_id),
        }

    def _send_chat(self, query: dict[str, list[str]]) -> None:
        """``GET /api/chat``: conversations plus one conversation's messages."""
        ui = self.server_ui
        store = ui.engine.chat
        conv_id = (query.get("conv") or [""])[0]
        try:
            limit = int((query.get("limit") or ["120"])[0] or 120)
        except ValueError:
            limit = 120
        before_raw = (query.get("before") or [""])[0]
        before = float(before_raw) if before_raw else None

        viewer = self._self_client_id(ui)
        conversations = [
            dict(item, **self._conversation_labels(ui, item, viewer))
            for item in store.conversations()
        ]
        if not conv_id and conversations:
            conv_id = conversations[0]["id"]
        messages = store.messages(conv_id, limit=limit, before=before) if conv_id else []
        if conv_id:
            store.mark_read(conv_id)
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "selfId": self._self_client_id(ui),
                "selfName": describe_agent(self.headers.get("User-Agent", "")),
                "conversations": conversations,
                "conversationId": conv_id,
                # ``mediaSource`` is where *this* computer keeps the file it
                # sent; a phone has no use for it and it describes the desktop's
                # folder layout, so it stays on the desktop side.
                #
                # 但手机需要知道"这条消息对应的分享条目 id"：手机 App 点气泡上的
                # 「保存」时要走 **FetchManager 下载队列**（才有进度/速度/ETA/
                # 「已保存」状态，PG-1/PG-2 的根因）；它没法从被抹掉的 mediaSource
                # 反推。这里把服务端已经算好的分享 id 作为不透明字段下发（id 本来
                # 就是手机下载时用的，不泄露任何路径）。
                "messages": [
                    self._message_for_phone(ui, message) for message in messages
                ],
            },
        )

    def _message_for_phone(self, ui, message: dict[str, Any]) -> dict[str, Any]:
        """一条发给手机 App 的聊天消息：抹掉本机路径，补上分享条目 id。

        手机点气泡上的「保存」时必须知道"这条消息对应哪条分享"，才能走统一的
        下载队列（FetchManager）拿到进度/速度/ETA/「已保存」；而分享 id 是服务端
        发布文件时生成的，手机侧无法自行推导。分享 id 本来就在手机下载 URL 里出现，
        下发它不泄露本机路径（``mediaSource`` 仍然不出去）。

        注意这里**无条件覆盖** ``shareId``：消息记录里那个 id 可能已经作废
        （分享表被清空/条目被顶掉），find_share_for_message 返回空时就要下发空，
        不能把记录里的旧值原样带出去 —— 否则手机会拿一个不存在的分享 id 去下载。
        """
        payload = {key: value for key, value in message.items() if key != "mediaSource"}
        if payload.get("mediaName"):
            payload["shareId"] = ui.find_share_for_message(message)
        return payload

    def _chat_message_payload(self, ui) -> tuple[str, str]:
        """``(conversation id, who is sending)`` for a chat POST."""
        body = self._json_body()
        conv_id = str(body.get("conv") or body.get("conversationId") or "").strip()
        return conv_id, self._self_client_id(ui)

    def _chat_send(self) -> None:
        """``POST /api/chat/send``: a message typed on the phone."""
        ui = self.server_ui
        engine = ui.engine
        body = self._json_body()
        conv_id = str(body.get("conv") or body.get("conversationId") or "").strip()
        to = str(body.get("to") or "").strip()
        text = str(body.get("text") or "")
        kind = str(body.get("kind") or "text")
        if kind not in ("text", "system"):
            raise _BadRequest("发附件请用 /api/chat/upload")
        if not text.strip():
            raise _BadRequest("消息是空的")
        if not conv_id:
            # A phone starting a 1:1 with this computer: the id both sides
            # compute is the sorted pair of member ids.
            conv_id = direct_conversation_id(
                engine.info.device_id, self._self_client_id(ui)
            )
        conv = engine.chat.conversation(conv_id) or {}
        members = list(body.get("members") or conv.get("members") or [])
        if not members:
            members = [engine.info.device_id, self._self_client_id(ui)]
        if to and to not in members:
            members.append(to)
        sender = self._self_client_id(ui)
        if sender not in members:
            members.append(sender)
        # ``accept_chat`` rather than ``send_chat``: from this computer's point
        # of view a message typed on the phone is *incoming* (it must show up as
        # received, and bump the unread badge), and it still has to reach every
        # other computer in the room -- hence the relay.
        result = engine.accept_chat(
            {
                "conv": {
                    "id": conv_id,
                    "kind": "group" if conv_id.startswith("g:") else "direct",
                    "title": str(body.get("title") or conv.get("title") or ""),
                    "members": members,
                },
                "msg": {
                    "sender": sender,
                    "senderName": self._self_client_name(ui),
                    "kind": kind,
                    "text": text,
                },
            },
            source="phone",
        )
        result["delivered"] = engine.relay_chat(conv_id, result)
        # 建群的人要能直接打开这个会话：把 id 明确回给它。
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "conversationId": conv_id, "members": members, **result},
        )

    def _chat_upload(self, query: dict[str, list[str]]) -> None:
        """``POST /api/chat/upload``: an attachment (or a voice note) from the phone."""
        ui = self.server_ui
        engine = ui.engine
        name = sanitize_component((query.get("name") or [""])[0])
        if not name:
            raise _BadRequest("缺少合法的文件名 (name)")
        conv_id = (query.get("conv") or [""])[0].strip()
        if not conv_id:
            conv_id = direct_conversation_id(engine.info.device_id, self._self_client_id(ui))
        kind = (query.get("kind") or ["file"])[0]
        if kind not in ("image", "video", "voice", "file"):
            kind = "file"
        try:
            duration_ms = int((query.get("duration") or ["0"])[0] or 0)
        except ValueError:
            duration_ms = 0

        rel = media_relpath(conv_id, name)
        target = os.path.join(ui.receive_dir, rel)
        declared = self._body_length()
        if declared is not None:
            available = free_space(ui.receive_dir)
            if available and declared > available:
                self._fail(
                    HTTPStatus.INSUFFICIENT_STORAGE,
                    f"接收目录剩余空间不足（需要 {declared} 字节，可用 {available} 字节）",
                )
                return
        os.makedirs(os.path.dirname(target), exist_ok=True)
        written = 0
        try:
            with open(target, "wb", buffering=0) as handle:
                for chunk in self._iter_body(ui.max_upload_bytes):
                    handle.write(chunk)
                    written += len(chunk)
        except BaseException:
            try:
                os.unlink(target)
            except OSError:
                pass
            raise

        # The message goes through the engine so it lands in exactly the same
        # store (and reaches the same peers) as one typed on the desktop.
        sender = self._self_client_id(ui)
        sender_name = self._self_client_name(ui)
        conv = engine.chat.conversation(conv_id) or {}
        members = list(conv.get("members") or [engine.info.device_id, sender])
        if sender not in members:
            members.append(sender)
        result = engine.accept_chat(
            {
                "conv": {
                    "id": conv_id,
                    "kind": "group" if conv_id.startswith("g:") else "direct",
                    "title": str(conv.get("title") or ""),
                    "members": members,
                },
                "msg": {
                    "sender": sender,
                    "senderName": sender_name,
                    "kind": kind,
                    "mediaName": os.path.basename(rel),
                    "mediaRel": rel,
                    "mediaSize": written,
                    "mediaMime": str(self.headers.get("Content-Type") or ""),
                    "durationMs": duration_ms,
                },
            },
            source="phone",
        )
        # …and then relayed to every *computer* in the room, which is what makes
        # a group with a phone and two laptops work.
        engine.relay_chat(conv_id, result)
        self._send_json(HTTPStatus.ACCEPTED, {"ok": True, **result})

    def _send_chat_media(self, message_id: str) -> None:
        """按消息 id 取附件（Range 可用）。

        **文件类消息的字节走的是分享通道**（电脑发布 → 手机来取）：真机上抓到过
        一条 `kind=file` 的消息 `media_rel=''`（桌面还没把相对路径写进来），而同一份
        文件**已经在分享表里**，于是 App 的「保存」永远 404 —— 用户看到的是"电脑
        说发了、我这儿没有"。

        现在的查找顺序（一个入口，不是两条各说各话的路）：

        1. 消息有 ``mediaRel`` → 照旧从接收目录流式返回（聊天附件一直是这条路）；
        2. 没有 ``mediaRel``（或它指向的文件不在了）但**分享表里有同一份文件**
           （按 ``mediaSource`` 绝对路径，退一步按 名字+大小 匹配）→ 交给
           :meth:`_send_share` 发：进度、Range 续传、完成回执都在那条路上，
           不会出现"另一个入口没有进度"；
        3. 都没有 → 404，但带 ``hint`` / ``shareId``，让 App 能说出下一步。
        """
        ui = self.server_ui
        message = ui.engine.chat.message(urllib.parse.unquote(message_id))
        if message is None:
            self._fail(
                HTTPStatus.NOT_FOUND,
                "没有这条消息：它可能已经被清理了。",
                extra={"hint": "刷新会话列表；如果记录还在，让电脑重新发一次。", "shareId": None},
            )
            return

        share_id = ui.find_share_for_message(message)
        if share_id:
            state, _path, name = ui.share_lookup(share_id)
            if state == "ok":
                self._send_share(share_id)
                return
            if state == "gone":
                self._fail(
                    HTTPStatus.NOT_FOUND,
                    "文件「" + (name or message.get("mediaName") or "这个文件") + "」在电脑上被删掉或移走了。",
                    extra={"hint": "让电脑重新发一次。", "shareId": share_id},
                )
                return

        relative = str(message.get("mediaRel") or "")
        if relative:
            path = os.path.join(ui.receive_dir, relative)
            real = os.path.realpath(path)
            root = os.path.realpath(ui.receive_dir)
            if (real == root or real.startswith(root + os.sep)) and os.path.isfile(real):
                self._stream_file(real)
                return

        # 走到这里：既没有可服务的相对路径，分享表里也没有它。
        self._fail(
            HTTPStatus.NOT_FOUND,
            "这条消息没有可下载的附件。",
            extra={
                "hint": "文件可能还在电脑上。让电脑重新发一次，或者在「传输 → 接收」里找它的分享条目。",
                "shareId": share_id or None,
                "name": str(message.get("mediaName") or ""),
            },
        )

    def _is_loopback_client(self) -> bool:
        """True when this request came from this very machine."""
        address = self.client_address[0] if self.client_address else ""
        return address in ("127.0.0.1", "::1", "localhost")

    def _share_files(self) -> None:
        """Publish local files for the connected phone to pick up.

        The desktop window does this in-process (``WebUI.share_files``); this
        route exists so a local script or a test can do the same.  It is
        **loopback only** on purpose: a device on the network must never be
        able to name a path on this machine and then download it.
        """
        if not self._is_loopback_client():
            self._fail(HTTPStatus.FORBIDDEN, "只有本机可以指定要交给手机的文件")
            return
        body = self._json_body()
        paths = body.get("paths")
        if paths is None and body.get("path"):
            paths = [body["path"]]
        if not isinstance(paths, list) or not paths:
            raise _BadRequest("需要 paths 数组")
        added = self.server_ui.share_files([str(p) for p in paths])
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "shares": [
                    {"id": a["id"], "name": a["name"], "size": a["size"]} for a in added
                ],
            },
        )

    def _respond_offer(self) -> None:
        body = self._json_body()
        request_id = str(body.get("requestId") or body.get("request_id") or "")
        if not request_id:
            raise _BadRequest("缺少 requestId")
        accept = bool(body.get("accept"))
        indices = body.get("indices")
        accepted: list[int] | None = None
        if indices is not None:
            if not isinstance(indices, list):
                raise _BadRequest("indices 必须是数组")
            try:
                accepted = [int(i) for i in indices]
            except (TypeError, ValueError):
                raise _BadRequest("indices 必须是整数数组") from None
        ok = self.server_ui.engine.resolve_offer(
            request_id, accept, accepted_indices=accepted, trust=bool(body.get("trust"))
        )
        with self.server_ui._state_lock:
            self.server_ui._offers.pop(request_id, None)
        self._send_json(HTTPStatus.OK if ok else HTTPStatus.GONE, {"ok": bool(ok), "requestId": request_id})

    def _cancel_transfer(self) -> None:
        body = self._json_body()
        transfer_id = str(body.get("transferId") or body.get("transfer_id") or "")
        if not transfer_id:
            raise _BadRequest("缺少 transferId")
        ok = self.server_ui.engine.cancel(transfer_id, str(body.get("reason") or "用户在手机上取消"))
        self._send_json(HTTPStatus.OK if ok else HTTPStatus.GONE, {"ok": bool(ok), "transferId": transfer_id})

    def _set_trust(self) -> None:
        body = self._json_body()
        device_id = str(body.get("deviceId") or body.get("device_id") or "")
        if not device_id:
            raise _BadRequest("缺少 deviceId")
        self.server_ui.engine.trust(device_id, bool(body.get("trusted", True)))
        self._send_json(HTTPStatus.OK, {"ok": True, "deviceId": device_id})

    def _add_peer(self) -> None:
        body = self._json_body()
        address = str(body.get("address") or "").strip()
        if not address:
            raise _BadRequest("缺少 address")
        try:
            port = int(body.get("port") or 0)
        except (TypeError, ValueError):
            raise _BadRequest("port 非法") from None
        peer = self.server_ui.engine.add_manual_device(address, port, str(body.get("name") or ""))
        self._send_json(HTTPStatus.OK, {"ok": True, "device": peer_dict(peer)})


def _range_unsatisfiable(header: str, size: int) -> bool:
    """这个 Range 头是不是"格式对、但要的那一段不存在"（该回 416 的那种）。

    HTTP 里两件事必须分开（以前都当成 416，下载管理器发个多段请求就拿到一个
    看不懂的错误）：

    * **不合法/用不上**（不是 bytes=、多段、写坏了）→ 忽略它，整个文件照发（200）；
    * **合法但要不到**（起点 ≥ 文件长度、后缀长度为 0）→ 416 + 中文说明 + Content-Range。
    """
    if not header.startswith("bytes=") or "," in header:
        return False
    spec = header[len("bytes=") :].strip()
    start_text, _, end_text = spec.partition("-")
    try:
        if not start_text:                      # 后缀范围：最后 N 字节
            return int(end_text) <= 0
        return int(start_text) >= size
    except ValueError:
        return False


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Parse a single-range ``Range`` header, or ``None`` if it cannot be used.

    ``None`` 的含义是"忽略这个头、按整个文件发"（多段请求也走这条路：手机一次
    只下一个文件，为它写一个完整的 multipart/byteranges 不值当）。真正"要不到"
    的情况由 :func:`_range_unsatisfiable` 判定，回 416。
    """
    if not header.startswith("bytes=") or "," in header:
        return None
    spec = header[len("bytes=") :].strip()
    start_text, _, end_text = spec.partition("-")
    try:
        if not start_text:  # suffix range: last N bytes
            length = int(end_text)
            if length <= 0:
                return None
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size:
        return None
    end = min(end, size - 1)
    if end < start:
        return None
    return start, end


_MISSING_ASSETS = (
    "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
    "<title>EverSend</title><body style=\"font-family:system-ui;padding:2rem\">"
    "<h1>韧传 Web 界面缺少资源文件</h1>"
    "<p>找不到 <code>eversend/web/assets/index.html</code>，请重新安装或检查安装目录。</p>"
    "</body></html>"
)


def serve(engine: Engine, port: int = DEFAULT_WEB_PORT, **kwargs: Any) -> WebUI:
    """Convenience helper: build a :class:`WebUI`, start it and return it."""
    ui = WebUI(engine, port=port, **kwargs)
    ui.start()
    return ui


__all__ = [
    "DEFAULT_MAX_UPLOAD",
    "MAX_JSON_BODY",
    "SSE_HEARTBEAT",
    "TOKEN_HEADER",
    "WebUI",
    "device_dict",
    "display_address",
    "event_dict",
    "local_addresses",
    "peer_dict",
    "serve",
    "transfer_dict",
]
