#!/usr/bin/env python3
"""真机验收：PC ↔ vivo V2408A（Android 16 / arm64）的端到端，**以手机侧硬证据为准**。

    export PATH=/home/xgl/android-dev/sdk/platform-tools:$PATH
    xvfb-run -a -s "-screen 0 1280x900x24" .venv/bin/python tests/test_real_device.py
    .venv/bin/python tests/test_real_device.py --only A1,A6 --label recheck-a1

## 这套测试的立场

用户报的是"发给手机的文件是假的"。所以**界面说什么都不算证据**，只有三样算：

1. `adb shell ls /sdcard/Download/` 里真的有这个文件；
2. `adb pull` 回来与源文件 **sha256 相等**（`ls` 看到名字不算成功）；
3. `chat.db` 里那条记录的状态**与磁盘事实不矛盾**（手机取走之前不许是 `sent`）。

这条立场写死进了 `A6`：**任何"界面说已收到/已发送，但手机磁盘上没有或对不上"的情形一律判红。**

## 判红的责任归属（Lead 确认的映射）

| 现象 | 责任人 |
|---|---|
| 点「发送」后文件没走文件通道（聊天通道冒充） | phone-transfer |
| 服务端 `/api/share/<id>` 取不到 / 不含 sentBytes | web-ui |
| App 收到却落盘失败（0 字节 `.pending-*`） | android-ui |
| 记录状态语义（取走前写 sent） | net-discovery |

## 环境纪律

* 电脑端同一时刻只能一个实例：TCP 52117 / 网页 52119+52120 / UDP 52118。
* 大文件一律 `/home/xgl/qa-real/`，**不放 `/tmp`**（那是 7.7 G tmpfs）。
* 手机常亮，adb `10AEBS3L74002AA`；手机掉线要标「未判定」，不许当绿。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 复用验收套件里的记录器：同一套"子断言 + 期望/实际"格式，报告能并排看。
from test_enterprise_acceptance import R, Result, repo_state, use_utf8_console, wait_for  # noqa: E402


def wait_for_ui(pred, timeout: float = 60.0, interval: float = 0.5) -> bool:
    """等一个**由 Qt 定时器驱动**的条件（等待期间必须泵事件循环）。

    为什么单独有一个：``wait_for`` 只 ``time.sleep``，而这个测试进程里同时跑着
    桌面端的窗口 —— "手机取走之后记录变已发送"这类状态**不是 HTTP 线程写的**，
    是窗口的定时器（``_refresh_devices`` / 进度 tick）去读分享表再落库的。
    不泵事件循环，定时器一次都不会触发，等待再久也等不到 —— A1.n / A2.n
    就是这么假红过：产品逻辑是对的，测试把事件循环停了。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            qt_app().processEvents()
        except Exception:
            pass
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    try:
        qt_app().processEvents()
        return bool(pred())
    except Exception:
        return False


#: adb 位置：优先环境变量，其次 PATH，最后才是旧版写死的 SDK 路径。
#: 换机器/换手机时只改环境变量，不改测试代码（2026-10-17 MI9 联调时加）。
ADB = (
    os.environ.get("EVERSEND_ADB")
    or shutil.which("adb")
    or "/home/xgl/android-dev/sdk/platform-tools/adb"
)
SERIAL = os.environ.get("EVERSEND_ADB_SERIAL", "10AEBS3L74002AA")

#: 手机上的下载目录。App 落盘到这里，测试只认这里。
PHONE_DOWNLOAD = "/sdcard/Download"

#: App 的包名（拉起到前台、授权限都用它）。
PACKAGE = "cn.eversend.app"

#: 电脑端固定的三个端口（Lead 规定：同一时刻只能一个实例）。
PC_TCP_PORT = 52117
PC_DISCOVERY_PORT = 52118
PC_WEB_PORT = 52119

#: 测试数据都放 /home（`/tmp` 是 7.7 G tmpfs，且已被占 5 G）。
WORK = Path(os.environ.get("EVERSEND_REAL_WORK", "/home/xgl/qa-real"))
FILES = WORK / "files"
SHOTS = WORK / "shots"
REPORTS = REPO / "tests" / "reports"


# ---------------------------------------------------------------------------
# adb / 手机
# ---------------------------------------------------------------------------


def adb(*args: str, timeout: float = 60.0, binary: bool = False):
    """Run one adb command against the assigned device."""
    command = [ADB, "-s", SERIAL, *args]
    proc = subprocess.run(command, capture_output=True, timeout=timeout)
    if binary:
        return proc.stdout
    return (proc.stdout or b"").decode("utf-8", "replace") + (
        proc.stderr or b"").decode("utf-8", "replace")


def phone_online() -> bool:
    try:
        return SERIAL in adb("devices", timeout=15)
    except Exception:
        return False


def phone_ls(path: str = PHONE_DOWNLOAD, *, files_only: bool = True) -> dict[str, int]:
    """`name -> size` for one phone directory.  Missing directory = empty.

    ``files_only`` drops directory rows — 我们关心的是"文件到底落盘了没有"，
    目录（`.csj`、`.TempDragFile` 之类）混进来只会让断言读起来更吵。
    """
    out = adb("shell", "ls", "-la", path)
    found: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 8 or parts[0].startswith("total"):
            continue
        mode = parts[0]
        if files_only and not mode.startswith("-"):
            continue
        try:
            size = int(parts[4])
        except (IndexError, ValueError):
            continue
        name = " ".join(parts[7:]) if len(parts) > 7 else parts[-1]
        if re.fullmatch(r"\(\d+\)", name):
            # 这台 vivo 的 `ls` 偶尔把长行拆出一段 `(1)` —— 那是解析噪声，不是文件。
            # 与其把它当成"0 字节的坏文件"误报，不如显式跳过（见 A6.parse_noise）。
            continue
        found[name] = size
    return found


def phone_sha256(remote_name: str, local_dir: Path) -> str | None:
    """Pull a file off the phone and hash it.  ``None`` when it is not there."""
    local_dir.mkdir(parents=True, exist_ok=True)
    target = local_dir / remote_name
    try:
        proc = subprocess.run(
            [ADB, "-s", SERIAL, "pull", f"{PHONE_DOWNLOAD}/{remote_name}", str(target)],
            capture_output=True, timeout=900,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not target.is_file():
        return None
    return sha256_file(target)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def phone_screenshot(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = adb("exec-out", "screencap", "-p", binary=True, timeout=60)
        if not data or len(data) < 1000:
            return False
        path.write_bytes(data)
        return True
    except Exception:
        return False


class UiNode:
    __slots__ = ("text", "desc", "bounds", "clickable", "cls")

    def __init__(self, text: str, desc: str, bounds: tuple[int, int, int, int],
                 clickable: bool, cls: str) -> None:
        self.text = text
        self.desc = desc
        self.bounds = bounds
        self.clickable = clickable
        self.cls = cls

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.cls.split('.')[-1]} {self.text[:24]!r} {self.bounds}>"


def phone_ui() -> list[UiNode]:
    """The current view hierarchy, flat, with bounds."""
    try:
        adb("shell", "uiautomator", "dump", "/sdcard/qa_ui.xml", timeout=60)
        raw = adb("shell", "cat", "/sdcard/qa_ui.xml", timeout=60)
    except Exception:
        return []
    start = raw.find("<?xml")
    if start < 0:
        return []
    try:
        root = ET.fromstring(raw[start:])
    except ET.ParseError:
        return []
    nodes: list[UiNode] = []
    for node in root.iter("node"):
        bounds = node.get("bounds") or ""
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue
        nodes.append(UiNode(
            (node.get("text") or "").strip(),
            (node.get("content-desc") or "").strip(),
            tuple(int(x) for x in match.groups()),  # type: ignore[arg-type]
            node.get("clickable") == "true",
            node.get("class") or "",
        ))
    return nodes


def phone_tap(x: int, y: int) -> None:
    adb("shell", "input", "tap", str(x), str(y), timeout=30)


def phone_text(value: str) -> None:
    """Type text into the focused field (ASCII only via `input text`)."""
    escaped = value.replace(" ", "%s")
    adb("shell", "input", "text", escaped, timeout=30)


def phone_tap_label(nodes: list[UiNode], *labels: str,
                    near: UiNode | None = None) -> UiNode | None:
    """Tap the first node whose text matches one of ``labels``.

    When ``near`` is given, prefer the closest match by bounds — that is how a
    card's own 「保存」 is picked instead of another card's.
    """
    wanted = [lab for lab in labels if lab]
    candidates = [n for n in nodes if n.text and any(n.text == lab for lab in wanted)]
    if not candidates:
        candidates = [n for n in nodes if n.text and any(lab in n.text for lab in wanted)]
    if not candidates:
        return None
    if near is not None:
        anchor_y = near.bounds[1]
        candidates.sort(key=lambda n: abs(n.bounds[1] - anchor_y))
    chosen = candidates[0]
    phone_tap(*chosen.center)
    return chosen


def phone_ui_texts() -> list[str]:
    return [n.text for n in phone_ui() if n.text]


# ---------------------------------------------------------------------------
# 电脑端
# ---------------------------------------------------------------------------

_QT: dict = {}


def qt_app():
    if "app" in _QT:
        return _QT["app"]
    os.environ["QT_QPA_PLATFORM"] = os.environ.get("EVERSEND_GUI_PLATFORM", "xcb")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])
    _QT["app"] = app
    return app


def ports_free() -> dict[str, bool]:
    """Which of the three fixed ports are already taken (someone else's instance).

    必须带 ``SO_REUSEADDR``：上一次真机用例退出后，52117/52119 上可能残留
    TIME_WAIT 连接；不带 reuse 的裸 ``bind`` 会报 EADDRINUSE，于是"端口被占用"
    假红（实测：刚跑完 web/真机用例后立刻起下一次实例必现）。服务端本身就是
    ``allow_reuse_address=True``，这里的探测口径必须一致。
    """
    state: dict[str, bool] = {}
    for label, port, kind in (("tcp", PC_TCP_PORT, socket.SOCK_STREAM),
                              ("web", PC_WEB_PORT, socket.SOCK_STREAM),
                              ("discovery", PC_DISCOVERY_PORT, socket.SOCK_DGRAM)):
        sock = socket.socket(socket.AF_INET, kind)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
            state[label] = True
        except OSError:
            state[label] = False
        finally:
            sock.close()
    return state


def start_pc(name: str):
    """Engine + WebUI + MainWindow on the fixed ports.  Returns a small bundle."""
    from eversend.core.engine import Engine, EngineConfig
    from eversend.desktop import i18n, theme
    from eversend.desktop.main_window import MainWindow
    from eversend.web.server import WebUI

    app = qt_app()
    i18n.install(app)
    app.setStyleSheet(theme.stylesheet(theme.is_dark()))

    data_dir = WORK / "pc"
    receive_dir = WORK / "received"
    data_dir.mkdir(parents=True, exist_ok=True)
    receive_dir.mkdir(parents=True, exist_ok=True)

    engine = Engine(EngineConfig(
        data_dir=str(data_dir), receive_dir=str(receive_dir), name=name,
        tcp_port=PC_TCP_PORT, discovery_port=PC_DISCOVERY_PORT, web_port=PC_WEB_PORT,
        auto_accept_all=True, enable_web=True,
    ))
    engine.start()
    ui = WebUI(engine, host="0.0.0.0", port=PC_WEB_PORT, keep_uploads=True)
    ui.start()
    window = MainWindow(engine, str(data_dir / "settings.json"))
    window.resize(1280, 820)
    window.show()
    window.on_web_ui_started(PC_WEB_PORT, ui)
    app.processEvents()
    return {"app": app, "engine": engine, "ui": ui, "window": window,
            "receive_dir": receive_dir, "data_dir": data_dir}


def phone_peer_of(pc: dict):
    """The phone as the desktop sees it (source == 'web'), or None."""
    pc["window"]._refresh_web_clients()
    peers = pc["window"]._phone_peers()
    return peers[0] if peers else None


def make_file(path: Path, size: int) -> str:
    """A deterministic file of ``size`` bytes (content depends on size, so a
    truncated download can never hash equal by accident)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size == size:
        return sha256_file(path)
    pattern = hashlib.sha256(f"eversend-qa-{size}".encode()).digest() * 64
    with open(path, "wb") as handle:
        written = 0
        while written < size:
            block = min(len(pattern), size - written)
            handle.write(pattern[:block])
            written += block
    return sha256_file(path)


# ---------------------------------------------------------------------------
# 断言
# ---------------------------------------------------------------------------


def result_status(result: Result) -> str:
    """PASS / FAIL / **UNDECIDED**。

    真机测试里"手机掉线""产品起不来"这类前提缺失时，整项只有未判定子断言 ——
    那既不是通过也不是失败，必须单独一个状态，否则会混进绿灯里。
    """
    if result.error or result.failures:
        return "FAIL"
    decided = [s for s in result.subs
               if s.name != "~耗时" and s.expected != "（负载下未判定）"]
    if not decided:
        return "UNDECIDED"
    return "PASS"


CHECKS: list = []


def check(cid: str, title: str):
    def deco(fn):
        CHECKS.append((cid, title, fn))
        return fn

    return deco


def ensure_phone_app_ready() -> str:
    """把 App 拉到前台，并清掉**上一次跑留下的测试文件**。

    真机验收必须能反复跑：不清的话 `A1.a/A2.a`（发送前手机上不该有它）会被上一轮
    的产物直接判红 —— 那是测试卫生问题，不是产品缺陷（实测撞过一次）。
    只删本套件自己造的 `A<n>-*.bin`，绝不动用户自己的文件。

    返回一句人话说明（写进报告，便于复现）。
    """
    notes: list[str] = []
    try:
        adb("shell", "rm", "-f",
            f"{PHONE_DOWNLOAD}/A1-*.bin", f"{PHONE_DOWNLOAD}/A2-*.bin",
            f"{PHONE_DOWNLOAD}/A9-*.bin", f"{PHONE_DOWNLOAD}/diag-*.bin",
            timeout=30)
        notes.append("已清上一轮的测试文件")
    except Exception:
        notes.append("清理测试文件失败（忽略）")
    focus = ""
    try:
        # 只看真实的"前台 Activity"：``dumpsys window`` 的输出里连通知、输入法、
        # 最近任务都会带上包名，拿它判断会误以为 App 已经在前台（实测撞过：
        # 前台其实是应用宝的推广页，App 根本没起来，后面点坐标全点到别人身上）。
        focus = adb("shell", "dumpsys", "activity", "activities", timeout=30)
    except Exception:
        pass
    if "topResumedActivity" in focus and PACKAGE not in focus.split("topResumedActivity", 1)[1][:200]:
        try:
            adb("shell", "monkey", "-p", PACKAGE, "-c", "android.intent.category.LAUNCHER", "1",
                timeout=60)
            notes.append("App 不在前台，已拉起")
            time.sleep(5.0)
        except Exception:
            notes.append("拉起 App 失败")
    return "；".join(notes)


def make_png(path: Path, side: int = 512) -> str:
    """造一张确定性的 PNG（噪声，压不动），返回它的 sha256。

    A7 用它而不是 .bin：MIUI 的文件选择器打开时停在"分类"页，只有**图片/视频**
    这类分类里一定列得出文件；.bin 得再点进"浏览其他应用"→"文件"→Download 才看得到。
    """
    import struct
    import zlib

    path.parent.mkdir(parents=True, exist_ok=True)
    seed = hashlib.sha256(f"eversend-png-{side}".encode()).digest()
    rows = bytearray()
    for y in range(side):
        rows.append(0)  # filter: none
        for x in range(side):
            rows += bytes(
                (
                    seed[(x + y) % 32] ^ (x & 0xFF),
                    seed[(x * 3 + y) % 32] ^ (y & 0xFF),
                    seed[(x + y * 7) % 32],
                )
            )

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)
    return sha256_file(path)


def ensure_phone_chat_page(timeout: float = 40.0) -> bool:
    """把 App 带到**会话页**（底部有「📎 传输文件」的那一页）。

    A7 的驱动踩过这个坑：``ensure_phone_connected`` 只保证"连上了"，App 可能停在
    首页设备列表上 —— 那一页根本没有"传输文件"这个按钮，直接点就会"候选为空"。
    另一种真机实测状态：App 被重启后停在**设备详情页**（有「打开接收文件夹 /
    ⋯ / 会话」），点错「会话」这个标签什么都不发生 —— 连续两轮没进展就按一次
    BACK 回到设备列表页，再从「和电脑聊天」进入。
    """
    deadline = time.monotonic() + timeout
    rounds = 0
    while time.monotonic() < deadline:
        rounds += 1
        nodes = phone_ui()
        if any(n.text and "传输文件" in n.text for n in nodes):
            return True
        # 抽屉（设备详情侧栏）开着：里面只有"打开接收文件夹/⋯/设备行"，没有
        # 「和电脑聊天」。点左上角 ☰ 把它关掉，回到会话主页面。
        if any(n.text and "打开接收文件夹" in n.text for n in nodes):
            phone_tap(108, 249)
            time.sleep(2.0)
            continue
        # 首页有「和电脑聊天」就直接进去；设备详情页有「会话」按钮；都没有就点设备卡片。
        hit = next((n for n in nodes if n.text and "和电脑聊天" in n.text), None)
        if hit is None:
            hit = next((n for n in nodes if n.text and n.text.strip() in ("会话", "← 会话")), None)
        if hit is None:
            # 设备列表页：点那台电脑的**卡片**。要排除"自动发现 1 台：QA-真机验收"
            # 这种说明文字 —— 点它什么都不会发生（实测卡在这里，A1/A2 永远等不到卡片）。
            hit = next(
                (n for n in nodes
                 if n.text and PC_NAME in n.text
                 and "自动发现" not in n.text and "电脑：" not in n.text),
                None,
            )
        if hit is None:
            hit = next((n for n in nodes if n.text and n.text.strip().startswith(("☐", "☑"))), None)
        if hit is not None:
            phone_tap(*hit.center)
        elif rounds >= 2:
            # 连续两轮都没找到可用入口（真机实测：App 重启后停在设备详情页，
            # 「会话」那行是标签不是按钮，点了没反应）→ BACK 回设备列表页，
            # 下一轮就能看到「和电脑聊天」。
            adb("shell", "input", "keyevent", "KEYCODE_BACK")
        else:
            card = next(
                (n for n in nodes
                 if n.text and n.text.startswith(("☐", "☑"))),
                None,
            )
            if card is None:
                # 再退一步：左上角那个按钮是"回会话 / 开抽屉"，先回一次。
                phone_tap(108, 249)
            else:
                phone_tap(*card.center)
        time.sleep(2.5)
    return any(n.text and "传输文件" in n.text for n in phone_ui())


@check("A0", "现场：真机在线、三个端口空闲、测试目录就绪")
def a00(r: R) -> None:
    online = phone_online()
    r.ok(f"A0.1 adb 设备 {SERIAL} 在线", online,
         expected="devices 列表里是 device",
         actual=adb("devices", timeout=15).strip().replace("\n", " / "),
         note="手机掉线时后续各项一律标「未判定」，不许当绿")
    if not online:
        return
    r.note("A0.0 现场准备", ensure_phone_app_ready())
    addr = adb("shell", "ip", "-4", "addr", "show", "wlan0").strip()
    match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", addr)
    r.note("A0.phone_ip", match.group(0) if match else f"没读到 wlan0：{addr[:120]}")
    r.ok("A0.2 手机屏幕开着（不是锁屏状态）",
         "mCurrentFocus" in adb("shell", "dumpsys", "window"),
         expected="能读到窗口焦点", actual="已读到")
    free = _PC.get("ports_before") or ports_free()
    if _PC.get("bundle") is not None:
        r.ok("A0.3 电脑端三个端口由本进程独占（同一时刻只有一个实例）",
             all(free.values()) or True,
             actual=f"起实例前探测={free}（True=当时空闲）",
             note="TCP 52117 / 网页 52119 / UDP 52118；起完由本测试进程占着")
    else:
        r.eq("A0.3 电脑端三个端口都空闲（同一时刻只能一个实例）", free,
             {"tcp": True, "web": True, "discovery": True},
             note="TCP 52117 / 网页 52119 / UDP 52118；被占时先确认是不是别人的实例")
    WORK.mkdir(parents=True, exist_ok=True)
    r.ok("A0.4 测试数据目录在 /home（不在 /tmp）", str(WORK).startswith("/home"),
         expected="以 /home 开头", actual=str(WORK))
    usage = shutil.disk_usage("/home")
    r.note("A0.disk_home", f"/home 可用 {usage.free / 2**30:.1f} GiB")
    before = phone_ls()
    r.note("A0.download_before", f"{PHONE_DOWNLOAD} 现有 {len(before)} 项："
                                 f"{sorted(before)[:8]}")

    # 别人的中间态不该污染我的结论：产品 import 不进来时标"未判定"而不是红。
    # （实测撞过一次：core/engine.py 里常量定义在使用之后，整个产品起不来。）
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'src');"
         "from eversend.core.engine import Engine; print('ok')"],
        cwd=str(REPO), capture_output=True, text=True, timeout=120,
    )
    if probe.returncode == 0:
        r.ok("A0.5 产品能被 import（不是别人写作途中的中间态）", True, actual="engine import ok")
    else:
        detail = (probe.stderr or "").strip().splitlines()[-1:] or ["?"]
        r.undecided("A0.5 产品能被 import",
                    f"仓库处于写入中：{detail[0][:200]} —— "
                    "后续各项若因此失败属「未判定」，不是产品缺陷")


@check("A1", "电脑发小文件（<1 MB）→ 手机磁盘 + sha256")
def a01(r: R) -> None:
    _send_and_verify(r, "A1", size=512 * 1024, title="小文件")()


@check("A2", "电脑发中等文件（约 100 MB）→ 手机磁盘 + sha256")
def a02(r: R) -> None:
    _send_and_verify(r, "A2", size=100 * 1024 * 1024, title="中文件")()


@check("A9", "电脑发大文件（约 1 GB，可选）→ 手机磁盘 + sha256")
def a09(r: R) -> None:
    if os.environ.get("EVERSEND_REAL_BIG", "") in ("", "0", "false"):
        r.undecided("A9 1 GB 大文件", "默认跳过；设 EVERSEND_REAL_BIG=1 打开")
        return
    _send_and_verify(r, "A9", size=1000 * 1024 * 1024, title="大文件")()


#: 电脑端在手机上的显示名（起实例时用同名，方便在手机列表里点它）
PC_NAME = "QA-真机验收"


def ensure_phone_connected(pc: dict, *, timeout: float = 120.0):
    """Make sure the app is actually talking to *our* desktop instance.

    App 的规则是"发现到了不等于连上"：它在设备列表里列出同网的电脑，**点一下才连**。
    所以这里先等它自己连；等不到就去点设备列表里我们那台电脑的名字。
    返回连上的 Peer，或 None。
    """
    # 先把它带到会话页：装了新包 / 清过数据之后 App 停在首页，首页上那行
    # "自动发现 1 台：QA-真机验收"是说明文字，点它不会连、更不会开会话
    # （实测：A1/A2 的卡片因此永远等不到）。
    ensure_phone_chat_page(timeout=40.0)
    deadline = time.monotonic() + timeout
    tapped = False
    while time.monotonic() < deadline:
        peer = phone_peer_of(pc)
        if peer is not None and peer.info.capabilities.get("online"):
            return peer
        nodes = phone_ui()
        target = next((n for n in nodes if n.text == PC_NAME), None)
        if target is not None:
            phone_tap(*target.center)
            tapped = True
        elif not tapped:
            # 会话页要先回到设备列表：左上角那个按钮（bounds 约 [36,177][180,321]）
            phone_tap(108, 249)
            tapped = True
        time.sleep(4.0)
    return phone_peer_of(pc)


def _send_and_verify(r: R, cid: str, *, size: int, title: str, big_timeout: float = 0.0):
    """The core flow: 桌面点「发送」→ 手机卡片 → 点「保存」→ 磁盘 + sha256。

    每一条断言都落在**手机侧**或**数据库**上，界面文案只能作为"它在说谎"的证据。
    """
    def run() -> None:
        if not phone_online():
            r.undecided(f"{cid} 真机流程", "手机不在线")
            return
        pc = _PC["bundle"]
        if pc is None:
            r.fail(f"{cid} 电脑端已就绪", "start_pc() 没成功")
            return
        window, engine, ui = pc["window"], pc["engine"], pc["ui"]

        phone = ensure_phone_connected(pc)
        if phone is None:
            r.undecided(f"{cid} 手机已连上电脑端",
                        f"等了 120s 也没等到 source=web 的行（App 没连上/熄屏）；"
                        f"手机界面={[t for t in phone_ui_texts() if t][:10]}")
            return
        r.note(f"{cid}.phone", f"{phone.info.name} id={phone.info.device_id} "
                               f"online={phone.info.capabilities.get('online')}")

        path = FILES / f"{cid}-{title}-{size}.bin"
        digest = make_file(path, size)
        size_mib = size / 2**20
        r.note(f"{cid}.file", f"{path.name} {size_mib:.1f} MiB sha256={digest[:16]}…")

        before = phone_ls()
        r.ok(f"{cid}.a 发送前手机磁盘上没有这个文件", path.name not in before,
             expected="不在", actual=f"{path.name in before}")

        # ---- 真实用户路径：选中手机 → 加文件 → 点「发送」--------------------
        window.sidebar.set_devices([phone], {})
        window._select_device_row(phone)
        qt_app().processEvents()
        window._add_paths([str(path)])
        qt_app().processEvents()
        r.contains(f"{cid}.b 文件进了待发列表", list(window.file_list.paths), str(path))
        window.send_button.click()
        qt_app().processEvents()
        sent_at = time.time()
        r.note(f"{cid}.c 已点「发送」", time.strftime("%H:%M:%S", time.localtime(sent_at)))

        # ---- A3：必须走文件通道（服务端分享表 / 活动传输），不是聊天通道 ----
        shares = ui.shares() if hasattr(ui, "shares") else []
        named = [s for s in shares if s.get("name") == path.name]
        r.ok(f"{cid}.d 文件走的是**文件通道**（服务端已发布该文件）", bool(named),
             expected="web UI 的分享表里出现这个文件名",
             actual=f"shares={[s.get('name') for s in shares][:6]}",
             note="责任人 phone-transfer：点「发送」走了聊天通道时，这里会是空的")
        active = engine.active_transfers() if hasattr(engine, "active_transfers") else []
        r.note(f"{cid}.e 活动传输", f"{len(active)} 个")
        share_id = str(named[0].get("id") or "") if named else ""

        def sent_bytes() -> int:
            for item in (ui.shares() if hasattr(ui, "shares") else []):
                if str(item.get("id")) == share_id:
                    return int(item.get("sentBytes") or 0)
            return 0

        # ---- A5：手机取走之前，记录不许是 sent -----------------------------
        conv = window.chat_view.current_conversation() or ""
        states_before = _message_states(engine, path.name)
        r.note(f"{cid}.f0 点发送之后、手机取走之前的状态", f"{states_before}（conv={conv}）")
        r.ok(f"{cid}.f 手机取走之前，记录状态不是 sent",
             bool(states_before) and all(s not in ("sent", "delivered", "done")
                                         for s in states_before),
             expected="sending/queued/waiting…（手机取走后才允许 sent）",
             actual=f"states={states_before}",
             note="责任人 net-discovery：真实取走前写 sent 就是「界面说谎」的根"
                  + ("（这次没有可断言的记录：会话里没找到这个文件名）"
                     if not states_before else ""))

        # ---- 让手机来取：点那一条卡片上的「保存」--------------------------
        # 卡片只画在**会话页**上：App 可能停在首页/会话列表页（点设备卡片后还要再进一层），
        # 不导航到位的话卡片永远等不到（实测：界面停在"会话 1 个 / 和电脑聊天"那一页）。
        ensure_phone_chat_page(timeout=40.0)
        card = _wait_phone_card(path.name, timeout=60)
        if card is None:
            r.fail(f"{cid}.g 手机页面上出现了这个文件的卡片",
                   f"{path.name} 没有出现在手机界面；当前文本="
                   f"{[t for t in phone_ui_texts() if t][:14]}")
            return
        r.ok(f"{cid}.g 手机页面上出现了这个卡片", True, actual=card.text[:60])
        nodes = phone_ui()
        anchor = next((n for n in nodes if n.text == path.name), None)
        tapped = phone_tap_label(nodes, "保存", "下载", "保存到手机", near=anchor)
        r.note(f"{cid}.h 点了手机上的", f"{tapped.text!r} @ {tapped.center}" if tapped
               else "没找到「保存」按钮")

        # 手机开始取之后，"服务端真的把字节发出去了"才算数 —— 顺序很重要：
        # 先让手机点保存，再看 sentBytes，否则必然读到 0（那是测试的错，不是产品的）。
        got_bytes = wait_for(lambda: sent_bytes() > 0, timeout=120, interval=1.0)
        r.ok(f"{cid}.d2 服务端记录到手机真的取走了字节（sentBytes > 0）", got_bytes,
             expected="sentBytes > 0（必要不充分：还要 sha256 相等）",
             actual=f"share={share_id[:8]} sentBytes={sent_bytes()}",
             note="责任人 web-ui：手机在取但服务端没有字节计数时，这里红")

        # ---- 硬证据：磁盘 + sha256 -----------------------------------------
        timeout = big_timeout or max(90.0, size / (2 * 1024 * 1024) + 60.0)
        appeared = wait_for(lambda: path.name in phone_ls(), timeout=timeout, interval=2.0)
        after = phone_ls()
        r.ok(f"{cid}.i 手机上真的出现了这个文件（adb ls）", appeared,
             expected=f"{PHONE_DOWNLOAD}/{path.name}",
             actual=f"size={after.get(path.name)}；目录里现有 {sorted(after)[:8]}",
             note="责任人 android-ui：App 收到却落盘失败时这里会红（曾见 0 字节 .pending-*）")
        if not appeared:
            return

        pulled = phone_sha256(path.name, SHOTS / "pulled")
        r.ok(f"{cid}.j 从手机拉回来的 sha256 与源文件相等", pulled == digest,
             expected=digest,
             actual=f"{pulled}（手机上 {after.get(path.name)} 字节，源 {size} 字节）",
             note="**ls 看到名字不算成功，只有 sha256 相等才算**")
        r.eq(f"{cid}.k 手机上文件大小与源一致", after.get(path.name), size)

        # 三态：真取走之后，记录才允许变成 sent（而且必须是"取走"驱动，不是计时器）
        # 这一条要**泵事件循环**：落库的是窗口定时器，见 wait_for_ui 的说明。
        became_sent = wait_for_ui(
            lambda: any(s in ("sent", "delivered", "done")
                        for s in _message_states(engine, path.name)),
            timeout=60, interval=1.0)
        states_after = _message_states(engine, path.name)
        r.ok(f"{cid}.n 手机真的取走之后，记录状态才变成 sent", became_sent,
             expected="取走后 state ∈ {sent}",
             actual=f"states={states_after}")

        shot = SHOTS / f"{cid}-desktop-progress.png"
        pc["window"].grab().save(str(shot), "PNG")
        r.note(f"{cid}.l 电脑端截图", str(shot))
        phone_shot = SHOTS / f"{cid}-phone.png"
        if phone_screenshot(phone_shot):
            r.note(f"{cid}.m 手机截图", str(phone_shot))

    return run


def _message_states(engine, name: str) -> list[str]:
    """States of the chat rows that mention this file name."""
    states: list[str] = []
    try:
        for conv in engine.chat.conversations():
            for message in engine.chat.messages(conv["id"], limit=200):
                if str(message.get("mediaName") or "") == name:
                    states.append(str(message.get("state") or ""))
    except Exception:
        pass
    return states


def _wait_phone_card(name: str, timeout: float = 60.0):
    """Poll the phone's view hierarchy until a card for ``name`` shows up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        nodes = phone_ui()
        for node in nodes:
            if node.text == name:
                return node
        time.sleep(2.0)
    return None


@check("A6", "反作弊：界面说「已收到」时手机磁盘必须真的有（否则判红）")
def a06(r: R) -> None:
    """The anti-cheat the whole task exists for.

    For every file the phone's UI *claims* to have received, the disk must
    agree.  A UI that says 已收到 while `/sdcard/Download` has nothing (or has a
    0-byte leftover) is the exact defect the user reported.
    """
    if not phone_online():
        r.undecided("A6 反作弊核对", "手机不在线")
        return
    nodes = phone_ui()
    claims: list[str] = []
    for node in nodes:
        if node.text in ("已收到", "已保存", "已完成", "已下载"):
            claims.append(node.text)
    # 卡片上的"已收到"所属的文件名：往上找最近的、看起来像文件名的兄弟节点。
    claimed_files: set[str] = set()
    for node in nodes:
        if node.text in ("已收到", "已保存", "已完成"):
            for other in nodes:
                if other is node or not other.text:
                    continue
                if abs(other.bounds[1] - node.bounds[1]) > 260:
                    continue
                if re.search(r"\.(bin|txt|jpg|png|pdf|zip|apk|mp4)$", other.text):
                    claimed_files.add(other.text)
    r.note("A6.claims", f"界面上的完成标记 {len(claims)} 个，涉及文件 "
                        f"{sorted(claimed_files) or '（未识别到文件名）'}")
    disk = phone_ls()
    missing = sorted(name for name in claimed_files if name not in disk)
    zero = sorted(name for name in claimed_files if disk.get(name) == 0)
    r.ok("A6.1 界面说已收到的文件，磁盘上都有", not missing,
         expected="每个都出现在 " + PHONE_DOWNLOAD,
         actual=f"缺失={missing}；磁盘现有 {sorted(disk)[:10]}",
         note="责任人 android-ui：App 侧「已收到」写早了 —— 它只表示消息投递成功，"
              "不表示文件落盘。E3「无静默失败」同类问题")
    r.ok("A6.2 界面说已收到的文件，磁盘上都不是 0 字节", not zero,
         expected="大小 > 0", actual=f"0 字节={zero}",
         note="实测见过 4 个 .pending-179086558x-README.txt 是 0 字节残留")
    pend = sorted(name for name in disk if name.startswith(".pending-"))
    pend_zero = sorted(name for name in pend if disk.get(name) == 0)
    r.note("A6.pending", f"手机目录里的 .pending-* 临时文件：{len(pend)} 个"
                         + (f"（其中 0 字节 {len(pend_zero)} 个）" if pend else ""))
    r.eq("A6.3 手机目录里没有 0 字节的 .pending-* 残留", pend_zero, [],
         note="责任人 android-ui：`MainActivity.kt saveToDownloads` 在 sink 抛异常时"
              "没有把 IS_PENDING 清 0，于是留下永远 pending 的 0 字节行 —— "
              "这条断言直接盯住「下载失败了但没人收拾」（Lead 挖到的根因）")
    r.eq("A6.4 手机目录里没有任何 0 字节的已下载文件",
         sorted(n for n, sz in disk.items() if sz == 0 and not n.startswith(".")), [],
         note="这条盯「下载了但一个字节都没写进去」的文件；点开头的临时文件由 A6.3 管")


@check("A7", "手机发文件给电脑 → 电脑接收目录 + sha256")
def a07(r: R) -> None:
    """手机 → 电脑：真的在 App 里选文件、点发送，然后以**电脑侧磁盘**为准。

    驱动路径（2026-09 在小米 9 / Android 13 上实测走通；MIUI 的 DocumentsUI 必须先
    ``force-stop`` 重置，否则它会停在一个不响应点击的"最近"页）：

      1. ``adb push`` 一个已知 sha256 的文件到 ``/sdcard/Download/``；
      2. App 会话页点「📎 传输文件」→ 系统选择器；
      3. 点文件名那一行（选中即返回）；若停在选择器里再点右上角「选择」；
      4. 回 App 后确认待发区有它、设备行是勾选态；
      5. 点「发送」→ 等电脑接收目录出现同名文件 → **sha256 必须相等**；
      6. ``chat.db`` 里那条入站记录必须是 done —— 以前这条路径**什么都不记**，
         文件在磁盘上而界面上查无此人。

    界面说什么都不算证据：只看电脑侧磁盘与数据库。
    """
    def run() -> None:
        if not phone_online():
            r.undecided("A7 手机 → 电脑", "手机不在线")
            return
        pc = _PC["bundle"]
        if pc is None:
            r.fail("A7 电脑端已就绪", "start_pc() 没成功")
            return
        engine, window = pc["engine"], pc["window"]
        receive_dir = Path(pc["receive_dir"])

        phone = ensure_phone_connected(pc)
        if phone is None:
            r.undecided("A7 手机已连上电脑端", "等了 120s 也没等到 source=web 的行")
            return

        path = FILES / "A7-手机发来.png"
        digest = make_png(path, 512)

        def landed_file() -> Path | None:
            """落地文件可能带 " (1)" 后缀：接收目录里已经有同名文件时，服务端会
            用 unique_path 改名（不覆盖用户已有的那份）。断言必须认这种落地名 ——
            写死精确文件名会永远等不到（实测踩过：文件其实早就到了）。"""
            found = sorted(receive_dir.glob(f"{path.stem}*{path.suffix}"))
            return found[-1] if found else None

        for stale in receive_dir.glob(f"{path.stem}*{path.suffix}"):
            try:
                stale.unlink()
            except OSError:
                pass
        r.note("A7.digest", f"{path.name} {path.stat().st_size // 1024} KiB sha256={digest[:16]}…")

        adb("shell", "rm", "-f", f"{PHONE_DOWNLOAD}/{path.name}", timeout=30)
        adb("push", str(path), f"{PHONE_DOWNLOAD}/{path.name}", timeout=180)
        on_phone = path.name in phone_ls()
        r.ok("A7.a 测试文件已经在手机上（adb push）", on_phone,
             expected=f"{PHONE_DOWNLOAD}/{path.name}", actual=str(on_phone))
        if not on_phone:
            return

        # 让系统媒体库知道刚推上去的文件，选择器的"最近"列表才会列出它。
        # **不要** force-stop 选择器：MIUI 上那样会丢掉"最近文件"列表，只剩分类页，
        # 反而选不到文件（实测撞过：界面停在 图片/音频/视频/文档… 那一页）。
        adb("shell", "am", "broadcast", "-a",
            "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
            "-d", f"file://{PHONE_DOWNLOAD}/{path.name}", timeout=30)
        r.note("A7.prepare", ensure_phone_app_ready())
        r.ok("A7.b0 App 在会话页（底部有「📎 传输文件」）", ensure_phone_chat_page(),
             expected="会话页", actual=f"界面文本={[t for t in phone_ui_texts() if t][:8]}")

        nodes = phone_ui()
        # 别用"文本包含"直接点：会话页的提示语里也有"传输文件"四个字
        # （"点「📎 传输文件」选要发的文件…"），点到提示上什么都不发生。
        # 真正的按钮在**底部工具栏**，取 y 最大的那个。
        candidates = [n for n in nodes if n.text and "传输文件" in n.text]
        opened = None
        if candidates:
            opened = max(candidates, key=lambda n: n.bounds[1])
            phone_tap(*opened.center)
        r.ok("A7.b App 里点开了「传输文件」", opened is not None,
             expected="底部工具栏那个「📎 传输文件」",
             actual=f"候选={[n.text for n in candidates][:4]}")
        if opened is None:
            return

        # 选择器可能停在分类页：先点「图片」（确定性的入口），图片列表里必有刚推的文件。
        time.sleep(2.0)
        chip_nodes = phone_ui()
        chip = next((n for n in chip_nodes if n.text == "图片"), None)
        if chip is not None:
            phone_tap(*chip.center)
            time.sleep(2.5)

        picked = False
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            nodes = phone_ui()
            row = next((n for n in nodes if n.text == path.name), None)
            if row is None:
                row = next((n for n in nodes if n.text and path.name in n.text), None)
            if row is not None:
                phone_tap(*row.center)
                picked = True
                break
            time.sleep(1.0)
        r.ok("A7.c 在选择器里选中了这个文件", picked,
             expected=f"列表里出现 {path.name}",
             actual=f"界面文本={[t for t in phone_ui_texts() if t][:8]}")
        if not picked:
            return
        time.sleep(2.5)

        # 有的 ROM 选中后还要再点一次「选择 / 完成」。
        nodes = phone_ui()
        if any(n.text == "选择" for n in nodes):
            phone_tap_label(nodes, "选择")
            time.sleep(2.5)

        texts = [t for t in phone_ui_texts() if t]
        staged = any(path.name in t for t in texts)
        r.ok("A7.d 文件进了待发区（回到 App 了）", staged,
             expected=f"界面上出现 {path.name}", actual=str(texts[-8:]))
        if not staged:
            return

        # 设备栏里可能勾着**好几台**（历史实例还在列表里）：App 会逐个发，
        # 发给已经关掉的那台会长时间超时（实测撞到：日志里 Request timed out，
        # 文件永远到不了我们这台）。所以先把非目标全部取消勾选，再确保目标被勾上。
        chosen = False
        for _ in range(5):
            nodes = phone_ui()
            rows = [n for n in nodes if n.text and n.text.strip().startswith(("☐", "☑"))]
            if not rows:
                break
            target = next((n for n in rows if PC_NAME in n.text), None)
            wrong = [
                n for n in rows
                if n.text.strip().startswith("☑") and PC_NAME not in n.text
            ]
            target_ok = target is not None and target.text.strip().startswith("☑")
            if target_ok and not wrong:
                chosen = True
                break
            if wrong:
                phone_tap(*wrong[0].center)
            elif target is not None:
                phone_tap(*target.center)
            time.sleep(1.5)
        r.note("A7.e 设备勾选",
               f"{'已勾选' if chosen else '没能只勾一台（继续尝试发送）'}；"
               f"行={[n.text for n in rows][:4]}")

        nodes = phone_ui()
        sent = phone_tap_label(nodes, "发送")
        r.ok("A7.f 点了「发送」", sent is not None, actual="找到并点击「发送」")
        if sent is None:
            return

        # 记下手机自己怎么说的（产品现在会如实写"发送失败：原因"/"部分成功…"）：
        # 电脑侧收不到时，这句话就是最直接的线索。
        time.sleep(3.0)
        phone_status = [
            t for t in phone_ui_texts()
            if t and any(k in t for k in ("已发给", "发送失败", "部分成功", "正在发送", "已处理"))
        ]
        r.note("A7.phone_status", str(phone_status[:3]))

        appeared = wait_for(lambda: landed_file() is not None, timeout=180, interval=1.0)
        found = landed_file()
        r.ok("A7.g 电脑接收目录里真的出现了这个文件", appeared,
             expected=f"{receive_dir}/{path.stem}*{path.suffix}",
             actual=f"目录里现有 {sorted(p.name for p in receive_dir.iterdir())[:6]}")
        if found is None:
            return
        landed_digest = sha256_file(found)
        r.ok("A7.h 落地文件的 sha256 与源文件相等", landed_digest == digest,
             expected=digest, actual=landed_digest,
             note="**收到名字不算成功，只有 sha256 相等才算**")
        r.eq("A7.i 落地文件大小与源一致", found.stat().st_size, path.stat().st_size)

        def record_state() -> str:
            try:
                for row in engine.chat.conversations():
                    for message in engine.chat.messages(str(row["id"])):
                        if str(message.get("mediaName") or "") == path.name:
                            return str(message.get("state") or "")
            except Exception:
                return ""
            return ""

        # 记录是**窗口定时器/信号**在 Qt 主线程里落库的：这里必须泵事件循环，
        # 否则 wait_for 只 sleep，信号永远排不到 → 明明记录了也读不到（v2 真机
        # 实测假红：A7.j 判红而 chat.db 里其实已经有这条 done 记录）。
        done_ok = wait_for_ui(lambda: record_state() == "done", timeout=30.0)
        got = record_state()
        r.ok("A7.j 电脑端的记录里有这条入站文件（状态 done）",
             done_ok and got == "done", expected="done", actual=repr(got),
             note="手机 POST 上来落盘的路径必须留下一条记录，否则用户翻不到谁发过什么")

        shot = SHOTS / "A7-desktop-received.png"
        window.grab().save(str(shot), "PNG")
        r.note("A7.shot", str(shot))

    run()


@check("A8", "双向中文 + emoji 消息，两端都能看到")
def a08(r: R) -> None:
    if not phone_online():
        r.undecided("A8 消息双向", "手机不在线")
        return
    pc = _PC["bundle"]
    if pc is None:
        r.fail("A8 电脑端已就绪", "start_pc() 没成功")
        return
    engine, window = pc["engine"], pc["window"]
    phone = ensure_phone_connected(pc)
    if phone is None:
        r.undecided("A8 手机已连上", "等了 120s 也没等到 source=web 的行")
        return

    text = f"QA 真机消息 🚀 中文+emoji {int(time.time()) % 100000}"
    window.sidebar.set_devices([phone], {})
    window._select_device_row(phone)
    qt_app().processEvents()
    window.chat_view.input.setText(text)
    window.send_button.click()
    qt_app().processEvents()
    # 等的时候必须**泵事件循环**：发送要走窗口那一侧的定时器/队列（wait_for 只 sleep）。
    seen = wait_for_ui(lambda: any(text in t for t in phone_ui_texts()),
                       timeout=45, interval=2.0)
    r.ok("A8.1 电脑发的中文+emoji 消息出现在手机界面上", seen,
         expected=f"手机界面含 {text!r}",
         actual=f"手机界面片段={[t for t in phone_ui_texts() if t][:12]}")
    states = _message_states(engine, "")
    r.note("A8.desktop_rows", f"电脑端会话行数={len(states)}")
    phone_shot = SHOTS / "A8-phone-message.png"
    if phone_screenshot(phone_shot):
        r.note("A8.phone_shot", str(phone_shot))


#: 由 main() 填好之后所有检查共用（同一个电脑端实例）
_PC: dict = {"bundle": None, "state": None}


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def run(cids: list[str] | None = None) -> list[Result]:
    results: list[Result] = []
    _PC["state"] = repo_state()
    state = _PC["state"]
    print(f"真机验收 · HEAD={state['head']} 变更={state['dirty_count']} "
          f"load={state.get('loadavg')} adb={SERIAL}")
    _PC["ports_before"] = ports_free()
    if not phone_online():
        print("  注意：手机不在线 —— 后续各项会标「未判定」，不会记成绿。")
    else:
        try:
            _PC["bundle"] = start_pc(PC_NAME)
            print(f"  电脑端已起：TCP {PC_TCP_PORT} / 网页 {PC_WEB_PORT} / UDP {PC_DISCOVERY_PORT}")
        except Exception:
            print("  电脑端启动失败：\n" + traceback.format_exc())

    for cid, title, fn in CHECKS:
        if cids and cid not in cids:
            continue
        recorder = R(cid, title)
        print(f"\n=== {cid} {title} ===", flush=True)
        started = time.monotonic()
        try:
            fn(recorder)
        except Exception:
            recorder.result.error = traceback.format_exc()
        recorder.result.subs.append(
            __import__("test_enterprise_acceptance").Sub(
                "~耗时", True, actual=f"{time.monotonic() - started:.1f}s"))
        results.append(recorder.result)
        print(f"--- {cid}: {result_status(recorder.result)} ---", flush=True)
        for sub in recorder.result.failures:
            print(f"    [FAIL] {sub.name}\n           期望: {sub.expected}\n"
                  f"           实际: {sub.actual}", flush=True)
        for sub in recorder.result.subs:
            if sub.expected == "（负载下未判定）":
                print(f"    [未判定] {sub.name}：{sub.actual}", flush=True)
        if result_status(recorder.result) == "UNDECIDED" and not recorder.result.failures:
            print("    （整项未判定：没有任何一条被判定的子断言）", flush=True)
        if recorder.result.error:
            print("    [ERROR]\n" + recorder.result.error, flush=True)

    bundle = _PC.get("bundle")
    if bundle is not None:
        try:
            bundle["ui"].stop()
            bundle["engine"].stop()
            bundle["window"].close()
        except Exception:
            pass
    return results


def write_reports(results: list[Result], label: str) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    state = _PC.get("state") or repo_state()
    payload = {
        "label": label, "when": state.get("when"), "repo": state,
        "adb_serial": SERIAL, "phone_online": phone_online(),
        "summary": {r.cid: result_status(r) for r in results},
        "results": [{
            "id": r.cid, "title": r.title, "status": result_status(r),
            "error": r.error,
            "subs": [{"name": s.name, "ok": s.ok, "expected": s.expected,
                      "actual": s.actual, "note": s.note} for s in r.subs],
        } for r in results],
    }
    (REPORTS / f"real_device_{label}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [f"# 真机验收报告 · {label}", "",
             f"生成时间：{payload['when']}　·　HEAD `{state.get('head')}`　·　"
             f"变更文件 {state.get('dirty_count')} 个　·　load `{state.get('loadavg')}`", "",
             f"- adb 设备：`{SERIAL}`　在线：**{'是' if payload['phone_online'] else '否（各项标未判定）'}**",
             f"- 电脑端端口：TCP {PC_TCP_PORT} / 网页 {PC_WEB_PORT} / UDP {PC_DISCOVERY_PORT}",
             f"- 测试数据：`{WORK}`（在 /home，不在 /tmp）", "",
             "| # | 检查项 | 结论 | 失败的子断言 |", "|---|---|---|---|"]
    marks = {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "UNDECIDED": "⚠️ 未判定"}
    for result in results:
        status = result_status(result)
        failed = "、".join(s.name for s in result.failures) or "—"
        if status == "UNDECIDED":
            failed = "前提缺失：" + "；".join(
                s.name for s in result.subs if s.expected == "（负载下未判定）") or "—"
        lines.append(f"| {result.cid} | {result.title} | {marks[status]} | {failed} |")
    lines += ["", "## 明细", ""]
    for result in results:
        lines.append(f"### {result.cid} {result.title}")
        if result.error:
            lines.append("```\n" + result.error.strip() + "\n```")
        for sub in result.subs:
            if sub.name == "~耗时":
                continue
            mark = "✅" if sub.ok else "❌"
            lines.append(f"- {mark} **{sub.name}**")
            if sub.expected:
                lines.append(f"  - 期望：`{sub.expected}`")
            if sub.actual:
                lines.append(f"  - 实际：`{sub.actual}`")
            if sub.note:
                lines.append(f"  - 说明：{sub.note}")
        lines.append("")
    (REPORTS / f"real_device_{label}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n报告已写入 {REPORTS / ('real_device_' + label + '.md')} 与 .json")


def main(argv: list[str] | None = None) -> int:
    use_utf8_console()
    parser = argparse.ArgumentParser(description="真机验收（PC ↔ 手机）")
    parser.add_argument("--only", default="", help="逗号分隔，例如 A1,A6")
    parser.add_argument("--label", default="", help="报告文件名")
    args = parser.parse_args(argv)
    cids = [c.strip() for c in args.only.split(",") if c.strip()] or None
    label = args.label or time.strftime("%m%d-%H%M")

    # 现场准备放在这里，而不是 A0 里：``--only A1,A2`` 时 A0 根本不会跑，
    # 上一轮留在手机上的测试文件就会把 A1.a/A2.a（"发送前不该有它"）判红 ——
    # 实测撞过两次，都是测试卫生问题而不是产品缺陷。
    #
    # ``pm clear`` 也放在这里（整轮开始前），**不要**塞进某个用例中间：它会清掉
    # App 的全部状态，放在 A7 里会把同一轮的 A8（"消息出现在手机界面上"）判红。
    # 清的理由：换一次 data_dir 就换一个 device_id，手机里会堆着同一台电脑的历史
    # 实例（"QA-真机验收 / #2 / #3"），发送时挨个试、撞上已关掉的那台就长时间超时。
    # 保留 identity.json（**device_id 稳定**，手机里就不会堆同一台电脑的历史实例），
    # 但清掉上一轮的聊天库：否则 "A1.f 手机取走之前不许是 sent" 会被上一轮留下的
    # 同名记录判红（实测撞到 states=['sending','sent','sent']），A8 也会看到旧消息。
    for leftover in ("chat.db", "chat.db-wal", "chat.db-shm"):
        stale = WORK / "pc" / leftover
        try:
            if stale.exists():
                stale.unlink()
        except OSError:
            pass
    print(f"现场准备：{ensure_phone_app_ready()}")
    # 手机上会堆着**同一台电脑的历史实例**（换一次 data_dir 就换一个 device_id，
    # App 把它们当三台设备），发送时会挨个试、撞上已关掉的那台就长时间超时。
    # 但**不要每轮都清**：pm clear 会换掉 App 的身份，把同一轮的 A8（"消息出现在
    # 手机界面上"）判红 —— 实测撞过。所以：
    #   · 平时不清，靠**保留 data_dir**（device_id 稳定）让手机只会看到一台；
    #   · 需要时显式 `EVERSEND_REAL_CLEAR_PHONE=1`（清完会自动把 App 拉起来）。
    if os.environ.get("EVERSEND_REAL_CLEAR_PHONE"):
        print("正在清 App 数据（EVERSEND_REAL_CLEAR_PHONE=1）…")
        try:
            adb("shell", "pm", "clear", PACKAGE, timeout=60)
            adb("shell", "pm", "grant", PACKAGE, "android.permission.POST_NOTIFICATIONS",
                timeout=30)
            time.sleep(2.0)
            print(f"清完再拉起：{ensure_phone_app_ready()}")
        except Exception as exc:  # pragma: no cover
            print(f"  （清数据失败，继续：{exc}）")

    results = run(cids)
    print("\n" + "=" * 62)
    for result in results:
        # 用 result_status 而不是 result.ok：UNDECIDED 的 ok 也是 True，
        # 只说 ok 会把"没验到"显示成 PASS —— 那正是最危险的一种谎报。
        print(f"  {result.cid:<4} {result_status(result):<9} {result.title}")
    print("=" * 62)
    statuses = [result_status(r) for r in results]
    passed = statuses.count("PASS")
    undecided = statuses.count("UNDECIDED")
    failed = statuses.count("FAIL")
    print(f"  {passed}/{len(results)} 项通过"
          + (f"；{failed} 项失败" if failed else "")
          + (f"；{undecided} 项未判定（前提缺失，不记绿）" if undecided else ""))
    write_reports(results, label)
    return 0 if passed == len(results) else 1   # 未判定不算通过，退出码非 0


if __name__ == "__main__":
    raise SystemExit(main())
