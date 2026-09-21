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


def check_http_surface(base: str, token: str) -> None:
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

        check_http_surface(base, ui.token)
        check_security(base, ui.token)
        check_keepalive(port, ui.token)
        check_download_range(base, engine_a)
        check_sse(base)
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
