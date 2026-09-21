"""``python -m eversend`` entry point.

命令行模式**绝不能**导入 PySide6。CI 上实测过：在没有图形库的 runner 上，
``python -m eversend --cli selftest`` 直接倒在
``ImportError: libEGL.so.1: cannot open shared object file`` ——
因为入口先无条件导入了桌面界面模块。

这不只是 CI 的问题。``--cli`` 的用途本来就是"没有图形界面的服务器上也能收发文件"，
而那种机器上通常**根本没装 Qt 的运行库**。所以先看参数再决定导入什么。
"""

from __future__ import annotations

import sys


def _wants_cli(argv: list[str]) -> bool:
    """Whether this invocation is headless.

    Deliberately a plain scan rather than argparse: importing argparse is
    cheap but running the real parser twice is not, and an unknown flag here
    just means "not obviously headless", which falls through to the GUI.
    """
    return "--cli" in argv or "cli" in argv[:1]


def main() -> int:
    argv = sys.argv[1:]
    if _wants_cli(argv):
        from .cli import main as cli_main

        # ``--cli`` may appear anywhere; strip it and hand the rest over.
        rest = [a for a in argv if a != "--cli"]
        return cli_main(rest)

    from .desktop.app import main as gui_main

    return gui_main(argv)


if __name__ == "__main__":
    sys.exit(main())
