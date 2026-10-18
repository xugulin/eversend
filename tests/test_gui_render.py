#!/usr/bin/env python3
"""Render the desktop UI off-screen and save screenshots.

Run under a virtual display so it works on a headless machine::

    xvfb-run -a python3 tests/test_gui_render.py

This proves the window actually builds and paints -- a GUI that raises during
construction is otherwise only discovered by a user clicking the launcher.

The screenshots are also the only honest record of the layout: 左侧栏（设备操作
在顶、设备列表在中、传输/设置在底）、主区（对方设备详情在顶、会话/传输在中、
输入区在底），见 ``docs/UI_LAYOUT.md``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _scratch import scratch, use_utf8_console  # noqa: E402

#: 截图落在哪：默认写进 docs 用的那份（tests/screenshots）。想拍浅色主题就
#: ``EVERSEND_SHOT_DIR=/tmp/light EVERSEND_THEME=light`` 再跑一次，不会把
#: 深色那套覆盖掉。
OUT = Path(os.environ.get("EVERSEND_SHOT_DIR") or (Path(__file__).resolve().parent / "screenshots"))


def main() -> int:
    use_utf8_console()
    # Force the platform rather than inheriting one.  A Wayland desktop session
    # exports QT_QPA_PLATFORM=wayland, and inside xvfb that cannot connect, so
    # the whole run dies with "no Qt platform plugin could be initialized"
    # before rendering a single screenshot.  EVERSEND_GUI_PLATFORM overrides
    # this if someone wants to try another plugin.
    os.environ["QT_QPA_PLATFORM"] = os.environ.get("EVERSEND_GUI_PLATFORM", "xcb")

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from eversend.core.engine import Engine, EngineConfig
    from eversend.core.sockutil import list_interfaces
    from eversend.desktop import theme
    from eversend.desktop.main_window import MainWindow
    from eversend.desktop.widgets import QrDialog

    OUT.mkdir(exist_ok=True)
    # GUI scratch also belongs on the big disk.
    _scratch_dir = scratch()
    _scratch_ctx = _scratch_dir.__enter__()
    tmp = str(_scratch_ctx)

    def free_port() -> int:
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    app = QApplication(sys.argv[:1])
    dark = theme.is_dark()
    app.setStyleSheet(theme.stylesheet(dark))
    app.setWindowIcon(theme.app_icon(128, dark))

    engine = Engine(
        EngineConfig(
            data_dir=os.path.join(tmp, "data"),
            receive_dir=os.path.join(tmp, "received"),
            name="我的电脑",
            tcp_port=0,
            discovery_port=free_port(),
            auto_accept_all=True,
            enable_broadcast=False,
            enable_mdns=False,
            enable_web=False,
        )
    )
    engine.start()

    window = MainWindow(engine, os.path.join(tmp, "data", "settings.json"))
    window.resize(1180, 780)

    # A stand-in for the browser UI that has one Android phone connected, wired
    # in from the start: this is the line that tells the user 「手机连接」 worked
    # (a phone is a browser client, so it never shows up in the peer list), and
    # it should be visible in the screenshots this test produces.
    demo_phone = {
        "address": "192.168.1.23",
        "label": "Android 上的 Chrome",
        "isLocal": False,
        "online": True,
        "secondsAgo": 1.0,
    }

    class _DemoWebUI:
        """Just enough of WebUI for the questions the window asks."""

        #: 画廊第 16 张（一台手机一行）要用的"两条记录"：页面首次加载留下的
        #: 影子记录 + 之后带设备号的那条。窗口必须把它们并成一行。
        records: list = [
            dict(demo_phone, key="192.168.1.23|abc12345", online=True, secondsAgo=1.0)
        ]

        def clients(self):
            return [demo_phone]

        def known_clients(self):
            # The window lists *remembered* phones, not just the ones talking
            # right now: a phone stays paired while its screen is off.
            return [dict(record) for record in self.records]

        def urls(self):
            return [self.url]

    demo_ui = _DemoWebUI()
    demo_ui.url = "http://192.168.1.5:52119/"
    window.on_web_ui_started(52119, demo_ui)
    window._refresh_web_clients()

    # A fake peer so the device list is not empty.
    from eversend.core.model import DeviceInfo, Peer

    fake = Peer(
        info=DeviceInfo(
            device_id="deadbeefcafe1234",
            name="客厅台式机",
            kind="desktop",
            platform="windows",
            tcp_port=52117,
        ),
        address="192.168.1.42",
        port=52117,
        trusted=True,
    )
    # The connected phone goes in the same list: that is the whole point of
    # _phone_peers() -- a phone is a browser client, and before this it simply
    # never appeared anywhere a user could see it.
    demo_rows = [fake] + window._phone_peers() + engine.devices()
    window.sidebar.set_devices(demo_rows, window._conversation_meta(demo_rows))

    # A conversation with that desktop, so the 会话 page shows real bubbles --
    # including a file that lives inside a bubble (the whole point of 聊天).
    from eversend.core.chat import direct_conversation_id

    conv = direct_conversation_id(engine.info.device_id, fake.info.device_id)
    engine.chat.upsert_conversation(
        conv, kind="direct", title="和 客厅台式机 的对话",
        members=[engine.info.device_id, fake.info.device_id],
    )
    engine.chat.add_message(conv, sender=fake.info.device_id, sender_name="客厅台式机",
                            text="报告我放桌面上啦", direction="in")
    sample = Path(tmp) / "示例文件.bin"
    sample.write_bytes(b"\0" * (12 * 1024 * 1024))
    engine.chat.add_message(conv, sender=engine.info.device_id, sender_name="我的电脑",
                            text="", kind="file", media_name=sample.name,
                            media_size=sample.stat().st_size, media_source=str(sample),
                            direction="out")
    window.chat_view.open_conversation(conv)
    window._update_peer_header()

    # 待发文件（输入区上方那一行的方块）也要在截图里，而且要能看出缩略图：
    # 一张真图 + 一个文本 + 一个二进制 + 一个文件夹，四种长法都在。
    folder = Path(tmp) / "照片"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "IMG_0002.jpg").write_bytes(b"\0" * (3 * 1024 * 1024))

    from PySide6.QtGui import QColor, QImage

    thumb = QImage(160, 100, QImage.Format_RGB32)
    for x in range(thumb.width()):
        for y in range(thumb.height()):
            thumb.setPixelColor(x, y, QColor(40 + x, 90 + y, 200 - x // 2))
    picture = Path(tmp) / "示例图片.png"
    thumb.save(str(picture), "PNG")
    note = Path(tmp) / "说明.md"
    note.write_text("# 说明\n\n这是一个用来演示预览的文本文件。\n" * 6, encoding="utf-8")
    window._add_paths([str(picture), str(note), str(sample), str(folder)])

    # 传输页两张表各有一行：发送中（带进度）与已收到的文件。
    row = window._add_transfer_row("demo-xfer", "发送到 客厅台式机", "send", key="demo-xfer")
    row.set_files("示例文件.bin")
    row.update_progress(7 * 1024 * 1024, 12 * 1024 * 1024, 92 * 1024 * 1024, 0.05)
    row.set_status("传输中")
    window._add_history("IMG_0001.jpg", str(sample), "demo-recv")
    # 一行失败的：状态药丸要红、悬停给原因、操作格里是「重试」（宪章 §3.2/C5）。
    failed = window._add_transfer_row("demo-failed", "发送到 客厅台式机", "send", key="demo-failed")
    failed.set_files("季度报表.xlsx")
    failed.set_paths([str(sample)])
    failed.update_progress(3 * 1024 * 1024, 12 * 1024 * 1024, 0, None)
    failed.set_status("传输中")
    failed.finish(False, "连接被对方拒绝（对方可能开了防火墙）")
    window.transfers_view.send_table.setRowHidden(0, False)
    window._update_total()

    # ------------------------------------------------------------------
    # 画廊（docs/screenshots/README 引用那 14 张）要的几组演示数据。
    # 每个场景都是**幂等**的：重复调用不会把同一台设备记两遍。
    # ------------------------------------------------------------------
    import time as time_module

    gallery: set[str] = set()

    def ensure_offline_devices() -> None:
        """画廊 10：离线那一段里有几台"以前见过"的设备。"""
        if "offline" in gallery:
            return
        gallery.add("offline")
        for index, (name, address) in enumerate(
            (
                ("书房的台式机", "192.168.1.51"),
                ("旧的安卓手机", "192.168.1.77"),
                ("备用笔记本", "192.168.1.88"),
            )
        ):
            window._remember_device(  # noqa: SLF001 - 测试脚本
                type(
                    "P",
                    (),
                    {
                        "info": DeviceInfo(
                            device_id=f"gallery-offline-{index}",
                            name=name,
                            kind="mobile" if "手机" in name else "desktop",
                            platform="linux",
                            version="1.0.0",
                        ),
                        "address": address,
                        "port": 52010 + index,
                        "source": "incoming",
                        "trusted": False,
                        "last_seen": time_module.monotonic(),
                    },
                )()
            )
        window._refresh_devices()

    def ensure_emoji_message() -> None:
        """画廊 12：消息正文里的 emoji 与中文同一个字号。"""
        if "emoji" in gallery:
            return
        gallery.add("emoji")
        engine.chat.add_message(
            conv,
            sender=engine.info.device_id,
            sender_name="我的电脑",
            text="🎉 传完啦 🚀 谢谢！",
            direction="out",
        )
        window.chat_view.reload()

    def ensure_one_row_per_file() -> None:
        """画廊 13：同一个文件收两次，接收表里只占一行（宪章 C8）。"""
        if "one-row" in gallery:
            return
        gallery.add("one-row")
        report = Path(tmp) / "季度报表.xlsx"
        report.write_bytes(b"\0" * (512 * 1024))
        # 两个不同的传输号、同一个文件：必须合成一行。
        window._add_history(report.name, str(report), "gallery-dup-1")  # noqa: SLF001
        window._add_history(report.name, str(report), "gallery-dup-2")  # noqa: SLF001
        window.transfers_view.refresh_empty()

    def ensure_transfer_record() -> None:
        """画廊 14：走「传输文件」的收发在会话里也留一条记录。"""
        if "record" in gallery:
            return
        gallery.add("record")
        report = Path(tmp) / "季度报表.xlsx"
        report.write_bytes(b"\0" * (24 * 1024 * 1024))
        engine.chat.add_message(
            conv,
            sender=engine.info.device_id,
            sender_name="我的电脑",
            text="",
            kind="file",
            media_name=report.name,
            media_size=report.stat().st_size,
            media_source=str(report),
            direction="out",
            state="sending",
        )
        window.chat_view.reload()

    def ensure_phone_dupes() -> None:
        """画廊 16：一台手机的两条注册表记录 → 设备列表里只有一行。"""
        if "phone-dupes" in gallery:
            return
        gallery.add("phone-dupes")
        demo_ui.records = [
            # 页面首次加载那条（只有地址 + UA 摘要）
            dict(demo_phone, key="192.168.1.23|abc12345", online=False, secondsAgo=30.0),
            # 之后带上设备号的那条
            dict(
                demo_phone,
                key="web:phone-uuid-1234",
                deviceId="phone-uuid-1234",
                identityKey="web:phone-uuid-1234",
                online=True,
                secondsAgo=1.0,
            ),
        ]
        window._refresh_devices()

    window.show()
    app.processEvents()

    #: 截图清单 = README 画廊引用的那 14 张（docs/screenshots/）+ 本轮新增的 2 张。
    #: 名字与 README 的表格一一对应，所以 `EVERSEND_SHOT_DIR=docs/screenshots` 跑一次
    #: 就能把画廊整批重拍，不需要人肉对文件名。
    shots: list[tuple[str, dict]] = [
        ("01-session.png", {}),
        ("02-transfers.png", {"page": "session", "view": "transfers", "direction": "send"}),
        ("03-receive.png", {"page": "session", "view": "transfers", "direction": "recv"}),
        ("04-settings.png", {"page": "settings"}),
        ("06-chat.png", {"page": "session", "view": "chat"}),
        ("09-emoji-and-sections.png", {"page": "session", "view": "chat", "emoji": True}),
        # 两段都折叠：收起的那一段只占一行标题、紧贴「设备列表」下边（余下的高度
        # 落在两段下面），同时露出放大后的表情面板。
        ("11-sections-collapsed.png", {"page": "session", "view": "chat", "emoji": True, "collapsed": True}),
        # 10：离线那一段展开着，「以前见过」的设备各自一行（名字 + 地址）。
        ("10-offline-devices.png", {"page": "session", "view": "chat", "offline": True}),
        # 12：消息正文里的 emoji（面板里多大，发出去就多大）。
        ("12-emoji-in-bubble.png", {"page": "session", "view": "chat", "emoji_message": True}),
        # 13：同一个文件收两次，接收表里只占一行（宪章 C8）。
        ("13-one-row-per-file.png", {"page": "session", "view": "transfers", "direction": "recv", "one_row": True}),
        # 14：走「传输文件」的收发会在会话里留一条带状态的记录。
        ("14-transfer-record-in-chat.png", {"page": "session", "view": "chat", "record": True}),
        # 15：设置页的「数据与维护」+「诊断」（宪章 §3.4 第 5、6 组 / E2）。
        ("15-settings-maintenance.png", {"page": "settings", "scroll_bottom": True}),
        # 16：一台手机的两条注册表记录并成一行。
        ("16-one-phone-one-row.png", {"page": "session", "view": "chat", "phone_dupes": True}),
        # 以下两张是本轮新增的核对图（不在 README 画廊里）：
        # 失败行 + 「失败」筛选：药丸是红的、操作格是「重试」。
        ("12-transfers-failed.png", {"page": "session", "view": "transfers", "direction": "send", "failed": True}),
        # 设置页顶部的样子（分组卡片 + 三态单选）；15 那张是滚到底。
        ("13-settings-top.png", {"page": "settings"}),
    ]
    taken = {"n": 0}

    def capture_qr() -> None:
        """Capture the QR dialog, which is its own widget tree."""
        import traceback

        try:
            ifaces = list_interfaces(include_virtual=False)
            url = f"http://{ifaces[0].address}:52119/" if ifaces else "http://127.0.0.1:52119/"
            # Same client list the window has: the dialog shows "已连上：…"
            # the moment a phone opens the page, which is the whole point of
            # that line (see the note in QrDialog).
            dialog = QrDialog(
                url, window, clients=lambda: [demo_phone], alternatives=["http://10.0.0.7:52119/"]
            )
            dialog.resize(420, 600)
            dialog.show()
            dialog.raise_()
        except Exception:
            traceback.print_exc()
            app.quit()
            return

        def grab_and_finish() -> None:
            try:
                ok = dialog.grab().save(str(OUT / "05-qr.png"))
                print(f"wrote {OUT / '05-qr.png'}: {ok}")
            except Exception as exc:  # pragma: no cover - diagnostics
                print(f"QR capture failed: {exc}")
            finally:
                dialog.close()
                capture_preview()

        QTimer.singleShot(500, grab_and_finish)

    def capture_preview() -> None:
        """Capture the file preview, which is a dialog of its own."""
        import traceback

        from eversend.desktop.preview import FilePreviewDialog

        try:
            dialog = FilePreviewDialog(
                str(note), note.name, window, engine.config.data_dir
            )
            dialog.resize(880, 620)
            dialog.show()
            dialog.raise_()
        except Exception:
            traceback.print_exc()
            app.quit()
            return

        def grab_and_finish() -> None:
            try:
                ok = dialog.grab().save(str(OUT / "07-preview.png"))
                print(f"wrote {OUT / '07-preview.png'}: {ok}")
            except Exception as exc:  # pragma: no cover - diagnostics
                print(f"preview capture failed: {exc}")
            finally:
                dialog.close()
                capture_image_preview()

        QTimer.singleShot(500, grab_and_finish)

    def capture_image_preview() -> None:
        """Same dialog, image flavour: 缩略图之外还能看大图。"""
        from eversend.desktop.preview import FilePreviewDialog

        try:
            dialog = FilePreviewDialog(
                str(picture), picture.name, window, engine.config.data_dir
            )
            dialog.resize(880, 620)
            dialog.show()
            dialog.raise_()
        except Exception as exc:  # pragma: no cover - diagnostics
            print(f"image preview capture failed: {exc}")
            app.quit()
            return

        def grab_and_finish() -> None:
            try:
                ok = dialog.grab().save(str(OUT / "08-preview-image.png"))
                print(f"wrote {OUT / '08-preview-image.png'}: {ok}")
            except Exception as exc:  # pragma: no cover - diagnostics
                print(f"image preview capture failed: {exc}")
            finally:
                dialog.close()
                app.quit()

        QTimer.singleShot(500, grab_and_finish)

    def apply(state: dict) -> None:
        page = state.get("page", "session")
        # 画廊场景的演示数据（幂等）：先备好数据，再切页排版。
        if state.get("offline"):
            ensure_offline_devices()
        if state.get("emoji_message"):
            ensure_emoji_message()
        if state.get("one_row"):
            ensure_one_row_per_file()
        if state.get("record"):
            ensure_transfer_record()
        if state.get("phone_dupes"):
            ensure_phone_dupes()
        # 每张图都回到同一个起点：表情面板收起、离线段折叠。
        window.chat_view.emoji_pad.setVisible(False)
        window.chat_view.emoji_button.setChecked(False)
        window.sidebar.offline_section.set_expanded(False)
        window.transfers_view.set_filter("all")
        window.transfers_view.search.clear()
        window.settings_scroll.verticalScrollBar().setValue(0)
        if page == "settings":
            window.show_page("settings")
            if state.get("scroll_bottom"):
                bar = window.settings_scroll.verticalScrollBar()
                bar.setValue(bar.maximum())
            return
        window.show_page("session", view=state.get("view", "chat"))
        if state.get("direction"):
            window.transfers_view.show_direction(state["direction"])
        if state.get("failed"):
            window.transfers_view.set_filter("failed")
        if state.get("emoji"):
            window.sidebar.offline_section.set_expanded(True)
            window.chat_view.emoji_button.click()
        if state.get("collapsed"):
            window.sidebar.offline_section.set_expanded(False)
            window.sidebar.online_section.set_expanded(False)
        if state.get("offline"):
            # 离线段展开、在线段收起来：这一张要讲的就是"以前见过的设备"。
            window.sidebar.online_section.set_expanded(False)
            window.sidebar.offline_section.set_expanded(True)

    def shoot() -> None:
        index = taken["n"]
        if index >= len(shots):
            capture_qr()
            return
        name, state = shots[index]
        apply(state)
        taken["n"] += 1
        QTimer.singleShot(
            300,
            lambda: (
                window.grab().save(str(OUT / name)),
                print(f"wrote {OUT / name}"),
                shoot(),
            ),
        )

    QTimer.singleShot(400, shoot)
    QTimer.singleShot(25000, app.quit)  # hard stop: never hang CI

    code = app.exec()
    engine.stop()
    _scratch_dir.__exit__(None, None, None)
    print(f"rendered {len(list(OUT.glob('*.png')))} screenshot(s) into {OUT}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
