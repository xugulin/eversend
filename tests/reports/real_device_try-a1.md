# 真机验收报告 · try-a1

生成时间：2026-09-25 22:03:03　·　HEAD `c3245e6`　·　变更文件 165 个　·　load `[14.44, 8.01, 4.92]`

- adb 设备：`10AEBS3L74002AA`　在线：**是**
- 电脑端端口：TCP 52117 / 网页 52119 / UDP 52118
- 测试数据：`/home/xgl/qa-real`（在 /home，不在 /tmp）

| # | 检查项 | 结论 | 失败的子断言 |
|---|---|---|---|
| A1 | 电脑发小文件（<1 MB）→ 手机磁盘 + sha256 | ❌ FAIL | A1.d2 服务端记录到手机真的取走了字节（sentBytes > 0）、A1.i 手机上真的出现了这个文件（adb ls） |

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
  - 说明：22:03:11
- ✅ **A1.d 文件走的是**文件通道**（服务端已发布该文件）**
  - 期望：`web UI 的分享表里出现这个文件名`
  - 实际：`shares=['A1-小文件-524288.bin']`
  - 说明：责任人 phone-transfer：点「发送」走了聊天通道时，这里会是空的
- ✅ **A1.e 活动传输**
  - 说明：0 个
- ❌ **A1.d2 服务端记录到手机真的取走了字节（sentBytes > 0）**
  - 期望：`sentBytes > 0（必要不充分：还要 sha256 相等）`
  - 实际：`share=SCYdbHye sentBytes=0`
- ✅ **A1.f 手机取走之前，记录状态不是 sent**
  - 期望：`sending/queued/waiting…（手机取走后才允许 sent）`
  - 实际：`conv=d:8770480db83e7eb20f33182a09a10c1f|web:97b9bfe5-5c8a-4ef6-913e-e6b28ef1c4e2 states=['sending']`
  - 说明：责任人 net-discovery：真实取走前写 sent 就是「界面说谎」的根
- ✅ **A1.g 手机页面上出现了这个卡片**
  - 实际：`A1-小文件-524288.bin`
- ✅ **A1.h 点了手机上的**
  - 说明：'保存' @ (209, 1056)
- ❌ **A1.i 手机上真的出现了这个文件（adb ls）**
  - 期望：`/sdcard/Download/A1-小文件-524288.bin`
  - 实际：`size=None；目录里现有 ['(1)', '.801ab.cuid', '.exmu-cfg1.data', '.pending-1790865287-IMG_20260923_175527.jpg', '.pending-1790865290-IMG_20260923_175527.jpg', '.pending-1790865291-IMG_20260923_175527.jpg', '.pending-1790865301-IMG_20260923_175527.jpg', '.pending-1790865580-README.txt']`
  - 说明：责任人 android-ui：App 收到却落盘失败时这里会红（曾见 0 字节 .pending-*）

