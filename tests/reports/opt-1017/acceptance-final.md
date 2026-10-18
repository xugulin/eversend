# 全量验收报告 · final

- 时间：2026-10-17 23:56:28　·　耗时：17.0 分钟　·　Python 3.14.7
- HEAD：`e18c079 snapshot: 2026-10-04 企业级改造完成状态 —— 稳定性/速度/GUI 优化基线`　·　工作树变更：38 项
- 负载：(3.828125, 15.72998046875, 19.79833984375)（16 核）

## 结果

| 步骤 | 内容 | 结果 | 耗时 | 日志 |
|---|---|---|---|---|
| compile | 编译检查（src/tests/tools 顶层） | ✅ PASS | 0.2s | `compile.log` |
| loopback | 内核 e2e（loopback，含 2GiB 吞吐） | ✅ PASS | 67.5s | `loopback.log` |
| resilience | 恶劣网络/中断恢复 | ✅ PASS | 36.8s | `resilience.log` |
| concurrency | 并发传输 | ✅ PASS | 10.0s | `concurrency.log` |
| chat | 聊天/记录模型 | ✅ PASS | 5.7s | `chat.log` |
| http-resume | 可续传上传（opt-1017 新增） | ✅ PASS | 1.3s | `http-resume.log` |
| web | 网页版 356 项自检 | ✅ PASS | 51.7s | `web.log` |
| gui-render | Qt 离屏渲染 | ✅ PASS | 9.7s | `gui-render.log` |
| gui-actions | Qt 真点击交互 | ❌ FAIL(exit=1) | 251.7s | `gui-actions.log` |
| enterprise | 企业级验收 C1-C14 | ❌ FAIL(exit=1) | 359.0s | `enterprise.log` |
| realdevice | MI9 真机 A0-A9（APK↔Linux） | ❌ FAIL(exit=1) | 223.5s | `realdevice.log` |

**8 / 11 步通过**

## 冻结树证据

```
HEAD: e18c079 snapshot: 2026-10-04 企业级改造完成状态 —— 稳定性/速度/GUI 优化基线
变更文件数: 38
  M android/app/src/main/AndroidManifest.xml
   M android/app/src/main/java/cn/eversend/app/ApiClient.kt
   M android/app/src/main/java/cn/eversend/app/Discovery.kt
   M android/app/src/main/java/cn/eversend/app/Fetch.kt
   M android/app/src/main/java/cn/eversend/app/LinkService.kt
   M android/app/src/main/java/cn/eversend/app/MainActivity.kt
   M docs/PROTOCOL.md
   M src/eversend/core/chunkstore.py
   M src/eversend/core/connection.py
   M src/eversend/core/crypto.py
   M src/eversend/core/model.py
   M src/eversend/core/sockutil.py
   M src/eversend/core/transfer.py
   M src/eversend/desktop/main_window.py
   M src/eversend/desktop/transfers_view.py
   M src/eversend/web/assets/app.js
   M src/eversend/web/server.py
   M tests/screenshots/conversation-list.png
   M tests/test_gui_actions.py
   M tests/test_real_device.py
  ?? tests/reports/opt-1017/
  ?? tests/reports/real_device_1017-2223.json
  ?? tests/reports/real_device_1017-2223.md
  ?? tests/reports/real_device_mi9-clean1.json
  ?? tests/reports/real_device_mi9-clean1.md
  ?? tests/reports/real_device_mi9-clean2.json
  ?? tests/reports/real_device_mi9-clean2.md
  ?? tests/reports/real_device_mi9-v2-a7rerun.json
  ?? tests/reports/real_device_mi9-v2-a7rerun.md
  ?? tests/reports/real_device_mi9-v2.json
  ?? tests/reports/real_device_mi9-v2.md
  ?? tests/reports/real_device_probe-before-oldapk.json
  ?? tests/reports/real_device_probe-before-oldapk.md
  ?? tests/test_http_resume.py
  ?? tools/acceptance_opt1017.py
  ?? tools/bench_transfer.py
  ?? tools/realdevice_probe.py
  ?? tools/realdevice_upload_resume_probe.py
```

## 失败步骤明细

### gui-actions（exit=1）

```
[26] 取消是事实：空原因的收尾也要说「已取消」
  [32mPASS[0m 受理后卡片先写「正在取消…」
  [32mPASS[0m 空原因的收尾也显示「已取消」（不是「失败」）
  [32mPASS[0m 操作格给的是「重试」而不是「移除」
  [32mPASS[0m 这一行不再是进行中
  [32mPASS[0m 占位名收尾时同样认得出「已取消」
  [32mPASS[0m 没人点过取消的静默失败仍然是「失败」
  [32mPASS[0m 而且失败要留下原因（不许静默）
  [32mPASS[0m 引擎说 cancelled 时同样是「已取消」
============================================================
2 failure(s):
  - 第一台收到了
  - 第二台也收到了
/home/xgl/我的项目/自制软件/韧传/eversend/tests/test_gui_actions.py:1091: DeprecationWarning: Function: 'QMouseEvent.QMouseEvent(QEvent.Type type, const QPointF &localPos, Qt.MouseButton button, QFlags<Qt.MouseButton> buttons, QFlags<Qt.KeyboardModifier> modifiers, const QPointingDevice *device)' is marked as deprecated, please check the documentation for more information.
  QMouseEvent(etype, pos, button, buttons, Qt.KeyboardModifier.NoModifier),
```

### enterprise（exit=1）

```
  C6    PASS  文件动作矩阵：四处入口同一套动作 + 属性齐全
  C7    PASS  记录卫生：按身份去重、保留策略、清空一次清干净、无孤儿
  C8    PASS  记录去重：同一 (方向, 对端, 内容) 只占一行并累加状态
  C9    PASS  设备信息：同一事实不在标题/副标题/详情里重复
  C10   PASS  全中文：Qt 系统对话框不再出现英文文案
  C11   PASS  预览：类型矩阵、工具栏、图片适应窗口不放大、文本等宽、二进制不装
  C12   PASS  网页版：同一套 IA 与 token、无三层嵌套、两次点击可达
  C13   PASS  自动发现：≤5s 零手动、扫描兜底、跨网段覆盖、bye 即时离线
  C14   PASS  消息必达：ACK、待重发队列、重连补投、失败原因码
  E4    PASS  无回归：宪章 §8 的现有测试继续全绿
  E5    PASS  性能：窗口冷启动 ≤1.5s、1000 条传输记录滚动流畅
  E6    PASS  交付质量：README 里每一条截图引用都能打开
==============================================================
  16/17 项通过（其中 1 条子断言因机器负载高而未判定：['E4 test_loopback 的吞吐地板（默认 60 MiB/s）']）
报告已写入 /home/xgl/我的项目/自制软件/韧传/eversend/tests/reports/opt-final.md 与 .json
```

### realdevice（exit=1）

```
=== A7 手机发文件给电脑 → 电脑接收目录 + sha256 ===
--- A7: PASS ---
=== A8 双向中文 + emoji 消息，两端都能看到 ===
--- A8: PASS ---
==============================================================
  A0   PASS      现场：真机在线、三个端口空闲、测试目录就绪
  A1   FAIL      电脑发小文件（<1 MB）→ 手机磁盘 + sha256
  A2   PASS      电脑发中等文件（约 100 MB）→ 手机磁盘 + sha256
  A9   UNDECIDED 电脑发大文件（约 1 GB，可选）→ 手机磁盘 + sha256
  A6   PASS      反作弊：界面说「已收到」时手机磁盘必须真的有（否则判红）
  A7   PASS      手机发文件给电脑 → 电脑接收目录 + sha256
  A8   PASS      双向中文 + emoji 消息，两端都能看到
==============================================================
  5/7 项通过；1 项失败；1 项未判定（前提缺失，不记绿）
报告已写入 /home/xgl/我的项目/自制软件/韧传/eversend/tests/reports/real_device_opt-final.md 与 .json
```

