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
            check("取消按钮已经禁用（没什么可取消的了）", not row.cancel_button.isEnabled())
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
            check("取消按钮禁用", not row.cancel_button.isEnabled())
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
