#!/usr/bin/env python3
"""Fetch a static FFmpeg into the green package (optional, off by default).

Why this is a separate, explicit step
-------------------------------------
FFmpeg is a big dependency: a maintained static build is 100-130 MB per
platform, which would roughly double the portable package (112 MB today).  So
it is **not** part of a normal build.  Run this script when you want the
package to carry its own decoder, and ``build_green.py`` will pick it up.

What it is used for
-------------------
* Video **thumbnails** in the chat: the desktop has no decoder of its own
  (PySide6-Essentials ships no QtMultimedia), so a video bubble shows a poster
  frame extracted with `ffmpeg -ss … -frames:v 1` when this binary is present,
  and a plain card when it is not.
* ``ffplay`` (shipped in the same archive) gives in-window playback for the
  people who want it, without adding a 160 MB Qt Addons wheel.

Licensing: use the **LGPL** builds (BtbN publishes both lgpl and gpl variants).
FFmpeg is invoked as a separate process, so this is not linking, but a
permissively-licensed binary keeps the question from coming up at all.

    python3 tools/fetch_ffmpeg.py --platform linux --out tools/.cache/ffmpeg
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

#: Where to get permissively-licensed builds.  BtbN publishes LGPL variants of
#: both Windows and Linux builds; sizes are honest, not rounded down.
SOURCES = {
    "windows": {
        "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
               "ffmpeg-n7.1-latest-win64-lgpl-7.1.zip",
        "archive": "zip",
        "license": "LGPL-3.0",
        "note": "ffmpeg.exe + ffplay.exe + ffprobe.exe ≈ 250 MB unpacked",
    },
    "linux": {
        "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
               "ffmpeg-n7.1-latest-linux64-lgpl-7.1.tar.xz",
        "archive": "tarxz",
        "license": "LGPL-3.0",
        "note": "ffmpeg + ffplay + ffprobe ≈ 260 MB unpacked",
    },
}

#: Only these three are worth carrying; the rest of the archive is headers,
#: pkg-config files and documentation.
WANTED = ("ffmpeg", "ffmpeg.exe", "ffplay", "ffplay.exe", "ffprobe", "ffprobe.exe")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch FFmpeg for the green package")
    parser.add_argument("--platform", choices=sorted(SOURCES), required=True)
    parser.add_argument("--out", type=Path, default=Path("tools/.cache/ffmpeg"))
    parser.add_argument("--keep-archive", action="store_true")
    args = parser.parse_args(argv)

    spec = SOURCES[args.platform]
    target = args.out / args.platform
    target.mkdir(parents=True, exist_ok=True)
    archive = args.out / os.path.basename(spec["url"].split("?")[0])

    print(f"下载 {spec['url']}")
    print(f"  许可证：{spec['license']}　体积：{spec['note']}")
    if not archive.exists():
        # curl rather than urllib: it resumes, and GitHub's asset hosts drop
        # connections often enough that a resume-capable fetch is the
        # difference between "one command" and "try again all afternoon".
        code = subprocess.call(
            ["curl", "-L", "--fail", "--retry", "5", "--retry-delay", "3",
             "-C", "-", "-o", str(archive), spec["url"]]
        )
        if code != 0:
            print(f"下载失败（curl 退出码 {code}）", file=sys.stderr)
            return code
    else:
        print(f"  用已缓存的 {archive}")

    print(f"解包到 {target}")
    if spec["archive"] == "zip":
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.namelist():
                name = os.path.basename(member)
                if name in WANTED:
                    with bundle.open(member) as src, open(target / name, "wb") as dst:
                        shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(archive, "r:xz") as bundle:
            for member in bundle.getmembers():
                name = os.path.basename(member.name)
                if member.isfile() and name in WANTED:
                    src = bundle.extractfile(member)
                    if src is None:
                        continue
                    with open(target / name, "wb") as dst:
                        shutil.copyfileobj(src, dst)

    if args.platform == "linux":
        for name in WANTED:
            path = target / name
            if path.exists():
                path.chmod(0o755)

    found = sorted(p.name for p in target.iterdir())
    if not found:
        print("解包后什么也没找到，归档结构可能变了", file=sys.stderr)
        return 1
    total = sum((target / name).stat().st_size for name in found)
    print(f"完成：{', '.join(found)}（合计 {total / 1048576:.0f} MB）")
    print("现在可以用 build_green.py --ffmpeg 打包，绿色包会带上它。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
