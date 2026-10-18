#!/usr/bin/env python3
"""Self-test for the EverSend web front end.

Everything here runs against **real** engines and a real HTTP server: two
:class:`~eversend.core.engine.Engine` instances talking over loopback, and
:class:`~eversend.web.server.WebUI` answering ``urllib`` requests.  Nothing is
mocked, because the point of the test is to prove the phone -> web server ->
engine -> peer path actually moves bytes.

Run from the repository root::

    PYTHONPATH=src python3 -u src/eversend/web/selftest.py

Exits non-zero if any check fails.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

if __package__ in (None, ""):  # allow "python3 src/eversend/web/selftest.py"
    _SRC = Path(__file__).resolve().parents[2]
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))

from eversend.core.chat import (  # noqa: E402
    conversation_title,
    direct_conversation_id,
    new_group_id,
)
from eversend.core.engine import Engine, EngineConfig  # noqa: E402
from eversend.core.model import disambiguate_names  # noqa: E402
from eversend.web import create_web_ui  # noqa: E402
from eversend.web import qr  # noqa: E402
from eversend.web.server import CLIENT_TTL, TOKEN_HEADER, display_address  # noqa: E402

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"
_failures: list[str] = []
_checks = 0


def _use_utf8_console() -> None:
    """Survive a legacy Windows code page.

    This script prints Chinese check names.  On a zh-CN Windows console the
    default code page is 936 and on an en-US one it is 1252; either way a
    character it cannot represent raises UnicodeEncodeError and kills the run
    partway through, which is a miserable way to learn nothing.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def check(name: str, condition: bool, detail: str = "") -> bool:
    global _checks
    _checks += 1
    if condition:
        print(f"  {GREEN}PASS{RESET} {name}")
    else:
        print(f"  {RED}FAIL{RESET} {name}" + (f"  [{detail}]" if detail else ""))
        _failures.append(name)
    return bool(condition)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def receive_dir(engine: Engine) -> str:
    """Where ``engine`` writes received files.

    The core keeps this on the config only (there is no ``Engine.receive_dir``
    property), so accept either shape.
    """
    return str(getattr(engine, "receive_dir", None) or engine.config.receive_dir)


def http(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> tuple[int, dict[str, str], bytes]:
    """Perform one request and return ``(status, headers, body)``.

    HTTP errors are returned rather than raised: a 403 is a result here, and
    the test has to look at it.
    """
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()


def make_engine(root: Path, name: str) -> Engine:
    engine = Engine(
        EngineConfig(
            data_dir=str(root / name / "state"),
            receive_dir=str(root / name / "recv"),
            name=name,
            tcp_port=0,
            discovery_port=free_port(),
            enable_broadcast=False,
            enable_mdns=False,
            enable_web=False,
            auto_accept_all=True,
        )
    )
    engine.start()
    return engine


def wait_for(predicate, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def check_qr_module() -> None:
    """The QR encoder's tables and geometry, independent of any server."""
    print("\n[0] QR encoder internals")
    geometry_bad = [
        version
        for version in range(1, 41)
        if qr.total_codewords(version) != qr.symbol_capacity_codewords(version)
        or qr.free_module_count(version) != qr.total_codewords(version) * 8 + qr._REMAINDER_BITS[version]
    ]
    check("block tables agree with the module geometry (versions 1-40)", not geometry_bad, str(geometry_bad))

    matrix, version, level, mask = qr.encode("http://192.168.1.100:52119/", ec_level="M")
    check("a LAN URL fits in a small symbol", version <= 6, f"version={version}")
    check("size matches the version", len(matrix) == version * 4 + 17, f"{len(matrix)}")
    check("error correction level is M", level == "M", level)
    check("a mask was selected", 0 <= mask <= 7, str(mask))

    # Finder patterns: the three corners must be the standard 7x7 bullseye
    # with a light separator ring.
    size = len(matrix)
    corners = ((0, 0), (0, size - 7), (size - 7, 0))
    ok = True
    for top, left in corners:
        for row in range(-1, 8):
            for col in range(-1, 8):
                r, c = top + row, left + col
                if not (0 <= r < size and 0 <= c < size):
                    continue
                if row in (-1, 7) or col in (-1, 7):
                    expected = False
                else:
                    expected = max(abs(row - 3), abs(col - 3)) != 2
                if matrix[r][c] != expected:
                    ok = False
    check("all three finder patterns are intact", ok)

    svg = qr.svg("http://192.168.1.100:52119/")
    check("SVG contains an <svg> root", "<svg" in svg and svg.rstrip().endswith("</svg>"))
    rects = svg.count("<rect")
    check("SVG has a plausible number of <rect> modules", 100 <= rects <= 6000, f"{rects} rects")

    png = qr.png("http://192.168.1.100:52119/", scale=3)
    check("PNG is a valid PNG container", png.startswith(b"\x89PNG\r\n\x1a\n") and png.endswith(b"IEND\xaeB`\x82"))


def check_qr_decodes(url: str) -> None:
    """Prove the generated symbol is a *real* QR code by decoding it again.

    zbar is an independent implementation; if it reads the URL back out, the
    module matrix, the error correction and the format information are all
    correct.  Skipped (not failed) when the tool is not installed, so the
    self-test still runs on a bare machine.
    """
    print("\n[0b] QR decoded by an independent tool (optional)")
    import shutil as _shutil
    import subprocess

    zbar = _shutil.which("zbarimg")
    if not zbar:
        print("  SKIP  zbarimg is not installed")
        return
    with tempfile.TemporaryDirectory() as tmp:
        # The PNG endpoint is the cleanest target: the same encoder produces
        # it, and it needs no rasteriser to be fed to the decoder.
        png_path = os.path.join(tmp, "qr.png")
        with open(png_path, "wb") as handle:
            handle.write(qr.png(url, scale=4))
        decoded = subprocess.run([zbar, "-q", "--raw", png_path], capture_output=True, text=True)
        check(
            "zbarimg decodes the generated QR back to the exact URL",
            decoded.returncode == 0 and decoded.stdout.strip() == url,
            repr(decoded.stdout.strip()[:80]),
        )

        render = _shutil.which("rsvg-convert")
        if not render:
            print("  SKIP  rsvg-convert is not installed; SVG not rasterised")
            return
        svg_path = os.path.join(tmp, "qr.svg")
        svg_png = os.path.join(tmp, "qr-svg.png")
        with open(svg_path, "w", encoding="utf-8") as handle:
            handle.write(qr.svg(url, scale=8))
        converted = subprocess.run([render, "-o", svg_png, svg_path], capture_output=True, text=True)
        if converted.returncode != 0:
            print("  SKIP  rsvg-convert failed: " + converted.stderr.strip()[:100])
            return
        decoded = subprocess.run([zbar, "-q", "--raw", svg_png], capture_output=True, text=True)
        check(
            "zbarimg decodes the SVG rendering too",
            decoded.returncode == 0 and decoded.stdout.strip() == url,
            repr(decoded.stdout.strip()[:80]),
        )


def _asset_text(ui, name: str) -> str:
    """Read one of the page's assets as text (empty when it is missing)."""
    try:
        with open(os.path.join(ui.asset_dir, name), encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def check_http_surface(base: str, token: str, ui=None) -> None:
    print("\n[1] HTTP surface")
    status, headers, body = http(base + "/api/state")
    check("/api/state returns 200", status == 200, str(status))
    check("/api/state is JSON", "application/json" in headers.get("Content-Type", ""), headers.get("Content-Type", ""))
    state = {}
    try:
        state = json.loads(body.decode("utf-8"))
    except ValueError as exc:
        check("/api/state parses", False, str(exc))
    if state:
        check("/api/state parses", True)
    check("state carries the device block", isinstance(state.get("device"), dict) and "id" in state["device"])
    check("state carries the device list", isinstance(state.get("devices"), list))
    check("state carries transfers", isinstance(state.get("transfers"), list))
    check("state reports receiveDir", isinstance(state.get("receiveDir"), str) and bool(state["receiveDir"]))
    check("state reports freeSpace", isinstance(state.get("freeSpace"), int) and state["freeSpace"] >= 0)

    # Who is connected has to be visible: a phone is a browser client and never
    # shows up in the peer list, so this list is the desktop's only way to say
    # "your phone is talking to me".
    clients = state.get("webClients")
    check("state reports connected browsers", isinstance(clients, list) and bool(clients), str(clients)[:120])
    if clients:
        me = clients[0]
        check("the client carries an address and a label",
              bool(me.get("address")) and bool(me.get("label")), str(me)[:120])
        check("a loopback client is flagged as local", me.get("isLocal") is True, str(me)[:120])
        check("the label names the browser family",
              any(
                  word in str(me.get("label"))
                  for word in ("浏览器", "Chrome", "Firefox", "Safari", "Edge", "命令行", "未知")
              ),
              str(me.get("label")))
        # The self-test talks HTTP with urllib, which must be recognised as a
        # script rather than announced to the user as a mystery browser.
        check("a script is labelled as one, not as a browser",
              "命令行" in str(me.get("label")), str(me.get("label")))

    status, _headers, body = http(base + "/api/devices")
    devices = json.loads(body.decode("utf-8")) if status == 200 else {}
    check("/api/devices returns 200 + a list", status == 200 and isinstance(devices.get("devices"), list))

    status, headers, body = http(base + "/")
    html = body.decode("utf-8", "replace")
    check("/ serves the app shell", status == 200 and "text/html" in headers.get("Content-Type", ""), str(status))
    check("the shell mentions 韧传", "韧传" in html)
    check("the shell declares a mobile viewport", "width=device-width" in html and "viewport-fit=cover" in html)
    # The phone page must describe both directions for what they are.  It used
    # to call the computer's folder 「已接收的文件」, which on a phone reads as
    # "files I received" -- so a user who had just downloaded a file saw an
    # empty list and concluded the download had failed.
    check("the phone page offers a disconnect button", "btn-disconnect" in html)
    check("the phone page can keep the link alive with the screen off",
          "keepalive" in html and "熄屏" in html)
    check("the page explains who owns the computer's file list",
          "这些文件在电脑上" in _asset_text(ui, "app.js"), "app.js")
    # Two functions with the same name in one scope: the last one silently wins,
    # and the first one's callers start throwing.  That is exactly how a chat
    # helper named ``append`` broke the send tab's rendering -- and no Python
    # test could see it.  A duplicate definition is always a mistake here.
    script = _asset_text(ui, "app.js")
    defined = re.findall(r"^\s*function\s+([A-Za-z_$][\w$]*)\s*\(", script, re.M)
    duplicates = sorted({name for name in defined if defined.count(name) > 1})
    check("the page script has no duplicate function definitions", not duplicates, str(duplicates))
    # HTTP 头部只收 ISO-8859-1。设备名是「电脑浏览器」「安卓手机」这种中文时，
    # fetch() 会直接抛 "String contains non ISO-8859-1 code point" —— 一行头部
    # 就让整页报废（用户看到的只有「无法连接服务器」）。这个 bug 在 Python 测试
    # 里完全看不见：它发生在浏览器里。所以这里盯住"名字先编码再进头部"。
    name_headers = re.findall(r"X-EverSend-Name'\s*,\s*([^)]*)\)", script)
    check(
        "网页版把设备名编码后才放进请求头（头部只收 ISO-8859-1）",
        bool(name_headers) and all("encodeHeader(" in item for item in name_headers),
        str(name_headers),
    )
    check("the phone page shows what the computer sent it", "电脑发来的文件" in html)
    check("the phone page calls the computer's folder the computer's", "电脑上的文件" in html)
    check("the CSRF token is embedded in the page", token in html)
    check("the page is never cached (it carries a token)", "no-store" in headers.get("Cache-Control", ""))

    # 手机上报的名字是 URL 编码过的（中文名），服务端不能因此报错。
    status, headers, body = http(
        base + "/api/state",
        headers={
            # 用一个自己的 User-Agent：下面的「断开连接」用例按 UA 找自己那条
            # 记录，多出一个 urllib 记录会让它误判。
            "User-Agent": "EverSend-Selftest-Zh/1.0",
            "X-EverSend-Device": "web:selftest-zh",
            "X-EverSend-Name": "%E7%94%B5%E8%84%91",
        },
    )
    check("带百分号编码的设备名不会让接口出错", status == 200, f"{status} {body[:80]!r}")

    status, headers, body = http(base + "/assets/app.js")
    check("/assets/app.js is served as JavaScript", status == 200 and "javascript" in headers.get("Content-Type", ""))
    etag = headers.get("ETag", "")
    check("static assets carry an ETag", bool(etag), etag)
    status, headers, body = http(base + "/assets/app.js", headers={"If-None-Match": etag})
    check("a matching If-None-Match gets a 304", status == 304 and body == b"", f"{status} {len(body)}")

    status, headers, body = http(base + "/api/qr.svg")
    svg = body.decode("utf-8", "replace")
    check("/api/qr.svg returns 200 SVG", status == 200 and "image/svg+xml" in headers.get("Content-Type", ""), str(status))
    check("the SVG contains <svg", "<svg" in svg)
    check("the SVG contains many <rect> modules", svg.count("<rect") >= 100, f"{svg.count('<rect')} rects")
    served_url = headers.get("X-EverSend-URL", "")
    check("the SVG encodes the URL this server is reachable at", served_url.startswith("http://"))
    check_qr_decodes(served_url or "http://192.168.1.100:52119/")

    status, headers, body = http(base + "/api/qr.png")
    check("/api/qr.png returns a PNG", status == 200 and body.startswith(b"\x89PNG"), str(status))

    status, _headers, _body = http(base + "/api/nope")
    check("unknown API routes 404", status == 404, str(status))


def check_security(base: str, token: str) -> None:
    print("\n[2] Security")
    status, _headers, body = http(
        base + "/api/cancel", method="POST", data=b'{"transferId":"x"}', headers={"Content-Type": "application/json"}
    )
    check("POST without a token is refused (403)", status == 403, f"{status} {body[:80]!r}")
    status, _headers, _body = http(
        base + "/api/cancel",
        method="POST",
        data=b'{"transferId":"x"}',
        headers={"Content-Type": "application/json", TOKEN_HEADER: "not-the-token"},
    )
    check("POST with a wrong token is refused (403)", status == 403, str(status))
    status, _headers, _body = http(
        base + "/api/cancel",
        method="POST",
        data=b'{"transferId":"x"}',
        headers={"Content-Type": "application/json", TOKEN_HEADER: token},
    )
    check("POST with the right token is accepted", status in (200, 404, 410), str(status))
    status, _headers, _body = http(
        base + "/api/cancel",
        method="POST",
        data=json.dumps({"transferId": "x", "token": token}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    check("a token in the JSON body is accepted too", status in (200, 404, 410), str(status))
    status, headers, _body = http(base + "/api/events", method="HEAD")
    check("HEAD on the event stream is refused instead of hanging", status == 405, str(status))
    status, headers, _body = http(base + "/api/upload")
    check("GET on a POST-only route returns 405", status == 405, str(status))
    check("405 advertises the allowed methods", "POST" in headers.get("Allow", ""), headers.get("Allow", ""))

    marker = "class Engine"  # a string that only exists in core/engine.py
    for path in (
        "/assets/../../core/engine.py",
        "/assets/..%2f..%2fcore%2fengine.py",
        "/assets/%2e%2e/%2e%2e/core/engine.py",
        "/assets/....//....//core/engine.py",
    ):
        status, _headers, body = http(base + path)
        text = body.decode("utf-8", "replace")
        check(f"traversal {path} is refused", marker not in text and status != 200, f"status={status}")

    for path in (
        "/api/download?path=../../etc/passwd",
        "/api/download?path=..%2f..%2fetc%2fpasswd",
        "/api/download?path=/etc/passwd",
        "/api/download?path=....//....//etc/passwd",
        "/api/download?path=sub%00file",
    ):
        status, _headers, body = http(base + path)
        text = body.decode("utf-8", "replace")
        check(
            f"download {path} is refused",
            "root:" not in text and status != 200,
            f"status={status}",
        )

    status, _headers, _body = http(base + "/api/state", headers={"Host": "evil.example.com"})
    check("a DNS name in Host is refused (rebinding)", status == 403, str(status))
    status, _headers, _body = http(
        base + "/api/state", headers={"Host": "127.0.0.1:1"}
    )
    check("an IP literal in Host is accepted", status == 200, str(status))

    # An oversized JSON body must be rejected without being buffered.
    big = b'{"transferId":"' + b"a" * (200 * 1024) + b'"}'
    status, _headers, _body = http(
        base + "/api/cancel",
        method="POST",
        data=big,
        headers={"Content-Type": "application/json", TOKEN_HEADER: token},
    )
    check("an oversized body is rejected (413)", status == 413, str(status))

    # A hostile uploader must not be able to write outside the receive dir.
    status, _headers, body = http(
        base + "/api/upload?name=" + urllib.parse.quote("../../evil.txt") + "&deviceId=nope",
        method="POST",
        data=b"x",
        headers={TOKEN_HEADER: token},
    )
    check("an upload to an unknown device is refused", status == 404, f"{status} {body[:60]!r}")


def check_keepalive(port: int, token: str) -> None:
    """One connection, several requests: no state may leak between them.

    ``BaseHTTPRequestHandler`` instances live for the whole connection under
    HTTP/1.1, so the per-request flags (HEAD, parsed body) have to be reset
    per request.  A phone keeping one connection alive across a HEAD and a GET
    would otherwise get an empty body.
    """
    print("\n[2b] Keep-alive connection reuse")
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request("HEAD", "/api/state")
        head = connection.getresponse()
        head.read()
        check("HEAD /api/state answers 200", head.status == 200, str(head.status))

        connection.request("GET", "/api/state")
        get = connection.getresponse()
        body = get.read()
        check("the next GET on the same connection still has a body", len(body) > 100, f"{len(body)} bytes")
        check("...and it is the state document", b'"device"' in body)

        connection.request(
            "POST",
            "/api/cancel",
            body=json.dumps({"transferId": "first", "token": token}),
            headers={"Content-Type": "application/json"},
        )
        first = connection.getresponse()
        first.read()

        connection.request(
            "POST",
            "/api/cancel",
            body=json.dumps({"transferId": "second"}),
            headers={"Content-Type": "application/json", TOKEN_HEADER: token},
        )
        second = connection.getresponse()
        payload = json.loads(second.read().decode("utf-8"))
        check(
            "a body token is not reused for the next request",
            payload.get("transferId") == "second",
            str(payload)[:100],
        )
    finally:
        connection.close()


def check_download_range(base: str, engine: Engine) -> None:
    print("\n[3] Download + Range")
    payload = bytes(range(256)) * 40  # 10240 bytes, position-dependent
    target = os.path.join(receive_dir(engine), "range-test.bin")
    with open(target, "wb") as handle:
        handle.write(payload)

    url = base + "/api/download?path=" + urllib.parse.quote("range-test.bin")
    status, headers, body = http(url)
    check("full download returns 200", status == 200 and body == payload, str(status))
    check("download advertises Accept-Ranges", headers.get("Accept-Ranges") == "bytes")
    check("download sets a filename", "attachment" in headers.get("Content-Disposition", ""))

    status, headers, body = http(url, headers={"Range": "bytes=100-199"})
    check("range request returns 206", status == 206, str(status))
    check("range bytes are correct", body == payload[100:200], f"{len(body)} bytes")
    check("Content-Range is right", headers.get("Content-Range") == f"bytes 100-199/{len(payload)}")

    status, headers, body = http(url, headers={"Range": "bytes=-16"})
    check("suffix range returns the tail", status == 206 and body == payload[-16:], str(status))

    status, headers, _body = http(url, headers={"Range": "bytes=999999-"})
    check("an unsatisfiable range returns 416", status == 416, str(status))

    status, _headers, body = http(base + "/api/files")
    listing = json.loads(body.decode("utf-8"))
    names = [entry["name"] for entry in listing.get("files", [])]
    check("/api/files lists the received file", "range-test.bin" in names, str(names[:5]))
    check(
        "/api/files hides internal spool files",
        not any(".eversend." in name for name in names),
        str(names[:5]),
    )

    # Internal state must not be downloadable, in any directory.
    os.makedirs(os.path.join(receive_dir(engine), ".eversend-uploads"), exist_ok=True)
    hidden = os.path.join(receive_dir(engine), ".eversend-uploads", "secret.bin")
    with open(hidden, "wb") as handle:
        handle.write(b"internal spool data")
    status, _headers, body = http(base + "/api/download?path=" + urllib.parse.quote(".eversend-uploads/secret.bin"))
    check(
        "a file inside the upload spool is not downloadable",
        status != 200 and b"internal spool" not in body,
        str(status),
    )
    check(
        "an IPv4-mapped address is displayed as IPv4",
        display_address("::ffff:192.168.1.50") == "192.168.1.50"
        and display_address("192.168.1.50") == "192.168.1.50"
        and display_address("fe80::1") == "fe80::1",
    )
    # Leave the receive directory exactly as it was found.
    os.unlink(hidden)
    os.rmdir(os.path.dirname(hidden))
    os.unlink(target)


def check_share_handoff(ui, base: str, root: Path) -> None:
    """The "send to phone" direction: a browser can only pull, never be pushed.

    The desktop publishes local files (paths never leave the process) and the
    phone downloads them by opaque id.  This is the whole answer to "the
    computer found my phone but cannot send to it".
    """
    print("\n[3b] Hand-off to the browser (send to phone)")
    payload = bytes(range(256)) * 64  # 16 KiB, position-dependent
    source = root / "handoff-source.bin"
    source.write_bytes(payload)

    added = ui.share_files([str(source)])
    check("sharing a local file succeeds", len(added) == 1, str(added)[:100])
    if not added:
        return
    share_id = added[0]["id"]

    status, _headers, body = http(base + "/api/state")
    shares = json.loads(body.decode("utf-8")).get("shares", [])
    check("the share shows up in /api/state", any(s["id"] == share_id for s in shares), str(shares)[:120])
    check("the share does not leak the local path",
          "handoff-source" in json.dumps(shares) and str(root) not in json.dumps(shares),
          json.dumps(shares)[:160])

    status, headers, body = http(base + "/api/share/" + share_id)
    check("the phone can download the shared file", status == 200 and body == payload, str(status))
    check("the download is an attachment with the real name",
          "attachment" in headers.get("Content-Disposition", "")
          and "handoff-source.bin" in headers.get("Content-Disposition", ""),
          headers.get("Content-Disposition", ""))

    status, headers, body = http(base + "/api/share/" + share_id, headers={"Range": "bytes=10-19"})
    check("a resumed (ranged) download works too",
          status == 206 and body == payload[10:20], f"{status} {len(body)}")

    status, _headers, _body = http(base + "/api/share/does-not-exist")
    check("an unknown share id is a 404", status == 404, str(status))

    shares = {s["id"]: s for s in ui.shares()}
    check("the desktop learns that the phone took the file",
          shares.get(share_id, {}).get("downloaded") is True, str(shares.get(share_id)))

    # A file that does not exist must never be published.
    check("a missing file is not shared",
          ui.share_files([str(root / "nope.bin")]) == [])

    # A phone is a *paired device*, not a session: its page freezes the moment
    # the screen goes off, and the desktop must keep listing it.  Only an
    # explicit goodbye removes it.
    # 一台"手机"（用另一个地址 + 安卓 UA 模拟），这样不会干扰后面针对本机
    # 客户端的检查。
    phone_agent = (
        "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.7680.178 Mobile Safari/537.36"
    )
    ui.touch_client("10.9.9.9", phone_agent)
    check(
        "a phone that is talking right now is online",
        any(c.get("online") for c in ui.known_clients() if c["address"] == "10.9.9.9"),
    )
    # Now it "goes to sleep": the live record expires (the normal TTL rule), but
    # the phone must still be remembered -- that is the whole point of pairing
    # it instead of treating every page as a session.
    with ui._state_lock:
        ui._clients.pop(ui.client_key("10.9.9.9", phone_agent), None)
    remembered = [c for c in ui.known_clients() if c["address"] == "10.9.9.9"]
    check("a phone that went quiet is still remembered", bool(remembered), str(remembered)[:120])
    if remembered:
        entry = remembered[0]
        check("but it is no longer marked online",
              entry.get("online") is False, str(entry)[:100])
        check("the remembered phone carries address, label and a key",
              bool(entry.get("address")) and bool(entry.get("label")) and bool(entry.get("key")),
              str(entry)[:120])
        check("state publishes the remembered phones too",
              bool(json.loads(http(base + "/api/state")[2].decode("utf-8")).get("knownClients")))
        check("removing a remembered phone works", ui.remove_client(entry["key"]) is True)
        check("and it is really gone", not any(
            c.get("key") == entry["key"] for c in ui.known_clients()))

    check_conversation_names(ui, base)
    check_app_registration(ui)

    # The phone's 「断开连接」 button: the desktop must stop showing it at once.
    # Only *this* client may disappear -- the raw-socket probes above have no
    # User-Agent and are therefore a different entry.
    ours = {c["agent"] for c in ui.clients() if "urllib" in c.get("agent", "")}
    status, _headers, body = http(
        base + "/api/leave",
        method="POST",
        data=b"{}",
        headers={"Content-Type": "application/json", TOKEN_HEADER: ui.token},
    )
    check("a browser can say goodbye", status == 200, f"{status} {body[:60]!r}")
    remaining = {c["agent"] for c in ui.clients()}
    check("saying goodbye removes that browser from the list",
          bool(ours) and not (ours & remaining), f"ours={ours} remaining={remaining}")

    # And the route a script would use: loopback only, CSRF-checked.
    status, _headers, body = http(
        base + "/api/share",
        method="POST",
        data=json.dumps({"paths": [str(source)]}).encode("utf-8"),
        headers={TOKEN_HEADER: ui.token, "Content-Type": "application/json"},
    )
    payload_json = json.loads(body.decode("utf-8")) if body else {}
    check("a local script can publish a file over the API",
          status == 200 and payload_json.get("shares"), f"{status} {body[:80]!r}")
    status, _headers, _body = http(
        base + "/api/share",
        method="POST",
        data=json.dumps({"paths": [str(source)]}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    check("publishing without the token is refused", status == 403, str(status))


def check_scan_reports_back(engine: Engine) -> None:
    """A scan has to finish *visibly*.

    It used to leave the status line saying 「正在扫描局域网，稍等几秒…」 for the
    rest of the session: nothing ever reported that the scan was over.
    """
    print("\n[1b] A scan reports when it is done")
    queue_ = engine.events.subscribe()
    engine.scan()
    deadline = time.monotonic() + 90
    seen = None
    while time.monotonic() < deadline:
        try:
            event = queue_.get(timeout=1.0)
        except Exception:
            continue
        if event.get("kind") == "scan_finished":
            seen = event
            break
    check("the scan emits a completion event", seen is not None, "no scan_finished event")
    if seen is not None:
        check("the event says how many devices answered",
              isinstance(seen.get("found"), int), str(seen))
        check("the event carries no error", not seen.get("error"), str(seen.get("error")))


def check_chat(ui, base: str, token: str, root: Path) -> None:
    """The phone side of chat: send text, upload an attachment, read it back."""
    print("\n[3c] Chat from the browser")
    headers = {TOKEN_HEADER: token, "Content-Type": "application/json"}

    status, _headers, body = http(
        base + "/api/chat/send",
        method="POST",
        data=json.dumps({"text": "你好 👋 表情也能发"}).encode("utf-8"),
        headers=headers,
    )
    sent = json.loads(body.decode("utf-8")) if body else {}
    check("a phone can send a chat line", status == 200 and sent.get("message"), f"{status} {body[:80]!r}")
    check("the line is stored as incoming on this computer",
          (sent.get("message") or {}).get("direction") == "in", str(sent.get("message"))[:100])
    check("the emoji survived the round trip",
          "👋" in str((sent.get("message") or {}).get("text", "")),
          str((sent.get("message") or {}).get("text")))

    voice = b"\x1a\x45\xdf\xa3" + bytes(range(256)) * 8
    status, _headers, body = http(
        base + "/api/chat/upload?name=voice.webm&kind=voice&duration=2400",
        method="POST",
        data=voice,
        headers={TOKEN_HEADER: token, "Content-Type": "audio/webm"},
    )
    uploaded = json.loads(body.decode("utf-8")) if body else {}
    message = uploaded.get("message") or {}
    check("a phone can upload a voice note", status == 202 and message, f"{status} {body[:80]!r}")
    check("the voice note knows how long it is", message.get("durationMs") == 2400, str(message)[:120])
    check("the attachment lands inside the receive directory",
          os.path.isfile(os.path.join(receive_dir(ui.engine), str(message.get("mediaRel") or ""))),
          str(message.get("mediaRel")))

    status, _headers, body = http(base + "/api/chat/media/" + str(message.get("id") or ""))
    check("the attachment can be fetched back byte for byte", status == 200 and body == voice,
          f"{status} {len(body)} 字节")

    status, _headers, body = http(base + "/api/chat")
    chat = json.loads(body.decode("utf-8")) if body else {}
    kinds = [m["kind"] for m in chat.get("messages", [])]
    check("the conversation lists both messages", "text" in kinds and "voice" in kinds, str(kinds))
    check("the browser is told which member id is itself",
          str(chat.get("selfId", "")).startswith("web:"), str(chat.get("selfId")))

    status, _headers, body = http(base + "/api/state")
    summary = (json.loads(body.decode("utf-8")) or {}).get("chat") or {}
    check("state carries the chat summary", bool(summary.get("conversations")), str(summary)[:100])

    # A message with an unknown conversation id is created on the fly, and one
    # addressed to nobody in particular must not crash the server.
    status, _headers, _body = http(
        base + "/api/chat/send",
        method="POST",
        data=json.dumps({"conv": "d:nobody|nobody2", "text": "hi"}).encode("utf-8"),
        headers=headers,
    )
    check("a chat with an unknown peer is still stored", status == 200, str(status))


def check_sse(base: str) -> None:
    print("\n[4] Server-Sent Events")
    request = urllib.request.Request(base + "/api/events", headers={"Accept": "text/event-stream"})
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except Exception as exc:  # pragma: no cover - only on a broken build
        check("/api/events opens", False, repr(exc))
        return
    try:
        check("/api/events opens", True)
        check(
            "stream content type is text/event-stream",
            "text/event-stream" in response.headers.get("Content-Type", ""),
            response.headers.get("Content-Type", ""),
        )
        check("proxies are told not to buffer", response.headers.get("X-Accel-Buffering") == "no")
        deadline = time.monotonic() + 8
        saw_hello = False
        while time.monotonic() < deadline and not saw_hello:
            line = response.readline()
            if not line:
                break
            if line.startswith(b"event: hello"):
                saw_hello = True
        check("a hello frame arrives immediately", saw_hello)
    finally:
        response.close()


def spool_files(engine: Engine) -> list[str]:
    """Files sitting in the hidden upload working directory."""
    directory = os.path.join(receive_dir(engine), ".eversend-uploads")
    try:
        return sorted(os.listdir(directory))
    except OSError:
        return []


def check_upload(engine_a: Engine, engine_b: Engine, base: str, token: str, root: Path) -> None:
    print("\n[5] Upload: phone -> web server -> engine -> peer")
    # Discovery is disabled in this test, so introduce B to A by hand the same
    # way an incoming connection would.
    engine_a.discovery.peers.add_direct(engine_b.info, "127.0.0.1", engine_b.port)

    status, _headers, body = http(base + "/api/devices")
    devices = json.loads(body.decode("utf-8")).get("devices", [])
    ids = {device["id"] for device in devices}
    if not check("the peer engine is visible through /api/devices", engine_b.info.device_id in ids, str(ids)):
        return
    check(
        "the machine serving the page is offered as a target",
        bool(devices) and devices[0]["id"] == engine_a.info.device_id and devices[0].get("local") is True,
        str(devices[:2]),
    )

    payload = b"eversend web upload payload \xf0\x9f\x9a\x80 " + bytes(range(256)) * 512
    url = (
        base
        + "/api/upload?name="
        + urllib.parse.quote("手机上传 测试.bin")
        + "&deviceId="
        + urllib.parse.quote(engine_b.info.device_id)
        + "&pin="
    )
    status, _headers, body = http(
        url, method="POST", data=payload, headers={TOKEN_HEADER: token, "Content-Type": "application/octet-stream"}
    )
    try:
        answer = json.loads(body.decode("utf-8"))
    except ValueError:
        answer = {}
    check("upload returns 202 Accepted", status == 202, f"{status} {body[:120]!r}")
    check("upload answers with a transferId", bool(answer.get("transferId")), str(answer)[:120])
    check("upload echoes the byte count", answer.get("size") == len(payload), str(answer.get("size")))
    check("the filename is sanitised but kept", "手机上传" in str(answer.get("name", "")), str(answer.get("name")))

    expected = os.path.join(receive_dir(engine_b), answer.get("name", "?"))
    landed = wait_for(lambda: os.path.exists(expected), timeout=30)
    if not check("the file lands in the peer's receive directory", landed, expected):
        return
    with open(expected, "rb") as handle:
        received = handle.read()
    check("the bytes are identical end to end", received == payload, f"{len(received)} != {len(payload)}")

    # The spool file must be cleaned up: an upload is a transfer, not an inbox.
    check(
        "the spool file is removed after sending",
        wait_for(lambda: not spool_files(engine_a), timeout=15),
        str(spool_files(engine_a)),
    )

    # ---- the headline flow: a phone sends a file to the computer it is on --
    own_id = json.loads(http(base + "/api/state")[2].decode("utf-8"))["device"]["id"]
    self_payload = b"self send \x00\x01\x02" + os.urandom(4096)
    self_name = "本机直传 测试.bin"
    status, _headers, body = http(
        base + "/api/upload?name=" + urllib.parse.quote(self_name) + "&deviceId=" + urllib.parse.quote(own_id),
        method="POST",
        data=self_payload,
        headers={TOKEN_HEADER: token, "Content-Type": "application/octet-stream"},
    )
    check("uploading to the local machine is accepted", status == 202, f"{status} {body[:100]!r}")
    own_path = os.path.join(receive_dir(engine_a), self_name)
    if check(
        "the file arrives under its own name (no ' (1)' suffix)",
        wait_for(lambda: os.path.exists(own_path), timeout=30),
        own_path,
    ):
        with open(own_path, "rb") as handle:
            check("the local upload is byte-identical", handle.read() == self_payload)
    check(
        "the spool directory is empty again",
        wait_for(lambda: not spool_files(engine_a), timeout=15),
        str(spool_files(engine_a)),
    )
    check(
        "no spool file leaks into the receive directory",
        not [n for n in os.listdir(receive_dir(engine_a)) if n.startswith(".eversend-upload-")],
        str(os.listdir(receive_dir(engine_a))),
    )

    # A too-large body must be refused rather than filling the disk.  The cap
    # is exercised on a second server so the default (32 GiB) stays in place.
    small = create_web_ui(engine_a, host="127.0.0.1", port=0, max_upload_bytes=1024)
    try:
        small_port = small.start()
        status, _headers, body = http(
            f"http://127.0.0.1:{small_port}/api/upload?name=big.bin&deviceId="
            + urllib.parse.quote(engine_b.info.device_id),
            method="POST",
            data=b"0" * 8192,
            headers={TOKEN_HEADER: small.token, "Content-Type": "application/octet-stream"},
        )
        check("an upload over the size cap is rejected (413)", status == 413, f"{status} {body[:80]!r}")
        check("a refused upload leaves no spool file behind", not spool_files(engine_a), str(spool_files(engine_a)))
    finally:
        small.stop()


def check_offer_roundtrip(engine_a: Engine, engine_b: Engine, base: str, token: str, ui) -> None:
    """The receive direction, driven through the API the phone actually uses."""
    print("\n[6] Incoming offer: peer -> engine -> /api/state -> accept over HTTP")
    import threading

    from eversend.core.model import Peer

    # Ask before accepting, which is what a real phone sees.
    payload = b"hello from the other side\n" * 100
    source = os.path.join(receive_dir(engine_b), "incoming.txt")
    with open(source, "wb") as handle:
        handle.write(payload)

    engine_a.config.auto_accept_all = False
    peer = Peer(info=engine_a.info, address="127.0.0.1", port=engine_a.port)
    result: list[bool] = []

    def send() -> None:
        try:
            result.append(engine_b.send(peer, [source]))
        except Exception:
            result.append(False)

    thread = threading.Thread(target=send, daemon=True)
    thread.start()

    def find_offer() -> dict | None:
        for offer in ui.state()["offers"]:
            if offer.get("peer", {}).get("id") == engine_b.info.device_id:
                return offer
        return None

    got = wait_for(lambda: find_offer() is not None, timeout=25)
    check("the pending offer shows up in /api/state and /api/events", got)

    # ...and it must also be visible over HTTP, which is what the page reads.
    status, _headers, body = http(base + "/api/state")
    over_http = [
        offer
        for offer in json.loads(body.decode("utf-8")).get("offers", [])
        if offer.get("peer", {}).get("id") == engine_b.info.device_id
    ]
    check("the offer is served over HTTP too", bool(over_http))
    if not over_http:
        engine_a.resolve_offer(find_offer()["request_id"], False) if find_offer() else None
        thread.join(timeout=20)
        return

    offer = over_http[0]
    check("the offer carries the sender name", offer.get("peer", {}).get("name") == "NodeB", str(offer.get("peer")))
    files = offer.get("files") or []
    check("the offer lists the file and its size", bool(files) and files[0]["name"] == "incoming.txt" and files[0]["size"] == len(payload), str(files)[:120])

    status, _headers, body = http(
        base + "/api/offer/respond",
        method="POST",
        data=json.dumps({"requestId": offer["request_id"], "accept": True}).encode("utf-8"),
        headers={TOKEN_HEADER: token, "Content-Type": "application/json"},
    )
    check("accepting over HTTP succeeds", status == 200, f"{status} {body[:80]!r}")

    thread.join(timeout=40)
    held = os.path.join(receive_dir(engine_a), "incoming.txt")
    arrived = wait_for(lambda: os.path.exists(held), timeout=20)
    check("the accepted file arrives", arrived, held)
    if os.path.exists(held):
        with open(held, "rb") as handle:
            check("the received content is intact", handle.read() == payload)
    check("the sending side reported success", result == [True], str(result))

    if not arrived or result != [True]:
        # "it failed" is not a diagnosis.  Both engines keep their recent events
        # in memory, and those hold the sender's status and the receiver's
        # error -- the only two places that say *why*.
        for label, engine in (("NodeA/接收端", engine_a), ("NodeB/发送端", engine_b)):
            print(f"      ── {label} 最近事件 ──")
            interesting = [
                event
                for event in engine.events.recent
                if event.get("kind")
                in (
                    "offer_received",
                    "transfer_accepted",
                    "transfer_started",
                    "transfer_finished",
                    "transfer_rejected",
                    "peer_rejected",
                    "file_failed",
                    "file_unverified",
                    "send_offering",
                    "send_started",
                    "send_finished",
                    "stream_error",
                )
            ]
            for event in interesting[-8:]:
                detail = {
                    key: value
                    for key, value in event.items()
                    if key
                    in (
                        "kind",
                        "status",
                        "error",
                        "reason",
                        "bytes",
                        "total",
                        "name",
                        "resumed",
                    )
                }
                print(f"        {detail}")
        print(f"      发送端返回: {result!r}")


def check_https_copy_keeps_http_port(engine: Engine, http_port: int) -> None:
    """The HTTPS page must not steal the port that discovery announces.

    The desktop starts two copies of the same page: HTTP on 52119 and HTTPS on
    52120, because a phone browser only hands over the microphone in a secure
    context.  Both used to write ``engine.info.web_port``, so the second one
    won and every discovery announcement went out saying ``"web": 52120`` --
    and anything that builds "http://<host>:<web>/" from that (the Android app,
    a peer's device card) asks a TLS socket for plain HTTP.  The symptom is
    "the app found the computer but cannot connect", which is a long way from
    the cause.
    """
    from eversend.core import tls as tls_module

    print("\n[HTTPS 副本] 加密页面不能顶掉对外公告的 HTTP 端口")
    context = tls_module.ssl_context(engine.config.data_dir)
    if context is None:  # pragma: no cover - needs a broken openssl
        check("自签证书可以生成", False, "ssl_context() 返回 None")
        return
    check("自签证书可以生成", True)

    tls_ui = create_web_ui(engine, host="127.0.0.1", port=0, ssl_context=context, log_requests=False)
    try:
        tls_port = tls_ui.start()
        check("HTTPS 页面用的是另一个端口", tls_port != http_port, f"{tls_port} vs {http_port}")
        check(
            "公告出去的仍然是 HTTP 端口",
            engine.info.web_port == http_port,
            f"{engine.info.web_port} vs {http_port}",
        )
        # And the secure copy still answers, i.e. it was not silently dropped.
        import ssl as ssl_module
        import urllib.request

        ctx = ssl_module.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl_module.CERT_NONE
        request = urllib.request.Request(f"https://127.0.0.1:{tls_port}/api/state", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=5, context=ctx) as response:  # noqa: S310
                body = response.read().decode("utf-8", "replace")
            check("HTTPS 页面能应答", '"ok": true' in body, body[:80])
        except Exception as exc:
            check("HTTPS 页面能应答", False, repr(exc))
    finally:
        tls_ui.stop()


def check_media_source_stays_local(ui, base: str, engine: Engine, root: Path) -> None:
    """The desktop's own copy of a sent picture: previewable, but never shared.

    ``mediaSource`` is the absolute path this computer sent a file from.  The
    desktop needs it (otherwise a photo *you* sent has no preview: your copy is
    wherever you picked it, not in the receive folder), and the phone must not
    get it -- it describes the desktop's folder layout and is of no use there.
    """
    print("\n[附件路径] 本机路径只留在本机")
    picture = root / "自检图片.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 2048)
    conv = direct_conversation_id(engine.info.device_id, "selftest-peer")
    event = engine.send_chat(conv, kind="image", media_path=str(picture), to="selftest-peer")
    message_id = str((event.get("message") or {}).get("id", ""))
    stored = engine.chat.message(message_id) or {}
    check("本机发出的图片记下了它的原始路径", stored.get("mediaSource") == str(picture), str(stored.get("mediaSource")))
    wire = engine.chat_payload(engine.chat.conversation(conv) or {}, stored)["msg"]
    check("这条路径不会随消息发给对方", "mediaSource" not in wire, str(sorted(wire)))
    status, _headers, body = http(base + "/api/chat?conv=" + urllib.parse.quote(conv))
    payload = json.loads(body.decode("utf-8")) if body else {}
    leaked = [m for m in payload.get("messages", []) if "mediaSource" in m]
    check("手机接口里也看不到这条路径", status == 200 and not leaked, f"{status} leaked={len(leaked)}")


def check_emoji_api(base: str) -> None:
    """``/api/emoji``: the phone page and the app draw the same palette."""
    print("\n[表情] 三端共用一份表情表")
    from eversend.core.emoji import EMOJI_GROUPS

    status, _headers, body = http(base + "/api/emoji")
    payload = json.loads(body.decode("utf-8")) if body else {}
    groups = payload.get("groups") or []
    check("接口给了表情分组", status == 200 and len(groups) == len(EMOJI_GROUPS), f"{status} {len(groups)}")
    total = sum(len(group.get("emoji") or []) for group in groups)
    expected = sum(len(group) for _title, group in EMOJI_GROUPS)
    check("数量和核心里的表一致", total == expected, f"{total} vs {expected}")
    check("够多（用户嫌 48 个太少）", total >= 300, str(total))
    check(
        "每个分组都有名字和表情",
        all(group.get("title") and group.get("emoji") for group in groups),
        str([group.get("title") for group in groups]),
    )


def check_device_identity_is_stable(ui, base: str) -> None:
    """Same phone, new address: still *one* device, still one conversation.

    The page used to be keyed by address + User-Agent, so a phone that changed
    networks became a second device and its chat a second conversation.  Now the
    page carries a device id of its own; this checks the registry follows the id
    and not the address.
    """
    print("\n[设备唯一性] 换地址不换身份")
    agent = "Mozilla/5.0 (Linux; Android 16; V2408A) AppleWebKit/537.36 Chrome/146 Mobile Safari/537.36"
    device = "selftest-device-0001"
    ui.touch_client("10.9.9.50", agent, device_id=device)
    first = [c for c in ui.known_clients() if c.get("deviceId") == device]
    check("带设备号的浏览器被登记", len(first) == 1, str(first)[:100])
    key = first[0]["key"] if first else ""
    check("键是设备号而不是地址", key == "web:" + device, key)

    # 换一个网段、换一个 User-Agent：还是同一台设备
    ui.touch_client("192.168.77.9", "Mozilla/5.0 (Linux; Android 16; V2408A) Chrome/147 Mobile Safari/537.36",
                    device_id=device)
    again = [c for c in ui.known_clients() if c.get("deviceId") == device]
    check("换 IP 之后仍然只有一台", len(again) == 1, str(again)[:120])
    check("地址跟着更新", again and again[0]["address"] == "192.168.77.9", str(again)[:120])

    # 历史遗留清理：老版本按"地址+UA"记的那几条（浏览器一升级就多一条），
    # 在稳定设备号出现、并且它们已不活跃之后应当被收掉。
    legacy_agent = "Mozilla/5.0 (Linux; Android 16; V2408A) Chrome/146 Mobile Safari/537.36"
    ui.touch_client("192.168.77.9", legacy_agent)
    legacy_key = ui.client_key("192.168.77.9", legacy_agent)
    with ui._state_lock:
        ui._clients.pop(legacy_key, None)      # 让它变成"不活跃的旧记录"
    ui.touch_client("192.168.77.9", legacy_agent, device_id=device)
    remaining = [c for c in ui.known_clients() if c.get("address") == "192.168.77.9"]
    check(
        "同一地址上历史遗留的旧记录会被收掉",
        all("|" not in str(c.get("key")) for c in remaining),
        str([c.get("key") for c in remaining]),
    )

    # 去掉旧地址那条腿之后，历史会话仍然指向同一个身份
    with ui._state_lock:
        stale = [k for k, c in ui._known.items() if c.get("deviceId") == device and k != key]
    check("没有为同一台设备再建一条记录", not stale, str(stale))
    ui.remove_client(key)


def check_one_device_one_row(ui, base: str) -> None:
    """用户报的那两行「Android 上的 Chrome」——回归检查。

    触发链：浏览器**不可能**给地址栏导航（``GET /``）或者 ``<script src>`` 加
    自定义头，所以"打开页面"这一下没有 ``X-EverSend-Device``，落到"地址 + UA"
    的老分支上建了第二条记录；页面里的 ``fetch`` 才带设备号，走 ``web:<id>``。
    两条都在说话，桌面端就显示成两台设备。这里按真实 HTTP 路径走一遍，
    再补上换地址、普通浏览器、以及已经落盘的脏数据。
    """
    print("\n[设备去重] 一台手机只能有一行")
    agent = "Mozilla/5.0 (Linux; Android 16; V2509A) AppleWebKit/537.36 Chrome/148 Mobile Safari/537.36"
    device = "one-row-selftest-0001"
    other_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/148 Safari/537.36"
    address = "127.0.0.1"

    # ① 页面首次加载：没有自定义头（浏览器加不了）
    status, _headers, _body = http(base + "/", headers={"User-Agent": agent})
    check("打开页面成功", status == 200, str(status))
    first = [c for c in ui.known_clients() if c.get("agent") == agent]
    check("第一次打开时还只能按地址记一条（此时谁都不知道它是谁）",
          len(first) == 1 and "|" in str(first[0]["key"]), str([c["key"] for c in first]))

    # ② 页面里的 fetch 带 localStorage 里的设备号
    status, _headers, body = http(
        base + "/api/state",
        headers={"User-Agent": agent, "X-EverSend-Device": device},
    )
    check("带设备号的 /api/state 成功", status == 200, str(status))
    rows = [c for c in ui.known_clients() if c.get("address") == address and c.get("agent") == agent]
    check("一台手机只剩一条记录", len(rows) == 1, str([c["key"] for c in rows]))
    check("留下的是带设备号的那条", bool(rows) and rows[0]["key"] == "web:" + device,
          str([c["key"] for c in rows]))
    check("地址只是它身上的一个字段", bool(rows) and rows[0]["address"] == address, str(rows)[:140])
    check("identityKey 就是稳定设备号",
          bool(rows) and rows[0].get("identityKey") == "web:" + device, str(rows)[:140])
    check("它还在线（刚说过话）", bool(rows) and rows[0].get("online") is True, str(rows)[:140])

    state = json.loads(
        http(base + "/api/state", headers={"User-Agent": agent, "X-EverSend-Device": device})[2].decode("utf-8")
    )
    known = state.get("knownClients", [])
    check("/api/state 的 knownClients 里也只有一条",
          len([c for c in known if str(c.get("deviceId")) == device]) == 1,
          str([c.get("key") for c in known])[:140])
    check("每一条 knownClient 都带 identityKey",
          bool(known) and all(c.get("identityKey") for c in known), str(known)[:140])
    keys = [str(c.get("identityKey")) for c in known]
    check("identityKey 在名单里唯一（客户端可以据此兜底去重）",
          len(set(keys)) == len(keys), str(keys)[:140])
    check("正在说话的名单里也只有一个身份",
          len({str(c.get("identityKey")) for c in state.get("webClients", [])}) == len(state.get("webClients", [])),
          str([c.get("key") for c in state.get("webClients", [])])[:140])

    # ③ 设备记录已经有了之后再打开一次页面 / 拉静态资源：不能再多一条
    http(base + "/", headers={"User-Agent": agent})
    http(base + "/assets/app.js", headers={"User-Agent": agent})
    rows = [c for c in ui.known_clients() if c.get("address") == address and c.get("agent") == agent]
    check("再打开一次页面/静态资源也不会多一条", len(rows) == 1, str([c["key"] for c in rows]))

    # ④ 换地址（手机换网络）：还是同一条，地址跟着变
    ui.touch_client("192.168.88.6", agent, device_id=device)
    rows = [c for c in ui.known_clients() if c.get("deviceId") == device]
    check("换地址之后仍然只有一条", len(rows) == 1, str([c["key"] for c in rows]))
    check("地址更新成最新的那个", bool(rows) and rows[0]["address"] == "192.168.88.6", str(rows)[:140])
    check("换地址之后也还是一条在线记录",
          bool(rows) and rows[0].get("online") is True, str(rows)[:140])

    # ⑤ 没有设备号的普通浏览器（另一个 UA）：仍然各自成条，不会被并进手机那条
    http(base + "/", headers={"User-Agent": other_agent})
    plain = [c for c in ui.known_clients() if c.get("agent") == other_agent]
    plain_key = str(plain[0]["key"]) if plain else ""
    check("没有设备号的普通浏览器照样有自己的记录",
          len(plain) == 1 and "|" in plain_key, str([c["key"] for c in plain]))
    check("它的 identityKey 只能退回自己的地址键",
          bool(plain) and plain[0].get("identityKey") == plain_key, str(plain)[:140])
    check("它不会把手机那条吃掉",
          len([c for c in ui.known_clients() if c.get("deviceId") == device]) == 1,
          str([c["key"] for c in ui.known_clients()])[:160])
    if plain_key:
        ui.remove_client(plain_key)

    # ⑥ 已经落盘的脏数据：同一台设备的"设备号记录 + 地址记录"，两条都还在说话
    dirty_device = "dirty-legacy-0001"
    dirty_agent = "Mozilla/5.0 (Linux; Android 16; Pixel 9) AppleWebKit/537.36 Chrome/150 Mobile Safari/537.36"
    device_key = "web:" + dirty_device
    legacy_key = ui.client_key("10.7.7.7", dirty_agent)
    now = time.time()
    with ui._state_lock:
        ui._known[device_key] = {
            "key": device_key, "address": "10.7.7.7", "agent": dirty_agent, "label": "Android 上的 Chrome",
            "deviceId": dirty_device, "kind": "browser", "firstSeen": now - 60, "lastSeen": now - 5,
        }
        ui._known[legacy_key] = {
            "key": legacy_key, "address": "10.7.7.7", "agent": dirty_agent, "label": "Android 上的 Chrome",
            "firstSeen": now - 600, "lastSeen": now - 1,
        }
        ui._clients[device_key] = {
            "address": "10.7.7.7", "agent": dirty_agent, "label": "Android 上的 Chrome",
            "since": now - 60, "lastSeen": now - 5,
        }
        ui._clients[legacy_key] = {
            "address": "10.7.7.7", "agent": dirty_agent, "label": "Android 上的 Chrome",
            "since": now - 600, "lastSeen": now - 1,
        }
    dirty = [c for c in ui.known_clients() if c.get("address") == "10.7.7.7"]
    check("老数据里同一台设备的两条被并成一条", len(dirty) == 1, str([c["key"] for c in dirty]))
    check("并的是地址键那条，留下设备号那条",
          bool(dirty) and dirty[0]["key"] == device_key, str([c["key"] for c in dirty]))
    check("被并掉的旧键留在 aliases 里（老会话还要靠它查名字）",
          bool(dirty) and legacy_key in list(dirty[0].get("aliases") or []),
          str(dirty[0].get("aliases") if dirty else None))
    check("lastSeen 取最新的（刚说过话的在线状态不丢）",
          bool(dirty) and dirty[0].get("online") is True and dirty[0]["lastSeen"] >= now - 1.01,
          str(dirty)[:160])
    check("firstSeen 取更早的（配对历史不丢）",
          bool(dirty) and float(dirty[0].get("firstSeen") or 0) <= now - 599, str(dirty)[:160])
    with ui._state_lock:
        still_there = legacy_key in ui._known or legacy_key in ui._clients
    check("地址键那条真的从注册表和活跃表里都清掉了", not still_there, legacy_key)

    # ⑦ 换过地址留下的幽灵行：它已经不在说话，也要能被收掉
    ghost_key = ui.client_key("10.7.7.8", dirty_agent)
    with ui._state_lock:
        ui._known[ghost_key] = {
            "key": ghost_key, "address": "10.7.7.8", "agent": dirty_agent, "label": "Android 上的 Chrome",
            "firstSeen": now - 900, "lastSeen": now - 800,
        }
    ghosts = [c for c in ui.known_clients() if c.get("agent") == dirty_agent]
    check("换过地址的旧记录也被收掉，不留幽灵行",
          len(ghosts) == 1 and ghosts[0]["key"] == device_key, str([c["key"] for c in ghosts]))

    # ⑧ online 仍然是"现在正在说话"（key 还在活跃表里），不是"记录还在"
    with ui._state_lock:
        ui._clients.pop(device_key, None)
    offline = [c for c in ui.known_clients() if c.get("key") == device_key]
    check("不在说话了就是 online=False（记录本身仍然留着）",
          len(offline) == 1 and offline[0].get("online") is False, str(offline)[:140])

    # ⑨ 「断开连接」（/api/leave）必须还能断开带设备号的那条记录
    status, _headers, body = http(
        base + "/api/leave",
        method="POST",
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            TOKEN_HEADER: ui.token,
            "User-Agent": agent,
            "X-EverSend-Device": device,
        },
    )
    answer = json.loads(body.decode("utf-8")) if body else {}
    check("带设备号的「断开连接」回 200", status == 200, f"{status} {body[:60]!r}")
    check("接口说它断开了", bool(answer.get("forgotten")), str(answer))
    check("断开之后名单里没有它了",
          not [c for c in ui.known_clients() if c.get("deviceId") == device],
          str([c["key"] for c in ui.known_clients()])[:160])
    ui.remove_client(device_key)  # 已经断了，这里只是保证不留残留


def check_merge_keeps_conversation_title(ui, base: str) -> None:
    """归并旧记录不能把老会话的名字弄丢。

    用户真实数据里那段 **289 条消息**的会话，成员写的是"影子记录"的写法
    ``web:127.0.0.1|d2445430`` —— 页面首次加载（浏览器加不了自定义头）时按
    "地址 + UA"建的那条。T1 把这台手机归并成一条设备记录之后，注册表里就只剩
    ``android:<id>`` / ``web:<uuid>``：名字必须仍然认得出来，否则会话列表上
    那段聊天会从「安卓 App」掉成「未知设备」。
    """
    print("\n[归并留痕] 老会话的成员写法还要认得出来")
    engine = ui.engine
    agent = "EverSend-Android/1.0 (Android 14)"
    legacy_key = ui.client_key("127.0.0.1", agent)      # 127.0.0.1|<UA摘要>
    member = "web:" + legacy_key                        # 会话里存的就是这个写法
    now = time.time()
    with ui._state_lock:
        ui._known[legacy_key] = {
            "key": legacy_key, "address": "127.0.0.1", "agent": agent,
            "label": "安卓 App", "firstSeen": now - 900, "lastSeen": now - 800,
        }
    conv_id = direct_conversation_id(engine.info.device_id, member)
    engine.chat.upsert_conversation(conv_id, kind="direct", members=[member])

    def title(conversation_id: str) -> str:
        """走真实接口：/api/state → _conversation_labels → 名字。"""
        body = json.loads(http(base + "/api/state")[2].decode("utf-8"))
        items = (body.get("chat") or {}).get("conversations") or []
        found = next((item for item in items if item.get("id") == conversation_id), None)
        return str((found or {}).get("displayTitle") or "")

    before = title(conv_id)
    check("归并前：那段老会话有名字（不是「未知设备」）", before not in ("", "未知设备"), before)

    # 这台手机带着设备号回来了：影子记录会被收掉（App 登记时的旧记录清理）。
    ui.register_app("legacy-shadow-0001", name="老安卓机", address="127.0.0.1", agent=agent)
    with ui._state_lock:
        kept = dict(ui._known.get("android:legacy-shadow-0001") or {})
        gone = legacy_key not in ui._known
    check("影子记录被并掉了（注册表里只剩设备号那条）", gone, str(sorted(ui._known))[:160])
    check("旧键被记成 alias", legacy_key in list(kept.get("aliases") or []),
          str(kept.get("aliases"))[:160])

    after = title(conv_id)
    check("归并之后老会话的标题一字不变", after == before, f"{before!r} → {after!r}")

    # 反过来也要对：新会话用的是设备现在的名字，没有被旧名字粘住。
    fresh_id = direct_conversation_id(engine.info.device_id, "android:legacy-shadow-0001")
    engine.chat.upsert_conversation(fresh_id, kind="direct", members=["android:legacy-shadow-0001"])
    check("新会话用的是设备现在的名字", title(fresh_id) == "老安卓机", title(fresh_id))

    for conversation_id in (conv_id, fresh_id):
        engine.chat.remove_conversation(conversation_id)
    ui.remove_client("android:legacy-shadow-0001")


def check_broken_registry_row_does_not_break_state(ui, base: str) -> None:
    """注册表里一行坏数据不能让整页挂掉。

    这份文件是直接从磁盘读回来的（手工改过、旧版本写的都可能），字段残缺时
    ``known_clients()`` 以前用 ``client["address"]`` 这种下标读，一行坏数据就
    让 ``/api/state`` 整个 500 —— 用户看到的是"页面连不上"，连哪台设备坏了
    都看不见。
    """
    print("\n[坏数据] 字段残缺的记录不能把 /api/state 拖崩")
    with ui._state_lock:
        ui._known["web:broken-row-0001"] = {"deviceId": "broken-row-0001", "aliases": "不是列表"}
        ui._known["broken-row-no-key"] = {"address": "10.4.4.4", "lastSeen": "昨天"}
    crashed = ""
    rows: list = []
    try:
        rows = ui.known_clients()
    except Exception as exc:  # noqa: BLE001 - 这条检查就是"不许抛"
        crashed = f"{type(exc).__name__}: {exc}"
    check("known_clients() 不抛异常", not crashed, crashed)

    broken = [c for c in rows if str(c.get("deviceId")) == "broken-row-0001"]
    check("残缺记录照样列得出来（缺字段用默认值）",
          len(broken) == 1
          and broken[0].get("address") == ""
          and broken[0].get("lastSeen") == 0.0
          and isinstance(broken[0].get("secondsAgo"), float),
          str(broken)[:160])
    keyless = [c for c in rows if str(c.get("key")) == "broken-row-no-key"]
    check("记录里没有 key 字段时用注册表的键兜底（那一行才点得动）",
          len(keyless) == 1 and keyless[0].get("address") == "10.4.4.4", str(keyless)[:160])
    check("时间字段是字符串也不炸（当成 0）",
          bool(keyless) and keyless[0].get("secondsAgo", -1) >= 0, str(keyless)[:160])
    check("每条 knownClient 仍然都有 identityKey",
          all("identityKey" in c for c in rows), str([c.get("key") for c in rows])[:160])

    status, _headers, _body = http(base + "/api/state")
    check("/api/state 不会因为一行坏数据 500", status == 200, str(status))
    with ui._state_lock:
        for row in ("web:broken-row-0001", "broken-row-no-key"):
            ui._known.pop(row, None)


def check_discovery_reply(engine: Engine) -> None:
    """A phone that asks "who is there?" must be answered immediately.

    The phone's 「搜索电脑」 sends one small probe and waits; the desktop used to
    answer only on its own 30-second broadcast schedule, so searching appeared
    to find nothing.  This is the desktop half of that fix: an announcement
    arriving from *any* port is answered with a unicast reply straight back to
    the sender's port, and the answer is rate-limited so a burst cannot turn
    into an amplification loop.
    """
    print("\n[发现回包] 电脑要立刻回答手机的探针")
    import socket as socket_module

    port = int(getattr(engine.discovery, "port", 0) or engine.config.discovery_port)
    if not port:
        check("引擎有发现端口", False, str(port))
        return
    announcement = json.dumps(
        {
            "t": "eversend/1",
            "id": "selftest-phone",
            "n": "测试手机",
            "k": "mobile",
            "p": "android",
            "v": "1.0.0",
            "port": 0,
            "web": 0,
            "ts": 0,
        }
    ).encode("utf-8")

    probe = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))  # 临时端口：手机绑不上 52118 时走的就是这条路
    probe.settimeout(2.0)
    try:
        probe.sendto(announcement, ("127.0.0.1", port))
        reply = None
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and reply is None:
            try:
                data, _address = probe.recvfrom(8192)
            except socket_module.timeout:
                continue
            payload = json.loads(data.decode("utf-8", "replace"))
            if payload.get("t") == "eversend/1":
                reply = payload
        check("手机发一条探针，电脑立刻单播回包", reply is not None, "4 秒内没有回包")
        if reply is not None:
            check(
                "回包里带着电脑的网页端口（手机要靠它拼地址）",
                int(reply.get("web", 0)) > 0,
                str(reply),
            )
            check("回包回的是发送方的端口，不是广播", True)

        # 冷却：紧接着再发一条，一秒内的第二次不应该再回（避免被当成放大器）
        probe.sendto(announcement, ("127.0.0.1", port))
        early = 0
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            try:
                data, _address = probe.recvfrom(8192)
            except socket_module.timeout:
                break
            if json.loads(data.decode("utf-8", "replace")).get("t") == "eversend/1":
                early += 1
        check("一秒内的重复探针不会反复回包（有冷却）", early == 0, f"多回了 {early} 次")

        # 不相干的 UDP 包不该得到任何回应（不做反射器）
        probe.sendto(b"hello there, not eversend", ("127.0.0.1", port))
        noise = 0
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            try:
                probe.recvfrom(8192)
            except socket_module.timeout:
                break
            noise += 1
        check("不是韧传的包一律不回应", noise == 0, f"回了 {noise} 次")
    finally:
        probe.close()


def check_mdns_answer(engine: Engine) -> None:
    """The third discovery channel: mDNS, and it must answer *queries*.

    ``mdns.build_announcement`` existed from the first release, but nothing ever
    called it -- so Android's own ``NsdManager`` (which the app uses) could not
    see this program at all, and the desktop only ever appeared on the network
    through its 30-second UDP broadcast.  This check sends a real PTR query to
    the mDNS group and inspects the answer: PTR + SRV + TXT + A, all four, with
    the web port in TXT -- that is exactly what NsdManager resolves.
    """
    print("\n[mDNS] 电脑要能回答查询（安卓 NsdManager 靠它找电脑）")

    # 专门起一个开了 mDNS 的引擎：自检里那两个引擎为了让测试快而关掉了发现。
    import tempfile as _tempfile

    root = Path(_tempfile.mkdtemp(prefix="eversend-mdns-"))
    engine = Engine(
        EngineConfig(
            data_dir=str(root / "state"),
            receive_dir=str(root / "recv"),
            name="mDNS-Desktop",
            tcp_port=0,
            discovery_port=free_port(),
            enable_broadcast=False,
            enable_mdns=True,
            enable_web=False,
            auto_accept_all=True,
        )
    )
    engine.start()
    try:
        check_mdns_answer_against(engine)
    finally:
        engine.stop()
        shutil.rmtree(root, ignore_errors=True)


def check_mdns_answer_against(engine: Engine) -> None:
    from eversend.core import mdns
    from eversend.core.constants import MDNS_GROUP_V4, MDNS_PORT, MDNS_SERVICE_TYPE

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    try:
        sock.bind(("", MDNS_PORT))
    except OSError as exc:
        check("能监听 mDNS 端口", False, str(exc))
        sock.close()
        return
    joined = False
    for iface in ("0.0.0.0",):
        try:
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                struct.pack("4s4s", socket.inet_aton(MDNS_GROUP_V4), socket.inet_aton(iface)),
            )
            joined = True
        except OSError:
            pass
    check("能加入 mDNS 组播组", joined)
    sock.settimeout(0.5)
    try:
        try:
            sock.sendto(mdns.build_query(MDNS_SERVICE_TYPE), (MDNS_GROUP_V4, MDNS_PORT))
        except OSError as exc:
            # 有些环境根本没有组播路由（CI 的 macOS runner 就是：Errno 65
            # No route to host）。那是环境限制，不是程序的缺陷 —— 说清楚跳过，
            # 别把它记成失败，也别假装验证过了。
            print(f"  (跳过：这台机器发不出组播 —— {exc})")
            return
        # Keep listening for the whole window: this machine may be running a
        # *second* EverSend (the user's own copy, the desktop's own periodic
        # announcement), and taking the first reply would then compare against
        # the wrong device -- which is exactly how this check flaked once.
        deadline = time.monotonic() + 4
        answers: list[list] = []
        kinds: set[int] = set()
        ours: dict = {}
        while time.monotonic() < deadline:
            try:
                data, _address = sock.recvfrom(9000)
            except socket.timeout:
                continue
            if MDNS_SERVICE_TYPE.split(".")[0].encode("utf-8") not in data:
                continue
            records = mdns.parse_records(data)
            if not records:
                continue
            answers.append(records)
            kinds |= {record.rtype for record in records}
            txt = next((r for r in records if r.rtype == mdns.TYPE_TXT), None)
            fields = mdns.parse_txt(txt.rdata) if txt is not None else {}
            if fields.get("id") == engine.info.device_id:
                ours = fields
                break
        check("查询得到回答", bool(answers), "4 秒内没有回答")
        check(
            "回答里有 PTR/SRV/TXT/A 四段",
            {mdns.TYPE_PTR, mdns.TYPE_SRV, mdns.TYPE_TXT, mdns.TYPE_A} <= kinds,
            str(sorted(kinds)),
        )
        check(
            "TXT 里带着设备号与网页端口（手机要用来拼地址）",
            bool(ours) and int(ours.get("web", 0) or 0) > 0,
            str(ours or "没等到本引擎自己的那条"),
        )
    finally:
        sock.close()


def check_apk_download(base: str, ui, engine: Engine, root: Path) -> None:
    """Handing the Android installer to a phone from this computer.

    The release page is where the app normally comes from, but a phone on a
    hotspot often cannot reach the internet, and it is already talking to this
    machine.  The card must appear only when a real installer is present --
    offering a download that 404s is worse than not offering it.
    """
    print("\n[APK] 手机从这台电脑直接下载安卓安装包")
    status, _, body = http(base + "/api/state")
    state = json.loads(body.decode("utf-8"))
    check("没有安装包时状态里说不可用", not state["app"]["apk"].get("available"), str(state["app"]["apk"]))
    check("没有安装包时 /apk 回 404", http(base + "/apk")[0] == 404)
    check("页面里那一块默认是隐藏的", 'id="app-card" hidden' in _asset_text(ui, "index.html"))
    # 设备列表：电脑和手机在同一个列表里，标题统一叫「设备列表」
    html = _asset_text(ui, "index.html")
    check("网页版有「设备列表」卡片", "<h2>设备列表</h2>" in html, html[:0])
    check("不再有单独的「局域网里的手机」卡片", "phone-peers" not in html)

    data_dir = Path(engine.config.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    installer = data_dir / "EverSend-android.apk"
    payload = b"PK\x03\x04" + b"eversend-selftest-apk" * 512
    installer.write_bytes(payload)
    try:
        # The lookup is cached for a few seconds so a phone polling /api/state
        # every second does not hit the filesystem every time; expire it here
        # rather than sleeping through it.
        ui._apk_checked = 0.0  # noqa: SLF001 - the test owns this instance
        status, _, body = http(base + "/api/state")
        apk = json.loads(body.decode("utf-8"))["app"]["apk"]
        check("放进安装包后状态里说可用", bool(apk.get("available")), str(apk))
        check("状态里带上文件名和大小", apk.get("name") == installer.name and apk.get("size") == len(payload), str(apk))

        status, headers, body = http(base + "/apk")
        check("下载得到安装包本体", status == 200 and body == payload, f"status={status} bytes={len(body)}")
        check(
            "带的是安卓安装包的 MIME 类型",
            headers.get("Content-Type", "").startswith("application/vnd.android.package-archive"),
            headers.get("Content-Type", ""),
        )
        check(
            "浏览器会把它当附件存下来",
            "attachment" in headers.get("Content-Disposition", ""),
            headers.get("Content-Disposition", ""),
        )

        # A phone that loses Wi-Fi mid-download retries with a Range request;
        # _stream_file already supports it and this is the case that uses it.
        status, headers, body = http(base + "/apk", headers={"Range": "bytes=4-19"})
        check(
            "断点续传取得到中间一段",
            status == 206 and body == payload[4:20],
            f"status={status} bytes={len(body)}",
        )
    finally:
        installer.unlink(missing_ok=True)
        ui._apk_checked = 0.0  # noqa: SLF001
    check("删掉之后又变回不可用", not json.loads(http(base + "/api/state")[2].decode("utf-8"))["app"]["apk"].get("available"))


def check_conversation_names(ui, base: str) -> None:
    """会话列表要给人看的名字，而不是数据库主键。

    安卓 App 的会话列表是直接读 ``/api/state`` 里的 ``chat.conversations`` 的，
    而库里存的只有会话 id（``d:c96ec2dd94``）。名字必须由服务端算好 —— 三个
    客户端各算一遍，就一定会有一处忘了算：CI 截图里 App 的列表上就写着
    "d:c96ec2dd94"。

    而且要看**谁在看**：电脑看一对一，对方是手机；手机看同一个会话，对方
    是电脑。两边都按电脑的视角算，手机上那条会话就写着手机自己的名字。
    """
    print("\n[会话名字] 三个客户端拿到的都是人能读的名字，而且视角正确")
    engine = ui.engine
    agent = "EverSend-Android/1.0 (Android 16)"
    entry = ui.register_app("name-probe", name="我的手机", version="1.0.0", address="10.8.8.8", agent=agent)
    key = str(entry.get("key") or "")
    conv_id = direct_conversation_id(engine.info.device_id, key)
    engine.chat.upsert_conversation(conv_id, kind="direct", members=[engine.info.device_id, key])
    engine.chat.add_message(conv_id, sender=key, sender_name="我的手机", text="你好", direction="in")

    # 手机的身份（请求头里带自己的设备号）就是 App 读列表时的那条路。
    phone = {"X-EverSend-Device": "name-probe", "User-Agent": agent}
    body = json.loads(http(base + "/api/state", headers=phone)[2].decode("utf-8"))
    items = (body.get("chat") or {}).get("conversations") or []
    mine = next((item for item in items if item.get("id") == conv_id), None)
    check("会话在列表里", mine is not None, str(items)[:120])
    title = str((mine or {}).get("displayTitle") or "")
    check("手机上看到的是电脑的名字", title == engine.info.name, f"{title!r} / {engine.info.name!r}")
    check("不是手机自己的名字（那等于照镜子）", title != "我的手机", title)
    check("会话 id 不再被当成名字用", not title.startswith("d:"), title)
    check(
        "成员也是名字（系统 · 版本 · IP）",
        engine.info.name in str((mine or {}).get("displayPeople") or ""),
        str((mine or {}).get("displayPeople")),
    )

    body = json.loads(http(base + f"/api/chat?conv={conv_id}", headers=phone)[2].decode("utf-8"))
    items = body.get("conversations") or []
    mine = next((item for item in items if item.get("id") == conv_id), None)
    check("/api/chat 也带同一个名字", str((mine or {}).get("displayTitle") or "") == engine.info.name)

    group = new_group_id()
    engine.chat.upsert_conversation(group, kind="group", title="", members=[engine.info.device_id, key])
    body = json.loads(http(base + "/api/state", headers=phone)[2].decode("utf-8"))
    items = (body.get("chat") or {}).get("conversations") or []
    mine = next((item for item in items if item.get("id") == group), None)
    check("没起名的群写人数", str((mine or {}).get("displayTitle") or "") == "群聊（2 人）",
          str((mine or {}).get("displayTitle")))

    # 对方的记录已经没了（重装、清理过配对列表）：宁可写"未知设备"，也不要
    # 甩一串 id 前缀（用户看到 "web:127.0." 只会更糊涂）。这条直接问那个
    # 共用函数 —— 上面几条已经证明接口走的就是它。
    unknown_member = "web:127.0.0.1|gone"
    unknown_title = conversation_title(
        {"id": "d:x", "kind": "direct", "members": [engine.info.device_id, unknown_member]},
        {engine.info.device_id: f"{engine.info.name}（本机）"},
        engine.info.device_id,
    )
    check("查不到名字时不拿 id 前缀充数", unknown_title == "未知设备", unknown_title)

    for conv in (conv_id, group):
        engine.chat.remove_conversation(conv)
    ui.remove_client(key)


def check_app_registration(ui) -> None:
    """The native app registers under its own id, and does not appear twice.

    An earlier build of the app never said who it was, so the desktop only saw
    its HTTP requests and filed it under the browser key (address + UA).  When
    the app does introduce itself, that older entry is the same handset: the
    user asked for one device, not one app plus one browser.
    """
    print("\n[手机 App 登记] 用设备号登记，并且不会和「浏览器」那条重复")
    agent = "EverSend-Android/1.0 (Android 16)"
    ui.touch_client("10.9.9.10", agent)  # 老版本 App：只留下"浏览器"式的记录
    stale = [c for c in ui.known_clients() if c["address"] == "10.9.9.10"]
    check("老版本 App 会以浏览器身份被记住", len(stale) == 1, str(stale)[:120])

    # 只"听见"过（UDP 公告）的设备是临时的：真 App 会继续说话、标记被清掉；
    # 一次性的探针（自检、别人手机搜一下）不能永远占着用户的配对列表。
    ui.register_from_discovery(
        {"device_id": "heard-once", "name": "只广播过一次", "address": "10.9.9.12", "version": "1.0.0"}
    )
    heard = [c for c in ui.known_clients() if c.get("key") == "android:heard-once"]
    check("只广播过的设备先记下来（也许马上就会说话）", len(heard) == 1, str(heard)[:80])
    # 同一台手机重装 App 会换设备号：同名、旧的不在线 → 当成同一台收掉
    ui.register_app("old-install", name="我的手机", address="10.9.9.13", agent=agent)
    ui.register_app("new-install", name="我的手机", address="10.9.9.13", agent=agent)
    installs = [c for c in ui.known_clients() if c.get("label") == "我的手机"]
    check("重装 App 后不会留下两条同名记录", len(installs) == 1, str([c["key"] for c in installs]))
    ui.remove_client("android:new-install")

    ui.register_app("heard-once", name="只广播过一次", address="10.9.9.12", agent=agent)
    with ui._state_lock:
        # 把时间推回到两分钟前：它一直没再出现
        ui._known["android:heard-once"]["lastSeen"] = time.time() - 120
        ui._known["android:heard-once"]["provisional"] = True
    left = [c for c in ui.known_clients() if c.get("key") == "android:heard-once"]
    check("一直没再出现的临时记录会被忘掉", not left, str(left)[:80])
    ui.remove_client("android:heard-once")

    entry = ui.register_app("device-42", name="我的手机", version="1.0.0", address="10.9.9.10", agent=agent)
    check("App 用自己的设备号登记", entry.get("key") == "android:device-42", str(entry)[:120])
    check("登记后标签就是设备名", entry.get("label") == "我的手机", str(entry.get("label")))
    check("类型标成 App（不是浏览器）", entry.get("kind") == "app")

    mine = [c for c in ui.known_clients() if c["address"] == "10.9.9.10"]
    check("同一个手机上不再有重复记录", len(mine) == 1, str([c["key"] for c in mine]))
    check("留下的那条就是 App", mine and mine[0]["key"] == "android:device-42")

    # 地址变了（手机换网）也要认得出是同一台，而不是多出一台新设备。
    ui.register_app("device-42", name="我的手机", address="10.9.9.99", agent=agent)
    keys = [c["key"] for c in ui.known_clients() if c.get("deviceId") == "device-42"]
    check("换网后仍然只有一台", keys == ["android:device-42"], str(keys))
    ui.remove_client("android:device-42")

    try:
        ui.register_app("", address="10.9.9.10")
        check("没有设备号就拒绝登记", False, "register_app('') 没有报错")
    except ValueError:
        check("没有设备号就拒绝登记", True)

    # App 每隔几秒就要问一次状态；这些普通请求不能再造出第二条记录，否则
    # /api/hello 刚删掉的那条"浏览器"记录下一轮又回来了。
    ui.touch_client("10.9.9.10", "EverSend-Android/1.0 (Android 16)")
    ui.register_app("device-9", name="我的手机", address="10.9.9.10", agent="EverSend-Android/1.0 (Android 16)")
    for _ in range(3):
        ui.touch_client("10.9.9.10", "EverSend-Android/1.0 (Android 16)")
    rows = [c for c in ui.known_clients() if c["address"] == "10.9.9.10"]
    check("App 反复轮询也只留一条记录", len(rows) == 1, str([c["key"] for c in rows]))
    check("而且它还显示为在线", bool(rows) and rows[0].get("online") is True, str(rows)[:120])
    ui.remove_client("android:device-9")

    # 记住的设备会一直留着，所以它的说明文字必须跟着 describe_agent() 一起更新，
    # 否则用户看到的永远是上周那套说法（App 一开始被当成"浏览器"）。
    ui.touch_client("10.9.9.11", "EverSend-Android/1.0 (Android 16)")
    old = [c for c in ui.known_clients() if c["address"] == "10.9.9.11"][0]
    with ui._state_lock:
        ui._known[old["key"]]["label"] = "Android 上的 浏览器"
    ui.touch_client("10.9.9.11", "EverSend-Android/1.0 (Android 16)")
    refreshed = [c for c in ui.known_clients() if c["address"] == "10.9.9.11"][0]
    check("记住的设备说明文字会跟着更新", refreshed["label"] == "安卓 App", refreshed["label"])
    ui.remove_client(old["key"])


def check_stream_keeps_client_online(ui, base: str) -> None:
    """开着的**事件流**就该一直算在线 —— 这条是回归测试。

    手机端的保活服务把 SSE 挂上之后就阻塞在流里（它本来就该一直挂着），心跳请求
    再也发不出去；服务端如果只把"离散的 HTTP 请求"算作在线，15 秒后这台手机就
    被标成离线 —— 用户看到的是"手机明明连着、电脑端却显示未连接"，发给它的文件
    被当成离线留着。安卓仪器测试 ``keepAliveServiceStaysOnline`` 抓的就是这个。

    所以这里真的开一条带设备号的事件流，然后**除了这条流什么都不发**，等过一个
    ``CLIENT_TTL`` 再问一次 /api/state。
    """
    print("\n[流保活] 开着事件流就该一直算在线")
    agent = "EverSend-Android/1.0 (Android 16)"
    device = "stream-keepalive"
    ui.register_app(device, name="流保活手机", address="10.9.9.31", agent=agent)
    try:
        request = urllib.request.Request(
            base + "/api/events",
            headers={
                "Accept": "text/event-stream",
                "User-Agent": agent,
                "X-EverSend-Device": device,
                "X-EverSend-Name": "%E6%B5%81%E4%BF%9D%E6%B4%BB%E6%89%8B%E6%9C%BA",
            },
        )
        stream = urllib.request.urlopen(request, timeout=10)
        try:
            check("事件流打开了", stream.status == 200, str(stream.status))
            # 手机就是这样：SSE 挂着，除此之外一个请求都不发。
            deadline = time.monotonic() + CLIENT_TTL + 3
            while time.monotonic() < deadline:
                time.sleep(0.5)
            state = json.loads(http(base + "/api/state")[2].decode("utf-8"))
            mine = [
                c for c in state.get("knownClients", []) if c.get("key") == f"android:{device}"
            ]
            check(
                f"{CLIENT_TTL:.0f} 秒没发请求、流还开着 → 仍然算在线",
                bool(mine) and mine[0].get("online") is True,
                str(mine)[:160],
            )
            check(
                "...而且是刚碰过的那种在线（secondsAgo 在窗口内）",
                bool(mine) and float(mine[0].get("secondsAgo", 999)) < CLIENT_TTL,
                str(mine)[:160],
            )
        finally:
            stream.close()
    finally:
        ui.remove_client(f"android:{device}")
    # 流关掉之后要慢慢回到"离线"，否则这个在线就是假的。
    deadline = time.monotonic() + CLIENT_TTL + 3
    while time.monotonic() < deadline:
        time.sleep(0.5)
    state = json.loads(http(base + "/api/state")[2].decode("utf-8"))
    mine = [c for c in state.get("knownClients", []) if c.get("key") == f"android:{device}"]
    check(
        "流关掉之后它才变成不在线",
        not mine or mine[0].get("online") is not True,
        str(mine)[:160],
    )
    ui.remove_client(f"android:{device}")



def check_offline_devices_are_listed(ui) -> None:
    """走过一台设备之后，它应该掉进「离线」那一栏，而不是从列表里消失。

    页面要求把设备列表分成「在线 / 离线」两段（``docs/UI_LAYOUT.md`` §2③）。
    引擎的发现表有 TTL：一台电脑关掉几分钟后就不在 ``engine.devices()`` 里了，
    如果 /api/state 只报"现在活着的"，离线那一段永远是空的 —— 功能等于没做。
    """
    print("\n[设备两段] 走过的设备要留在「离线」里")
    state = json.loads(http(f"http://127.0.0.1:{ui._bound_port}/api/state")[2].decode("utf-8"))
    devices = {str(d.get("id")): d for d in state.get("devices", [])}
    check("设备列表里每一台都带 online 标记",
          all("online" in d for d in devices.values()),
          str([(d.get("name"), d.get("online")) for d in list(devices.values())[:4]]))
    check("在线的那台（本机）online=True",
          devices.get(str(state["device"]["id"]), {}).get("online") is not False)

    # 记一台假的设备，然后把它的时间推回到 10 分钟前：它就该出现在离线里
    ui._remember_device(
        {"id": "offline-demo", "name": "早就关机的笔记本", "address": "10.9.9.77", "online": True},
        time.time() - 600,
    )
    state = json.loads(http(f"http://127.0.0.1:{ui._bound_port}/api/state")[2].decode("utf-8"))
    found = [d for d in state.get("devices", []) if str(d.get("id")) == "offline-demo"]
    check("离线设备仍然出现在设备列表里", len(found) == 1, str(found)[:140])
    if found:
        check("它被标成 online=false", found[0].get("online") is False, str(found[0])[:140])
        check("并带上「最后在线」秒数", float(found[0].get("secondsAgo") or 0) >= 600,
              str(found[0].get("secondsAgo")))
    ui._remembered.pop("offline-demo", None)


def check_offline_devices_can_be_cleared(ui) -> None:
    """清空「以前见过」的离线设备：列表干净了，在线的与配对关系都不受影响。"""
    print("\n[设备两段] 离线名单要能一键清空")
    ui._remember_device(
        {"id": "cleanup-demo", "name": "以前见过的电脑", "address": "10.9.9.88", "online": True},
        time.time() - 600,
    )
    state = json.loads(http(f"http://127.0.0.1:{ui._bound_port}/api/state")[2].decode("utf-8"))
    before = [d for d in state.get("devices", []) if str(d.get("id")) == "cleanup-demo"]
    check("清空前它还在离线列表里", len(before) == 1, str(before)[:120])

    answer = json.loads(
        http(
            f"http://127.0.0.1:{ui._bound_port}/api/devices/forget-offline",
            method="POST",
            data=b"",
            headers={"X-EverSend-Token": ui.token},
        )[2].decode("utf-8")
    )
    check("接口回了清掉几台",
          bool(answer.get("ok")) and int(answer.get("forgotten") or 0) >= 1, str(answer))
    state = json.loads(http(f"http://127.0.0.1:{ui._bound_port}/api/state")[2].decode("utf-8"))
    check("清空后列表里没有它了",
          not [d for d in state.get("devices", []) if str(d.get("id")) == "cleanup-demo"],
          str([d.get("name") for d in state.get("devices", [])])[:120])
    check("本机（在线设备）不受影响",
          any(str(d.get("id")) == str(state["device"]["id"]) for d in state.get("devices", [])))


# =====================================================================
# 企业级改造（docs/ENTERPRISE_CHARTER.md §2 / §3 / §4 / §5，C12）
#
# 这一节盯的是**网页版自己不杂乱、不重复、与桌面端同一套语义**：
#   · 骨架：宽屏侧栏 288px、窄屏抽屉 + ☰；主区只有 会话/传输/设置 三页；
#   · 容器：没有三层以上的包裹容器（div/section），行里（li/td）不许再套容器；
#   · token：颜色来自 desktop/theme.py 那一组变量，规则里不写裸 hex；
#   · 文案：设备列表 / 会话 / 传输 / 接收 / 发送 / 设置 / 连接手机 / 扫描 逐字一致；
#   · 入口：一个意图一个入口（刷新并进扫描、二维码并进连接手机、主题只在设置里）；
#   · 传输页：筛选药丸 + 搜索 + 空状态 + 中文失败原因 + 行右键（长按）完整动作菜单。
#
# 结构检查走的是"解析 index.html / app.css / app.js"这条路：这些都是**静态事实**，
# 不依赖浏览器，也就不需要 UI 测试框架（脚本要在没有图形库的机器上跑得起来）。
# =====================================================================

#: 包裹容器：只为"把别的东西套起来"而存在的标签。列表（ul/ol）、表格
#: （table/thead/tbody/tr/td）、列表项（li）与所有控件都是**内容**，不是容器 ——
#: 内容网格本来就长这样，用户看到的"杂乱"来自一层层白套的 div/section。
_WRAP_TAGS = {"div", "section"}
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}
#: 主区容器层数上限（宪章 C12：没有嵌套三层以上的容器）。
MAX_WRAP_DEPTH = 3


class _HtmlDepth(HTMLParser):
    """量 index.html 的两件事：包裹层最深几层、行里有没有套容器。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.max_depth = 0
        self.deepest: list[str] = []
        self.inside_row: list[str] = []
        self.main_depths: list[int] = []
        self.main_deepest: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        label = tag
        if values.get("id"):
            label += "#" + str(values["id"])
        elif values.get("class"):
            label += "." + str(values["class"]).split()[0]
        if tag in _VOID_TAGS:
            path = self.stack + [label]
        else:
            self.stack = self.stack + [label]
            path = self.stack
        if tag in _WRAP_TAGS:
            for enclosing in self.stack[:-1]:
                if enclosing.split("#")[0].split(".")[0] in ("li", "td"):
                    self.inside_row.append(" > ".join(path))
                    break
            # 层数只数 div/section：main/aside/header/nav 是语义骨架，ul/li/table
            # 是内容网格 —— 用户看到的"杂乱"来自一层层白套的 div/section。
            wraps = [item for item in path if item.split("#")[0].split(".")[0] in _WRAP_TAGS]
            if len(wraps) > self.max_depth:
                self.max_depth = len(wraps)
                self.deepest = list(wraps)
            names = [item.split("#")[0].split(".")[0] for item in self.stack]
            if "main" in names:
                inside_main = names[names.index("main") + 1:]
                depth = len([name for name in inside_main if name in _WRAP_TAGS])
                self.main_depths.append(depth)
                if depth >= max(self.main_depths):
                    self.main_deepest = list(wraps[-depth:]) if depth else []

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].split("#")[0].split(".")[0] == tag:
                del self.stack[index:]
                return


def _hex_outside_tokens(css: str) -> list[str]:
    """规则体里的裸 hex（token 声明块之外、注释之外）。

    注释里那张"深色 / 浅色对照表"本来就该写满 hex —— 那是给人看的文档，
    不是规则。所以先把注释去掉，再去掉两个 token 块，剩下的才是违规的。
    """
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    body = re.sub(r":root\s*\{[^}]*\}", "", body, count=1)
    body = re.sub(r'html\[data-theme="light"\]\s*\{[^}]*\}', "", body, count=1)
    return re.findall(r"#[0-9a-fA-F]{3,8}\b", body)


def check_enterprise_layout(ui, base: str) -> None:
    print("\n[企业级 §3] 网页版的信息架构与文案")
    html = _asset_text(ui, "index.html")
    css = _asset_text(ui, "app.css")
    script = _asset_text(ui, "app.js")

    # ---- 骨架：宽屏 288px 固定侧栏，窄屏抽屉 + ☰ -------------------------
    check("侧栏宽度是 288px（与电脑端同一个数，§2.3）",
          re.search(r"--sidebar-w:\s*288px", css) is not None, "app.css")
    check("宽屏（≥721px）侧栏常驻、窄屏收成抽屉",
          "@media (min-width: 721px)" in css and "translateX(-102%)" in css, "app.css")
    check("窄屏左上角是 ☰（抽屉开关）",
          'id="btn-menu"' in html and "☰" in html and "openDrawer" in script)
    check("主区就是拖放目标（没有 .body 那一层多余包裹）",
          'id="main"' in html and "$('#main')" in script and 'id="dropzone"' not in html)

    # ---- 主区只有三种内容 ------------------------------------------------
    pages = re.findall(r'<div class="page" id="page-([a-z]+)"', html)
    check("主区只有 会话/传输/设置 三页",
          pages == ["session", "transfers", "settings"], str(pages))
    check("主区顶部那套 [会话｜传输] 分段控件已经删掉（它是第二个入口）",
          "view-seg" not in html and "view-seg" not in script)
    check("侧栏底部保留「传输」与「设置」两个导航",
          'id="nav-transfers"' in html and 'id="nav-settings"' in html)
    check("导航是唯一的页面入口（switchView 只由侧栏与设备行触发）",
          script.count("switchView(") >= 3 and "data-view=\"session\"" not in html)

    # ---- 容器层数：没有三层以上的包裹 -----------------------------------
    parser = _HtmlDepth()
    parser.feed(html.split("<body>", 1)[-1])
    check(f"包裹容器（div/section）最深 {MAX_WRAP_DEPTH} 层以内",
          parser.max_depth <= MAX_WRAP_DEPTH,
          f"{parser.max_depth} 层：" + " > ".join(parser.deepest[-4:]))
    check("主区里的包裹容器不超过 3 层",
          bool(parser.main_depths) and max(parser.main_depths) <= MAX_WRAP_DEPTH,
          f"{max(parser.main_depths or [0])} 层：" + " > ".join(parser.main_deepest[-4:]))
    check("行里（li / td）不再套 div 或 section",
          not parser.inside_row, "; ".join(parser.inside_row[:2]))
    check("卡片标题是 <h2 class=\"card-title\">（不再是 div.card-head > h2）",
          'class="card-title"' in html and "card-head" not in html)
    check("设备列表与消息列表都是 <ul>，行是 <li>",
          '<ul class="device-list" id="device-list">' in html and
          '<ul class="msg-list" id="msg-list"' in html)

    # ---- JS 引用的静态 id 必须真的存在（删元素留下的死引用会当场抛异常）--
    html_ids = set(re.findall(r'id="([^"]+)"', html))
    used = set(re.findall(r"\$\('#([A-Za-z0-9_-]+)'\)", script))
    used |= set(re.findall(r"getElementById\('([A-Za-z0-9_-]+)'\)", script))
    created = set(re.findall(r"id:\s*'([A-Za-z0-9_-]+)'", script))
    missing = sorted(used - html_ids - created)
    check("app.js 引用的每个静态 id 都在页面上", not missing, str(missing))

    # ---- 设计 token：颜色只有一处来源 -----------------------------------
    check("app.css 声明了深色画布 token（#0f0f11）", "#0f0f11" in css)
    check("app.css 声明了深色面 token（#151517）", "#151517" in css)
    wanted = ["--canvas", "--surface", "--raised", "--overlay", "--border", "--border-strong",
              "--text", "--text-muted", "--text-faint", "--accent", "--accent-fg",
              "--success", "--warning", "--danger", "--info"]
    absent = [name for name in wanted if name + ":" not in css]
    check("§2.1 的十五个 token 一个不少", not absent, str(absent))
    check("浅色一套也在（html[data-theme=\"light\"]）",
          'html[data-theme="light"]' in css and "--canvas: #ffffff" in css)
    stray = _hex_outside_tokens(css)
    check("token 声明之外不再出现裸 hex", not stray, str(stray[:6]))
    check("规则引用变量而不是写死颜色", css.count("var(--") > 200, str(css.count("var(--")))

    # ---- 文案冻结：与电脑端逐字一致 -------------------------------------
    frozen = ["设备列表", "会话", "传输", "接收", "发送", "设置", "连接手机", "扫描"]
    missing_words = [word for word in frozen if word not in html]
    check("八个冻结词都在页面上", not missing_words, str(missing_words))
    check("筛选药丸用的是 全部/进行中/已完成/失败",
          all(f'data-filter="{name}"' in html for name in ("all", "active", "done", "failed")) and
          all(word in html for word in ("全部", "进行中", "已完成", "失败")))
    check("侧栏四个入口的标签是纯文字（符号交给图标）",
          ">扫描<" in html and ">连接手机<" in html and ">群聊<" in html and
          "⟳" not in html.split('class="side-actions"')[1].split("</div>")[0])

    # ---- 一个意图一个入口 ------------------------------------------------
    check("「刷新」并进了「扫描」（页面上没有第二个扫描/刷新按钮）",
          'id="btn-refresh-devices"' not in html and html.count('id="btn-scan"') == 1)
    check("「手动添加 IP」收进扫描的下拉里",
          'id="btn-manual"' in html and 'id="scan-menu"' in html and
          html.count(">手动输入地址…</button>") == 1)
    check("二维码只有一个入口（连接手机弹窗），下拉里的重复项已删",
          'id="qr-modal"' in html and "用二维码连接" not in html and "openQr" in script)
    check("主题开关只在设置页（侧栏那颗 🌙 已删）",
          'id="theme-seg"' in html and 'id="btn-theme"' not in html and "btn-theme" not in script)
    check("PIN 输入框默认隐藏，只有对方要 PIN 时才出现",
          re.search(r'id="pin"[^>]*hidden', html) is not None and "pinNeeded" in script)
    check("「和电脑聊天」按钮已去掉（它和设备行是同一个入口）",
          "btn-new-chat" not in html and "和电脑聊天" not in html and "和电脑聊天" not in script)

    # ---- 设备列表：去重 + 省略号 + 全名 ---------------------------------
    check("设备行按 identityKey 归并（同一台设备一行）",
          "groupByIdentity" in script and "identityVariants" in script and
          "identityKey" in script)
    check("设备名用 CSS 省略号 + title 放全名",
          "text-overflow: ellipsis" in css and "title: title" in script)
    check("设备 > 6 台时才出现搜索框",
          'id="device-search"' in html and "allRows.length <= 6" in script)

    # ---- 传输页：筛选 / 搜索 / 空状态 / 失败原因 / 行动作菜单 -----------
    check("传输页有搜索框", 'id="transfer-search"' in html and "store.search" in script)
    check("两张表都有空状态节点", 'id="receive-empty-text"' in html and 'id="send-empty-text"' in html)
    check("两张表常驻 DOM，分段控件只切 class（不靠 hidden 顶开彼此）",
          "is-seg-off" in script and "is-seg-off" in css and
          'id="send-table" data-seg="send">' in html and
          'id="receive-table" data-seg="receive">' in html)
    check("空状态文案跟着筛选走", "function emptyText(" in script and "没有匹配" in script)
    check("失败行给中文原因 + 原因码",
          "failureReason" in script and "E_TIMEOUT" in script and "E_LINK" in script and
          "fail-reason" in css)
    check("失败原因不把英文原文直接摆上来（原文只在 title 里）",
          "reason.text + '（' + reason.code + '）'" in script)
    check("引擎已经给了中文原因时原样用（不再包一层「传输失败」）",
          "u4e00" in script and "return { code: 'E_FAIL', text: raw, raw: raw }" in script)
    check("行右键/长按 = 完整文件动作菜单",
          "bindRowActions" in script and "bindFileActions" in script and
          "has-menu" in script and "has-menu" in css)
    check("动作菜单里网页版该有的一样不少",
          all(word in script for word in
              ("'预览'", "'打开'", "'下载'", "'另存为…'", "'复制文件'", "'重新发送'", "'属性'")))
    check("「打开所在文件夹」写明不支持、换成「下载」",
          "网页版没有" in script and "打开所在文件夹" in script)
    check("属性弹层写明没有路径与 SHA-256",
          'id="facts-modal"' in html and ("SHA-256" in html or "SHA-256" in script))
    check("网页版做不到的事写在设置页那张卡片里",
          'id="web-limits"' in html and "打开接收文件夹" in html and "清空记录" in html)
    check("能力边界卡片里写清了失败记录去哪儿看",
          "失败记录" in html and "传输页点「失败」" in html)
    # 「来自」这一列不许整列破折号（用户原话：信息不明、不详细）。
    check("已完成那一行的「来自」有出处，不留破折号",
          "function landedSource(" in script and "var from = landedSource(file)" in script and
          "text: '—'" not in script)
    check("「来自」按证据说话：传输对端 / 分享方 / 上传记录 / 兜底本机",
          script.count("landedSource(") >= 2 and
          all(word in script for word in ("这台电脑接收目录里的文件", "电脑分享给这台手机的文件",
                                          "这台手机发过去的文件")))
    # 设备行副标题 = 地址 · 来源（端口与平台留给 PeerHeader，桌面端口径一致）。
    subtitle = script[script.index("function deviceSubtitle("):script.index("function sourceShort(")]
    check("设备行副标题不放端口/平台（端口只出现在会话顶部详情行）",
          "device.address" in subtitle and "deviceAddress(" not in subtitle and
          "tcpPort" not in subtitle and "platformName(" not in subtitle)
    check("副标题的发现来源用短词（本机 / 网页 / 局域网 / 手动 / 扫描）",
          "function sourceShort(" in script and
          all(word in script[script.index("function sourceShort("):script.index("function sourceShort(") + 900]
              for word in ("'本机'", "'网页'", "'局域网'", "'手动'", "'扫描'")))

    # ---- 服务端：手动占位行与同名设备消歧 --------------------------------
    rows, dropped = ui._drop_manual_phantoms([
        {"id": "real-1", "name": "客厅的笔记本", "address": "192.168.1.20", "tcpPort": 52117,
         "source": "mdns", "addresses": ["192.168.1.20"]},
        {"id": "manual:127.0.0.1:52117", "name": "127.0.0.1", "address": "127.0.0.1",
         "tcpPort": 52117, "source": "manual"},
        {"id": "manual:192.168.1.77:52119", "name": "另一台", "address": "192.168.1.77",
         "tcpPort": 52119, "source": "manual"},
    ])
    check("同端口的手动占位行被真实设备行吸收",
          [row["id"] for row in rows] == ["real-1", "manual:192.168.1.77:52119"], str(rows)[:120])
    check("被丢掉的是占位行自己的 id", dropped == {"manual:127.0.0.1:52117"}, str(dropped))
    names = disambiguate_names([
        {"id": "a", "name": "RealProbe", "online": True, "lastSeen": 10},
        {"id": "b", "name": "RealProbe", "online": True, "lastSeen": 5},
        {"id": "c", "name": "另一台", "online": True, "lastSeen": 1},
    ])
    check("同名设备给出编号（RealProbe / RealProbe #2）",
          sorted(names.values()) == ["RealProbe", "RealProbe #2", "另一台"], str(names))
    state = json.loads(http(base + "/api/state")[2].decode("utf-8"))
    names = [str(row.get("name") or "") for row in state.get("devices", [])]
    check("/api/state 的设备名不重复（去重 + 消歧在服务端就做完）",
          len(set(names)) == len(names), str(names)[:160])
    check("每条设备记录都带 identityKey 或 id",
          all(row.get("id") for row in state.get("devices", [])))


def check_share_download(ui, base: str, root: Path) -> None:
    """手机来取电脑发布的文件：分享表 → 下载 → Range 续传 → 完成回执。

    真机上的原始问题就是这一条链断了（电脑"发出去了"、手机 `/sdcard/Download`
    里一个文件都没有）。这里用真实 HTTP 请求把它整条走一遍：字节数、sha256、
    进度语义、完成回执、以及四种错误各自的中文说法。
    """
    print("\n[分享/下载] 手机取文件的整条路")
    source = root / "share-source.bin"
    payload = bytes(range(256)) * 512          # 128 KiB，内容可校验、不是全 0
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    added = ui.share_files([str(source)])
    check("发布一个文件进分享表", len(added) == 1 and added[0]["name"] == "share-source.bin", str(added)[:120])
    share_id = added[0]["id"]

    status, headers, body = http(base + "/api/shares")
    listed = {}
    try:
        listed = json.loads(body.decode("utf-8"))
    except ValueError:
        pass
    shares = {item["id"]: item for item in listed.get("shares", [])}
    check("/api/shares 返回分享表", status == 200 and share_id in shares, str(status))
    entry = shares.get(share_id, {})
    check("分享条目带 size / sentBytes / downloaded",
          entry.get("size") == len(payload) and entry.get("sentBytes") == 0
          and entry.get("downloaded") is False, str(entry)[:140])
    check("分享表里没有本机文件系统路径（只给不透明 id）",
          "path" not in entry and str(source) not in body.decode("utf-8", "replace"))

    # -- 完整下载：字节与 sha256 都要对得上（curl 手工验证的就是这条） --------
    status, headers, body = http(base + f"/api/share/{share_id}")
    check("完整下载拿到 200", status == 200 and len(body) == len(payload), f"{status} {len(body)}")
    check("下载到的字节与源文件逐字节一致（sha256）",
          hashlib.sha256(body).hexdigest() == digest, hashlib.sha256(body).hexdigest()[:16])
    check("响应告诉手机可以续传（Accept-Ranges）", headers.get("Accept-Ranges") == "bytes")
    disposition = headers.get("Content-Disposition", "")
    check("Content-Disposition 带 UTF-8 文件名（手机存下来名字是中文也不乱）",
          "filename*=UTF-8''" in disposition and "share-source.bin" in disposition, disposition[:120])
    # 回执（sentBytes / downloaded）是服务端**把响应写完以后**才回调记录的
    # （server.py 的 on_progress / on_complete）。客户端刚拿到 body 就立刻读，
    # 可能读到还没落地的旧值 —— 负载高、服务端线程被抢占时尤其明显。
    # 用 wait_for 等它落地：**断言内容一个都没改**（仍然要求 downloaded is True
    # 且 sentBytes 等于文件大小），只是把"无同步的立刻读"换成有界等待；
    # 等不到照样 FAIL，门禁没有被放松。
    def share_row(sid: str) -> dict:
        return next((item for item in ui.shares() if item["id"] == sid), {})

    wait_for(lambda: share_row(share_id).get("downloaded") is True
             and share_row(share_id).get("sentBytes") == len(payload), 3.0)
    after = share_row(share_id)
    check("取完之后回执是 downloaded=True", after["downloaded"] is True, str(after)[:140])
    check("取完之后 sentBytes 等于文件大小", after["sentBytes"] == len(payload), str(after)[:140])

    # -- Range 续传：分段取，进度必须是"总体取到哪了" ----------------------
    tail_file = root / "share-resume.bin"
    tail_file.write_bytes(payload)
    resume_id = ui.share_files([str(tail_file)])[0]["id"]
    half = len(payload) // 2

    status, headers, body = http(base + f"/api/share/{resume_id}", headers={"Range": f"bytes=0-{half - 1}"})
    check("Range 头拿到 206", status == 206 and len(body) == half, f"{status} {len(body)}")
    check("206 带 Content-Range", headers.get("Content-Range") == f"bytes 0-{half - 1}/{len(payload)}",
          headers.get("Content-Range", ""))
    # Range 段只有 64KiB，比服务端的 IO_CHUNK（256KiB）还小 ⇒ 整段是**一块**发完的，
    # 于是"客户端读完了、服务端那次 on_progress 还没跑"的窗口真实存在
    # （2026-10-04 的企业级验收就这么偶发红过一次：356 项里只错这一项，
    #  sentBytes 读到 0，而单跑 6 次全绿）。等它落地，断言内容不变。
    wait_for(lambda: share_row(resume_id).get("sentBytes") == half, 3.0)
    mid = share_row(resume_id)
    check("只取了一半：进度涨到一半", mid["sentBytes"] == half, str(mid)[:140])
    check("只取了一半**不算取完**（downloaded 仍为 False）",
          mid["downloaded"] is False, str(mid)[:140])

    status, headers, body = http(base + f"/api/share/{resume_id}", headers={"Range": f"bytes={half}-"})
    check("续传剩下的一半也拿到 206", status == 206 and len(body) == len(payload) - half, str(status))
    wait_for(lambda: share_row(resume_id).get("downloaded") is True
             and share_row(resume_id).get("sentBytes") == len(payload), 3.0)
    done = share_row(resume_id)
    check("续传补齐后才算取完（downloaded=True）", done["downloaded"] is True, str(done)[:140])
    check("续传后的进度是总体进度（等于文件大小，不是最后一段的大小）",
          done["sentBytes"] == len(payload), str(done)[:140])

    # 从头重下（浏览器"重新下载"就是这么发的）：进度不许倒退。
    ui.note_share_progress(resume_id, 10)
    back = next(item for item in ui.shares() if item["id"] == resume_id)
    check("进度只增不减（重试/重下不会倒退）", back["sentBytes"] == len(payload), str(back)[:140])

    # -- 错误都要说人话 ----------------------------------------------------
    status, headers, body = http(base + f"/api/share/{resume_id}", headers={"Range": f"bytes={len(payload) + 10}-"})
    message = body.decode("utf-8", "replace")
    check("要不到的 Range 回 416", status == 416, str(status))
    check("416 带 Content-Range: bytes */长度",
          headers.get("Content-Range") == f"bytes */{len(payload)}", headers.get("Content-Range", ""))
    check("416 的说明是中文（含真实长度）",
          "要不到" in message and str(len(payload)) in message, message[:120])

    status, _headers, body = http(base + f"/api/share/{resume_id}",
                                  headers={"Range": "bytes=0-9,20-29"})
    check("多段 Range 被忽略、整文件照发（不再回 416）",
          status == 200 and len(body) == len(payload), f"{status} {len(body)}")

    status, _headers, body = http(base + "/api/share/this-id-does-not-exist")
    message = body.decode("utf-8", "replace")
    check("不存在的分享 id 回 404", status == 404, str(status))
    check("分享不存在时说的是「过期 / 让电脑重发」",
          "分享列表" in message and "重新发" in message, message[:120])

    gone_file = root / "share-deleted.bin"
    gone_file.write_bytes(b"x" * 1024)
    gone_id = ui.share_files([str(gone_file)])[0]["id"]
    gone_file.unlink()
    status, _headers, body = http(base + f"/api/share/{gone_id}")
    message = body.decode("utf-8", "replace")
    check("文件被删掉时回 404", status == 404, str(status))
    check("文件被删的说法与「分享不存在」不同（指出文件本身没了）",
          "已经不在了" in message and "share-deleted.bin" in message, message[:140])

    # -- forKey：指定给某台设备的分享，别人看不见 -------------------------
    addressed = ui.share_files([str(source)], only_key="some-other-phone")
    status, _headers, body = http(base + "/api/shares")
    mine = {item["id"] for item in json.loads(body.decode("utf-8"))["shares"]}
    check("指定给别的设备的分享不出现（forKey 过滤）",
          addressed and addressed[0]["id"] not in mine, str(sorted(mine))[:140])
    check("没有指定接收方的分享所有人都能看到",
          share_id in mine, str(sorted(mine))[:140])


def check_chat_media_rides_the_share(ui, base: str, root: Path) -> None:
    """`/api/chat/media/<id>` 与分享通道**必须是同一条路**（真机 404 的回归）。

    真机现场：一条 `kind=file` 的消息 `media_rel=''`，同一份文件在分享表里，
    App 的「保存」打 `/api/chat/media/<id>` → 404，用户看到的却是"电脑说发了、
    我这儿什么都没有"。这里按真机那条数据的三步兜底各走一遍：

    1. 消息里只有 ``media_source``（绝对路径）→ 按路径找到分享；
    2. 连 ``media_source`` 都没有，只剩 名字 + 大小 → 仍然找得到；
    3. 都没有 → 404 但带 ``hint`` / ``shareId``（App 能说出下一步）。
    """
    print("\n[分享/下载] 记录通道与分享通道是同一条路")
    receive = ui.receive_dir
    payload = bytes(range(256)) * 128          # 32 KiB
    landed = os.path.join(receive, "记录通道 文件.bin")
    with open(landed, "wb") as handle:
        handle.write(payload)
    share_id = ui.share_files([landed])[0]["id"]

    conv = "d:selftest-media-share"
    # ① 真机那条数据的形状：有 media_source、media_rel 是空串
    message = ui.engine.chat.add_message(
        conv,
        kind="file",
        direction="out",
        state="sending",
        media_name="记录通道 文件.bin",
        media_rel="",
        media_size=len(payload),
        media_source=landed,
        record=False,
    )
    status, headers, body = http(base + "/api/chat/media/" + message["id"])
    check("media_rel 为空时不再 404（按 media_source 找到分享）",
          status == 200 and body == payload, f"{status} {len(body)}")
    check("走的是分享通道：字节与分享文件一致（sha256）",
          hashlib.sha256(body).hexdigest() == hashlib.sha256(payload).hexdigest())
    taken = {item["id"]: item for item in ui.shares()}.get(share_id, {})
    check("同一条路才有进度与回执：sentBytes 涨到文件大小",
          taken.get("sentBytes") == len(payload), str(taken)[:140])
    check("同一条路才有回执：downloaded=True",
          taken.get("downloaded") is True, str(taken)[:140])

    # ② 路径也丢了（老记录 / 文件挪过位置）：只剩 名字 + 大小
    second = os.path.join(receive, "只剩名字.bin")
    with open(second, "wb") as handle:
        handle.write(payload[:1024])
    second_id = ui.share_files([second])[0]["id"]
    orphan = ui.engine.chat.add_message(
        conv,
        kind="file",
        direction="out",
        state="sending",
        media_name="只剩名字.bin",
        media_rel="",
        media_size=1024,
        media_source="",
        record=False,
    )
    status, _headers, body = http(base + "/api/chat/media/" + orphan["id"])
    check("连 media_source 都没有时按 名字+大小 也能找到（1024 字节一致）",
          status == 200 and body == payload[:1024], f"{status} {len(body)}")
    check("这条也记了进度", {i["id"]: i for i in ui.shares()}[second_id]["sentBytes"] == 1024)

    # ③ 什么都没有：404 也要能照做
    missing = ui.engine.chat.add_message(
        conv,
        kind="file",
        direction="out",
        state="sending",
        media_name="根本没有的.bin",
        media_rel="",
        media_size=7,
        media_source="",
        record=False,
    )
    status, _headers, body = http(base + "/api/chat/media/" + missing["id"])
    answer = json.loads(body.decode("utf-8"))
    check("确实找不到的附件回 404", status == 404, str(status))
    check("404 的 error 是中文、说清「没有可下载的附件」",
          "没有可下载的附件" in str(answer.get("error")), str(answer)[:140])
    check("404 带可执行的下一步（hint + shareId 字段）",
          bool(answer.get("hint")) and "shareId" in answer, str(answer)[:160])
    check("老字段 error 还在（老客户端不受影响）",
          set(["ok", "error"]) <= set(answer.keys()), str(sorted(answer))[:120])

    # ④ 不存在的消息 id：同样给 hint，不再是干巴巴一句
    status, _headers, body = http(base + "/api/chat/media/no-such-message")
    answer = json.loads(body.decode("utf-8"))
    check("不存在的消息 id 也是中文 + hint",
          status == 404 and bool(answer.get("hint")), str(answer)[:140])


def check_share_events(ui, base: str) -> None:
    """发布即通知 + 取走回执：都走已有的 SSE 流（新增事件名，不动旧的）。"""
    print("\n[分享/下载] 发布即通知（SSE）")
    request = urllib.request.Request(base + "/api/events", headers={"Accept": "text/event-stream"})
    response = urllib.request.urlopen(request, timeout=15)
    frames: list[str] = []

    def reader() -> None:
        try:
            while True:
                line = response.readline()
                if not line:
                    return
                if line.startswith(b"data: ") or line.startswith(b"event: "):
                    frames.append(line.decode("utf-8", "replace").strip())
        except Exception:
            return

    thread = threading.Thread(target=reader, name="sse-share-reader", daemon=True)
    thread.start()
    try:
        # 先等 hello，确认流真的起来了（否则后面的断言会因为"流没开"而假红）。
        check("事件流先给了 hello", wait_for(lambda: any("hello" in f for f in frames), 5.0))

        source = ui.receive_dir + "/通知用文件.bin"
        with open(source, "wb") as handle:
            handle.write(b"notify" * 1024)
        added = ui.share_files([source])
        check("发布之后事件流上出现 share_added",
              wait_for(lambda: any('"kind": "share_added"' in f or '"kind":"share_added"' in f for f in frames), 5.0),
              str(frames[-3:])[:200])
        check("share_added 带上文件名（手机能直接说清是什么）",
              any("通知用文件.bin" in f for f in frames), str(frames[-3:])[:200])

        share_id = added[0]["id"]
        http(base + f"/api/share/{share_id}")
        check("取完之后事件流上出现 share_taken",
              wait_for(lambda: any("share_taken" in f for f in frames), 5.0), str(frames[-3:])[:200])
        before = len([f for f in frames if "share_taken" in f])
        ui.mark_share_downloaded(share_id)
        time.sleep(0.5)
        after = len([f for f in frames if "share_taken" in f])
        check("重复回执不会再刷一条事件", before == after, f"{before} -> {after}")
    finally:
        try:
            response.close()
        except Exception:
            pass


def main() -> int:
    _use_utf8_console()
    print("EverSend web UI self-test")
    print("=" * 62)

    check_qr_module()

    root = Path(tempfile.mkdtemp(prefix="eversend-web-selftest-"))
    engine_a: Engine | None = None
    engine_b: Engine | None = None
    ui = None
    try:
        engine_a = make_engine(root, "NodeA")
        engine_b = make_engine(root, "NodeB")
        ui = create_web_ui(engine_a, host="127.0.0.1", port=0, log_requests=False)
        port = ui.start()
        # The engine must learn the real port, or discovery would advertise a
        # URL that does not answer.
        check("the engine learns the bound web port", engine_a.info.web_port == port, f"{engine_a.info.web_port} vs {port}")
        base = f"http://127.0.0.1:{port}"

        check_https_copy_keeps_http_port(engine_a, port)
        check_apk_download(base, ui, engine_a, root)
        check_discovery_reply(engine_a)
        check_emoji_api(base)
        check_device_identity_is_stable(ui, base)
        check_one_device_one_row(ui, base)
        check_merge_keeps_conversation_title(ui, base)
        check_broken_registry_row_does_not_break_state(ui, base)
        check_mdns_answer(engine_a)
        check_media_source_stays_local(ui, base, engine_a, root)

        check_scan_reports_back(engine_a)
        check_http_surface(base, ui.token, ui)
        check_security(base, ui.token)
        check_keepalive(port, ui.token)
        check_download_range(base, engine_a)
        check_sse(base)
        check_share_handoff(ui, base, root)
        check_chat(ui, base, ui.token, root)
        check_upload(engine_a, engine_b, base, ui.token, root)
        check_offer_roundtrip(engine_a, engine_b, base, ui.token, ui)
        # 放在最后：它会注册一台"只有事件流"的手机，后面的检查不该被它影响。
        check_stream_keeps_client_online(ui, base)
        check_offline_devices_are_listed(ui)
        check_offline_devices_can_be_cleared(ui)
        check_enterprise_layout(ui, base)
        check_share_download(ui, base, root)
        check_share_events(ui, base)
        check_chat_media_rides_the_share(ui, base, root)
    finally:
        if ui is not None:
            ui.stop()
        for engine in (engine_a, engine_b):
            if engine is not None:
                engine.stop()
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 62)
    if _failures:
        print(f"{len(_failures)} of {_checks} checks FAILED:")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print(f"All {_checks} checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
