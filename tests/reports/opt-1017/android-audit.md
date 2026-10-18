# 安卓端（APK）审计 · 稳定性 / 速度 / 显示准确性 · opt-1017

> 负责人：android-device · 日期：2026-10-17 · 仓库 `/home/xgl/我的项目/自制软件/韧传/eversend`
> 审计对象：**git e18c079（opt-1017/baseline）的 APK 源码**（`android/app/src/main/java/cn/eversend/app/`，10436 行）
> 真机：MI 9 (cepheus) serial `2430d3c`，**crDroid 12.12 / Android 16 (API 36)**，arm64-v8a，1080x2340，
> 手机 WiFi `192.168.110.158/24`，电脑 WiFi `192.168.110.41/24`。装的 APK 是 2026-09-26 构建的 v1.0.0。
> 判据：每个结论都有「file:line（基线）+ 真机 logcat/dumpsys 证据 + 机制 + 影响 + 修法 + 验证方法」。
> 文中 file:line 若无特别说明均指**基线版本**；阶段2 修复后的行号会变，修复清单见 §9。

## 0. 结论摘要

| 级别 | 问题 | 文件:line（基线） | 一句话根因 | 状态 |
|---|---|---|---|---|
| **P0-A** | PC 重启后手机发文件必失败（A7）：令牌过期→自动换令牌路径崩在**主线程** | `MainActivity.kt:504-530`（`api.refreshToken()` 未包 IO） | `refreshToken()` 里 `HttpURLConnection` 在主线程 → `NetworkOnMainThreadException`，自动恢复永不生效 | **阶段2已修** |
| **P0-B** | **前台服务实际从未启动**（`dumpsys activity services` 为空）→ App 掉到 cached 状态后系统销毁 TCP socket / 拒绝所有 UDP sendto（EPERM） | `MainActivity.kt:2933-2940`（启动点在设置页 LaunchedEffect）、`LinkService.kt` 全文件 | 启动点放错：用户不进设置页就没有 FGS；重要级掉到 CACHED(400) | **阶段2已修** |
| **P1-C1** | 发现通道 EPERM 日志刷屏：2777+1280 条，单轮 256 条；每包 `record()` 一次 = 1 行 log + 1 次 UI publish | `Discovery.kt:409-411`（254 单播）、`Discovery.kt:417-442`（send/record） | 后台时系统拒绝全部发送；代码每包都记错误 | **阶段2已修** |
| **P1-C2** | 每轮公告给 /24 每台主机发 254 个 UDP（每 15s，每网卡）+ 每 20s 全 /24 TCP 扫描 254 连接 | `Discovery.kt:406-412`、`Discovery.kt:507-515` | "单播叫醒"没有节流/退避，代价远大于收益 | **阶段2已修** |
| **P1-C3** | 主动扫描计数 266/254、优先地址被重复探两遍 | `ApiClient.kt:563-575`（preferred 先探 12 个并计数）+ `ApiClient.kt:584-592`（worker 再探全部 254） | 计数与探测重复 | **阶段2已修**（计数口径） |
| **P1-D** | 下载中切前台 App（文件选择器）→ 连接 abort，73MiB 全废 | `Fetch.kt:206-272`（无重试/无 Range）、`ApiClient.kt:161-179`（download 无 Range） | 系统销毁 cached 进程 socket + 下载没有断点续传 | **阶段2已修** |
| **P1-E** | SSE 事件流 `readTimeout=0`：半死连接永远阻塞，永不重连；LinkService 心跳在 SSE 连上后永不执行；退避被 `minOf(backoff,1000)` 写死成 1s | `ApiClient.kt:243`、`LinkService.kt:155-168`、`LinkService.kt:204` | 阻塞式读 + 逻辑位置错误 + 退避 bug | **阶段2已修** |
| **P2-a** | 接收方向传输行不显示速度/ETA（`extra=""`），发送方向显示，三端口径不一致 | `MainActivity.kt:2457-2459` | 只填了 percent，没有 extra | 待 Lead 排期 |
| **P2-b** | APK 包内无 `WifiLock/WakeLock`：熄屏后 Wi-Fi 进省电、CPU 可休眠，长连接/续传速度受主动影响 | `LinkService.kt`（基线无 PowerManager/WifiManager 引用） | 缺保活锁 | **阶段2已修** |
| **P2-c** | Android 15+ `dataSync` FGS 有 6h/天配额，到点强杀 —— 与"常驻"语义冲突 | `AndroidManifest.xml:46-49`、`LinkService.kt:300-308` | FGS 类型选错 | **阶段2已修**（specialUse） |

## 1. P0-B：前台服务没起来 → cached 状态下被系统掐网（含完整证据链）

这是本轮安卓侧最重要的发现，**同时解释了 A2 下载中断和 A7 发送失败两个现象**。

### 1.1 现象（logcat 实证）

`adb logcat -d -s EverSend:V`，非过滤全量 20803 行（22:15:54–22:24:30）统计：

```
发现通道 单播应答：发往 /192.168.110.x 失败：IOException: sendto failed: EPERM (Operation not permitted)
→ 共 2777 条；不同目标地址 254 个；每轮 256 条 = {组播 1, 广播 2, 单播 253}
```

第二轮窗口（22:23–22:33）再抓到 1280 条。burst 时间线与系统事件**逐条对得上**：

| 时刻 | 事件（来源） | 网络发送 |
|---|---|---|
| 22:11:01 | `PowerManagerService: Going to sleep due to timeout`（熄屏） | — |
| 22:17:54–58 | 应用启动后第一轮公告，屏幕仍灭 | **全 EPERM**（912 条） |
| 22:18:02 | `Waking up from Dozing (WAKE_REASON_WAKE_KEY)`（亮屏） | — |
| 22:18:18–30 | 启动/首包/退避公告（前台） | **正常**（无 EPERM） |
| 22:18:32.339 | `Going to sleep due to timeout`（再熄屏） | — |
| 22:18:38–22:19:36 | 每 16s 一轮公告 | **全 EPERM**（256 条/轮） |
| 22:19:41.67 | `Waking up … POWER`（亮屏） | — |
| 22:19:47 | `听到公告 … name=xgl web=52119 来自 192.168.110.41（组播）` | 接收/发现恢复正常 |
| 22:25:21.24 | `ActivityTaskManager: START … documentsui/.picker`（点「📎传输文件」） | A7 用例开始 |
| 22:25:22.68 | `WifiMulticastLockManager: uid=10292 importance changed=400`（**CACHED**） | — |
| 22:25:27.68 | `InetDiagMessage: **Destroyed live tcp sockets for uids={10292}**` | **系统销毁 App 的 TCP socket** |
| 22:25:38–22:26:27 | 每 16s 一轮公告 | **全 EPERM**（4 轮 × 256） |
| 22:26:46.08 | `importance=100`（**回前台**） | — |
| 22:26:47+ | 发送恢复正常（无 EPERM） | 正常 |
| 22:27:47.53 | 又进文件选择器；22:27:48.35 `importance=400` | — |
| 22:27:53.35 | `Destroyed live tcp sockets for uids={10292}` | — |
| 22:27:55.82 | 公告一轮 | **全 EPERM**（256 条） |
| 22:27:57.35 | `importance=100`（回前台） | 恢复 |

**结论：EPERM 的触发条件不是"屏幕亮/灭"，而是 App 的进程重要级掉到 CACHED(400)**——
熄屏（Activity 不可见）或切到文件选择器（另一个 Activity 到前台）都会触发；回到前台
(importance=100) 立即恢复。这与 Lead 在 22:25:38 亮屏期间抓到的 EPERM 完全一致
（那一轮正是文件选择器在前台）。

### 1.2 根因（为什么重要级会掉下去）

- `AndroidManifest.xml:46-49` 声明了 `.LinkService`（`foregroundServiceType="dataSync"`），
  但**运行时从未启动**：`adb shell dumpsys activity services cn.eversend.app` → `(nothing)`；
  `ps` 里只有 `cn.eversend.app` 进程（Activity），没有服务记录；logcat 无任何 `LinkService`
  相关行（`事件流/前台服务` 均 0 条）。
- 启动代码只在**设置页**的 Composable 里：

  ```kotlin
  // MainActivity.kt:2933-2940（基线）
  // 打开设置页就把保活服务拉起来（如果该开而没开）。
  LaunchedEffect(keepAlive, store.host) {
      if (keepAlive && normalizeHost(store.host).isNotBlank()) {
          startLinkService(context, store, true)
      }
  }
  ```

  `AppState.keepAlive` 默认 `true`（`MainActivity.kt:158-162`），但**用户不点进设置页就永远
  不执行**。于是 App 一直是一个"只有 Activity 的普通后台进程"，熄屏/切走就会被系统按
  cached 处理（Android 16 上还叠加了 doze/App Freezer 的收紧）。
- `LinkService.kt:19-22` 的注释写着"前台服务则可以在熄屏、锁屏、切后台时继续持有长连接
  （SSE），来了消息发通知。用户要的'只有手动断开或彻底退出才断开'，在这里是默认行为" ——
  **产品承诺和运行时行为不一致**，这是本 bug 的本质。

### 1.3 机制（EPERM 从哪来）

Android 16（该机是 crDroid 12.12，AOSP 16 基座）在进程进入 cached 后：
1. 通过 `InetDiagMessage.destroyLiveTcpSockets(uid)` **销毁该 UID 的所有 live TCP socket**
   （logcat 原文 `Destroyed live tcp sockets for uids={10292} in 3ms`）；
2. 对 UDP 发送在 netd/eBPF 层拒绝，`sendto()` 返回 **EPERM**（不是 EACCES；EACCES 才是
   "socket 没开 SO_BROADCAST"那种错误）—— 所以**组播/广播/单播一视同仁全失败**，
   这就是为什么错误里既有 `239.255.83.68` 也有 `255.255.255.255` 还有 254 个单播地址。
3. 进程回前台（importance=100/125）后规则撤销，发送立刻恢复 —— 时间线里恢复点误差 <2s。

> 注：定位"具体是哪个系统组件销毁 socket"需要 AOSP 源码级追溯；本机 dmesg 不可读
> （`dmesg: klogctl: Permission denied`），grep.app/raw.githubusercontent 均被网络侧拦截，
> 未能拿到调用方源码。但**触发条件—系统日志—发送失败—恢复**四段证据链完整，修复方向明确：
> 让进程保持前台服务重要级，系统就不会这样处理。

### 1.4 影响

- **稳定性**：App 在后台期间（熄屏、切文件选择器、切其它 App）**完全收不到也发不出**；
  电脑端 15s 后把手机标成离线。这正是"息屏/切后台就断"的用户抱怨。A2 的 100MB 下载
  在 73MiB 被 abort（`Software caused connection abort`）是同一根因（socket 被销毁）。
- **速度/耗电**：EPERM 期间 app 仍在每 15s 重试 254 个包 + 每 20s 扫 254 个 TCP 连接，
  白白唤醒 Wi-Fi/CPU。
- **显示准确性**：失败被 `record()` 逐包写进 `fatal`，设置页"最近错误"被刷成
  `发送失败（EPERM）`，用户看到的"发现故障"其实是"你切了个文件选择器"。
- **A7 的令牌 403 与后台掐网叠加**：文件选择器期间后台 socket 被销毁，回到 App 后
  `uploadToDevice` 的 403 恢复路径又崩在主线程（P0-A），形成"必失败"组合。

### 1.5 修法（阶段2 已实施，见 §9）

1. **App 启动 + 每次连上电脑就 `startForegroundService`**（不依赖设置页），由 keepAlive 开关控制；
   `MainActivity.Root()` 里 3s 幂等轮询（`startLinkService` 本身幂等，只有地址变化才重发）。
2. `LinkService` 拿 **`WifiManager.WifiLock(WIFI_MODE_FULL_HIGH_PERF)` + `PowerManager.PARTIAL_WAKE_LOCK`**，
   熄屏保持无线电/CPU（用户显式开启常驻，代价可接受；关掉开关即释放）。
3. **FGS 类型 dataSync → specialUse**：Android 15+ dataSync 有"每 24h 累计 6h"配额，
   到点系统强杀，与"常驻"冲突；manifest 补 `FOREGROUND_SERVICE_SPECIAL_USE` + `<property>` 用途声明。
4. 保留 `POST_NOTIFICATIONS` 申请；被拒也不影响 FGS 启动（通知只是不显示）。

### 1.6 验证方法（60s 熄屏对照实验，Lead 已授权 ADB 写操作）

```bash
S=2430d3c
# 0) 新 APK 装上后，确认 FGS 真的起来了
adb -s $S shell 'dumpsys activity services cn.eversend.app' | grep -E "ServiceRecord|isForeground|foregroundId"
adb -s $S shell 'dumpsys activity processes | grep -A2 cn.eversend.app | grep -i importance'   # 期望 125
# 1) 清日志，灭屏 60s，再亮屏
adb -s $S logcat -c
adb -s $S shell input keyevent 26          # 熄屏
sleep 60
adb -s $S shell input keyevent 26          # 亮屏
adb -s $S shell wm dismiss-keyguard 2>/dev/null
# 2) 判据（全部满足才算通过）
adb -s $S logcat -d -s EverSend:V | grep -c EPERM        # 期望 0（基线期间 256/轮）
adb -s $S logcat -d -s EverSend:V | grep -E "听到公告" | tail -3
adb -s $S shell 'dumpsys activity services cn.eversend.app' | grep -c ServiceRecord  # 期望 ≥1
# 3) 同时电脑端 /api/state 看这台手机在线（Lead 侧执行）
curl -s http://192.168.110.41:52119/api/state | grep -o '"name": "MI 9"' | head -1
# 4) 恢复常亮设置（实验后）
adb -s $S shell svc power stayon true
```

## 2. P0-A：令牌自动恢复崩在主线程（A7 必失败）

- **证据**：真机 logcat 22:28:12
  ```
  W EverSend: 发送失败（准备换令牌）: 缺少或错误的防跨站令牌，请刷新页面
  W EverSend: refreshToken 失败: NetworkOnMainThreadException: null
  ```
- **代码**（基线 `MainActivity.kt:504-530`，`sendPending` 里的局部 `suspend fun deliver()`）：
  ```kotlin
  } catch (problem: Exception) {
      val why = problem.message ?: problem.javaClass.simpleName
      if (why.contains("令牌") && api.refreshToken()) {   // ← 裸在主线程
          withContext(Dispatchers.IO) { uploadToDevice(...) }
          true
      } else throw problem
  }
  ```
  `deliver()` 由 `scope.launch`（`rememberCoroutineScope`，Main dispatcher）驱动；
  第一次 `uploadToDevice` 有 `withContext(Dispatchers.IO)`，但 **catch 里的
  `api.refreshToken()` 没有**。`refreshToken()` → `fetchToken()` → `HttpURLConnection`
  → `NetworkOnMainThreadException`（`ApiClient.kt:48-64` catch 住并返回 false）。
- **影响**：令牌过期（电脑端重启必换令牌）时自动恢复链路 100% 失效；界面只能提示
  "回设备列表重新点一下"，A7 用例 FAIL、电脑接收目录为空。这是**每次 PC 重启后的必现 bug**。
- **修法（已实施）**：`refreshToken()` 包 `withContext(Dispatchers.IO)`；换到令牌后原样重试一次；
  仍失败才抛给外层给可执行错误。
- **`MainActivity.kt:6876` 的 `ApiClient.fetchToken(base)` 排查**：该调用在 `suspend fun
  connectToDesktop()` 里，两个调用点都在 IO 上下文（`2971` 行 `withContext(Dispatchers.IO)`；
  `6421` 行 `runBlocking { connectToDesktop(...) }` —— 这是仪器测试/工具路径，非主线程 UI 路径）。
  **结论：非主线程 bug**，无需改。
- **验证**：重启电脑端（换令牌）→ 手机发一张 >300KB 图 → 电脑接收目录出现且 sha256 一致；
  logcat 应出现 `refreshToken: 换到新令牌 xxxxxxxx` 且随后上传成功，无 `NetworkOnMainThreadException`。

## 3. P1-C：发现通道（EPERM 降噪 + 洪泛 + 扫描退避）

### 3.1 单播洪泛（`Discovery.kt:406-412` 基线）

```kotlin
unicastSocket?.let { socket ->
    // 广播被 ROM / 交换机拦掉时的第二条路：把本网段每一台主机各敲一下。
    // 254 个小包，几十毫秒的事。
    for (host in subnetHosts(nic)) {
        send(socket, payload, host, PORT, nic, DiscoveryChannel.Unicast)
    }
}
```

- 这行代码在 `announceOnce()` 的 `for (nic in interfaces())` 里，且 `announceLoop()` 首包/退避后
  **每 15 秒**（`STEADY_MS=15_000`）调一次；每次进入还会被"定期公告"线程再调。
- 实测结果（`adb shell ip neigh`）：邻居表里**上百条 `FAILED`**（真机现网截图见
  `.testscratch/android-device/full-logcat2.txt` 与现场采集），全是这 254 个包轰出来的 ARP。
- 代价：每 15s 254 包/网卡 ≈ **1016 包/分钟**（单网卡）；EPERM 时每包一条 log + 一次
  `publish()`（StateFlow 更新 → Compose 重组），一轮公告就 256 条日志、256 次 UI 刷新。
- 收益：只在"广播/组播都被网络拦掉"时才有意义 —— 而真机上组播是通的（电脑公告能收到），
  这 254 包纯属无效流量。

**修法（已实施）**：平常只发 组播×1 + 全局广播×1 + 定向广播×1；只有连续 2 轮**没有**
可连设备、且距上次 ≥60s 才做一次单播兜底扫。包数对比：

| 场景 | 改前 | 改后 | 降幅 |
|---|---|---|---|
| 公告 UDP（组播+广播+单播） | 254+3 = 257 包/轮 × ~4 轮/min ≈ **1028 包/min** | 3 包/轮 × 4 ≈ **12 包/min**（无设备时才 +254/60s≈4 包/min） | **-98% 以上** |
| 主动 TCP 扫描 | 254 连接/**20s** ≈ 762 连接/min | 254 连接/20s → 40s → … → 300s 退避（无设备时），有设备即停 | **-80%～-97%** |
| EPERM 日志 | 256 行/轮 | 每通道 5s 最多 1 条 + 本轮失败计数 | **-99%** |

### 3.2 EPERM 日志刷屏 + UI 被覆盖（`Discovery.kt:417-442`、`553-558` 基线）

- `send()` 的 catch 对**每个包**调 `record(channel, ...)`；`record()` 做三件事：
  `lastError[channel]=…`、`fatal=…`、`Log.d`、`publish()`。
- EPERM 时一轮 256 包 → 256 行 logcat + 256 次 `_stats.value=`；设置页"发现通道"里
  `lastError` 显示最后一条（地址随机），用户无法知道"本轮全挂了"。
- **修法（已实施）**：发送失败走 `throttledSendError()`——同通道 5 秒最多一条，内容是
  "发送失败（本轮已 N 个包）：异常：例目标地址"；不再逐包 `publish`/覆盖 fatal。

### 3.3 主动扫描计数 266/254（`ApiClient.kt:563-592` 基线）

- 日志：`TCP 扫描结束：探了 266/254 台，找到 0 台`，266 = 12 + 254。
- 原因：`preferred.take(12)` 串行先探 12 个并 `probed.incrementAndGet()`；随后 worker 线程池
  对 `candidates`（254 个，**没有排除刚探过的 12 个**）再全探一遍 → 计数与网络请求都多 12。
- 影响：计数自相矛盾（"探了 266/254 台"），测试/诊断口径不可信；多 12 条连接。
- **修法（已实施）**：worker 跳过 preferred；日志口径 = `probed/candidates`（另把
  `lastTcpScanProbed` 只在扫描结束时写一次，避免并发扫描互相覆盖）。
- 备注（未改，属服务端/协议范围）：`discoverByTcp` 在 `found.size >= 4` 时提前 return@execute，
  但已排队的任务仍会跑完，早退判据在"4 台以上设备"的办公网会多探——对家用 /24 无影响。

### 3.4 "扫了 266 台没有应答"但组播正常（22:18–22:19 的日志）

- 那几轮扫描正好落在 §1 的 **cached 窗口**里：TCP connect 也被系统掐（回到 App 后
  `nc 192.168.110.41 52119` 从手机 shell 立刻 200 OK，证明网络本身通）。
- **判据**：不能把这句日志当成"扫描代码坏了"；要看当时 App 是否前台/FGS 是否在跑。
  修完 P0-B 后同样条件下扫描应命中（见 §9 验证）。

## 4. P1-D：下载中断（A2 的 73MiB 回滚）

- 现象：100MiB 从电脑下载到手机，切到文件选择器再回来 → `Software caused connection abort`，
  已下的 73MiB 全废（MediaStore pending 行被删）。
- 直接原因：cached 时系统销毁 TCP socket（§1 的 `Destroyed live tcp sockets`）；
  次因：客户端**没有任何重试/续传**（基线 `ApiClient.kt:161-179` 无 Range；`Fetch.kt:206-272`
  一次失败就把 pending 删掉）。
- **修法（已实施）**：
  1. FGS+wakelock 让下载期间 socket 不被销毁（治本）；
  2. `ApiClient.download(path, sink, offset)` 支持 **HTTP Range + 206** 校验
     （服务端 `/api/share`、`/api/chat/media`、`/api/download` 均支持单段 Range，见
     `src/eversend/web/server.py:3190-3244`）；
  3. `FetchManager.run()` 改为：先写 `cacheDir/fetch-<id>.part`，断线后按 `/api/share` 的
     **Range 从 .part 长度续传**（最多 4 次、0.5s/1s/2s 指数退避），收满校验字节数后再发布到
     MediaStore「下载」（失败仍回滚 pending 行，不会留半截文件）。
- 代价：多一次本地拷贝（cache → MediaStore），100MB 在 UFS 2.1 上约 +0.5–1s；换来断点续传。
- 验证：`adb shell am start` 文件选择器（或直接切后台 30s）→ 下载应自动续传，logcat 出现
  `下载中断（第 N 次尝试，已落 X/Y 字节）` 且最终 `已保存到手机`、**sha256 与电脑一致**
  （`adb shell sha256sum /sdcard/Download/<name>` vs 电脑端 `sha256sum`）。

## 5. P1-E：保活链路（SSE / 心跳 / 退避）

| # | 位置（基线） | 问题 | 影响 | 修法 |
|---|---|---|---|---|
| E1 | `ApiClient.kt:243` | `conn.readTimeout = 0`（SSE 永不超时） | Wi-Fi 切换/路由器掉 NAT 表但没有 RST 时 `readLine()` 永久阻塞：事件永远不来、LinkService 的 catch/重连/心跳全部轮不到执行，界面一直"已连接" | 改 90s；连续 2 次超时（180s 无任何数据）抛 IOException 触发重连。电脑端每 15s 发 `: ping`（`web/server.py:83` `SSE_HEARTBEAT=15.0`），90s 足够松 |
| E2 | `LinkService.kt:155-168` | 心跳写在 `events()` **之前**：SSE 一旦连上就阻塞在 `events()`，心跳再也不发（注释写"每 5 秒一次"实际是"每次重连一次"） | 电脑端 15s 判离线；"明明连着却掉线" | 拆出独立心跳线程 `heartbeatLoop()`：SSE 通不通都每 5s `/api/state`，顺带 `offerShares()`（熄屏时界面轮询停了，这是唯一还在收文件的地方） |
| E3 | `LinkService.kt:204` | `Thread.sleep(minOf(backoff, 1000L))`：永远 ≤1s，指数退避形同虚设 | 电脑端重启/断网时每秒一次重连风暴 | 改成真退避 `sleep(backoff)`，1s→2s→…→15s 封顶 |
| E4 | `LinkService.kt`（基线） | 全程无 WifiLock / PARTIAL_WAKE_LOCK | 熄屏后 Wi-Fi 进 DTIM 省电（现场抓到 `IpClient.wlan0: set max DTIM multiplier to 9`）、CPU 可 Doze 休眠；SSE/续传变慢或断 | 前台服务期间持有 `WIFI_MODE_FULL_HIGH_PERF` + `PARTIAL_WAKE_LOCK`，onDestroy 释放 |
| E5 | `ApiClient.kt:67-89` | 每个请求新建 `HttpURLConnection` 并 `disconnect()`；`useCaches=false` | 状态轮询 3s/心跳 5s 每次都要新 TCP 握手；`disconnect()` 会放弃 keep-alive 复用 | 保留（HTTPS 无、握手 <1ms，影响小）；后台改造成 OkHttp/连接复用属"速度优化"可选 |

## 6. 速度审计（手机↔电脑 HTTP 链路）

| 项 | 现状（基线） | 评估 | 建议 |
|---|---|---|---|
| 上传是否流式 | ✅ `setFixedLengthStreamingMode(length)` + 64KiB buffer 从 Content URI 直读（`ApiClient.kt:118-159`） | 不整份入内存，好 | 保持；续传后每段同样流式 |
| 下载是否流式 | ✅ 64KiB buffer 直写 sink（`ApiClient.kt:161-179`） | 好 | 保持 |
| Range/续传 | ❌ 基线没有 | 弱网/切后台必从头再来 | **已加**（§4） |
| 缓冲区 | 64KiB | 局域网 24MB/s 下没问题；可在 256KiB 时再测（收益通常 <2%） | 可选 A/B |
| 单连接 vs 并发 | 单文件单连接 | 受服务端单流限速影响更大（PC→手机实测 ~24-25 MiB/s，已接近 WiFi 上限） | 不属安卓侧 |
| HTTP 层开销 | 明文 HTTP/1.1，无 gzip；每次请求新建连接 | `/api/state` 3KB JSON 每 3s 一次，开销可忽略 | 无需改 |
| 文件读写 | 下载先写 MediaStore pending，直接写 Provider 输出流 | Provider 写路径比裸 File 略慢（真机未单测） | 已改为 cache.part + 一次性发布：续传需要，且对"100MB 级"额外拷贝 <1s |
| 上传失败重试 | ❌ 基线一次性、失败即整份重来 | 手机 WiFi 抖动时浪费全部已发字节 | **已加断点续传**（`caps.uploadResume` 协商，见 `http-upload-resume-api.md`） |
| 上传并发 | 多文件/多设备逐个串行（`sendPending` 里 for 循环） | 多目标时慢，但省带宽、行为可预期 | 本轮不动（改动面大，收益低） |
| 显示用进度频率 | 上传无 UI 进度；下载每 400ms 更新 | 400ms=2.5Hz 合理 | 保持 |

## 7. 显示准确性审计（进度/速度/ETA 的数据来源）

**数据来源分两类**，这是最容易出现"数字对不上"的地方：
- 电脑↔电脑/电脑↔手机的**传输**：手机显示的是 `/api/state` 的 `transfers[]` 字段
  （`percent/speed/eta/done/total`），**由服务端计算**；安卓只做单位换算（`percent / 100f`）。
  逐条核对（基线 `MainActivity.kt:2449-2478`、`2582-2598`、`4068-4071`）：
  `percent` 0-100 → Float 除 100 ✅；`speed` 单位字节/秒 → `fmtSpeed` 转 MB/s ✅；
  `eta` 秒 → `fmtEta` ✅；未发现把 KB/MB、0-1/0-100 弄混的地方。
- **手机自己下载电脑文件**：`FetchTask` 本地计算（`Fetch.kt:44-81`、`206-237`）：
  `percent = got/size`；`speed = (sent-lastBytes)*1000/(now-lastAt)` 400ms 窗口；
  `ETA = (size-got)/speed`，`speed<=0` 或 `left<=0` 时显示"—"。量纲正确。

**发现的问题/风险**：
1. **接收方向不显示速度/ETA**：`MainActivity.kt:2457-2459` 只写 `status="接收中 N%"`、
   `extra=""`；发送方向（电脑端 transfers）有 `fmtSpeed + fmtEta`。同一张表两种精度，
   用户在"接收"时看不到速度和剩余时间 —— 显示完整性缺陷（P2）。
2. **同一个文件有两套进度源**：`fetches`（本地下载队列）与 `/api/state` 的
   `transfers`（服务端视角）。页面把 `fetchTransfers(fetches)` 造进 transfers 一起显示
   （`Fetch.kt:354-367`），如果同一次"电脑发给手机"在服务端也登记为 transfer，
   两行可能同时出现且百分比来自不同时钟（服务端按转发进度、本地按落盘进度）——
   本轮真机未复现"双行"，但代码路径存在；建议 Lead 用 `pc 发 100MB → 手机截图` 的方式验证
   （本次 GUI 探针正在做）。
3. **`fetch` 进度更新对象 `startedAt` 丢失**：`Fetch.kt:224-234`（基线）用 `publish(FetchTask(...))`
   新建对象时 `startedAt = task.startedAt`，而 `run(api, task)` 的 `task` 是入队时的原始对象
   （`startedAt=0`）→ 进度行 startedAt 恒 0。当前 UI 该行时间取 `share.added`，所以未暴露；
   若以后用 startedAt 显示会出错（P3）。
4. **EPERM 日志覆盖"真正错误"**：见 §3.2，`fatal` 被逐包覆盖导致设置页"最近错误"显示的是
   随机地址的发送失败，不是根因 —— 修完降噪后该字段才有参考价值。
5. **进度到 100% 后状态残留**：下载完成走 `FetchState.Done` 并删除 pending 行；
   上传完成（A7）基线因 P0-A 直接失败，不存在"100% 残留"；断点续传加入后新增
   `error="连接中断，正在从断点续传…（第 N 次）"` 会显示在行上，完成时清空 —— 需真机验收
   确认不会卡在"续传中"字样（已在失败/成功分支都置 `error`）。

## 8. 发现机制（Discovery.kt / ApiClient 发现部分）其他核对

- 三条通道：mDNS（NsdManager，API34+ 才有 host）、组播/广播公告、TCP 全 /24 扫描；
  **组播锁已拿**（`multicastLock=true`，现场 log 有），组播接收正常（`听到公告…（组播）`）。
- `openSocket()` 已设 `socket.broadcast=true`，EPERM 与 SO_BROADCAST 无关（那是 EACCES）——
  证据：同一 socket 在前台时发送正常。
- `interfaces()` 现网正确（log：`网卡=wlan0(192.168.110.158)`），本机没有"多网卡挑错"问题；
  `ip link` 里 `dummy0`/`rmnet_data*` 因 `isVirtual`/无 IPv4 被正确排除。
- `subnetHosts()` 对 `/24` 生成 .1–.254；对 `prefix>24` 直接跳过（点对点链路）——逻辑正确。
- `discoverByTcp` 的 TCP probe 有 `Connection: close` + Content-Length 读取，不会因 keep-alive
  等 EOF（注释里记了真机教训）—— 实现正确。
- 主动扫描无设备时**每秒都在试**的问题见 §3.3/§3.1；修完退避后无设备场景的探测频率大降。

## 9. 阶段2 修复清单（已改代码，待验收）

| 文件 | 改动 |
|---|---|
| `MainActivity.kt` | ① `refreshToken()` 包 `withContext(Dispatchers.IO)`（P0-A）；② `Root()` 新增"启动/连上即启 FGS"3s 幂等循环 + `UiState.linkHost`（P0-B）；③ `uploadToDevice()` 改为按 `caps.uploadResume` 协商的**断点续传**（202/409/status 跟 offset、6 次指数退避、旧服务端自动退回一次性上传） |
| `LinkService.kt` | ① 独立心跳线程（每 5s `/api/state` + `offerShares`）；② 真指数退避（1s→15s）；③ 新增 `WifiLock(HIGH_PERF)` + `PARTIAL_WAKE_LOCK` 获取/释放；④ FGS 类型 `dataSync`→`specialUse`（API34+） |
| `ApiClient.kt` | ① `events()` `readTimeout=0`→90s + 连续 2 次超时重建连接（P1-E）；② `download()` 加 `offset`/`Range`/206 校验（P1-D）；③ `upload()` 加 `uploadId/offset/total` + `X-EverSend-Total`，202/409 返回给调用方续传；④ 新增 `uploadStatus()`；⑤ `skipFully()` 兼容受控流 |
| `Fetch.kt` | ① 下载改 `cacheDir/.part` + Range 续传（4 次、0.5/1/2s 退避）+ 收满再发布 MediaStore；② 失败清理 .part、成功删除；③ 进度原地更新带速度/ETA |
| `Discovery.kt` | ① 取消"每轮 254 单播"，改"连续 2 轮无设备且 ≥60s 才单播兜底"（-98% 包数）；② 发送失败限流+聚合（5s/通道、本轮失败数）；③ `scanLoop` 20s→300s 指数退避；④ 常量 `SCAN_MAX_PERIOD_MS/UNICAST_SWEEP_MS/SEND_ERROR_LOG_GAP_MS` |
| `AndroidManifest.xml` | ① 加 `FOREGROUND_SERVICE_SPECIAL_USE` 权限；② LinkService `foregroundServiceType="dataSync\|specialUse"` + `<property>` 用途声明 |

## 10. 待验证 / 未测（不猜）

1. 第二版 APK 的真机行为（A0/A1/A2/A6/A7/A8 回归 + 60s 熄屏实验 + 下载中断续传）——**Lead 装机后由我按 §1.6 执行并回填数字**。
2. "cached 状态下系统销毁 socket" 的 AOSP 调用方源码未拿到（网络受限，见 §1.3 注）；修复不依赖它。
3. 多网卡/热点/蜂窝同时在线场景（上一台 vivo 的 wlan1 僵尸网卡问题）本轮 MI9 单网卡未复现，未测。
4. 上传断点续传的真机分段效果（需人为断网/切后台触发）——新 APK 装机后配合 Lead 用例测。
5. 缓冲区 64KiB→256KiB 的吞吐 A/B、并发上传是否提速：未测，属优化建议非缺陷。
6. 显示准确性 #7.2 的"双进度源"是否在真机同时出现：等 Lead 的 GUI 探针截图对照。

## 11. 原始证据文件

- 全量 logcat 两次抓取：`.testscratch/android-device/{full-logcat.txt,full-logcat2.txt,now-logcat.txt}`（`adb logcat -d`，未提交）
- 工具链/构建日志：`~/android-dev/logs/{install.log,build.log}`
- 构建复现：`tests/reports/opt-1017/android-build.md`
- 上传续传协议：`tests/reports/opt-1017/http-upload-resume-api.md`

## 12. v2 真机装机后的新证据（2026-10-17 23:18 安装，v2=sha256 9cae120c…）

- **EPERM 在"App 可见但无 FGS"时不再出现**：v2 装好后（首次运行停在系统通知权限弹窗，
  App 处于"可见但未前台"状态）跑了 3 分钟，logcat **0 条 EPERM**；发现引擎日志显示
  `组播锁=true、网卡=wlan0(192.168.110.158)`、公告每 ~15s 一轮正常、`听到公告…（组播）` 正常。
  与基线（同状态下每轮 256 条 EPERM）对比，说明 §1 的降噪与 P0-B 方向正确。
- **FGS 启动条件发现缺口（v2 已修一半，v3 修完）**：v2 的自动启动循环要求 `host.isNotBlank()`，
  新装 App 的 `store.host` 为空 → FGS 仍没起来（`dumpsys activity services cn.eversend.app` → `(nothing)`）。
  v3 起改为 **keepAlive=true 即启动 FGS（host 可空，先跑发现）**，连上后 3s 内自动补地址。
- **`TCP 扫描 266/254` 在 v2 真机日志复现**：`TCP 扫描结束：探了 266/254 台，找到 0 台`。
  原因确认就是 §3.3 的 preferred(12)+worker(254) 重复计数；v3 已修（worker 跳过已探地址）。
- **启动瞬间单播兜底扫过早**：v2 启动 2.5s 就打了 `单播兜底扫一轮：254 个地址`（原意是
  "连续 2 轮无设备且距上次 ≥60s"）。原因：`lastUnicastSweepAt` 初值 0，启动即满足 60s 冷却。
  v3 已在 `start()` 里把冷却起点设为启动时刻。
- v2 装机时 `host` 为空 + 电脑 QA 实例未在 52119 → 无法验证 FGS 起来后的完整链路，
  该项由 Lead 的 A0/A1/A2/A6/A7/A8 回归与 §1.6 的 60s 熄屏实验覆盖。

## 13. PG-1～PG-6（手机 GUI 显示准确性，Lead 探针证据 → v3 修复）

证据：`tests/reports/opt-1017/realdevice-gui-findings.md` + `gui-probe-shots/before-oldapk/{0036,0040,0042}.png`。

| # | 现象（旧 APK） | 根因（file:line 基线） | v3 修法 |
|---|---|---|---|
| PG-1 | 完成 3.7s 后卡片仍「接收中…」+ 操作仍「点击保存到手机」 | `MainActivity.kt:3950`（file 卡片 detail 是**死文案**，永远写"点击保存到手机"）；完成状态只看服务端 `message.state`，而手机自己下载的完成态在 `FetchTask` | 文件卡片/气泡按 `FetchTask` 状态渲染：`Done → "已保存到手机"`（图标 ✅）、保存按钮变「已保存」且不可点；进度行在完成时显示已完成 |
| PG-2 | 41.8% 时卡片只有「接收中…」，无百分比/速度/ETA | 气泡进度只从服务端 `transfers` 找（`liveTransferOf`），而"电脑交给手机"的文件是**本机下载**，服务端没有对应 transfer | `Bubble` 新增 `fetches` 参数；下载中直接显示 `FetchTask.progressText`（百分比 + 已收/总 + 速度 + ETA）+ 进度条；`fetchTransfers()` 补 `speed/eta` 字段给合成 transfer 用 |
| PG-3 | 单文件已下载 41.8% 时批量条目仍「正在排队」 | `MainActivity.kt:2841-2862`：`running = tasks.any { it.active }`，只认"已匹配到的 tasks"；匹配为空时误判成排队 | 同时看全局 `ui.fetches` 的 active；运行中显示「正在接收 · 百分比 · 已收/总 · 速度 · ETA」，标题改「正在接收电脑发来的文件…」 |
| PG-5 | 会话标题露内部设备号「QA-真机验收 与 00254c4971c16f15c4bcd7982900f3cb…」 | `MainActivity.kt:1680/1726` 直接渲染服务端 `displayTitle/title`；`chatMembersLabel` 也直接拼 `member.name`（认不出时就是 id） | 新增 `readableTitle()`/`humanDeviceName()`/`looksLikeInternalId()`：标题里 16–64 位 hex 设备号换成设备名，查不到写"一台设备"；成员名同样去 id 化 |
| PG-6 | 100 MiB 显示「100.0 MB」/512 KiB 显示「512.0 KB」 | `fmtSize()` 用 1024 进制却标 SI 单位 | 单位改 IEC：`B/KiB/MiB/GiB/TiB`（与 gui-desktop 对齐，保留 1024 进制语义，只改标签） |
| PG-4 | 失败状态/待发区残留一直挂着 | 失败消息保留是**有意的**（待发区可重试/放弃），但顶部 `ui.error` 在连续刷新失败时不会被清 | 未改（低优，等 Lead 决定是否加"失败清空"入口；待发区本身已有重试/放弃） |

v3 验收仍按 `realdevice-gui-findings.md` 的 4 条判据 + `realdevice_probe --label after-newapk`。
