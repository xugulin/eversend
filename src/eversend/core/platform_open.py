"""Opening files and folders in the platform's file manager.

Three platforms, three completely different mechanisms, none of them in the
standard library.  Keeping it in one small module means the GUI never has to
care which one it is running on, and a failure here can never break a transfer.
"""

from __future__ import annotations

import os
import subprocess
import sys

from .sockutil import IS_ANDROID, IS_LINUX, IS_MACOS, IS_WINDOWS


def open_path(path: str) -> bool:
    """Open a file or folder with the default application."""
    if not path or not os.path.exists(path):
        return False
    try:
        if IS_WINDOWS:
            os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
            return True
        if IS_MACOS:
            subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if IS_ANDROID:
            subprocess.Popen(
                ["am", "start", "-a", "android.intent.action.VIEW", "-d", f"file://{path}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, AttributeError):
        return False


def reveal_in_file_manager(path: str) -> bool:
    """Show ``path`` selected inside its containing folder where supported."""
    if not path:
        return False
    if not os.path.exists(path):
        # The file may have been moved; fall back to its folder, then to nothing.
        folder = os.path.dirname(path)
        return open_path(folder) if folder and os.path.isdir(folder) else False

    try:
        if IS_WINDOWS:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return True
        if IS_MACOS:
            subprocess.Popen(["open", "-R", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if IS_LINUX:
            # There is no portable "reveal" on Linux, so open the folder and
            # let the user find the file: every file manager behaves differently.
            return open_path(os.path.dirname(path) or path)
    except OSError:
        pass
    return open_path(os.path.dirname(path) or path)


def default_download_dir() -> str:
    """The most sensible place to put received files on this platform."""
    home = os.path.expanduser("~")
    candidates = []
    if IS_WINDOWS:
        candidates = [
            os.path.join(home, "Downloads"),
            os.path.join(os.environ.get("USERPROFILE", home), "Downloads"),
        ]
    else:
        candidates = [
            os.path.join(home, "Downloads"),
            os.path.join(home, "下载"),
        ]
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return home


def is_graphical_session() -> bool:
    """Whether a GUI can plausibly start here.

    Used to give a clear error instead of Qt's rather cryptic one when the
    launcher is run over SSH or from a TTY.
    """
    if IS_WINDOWS or IS_MACOS:
        return True
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return True
    return False


__all__ = [
    "default_download_dir",
    "is_graphical_session",
    "open_path",
    "reveal_in_file_manager",
]
