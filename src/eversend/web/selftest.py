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

import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

if __package__ in (None, ""):  # allow "python3 src/eversend/web/selftest.py"
    _SRC = Path(__file__).resolve().parents[2]
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))

from eversend.core.engine import Engine, EngineConfig  # noqa: E402
from eversend.web import create_web_ui  # noqa: E402
from eversend.web import qr  # noqa: E402
from eversend.web.server import TOKEN_HEADER, display_address  # noqa: E402

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
    check("the phone page shows what the computer sent it", "电脑发来的文件" in html)
    check("the phone page calls the computer's folder the computer's", "电脑上的文件" in html)
    check("the CSRF token is embedded in the page", token in html)
    check("the page is never cached (it carries a token)", "no-store" in headers.get("Cache-Control", ""))

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
