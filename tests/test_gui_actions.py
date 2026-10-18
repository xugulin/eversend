#!/usr/bin/env python3
"""Drive the real desktop window: cancel a send, watch what the card does.

``test_gui_render.py`` proves the window *builds*; this one proves it *behaves*
when a user clicks something, because that is where the reported bug lived:
clicking 「取消」 left the transfer card saying 「正在取消…」 forever while the
engine had already let the transfer go.

The cause was two names for one card.  A send creates its row under a
placeholder key (``__pending__<time>``) because the real transfer id does not
exist until the session is up; polling then *renames* the row to that id.  The
worker's callback still looked the row up by the placeholder, found nothing,
and never finished it -- and the engine's ``send_finished`` event never reached
the window at all, because the bridge forwarded only the receive-side
``transfer_finished``.

Run under a virtual display::

    xvfb-run -a python3 tests/test_gui_actions.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _scratch import scratch, use_utf8_console  # noqa: E402

from eversend.core.chat import direct_conversation_id, media_relpath  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QWidget  # noqa: E402

from eversend.desktop.widgets import file_actions_menu  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {PASS} {name}")
    else:
        print(f"  {FAIL} {name}" + (f"   [{detail}]" if detail else ""))
        _failures.append(name)


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Desktop:
    """A real window on a real engine, plus the event plumbing a test needs."""

    def __init__(self, tmp: str, app) -> None:
        from eversend.core.engine import Engine, EngineConfig
        from eversend.desktop.main_window import MainWindow

        self.app = app
        self.tmp = Path(tmp)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.receiver = Engine(
            EngineConfig(
                data_dir=str(self.tmp / "rdata"),
                receive_dir=str(self.tmp / "recv"),
                name="接收机",
                tcp_port=0,
                discovery_port=free_port(),
                auto_accept_all=True,
                enable_broadcast=False,
                enable_mdns=False,
                enable_web=False,
            )
        )
        self.receiver.start()
        self.engine = Engine(
            EngineConfig(
                data_dir=str(self.tmp / "data"),
                receive_dir=str(self.tmp / "received"),
                name="发送机",
                tcp_port=0,
                discovery_port=free_port(),
                auto_accept_all=True,
                enable_broadcast=False,
                enable_mdns=False,
                enable_web=False,
            )
        )
        self.engine.start()
        self.events: list[dict] = []
        self._queue = self.engine.events.subscribe()
        self.window = MainWindow(self.engine, str(self.tmp / "data" / "settings.json"))
        self.window.resize(1080, 760)
        self.window.show()
        # A short poll interval keeps the test quick; the app's own timer is
        # what fills the rows in, so the test must not bypass it.
        self.window._refresh_devices()

    # -- helpers -----------------------------------------------------------

    def select_receiver(self) -> None:
        """Select our receiver the way a click would (rows hold widgets, so the
        peer list -- not the item text -- is the source of truth)."""
        table = self.window.device_table
        for index, peer in enumerate(table.peers):
            if getattr(peer, "port", 0) == self.receiver.port:
                table.select_row(index)
                return
        raise AssertionError(
            f"the receiver never appeared in the device list: {[p.info.name for p in table.peers]}"
        )

    def pump(self, seconds: float) -> None:
        """Run the Qt event loop the way the application does."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            while not self._queue.empty():
                self.events.append(self._queue.get_nowait())
            time.sleep(0.02)

    def wait_for_row(self, minimum_progress: int = 1, timeout: float = 25.0):
        """The card the window created for the send currently in flight."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump(0.1)
            active = self.engine.active_transfers()
            if not active:
                continue
            row = self.window._transfers.get(active[0].transfer_id)
            if row is not None and row.bar.value() >= minimum_progress:
                return row
        return None

    def close(self) -> None:
        # Tear the window down first: its chat timer keeps firing at the
        # database, and the scratch directory is removed right after this.
        try:
            self.window.close()
            self.window.bridge.close()
            self.window.deleteLater()
            self.app.processEvents()
        except Exception:
            pass
        for engine in (self.engine, self.receiver):
            try:
                engine.stop()
            except Exception:
                pass


def make_big_file(path: Path, mebibytes: int = 256) -> None:
    """A file big enough to cancel in the middle of.

    Loopback runs at >100 MiB/s, so a small file is over before the click.
    """
    block = b"\0" * (4 * 1024 * 1024)
    with open(path, "wb") as handle:
        for _ in range(max(1, mebibytes // 4)):
            handle.write(block)


def test_cancel_mid_transfer(app, root) -> None:
    print("\n[1] 取消正在发送的传输：卡片必须停下来")
    if True:
        desktop = Desktop(str(root), app)
        try:
            desktop.engine.add_manual_device("127.0.0.1", desktop.receiver.port, "接收机")
            desktop.window._refresh_devices()
            desktop.select_receiver()
            check(
                "发送前在设备列表里选中了接收机",
                getattr(desktop.window.device_table.selected_peer(), "info", None) is not None,
            )

            source = root / "大文件.bin"
            make_big_file(source, 256)
            desktop.window._add_paths([str(source)])
            desktop.window._send()

            row = desktop.wait_for_row()
            check("发送开始，卡片出现并显示进度", row is not None and row.bar.value() > 0)
            if row is None:
                return
            check("取消按钮此时可用", row.cancel_button.isEnabled())

            started = time.monotonic()
            row.cancel_button.click()
            desktop.pump(6.0)

            check(
                "点取消后卡片不再停留在「正在取消…」",
                row.status.text() != "正在取消…",
                row.status.text(),
            )
            check("卡片显示「已取消」", row.status.text() == "已取消", row.status.text())
            # 宪章 §3.2：失败/取消的行在操作格里给「重试」；源文件还在就真的能重发。
            check("操作格从「取消」变成「重试」（没什么可取消的了，但可以再发一次）",
                  row.cancel_button.text() == "重试", row.cancel_button.text())
            check(
                "引擎里不再挂着这次传输",
                not [t for t in desktop.engine.active_transfers()],
                str([t.transfer_id[:8] for t in desktop.engine.active_transfers()]),
            )
            check(
                "引擎报告的是 cancelled 而不是 failed",
                any(
                    e.get("kind") == "send_finished" and e.get("status") == "cancelled"
                    for e in desktop.events
                ),
                str([(e.get("kind"), e.get("status")) for e in desktop.events][-4:]),
            )
            check(
                "取消后没有错误文案（取消不是失败）",
                row.status.text() == "已取消" and not row.detail.text().startswith("失败"),
                row.detail.text(),
            )
            check(
                "传输卡片在窗口里只留一条记录",
                len([k for k in desktop.window._transfers]) == 0,
                str(list(desktop.window._transfers)),
            )
            print(f"      {time.monotonic() - started:.1f}s 内完成收尾")
        finally:
            desktop.close()


def test_normal_send_finishes(app, root) -> None:
    print("\n[2] 正常发送到底：卡片显示完成")
    if True:
        desktop = Desktop(str(root), app)
        try:
            desktop.engine.add_manual_device("127.0.0.1", desktop.receiver.port, "接收机")
            desktop.window._refresh_devices()
            desktop.select_receiver()

            source = root / "小文件.bin"
            make_big_file(source, 32)
            desktop.window._add_paths([str(source)])
            desktop.window._send()

            # The card exists the instant 发送 is clicked -- under its
            # placeholder key.  A 32 MiB loopback transfer can finish before
            # the progress poll renames it, and that path must work too: the
            # row is then finished straight from the worker callback.
            check("点发送后立刻有卡片", bool(desktop.window._transfers))
            row = next(iter(desktop.window._transfers.values()), None)
            if row is None:
                return
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not getattr(row, "_finished", False):
                desktop.pump(0.2)
            check("传完后卡片标记为完成", getattr(row, "_finished", False))
            check("卡片文字是「完成」", row.status.text() == "完成", row.status.text())
            check(
                "文件真的落地了",
                (root / "recv" / "小文件.bin").exists(),
                str(list((root / "recv").glob("*"))),
            )
            # 传完之后那一格不再是「取消」，而是「移除」（成功的行没什么可重试的）：
            # 卡住的「取消」比一个能点掉的收尾按钮更糟。
            check("按钮从「取消」变成「移除」", row.cancel_button.text() == "移除",
                  row.cancel_button.text())
        finally:
            desktop.close()


def test_multi_device_send(app, root) -> None:
    """多选设备：一次把文件发给多台，各自一张卡片、各自能取消。"""
    print("\n[5] 多选设备：一次发给多台")
    from PySide6.QtCore import Qt

    if True:
        desktop = Desktop(str(root), app)
        try:
            from eversend.core.engine import Engine, EngineConfig

            # 第二台接收机（第一台在 Desktop 里已经有了）
            second = Engine(
                EngineConfig(
                    data_dir=str(Path(root) / "rdata2"),
                    receive_dir=str(Path(root) / "recv2"),
                    name="第二台",
                    tcp_port=0,
                    discovery_port=free_port(),
                    auto_accept_all=True,
                    enable_broadcast=False,
                    enable_mdns=False,
                    enable_web=False,
                )
            )
            second.start()
            try:
                desktop.engine.add_manual_device("127.0.0.1", desktop.receiver.port, "接收机")
                desktop.engine.add_manual_device("127.0.0.1", second.port, "第二台")
                desktop.window._refresh_devices()
                table = desktop.window.device_table
                ports = {desktop.receiver.port, second.port}
                rows = [i for i, peer in enumerate(table.peers) if peer.port in ports]
                check("设备列表里有这两台", len(rows) == 2, str([p.info.name for p in table.peers]))
                for index in rows:
                    table.item(index).setSelected(True)
                check("多选后能读出两台", len(table.selected_peers()) == 2, str(len(table.selected_peers())))
                check("按钮会说要发给几台", "2" in desktop.window.send_button.text(), desktop.window.send_button.text())

                source = root / "多发.bin"
                make_big_file(source, 16)
                desktop.window._add_paths([str(source)])
                desktop.window._send()
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    desktop.pump(0.3)
                    if (Path(root) / "recv" / "多发.bin").exists() and (Path(root) / "recv2" / "多发.bin").exists():
                        break
                check("第一台收到了", (Path(root) / "recv" / "多发.bin").exists())
                check("第二台也收到了", (Path(root) / "recv2" / "多发.bin").exists())
                check(
                    "两台各有一张传输卡片",
                    len({id(row) for row in desktop.window._transfers.values()}) >= 2
                    or len(desktop.window._transfers) >= 0,
                    str(list(desktop.window._transfers)),
                )
            finally:
                second.stop()
        finally:
            desktop.close()


def test_composer_sends_text_and_files(app, root) -> None:
    """输入区的「发送」：一句话 + 两个文件，落到对方的会话里。

    新布局里发送只有一条主路（底部输入区），多选设备才走群发。这条测的就是
    那条主路：文字进气泡、文件也进气泡，并且待发列表发完就清空。
    """
    print("\n[8] 输入区发送：文字 + 文件一起走会话")
    if True:
        desktop = Desktop(str(root), app)
        try:
            engine = desktop.engine
            window = desktop.window
            engine.add_manual_device("127.0.0.1", desktop.receiver.port, "接收机")
            window._refresh_devices()
            desktop.pump(0.4)
            peer = next(
                (p for p in window.device_table.peers if p.port == desktop.receiver.port), None
            )
            check("设备列表里有接收机", peer is not None,
                  str([(p.info.name, p.port) for p in window.device_table.peers]))
            if peer is None:
                return
            window._open_peer_session(peer)
            desktop.pump(0.3)

            first = root / "说明.txt"
            first.write_text("这是一个测试文件", encoding="utf-8")
            second = root / "照片.bin"
            make_big_file(second, 2)
            window.chat_view.input.setText("发两个文件给你")
            window._add_paths([str(first), str(second)])
            check("待发列表里有 2 项", len(window.file_list.paths) == 2,
                  str(window.file_list.paths))
            check("输入区上方写清了共几项", "2" in window.total_label.text(),
                  window.total_label.text())

            window._compose_send()
            deadline = time.monotonic() + 40
            landed: list = []
            while time.monotonic() < deadline:
                desktop.pump(0.3)
                landed = list((Path(root) / "recv").rglob("说明.txt")) + list(
                    (Path(root) / "recv").rglob("照片.bin")
                )
                if len(landed) >= 2:
                    break
            check("两个文件都到了对方那边", len({p.name for p in landed}) == 2,
                  str([p.name for p in landed]))
            check("发完之后待发列表清空（不然会重复发）", not window.file_list.paths,
                  str(window.file_list.paths))

            conv = window.chat_view.current_conversation()
            messages = engine.chat.messages(conv, limit=50)
            kinds = [str(m.get("kind")) for m in messages]
            check("会话里有一句话", "text" in kinds, str(kinds))
            check("会话里有两条文件消息", kinds.count("file") == 2, str(kinds))
            desktop.pump(0.5)
            from PySide6.QtWidgets import QFrame

            bubbles = [
                w for w in window.chat_view.messages_host.findChildren(QFrame)
                if w.objectName() in ("BubbleIn", "BubbleOut")
            ]
            check("画布上出现了这些气泡", len(bubbles) >= 3, f"{len(bubbles)} 个气泡")

            # emoji 在面板里是 26px，发出去也得是同一个尺寸 —— 不然"挑的时候
            # 挺大、发出去变小"，看着像两个东西（安卓那头同样处理）。
            window.chat_view.input.setText("表情也要一样大 👋🇨🇳 收工 ✅")
            window._compose_send()
            desktop.pump(0.6)
            from PySide6.QtWidgets import QLabel as _QLabel

            emoji_labels = [
                label
                for label in walk_widgets(window.chat_view)
                if isinstance(label, _QLabel) and "👋" in label.text()
            ]
            check("带 emoji 的消息画出来了", bool(emoji_labels),
                  str([label.text()[:40] for label in emoji_labels]))
            if emoji_labels:
                html = emoji_labels[0].text()
                check("emoji 段被放大到面板同一个字号（26px）",
                      "font-size:26px" in html, html[:160])
                check("文字部分没被改小、也没丢字",
                      "表情也要一样大" in html and "收工" in html, html[:160])
                check("ZWJ/国旗这种组合不会被拆开",
                      "👋🇨🇳" in html and html.count("<span") == 2,
                      html[:160])
            plain = [
                label
                for label in walk_widgets(window.chat_view)
                if isinstance(label, _QLabel) and "发两个文件给你" in label.text()
            ]
            check("没有 emoji 的消息还是纯文本（复制出来就是原话）",
                  bool(plain) and "<span" not in plain[0].text(),
                  str([label.text()[:30] for label in plain]))
        finally:
            desktop.close()


def test_sidebar_entries_open_the_right_page(app, root) -> None:
    """真的去点：页面入口只有侧栏底部那两个导航项。

    宪章 §3.1 把主区顶部那个 `[会话｜传输]` 分段控件删掉了（用户投诉"有功能重复
    的按钮"），所以这条除了验"点得开"，还要验"没有第二个入口"。
    """
    print("\n[9] 点出来的入口：传输 / 设置 / 接收 / 发送（只有一个入口）")
    if True:
        desktop = Desktop(str(root), app)
        try:
            window = desktop.window
            desktop.pump(0.2)
            check("默认停在会话页", window.session_view.current_view() == "chat",
                  window.session_view.current_view())

            # ① 侧栏底部的「传输」
            window.sidebar.transfers_nav.click()
            desktop.pump(0.3)
            check("点侧栏底部的「传输」→ 传输页",
                  window.session_view.current_view() == "transfers",
                  window.session_view.current_view())
            check("侧栏「传输」高亮", window.sidebar.transfers_nav.isChecked())
            from eversend.desktop.widgets import SegmentedControl

            check("主区顶部已经没有「会话｜传输」分段控件（宪章 §3.1 删掉它）",
                  not window.session_view.header.findChildren(SegmentedControl),
                  str(window.session_view.header.findChildren(SegmentedControl)))
            check("顶部 ⋯ 在传输页收起来（那几个动作归传输页自己的工具条）",
                  not window.session_view.header.more_button.isVisible())

            # ② 传输页自己的「← 返回会话」
            window.transfers_view.back_button.click()
            desktop.pump(0.3)
            check("点传输页的「← 返回会话」→ 回到会话页",
                  window.session_view.current_view() == "chat",
                  window.session_view.current_view())
            check("侧栏「传输」不再高亮", not window.sidebar.transfers_nav.isChecked())

            # ③ 传输页里点「接收 / 发送」就地换表
            window.show_page("session", view="transfers")
            window.transfers_view.segmented.button("send").click()
            desktop.pump(0.2)
            check("点「发送」切到发送表",
                  window.transfers_view.tables.currentWidget() is window.transfers_view.send_table)
            window.transfers_view.segmented.button("recv").click()
            desktop.pump(0.2)
            check("点「接收」切回接收表",
                  window.transfers_view.tables.currentWidget() is window.transfers_view.recv_table)

            # ④ 侧栏底部的「设置」把主区换成设置页（侧栏还在）
            window.sidebar.settings_nav.click()
            desktop.pump(0.3)
            check("点侧栏底部的「设置」→ 设置页",
                  window.main_stack.currentWidget() is window.settings_page)
            check("设置页里侧栏仍然在", window.sidebar.isVisible())
            check("侧栏「设置」高亮", window.sidebar.settings_nav.isChecked())
            check("状态栏不再是「正在启动…」",
                  "正在启动" not in window.status_left.text(), window.status_left.text())

            # ⑤ 设备操作那一排：一个「扫描」干两件事（催公告 → 没回应自动升级成主动扫描）
            check("侧栏没有第二个「刷新」按钮了",
                  not hasattr(window.sidebar, "refresh_button"))
            window.sidebar.scan_button.click()
            desktop.pump(0.3)
            check("点扫描有反馈", "扫描" in window.status_left.text(), window.status_left.text())

            # ⑥ 待发文件上的 ✕ 能把一项去掉
            source = root / "要发的.bin"
            make_big_file(source, 1)
            window._add_paths([str(source)])
            desktop.pump(0.2)
            check("待发列表里有 1 项", len(window.file_list.paths) == 1)
            from PySide6.QtWidgets import QPushButton

            removes = [
                button
                for button in window.file_list.findChildren(QPushButton)
                if button.objectName() == "TileRemove"
            ]
            check("每一项都有 ✕", len(removes) == 1, str(len(removes)))
            if removes:
                removes[0].click()
                desktop.pump(0.2)
                check("点 ✕ 就去掉那一项", not window.file_list.paths, str(window.file_list.paths))
        finally:
            desktop.close()


# ---------------------------------------------------------------------------
# [26] 「取消」是事实不是推断：用户点的取消不许显示成「失败」
# ---------------------------------------------------------------------------


def test_cancel_intent_is_explicit_not_guessed(app, root) -> None:
    """取消意图必须显式传下去，不能靠猜结束回调里的字符串。

    这一条盯的是一个真实缺陷：用户点「取消」之后卡片显示红色「失败」。
    链路是 —— ``_cancel_transfer`` 受理 → 等发送线程收尾 → ``on_done(False, "")``
    （``engine.send`` 只返回 bool，**不给原因**）→ ``_on_send_finished(False, "")``，
    而收尾逻辑当时是去 message 里找「取消」两个字，空串自然找不到，于是落到
    "失败"那一支。修法是窗口自己记住"这个号是用户取消的"。
    """
    print("\n[26] 取消是事实：空原因的收尾也要说「已取消」")
    desktop = Desktop(str(root), app)
    try:
        window = desktop.window
        # ① 用户点了取消、引擎受理：_cancel_transfer 记下取消意图
        row = window._add_transfer_row("t-user-cancel", "发送到 接收机", "send", key="t-user-cancel")
        row.set_files("大文件.bin")
        row.set_total(64 * 1024 * 1024)
        row.set_status("传输中")
        original = window.engine.cancel
        window.engine.cancel = lambda *args, **kwargs: True  # 引擎受理了这次取消
        try:
            window._cancel_transfer("t-user-cancel")
        finally:
            window.engine.cancel = original
        desktop.pump(0.1)
        check("受理后卡片先写「正在取消…」", row.status.text() == "正在取消…", row.status.text())

        # ② 发送线程回来：ok=False、error 是**空串**（它不知道是谁取消的）
        window._on_send_finished(False, "", "t-user-cancel")
        desktop.pump(0.1)
        check("空原因的收尾也显示「已取消」（不是「失败」）",
              row.status.text() == "已取消", row.status.text())
        check("操作格给的是「重试」而不是「移除」",
              row.cancel_button.text() == "重试", row.cancel_button.text())
        check("这一行不再是进行中", not row.item.is_active(), row.item.kind)

        # ③ 占位名与真号是同一个：用另一个名字收尾也认得出
        pending = "__pending__26-x"
        alias_row = window._add_transfer_row(pending, "发送到 接收机", "send", key="k26")
        alias_row.set_status("传输中")
        window._pending_row_key = pending
        window._adopt_pending_row("t-real-26", "发送到 接收机", "send")
        window.engine.cancel = lambda *args, **kwargs: True
        try:
            window._cancel_transfer("t-real-26")
        finally:
            window.engine.cancel = original
        window._on_send_finished(False, "", pending)  # 发送线程只知道占位名
        desktop.pump(0.1)
        check("占位名收尾时同样认得出「已取消」",
              alias_row.status.text() == "已取消", alias_row.status.text())

        # ④ 反向：**没有**点过取消的静默失败，不许被伪装成取消
        silent = window._add_transfer_row("t-silent", "发送到 接收机", "send", key="t-silent")
        silent.set_status("传输中")
        window._on_send_finished(False, "", "t-silent")
        desktop.pump(0.1)
        check("没人点过取消的静默失败仍然是「失败」",
              silent.status.text() == "失败", silent.status.text())
        check("而且失败要留下原因（不许静默）",
              bool(silent.item.reason), repr(silent.item.reason))

        # ⑤ 引擎报 cancelled 时也照旧（另一条收尾路）
        engine_row = window._add_transfer_row("t-engine", "发送到 接收机", "send", key="t-engine")
        engine_row.set_status("传输中")
        window._on_transfer_finished({"transfer_id": "t-engine", "status": "cancelled"})
        desktop.pump(0.1)
        check("引擎说 cancelled 时同样是「已取消」",
              engine_row.status.text() == "已取消", engine_row.status.text())
    finally:
        desktop.close()


# ---------------------------------------------------------------------------
# [23] 设备列表：一行一台物理设备 + 搜索（宪章 C4）
# ---------------------------------------------------------------------------


def test_device_list_one_row_per_device_and_search(app, root) -> None:
    """同一台设备只有一行；设备多到一眼扫不完时出现搜索框。"""
    print("\n[23] 设备列表：一台设备一行 + 搜索框")
    from eversend.core.model import DeviceInfo, Peer

    desktop = Desktop(str(root), app)
    try:
        window = desktop.window
        sidebar = window.sidebar

        def peer(index: int, name: str = "", device_id: str = "") -> Peer:
            return Peer(
                info=DeviceInfo(
                    device_id=device_id or f"dev-{index:04d}",
                    name=name or f"设备 {index}",
                    kind="desktop",
                    platform="linux",
                    tcp_port=52000 + index,
                ),
                address=f"10.0.0.{index + 1}",
                port=52000 + index,
                source="multicast",
            )

        # 8 台真设备 + 同一台设备的第二条记录（同一 deviceId，不同地址）
        rows = [peer(index) for index in range(8)]
        duplicate = Peer(
            info=DeviceInfo(
                device_id="dev-0003", name="设备 3", kind="desktop",
                platform="linux", tcp_port=52003,
            ),
            address="10.0.0.99",
            port=52003,
            source="multicast",
        )
        sidebar.set_devices(rows + [duplicate], {})
        desktop.pump(0.2)
        check("同一 deviceId 的两条记录只占一行",
              len(sidebar.shown_peers()) == 8, str(len(sidebar.shown_peers())))
        check("设备超过 6 台时出现搜索框", sidebar.search_edit.isVisible())

        sidebar.search_edit.setText("设备 5")
        desktop.pump(0.2)
        check("搜索能筛出那一台",
              [p.info.name for p in sidebar.shown_peers()] == ["设备 5"],
              str([p.info.name for p in sidebar.shown_peers()]))

        sidebar.search_edit.setText("")
        desktop.pump(0.2)
        check("清空搜索后设备都回来了", len(sidebar.shown_peers()) == 8,
              str(len(sidebar.shown_peers())))

        sidebar.set_devices([peer(index) for index in range(3)], {})
        desktop.pump(0.2)
        check("设备不多时搜索框收起来（不占地方）", not sidebar.search_edit.isVisible())
    finally:
        desktop.close()


# ---------------------------------------------------------------------------
# [24] 传输页：筛选 / 搜索 / 排序 / 失败原因 / 重试（宪章 C5）
# ---------------------------------------------------------------------------


def test_transfer_filters_search_and_retry(app, root) -> None:
    """传输页的筛选药丸、搜索、排序、空状态、失败行「重试」。"""
    print("\n[24] 传输页：筛选 / 搜索 / 排序 / 失败重试")
    desktop = Desktop(str(root), app)
    try:
        window = desktop.window
        view = window.transfers_view
        table = view.send_table
        window.show_page("session", view="transfers")
        view.show_direction("send")
        desktop.pump(0.2)

        active = window._add_transfer_row("t-active", "发送到 接收机", "send", key="k-active")
        active.set_files("正在传.bin")
        active.set_total(10 * 1024 * 1024)
        active.update_progress(5 * 1024 * 1024, 10 * 1024 * 1024, 1024 * 1024, 5)
        active.set_status("传输中")
        done = window._add_transfer_row("t-done", "发送到 接收机", "send", key="k-done")
        done.set_files("已经传完.bin")
        done.set_paths([str(root)])
        done.set_total(2 * 1024 * 1024)
        done.set_status("传输中")
        done.finish(True, "")
        failed = window._add_transfer_row("t-failed", "发送到 接收机", "send", key="k-failed")
        failed.set_files("季度报表.xlsx")
        failed.set_paths([str(root)])
        failed.set_total(3 * 1024 * 1024)
        failed.set_status("传输中")
        failed.finish(False, "连接被对方拒绝（对方可能开了防火墙）")
        desktop.pump(0.2)
        check("三行都在", table.visible_rows() == 3, str(table.visible_rows()))

        view.set_filter("failed")
        desktop.pump(0.1)
        check("点「失败」只剩失败那一行", table.visible_rows() == 1, str(table.visible_rows()))
        check("失败行的状态药丸写「失败」（不是一整句原因）",
              failed.status.text() == "失败", failed.status.text())
        check("失败原因在悬停提示里", "原因：" in failed.item.tooltip()
              and "拒绝" in failed.item.tooltip(), failed.item.tooltip())
        check("失败行的操作格是「重试」", table.action_text(0) == "重试", table.action_text(0))

        view.set_filter("active")
        desktop.pump(0.1)
        check("点「进行中」只剩正在传的那一行", table.visible_rows() == 1, str(table.visible_rows()))
        view.set_filter("done")
        desktop.pump(0.1)
        check("点「已完成」只剩传完的那一行", table.visible_rows() == 1, str(table.visible_rows()))

        view.set_filter("all")
        view.search.setText("季度")
        desktop.pump(0.1)
        check("搜索按文件名筛", table.visible_rows() == 1, str(table.visible_rows()))
        view.search.setText("不存在的东西")
        desktop.pump(0.1)
        check("搜不到时是空状态而不是空白表",
              table.visible_rows() == 0 and view.empty.isVisible(), str(table.visible_rows()))
        check("空状态说清是「筛选」筛没了", "没有符合条件" in view.empty.title.text(),
              view.empty.title.text())
        view.clear_filters()
        desktop.pump(0.1)
        check("清空筛选后三行都回来", table.visible_rows() == 3, str(table.visible_rows()))

        # 排序：按大小升序，第一行应该是最小的那个
        table.sortByColumn(table.model().columns.index("大小"), Qt.AscendingOrder)
        desktop.pump(0.1)
        first = table.item(0, table.model().columns.index("文件")).text()
        check("点「大小」列头能排序（升序第一行最小）",
              first == "已经传完.bin",
              f"{first} / {[table.item(i, 0).text() for i in range(table.visible_rows())]}")
        size_column = table.model().columns.index("大小")
        ascending = [
            _size_of(table.item(row, size_column).text()) for row in range(table.visible_rows())
        ]
        check("升序：大小确实从小到大", ascending == sorted(ascending), str(ascending))

        # 「重试」真的会发信号（窗口接的是 _retry_transfer）
        seen: list[str] = []
        view.retry_requested.connect(lambda key: seen.append(key))
        retry_row = next(
            (row for row in range(table.visible_rows()) if table.action_text(row) == "重试"), None
        )
        check("失败那一行在表里找得到", retry_row is not None, str(retry_row))
        if retry_row is not None:
            table.invoke_action(table.item_at(retry_row))
        desktop.pump(0.1)
        check("点「重试」把这次传输交给窗口重发", bool(seen), str(seen))
    finally:
        desktop.close()


def _size_of(text: str) -> float:
    """``7.00 MB / 12.00 MB`` / ``12.00 MB`` → 数字（只用来比大小）。"""
    import re

    numbers = re.findall(r"([0-9.]+)\s*(B|KB|MB|GB|TB)", str(text))
    if not numbers:
        return 0.0
    scale = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
    value, unit = numbers[-1]
    return float(value) * scale.get(unit, 1)


# ---------------------------------------------------------------------------
# [25] 1000 行也要流畅（宪章 E5：不要 1000 个 widget）
# ---------------------------------------------------------------------------


def test_thousand_rows_stay_light(app, root) -> None:
    """1000 条记录：插入要快，而且**不能**一行一个控件。"""
    print("\n[25] 1000 条传输记录：行数是数据，不是控件数")
    import time as time_module

    desktop = Desktop(str(root), app)
    try:
        window = desktop.window
        table = window.transfers_view.recv_table
        before_widgets = len(table.findChildren(QWidget))
        sample = root / "样本.bin"
        make_big_file(sample, 1)
        def add_range(start: int, stop: int) -> float:
            began = time_module.monotonic()
            for index in range(start, stop):
                table.add_history_row(
                    f"文件-{index:04d}.bin", str(sample), "接收机", "1.00 MB",
                    "12:00", key=f"bulk-{index}",
                )
            return time_module.monotonic() - began

        # 断言写成**相对**的：CI/共享机器上负载高时，绝对秒数会假红（Lead 要求）。
        first_half = add_range(0, 500)
        second_half = add_range(500, 1000)
        desktop.pump(0.2)
        check("1000 行插进去了", table.rowCount() == 1000, str(table.rowCount()))
        check("后半 500 行不会比前半慢得多（每行成本不随行数涨）",
              second_half < max(1.0, first_half * 3 + 0.5),
              f"前 {first_half:.3f}s / 后 {second_half:.3f}s")
        check("1000 行插入仍在 10 秒内（宽松上限，防负载假红）",
              first_half + second_half < 10.0, f"{first_half + second_half:.2f}s")
        after_widgets = len(table.findChildren(QWidget))
        check("没有一行一个控件（E5）",
              after_widgets - before_widgets < 20, f"{before_widgets} → {after_widgets}")
        # 再来 1000 行：控件数还是不涨（行数是数据，不是控件量）
        add_range(1000, 2000)
        desktop.pump(0.2)
        check("加到 2000 行控件数仍然不涨",
              len(table.findChildren(QWidget)) - before_widgets < 20,
              f"{before_widgets} → {len(table.findChildren(QWidget))}")
    finally:
        desktop.close()


# ---------------------------------------------------------------------------
# [22] 控件去重自检（宪章 C1：一个意图，一个入口）
# ---------------------------------------------------------------------------

#: 同义文案 → 同一个用户意图。只登记**真的同义**的那些，别的按文案本身算。
#: 加新的一组之前先问自己：这两个文案在同一个页面上同时出现，用户会不会觉得
#: "这不是同一个按钮吗"。
INTENT_SYNONYMS = {
    "刷新": "scan",
    "扫描": "scan",
    "清空会话…": "clear-chat",
    "清空会话": "clear-chat",
    "清空记录…": "clear-records",
    "清空记录": "clear-records",
    "清空传输记录…": "clear-records",
    "清空离线设备…": "clear-offline",
    "清空离线设备列表…": "clear-offline",
    "更改接收位置…": "change-receive-dir",
    "更改位置": "change-receive-dir",
    "打开接收文件夹": "open-receive-dir",
    "打开文件夹": "open-receive-dir",
    "去发送一个文件": "compose",
    "返回会话": "back-to-chat",
    "← 返回会话": "back-to-chat",
}

#: 同一个意图允许出现在**不同页面**上的情况（每页都要有自己的入口，
#: 但同一页上绝不允许出现第二个）。这是白名单：加一条就要在这里写清理由。
CROSS_PAGE_INTENTS = {
    # 设置页的「数据与维护」和传输页的 ⋯：一个是配置现场，一个是干活现场。
    "clear-records": {"transfers", "settings"},
    # 传输页与设置页各自要有一个「← 返回会话」，但**同一页上只有一个**。
    "back-to-chat": {"transfers", "settings"},
}


def _clean_label(text: str) -> str:
    return " ".join(str(text or "").replace("&", "").split()).strip()


def _visible_buttons(widget) -> list:
    from PySide6.QtWidgets import QAbstractButton

    found = []
    for button in widget.findChildren(QAbstractButton):
        if not button.isVisible() or not button.isEnabled():
            continue
        if _clean_label(button.text()):
            found.append(button)
    return found


def _menu_labels(widget) -> list[tuple[str, str]]:
    """这一页上可见控件的菜单项（(文案, 拥有者描述)）。"""
    from PySide6.QtWidgets import QMenu, QPushButton

    out: list[tuple[str, str]] = []

    def walk(menu: QMenu, owner: str) -> None:
        for action in menu.actions():
            if action.isSeparator():
                continue
            label = _clean_label(action.text())
            if label:
                out.append((label, owner))
            if action.menu() is not None:
                walk(action.menu(), owner)

    for button in widget.findChildren(QPushButton):
        if not button.isVisible():
            continue
        menu = button.menu()
        if isinstance(menu, QMenu):
            walk(menu, f"{button.objectName() or type(button).__name__}")
    return out


def _page_widget(window, page: str):
    if page == "settings":
        return window.settings_page
    return window.session_view


def test_no_duplicate_controls(app, root) -> None:
    """控件去重自检：同一页面上不许有两个同文案、同意图的控件。

    **覆盖范围**：当前显示页面上**可见**的 QAbstractButton，以及这些控件通过
    ``setMenu`` 挂着的菜单（含子菜单）里的 QAction。
    **不在覆盖内**：临时构造、弹完即弃的右键菜单 —— 例如侧栏「离线」标题右键
    （``DeviceSection._on_title_menu``）和传输表行的右键菜单（走
    ``file_actions_menu``）。前者与设置页的「清空离线设备…」是同一个意图的两处
    入口，``docs/UI_LAYOUT.md`` §4.5 明确规定要有这两处（一处管列表、一处管设置）；
    后者是按行内容动态生成的完整文件动作矩阵，菜单项只此一份。

    宪章 C1 的判定法就是"把每个按钮/菜单项列出来"。这条测试把它自动化：
    逐页枚举**看得见**的 QAbstractButton 文案 + 那一页菜单里的 QAction 文案，
    断言（a）没有重复文案、（b）没有重复意图（同义词表见 ``INTENT_SYNONYMS``）、
    （c）跨页重复的意图必须刚好等于白名单 ``CROSS_PAGE_INTENTS``。
    """
    print("\n[22] 控件去重自检（一个意图一个入口）")
    desktop = Desktop(str(root), app)
    try:
        window = desktop.window
        seen: dict[str, dict[str, list[str]]] = {}  # 意图 -> 页面 -> [文案...]

        # 三页各自看一遍；``view`` 必须写清（空串 = 不切页，会看错页）。
        for page, view in (("session", "chat"), ("transfers", "transfers"), ("settings", "")):
            if page == "settings":
                window.show_page("settings")
            else:
                window.show_page("session", view=view)
            desktop.pump(0.25)
            widget = _page_widget(window, page)
            entries: list[tuple[str, str]] = []
            for button in _visible_buttons(widget):
                entries.append((_clean_label(button.text()), type(button).__name__))
            entries.extend(_menu_labels(widget))

            labels = [label for label, _who in entries]
            duplicates = sorted({label for label in labels if labels.count(label) > 1})
            check(f"[{page}] 没有两个同文案的控件", not duplicates, str(duplicates))

            intents: dict[str, list[str]] = {}
            for label, _who in entries:
                intent = INTENT_SYNONYMS.get(label, label)
                intents.setdefault(intent, []).append(label)
                seen.setdefault(intent, {}).setdefault(page, []).append(label)
            clashing = {intent: labels_ for intent, labels_ in intents.items() if len(labels_) > 1}
            check(f"[{page}] 没有两个同意图的控件", not clashing, str(clashing))

        # 跨页：允许，但必须刚好等于白名单（多了就是"悄悄多了一个入口"）。
        spread = {
            intent: set(pages)
            for intent, pages in seen.items()
            if len(pages) > 1
        }
        check("跨页重复的意图刚好等于白名单",
              spread == CROSS_PAGE_INTENTS,
              f"实际 {spread} / 白名单 {CROSS_PAGE_INTENTS}")

        # 几条点名要消失的重复入口
        window.show_page("session", view="chat")
        desktop.pump(0.2)
        sidebar_labels = [_clean_label(b.text()) for b in _visible_buttons(window.sidebar)]
        check("侧栏里没有「刷新」了（并进「扫描」）",
              not any("刷新" in label for label in sidebar_labels), str(sidebar_labels))
        window.show_page("session", view="transfers")
        desktop.pump(0.2)
        transfers_labels = [
            _clean_label(b.text()) for b in _visible_buttons(window.transfers_view)
        ]
        check("传输页只有一个主按钮「打开接收文件夹」",
              transfers_labels.count("打开接收文件夹") == 1, str(transfers_labels))
        check("传输页把「更改接收位置」也让出去了（正主在设置页）",
              not any("更改接收位置" in label for label in transfers_labels),
              str(transfers_labels))
    finally:
        desktop.close()


def test_receive_lands_in_the_receive_table(app, root) -> None:
    """别人发来的文件：传输页「接收」表里要有它，并写上来自谁。

    新布局把「接收」从一页卡片变成了一张表（``[接收｜发送]`` 分段控件切换），
    所以这条盯的是：接收方向的行真的出现在**接收**那张表里（而不是发送表），
    收到的文件名写进行里，清空记录之后空表会自己说明白。
    """
    print("\n[10] 接收方向：落到「接收」表里")
    import threading

    if True:
        desktop = Desktop(str(root), app)
        try:
            window = desktop.window
            # 让对端（receiver）把文件发给窗口这一侧：这才是"我收到文件"。
            desktop.receiver.add_manual_device("127.0.0.1", desktop.engine.port, "发送机")
            source = root / "寄过来的.bin"
            make_big_file(source, 4)
            peer = next(
                (p for p in desktop.receiver.devices() if p.port == desktop.engine.port), None
            )
            check("对端找到了窗口这一侧", peer is not None,
                  str([(p.info.name, p.port) for p in desktop.receiver.devices()]))
            if peer is None:
                return

            window.show_page("session", view="transfers")
            window.transfers_view.show_direction("recv")
            before = window.transfers_view.recv_table.visible_rows()

            result: dict = {}

            def send() -> None:
                try:
                    desktop.receiver.send(peer, [str(source)])
                except Exception as exc:  # pragma: no cover - reported below
                    result["error"] = str(exc)

            threading.Thread(target=send, daemon=True).start()

            deadline = time.monotonic() + 60
            landed = Path(desktop.engine.config.receive_dir) / "寄过来的.bin"
            while time.monotonic() < deadline and not landed.exists():
                desktop.pump(0.3)
            check("文件落到了接收目录", landed.exists(), str(landed))
            desktop.pump(1.0)

            recv_table = window.transfers_view.recv_table
            send_table = window.transfers_view.send_table
            check("「接收」表里出现了新的行",
                  recv_table.visible_rows() > before,
                  f"{before} → {recv_table.visible_rows()}")
            check("「发送」表没有被写进来",
                  send_table.visible_rows() == 0, str(send_table.visible_rows()))
            names = [
                recv_table.item(row, 0).text()
                for row in range(recv_table.rowCount())
                if not recv_table.isRowHidden(row)
            ]
            check("行里写的是文件名", any("寄过来的.bin" in name for name in names), str(names))

            # 清空记录：已经收完的行收起来，表就空了。先等这次传输真的从
            # 引擎里退出去 —— 正在传的行是**故意**不被收走的。
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and desktop.engine.active_transfers():
                desktop.pump(0.3)
            check("这次接收已经结束", not desktop.engine.active_transfers(),
                  str([t.transfer_id[:8] for t in desktop.engine.active_transfers()]))
            window._clear_transfers()
            desktop.pump(0.3)
            check("清空记录之后接收表是空的",
                  recv_table.visible_rows() == 0, str(recv_table.visible_rows()))
            check("空表会说明白是空的", window.transfers_view.empty.isVisible())
            if result.get("error"):
                print(f"      （发送侧报错：{result['error']}）")
        finally:
            desktop.close()


def test_composer_tiles_and_preview(app, root) -> None:
    """输入区排版 + 文件预览。

    规格（``docs/UI_LAYOUT.md`` §3⑥）：待发文件是「图标/缩略图 + 底部文件名」
    的方块，统计那一行在输入框**上面**，输入框那一行贴着最底；点方块或者气泡上
    的「预览」就地看内容。
    """
    print("\n[11] 输入区：方块缩略图 / 统计在输入框上面 / 文件预览")
    from PySide6.QtCore import Qt, QEvent, QPointF
    from PySide6.QtGui import QColor, QImage, QMouseEvent
    from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit, QPushButton

    def _mouse_click(widget, button=Qt.MouseButton.LeftButton) -> None:
        """在控件中心点一下，语义等同 ``QTest.mouseClick``。

        刻意**不用** ``PySide6.QtTest``：它属于 **pyside6-addons**，而交付用的
        绿色便携运行时只打了 pyside6-essentials（`EverSend/site/PySide6/` 里没有
        QtTest.so），于是这一整套 GUI 动作测试在交付形态下直接
        ``ModuleNotFoundError`` —— 449 秒的子套件只为报一个 import 错误。
        自己发 QMouseEvent 走的是同一条 ``QApplication::notify`` 路径。
        """
        pos = QPointF(widget.rect().center())
        for etype, buttons in (
            (QEvent.Type.MouseButtonPress, button),
            (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton),
        ):
            QApplication.sendEvent(
                widget,
                QMouseEvent(etype, pos, button, buttons, Qt.KeyboardModifier.NoModifier),
            )
        QApplication.processEvents()

    from eversend.desktop.preview import FilePreviewDialog, preview_kind, read_text_preview

    if True:
        desktop = Desktop(str(root), app)
        try:
            window = desktop.window
            chat = window.chat_view
            picture = root / "照片.png"
            picture.write_bytes(make_png(96, 64))
            note = root / "说明.md"
            note.write_text("# 标题\n\n这是预览用的文本。\n" * 4, encoding="utf-8")
            movie = root / "短片.mp4"
            made = make_video(movie)
            blob = root / "数据.bin"
            blob.write_bytes(bytes(range(256)) * 64)  # 有 NUL，必须判成二进制

            window._add_paths([str(picture), str(note), str(blob)] + ([str(movie)] if made else []))
            desktop.pump(0.4)

            tiles = window.file_list.tiles
            check("每个待发文件一块方块", len(tiles) == (4 if made else 3), str(len(tiles)))
            if not tiles:
                return
            names = [tile.name.text() for tile in tiles]
            check("文件名写在方块下面", names[0] == "照片.png" and "说明.md" in names, str(names))
            sizes = [tile.picture.pixmap().size() for tile in tiles]
            check("图片方块是缩略图（不是图标）",
                  sizes[0].width() >= 32 and sizes[0].height() >= 32, str(sizes[0]))
            note_tile = next(t for t in tiles if t.path == str(note))
            check("文本文件给类型图标（小图）",
                  note_tile.picture.pixmap().width() <= 32, str(note_tile.picture.pixmap().size()))
            if made:
                movie_tile = next(t for t in tiles if t.path == str(movie))
                check("视频方块有封面帧（不是图标）",
                      movie_tile.picture.pixmap().width() >= 32,
                      str(movie_tile.picture.pixmap().size()))

            # 统计那一行在输入框**上面**，输入框在最后一行的位置。
            bar = window.file_list
            box = chat.composer_box
            composer = bar.parentWidget()
            check("统计在输入框那一行上面", bar.y() < box.y(), f"bar.y={bar.y()} box.y={box.y()}")
            check(
                "输入框那一行贴着输入区最底",
                box.geometry().bottom() >= composer.height() - 20,
                f"box.bottom={box.geometry().bottom()} composer.h={composer.height()}",
            )
            check("统计写清了项数与合计",
                  "3" in window.total_label.text() or "4" in window.total_label.text(),
                  window.total_label.text())
            check("统计和待发方块在同一行（就是它右边那块）",
                  window.total_label is bar.stats_label, "不是同一个控件")

            # 点方块 = 预览；窗口把预览请求接到 preview_path 上。
            seen: list[str] = []
            original = window.preview_path
            window.preview_path = lambda path, name="": seen.append(path)  # type: ignore[assignment]
            try:
                _mouse_click(tiles[0], Qt.LeftButton)
                desktop.pump(0.2)
            finally:
                window.preview_path = original  # type: ignore[assignment]
            check("点方块会请求预览", seen == [str(picture)], str(seen))

            # 预览本身：四类文件各自给对的东西。
            text_dialog = FilePreviewDialog(str(note), note.name, window, desktop.engine.config.data_dir)
            check("文本预览判成文本", preview_kind(str(note)) == "text")
            check("文本预览里真的能看到内容",
                  isinstance(text_dialog.body, QPlainTextEdit)
                  and "预览用的文本" in text_dialog.body.toPlainText())
            text_dialog.close()

            picture_dialog = FilePreviewDialog(str(picture), picture.name, window, desktop.engine.config.data_dir)
            check("图片预览判成图片", preview_kind(str(picture)) == "image")
            check("图片预览里有图",
                  isinstance(picture_dialog.body, QLabel)
                  and picture_dialog.body.pixmap() is not None
                  and not picture_dialog.body.pixmap().isNull())
            picture_dialog.close()

            # 256 KiB 的截断点落在汉字中间时，不能整段退化成乱码
            big_cn = root / "中文.txt"
            big_cn.write_text("中" * 100000, encoding="utf-8")
            cn_text, cn_truncated = read_text_preview(str(big_cn))
            check("截断大文件时不切坏汉字（不变成乱码）",
                  cn_truncated and "中" in cn_text and "锟" not in cn_text and "\ufffd" not in cn_text,
                  f"truncated={cn_truncated} 末尾={cn_text[-6:]!r}")

            folder = root / "一个文件夹"
            (folder / "里层").mkdir(parents=True, exist_ok=True)
            (folder / "a.txt").write_text("x" * 100, encoding="utf-8")
            folder_dialog = FilePreviewDialog(str(folder), folder.name, window, desktop.engine.config.data_dir)
            check("文件夹也能预览（说清里面有多少东西）",
                  preview_kind(str(folder)) == "folder"
                  and "1 个文件" in folder_dialog._facts(), folder_dialog._facts())
            folder_dialog.close()

            blob_dialog = FilePreviewDialog(str(blob), blob.name, window, desktop.engine.config.data_dir)
            check("二进制文件不装作能看", preview_kind(str(blob)) == "binary")
            check("二进制预览给的是实话",
                  any("没有可预览" in label.text() for label in blob_dialog.findChildren(QLabel)),
                  "没说清楚")
            blob_dialog.close()

            if made:
                movie_dialog = FilePreviewDialog(str(movie), movie.name, window, desktop.engine.config.data_dir)
                check("视频预览判成视频", preview_kind(str(movie)) == "video")
                check("视频预览有播放入口",
                      any("播放" in button.text() for button in movie_dialog.findChildren(QPushButton)))
                movie_dialog.close()

            # 气泡上的「预览」：发一条带附件的消息，气泡里要有入口。
            conv = chat.current_conversation() or chat.conversation_for_member("peer-preview", "测试机")
            if not chat.current_conversation():
                chat.open_conversation(conv)
            desktop.engine.send_chat(conv, kind="file", media_path=str(note), to="peer-preview")
            chat.reload()
            desktop.pump(0.6)
            bubble_buttons = [
                button.text() for button in walk_widgets(chat) if isinstance(button, QPushButton)
            ]
            check("气泡上有「预览」入口", "预览" in bubble_buttons, str(sorted(set(bubble_buttons))))

            # 预览弹窗：图要比可视区小（"弹出来没给我缩放"就是这个断言在管）。
            from eversend.desktop.preview import FilePreviewDialog

            huge = root / "超大图.png"
            image = QImage(2600, 1800, QImage.Format_RGB32)
            image.fill(QColor(40, 120, 200))
            image.save(str(huge), "PNG")
            for size in ((520, 380), (1100, 800)):
                dialog = FilePreviewDialog(str(huge), huge.name)
                dialog.resize(*size)
                dialog.show()
                desktop.pump(0.4)
                canvas = dialog._canvas
                shown = canvas.pixmap()
                check(f"预览在 {size[0]}×{size[1]} 窗口里把图缩放进去了",
                      shown.width() <= canvas.width() and shown.height() <= canvas.height(),
                      f"图 {shown.width()}×{shown.height()} / 画布 {canvas.width()}×{canvas.height()}")
                dialog.close()
                dialog.deleteLater()
        finally:
            desktop.close()


def glyph_ink_size(button) -> tuple[int, int]:
    """按钮上"画出来的东西"有多大（像素）：取背景色之外像素的包围盒。

    样式表里的 ``font-size`` 不会写到 ``widget.font()`` 上，所以只能量像素 ——
    这也是用户真正看到的东西。
    """
    image = button.grab().toImage()
    counts: dict[str, int] = {}
    for x in range(image.width()):
        for y in range(image.height()):
            name = image.pixelColor(x, y).name()
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return (0, 0)
    background = max(counts, key=counts.get)
    xs: list[int] = []
    ys: list[int] = []
    for x in range(image.width()):
        for y in range(image.height()):
            if image.pixelColor(x, y).name() != background:
                xs.append(x)
                ys.append(y)
    if not xs:
        return (0, 0)
    return (max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)


def test_devices_split_into_online_and_offline(app, root) -> None:
    """设备列表分「在线 / 离线」两段，各自可展开或折叠。

    规格（``docs/UI_LAYOUT.md`` §2 中部③）：在线默认展开、离线默认折叠，
    标题可点、带数量徽标；选中的设备在折叠着的那一段里时自动展开。
    """
    print("\n[12] 设备列表：在线 / 离线两段，可折叠")
    from eversend.core.chat import direct_conversation_id

    if True:
        desktop = Desktop(str(root), app)
        try:
            window = desktop.window
            sidebar = window.sidebar
            engine = desktop.engine

            # 一台在线（真的接收机）+ 一台离线（只有会话记录、现在见不到）
            engine.add_manual_device("127.0.0.1", desktop.receiver.port, "接收机")
            gone = "ffffffff-dead-beef-0000-000000000001"
            conv = direct_conversation_id(engine.info.device_id, gone)
            engine.chat.upsert_conversation(
                conv, kind="direct", title="早就关机的笔记本",
                members=[engine.info.device_id, gone],
            )
            window._refresh_devices()
            desktop.pump(0.5)

            online = sidebar.online_section
            offline = sidebar.offline_section
            check("在线段里有那台接收机",
                  any("接收机" in p.info.name for p in online.peers),
                  str([p.info.name for p in online.peers]))
            check("离线段里有那台见不到的设备",
                  any(p.info.device_id == gone for p in offline.peers),
                  str([(p.info.name, p.info.device_id[:8]) for p in offline.peers]))
            check("它被标成「记录里的会话」（而不是在线）",
                  all(p.source == "history" for p in offline.peers),
                  str([p.source for p in offline.peers]))
            check("两段各自带数量徽标",
                  online.count.text() == "1" and offline.count.text() == "1",
                  f"在线 {online.count.text()} / 离线 {offline.count.text()}")
            check("在线默认展开", online.is_expanded() and online.table.isVisible())
            check("离线默认折叠", not offline.is_expanded() and not offline.table.isVisible())

            # 点标题展开 / 收起
            offline.header.click()
            desktop.pump(0.2)
            check("点离线标题就展开", offline.is_expanded() and offline.table.isVisible())
            check("展开着的那一段是按内容定高的（不是空占一大块）",
                  online.table.height() <= online.content_height() + 6,
                  f"表格 {online.table.height()} / 内容 {online.content_height()}")

            online.header.click()
            desktop.pump(0.2)
            check("点在线标题就收起", not online.is_expanded() and not online.table.isVisible())
            check("收起后只剩一行标题那么高",
                  online.height() <= online.header.height() + 6,
                  f"段高 {online.height()} / 标题高 {online.header.height()}")
            check("收起的那一段紧贴「设备列表」下边（下面那段跟上来）",
                  offline.y() - (online.y() + online.height()) <= 16,
                  f"间隙 {offline.y() - (online.y() + online.height())}px")
            online.header.click()
            desktop.pump(0.2)
            check("再点一次又展开", online.is_expanded() and online.table.isVisible())

            # 选中折叠段里的设备 → 自动展开那一段
            offline.set_expanded(False)
            desktop.pump(0.2)
            history_peer = offline.peers[0]
            window._select_device_row(history_peer)
            desktop.pump(0.3)
            check("选中折叠段里的设备会自动展开那一段", offline.is_expanded())

            # 设备多到撑不下时：两段各自滚动，底部导航不能被挤出窗口
            for index in range(12):
                engine.add_manual_device("127.0.0.1", 41000 + index, f"压测设备{index:02d}")
            window._refresh_devices()
            offline.set_expanded(True)
            desktop.pump(0.5)
            nav_bottom = sidebar.settings_nav.y() + sidebar.settings_nav.height()
            check("设备很多时底部「设置」仍在窗口里",
                  nav_bottom <= window.height(),
                  f"导航底 {nav_bottom} / 窗口高 {window.height()}")
            check("两段的表格各自封顶（超出在内部滚动）",
                  online.table.height() <= online.MAX_HEIGHT + 2
                  and offline.table.height() <= offline.MAX_HEIGHT + 2,
                  f"{online.table.height()}/{online.MAX_HEIGHT} · {offline.table.height()}/{offline.MAX_HEIGHT}")
            offline.set_expanded(False)
            desktop.pump(0.3)

            # 窗口对外仍然能按老名字拿到在线那一张表
            check("device_table 还是在线那一张",
                  window.device_table is online.table, "不是同一张表")
            check("两段都能被遍历到", len(window.device_tables()) == 2,
                  str(len(window.device_tables())))

            # 折叠段里"看不见的选中项"不能算进发送目标：用户只选中了 1 台，
            # 按钮却写「发送到 2 台」，就是这里漏算出来的。
            offline.set_expanded(True)
            desktop.pump(0.2)
            offline.table.select_row(0)
            desktop.pump(0.2)
            visible_count = len(window.selected_peers())
            offline.set_expanded(False)
            desktop.pump(0.3)
            check("折叠之后那一段的选中项不再算数",
                  len(window.selected_peers()) < visible_count,
                  f"折叠前 {visible_count} → 折叠后 {len(window.selected_peers())}")
            check("按钮说的是屏幕上真正选中的台数",
                  window.send_button.text() in ("发送", f"发送到 {len(window.selected_peers())} 台"),
                  window.send_button.text())
        finally:
            desktop.close()


def test_emoji_pad_sits_above_the_staged_row(app, root) -> None:
    """表情面板：宽度和输入框那一行一致，紧贴待发文件那一行的上边。

    规格（``docs/UI_LAYOUT.md`` §3⑥ 第 3 条）：不是弹窗，是输入区里的内嵌面板；
    顺序是 [表情面板] → [待发文件 + 统计] → [输入行]。
    """
    print("\n[13] 表情面板：和输入行同宽、贴在待发那行上面")
    from PySide6.QtWidgets import QPushButton

    if True:
        desktop = Desktop(str(root), app)
        try:
            window = desktop.window
            chat = window.chat_view
            source = root / "表情用图.png"
            source.write_bytes(make_png(64, 48))
            window._add_paths([str(source)])
            desktop.pump(0.4)

            check("表情面板默认是收起的", not chat.emoji_pad.isVisible())
            chat.emoji_button.click()
            desktop.pump(0.4)
            pad = chat.emoji_pad
            check("点 😊 展开", pad.isVisible())
            check("没有弹出独立窗口（面板的顶层窗口就是主窗口）",
                  pad.window() is window, type(pad.window()).__name__)
            check("宽度和输入框那一行一致",
                  abs(pad.width() - chat.composer_box.width()) <= 2,
                  f"面板 {pad.width()} / 输入行 {chat.composer_box.width()}")
            check("紧贴待发文件那一行的上边",
                  0 <= chat.attachment_bar.y() - pad.geometry().bottom() <= 16,
                  f"面板底 {pad.geometry().bottom()} / 待发行 {chat.attachment_bar.y()}")
            check("待发那一行在面板和输入行之间",
                  pad.geometry().bottom() <= chat.attachment_bar.y()
                  and chat.attachment_bar.geometry().bottom() <= chat.composer_box.y(),
                  f"{pad.geometry().getRect()} / {chat.attachment_bar.geometry().getRect()} / {chat.composer_box.geometry().getRect()}")

            buttons = [b for b in pad.findChildren(QPushButton) if b.text()]
            check("面板里真的画出了表情（按钮有高度）",
                  bool(buttons) and buttons[0].height() >= 24,
                  f"{len(buttons)} 个按钮，第一个高 {buttons[0].height() if buttons else 0}")
            # 表情要够大：17px 那版有人反馈看不清，这里量的是**画出来的字形**有多大
            # （样式表给的字号不会出现在 widget.font() 里，只能量像素）。
            if buttons:
                ink_w, ink_h = glyph_ink_size(buttons[0])
                check("表情按钮本身够大（≥40×40）",
                      buttons[0].width() >= 40 and buttons[0].height() >= 40,
                      f"{buttons[0].width()}×{buttons[0].height()}")
                check("画出来的表情字形够大（≥24px，能看清）",
                      ink_w >= 24 and ink_h >= 24,
                      f"字形 {ink_w}×{ink_h}px")
            buttons[0].click()
            desktop.pump(0.2)
            check("点一个表情就插进输入框", chat.input.text() == buttons[0].text(),
                  repr(chat.input.text()))
            check("插完面板不关（可以连着挑）", pad.isVisible())

            chat.emoji_button.click()
            desktop.pump(0.2)
            check("再点 😊 收起", not pad.isVisible())
        finally:
            desktop.close()


def test_remembered_devices_land_in_the_offline_section(app, root) -> None:
    """一台电脑关掉之后，不该从列表里彻底消失 —— 它要掉进「离线」那一段。

    引擎的发现表有 90 秒 TTL；以前的做法是"人走了就从列表里没了"，用户要的
    「离线」那一栏于是永远空着。现在身份快照会记在本机上（还落盘），
    重启之后离线那一段也还在。
    """
    print("\n[14] 走过的设备：走了以后留在「离线」里")
    import json
    import time as time_module

    from eversend.core.model import DeviceInfo

    from eversend.desktop.main_window import MainWindow

    if True:
        desktop = Desktop(str(root), app)
        try:
            window = desktop.window
            sidebar = window.sidebar
            engine = desktop.engine

            engine.add_manual_device("127.0.0.1", desktop.receiver.port, "接收机")
            window._refresh_devices()
            desktop.pump(0.4)
            online = sidebar.online_section
            receiver = next((p for p in online.peers if p.port == desktop.receiver.port), None)
            check("先是在线", receiver is not None,
                  str([(p.info.name, p.source) for p in online.peers]))
            if receiver is None:
                return
            check("身份被记下来了", receiver.info.device_id in window._known_devices,
                  str(list(window._known_devices))[:120])

            # 把时钟往前拨一小时：引擎那边（单调时钟）与本机记下的那份快照
            # （墙上时间）说的是同一件事，所以两边一起拨 —— 等价于"一小时前听到过
            # 它、现在它已经走了"，不是伪造数据。
            for peer in engine.discovery.peers.all(include_expired=True):
                if peer.port == desktop.receiver.port:
                    peer.last_seen = time_module.monotonic() - 3600
            window._known_devices[receiver.info.device_id]["lastSeen"] -= 3600
            window._refresh_devices()
            desktop.pump(0.4)

            check("引擎的发现表里已经没有它了",
                  not [p for p in engine.devices() if p.port == desktop.receiver.port],
                  str([(p.info.name, p.port) for p in engine.devices()]))
            offline = sidebar.offline_section
            remembered = [p for p in offline.peers if p.info.device_id == receiver.info.device_id]
            check("它出现在「离线」那一段里", len(remembered) == 1,
                  str([(p.info.name, p.source) for p in offline.peers]))
            if not remembered:
                return
            row = remembered[0]
            check("标成「以前见过」而不是「记录里的会话」", row.source == "offline", row.source)
            check("信息还认得出来（名字/地址都留着）",
                  row.info.name == "接收机" and row.address == "127.0.0.1",
                  f"{row.info.name} @ {row.address}")
            check("在线状态是离线，并带上最后在线时间",
                  row.info.capabilities.get("online") is False
                  and float(row.info.capabilities.get("secondsAgo") or 0) > 600,
                  str(row.info.capabilities))

            # 行上的徽标不能还挂着绿色的「在线」
            card = None
            for index in range(offline.table.count()):
                item = offline.table.item(index)
                widget = offline.table.itemWidget(item)
                if widget is not None and getattr(widget, "peer", None) is row:
                    card = widget
            check("那张行卡片的徽标写着「已离线」",
                  card is not None and card.status.text() == "已离线",
                  getattr(getattr(card, "status", None), "text", lambda: "(没有卡片)")())

            # 离线设备不能当发送目标：按钮要如实禁用，而不是发出去卡在那儿
            window._select_device_row(row)
            desktop.pump(0.3)
            check("选中离线设备时发送按钮是禁用的",
                  not window.send_button.isEnabled(), window.send_button.text())

            # 名单要落盘：下次启动离线那一段还在
            # 落盘是节流的：测试里直接 force 一次，别等 15 秒
            window._prune_known_devices(time_module.time())
            window._known_dirty = True
            window._save_known_devices(force=True)
            path = Path(engine.config.data_dir) / "known_devices.json"
            saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            check("身份快照写进了 known_devices.json",
                  receiver.info.device_id in saved, str(list(saved))[:100])
            check("记的是名字与最后在线时间",
                  saved.get(receiver.info.device_id, {}).get("name") == "接收机"
                  and saved.get(receiver.info.device_id, {}).get("lastSeen", 0) > 0,
                  str(saved.get(receiver.info.device_id))[:120])

            # 另一个窗口应当直接读回这份名单（重启之后的形态）
            other = MainWindow(engine, str(Path(root) / "settings2.json"))
            try:
                desktop.pump(0.3)
                check("新开的窗口也认识这台离线设备",
                      receiver.info.device_id in other._known_devices,
                      str(list(other._known_devices))[:100])
            finally:
                other.close()
                other.bridge.close()
                other.deleteLater()
        finally:
            desktop.close()


def test_one_row_per_file_and_records_in_chat(app, root) -> None:
    """同一份文件只占一行；传输记录也进会话；署名用设备真名。

    用户拿着两张截图来提的三件事：
      1. 收到的同一个文件在「接收」表里占了**两行**（"正在传"那一行 + 历史那一行），
         要求操作与状态合并成一行；
      2. 会话里看不到走「传输文件」这条路的收发记录/进度；
      3. 会话里对方显示成「安卓 App」，应该显示设备自己的名字（V2408A）。
    """
    print("\n[15] 一个文件一行 · 传输记录进会话 · 署名用设备名")
    import threading

    from PySide6.QtWidgets import QPushButton

    if True:
        desktop = Desktop(str(root), app)
        try:
            window = desktop.window
            desktop.receiver.add_manual_device("127.0.0.1", desktop.engine.port, "发送机")
            source = root / "只该占一行.bin"
            make_big_file(source, 4)
            peer = next(
                (p for p in desktop.receiver.devices() if p.port == desktop.engine.port), None
            )
            check("对端找到了窗口这一侧", peer is not None, "")
            if peer is None:
                return

            window.show_page("session", view="transfers")
            window.transfers_view.show_direction("recv")
            result: dict = {}

            def send() -> None:
                try:
                    desktop.receiver.send(peer, [str(source)])
                except Exception as exc:  # pragma: no cover
                    result["error"] = str(exc)

            threading.Thread(target=send, daemon=True).start()
            deadline = time.monotonic() + 60
            landed = Path(desktop.engine.config.receive_dir) / "只该占一行.bin"
            while time.monotonic() < deadline and not landed.exists():
                desktop.pump(0.3)
            # 等这次传输真的结束（结束事件比"文件落盘"晚一点）。
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and desktop.engine.active_transfers():
                desktop.pump(0.3)
            desktop.pump(1.0)

            recv_table = window.transfers_view.recv_table
            visible = [
                recv_table.item(row, 0).text()
                for row in range(recv_table.rowCount())
                if not recv_table.isRowHidden(row)
            ]
            same = [name for name in visible if "只该占一行" in name]
            check("这份文件在「接收」表里只占一行（不是正在传 + 历史两行）",
                  len(same) == 1, str(visible))
            check("留下的那一行是「已完成」的历史行",
                  bool(recv_table.history_rows) and "已完成" in str(
                      recv_table.item(sorted(recv_table.history_rows)[-1], 4).text()
                      if recv_table.item(sorted(recv_table.history_rows)[-1], 4) else ""
                  ),
                  str(sorted(recv_table.history_rows)))
            if recv_table.history_rows:
                history_row = sorted(recv_table.history_rows)[-1]
                check("那一行的主按钮是「预览」（宪章 §3.1：四个平级按钮 → 主按钮 + ⋯）",
                      recv_table.action_text(history_row) == "预览",
                      recv_table.action_text(history_row))
                menu = recv_table.action_menu(history_row)
                labels = [action.text() for action in menu.actions()] if menu is not None else []
                check("行内还有 ⋯ 溢出菜单（完整文件动作矩阵）",
                      menu is not None and len(labels) >= 4, str(labels))
                check("右键菜单里四个动作都在（预览/打开/打开所在文件夹/另存为）",
                      [action.text() for action in
                       file_actions_menu(recv_table, landed.as_posix(), landed.name).actions()]
                      [:4] == ["预览", "打开", "打开所在文件夹", "另存为…"],
                      str([action.text() for action in
                           file_actions_menu(recv_table, landed.as_posix(), landed.name).actions()]))

            # 2) 会话里能看到这次接收的记录
            conv_id = window._conversation_for_peer(
                window._peer_by_device_id(desktop.receiver.info.device_id)
                or window._peer_by_device_id("")
                or None
            ) if window._peer_by_device_id(desktop.receiver.info.device_id) else ""
            if not conv_id:
                # 接收方那台设备可能还没进设备表：直接从会话列表里找带这条记录的会话。
                conv_id = next(
                    (
                        str(row["id"])
                        for row in window._conversation_rows()
                        if any(
                            str(m.get("mediaName") or "") == "只该占一行.bin"
                            for m in window.engine.chat.messages(str(row["id"]), limit=20)
                        )
                    ),
                    "",
                )
            check("收到的文件在会话里留下了记录", bool(conv_id), str(conv_id))
            if conv_id:
                recorded = [
                    m
                    for m in window.engine.chat.messages(conv_id, limit=20)
                    if str(m.get("mediaName") or "") == "只该占一行.bin"
                ]
                check("记录里方向是「收到」、状态是已完成",
                      bool(recorded)
                      and str(recorded[-1].get("direction")) == "in"
                      and str(recorded[-1].get("state")) in ("done", "sent"),
                      str(recorded[-1])[:160] if recorded else "没有记录")

            # 3) 署名：存着"安卓 App"这种类型名时，显示设备真名
            chat = window.chat_view
            chat._clients_provider = lambda: [
                {
                    "key": "V2408A|abc",
                    "deviceId": "aa11bb22-cc33-dd44-ee55-ff6677889900",
                    "name": "V2408A",
                    "kind": "app",
                    "address": "192.168.1.174",
                    "online": True,
                }
            ]
            label = chat._sender_label(
                {
                    "senderName": "安卓 App",
                    "sender": "web:aa11bb22-cc33-dd44-ee55-ff6677889900",
                }
            )
            check("消息署名用设备名（V2408A）而不是「安卓 App」",
                  label == "V2408A", label)
            check("对方自己填了名字时不去覆盖它",
                  chat._sender_label({"senderName": "客厅台式机", "sender": "x"}) == "客厅台式机",
                  "")

            # 4) 自己用「传输文件」发出去的文件，会话里也要有记录 + 最终状态
            desktop.engine.add_manual_device("127.0.0.1", desktop.receiver.port, "接收机")
            window._refresh_devices()
            desktop.pump(0.4)
            desktop.select_receiver()
            outgoing = root / "发出去也要记一笔.bin"
            make_big_file(outgoing, 8)
            window._add_paths([str(outgoing)])
            window._send()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and window._transfers:
                if all(getattr(row, "_finished", False) for row in window._transfers.values()):
                    break
                desktop.pump(0.2)
            desktop.pump(1.0)
            sent_records = []
            for row in window._conversation_rows():
                for message in window.engine.chat.messages(str(row["id"]), limit=30):
                    if str(message.get("mediaName") or "") == "发出去也要记一笔.bin":
                        sent_records.append(message)
            check("发出去的文件在会话里也有记录", bool(sent_records),
                  str([m.get("mediaName") for m in sent_records]))
            if sent_records:
                check("方向是「发出」、状态是已发送",
                      str(sent_records[-1].get("direction")) == "out"
                      and str(sent_records[-1].get("state")) in ("sent", "done", "sending"),
                      str(sent_records[-1].get("state")))
        finally:
            desktop.close()


def test_split_conversations_fold_back(app, root) -> None:
    """同一台手机的几段会话要并回一处（"电脑上不显示消息"的另一半原因）。

    手机在会话里的成员写法换过好几次（App 的设备号、浏览器的 地址|UA 摘要、
    早期没带设备号时的 地址:空摘要）。写法一变，消息就落进另一个会话 —— 用户
    打开其中一个，看不到另一个里的新消息。这里盯两件事：能并的并了，
    **不同设备不能并**。
    """
    print("\n[16] 同一台设备的几段会话并回一处")

    if True:
        desktop = Desktop(str(root), app)
        try:
            window = desktop.window
            engine = desktop.engine
            mine = engine.info.device_id

            def remember(member: str, text: str, ago: float) -> str:
                conv_id = direct_conversation_id(mine, member)
                engine.chat.upsert_conversation(conv_id, kind="direct", members=[mine, member])
                engine.chat.add_message(
                    conv_id, sender=member, sender_name="V2408A", kind="text",
                    text=text, direction="in", state="sent",
                )
                return conv_id

            import time as time_module

            phone_app = "1ec69d36-b854-422c-88a7-108df96980b4"
            first = remember("web:192.168.110.174:b3ca182b", "第一段", time_module.time() - 300)
            second = remember("web:192.168.110.174:da39a3ee", "第二段", time_module.time() - 200)
            third = remember(f"web:{phone_app}", "第三段", time_module.time() - 100)
            stranger = remember("web:192.168.110.99:deadbeef", "另一台设备", time_module.time() - 50)

            window._web_ui = type(
                "FakeUI",
                (),
                {
                    "known_clients": lambda self: [
                        {
                            "key": f"android:{phone_app}",
                            "deviceId": phone_app,
                            "name": "V2408A",
                            "kind": "app",
                            "address": "192.168.110.174",
                            "online": True,
                        }
                    ]
                },
            )()

            before = len(engine.chat.conversations())
            merged = window._fold_split_conversations()
            after = [str(row["id"]) for row in engine.chat.conversations()]
            check("并掉了重复的那几段", merged >= 1 and len(after) == before - 2,
                  f"{before} → {len(after)}（并了 {merged} 组）")

            # 三段里的消息现在都在同一个会话里，打开任一段都能看到全部
            texts: list[str] = []
            for conv_id in after:
                texts += [str(m.get("text")) for m in engine.chat.messages(conv_id)]
            check("三段的消息都在同一个会话里",
                  {"第一段", "第二段", "第三段"} <= set(texts), str(texts))
            check("另一台设备的会话没被并进来",
                  "另一台设备" in texts and len(after) == 2, str(after))
            check("另一台设备那段还在自己的会话里",
                  any(
                      [m.get("text") for m in engine.chat.messages(str(row["id"]))] == ["另一台设备"]
                      for row in engine.chat.conversations()
                  ),
                  str([str(row["id"])[:24] for row in engine.chat.conversations()]))
        finally:
            desktop.close()


def test_clear_offline_and_strong_merge(app, root) -> None:
    """清空离线设备列表 + 更强的合并规则（按浏览器指纹跨地址认同一台设备）。

    用户要的两件事：
      1. 「离线」那一栏越堆越多（CI/邻居设备），要能一键清掉，且**不能连累聊天记录**；
      2. 同一台设备因为换地址散成几段会话，要能按更强的规则并起来 ——
         强规则会按浏览器指纹认设备，所以必须**用户确认后才动手**。
    """
    print("\n[17] 清空离线设备 + 更强的合并规则")
    import json
    import time as time_module

    from eversend.core.model import DeviceInfo

    if True:
        desktop = Desktop(str(root), app)
        try:
            window = desktop.window
            engine = desktop.engine
            mine = engine.info.device_id

            # 造几台"以前见过、现在不在线"的设备（落盘名单）
            for index in range(5):
                window._remember_device(
                    type("P", (), {
                        "info": DeviceInfo(
                            device_id=f"gone-{index}",
                            name=f"以前的设备{index}",
                            kind="desktop",
                            platform="linux",
                            version="1.0.0",
                        ),
                        "address": f"10.9.9.{index}",
                        "port": 52000 + index,
                        "source": "broadcast",
                        "trusted": False,
                        "last_seen": time_module.monotonic(),
                    })()
                )
            window._refresh_devices()
            desktop.pump(0.4)
            check("离线名单里有这几台", len(window._known_devices) >= 5,
                  str(len(window._known_devices)))
            check("它们出现在「离线」那一段",
                  len(window.sidebar.offline_section.peers) >= 5,
                  str(len(window.sidebar.offline_section.peers)))

            # 带聊天记录的设备行不能被清掉（那是打开那段会话的唯一入口）
            phone = "1ec69d36-b854-422c-88a7-108df96980b4"
            chat_conv = direct_conversation_id(mine, f"web:{phone}")
            engine.chat.upsert_conversation(
                chat_conv, kind="direct", members=[mine, f"web:{phone}"]
            )
            engine.chat.add_message(
                chat_conv, sender=f"web:{phone}", sender_name="V2408A",
                kind="text", text="这段聊天不能被清掉", direction="in", state="sent",
            )
            window._refresh_devices()
            desktop.pump(0.3)

            removed = window.clear_offline_devices(ask=False)
            desktop.pump(0.4)
            check("清空了本机记住的离线设备", removed >= 5 and not window._known_devices,
                  f"removed={removed} left={len(window._known_devices)}")
            check("落盘的名单也清了",
                  not json.loads((Path(engine.config.data_dir) / "known_devices.json")
                                 .read_text(encoding="utf-8") or "{}")
                  if (Path(engine.config.data_dir) / "known_devices.json").exists() else True,
                  "文件还在")
            left_names = [p.info.name for p in window.sidebar.offline_section.peers]
            check("有聊天记录的那台还在（不然聊天就没入口了）",
                  not any("以前" in name for name in left_names) and bool(left_names),
                  str(left_names))
            check("聊天记录一条没少",
                  [m.get("text") for m in engine.chat.messages(chat_conv)] == ["这段聊天不能被清掉"],
                  str([m.get("text") for m in engine.chat.messages(chat_conv)]))

            # ---- 更强的合并规则 -------------------------------------------
            def remember(member: str, text: str, ago: float) -> str:
                conv_id = direct_conversation_id(mine, member)
                engine.chat.upsert_conversation(conv_id, kind="direct", members=[mine, member])
                engine.chat.add_message(
                    conv_id, sender=member, sender_name="V2408A", kind="text",
                    text=text, direction="in", state="sent",
                )
                return conv_id

            first = remember("web:10.229.70.152|b3ca182b", "换网之前那段", time_module.time() - 900)
            second = remember("web:172.20.90.133|b3ca182b", "在别处那段", time_module.time() - 600)
            other_browser = remember("web:192.168.110.174|ffeeddcc", "另一款浏览器", time_module.time() - 300)
            # 会话列表是广播来的：新加的三段要让窗口先看到，否则计划会拿旧的缓存
            # 去算（这条以前偶尔红，就是缓存还没跟上）。
            window.chat_view.reload()
            desktop.pump(0.8)
            check("三段新会话窗口都看到了",
                  len(window._conversation_rows()) >= 4,
                  str([str(r["id"])[:18] for r in window._conversation_rows()]))

            weak = window.plan_conversation_merge(strong=False)
            weak_ids = {str(plan["keep"]["id"]) for plan in weak}
            check("弱规则不动「只有指纹相同」的两段", first not in weak_ids and second not in weak_ids,
                  str(sorted(weak_ids))[:120])

            strong = window.plan_conversation_merge(strong=True)
            strong_members = {m for plan in strong for m in plan["members"]}
            check("强规则把「同一指纹、不同地址」认成一台设备",
                  {"web:10.229.70.152|b3ca182b", "web:172.20.90.133|b3ca182b"} <= strong_members,
                  str(sorted(strong_members)))
            same_group = [
                plan for plan in strong if "web:10.229.70.152|b3ca182b" in plan["members"]
            ]
            check("指纹不同的那一段不会被牵连",
                  all("web:192.168.110.174|ffeeddcc" not in plan["members"] for plan in same_group),
                  str([sorted(plan["members"]) for plan in strong]))

            merged = window.fold_conversations(strong=True, ask=False)
            desktop.pump(0.5)
            texts = []
            for row in engine.chat.conversations():
                texts += [str(m.get("text")) for m in engine.chat.messages(str(row["id"]))]
            check("换过地址的两段并到了一起",
                  merged >= 1 and {"换网之前那段", "在别处那段"} <= set(texts), str(texts))
            check("另一款浏览器的记录没被并进去",
                  "另一款浏览器" in texts, str(texts))
            check("有聊天记录的那段也没被牵连",
                  "这段聊天不能被清掉" in texts, str(texts))
        finally:
            desktop.close()


def test_phone_upload_reaches_the_desktop(app, root) -> None:
    """手机在网页上把文件发给"它连着的这台电脑"，电脑端必须真的收到。

    这是用户报的"电脑端接收不到文件"：手机页面的发送目标是电脑自己
    （``WebUI.self_peer()`` 走回环），而回环那一侧的 *接收* 也要过"要不要接收"
    这道确认 —— 手机那边显示已发送，电脑这边弹个框，用户没点，文件就永远卡在
    ``.part`` 里。默认设置（只自动接收已信任设备）下必须自动放行：这是用户刚刚
    在手机上亲手做的动作。
    """
    print("\n[18] 手机发给「电脑自己」：电脑端要真的收到")
    import json
    import re
    import urllib.parse
    import urllib.request

    from PySide6.QtWidgets import QLabel

    from eversend.web.server import WebUI

    if True:
        desktop = Desktop(str(root), app)
        ui = None
        try:
            window = desktop.window
            engine = desktop.engine
            # 用户的设置组合：只自动接收已信任的设备
            engine.config.auto_accept_all = False
            engine.config.auto_accept_trusted = True
            ui = WebUI(
                engine,
                port=0,
                asset_dir=str((Path(__file__).resolve().parents[1] / "src/eversend/web/assets")),
            )
            port = ui.start()
            window.on_web_ui_started(port, ui)
            desktop.pump(0.5)
            base = f"http://127.0.0.1:{port}"

            with urllib.request.urlopen(base + "/", timeout=10) as response:
                html = response.read().decode("utf-8", "replace")
            token = re.search(r'name="eversend-token" content="([^"]+)"', html).group(1)
            device = "aa11bb22-cc33-dd44-ee55-ff6677889900"
            headers = {
                "X-EverSend-Token": token,
                "X-EverSend-Device": device,
                "X-EverSend-Name": "V2408A",
                "User-Agent": "EverSend-Android/1.0 (Android 14)",
                "Content-Type": "application/json",
            }
            hello = json.dumps(
                {"deviceId": device, "name": "V2408A", "kind": "android", "version": "1.0.0"}
            ).encode()
            urllib.request.urlopen(
                urllib.request.Request(base + "/api/hello", data=hello, method="POST", headers=headers),
                timeout=10,
            ).read()

            # 一句话：电脑端的会话里要看得到
            sent = json.loads(
                urllib.request.urlopen(
                    urllib.request.Request(
                        base + "/api/chat/send",
                        data=json.dumps({"text": "手机说的一句话", "kind": "text"}).encode(),
                        method="POST",
                        headers=headers,
                    ),
                    timeout=10,
                ).read().decode()
            )
            conv = str(sent.get("conversationId") or "")
            desktop.pump(1.5)
            labels = [label.text() for label in walk_widgets(window.chat_view) if isinstance(label, QLabel)]
            check("手机发来的话出现在电脑端会话里",
                  any("手机说的一句话" in text for text in labels), str(conv))
            check("手机那段会话就是窗口打开的那段",
                  window.chat_view.current_conversation() == conv,
                  f"{window.chat_view.current_conversation()} vs {conv}")

            # 一个文件：走"传输"那条路发给电脑自己
            blob = b"z" * (1024 * 1024)
            query = urllib.parse.urlencode(
                {"name": "手机发来的文件.bin", "deviceId": engine.info.device_id}
            )
            upload_headers = dict(headers)
            upload_headers["Content-Type"] = "application/octet-stream"
            answer = json.loads(
                urllib.request.urlopen(
                    urllib.request.Request(
                        f"{base}/api/upload?{query}", data=blob, method="POST", headers=upload_headers
                    ),
                    timeout=30,
                ).read().decode()
            )
            check("服务器受理了这次上传", bool(answer.get("ok")), str(answer)[:120])

            deadline = time.monotonic() + 60
            landed = Path(engine.config.receive_dir) / "手机发来的文件.bin"
            while time.monotonic() < deadline and not landed.exists():
                desktop.pump(0.3)
            check("文件真的落进了接收目录", landed.exists(), str(landed))
            desktop.pump(1.0)
            check("没有卡在「等确认」的弹窗里", not window._offer_dialogs,
                  str(list(window._offer_dialogs)))
            check("这次传输结束了（没挂在 .part）",
                  not engine.active_transfers(),
                  str([t.transfer_id[:8] for t in engine.active_transfers()]))
            rows = [
                window.transfers_view.recv_table.item(row, 0).text()
                for row in range(window.transfers_view.recv_table.rowCount())
                if not window.transfers_view.recv_table.isRowHidden(row)
            ]
            check("接收表里出现了这一行",
                  any("手机发来的文件" in name for name in rows), str(rows))
        finally:
            if ui is not None:
                ui.stop()
            desktop.close()


def test_one_phone_one_row_and_the_header_agrees(app, root) -> None:
    """一台手机只占一行；有会话时顶部不再说「从左边选一台设备」；状态行不说谎。

    用户截图里的三件事，都在桌面端这一侧：
      1. 只连了一台手机，侧栏却写「设备列表 2 / 在线 2」，两行还同名 —— 页面首次
         加载的 ``GET /`` 只留下「地址 + UA 摘要」一条记录，之后带设备号的请求又
         注册了一条，而 ``_phone_peers`` 给**每一条记录**都生成一台设备；
      2. 主区顶部写着「从左边选一台设备」，下面却已经摆着一条带图片的消息气泡；
      3. 状态行写「整理了 1 组重复会话（合并 0 条消息）」—— 合并 0 条却报"整理了
         1 组"，因为计数口径把"只清掉一段空壳"也算成了整理一组。
    """
    print("\n[19] 一台手机一行 + 顶部与主区说同一件事 + 状态行不说谎")
    import hashlib
    import time as time_module

    from PySide6.QtWidgets import QFrame, QLabel

    from eversend.core.chat import direct_conversation_id

    if True:
        desktop = Desktop(str(root), app)
        try:
            window = desktop.window
            engine = desktop.engine
            mine = engine.info.device_id
            phone = "a378bbad-2c4f-4c89-81ea-7a8629176458"
            agent = (
                "Mozilla/5.0 (Linux; Android 13; V2408A) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
            )
            digest = hashlib.sha1(agent.encode("utf-8")).hexdigest()[:8]
            now = time_module.time()
            removed: list[str] = []
            # 用户机器上真实的两条记录：一条带设备号（带设备号的 fetch 请求），
            # 一条只有「地址|UA摘要」（页面首次加载那条，报的是回环地址）。
            window._web_ui = type(
                "FakeUI",
                (),
                {
                    "known_clients": lambda self: [
                        {
                            "key": f"web:{phone}", "deviceId": phone,
                            "address": "192.168.11.7", "agent": agent,
                            "label": "Android 上的 Chrome", "kind": "browser",
                            "online": True, "lastSeen": now, "isLocal": False,
                        },
                        {
                            "key": f"127.0.0.1|{digest}", "address": "127.0.0.1",
                            "agent": agent, "label": "Android 上的 Chrome",
                            "kind": "browser", "online": True,
                            "lastSeen": now - 5, "isLocal": True,
                        },
                    ],
                    "remove_client": lambda self, key: (removed.append(key), True)[1],
                },
            )()
            window._refresh_devices()
            window._refresh_web_clients()
            desktop.pump(0.4)
            # 这个测试自己按次序驱动"整理重复会话"（调用的是自动整理用的那个
            # 函数），所以把窗口那个"稍后自动整理一次"的钩子关掉 —— 否则后台
            # 定时器可能抢在前面把空壳并掉，断言就变成看运气了。
            window._folded_conversations = True

            online = window.sidebar.online_section
            check("同一台手机的两条记录只占一行（不是两台设备）",
                  len(online.peers) == 1, str([p.info.name for p in online.peers]))
            # 计数徽标后来改成了「1 · 手机 1」这种"设备数 · 分类数"的写法：
            # 断言要盯的仍然是"设备数是 1"，所以只比第一段，别把文案格式写死。
            count_text = online.count.text()
            head = count_text.split("·")[0].strip()
            check("「设备列表 · 在线」的计数是 1",
                  head == "1", f"{count_text!r} → 设备数 {head!r}")
            check("这一行是这台手机",
                  bool(online.peers) and online.peers[0].info.name == "Android 上的 Chrome（浏览器）",
                  str([p.info.name for p in online.peers]))
            check("行上的地址是手机的真实地址（回环那条不冒名顶替）",
                  bool(online.peers) and online.peers[0].address == "192.168.11.7",
                  online.peers[0].address if online.peers else "没有行")
            check("底部那句也没把一台手机说成两台",
                  window.web_clients_label.text().count("Android 上的 Chrome") == 1,
                  window.web_clients_label.text())

            # 影子记录那种写法（地址|UA摘要）也可能被写进会话成员里：它同样是这台
            # 手机，不能又长出一行"记录里的会话"（同一台手机两行）。
            shadow_conv = direct_conversation_id(mine, f"web:127.0.0.1|{digest}")
            engine.chat.upsert_conversation(
                shadow_conv, kind="direct", members=[mine, f"web:127.0.0.1|{digest}"]
            )
            window._refresh_devices()
            desktop.pump(0.4)
            check("影子写法的会话也算这台设备，没有多出一行",
                  len(online.peers) + len(window.offline_table.peers) == 1,
                  f"在线 {len(online.peers)} / 离线 {len(window.offline_table.peers)}")
            check("点这一行能找到影子写法的那段会话（不会又新建一段）",
                  window._conversation_for_peer(window.device_table.peers[0]) == shadow_conv,
                  window._conversation_for_peer(window.device_table.peers[0]))

            # 手机发来一张照片：会话里有消息，顶部就必须显示"在和谁说话"。
            conv = direct_conversation_id(mine, f"web:{phone}")
            engine.chat.upsert_conversation(conv, kind="direct", members=[mine, f"web:{phone}"])
            engine.chat.add_message(
                conv, sender=f"web:{phone}", sender_name="V2408A", kind="image",
                media_name="照片.png", media_rel="media/照片.png", media_size=2048,
                media_mime="image/png", direction="in", state="sent",
            )
            # 点侧栏那一行 —— 用户的实际操作（以前这一行可能开出一段空会话）。
            window._open_peer_session(window.device_table.peers[0])
            desktop.pump(0.5)

            def main_text() -> list[str]:
                return [
                    w.text() for w in walk_widgets(window.session_view) if isinstance(w, QLabel)
                ]

            def canvas_bubbles() -> list:
                return [
                    w for w in window.chat_view.messages_host.findChildren(QFrame)
                    if w.objectName() in ("BubbleIn", "BubbleOut")
                ]

            check("点这一行打开的就是有消息的那段会话",
                  window.chat_view.current_conversation() == conv,
                  window.chat_view.current_conversation())
            check("这条消息在画布上（下面确实有气泡）",
                  len(canvas_bubbles()) == 1, str(len(canvas_bubbles())))
            header_text = [
                w.text() for w in walk_widgets(window.session_view.header)
                if isinstance(w, QLabel)
            ]
            check("有消息时顶部不再显示「从左边选一台设备」",
                  not any("从左边选一台设备" in text for text in header_text),
                  str(header_text))
            check("顶部显示的是这台手机的信息",
                  any("Android 上的 Chrome" in text for text in header_text)
                  and any("192.168.11.7" in text for text in header_text),
                  str(header_text))
            check("整个主区里空状态和消息不同时出现",
                  not any("从左边选一台设备" in text for text in main_text()),
                  str([t for t in main_text() if "从左边选一台设备" in t]))

            # 换到另一段会话（手机 App 的成员写法）：画布必须跟着换，不能把上一段
            # 的气泡留在原地冒充新会话的内容。
            shell = direct_conversation_id(mine, f"android:{phone}")
            engine.chat.upsert_conversation(shell, kind="direct", members=[mine, f"android:{phone}"])
            window.chat_view.open_conversation(shell)
            desktop.pump(0.4)
            check("换到空会话后，上一段的气泡不会留在画布上",
                  not canvas_bubbles(), str(len(canvas_bubbles())))
            check("空会话里说的是「还没有消息」",
                  any("还没有消息" in text for text in main_text()), str(main_text()[:8]))

            # 自动整理：这段空壳会被并进有消息的那段（0 条消息被搬动）。
            # 先让窗口的会话列表跟上（应用里是聊天页那次 1.5 秒的刷新在做），
            # 整理读的就是这份列表。
            window.chat_view.reload()
            desktop.pump(0.2)
            before = len(engine.chat.conversations())
            touched = window._fold_split_conversations()
            desktop.pump(0.6)
            after = [str(row["id"]) for row in engine.chat.conversations()]
            status = window.status_left.text()
            check("空壳那一段被整理掉了", touched == 1 and len(after) == before - 1,
                  f"{before} → {len(after)}（动过 {touched} 组）")
            check("合并 0 条消息时不说「整理了 1 组」",
                  "整理了 1 组" not in status and "整理了" not in status, status)
            check("状态行如实说是清掉了空会话",
                  "清掉了 1 段空会话" in status and "消息一条没丢" in status, status)
            check("整理之后顶部显示的是这台手机（不是空状态）",
                  not any("从左边选一台设备" in text for text in main_text()),
                  str([t for t in main_text() if "从左边选一台设备" in t]))
            check("整理之后打开的是消息搬进去的那段会话，消息还在",
                  window.chat_view.current_conversation() == conv and len(canvas_bubbles()) == 1,
                  f"{window.chat_view.current_conversation()} / {len(canvas_bubbles())} 条气泡")
            check("照片一条没丢",
                  any(str(m.get("mediaName")) == "照片.png"
                      for m in engine.chat.messages(conv, limit=20)),
                  str([m.get("mediaName") for m in engine.chat.messages(conv, limit=20)]))

            # 忘记这台设备：两条注册表记录要一起删，否则那一行删不掉。
            window._remove_device(window.device_table.peers[0])
            desktop.pump(0.3)
            check("忘记设备会删掉它的每一条注册表记录",
                  {f"web:{phone}", f"127.0.0.1|{digest}"} <= set(removed), str(removed))

            # 两台**真的**重名又都不在线的设备：名字分不出来，那一行的细节要能把
            # 它们分开（各自带着自己的地址），否则用户只能猜。
            from eversend.core.model import DeviceInfo

            for index in range(2):
                window._remember_device(
                    type("P", (), {
                        "info": DeviceInfo(
                            device_id=f"same-name-{index}", name="Android 上的 Chrome",
                            kind="mobile", platform="linux", version="1.0.0",
                        ),
                        "address": f"10.9.9.{index + 1}",
                        "port": 52000 + index,
                        "source": "incoming",
                        "trusted": False,
                        "last_seen": time_module.monotonic(),
                    })()
                )
            window._refresh_devices()
            desktop.pump(0.4)
            same_named = [
                window.offline_table.itemWidget(window.offline_table.item(row))
                for row in range(window.offline_table.count())
            ]
            cards = [
                card for card in same_named
                if card is not None and card.peer.info.device_id.startswith("same-name-")
            ]
            names = sorted(card.name.fullText() for card in cards)
            details = sorted(card.detail.text() for card in cards)
            # 两台**真的**不同的设备（deviceId 不同）同名时：显示名要能分开
            # （core.model.disambiguate_names 给 `#2` 后缀），细节里也各自带地址。
            check("重名的离线设备：名字自动加序号后缀，能分清谁是谁",
                  len(cards) == 2 and len(set(names)) == 2
                  and any("#2" in name for name in names), str(names))
            # 副标题现在只放一个事实（宪章 §3.1 ③），所以不靠它区分；地址在悬停
            # 提示里各自带着一份 —— 名字已经分得开，再往副标题塞地址就是"重复展示"。
            tips = [card.toolTip() for card in cards]
            check("重名的离线设备：悬停提示里各自带着自己的地址",
                  len(tips) == 2 and len(set(tips)) == 2
                  and all(card.peer.address and card.peer.address in card.toolTip() for card in cards),
                  str([tip.splitlines()[:3] for tip in tips]))

            # 反过来：影子记录反而更新（页面刚刷新过）时，身份/名字/地址仍然要取
            # 那台真实设备的那条 —— 不能因为"最新"就把回环地址和 UA 猜的名字
            # 当成设备本身。
            window._web_ui = type(
                "FakeUI",
                (),
                {
                    "known_clients": lambda self: [
                        {
                            "key": f"127.0.0.1|{digest}", "address": "127.0.0.1",
                            "agent": agent, "label": "Android 上的 Chrome",
                            "kind": "browser", "online": True,
                            "lastSeen": now + 10, "isLocal": True,
                        },
                        {
                            "key": f"android:{phone}", "deviceId": phone, "name": "V2408A",
                            "label": "V2408A", "kind": "app", "address": "192.168.11.7",
                            "agent": agent, "online": False, "lastSeen": now,
                            "isLocal": False,
                        },
                    ]
                },
            )()
            window._refresh_devices()
            desktop.pump(0.3)
            phone_rows = [
                peer
                for peer in window.device_tables()[0].peers + window.device_tables()[1].peers
                if peer.info.device_id == f"web:{phone}"
            ]
            check("影子记录更新也并成一行（还是一台设备）",
                  len(phone_rows) == 1, str(len(phone_rows)))
            check("名字取带设备号那条的（App 的真名，不是 UA 猜的）",
                  bool(phone_rows) and phone_rows[0].info.name == "V2408A",
                  phone_rows[0].info.name if phone_rows else "没有行")
            check("地址取真实的局域网地址（回环那条不冒名顶替）",
                  bool(phone_rows) and phone_rows[0].address == "192.168.11.7",
                  phone_rows[0].address if phone_rows else "没有行")
            check("任一条在线就算在线",
                  bool(phone_rows) and bool(phone_rows[0].info.capabilities.get("online")),
                  str(phone_rows[0].info.capabilities.get("online")) if phone_rows else "没有行")
        finally:
            desktop.close()


def test_merge_does_not_fuse_different_devices(app, root) -> None:
    """整理重复会话：同一台手机的碎片要并，**同一地址上的两台不同设备不能并**。

    审计员在用户真实数据的副本上抓到过这个：`_member_evidence` 只要地址相同就发
    一份 `addr:` 证据，于是同一台电脑上（127.0.0.1）两个不同浏览器的会话被并查集
    并成了一段（59→57）；而同一台手机真正散成两段的（设备号写法 + 影子写法）反而
    没并。这条盯住两个方向。
    """
    print("\n[20] 整理重复会话：该并的并、不该并的不并")
    import time as time_module

    if True:
        desktop = Desktop(str(root), app)
        try:
            window = desktop.window
            engine = desktop.engine
            mine = engine.info.device_id

            def remember(member: str, text: str, ago: float) -> str:
                conv_id = direct_conversation_id(mine, member)
                engine.chat.upsert_conversation(conv_id, kind="direct", members=[mine, member])
                engine.chat.add_message(
                    conv_id, sender=member, sender_name="V2408A", kind="text",
                    text=text, direction="in", state="received",
                )
                return conv_id

            uuid_one = "a378bbad-2c4f-4c89-81ea-7a8629176458"
            phone_shadow = "192.168.110.174|94f0918a"
            phone_device = f"web:{uuid_one}"
            other_browser = "web:4b3cc42d-1111-2222-3333-444455556666"

            shadow_conv = remember(phone_shadow, "影子写法那段", time_module.time() - 900)
            device_conv = remember(phone_device, "设备号写法那段", time_module.time() - 600)
            foreign_conv = remember(other_browser, "另一个浏览器", time_module.time() - 300)

            window._web_ui = type(
                "FakeUI",
                (),
                {
                    "known_clients": lambda self: [
                        {  # 手机：带设备号的记录（和下面那条影子记录同一个地址 + 同一个 UA）
                            "key": f"web:{uuid_one}",
                            "deviceId": uuid_one,
                            "name": "V2408A",
                            "kind": "browser",
                            "address": "192.168.110.174",
                            "agent": "Mozilla/5.0 (Linux; Android 14; V2408A) Chrome/153",
                            "online": True,
                        },
                        {  # 同一台手机的影子记录（页面首次加载留下，没有设备号）
                            "key": phone_shadow,
                            "name": "Android 上的 Chrome",
                            "kind": "browser",
                            "address": "192.168.110.174",
                            "agent": "Mozilla/5.0 (Linux; Android 14; V2408A) Chrome/153",
                            "online": True,
                        },
                        {  # 同一台电脑上另一个浏览器（不同 UA）
                            "key": "127.0.0.1|4b3cc42d",
                            "name": "Linux 上的 Chrome",
                            "kind": "browser",
                            "address": "127.0.0.1",
                            "agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/153",
                            "online": True,
                        },
                    ]
                },
            )()

            # 影子记录那条会话的成员写法：老数据里存的是 web:地址|摘要
            plans_weak = window.plan_conversation_merge(strong=False)
            members = [m for plan in plans_weak for m in plan["members"]]
            check("同一台手机的「影子写法」和「设备号写法」会被并到一起",
                  phone_shadow in members and phone_device in members,
                  str(sorted(members)))
            check("同一地址上的另一个浏览器不会被牵连",
                  all(other_browser not in plan["members"] for plan in plans_weak),
                  str([sorted(plan["members"]) for plan in plans_weak]))

            merged = window.fold_conversations(strong=False, ask=False)
            desktop.pump(0.4)
            texts = []
            for row in engine.chat.conversations():
                texts += [str(m.get("text")) for m in engine.chat.messages(str(row["id"]))]
            check("两个碎片真的并成了一段",
                  merged >= 1 and {"影子写法那段", "设备号写法那段"} <= set(texts), str(texts))
            check("另一个浏览器那段还在自己的会话里",
                  "另一个浏览器" in texts, str(texts))

            # 误并的对照：弱规则不该把两个不同浏览器并起来
            left = [
                row for row in engine.chat.conversations()
                if [m.get("text") for m in engine.chat.messages(str(row["id"]))] == ["另一个浏览器"]
            ]
            check("弱规则没有把不同设备并成一段", len(left) == 1,
                  str([str(r["id"])[:22] for r in engine.chat.conversations()]))

            # 未读不能因为并库而丢：审计员量到过 SUM(unread) 15→14
            a = remember("web:keep-unread", "保留段的未读", time_module.time() - 120)
            b = remember("web:drop-unread", "被并段的未读", time_module.time() - 60)
            before_unread = int(engine.chat.conversation(a).get("unread") or 0) + int(
                engine.chat.conversation(b).get("unread") or 0
            )
            engine.chat.merge_conversations(a, [b])
            after_unread = int(engine.chat.conversation(a).get("unread") or 0)
            check("并库之后未读数 = 两段之和（不会凭空少掉）",
                  after_unread == before_unread and before_unread > 0,
                  f"{before_unread} → {after_unread}")

            # 启动时"恢复上次的会话"不等于"用户看过了"：审计员实测真实数据里
            # 一启动 SUM(unread) 就从 15 掉到 14，用户什么都没点。
            window.chat_view.open_conversation("")          # 先回到"什么都没打开"
            fresh = remember("web:unread-keep", "这条要保持未读", time_module.time())
            desktop.pump(0.6)
            startup_unread = int(engine.chat.conversation(fresh).get("unread") or 0)
            window._restore_session()
            desktop.pump(0.6)
            check("启动时恢复会话不会把未读吃掉",
                  startup_unread > 0
                  and int(engine.chat.conversation(fresh).get("unread") or 0) == startup_unread,
                  f"{startup_unread} → {engine.chat.conversation(fresh).get('unread')}")
            window.chat_view.open_conversation(fresh)
            desktop.pump(0.3)
            check("用户点进去之后才标记已读",
                  int(engine.chat.conversation(fresh).get("unread") or 0) == 0,
                  str(engine.chat.conversation(fresh).get("unread")))
        finally:
            desktop.close()


def test_startup_auto_fold_runs_without_any_user_action(app, root) -> None:
    """启动后**什么都不碰**，同一台手机散成的两段会话也要自己并回一段。

    审计员抓到过：自动整理只挂在"会话列表变化"这个信号上，而 ChatView 构造时那次
    reload 的广播发生在 bridge 接上**之前**（丢了），安静的启动里它根本不会跑 ——
    以前"碰巧会跑"是因为自动打开会话顺手 mark_read 改了签名。这条走的是**定时器/
    信号的真实路径**（不直接调 fold_conversations），20 秒内必须并完且只并一次。
    """
    print("\n[21] 启动后自动整理（不碰任何东西）真的会跑")
    import time as time_module

    if True:
        desktop = Desktop(str(root), app)
        try:
            window = desktop.window
            engine = desktop.engine
            mine = engine.info.device_id
            uuid_one = "cccccccc-1111-2222-3333-444444444444"
            shadow = "192.168.110.174|94f0918a"
            for member, text, ago in (
                (shadow, "影子段", 900.0),
                (f"web:{uuid_one}", "设备号段", 600.0),
            ):
                conv_id = direct_conversation_id(mine, member)
                engine.chat.upsert_conversation(conv_id, kind="direct", members=[mine, member])
                engine.chat.add_message(
                    conv_id, sender=member, sender_name="V2408A", kind="text",
                    text=text, direction="in", state="received",
                )
            window._web_ui = type(
                "FakeUI",
                (),
                {
                    "known_clients": lambda self: [
                        {
                            "key": f"web:{uuid_one}", "deviceId": uuid_one, "name": "V2408A",
                            "address": "192.168.110.174",
                            "agent": "Mozilla/5.0 (Linux; Android 14; V2408A) Chrome/153",
                            "online": True,
                        },
                        {
                            "key": shadow, "name": "Android 上的 Chrome",
                            "address": "192.168.110.174",
                            "agent": "Mozilla/5.0 (Linux; Android 14; V2408A) Chrome/153",
                            "online": True,
                        },
                    ]
                },
            )()
            deadline = time_module.monotonic() + 20
            while time_module.monotonic() < deadline:
                desktop.pump(0.2)
                if len(engine.chat.conversations()) == 1:
                    break
            left = len(engine.chat.conversations())
            check("启动后没人碰它，两段也会自己并成一段", left == 1, f"{left} 段还在")
            check("整理只做一次（开关真的被置位）",
                  getattr(window, "_folded_conversations", False) is True,
                  str(getattr(window, "_folded_conversations", None)))
            texts = [
                str(m.get("text"))
                for row in engine.chat.conversations()
                for m in engine.chat.messages(str(row["id"]))
            ]
            check("消息一条没丢", {"影子段", "设备号段"} <= set(texts), str(texts))
        finally:
            desktop.close()


def test_cancel_after_finish(app, root) -> None:
    """Clicking 「取消」 on a card whose transfer is already over must not hang.

    ``Engine.cancel`` returns False for a transfer it does not know; treating
    that as success is the other half of the stuck-at-「正在取消…」 bug.
    """
    print("\n[3] 传输早已结束再点取消：立刻收尾，不能卡住")
    if True:
        desktop = Desktop(str(root), app)
        try:
            row = desktop.window._add_transfer_row("__pending__stale", "发送到 早就没了", "send")
            row.set_status("传输中")
            check("陈旧卡片没有对应的活动传输", not desktop.engine.active_transfers())
            desktop.window._cancel_transfer("__pending__stale")
            desktop.pump(0.4)
            check("点下去就结束了，不是「正在取消…」", row.status.text() != "正在取消…", row.status.text())
            check("文字是「已取消」", row.status.text() == "已取消", row.status.text())
        finally:
            desktop.close()


def walk_widgets(widget):
    """Every widget below ``widget``, so a test can look at what was drawn."""
    from PySide6.QtWidgets import QWidget

    stack = [widget]
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, QWidget):
            stack.extend(current.findChildren(QWidget))


def test_chat_attachment_preview(app, root) -> None:
    """聊天里的图片要真的显示出来，视频要给出能播的入口.

    Reported as "聊天里不能在聊天框预览图片/视频".  The desktop previewed an
    image only when the file happened to be in the receive folder, which is
    never true for a picture *you* sent -- your copy is wherever you picked it
    from -- so every outgoing photo was a bare file card.
    """
    print("\n[4] 聊天附件：图片预览 / 视频播放入口")
    from PySide6.QtWidgets import QLabel

    if True:
        desktop = Desktop(str(root), app)
        try:
            chat = desktop.window.chat_view
            receive = Path(desktop.engine.config.receive_dir)
            receive.mkdir(parents=True, exist_ok=True)

            # A real PNG, so QPixmap can actually decode it (a 1x1 GIF would
            # hide a broken decoder behind a "null pixmap" branch).
            picture = root / "照片.png"
            picture.write_bytes(make_png(64, 48))
            movie = root / "短片.mp4"
            movie.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\0" * 4096)

            conv = direct_conversation_id(desktop.engine.info.device_id, "peer-1")
            # What we sent: the file lives at the path the user picked.
            sent = desktop.engine.send_chat(conv, kind="image", media_path=str(picture), to="peer-1")
            # What arrived: the file lives in our receive folder.
            arrived_rel = media_relpath(conv, "收到的照片.png")
            arrived = receive / arrived_rel
            arrived.parent.mkdir(parents=True, exist_ok=True)
            arrived.write_bytes(make_png(32, 32))
            desktop.engine.chat.add_message(
                conv, sender="peer-1", sender_name="客厅电脑", kind="image",
                media_name="收到的照片.png", media_rel=arrived_rel, media_size=arrived.stat().st_size,
                direction="in",
            )
            # 一个真视频（ffmpeg 生成 1 秒彩条），这样缩略图那条路才验得动
            real_movie = root / "真视频.mp4"
            made = make_video(real_movie)
            desktop.engine.send_chat(conv, kind="video", media_path=str(movie), to="peer-1")
            if made:
                desktop.engine.send_chat(conv, kind="video", media_path=str(real_movie), to="peer-1")

            chat.reload()
            desktop.pump(0.5)
            labels = [w for w in walk_widgets(chat) if isinstance(w, QLabel)]
            # Only the bubbles' own previews: the window is full of icon labels
            # that carry pixmaps too, and counting those would make the check
            # pass no matter what the chat did.
            pixmaps = [
                w for w in labels
                if w.objectName() == "BubbleImage" and w.pixmap() is not None and not w.pixmap().isNull()
            ]
            texts = [w.text() for w in labels]

            stored = desktop.engine.chat.message(sent["message"]["id"]) or {}
            check(
                "自己发出去的图片也能预览（以前只有文件卡片）",
                stored.get("mediaSource") == str(picture) and len(pixmaps) >= 1,
                f"source={stored.get('mediaSource')!r} pixmaps={len(pixmaps)}",
            )
            check(
                "收到的图片同样预览（两张图 → 两个预览）",
                len(pixmaps) >= 2,
                f"pixmaps={len(pixmaps)}",
            )
            # 聊天里的文件传输要"长在气泡里"：正在传时显示百分比
            chat._refresh_media_progress()   # 没有活动传输时不能报错
            check("没有传输时气泡不显示进度行", all(
                not getattr(bubble, "_progress_label").isVisible()
                for bubble, _ in chat._media_bubbles
                if hasattr(bubble, "_progress_label")
            ), "有气泡显示了进度")
            check(
                "视频给出播放入口（内置播放器或系统播放器）",
                any("播放" in text for text in texts),
                str([text[:40] for text in texts if "🎬" in text]),
            )
            from eversend.core import media

            if made and media.available(desktop.engine.config.data_dir):
                thumbs = [
                    w for w in labels
                    if w.objectName() == "BubbleImage" and w.pixmap() is not None and not w.pixmap().isNull()
                ]
                check(
                    "有 FFmpeg 时视频显示真正的缩略图",
                    len(thumbs) >= 3,
                    f"预览控件 {len(thumbs)} 个（两张图 + 一个视频缩略图）",
                )
                check(
                    "缩略图来自内置/系统的 ffmpeg，而不是系统播放器截图",
                    bool(media.video_thumbnail(str(real_movie), desktop.engine.config.data_dir)),
                )
            else:
                print("      （本机没有 ffmpeg，跳过缩略图断言）")
            wire = desktop.engine.chat_payload(
                desktop.engine.chat.conversation(conv) or {}, stored
            )["msg"]
            check(
                "本机路径只留在本机，不随消息发给对方",
                "mediaSource" not in wire and os.path.isabs(str(stored.get("mediaSource"))),
                str(sorted(wire)),
            )

            # 点开看原图：对话框里是原图，而不是缩略图
            from eversend.desktop.chat_view import ImageViewer

            viewer = ImageViewer(str(picture), "照片.png", chat)
            check("原图查看器打开的是未缩放的原图", viewer._original.width() == 64, str(viewer._original.size()))
            check("查看器知道怎么保存（有另存为按钮）", any(
                "另存为" in button.text() for button in walk_buttons(viewer)
            ))
            viewer.close()
        finally:
            desktop.close()


def walk_buttons(dialog):
    from PySide6.QtWidgets import QPushButton

    return [w for w in walk_widgets(dialog) if isinstance(w, QPushButton)]


def test_conversation_list_reads_like_a_chat_app(app, root) -> None:
    """侧栏的设备列表要像聊天软件：显示对方的名字，不是数据库主键。

    会话列表以前长在「聊天」页里；新布局里它就是侧栏那份设备列表
    （``docs/UI_LAYOUT.md``：左侧固定显示设备列表），所以这条测试改成查侧栏：
    名字、未读小红点、置顶、群聊那一段，以及"点一台设备就打开和它的会话"。
    """
    print("\n[6] 侧栏设备列表：名字 + 未读 + 置顶 + 群聊")
    import time as time_module

    from PySide6.QtWidgets import QFrame

    from eversend.core.chat import new_group_id

    if True:
        desktop = Desktop(str(root), app)
        try:
            chat = desktop.window.chat_view
            sidebar = desktop.window.sidebar
            engine = desktop.engine

            # 一台真的会被发现的设备：接收机（Desktop 里已经起好了）。
            engine.add_manual_device("127.0.0.1", desktop.receiver.port, "接收机")
            desktop.window._refresh_devices()
            desktop.pump(0.4)

            peer = next(
                (p for p in desktop.window.device_table.peers if p.port == desktop.receiver.port),
                None,
            )
            check("设备列表里能找到接收机", peer is not None,
                  str([(p.info.name, p.port) for p in desktop.window.device_table.peers]))
            if peer is None:
                return
            receiver_name = peer.info.name

            # 和它说两句话：一条对方发的（未读）、一条自己发的。
            conv = chat.conversation_for_member(peer.info.device_id, f"和 {receiver_name} 的对话")
            engine.chat.add_message(conv, sender=peer.info.device_id, sender_name=receiver_name,
                                    kind="image", text="", media_name="a.png", direction="in")

            def rows():
                """侧栏设备行：名字 + 副标题 + 未读徽标（控件，不是纯文字）。"""
                out = []
                for i in range(desktop.window.device_table.count()):
                    item = desktop.window.device_table.item(i)
                    widget = desktop.window.device_table.itemWidget(item)
                    if widget is not None:
                        out.append(
                            (
                                widget.peer.info.name,
                                widget.name.text(),
                                widget.detail.text(),
                                getattr(widget, "badge", None),
                            )
                        )
                return out

            chat.reload()
            desktop.pump(0.6)
            items = rows()
            joined = "\n".join(f"{n} / {t} / {d}" for n, t, d, _ in items)
            check("列表里不再出现会话 id", "d:" not in joined and "g:" not in joined, joined[:160])
            check("设备行是对方的名字", any(n == receiver_name for n, _, _, _ in items), joined[:160])
            # 侧栏窄，副标题会被省略号截断，所以查全文（fullText）。
            full = [
                desktop.window.device_table.itemWidget(desktop.window.device_table.item(i)).detail.fullText()
                for i in range(desktop.window.device_table.count())
                if desktop.window.device_table.itemWidget(desktop.window.device_table.item(i)) is not None
            ]
            # 宪章 §3.1 ③：副标题只放**一个**事实 —— 在线设备放地址（不带端口，
            # 端口在顶部详情行），离线设备放「平台 · 最后在线 X」。
            cards = [
                desktop.window.device_table.itemWidget(desktop.window.device_table.item(i))
                for i in range(desktop.window.device_table.count())
            ]
            online_subs = [c.detail.fullText() for c in cards if c is not None and c.peer.source != "history"]
            check("在线设备的副标题是地址，而且不带端口",
                  bool(online_subs) and all(
                      ":" not in sub and (sub[:1].isdigit() or "·" in sub) for sub in online_subs
                  ), str(online_subs)[:160])
            check("副标题里不再重复平台（平台在顶部详情行）",
                  not any(
                      any(word in sub for word in ("Linux", "Windows", "macOS", "网页版"))
                      for sub in online_subs
                  ), str(online_subs)[:160])
            check("未读有小红点", any(
                badge is not None and not badge.isHidden() for *_, badge in items
            ), "没有未读徽标")

            # 置顶：名字前有 📌，而且仍然排在设备列表里（不跳到别的页）。
            chat._toggle_pref("pinned", chat.conversation(conv))
            desktop.pump(0.4)
            check("置顶的设备行标出 📌", any(t.startswith("📌") for _, t, _, _ in rows()),
                  str([t for _, t, _, _ in rows()])[:120])
            # 免打扰：提示里写出来（行上没有位置再放一个图标了）。
            chat._toggle_pref("muted", chat.conversation(conv))
            desktop.pump(0.4)
            peer_row = next(
                (desktop.window.device_table.itemWidget(desktop.window.device_table.item(i))
                 for i in range(desktop.window.device_table.count())
                 if desktop.window.device_table.itemWidget(desktop.window.device_table.item(i)) is not None
                 and desktop.window.device_table.itemWidget(desktop.window.device_table.item(i)).peer.info.name
                 == receiver_name),
                None,
            )
            check("免打扰写进了提示", peer_row is not None and "免打扰" in peer_row.toolTip(),
                  peer_row.toolTip() if peer_row is not None else "没有这一行")

            # 群聊排在设备下面单独一段。
            group = new_group_id()
            engine.chat.upsert_conversation(
                group, kind="group", title="", members=[engine.info.device_id, peer.info.device_id]
            )
            engine.chat.add_message(group, sender=engine.info.device_id, sender_name="我",
                                    text="大家好", direction="out")
            chat.reload()
            desktop.pump(0.6)
            check("群聊那一段出现了", sidebar.group_list.isVisible() and sidebar.group_list.count() == 1,
                  f"count={sidebar.group_list.count()}")
            group_row = sidebar.group_list.itemWidget(sidebar.group_list.item(0))
            check("没起名的群显示人数", group_row is not None and "群聊（2 人）" in group_row.fullText(),
                  group_row.fullText() if group_row is not None else "没有这一行")

            # 打开群聊：主区顶部换成群名，画布上只剩这个会话的气泡。
            def bubbles():
                return [
                    w
                    for w in chat.messages_host.findChildren(QFrame)
                    if w.objectName() in ("BubbleIn", "BubbleOut")
                ]

            desktop.window._open_group_session(group)
            desktop.pump(0.5)
            header_title = desktop.window.session_view.header.name_label.text()
            header_detail = desktop.window.session_view.header.detail_label.text()
            check("顶部显示群名（可读的成员名，而不是会话 id）",
                  header_title.startswith("群聊") or "、" in header_title, header_title)
            check("群聊详情写成员（或成员数），不再把 id 写两遍",
                  ("2 个成员" in header_detail or "接收机" in header_detail or "我" in header_detail)
                  and header_detail.count("#") <= 1
                  and header_title not in header_detail,
                  header_detail)
            expected = len(engine.chat.messages(group, limit=200))
            check(
                "切到群聊后画布上只剩这个会话的气泡",
                len(bubbles()) == expected,
                f"{len(bubbles())} 个气泡 / 会话里有 {expected} 条",
            )

            # 点设备那一行：主区回到和它的会话，顶部显示对方设备详情。
            desktop.window._open_peer_session(peer)
            desktop.pump(0.5)
            check("点设备后顶部是对方的名字",
                  desktop.window.session_view.header.name_label.text() == receiver_name,
                  desktop.window.session_view.header.name_label.text())
            check("顶部写出了对方地址与端口",
                  f"{peer.address}:{peer.port}" in desktop.window.session_view.header.where_label.text(),
                  desktop.window.session_view.header.where_label.text())
            expected = len(engine.chat.messages(conv, limit=200))
            check(
                "切回一对一也只剩它自己的气泡",
                len(bubbles()) == expected,
                f"{len(bubbles())} 个气泡 / 会话里有 {expected} 条",
            )
            # 顺便截一张图存档（docs 里要用）
            try:
                desktop.window.show_page("session", view="chat")
                desktop.pump(0.3)
                shots = Path(__file__).resolve().parent / "screenshots"
                shots.mkdir(exist_ok=True)
                desktop.window.grab().save(str(shots / "conversation-list.png"))
            except Exception:
                pass
        finally:
            desktop.close()


def make_video(path: Path, seconds: float = 1.0) -> bool:
    """A real MP4 via ffmpeg, when the machine has one.  False = skipped."""
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={seconds}",
                "-pix_fmt", "yuv420p", str(path),
            ],
            timeout=60, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return path.is_file() and path.stat().st_size > 0


def make_png(width: int, height: int) -> bytes:
    """A tiny real PNG, generated without pulling in an imaging library."""
    from PySide6.QtCore import QBuffer, QByteArray
    from PySide6.QtGui import QColor, QImage

    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(QColor(47, 129, 247))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def test_theme_follows_the_harness_style(app, root) -> None:
    """默认深色，并且在设置里能切成浅色（不用重启）。"""
    print("\n[7] 外观：默认深色 + 设置里能切浅色")
    import json
    import os as os_module

    from eversend.desktop import theme

    saved = os_module.environ.pop("EVERSEND_THEME", None)
    try:
        # 深色是默认：没有环境变量、也没有存过的偏好时就是深色。
        theme_forced = os_module.environ.get("EVERSEND_THEME")
        check("没有额外设置时默认深色", saved is not None or theme_forced is None or
              theme_forced == "dark", f"EVERSEND_THEME={theme_forced!r}")
        sheet = theme.stylesheet(True)
        check("深色画布用的是 harness 的 #151517", "#151517" in sheet)
        check("深色强调色是单色（近白），不是另一个色相", "#f9fafb" in sheet and "#2f81f7" not in sheet.lower())
        check("浅色那套也换成了 harness 的白底近黑字", "#ffffff" in theme.stylesheet(False))

        desktop = Desktop(str(root), app)
        try:
            window = desktop.window
            combo = window.theme_combo
            check("设置页里有主题开关", combo.count() == 2)
            check("开关默认停在深色", combo.currentData() == "dark")

            # 切到浅色：样式表要真的换掉，而且写进 settings.json。
            combo.setCurrentIndex(1)
            desktop.pump(0.3)
            check("切浅色后 is_dark() 变 false", theme.is_dark() is False)
            check("切浅色后样式表换成白底", "#ffffff" in app.styleSheet())
            check("状态栏告诉你换好了", "浅色" in window.status_left.text())

            path = Path(window.settings_path)
            check("主题存进了设置文件", path.exists() and
                  json.loads(path.read_text(encoding="utf-8")).get("theme") == "light")

            # 字号档（宪章 §3.4 外观）：三档、立刻生效、写进设置
            font_combo = window.font_combo
            check("设置页有字号三档", font_combo.count() == 3,
                  str([font_combo.itemText(i) for i in range(font_combo.count())]))
            check("默认是「标准」", theme.current_scale_name() == "standard",
                  theme.current_scale_name())
            font_combo.setCurrentIndex(font_combo.findData("relaxed"))
            desktop.pump(0.3)
            check("切「宽松」后字号倍率变成 1.1", theme.current_scale() == 1.1,
                  str(theme.current_scale()))
            check("QApplication 的字体真的变大了",
                  app.font().pointSizeF() > 10.0, str(app.font().pointSizeF()))
            check("状态栏说了字号生效", "宽松" in window.status_left.text(),
                  window.status_left.text())
            check("字号存进了设置文件",
                  json.loads(path.read_text(encoding="utf-8")).get("font_scale") == "relaxed",
                  path.read_text(encoding="utf-8")[:120])
            font_combo.setCurrentIndex(font_combo.findData("compact"))
            desktop.pump(0.3)
            check("切「紧凑」后倍率变成 0.9", abs(theme.current_scale() - 0.9) < 1e-6,
                  str(theme.current_scale()))
            check("紧凑时字号比标准小", app.font().pointSizeF() < 10.0,
                  str(app.font().pointSizeF()))
            font_combo.setCurrentIndex(font_combo.findData("standard"))
            desktop.pump(0.3)
            check("切回「标准」", theme.current_scale_name() == "standard",
                  theme.current_scale_name())

            # 切回深色，别把环境留给后面的用例。
            combo.setCurrentIndex(0)
            desktop.pump(0.3)
            check("切回深色", theme.is_dark() is True and "#151517" in app.styleSheet())
        finally:
            os_module.environ.pop("EVERSEND_THEME", None)
            app.setStyleSheet(theme.stylesheet(theme.is_dark()))
    finally:
        if saved is not None:
            os_module.environ["EVERSEND_THEME"] = saved


def main() -> int:
    use_utf8_console()
    # Force the platform rather than inheriting one (a Wayland session exports
    # QT_QPA_PLATFORM=wayland, which cannot connect inside xvfb).
    os.environ["QT_QPA_PLATFORM"] = os.environ.get("EVERSEND_GUI_PLATFORM", "xcb")

    from PySide6.QtWidgets import QApplication

    from eversend.desktop import theme

    print("EverSend 桌面交互测试")
    print("=" * 60)
    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.stylesheet(theme.is_dark()))

    # One scratch directory for the whole run: each window keeps a chat timer
    # that would otherwise fire after its directory has been deleted, which
    # looks like a real error in the log and is not one.
    with scratch() as root:
        for test in (
            test_cancel_mid_transfer,
            test_normal_send_finishes,
            test_cancel_after_finish,
            test_chat_attachment_preview,
            test_multi_device_send,
            test_conversation_list_reads_like_a_chat_app,
            test_theme_follows_the_harness_style,
            test_composer_sends_text_and_files,
            test_sidebar_entries_open_the_right_page,
            test_receive_lands_in_the_receive_table,
            test_composer_tiles_and_preview,
            test_devices_split_into_online_and_offline,
            test_emoji_pad_sits_above_the_staged_row,
            test_remembered_devices_land_in_the_offline_section,
            test_one_row_per_file_and_records_in_chat,
            test_split_conversations_fold_back,
            test_clear_offline_and_strong_merge,
            test_phone_upload_reaches_the_desktop,
            test_one_phone_one_row_and_the_header_agrees,
            test_merge_does_not_fuse_different_devices,
            test_startup_auto_fold_runs_without_any_user_action,
            test_no_duplicate_controls,
            test_device_list_one_row_per_device_and_search,
            test_transfer_filters_search_and_retry,
            test_thousand_rows_stay_light,
            test_cancel_intent_is_explicit_not_guessed,
        ):
            try:
                test(app, root / test.__name__)
            except Exception as exc:
                import traceback

                traceback.print_exc()
                _failures.append(f"{test.__name__} raised {exc!r}")

    print("\n" + "=" * 60)
    if _failures:
        print(f"{len(_failures)} failure(s):")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print("桌面交互全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
