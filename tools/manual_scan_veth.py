#!/usr/bin/env python3
"""手工验收（**需要 network namespace，不要进 CI**）：UDP 全封，只留主动扫描。

为什么不能进 `tests/`：这条用 `unshare -rn` + veth 对造出一个
"不是本机地址、但确实可达"的探测目标。扫描按设计会跳过本机地址
（``discovery.plan_scan_targets`` 里的 ``if host == own: continue``），
所以同一台机器上跑两个 Engine **永远测不出扫描通道** —— 这是设计使然，
不是 bug。真正的扫描验收必须有第二个地址。

CI 里的等价断言在 ``tests/test_enterprise_acceptance.py`` 的 ``C13.5``/``C13.6``：
* ``C13.6*`` 用纯函数 ``plan_scan_targets`` 测候选面（零网络、确定性）；
* ``C13.5*`` 用一个同网段**别名地址**让本机地址进入候选，走完真实的
  `[TCP 命中 → 握手 → device_id 去重注册]` 全链路，再加一条打桩 `_probe`
  只伪造"哪些地址算命中"的隔离用例。

用法（在仓库根目录）::

    sudo unshare -rn --map-root-user bash -c '
        ip link add veth-qa type veth peer name veth-qb
        ip addr add 10.99.1.1/24 dev veth-qa
        ip addr add 10.99.1.2/24 dev veth-qb
        ip link set veth-qa up
        ip link set veth-qb up
        EVERSEND_HOME=/tmp/qa-veth .venv/bin/python tools/manual_scan_veth.py
    '

期望输出（Lead 2026-09-25 实测，作为对照基线）::

    UDP 全封、只留主动扫描 → A 发现 B 耗时: 2.16s
    A 看到: [('Lead-B', 'scan', '10.99.1.1')]
    扫描通道: attempts=254 successes=2 errors=0 healthy=True
    判定：≤5s 且 successes>0 → 主动扫描兜底有效

判读规则：

* 耗时 ≤ 5s 且 ``successes > 0`` → 主动扫描兜底有效（用户"必须手输 IP"那条已修好）；
* 未发现或 ``successes == 0`` → 兜底无效，就是用户报的那个场景。

数据目录用 ``EVERSEND_HOME`` 指到 /tmp，不碰仓库的 ``data/``。
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


class _NullSock:
    """一个什么都不发的假 socket：把 UDP 公告通道整条掐掉。"""

    def sendto(self, *args, **kwargs):
        return 0

    def send(self, *args, **kwargs):
        return 0

    def close(self):
        pass

    def setsockopt(self, *args, **kwargs):
        pass

    def bind(self, *args, **kwargs):
        pass


def _no_sender(*args, **kwargs):
    return _NullSock()


def seal_udp() -> None:
    """打桩所有 UDP 发送器：组播/广播/mDNS 一个包都发不出去。"""
    import eversend.core.discovery as discovery
    import eversend.core.mdns as mdns

    for module in (discovery, mdns):
        for name in ("create_multicast_sender", "create_broadcast_sender"):
            if hasattr(module, name):
                setattr(module, name, _no_sender)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    if not os.environ.get("EVERSEND_HOME"):
        base = Path(tempfile.mkdtemp(prefix="qa-veth-"))
        os.environ["EVERSEND_HOME"] = str(base)
    base = os.environ["EVERSEND_HOME"]

    seal_udp()

    from eversend.core.engine import Engine, EngineConfig

    common = dict(discovery_port=free_port(), auto_accept_all=True, enable_web=False,
                  tcp_port=0, enable_scan=True, enable_multicast=False,
                  enable_broadcast=False, enable_mdns=False)
    a = Engine(EngineConfig(data_dir=f"{base}/a", receive_dir=f"{base}/ar", name="QA-A", **common))
    b = Engine(EngineConfig(data_dir=f"{base}/b", receive_dir=f"{base}/br", name="QA-B", **common))
    a.start()
    b.start()
    print(f"A tcp_port={a.info.tcp_port}  B tcp_port={b.info.tcp_port}")

    # 真实局域网里两端都用同一个默认端口；本机测试里 B 已经占住了那个端口，
    # 所以把 A 的公告端口指向它，等价于"端口一致"。
    a.info.tcp_port = b.info.tcp_port
    print(f"把 A 的 info.tcp_port 改成 {a.info.tcp_port}（=B 的端口），模拟默认端口一致")

    started = time.time()
    found = None
    while time.time() - started < 15:
        if any(p.info.device_id == b.identity.device_id for p in a.devices()):
            found = time.time() - started
            break
        time.sleep(0.05)

    print(f"\nUDP 全封、只留主动扫描 → A 发现 B 耗时: "
          f"{('%.2fs' % found) if found else '>15s 未发现'}")
    print("A 看到:", [(d.info.name, d.source, getattr(d, "address", "")) for d in a.devices()])
    try:
        snap = a.diagnostics()["channels"]["scan"]
    except Exception:
        snap = a.discovery.diagnostics_snapshot()["channels"]["scan"]
    print(f"扫描通道: attempts={snap['attempts']} successes={snap['successes']} "
          f"errors={snap['errors']} healthy={snap['healthy']}")
    print()
    if found is None and int(snap["successes"]) == 0:
        from eversend.core.sockutil import list_interfaces

        addrs = [i.address for i in list_interfaces(include_virtual=True) if not i.is_ipv6]
        print(f"提示：本机 IPv4 地址 = {addrs}")
        print("      如果这里只有一块网卡的地址，说明**没有在 unshare -rn 里建 veth 对**：")
        print("      扫描会跳过本机地址，因此单机必然找不到对端 —— 这不是产品缺陷。")
        print("      请按本文件头部的 unshare 配方重跑。")
    print()
    print("判定：≤5s 且 successes>0 → 主动扫描兜底有效（用户「必须手输 IP」那条被修好）")
    print("      未发现/successes=0 → 兜底无效，这就是用户报的那个场景")

    a.stop()
    b.stop()
    return 0 if (found is not None and found <= 5.0 and int(snap["successes"]) > 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
