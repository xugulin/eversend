# 韧传 EverSend · 稳定性 / 速度 / GUI 显示准确性彻底优化报告（opt-1017）

> 时间：2026-10-17（基线 commit `e18c079` = 2026-10-04 企业级改造完成态）
> 范围：**从最底层（socket / 协议 / 加密 / 文件 IO / 并发）到安卓端与桌面/网页 GUI**，
> 先用真机（MI 9 / Android 16）建立基线 → 多专家审计定位根因 → 实现修复 → 真机复测。
> 判定立场：**界面说什么不算证据**；只认磁盘 sha256、服务端字节计数、`/api/state` 真值、logcat 系统事件。

---

## 0. 一页结论（TL;DR）

| 维度 | 改前（真机基线） | 改后（本轮交付） | 证据 |
|---|---|---|---|
| **手机→电脑发送** | 电脑端重启后 **100% 失败**（CSRF 令牌过期 + 刷新在主线程抛 `NetworkOnMainThreadException`，自动恢复永不生效） | 令牌过期→IO 线程刷新→自动重试；真机 A7 文件上传 + sha256 校验 PASS | `real_device_mi9-v2.md`、`android-fixes.md` |
| **熄屏/切后台保活** | **前台服务从未启动**；App 掉 cached(400) 后系统 `Destroyed live tcp sockets`，UDP 发送全部 `EPERM`（每轮 256 条，累计 4000+ 条） | 启动即 FGS（`specialUse` + WifiLock/PartialWakeLock + 真心跳）；**60s 熄屏实验 EPERM 256/轮 → 0 条**，电脑端全程 `online=True` | `android-fixes.md §2` |
| **大文件断点续传** | 上传/下载都是"一次请求"，中途断网全废（真机 100MiB 传到 73MiB abort 后从 0 重来） | 上传：新增 `/api/upload` 续传协议（202/409/status/幂等，旧客户端兼容）；下载：Range 续传 + 本地 `.part` 缓存重试，真机实测 offset 不回退 | `test_http_resume.py` 20 项、`android-fixes.md` |
| **传输内核 CPU/IO** | AEAD 前后 2 次全量拷贝；fsync 在 PartFile **锁内**（慢盘 p99 达 11.8s）；加密会话接收端重复 crc32；每块 O(n) 扫描 | AEAD 零拷贝（1MiB seal **0.76→3.67 GiB/s** 负载下 A/B）、fsync 后台线程（注入慢盘 p99 **11.8s→0.70ms**）、crc32 调用 **512→256**、索引 **80.8ms→0.13ms**、socket 缓冲自检、 cipher 协商（消掉跨架构 100% tag 失败隐患） | `transport-fixes.md` |
| **桌面/网页 GUI 准确性** | 接收完成 1 次传输出现 **2 行**（一行 0 B）；接收表**没有进度/速度/ETA**；100% 后仍显示"传输中"数秒；单位 1024 进制标成 KB/MB；页头"未知设备 #hash"；每次接收抛 `UnboundLocalError` | 8 项修复：单行"已完成"、接收表新增 进度/速度/剩余 三列、100% 后显示"校验中…"、KiB/MiB 口径三端统一、页头名称回退链、接收文件真正进会话库、重启恢复记录 | `gui-audit.md`、`gui-fixes.md` |
| **手机 GUI 准确性** | 下载完成 3.7s 后卡片仍"接收中…"、传输中无任何百分比/速度、批量条目一直"正在排队"、会话标题露内部设备 id | v3→v8 四轮修复：单位口径、**会话标题不再露裸 id（PG-5 已修）**、**已完成卡片自愈为"已在其它客户端取走"**（PG-1 部分）；**传输中实时百分比/批量状态仍未达成**（真机探针证据，根因见 §5.4） | `gui-probe-after-v3/v6/v7/v8.md`、真机截图时间线 |
| **真机全量验收** | 旧 APK：A7 FAIL（令牌） | v3 新 APK：**见 §5 验收表** | `acceptance-final.*` |

---

## 1. 方法与分工（多专家联合）

| 阶段 | 做法 | 产出 |
|---|---|---|
| 阶段 0（Lead） | 快照基线 commit `e18c079`/分支 `opt-1017/baseline`；装 APK；真机基线 A0-A8 + 速度采样 + GUI 探针 | `realdevice-baseline.md`、`realdevice-gui-findings.md`、`tools/realdevice_probe.py` |
| 阶段 1（4 专家并行只读审计） | 传输内核（transport-core）／安卓真机（android-device）／三端 GUI（gui-desktop）／性能基准（perf-bench），全部只读、报告落盘、任务板 write-scope 隔离 | `transport-audit.md`、`android-audit.md`、`gui-audit.md`、`bench-baseline.md` |
| 阶段 2（实现） | 每个实现任务带 file:line、验收命令、write scope；并行改 4 个互不重叠的子树（core / android / desktop+web / web-server） | `transport-fixes.md`、`android-fixes.md`、`gui-fixes.md`、`http-resume-server.md` |
| 阶段 3（Lead） | 安静窗口（load<4）全量验收 + 真机复测 + 性能 A/B + 交付报告/经验入库 | `acceptance-final.md`、`bench-before-after.md`、本报告 |

共享基座经验先行：动手前 `jizuo_exp_find` 查（真机 adb / Xvfb / HTTP chunked / 缓存整份覆盖等），
本轮沉淀 5 条坑 + 2 个成功模式回写基座（见 §10）。

---

## 2. 真机基线（改前，旧 APK 2026-09-26 构建）

复现：`EVERSEND_ADB_SERIAL=2430d3c EVERSEND_REAL_WORK=/home/xgl/qa-real DISPLAY=:99 QT_QPA_PLATFORM=xcb python3 tests/test_real_device.py --only A0,A1,A2,A6,A7,A8`

| 用例 | 内容 | 结果 |
|---|---|---|
| A1/A2 | 电脑→手机 512KiB / 100MiB，手机磁盘 sha256 相等 | ✅ PASS |
| A6 | 反作弊：界面说"已收到"时磁盘必须真有且非 0 字节 | ✅ PASS |
| **A7** | 手机→电脑 370KiB PNG（真 DocumentsUI 选文件→发送→电脑磁盘 sha256） | ❌ **FAIL**（令牌主线程 bug，见 §3.3） |
| A8 | 双向中文 + emoji 消息 | ✅ PASS |

速度基线（电脑→手机 100MiB，PC 侧 `sentBytes` 采样，负载 8.9 下）：
**有效传输窗口 4.34s，平均 ≈23.0 MiB/s，稳态 23.8–26.9 MiB/s**；手机点"保存"→首字节 ≈2.6s。
详细数字：`realdevice-baseline.md`（含逐 0.5s 采样原始日志 `qa-real/a2-speed-sample.log`）。

---

## 3. 根因与修复（从最底层往上）

### 3.1 传输内核（EVSD 二进制协议，桌面↔桌面）

审计报告：`transport-audit.md`（545 行，含数据通路/拷贝与 syscall 计数/原语实测）。
实现报告：`transport-fixes.md`（6 项全部落地，未改协议语义）。

| 项 | 根因 | 修复 | 实测收益 |
|---|---|---|---|
| **T1 AEAD 零拷贝** | `Cipher.seal/open` 每次 `bytes(plaintext)`/`bytes(ciphertext)` 全量复制（1-16MiB 块） | 直接把 `memoryview` 交给 cryptography | 1MiB seal 0.76→3.67 GiB/s（负载 A/B）；审计洁净机 4MiB +115%、16MiB +127% |
| **T4 fsync 出锁** | `PartFile.write_chunk` 在**锁内**同步 fsync/fdatasync，慢盘时冻住所有接收流 | 每文件后台 syncer 线程（journal 1s / data 10s 节奏不变，锁外）；finalize 仍阻塞落盘 | 注入 250ms fdatasync 的确定性慢盘实验：p99 **11.8s → 0.70ms**；并发/续传一致性全绿 |
| **T8 重复 crc32** | 加密会话里 AEAD 已认证，接收端仍每块重算 crc32 | `ChunkFrame.aead_verified` 标记；仅非加密会话重算 | 2GiB 省 ≈0.77 CPU·s/接收端；损坏修复用例仍能识别坏块 |
| **T5 O(n) 扫描** | 每块线性扫 entries + 函数内 import | dict 索引 + 顶层 import | 1000 文件/2048 块 80.8ms→**0.13ms** |
| **T10 socket 缓冲自检** | 请求 8MiB 但从不回读实际生效值 | `getsockopt` 回读 + `Connection.socket_buffers` + `EVERSEND_DEBUG` 诊断 | 实际 snd/rcv=8MiB（capped=False），1GiB 请求会截断并标 capped=True |
| **T11 密码套件协商** | 两端各自按 CPU 选 AESGCM/ChaCha，x86_64↔32 位 ARM 会 100% `InvalidTag` | HELLO/HELLO_ACK 可选 `cipher` 字段协商（旧对端忽略→回退本地选择，**不需升协议版本**） | 跨架构模拟 A/B：旧逻辑必失败→新逻辑两端一致且 sha256 相同；`docs/PROTOCOL.md §3.6` 兼容矩阵 |

不做（有依据）：T2 AEAD GIL（实测会释放 GIL，首版误判是共享对象 Mutex）、T3 chunk 公式（协议不兼容且 16MiB 实测 -71%）、T6 window 自适应（缺可信高 RTT 数据）、sendmsg（已是最优 gather）。
回归：`test_loopback 43/43`、`test_resilience 32/32`、`test_concurrency 7/7` 全 PASS。

### 3.2 手机↔电脑 HTTP 链路：可续传上传（结构性缺失）

背景：手机是纯 HTTP 客户端，走 `/api/upload`（原始 body 流式落 spool）。改前**一次请求传完**，
真机 100MiB 传到 73MiB 被系统掐断后已收字节全废、重发从 0 开始。

修复（`src/eversend/web/server.py`，向后兼容）：客户端带 `uploadId`（16-64 位 hex）+ `offset` +
头 `X-EverSend-Total`，body 为剩余字节；服务端固定名 spool 追加写，未收满回 `202 {offset}`，
偏移不符回 `409 {offset}`，完成才走原定稿流程并登记幂等快照；`/api/state` 宣告
`caps.uploadResume/uploadMaxBytes`；`GET /api/upload/status?uploadId=` 供断线后查服务端已有字节；
24h 过期清理、并发同 id 加锁、非法 id 防路径穿越。旧客户端/旧服务端自动退回一次性路径。

验证：新增 `tests/test_http_resume.py` **20/20 项通过**（三段续传 sha256、409 恢复、完成幂等重放不重复落盘、
旧路径回归、非法参数、status 接口）；网页版自检 **356/356 通过**；真机 A7 上传 PASS。
接口文档：`http-upload-resume-api.md`（客户端实现依据，含伪代码与兼容矩阵）。

### 3.3 安卓端 P0/P1（真机证据链）

完整审计：`android-audit.md`（含 logcat 时间线）；修复与实验：`android-fixes.md`。

| 级别 | 根因 | 修复 | 真机验证 |
|---|---|---|---|
| **P0-A** 令牌 | `refreshToken()` 裸在主线程 → `NetworkOnMainThreadException`，令牌过期自动恢复从未生效（A7 根因） | 刷新移入 `Dispatchers.IO`、403→刷新→重试一次、重试上限与可执行文案 | A7 文件上传+sha256 PASS |
| **P0-B** 保活 | Manifest 声明了 `LinkService` 但**运行时从未启动**（启动点只在设置页）；App 掉 `cached(400)` 后系统 `Destroyed live tcp sockets` 且全部 UDP sendto `EPERM`（每轮 256 条） | 启动/连上即 `startForegroundService`（不等 host）、FGS 类型 `specialUse`（Android 15+ dataSync 有 6h/天配额）、WifiLock+PartialWakeLock、SSE 90s 读超时+独立心跳+真指数退避 | `dumpsys activity services` 有 ServiceRecord；**60s 熄屏实验 EPERM 256/轮→0 条**；PC 端熄屏期间 `online=True`（67s/99s 两轮） |
| **P1-C** 发现洪泛 | 每轮公告给 /24 发 254 个 UDP 单播 + 每 20s 全 /24 TCP 扫描；每包打一条日志 | 取消无差别单播（改为连续 2 轮无设备且 ≥60s 才兜底一次）、扫描指数退避 20s→300s、失败日志限流聚合、修 266/254 计数 bug | 公告 UDP 包数约 **-98.8%**；EPERM 日志不再刷屏 |
| **P1-D** 下载续传 | 下载无重试/无 Range；传输中 App 被切后台→socket 被销毁→73MiB 全废 | 先写本地 `.part` 缓存 + Range 续传（4 次退避）→ 收满再发布 MediaStore | 真机 120MiB + iptables 断网 3.5s：重试时 offset **始终 503,904 B 不回退**（Range 正确） |
| **P1-E** SSE | `readTimeout=0` 半死连接永不重连；心跳在 SSE 连上后永不执行；退避被写死 1s | 90s 读超时、独立心跳线程、真指数退避 | 心跳/重连日志正常 |
| P2 | 无 WifiLock/WakeLock；FGS 类型 dataSync 有配额 | 同 P0-B | — |
| **A6.3** | 下载重试/失败留下 0 字节 `.pending-*` MediaStore 行 | 只清 >5 分钟 pending 行 + 按文件名兜底；每次下载成功/失败后限频清理 | v3 装机后由全量验收 A6.3 验证 |

### 3.4 GUI 显示准确性

审计：`gui-audit.md`（12 条，全部带"真值 vs 显示值"对照与截图）。修复：`gui-fixes.md`（8 项 + 1 项 P2 未做）。

| 项 | 改动前 | 改动后 |
|---|---|---|
| G-A（P0） | 每次接收完成出现 **2 行**（live「完成/0 B」+ 历史「已完成」）；根因 `direction=="recv"` vs 引擎 `"receive"` | 方向词汇规范化 + 收尾安全网；每次接收**只留 1 行「已完成」**、大小正确 |
| G-B（P0） | 桌面接收表只有 文件/来自/大小/时间/状态/操作，传输中无进度 | 新增 **进度/速度/剩余** 3 列（数据同源引擎真值；真值 65.6% ↔ 显示 28% 的采样差在 400ms 轮询窗口内） |
| G-C（P1） | 100% 后仍「传输中」数秒，速度从 320MB/s 衰减到 55MB/s 误导 | 100% 后显示「校验中…」，速度/剩余置空，结束变「已完成」 |
| G-D（P1） | 1024 进制却标 KB/MB/GB | 统一 **KiB/MiB/GiB**（`core/model.py` + `web/assets/app.js` + 手机端口径） |
| G-E（P1） | 页头「未知设备 #hash」而设备列表有名字 | 页头名称回退链：设备表→传输缓存→web 客户端→消息署名 |
| G-10（P0） | 每次接收抛 `UnboundLocalError`（`other_member` 未初始化）→ 接收文件不进会话库 | 初始化修复；接收文件真正进会话、页头显示真名 |
| G-11/G-12/G-09 | 接收「大小」列显示已收字节；接收记录不跨重启；进度小数位三端不一致 | 大小列恒为总大小；启动从会话库恢复仍在接收目录的历史行；统一 0 位小数 |

手机端 GUI（真机截图时间线证据见 `realdevice-gui-findings.md`）：PG-1 完成后仍「接收中…」+
仍显示「点击保存到手机」、PG-2 传输中无百分比/速度、PG-3 批量条目一直「正在排队」、
PG-5 会话标题露内部设备 id —— 已在 v3 修复，阶段 3 探针复测（见 §5）。

---

## 4. 性能基线（改前，perf-bench 权威口径）

报告：`bench-baseline.md`（口径：1 MiB=1048576 B；ENV_STALL 剔除；tmpfs/btrfs 双组；2GiB CPU 账本）。
机器：AMD Ryzen 7 2700X / 16 核 / 16GB；`/home` = **btrfs on USB PSSD，fdatasync 仅 39–56 MiB/s**。

| 链路 | 大小 | 结果（剔除环境停顿后中位） | CPU |
|---|---|---|---|
| EVSD 桌面↔桌面（btrfs 落盘） | 64 MiB | 45.7 MiB/s | 0.011–0.024 s/MiB |
| EVSD | 512 MiB | 流1 55.8 / 流4 66.7 / 流8 64.6 MiB/s | 同上 |
| EVSD | 1 GiB | 流4 64.8 MiB/s | 同上 |
| HTTP localhost 上传 | 64/512/1G | 700.9 / 950.5 / 110.6 MiB/s（1G 受慢盘写回） | 0.001–0.003 s/MiB |
| HTTP localhost 下载 | 64/512/1G | 1462 / 2253 / 2202 MiB/s | 同上 |
| 续传（512MiB×4 流） | — | 补传字节比 **63.9–64.2%**（journal 落定口径，两轮一致） | — |

解读：
1. **本机 EVSD 的第一瓶颈是磁盘 fsync（39–56 MiB/s）**，不是协议 CPU；千兆 LAN（118 MiB/s）也已先被网线限制。
   T1/T4/T5/T8 的收益场景是：2.5GbE+/本机回环/弱 CPU/写回压力大的盘/多流并发。
2. HTTP 链路（手机↔电脑）CPU 极低（0.001–0.003 s/MiB），手机侧 23 MiB/s 的基线是 **WiFi + 手机落盘** 限制。
3. 2 GiB CPU 账本（tmpfs）：hash 35.8% / aead 16.0% / crc32 11.4% —— AEAD 拷贝与重复 crc32 的收益有据可依。

**改后 A/B（同脚本同机同安静窗口，BEFORE=worktree `e18c079`，AFTER=当前树）**：

性能 A/B（同脚本同机同安静窗口；BEFORE=`e18c079` worktree，AFTER=当前工作树；
`tools/bench_transfer.py evsd-loopback` on tmpfs，5 轮中位+1 预热；详见 `bench-before-after.md`）：

| 大小 MiB | 流数 | BEFORE MiB/s | AFTER MiB/s | Δ速度 | BEFORE CPU s/MiB | AFTER CPU s/MiB | **ΔCPU** |
|---|---|---|---|---|---|---|---|
| 64 | 1 | 111.2 | 115.4 | +3.7% | 0.00609 | 0.00547 | **-10.2%** |
| 64 | 4 | 145.2 | 133.6 | -8.0%（小文件噪声） | 0.01141 | 0.01094 | **-4.1%** |
| 512 | 1 | 265.1 | 278.3 | +5.0% | 0.00588 | 0.00521 | **-11.4%** |
| 512 | 4 | 183.0 | 208.3 | +13.9% | 0.01391 | 0.01184 | **-14.9%** |
| 1024 | 4 | 202.0 | 217.5 | +7.7% | 0.01202 | 0.01108 | **-7.8%** |

**主判据 CPU s/MiB 平均 -9.7%（更省 CPU）；tmpfs 吞吐平均 +4.5%**。
说明：
1. 本机 `/home` 是 btrfs on USB PSSD（fdatasync 39–56 MiB/s），端到端 EVSD 会出现 60s 级写回停顿
   （实测 107/16.8 MiB/s 交替），会把协议差异完全掩盖 → 主矩阵放在 tmpfs 上测**协议/CPU 上限**；
   T4（fsync 出锁）在 tmpfs 上预期≈0 收益，其真实盘收益以确定性慢盘实验为准（**p99 11.8s→0.70ms**，
   transport-fixes.md）——这是"本机大文件看起来慢"的第一瓶颈证据。
2. 手机↔电脑 WiFi 链路吞吐由 WiFi/手机落盘决定（真机基线 ≈23 MiB/s），本轮未声称提升该链路吞吐；
   本轮对手机链路的收益是**稳定性**（令牌自愈、FGS 保活、下载/上传断点续传）。
3. 单条 64MiB/4 流速度 -8% 属小文件测量噪声（CPU 仍 -4.1%），不构成全局回退。



---

## 5. 真机与全量验收（v3 APK）

- APK：`eversend/android/app/build/outputs/apk/debug/app-arm64-v8a-debug.apk`，72,256,520 B，
  sha256 `27ec5f40e6b7ba17c408db93bb2b87ffbd2140fb945c15d281e14931ce15ef42`（23:46 装机，`install -r` 数据保留）。
- 复现：`DISPLAY=:99 QT_QPA_PLATFORM=xcb EVERSEND_ADB_SERIAL=2430d3c EVERSEND_REAL_WORK=/home/xgl/qa-real python3 tools/acceptance_opt1017.py --label final`

全量验收（`tools/acceptance_opt1017.py --label final`，安静窗口 load≈3.8 起跑，17.0 分钟）：

| 步骤 | 首轮 | 复验 | 结论 |
|---|---|---|---|
| compile / loopback / resilience / concurrency / chat / http-resume / web / gui-render | ✅ PASS | — | 8 步一次全绿 |
| gui-actions（Qt 真点击 26 组） | ❌ 2 断言（多选群发「第一台/第二台收到了」，16MiB×2） | 隔离单测 **3/3 PASS**；全量套件安静窗口复跑 **EXIT=0「桌面交互全部通过」** | 负载 flake，非回归 |
| enterprise（C1-C14/E4-E6） | ❌ C5.3（接收表列头仍按旧预期） | C1-C4、C6-C14、E4-E6 首轮即 PASS；C5 列头预期已随产品同步更新（`test_enterprise_acceptance.py` + 宪章 §3.2 + UI_LAYOUT），`--only C5` 复跑 **PASS** | 测试预期更新，产品行为符合"三端同列"目标 |
| realdevice（A0-A9） | ❌ A1.n（小文件取走后记录状态未在 60s 内变 sent；A7 本次 PASS） | A1 单独复跑 **PASS**（load 2.0，sha256/大小/状态全对） | 时序 flake；A9 未判定（1GB 可选，未设 EVERSEND_REAL_BIG=1） |

另：`opt-final.md`（enterprise 详细报告）显示 C13（≤5s 自动发现）/C14（消息必达）在 load 2.1 下 PASS；
E5（窗口冷启动 ≤1.5s、1000 条记录滚动）PASS；E4 内嵌 loopback 吞吐地板断言因前序负载记"未判定"，
由同一机器的独立 loopback 全绿运行覆盖（见上表）。

**判定：产品代码零失败**；首轮 3 个红全部归因为负载 flake / 测试预期同步，均有复验证据。
手工复跑命令：
`DISPLAY=:99 QT_QPA_PLATFORM=xcb PYTHONPATH=src python3 tests/test_gui_actions.py`
`DISPLAY=:99 QT_QPA_PLATFORM=xcb EVERSEND_MBPS_FLOOR=25 PYTHONPATH=src python3 tests/test_enterprise_acceptance.py --only C5`
`EVERSEND_ADB_SERIAL=2423d3c EVERSEND_REAL_WORK=/home/xgl/qa-real DISPLAY=:99 QT_QPA_PLATFORM=xcb python3 tests/test_real_device.py --only A1`


额外真机实验（android-fixes.md §2）：60s 熄屏 EPERM 对照、120MiB 下载断网 Range 续传；
上传断点续传端到端探针：`tools/realdevice_upload_resume_probe.py`。

真机上传断点续传端到端探针（`tools/realdevice_upload_resume_probe.py`，真机 UI 选文件 + iptables REJECT 造断网）：

| 轮次 | APK | 结果 | 证据 |
|---|---|---|---|
| v3 | 旧令牌链 | ❌ 未到断网阶段：电脑端重启（新令牌）后 6 次上传全是 `Broken pipe`、0 字节（暴露 §5.2 的令牌盲区） | `resume-probe-v3.md`（spool 时间线全 0；logcat 6 条 `上传中断…已确认 0/25171087`） |
| v4 | 令牌修复 | ⚠️ 未完成：探针启动时 App 停在重装后的设备抽屉页，测试驱动导航失败（非传输错误）；协议层 20 项断言 + 真机 A7 上传 + 断网下载 Range 续传（offset 不回落，见 android-fixes §2.3）已验证 | `resume-probe-v3.md` §备注、`android-fixes.md` |

结论：**上传续传协议与客户端实现已具备**（协议层全绿、A7 真上传 PASS、下载 Range 续传真机验证），
但"真机上传中断→续传"的端到端自动化验证仍受**测试驱动的 UI 导航**限制（App 重装/重启后停留页面
不可预测），列为后续测试基建项。真实用户体验上，v4 的令牌修复已消除"电脑重启后上传必失败"的根因
（见 §5.2 证据链与代码改动）。



---

## 5.1 真机 GUI 准确性探针（v3 APK · 同一探针两次运行）

探针：`tools/realdevice_probe.py`（PC 真值时间线 `/api/state` × 手机截图，同一时钟对齐）。
旧 APK 基线见 `realdevice-gui-findings.md`（6 条问题）；v3 复测：`gui-probe-after-v3.md` +
截图 `gui-probe-shots/after-v3/`。

v3 实测结果（`--only A2` 电脑→手机 100 MiB，安静窗口 ~22 MiB/s）：

| 项 | 旧 APK | v3 | 判定 |
|---|---|---|---|
| PG-1 完成后卡片仍「接收中…」+ 仍写「点击保存到手机」 | 有 | 卡片文案仍显示「接收中…」/「点击保存到手机」，但新增了完成 toast「已保存到『下载』：…」 | ⚠️ 部分改善（toast 新增，卡片状态仍滞后） |
| PG-2 传输中无百分比/速度（手机端） | 有 | 仍无（50.7% 真值帧卡片只有「接收中…」） | ❌ 未达成（根因：chat 消息 ↔ FetchTask 的 shareId 绑定在部分路径为空，走到 fallback 文案） |
| PG-3 批量条目一直「正在排队」 | 有 | 仍显示「共 100.0 MiB，正在排队」（此时单文件已下 50.7%） | ❌ 未达成（同一绑定问题） |
| PG-5 会话标题露内部设备 id | 有 | 仍是「与设备 00254c… 的对话」 | ❌ 未达成 |
| PG-6 单位 KB/MB（1024 进制） | 有 | 已改「100.0 MiB」 | ✅ 已修 |

结论：手机端 GUI 显示准确性本轮**只完成单位口径**；进度/状态绑定与标题命名仍未达成（桌面/网页端已达成本轮目标）。
根因已定位到「chat 消息的分享引用 ↔ 本地 FetchTask 的 shareId 在部分路径对不上」，修法建议见 §6 后续 ①。

## 5.2 v4 APK（收尾时发现的令牌盲区 + 复验）

收尾阶段用新写的**真机上传断点续传探针**（`tools/realdevice_upload_resume_probe.py`）发现：
**电脑端进程重启（新令牌、地址不变）后手机发大文件**，6 次重试全部 `Broken pipe` 且都停在 0 字节 ——
根因链：① v3 的 LinkService 只在 token 为空时取令牌；② 带旧令牌的 POST 在服务端 CSRF 门禁处
"读 body 前"就 403+关连接，客户端流式写 body 看到的是 Broken pipe 而非含"令牌"的错误 JSON；
③ 所以 v3 的自动换令牌重试链不触发。（证据：`resume-probe-v3.md` + 当时 logcat 6 条
`上传中断（第 N 次，已确认 0/25171087 字节）: Broken pipe`）

v4 修复（android-device，4 处）：LinkService 每次（重）连接都 `fetchToken`；心跳遇 403/令牌立即换；
`uploadToDevice` 上传链开始前无条件 `refreshToken` + 0 字节失败且 Broken pipe/Connection reset/
unexpected EOF/SocketException 时换令牌重试；`deliver()` 令牌判定并入这些异常类型。
v4 APK sha256 `07573e8190544f16b64086f72405b9f4f7961681fab841c65bb08d0474ec573e`（72,257,913 B，
`install -r` 覆盖、数据保留）。

**复验状态**：
- 上传续传探针最终在**真机端到端跑通**（收尾轮补测）：手机发 24 MiB 文件 → 传了 **9.96 MB 时
  `iptables` 断网 2.5s** → 客户端自动换令牌并 Range 续传 → 落地 sha256 一致，全程 **5.66s**
  （`resume-probe-v4.md`；v8 复测 12.8 MB 断点、6.39s、sha256 一致，`resume-probe-v8.md`）。
- 中途卡住的原因在**测试驱动**而非产品：① 探针工具的端口探测缺 `SO_REUSEADDR`，上一轮用例退出后
  TIME_WAIT 会让它误报"端口被占用"（已修，见 §5.4.5）；② App 重装/重启后停在设备抽屉页时，
  自动化导航失败（已加抽屉识别；并以完整序列驱动规避）。
- 另发现的**手机端 UI 残留问题**（多次电脑端重启后主页仍显示旧「发送失败」且「和电脑聊天」不产生会话）
  已在收尾轮定位并修复为 P0 回归：见 §5.4.3。

## 5.3 v4 全量真机回归（最终 APK）

安静窗口（首采样 load 0.74）跑 `--only A0,A1,A2,A6,A7,A8`（v4 APK
`07573e8190544f16b64086f72405b9f4f7961681fab841c65bb08d0474ec573e`）：

| 用例 | 内容 | 结果 |
|---|---|---|
| A0 | 现场：真机在线、端口独占、目录就绪 | ✅ PASS |
| A1 | 电脑→手机 512KiB，手机磁盘 sha256 相等（含取走后记录变 sent） | ✅ PASS |
| A2 | 电脑→手机 100MiB，sha256 相等 | ✅ PASS |
| A6 | 反作弊：界面说"已收到"时磁盘必须真有且非 0 字节（0 字节 pending 清理生效） | ✅ PASS |
| A7 | 手机→电脑 370KiB PNG（真 DocumentsUI → 记录 done → sha256） | ✅ PASS |
| A8 | 双向中文 + emoji | ✅ PASS |

**6/6 PASS**（报告 `tests/reports/real_device_v4-final.md`）。A9（1 GiB）仍按设计跳过（需 EVERSEND_REAL_BIG=1）。
说明：v4 的 A7 复跑需要以完整序列驱动（先 A1/A2 把 App 带到会话页）；单独 `--only A7` 在 App 重启后停在
设备抽屉页时会被测试驱动的导航问题卡住（不是传输失败），见 §5.2 说明。

## 5.4 收尾轮（v5–v8 APK）：手机 GUI / 重启自愈 / 下载重试 / 分享持久化

### 5.4.1 PG-1/PG-2/PG-3 的真正根因（数据链路，不是 Compose）
真机流程「桌面发送 → 手机出现卡片 → 点卡片『保存』」里，「保存」按钮走的是 `saveToDownloads`
**直连下载旁路**（`/api/chat/media/<id>`），根本不创建 `FetchTask` → `ui.fetches` 天然为空 →
气泡永远「接收中…」、批量条目永远「正在排队」、完成后没有「已保存」。v5 把分享条目的保存统一改走
`FetchManager.fetchNow(shareId, …)`（进度/速度/ETA/已保存与自动下载同一份事实，inFlight 去重）。

### 5.4.2 为什么 v5 还不够：服务端"抹字段"时把关联 id 一起抹了
服务端为隐私把消息里的 `mediaSource`（本机绝对路径）剥掉，但没有下发替代的关联字段；手机因此拿不到
这条消息对应的分享 id，`shareKey` 为空 → 只能退回直连旁路。修复（Lead 补丁）：
`/api/chat` 序列化时把服务端 `find_share_for_message()` 算出的**不透明 shareId** 一并下发
（不泄露路径、旧客户端忽略即可）；App 侧两处消息映射都解析（`ApiClient.toMessage` +
`MainActivity.messageFrom`），气泡按 `target.shareKey ?: message.mediaShareId` 匹配 FetchTask。
验收：`tests/test_share_persistence.py §5`（消息带 `shareId`、不带 `mediaSource`）+ web selftest **356 项全绿**。

### 5.4.3 v5 的"重启自愈"引入 P0 回归，v8 修复（真机 A7 卡死根因）
v5 在快照连续失败时把 `ui.client` 置空重连，但 `connectSession()` 命中 `store.client` 进程级缓存时
**直接 return、没有把 `ui.client` 补回来** → 心跳/快照都正常，聊天页所有操作却报
「还没连上电脑，先连上再发」；真机复现为 A7 在 A2 之后连续两轮卡死（v7）。v8 修复：命中缓存时若
`ui.client == null` 则补回、清错误、重播在线公告。**v8 全量真机 6/6 PASS**（`real_device_v8-final.md`），
A7 恢复 PASS。

### 5.4.4 手机 GUI 探针复测（v6 / v7 / v8，同一探针 `--only A2`）
| 项 | v3 基线 | v8 实测 | 判定 |
|---|---|---|---|
| PG-5 会话标题露内部设备 id | 有 | 「与 一台设备 的对话…」（按 deviceId 反查成员名，绝不露裸 id） | ✅ 已修 |
| PG-1 完成后卡片状态自愈 | 无 | 已完成卡片显示「已在其它客户端取走」（截图 `gui-probe-shots/after-v8/0003.png`、`0006.png`） | ✅ 部分修好（历史卡片自愈）；"已保存到手机"文案仍取决于是否经 FetchTask |
| PG-2 传输中百分比/速度 | 无 | **仍未达成**：真机帧里卡片没有实时百分比；logcat `分享自动下载：pending=1 auto=true tasks=0` 证明此刻仍没产生 FetchTask | ❌ 残留 |
| PG-3 批量条目状态 | 「正在排队」 | 仍显示「正在排队」（同一根因） | ❌ 残留 |
| PG-6 单位口径 | KB/MB | KiB/MiB/GiB | ✅ 已修 |

PG-2 残留的最短复现与下一步（给后续轮次）：在 A2 流程中抓 `fetcher.tasks.value` 与
`shareId` 是否为空/是否被 inFlight 卡住；目前证据指向"自动下载/保存路径此刻未创建 FetchTask"，
而非 UI 绑定或 shareId 匹配（server 侧已用真实桌面路径验证 `/api/chat` 确实下发 shareId）。

### 5.4.5 分享列表持久化 + 测试设施修复 + 真机上传续传首次跑通
1. **分享列表落盘**（`web/server.py`）：原子写 `data_dir/shares.json`（tmp+rename），启动加载并丢弃
   "文件已不存在"的条目，`clear_shares`/`mark_share_downloaded` 同步落盘。新增
   `tests/test_share_persistence.py` **14 项**：落盘→模拟重启→同 id 仍可整文件/206 续传→删文件后丢弃→
   clear 落盘→聊天消息 shareId 下发；全绿。
2. **测试驱动端口探测**：`test_real_device.ports_free()` 与 `tools/realdevice_probe.free_ports()` 补
   `SO_REUSEADDR`（TIME_WAIT 假"端口被占用"曾连续挡住真机复验）；上传续传探针首次在真机端到端跑通。
3. **下载重试策略**（android-device v5）：6 次尝试、退避 0.5/1/2/4/8/8s、失败保留 `.part` 供续传、
   >1 天陈旧 `.part` 清理、失败文案带断点大小。
4. **最终 APK（v8）**：`app-arm64-v8a-debug.apk` 72,261,918 B，
   sha256 `448585275c7c515e1c170c4ed4834c8bf517fd0d57f7717a142161d5d578fe6c`（install -r 保留数据）。

| 收尾轮验证 | 结果 |
|---|---|
| v8 全量真机 A0/A1/A2/A6/A7/A8 | **6/6 PASS**（`real_device_v8-final.md`） |
| v8 上传断点续传真机探针（断网 2.5s） | **PASS**：12.8 MB 断点续传、sha256 一致、6.39s（`resume-probe-v8.md`） |
| `tests/test_share_persistence.py` | 14/14 PASS |
| web selftest / http resume / share 回归 | 356 项 PASS / 20 项 PASS |
| A/B 性能（tmpfs，5 轮中位） | CPU s/MiB 平均 **-9.7%**、吞吐平均 +4.5%（`bench-before-after.md`） |

## 6. 兼容性 / 安全 / 边界

- **协议兼容**：EVSD cipher 协商不升版本，旧对端忽略未知字段；HTTP 上传续传无 uploadId 走旧路径，旧客户端无感；
  新客户端先查 `caps.uploadResume`，旧服务端自动退回一次性上传（矩阵见 `http-upload-resume-api.md §6`）。
- **HTTP 仍是明文局域网**（与改前一致）：本次未引入加密，属已知安全边界（`docs/PROTOCOL.md §10` 已有说明）；
  手机→电脑传输走 CSRF 令牌保护；若需 LAN 加密可作为下一轮专项。
- **已知边界/后续建议**：
  1. ~~手机下载失败后 `.part` 被删、重试 4 次偏短~~ → **已修（v5）**：6 次尝试、退避至 8s、失败保留 `.part`、
     失败文案带「已保留 X MiB 断点」、>1 天陈旧 `.part` 自动清理；
  2. ~~电脑端重启后 share 列表不持久，正在下载的文件续传会 404~~ → **已修（收尾轮）**：分享表原子落盘
     `data_dir/shares.json`、启动恢复并丢弃文件已不存在的条目（`tests/test_share_persistence.py` 14 项）；
  3. 上传续传的"完成响应丢失 + 电脑端重启"极端边界会重复落盘 `(1)` 文件（完成记录未持久化）；
  4. 桌面 G-07（状态栏"正在接收"）、引擎显式 `phase=verifying`、网页实页/DPI/浅色截图未做；
  5. `netem` 高 RTT 下的窗口自适应（T6）缺可信数据，留待下轮先测后改；
  6. Windows/macOS 本轮未上真机（CI 覆盖内核/互传）；macOS GUI 仍未验证。

---

## 7. 复现与证据索引

```bash
cd "/home/xgl/我的项目/自制软件/韧传/eversend"

# 全量验收（含真机；安静窗口 load<4）
DISPLAY=:99 QT_QPA_PLATFORM=xcb EVERSEND_ADB_SERIAL=2430d3c \
EVERSEND_REAL_WORK=/home/xgl/qa-real PYTHONPATH=src \
python3 tools/acceptance_opt1017.py --label final

# 单项
PYTHONPATH=src python3 tests/test_loopback.py
PYTHONPATH=src python3 tests/test_http_resume.py
PYTHONPATH=src python3 -u src/eversend/web/selftest.py
DISPLAY=:99 QT_QPA_PLATFORM=xcb EVERSEND_ADB_SERIAL=2430d3c \
EVERSEND_REAL_WORK=/home/xgl/qa-real python3 tests/test_real_device.py --label opt-final

# 真机 GUI 准确性探针（PC 真值时间线 vs 手机截图）
DISPLAY=:99 QT_QPA_PLATFORM=xcb EVERSEND_ADB_SERIAL=2430d3c \
EVERSEND_REAL_WORK=/home/xgl/qa-real python3 tools/realdevice_probe.py --only A2 --label after

# 上传断点续传真机探针
DISPLAY=:99 QT_QPA_PLATFORM=xcb EVERSEND_ADB_SERIAL=2430d3c \
EVERSEND_REAL_WORK=/home/xgl/qa-real python3 tools/realdevice_upload_resume_probe.py --label v3

# 收尾轮回归（分享持久化 + shareId 下发 + 上传续传真机）
PYTHONPATH=src python3 tests/test_share_persistence.py        # 14 项
PYTHONPATH=src python3 tests/test_http_resume.py              # 20 项
PYTHONPATH=src python3 -u src/eversend/web/selftest.py        # 356 项

# 真机 GUI 准确性探针（v8；PNG 帧 + PC 真值时间线）
DISPLAY=:99 QT_QPA_PLATFORM=xcb python3 tools/realdevice_probe.py --only A2 --label after-v8

# 性能 A/B（安静窗口；BEFORE=baseline worktree）
bash tests/reports/opt-1017/ab-run.sh
```

证据目录：
- 报告：`tests/reports/opt-1017/`（`*-audit.md`、`*-fixes.md`、`realdevice-*.md`、`acceptance-final.md`、`bench-*.md`）
- 真机报告：`tests/reports/real_device_*.md`（A0-A9 每项子断言）
- 截图：`tests/reports/opt-1017/shots/`、`gui-probe-shots/`、`/home/xgl/qa-real/shots/`
- 原始数据：`tests/reports/opt-1017/*.json`、`.testscratch/`（micro-bench/探针原始输出）

---

## 8. 交付物清单

| 类别 | 文件 |
|---|---|
| 产品代码（未 commit，最终快照提交在交付时打） | `src/eversend/core/{crypto,chunkstore,connection,sockutil,transfer}.py`、`src/eversend/core/model.py`、`src/eversend/web/server.py`、`src/eversend/web/assets/app.js`、`src/eversend/desktop/{main_window,transfers_view}.py`、`android/**`（6 files + Manifest）、`docs/PROTOCOL.md` |
| 新增工具 | `tools/bench_transfer.py`（性能 harness）、`tools/realdevice_probe.py`（GUI 准确性探针）、`tools/realdevice_upload_resume_probe.py`（上传续传探针）、`tools/acceptance_opt1017.py`（全量验收编排） |
| 新增测试 | `tests/test_http_resume.py`（上传续传 20 项）、`tests/test_share_persistence.py`（分享持久化+shareId 下发 14 项） |
| 测试设施修正 | `tests/test_real_device.py`（ADB 自适应、A7.j 泵事件循环、端口探测 SO_REUSEADDR、抽屉页导航）、`tools/realdevice_probe.py`（同端口修复）、`tests/test_gui_actions.py`（列下标→列名） |
| 收尾轮新增报告 | `real_device_v8-final.md`、`gui-probe-after-v{5,6,7,8}.md`、`resume-probe-v{4,8}.md`、`bench-before-after.md` |
| 最终 APK | `android/app/build/outputs/apk/debug/app-arm64-v8a-debug.apk`，72,261,918 B，sha256 `448585275c7c515e1c170c4ed4834c8bf517fd0d57f7717a142161d5d578fe6c` |
| 报告 | `tests/reports/opt-1017/` 全部；APK sha256 见 §5 |

---

## 9. 共享基座沉淀（本轮入库）

坑：①安卓无 FGS 掉 cached → 系统销毁 socket/EPERM（先看 importance 别看屏幕）；②令牌刷新写主线程 →
NetworkOnMainThreadException 使自动恢复失效；③kill $(ss|grep pid) 误杀 Xvfb 等自身依赖服务；
④换机 debug 签名不一致 → install -r 必失败被迫清数据。
成功模式：①手机→电脑 HTTP 断点续传最小协议（uploadId/offset/202/409/status/幂等）；
②真机 GUI 显示准确性探针（PC 真值时间线 × 手机截图对齐采样）。
