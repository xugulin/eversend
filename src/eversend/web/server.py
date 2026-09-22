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
import secrets
import socket
import threading
import time
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, ClassVar, Iterable, Iterator

from ..core.constants import APP_NAME, APP_NAME_CN, APP_VERSION, DEFAULT_WEB_PORT
from ..core.engine import Engine, build_file_entries, free_space
from ..core.model import DeviceInfo, Peer, new_transfer_id, sanitize_component, unique_path
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
}

#: Routes that only accept POST, so a GET can be answered with 405.
_POST_ONLY_ROUTES = frozenset(
    {"/api/announce", "/api/scan", "/api/upload", "/api/share", "/api/leave", "/api/offer/respond", "/api/cancel", "/api/trust", "/api/peer"}
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
    ) -> None:
        self.engine = engine
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
        #: Files the desktop handed to the browser, by share id.  A browser has
        #: no receiving service, so "send to phone" cannot be a push: the
        #: desktop publishes the file and the phone picks it up with one tap.
        #: Only paths that came from *this* process are ever served, so the
        #: network can never ask for an arbitrary file.
        self._shares: dict[str, dict[str, Any]] = {}
        self._workers: set[threading.Thread] = set()
        self._upload_lock = threading.Lock()
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

    # -- connected browsers ------------------------------------------------

    def touch_client(self, address: str, agent: str = "") -> None:
        """Remember that a browser at ``address`` just did something.

        Keyed by address **and** User-Agent.  Address alone is not enough: a
        phone reaching us through a port forward (``adb reverse``, a reverse
        proxy, an SSH tunnel) arrives from the same 127.0.0.1 as a local health
        check, and the two would collapse into one entry -- which is exactly
        how the Android CI check first "saw" a client that was really its own
        probe.  One phone in a normal LAN still gets exactly one entry.
        """
        if not address:
            return
        now = time.time()
        digest = hashlib.sha1((agent or "").encode("utf-8", "replace")).hexdigest()[:8]
        key = f"{address}|{digest}"
        with self._state_lock:
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

    def share_files(self, paths: Iterable[str]) -> list[dict[str, Any]]:
        """Publish local files for the connected browser to download.

        Called by the desktop window, in this process: the paths never travel
        over the network, so `/api/share/<id>` cannot be turned into a file
        read primitive.  The phone gets an opaque id.
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
            }
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
        return added

    def shares(self) -> list[dict[str, Any]]:
        """Shares as the *phone* may see them: no filesystem paths."""
        with self._state_lock:
            found = [
                {
                    "id": s["id"],
                    "name": s["name"],
                    "size": s["size"],
                    "added": s["added"],
                    "downloaded": bool(s.get("downloaded")),
                }
                for s in self._shares.values()
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

    def mark_share_downloaded(self, share_id: str) -> None:
        with self._state_lock:
            entry = self._shares.get(str(share_id))
            if entry is not None:
                entry["downloaded"] = True

    def clear_shares(self) -> None:
        with self._state_lock:
            self._shares.clear()

    def forget_client(self, address: str, agent: str = "") -> bool:
        """Drop one browser from the connected list right now.

        Called when the page says goodbye (the phone's 「断开连接」 button) so
        the desktop reflects it immediately instead of waiting for the TTL.
        """
        digest = hashlib.sha1((agent or "").encode("utf-8", "replace")).hexdigest()[:8]
        key = f"{address}|{digest}"
        with self._state_lock:
            return self._clients.pop(key, None) is not None

    def clients(self, ttl: float = CLIENT_TTL) -> list[dict[str, Any]]:
        """Browsers with the page open right now, newest activity first."""
        cutoff = time.time() - ttl
        with self._state_lock:
            for key in [k for k, c in self._clients.items() if c["lastSeen"] < cutoff]:
                del self._clients[key]
            found = [dict(c) for c in self._clients.values()]
        for client in found:
            client["isLocal"] = client["address"] in ("127.0.0.1", "::1")
            client["secondsAgo"] = round(max(0.0, time.time() - client["lastSeen"]), 1)
        found.sort(key=lambda c: c["lastSeen"], reverse=True)
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
        self._httpd = httpd
        self._bound_port = int(httpd.server_address[1])
        self.port = self._bound_port

        # Advertise the real port: the engine's configured web_port is only a
        # wish until the socket is bound, and a peer that reads the wrong port
        # from discovery shows a dead link.
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
        )
        out: list[dict[str, Any]] = [local]
        for peer in self.engine.devices():
            # A self-send makes the engine discover itself; never list it
            # twice, and never let it masquerade as a remote device.
            if peer.info.device_id == mine:
                continue
            entry = peer_dict(peer)
            entry["isSelf"] = False
            entry["local"] = False
            out.append(entry)
        # Remote devices with a reachable web UI first, then by name: the list
        # is a picker, and the wanted entry is almost always near the top.
        out[1:] = sorted(out[1:], key=lambda d: (not d.get("webUrl", ""), d.get("name", "").lower()))
        return out

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
            "app": {"name": APP_NAME, "nameCn": APP_NAME_CN, "version": APP_VERSION},
            "device": self._self_device(),
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
                upload["error"] = "对方拒绝或传输失败"
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
                try:
                    os.unlink(path)
                    # Leave no trace in the user's receive directory.
                    os.rmdir(os.path.dirname(path))
                except OSError:
                    pass
            with self._state_lock:
                self._uploads.pop(upload.get("id", ""), None)
                self._uploads_done.append(dict(upload))
                del self._uploads_done[:-50]

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
        if path == "/api/state":
            self._send_json(HTTPStatus.OK, ui.state())
            return
        if path == "/api/devices":
            self._send_json(HTTPStatus.OK, {"ok": True, "devices": ui._devices()})
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
        if path == "/api/download":
            self._send_download(query)
            return
        if path.startswith("/api/share/"):
            # What the desktop handed over for this phone, by opaque id.
            self._send_share(path[len("/api/share/") :])
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
            if path == "/api/leave":
                # The page is going away on purpose.  Drop it now; the desktop
                # would otherwise keep showing it for another 15 seconds.
                forgotten = self.server_ui.forget_client(
                    self.client_address[0] if self.client_address else "",
                    self.headers.get("User-Agent", ""),
                )
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

    def _fail(self, status: int, message: str) -> None:
        """Errors are JSON for the API and text for everything else.

        A failed request may still have an unread body; keeping the connection
        alive would make the next parse read those bytes as a request, so the
        connection is closed instead.
        """
        if self.command == "POST":
            self.close_connection = True
        body = json.dumps({"ok": False, "error": message}, ensure_ascii=False).encode("utf-8")
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
                "Content-Security-Policy": (
                    "default-src 'none'; img-src 'self' data:; "
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
        """Stream a file the desktop published for the browser.

        Marked as downloaded once the whole file has gone out, so the desktop
        can show "手机已取走" instead of guessing.
        """
        share_id = urllib.parse.unquote(share_id)
        path = self.server_ui.share_path(share_id)
        if path is None:
            self._fail(HTTPStatus.NOT_FOUND, "这个文件已经不在分享列表里了")
            return
        self._stream_file(path, on_complete=lambda: self.server_ui.mark_share_downloaded(share_id))

    def _send_download(self, query: dict[str, list[str]]) -> None:
        """Stream a received file, honouring a single-range ``Range`` header."""
        relative = (query.get("path") or [""])[0]
        path = self.server_ui.resolve_download(relative)
        if path is None:
            self._fail(HTTPStatus.NOT_FOUND, "文件不存在或不在接收目录内")
            return
        self._stream_file(path)

    def _stream_file(self, path: str, on_complete=None) -> None:
        """Send one local file, honouring a single-range ``Range`` header."""
        try:
            size = os.path.getsize(path)
        except OSError:
            self._fail(HTTPStatus.NOT_FOUND, "文件无法读取")
            return

        start, end = 0, size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range", "")
        if range_header:
            parsed = _parse_range(range_header, size)
            if parsed is None:
                self._send_bytes(
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                    b"",
                    "text/plain; charset=utf-8",
                    extra={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
                )
                return
            start, end = parsed
            status = HTTPStatus.PARTIAL_CONTENT

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
            while not ui._stop.is_set():
                try:
                    event = mailbox.get(timeout=1.0)
                except queue.Empty:
                    event = None
                if event is not None:
                    self._sse_write(
                        "data: " + json.dumps(event_dict(event), ensure_ascii=False, default=_jsonable) + "\n\n"
                    )
                    continue
                now = time.monotonic()
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
        try:
            port = int((query.get("port") or ["0"])[0] or 0)
        except ValueError:
            port = 0

        clean = sanitize_component(name)
        if not clean:
            raise _BadRequest("缺少合法的文件名 (name)")

        peer = ui._find_peer(device_id, address, port)
        if peer is None:
            self._fail(HTTPStatus.NOT_FOUND, "未找到目标设备，请让对方保持在线后重新扫描")
            return

        # Refuse before writing a byte if the disk cannot hold the file.
        declared = self._body_length()
        if declared is not None:
            available = free_space(ui.receive_dir)
            if available and declared > available:
                self._fail(
                    HTTPStatus.INSUFFICIENT_STORAGE,
                    f"接收目录剩余空间不足（需要 {declared} 字节，可用 {available} 字节）",
                )
                return

        spool = ui.spool_path(clean)
        written = 0
        try:
            with open(spool, "wb", buffering=0) as handle:
                for chunk in self._iter_body(ui.max_upload_bytes):
                    handle.write(chunk)
                    written += len(chunk)
            if written == 0 and declared != 0:
                raise _BadRequest("请求体为空")
            final = ui.finish_upload(spool)
        except BaseException:
            # Any failure -- a sleeping phone, a disk error, a refused body --
            # must not leave a spool file behind.
            try:
                os.unlink(spool)
            except OSError:
                pass
            raise

        upload = {
            "id": uuid.uuid4().hex,
            "name": os.path.basename(final),
            "size": written,
            "deviceId": peer.info.device_id,
            "deviceName": peer.info.name,
            "status": "queued",
            "error": "",
            "started": time.time(),
            "finished": 0.0,
        }
        ui.register_upload(upload)
        transfer_id = ui.start_send(peer, final, pin, upload)
        self._send_json(
            HTTPStatus.ACCEPTED,
            {
                "ok": True,
                "uploadId": upload["id"],
                "transferId": transfer_id,
                "name": upload["name"],
                "size": written,
                "device": device_dict(peer.info),
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


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Parse a single-range ``Range`` header, or ``None`` if unsatisfiable.

    Multi-range requests are answered with the whole file instead: the phone
    downloads a single file at a time and a correct multipart/byteranges reply
    is a lot of code for a case that never happens.
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
