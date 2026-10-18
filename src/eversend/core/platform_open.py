"""Opening files and folders in the platform's file manager.

Three platforms, three completely different mechanisms, none of them in the
standard library.  Keeping it in one small module means the GUI never has to
care which one it is running on, and a failure here can never break a transfer.

A failure *is* reported, though.  "Start a process and hope" is not a success
signal: ``xdg-open`` exits 3 with "no method available" on a machine that has
no application registered for the file type, and the user who clicked 「打开」
must not be left staring at a window that never appears.  Every opener is
therefore waited on (bounded, see :data:`OPEN_WAIT_SECONDS`) and its exit code
checked, :func:`open_path` returns the real answer, and the reason is available
to whatever wants to show it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from typing import Callable

from .sockutil import IS_ANDROID, IS_LINUX, IS_MACOS, IS_WINDOWS

#: How long to wait for an opener to tell us whether it worked.
#:
#: ``xdg-open``/``open``/``am`` return as soon as they have handed the file to
#: something else, so in practice this costs nothing -- but an opener that stays
#: in the foreground (a terminal editor, a viewer that never detaches) must not
#: freeze the GUI thread, so the wait is bounded and such a process counts as a
#: success: it is still running, which is not evidence of failure.
OPEN_WAIT_SECONDS = 3.0

#: The last failure message, for front ends that only want a reason.
_last_error = ""

_lock = threading.Lock()

#: Where "cannot open this file" goes when no front end installed a handler.
_failure_handler: Callable[[str], None] | None = None


def set_failure_handler(handler: Callable[[str], None] | None) -> None:
    """Install the callback that shows an "opening failed" message to the user.

    A front end should call this once at start-up -- e.g. with something that
    puts the text in its status bar or a message box -- and then the user learns
    *why* nothing happened instead of clicking a dead button.  Without a handler
    the message goes to stderr, which at least covers the CLI/TTY case.
    """
    global _failure_handler
    with _lock:
        _failure_handler = handler


def last_open_error() -> str:
    """Why the last :func:`open_path` failed (``""`` when it succeeded)."""
    with _lock:
        return _last_error


def _clear_error() -> None:
    global _last_error
    with _lock:
        _last_error = ""


def _report_failure(message: str) -> None:
    global _last_error
    with _lock:
        _last_error = message
        handler = _failure_handler
    try:
        if handler is not None:
            handler(message)
        else:
            print(f"[EverSend] {message}", file=sys.stderr, flush=True)
    except Exception:
        # A front end whose notification path is broken must not turn "the file
        # did not open" into a crash in the middle of a UI callback.
        pass


def _failure_message(path: str, reason: str) -> str:
    name = os.path.basename(path) or path
    text = f"打不开「{name}」：{reason}"
    if IS_LINUX and not is_graphical_session():
        text += "（当前没有图形会话：DISPLAY / WAYLAND_DISPLAY 都没有设置）"
    return text


def _run_opener(argv: list[str], path: str) -> tuple[bool, str]:
    """Run an opener and report whether it really accepted the job.

    The exit code is the only signal a program like ``xdg-open`` gives us, so
    the process is waited on instead of being fired and forgotten.
    """
    # A file rather than a pipe: a long-lived opener must not be able to block
    # on a full stderr pipe while we are not reading it.
    with tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=errors)
        except OSError as exc:
            return False, _failure_message(
                path, f"无法运行 {argv[0]}（{exc.strerror or exc}）"
            )
        try:
            code = proc.wait(timeout=OPEN_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            # Still alive: it handed nothing back to us, but it did not fail
            # either -- an editor or viewer that stays attached looks like this.
            return True, ""
        if code == 0:
            return True, ""
        errors.seek(0)
        detail = errors.read().decode("utf-8", "replace").strip()

    reason = f"{argv[0]} 退出码 {code}"
    if detail:
        # The last line is the one that says why ("no method available").
        reason = f"{reason}：{detail.splitlines()[-1][:200]}"
    return False, _failure_message(path, reason)


def _open_with(argv: list[str], path: str) -> bool:
    ok, message = _run_opener(argv, path)
    if not ok:
        _report_failure(message)
    return ok


def open_path(path: str) -> bool:
    """Open a file or folder with the default application.

    Returns ``True`` only when the platform's opener really accepted the job.
    It used to return ``True`` the moment a process existed, so an ``xdg-open``
    that exits 3 ("no method available") was indistinguishable from a window
    that opened off-screen -- and every caller ignored the result anyway.  Call
    :func:`open_path_report` when the user should be told *why* it failed.
    """
    if not path:
        return False
    # A fresh attempt: the previous reason must not be shown for this one.
    _clear_error()
    if not os.path.exists(path):
        # "The file was moved" is one of the clicks that used to do nothing at
        # all, so it gets a reason like any other failure.
        _report_failure(_failure_message(path, "文件已经不在了（可能已被移动或删除）"))
        return False
    try:
        if IS_WINDOWS:
            # os.startfile hands the file to the shell and never reports back,
            # so an exit code does not exist here; only a refusal to launch
            # (no association, missing file) raises, and that is caught below.
            os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
            return True
        if IS_MACOS:
            return _open_with(["open", path], path)
        if IS_ANDROID:
            return _open_with(
                ["am", "start", "-a", "android.intent.action.VIEW", "-d", f"file://{path}"],
                path,
            )
        return _open_with(["xdg-open", path], path)
    except (OSError, AttributeError) as exc:
        _report_failure(_failure_message(path, str(exc)))
        return False


def open_path_report(path: str) -> tuple[bool, str]:
    """Like :func:`open_path`, but hands back a message fit for the user.

    Returns ``(ok, message)``; ``message`` is empty on success.  This is the
    one-liner a front end wants: ``ok, why = open_path_report(path)`` and then
    show ``why`` if ``not ok``.
    """
    ok = open_path(path)
    return ok, ("" if ok else last_open_error())


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
            # explorer.exe exits 1 even when it successfully hands the request
            # to an already-running instance, so its exit code says nothing;
            # only "it would not start at all" is worth reporting.
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return True
        if IS_MACOS:
            return _open_with(["open", "-R", path], path)
        if IS_LINUX:
            # There is no portable "reveal" on Linux, so open the folder and
            # let the user find the file: every file manager behaves differently.
            return open_path(os.path.dirname(path) or path)
    except OSError as exc:
        _report_failure(_failure_message(path, str(exc)))
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
    "OPEN_WAIT_SECONDS",
    "default_download_dir",
    "is_graphical_session",
    "last_open_error",
    "open_path",
    "open_path_report",
    "reveal_in_file_manager",
    "set_failure_handler",
]
