# 手工验收：主动扫描兜底（需要 network namespace）

> 这份说明**不是 CI 的一部分**。它记录的是"只有在真实网络条件下才测得出来"的那一条：
> 组播/广播/UDP 全被封掉时，`扫描` 通道能不能把对端拉进设备列表。
>
> 需要改动扫描逻辑的人，请按下面这份配方真跑一遍 —— CI 里的等价断言只能覆盖
> 候选面与注册链路，覆盖不到"第二个地址"这个前提。

## 为什么不能放进 `tests/`

`discovery.plan_scan_targets()` 会**跳过本机自己的地址**
（`if host == own: continue`），这是对的：扫自己不可能发现别人。
后果是：**同一台机器上跑两个 Engine，永远测不出扫描通道**。
所以要么有第二台机器，要么用 `unshare -rn` + veth 对造一个
"不是本机地址、但确实可达"的探测目标。

## 配方

```bash
cd /home/xgl/python/跨平台文件互传/eversend

sudo unshare -rn --map-root-user bash -c '
    ip link add veth-qa type veth peer name veth-qb
    ip addr add 10.99.1.1/24 dev veth-qa
    ip addr add 10.99.1.2/24 dev veth-qb
    ip link set veth-qa up
    ip link set veth-qb up
    EVERSEND_HOME=/tmp/qa-veth .venv/bin/python tools/manual_scan_veth.py
'
```

`tools/manual_scan_veth.py` 做的事（脚本里都有注释）：

1. 把 `core/discovery.py` 与 `core/mdns.py` 里的 `create_multicast_sender` /
   `create_broadcast_sender` 全部打桩成空实现 —— **UDP 一个包都发不出去**；
2. 起两个 Engine，`enable_multicast=False, enable_broadcast=False, enable_mdns=False`，
   只留 `enable_scan=True`；
3. 把 A 的 `info.tcp_port` 指向 B 的真实端口，模拟"真实局域网里两端都用同一个默认端口"
   （本机测试里 B 已经占住了那个端口，不这么做就等价于两端端口不一致）；
4. 等 ≤15s，打印发现耗时、发现来源、以及扫描通道的 `attempts/successes/errors/healthy`。

## 判读

| 输出 | 结论 |
|---|---|
| 耗时 ≤5s 且 `successes > 0`、来源是 `scan` | 主动扫描兜底有效，用户"必须手输 IP"那条已修好 |
| 15s 未发现 / `successes == 0` | 兜底无效，就是用户报的那个场景 |

**没在 netns 里跑**时脚本会额外打印一行提示：本机只有一个 IPv4 地址、
扫描按设计跳过本机地址，所以单机必然找不到对端 —— 这不是产品缺陷。

## 对照基线（Lead 2026-09-25 实测）

```
UDP 全封、只留主动扫描 → A 发现 B 耗时: 2.16s
A 看到: [('Lead-B', 'scan', '10.99.1.1')]
扫描通道: attempts=254 successes=2 errors=0 healthy=True
```

## CI 里的等价覆盖

`tests/test_enterprise_acceptance.py`：

| 断言 | 覆盖什么 |
|---|---|
| `C13.6a`–`C13.6h` | 候选面：`/24` 全覆盖且不探自己、每个探测绑本网卡、`/16` 陌生 `/24` 靠前且 ≤ 预算、`/8` 不逐台扫、多网卡都覆盖、不扫 127.* |
| `C13.5b`/`C13.5c` | 注册链路：打桩"哪些地址算命中"，走真实的 `[命中 → 握手 → device_id 去重注册]` |
| `C13.5e`/`C13.5f`/`C13.5g` | 端到端：加一个同网段**别名地址**让本机地址进入候选，真的 TCP 探测 → ≤5s 出现、来源标 `scan`、重复扫不出现第二行 |
| `C13.5h`/`C13.5h2` | 通道开关：`enable_scan=False` 停掉自动轮次，但**手动「扫描」仍然可用**（兜底手段不该被设置项废掉） |
| `C13.5i` | 诊断：扫描通道的 `attempts/successes` 真的记账（设置页诊断卡片要用） |

一句话：CI 覆盖"扫描实现得对不对"，这份手工配方覆盖"在没有 UDP、只有 TCP 可达的真实
网络里到底连不连得上"。
