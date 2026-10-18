# 性能 A/B 对比（opt-1017：旧树 e18c079 → 当前树）

> 测量：`tools/bench_transfer.py evsd-loopback`，**tmpfs(/dev/shm)**，5 轮中位 + 1 预热，
> 同一脚本同一机器；BEFORE=git worktree `e18c079`，AFTER=当前工作树。
> **为什么用 tmpfs**：本机 `/home` 是 btrfs on USB PSSD，fdatasync 仅 39–56 MiB/s，
> 会把 EVSD 收尾卡到 60s 级停顿（实测出现 107/16.8 MiB/s 交替），掩盖协议/CPU 差异。
> 主判据 = **CPU s/MiB**（与负载无关），辅判据 = tmpfs MiB/s。

| 大小 MiB | 流数 | BEFORE MiB/s | AFTER MiB/s | Δ MiB/s | BEFORE CPU s/MiB | AFTER CPU s/MiB | Δ CPU | 判定 |
|---|---|---|---|---|---|---|---|---|
| 512 | 1 | 265.1 | 278.3 | +5.0% | 0.00588 | 0.00521 | -11.4% | ✅ CPU 降低 |
| 512 | 4 | 183.0 | 208.3 | +13.9% | 0.01391 | 0.01184 | -14.9% | ✅ CPU 降低 |
| 64 | 1 | 111.2 | 115.4 | +3.7% | 0.00609 | 0.00547 | -10.2% | ✅ CPU 降低 |
| 64 | 4 | 145.2 | 133.6 | -8.0% | 0.01141 | 0.01094 | -4.1% | ✅ CPU 降低 / 速度回退 |
| 1024 | 4 | 202.0 | 217.5 | +7.7% | 0.01202 | 0.01108 | -7.8% | ✅ CPU 降低 |

## 结论

- **CPU s/MiB 平均变化 -9.7%**（负值=更省 CPU）；tmpfs 吞吐平均 +4.5%。
- T1（AEAD 去 `bytes()` 全量拷贝）+ T5（dict 索引去 O(n) 扫描）+ T8（加密会话不重复 crc32） 的直接收益是**每字节 CPU 成本下降**；tmpfs 上吞吐还受 Python/loopback 上限影响，所以以 CPU 判据为准。
- T4（fsync 移出 PartFile 锁）在 tmpfs 上**预期约等于 0 收益**（tmpfs 的 fsync 是空操作）；它的收益在真实盘：transport-fixes.md 的确定性慢盘实验（注入 250ms fdatasync）为 **p99 11.8s → 0.70ms**；本机 btrfs 端到端 w/ USB 盘停顿噪声太大，不作为判据。
- HTTP 手机链路本轮只加了**可续传上传**（旧路径不变），协议层回归见 `test_http_resume.py` 与 `web/selftest.py`（356 项）；真实 WiFi 链路吞吐主要由 WiFi/手机落盘决定（真机基线 电脑→手机 ≈23 MiB/s），本轮未声称提升该链路吞吐。

## 复现

```bash
# BEFORE（旧树）
cd /home/xgl/qa-real/baseline-tree
PYTHONPATH=src python3 tools/bench_transfer.py evsd-loopback --sizes 64,512 --streams 1,4 \
  --repeats 5 --warmups 1 --scratch /dev/shm/ab --json out.json --md out.md --label before:tmpfs
# AFTER（当前树）
cd /home/xgl/我的项目/自制软件/韧传/eversend
PYTHONPATH=src python3 tools/bench_transfer.py evsd-loopback --sizes 64,512 --streams 1,4 \
  --repeats 5 --warmups 1 --scratch /dev/shm/ab --json out.json --md out.md --label after:tmpfs
```
