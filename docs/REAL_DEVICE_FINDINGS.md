# 真机联调发现 · Real-device findings（vivo V2408A / Android 16）

> 这份是**在真手机上跑出来的**事实，不是推测。每条都带复现方式与原始证据。
> 接手的每个人**先读这份**，再动代码。范围：PC ↔ 手机（App 与浏览器两种形态）。

## 0. 现场

| 项 | 值 |
|---|---|
| 手机 | vivo **V2408A**，Android **16**，arm64-v8a，IP `172.20.90.82/24` |
| 手机另一块网卡 | `wlan1` = `172.20.110.78/24`（**有 IP、rx/tx 全为 0 的僵尸网卡**） |
| 蜂窝 | `rmnet_data3` = `10.19.5.115/29` |
| 电脑 | `172.20.90.177`，TCP 52117（传输）/ 52119+52120（网页）/ UDP 52118（发现）/ 5353（mDNS） |
| ADB | `export PATH=/home/xgl/android-dev/sdk/platform-tools:$PATH`，设备号 `10AEBS3L74002AA` |

**手机屏幕已设为常亮**（`svc power stayon usb` + `stay_on_while_plugged_in=7`）。
用 adb 驱动手机：`adb shell input tap X Y`（截图 1080×2376）、`adb exec-out screencap -p`、
`adb shell uiautomator dump` 取控件树。**这些操作都已经被验证可用。**

## 1. 已修复并验证（不要再"修"一遍，改动前先看 git log）

### 1.1 本机冒充成一台设备（用户截图里的 `xgl / 127.0.0.1`）
`Engine.devices()` 原来直接返回发现表，**没有滤掉本机自己**。本机一旦进表
（自己公告回环收到／入站连接带本机设备号／`add_manual_device("127.0.0.1")`），
它就以**自己的名字**出现在「在线设备」里，还会被自动选中 ——
用户点手机却看到自己的会话，观感就是"与手机的对话不显示"。
**修在源头**：`core/engine.py` 的 `devices()` 现在按 `device_id` 过滤自己。
验证：注入本机公告后，发现表含本机、`devices()` 不含。

### 1.2 终端刷 traceback
安卓 HTTP 客户端中途断连（切后台、取消请求）时，`socketserver` 默认
`traceback.print_exc()`，用户终端刷出 20 行红字 `ConnectionResetError: [Errno 104]`。
**修**：`web/server.py` 的 `_ThreadingServer.handle_error` 把
连接重置／管道断开／超时降为一行 debug，其余走 logger。验证：新一轮 `Traceback` = 0。

### 1.3 安卓"必须手输 IP"的根因（真机）
`NetworkInterface.getNetworkInterfaces()` 在这台机器上**时好时坏**：
第一次启动拿到 `wlan1`（僵尸网卡）+ 蜂窝，**漏掉真正在用的 `wlan0`**。
后果：组播/广播从错网卡发出（电脑收不到），主动扫描扫的是 `172.20.110.0/24`
与 `10.19.5.x`（日志原文：`探了 508 台，命中 0 台`），**压根没扫 `172.20.90.0/24`**。
**修**：`Discovery.kt` 以 `ConnectivityManager.getLinkProperties` 的系统当前网络为准，
与枚举结果求并集；`ApiClient.subnets()` 同样处理；并加「网卡全景」诊断。
验证（真机）：`网卡=rmnet_data3(10.19.5.115),wlan0(172.20.90.82)`，
**0.15 秒**内组播+广播+单播听到电脑，自动连上、零输入。

## 2. **未修复的 P0**：发给手机的文件是假的

### 2.1 复现
1. 电脑端点「传输文件」选一个大文件（试过 11 MB / 572 MB / 762 MB）；
2. 按「发送」；
3. 现象：
   - 会话里**出现**那条文件气泡（`文件名 + 大小` + `预览 ⋯`）—— 这一步是对的；
   - 但**没有进度条、没有百分比**，从头到尾什么都没有；
   - 电脑端待发区**发送后没有清空**（状态栏还写着「已加入 1 项…回车发给 V2408A」）；
   - 手机 `/sdcard/Download` 里**一个文件都没有**；
   - 聊天库里那条记录却写着 `state=sent`。

### 2.2 根因（已钉死，不要再猜）
发送按钮接的是 `MainWindow._compose_send`（`main_window.py:2016`），
选中**单台设备**时它走：

```python
if text or paths:
    self.chat_view.send_items(text, paths)   # ← 把文件当成"聊天消息"发
```

`chat_view.send_items` → `engine.send_chat(conv_id, kind="file", media_path=...)`，
也就是**聊天投递通道**，不是文件传输通道。于是：

- `engine.active_transfers()` **全程为空** → 传输页与会话都刷不到进度（已用
  `EVERSEND_DEBUG_PROGRESS=1` 实测 35 次采样，`active=[]`）；
- 聊天通道把一条"带大附件的消息"认为投递成功 → 记录写成 `sent`；
- 文件**从来没有被传给手机**。

同一个函数里还有第二个入口 `MainWindow._send()`（`main_window.py:2053`），
它才是文件传输路径（`_send_to_peer` / `_hand_off_to_phone`）——
**但按「发送」根本不会走到它**（已用 `EVERSEND_DEBUG_SEND=1` 验证：诊断一行都没打）。

### 2.3 手机是纯 HTTP 客户端（这决定了正确的修法）
`adb shell` 查 `/proc/net/tcp6`：**App 进程没有任何 LISTEN 端口**。
所以电脑**推不过去** —— 正确的形态只有一种：

> 电脑把文件**发布**出来（`/api/...` 分享表），手机**主动取**；
> 电脑这一侧是 HTTP 服务端，**"传到哪了"只有它知道** → 进度由它报。

### 2.4 要求做到的样子（验收口径）
1. **按「发送」发文件时，文件必须走文件传输通道，不允许走聊天消息通道**；
   手机这条路径 = 发布 + 手机来取。
2. **记录状态必须诚实**：手机真正取走之前**不许写 `sent`**。
   依次是 `sending` →（已发布会话里写）「已交给手机，等它下载」→ 手机取走后才 `sent`。
3. **进度要看得见**：会话里那条气泡 + 传输表的行都要有**进度条与百分比**
   （形如 `⬇ 手机正在取 42% · 120 MB / 572 MB`）。进度来源 = 服务端发出去的字节数。
4. **发送后待发区要清空**。
5. **失败要明说**：手机掉线、文件读不到、端口被占 —— 都要在会话与状态栏给出中文原因，
   不许静默变 `sent`。

### 2.5 已经铺好的半成品（可以直接用，不要重复造）
- `web/server.py`：分享条目带 `sentBytes`；`note_share_progress(share_id, sent)`；
  `_stream_file(..., on_progress=...)` 每写一块就回调；`shares()` 里带 `sentBytes`。
- `main_window._refresh_phone_shares()`：把 `sentBytes/size` 换算成百分比，
  写进传输表的行**和会话里那条记录**（`chat_view.set_transfer_progress(name, pct, text)`）。
- `chat_view.set_transfer_progress(name, percent, text)`：按文件名找到会话里那条气泡写进度。
- **这些都还没在真机上跑通过一次真实的手机下载**，所以请当作"待验证"而不是"已完成"。

## 3. 已经验证可用的调试手段（直接用）

| 手段 | 命令 |
|---|---|
| 看手机日志 | `adb logcat -d -v time \| grep EverSend \| grep -v Posix_connect` |
| 手机截图 | `adb exec-out screencap -p > /home/xgl/x.png`（1080×2376） |
| 手机控件树 | `adb shell uiautomator dump /sdcard/u.xml && adb shell cat /sdcard/u.xml` |
| 点手机 | `adb shell input tap X Y`（坐标从控件树 bounds 取中点） |
| 电脑端 stderr | 起应用时重定向到 `/home/xgl/dbg/stderr*.log` |
| 任务路由诊断 | `EVERSEND_DEBUG_SEND=1`（`_send` 打印每个 peer 的 source/addr/kind） |
| 进度诊断 | `EVERSEND_DEBUG_PROGRESS=1`（打印气泡名单与活动传输名单） |
| 数据目录 | `/home/xgl/dbg`（**不要写仓库里的 `data/`**） |
| 起电脑端 | `display_panel_run`，或 `EVERSEND_HOME=/home/xgl/dbg PYTHONPATH=src .venv/bin/python -m eversend` |

**资源纪律**：Gradle 一律 `--max-workers=2`；模拟器与真机不要同时跑；`/tmp` 是 7.7 G 的
tmpfs（已被 DSH 壁纸缓存占 5 G），**测试数据放 `/home`**。

## 4. 不许回退的既有约定

- 三端文案与语义一致；`设备列表 / 会话 / 传输 / 接收 / 发送 / 设置 / 连接手机 / 扫描` 这些词不改。
- HTTP 路由、SSE 事件名、`EngineBridge` 信号名只增不改。
- 任何"看起来修好了"的结论都要有**可失败的断言**；真机证据要写清"谁复现的"。


---

# 真机联调发现 · 小米 9（MI 9 / Android 13 / cepheus）2026-09-26

> 第二次真机联调，换了一台机器（小米 9，USB + 同 WiFi 局域网 192.168.110.0/24）。
> 下面每条都是**这台机器上跑出来的**，带复现方式。adb 序列号 `2430d3c`。

## 现场

| 项 | 值 |
|---|---|
| 手机 | 小米 **MI 9**，Android **13**，arm64-v8a，`wlan0 = 192.168.110.27/24` |
| 电脑 | `192.168.110.41`，TCP 52117 / 网页 52119+52120 / UDP 52118 |
| 启动方式 | 电脑端在**显示器插件**里真启动：`EVERSEND_HOME=/home/xgl/qa-m9/home PYTHONPATH=src .venv/bin/python -m eversend` |

## 1. 已修复（每条都有真机证据或可失败断言）

### 1.1 「手机发文件给电脑」整条链路 500：`_accept_upload_body` 根本不存在
`web/server.py` 的 `_receive_upload` 重构时把"发给自己"（本机落地）那一支改坏了：调用了
**没有定义**的 `self._accept_upload_body(...)`，而且 `declared` / `spool` 在那一支里还没赋值。
**修**：把"空间预检 + spool + 流式写盘"抽成 `_accept_upload_body()`（三类目标共用），
`self` 分支补上 `declared`/`spool`。证据：`web/selftest.py` **358 项全绿**（修前
`FAIL uploading to the local machine is accepted … AttributeError`）。

### 1.2 手机发来的文件**不进「接收」列表、不进会话**
`land_upload()` 只落盘 + 发 `web_upload_finished`，桌面端**没人接**这个事件 ——
文件在磁盘上，界面上查无此人。
**修**：`bridge.py` 新增 `local_upload` 信号；`MainWindow._on_local_upload()` 落一行接收记录
（带发件人）并写一条 `kind=file/state=done` 的会话记录；`land_upload` 捎上 `from_id/from_name`。
证据（真机 20:45）：接收目录有 `phone-to-pc-1mb.bin`、sha256 与源**逐字节相等**、
`chat.db` 里 `dir=in … kind=file … [done]`、电脑端状态栏「收到「MI 9」发来的文件：…」。

### 1.3 手机端：发送失败却显示「已发给 N 台设备」
`MainActivity.sendPending` 循环里失败会写状态，但循环**结束后无条件覆盖**成"已发给 N 台设备"，
失败原因被吞掉；`uploadToDevice` 里 `openInputStream(uri)?.use { … }` 在读不到文件时
**什么都不做也不报错**。
**修**：统计成败，如实写"发送失败：原因"／"部分成功…没发出去的文件还在待发区"；
读不到就抛（带中文原因）。界面从此不替一次失败背书。

### 1.4 `device_dict()` 不给 `identityKey` → 手机端设备行 id 为空
`/api/state.devices[]` 只有 `id`，而 App 的 `DeviceRow.id = peer.identityKey` 拿到空串 →
会话成员（存的是裸设备号）**永远匹配不上**，标题退化成「与 e4233004… 的对话」。
**修**：`device_dict()` 增补 `identityKey`（= 设备号；只增字段）。

### 1.5 手机端会话标题「与 我（MI 9） 的对话」
`chatMembersLabel` 把"我"也塞进标题；对方认不出来时更是只剩"我"。
**修**：一对一标题只列**对方**；一个都对不上时如实写原始写法 + "这台设备现在不在列表里"。
同时去掉设备副标题里的**设备号前 8 位**（用户看不懂，还把「发现方式」挤到截断），
并允许副标题折两行。

### 1.6 电脑端两处"状态与事实不符"
- 状态栏「发现 0 台设备」而左栏写着「设备列表 1 · 在线 1」（那句话是启动时算死的）→ 设备刷新时重算。
- 手机是**组播自动发现**连上来的，副标题却写「扫码连接的手机」→ 改成「它连过来的」
  （`source=web` 只说明"它是连过来的 HTTP 客户端"，不等于扫码）。

## 2. 真机验收（`tests/test_real_device.py`，以手机侧硬证据为准）

| 项 | 结果 | 说明 |
|---|---|---|
| A0 现场 / A1 小文件 / A2 100 MB / A6 反作弊 / A8 双向中文+emoji | **PASS** | 磁盘 + `adb pull` 回来 sha256 都相等 |
| A7 手机→电脑（自动驱动） | **未判定** | 自动驱动在 MIUI 的 DocumentsUI 上还不稳；**手工路径已走通**（证据见 §1.2） |
| A9 1 GB | 未跑 | 需要 `EVERSEND_REAL_BIG=1` |

**A1/A2 踩到的测试口径坑（已修，别当产品缺陷）**：真机验收与桌面端在**同一个进程**里，
等待循环若只 `time.sleep` 不泵 Qt 事件，窗口定时器一次都不触发 —— "手机取走后记录变已发送"
这类状态永远等不到（A1.n/A2.n 因此假红过）。现在这类等待用 `wait_for_ui()`（泵事件循环）。

**A7 驱动踩到的坑（写给下一位）**：① 会话页提示语里也有"传输文件"四个字，按文本包含点击会
点到提示上（要取**底部**那个）；② MIUI 的选择器打开时停在**分类页**，先点「图片」
（用图片当测试文件最稳）；③ 手机里会堆着同一台电脑的历史实例（换一次 data_dir 就换一个
device_id），发送时挨个试并长时间超时 —— 先 `pm clear` 最确定。

## 3. 已知限制 / 还没做完的（如实记录）

### 3.1 电脑端**重启后**，手机第一次发送会 403（需要点一下设备重新连接）
现象：App 连着实例 A → A 关掉、起实例 B（同一个 IP:端口，但 **B 的防跨站令牌是新的**）
→ 手机发送 → 服务端 403 `缺少或错误的防跨站令牌`。

已经做的：
- `ApiClient.upload()` **不再把非 2xx 当成功**（以前 403 也返回"成功"，界面写"已发给 N 台
  设备" —— 这才是"手机说发了、电脑上没有"的真正原因）。
- App 现在**如实显示**："发给 xgl 失败：电脑端重启过，令牌已过期 —— 回设备列表点一下
  「xgl」重新连接，再发一次"（带可执行的下一步）。
- 加了 `ApiClient.refreshToken()`：捕获到令牌类错误时**换一张令牌重试一次**，
  并且所有请求 `useCaches = false`（Android 的 HttpResponseCache 会把带旧令牌的 HTML
  再喂回来，那样"刷新"等于没刷）。**但真机复现里这次重试仍以 403 结束**，
  原因待查（已在该路径留了 `Log.w("EverSend", …)`，`adb logcat -s EverSend:W` 能看到
  它到底换没换到令牌）。

**正常的用户路径不受影响**：App 连接一次之后，发送/接收都正常 —— 23:20 那次实测：
手机发 `A7-手机发来.png` → 电脑接收目录出现 `A7-手机发来 (2).png`，
**sha256 与源文件逐字节相等**，`chat.db` 里 `dir=in … kind=image … [done]`，
电脑端状态栏「收到「MI 9」发来的文件：…」。多次复现都是这样（20:45 / 22:17 / 23:06 / 23:20）。

### 3.2 同一台手机重装 App / 清数据之后，会话会分成两段
`deviceId` 变了，电脑端把它当新设备（两条记录、两段会话）。设备列表里已经是一台（服务端
按同地址同 UA 归并记录），但**会话**仍按成员写法分开 —— 收到的文件会落进"新"的那一段，
而用户可能正看着旧的那一段（设备行上有未读红点提示）。要么给 App 一个"设备号迁移"的路子，
要么在会话列表里明确提示"这可能是同一台手机的旧会话"。

### 3.3 A7（手机→电脑）的**自动验收**还没稳定
驱动要穿过 MIUI 的 DocumentsUI，踩过的坑都写在 §2 末尾。产品链路本身是通的（§3.1 的证据），
不稳的是"用 adb 把手机 UI 开到该开的那一页"。跑法：
`EVERSEND_ADB_SERIAL=2430d3c xvfb-run -a -s "-screen 0 1280x900x24" .venv/bin/python tests/test_real_device.py --only A7`。
