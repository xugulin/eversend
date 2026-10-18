# 传输内核底层审计：EVSD 协议 / 加密 / 文件 IO / 并发 / GIL

> 任务：`task-1`（transport-core，阶段 1 只读审计）
> 基线：`e18c079`（2026-10-04 企业级改造完成状态），分支 `main`，备份 `opt-1017/baseline`
> 审计日期：2026-10-17 晚（与 Lead 真机基线并行执行，机器有其他专家负载，见 §2 噪声控制）
> 产物：本报告 + `.testscratch/transport-bench/{micro.py,e2e_sweep.py,profile_pipeline.py}`（实验脚本，未进产品代码）
> 说明：本阶段**没有修改任何产品代码**；所有结论都带可复现命令与原始数字。

---

## 0. 摘要（给 Lead 的 3 分钟版）

**基线**：`tests/test_loopback.py` 全部 PASS（其中 64 MiB/4 流吞吐首个采样 72 MiB/s；洁净扫描见 §2.2）；
`test_resilience.py` 32/32 PASS；`test_concurrency.py` 7/7 PASS（均退出码 0）。
洁净 e2e（4 流 / 1 MiB 块 / AEAD）**中位数 110-129 MiB/s、最好 164-167 MiB/s**（机器被多专家共用，
单次 11 s / 55 s 外部卡顿各出现一次，见 §2.2 噪声说明）。

**前三大底层问题**（全部是「不改协议、只改实现」可解；数字均为本机实测）：

| # | 问题 | 位置 | 实测证据 | 预期收益（2 GiB 折算见 §10） |
|---|---|---|---|---|
| **T1** | AEAD 每次 seal/open 都 `bytes(plaintext)` / `bytes(ciphertext)` 全量复制（cryptography **完全接受 memoryview**） | `crypto.py:445-452` | 4 MiB 块 seal **1.45 → 3.12 GiB/s（+115%）**、16 MiB 块 1.24 → 2.81（+127%）、1 MiB 块 3.24 → 3.79（+17%）；每方向每字节省 1 次全量拷贝 | CPU：2 GiB 省 **~0.18 CPU·s**（1 MiB 块）/ **~1.5 CPU·s**（4 MiB 块）。LAN 千兆墙钟 <2%（网络受限）；本机回环/2.5GbE/弱 CPU 才显著 |
| **T2** | ~~AEAD 不释放 GIL~~ **已核实并纠正**：cryptography AEAD **会释放 GIL**（自旋探针：AEAD 运行时纯 Python 线程仍保持 73.5% 迭代），但 **AEAD 对象内部有 Mutex**：同一个 AESGCM 对象多线程共享会串行化（4 线程 2.05-2.26 GiB/s vs 1 线程 2.70-2.95 = 0.68-0.77x）。**产品从不共享**：`connection.py:341-365` 每条 TCP 连接 `establish_session()` 建独立 Cipher（独立 AESGCM 对象），一连接=一线程=一对象 | `crypto.py:406-452` | 共享对象 0.68-0.77x；每线程独立对象：AESGCM seal 2.94→2.44 GiB/s（0.83x，仍不扩展）、ChaCha per-thread 0.93→2.00 GiB/s（2.16x）；GIL 自旋探针 1 线程 73.5% / 4 线程 43.5% | **结论：AEAD 不是多流的 GIL 堵点**；AESGCM 因每次 4 MiB 输出分配+拷贝在 GIL 下仍只有 0.83x，`CipherContext.update_into` 写复用缓冲可再省 2 次拷贝/块（单线程实测仅 +5%，价值主要在免分配，需阶段 2 验证多流扩展） |
| **T3** | chunk 大小推导实际恒为 1 MiB：`size//4096` 对 <4 GiB 文件全部被 clamp 到 `MIN_CHUNK_SIZE`；`MAX_CHUNK_SIZE=16 MiB` 只有 >64 GiB 文件才触发 | `constants.py:106-119` | 2 GiB 文件=2048 块；16 MiB 块实测反而 **4x 变慢**（32.2 MiB/s，CPU 反而仅 0.49x → 不是 CPU 是窗口/流水线：16 MiB 块时窗口公式只给 2） | 块大小是**协议兼容项**（改公式→旧客户端算错偏移并拒绝块）；不建议 v1 动；低风险替代是压缩每块固定开销（T4-T8），或 v2 加"块大小协商" |

**CPU 账本（回答 Lead 追问 2：哈希路径核实）**：EVSD 用 **BLAKE2b-256**（`hashing.py:41`，不是 sha256）。
正常路径**每字节恰好 2 次哈希**：发送端读块时增量折叠 1 次（`transfer.py:2320`），接收端收块时增量折叠 1 次
（`transfer.py:1210-1212`）；**没有额外整文件读一遍**（摘要直接从运行中的 hasher 取，`transfer.py:2214-2216`，`_digest_for`）。
只有增量哈希溢出（>64 MiB 乱序，`hashing.py:104,119`）或校验失败修复时才整文件重读一遍（`transfer.py:1252` → `_digest_fd`）。
blake2b 单线程仅 **0.76 GiB/s** → 2 GiB 传输 **≈5.2 CPU·秒**（两端合计），是 CPU 账本最大单项，但它释放 GIL（4 线程 3.37x）不阻塞并发。
**协议兼容性判断**：digest 是 `MSG_FILE_DIGEST` 帧内容（blake2b-256 hex），换算法=与旧版不兼容；EVSD 只用于桌面/CLI 互传
（Android App 走 HTTP，不在 EVSD 链路上）。→ **建议不动**（网络受限场景哈希不是瓶颈；同版本两端可换但收益有限且风险>收益）。

**优先级建议（阶段 2）**：见 §10（top5，含 2 GiB 折算 CPU 秒 / 墙钟秒、风险、验收方法）。

---

## 1. 审计范围与方法

- **代码**：`src/eversend/core/{framing,connection,transfer,chunkstore,hashing,crypto,fileio,sockutil,engine,server,discovery}.py`
  与 `docs/PROTOCOL.md`、`docs/ARCHITECTURE.md`；手机链路对照读 `src/eversend/web/server.py`（只读，不改）。
- **既有测试基线**：`tests/test_loopback.py`、`tests/test_resilience.py`、`tests/test_concurrency.py`
  （`EVERSEND_TEST_TMP=$PWD/.testscratch/transport-bench` 隔离到项目大盘）。
- **自写 micro-bench**（`.testscratch/transport-bench/micro.py`）：逐原语 GB/s、线程扩展（GIL 释放与否）、
  `pwrite` 模式、`sendmsg` vs `sendall`、socket 缓冲、AES-GCM 零拷贝替代。
- **自写 e2e 参数扫描**（`.testscratch/transport-bench/e2e_sweep.py`）：真实 `Engine` 对跑 256 MiB，
  扫 流数 1/2/4/8/16、块大小 1/2/4/8/16 MiB、AEAD 开/关、窗口字节数；每配置 1 次预热 + ≥3 次取中位数，
  每次校验接收文件 sha256。
- **高延迟模拟**（`e2e_sweep.py --phase latency`）：用户命名空间 + `tc netem delay 20ms`（RTT≈40 ms），
  测窗口×流数曲线。**不需要 root、不影响宿主网络**：
  `unshare -rn bash -c 'ip link set lo up; tc qdisc add dev lo root netem delay 20ms; ...'`（本机实测可行）。
- **逐阶段 CPU 归属**（`.testscratch/transport-bench/profile_pipeline.py`）：对真实传输的
  pread/crc32/blake2b/AEAD/sendmsg/recv_into/pwrite/journal 分别累计墙钟与调用次数。

---

## 2. 基线与噪声控制

### 2.1 既有测试（首次基线）

命令：

```bash
cd /home/xgl/我的项目/自制软件/韧传/eversend
export EVERSEND_TEST_TMP=$PWD/.testscratch/transport-bench PYTHONPATH=src
python3 tests/test_loopback.py     # 全部 PASS（0 failures），吞吐见下
python3 tests/test_resilience.py   # 32 项 PASS
python3 tests/test_concurrency.py  # 7 项 PASS
```

- `test_loopback.py`：**全部 PASS**。其中「[6] End-to-end throughput (64 MiB, 4 streams)」：
  `64 MiB in 0.89s = 72 MiB/s`（floor=60）。
- `test_resilience.py`：全 32 项 PASS，退出码 0。
- `test_concurrency.py`：7 项 PASS，退出码 0（含"整块都收到了，没有被静默截断"）。

> 噪声说明：这三个测试与 Lead/其它专家的实例并行跑，且 `test_loopback.py` 的 `Rig` 使用默认
> `enable_multicast/broadcast/mdns/scan=True`，后台发现逻辑会偷 CPU；因此 72 MiB/s 只作为"能过线"的证据，
> **协议成本用 §2.2 的洁净配置扫描（发现/扫描全关、预热缓存、≥3 次中位数）**。正式对比一律看 §2.2。

### 2.2 洁净 e2e 扫描（发现/广播/mDNS/扫描全关，256 MiB，中位数）

| 配置（256 MiB，每次 sha256 校验） | 中位数 MiB/s | 三次原始值 | 备注 |
|---|---|---|---|
| 基线：4 流 / 1 MiB 块 / AEAD 开（L0） | 110.1 | 90.2 / 129.9 / 110.1 | 第一个配置，机器较静 |
| 1 流 | 118.7 | 104.8 / 118.7 / 134.0 | 单流已接近饱和（同进程 GIL 限制） |
| 2 流 | **164.5** | 164.6 / 166.7 / 125.0 | 测得最高值（外部负载低谷） |
| 4 流 | 106.2 | 112.9 / 106.2 / 105.4 | |
| 8 流 | 111.2 | 111.1 / 108.8 / 113.2 | 加流不再涨 → 同进程 CPU/GIL 饱和 |
| 16 流 | 111.9 | 114.1 / 108.8 / 111.9 | 同上 |
| 块 1 / 2 / 4 / 8 MiB（4 流） | 120.2 / 114.9 / **126.8 / 128.8** | 最高 137.1 | 4-8 MiB 块略优（+7~17%），见 T3 |
| 块 16 MiB（4 流） | **32.2** | 55.1 s(4.6) / 7.95 s(32.2) / 7.17 s(35.7) | **塌陷 4x**：16 MiB 块 + 窗口=2 块；CPU 仅 0.49x → 等待/流水线问题（§7.5） |
| AEAD 关（4 流 / 1 MiB） | 110.5 | 108.3 / 110.5 / 147.5 | **与 AEAD 开无差别** → 该速度下 AEAD 不是瓶颈 |
| 窗口 2 / 4 / 8 / 16 MiB（=块数 2/4/8/8） | 112.8 / 76.0 / 65.8 / 68.0 | — | **L4 整段被外部负载带偏**：win8MiB 与基线同配置却只有 65.8 → 本组无效，窗口效应见 §2.3 |

> **测量条件**：每配置 1 次预热 + 3 次测量取中位数；发现/广播/mDNS/扫描全关；源文件已被页缓存；
> 每次删除接收文件并校验 sha256。**关键 caveat**：这是**同进程双 Engine**（两个引擎跑在同一个 Python
> 解释器里），8+ 线程共享 1 个 GIL；真实桌面↔桌面是两台机器、两个进程、各自独立 GIL。所以本表数字：
> ① 对"同机回环/本机两窗口互传"直接有效；② 对千兆 LAN 只能作下限参考（真实 LAN 双机 118 MiB/s 线速即可打满）；
> ③ 对"协议 CPU 成本"有参考意义（§4 的原语数字才是硬证据）。
>
> **噪声证据**：`chunk_8MiB` 第三次出现 11.0 s（=23.2 MiB/s）离群；`chunk_16MiB` 首跑 55 s；
> `winbytes_16MiB` 首跑 55 s —— 同时段其它专家在跑重负载（机器 16 核共享）。所有结论都尽量用"同配置/对照"
> 或多次重复来验证，单次离群值不用于下结论。



### 2.3 高延迟（netem 40 ms RTT）窗口×流数

**结论先行**：本机 netem 实验**不能作为生产结论**（共享机器 + netem 队列参数会引入丢包/排队伪影，见 §7.6），
但它暴露了两个需要阶段 2 在干净环境复测的真问题：① 40 ms RTT 下吞吐远低于 `W×chunk/RTT` 的理论值；
② 窗口给大之后出现了**整次传输失败**（`ok=False`），不是变慢而是失败。

| 实验 | 参数 | 结果 | 判读 |
|---|---|---|---|
| Round 1（64 MiB） | `netem delay 20ms`（默认 limit=1000 包），窗口 2/4/8 MiB × 流 1/2/4 | 多数配置 22-46 MiB/s；**win2/1流 与 win2/2流 出现 `ok=False`**（11.5 s、59.8 s 卡死后失败） | limit 太小 → 1 MiB 突发被丢包 → TCP 重传/RTO；失败说明**超时后整次传输会放弃**（chunk deadline 8 s） |
| Round 2（128 MiB） | netem limit=100000（防丢包） | 跑到 3 分钟未完成，吞吐塌陷 | 队列过大引入 bufferbloat/内存压力，测试无效（已终止） |
| Round 3（16-32 MiB，limit=30000，单流窗口 1/2/4/8） | `netem-curve.py` | **7.4 / 0.5（失败）/ 14.9 / 8.1 MiB/s**；win2 的那次 32.6 s 后失败 | **吞吐与窗口不成正比**、且出现失败 → 说明瓶颈不是「窗口太小」而是丢包/超时（netem 队列伪影或真实 RTO），必须干净环境重测才能下结论 |

> 复现（需要干净环境 + 合适的 netem limit，limit 必须 ≥ 窗口+1 个 chunk 的包数，推荐用 `rate` 而非无限队列）：
> ```
> unshare -rn bash -c 'ip link set lo up && tc qdisc add dev lo root netem delay 20ms limit 30000 && \
>   NETEM_DELAY_MS=20 PYTHONPATH=src python3 .testscratch/transport-bench/netem_curve.py'
> ```
> **阶段 2 建议**：把「40ms RTT + 0.1-1% 随机丢包」作为抗抖验收矩阵的一列，先测「窗口 2/4/8 × 流 1/2/4 ×
> 丢包 0/0.1%」的吞吐与成功率，再决定是否改窗口策略。**不要**拿现在的 netem 数字直接改产品参数。



---

## 3. 线程模型与一次 2 GiB 传输的真实数据通路（含拷贝/syscall 计数）

### 3.1 线程模型（与 ARCHITECTURE.md §3 核对一致，另有实测线程数）

一次 4 流、桌面↔桌面传输的实际线程（两端合计，`/proc/self/task` 实测见 §2.2 `threads_peak`）：

| 线程 | 数量 | 说明 |
|---|---|---|
| accept 循环 | 每监听 socket 1（通常 1 个 TCP 监听） | `server.py:112 _accept_loop`，每个入站连接再起 1 个 `peer-*` 线程 |
| 入站连接处理 | 每连接 1 | `server.py:140 _serve`：握手 → 第一条帧是 OFFER / ATTACH / CHAT / PING |
| 接收集成控制读线程 | 每会话 1 | `transfer.py:815 _control_loop` |
| 接收数据流工作线程 | 每数据流 1 | `transfer.py:889 stream_worker`（拉取式：发 CHUNK_REQ→收 CHUNK→校验→pwrite→记账） |
| 发送方流服务线程 | 每数据流 1 | `transfer.py:2008 _serve_stream`（收 CHUNK_REQ→读盘→CRC→哈希→AEAD→sendmsg） |
| 发送方 supervisor | 每会话 1 | `transfer.py:1957 _supervise_streams`（补流/重连） |
| 摘要/报告短任务 | 临时 | `_request_digest`/`_finalize_file`/`_broadcast_report` |

**关键事实**：阻塞 socket + 线程，不用 asyncio；所有热路径都在线程里同步循环。
- `hashlib.update()`（blake2b）>2047 B 释放 GIL —— 实测 4 线程 3.37x 扩展 ✅
- `zlib.crc32` 实测也释放 GIL（4 线程 3.31x）✅
- **`cryptography` 的 `AESGCM.encrypt/decrypt`（高层 AEAD）实测不释放 GIL**（4 线程合计 0.67x，见 §4.3）❌
  → 同一进程内 N 条流做 AEAD 会互相排队，且线程越多 convoy 越重。

### 3.2 发送侧：一个 chunk 的真实路径（加密模式，1 MiB 块）

`transfer.py:2264 _send_one_chunk()` ← `_serve_stream` 收到一条 `CHUNK_REQ`：

```
① 每块（且仅每块一次）：
   transfer.py:2276  with self._reader_lock:        # 全局读锁（每块进一次）
   transfer.py:2294  next(...) 线性扫 entries        # O(文件数)
   transfer.py:2295  from .constants import chunk_range   # 函数体内 import
   transfer.py:2297  chunk_range(...)               # 纯计算
   transfer.py:2299  FileReader.read_chunk()        # → fileio.py:144 os.preadv
② transfer.py:2314  crc32(view)                     # zlib，读一遍内存
③ transfer.py:2320  hasher.update(offset, view)      # blake2b，读一遍内存
④ connection.py:411 send_chunk()
   connection.py:436   Cipher.seal(data, aad)        # crypto.py:448  bytes(plaintext) ★拷贝2
   connection.py:469   sendmsg_all([帧头, 块头, body])  # framing.py:227；内核 gather
```

**每 1 MiB 的字节拷贝次数（发送侧）**：

| # | 拷贝 | 位置 | 说明 |
|---|---|---|---|
| 1 | 磁盘页缓存 → 用户缓冲 | `fileio.py:144 os.preadv` | 不可避免（除非 sendfile/splice） |
| 2 | 用户缓冲 → **新 bytes** | `crypto.py:448 bytes(plaintext)` | **可去**（cryptography 接受 memoryview，实测） |
| 3 | 明文 → 密文输出缓冲 | cryptography 内部 | 不可避免（AEAD 输出新缓冲；`update_into` 可写进复用缓冲，但实测只快 5%） |
| 4 | 用户缓冲 → 内核 socket 缓冲 | `sendmsg(2)` | 不可避免（iovec gather 已是当前最优） |

即 **4 次/字节**，其中 1 次纯冗余。syscall ≈ 4-6 次/块（recv 请求 1-2、preadv 1-2、sendmsg 1-2、控制帧偶发）。

### 3.3 接收侧：一个 chunk 的真实路径（加密模式，1 MiB 块）

`transfer.py:889 stream_worker` 循环：

```
① 发请求： conn.send_json(MSG_CHUNK_REQ)               # connection.py:376；每块 1 条 JSON 帧
   ↑ 窗口维护：transfer.py:889-893 window = clamp(8MiB/chunk, 2, 8) = 8（1 MiB 块）
② conn.recv_data()                                    # connection.py:526
   connection.py:539  _read_header()                  # framing.py:192  bytearray(12)+recv_into+bytes()
   connection.py:557  _recv_into_exact(24 B 块头)      # framing.py:176
   connection.py:569-573 recv_into(1 MiB+16) 进 _chunk_buffer   ★拷贝1
   connection.py:582  Cipher.open(view)               # crypto.py:452 bytes(ciphertext) ★拷贝2 + 解密输出★拷贝3
   connection.py:587  ChunkFrame(memoryview(plain))
③ transfer.py:1184  crc32(frame.data)                  # 再读一遍（AEAD 已认证，冗余）
④ transfer.py:1197  part.write_chunk(...)
   chunkstore.py:272-273  PositionedFile.write_at → os.pwrite  ★拷贝4
   chunkstore.py:280      os.write(journal, 8B)        # 每块 1 次 syscall
   chunkstore.py:282/315  _maybe_flush → 锁内 fsync     # 每 1s/10s 一次（见 T4）
⑤ transfer.py:1199-1202 计数/统计；transfer.py:1208 progress_report()（0.2s 节流）
⑥ transfer.py:1210-1212 hasher.update(offset, view)   # blake2b
```

**每 1 MiB 的拷贝次数（接收侧）**：**4 次/字节**（第 2、3 次纯冗余中的 #2 可去），syscall ≈ 7-9 次/块
（recv 帧头 1-2、块头 1、body 1-4（取决于 8 MiB SO_RCVBUF 与到达节奏）、pwrite 1、journal 1、请求 send 1）。

### 3.4 2 GiB 传输的宏观账本（按实测原语速度估算）

- 2 GiB = 2048 个 1 MiB 块（由 `pick_chunk_size` 实测恒为 1 MiB，见 T3）。
- 每块固定成本 ×2048：3 次 recv 头 + JSON 请求/解析 + 锁 + journal write + 每块 Python 热路径 ≈ 无危险但真实存在。
- CPU 大头（单方向 / 2 GiB）：AEAD seal ≈ 2 GiB / 3.24 GiB/s ≈ **0.62 s**（带 bytes 拷贝） → 去掉后 ≈0.55 s；
  blake2b ≈ 2 GiB / 0.76 GiB/s ≈ **2.6 s**；crc32 ≈ 0.77 s；接收侧 AEAD open(≈0.63 s)+crc32(0.77 s)+blake2b(2.6 s)。
  合计两端 ≈ **8-9 CPU·秒/2 GiB**（不含 socket/磁盘/Python 开销）。
- 千兆 LAN（118 MiB/s）传 2 GiB ≈ 17 s → CPU 只占 ~25%（两端合计），**网络才是瓶颈**；
  本机回环/2.5GbE 才会把瓶颈推到 CPU/GIL（这也是为什么本审计要区分"LAN 现实"与"回环基准"）。

### 3.5 哈希路径核实（Lead 追问 2）

- **算法**：`hashing.py:41` `hashlib.blake2b(digest_size=32)` → BLAKE2b-256（不是 sha256）。SHA-256 在 CPython
  里比 blake2b 更慢，换 sha256 只会更差；换 xxhash 之类快哈希会失去抗碰撞性且是协议不兼容变更。
- **每字节几次**：正常路径 **恰好 2 次**——发送端在 `_send_one_chunk` 里 `hasher.update(offset, view)`
  （`transfer.py:2315-2320`）增量折叠；接收端在 `_store_chunk` 里 `hasher.update(frame.offset, frame.data)`
  （`transfer.py:1210-1212`）增量折叠。**没有"传完再读一遍"的额外整文件哈希**：摘要由运行中的 hasher
  `finish()` 直接给出（发送端 `transfer.py:2214-2216`，接收端 `transfer.py:1248-1250`）。
- **例外（会多一遍全文件读，0.64 GiB/s）**：① 乱序 parking 超过 64 MiB（`hashing.py:104,119-121`）后 hasher
  放弃快速路径，`_finalize_file` 落回 `_digest_fd(part)` 整文件重读（`transfer.py:1252`、`hashing.py:56-73`）；
  ② 整文件摘要失配后的修复轮（`_repair`）用 journal CRC 逐块重读校验（`chunkstore.py:297-311`）。两种都只在
  异常路径触发，2 GiB 传输的代价 ≈ 2.6 CPU·秒 + 一次全文件读盘。
- **协议兼容性**：FILE_DIGEST 帧传的是 blake2b-256 hex，两端必须一致；不是"本地优化"能改的东西。
  **结论：不动算法、不动次数**；只在阶段 2 观察 parking 溢出是否真的发生（若频繁，调大 `max_pending_bytes`
  或改窗口收敛策略比换 hash 划算）。

---

## 4. micro-bench 原始数字（`.testscratch/transport-bench/micro-results.json`）

环境：x86_64，16 核，Python 3.14.7，cryptography 50.0.2，`/home` ext4；每项 ≥3 次取中位数、每次 ≥256 MiB 载荷。

### 4.1 冗余拷贝成本（`bytes(memoryview)`）

| 块大小 | 速度 |
|---|---|
| 1 MiB | 38.6 GiB/s（pymalloc 复用缓冲） |
| 4 MiB | 6.00 GiB/s |
| 16 MiB | 5.19 GiB/s |

→ 4/16 MiB 块时每次拷贝要新分配大对象 + page fault，所以 AEAD 里那行 `bytes()` 的代价远大于 memcpy 本身。

### 4.2 AEAD / 哈希原语（单线程，GiB/s）

| 原语 | 1 MiB 块 | 4 MiB 块 | 16 MiB 块 |
|---|---|---|---|
| AESGCM seal（现行 `bytes()` 路径） | 3.24 | 1.45 | 1.24 |
| AESGCM seal（直接 memoryview） | **3.79** | **3.12** | **2.81** |
| AESGCM open（`bytes()`） | 3.28 | 1.66 | 1.28 |
| AESGCM open（memoryview） | **3.79** | **3.58** | **2.73** |
| ChaCha20-Poly1305 seal（memoryview） | 1.11 | 1.10 | 0.99 |
| zlib.crc32 | 2.51 | 2.63 | 2.62 |
| hashlib.blake2b | 0.76 | 0.76 | 0.76 |
| `file_digest` 重读 256 MiB 文件 | 0.64 | — | — |

注：本机 CPU 有 AES-NI，`_prefer_aesgcm()` 选中 AES-GCM（正确）；无 AES-NI 的平台会落到 ChaCha20 的 ~1.1 GiB/s，
**此时 bytes() 拷贝占比更小（AEAD 本身更慢）**，T1 的收益按绝对值等比缩小。

### 4.3 多线程扩展：GIL 与 AEAD 对象锁（**已用两轮实验纠正第一版结论**）

第一版只测了"所有线程共享同一个 AESGCM 对象"，那测到的是 cryptography 内部 Mutex 的竞争，不是产品路径。
产品的做法是**每连接一个独立 Cipher 对象**（`connection.py:341-365`），所以补测"每线程独立对象"。

**第一轮（共享一个 AESGCM 对象；`micro_cipher_gil.py`）** —— 会误导：

| 原语 | 1 线程 | 4 线程合计 | 扩展比 |
|---|---|---|---|
| AESGCM seal（共享对象） | 2.70 GiB/s | 2.05-2.28 GiB/s | **0.76-0.85x** |
| AESGCM open（共享对象） | 2.67 GiB/s | 2.18 GiB/s | 0.81x |
| ChaCha20 seal（共享对象） | 1.04 GiB/s | 1.93-2.06 GiB/s | 1.81-1.98x |

**GIL 自旋探针**（一个纯 Python 计数线程与 AEAD 线程并行 1.2 s，看计数保持率）：
`idle 100%` → `AESGCM.seal 86.9%`、`AESGCM.open 87.3%`、`ChaCha20.seal 97.4%`
→ **AEAD 确实释放 GIL**（否则自旋线程会被饿死），第一版"AEAD 不释放 GIL"的结论是错的，原因是共享对象 Mutex。

**第二轮（每线程独立对象 = 产品路径；`micro_cipher_gil2.py`）**：

| 配置 | 1 线程 | 2 线程 | 4 线程 | 8 线程 | 扩展比 |
|---|---|---|---|---|---|
| AESGCM seal 每线程对象 | 2.94 GiB/s | 3.06 | 2.34 | 2.44 | 0.80-1.04x |
| ChaCha20 seal 每线程对象 | 0.93 GiB/s | 1.90 | 1.97 | 2.00 | **2.05-2.16x** |

GIL 自旋探针（每线程对象）：1 个 AESGCM 线程 73.5%、4 个 AESGCM 线程 43.5%、4 个 ChaCha 线程 19.6%。

**结论（写给阶段 2）**：
1. **不存在"AEAD 把 GIL 锁死"的问题**；多流不会被 AEAD 串行化（每连接独立对象）。
2. AESGCM 在多线程下仍只有 ~0.8x 扩展，原因更像是**每块 4 MiB 输出缓冲的分配 + 拷贝发生在 GIL 下**（不是密码运算本身）。
   若阶段 2 要追高吞吐，正确方向是 `CipherContext.update_into` 写进复用缓冲（§4.6 单线程 +5%，但它同时去掉
   每块一次 4 MiB 输出分配，可能改善多流扩展——**待阶段 2 实测验证，不要现在就当成收益**）。
3. `crc32` 4 线程 3.31x、`blake2b` 4 线程 3.37x —— 这两个大块 C 函数确实释放 GIL，多流可并行。
4. **纪律**：永远不要跨连接共享 Cipher/AESGCM 对象（共享 = 0.68-0.77x 退化 + nonce 锁）。当前实现正确。

### 4.4 落盘### 4.4 落盘（`/home` ext4，预分配 512 MiB，末尾 fdatasync）

| 模式 | MiB/s |
|---|---|
| pwrite 256 KiB ×2048 | 936 |
| pwrite 1 MiB ×512 | **954** |
| pwrite 4 MiB ×128 | 869 |
| pwrite 8 MiB ×64 | 1015 |
| pwrite 16 MiB ×32 | 1049 |
| pwrite 8 MiB 聚合写 ×64 | 1000 |

→ **块大小从 256 KiB 到 16 MiB，落盘吞吐差 <20%；1 MiB 块没有明显劣势**（真正贵的是 fsync，见 T4，
以及接收端每块一次 `write` + 一次 journal `write` 的 syscall）。所以 T3"块太小"的代价主要不在磁盘，而在
每块固定 CPU/syscall/协议开销。

### 4.5 socket 路径

| 场景 | 吞吐 |
|---|---|
| socketpair `sendmsg([hdr,body])` | 2531 MiB/s |
| socketpair `sendall(hdr+body 拼接)` | 2305 MiB/s（sendmsg 快 +9.8%，代价是拼接拷贝） |
| TCP 回环 内核默认缓冲 | 1647 MiB/s |
| TCP 回环 SO_SNDBUF/RCVBUF=256 KiB | 2972 MiB/s（**默认缓冲的 1.8 倍**） |
| TCP 回环 1 MiB / 4 MiB / 8 MiB | 3005 / 3122 / 3041 MiB/s（1 MiB 以上无差别） |
| TCP 回环 8 MiB + TCP_NODELAY=0 | 3557 MiB/s（Nagle 关掉更快，但 1 MiB 大写入本就无碍） |

→ 回环上 sendmsg/缓冲/NAGLE 的差异 **全部 ≫ 千兆 LAN 的 118 MiB/s**，**不是 LAN 瓶颈**；
但解释了"同一台机器两进程回环传输"为什么能到 GB/s 级而当前 e2e 只有 ~百 MiB/s —— 瓶颈在 Python/GIL/协议固定成本，不在 socket。

### 4.6 AES-GCM 解密的零拷贝替代（评估）

| 方式 | 速度 |
|---|---|
| `AESGCM.decrypt`（分配输出+拷贝） | 3.81 GiB/s |
| `CipherContext.update_into(preallocated)` | 3.99 GiB/s（+5%） |

→ 只值 +5%，**不建议为它增加复杂度**；T1（去 `bytes()` 输入拷贝）才是大头。

### 4.7 密码套件：产品实际用哪个 AEAD（Lead 追问 1）

- **选择逻辑**：`crypto.py:512-513` `_prefer_aesgcm()`：`x86_64/amd64/i386/i686/aarch64/arm64/armv8*` → `aes256gcm`；
  其余（典型如 32 位 ARM `armv7l`）→ `chacha20poly1305`。本机 x86_64 → 产品实际路径是 **AES-256-GCM**。
- **本项目所有支持的桌面平台**（Win x86_64 / Linux x86_64 / Linux aarch64 / Windows ARM64）都命中 AESGCM；
  ChaCha 只在 32 位 ARM 或冷门架构上出现。所以 §4.2/4.3 的 AESGCM 数字是产品主路径。
- **发现一个潜在兼容性缺陷（T11）**：握手的 HELLO/HELLO_ACK 里**没有 cipher 协商字段**（`connection.py` 只发
  `proto/device/x25519/ed25519/nonce/encrypt`），两端**各自按本机 CPU 选算法**。若一端 x86_64（AESGCM）、
  另一端 32 位 ARM（ChaCha）→ 所有加密帧 tag 校验失败，传输 100% 失败且报错是"chunk authentication failed"。
  当前目标平台不受影响，但属于"换个设备就炸"的隐患；修复是握手加一个 additive 的 `"cipher"` 字段并校验
  （旧端忽略未知字段 → 向后兼容），成本很低。
- **GIL 结论按算法分开**：AESGCM 对象内部有 Mutex（共享对象会串行）、密码运算释放 GIL；ChaCha20-Poly1305 的
  GIL 释放更彻底（自旋保持率 19.6% 说明大量时间在释放状态），共享对象也能到 1.8-2.0x（因为密码本身慢，
  Mutex 临界区外的时间占比更高）。产品每连接独立对象，两者都不构成多流堵点。

### 4.8 A/B 原型：不改产品代码，用 monkeypatch 测两个候选修复的端到端效果

脚本 `prototype_ab.py`（monkeypatch 只在测试进程内生效，产品代码零改动）：
A=原样；B=`Cipher.seal/open` 直传 memoryview（T1）；C=B + 锁内 fsync/fdatasync 置空（T4 上界，
把"落盘不阻塞接收线程"的收益上限测出来）。256 MiB / 4 流 / 1 MiB 块 / AEAD 开，每轮 A→B→C 交错执行。

| 轮次 | A 基线 | B（T1） | C（T1+T4 上界） |
|---|---|---|---|
| round0 | 46.2 MiB/s（5.54 s） | 38.2（6.71 s） | 55.4（4.62 s） |
| round1 | **4.6（55.75 s，外部卡顿）** | 49.9（5.13 s） | 57.2（4.48 s） |
| round2 | 54.9（4.66 s） | 74.7（3.43 s） | **4.4（57.53 s，外部卡顿）** |
| **中位数** | **46.2** | **49.9（+8.0%）** | **55.4（+19.9%）** |

**判读**：方向与 §4.2 的原语数字一致（B 省拷贝、C 再省锁内落盘），但本机当时被其它专家重负载
干扰（A/C 各出现一次 55-57 s 的整次卡顿；同期 e2e 也从 ~110 MiB/s 掉到 ~46 MiB/s），
**这些百分比不能当最终收益**。阶段 2 应在安静机器上复用该脚本跑 ≥5 轮 A/B，取中位数并记录
`/proc/loadavg`；验收标准建议：B 的 CPU·秒下降 ≥10%、C 的尾延迟（P99 chunk 间隔）显著下降。

---

## 5. 结论表：底层可优化项（按「收益 × 风险」排序）

格式：**编号｜位置｜机制｜预期收益｜风险｜验证命令｜低风险标记（不改协议只改实现）**

| 编号 | 文件:行号 | 机制（为什么慢/不稳） | 实测收益（2 GiB 折算） | 风险 | 验证命令 | 不改协议/低风险 |
|---|---|---|---|---|---|---|
| **T1** | `crypto.py:445-452` | `Cipher.seal/open` 内 `bytes(plaintext)/bytes(ciphertext)` 全量复制；4/16 MiB 块时还触发新分配+缺页 | 原语 4 MiB seal +115%、16 MiB +127%、1 MiB +17%；2 GiB **省 ~0.17 CPU·s**（1 MiB 块）/ **~1.4 CPU·s**（4 MiB 块）。**原型 A/B 实测中位 +8.0%**（256 MiB，噪声大见 §4.8） | 低 | `micro.py` B 段；`prototype_ab.py`；`test_loopback.py`+`test_resilience.py` 全 PASS | ✅ |
| **T2** | `crypto.py:406-452`、`connection.py:341-365` | **不是 GIL 问题**：AEAD 释放 GIL；但 AEAD **对象内部 Mutex**：共享对象跨线程串行（0.68-0.77x）。产品每连接独立 Cipher → 正确。 | 无直接收益；**纪律**：永不复用/共享 Cipher 对象 | 低 | `micro_cipher_gil.py` / `micro_cipher_gil2.py` | ✅（纪律） |
| **T3** | `constants.py:106-119` | `TARGET_CHUNKS_PER_FILE=4096` 使 <4 GiB 文件恒得 `MIN_CHUNK_SIZE=1 MiB`；每块固定开销被摊薄 8-16 倍 | 4/8 MiB 块 LAN 内 +7~17%；16 MiB 块 **-71%**（窗口=2 + 大分配）→ 改公式收益不确定 | **高**（协议不兼容：旧端算错偏移拒绝块） | 改 `PROTOCOL_VERSION` 后两端同测 | ❌ 阶段 2 不做 |
| **T4** | `chunkstore.py:275-282,315-328` | 每块在 `PartFile._lock` 内 `os.write(journal)`；每 1 s 锁内 `fsync(journal)`、每 10 s 锁内 `fdatasync(.part)` → **所有接收流一起停** | profile 实测：256 MiB 传输 2 次 fdatasync **合计 2.4-2.5 s**（每次 1.23 s，wall 3.7 s）；原型 C 中位 **+19.9%**（含离群，方向一致） | 低-中（挪锁外/后台 flush，顺序语义不变） | `prototype_ab.py`；`test_resilience.py`；慢盘复测 | ✅ |
| **T4b** | `chunkstore.py:330-340` + `transfer.py:1214+ finalize` | 文件完成时同步 `flush()`→`fdatasync` 全部脏页；2 GiB 单文件结束时会**卡在"完成"瞬间**（用户可见） | 200 MiB 级可能 0.5-2 s；2 GiB 视 writeback 状态 2-10 s | 中（durability 语义要设计） | 传输结束时刻日志/UI 时间戳；profile 的 fdatasync 时长 | ✅（体验向） |
| **T5** | `transfer.py:2294-2295`（另 `:2283` 在锁内） | 每块 `next(e for e in entries if e.index==i)` O(文件数) 线性扫 + 函数体内 `from .constants import chunk_range` | 2048 块 × 1-3 µs ≈ 4-10 ms/2 GiB（文件多时放大）；纯零风险清理 | 低 | 单测 + 1000 小文件传输对比 | ✅ |
| **T6** | `transfer.py:889-893`、`constants.py:334` | 窗口= `clamp(8MiB/chunk, 2, 8)` **块**，上限 8 块；高 RTT 下吞吐 = W×chunk/RTT，固定窗口打不满高 BDP 链路 | 40 ms RTT 理论 200 MiB/s（W=8,1MiB），实测远低（§2.3，环境混杂待复测）；**干净环境待证** | 中（调度/内存上界） | §2.3 netem 矩阵；`test_resilience.py` | ✅（配置/自适应，先测） |
| **T7** | `framing.py:192-215`、`connection.py:539-573` | 每块 3 次 recv（12 B 帧头 + 24 B 块头 + body）+ 每帧 `bytearray(12)`/`bytes()` 小分配 | 2048×2 次 syscall/2 GiB ≈ 几 ms（太小，随 T1 顺手做） | 低 | `micro.py` / 代码审查 | ✅（低优先） |
| **T8** | `transfer.py:1184`（接收端）、`:2314`（发送端） | AEAD tag 已保证完整性，接收端仍对每块重算 crc32（2.6 GiB/s）；AEAD 关闭时该 CRC 才有独立价值 | 2 GiB 接收端 **0.77 CPU·s**（约占 LAN 传输 4.4% 单核）；回环/快网更显著 | 低（仅跳过校验；CRC 仍按 wire 值存 journal 供修复扫描） | 加密传输 sha 一致；`test_loopback` 修复用例（非加密/篡改路径） | ✅ |
| **T9** | `hashing.py:41` | blake2b 0.76 GiB/s，两端各 1 遍 = 2 GiB **5.2 CPU·s**；释放 GIL；换算法=协议不兼容；sha256 更慢 | 无法在不改协议下降低 | 高（协议） | 不需要 | ❌ 不动 |
| **T10** | `sockutil.py:388-421` | `SO_SNDBUF/RCVBUF=8 MiB` 请求后从不 `getsockopt` 回读；默认 sysctl 下会被截到 ~200 KiB 级，无人知道 | 无直接吞吐收益（本机实测 LAN 不受限）；**可观测性**：把"单流莫名卡 100 Mbps"变成一条诊断 | 低 | 启动后事件/日志里回读值 vs 请求值 | ✅ |
| **T11** | `crypto.py:512-513` + 握手（`connection.py:179+`） | AEAD 算法按本机 CPU 选（x86_64→AESGCM，armv7l→ChaCha），**握手没有 cipher 协商字段** → 跨架构两端算法不一致时所有加密帧 tag 失败，传输 100% 失败 | 当前 Win/Linux x86_64、aarch64、Win ARM64 都不受影响；32 位 ARM ↔ x86_64 必炸 | 低（当前平台）/ 修复=握手加 additive `"cipher"` 字段（向后兼容） | 交叉启动（强制 `_prefer_aesgcm` False）测一次 | ✅ |



---

## 6. 手机 App ↔ 电脑的 HTTP 链路 vs EVSD（只读对比，未改 web/server.py）

任务要求对比"手机走 HTTP"与"桌面走 EVSD"的差距。实读 `src/eversend/web/server.py`（3988 行）：

### 6.1 手机→电脑上传：`POST /api/upload`（原始 body）

- 路由与目标分类：`server.py:2679-2680` → `_receive_upload()`（`server.py:3308`）。
- 目标三分类：`self`（本地落地）/`client`（发布到分享表等手机来取）/`peer`（真发 EVSD 协议）；
  手机→电脑 app 的常见情况是 `self`，即**只写盘、不走 EVSD**。
- body 流式落盘：`_accept_upload_body()`（`server.py:3284-3306`）：
  `open(spool, "wb", buffering=0)` + `_iter_body()`（`server.py:2771-2785`）按 **`IO_CHUNK = 256 KiB`**
  （`server.py:86`）从 `rfile` 读 → 无缓冲 `handle.write(chunk)`（每 256 KiB 一次 write syscall）。
- 没有：每块 CRC、整文件 BLAKE2b/摘要校验、上传断点续传（异常时 `os.unlink(spool)` 删掉从头来）、
  多流/并行、AEAD。只有一个 Content-Length 预检（剩余空间，`server.py:3349-3358`）。
- 对比 EVSD：EVSD 每块 CRC32 + 整文件 BLAKE2b + 断点续传 + 修复 + 可选 AEAD + 4 流拉取。
  **HTTP 链路的完整性依赖 TCP 校验和与磁盘；一段上传失败=整文件重传。**

### 6.2 电脑→手机下载：`GET /api/download` / Range

- `_stream_file()`（`server.py:3113-3184`）：单 Range、`open(path,"rb")` 后按 `IO_CHUNK=256 KiB`
  循环 `handle.read` → `self.wfile.write(block)`；`wfile` 为无缓冲 socket 文件对象，即每 256 KiB 一次 write。
- 有 Range 续传（对下载端是加分项），但同样**单连接单流、无整文件摘要、无逐块校验**。
- 对比 EVSD：Range 续传只覆盖"下载"方向；且 Range 续传的正确性没有内容校验兜底。

### 6.3 结论（不改代码，交给阶段 2 决策）

1. **手机链路的稳定性/速度上限不在加密/IO 内核，而在 HTTP 单请求模型**：单连接 + 256 KiB 读 + 无并行。
   在 WiFi 上单个 Python 线程做 `rfile.read/write` 通常还能跑满 WiFi，但抗抖动能力弱（一次 RST 整文件重来）。
2. **完整性是最大差距**：上传没有摘要，无法回答"这个文件真的完整吗"；EVSD 的 CRC+BLAKE2b 不能直接搬到 HTTP，
   但可以在 `/api/upload` 完成时**增量算一遍摘要并落 sidecar/状态**（客户端已在算？见 android 侧任务），
   这样 GUI 的"已完成"才有可信语义 —— 这条属于 gui/android 的范围，这里只指出传输内核能力缺口。
3. 若阶段 2 要提速手机链路，**最低风险项**是：把 `IO_CHUNK` 从 256 KiB 提到 1 MiB（`server.py:86`，
   减少 syscall 数与 Python 循环次数；实测本机 pwrite/read 在 256 KiB→1 MiB 有 ~2% 提升、syscall 数 ×1/4）；
   `buffering=0` 的 spool 写入保持（避免二次拷贝）；`wfile` 前加 `sendall` 级缓冲对 256 KiB 块意义不大。
   **但这属相邻链路，按分工留给 Lead 决定是否派人做。**

---

## 7. 稳定性风险清单（与速度无关、但值得阶段 2 验证）

1. **`fsync` 在锁内（T4）**：慢盘/HDD/网络盘上，每 10 s 一次全量 `fdatasync` 期间所有接收流一起停，
   进度条/ETA 会看到周期性"卡一下"；接收端取消/崩溃会触发更多 fsync。
2. **高延迟下窗口≠BDP（T6）**：0.67x 的 AEAD 扩展 + 8 MiB 固定窗口，在 RTT 抖动大的 WiFi 上
   可能表现为"速度掉一半后再也不回升"；自适应窗口/流数需要把"实测 RTT/吞吐"接进调度。
3. **chunk deadline = max(8s, chunk/512KiBps)=8 s**（`constants.py:278-284`）：链路瞬时吞吐低于
   `1 MiB/8 s = 128 KiB/s` 时，1 MiB 块会被误判超时→换流重试（重试有 6 次上限，最坏情况换到控制连接），
   在极弱 WiFi/热点共享场景可产生"越慢越重试"。建议把 deadline 与实测吞吐挂钩（滑动平均）而不是固定 floor。
4. **`IncrementalFileHasher` 溢出→整文件重读**（`hashing.py:104,119-121`；`transfer.py:2319`）：
   乱序>64 MiB 时发送方停止增量哈希，接收方 `_finalize_file` 落回 `_digest_fd` 整文件再读一遍
   （2 GiB 额外 ≈2.6 CPU·秒 + 一次全盘读）。当前窗口/顺序发放下很少触发，但断流重排后会。
5. **零拷贝承诺与实际不符**（§3.2/3.3）：文档 `ARCHITECTURE.md §4` 说"发送方零分配/接收方零拷贝"，
   但加密路径实际每字节 2 次冗余/额外拷贝。这是文档与实现的偏差，建议 T1 修复后同步修文档。
6. **socket 缓冲"设了也没生效"（T10）**：默认 sysctl 下 `SO_SNDBUF=8MiB` 被截到 ~4 MiB（本机）或 ~200 KiB（常见默认），
   代码没有任何自检/告警。高延迟单流吞吐会莫名受限且无法归因。
7. **内存上界**：每接收流 1 个 `_chunk_buffer`（=chunk+tag）+ 发送每流 1 个 thread-local 块缓冲 + 哈希 parking 上限 64 MiB/文件，
   4 流 ≈ 4-6 MiB + 64 MiB（最坏）；16 流 16 MiB。**有界，无泄漏风险；但 64 MiB parking 可再调小**（超了就走重读，重读代价 0.64 GiB/s，权衡即可）。

8. **文件完成瞬间的同步落盘（T4b）**：`PartFile.close()/flush()` 会在传输收尾时对整份 `.part`
   做 `fdatasync`。profile 里 256 MiB 传输的 2 次 fdatasync 各 **1.23 s**（合计 2.4-2.5 s，占 wall 3.7 s 的 2/3）——
   即使其中一部分是当时的并行盘负载所致，"接收线程在收尾时同步刷全部脏页"这一结构在 2-10 GiB 大文件上
   必然表现为「进度 100% 后卡几秒」。需要阶段 2 复测并决定：后台落盘 + 完成回执分离，或减少强制 flush 频率。
9. **高延迟/丢包下会整次失败，不只是变慢**：§2.3 的三轮 netem 实验里多次出现 `ok=False`（发送方报错/放弃，
   接收端文件不完整）。触发路径是 chunk deadline（`constants.py:278-284`，`max(8 s, chunk/512KiBps)`）
   超时 → 换流重试 → 重试预算耗尽后整次传输失败。**netem 参数本身有伪影（丢包/排队）**，因此这不是对产品的
   定罪，而是"必须列入抗抖验收矩阵"的证据：40 ms RTT + 0.1% 丢包下单流/多流的**成功率**比吞吐更重要。
10. **16 MiB 块的 4x 塌陷（§2.2）**：16 MiB 块时窗口公式只给 2 块，实测 32 MiB/s（4 流）；CPU 仅 0.49x、
    wall 7-8 s，说明线程在等而不是在算。这是反例：**不能靠"把块改大"来提速**（块大小还是协议兼容项）。
    阶段 2 若要动块策略，先做逐块间隔归因（profile 脚本可加"chunk 完成时间线"）。
11. **加密算法未协商（T11）**：`_prefer_aesgcm()` 在两端各自选择，握手无 cipher 字段；x86_64 与 32 位 ARM
    （armv7l→ChaCha）之间所有加密帧会 tag 失败。当前目标平台不受影响，但属于"换台设备就 100% 失败"的隐患，
    修复成本低（握手加 additive 字段）。

---

## 8. 复现命令（全部可复制粘贴）

```bash
cd /home/xgl/我的项目/自制软件/韧传/eversend

# 0) 既有基线（隔离临时目录）
export EVERSEND_TEST_TMP=$PWD/.testscratch/transport-bench PYTHONPATH=src
python3 tests/test_loopback.py
python3 tests/test_resilience.py
python3 tests/test_concurrency.py

# 1) 原语 micro-bench（AEAD/哈希/pwrite/socket/GIL 扩展）
PYTHONPATH=src python3 .testscratch/transport-bench/micro.py

# 2) 洁净 e2e 参数扫描（流数/块大小/AEAD/窗口；约 3-4 分钟）
PYTHONPATH=src python3 .testscratch/transport-bench/e2e_sweep.py --phase lan --reps 3

# 3) 高延迟窗口曲线（netem 20ms ≈ RTT 40ms，无需 root）
unshare -rn bash -c 'ip link set lo up && tc qdisc add dev lo root netem delay 20ms && \
  NETEM_DELAY_MS=20 PYTHONPATH=src python3 .testscratch/transport-bench/e2e_sweep.py --phase latency --reps 2'

# 4) 逐阶段 CPU 归属（可选）
PYTHONPATH=src python3 .testscratch/transport-bench/profile_pipeline.py
```


# 5) 密码套件 / GIL 两轮验证（产品实际 AES-256-GCM；共享对象 vs 每线程对象）
PYTHONPATH=src python3 .testscratch/transport-bench/micro_cipher_gil.py
PYTHONPATH=src python3 .testscratch/transport-bench/micro_cipher_gil2.py

# 6) A/B 原型（monkeypatch T1/T4，不改产品代码；建议安静机器 ≥5 轮）
PYTHONPATH=src python3 .testscratch/transport-bench/prototype_ab.py

# 7) netem 高延迟实验（本机 3 轮均受队列/丢包伪影影响，仅作阶段 2 复测脚本模板）
unshare -rn bash -c 'ip link set lo up && tc qdisc add dev lo root netem delay 20ms limit 30000 && \
  NETEM_DELAY_MS=20 PYTHONPATH=src python3 .testscratch/transport-bench/netem_curve.py'

原始数据：`.testscratch/transport-bench/{loopback-baseline.log,resilience-baseline.log,concurrency-baseline.log,micro.log,micro-results.json,e2e-results.json,profile-results.json}`。

---

## 9. 未测/限制声明

- 未在 **Windows / 安卓原生 EVSD** 上测（本机 Linux；Windows 路径 `fileio.py` 用每线程 lseek 句柄，
  本次未实测其吞吐，代码审查未见额外拷贝但**未测**）。
- 高延迟/丢包：netem 实验共 3 轮，均被本机共享负载 + netem 队列参数伪影污染（见 §2.3、§7.9），**未得到可下结论的高 RTT 窗口曲线**；丢包/乱序/限速（netem loss/duplicate/reorder）未系统测，留给阶段 2。
- 真实 WiFi 链路由 Lead 的 MI9 真机测试覆盖（本审计不含真机驱动，ADB 写操作归 Lead）。
- `AEAD memoryview`（T1）与 `fsync 出锁`（T4）的端到端数字只有 §4.8 的原型 A/B（受负载污染，方向性）；最终收益必须在阶段 2 修改后、安静机器上复测。

## 10. 阶段 2 优先级建议（回答 Lead 追问 3）

排序口径：**不改协议、低风险、可量化收益**。所有"2 GiB 折算"基于本机实测原语速度；
LAN 场景以千兆 118 MiB/s（2 GiB≈17.4 s）为参照，快网/回环收益按比例放大。

| 优先级 | 项 | 改动点 | 预期收益（2 GiB 折算） | 风险 | 验收方法 |
|---|---|---|---|---|---|
| **P0-1** | **T1 crypto memoryview** | `crypto.py:445-452` 去掉 2 个 `bytes()`（2 行） | CPU **-0.17 CPU·s/2 GiB**（1 MiB 块；若块 4 MiB 则 -1.4 CPU·s）；LAN 墙钟 <1%，本机回环/2.5GbE/弱 CPU 更明显；§4.8 原型中位 +8%（受污染，方向性） | **最低** | `test_loopback`+`test_resilience` 全 PASS；加密/非加密、AESGCM/强制 ChaCha 三种模式各 1 GiB 随机文件 sha 一致；`prototype_ab.py` 安静机器 ≥5 轮 |
| **P0-2** | **T4 锁内 fsync 移出/异步化** | `chunkstore.py:275-282,315-340`：journal 写与 fsync 分离，数据 fdatasync 改后台/降低频率；保持"先写数据后记 journal"顺序 | profile 实测 256 MiB 传输 **2.4-2.5 s 在 fdatasync 上**；§4.8 原型 C 中位 **+19.9%**（含离群，方向一致）；慢盘/HDD 放大 | 低-中（durability 语义、后台线程生命周期） | `test_resilience.py`（RST 中断+续传）PASS；`test_loopback` 取消/续传 PASS；新加"完成尾延迟"测量：记录最后一块到 done 事件的间隔，目标 <100 ms |
| **P0-3** | **T8 AEAD 下跳过接收端 crc32** | `transfer.py:1184` 外包一层 `if not frame.aead_verified:`（`Connection.recv_data` 已有 `encrypted` 信息，加个标志即可；wire 上 CRC 字段不动、journal 仍存） | 接收端 **-0.77 CPU·s/2 GiB**（≈LAN 传输 4.4% 单核）；回环/快网更明显 | 低（AEAD 已认证；非加密路径保持原校验；修复扫描仍用 journal CRC） | 加密 1 GiB sha 一致；`test_loopback` 的"corruption repair"（非加密路径）PASS；强制 AEAD off 回归 |
| **P0-4** | **T5 每块 O(n) 扫描 + 函数内 import** | `transfer.py:2294-2295` 改 dict 索引、`chunk_range` 提到模块顶部（另 `:2283` 锁内扫描/`_digest_for:2207` 同型） | 2048 块 ≈ 4-10 ms/2 GiB（文件多时×文件数）；**零风险清理** | 低 | 单测；1000 小文件目录传输对比；`test_loopback` 多文件用例 |
| **P0-5** | **T10+T11 可观测性/兼容性** | T10：`sockutil.py:388-421` 末尾 `getsockopt` 回读并写诊断事件；T11：握手加 `"cipher"` 字段并校验（缺省回退本机选择） | 无法折算秒数；避免"默认 sysctl 下 8 MiB 调优静默失效"无法归因、"跨架构换设备 100% 失败"两类支持成本 | 低（T11 加字段需走协议兼容评审） | 启动日志/诊断事件含实际缓冲值；强制 `_prefer_aesgcm=False` 模拟 32 位 ARM 与正常端互连成功 |

**对 Lead 倾向清单的核对（明确说"做/不做"）**：

- ✅ `crypto bytes()` 拷贝 → **做（P0-1），最高确定性**。
- ✅ `chunkstore 锁内 fsync` → **做（P0-2）**，但注意它主要是**尾延迟/稳定性**收益，吞吐收益在安静机器上需复测。
- ⚠️ **"GIL/哈希归属"**：已查明——AEAD **不是** GIL 堵点（会释放 GIL，且产品每连接独立对象）；blake2b 是最大 CPU 单项
  （5.2 CPU·s/2 GiB）但**释放 GIL 且网络受限时不构成瓶颈**；换算法=协议不兼容。→ **阶段 2 不把它当速度项做**；
  若目标是本机回环/2.5GbE+，再考虑 `CipherContext.update_into` 免分配（P2，需先干净环境验证多流扩展）。
- ⚠️ `pick_chunk_size 策略` → **不建议在 v1 做**：改公式=与旧客户端不兼容；且实测 16 MiB 块 **-71%**，
  大块不是免费的。若要做只能作为 v2"块大小协商"，需产品版本决策。
- ❌ `sendmsg` → **不需要动**：`connection.py:453-475` 已是 gather sendmsg + 一次探测回退（Windows），
  §4.5 实测 sendmsg 比 `sendall(concat)` 快 +9.8%；瓶颈不在这里。

**阶段的建议顺序**：P0-1 → P0-4（同一文件顺手）→ P0-3 → P0-2（需要设计 durability）→ P0-5；
每步都跑 `test_loopback.py` + `test_resilience.py`，并用 `.testscratch/transport-bench/prototype_ab.py` 的口径复测。
