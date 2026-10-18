# 真机验收报告 · opt-a1-rerun

生成时间：2026-10-18 00:15:55　·　HEAD `e18c079`　·　变更文件 63 个　·　load `[2.01, 2.83, 7.89]`

- adb 设备：`2430d3c`　在线：**是**
- 电脑端端口：TCP 52117 / 网页 52119 / UDP 52118
- 测试数据：`/home/xgl/qa-real`（在 /home，不在 /tmp）

| # | 检查项 | 结论 | 失败的子断言 |
|---|---|---|---|
| A1 | 电脑发小文件（<1 MB）→ 手机磁盘 + sha256 | ✅ PASS | — |

## 明细

### A1 电脑发小文件（<1 MB）→ 手机磁盘 + sha256
- ✅ **A1.phone**
  - 说明：MI 9 id=web:97150a87-cc0b-4b7f-ba86-9ddf3be7d2d1 online=True
- ✅ **A1.file**
  - 说明：A1-小文件-524288.bin 0.5 MiB sha256=b72a28cdebac6320…
- ✅ **A1.a 发送前手机磁盘上没有这个文件**
  - 期望：`不在`
  - 实际：`False`
- ✅ **A1.b 文件进了待发列表**
  - 期望：`含 '/home/xgl/qa-real/files/A1-小文件-524288.bin'`
  - 实际：`['/home/xgl/qa-real/files/A1-小文件-524288.bin']`
- ✅ **A1.c 已点「发送」**
  - 说明：00:15:58
- ✅ **A1.d 文件走的是**文件通道**（服务端已发布该文件）**
  - 期望：`web UI 的分享表里出现这个文件名`
  - 实际：`shares=['A1-小文件-524288.bin']`
  - 说明：责任人 phone-transfer：点「发送」走了聊天通道时，这里会是空的
- ✅ **A1.e 活动传输**
  - 说明：0 个
- ✅ **A1.f0 点发送之后、手机取走之前的状态**
  - 说明：['sending']（conv=d:00254c4971c16f15c4bcd7982900f3cb|web:97150a87-cc0b-4b7f-ba86-9ddf3be7d2d1）
- ✅ **A1.f 手机取走之前，记录状态不是 sent**
  - 期望：`sending/queued/waiting…（手机取走后才允许 sent）`
  - 实际：`states=['sending']`
  - 说明：责任人 net-discovery：真实取走前写 sent 就是「界面说谎」的根
- ✅ **A1.g 手机页面上出现了这个卡片**
  - 实际：`A1-小文件-524288.bin`
- ✅ **A1.h 点了手机上的**
  - 说明：'保存' @ (194, 945)
- ✅ **A1.d2 服务端记录到手机真的取走了字节（sentBytes > 0）**
  - 期望：`sentBytes > 0（必要不充分：还要 sha256 相等）`
  - 实际：`share=jlZ407ez sentBytes=524288`
  - 说明：责任人 web-ui：手机在取但服务端没有字节计数时，这里红
- ✅ **A1.i 手机上真的出现了这个文件（adb ls）**
  - 期望：`/sdcard/Download/A1-小文件-524288.bin`
  - 实际：`size=524288；目录里现有 ['.pending-1792858566-A1-小文件-524288.bin', 'A1-小文件-524288.bin', 'A7-手机发来.png', 'resume-test-120m.bin']`
  - 说明：责任人 android-ui：App 收到却落盘失败时这里会红（曾见 0 字节 .pending-*）
- ✅ **A1.j 从手机拉回来的 sha256 与源文件相等**
  - 期望：`b72a28cdebac6320e1cf4e83d5c1878f81e63da0bb4d3a8ef207da7405c8635c`
  - 实际：`b72a28cdebac6320e1cf4e83d5c1878f81e63da0bb4d3a8ef207da7405c8635c（手机上 524288 字节，源 524288 字节）`
  - 说明：**ls 看到名字不算成功，只有 sha256 相等才算**
- ✅ **A1.k 手机上文件大小与源一致**
  - 期望：`524288`
  - 实际：`524288`
- ✅ **A1.n 手机真的取走之后，记录状态才变成 sent**
  - 期望：`取走后 state ∈ {sent}`
  - 实际：`states=['sent']`
- ✅ **A1.l 电脑端截图**
  - 说明：/home/xgl/qa-real/shots/A1-desktop-progress.png
- ✅ **A1.m 手机截图**
  - 说明：/home/xgl/qa-real/shots/A1-phone.png

