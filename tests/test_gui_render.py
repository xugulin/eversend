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
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

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
    window.device_table.set_peers([fake] + engine.devices())

    window._add_transfer_row("demo-xfer", "发送到 客厅台式机", "send")
    row = window._transfers["demo-xfer"]
    row.update_progress(7 * 1024 * 1024, 12 * 1024 * 1024, 92 * 1024 * 1024, 0.05)
    row.set_status("传输中")
    window._add_history("IMG_0001.jpg", str(folder / "IMG_0001.jpg"), "demo")

    shots: list[tuple[str, int]] = [
        ("01-send.png", 0),
        ("02-transfers.png", 2),
        ("03-receive.png", 1),
        ("04-settings.png", 3),
    ]
    taken = {"n": 0}

    def capture_qr() -> None:
        """Capture the QR dialog, which is its own widget tree."""
        import traceback

        try:
            ifaces = list_interfaces(include_virtual=False)
            url = f"http://{ifaces[0].address}:52119/" if ifaces else "http://127.0.0.1:52119/"
            dialog = QrDialog(url, window)
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
