#!/usr/bin/env python3
"""Render the desktop UI off-screen and save screenshots.

Run under a virtual display so it works on a headless machine::

    xvfb-run -a python3 tests/test_gui_render.py

This proves the window actually builds and paints -- a GUI that raises during
construction is otherwise only discovered by a user clicking the launcher.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _scratch import scratch, use_utf8_console  # noqa: E402

OUT = Path(__file__).resolve().parent / "screenshots"


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
    window.resize(1080, 760)

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

        def clients(self):
            return [demo_phone]

        def known_clients(self):
            # The window lists *remembered* phones, not just the ones talking
            # right now: a phone stays paired while its screen is off.
            return [dict(demo_phone, key="192.168.1.23|abc12345", online=True, secondsAgo=1.0)]

        def urls(self):
            return [self.url]

    demo_ui = _DemoWebUI()
    demo_ui.url = "http://192.168.1.5:52119/"
    window.on_web_ui_started(52119, demo_ui)
    window._refresh_web_clients()
    # Refresh the list now, not in two seconds: the send-tab screenshot should
    # show the connected phone the way a user sees it.
    window._refresh_devices()
    window.show()

    # Put some content in the send tab so the screenshot shows a real state.
    sample = Path(tmp) / "示例文件.bin"
    sample.write_bytes(b"\0" * (12 * 1024 * 1024))
    folder = Path(tmp) / "照片"
    (folder / "子目录").mkdir(parents=True)
    (folder / "IMG_0001.jpg").write_bytes(b"\0" * (3 * 1024 * 1024))
    (folder / "子目录" / "IMG_0002.jpg").write_bytes(b"\0" * 900_000)
    window._add_paths([str(sample), str(folder)])

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
    window.device_table.set_peers([fake] + window._phone_peers() + engine.devices())

    window._add_transfer_row("demo-xfer", "发送到 客厅台式机", "send")
    row = window._transfers["demo-xfer"]
    row.update_progress(7 * 1024 * 1024, 12 * 1024 * 1024, 92 * 1024 * 1024, 0.05)
    row.set_status("传输中")
    window._add_history("IMG_0001.jpg", str(folder / "IMG_0001.jpg"), "demo")

    # 文件名必须对得上真正截到的那一页：之前 04-settings.png 拍的是「聊天」页，
    # README 里就挂着一张标着「设置」的聊天截图。
    shots: list[tuple[str, int]] = [
        ("01-send.png", 0),
        ("02-transfers.png", 2),
        ("03-receive.png", 1),
        ("04-settings.png", 4),
        ("06-chat.png", 3),
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
                app.quit()

        QTimer.singleShot(500, grab_and_finish)

    def shoot() -> None:
        index = taken["n"]
        if index >= len(shots):
            capture_qr()
            return
        name, tab = shots[index]
        window.tabs.setCurrentIndex(tab)
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
    QTimer.singleShot(8000, app.quit)  # hard stop: never hang CI

    code = app.exec()
    engine.stop()
    _scratch_dir.__exit__(None, None, None)
    print(f"rendered {len(list(OUT.glob('*.png')))} screenshot(s) into {OUT}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
