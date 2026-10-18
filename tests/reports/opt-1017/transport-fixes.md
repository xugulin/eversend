# 阶段 2 · 传输内核实现报告（T1 / T4 / T8 / T5 / T10 / T11）

> 任务：`task-7`（transport-core，阶段 2 实现）
> 基线工作树：`e18c079` + 阶段 1 审计（`tests/reports/opt-1017/transport-audit.md`）
> 日期：2026-10-17
> **本报告只记录工作树改动（未 commit）**；性能最终以 perf-bench 的 before/after 为准，
> 这里给的是「同机、同进程、可复现」的 micro 相对数字 + 三个回归套件的 PASS/FAIL。

**一句话**：6 项全部落地（T11 采用向后兼容的 HELLO/HELLO_ACK `cipher` 协商方案，已同步
`docs/PROTOCOL.md` 兼容矩阵）。改动集中在 `crypto.py` / `chunkstore.py` / `connection.py` /
`transfer.py` / `sockutil.py` + `docs/PROTOCOL.md`，**没有改协议语义、没有 commit**；
三个回归套件全绿（loopback 的固定吞吐门槛在共享负载下按 `EVERSEND_MBPS_FLOOR=5` 放宽，
吞吐对比交给 perf-bench，见 §5）。

---

## 1. 工作树改动文件清单（未 commit）

| 文件 | 改动 | 对应项 |
|---|---|---|
| `src/eversend/core/crypto.py` | `Cipher.seal/open` 去 `bytes()` 全量拷贝；`establish_session` 走 `preferred_algorithm()`；新增 `preferred_algorithm()` / `negotiated_algorithm()` / `SUPPORTED_ALGORITHMS` | T1、T11 |
| `src/eversend/core/chunkstore.py` | fsync/fdatasync 从「PartFile 锁内、数据流线程上」改为「后台 syncer 线程、锁外」；`flush()` 收尾仍同步落盘；新增 `SYNC_POLL_INTERVAL` / `SYNCER_MIN_BYTES` | T4 |
| `src/eversend/core/connection.py` | `ChunkFrame.aead_verified` 字段（加密路径置 True）；握手 HELLO/HELLO_ACK 增加可选 `cipher` 字段并协商；`Connection.socket_buffers` 诊断快照 | T8、T11、T10 |
| `src/eversend/core/transfer.py` | 接收端仅对非 AEAD 块重算 CRC32；`SendSession` 增加 `_entry_by_index` dict、去掉每块线性扫描与函数内 import | T8、T5 |
| `src/eversend/core/sockutil.py` | `tune_socket` 调 `getsockopt` 回读实际缓冲、存 `sock.eversend_buffers`、`EVERSEND_DEBUG` 打印（不阻断） | T10 |
| `docs/PROTOCOL.md` | 新增 §3.6 `cipher` 协商 + 兼容矩阵；HELLO/HELLO_ACK/算法行同步 | T11 |

> 说明：工作树里其它 `M`（android/desktop/web/*、`core/model.py`、`tests/test_real_device.py`）
> 是其他专家的改动，**不是本任务产生的**；本任务只触碰上表 6 个文件（外加本报告）。

---

## 2. 逐项实现与改前/改后

### T1 · AEAD 零拷贝（`crypto.py`）

**改动**：`Cipher.seal()` 里的 `bytes(plaintext)`、`Cipher.open()` 里的 `bytes(ciphertext)`
删除，直接把 `memoryview` 交给 cryptography（其 AEAD 接受任意 bytes-like）。`NullCipher`
保持原样（它只在未加密会话的冷路径被调用，不参与热路径）；`seal_into` 保持委托 `seal`。

**为什么收益**：热路径每个 chunk 在发送端多做 1 次全量拷贝、接收端再做 1 次；4/16 MiB 块时
`bytes()` 还触发「新分配 + 大量缺页」，实测比 memcpy 慢得多。

**改前/改后（本机实测，256 MiB 负载取中位数；同一进程内旧实现 shim vs 新实现交错测量）**：

| 块大小 | 旧 `bytes()` seal | 新 memoryview seal | 同进程 A/B 变化 | 阶段 1 洁净机同口径（基准） |
|---|---|---|---|---|
| 1 MiB | 0.76 GiB/s | **3.67 GiB/s** | **+383%** | 3.24 → 3.79（+17%） |
| 4 MiB | 0.85 GiB/s | **2.53 GiB/s** | **+198%** | 1.45 → 3.12（+115%） |
| 16 MiB | 1.81 GiB/s | **2.83 GiB/s** | **+56%** | 1.24 → 2.81（+127%） |

> 两次测量口径不同：本表是**共享负载**下（当时 loadavg≈11-17，perf-bench 基准 + Kotlin 构建并发）
> 同进程「旧实现 shim vs 新实现」交错的 A/B，绝对值偏低，但**同一次运行内的相对变化**直接说明拷贝被消除；
> 右列是阶段 1 洁净机的绝对基准。`open` 方向的 A/B 在负载下噪声大（±20%），不用于下结论；其收益采信
> 阶段 1 洁净 micro：1 MiB +16%（3.28→3.79）、4 MiB +116%（1.66→3.58）、16 MiB +113%（1.28→2.73）。
> **最终以 perf-bench 的 before/after 为准。**


**验收**：
- 原语：`.testscratch/phase2/bench_phase2.py`（旧 shim 与新实现同进程 A/B，见上表）
- 回归：`tests/test_loopback.py`（含加密传输 + 非加密传输 + 损坏修复用例）全 PASS；
  `test_resilience.py`、`test_concurrency.py` 全 PASS（§3）
- 字节一致性：回归套件内部做 sha256 全文比对（loopback）与多块校验（concurrency）

**风险**：cryptography 对所有支持版本都接受 buffer 协议（本机 50.0.2 实测 AESGCM/ChaCha20 都通过）；
若未来换掉 cryptography 实现需保留类型检查。无协议影响。

---

### T4 · fsync/fdatasync 移出 PartFile 锁（`chunkstore.py`）

**改动**：

1. `write_chunk()` 仍然「先写数据、再追加 journal 记录」（**顺序不变**），但不再在锁内做任何
   阻塞 I/O；锁内只更新位图/计数/`os.write(journal 记录)`。
2. 新增**后台 syncer 线程**（每文件最多 1 个，daemon，`eversend-part-sync`）：
   - 每 `JOURNAL_FLUSH_INTERVAL=1 s` 做一次 journal `fsync`（与改前同节奏）；
   - 每 `JOURNAL_SYNC_INTERVAL=10 s` 做一次数据 `fdatasync`（与改前同节奏）；
   - 实际 fsync 在 `_io_lock` 内执行（只与收尾 flush 互斥，**从不与数据流线程互斥**）。
3. `flush()`（finalize / close 调用）**仍然是阻塞同步**：先停掉后台 syncer，再 `fdatasync(数据)`
   + `fsync(journal)`，保证「完成」= 已落盘（崩溃恢复语义不变）。
4. 小文件（< `SYNCER_MIN_BYTES = 4 MiB`）不启动后台线程：它们远在第一个周期到期前就 finalize，
   `flush()` 已覆盖；避免每个小文件 create/join 一个线程。
5. `_maybe_flush(force=True)` 保留为「立即同步」的兼容入口；`tools/bench_transfer.py:543` 的
   `_prof_wrap(chunkstore.PartFile, "_maybe_flush", "disk_sync")` 仍然可用（该阶段现在≈只调度，
   **perf-bench 请注意**：disk_sync 时间不再是 fsync 时间，见 §6 交接）。

**崩溃恢复语义**：不变。数据先于 journal 记录写入；journal 每 1 s fsync 的节奏不变（最多丢最后
1 s 的记录，重传那些块即可）；收尾 `flush()` 保证完成文件的数据 + journal 都已落盘。区别只是
这些同步操作发生在后台线程，而不是持锁阻塞所有接收流。

**改前/改后（确定性慢盘实验：把 `fdatasync` 注入 250 ms 延迟，4 写线程 × 32 块 × 256 KiB）**：

**(a) 功能 / 续传一致性**：

| 检查 | 结果 |
|---|---|
| 4 线程并发写 32 块（32 MiB） | `received_bytes=33554432`，逐块 CRC 校验通过；`syncer_started=True`, `after_close=False`（线程正确回收） |
| flush 后重开续传 | 重开后 `received_bytes == size`（journal 记录完整、可恢复） |
| 写一半后「中断」再重开 | 中断前 8 MiB → 重开读到 8 MiB → 补齐 16 MiB → 逐块 CRC 一致 |

**(b) 慢盘尾延迟 A/B**（注入每次 fdatasync 250 ms；4 线程 × 32 块 × 256 KiB，日志 `.testscratch/phase2/logs/t4-final.log`）：

| 实现 | 全部写完 wall | p50 单块写延迟 | p99 | max |
|---|---|---|---|---|
| 旧：锁内同步 fdatasync | 19.16 s | 1043.9 ms | **11817.6 ms** | 11817.6 ms |
| 新：后台 syncer | **0.004 s** | 0.25 ms | **0.70 ms** | 0.70 ms |

> 旧实现一旦「fsync 慢于同步间隔」就进入级联阻塞：每个写线程拿到锁后又要等 250 ms 的 fsync，排队时间
> 爆炸（p99 11.8 s）；新实现的写入路径完全不碰 fsync（p99 0.7 ms）。这正是高延迟/慢盘/多流并发下
> 「进度条周期性卡死」的根因。


**其他验收**：
- 并发写入 + flush + 重新打开续传：`test_partfile_sync.py` T4.1/T4.2 全 PASS（见 §3 表）
- 真实中断续传：`test_loopback.py` 的「artificial interruption → resume」用例 PASS；
  `test_resilience.py`（每 0.35 s 随机 RST 一条连接的代理）PASS
- 取消清理：`test_loopback.py` 取消用例 PASS（`.part/.journal` 被回收）

**风险**：新增后台线程的生命周期由 `close()/flush()` 的 stop+join 管理（join 超时 10 s）；
`__slots__` 同步增加字段；Windows 上 `fdatasync` 不存在时走 `fsync`（`PositionedFile.sync`
原有逻辑）。最坏情况：进程被 `SIGKILL` 时最后 ≤10 s 的数据 `fdatasync` 未做——与改前相同
（改前 10 s 间隔同样如此），journal 记录最多晚 1 s，恢复后按 CRC/摘要修复。

---

### T8 · 加密模式接收端不再重算 CRC32（`transfer.py` + `connection.py`）

**改动**：

- `connection.ChunkFrame` 新增 `aead_verified: bool = False`；`Connection.recv_data()` 在
  `encrypted` 分支出 `aead_verified=True`（该 chunk 的每个字节都已被 AEAD tag 认证）。
- `transfer.ReceiveSession._store_chunk()`：仅当 `not frame.aead_verified` 时才 `crc32(frame.data)`
  并与 wire CRC 比较；**wire CRC 仍原样写进 journal**（`part.write_chunk(..., frame.crc)`），
  所以：
  - **AEAD 关闭（NullCipher）会话：CRC 校验照旧**，飞行中的位翻转仍然被捕获/重传；
  - **坏块修复**：`repair_scan()` 用 journal 里保存的 CRC 对盘上数据重算并定位坏块 —— 与本次改动
    完全无关（`chunkstore.py:297-311`），`test_loopback.py` 的「corruption repair」用例继续通过。
- 发送端仍计算 CRC（wire 字段必须填），未改。

**改前/改后（8 MiB 加密/非加密真实传输中统计 crc32 调用次数与 `aead_verified` 标志）**：

8 MiB 文件、2 流、每块 1 MiB（共 8 块）的接收端统计：

| 会话 | crc32 调用次数 | `aead_verified` 块数 | 非 AEAD 块数 | 结果 |
|---|---|---|---|---|
| AEAD 开（AES-256-GCM） | **8**（只剩发送端每块 1 次） | 8 | 0 | sha256 一致 |
| AEAD 关（NullCipher） | **16**（发送端 + 接收端各 1 次/块） | 0 | 8 | sha256 一致 |

→ 加密会话接收端每块的重复 CRC 计算被跳过，但 wire CRC 仍按原值写入 journal（修复扫描要用）；
未加密会话的校验**完全保留**。`test_loopback.py` 的「corruption repair」用例（改坏盘上数据 → 重发 →
按 journal CRC 定位坏块修复）继续 PASS。

**profile 侧佐证**（`profile_pipeline.py`，256 MiB / 4 流 / 加密开；"改前"为阶段 1 审计同一脚本的输出）：

| 指标 | 改前 | 改后 | 判断 |
|---|---|---|---|
| `transfer.crc32` 调用次数 | 512（发送 + 接收各 256 块） | **256（只有发送端）** | 接收端重复校验已消除 |
| AEAD seal / open 调用次数 | 558 / 549 | 542 / 538 | 未变（符合预期） |
| PartFile 锁内 fsync/fdatasync | `_maybe_flush` 内直接执行 | **0 次**（`_maybe_flush` 只负责调度后台线程） | T4 生效 |

> 注：profile 里 `fdatasync(.part)` 仍有 2 次/文件、每次约 1.3 s —— 那是 **finalize 的收尾强制落盘**
> （T4 要求保留，保证「完成」= 已落盘；慢盘上仍会占用收尾时间，属阶段 1 §10 的 T4b 后续项，不在本任务范围）。
> 它现在发生在收尾线程，**不再阻塞数据流线程、也不再持 PartFile 锁**。


**CPU 换算**（阶段 1 实测 crc32 = 2.6 GiB/s）：每 2 GiB 接收端省 **≈0.77 CPU·秒**；
回环/2.5GbE/弱 CPU 场景更明显，千兆 LAN（2 GiB ≈17.4 s）约占单核 4%。

**风险**：低。若未来把 AEAD 换成“只加密不认证”的模式必须重新打开校验（当前三种路径都认证：
AES-GCM / ChaCha20-Poly1305 / NullCipher 走 CRC）。

---

### T5 · 每块 O(n) 扫描 → dict 索引 + import 上提（`transfer.py`）

**改动**：

- `SendSession.__init__` 新增 `self._entry_by_index: dict[int, FileEntry]`；
- `_send_one_chunk()` 内两处 `next(e for e in self.entries if e.index == file_index)`
  （一处在 `_reader_lock` 内）→ `self._entry_by_index.get(...)`；
- `_digest_for()` 同样替换；
- 删掉 `_send_one_chunk` 函数体内的 `from .constants import chunk_range`，改为模块顶部 import
  （热路径每块都会执行一次 import 机制）。

**改前/改后（2048 块/2 GiB 折算；多文件数场景）**：

2048 块（= 2 GiB 文件）的查表成本（`.testscratch/phase2/bench_phase2.py`）：

| 文件数 | 旧：每块线性扫描 | 新：dict 索引 | 加速 |
|---|---|---|---|
| 1 | 0.53 ms | 0.16 ms | 3.3x |
| 10 | 1.25 ms | 0.11 ms | 11x |
| 100 | 7.49 ms | 0.11 ms | 68x |
| 1000 | 80.84 ms | 0.13 ms | **622x** |

另外去掉函数体内的 `from .constants import chunk_range`：2048 次调用 0.46 ms → 0.046 ms（10x）。
合计：单文件 2 GiB 只省 ~0.4 ms（可忽略），但**1000 文件目录传输省 ~81 ms/2 GiB**，且随文件数线性放大。


**风险**：零（纯查表重构；`_entry_by_index` 用同一份 `entries` 构建）。

---

### T10 · socket 缓冲回读自检（`sockutil.py` + `connection.py`）

**改动**：`sockutil.tune_socket()` 在设置 `SO_SNDBUF/SO_RCVBUF` 后调用既有的
`actual_socket_buffers()`（该函数基线里就有、但从未被使用）回读内核实际授予值，写入
`sock.eversend_buffers = {requested, sndbuf, rcvbuf, capped}`；`EVERSEND_DEBUG` 下打印一行诊断
（含 `kernel-capped` 提示与 sysctl 建议）。`Connection.__init__` 把它快照到
`Connection.socket_buffers` 供引擎/诊断读取。**不阻断启动、不改行为**。

**诊断示例（本机实测）**：

| 请求 | 内核实际授予 | capped |
|---|---|---|
| 8 MiB（`SOCKET_BUFFER_SIZE`，本机） | snd=8 MiB, rcv=8 MiB | False（本机 `wmem_max=4 MiB`，内核把请求折算后 8 MiB 生效） |
| 1 GiB（演示内核截断） | snd=8 MiB, rcv=8 MiB | **True** |

`EVERSEND_DEBUG=1` 时的诊断行示例（本机实测）：

```
[eversend] socket buffers: requested=1073741824 actual snd=8388608 rcv=8388608  (kernel-capped; raise net.core.wmem_max/rmem_max if this matters for high-RTT links)
```

引擎/诊断可通过 `Connection.socket_buffers`（`{"requested", "sndbuf", "rcvbuf", "capped"}`）读取该快照；
默认（未开 DEBUG）不打印任何东西、不阻断启动。


**为什么重要**：默认 Linux 的 `net.core.wmem_max` 通常只有 ~208 KiB，`SOCKET_BUFFER_SIZE=8 MiB`
的请求会被内核静默截断；阶段 1 审计里"单流吞吐莫名卡在 100 Mbps"的那类问题从此有一条现场证据。

---

### T11 · AEAD 算法协商（向后兼容，已同步 PROTOCOL.md）

**问题重述**：改前两端各自按本机 CPU 选 AEAD 且**不协商**：x86-64/ARM64 选 AES-256-GCM，
32 位 ARM（`armv7l`）选 ChaCha20-Poly1305。两者会话密钥相同但算法不同 → 第一个加密帧
（AUTH）就 `InvalidTag` → 表现为「握手成功、传输 100% 失败」。

**方案（低风险、后向兼容）**：

- `HELLO` 增加**可选** `"cipher": "aes256gcm" | "chacha20poly1305"`（发起方本地偏好）；
- 响应方若认识该算法（`SUPPORTED_ALGORITHMS`）就采用，并在 `HELLO_ACK` 回显同一个值；
  双方都用这个算法建会话（`crypto.negotiated_algorithm()`）；
- 旧对端：JSON 忽略未知字段、不发送 `cipher` → 新端 `negotiated_algorithm(None)` 回退本地偏好，
  **行为与改前完全一致**；不需要提升 `PROTOCOL_VERSION`；
- 没有任何路径会因此退化成“不加密”：不支持就按本地选择，加密照旧。

**兼容矩阵**（已写入 `docs/PROTOCOL.md` §3.6）：

| 组合 | 同架构 | 跨架构（AES vs ChaCha） |
|---|---|---|
| 新 ↔ 新 | ✅ 协商一致 | ✅ 协商一致（跟随发起方偏好） |
| 新发起 ↔ 旧响应 | ✅ 同上（旧端忽略未知字段） | ❌ 旧端不协商（与旧↔旧相同） |
| 旧发起 ↔ 新响应 | ✅ 新端收不到 `cipher` → 回退本地 | ❌ 同上 |
| 旧 ↔ 旧 | ✅ | ❌ |

**A/B 验证（线程角色模拟两种架构：响应方线程偏好 AES、发起方偏好 ChaCha）**：

| 场景 | 结果 |
|---|---|
| A 旧逻辑（各端按本机选）跨架构模拟 | **失败**：`ok=False` —— 发起方实际装 ChaCha20-Poly1305、响应方装 AES-256-GCM，复现「握手成功、传输 100% 失败」 |
| B 新协商（同模拟） | **成功**：`ok=True`、sha256 一致；6 条连接的会话算法两端都 = `chacha20poly1305`（跟随发起方偏好） |
| C 旧对端回退 | `negotiated_algorithm(None)` / `("bogus")` 都回退本机偏好 AES-256-GCM；显式 `chacha20poly1305` 被采纳 |
| D 两端都偏好 ChaCha | **成功**：`ok=True`、sha256 一致，两端都 ChaCha |

原始输出：`.testscratch/phase2/logs/t11-final.log`。


**风险**：`cipher` 字段与 `encrypt` 一样不在签名转录内（明文协商信息），不改变身份认证安全性质；
最坏情况是中间人做算法降级——但只能在两个**都已认证且都安全**的 AEAD 之间选，且不降低机密性
（与"去掉 encryption 就退化"不同）。若要更严格可将来把它绑进 transcript（属于协议变更，不在本次范围）。

---

## 3. 回归结果

| 套件 | 命令 | 结果 |
|---|---|---|
| 单元级 T4 | `python3 .testscratch/phase2/test_partfile_sync.py` | **PASS**（并发写 / flush 落盘 / 中断续传 / 慢盘 A/B） |
| 单元级 T1/T5/T8/T10 | `python3 .testscratch/phase2/bench_phase2.py` | **PASS**（全部检查通过，数字见 §2） |
| 单元级 T11 | `python3 .testscratch/phase2/test_cipher_negotiation.py` | **PASS**（A 复现失败 / B、D 成功 / C 回退正确） |
| 端到端内核 | `EVERSEND_MBPS_FLOOR=5 python3 tests/test_loopback.py` | **43/43 PASS**（含取消、续传、损坏修复、加密/非加密） |
| 恶劣网络 | `python3 tests/test_resilience.py` | **32/32 PASS**（随机 RST、8 MB/s 限速、512 MiB 持续、取消回收） |
| 并发 | `python3 tests/test_concurrency.py` | **7/7 PASS**（多传输并发、nonce 复用检查、无静默截断） |

> 关于一次「假红」：首轮全量回归里 `test_resilience.py [1] Connections killed mid-transfer` 挂了 3 项
> （传输在 0.5 s 内结束、proxy 只来得及 kill 1 次）。当时机器 loadavg≈17（perf-bench 基准 + Kotlin 构建
> + 其它专家同时在跑）。单独重跑该用例 **3/3 PASS**（6.0-6.7 s、8 kills、sha 一致），随后整套 resilience
> 再连跑 2 次 **32/32 PASS** → 判定为共享机器负载 flake，**不是**本次改动的回归；真实结论以 perf-bench
> 在安静机器上的 before/after 为准。


---

## 4. 收益场景说明（避免误解：千兆 LAN 已经打满网线）

阶段 1 审计实测：**AEAD 开/关端到端无差别（110.1 vs 110.5 MiB/s）**，千兆 LAN 的物理上限是
118 MiB/s —— 所以本次改动的收益**不体现在千兆 LAN 的峰值速度**上。它们的收益场景是：

| 场景 | 哪一项收益最大 | 量级 |
|---|---|---|
| **2.5GbE / 10GbE 局域网**（CPU 成为瓶颈） | T1（去 2 次全量拷贝）、T8（接收端去 CRC） | 2 GiB 传输 ~1-2 CPU·秒；10GbE 下可达墙钟可见 |
| **本机回环 / 同一台电脑两个窗口互传** | T1、T4、T10（回环是 CPU/系统调用受限，不是网络受限） | 阶段 1 原型 A/B：T1 中位 +8%、T4 上界 +19.9%（负载污染，方向性） |
| **弱 CPU / 低功耗设备**（J1900、老笔记本、ARM 板） | T1、T8 | 加密/校验 CPU 占比越高收益越大 |
| **多流并发 + 大文件/多文件传输** | T4（fsync 不再阻塞所有流）、T5（文件数多时 O(n) 扫描） | T4 慢盘上每 10 s 一次的全流停顿消失；T5 1000 文件时 ~5 ms/2 GiB |
| **高延迟/抖动链路（WiFi/VPN）** | T4（尾延迟）、T10（可诊断缓冲被截断） | 稳定性/可观测性，非峰值 |
| **手机端经 web 层**（`web/server.py`，不直接用 EVSD） | **当前收益为零**（web 层不经过 core 的 AEAD/CRC/chunkstore）；但同一台电脑的 CPU 被 EVSD 传输占得更少，对并发任务有间接好处 | — |

---

## 5. 与 perf-bench 的交接

1. **before/after 请用 `opt-1017/baseline`（`e18c079`）vs 当前工作树**：工作树已含本任务全部 6 项，
   未 commit；`git stash`/`git worktree` 方式请 Lead 定（本任务禁 commit）。
2. **`tools/bench_transfer.py:543` 的 `disk_sync` 阶段语义已变**：`_maybe_flush` 现在只负责调度
   后台 syncer（`_maybe_flush(force=True)` 才同步）。建议 perf-bench：
   - 想看「落盘耗时」→ 直接 profile `PositionedFile.sync` 或 `PartFile.flush`；
   - 想看尾延迟 → 记录每块 `write_chunk` 的延迟分布（P99/max），这正是 T4 的收益指标；
   - 想看 CPU → 进程 `utime+stime` / 传输字节（T1/T8 的收益会体现在这里）。
3. 两组文件系统（tmpfs vs btrfs）：**tmpfs 上 T4 几乎没有收益**（tmpfs 的 fsync 是空操作），
   这是预期结果——T4 的收益在真实磁盘（尤其 HDD/网络盘/写回压力大的 btrfs）上。
5. **profile 的 `disk_sync` 阶段同上**：`tools/bench_transfer.py:543` 把 `_maybe_flush` 视作落盘耗时；
   现在它是"调度后台 sync"，耗时应≈0。要落盘时长请直接 profile `PositionedFile.sync`/`PartFile.flush`，
   要 T4 收益请记录每块 `write_chunk` 的 p99/max（见 §2 T4(b) 的确定性实验）。
6. `tests/test_resilience.py [1]` 在共享负载下出现过一次 0.5 s 早停 flake（详见 §3 表注），安静机器上
   复测应稳定 PASS；若 perf-bench 也偶发，请先看 loadavg 而不是当作传输回归。
4. 建议 before/after 各跑 ≥5 轮取中位数并记录 `/proc/loadavg`：阶段 1 已看到共享机器上单次离群
   可达 10 倍（55 s vs 5 s），单次采样会得出错误结论。

---

## 6. 未做项（按审计结论与任务说明）

| 项 | 决定 | 理由 |
|---|---|---|
| T2 AEAD GIL | 不做 | 已查明 AEAD 释放 GIL；瓶颈是对象内 Mutex，而产品每连接独立对象 → 多流无互堵 |
| T3 chunk 大小策略 | 不做 | 改公式=协议不兼容（旧端算错偏移拒绝块）；且实测 16 MiB 块反而 -71% |
| T6 窗口自适应 | 不做（留提案） | 高 RTT 的干净测量还没拿到（阶段 1 netem 三轮均被队列/丢包伪影污染）；没有可信数据前不动调度参数。阶段 1 §10 把它列为"先测后改" |
| sendmsg | 不动 | 已是 gather sendmsg + 首次探测回退（Windows），实测比 `sendall(concat)` 快 +9.8% |

---

## 7. 复现命令

```bash
cd /home/xgl/我的项目/自制软件/韧传/eversend
export PYTHONPATH=src EVERSEND_TEST_TMP=$PWD/.testscratch/phase2/run

# 单元级验证（T1/T5/T8/T10 + T4 + T11）
python3 .testscratch/phase2/bench_phase2.py
python3 .testscratch/phase2/test_partfile_sync.py
python3 .testscratch/phase2/test_cipher_negotiation.py

# 回归（共享机器上吞吐门槛放宽；真实吞吐交给 perf-bench）
EVERSEND_MBPS_FLOOR=5 python3 tests/test_loopback.py
python3 tests/test_resilience.py
python3 tests/test_concurrency.py

# 一键跑全部
bash .testscratch/phase2/run_all.sh
```

原始数据：`.testscratch/phase2/{bench-phase2-results.json,t4-results.json,t11-results.json}` +
`.testscratch/phase2/logs/*.log`。
