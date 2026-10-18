# 企业级验收报告 · final

生成时间：2026-09-25 19:39:50　·　HEAD `c3245e6`　·　变更文件 156 个（仓库处于写入中）　·　load average `[4.36, 8.29, 11.46]` / 8 核

> **时序断言的环境说明**：C13/C14 里有若干带时间预算的断言（≤5s 自动发现、≤1s bye 转离线、补投 ≤20s）。当 load average 接近或超过核数时，这些断言可能**假红**，判断时请对照上面的负载数字。

> **本次复跑时仓库处于写入中**：下面这些文件在跑之前刚被改过，某条 FAIL 可能是某个实现者的中间状态。

```
README.md
android/app/src/androidTest/java/cn/eversend/app/DiscoveryAndActionsTest.kt
android/app/src/androidTest/java/cn/eversend/app/EverSendInstrumentedTest.kt
android/app/src/main/AndroidManifest.xml
android/app/src/main/java/cn/eversend/app/ApiClient.kt
android/app/src/main/java/cn/eversend/app/Discovery.kt
android/app/src/main/java/cn/eversend/app/LinkService.kt
android/app/src/main/java/cn/eversend/app/MainActivity.kt
docs/ENTERPRISE_CHARTER.md
docs/PROTOCOL.md
docs/UI_LAYOUT.md
docs/screenshots/01-send.png
docs/screenshots/01-session.png
docs/screenshots/02-transfers.png
docs/screenshots/03-receive.png
docs/screenshots/04-settings.png
docs/screenshots/05-qr.png
docs/screenshots/06-chat.png
docs/screenshots/07-preview.png
docs/screenshots/08-preview-image.png
docs/screenshots/09-emoji-and-sections.png
docs/screenshots/10-offline-devices.png
docs/screenshots/11-sections-collapsed.png
docs/screenshots/12-emoji-in-bubble.png
docs/screenshots/12-transfers-failed.png
docs/screenshots/13-one-row-per-file.png
docs/screenshots/13-settings-top.png
docs/screenshots/14-transfer-record-in-chat.png
docs/screenshots/15-settings-maintenance.png
docs/screenshots/16-one-phone-one-row.png
docs/screenshots/android-app-auto-discovery-drawer.png
docs/screenshots/android-app-auto-discovery.png
docs/screenshots/android-app-chat.png
docs/screenshots/android-app-clear-offline-empty.png
docs/screenshots/android-app-clear-offline.png
docs/screenshots/android-app-composer-tiles.png
docs/screenshots/android-app-conversation.png
docs/screenshots/android-app-device-sections-offline.png
docs/screenshots/android-app-device-sections.png
docs/screenshots/android-app-emoji-message.png
```

| # | 检查项 | 结论 | 失败的子断言 |
|---|---|---|---|
| C1 | 控件去重（一个意图一个入口，三端一致） | ✅ PASS | — |
| C2 | 排版：间距 4 的倍数、字号取类型刻度、圆角来自 token | ✅ PASS | — |
| C3 | 操作逻辑：发一个文件给一台设备 ≤ 3 次点击，无隐藏前置 | ✅ PASS | — |
| C4 | 设备列表：同一物理设备一行、名字不断词、副标题不重复事实 | ✅ PASS | — |
| C5 | 传输页：筛选、列头、排序、空状态、失败原因与重试 | ✅ PASS | — |
| C6 | 文件动作矩阵：四处入口同一套动作 + 属性齐全 | ✅ PASS | — |
| C7 | 记录卫生：按身份去重、保留策略、清空一次清干净、无孤儿 | ✅ PASS | — |
| C8 | 记录去重：同一 (方向, 对端, 内容) 只占一行并累加状态 | ✅ PASS | — |
| C9 | 设备信息：同一事实不在标题/副标题/详情里重复 | ✅ PASS | — |
| C10 | 全中文：Qt 系统对话框不再出现英文文案 | ✅ PASS | — |
| C11 | 预览：类型矩阵、工具栏、图片适应窗口不放大、文本等宽、二进制不装 | ✅ PASS | — |
| C12 | 网页版：同一套 IA 与 token、无三层嵌套、两次点击可达 | ✅ PASS | — |
| C13 | 自动发现：≤5s 零手动、扫描兜底、跨网段覆盖、bye 即时离线 | ❌ FAIL | C13.5b 桩命中 → 真实握手 → 并入 devices()、C13.5b 桩命中走完 [命中 → 握手 → 注册] 并出现在设备列表里 |
| C14 | 消息必达：ACK、待重发队列、重连补投、失败原因码 | ✅ PASS | — |
| E4 | 无回归：宪章 §8 的现有测试继续全绿 | ❌ FAIL | E4 compileall 退出码 0（0.5s）、E4 test_chat 输出里没有 FAIL 字样、E4 test_gui_render 输出里没有 FAIL 字样、E4 test_gui_actions 输出里没有 FAIL 字样 |
| E5 | 性能：窗口冷启动 ≤1.5s、1000 条传输记录滚动流畅 | ❌ FAIL | E5.1 窗口冷启动 ≤1.5s（构造 1.96s / 首次绘制 2.01s） |
| E6 | 交付质量：README 里每一条截图引用都能打开 | ✅ PASS | — |

## 失败明细

### C13 自动发现：≤5s 零手动、扫描兜底、跨网段覆盖、bye 即时离线
- **C13.5b 桩命中 → 真实握手 → 并入 devices()**
  - 期望：`通过`
  - 实际：`scan_subnets 抛异常 too many values to unpack (expected 2)`
- **C13.5b 桩命中走完 [命中 → 握手 → 注册] 并出现在设备列表里**
  - 期望：`device_id=03f2cd1767a9dac33fb7134b9b615bdd 出现在 a.devices()`
  - 实际：`registered=[] probe_peer 调用=[]`

### E4 无回归：宪章 §8 的现有测试继续全绿
- **E4 compileall 退出码 0（0.5s）**
  - 期望：`exit 0`
  - 实际：`exit=1；末尾输出：{% for module in qt_modules %}  # noqa: E999 / ^ / SyntaxError: invalid syntax`
  - 说明：宪章 E4 + §8 验收命令
- **E4 test_chat 输出里没有 FAIL 字样**
  - 期望：`无 FAIL`
  - 实际：` / ============================================================ / All chat checks passed.`
- **E4 test_gui_render 输出里没有 FAIL 字样**
  - 期望：`无 FAIL`
  - 实际：`wrote /home/xgl/python/跨平台文件互传/eversend/tests/screenshots/07-preview.png: True / wrote /home/xgl/python/跨平台文件互传/eversend/tests/screenshots/08-preview-image.png: True / rendered 41 screenshot(s) into /home/xgl/python/跨平台文件互传/eversend/tests/screenshots`
- **E4 test_gui_actions 输出里没有 FAIL 字样**
  - 期望：`无 FAIL`
  - 实际：` / ============================================================ / 桌面交互全部通过。`

### E5 性能：窗口冷启动 ≤1.5s、1000 条传输记录滚动流畅
- **E5.1 窗口冷启动 ≤1.5s（构造 1.96s / 首次绘制 2.01s）**
  - 期望：`≤1.5s`
  - 实际：`构造 1.96s，show+processEvents 后 2.01s`
  - 说明：宪章 E5；本机负载高时可能假红
