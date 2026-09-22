"""Video/audio helpers that work with *or without* a bundled FFmpeg.

The desktop build deliberately does not ship a media framework: PySide6
Essentials has no QtMultimedia, and Qt Addons would add ~160 MB to a 112 MB
portable package.  So playback is delegated -- but a bare file card is a poor
preview, and the user asked for a real one.  When an ``ffmpeg`` binary is
available (next to the program, in ``data_dir/ffmpeg``, or on PATH) this module
uses it to:

* extract a **poster frame** so a video bubble shows a picture, and
* report a video's **duration**, which the chat shows next to the size.

Without ffmpeg everything degrades to the previous behaviour: a card with a
play button.  Nothing here raises when the binary is missing -- a missing
optional tool must never break a chat.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

#: Extractor output: a small JPEG next to the cache directory.
THUMBNAIL_WIDTH = 480
#: Hard cap: a poster frame should never make the UI wait noticeably.
TIMEOUT_SECONDS = 12.0

_lock = threading.Lock()
_cache: dict[str, str | None] = {}
_resolved: dict[str, str | None] = {}
_resolved_lock = threading.Lock()


def _candidate_names(tool: str) -> list[str]:
    suffix = ".exe" if os.name == "nt" else ""
    return [f"{tool}{suffix}"]


def find_tool(tool: str, data_dir: str = "") -> str | None:
    """Locate ``ffmpeg``/``ffplay``/``ffprobe``, or ``None``.

    Search order matters: the copy shipped *with this program* must win over
    whatever happens to be on PATH, so the portable package behaves the same on
    every machine it is unpacked onto.
    """
    with _resolved_lock:
        if tool in _resolved:
            return _resolved[tool]

    found: str | None = None
    roots: list[Path] = []
    if data_dir:
        roots.append(Path(data_dir) / "ffmpeg")
        roots.append(Path(data_dir))
    # .../app/eversend/core/media.py -> .../  (the unpacked package root)
    package_root = Path(__file__).resolve().parents[3]
    roots.append(package_root / "ffmpeg")
    roots.append(package_root)
    for root in roots:
        for name in _candidate_names(tool):
            candidate = root / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                found = str(candidate)
                break
        if found:
            break
    if found is None:
        found = shutil.which(tool)
    with _resolved_lock:
        _resolved[tool] = found
    return found


def available(data_dir: str = "") -> bool:
    """Whether a bundled/system ffmpeg can be used at all."""
    return find_tool("ffmpeg", data_dir) is not None


def _run(command: list[str]) -> subprocess.CompletedProcess:
    """Run ffmpeg/ffprobe without ever opening a console window on Windows."""
    creation = 0
    if os.name == "nt":  # pragma: no cover - Windows only
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        timeout=TIMEOUT_SECONDS,
        creationflags=creation,
    )


def video_thumbnail(path: str, data_dir: str = "", cache_dir: str = "") -> str | None:
    """A JPEG poster frame for ``path``, or ``None`` when it cannot be made.

    Cached by (path, size, mtime): a chat re-renders often, and re-running
    ffmpeg for every repaint would make the window feel broken.
    """
    if not path or not os.path.isfile(path):
        return None
    ffmpeg = find_tool("ffmpeg", data_dir)
    if not ffmpeg:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = f"{path}|{stat.st_size}|{int(stat.st_mtime)}"
    with _lock:
        if key in _cache:
            return _cache[key]

    target_dir = Path(cache_dir or (Path(data_dir or ".") / "thumbnails"))
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    import hashlib

    digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]
    target = target_dir / f"{digest}.jpg"

    result: str | None = None
    if target.is_file():
        result = str(target)
    else:
        # Seek a little way in: the very first frame of a phone video is often
        # black, which reads as "the preview is broken".
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", "0.5", "-i", path, "-frames:v", "1",
            "-vf", f"scale={THUMBNAIL_WIDTH}:-1",
            str(target),
        ]
        try:
            _run(command)
        except (OSError, subprocess.SubprocessError):
            result = None
        if target.is_file() and target.stat().st_size > 0:
            result = str(target)
        else:
            # Fall back to frame 0 for very short clips.
            command[4] = "0"
            try:
                _run(command)
            except (OSError, subprocess.SubprocessError):
                pass
            if target.is_file() and target.stat().st_size > 0:
                result = str(target)

    with _lock:
        if len(_cache) > 256:
            _cache.clear()
        _cache[key] = result
    return result


def media_duration(path: str, data_dir: str = "") -> float:
    """Length in seconds (0.0 when unknown).  Uses ffprobe when present."""
    if not path or not os.path.isfile(path):
        return 0.0
    probe = find_tool("ffprobe", data_dir)
    if not probe:
        return 0.0
    try:
        result = _run([
            probe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ])
        return max(0.0, float(result.stdout.decode("utf-8", "replace").strip() or 0))
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0


def play_command(path: str, data_dir: str = "") -> list[str] | None:
    """``ffplay`` for in-window playback, or ``None`` when it is not shipped."""
    player = find_tool("ffplay", data_dir)
    if not player:
        return None
    return [player, "-autoexit", "-window_title", os.path.basename(path), path]


def describe(data_dir: str = "") -> str:
    """One line for the UI/logs: what media support this install actually has."""
    ffmpeg = find_tool("ffmpeg", data_dir)
    if not ffmpeg:
        return "未内置 FFmpeg：视频只显示卡片，点「打开」交给系统播放器"
    parts = [os.path.basename(ffmpeg)]
    if find_tool("ffplay", data_dir):
        parts.append("ffplay")
    return f"内置 FFmpeg（{', '.join(parts)}）：视频有缩略图，可窗口内播放"


__all__ = [
    "available",
    "describe",
    "find_tool",
    "media_duration",
    "play_command",
    "video_thumbnail",
]
