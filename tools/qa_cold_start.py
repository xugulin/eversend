#!/usr/bin/env python3
"""量一次**真的冷启动**：新进程里起引擎 + 建主窗口，把耗时打成 JSON。

为什么要单独一个进程：`E5` 的其它断言在验收进程里跑，跑到第 16 项时那个进程里已经
有一个 QApplication、若干 Engine、两百多个控件、前面几套子测试留下的负载。
在那儿量出来的 1.58s 不是"窗口冷启动慢"，而是"测量环境不干净" ——
同一份代码空闲时单独量是 **0.20~0.22s**，差 7 倍全是环境。

用户按图标启动应用，经历的就是"新进程 → 引擎 → 窗口"，所以冷启动只能在
新进程里量。这个脚本就是那一次。

用法::

    .venv/bin/python tools/qa_cold_start.py            # 打一行 JSON
    xvfb-run -a .venv/bin/python tools/qa_cold_start.py

输出字段：``engine_ms`` / ``window_ms`` / ``first_paint_ms`` / ``total_ms``。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    os.environ["QT_QPA_PLATFORM"] = os.environ.get("EVERSEND_GUI_PLATFORM", "xcb")
    root = Path(os.environ.get("EVERSEND_HOME")
                or tempfile.mkdtemp(prefix="qa-cold-", dir=str(REPO / ".testscratch")))

    started = time.monotonic()
    from PySide6.QtWidgets import QApplication

    from eversend.core.engine import Engine, EngineConfig
    from eversend.desktop.main_window import MainWindow

    app = QApplication(sys.argv[:1])
    from eversend.desktop import i18n, theme

    i18n.install(app)
    app.setStyleSheet(theme.stylesheet(theme.is_dark()))

    engine = Engine(EngineConfig(data_dir=str(root / "data"), receive_dir=str(root / "received"),
                                 name="QA-冷启动", tcp_port=0, discovery_port=free_port(),
                                 auto_accept_all=True, enable_web=False,
                                 enable_broadcast=False, enable_mdns=False))
    engine.start()
    engine_done = time.monotonic()

    window = MainWindow(engine, str(root / "settings.json"))
    window_done = time.monotonic()
    window.resize(1180, 780)
    window.show()
    app.processEvents()
    painted = time.monotonic()

    result = {
        "engine_ms": round((engine_done - started) * 1000),
        "window_ms": round((window_done - engine_done) * 1000),
        "first_paint_ms": round((painted - window_done) * 1000),
        "total_ms": round((painted - started) * 1000),
        "loadavg": [round(x, 2) for x in (os.getloadavg() if hasattr(os, "getloadavg") else ())],
        "cores": os.cpu_count(),
    }
    print(json.dumps(result, ensure_ascii=False))

    window.close()
    engine.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
