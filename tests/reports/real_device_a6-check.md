# 真机验收报告 · a6-check

生成时间：2026-09-25 22:16:13　·　HEAD `c3245e6`　·　变更文件 169 个　·　load `[2.73, 5.13, 6.49]`

- adb 设备：`10AEBS3L74002AA`　在线：**是**
- 电脑端端口：TCP 52117 / 网页 52119 / UDP 52118
- 测试数据：`/home/xgl/qa-real`（在 /home，不在 /tmp）

| # | 检查项 | 结论 | 失败的子断言 |
|---|---|---|---|
| A6 | 反作弊：界面说「已收到」时手机磁盘必须真的有（否则判红） | ❌ FAIL | A6.3 手机目录里没有 0 字节的 .pending-* 残留 |

## 明细

### A6 反作弊：界面说「已收到」时手机磁盘必须真的有（否则判红）
- ✅ **A6.claims**
  - 说明：界面上的完成标记 0 个，涉及文件 （未识别到文件名）
- ✅ **A6.1 界面说已收到的文件，磁盘上都有**
  - 期望：`每个都出现在 /sdcard/Download`
  - 实际：`缺失=[]；磁盘现有 ['.801ab.cuid', '.exmu-cfg1.data', '.pending-1790865287-IMG_20260923_175527.jpg', '.pending-1790865290-IMG_20260923_175527.jpg', '.pending-1790865291-IMG_20260923_175527.jpg', '.pending-1790865301-IMG_20260923_175527.jpg', '.pending-1790865580-README.txt', '.pending-1790865581-README.txt', '.pending-1790865582-README.txt', '.pending-1790865583-README.txt']`
  - 说明：责任人 android-ui：App 侧「已收到」写早了 —— 它只表示消息投递成功，不表示文件落盘。E3「无静默失败」同类问题
- ✅ **A6.2 界面说已收到的文件，磁盘上都不是 0 字节**
  - 期望：`大小 > 0`
  - 实际：`0 字节=[]`
  - 说明：实测见过 4 个 .pending-179086558x-README.txt 是 0 字节残留
- ✅ **A6.pending**
  - 说明：手机目录里的 .pending-* 临时文件：14 个（其中 0 字节 14 个）
- ❌ **A6.3 手机目录里没有 0 字节的 .pending-* 残留**
  - 期望：`[]`
  - 实际：`['.pending-1790865287-IMG_20260923_175527.jpg', '.pending-1790865290-IMG_20260923_175527.jpg', '.pending-1790865291-IMG_20260923_175527.jpg', '.pending-1790865301-IMG_20260923_175527.jpg', '.pending-1790865580-README.txt', '.pending-1790865581-README.txt', '.pending-1790865582-README.txt', '.pending-1790865583-README.txt', '.pending-1790865583-README.txt (1)', '.pending-1790865584-README.txt', '.pending-1790865584-README.txt (1)', '.pending-1790865585-README.txt', '.pending-1790949886-A1-小文件-524288.bin', '.pending-1790950294-A1-小文件-524288.bin']`
  - 说明：责任人 android-ui：`MainActivity.kt saveToDownloads` 在 sink 抛异常时没有把 IS_PENDING 清 0，于是留下永远 pending 的 0 字节行 —— 这条断言直接盯住「下载失败了但没人收拾」（Lead 挖到的根因）
- ✅ **A6.4 手机目录里没有任何 0 字节的已下载文件**
  - 期望：`[]`
  - 实际：`[]`
  - 说明：这条盯「下载了但一个字节都没写进去」的文件；点开头的临时文件由 A6.3 管

