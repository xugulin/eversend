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

#: The page carries this header on every POST; the desktop reads it from
#: the <meta> tag it serves.
TOKEN_HEADER = "X-EverSend-Token"

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
        #: Error object from the most recent CDP call, if it returned one.
        self.last_error = None

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
        return self._open_socket(create_connection, target["webSocketDebuggerUrl"])

    def _open_socket(self, create_connection, url: str) -> bool:
        """Open the DevTools WebSocket without an ``Origin`` header.

        Chrome 111+ rejects the DevTools endpoint when the client sends an
        ``Origin`` header -- and ``websocket-client`` sends one by default,
        derived from the URL.  The refusal says so itself::

            Handshake status 403 Forbidden ... Rejected an incoming WebSocket
            connection from the http://127.0.0.1:9222 origin.

        On the desktop that is fixed with ``--remote-allow-origins``; Chrome on
        a phone takes no such flag, so the header has to go instead.
        """
        last_error: Exception | None = None
        for options in ({"suppress_origin": True}, {}):
            try:
                self.ws = create_connection(url, timeout=30, **options)
                return True
            except TypeError as exc:
                # websocket-client older than 1.7 has no ``suppress_origin``.
                last_error = exc
                continue
            except Exception as exc:
                last_error = exc
        print(f"      CDP 握手失败: {last_error}")
        body = getattr(last_error, "resp_body", b"") or b""
        if body:
            print(f"      Chrome 说: {body[:180].decode('utf-8', 'replace')}")
        return False

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
                # A CDP error is a *result* here: swallowing it once cost an
                # hour of "why is files.length still 0".
                self.last_error = data.get("error")
                return data.get("result")
        return None

    def file_input_node(self):
        """The page's ``<input type=file>``, looked up fresh.

        The page re-renders on every state poll, so a node id from an earlier
        lookup can be stale by the time it is used -- and a stale node makes
        ``DOM.setFileInputFiles`` do nothing at all.
        """
        doc = self.call("DOM.getDocument", depth=-1)
        if not doc:
            return None
        found = self.call(
            "DOM.querySelector", nodeId=doc["root"]["nodeId"], selector="input[type=file]"
        )
        return (found or {}).get("nodeId")

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


def desktop_token(host_url: str) -> str:
    """The page's CSRF token, read from the shell it serves.

    A local script has to do this too (the token is per-run and only ever
    delivered inside the page), which is exactly what makes it a real check of
    the documented path rather than of a back door.
    """
    try:
        with urllib.request.urlopen(host_url, timeout=10) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception:
        return ""
    match = re.search(r'name="eversend-token"\s+content="([^"]+)"', html)
    return match.group(1) if match else ""


def share_on_desktop(host_url: str, paths: list[str]) -> list[dict]:
    """Publish files for the phone, through the loopback-only API."""
    token = desktop_token(host_url)
    if not token:
        return []
    body = json.dumps({"paths": paths}).encode("utf-8")
    request = urllib.request.Request(
        host_url + "api/share",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", TOKEN_HEADER: token},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return json.load(resp).get("shares", [])
    except Exception as exc:
        print(f"      调用 /api/share 失败: {exc}")
        return []


def desktop_shares_state(host_url: str) -> dict[str, dict]:
    """``/api/state``'s shares, keyed by id (the desktop's own view)."""
    try:
        with urllib.request.urlopen(host_url + "api/state", timeout=10) as resp:
            return {s["id"]: s for s in json.load(resp).get("shares", [])}
    except Exception:
        return {}


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

        # The two controls the phone needs: a way to keep the link alive with
        # the screen off, and a way to hang up on purpose.
        devtools.evaluate(
            "(function(){var t=[...document.querySelectorAll('.tab')]"
            ".find(x=>/设置/.test(x.textContent));if(t)t.click();return !!t;})()"
        )
        time.sleep(1)
        keep = devtools.evaluate(
            "(function(){var c=document.querySelector('#keepalive');if(!c)return null;"
            "c.click();return {checked:c.checked,"
            "text:(document.querySelector('#keepalive-state')||{}).textContent||''};})()"
        )
        check("手机上能打开「熄屏保持连接」", bool(keep) and keep.get("checked") is True, str(keep)[:90])
        check("页面说明了保持连接的手段与代价",
              bool(keep) and ("无声" in str(keep.get("text")) or "后台" in str(keep.get("text"))),
              str(keep)[:90] if keep else "")
        check(
            "手机上有断开按钮",
            devtools.evaluate("!!document.querySelector('#btn-disconnect')") is True,
        )
        shot_settings = adb("exec-out", "screencap", "-p", binary=True, timeout=120)
        (shots / "01b-android-settings.png").write_bytes(shot_settings)
        # Back to the send view for the steps below.
        devtools.evaluate(
            "(function(){var t=[...document.querySelectorAll('.tab')]"
            ".find(x=>/发送/.test(x.textContent));if(t)t.click();return !!t;})()"
        )
        time.sleep(1)

        # And the *desktop* has to be able to see that the phone is there.
        # That is the whole point of 「手机连接」: the phone is a browser client,
        # so it never appears in the peer list, and /api/state is the only place
        # it can show up.  (Note the address looks like loopback here because
        # the emulator reaches us through `adb reverse`; the Android label is
        # what identifies it.)
        try:
            with urllib.request.urlopen(args.host_url + "api/state", timeout=10) as resp:
                host_state = json.load(resp)
        except Exception as exc:
            host_state = {}
            print(f"      读取桌面端 /api/state 失败: {exc}")
        labels = [str(c.get("label", "")) for c in host_state.get("webClients", [])]
        print(f"      桌面端看到的浏览器: {labels}")
        check("桌面端能看到连上的手机", any("Android" in label for label in labels), str(labels))

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
        # Where to leave the file so Chrome can actually read it.
        #
        # Not /sdcard/Download: since Android 11 an app cannot open arbitrary
        # paths there (scoped storage), and ``DOM.setFileInputFiles`` gives the
        # *browser* process a raw path -- which then fails silently, leaving
        # ``files.length`` at 0.  An app can always read its own external files
        # directory, so that is where the file goes.
        app_dir = "/sdcard/Android/data/com.android.chrome/files/Download"
        remote_paths = [f"{app_dir}/upload-{name}", f"/sdcard/Download/upload-{name}"]
        adb_shell(f"mkdir -p {app_dir}")
        for remote in remote_paths:
            adb("push", str(receive_dir / name), remote, timeout=180)
        probe = adb_shell(f"ls -l {remote_paths[0]}")
        check("测试文件已推进模拟器", "No such" not in probe, probe.strip())

        # The page's <input type=file> is what a person taps to pick a file.
        #
        # Picking *is* the upload here: the page queues the file and starts the
        # HTTP upload to the selected device straight away
        # (``enqueueFiles`` -> ``pump``).  The 「发送」 button belongs to the
        # other flow -- the peer-to-peer protocol transfer -- so waiting for it
        # to light up would be testing a path this page never uses for
        # uploads.  What a person sees is the upload card.
        uploaded_name = f"upload-{name}"
        node_id = devtools.file_input_node()
        if check("页面上找到文件选择框", bool(node_id)):
            shown = ""
            for remote in remote_paths:
                node_id = devtools.file_input_node()  # the page re-renders
                if not node_id:
                    break
                devtools.call("DOM.setFileInputFiles", files=[remote], nodeId=node_id)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    state = devtools.evaluate(
                        "(function(){var c=document.querySelector('#upload-card');"
                        "var b=document.querySelector('#upload-body');"
                        "return {card:!!c && !c.hidden,"
                        "text:(b?(b.innerText||b.textContent):'')||''};})()"
                    ) or {}
                    shown = str(state.get("text") or "").replace("\n", " ").strip()
                    if uploaded_name in shown or "已发送" in shown:
                        break
                    time.sleep(2)
                if uploaded_name in shown or "已发送" in shown:
                    print(f"      文件从 {remote} 进了页面：{shown[:70]}")
                    break
                print(f"      {remote} 没被页面接受（上传区显示 {shown[:60]!r}）: {devtools.last_error}")
            check("页面开始上传选中的文件", uploaded_name in shown or "已发送" in shown, shown[:80])
            shot3 = adb("exec-out", "screencap", "-p", binary=True, timeout=120)
            (shots / "03-android-upload.png").write_bytes(shot3)

        # The file we pushed into the emulator has to arrive on the desktop
        # under its own name, byte for byte.
        landed = receive_dir / uploaded_name
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not landed.exists():
            time.sleep(3)
        if check("上传的文件落到了桌面端", landed.exists(), str(landed)):
            check("上传逐字节一致", hashlib.sha256(landed.read_bytes()).hexdigest() == expected,
                  str(landed))

    # -- 3b. 聊天：手机上打字，电脑端收到 -------------------------------------
    print("\n[3b] 聊天：手机上发病消息，电脑端收到")
    if cdp_ok:
        devtools.evaluate(
            "(function(){var t=[...document.querySelectorAll('.tab')]"
            ".find(x=>/聊天/.test(x.textContent));if(t)t.click();return !!t;})()"
        )
        time.sleep(1)
        chat_text = "来自安卓手机的问候 " + time.strftime("%H:%M:%S")
        typed = devtools.evaluate(
            "(function(){var b=document.querySelector('#btn-new-chat');if(b)b.click();return !!b;})()"
        )
        check("手机页面有聊天入口", typed is True)
        time.sleep(2)
        sent = devtools.evaluate(
            "(function(){var i=document.querySelector('#chat-input');"
            "var s=document.querySelector('#btn-chat-send');if(!i||!s)return false;"
            "i.value=" + json.dumps(chat_text) + ";s.click();return true;})()"
        )
        check("手机上能输入并发送消息", sent is True)
        time.sleep(3)
        shot_chat = adb("exec-out", "screencap", "-p", binary=True, timeout=120)
        (shots / "03b-android-chat.png").write_bytes(shot_chat)

        # 电脑端的聊天库里必须出现这句话
        deadline = time.monotonic() + 30
        seen = ""
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(args.host_url + "api/chat", timeout=10) as resp:
                    data = json.load(resp)
                texts = [str(m.get("text") or "") for m in data.get("messages", [])]
                if chat_text in texts:
                    seen = chat_text
                    break
            except Exception:
                pass
            time.sleep(2)
        check("电脑端收到了手机发的消息", bool(seen), chat_text)
        devtools.evaluate(
            "(function(){var t=[...document.querySelectorAll('.tab')]"
            ".find(x=>/发送|Send/.test(x.textContent));if(t)t.click();return !!t;})()"
        )
        time.sleep(1)

    # -- 4. 电脑 → 手机（浏览器只能"拉"，所以是交接） -------------------------
    print("\n[4] 电脑 → 手机：把文件交给手机页面，再从手机里取回来")
    if cdp_ok:
        handoff_name = "desktop-to-phone.bin"
        handoff_path = work / handoff_name
        payload4 = os.urandom(4 * 1024 * 1024)
        handoff_path.write_bytes(payload4)
        expected4 = hashlib.sha256(payload4).hexdigest()

        shares = share_on_desktop(args.host_url, [str(handoff_path)])
        check("桌面端把文件交给了手机", bool(shares), str(shares)[:120])

        if shares:
            # 页面自己应该出现「电脑发来的文件」那张卡片
            shown = ""
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                state = devtools.evaluate(
                    "(function(){var c=document.querySelector('#share-card');"
                    "var l=document.querySelector('#share-list');"
                    "return {card:!!c && !c.hidden,"
                    "text:(l?(l.innerText||l.textContent):'')||''};})()"
                ) or {}
                shown = str(state.get("text") or "").replace("\n", " ").strip()
                if handoff_name in shown:
                    break
                time.sleep(2)
            check("手机上出现「电脑发来的文件」", handoff_name in shown, shown[:90])
            shot4 = adb("exec-out", "screencap", "-p", binary=True, timeout=120)
            (shots / "04-phone-handoff.png").write_bytes(shot4)

            # 点页面上的「下载」，让这台真 Chrome 去取文件
            adb_shell(f"rm -f /sdcard/Download/{handoff_name}")
            clicked = devtools.evaluate(
                "(function(){var a=[...document.querySelectorAll('#share-list a')]"
                ".find(x=>/下载/.test(x.textContent));if(a){a.click();return true;}return false;})()"
            )
            check("点到了页面上的下载按钮", clicked is True, str(clicked))

            got = work / f"pulled-{handoff_name}"
            deadline = time.monotonic() + 90
            size4 = 0
            while time.monotonic() < deadline:
                reported = adb_shell(
                    f"stat -c %s /sdcard/Download/{handoff_name} 2>/dev/null || echo 0"
                ).strip()
                size4 = int(reported) if reported.isdigit() else 0
                if size4 >= len(payload4):
                    break
                time.sleep(3)
            adb("pull", f"/sdcard/Download/{handoff_name}", str(got), timeout=180)
            if check("手机把电脑发来的文件下载完了",
                     got.exists() and got.stat().st_size == len(payload4),
                     f"{got.stat().st_size if got.exists() else 0} / {len(payload4)} 字节"):
                check("下载逐字节一致（电脑 → 手机）",
                      hashlib.sha256(got.read_bytes()).hexdigest() == expected4)
            landed_by_desktop = desktop_shares_state(args.host_url).get(shares[0]["id"], {})
            check("桌面端看到手机已经取走",
                  bool(landed_by_desktop.get("downloaded")), str(landed_by_desktop))

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
