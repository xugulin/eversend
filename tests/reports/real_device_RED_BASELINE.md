# 真机红基线 · PC ↔ vivo V2408A（task-13）

> **谁复现的**：qa-verify（独立验证者）
> **环境**：adb `10AEBS3L74002AA`（vivo V2408A / Android 16 / arm64）；电脑端 `172.20.90.177`
> TCP 52117 / 网页 52119 / UDP 52118；`uptime` = `22:16:22 up 11:34, load average: 2.54, 5.01, 6.43`
> **跑法**：`xvfb-run -a -s "-screen 0 1280x900x24" .venv/bin/python tests/test_real_device.py --only A1,A6 --label baseline2`
> 原始数据：`tests/reports/real_device_baseline2.md|.json`、`real_device_a6-check.md|.json`

## 结论

| 项 | 状态 | 说明 |
|---|---|---|
| A0 现场 | ✅ PASS | 真机在线、三个端口空闲、数据目录在 `/home`、产品可 import |
| A1 电脑发小文件（<1 MB）→ 手机磁盘 + sha256 | ❌ **FAIL** | 电脑发布了文件，**手机一个字节都没取到** |
| A6 反作弊（界面说已收到 → 磁盘必须真的有） | ❌ **FAIL** | **14 个 0 字节 `.pending-*`**，其中 **2 个是本次测试的文件** |

**一句话**：点「发送」之后文件**确实进了文件通道**（分享表里有），会话状态也**诚实**（`sending`，不是 `sent`）——
但手机点「保存」时打的是 **`/api/chat/media/<id>`**，而那条消息的 `media_rel` 是**空串** → 服务端 **404** →
App 留下一个 0 字节 `.pending-*` 就没了下文，`sentBytes` 始终 0，`/sdcard/Download` 里什么都没有。

---

## 缺陷 1（P0 · 主链路）：手机保存文件 → 404，文件永远到不了手机

### 最小复现（无需手机，纯服务端）
```bash
cd /home/xgl/python/跨平台文件互传/eversend
.venv/bin/python /home/xgl/qa-phone/repro_media_404.py
```
原始输出：
```
消息行（最近 3 条文件）：
  id=1d5b844ff78347f59e8b name='A1-小文件-524288.bin' media_rel='' media_source='/home/xgl/qa-real/files/A1-小文件-524288.bin' state=sending

GET http://127.0.0.1:46039/api/chat/media/1d5b844ff78347f59e8b
  HTTP 404 body: {"ok": false, "error": "没有这条附件"}

对照：接收目录里有没有这个文件？
  /home/xgl/qa-real/received/<media_rel 为空> → 不存在
  源文件 /home/xgl/qa-real/files/A1-小文件-524288.bin → 存在
```

### 真机侧同一时刻的 App 日志（`adb logcat -d | grep EverSend`）
```
09-25 22:04:46.873 D/EverSend( 8300): 保存失败: http://172.20.90.177:52119/api/chat/media/1d5b844ff78347f59e8b
09-25 22:11:34.600 D/EverSend( 8300): 保存失败: http://172.20.90.177:52119/api/chat/media/1d5b844ff78347f59e8b
```
两次正是 `A1` 的两次运行。

### 完整因果链（每一环都有证据）

| # | 环节 | 证据 | 结论 |
|---|---|---|---|
| 1 | 桌面点「发送」→ 走**文件通道** | `A1.d` ✅ 分享表里出现 `A1-小文件-524288.bin` | phone-transfer 的路由改造**生效** |
| 2 | 会话里立刻有一条记录，状态诚实 | `A1.f` ✅ `messages.state='sending'`（不是 `sent`） | net-discovery 的状态机**生效** |
| 3 | 服务端把文件挂到 `/api/share/<id>` | `share=QKpP__am`（`sentBytes=0`） | 发布成功 |
| 4 | **App 却去取 `/api/chat/media/<id>`** | logcat `保存失败: …/api/chat/media/1d5b844ff78347f59e8b` | **App 用错了下载入口** |
| 5 | 该消息行 `media_rel` 是**空串** | DB 原文 `media_rel=''`（`media_source` 有值） | **写这条记录的人没填 `media_rel`** |
| 6 | 服务端对空 `media_rel` 回 404 | `_send_chat_media`：`if message is None or not message.get("mediaRel"): 404 "没有这条附件"` | 404 是**正确**的（它确实没有可服务的路径） |
| 7 | App 失败后留下 0 字节 `.pending-*` 且不清理 | `A6.3`：`.pending-1790949886-A1-小文件-524288.bin`、`.pending-1790950294-A1-小文件-524288.bin` | **android-ui** 的 `saveToDownloads` 回滚缺口 |

### 责任归属与修法（供裁决）

| 责任人 | 该做什么 |
|---|---|
| **phone-transfer** | 会话里那条"交给手机的文件"记录必须带上可下载的引用。现在 `main_window._announce_sent_files` → `_announce_transfer_message(...)` 调用时**没有传 `rel=`**（该函数签名里 `rel: str = ""` 是默认值），于是落库 `media_rel=''`。要么填 `media_rel`（把文件放进会话媒体目录），要么让手机端明确走 `/api/share/<id>`。 |
| **android-ui** | App 的「保存」应当取**电脑发布的分享条目**（`/api/share/<id>`，这正是 `docs/REAL_DEVICE_FINDINGS.md §2.3` 写的形态），而不是 `/api/chat/media/<id>`；并且 404/失败必须**回滚**那条 pending 记录（否则永远留 0 字节垃圾），界面要写中文失败原因。 |
| **web-ui** | 一个"该消息有 `media_source` 但没有 `media_rel`"的请求，回 404 是对的，但**同时它在分享表里是有文件的** —— 建议服务端把两者打通（例如 `/api/chat/media/<id>` 在这条消息已被 `share_files` 发布过时重定向到分享条目），或在 404 里给出可执行信息，不要只回"没有这条附件"。 |
| **net-discovery** | 状态语义没问题（`sending` 正确）。但请确认"发布成功"与"记录可被下载"是同一件事的两面 —— 现在 record 写了、share 也发了，唯独 record 上没有取文件的引用。 |

---

## 缺陷 2（P0 · 反作弊）：14 个 0 字节 `.pending-*`，其中 2 个是本次测试留下的

```
$ adb shell ls -la /sdcard/Download/ | grep ' 0 ' | awk '{print $NF}'
.pending-1790865287-IMG_20260923_175527.jpg
.pending-1790865290-IMG_20260923_175527.jpg
.pending-1790865291-IMG_20260923_175527.jpg
.pending-1790865301-IMG_20260923_175527.jpg
.pending-1790865580-README.txt
.pending-1790865581-README.txt
.pending-1790865582-README.txt
.pending-1790865583-README.txt
.pending-1790865584-README.txt
.pending-1790865585-README.txt
.pending-1790949886-A1-小文件-524288.bin     ← 本次 A1 第一次运行
.pending-1790950294-A1-小文件-524288.bin     ← 本次 A1 第二次运行
```
断言：`A6.3 手机目录里没有 0 字节的 .pending-* 残留` ❌
根因（Lead 已挖到，我复现了后果）：`MainActivity.kt saveToDownloads` 在 `sink` 抛异常时**不清 `IS_PENDING`**、
也不删除半成品，于是"下载失败没人收拾"。

**注意**：`.pending-*` 全为 0 字节，说明**没有一次下载真正写入过数据**。

## 缺陷 3（已修，保留为回归证据）：界面说「已收到」但磁盘没有
本次复跑 `A6.1`/`A6.2` **已转绿**（android-ui 的文案改造生效：消息投递与文件落盘不再共用一句话）。
历史证据保留：三条卡片曾同时显示「已收到」而 `/sdcard/Download` 里没有它们。

---

## 没验到的（不记绿）

| 项 | 原因 |
|---|---|
| A2（约 100 MB）、A9（约 1 GB） | 被缺陷 1 挡住：手机取不到任何文件，跑大文件只会重复同一个 404。**先修 A1 再跑。** |
| A7（手机发文件给电脑） | 需要驱动 App 的选文件 UI（系统文件选择器），尚未实现驱动。 |
| A8（双向中文 + emoji 消息） | 已写好断言，本次未跑；下轮补。 |
| 真机上的主动发现（C13 安卓侧） | 属上一轮 task-7 的保留项，本轮不重复。 |

## 复跑方式
```bash
export PATH=/home/xgl/android-dev/sdk/platform-tools:$PATH
cd /home/xgl/python/跨平台文件互传/eversend
xvfb-run -a -s "-screen 0 1280x900x24" .venv/bin/python tests/test_real_device.py \
    --only A1,A6 --label recheck-after-fix
```
**占用纪律**：起电脑端前先 `pkill -f "[p]ython -m eversend"`（注意用 `[p]` 免得把自己那条命令也杀掉）；
数据目录 `/home/xgl/qa-real/pc`；跑完释放端口。
