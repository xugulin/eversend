#!/usr/bin/env python3
"""QA 截图：把验收关心的几个界面状态拍下来，落到 tests/screenshots/qa-*.png。

文件名统一 `qa-` 前缀，**不覆盖** `tests/test_gui_render.py` 拍的那一套
（`01-session.png` 之类）—— 两套截图并存，各自的命名空间互不干扰。

用法::

    xvfb-run -a -s "-screen 0 1280x900x24" .venv/bin/python tools/qa_screenshots.py

拍的是"验收要判定什么"，不是"界面好不好看"：

* `qa-01-device-list.png`  设备列表：同一身份一行、名字省略、在线/离线两段
* `qa-02-transfers.png`    传输页：筛选药丸 + 列头 + 空状态
* `qa-03-transfers-fail.png` 失败行：中文原因 + 重试入口
* `qa-04-settings-diag.png` 设置页（含诊断卡片，E2）
* `qa-05-preview-image.png` 图片预览：工具栏 + 适应窗口不放大
* `qa-06-preview-text.png`  文本预览：等宽可选中
* `qa-07-preview-binary.png` 二进制预览：不装作能看
* `qa-08-chat-pending.png`  会话：待发消息（时钟角标 + 等待对方上线）

数据目录走 `EVERSEND_HOME`（没设就落在 `.testscratch/`），不碰仓库的 `data/`。
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

OUT = Path(os.environ.get("EVERSEND_QA_SHOT_DIR") or (REPO / "tests" / "screenshots"))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    os.environ["QT_QPA_PLATFORM"] = os.environ.get("EVERSEND_GUI_PLATFORM", "xcb")
    if not os.environ.get("EVERSEND_HOME"):
        os.environ["EVERSEND_HOME"] = tempfile.mkdtemp(
            prefix="qa-shots-", dir=str(REPO / ".testscratch"))

    from PySide6.QtWidgets import QApplication

    from eversend.core.engine import Engine, EngineConfig
    from eversend.core.model import DeviceInfo, Peer
    from eversend.desktop import i18n, theme
    from eversend.desktop.main_window import MainWindow

    app = QApplication(sys.argv[:1])
    i18n.install(app)
    dark = theme.is_dark()
    app.setStyleSheet(theme.stylesheet(dark))
    app.setWindowIcon(theme.app_icon(128, dark))

    root = Path(os.environ["EVERSEND_HOME"])
    engine = Engine(EngineConfig(data_dir=str(root / "data"), receive_dir=str(root / "received"),
                                 name="我的电脑", tcp_port=0, discovery_port=free_port(),
                                 auto_accept_all=True, enable_broadcast=False, enable_mdns=False,
                                 enable_web=False))
    engine.start()
    window = MainWindow(engine, str(root / "settings.json"))
    window.resize(1280, 820)
    window.show()
    app.processEvents()

    OUT.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    def shoot(name: str) -> None:
        app.processEvents()
        path = OUT / name
        window.grab().save(str(path), "PNG")
        written.append(str(path))
        print(f"  {path}")

    from eversend.core.chat import direct_conversation_id

    peer = Peer(info=DeviceInfo(device_id="qa-shot-peer", name="客厅台式机", kind="desktop",
                                platform="windows", tcp_port=52117),
                address="192.168.1.42", port=52117, source="multicast", trusted=True)
    twin = Peer(info=DeviceInfo(device_id="qa-shot-peer", name="客厅台式机", kind="desktop",
                                platform="windows", tcp_port=52117),
                address="192.168.1.42", port=52117, source="broadcast")
    offline = Peer(info=DeviceInfo(device_id="qa-shot-off", name="书房的 MacBook Pro", kind="desktop",
                                   platform="macos", tcp_port=52117),
                   address="192.168.1.77", port=52117, source="scan")

    # ① 设备列表：故意塞两行同身份，验收点就是"界面上只有一行"
    window.sidebar.set_devices([peer, twin, engine.self_peer()], {})
    window._refresh_offline_count()
    qt_offline = getattr(window.sidebar, "offline_table", None)
    if qt_offline is not None:
        try:
            window.sidebar.offline_section.set_peers([offline], {})
        except Exception:
            pass
    window.show_page("session")
    shoot("qa-01-device-list.png")

    # ② / ③ 传输页：正常 + 失败行
    window.show_page("session", view="transfers")
    window.transfers_view.show_direction("recv")
    sample = root / "示例文件.bin"
    sample.write_bytes(b"\0" * (3 * 1024 * 1024))
    window._recv_from["qa-shot-xfer"] = "客厅台式机"
    window._add_history(sample.name, str(sample), "qa-shot-xfer")
    window.transfers_view.refresh_empty()
    shoot("qa-02-transfers.png")

    row = window._add_transfer_row("qa-shot-fail", "发送到 客厅台式机", "send")
    row.set_files("坏文件.bin")
    if hasattr(row, "finish"):
        row.finish(False, "连接被拒绝")
    window.transfers_view.show_direction("send")
    shoot("qa-03-transfers-fail.png")

    # ④ 设置页（含诊断卡片）
    window.show_page("settings")
    shoot("qa-04-settings-diag.png")

    # ⑤/⑥/⑦ 预览三态
    from PySide6.QtGui import QColor, QImage

    from eversend.desktop.preview import FilePreviewDialog

    picture = root / "示例图片.png"
    image = QImage(160, 100, QImage.Format_RGB32)
    for x in range(image.width()):
        for y in range(image.height()):
            image.setPixelColor(x, y, QColor(40 + x % 200, 90 + y % 150, 200 - x // 2))
    image.save(str(picture), "PNG")

    note = root / "说明.md"
    note.write_text("# 说明\n\n这是一个用来演示预览的文本文件。\n" * 20, encoding="utf-8")

    blob = root / "固件.img"
    blob.write_bytes(bytes(range(256)) * 256)

    for name, path in (("qa-05-preview-image.png", picture),
                       ("qa-06-preview-text.png", note),
                       ("qa-07-preview-binary.png", blob)):
        dialog = FilePreviewDialog(str(path), path.name, parent=window)
        dialog.resize(880, 620)
        dialog.show()
        app.processEvents()
        (OUT / name).write_bytes(b"")  # 占位，随后被真实截图覆盖
        dialog.grab().save(str(OUT / name), "PNG")
        written.append(str(OUT / name))
        print(f"  {OUT / name}")
        dialog.close()

    # ⑧ 会话：待发消息（时钟角标 + 等待对方上线）
    conv = direct_conversation_id(engine.info.device_id, peer.info.device_id)
    engine.chat.upsert_conversation(conv, kind="direct", title="和 客厅台式机 的对话",
                                    members=[engine.info.device_id, peer.info.device_id])
    engine.chat.add_message(conv, sender=peer.info.device_id, sender_name="客厅台式机",
                            text="报告我放桌面上啦", direction="in")
    engine.chat.add_message(conv, sender=engine.info.device_id, sender_name="我的电脑",
                            text="收到，马上传给你", direction="out")
    pending = engine.chat.add_message(conv, sender=engine.info.device_id, sender_name="我的电脑",
                                      text="这条还在排队（对面不在线）", direction="out",
                                      state="queued")
    window.chat_view.open_conversation(conv)
    window._update_peer_header()
    window.show_page("session", view="chat")
    app.processEvents()
    shoot("qa-08-chat-pending.png")
    print(f"  待发消息 id={pending.get('id')} state={pending.get('state')}")

    engine.stop()
    print(f"\n共 {len(written)} 张，写到 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
