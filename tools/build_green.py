#!/usr/bin/env python3
"""Assemble the portable ("green") EverSend tree and zip it.

The output of this script is *the product*: a single ``.zip`` whose first
entry is the folder ``EverSend/``.  A user copies it to a USB stick, extracts
it (Windows Explorer, 7-Zip, ``unzip`` -- anything works, see "portability
rules" below), double-clicks ``run.bat`` / ``run.sh`` and the application runs
with no installer, no registry entry and no ``%APPDATA%`` state.

Layout produced (the launchers depend on exactly this shape)::

    EverSend/
      run.sh              Linux/macOS launcher (executable bit set in the zip)
      run.bat             Windows launcher
      README.txt          first-run help, Chinese, UTF-8 with BOM
      runtime/            embedded CPython (python-build-standalone)
      app/eversend/      our source package (kept inspectable on purpose)
      app/eversend_green.py  entry point
      site/               vendored wheels, site-packages layout
      data/               empty; mutable state at runtime
      received/           empty; default receive folder

Portability rules this script enforces, because the artifact is expected to
land on FAT32/exFAT USB sticks:

* **No symlinks anywhere.**  Neither zip nor FAT32 can represent them
  faithfully, so every symlink is materialised into a real file first.
* **No absolute build paths.**  ``verify_green.py`` greps the whole tree for
  the build directory to prove it.
* **Everything relative to the launcher.**  ``runtime/`` is relocatable by
  construction (python-build-standalone) and the launchers derive every path
  from ``$0`` / ``%~dp0``.
"""

from __future__ import annotations

import argparse
import filecmp
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TOOLS_DIR.parent
SRC_PACKAGE = PROJECT_ROOT / "src" / "eversend"
CONSTANTS_PY = SRC_PACKAGE / "core" / "constants.py"
CACHE_DIR = TOOLS_DIR / ".cache"
DEFAULT_ENTRY = TOOLS_DIR / "green_main.py"

sys.path.insert(0, str(TOOLS_DIR))
import fetch_runtime  # noqa: E402  (same directory, no package needed)

# --------------------------------------------------------------------------
# Launchers
#
# The launchers are generated from templates rather than kept as separate
# files so that the toolchain has a single source of truth for the layout: if
# a directory is renamed here, both launchers follow automatically.
# --------------------------------------------------------------------------

RUN_SH = r'''#!/bin/sh
# ==========================================================================
#  EverSend 韧传 —— 便携版启动器 (Linux / macOS)
#
#  用法:
#      ./run.sh              启动图形界面
#      ./run.sh --cli        命令行模式（无窗口，适合服务器/SSH）
#      ./run.sh --selftest   自检（检查内嵌 Python、Qt、加密库、端口）
#      ./run.sh --help       查看全部参数
#
#  说明:
#    * 本脚本只使用 POSIX sh 语法，不使用任何 bash 专有写法。
#    * 所有路径都相对于 “本脚本自己所在的目录”，与当前工作目录无关，
#      因此解压到 U 盘、挂载点、带空格或中文的目录都能正常运行。
#    * 支持通过符号链接调用本脚本（会自动跟随链接找到真实位置）。
# ==========================================================================

set -u

# --- 1. 找到脚本自身所在目录（关键：不看 $PWD） ---------------------------
SELF="$0"
_tries=0
while [ -L "$SELF" ]; do
    _tries=$((_tries + 1))
    if [ "$_tries" -gt 40 ]; then
        echo "错误：符号链接层级过深，无法确定程序目录。" >&2
        exit 1
    fi
    _link=$(readlink "$SELF" 2>/dev/null) || break
    case "$_link" in
        /*) SELF="$_link" ;;
        *)  SELF="$(dirname "$SELF")/$_link" ;;
    esac
done
APPDIR=$(cd -P "$(dirname "$SELF")" >/dev/null 2>&1 && pwd) || {
    echo "错误：无法确定程序所在目录。" >&2
    exit 1
}
cd "$APPDIR" || { echo "错误：无法进入目录 $APPDIR" >&2; exit 1; }

# --- 2. 判断运行模式 ------------------------------------------------------
NEED_DISPLAY=1
for _arg in "$@"; do
    case "$_arg" in
        --cli|--selftest|--selftest-transfer|--print-paths|--version|-h|--help) NEED_DISPLAY=0 ;;
    esac
done

if [ "$NEED_DISPLAY" = "1" ] && [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    echo "错误：没有检测到图形界面（DISPLAY 与 WAYLAND_DISPLAY 都没有设置）。" >&2
    echo "      EverSend 的窗口需要图形会话。可以：" >&2
    echo "        1) 在桌面环境（X11 或 Wayland）里双击/运行本程序；" >&2
    echo "        2) 使用命令行模式：  ./run.sh --cli" >&2
    echo "        3) 只做自检：        ./run.sh --selftest" >&2
    exit 3
fi

# --- 3. 定位内嵌解释器 ----------------------------------------------------
RUNTIME="$APPDIR/runtime"
PY=""
for _cand in "$RUNTIME/bin/python3" "$RUNTIME/bin/python3.14" "$RUNTIME/bin/python"; do
    if [ -x "$_cand" ]; then PY="$_cand"; break; fi
done
if [ -z "$PY" ]; then
    echo "错误：找不到内嵌 Python 解释器（$RUNTIME/bin/python3）。" >&2
    echo "      压缩包可能没有完整解压，请重新解压全部文件后重试。" >&2
    exit 1
fi

# --- 4. 可写性探测：U 盘写保护 / 只读挂载也能启动 -------------------------
probe_write() {
    # 真正写一个文件来判断。-w 在只读介质上并不可靠，只有写成功才算数。
    _dir="$1"
    [ -d "$_dir" ] || mkdir -p "$_dir" 2>/dev/null || return 1
    _t="$_dir/.eversend-write-test.$$"
    ( umask 077; : > "$_t" ) 2>/dev/null || return 1
    rm -f "$_t" 2>/dev/null
    return 0
}

READONLY=0
if [ -n "${EVERSEND_DATA_DIR:-}" ]; then
    DATA="$EVERSEND_DATA_DIR"
    mkdir -p "$DATA" 2>/dev/null || true
else
    DATA="$APPDIR/data"
    if ! probe_write "$DATA"; then
        READONLY=1
        DATA="${TMPDIR:-/tmp}/eversend-$(id -u 2>/dev/null || echo 0)"
        if ! mkdir -p "$DATA" 2>/dev/null; then
            echo "错误：程序目录不可写，且无法创建临时数据目录 $DATA。" >&2
            exit 1
        fi
        echo "提示：程序所在位置不可写（只读介质或写保护）。"
        echo "      数据（配置、身份、日志）改为保存在：$DATA"
        echo "      接收到的文件仍会保存到：$( [ -w "$APPDIR" ] && echo "$APPDIR/received" || echo "$DATA/received" )"
    fi
fi

if [ -n "${EVERSEND_RECEIVE_DIR:-}" ]; then
    RECV="$EVERSEND_RECEIVE_DIR"
elif [ "$READONLY" = "1" ]; then
    RECV="$DATA/received"
else
    RECV="$APPDIR/received"
fi
mkdir -p "$RECV" 2>/dev/null || true
mkdir -p "$DATA/logs" 2>/dev/null || true

# --- 5. 环境变量：只指向本目录内部，绝不泄漏到系统 ------------------------
export PYTHONHOME="$RUNTIME"
export PYTHONPATH="$APPDIR/app:$APPDIR/site"
export PYTHONDONTWRITEBYTECODE=1     # 只读介质上绝不尝试写 .pyc
export PYTHONUTF8=1                  # 中文/非 ASCII 路径
export PYTHONNOUSERSITE=1            # 不加载用户级 site-packages
export PYTHONHASHSEED=0
# 关键：--cli 模式下的启动信息立刻输出。Python 在“输出到管道/文件”时默认块缓冲，
# 没有这一行，`./run.sh --cli | tee log`、systemd、docker logs 都会长时间看不到
# 任何输出（看起来像卡住了）。
export PYTHONUNBUFFERED=1
# Qt 的目录在两种 wheel 布局里不一样：
#   Linux/macOS 的 PySide6 wheel : site/PySide6/Qt/{lib,plugins}
#   Windows   的 PySide6 wheel : site/PySide6/{lib(隐式),plugins}
# 所以两个都探测，并且只有在目录确实存在时才覆盖 Qt 的查找路径 —— 把一个
# 不存在的目录塞给 QT_QPA_PLATFORM_PLUGIN_PATH 会让 Qt 直接启动失败。
QTLIB="$APPDIR/site/PySide6/Qt/lib"
[ -d "$QTLIB" ] || QTLIB="$APPDIR/site/PySide6"
QTPLUGINS="$APPDIR/site/PySide6/Qt/plugins"
[ -d "$QTPLUGINS" ] || QTPLUGINS="$APPDIR/site/PySide6/plugins"
export LD_LIBRARY_PATH="$RUNTIME/lib:$QTLIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export DYLD_LIBRARY_PATH="$RUNTIME/lib:$QTLIB${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
if [ -d "$QTPLUGINS/platforms" ]; then
    export QT_PLUGIN_PATH="$QTPLUGINS"
    export QT_QPA_PLATFORM_PLUGIN_PATH="$QTPLUGINS/platforms"
fi
# 不硬编码 xcb：让 Qt 自己选择 xcb / wayland，用户也可以用 QT_QPA_PLATFORM 覆盖。
# EVERSEND_HOME 告诉程序“这个绿色包的家在哪”。应用的 CLI（eversend.cli）
# 和桌面入口（eversend.desktop.app）都读它；不设置的话它们会退回到“当前目录”，
# 那样从别的目录启动就会写错地方。只读介质上它指向临时目录。
if [ "$READONLY" = "1" ]; then
    export EVERSEND_HOME="$DATA"
else
    export EVERSEND_HOME="$APPDIR"
fi
# XDG_* 指向本程序的数据目录，这样 fontconfig / Qt / GL 的缓存不会写到 ~/.cache
# 或 ~/.config —— “纯绿色”意味着用户的家目录里一个字节都不该多出来。
export XDG_CONFIG_HOME="$DATA/xdg-config"
export XDG_CACHE_HOME="$DATA/xdg-cache"
export XDG_DATA_HOME="$DATA/xdg-data"
export EVERSEND_APP_HOME="$APPDIR"
export EVERSEND_DATA_DIR="$DATA"
export EVERSEND_RECEIVE_DIR="$RECV"
export EVERSEND_READONLY="$READONLY"
export EVERSEND_LOG="$DATA/logs/eversend.log"

# --- 6. 启动 --------------------------------------------------------------
# -s: 忽略用户 site-packages；-B: 不写字节码。
exec "$PY" -s -B "$APPDIR/app/eversend_green.py" "$@"
'''

RUN_BAT = r'''@echo off
rem ==========================================================================
rem  EverSend 韧传 —— 便携版启动器 (Windows)
rem
rem  用法（双击即为图形界面）:
rem      run.bat               启动图形界面
rem      run.bat --cli         命令行模式（保留控制台窗口）
rem      run.bat --selftest    自检
rem      run.bat --help        查看全部参数
rem
rem  说明:
rem    * 全部路径都相对本文件所在目录（%~dp0），与“当前目录”无关，
rem      所以解压到 U 盘、桌面、含空格或中文的目录都能运行。
rem    * 图形界面用 pythonw.exe 启动，不会留下黑色控制台窗口；
rem      出错时日志写在 data\logs\launch.log，并弹出提示框。
rem ==========================================================================
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1

set "APPDIR=%~dp0"
if "%APPDIR:~-1%"=="\" set "APPDIR=%APPDIR:~0,-1%"
cd /d "%APPDIR%" 2>nul
if errorlevel 1 (
    echo 错误：无法进入程序目录 "%APPDIR%"
    pause
    exit /b 1
)

rem --- 运行模式 -------------------------------------------------------------
set "ARGS=%*"
set "MODE=gui"
:parse_args
if "%~1"=="" goto :args_done
if /I "%~1"=="--cli"          set "MODE=cli"
if /I "%~1"=="--selftest"     set "MODE=cli"
if /I "%~1"=="--print-paths"  set "MODE=cli"
if /I "%~1"=="--version"      set "MODE=cli"
if /I "%~1"=="-h"             set "MODE=cli"
if /I "%~1"=="--help"         set "MODE=cli"
shift
goto :parse_args
:args_done

rem --- 定位内嵌解释器 -------------------------------------------------------
set "RUNTIME=%APPDIR%\runtime"
set "PY=%RUNTIME%\python.exe"
set "PYW=%RUNTIME%\pythonw.exe"
if not exist "%PY%" (
    echo 错误：找不到内嵌解释器 "%PY%"。
    echo       压缩包可能没有完整解压，请重新解压全部文件后重试。
    pause
    exit /b 1
)
if not exist "%PYW%" set "PYW=%PY%"

rem --- 可写性探测：写保护 U 盘也能启动 --------------------------------------
set "READONLY=0"
set "DATA=%APPDIR%\data"
if defined EVERSEND_DATA_DIR (
    set "DATA=%EVERSEND_DATA_DIR%"
) else (
    mkdir "%DATA%" 2>nul
    echo. > "%DATA%\.eversend-write-test" 2>nul
    if not exist "%DATA%\.eversend-write-test" (
        set "READONLY=1"
        set "DATA=%TEMP%\eversend"
        mkdir "!DATA!" 2>nul
        echo 提示：程序所在位置不可写（只读介质或写保护）。
        echo       数据改为保存在：!DATA!
    ) else (
        del "%DATA%\.eversend-write-test" >nul 2>&1
    )
)
if defined EVERSEND_RECEIVE_DIR (
    set "RECV=%EVERSEND_RECEIVE_DIR%"
) else (
    if "!READONLY!"=="1" (set "RECV=!DATA!\received") else (set "RECV=%APPDIR%\received")
)
mkdir "!DATA!" 2>nul
mkdir "!RECV!" 2>nul
mkdir "!DATA!\logs" 2>nul

rem --- 环境变量：只指向本目录内部 -------------------------------------------
set "PYTHONHOME=%RUNTIME%"
set "PYTHONPATH=%APPDIR%\app;%APPDIR%\site"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
set "PYTHONHASHSEED=0"
rem 关掉输出缓冲：日志重定向到文件/管道时也能立刻看到内容。
set "PYTHONUNBUFFERED=1"
rem Qt 目录在两种 wheel 布局里不同（Windows wheel 是 site\PySide6\plugins，
rem Linux wheel 是 site\PySide6\Qt\plugins），所以先探测再设置；只有目录真的
rem 存在时才覆盖，否则会把 Qt 指向一个不存在的路径导致窗口无法创建。
set "QTPLUGINS=%APPDIR%\site\PySide6\Qt\plugins"
if not exist "%QTPLUGINS%\platforms" set "QTPLUGINS=%APPDIR%\site\PySide6\plugins"
if exist "%QTPLUGINS%\platforms" (
    set "QT_PLUGIN_PATH=%QTPLUGINS%"
    set "QT_QPA_PLATFORM_PLUGIN_PATH=%QTPLUGINS%\platforms"
)
rem 让 Qt 插件能找到同目录下的 Qt6*.dll（PySide6 自己也会加，这里提前加上更稳）。
set "PATH=%APPDIR%\site\PySide6;%RUNTIME%;%PATH%"
set "EVERSEND_HOME=%APPDIR%"
if "!READONLY!"=="1" set "EVERSEND_HOME=!DATA!"
rem XDG_* 指向本程序的数据目录，避免把字体/Qt 缓存写进 %APPDATA% 或用户家目录。
set "XDG_CONFIG_HOME=!DATA!\xdg-config"
set "XDG_CACHE_HOME=!DATA!\xdg-cache"
set "XDG_DATA_HOME=!DATA!\xdg-data"
set "EVERSEND_APP_HOME=%APPDIR%"
set "EVERSEND_DATA_DIR=!DATA!"
set "EVERSEND_RECEIVE_DIR=!RECV!"
set "EVERSEND_READONLY=!READONLY!"
set "EVERSEND_LOG=!DATA!\logs\eversend.log"

set "ENTRY=%APPDIR%\app\eversend_green.py"
if not exist "%ENTRY%" (
    echo 错误：找不到程序入口 "%ENTRY%"。
    pause
    exit /b 1
)

rem --- 启动前的快速自检：内嵌 Python 至少能起来 ------------------------------
"%PY%" -s -B -c "pass" >nul 2>&1
if errorlevel 1 (
    echo 错误：内嵌 Python 无法启动。
    echo       请确认整个文件夹已完整解压（不要直接在压缩包里运行）。
    "%PY%" -s -B -c "import sys; print(sys.version)" 2>&1
    pause
    exit /b 1
)

if /I "%MODE%"=="cli" (
    rem 命令行模式：保留控制台，输出直接可见。
    "%PY%" -s -B "%ENTRY%" %ARGS%
    exit /b %ERRORLEVEL%
)

rem 图形界面模式：pythonw.exe 没有控制台窗口，因此不会闪烁；
rem 失败时由程序写日志并用 MessageBox 弹窗提示，错误不会被藏起来。
start "" "%PYW%" -s -B "%ENTRY%" %ARGS%
exit /b 0
'''

README_TXT = """EverSend 韧传 {version} —— 便携版（绿色版）使用说明
================================================================

一、这是什么
    这是一个“解压即用”的绿色软件包：里面已经内置了 Python 运行环境和
    所有依赖库。你不需要安装 Python、不需要 pip install、不会写入注册表，
    也不会在 C:\\Users 或 /home 里留下任何东西。
    整个程序（包括配置、身份密钥、日志、接收到的文件）都保存在这个文件夹
    里面。把它解压到 U 盘、移动硬盘或任意目录都能直接运行。

二、怎么启动
    Windows :  双击  run.bat
    Linux   :  双击或在终端里执行  ./run.sh
    macOS   :  在终端里执行  ./run.sh

    命令行模式（没有图形界面时使用，例如 SSH、服务器）：
        Windows:  run.bat --cli
        Linux  :  ./run.sh --cli

    自检（检查内嵌 Python、Qt、加密库、端口是否正常）：
        Windows:  run.bat --selftest
        Linux  :  ./run.sh --selftest

三、目录说明
    run.bat / run.sh   启动器
    runtime/           内置的 Python 解释器（不要删除）
    app/               程序源码（可以查看，也可以自己改）
    site/              依赖库（PySide6 / cryptography 等，不要删除）
    data/              运行时产生的配置、身份密钥、日志
    received/          默认的文件接收目录

四、常见问题
    1) “找不到内嵌解释器 / 内嵌 Python 无法启动”
       说明压缩包没有完整解压。请把整个 EverSend 文件夹完整解压出来再运行，
       不要直接在压缩包预览窗口里双击运行。
    2) Linux 下提示“没有检测到图形界面”
       当前会话没有 DISPLAY / WAYLAND_DISPLAY，请在桌面环境里运行，
       或者改用命令行模式：./run.sh --cli
    3) U 盘写保护 / 只读介质
       程序会自动把数据目录切换到系统临时目录（Linux: ${{TMPDIR:-/tmp}}/eversend-<uid>，
       Windows: %TEMP%\\eversend），并在启动时提示。程序仍然可以正常收发文件。
    4) 想换一个接收目录
       设置环境变量 EVERSEND_RECEIVE_DIR 指向你要的目录即可。
    5) 首次运行被防火墙拦截
       本程序需要监听本地网络端口用于局域网互传，请选择“允许”。

五、安全提示
    本程序只在局域网内传输文件，传输内容使用端到端加密（Ed25519 身份 +
    X25519 密钥交换 + ChaCha20-Poly1305）。请只与你信任的设备互相发送文件。

版本：{version}    平台：{platform}    构建时间：{build_time}
"""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


@dataclass
class Step:
    """One reported build step (name, before bytes, after bytes)."""

    name: str
    before: int
    after: int
    note: str = ""


def tree_size(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
            elif path.is_symlink():
                total += path.lstat().st_size
        except OSError:
            pass
    return total


def directory_breakdown(root: Path) -> list[tuple[str, int, int]]:
    """Return ``(name, bytes, files)`` for each entry directly under ``root``."""
    rows: list[tuple[str, int, int]] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if child.is_dir() and not child.is_symlink():
            size = tree_size(child)
            files = sum(1 for p in child.rglob("*") if p.is_file())
            rows.append((child.name + "/", size, files))
        elif child.is_file():
            rows.append((child.name, child.stat().st_size, 1))
    return rows


def human(num: float) -> str:
    return fetch_runtime.human_size(num)


def rmtree_force(path: Path) -> None:
    """Remove a tree even if it contains read-only files (from a chmod test)."""
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), reverse=True):
        try:
            if child.is_dir() and not child.is_symlink():
                child.chmod(0o755)
            else:
                child.chmod(0o644)
        except OSError:
            pass
    try:
        path.chmod(0o755)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def read_app_version() -> str:
    """Read ``APP_VERSION`` straight out of the source, so the zip name matches the app."""
    if not CONSTANTS_PY.exists():
        return "0.0.0"
    match = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', CONSTANTS_PY.read_text("utf-8"), re.M)
    return match.group(1) if match else "0.0.0"


# --------------------------------------------------------------------------
# Copy steps
# --------------------------------------------------------------------------

EXCLUDE_DIR_NAMES = {"__pycache__", "tests", "test", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".pyd.tmp"}


def copy_app_package(source: Path, destination: Path) -> int:
    """Copy ``src/eversend`` into ``app/eversend`` without build junk."""
    if destination.exists():
        rmtree_force(destination)
    copied = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in EXCLUDE_DIR_NAMES for part in relative.parts[:-1]):
            continue
        if path.is_dir():
            if path.name in EXCLUDE_DIR_NAMES:
                continue
            (destination / relative).mkdir(parents=True, exist_ok=True)
            continue
        if path.suffix in EXCLUDE_SUFFIXES or path.name.endswith(".pyc"):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    return copied


#: Development-only symlinks inside the embedded runtime.  Their targets ship
#: under their real names, so dropping the link costs nothing -- and copying
#: them instead would add two more 32 MiB duplicates to the archive
#: (``bin/python`` -> ``bin/python3.14`` and ``lib/libpython3.14.so`` ->
#: ``libpython3.14.so.1.0``), because a zip cannot store symlinks in a way that
#: survives extraction on FAT32/exFAT.
RUNTIME_DROP_LINKS = frozenset(
    {
        "runtime/bin/python",
        "runtime/bin/idle3",
        "runtime/bin/pydoc3",
        "runtime/bin/python3-config",
        "runtime/bin/python3.14-config",
        "runtime/lib/libpython3.14.so",
        "runtime/lib/pkgconfig/python3.pc",
        "runtime/lib/pkgconfig/python3-embed.pc",
        "runtime/share/man/man1/python3.1",
    }
)


def materialize_symlinks(root: Path) -> tuple[int, int, int]:
    """Replace every symlink under ``root`` with a real file/directory copy.

    Returns ``(materialised, dropped, extra_bytes)``.  This is what makes the
    tree safe on FAT32/exFAT and inside a zip: both lose the difference between
    a symlink and a small text file, which would turn ``bin/python3`` into a
    10-byte "program" containing the word ``python3.14``.

    Symlinks listed in :data:`RUNTIME_DROP_LINKS` are removed instead of
    copied: they are development conveniences whose target is still present
    under its real name, and materialising them would duplicate 32 MiB blobs.
    ``runtime/bin/python3`` itself -- the path the launcher tries first -- is
    deliberately materialised.
    """
    materialised = 0
    dropped = 0
    extra = 0
    links = [p for p in root.rglob("*") if p.is_symlink()]
    # Deepest first so linked directories are resolved before their parents.
    for link in sorted(links, key=lambda p: len(p.parts), reverse=True):
        relative = link.relative_to(root).as_posix()
        try:
            if relative in RUNTIME_DROP_LINKS:
                link.unlink()
                dropped += 1
                continue
            if link.is_dir():
                source = link.resolve()
                if not source.is_dir():
                    link.unlink()
                    dropped += 1
                    continue
                staging = link.with_name(link.name + ".__link_tmp__")
                shutil.copytree(source, staging, symlinks=False)
                link.unlink()
                staging.rename(link)
                extra += tree_size(link)
            else:
                source = link.resolve()
                if not source.is_file():
                    link.unlink()
                    dropped += 1
                    continue
                data = source.read_bytes()
                link.unlink()
                link.write_bytes(data)
                extra += len(data)
            materialised += 1
        except OSError as exc:
            print(f"    ! could not materialise {link}: {exc}")
    return materialised, dropped, extra


# --------------------------------------------------------------------------
# Bytecode compilation
# --------------------------------------------------------------------------


def compile_bytecode(python: Path, tree: Path, targets: Sequence[str], *, label: str) -> bool:
    """Pre-compile ``targets`` to ``.pyc`` with the given interpreter.

    ``-b`` writes ``foo.pyc`` *next to* ``foo.py`` (legacy layout) and
    ``--invalidation-mode unchecked-hash`` makes the ``.pyc`` valid no matter
    what mtime the files get after being zipped and unzipped on a USB stick --
    with the default timestamp invalidation every extracted file would look
    stale and Python would try to rewrite the cache (impossible on a read-only
    medium, so it would silently re-parse the sources on every start).
    """
    command = [
        str(python),
        "-s",
        "-B",
        "-m",
        "compileall",
        "-q",
        "-b",
        "--invalidation-mode",
        "unchecked-hash",
        "-x",
        # Skip VCS/cache dirs and PySide6's Jinja-style ``*.tmpl.py`` templates
        # (site/PySide6/scripts/deploy_lib/**): they are not Python modules and
        # make compileall exit non-zero, which would look like a build failure.
        r"(^|/)(\.git|__pycache__)(/|$)|\.tmpl\.py$",
        *targets,
    ]
    print(f"    $ {' '.join(command[1:])}")
    result = subprocess.run(command, cwd=str(tree), capture_output=True, text=True)
    if result.stdout.strip():
        print("      " + result.stdout.strip().replace("\n", "\n      "))
    if result.returncode != 0:
        print(f"    ! compileall failed ({label}, rc={result.returncode})")
        if result.stderr.strip():
            print("      " + result.stderr.strip().replace("\n", "\n      "))
        return False
    return True


def count_bytecode(root: Path) -> tuple[int, int]:
    pyc = sum(1 for _ in root.rglob("*.pyc"))
    return pyc, tree_size(root)


def drop_sources(root: Path, keep: Iterable[Path] = ()) -> tuple[int, int]:
    """Delete ``.py`` files that have a sibling ``.pyc``.  Returns ``(count, bytes)``."""
    keep_set = {p.resolve() for p in keep}
    removed = 0
    freed = 0
    for path in sorted(root.rglob("*.py")):
        if path.resolve() in keep_set:
            continue
        if not path.with_suffix(".pyc").exists():
            continue  # never delete a source we could not compile
        size = path.stat().st_size
        path.unlink()
        removed += 1
        freed += size
    return removed, freed


# --------------------------------------------------------------------------
# Optional Qt payload pruning
# --------------------------------------------------------------------------

#: Files that must survive any pruning, with the reason.  Kept as data so the
#: rule is auditable and so `--strip` can refuse to remove them even if a
#: pattern would match.
#: Both wheel layouts are listed: Linux/macOS wheels put the Qt payload under
#: ``PySide6/Qt/...`` while Windows wheels put it directly under ``PySide6/``.
STRIP_PROTECTED = (
    # platform plugins: qwindows.dll / libqxcb.so / libqwayland*.so -- without
    # one of these Qt cannot open a window at all, on any platform.
    "PySide6/Qt/plugins/platforms/",
    "PySide6/plugins/platforms/",
    "PySide6/Qt/plugins/xcbglintegrations/",  # GL integration for the xcb plugin
    "PySide6/plugins/xcbglintegrations/",
    "PySide6/Qt/plugins/imageformats/",  # png/jpeg/svg icons
    "PySide6/plugins/imageformats/",
    "PySide6/Qt/plugins/iconengines/",
    "PySide6/plugins/iconengines/",
    "PySide6/Qt/plugins/styles/",
    "PySide6/plugins/styles/",
    "PySide6/Qt/plugins/platformthemes/",  # native file dialog look
    "PySide6/plugins/platformthemes/",
    "PySide6/Qt/plugins/networkinformation/",  # QtNetwork enumerates interfaces for discovery
    "PySide6/plugins/networkinformation/",
    "PySide6/Qt/plugins/tls/",
    "PySide6/plugins/tls/",
    "PySide6/Qt/plugins/generic/",
    "PySide6/plugins/generic/",
    "PySide6/Qt/plugins/platforminputcontexts/",
    "PySide6/plugins/platforminputcontexts/",
    # core libraries, in both spellings
    "PySide6/Qt/lib/libQt6Core",
    "PySide6/Qt/lib/libQt6Gui",
    "PySide6/Qt/lib/libQt6Widgets",
    "PySide6/Qt/lib/libQt6Network",
    "PySide6/Qt/lib/libQt6Svg",
    "PySide6/Qt/lib/libQt6DBus",
    "PySide6/Qt/lib/libQt6PrintSupport",
    "PySide6/Qt/lib/libEGL",
    "PySide6/Qt/lib/libGL",
    "PySide6/Qt/lib/libxcb",
    "PySide6/Qt/lib/libicu",
    "PySide6/Qt/lib/libQt6XcbQpa",
    "PySide6/Qt6Core.",
    "PySide6/Qt6Gui.",
    "PySide6/Qt6Widgets.",
    "PySide6/Qt6Network.",
    "PySide6/Qt6Svg.",
    "PySide6/Qt6DBus.",
    "PySide6/Qt6PrintSupport.",
    "PySide6/libEGL",
    "PySide6/libGLESv2",
    "PySide6/libicu",
    "PySide6/opengl32sw",
    "PySide6/d3dcompiler",
    "PySide6/pyside6.abi3",
    "PySide6/shiboken6.",
    "PySide6/msvcp140",
    "PySide6/concrt140",
    "PySide6/vcruntime140",
)


def strip_candidates(tree: Path, platform: str) -> list[tuple[Path, str]]:
    """Return the ``(path, reason)`` list for ``--strip``.

    Every rule below was derived from the *measured* contents of the wheels
    (see docs/PACKAGING.md for the numbers) and is deliberately conservative:
    nothing here is reachable from a QtWidgets application.  The build re-runs
    an import plus an offscreen window creation afterwards and fails if the GUI
    stopped working, so this list cannot silently break the artifact.

    Nothing in :data:`STRIP_PROTECTED` is ever removed, whatever a pattern
    says: the platform plugins (``qwindows.dll``, ``libqxcb.so``,
    ``libqwayland*.so``), the GL integration, image format plugins and the core
    Qt libraries are what make a window appear at all.
    """
    plan: list[tuple[Path, str]] = []
    site = tree / "site"
    if not site.exists():
        return plan

    def protected(path: Path) -> bool:
        rel = path.relative_to(tree).as_posix()
        return any(rel.startswith(prefix) for prefix in STRIP_PROTECTED)

    def add(path: Path, reason: str) -> None:
        if path.exists() and not protected(path):
            plan.append((path, reason))

    # ---- 1. type stubs ---------------------------------------------------
    for path in list(site.rglob("*.pyi")):
        add(path, "type stub (.pyi): used by IDEs, never loaded at runtime")

    qml_plugin_dir = site / "PySide6" / "Qt" / "plugins"
    if not qml_plugin_dir.is_dir():
        qml_plugin_dir = site / "PySide6" / "plugins"

    # ---- 2. the whole QML / Qt Quick stack -------------------------------
    # A QtWidgets UI never loads any of it; it is ~30% of the Qt payload.
    for relative in ("PySide6/Qt/qml", "PySide6/Qt/metatypes", "PySide6/Qt/libexec"):
        add(site / relative, "QML/Quick runtime data (widgets UI does not use it)")
    if qml_plugin_dir.is_dir():
        for name in ("qmltooling", "qmllint", "scenegraph"):
            add(qml_plugin_dir / name, "QML/Quick plugin")

    # Two wheel layouts: Linux/macOS use PySide6/Qt/lib + PySide6/Qt/plugins,
    # Windows puts the DLLs and plugins directly under PySide6/.
    lib_dir = site / "PySide6" / "Qt" / "lib"
    if not lib_dir.is_dir():
        lib_dir = site / "PySide6"
    if lib_dir.is_dir():
        for path in lib_dir.iterdir():
            name = path.name
            if name.startswith("libicudata") or name.startswith("icu"):
                continue
            # libQt6Quick.so.6 on Linux, Qt6Quick.dll on Windows
            if re.match(
                r"^(lib)?Qt6(Qml|Quick|Labs|Lottie|ShaderTools|EglFS|WlShell|WaylandEgl|QmlCompiler)",
                name,
            ):
                add(path, "QML/Quick library")
    for pattern in (
        "QtQuick*.abi3.so",
        "QtQml*.abi3.so",
        "Qt3D*.abi3.so",
        "QtCharts*.abi3.so",
        "QtGraphs*.abi3.so",
        "QtDataVisualization*.abi3.so",
        "qmlls*",
        "qmlformat*",
        "qmllint*",
        "qmltyperegistrar*",
        "qmlcachegen*",
        "qsb*",
    ):
        for path in site.glob(f"PySide6/{pattern}"):
            add(path, "QML/Quick tool or module")

    # ---- 3. developer tools shipped inside PySide6 ------------------------
    # Qt Designer / Linguist / Assistant / lupdate and the SQL driver plugins
    # are applications of their own; EverSend is a file transfer tool.
    for pattern in (
        "assistant*",
        "designer*",
        "linguist*",
        "lupdate*",
        "lrelease*",
        "pyside6-*",
        "pyside6*.exe",
        "shiboken6-*",
        "QtDesigner*.abi3.so",
        "QtDesigner*.pyd",
        "QtHelp*.abi3.so",
        "QtUiTools*.abi3.so",
        "QtSql*.abi3.so",
        "QtTest*.abi3.so",
    ):
        for path in site.glob(f"PySide6/{pattern}"):
            add(path, "Qt developer tool (ships with the wheel, unused by the app)")
    for stem in ("Designer", "DesignerComponents", "Help", "UiTools", "Sql", "Test", "Charts"):
        for pattern in (f"libQt6{stem}*", f"Qt6{stem}*.dll", f"Qt6{stem}*.pyd"):
            for path in lib_dir.glob(pattern):
                add(path, "unused Qt subsystem")
    if qml_plugin_dir.is_dir():
        for name in ("designer", "sqldrivers", "help"):
            add(qml_plugin_dir / name, "plugin for a Qt tool the app never starts")

    # ---- 4. Qt translations: keep Chinese, drop the other ~40 locales -----
    translations = site / "PySide6" / "Qt" / "translations"
    if not translations.is_dir():
        translations = site / "PySide6" / "translations"
    if translations.is_dir():
        for path in translations.iterdir():
            if path.name.endswith("_zh_CN.qm") or path.name.endswith("_zh_TW.qm"):
                continue
            add(path, "Qt translation for a locale we do not ship (kept zh_CN/zh_TW)")

    # ---- 5. runtime pieces a GUI application never touches ----------------
    runtime = tree / "runtime"
    if runtime.exists():
        for relative, reason in (
            ("include", "C headers for building extensions"),
            ("lib/pkgconfig", "pkg-config metadata for building extensions"),
            ("share/man", "man pages"),
            ("share/terminfo", "terminal database (curses/Tcl only)"),
            ("lib/tcl9", "Tcl runtime (tkinter only)"),
            ("lib/tcl9.0", "Tcl runtime (tkinter only)"),
            ("lib/tk9.0", "Tk runtime (tkinter only)"),
            ("lib/itcl4.3.8", "Tcl runtime (tkinter only)"),
            ("lib/thread3.0.6", "Tcl runtime (tkinter only)"),
        ):
            add(runtime / relative, reason)
        for pattern in ("bin/pip*", "bin/idle3*", "bin/pydoc3*", "bin/*-config"):
            for path in runtime.glob(pattern):
                add(path, "Python development tool (pip/idle/pydoc/build config)")

    seen: set[Path] = set()
    unique: list[tuple[Path, str]] = []
    for path, reason in plan:
        if path in seen:
            continue
        seen.add(path)
        unique.append((path, reason))
    return unique


def apply_strip(tree: Path, platform: str) -> tuple[int, list[tuple[str, int, str]]]:
    """Apply :func:`strip_candidates`; returns ``(freed_bytes, per-item report)``."""
    report: list[tuple[str, int, str]] = []
    freed = 0
    for path, reason in strip_candidates(tree, platform):
        size = tree_size(path) if path.is_dir() else path.stat().st_size
        rel = path.relative_to(tree).as_posix()
        if path.is_dir():
            rmtree_force(path)
        else:
            try:
                path.unlink()
            except OSError:
                continue
        freed += size
        report.append((rel, size, reason))
    return freed, report


# --------------------------------------------------------------------------
# Zip
# --------------------------------------------------------------------------


def make_zip(tree: Path, destination: Path, *, compress_level: int = 6) -> tuple[int, int]:
    """Zip ``tree`` with the tree's own name as the single top level entry.

    The executable bit travels in ``external_attr`` (the high 16 bits are the
    Unix mode) -- without that, ``run.sh`` comes out of the zip mode 0644 and
    a double-click silently does nothing.

    The archive is built as ``<name>.part`` and renamed into place at the end:
    a reader (or a second build started from another terminal) then either sees
    the previous complete artifact or the new complete one, never a half-written
    file that "is not a zip file".
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")
    part.unlink(missing_ok=True)
    members = 0
    started = time.monotonic()
    with zipfile.ZipFile(
        part, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=compress_level, allowZip64=True
    ) as archive:
        for path in sorted(tree.rglob("*"), key=lambda p: p.as_posix()):
            arcname = f"{tree.name}/{path.relative_to(tree).as_posix()}"
            info = path.lstat()
            if path.is_dir():
                entry = zipfile.ZipInfo(arcname + "/", date_time=time.localtime(info.st_mtime)[:6])
                entry.external_attr = ((stat.S_IFDIR | (info.st_mode & 0o777)) & 0xFFFF) << 16 | 0x10
                entry.compress_type = zipfile.ZIP_STORED
                archive.writestr(entry, b"")
                members += 1
                continue
            entry = zipfile.ZipInfo(arcname, date_time=time.localtime(info.st_mtime)[:6])
            entry.external_attr = ((stat.S_IFREG | (info.st_mode & 0o777)) & 0xFFFF) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.compress_level = compress_level
            entry.file_size = info.st_size
            with path.open("rb") as source, archive.open(entry, "w") as sink:
                shutil.copyfileobj(source, sink, 1 << 20)
            members += 1
    part.replace(destination)
    return members, int(time.monotonic() - started)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_green.py",
        description="Assemble the portable EverSend tree and produce the release zip.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--platform", choices=sorted(fetch_runtime.PLATFORMS), default="linux")
    parser.add_argument("--out", type=Path, default=None, help="output directory (default tools/.build)")
    parser.add_argument("--runtime-dir", type=Path, default=None, help="staging dir from fetch_runtime.py")
    parser.add_argument("--version", default=None, help="override the version used in the artifact name")
    parser.add_argument("--entry", type=Path, default=DEFAULT_ENTRY, help="entry point script to ship")
    parser.add_argument(
        "--entry-name",
        default="eversend_green.py",
        help="name the entry point gets inside app/ (the launchers hard-code this)",
    )
    parser.add_argument("--strip", action="store_true", help="prune unused Qt payload (see docs/PACKAGING.md)")
    parser.add_argument("--drop-site-sources", action="store_true", help="delete site/**/*.py after compiling them to .pyc")
    parser.add_argument("--no-zip", action="store_true", help="assemble the tree but do not create the archive")
    parser.add_argument("--compress-level", type=int, default=6, choices=range(0, 10))
    parser.add_argument("--fetch", action="store_true", help="run fetch_runtime.py first if the staging dir is missing")
    parser.add_argument("--no-selftest", action="store_true", help="skip the post-build self-test")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    spec = fetch_runtime.PLATFORMS[args.platform]
    out_dir: Path = (args.out or (CACHE_DIR.parent / ".build")).resolve()
    stage_dir: Path = (args.runtime_dir or (CACHE_DIR / f"stage-{spec.name}")).resolve()
    version = args.version or read_app_version()
    host_python = Path(sys.executable)

    print("== EverSend green build ==")
    print(f"platform : {args.platform} ({spec.pbs_triple})")
    print(f"version  : {version}")
    print(f"out      : {out_dir}")
    print(f"stage    : {stage_dir}")

    # -- 1. runtime + wheels -------------------------------------------------
    if not (stage_dir / "runtime").exists() or not (stage_dir / "site").exists():
        if not args.fetch:
            print(
                f"\nERROR: {stage_dir} has no runtime/ and site/.\n"
                f"       Run:  python3 tools/fetch_runtime.py --platform {args.platform} "
                f"--out {stage_dir}\n"
                f"       (or pass --fetch to do it automatically)"
            )
            return 2
        print("\n[1/7] fetching runtime and wheels")
        rc = fetch_runtime.main(["--platform", args.platform, "--out", str(stage_dir)])
        if rc != 0:
            return rc

    tree = out_dir / "EverSend"
    print(f"\n[1/7] assembling {tree}")
    if tree.exists():
        rmtree_force(tree)
    tree.mkdir(parents=True)

    print(f"    runtime/ <- {stage_dir / 'runtime'}")
    shutil.copytree(stage_dir / "runtime", tree / "runtime", symlinks=True, copy_function=shutil.copy2)
    print(f"    site/    <- {stage_dir / 'site'}")
    shutil.copytree(stage_dir / "site", tree / "site", symlinks=True, copy_function=shutil.copy2)

    copied = copy_app_package(SRC_PACKAGE, tree / "app" / "eversend")
    print(f"    app/eversend/ <- {SRC_PACKAGE} ({copied} files)")
    entry_target = tree / "app" / args.entry_name
    shutil.copy2(args.entry, entry_target)
    if args.platform == "linux":
        entry_target.chmod(0o644)
    print(f"    app/{args.entry.name}")

    (tree / "data").mkdir(exist_ok=True)
    (tree / "received").mkdir(exist_ok=True)
    (tree / "data" / ".gitkeep").write_text("", "utf-8")
    (tree / "received" / ".gitkeep").write_text("", "utf-8")

    steps: list[Step] = []
    size_after_assemble = tree_size(tree)
    steps.append(Step("assemble (runtime + site + app)", 0, size_after_assemble))

    # -- 2. launchers --------------------------------------------------------
    print("\n[2/7] launchers and README")
    run_sh = tree / "run.sh"
    run_sh.write_text(RUN_SH, encoding="utf-8", newline="\n")
    run_sh.chmod(0o755)
    run_bat = tree / "run.bat"
    run_bat.write_text(RUN_BAT.replace("\n", "\r\n"), encoding="utf-8", newline="")
    readme = tree / "README.txt"
    readme.write_text(
        README_TXT.format(
            version=version,
            platform="Windows x64" if args.platform == "windows" else "Linux x86_64",
            build_time=time.strftime("%Y-%m-%d %H:%M:%S"),
        ),
        encoding="utf-8-sig",  # BOM: Windows Notepad/写字板 otherwise guesses ANSI and garbles Chinese
        newline="\r\n",
    )
    print(f"    run.sh (mode {oct(run_sh.stat().st_mode & 0o777)}), run.bat (CRLF), README.txt (UTF-8 BOM)")

    # -- 3. no symlinks anywhere --------------------------------------------
    print("\n[3/7] materialising symlinks (FAT32/exFAT + zip safety)")
    links, dropped, extra = materialize_symlinks(tree)
    print(
        f"    {links} symlink(s) replaced with real copies (+{human(extra)}), "
        f"{dropped} dev-only link(s) dropped"
    )
    steps.append(
        Step("materialise symlinks", 0, 0, f"{links} copied (+{human(extra)}), {dropped} dropped")
    )

    # -- 4. bytecode ---------------------------------------------------------
    print("\n[4/7] compiling bytecode")
    if args.platform == "linux":
        embedded = tree / "runtime" / spec.interpreter_rel
        compiler = embedded if embedded.exists() else host_python
        compiler_label = "embedded" if compiler == embedded else "host"
        if compiler == embedded:
            embedded.chmod(embedded.stat().st_mode | 0o755)
    else:
        compiler = host_python
        compiler_label = "host (cannot execute the Windows interpreter here)"
    print(f"    compiler: {compiler} [{compiler_label}]")
    before_compile = tree_size(tree)
    ok = compile_bytecode(compiler, tree, ["app", "site"], label=compiler_label)
    if not ok:
        print("    ! bytecode compilation failed; the tree still works, only startup is slower")
    pyc, after_compile = count_bytecode(tree)
    steps.append(
        Step(
            "bytecode (compileall -b, unchecked-hash)",
            before_compile,
            tree_size(tree),
            f"{pyc} .pyc written",
        )
    )
    print(f"    {pyc} .pyc files; tree {human(before_compile)} -> {human(tree_size(tree))}")

    if args.drop_site_sources:
        removed, freed = drop_sources(tree / "site")
        print(f"    dropped {removed} site/*.py ({human(freed)}); app/ sources kept")
        steps.append(Step("drop site sources", 0, 0, f"-{human(freed)} ({removed} files)"))
    else:
        measured = sum(p.stat().st_size for p in (tree / "site").rglob("*.py"))
        print(f"    keeping site/*.py: deleting them would save only {human(measured)} (see docs/PACKAGING.md)")

    # -- 5. optional strip ---------------------------------------------------
    if args.strip:
        print("\n[5/7] stripping unused Qt payload")
        freed, report = apply_strip(tree, args.platform)
        for rel, size, reason in report:
            print(f"    - {rel}  ({human(size)})  -- {reason}")
        print(f"    total removed: {human(freed)}")
        steps.append(Step("--strip", 0, 0, f"-{human(freed)} in {len(report)} item(s)"))
        if not args.no_selftest and args.platform == "linux":
            print("    re-checking the GUI after stripping")
            probe = subprocess.run(
                [
                    str(tree / "runtime" / spec.interpreter_rel),
                    "-s",
                    "-B",
                    "-c",
                    "import PySide6.QtCore, PySide6.QtGui, PySide6.QtWidgets; print('qt ok')",
                ],
                cwd=str(tree),
                capture_output=True,
                text=True,
                env={
                    "PYTHONHOME": str(tree / "runtime"),
                    "PYTHONPATH": f"{tree / 'app'}:{tree / 'site'}",
                    "QT_QPA_PLATFORM": "offscreen",
                    "LD_LIBRARY_PATH": f"{tree / 'runtime' / 'lib'}:{tree / 'site' / 'PySide6' / 'Qt' / 'lib'}",
                    "PATH": "/usr/bin:/bin",
                },
            )
            print(f"      rc={probe.returncode} {probe.stdout.strip() or probe.stderr.strip()}")
            if probe.returncode != 0:
                print("    ! --strip broke the Qt import; rebuild without --strip")
                return 3
    else:
        print("\n[5/7] strip: off (pass --strip to prune; conservative default)")

    # -- 6. self-test --------------------------------------------------------
    final_size = tree_size(tree)
    if not args.no_selftest and args.platform == "linux":
        print("\n[6/7] self-test with the embedded interpreter")
        env = {
            "PYTHONHOME": str(tree / "runtime"),
            "PYTHONPATH": f"{tree / 'app'}:{tree / 'site'}",
            "PYTHONDONTWRITEBYTECODE": "1",
            "EVERSEND_DATA_DIR": str(tree / "data"),
            "EVERSEND_RECEIVE_DIR": str(tree / "received"),
            "PATH": "/usr/bin:/bin",
            "HOME": os.environ.get("HOME", "/tmp"),
        }
        result = subprocess.run(
            [str(tree / "runtime" / spec.interpreter_rel), "-s", "-B", str(entry_target), "--selftest"],
            cwd=str(tree),
            capture_output=True,
            text=True,
            env=env,
        )
        print("      " + (result.stdout or result.stderr).strip().replace("\n", "\n      "))
        if result.returncode != 0:
            print("    ! self-test failed")
            return 3
    else:
        print("\n[6/7] self-test skipped")

    # -- 7. zip --------------------------------------------------------------
    artifact = out_dir / f"EverSend-{version}-{args.platform}.zip"
    if args.no_zip:
        print(f"\n[7/7] --no-zip: tree left at {tree}")
    else:
        print(f"\n[7/7] zipping -> {artifact}")
        members, elapsed = make_zip(tree, artifact, compress_level=args.compress_level)
        print(f"    {members} entries in {elapsed}s")

    # -- report --------------------------------------------------------------
    print("\n" + "=" * 72)
    print("BUILD REPORT")
    print("=" * 72)
    print(f"tree      : {tree}  ({human(final_size)})")
    print("-" * 72)
    print(f"{'entry':<24}{'size':>14}{'files':>10}")
    for name, size, files in directory_breakdown(tree):
        print(f"{name:<24}{human(size):>14}{files:>10}")
    print("-" * 72)
    for step in steps:
        detail = f"  [{step.note}]" if step.note else ""
        if step.before and step.after and step.before != step.after:
            print(f"{step.name:<40} {human(step.before):>12} -> {human(step.after):>12}{detail}")
        else:
            print(f"{step.name:<40} {'':>12}    {human(step.after or 0):>12}{detail}")
    print("-" * 72)
    if not args.no_zip:
        print(f"artifact  : {artifact}")
        print(f"size      : {human(artifact.stat().st_size)} ({artifact.stat().st_size} bytes)")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except fetch_runtime.FetchError as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130) from None
