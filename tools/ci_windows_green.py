#!/usr/bin/env python3
"""Run the *packaged* Windows artifact for real, on a real Windows machine.

Why this file exists
--------------------
An artifact that has never been executed is a guess.  On Linux the Windows
package can only be checked structurally -- the files are there, the launchers
point at paths that exist -- because ``python.exe`` cannot run there.  That is
exactly the kind of verification that stays green while the product is broken:
two of the worst bugs in this project's history (POSIX-only ``os.pwrite``, and
``os.open`` defaulting to *text* mode on Windows) would have passed every
structural check ever written.

So this script is run by CI **on a real ``windows-latest`` runner**, using the
interpreter that is inside the extracted zip and nothing else.  It proves three
things a user would notice immediately if they were false:

1. the bundled CPython starts, and the bundled wheels (``cryptography``,
   ``PySide6``) import -- with no system Python involved at all;
2. a real transfer through the packaged code is byte-identical, which is what
   exercises the Windows-specific positioned I/O and binary-mode handling;
3. the desktop window really builds and paints with the bundled Qt.

Usage (the interpreter must be the artifact's own)::

    EverSend\\runtime\\python.exe tools\\ci_windows_green.py --tree <EverSend dir>
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import threading
import time
from pathlib import Path

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    print(f"  {PASS if condition else FAIL} {name}" + ("" if condition else f"  {detail}"))
    if not condition:
        _failures.append(name)
    return bool(condition)


def use_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_interpreter(tree: Path) -> None:
    """The interpreter and the imports must come from the tree, not the runner."""
    print("\n[1] 内置解释器与内置依赖")
    print(f"      python : {sys.executable}")
    print(f"      version: {sys.version.split()[0]}")
    # ``samefile`` rather than a string prefix: the interpreter can be reached
    # through more than one path (a mapped drive, an 8.3 short name, a UNC
    # share) and all of them are the same file.
    try:
        same = os.path.samefile(sys.executable, tree / "runtime" / "python.exe")
    except OSError:
        same = Path(sys.executable).resolve() == (tree / "runtime" / "python.exe").resolve()
    check("跑的是包里的解释器", same, sys.executable)
    check("不是系统 Python", "hostedtoolcache" not in sys.executable.lower(), sys.executable)

    import eversend

    module_path = Path(eversend.__file__).resolve()
    check(
        "eversend 包来自 app/ 目录",
        module_path.is_relative_to((tree / "app").resolve()),
        str(module_path),
    )

    from eversend.core import crypto

    check("内置 cryptography 可用（加密传输不是关着的）", crypto.CRYPTO_AVAILABLE)
    if crypto.CRYPTO_AVAILABLE:
        key = os.urandom(32)
        cipher = crypto.Cipher(key, b"i", "aes256gcm")
        sealed = cipher.seal(b"hello eversend")
        opener = crypto.Cipher(key, b"i", "aes256gcm")
        check("AES-256-GCM 能加密也能解密", opener.open(sealed) == b"hello eversend")


def check_transfer(tree: Path, work: Path) -> None:
    """A real transfer, through the packaged code, on real Windows."""
    print("\n[2] 包里的代码真传一个文件（走完整协议）")
    from eversend.core.engine import Engine, EngineConfig
    from eversend.core.model import Peer

    work.mkdir(parents=True, exist_ok=True)
    payload_path = work / "windows-green.bin"
    payload = os.urandom(24 * 1024 * 1024)
    payload_path.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    receiver = Engine(
        EngineConfig(
            data_dir=str(work / "recv-state"),
            receive_dir=str(work / "recv"),
            name="GreenReceiver",
            tcp_port=0,
            discovery_port=free_port(),
            enable_broadcast=False,
            enable_mdns=False,
            enable_web=False,
            auto_accept_all=True,
        )
    )
    sender = Engine(
        EngineConfig(
            data_dir=str(work / "send-state"),
            receive_dir=str(work / "send-recv"),
            name="GreenSender",
            tcp_port=0,
            discovery_port=free_port(),
            enable_broadcast=False,
            enable_mdns=False,
            enable_web=False,
            auto_accept_all=True,
        )
    )
    receiver.start()
    sender.start()
    try:
        peer = Peer(info=receiver.info, address="127.0.0.1", port=receiver.port)
        result: list[bool] = []

        def send() -> None:
            try:
                result.append(sender.send(peer, [str(payload_path)]))
            except Exception as exc:  # pragma: no cover - reported as a failure
                print(f"      send raised: {exc!r}")
                result.append(False)

        started = time.monotonic()
        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        thread.join(timeout=300)
        elapsed = max(1e-6, time.monotonic() - started)

        landed = work / "recv" / "windows-green.bin"
        check("传输成功", result == [True], str(result))
        check("文件落地", landed.exists(), str(landed))
        if landed.exists():
            check("大小一致", landed.stat().st_size == len(payload))
            check("SHA-256 逐字节一致", sha256_file(landed) == expected)
            print(f"      24 MiB in {elapsed:.2f}s = {24 / elapsed:.0f} MiB/s")
    finally:
        sender.stop()
        receiver.stop()


def check_gui(tree: Path, work: Path) -> None:
    """The bundled Qt must build and paint the real main window."""
    print("\n[3] 内置 Qt 真的能画出窗口")
    work.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("EVERSEND_DATA_DIR", str(work / "gui-data"))

    from PySide6 import __version__ as pyside_version
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from eversend.core.engine import Engine, EngineConfig
    from eversend.desktop import theme
    from eversend.desktop.main_window import MainWindow

    shot = work / "green-window.png"

    # Qt's own headless platform plugin, deliberately: a CI runner has no
    # desktop session to speak of, and asking the native Windows plugin to
    # paint into one is not what this check is about.  (It is also actively
    # hostile: with the native plugin under Wine, grabbing a 1180x760 window on
    # a 1024x768 virtual screen ballooned the process from 139 MiB to 8.4 GB.
    # Same window, same code, offscreen: 139 MiB and a PNG.)  What is being
    # tested is that the *bundled* Qt builds, styles and paints our window.
    app = None
    for platform_name in ("offscreen", "", "minimal"):
        if platform_name:
            os.environ["QT_QPA_PLATFORM"] = platform_name
        try:
            app = QApplication(sys.argv[:1])
            print(f"      Qt platform: {app.platformName()}")
            break
        except Exception as exc:
            print(f"      Qt platform {platform_name or '(原生)'} 起不来: {exc}")
    if app is None:
        check("QApplication 能创建", False, "所有平台插件都失败了")
        return

    dark = theme.is_dark()
    app.setStyleSheet(theme.stylesheet(dark))
    engine = Engine(
        EngineConfig(
            data_dir=str(work / "gui-state"),
            receive_dir=str(work / "gui-recv"),
            name="GreenWindows",
            tcp_port=0,
            discovery_port=free_port(),
            enable_broadcast=False,
            enable_mdns=False,
            enable_web=False,
        )
    )
    engine.start()
    window = MainWindow(engine)

    # Never ask for a window bigger than the screen it has to be painted on.
    screen = app.primaryScreen()
    available = screen.availableGeometry() if screen else None
    width = min(1180, available.width()) if available else 1180
    height = min(760, available.height()) if available else 760
    window.resize(max(720, width), max(520, height))
    print(f"      窗口 {window.width()}x{window.height()}（屏幕 {available.width() if available else '?'}x{available.height() if available else '?'}）")

    def capture() -> None:
        window.grab().save(str(shot))
        app.quit()

    window.show()
    QTimer.singleShot(1500, capture)
    QTimer.singleShot(60_000, app.quit)  # never let a paint problem hang CI
    app.exec()

    check(f"PySide6 {pyside_version} 能建出主窗口", window.isVisible() or shot.exists())
    check("窗口截图已保存", shot.exists() and shot.stat().st_size > 5_000,
          f"{shot.stat().st_size if shot.exists() else 0} 字节")
    engine.stop()


def use_tree(tree: Path) -> None:
    """Put the artifact's own code on ``sys.path``.

    ``run.bat`` does exactly this with ``PYTHONPATH`` before starting
    ``app/eversend_green.py``.  Doing it here too means the script cannot
    accidentally measure the *checkout's* code while claiming to test the
    package -- and it means nobody has to remember to export PYTHONPATH in CI.
    """
    for name in ("site", "app"):  # app last: it wins, and must come first
        path = tree / name
        if path.is_dir():
            sys.path.insert(0, str(path))


def main() -> int:
    use_utf8_console()
    parser = argparse.ArgumentParser()
    parser.add_argument("--tree", type=Path, required=True, help="解压出来的 EverSend 目录")
    parser.add_argument("--work", type=Path, default=None, help="临时目录（默认系统 TEMP）")
    parser.add_argument(
        "--phase",
        choices=("all", "interpreter", "transfer", "gui"),
        default="all",
        help="只跑其中一段（排查用；CI 跑 all）",
    )
    args = parser.parse_args()

    tree = args.tree.resolve()
    work = args.work or Path(os.environ.get("TEMP", ".")) / "eversend-green-check"

    print("EverSend 绿色包 · 真 Windows 上跑一遍")
    print("=" * 70)
    print(f"tree : {tree}")

    if sys.platform != "win32":
        print("  这个脚本只应该跑在 Windows 上")
        return 2

    use_tree(tree)

    if args.phase in ("all", "interpreter"):
        check_interpreter(tree)
    if args.phase in ("all", "transfer"):
        check_transfer(tree, work)
    if args.phase in ("all", "gui"):
        check_gui(tree, work)

    print("\n" + "=" * 70)
    if _failures:
        print(f"{len(_failures)} 项失败：")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print("绿色包在真 Windows 上跑通了：解释器、加密、传输、界面全部来自包内。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
