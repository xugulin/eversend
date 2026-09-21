#!/usr/bin/env python3
"""安卓 ↔ 桌面 互传测试（真机模拟器 + 真 Chrome）。

为什么要有它
------------
手机端的载体是浏览器，不是我们自己的进程，所以桌面端的测试**一行都覆盖不到它**：
页面能不能在安卓的 Chrome 上正常渲染、移动端视口下控件会不会被压扁、
``adb reverse`` 之后手机能不能真的连上来、上传/下载走的 HTTP 表面在安卓的
网络栈上是否正常——这些都只有真跑一个模拟器才知道。

测什么
------
1. **进得去**：模拟器里的 Chrome 打开页面，通过 CDP 读回 ``document.title``
   和设备列表，证明 HTML/JS/SSE 都真的跑起来了（不只是 TCP 通了）。
2. **下载（桌面 → 手机）**：让 Chrome 下载一个文件，``adb pull`` 回来逐字节比对。
   这条路径就是手机点「下载」时走的 ``GET /api/download``（带 Range）。
3. **上传（手机 → 桌面）**：往模拟器里 push 一个文件，用 CDP 的
   ``DOM.setFileInputFiles`` 把它塞进页面上的 ``<input type=file>``，
   再点「发送」——**和真人操作走的是同一条路**：页面的 XMLHttpRequest 上传到
   ``POST /api/upload``，桌面端收下并落盘，最后逐字节比对。

用法::

    python3 tools/ci_android_interop.py --workdir <临时目录>

前置：``adb`` 可用、模拟器已启动、``adb reverse`` 已把设备端口映射回宿主机。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
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

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {PASS if condition else FAIL} {name}" + ("" if condition else f"  {detail}"))
    if not condition:
        _failures.append(name)
    return condition


def adb(*args: str, timeout: int = 120, binary: bool = False):
    """Run ``adb`` and return its output.

    ``adb`` insists on CRLF even on Linux, so the text path normalises line
    endings; binary output (``exec-out``) is handed back untouched.
    """
    result = subprocess.run(["adb", *args], capture_output=True, timeout=timeout)
    if binary:
        return result.stdout
    return result.stdout.decode("utf-8", "replace").replace("\r\n", "\n")


def adb_shell(command: str, timeout: int = 120) -> str:
    return adb("shell", command, timeout=timeout)


# ---------------------------------------------------------------------------
# Chrome DevTools Protocol
# ---------------------------------------------------------------------------


class DevTools:
    """A very small CDP client, enough to drive one page.

    Chrome on Android exposes the same protocol as desktop Chrome over
    ``localabstract:chrome_devtools_remote``; ``adb forward`` turns that into a
    local TCP port.  We use it to prove the page really executed (rather than
    merely returning 200) and to place a file into the file input the way a
    person picking a photo would.
    """

    def __init__(self, port: int = 9222) -> None:
        self.port = port
        self.ws = None
        self.counter = 0

    def connect(self, timeout: float = 60.0) -> bool:
        try:
            from websocket import create_connection  # type: ignore
        except ImportError:
            print("       （缺 websocket-client，跳过 CDP 部分）")
            return False

        deadline = time.monotonic() + timeout
        target = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=5) as resp:
                    targets = json.load(resp)
                pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
                if pages:
                    target = pages[0]
                    break
            except Exception:
                pass
            time.sleep(2)
        if target is None:
            return False
        self.ws = create_connection(target["webSocketDebuggerUrl"], timeout=30)
        return True

    def call(self, method: str, **params):
        if self.ws is None:
            return None
        self.counter += 1
        message = {"id": self.counter, "method": method, "params": params}
        self.ws.send(json.dumps(message))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            raw = self.ws.recv()
            data = json.loads(raw)
            if data.get("id") == self.counter:
                return data.get("result")
        return None

    def evaluate(self, expression: str):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if not result:
            return None
        return result.get("result", {}).get("value")

    def close(self) -> None:
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# driving Chrome on the emulator
# ---------------------------------------------------------------------------

#: Chrome's first-run experience, in the languages the emulator might be in.
#: Until it is dismissed, ``am start -d <url>`` is swallowed by the welcome
#: screen: the page never opens, and the download test then finds zero bytes
#: with nothing in the log to say why.  (It took a screenshot to see it.)
_FIRST_RUN_LABELS = (
    "Use without an account",
    "不用账号即可使用",
    "Accept & continue",
    "接受并继续",
    "No thanks",
    "以后再说",
    "Got it",
    "知道了",
)

_CHROME_ACTIVITY = "com.android.chrome/com.google.android.apps.chrome.Main"


def open_url(url: str, timeout: int = 60) -> None:
    adb_shell(f"am start -a android.intent.action.VIEW -d '{url}' -n {_CHROME_ACTIVITY}", timeout=timeout)


def ui_nodes() -> list[tuple[str, tuple[int, int, int, int]]]:
    """``[(text, (x1, y1, x2, y2))]`` for everything on screen right now.

    ``uiautomator dump`` is the only view of the UI available before the
    DevTools port is up -- which is exactly the situation where we need one.
    """
    adb_shell("uiautomator dump /sdcard/window_dump.xml", timeout=90)
    xml = adb_shell("cat /sdcard/window_dump.xml", timeout=60)
    found: list[tuple[str, tuple[int, int, int, int]]] = []
    for match in re.finditer(
        r'text="([^"]*)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml
    ):
        text = match.group(1)
        if not text.strip():
            continue
        box = tuple(int(match.group(i)) for i in range(2, 6))
        found.append((text, box))  # type: ignore[arg-type]
    return found


def tap_text(labels: tuple[str, ...]) -> str:
    """Tap the first on-screen element whose text matches; returns what was tapped."""
    for text, (x1, y1, x2, y2) in ui_nodes():
        for label in labels:
            if label.lower() in text.lower():
                adb_shell(f"input tap {(x1 + x2) // 2} {(y1 + y2) // 2}")
                return text
    return ""


def dismiss_first_run(rounds: int = 3) -> list[str]:
    """Click through Chrome's welcome screens.  Returns the buttons tapped.

    Deliberately *not* done by writing ``/data/local/tmp/chrome-command-line``
    and calling ``am set-debug-app``: that combination made Chrome fail to
    start at all on the emulator (the screenshot showed the Android home
    screen, and there was no ``chrome_devtools_remote`` socket), and the flag
    file needs the program name as its first token or Chrome misparses it.
    Tapping what is actually on screen has no such failure mode.
    """
    tapped: list[str] = []
    for _ in range(rounds):
        hit = tap_text(_FIRST_RUN_LABELS)
        if not hit:
            break
        tapped.append(hit)
        time.sleep(2)
    return tapped


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def wait_for_http(url: str, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(1)
    return False


def main() -> int:
    use_utf8_console()
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--url", default="http://localhost:52119/")
    parser.add_argument("--host-url", default="http://127.0.0.1:52119/")
    parser.add_argument("--size-mib", type=int, default=6)
    parser.add_argument(
        "--receive",
        default="",
        help="桌面端的接收目录。必须和桌面进程启动时的 --receive 一致，"
        "否则上传的文件会落在别处，测试会以一句看不懂的失败收场。",
    )
    args = parser.parse_args()

    work = Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)
    shots = work / "shots"
    shots.mkdir(exist_ok=True)

    print("安卓 ↔ 桌面 互传测试")
    print("=" * 66)

    if shutil.which("adb") is None:
        print("  找不到 adb")
        return 2

    devices = adb("devices")
    print("  adb devices:\n" + "".join(f"    {line}\n" for line in devices.splitlines()[1:] if line.strip()))
    if "device" not in devices.replace("List of devices attached", ""):
        print("  没有可用的模拟器")
        return 2

    # 屏幕别睡，Chrome 会被冻住
    adb_shell("svc power stayon true")
    adb_shell("settings put global window_animation_scale 0")
    adb_shell("settings put system screen_off_timeout 1800000")

    web_up = wait_for_http(args.host_url + "api/state", timeout=60)
    check("桌面的浏览器界面在跑", web_up, args.host_url)
    if not web_up:
        return 1

    # -- 1. 进得去 ---------------------------------------------------------
    print("\n[1] 手机能不能打开页面")
    reverse = adb("reverse", "tcp:52119", "tcp:52119")
    check("adb reverse 建立成功", "error" not in reverse.lower(), reverse.strip())

    open_url(args.url)
    time.sleep(12)

    tapped = dismiss_first_run()
    if tapped:
        print("      关掉了 Chrome 首次运行的拦路屏：" + "、".join(f"「{t}」" for t in tapped))
        open_url(args.url)
        time.sleep(10)

    shot = adb("exec-out", "screencap", "-p", binary=True, timeout=120)
    (shots / "01-android-page.png").write_bytes(shot)
    check("拿到模拟器截图", len(shot) > 10_000, f"{len(shot)} 字节")

    forward = adb("forward", "tcp:9222", "localabstract:chrome_devtools_remote")
    print(f"      adb forward: {forward.strip()}")
    devtools = DevTools()
    cdp_ok = devtools.connect(timeout=60)
    if not cdp_ok:
        # Chrome may have spent the first launch on its welcome screens and
        # dropped the URL on the floor.  Open it once more before giving up.
        print("      没连上，再打开一次页面并重试")
        open_url(args.url)
        time.sleep(10)
        adb("forward", "tcp:9222", "localabstract:chrome_devtools_remote")
        cdp_ok = devtools.connect(timeout=45)
    if not check("Chrome 调试端口已连上", cdp_ok):
        # 连不上时唯一有用的信息是"套接字到底有没有"，所以直接问内核。
        sockets = adb_shell("cat /proc/net/unix | grep -i devtools || echo '（没有 devtools 套接字）'")
        print("      模拟器里的 devtools 套接字:")
        for line in sockets.strip().splitlines()[:5]:
            print(f"        {line}")
        print("      当前屏幕上的文字（前 12 条）:")
        for text, _box in ui_nodes()[:12]:
            print(f"        {text}")
    if cdp_ok:
        devtools.call("Runtime.enable")
        title = devtools.evaluate("document.title")
        check("页面标题是韧传", bool(title) and ("韧传" in str(title) or "EverSend" in str(title)), str(title))
        body = devtools.evaluate("document.body ? document.body.innerText.slice(0,400) : ''")
        check("页面正文已渲染", bool(body) and len(str(body)) > 20, str(body)[:120])
        ua = devtools.evaluate("navigator.userAgent")
        check("确实是安卓的 Chrome", "Android" in str(ua), str(ua)[:120])
        print(f"      UA: {str(ua)[:100]}")
        devtools.call("Page.enable")

    # -- 2. 桌面 → 手机 ----------------------------------------------------
    print("\n[2] 桌面 → 手机（Chrome 下载 + adb pull）")
    payload = os.urandom(args.size_mib * 1024 * 1024)
    # Put it where the web UI can serve it from: the engine's receive dir.
    receive_dir = Path(args.receive or os.environ.get("EVERSEND_RECEIVE", "") or work / "desktop-recv")
    receive_dir.mkdir(parents=True, exist_ok=True)
    name = "android-download.bin"
    (receive_dir / name).write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    adb_shell("rm -f /sdcard/Download/" + name)
    download_url = f"{args.url}api/download?path={name}"
    open_url(download_url)

    # A download finishes on its own schedule, and Chrome writes it through its
    # own download manager -- so poll for the file instead of sleeping a fixed
    # amount and hoping.  (Sleeping 15s was how this failed the first time: the
    # bytes were still in flight, and the failure looked like "0 字节".)
    pulled = work / name
    deadline = time.monotonic() + 90
    size = 0
    while time.monotonic() < deadline:
        reported = adb_shell(f"stat -c %s /sdcard/Download/{name} 2>/dev/null || echo 0").strip()
        size = int(reported) if reported.isdigit() else 0
        if size >= len(payload):
            break
        time.sleep(3)

    drawn = adb("exec-out", "screencap", "-p", binary=True, timeout=120)
    (shots / "02-android-download.png").write_bytes(drawn)
    if size < len(payload):
        # Say what the phone was showing: a permission prompt and a failed
        # download look identical from the outside otherwise.
        print("      下载没落地，屏幕上现在写着（前 10 条）:")
        for text, _box in ui_nodes()[:10]:
            print(f"        {text}")
        print("      /sdcard/Download 内容:")
        for line in adb_shell("ls -l /sdcard/Download/").strip().splitlines()[:8]:
            print(f"        {line}")

    adb("pull", f"/sdcard/Download/{name}", str(pulled), timeout=180)
    if check("下载的文件已取回", pulled.exists() and pulled.stat().st_size > 0,
             f"{pulled.stat().st_size if pulled.exists() else 0} 字节"):
        check("下载逐字节一致", hashlib.sha256(pulled.read_bytes()).hexdigest() == expected)

    # -- 3. 手机 → 桌面 ----------------------------------------------------
    print("\n[3] 手机 → 桌面（页面上的文件选择框 + 点发送）")
    if cdp_ok:
        remote = f"/sdcard/Download/upload-{name}"
        adb("push", str(receive_dir / name), remote, timeout=180)
        probe = adb_shell(f"ls -l {remote}")
        check("测试文件已推进模拟器", name in probe or "No such" not in probe, probe.strip())

        # The page's <input type=file> is what a person taps to pick a file.
        doc = devtools.call("DOM.getDocument", depth=-1)
        node_id = None
        if doc:
            found = devtools.call("DOM.querySelector", nodeId=doc["root"]["nodeId"], selector="input[type=file]")
            node_id = (found or {}).get("nodeId")
        if check("页面上找到文件选择框", bool(node_id)):
            devtools.call("DOM.setFileInputFiles", files=[remote], nodeId=node_id)
            time.sleep(1)
            count = devtools.evaluate(
                "document.querySelector('input[type=file]').files.length"
            )
            check("文件已放进选择框", count == 1, f"files.length={count}")
            # Tap the page's own send button.  Nothing here reaches into the
            # app's internals: if the button is missing or mislabelled, this
            # fails and says so, which is the point of driving a real browser.
            clicked = devtools.evaluate(
                "(function(){var b=[...document.querySelectorAll('button')]"
                ".find(x=>/发送|Send/.test(x.textContent));if(b){b.click();return true;}return false;})()"
            )
            check("找到并点击了发送按钮", clicked is True, str(clicked))
            time.sleep(20)
            shot3 = adb("exec-out", "screencap", "-p", binary=True, timeout=120)
            (shots / "03-android-upload.png").write_bytes(shot3)

        landed = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            candidates = [p for p in receive_dir.rglob("*") if p.is_file() and p.name != name]
            if candidates:
                landed = max(candidates, key=lambda p: p.stat().st_mtime)
                break
            time.sleep(3)
        if check("上传的文件落到了桌面端", landed is not None):
            check("上传逐字节一致", hashlib.sha256(landed.read_bytes()).hexdigest() == expected,
                  str(landed))

    print("\n" + "=" * 66)
    print(f"截图: {shots}")
    if _failures:
        print(f"{len(_failures)} 项失败：")
        for name_ in _failures:
            print(f"  - {name_}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
