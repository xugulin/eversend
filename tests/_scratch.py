"""Where tests put their temporary data.

``/tmp`` is commonly a small tmpfs (7.7 GB here) while the project lives on a
large disk, and these tests write multi-gigabyte files on purpose.  Putting the
scratch space next to the project keeps the tmpfs free for everything else --
filling it makes unrelated tools fail in confusing ways.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

#: Root for test scratch space.  Override with ``EVERSEND_TEST_TMP``.
SCRATCH_ROOT = Path(
    os.environ.get("EVERSEND_TEST_TMP")
    or Path(__file__).resolve().parent.parent / ".testscratch"
)


@contextmanager
def scratch(prefix: str = "run-"):
    """A temporary directory on the big disk, always cleaned up."""
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(SCRATCH_ROOT)))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


__all__ = ["SCRATCH_ROOT", "scratch"]


def use_utf8_console() -> None:
    """Make Chinese output survive a legacy Windows console.

    A zh-CN Windows console defaults to code page 936 and an en-US one to
    1252; printing a Chinese test name there raises ``UnicodeEncodeError`` and
    takes the whole script down.  ``errors="replace"`` guarantees the run
    finishes even on a console that simply cannot represent the characters.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def peak_rss_mib() -> float:
    """Peak resident memory of this process, in MiB, on every platform.

    ``resource`` does not exist on Windows, and on macOS ``ru_maxrss`` is in
    *bytes* while Linux reports *kilobytes* -- reading it without accounting
    for that reported a 240 MB peak as "237088 MiB" and failed the bound check
    for a completely bogus reason.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _Counters()
            counters.cb = ctypes.sizeof(counters)
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(  # type: ignore[attr-defined]
                ctypes.windll.kernel32.GetCurrentProcess(),  # type: ignore[attr-defined]
                ctypes.byref(counters),
                counters.cb,
            )
            if ok:
                return counters.PeakWorkingSetSize / 1048576
        except Exception:
            pass
        return 0.0

    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:
        return 0.0
    return usage / 1048576 if sys.platform == "darwin" else usage / 1024


__all__ += ["peak_rss_mib", "use_utf8_console"]
