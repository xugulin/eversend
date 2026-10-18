#!/usr/bin/env python3
"""为 Linux 桌面安装 EverSend（韧传）启动项：.desktop + 图标。

用法（任意目录均可）::

    python3 tools/install_desktop_entry.py                  # 装到应用菜单 + 桌面快捷方式
    python3 tools/install_desktop_entry.py --no-desktop-icon # 只装应用菜单
    python3 tools/install_desktop_entry.py --uninstall      # 全部撤掉

做的事：
1. 用源码树自带的运行时图标（``eversend.desktop.theme.app_icon``，和窗口图标同一份
   绘制代码）渲染 hicolor 各尺寸 PNG —— **不依赖任何二进制资源**，也避免"desktop 的
   Icon 指向不存在文件 → 桌面显示白板"；
2. 用 ``packaging/linux/eversend.desktop.in`` 模板生成
   ``~/.local/share/applications/eversend.desktop``，``Exec``/``TryExec`` 指向仓库内
   启动器 ``packaging/linux/eversend-launcher.sh`` 的**绝对路径**（启动器自己解析
   ``EVERSEND_HOME`` 与 ``PYTHONPATH``，所以从菜单启动不会把 data/received 写到 $HOME）；
3. 按 ``XDG_DESKTOP_DIR``（读 ``~/.config/user-dirs.dirs``，回退 ~/Desktop、~/桌面）
   复制一份可执行的桌面快捷方式，并尽力标记 ``metadata::trusted``；
4. 用 ``desktop-file-validate`` 校验；刷新 ``update-desktop-database`` /
   ``gtk-update-icon-cache``（有就刷新，没有就跳过）。

仓库移动位置后重新跑一次即可（模板里的绝对路径会更新）。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
LAUNCHER = REPO / "packaging" / "linux" / "eversend-launcher.sh"
TEMPLATE = REPO / "packaging" / "linux" / "eversend.desktop.in"
SVG_FALLBACK = REPO / "src" / "eversend" / "web" / "assets" / "icon.svg"
ICON_NAME = "eversend"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
#: Qt 应用在 X11 下的窗口类（StartupWMClass）。app.py 里 setApplicationName("EverSend")，
#: 实测 WM_CLASS 的 class 就是 "EverSend"；写成一致，任务栏才能把窗口归到这个启动项。
WM_CLASS = "EverSend"


def data_home() -> Path:
    raw = os.environ.get("XDG_DATA_HOME")
    return Path(raw) if raw else Path.home() / ".local" / "share"


def desktop_dir() -> Path:
    """XDG_DESKTOP_DIR（本机实测 ``$HOME/Desktop``），回退中文桌面目录。"""
    config = Path.home() / ".config" / "user-dirs.dirs"
    try:
        for line in config.read_text(encoding="utf-8").splitlines():
            match = re.match(r'^XDG_DESKTOP_DIR="?\$HOME/([^"/]+)"?$', line.strip())
            if match:
                candidate = Path.home() / match.group(1)
                if candidate.is_dir():
                    return candidate
    except OSError:
        pass
    for name in ("Desktop", "桌面"):
        candidate = Path.home() / name
        if candidate.is_dir():
            return candidate
    return Path.home() / "Desktop"


def link_or_copy(source: Path, target: Path) -> None:
    """桌面项用**硬拷贝**而不是符号链接：文件管理器对链接的处理各不相同。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def run_quiet(argv: list[str]) -> bool:
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=60)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def render_icons(verbose: bool = True) -> str:
    """渲染 hicolor 图标集；成功返回 ``(图标路径 str)``，失败返回空串。

    返回的是**绝对路径**（而不是主题名）：桌面环境的图标缓存刷新时机不可控，
    绝对路径能保证图标立刻显示，不会出现"白板"。
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    try:
        from PySide6.QtGui import QGuiApplication

        from eversend.desktop import theme
    except Exception as exc:  # pragma: no cover - 装桌面项的环境一般都有 PySide6
        if verbose:
            print(f"[warn] 无法加载 PySide6/主题（{exc}），图标退回内置 SVG")
        return ""
    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841 - 需要保持存活
    icons_root = data_home() / "icons" / "hicolor"
    rendered: list[Path] = []
    for size in ICON_SIZES:
        pixmap = theme.app_icon(size, dark=True).pixmap(size, size)
        target = icons_root / f"{size}x{size}" / "apps" / f"{ICON_NAME}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not pixmap.save(str(target), "PNG"):
            if verbose:
                print(f"[warn] 图标 {size}x{size} 渲染失败：{target}")
            continue
        rendered.append(target)
    if not rendered:
        return ""
    if verbose:
        print(f"[ok] 图标：{len(rendered)} 个尺寸写入 {icons_root}/<size>x<size>/apps/{ICON_NAME}.png")
    # 选最大的一档给 desktop 用绝对路径
    return str(icons_root / f"{ICON_SIZES[-1]}x{ICON_SIZES[-1]}" / "apps" / f"{ICON_NAME}.png")


def build_desktop_text(icon_value: str) -> str:
    exec_path = str(LAUNCHER)
    if any(ch.isspace() for ch in exec_path):
        exec_path = '"' + exec_path.replace('"', '\\"') + '"'
    text = TEMPLATE.read_text(encoding="utf-8")
    return (
        text.replace("@EXEC@", exec_path)
        .replace("@TRYEXEC@", str(LAUNCHER))
        .replace("@ICON@", icon_value)
        .replace("@WMCLASS@", WM_CLASS)
    )


def install() -> int:
    if not LAUNCHER.is_file():
        print(f"[error] 启动器不存在：{LAUNCHER}", file=sys.stderr)
        return 2
    for path in (LAUNCHER,):
        path.chmod(0o755)

    icon_path = render_icons()
    if not icon_path:
        # 回退：把内置 SVG 装到图标目录，并把绝对路径交给 desktop 项。
        icons_root = data_home() / "icons" / "hicolor" / "scalable" / "apps"
        icons_root.mkdir(parents=True, exist_ok=True)
        target = icons_root / f"{ICON_NAME}.svg"
        if SVG_FALLBACK.is_file():
            shutil.copyfile(SVG_FALLBACK, target)
            icon_path = str(target)
            print(f"[warn] 图标退回 SVG：{target}")
        else:
            icon_path = ICON_NAME
            print("[warn] 找不到任何图标资源，desktop 项将使用主题名")

    content = build_desktop_text(icon_path)

    applications = data_home() / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    entry = applications / f"{ICON_NAME}.desktop"
    entry.write_text(content, encoding="utf-8")
    entry.chmod(0o755)
    print(f"[ok] 应用菜单项：{entry}")

    shortcut = desktop_dir() / f"{ICON_NAME}.desktop"
    link_or_copy(entry, shortcut)
    shortcut.chmod(0o755)
    # GNOME 桌面需要 trusted 标记才允许双击启动；其它环境忽略失败。
    run_quiet(["gio", "set", str(shortcut), "metadata::trusted", "true"])
    print(f"[ok] 桌面快捷方式：{shortcut}")

    ok = True
    if shutil.which("desktop-file-validate"):
        for target in (entry, shortcut):
            proc = subprocess.run(
                ["desktop-file-validate", str(target)], capture_output=True, text=True, timeout=60
            )
            if proc.returncode != 0:
                ok = False
                print(f"[warn] desktop-file-validate 报错：{target}\n{proc.stderr.strip()}")
            elif proc.stderr.strip():
                print(f"[info] desktop-file-validate 提示：{proc.stderr.strip()}")
        if ok:
            print("[ok] desktop-file-validate：通过")
    else:
        print("[info] 未安装 desktop-file-validate，跳过校验")

    run_quiet(["update-desktop-database", str(applications)])
    run_quiet(["gtk-update-icon-cache", "-f", "-t", str(data_home() / "icons" / "hicolor")])
    print(
        "\n完成。用下面的命令验证启动：\n"
        f"  gtk-launch {ICON_NAME}\n"
        "卸载：python3 tools/install_desktop_entry.py --uninstall"
    )
    return 0 if ok else 1


def uninstall() -> int:
    removed: list[Path] = []
    applications = data_home() / "applications" / f"{ICON_NAME}.desktop"
    for target in (applications, desktop_dir() / f"{ICON_NAME}.desktop"):
        if target.exists():
            target.unlink()
            removed.append(target)
    icons_root = data_home() / "icons" / "hicolor"
    for size in ICON_SIZES:
        target = icons_root / f"{size}x{size}" / "apps" / f"{ICON_NAME}.png"
        if target.exists():
            target.unlink()
            removed.append(target)
    svg = icons_root / "scalable" / "apps" / f"{ICON_NAME}.svg"
    if svg.exists():
        svg.unlink()
        removed.append(svg)
    run_quiet(["update-desktop-database", str(applications.parent)])
    run_quiet(["gtk-update-icon-cache", "-f", "-t", str(icons_root)])
    for path in removed:
        print(f"[ok] 已删除：{path}")
    if not removed:
        print("[info] 没有找到已安装的桌面项/图标")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装/卸载 EverSend Linux 桌面启动项")
    parser.add_argument("--uninstall", action="store_true", help="删除已安装的 desktop 项与图标")
    args = parser.parse_args(argv)
    return uninstall() if args.uninstall else install()


if __name__ == "__main__":
    sys.exit(main())
