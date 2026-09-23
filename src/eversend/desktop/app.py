"""Qt application bootstrap."""

from __future__ import annotations

import os
import sys
import traceback
from typing import Sequence

from ..core.constants import APP_NAME, APP_VERSION
from ..core.engine import Engine, EngineConfig
from ..core.model import default_device_name
from ..core.platform_open import default_download_dir, is_graphical_session
from . import theme


def portable_root() -> str:
    """The folder the portable package lives in, or the CWD in a source tree.

    Everything the application writes goes under this directory so that
    unzipping to a USB stick and running from there leaves nothing behind on
    the host machine.  The launcher exports ``EVERSEND_HOME``; when the app is
    run straight from a source checkout we fall back to the current directory.
    """
    override = os.environ.get("EVERSEND_HOME")
    if override:
        return os.path.abspath(override)
    return os.path.abspath(os.getcwd())


def writable_dir(preferred: str, fallback_name: str) -> str:
    """Return ``preferred`` when it can actually be written to.

    A USB stick can be write-protected or mounted read-only, and a package
    running from one must still start.  The fallback is a per-user temp
    directory so the application degrades instead of dying.
    """
    try:
        os.makedirs(preferred, exist_ok=True)
        probe = os.path.join(preferred, ".write-test")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.unlink(probe)
        return preferred
    except OSError:
        import getpass
        import tempfile

        try:
            who = getpass.getuser()
        except Exception:
            who = "user"
        fallback = os.path.join(tempfile.gettempdir(), f"{fallback_name}-{who}")
        os.makedirs(fallback, exist_ok=True)
        return fallback


def build_config(root: str | None = None) -> EngineConfig:
    """Assemble the engine configuration for a portable run."""
    root = root or portable_root()
    data_dir = writable_dir(os.path.join(root, "data"), "eversend")
    receive_dir = os.path.join(root, "received")
    if not os.access(root, os.W_OK):
        receive_dir = os.path.join(default_download_dir(), "EverSend")

    config = EngineConfig(
        data_dir=data_dir,
        receive_dir=receive_dir,
        name=default_device_name(),
    )

    settings_path = os.path.join(data_dir, "settings.json")
    if os.path.exists(settings_path):
        from .main_window import apply_settings, load_settings

        apply_settings(config, load_settings(settings_path))

    try:
        os.makedirs(config.receive_dir, exist_ok=True)
    except OSError:
        config.receive_dir = writable_dir(
            os.path.join(default_download_dir(), "EverSend"), "eversend-recv"
        )
        os.makedirs(config.receive_dir, exist_ok=True)
    return config


def run_gui(argv: Sequence[str] | None = None) -> int:
    """Start the desktop application.  Returns the process exit code."""
    argv = list(argv if argv is not None else sys.argv[1:])

    if not is_graphical_session() and "--cli" not in argv:
        print(
            "韧传 EverSend 需要图形界面才能启动。\n"
            "当前环境没有检测到 DISPLAY / WAYLAND_DISPLAY。\n"
            "如果只是想在这台机器上收发文件，请改用：  run.sh --cli\n",
            file=sys.stderr,
        )
        return 2

    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:  # pragma: no cover - packaging failure
        print(f"无法加载 PySide6：{exc}", file=sys.stderr)
        return 3

    # 先读设置再建 QApplication：主题偏好（深色/浅色）决定用哪套样式表，
    # 建完窗口再换会先按另一套颜色画一遍。
    config = build_config()

    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("EverSend")
    # Closing the window must actually quit, including the engine's threads.
    app.setQuitOnLastWindowClosed(True)

    dark = theme.is_dark()
    app.setStyleSheet(theme.stylesheet(dark))
    app.setWindowIcon(theme.app_icon(128, dark))

    engine = Engine(config)

    from .main_window import MainWindow

    window = MainWindow(engine, os.path.join(config.data_dir, "settings.json"))

    try:
        engine.start()
    except Exception as exc:
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.critical(
            None,
            "启动失败",
            f"无法启动传输服务：{exc}\n\n"
            f"端口 {config.tcp_port} 可能被占用，或者防火墙拦截了监听。",
        )
        return 4

    # The browser UI is how phones take part: PySide6 has no Android build, so
    # the mobile experience is a web page served from this process.  It is
    # optional -- a failure here must not stop the desktop app from running.
    web_ui = start_web_ui(engine, config, window)
    # …and a second, HTTPS copy of the same page.  A browser only allows the
    # microphone in a secure context, so voice messages need it; the HTTP port
    # stays the quick "scan and go" one.  Failure here is not fatal either.
    tls_ui = start_web_ui(engine, config, window, secure=True)

    window.show()

    def shutdown() -> None:
        for ui in (web_ui, tls_ui):
            if ui is None:
                continue
            try:
                ui.stop()
            except Exception:
                pass
        try:
            engine.stop()
        except Exception:
            pass

    app.aboutToQuit.connect(shutdown)

    code = app.exec()
    shutdown()
    return code


def start_web_ui(engine: Engine, config: EngineConfig, window=None, *, secure: bool = False):
    """Start the browser interface if it is enabled.

    Returns the :class:`~eversend.web.server.WebUI`, or ``None`` when it is
    disabled or could not bind -- neither is fatal for the desktop app.
    """
    if not config.enable_web:
        return None
    try:
        from ..web import create_web_ui

        port = config.web_port + 1 if secure else config.web_port
        ssl_context = None
        if secure:
            from ..core import tls

            ssl_context = tls.ssl_context(config.data_dir)
            if ssl_context is None:
                return None
        ui = create_web_ui(engine, port=port, ssl_context=ssl_context)
        bound = ui.start(port)
        if not secure:
            engine.info.web_port = bound
        if window is not None:
            if secure:
                window.on_web_tls_started(bound)
            else:
                # The window keeps the object, not just the port: it needs to be
                # able to ask who is connected (MainWindow._refresh_web_clients).
                window.on_web_ui_started(bound, ui)
        return ui
    except Exception as exc:
        # Port already in use almost always means a second instance of this app
        # is running, and the message should say so: "address already in use"
        # on its own reads like a bug rather than "the other copy owns it".
        import errno as _errno

        if getattr(exc, "errno", None) == _errno.EADDRINUSE or "in use" in str(exc).lower():
            reason = (
                f"端口 {config.web_port} 已被占用——本机应该还有另一个韧传实例在运行，"
                "手机界面由它提供，不影响本窗口收发文件"
            )
        else:
            reason = str(exc)
        if window is not None:
            window.on_web_ui_failed(reason)
        else:
            print(f"浏览器界面未能启动：{reason}", file=sys.stderr)
        return None


def run_cli(argv: Sequence[str] | None = None) -> int:
    """Headless mode: run the engine and print what happens."""
    from ..cli import main as cli_main

    return cli_main(list(argv if argv is not None else sys.argv[1:]))


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "--cli":
        return run_cli(argv[1:])
    try:
        return run_gui(argv)
    except Exception:  # pragma: no cover - last-resort diagnostics
        traceback.print_exc()
        return 1


__all__ = ["build_config", "main", "portable_root", "run_cli", "run_gui", "writable_dir"]
