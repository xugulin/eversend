#!/usr/bin/env python3
"""手机浏览器 ↔ 桌面 的 HTTP 互传测试（不需要模拟器）。

为什么单独有这一条
------------------
``ci_android_interop.py`` 在 Linux runner 上用真模拟器跑，覆盖"页面能不能在安卓上
渲染"和"从页面点上传/下载"。但 **Windows runner 上起不了安卓模拟器**（没有嵌套
虚拟化），而 Windows 又恰恰是最需要验证的平台。所以这一条换一个角度：把**手机页面
实际发出的那套 HTTP 请求**原样打给 Windows 原生构建，逐字节比对结果。

诚实地讲清楚差别：这不是"从模拟器里点出来的"，它证明的是**手机页面依赖的 HTTP
表面在 Windows 上完全正常**——包括分块上传、Range 下载、SSE 事件流、CSRF 令牌、
路径穿越防护。渲染和触摸那部分由 Linux 上的模拟器那条流水线负责。

用法::

    python3 tools/ci_android_http.py --size-mib 8
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

def use_utf8_console() -> None:
    """让中文输出在旧版 Windows 控制台上也能活下来。

    zh-CN 的 Windows 控制台默认代码页是 936、en-US 是 1252，打印中文检查名会
    抛 UnicodeEncodeError 把整个脚本打断——CI 在 Windows runner 上就是这么挂的。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

#: What a phone actually sends.  Using the real string matters: the point is
#: to exercise the browser's request shape, not a Python client's.
ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
)

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures: list[str] = []
_evidence: list[dict] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    print(f"  {PASS if condition else FAIL} {name}" + ("" if condition else f"  {detail}"))
    _evidence.append({"check": name, "ok": bool(condition), "detail": str(detail)[:400]})
    if not condition:
        _failures.append(name)
    return bool(condition)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(url: str, *, method: str = "GET", data: bytes | None = None,
            headers: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
    """A phone-shaped request: real Chrome UA, no Python-urllib tells."""
    all_headers = {
        "User-Agent": ANDROID_UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        **(headers or {}),
    }
    req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def main() -> int:
    use_utf8_console()
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mib", type=int, default=8)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    # A fresh directory per run.  Reusing one leaves half-received ``.part``
    # files and their journals behind, and the next run then races a resume of
    # a file it never sent -- which looks exactly like a hang.
    if args.out:
        work = Path(args.out)
    else:
        work = Path(tempfile.mkdtemp(prefix="eversend-http-"))
    work.mkdir(parents=True, exist_ok=True)

    print("手机浏览器 ↔ 桌面 HTTP 互传测试")
    print("=" * 66)
    print(f"  UA: {ANDROID_UA[:70]}…")

    # ---- 起一个真实的桌面端（自带浏览器界面）----------------------------
    receive_dir = work / "recv"
    receive_dir.mkdir(parents=True, exist_ok=True)
    data_dir = work / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    web_port = free_port()
    tcp_port = free_port()

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["EVERSEND_HOME"] = str(data_dir)
    env["PYTHONIOENCODING"] = "utf-8"

    print(f"\n[0] 启动桌面端 (tcp {tcp_port}, web {web_port})")
    server = subprocess.Popen(
        [sys.executable, "-m", "eversend", "--cli", "serve",
         "--auto-accept", "--name", "CI-Desktop",
         "--port", str(tcp_port), "--receive", str(receive_dir),
         "--web", "--web-port", str(web_port), "--no-broadcast", "--no-mdns"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
    )
    base = f"http://127.0.0.1:{web_port}/"
    ready = False
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            status, _, _ = request(base + "api/state")
            if status == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(1)
    if not check("浏览器界面已就绪", ready, base):
        server.kill()
        return 1
    time.sleep(1)
    print(f"      端口 {web_port} 就绪（注意：默认端口可能被别的东西占了，这里用的是随机端口）")

    try:
        # ---- 1. 页面本身 -------------------------------------------------
        print("\n[1] 页面与状态接口")
        status, body, headers = request(base)
        text = body.decode("utf-8", "replace")
        check("GET / 返回 200", status == 200, str(status))
        check("是 HTML", "text/html" in headers.get("Content-Type", ""), headers.get("Content-Type", ""))
        check("页面含移动端视口", "viewport" in text and "width=device-width" in text)
        check("页面含应用名", "韧传" in text or "EverSend" in text)
        token_match = re.search(r'<meta\s+name="eversend-token"\s+content="([^"]+)"', text)
        token = token_match.group(1) if token_match else ""
        check("页面带 CSRF 令牌", bool(token), "没在 HTML 里找到令牌")

        status, body, _ = request(base + "api/state")
        state = json.loads(body)
        check("GET /api/state 是 JSON 且含设备块", status == 200 and "device" in state, str(list(state))[:120])

        status, body, headers = request(base + "api/qr.svg")
        check("GET /api/qr.svg 是 SVG", status == 200 and b"<svg" in body)

        # ---- 2. 手机 → 桌面 ----------------------------------------------
        print("\n[2] 手机 → 桌面（POST /api/upload，分块流式上传）")
        payload = os.urandom(args.size_mib * 1024 * 1024)
        expected = hashlib.sha256(payload).hexdigest()
        name = f"from-phone-{os.urandom(4).hex()}.bin"
        upload_url = f"{base}api/upload?name={name}&deviceId=local&address=127.0.0.1&port={tcp_port}"
        status, body, _ = request(
            upload_url, method="POST", data=payload,
            headers={
                "Content-Type": "application/octet-stream",
                "X-EverSend-Token": token,
                "Origin": base.rstrip("/"),
                "Referer": base,
            },
        )
        ok = status in (200, 202)
        check("上传被接受 (200/202)", ok, f"{status}: {body[:200]!r}")
        check("上传返回 transferId", b"transferId" in body, body[:200].decode("utf-8", "replace"))

        # Wait for the *finished* file.  Two traps here, both of which made
        # an earlier version of this script report a false failure:
        #
        #   * the upload spools the body under a hidden sub-directory first, so
        #     a directory scan finds a byte-identical copy before the transfer
        #     has even started;
        #   * a file being received exists as ``<name>.eversend.part``, and it
        #     is preallocated to the full size -- so it looks complete to
        #     anything that only checks the size, while actually being mostly
        #     zeros.
        #
        # So wait for the final name, with no ``.part`` beside it.
        final = receive_dir / name
        part = receive_dir / (name + ".eversend.part")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if final.is_file() and not part.exists():
                break
            time.sleep(2)

        landed = final if final.is_file() and not part.exists() else None
        if landed is None:
            present = sorted(p.name for p in receive_dir.iterdir()) if receive_dir.exists() else []
            print(f"      超时；接收目录内容: {present[:12]}")
        check("文件落到桌面端接收目录", landed is not None, str(final))
        if landed is not None:
            actual = hashlib.sha256(landed.read_bytes()).hexdigest()
            check("上传逐字节一致", actual == expected, f"\n      期望 {expected}\n      实得 {actual}")
            print(f"      {args.size_mib} MiB 上传完成 -> {landed.name}")

        # ---- 3. 桌面 → 手机 ----------------------------------------------
        print("\n[3] 桌面 → 手机（GET /api/download，带 Range 断点下载）")
        dl_name = landed.name if landed else name
        status, body, headers = request(f"{base}api/download?path={dl_name}")
        check("完整下载返回 200", status == 200, str(status))
        check("下载逐字节一致", hashlib.sha256(body).hexdigest() == expected)
        check("声明支持 Range", headers.get("Accept-Ranges") == "bytes", headers.get("Accept-Ranges", ""))

        half = len(payload) // 2
        status, part, headers = request(
            f"{base}api/download?path={dl_name}", headers={"Range": f"bytes={half}-"}
        )
        check("Range 请求返回 206", status == 206, str(status))
        check("Range 内容正确", part == payload[half:], f"{len(part)} 字节")
        check("Content-Range 正确",
              headers.get("Content-Range", "").startswith(f"bytes {half}-"), headers.get("Content-Range", ""))

        # ---- 4. 手机页面依赖的安全边界 ------------------------------------
        print("\n[4] 页面依赖的安全边界（手机上也要挡住）")
        status, _, _ = request(base + "api/download?path=../../etc/passwd")
        check("路径穿越被拒绝", status in (400, 403, 404, 500), str(status))
        status, _, _ = request(base + "api/upload?name=x.bin", method="POST", data=b"x")
        check("无令牌的上传被拒绝 (403)", status == 403, str(status))
        status, _, _ = request(base + "assets/../../core/engine.py")
        check("静态资源穿越被拒绝", status in (400, 403, 404), str(status))

        # ---- 5. SSE ------------------------------------------------------
        print("\n[5] SSE 事件流（页面靠它实时更新进度）")
        try:
            req = urllib.request.Request(base + "api/events", headers={"User-Agent": ANDROID_UA, "Accept": "text/event-stream"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                ctype = resp.headers.get("Content-Type", "")
                first = resp.read(256)
            check("SSE 响应类型正确", "text/event-stream" in ctype, ctype)
            check("SSE 立刻有数据", len(first) > 0, f"{len(first)} 字节")
        except Exception as exc:
            check("SSE 可连接", False, str(exc))
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()

    (work / "evidence.json").write_text(
        json.dumps({"failures": _failures, "checks": _evidence}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n" + "=" * 66)
    if _failures:
        print(f"{len(_failures)} 项失败：")
        for name_ in _failures:
            print(f"  - {name_}")
        return 1
    print(f"全部通过（{len(_evidence)} 项）。证据: {work / 'evidence.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
