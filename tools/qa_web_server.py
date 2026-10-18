#!/usr/bin/env python3
"""跑一个只是为了让网页版能被截图的 EverSend 实例（前台，Ctrl-C 退出）。

它是 QA 工具：打印出可访问的 URL，然后用浏览器打开它截图。
数据目录走 ``EVERSEND_HOME``，不碰仓库的 ``data/``。

用法::

    EVERSEND_HOME=/tmp/qa-web .venv/bin/python tools/qa_web_server.py [--port 0]
"""

from __future__ import annotations

import argparse
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0, help="网页端口（0 = 随机）")
    parser.add_argument("--name", default="QA 台式机")
    args = parser.parse_args()

    if not os.environ.get("EVERSEND_HOME"):
        os.environ["EVERSEND_HOME"] = tempfile.mkdtemp(
            prefix="qa-web-", dir=str(REPO / ".testscratch"))
    root = Path(os.environ["EVERSEND_HOME"])

    from eversend.core.engine import Engine, EngineConfig
    from eversend.core.model import DeviceInfo, Peer
    from eversend.web.server import WebUI

    port = args.port or free_port()
    engine = Engine(EngineConfig(data_dir=str(root / "data"), receive_dir=str(root / "received"),
                                 name=args.name, tcp_port=0, discovery_port=free_port(),
                                 web_port=port, auto_accept_all=True, enable_web=True,
                                 enable_broadcast=False, enable_mdns=False))
    engine.start()

    # 造一台"在线设备"和一条记录，截图里才有东西可看（否则是空状态）。
    from eversend.core.chat import direct_conversation_id

    peer = Peer(info=DeviceInfo(device_id="qa-web-peer", name="客厅台式机", kind="desktop",
                                platform="windows", tcp_port=52117),
                address="192.168.1.42", port=52117, source="multicast", trusted=True)
    engine.discovery.peers.add_direct(peer.info, peer.address, peer.port, "multicast")
    conv = direct_conversation_id(engine.info.device_id, peer.info.device_id)
    engine.chat.upsert_conversation(conv, kind="direct", title="和 客厅台式机 的对话",
                                    members=[engine.info.device_id, peer.info.device_id])
    engine.chat.add_message(conv, sender=peer.info.device_id, sender_name="客厅台式机",
                            text="报告我放桌面上啦", direction="in")
    engine.chat.add_message(conv, sender=engine.info.device_id, sender_name=args.name,
                            text="收到，这就传给你", direction="out")
    sample = root / "示例文件.bin"
    sample.write_bytes(b"\0" * (2 * 1024 * 1024))
    engine.chat.record_transfer(conv, direction="out", peer=peer.info.device_id,
                                name=sample.name, size=sample.stat().st_size, state="sent")

    ui = WebUI(engine, port=port)
    ui.start()
    url = (ui.urls() or [f"http://127.0.0.1:{port}/"])[0]
    print(f"WEB_URL={url}", flush=True)
    print(f"WEB_PORT={port}", flush=True)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        ui.stop()
        engine.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
