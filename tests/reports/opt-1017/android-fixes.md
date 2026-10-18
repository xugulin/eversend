# 安卓端修复与真机实验数据（v2/v3）· opt-1017 阶段2/3

> 负责人：android-device · 2026-10-17 23:4x
> 构建产物：**v3 = `app-arm64-v8a-debug.apk`，72,256,520 B，
> sha256 `27ec5f40e6b7ba17c408db93bb2b87ffbd2140fb945c15d281e14931ce15ef42`**
> （23:43 构建完成；23:46 已 `adb install -r` 覆盖安装成功，App 数据保留，FGS 启动即生效）
> 真机：MI9 cepheus / crDroid 12.12 / Android 16 (API36)；实验时系统负载 **22–25**（多专家并行，时序类断言偏悲观）。

## 1. v3 修复清单（相对基线 e18c079）

| # | 问题 | 修复 | 文件 | 验证方法 |
|---|---|---|---|---|
| P0-A | 令牌过期自动恢复崩主线程（A7 FAIL） | `refreshToken()` 包 `withContext(Dispatchers.IO)`；403→刷新令牌→原样重试一次；`fetchToken` 调用点核查（6876 在 suspend+IO 上下文，无需改） | MainActivity.kt | Lead 真机 A7：v2 已 **PASS**（电脑接收目录出现、sha256 一致、状态「已发给 1 台」） |
| P0-B | 前台服务从未启动 → 掉 cached 后系统销毁 socket / 全部 sendto EPERM | ① App 启动即 `startForegroundService`（**keepAlive=true 即启动，不等 host 非空**，3s 幂等循环）② LinkService 独立心跳线程（5s `/api/state`）③ 真指数退避 1s→15s ④ `WifiLock(HIGH_PERF)`+`PARTIAL_WAKE_LOCK` ⑤ FGS 类型 dataSync→**specialUse**（避开 Android15+ 6h/天配额） | MainActivity.kt / LinkService.kt / AndroidManifest.xml | v3 装机后：`dumpsys activity services` 见 `ServiceRecord{…/.LinkService}`；logcat `保活服务启动（host=…）`；v2 的 60s 熄屏实验 EPERM=0、FGS=1、PC 端 online=True |
| P1-C | 发现通道 EPERM 刷屏 + 每轮 254 单播洪泛 + 扫描无退避 | 发送失败日志限流（同通道 5s 一条 + 本轮失败计数，不再每包 publish）；单播全网改为"连续 2 轮无设备且 ≥60s"才兜底扫一次；TCP 扫描 20s→40→…→300s 指数退避 | Discovery.kt | 包数：公告 UDP 约 **1028→12 包/min（-98.8%）**；单测/日志：v3 logcat 无"单播应答 EPERM"洪泛 |
| P1-C2 | `TCP 扫描 266/254` 计数重复 | worker 跳过 `preferred.take(12)` 已探地址 | ApiClient.kt | v3 真机日志应为 `探了 254/254 台`（v2 实测仍 266/254，已定位该修复未进 v2） |
| P1-C3 | 启动瞬间单播兜底扫过早（254 包） | `start()` 时把 `lastUnicastSweepAt` 设为当前时刻，冷却 60s | Discovery.kt | v3 启动日志不应立即出现"单播兜底扫一轮" |
| P1-D | 下载中断（A2：73MiB 全废） | `download()` 支持 Range/206；Fetch 改"cache `.part` + 断点续传（4 次退避）→ 收满发布 MediaStore"；进度/速度原地更新 | ApiClient.kt / Fetch.kt | 见 §2.2 实验：中断在 503,904 B，后续重试**从同一 offset 续**（不回到 0） |
| P1-E | SSE `readTimeout=0` 永不重连；心跳在 events() 前跑；退避被 `minOf(…,1000)` 写死 | readTimeout 90s + 连续 2 次超时重建连接；心跳拆独立线程；`sleep(backoff)` 真退避 | ApiClient.kt / LinkService.kt | logcat `事件流 180 秒没有任何数据`应只在真断流时出现；v2/v3 熄屏期间心跳正常（PC 端在线） |
| A6.3 | 下载后留 0 字节 `.pending-*` 残留 | ① `cleanupPendingDownloads` 只清 **>5 分钟** 的 pending 行（不误删并发下载中新行）+ 按文件名扫 `.pending-*` 文件兜底；② 每次下载成功/失败后 `cleanupStalePending()`（限频 60s，App 不重启也自愈） | MainActivity.kt / Fetch.kt | 装 v3 后启动 App：Root 清理会删陈旧行；跑 A6.3 应绿（无 `.pending-*` 残留） |
| PG-1/2/3 | 完成 3.7s 后仍「接收中…/点击保存到手机」；41.8% 无进度；批量条目仍「正在排队」 | Bubble 新增 `fetches`：文件卡片 detail 按 FetchTask 渲染（运行中 `progressText`=百分比+已收/总+速度+ETA；完成→已保存到手机、保存按钮变「已保存」不可点）；SharesSummaryCard 同时看全局 active fetch，显示同样进度；`fetchTransfers()` 补 speed/eta | MainActivity.kt / Fetch.kt | Lead 探针 `--label after-newapk` 四判据：百分比±3%、速度非零、完成 2s 内变已保存、不再显示"正在排队" |
| PG-5 | 会话标题露内部设备号 | `readableTitle()`/`humanDeviceName()`/`looksLikeInternalId()`：标题里 16–64 位 hex 换设备名/"一台设备" | MainActivity.kt | 真机截图会话标题不得出现内部 id |
| PG-6 | 1024 进制标 SI 单位 | `fmtSize()` 单位改 IEC：B/KiB/MiB/GiB/TiB（与 gui-desktop 对齐） | MainActivity.kt | 512KiB 显示 `512.0 KiB`、100MiB 显示 `100.0 MiB` |

## 2. 真机实验数据（load 22–25，注意偏悲观）

### 2.1 60s 熄屏对照实验（v2，FGS 已由 A0 用例拉起）

| 指标 | 基线（无 FGS，旧 APK） | v2（FGS 在跑） | 判定 |
|---|---|---|---|
| 熄屏状态 | `Dozing` | `Dozing`（实测 67s/99s 两轮） | 有效 |
| `sendto EPERM` 条数 | **256 条/轮公告**（现网共 2777+1280 条） | **0 条**（两轮实验都是 0） | ✅ |
| `dumpsys activity services` LinkService | `(nothing)` | `ServiceRecord{…/.LinkService}` ×1 | ✅ |
| PC 端（`/api/state`）手机在线 | 熄屏后 15s 变离线 | **online=True**（熄屏期间 PC 仍看到 AD-test 在线） | ✅ |
| 熄屏期间发现活动 | — | `听到公告/公告一轮` 42 条（组播接收与公告继续） | ✅ |
| 亮屏后 | 恢复慢 | 立即 Awake，无重连卡顿 | ✅ |
| 熄屏收消息通知 | — | ⚠️ **未证明**：`/api/chat/send` 返回 ok，但手机通知数 22→22 无增量。通知权限已确认 granted=true，怀疑该消息的会话成员路由（请求来自 web 客户端，会话 members 含 web:127.0.0.1）未把事件推到手机 SSE；需 Lead 用真实手机会话重测 | 待验证 |

### 2.2 下载断点续传实验（v2：PC→手机 120 MiB，iptables 造断网）

- 正常路径（实验#1）：120 MiB 下载完成，手机 `/sdcard/Download/resume-test-120m.bin` 与 PC 文件
  **sha256 完全一致**（`55750b14…96fbb`）→ 新 Fetch（.part→MediaStore）主路径正确。
- 断网续传（实验#3，`sudo iptables REJECT 手机→PC:52119` 3.5s）：

```
下载中断（第 1 次尝试，已落 503904/125829120 字节）: Connection reset
下载中断（第 2 次尝试，已落 503904/125829120 字节）: Failed to connect to /192.168.110.41:52119
下载中断（第 3 次尝试，已落 503904/125829120 字节）: …
下载中断（第 4 次尝试，已落 503904/125829120 字节）: …
下载失败 resume3-234344.bin: Failed to connect …
```

- **续传 offset 正确**：4 次重试的"已落"始终是同一 offset 503,904 B（没有回到 0 重下）。
- **未完成原因（两个真问题，建议 v4/后续修）**：
  1. `MAX_DOWNLOAD_ATTEMPTS=4` 太小、退避总长仅 ~3.5s：网络恢复（23:43:49.526）比第 4 次尝试
     （23:43:49.850）早 300ms，但 4 次已耗尽 → 判定失败。**建议 4→6 次，退避 0.5/1/2/4/8/8s**。
  2. 失败时把 `.part` 删了 → 下一次 `offer()`/用户「重试」只能从 0 重来。**建议保留 `.part`，
     下次启动同一 share 时按已有长度续传**（.part 在 cache，不产生 A6.3 的 MediaStore 残留；
     另加"启动清 >1 天陈旧 .part"）。
- 另一发现（**产品级，属服务端/Lead**）：实验#2 用"杀 PC 再起"造断网时，重启后的 share 列表丢失，
  手机第 4 次重试收到 `HTTP 404 这个文件已经不在分享列表里了` → 电脑端重启后**分享条目不持久**，
  正在下载被中断的文件无法续传。建议服务端把 share 列表落盘（或重启后扫描接收目录重建），
  否则"杀 PC 后续传"在真实场景仍不可用。

### 2.3 上传断点续传（手机→PC）

- Lead 真机回归 **A7 PASS**（v2）：上传本体成功、电脑接收目录出现、sha256/大小一致、手机状态
  「已发给 1 台」→ 续传上传客户端（caps 协商 + uploadId/offset/202/409/status）联通且非中断路径正确。
- **"传输中断开再续"的端到端实验未做**：需要 App UI 触发"发送文件"（文件选择器 → 选中 → 发送），
  当前 ADB 写操作权限只给到熄屏/安装，故未执行。建议 Lead 在 v3 全量跑完后做一次：
  在手机上点发送大文件，我/Lead 在传输中 `sudo iptables REJECT 手机→PC:52119` 3–5s，
  判据 = 手机 logcat 出现 `上传中断（第 N 次，已确认 X/Y 字节）` 且后续 `X` 单调不回退、
  最终 A7 的 sha256 一致；服务端 `/api/upload/status?uploadId=` 的 offset 单调递增。

## 3. 收尾状态

- v3 已装（23:46，`install -r` 成功，App 数据保留）；装完即验证：logcat
  `保活服务启动（host=192.168.110.41:52119）`、`发现引擎启动：…组播锁=true 网卡=wlan0`、
  `dumpsys activity services` 见 LinkService ServiceRecord。**FGS 启动条件修复（keepAlive 即启动）验证通过**。
- 我起的 AD-test CLI 实例（pid 807470）与实验用 iptables REJECT 规则**已清理**，默认端口 52117/52119 空闲，
  手机交还，待命不再操作。
- 未验证项（交 Lead 全量覆盖）：A6.3（v3 清理逻辑）、PG-1/2/3/5/6（探针 after-newapk）、
  熄屏收消息通知、上传中断续传。

---

## 4. v4 追加修复（P0：电脑端重启后上传必失败 —— 令牌盲区）

真机复现（Lead 的上传续传探针）：电脑端重启（换令牌、地址不变）后手机发 25MB 文件，
6 次重试全部 `上传中断（第 N 次，已确认 0/25171087 字节）: Broken pipe`，最终「发送失败」。

**根因链条**
1. `LinkService` 重连后不刷新令牌（原代码只在 `token.isBlank()` 时 fetchToken）；
   电脑重启后地址不变、SSE 心跳重连成功，但手里仍是旧令牌。
2. 带旧令牌 POST 上传 → 服务端 CSRF 门禁在**读 body 之前**就 403 并关连接 →
   客户端正在流式写 body，看到的是 `Broken pipe`，**不是**含"令牌"的错误 JSON。
3. 所以 `deliver()` 的 `why.contains("令牌")` 自动刷新+重试不触发；
   `uploadToDevice` 6 次重试都是 0 字节 Broken pipe（续传协议本身没问题，服务端连 body 都不读）。

**v4 修法（双保险，四处）**
| 位置 | 改动 |
|---|---|
| LinkService.kt `loop()` | **每次（重）连接都 `token = ApiClient.fetchToken(base)`**（不再只在 blank 时取）；每次重连一次 GET，成本可忽略 |
| LinkService.kt `heartbeatLoop()` | 心跳请求报 403/含"令牌/防跨站"时立即 `fetchToken(host)` 换新令牌（界面/服务不再长期持旧令牌） |
| MainActivity.kt `uploadToDevice()` | **上传链开始前无条件 `refreshToken()` 一次**（放在问 caps 之前——caps 查询本身也会 403）；catch 里 0 字节失败且 `looksLikeTokenFailure()` 时再换令牌重试（`Broken pipe`/`Connection reset`/`unexpected end of stream`/`SocketException`） |
| MainActivity.kt `deliver()` | 令牌判断从 `why.contains("令牌")` 扩展为 `tokenish`：令牌 / broken pipe / connection reset / socketexception / unexpected end of stream |

**验证方法（Lead）**：杀电脑端进程重启（新令牌）→ 手机不发任何手动操作，直接用 A7 用例
或 `tools/realdevice_upload_resume_probe.py` 发大文件 → 期望一次成功（或最多一次自动
换令牌后成功），服务端 `/api/upload/status?uploadId=` 的 offset 从 0 正常增长；
logcat 应出现 `发送失败（准备换令牌）: Broken pipe` 后自动恢复，或 `已换令牌后重试`，
不再出现 6 次连续 `已确认 0/…` 失败。

> 与历史经验的关系：`android-fixes.md` §1 的 P0-A（令牌刷新的**线程**问题）修的是
> `NetworkOnMainThreadException`；本次修的是**触发判定**与**令牌生命周期**问题 ——
> 两者叠加才是"A7 非中断 + 慢启动"全绿，而"电脑端重启后立刻发大文件"这个场景之前仍会红。

---

## 5. v5 追加修复（Round 2：PG-1/PG-2 根因、PG-5、重启后 UI 自愈、下载重试策略）

真机探针 after-v3 证据（`gui-probe-shots/after-v3/0006.png`、`0011.png`）复现：
50.7% 时气泡仍「接收中…」、批量条目仍「共 100.0 MiB，正在排队」、完成 4.5s 后仍「点击保存到手机」、
会话标题露 `00254c…`。

### 5.1 PG-1/PG-2 根因（不是 Compose 观察问题，是下载走了旁路）

真机用例 A2 的流程是：桌面点「发送」→ 手机出现卡片 → **点卡片上的「保存」**。而「保存」按钮
（`MainActivity.kt:4081` 附近）走的是 **`saveToDownloads { active.saveMedia/download }` 直接下载旁路**：
- 不创建 `FetchTask` → `ui.fetches` 天然是空的 → 气泡 `fetch==null`、批量条目 `activeFetches` 为空，
  于是永远显示「接收中…」「正在排队」；
- 完成后也没有 FetchState.Done，气泡状态映射拿不到「已保存」。
（自动下载（Root 轮询 `fetcher.offer`）理论上也会建任务，但探针在卡片出现后立刻点了「保存」，
 直接旁路先拿到连接；且它在界面上不产生任何进度事实。）

**v5 修法**：`保存` 按钮对**分享条目（`/api/share/<id>`）统一走 FetchManager**：
`FetchManager.get(context).fetchNow(active, shareId, name, size)`（MainActivity.kt:4158 附近）。
- 进度条/速度/ETA/「已保存」与自动下载是**同一份事实**（400ms 刷新，`Fetch.kt` run() 内 `now-lastReport>=400`）；
- `inFlight` 去重，和自动下载不会重复抢同一个分享；
- 聊天附件（`/api/chat/media`）不属于分享条目，保留原来的直接保存路径。

**留下的诊断日志**（下次真机探针 logcat 可 grep，确认自动下载路径是否也在跑）：
`EverSend: 分享自动下载：pending=N auto=true tasks=M`（MainActivity.kt:503）。

### 5.2 PG-5 会话标题不再露内部设备 id

真机标题「与 设备 00254c… 的对话」来自两处：
1. `humanDeviceName()` 对不可读名字返回 `设备 <id 前6位>`（MainActivity.kt:7949）→ 改成返回「一台设备」，**任何情况不出现 id 片段**；
2. `chatMembersLabel()` 兜底直接写原始 id（MainActivity.kt:7898 附近）→ 改成按 deviceId 反查：
   members/identityAliases 命中 → 用名字；是本机（deviceId 相等）→ 「我的手机」；对端是电脑 → 「电脑」；否则「一台设备」。
`readableTitle()` 同步加 `mineId` 参数（deviceId 相等 → 「本机」），两处调用已传入 `store.deviceId`。

### 5.3 P1 电脑端重启后 UI 状态自愈

现象（00:36 真机）：后台心跳已恢复、电脑端看到手机在线，但主页仍挂「发送失败：Failed to connect…」，
点「和电脑聊天」无反应 —— 因为 `ui.client` 还拿着**旧令牌**的 ApiClient。

v5 三处修：
1. `LinkService`: 新增 `reconnectGeneration`（后台连接"从坏到好"时 +1，LinkService.kt:69/447）；
2. `MainActivity.Root`: 观察这个代数，**一变就清 `ui.error`、`ui.client=null`、`linkHost`**，
   下一轮轮询用新令牌重连并刷新设备列表/选中态（MainActivity.kt:396-410）；
3. 轮询循环：快照**连续 2 轮失败** → 丢弃旧 client 重连（MainActivity.kt:511-516）；
   「和电脑聊天」按钮失败时也自动 `ui.client=null` → `connectSession` 重连 → 重试一次（MainActivity.kt:1719 附近）。

### 5.4 P1 下载重试策略（android-fixes §2.2 自己提的待修）

- `MAX_DOWNLOAD_ATTEMPTS` 4→**6**（Fetch.kt:100）；
- 退避 0.5/1/2/4/8/8s（原 0.5/1/2/4s 封顶，总窗口 ~3.9s 扛不住真实断网）；
- 失败**保留 `.part`**（仅用户「取消」才删，Fetch.kt:337-360）：下次自动 offer / 点「重试」时
  run() 从 .part 长度续传，不再从 0 重下；
- 失败文案带下一步与已保留字节数：「…（已保留 X MiB 断点：网络恢复后点「重试」会从断点续传）」；
- `cleanupStalePending()` 顺带清理 >1 天的陈旧 `.part`（Fetch.kt:145-155）。

**v5 验收判据（Lead 探针）**
1. A2 传输中截图：气泡/卡片出现「接收中 50% · 50.0 MiB / 100.0 MiB · X MiB/s · 剩余 …」，
   批量条目「正在接收 · …」而不是「正在排队」（±3% 与 PC 真值一致）；
2. 完成后 2s 内：卡片「✅ 已保存到手机」、按钮变「已保存」（不可点）；
3. 会话标题无任何 16+ 位 hex id；
4. 重启电脑端后：主页错误自动消失、点「和电脑聊天」能建会话/发消息；
5. 断网 3–10s 恢复：下载自动续传完成且 sha256 一致；logcat 出现 `下载中断（第 N 次，已落 X/Y）`
   且 X 不回落，最终 `已保存到手机`。


## 6. Lead 补充补丁（v6–v8，收尾轮）

在 android-device v5 完成后，真机探针发现 PG-2/PG-1 仍未达成，Lead 继续定位并补了两处**跨端**修复
（只改 `src/eversend/web/server.py` 与 `android/**` 的消息映射，不动 UI 布局）：

1. **`/api/chat` 下发 `shareId`**（`web/server.py` 新增 `_message_for_phone()`）：消息给手机时不带
   `mediaSource` 路径，但额外带上服务端 `find_share_for_message()` 算出的不透明分享 id —— 手机点「保存」
   才有 `shareId` 进入 FetchManager（PG-1/PG-2 的数据链路前提）。
   证据：`tests/test_share_persistence.py §5`；真实桌面路径已 curl 验证 `/api/chat` 返回 shareId。
2. **App 侧两处消息映射都解析 `shareId`**：`ApiClient.toMessage()` 与 `MainActivity.messageFrom()`
   都加 `mediaShareId`（主聊天路径实际走 `messageFrom`，只改一处会漏）；气泡的 `shareId` 改为
   `target.shareKey ?: message.mediaShareId`，保存统一走 `FetchManager.fetchNow`。
3. **P0 回归修复（v8）**：`connectSession()` 命中 `store.client` 缓存时未把 `ui.client` 补回来 →
   心跳/快照正常但聊天页所有操作报「还没连上电脑，先连上再发」（真机 A7 卡死根因）。修复后 v8 全量
   真机 6/6 PASS。

**残留（后续轮次）**：A2 传输中真机帧仍无实时百分比，logcat `分享自动下载：pending=1 auto=true tasks=0`
说明此刻自动下载/保存路径仍未创建 FetchTask；下一步在真机上加日志打 `shareId` 与 `fetcher.tasks`
的即时值，确认是"shareId 为空"还是"offer/start 被 inFlight 拦住"。
