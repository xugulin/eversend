# 真机验收报告 · baseline2

生成时间：2026-09-25 22:11:23　·　HEAD `c3245e6`　·　变更文件 169 个　·　load `[3.05, 9.03, 7.87]`

- adb 设备：`10AEBS3L74002AA`　在线：**是**
- 电脑端端口：TCP 52117 / 网页 52119 / UDP 52118
- 测试数据：`/home/xgl/qa-real`（在 /home，不在 /tmp）

| # | 检查项 | 结论 | 失败的子断言 |
|---|---|---|---|
| A1 | 电脑发小文件（<1 MB）→ 手机磁盘 + sha256 | ❌ FAIL | A1.d2 服务端记录到手机真的取走了字节（sentBytes > 0）、A1.i 手机上真的出现了这个文件（adb ls） |
| A6 | 反作弊：界面说「已收到」时手机磁盘必须真的有（否则判红） | ❌ FAIL | A6.3 手机目录里没有 0 字节的 .pending-* 残留、A6.4 手机目录里没有任何 0 字节的已下载文件 |

## 明细

### A1 电脑发小文件（<1 MB）→ 手机磁盘 + sha256
- ✅ **A1.phone**
  - 说明：V2408A id=web:97b9bfe5-5c8a-4ef6-913e-e6b28ef1c4e2 online=True
- ✅ **A1.file**
  - 说明：A1-小文件-524288.bin 0.5 MiB sha256=b72a28cdebac6320…
- ✅ **A1.a 发送前手机磁盘上没有这个文件**
  - 期望：`不在`
  - 实际：`False`
- ✅ **A1.b 文件进了待发列表**
  - 期望：`含 '/home/xgl/qa-real/files/A1-小文件-524288.bin'`
  - 实际：`['/home/xgl/qa-real/files/A1-小文件-524288.bin']`
- ✅ **A1.c 已点「发送」**
  - 说明：22:11:30
- ✅ **A1.d 文件走的是**文件通道**（服务端已发布该文件）**
  - 期望：`web UI 的分享表里出现这个文件名`
  - 实际：`shares=['A1-小文件-524288.bin']`
  - 说明：责任人 phone-transfer：点「发送」走了聊天通道时，这里会是空的
- ✅ **A1.e 活动传输**
  - 说明：0 个
- ✅ **A1.f0 点发送之后、手机取走之前的状态**
  - 说明：['sending']（conv=d:8770480db83e7eb20f33182a09a10c1f|web:97b9bfe5-5c8a-4ef6-913e-e6b28ef1c4e2）
- ✅ **A1.f 手机取走之前，记录状态不是 sent**
  - 期望：`sending/queued/waiting…（手机取走后才允许 sent）`
  - 实际：`states=['sending']`
  - 说明：责任人 net-discovery：真实取走前写 sent 就是「界面说谎」的根
- ✅ **A1.g 手机页面上出现了这个卡片**
  - 实际：`A1-小文件-524288.bin`
- ✅ **A1.h 点了手机上的**
  - 说明：'保存' @ (209, 1056)
- ❌ **A1.d2 服务端记录到手机真的取走了字节（sentBytes > 0）**
  - 期望：`sentBytes > 0（必要不充分：还要 sha256 相等）`
  - 实际：`share=QKpP__am sentBytes=0`
  - 说明：责任人 web-ui：手机在取但服务端没有字节计数时，这里红
- ❌ **A1.i 手机上真的出现了这个文件（adb ls）**
  - 期望：`/sdcard/Download/A1-小文件-524288.bin`
  - 实际：`size=None；目录里现有 ['(1)', '.801ab.cuid', '.exmu-cfg1.data', '.pending-1790865287-IMG_20260923_175527.jpg', '.pending-1790865290-IMG_20260923_175527.jpg', '.pending-1790865291-IMG_20260923_175527.jpg', '.pending-1790865301-IMG_20260923_175527.jpg', '.pending-1790865580-README.txt']`
  - 说明：责任人 android-ui：App 收到却落盘失败时这里会红（曾见 0 字节 .pending-*）

### A6 反作弊：界面说「已收到」时手机磁盘必须真的有（否则判红）
- ✅ **A6.claims**
  - 说明：界面上的完成标记 0 个，涉及文件 （未识别到文件名）
- ✅ **A6.1 界面说已收到的文件，磁盘上都有**
  - 期望：`每个都出现在 /sdcard/Download`
  - 实际：`缺失=[]；磁盘现有 ['(1)', '.801ab.cuid', '.exmu-cfg1.data', '.pending-1790865287-IMG_20260923_175527.jpg', '.pending-1790865290-IMG_20260923_175527.jpg', '.pending-1790865291-IMG_20260923_175527.jpg', '.pending-1790865301-IMG_20260923_175527.jpg', '.pending-1790865580-README.txt', '.pending-1790865581-README.txt', '.pending-1790865582-README.txt']`
  - 说明：责任人 android-ui：App 侧「已收到」写早了 —— 它只表示消息投递成功，不表示文件落盘。E3「无静默失败」同类问题
- ✅ **A6.2 界面说已收到的文件，磁盘上都不是 0 字节**
  - 期望：`大小 > 0`
  - 实际：`0 字节=[]`
  - 说明：实测见过 4 个 .pending-179086558x-README.txt 是 0 字节残留
- ✅ **A6.pending**
  - 说明：手机目录里的 .pending-* 临时文件：12 个（其中 0 字节 12 个）
- ❌ **A6.3 手机目录里没有 0 字节的 .pending-* 残留**
  - 期望：`[]`
  - 实际：`['.pending-1790865287-IMG_20260923_175527.jpg', '.pending-1790865290-IMG_20260923_175527.jpg', '.pending-1790865291-IMG_20260923_175527.jpg', '.pending-1790865301-IMG_20260923_175527.jpg', '.pending-1790865580-README.txt', '.pending-1790865581-README.txt', '.pending-1790865582-README.txt', '.pending-1790865583-README.txt', '.pending-1790865584-README.txt', '.pending-1790865585-README.txt', '.pending-1790949886-A1-小文件-524288.bin', '.pending-1790950294-A1-小文件-524288.bin']`
  - 说明：责任人 android-ui：`MainActivity.kt saveToDownloads` 在 sink 抛异常时没有把 IS_PENDING 清 0，于是留下永远 pending 的 0 字节行 —— 这条断言直接盯住「下载失败了但没人收拾」（Lead 挖到的根因）
- ❌ **A6.4 手机目录里没有任何 0 字节的已下载文件**
  - 期望：`[]`
  - 实际：`['(1)']`

