#!/usr/bin/env python3
"""用面板浏览器的 CDP 拍一张指定视口的截图（不依赖 headless 模式）。

用法：
    python tools/cdp_shot.py --port 42875 --url http://127.0.0.1:53919/ \
        --width 390 --height 844 --out /tmp/x.png

为什么有这个脚本：这台机器上 `brave --headless=new --screenshot` 会挂住
（首次运行/权限/GPU 各种原因），而面板浏览器本身就是一个跑在 Xvfb 里的
Chromium，带 `--remote-debugging-port`。直接对它发 CDP 就能把视口改成手机尺寸
（`Emulation.setDeviceMetricsOverride`）再截图，比装 playwright 轻得多。

只用标准库：WebSocket 握手和帧编解码都是手写的（客户端帧必须掩码）。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import time
import urllib.request


class WebSocket:
    """够用就好的 WebSocket 客户端：文本帧、掩码、不做分片重组之外的魔法。"""

    def __init__(self, url: str, timeout: float = 20.0) -> None:
        without_scheme = url.split("://", 1)[1]
        hostport, _, path = without_scheme.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode())
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("握手期间连接被关闭")
            header += chunk
        if b"101" not in header.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"握手失败：{header.split(b'\r\n', 1)[0]!r}")
        self._buffer = header.split(b"\r\n\r\n", 1)[1]

    def send_json(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        mask = os.urandom(4)
        header = bytearray([0x81])  # FIN + text
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def _read_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("连接已关闭")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def recv_text(self) -> str:
        while True:
            first, second = self._read_exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            payload = self._read_exact(length)
            if opcode == 0x8:  # close
                raise RuntimeError("对端关闭了连接")
            if opcode in (0x1, 0x2):
                return payload.decode("utf-8", "replace")
            # ping/pong/其它：忽略

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self.send_json({"id": self._next_id(), "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = json.loads(self.recv_text())
            if message.get("id") == self._last_id:
                if "error" in message:
                    raise RuntimeError(f"{method} 失败：{message['error']}")
                return message.get("result", {})
        raise TimeoutError(f"{method} 超时")

    def _next_id(self) -> int:
        self._last_id = getattr(self, "_last_id", 0) + 1
        return self._last_id

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def new_tab(port: int, url: str = "about:blank") -> str:
    """开一个新标签页，返回它的 WebSocket 调试地址。"""
    endpoint = f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe='')}"
    request = urllib.request.Request(endpoint, method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode())["webSocketDebuggerUrl"]
    except Exception:
        # 老一点的 Chromium 只认 GET
        with urllib.request.urlopen(endpoint, timeout=15) as response:
            return json.loads(response.read().decode())["webSocketDebuggerUrl"]


def close_tab(port: int, target_id: str) -> None:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{target_id}", timeout=10).read()
    except Exception:
        pass


def stage_files(ws: WebSocket, selector: str, paths: list[str]) -> None:
    """把本地文件塞进页面的 <input type=file>（CDP 的 DOM.setFileInputFiles）。

    浏览器里没有"选文件"的自动点法（原生文件选择框机器人点不了），但这台电脑上
    的文件可以这样交给页面 —— 页面的 change 事件会照常触发，等于用户真的选了。
    """
    ws.call("DOM.enable")
    ws.call("Runtime.enable")
    result = ws.call(
        "Runtime.evaluate",
        {"expression": f"document.querySelector({selector!r})", "returnByValue": False},
    )
    object_id = (result.get("result") or {}).get("objectId")
    if not object_id:
        raise RuntimeError(f"页面上找不到 {selector}")
    ws.call("DOM.setFileInputFiles", {"files": paths, "objectId": object_id})


def evaluate(
    port: int,
    url: str,
    expression: str,
    *,
    settle: float = 2.5,
    width: int | None = None,
    height: int | None = None,
    mobile: bool = True,
    scale: float = 1.0,
) -> str:
    """打开页面、跑一段 JS、把结果打印出来（用来独立核对几何数字）。"""
    socket_url = new_tab(port, "about:blank")
    target_id = socket_url.rsplit("/", 1)[-1]
    ws = WebSocket(socket_url)
    try:
        ws.call("Page.enable")
        ws.call("Runtime.enable")
        if width and height:
            # 量手机形态时必须先改视口，否则量到的是桌面宽度。
            ws.call(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": width,
                    "height": height,
                    "deviceScaleFactor": scale,
                    "mobile": mobile,
                },
            )
        ws.call("Page.navigate", {"url": url})
        time.sleep(settle)
        result = ws.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=60.0,
        )
        value = (result.get("result") or {}).get("value")
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, indent=2)
        return str(value)
    finally:
        ws.close()
        close_tab(port, target_id)


def shoot(
    port: int,
    url: str,
    out: str,
    width: int,
    height: int,
    *,
    mobile: bool = True,
    scale: float = 2.0,
    settle: float = 2.5,
    full_page: bool = False,
    clicks: list[tuple[float, float]] | None = None,
    files: list[str] | None = None,
    file_selector: str = "#file-input",
    wait_after_files: float = 2.0,
) -> str:
    """Open ``url`` in a new tab sized ``width``x``height`` and save a screenshot."""
    import urllib.parse  # noqa: F401 - used by new_tab

    socket_url = new_tab(port, "about:blank")
    target_id = socket_url.rsplit("/", 1)[-1]
    ws = WebSocket(socket_url)
    try:
        ws.call("Page.enable")
        ws.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": scale,
                "mobile": mobile,
            },
        )
        ws.call("Page.navigate", {"url": url})
        time.sleep(settle)
        if files:
            stage_files(ws, file_selector, files)
            time.sleep(wait_after_files)
        for point in clicks or []:
            for kind in ("mousePressed", "mouseReleased"):
                ws.call(
                    "Input.dispatchMouseEvent",
                    {
                        "type": kind,
                        "x": point[0],
                        "y": point[1],
                        "button": "left",
                        "clickCount": 1,
                    },
                )
            time.sleep(0.8)
        result = ws.call(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": full_page},
            timeout=60.0,
        )
        with open(out, "wb") as handle:
            handle.write(base64.b64decode(result["data"]))
        return out
    finally:
        ws.close()
        close_tab(port, target_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True, help="浏览器的 CDP 端口")
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", help="截图输出路径（--eval 时不需要）")
    parser.add_argument("--width", type=int, default=390)
    parser.add_argument("--height", type=int, default=844)
    parser.add_argument("--scale", type=float, default=2.0)
    parser.add_argument("--settle", type=float, default=2.5)
    parser.add_argument("--desktop", action="store_true", help="按桌面模式（非 mobile）截")
    parser.add_argument("--full-page", action="store_true")
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        help="塞进页面文件选择框的本地文件（可重复），用来拍要选文件的界面（如待发方块）",
    )
    parser.add_argument("--file-selector", default="#file-input")
    parser.add_argument(
        "--eval",
        metavar="JS",
        help="不截图：打开页面后跑这段 JS 并把结果打印出来（量几何用）",
    )
    parser.add_argument(
        "--click",
        action="append",
        default=[],
        metavar="X,Y",
        help="截图前点一下这个坐标（可重复，用来切换分段控件/页面）",
    )
    args = parser.parse_args()
    if args.eval:
        print(
            evaluate(
                args.port,
                args.url,
                args.eval,
                settle=args.settle,
                width=args.width,
                height=args.height,
                mobile=not args.desktop,
            )
        )
        return 0
    path = shoot(
        args.port,
        args.url,
        args.out,
        args.width,
        args.height,
        mobile=not args.desktop,
        scale=args.scale,
        settle=args.settle,
        full_page=args.full_page,
        files=args.file or None,
        file_selector=args.file_selector,
        clicks=[
            (float(part.split(",")[0]), float(part.split(",")[1]))
            for part in args.click
        ]
        or None,
    )
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    import urllib.parse  # noqa: E402 - only needed by new_tab

    raise SystemExit(main())
