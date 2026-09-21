#!/usr/bin/env python3
"""EverSend entry point for the portable ("green") build.

``build_green.py`` copies this file into the artifact as
``EverSend/app/eversend_green.py`` and the launchers run it with the embedded
interpreter.  It lives in ``tools/`` rather than in ``src/eversend/`` on
purpose: the packaging toolchain owns it, and it must not depend on a
``__main__.py`` inside the application package (which does not exist yet).

Three modes:

``--selftest``
    Import the core, print versions, prove the transfer port can be bound and
    release it.  Used by ``build_green.py`` and ``verify_green.py``; exits
    non-zero on any problem.

``--cli``
    Headless: start the engine and stay in the foreground until Ctrl-C.
    This is the documented fallback when there is no graphical session.

default
    Start the Qt desktop UI when ``eversend.desktop`` provides one; otherwise
    say so clearly (in Chinese) and fall back to ``--cli`` rather than dying
    with a traceback the user cannot act on.

Everything is resolved relative to this file, never from the current directory,
so the launcher can run it from anywhere.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

#: ``EverSend/`` -- the directory the user unzipped.
APP_HOME = Path(__file__).resolve().parent.parent
APP_DIR = APP_HOME / "app"
SITE_DIR = APP_HOME / "site"
RUNTIME_DIR = APP_HOME / "runtime"


def _bootstrap_sys_path() -> None:
    """Make ``app/`` and ``site/`` importable even if PYTHONPATH was lost.

    The launchers set ``PYTHONPATH`` already; doing it again here costs nothing
    and keeps the app working when someone runs the interpreter by hand.
    """
    for candidate in (str(APP_DIR), str(SITE_DIR)):
        if candidate not in sys.path:
            sys.path.append(candidate)


def _data_dir() -> Path:
    """Where mutable state (config, identity, logs, partial transfers) lives.

    The launchers pass ``EVERSEND_DATA_DIR``: normally ``<app>/data``, or a
    per-user temporary directory when the medium is read-only.  ``received/``
    is derived the same way.  Creating the directories here (and not at import
    time) keeps ``--selftest`` free of side effects.
    """
    override = os.environ.get("EVERSEND_DATA_DIR")
    base = Path(override) if override else APP_HOME / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _receive_dir() -> Path:
    override = os.environ.get("EVERSEND_RECEIVE_DIR")
    base = Path(override) if override else APP_HOME / "received"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _print_environment() -> None:
    import platform

    print(f"python      : {sys.version.split()[0]} ({sys.executable})")
    print(f"prefix      : {sys.prefix}")
    print(f"app home    : {APP_HOME}")
    print(f"platform    : {platform.platform()}")


def selftest() -> int:
    """Fast sanity check of the assembled tree.  Returns a process exit code.

    A port that is already taken is reported as a *warning*, not a failure:
    ``EADDRINUSE`` means another EverSend (or an unrelated program) holds it,
    which says nothing about whether this artifact is sound -- and failing a
    build because the developer left the app running would be wrong.  Every
    other bind error still fails.
    """
    import errno

    failures: list[str] = []
    warnings: list[str] = []
    print("EverSend green self-test")
    print("=" * 60)
    _print_environment()

    try:
        from eversend.core import constants as core_constants

        print(f"core        : {core_constants.APP_NAME} {core_constants.APP_NAME_CN} {core_constants.APP_VERSION}")
        print(f"protocol    : v{core_constants.PROTOCOL_VERSION} magic={core_constants.MAGIC!r}")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        failures.append("import eversend.core.constants")
        core_constants = None  # type: ignore[assignment]

    try:
        import PySide6

        from PySide6 import QtCore

        print(f"PySide6     : {PySide6.__version__} at {PySide6.__file__}")
        print(f"Qt          : {QtCore.qVersion()} ({QtCore.__file__})")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        failures.append("import PySide6.QtCore")

    try:
        import cryptography

        print(f"cryptography: {cryptography.__version__} at {cryptography.__file__}")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        Ed25519PrivateKey.generate()
        X25519PrivateKey.generate()
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

        ChaCha20Poly1305(ChaCha20Poly1305.generate_key())
        print("crypto      : Ed25519 / X25519 / ChaCha20-Poly1305 available")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        failures.append("import cryptography (+ AEAD smoke test)")

    if core_constants is not None:
        import socket

        port = core_constants.DEFAULT_TCP_PORT
        def probe_port(kind: str, family: int, sock_type: int, number: int) -> None:
            try:
                with socket.socket(family, sock_type) as probe:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind(("0.0.0.0", number))
                print(f"port        : {kind}/{number} can be bound")
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE:
                    warnings.append(f"{kind}/{number} busy")
                    print(
                        f"port        : {kind}/{number} is already in use "
                        f"(另一个 EverSend 实例正在运行？这不影响程序本身)"
                    )
                else:
                    print(f"port        : {kind}/{number} NOT bindable: {exc}")
                    failures.append(f"bind {kind}/{number}")

        probe_port("tcp", socket.AF_INET, socket.SOCK_STREAM, port)
        probe_port("udp", socket.AF_INET, socket.SOCK_DGRAM, core_constants.DEFAULT_DISCOVERY_PORT)

    print("=" * 60)
    if warnings:
        print(f"warnings    : {', '.join(warnings)}")
    if failures:
        print(f"SELFTEST FAILED: {', '.join(failures)}")
        return 1
    print("SELFTEST OK")
    return 0


def transfer_selftest(size_mib: int = 4) -> int:
    """Real loopback transfer between two in-process engines.

    This is the "does this *machine* actually work" check: it exercises the
    socket stack, the chunk store, the digest verification and (because
    ``encrypt=True`` by default) the bundled ``cryptography`` build, then
    compares SHA-256 of the bytes that landed on disk with the bytes sent.
    Everything happens inside the data directory, so on a read-only medium it
    runs in the temp fallback; nothing outside is touched.
    """
    import hashlib
    import shutil
    import socket
    import tempfile

    from eversend.core.engine import Engine, EngineConfig

    failures: list[str] = []
    print(f"EverSend loopback transfer self-test ({size_mib} MiB)")
    print("=" * 60)
    root = Path(tempfile.mkdtemp(prefix="transfer-selftest-", dir=str(_data_dir())))
    try:
        def free_port() -> int:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                return int(probe.getsockname()[1])

        engines = []
        for label in ("A", "B"):
            (root / label).mkdir(parents=True, exist_ok=True)
            engine = Engine(
                EngineConfig(
                    data_dir=str(root / label / "state"),
                    receive_dir=str(root / label / "recv"),
                    name=f"SelfTest-{label}",
                    tcp_port=0,
                    discovery_port=free_port(),
                    streams=2,
                    auto_accept_all=True,
                    enable_broadcast=False,
                    enable_mdns=False,
                    enable_web=False,
                )
            )
            engine.start()
            engines.append(engine)
        sender, receiver = engines

        source = root / "A" / "payload.bin"
        digest = hashlib.sha256()
        block = bytes(range(256)) * 4096  # 1 MiB, deterministic and compressible
        with source.open("wb") as handle:
            for _ in range(size_mib):
                handle.write(block)
                digest.update(block)
        expected = digest.hexdigest()

        from eversend.core.model import Peer

        peer = Peer(info=receiver.info, address="127.0.0.1", port=receiver.port)
        ok = sender.send(peer, [str(source)])
        received = root / "B" / "recv" / "payload.bin"
        if not ok:
            failures.append("send() reported failure")
        if not received.exists():
            failures.append("received file missing")
        else:
            actual = hashlib.sha256(received.read_bytes()).hexdigest()
            if actual != expected:
                failures.append(f"sha256 mismatch ({actual[:16]} != {expected[:16]})")
            else:
                print(f"  transferred {size_mib} MiB, sha256 {actual[:32]}... matches")
        for engine in engines:
            engine.stop()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        failures.append("loopback transfer raised")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("=" * 60)
    if failures:
        print(f"TRANSFER SELFTEST FAILED: {'; '.join(failures)}")
        return 1
    print("TRANSFER SELFTEST OK")
    return 0


def _desktop_entry():
    """Return a callable that starts the Qt UI, or ``None`` if there is none.

    The desktop package is still being written, so we probe for the two shapes
    it is most likely to take instead of hard-coding an import that would turn
    "UI not ready yet" into a crash.
    """
    for module_name, attribute in (
        ("eversend.desktop.app", "main"),
        ("eversend.desktop", "main"),
        ("eversend.desktop.__main__", "main"),
    ):
        try:
            module = __import__(module_name, fromlist=["*"])
        except Exception:  # noqa: BLE001 - "no UI yet" is an expected state
            continue
        entry = getattr(module, attribute, None)
        if callable(entry):
            return entry
    return None



def run_cli(argv: list[str]) -> int:
    """Headless mode: hand over to the application's own CLI.

    ``eversend.cli`` already implements ``serve`` / ``discover`` / ``send`` /
    ``selftest`` and defaults to ``serve``, so the portable launcher adds
    nothing here -- it only makes sure ``EVERSEND_HOME`` points inside the
    artifact first (see the launchers).
    """
    try:
        from eversend.cli import main as cli_main
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1
    return int(cli_main(argv) or 0)


def run_gui(argv: list[str]) -> int:
    """Start the Qt UI through the application's own bootstrap."""
    entry = _desktop_entry()
    if entry is None:
        print("提示：当前构建里还没有图形界面模块（eversend.desktop）。")
        print("      将以命令行（headless）模式启动，功能与界面模式下完全一致。")
        return run_cli(argv)
    return int(entry(argv) or 0)


def _show_error_box(message: str) -> None:
    """Best-effort Windows message box so a double-click failure is visible.

    Only used when the launcher asked for it; on Linux (or when Qt is what
    broke) we simply write to stderr and the log file.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "EverSend 韧传 - 启动失败", 0x10)
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    _bootstrap_sys_path()
    parser = argparse.ArgumentParser(
        prog="eversend",
        description="EverSend 韧传 (portable build launcher)",
        add_help=True,
    )
    parser.add_argument("--selftest", action="store_true", help="run environment checks and exit")
    parser.add_argument(
        "--selftest-transfer",
        action="store_true",
        help="run a 4 MiB loopback transfer self-test and exit",
    )
    parser.add_argument("--cli", action="store_true", help="run headless (no window)")
    parser.add_argument("--print-paths", action="store_true", help="print resolved paths and exit")
    parser.add_argument("--error-box", metavar="MESSAGE", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    args, extra = parser.parse_known_args(argv)

    if args.error_box is not None:
        _show_error_box(args.error_box)
        return 1
    if args.print_paths:
        _print_environment()
        print(f"data dir    : {os.environ.get('EVERSEND_DATA_DIR') or APP_HOME / 'data'}")
        print(f"read-only   : {os.environ.get('EVERSEND_READONLY') or '0'}")
        return 0
    if args.version:
        try:
            from eversend.core.constants import APP_VERSION

            print(f"EverSend {APP_VERSION}")
        except Exception:  # noqa: BLE001
            print("EverSend (core not importable)")
        return 0
    if args.selftest:
        return selftest()
    if args.selftest_transfer:
        # The application ships a deeper self-test (engine + 3 MiB loopback
        # transfer + crypto); use it when it is importable and fall back to the
        # toolchain's own in-process transfer otherwise.
        try:
            from eversend.cli import main as cli_main

            return int(cli_main(["selftest"]) or 0)
        except ImportError:
            return transfer_selftest()
    if args.cli:
        return run_cli(extra)

    if os.name != "nt":
        # No display: fail with an actionable Chinese message instead of a Qt
        # "could not connect to display" abort.
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            print("错误：没有检测到图形界面（DISPLAY / WAYLAND_DISPLAY 均未设置）。", file=sys.stderr)
            print("      EverSend 的窗口需要图形会话。你可以：", file=sys.stderr)
            print("        1) 在桌面环境里运行本程序；或", file=sys.stderr)
            print("        2) 使用命令行模式：./run.sh --cli", file=sys.stderr)
            return 3

    try:
        return run_gui(extra)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        _show_error_box("EverSend 启动失败，详情见日志文件。\n\n" + traceback.format_exc()[-1500:])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
