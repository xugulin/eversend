# 企业级验收报告 · e45-fix

生成时间：2026-10-04 04:04:58　·　HEAD `c3245e6`　·　变更文件 172 个（仓库处于写入中）　·　load average `[10.11, 14.95, 11.91]` / 8 核

> **冻结树规则**：`final` 必须对着**冻结的树**跑 —— 开跑前确认没有文件在mtime 上晚于开跑时刻（报告头会记 HEAD + 变更清单 + 各文件 mtime）。对着「别人还在写」的树跑出来的结论只能叫候选，不能当交付证据。

> **时序断言的环境说明**：C13/C14 里有若干带时间预算的断言（≤5s 自动发现、≤1s bye 转离线、补投 ≤20s）。当 load average 接近或超过核数时，这些断言可能**假红**，判断时请对照上面的负载数字。

> **本次复跑时仓库处于写入中**：下面这些文件在跑之前刚被改过，某条 FAIL 可能是某个实现者的中间状态。

```
README.md
android/app/src/androidTest/java/cn/eversend/app/DiscoveryAndActionsTest.kt
android/app/src/androidTest/java/cn/eversend/app/EverSendInstrumentedTest.kt
android/app/src/androidTest/java/cn/eversend/app/FetchAndSaveTest.kt
android/app/src/main/AndroidManifest.xml
android/app/src/main/java/cn/eversend/app/ApiClient.kt
android/app/src/main/java/cn/eversend/app/Discovery.kt
android/app/src/main/java/cn/eversend/app/EverSendApp.kt
android/app/src/main/java/cn/eversend/app/Fetch.kt
android/app/src/main/java/cn/eversend/app/LinkService.kt
android/app/src/main/java/cn/eversend/app/MainActivity.kt
docs/ENTERPRISE_CHARTER.md
docs/PROTOCOL.md
docs/REAL_DEVICE_FINDINGS.md
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
```

| # | 检查项 | 结论 | 失败的子断言 |
|---|---|---|---|
| E4 | 无回归：宪章 §8 的现有测试继续全绿 | ❌ FAIL | E4 test_loopback 退出码 0（40.1s）、E4 test_loopback 自报失败数为 0、E4 test_gui_actions 退出码 0（449.3s）、E4 test_gui_actions 自报失败数为 0 |
| E5 | 性能：窗口冷启动 ≤1.5s、1000 条传输记录滚动流畅 | ❌ FAIL | E5.1 窗口冷启动 ≤1.5s（新进程实测 1.75s：引擎 1008ms + 窗口 650ms + 首绘 97ms） |

## 失败明细

### E4 无回归：宪章 §8 的现有测试继续全绿
- **E4 test_loopback 退出码 0（40.1s）**
  - 期望：`exit 0`
  - 实际：`exit=1；末尾输出：- throughput above the floor (25 MiB/s) / 警告：/home/xgl/我的项目/自制软件/韧传/eversend/.testscratch/run-wnxpl_7b/identity.json 公钥与私钥不匹配（这份身份是在缺少加密库时生成的），已重新生成身份。 / 本机设备号会变化，其他设备需要重新配对一次。`
  - 说明：宪章 E4 + §8 验收命令
- **E4 test_loopback 自报失败数为 0**
  - 期望：`0 failure(s)`
  - 实际：`1 failure(s)：FAIL throughput above the floor (25 MiB/s) 两次最好成绩 17 MiB/s／- throughput above the floor (25 MiB/s)`
- **E4 test_gui_actions 退出码 0（449.3s）**
  - 期望：`exit 0`
  - 实际：`exit=1；末尾输出：File "/home/xgl/我的项目/自制软件/韧传/eversend/tests/test_gui_actions.py", line 1072, in test_composer_tiles_and_preview / from PySide6.QtTest import QTest / ModuleNotFoundError: No module named 'PySide6.QtTest'`
  - 说明：宪章 E4 + §8 验收命令
- **E4 test_gui_actions 自报失败数为 0**
  - 期望：`0 failure(s)`
  - 实际：`1 failure(s)：- test_composer_tiles_and_preview raised ModuleNotFoundError("No module named 'PySide6.QtTest'")`

### E5 性能：窗口冷启动 ≤1.5s、1000 条传输记录滚动流畅
- **E5.1 窗口冷启动 ≤1.5s（新进程实测 1.75s：引擎 1008ms + 窗口 650ms + 首绘 97ms）**
  - 期望：`≤1500ms（宪章 E5，本机）`
  - 实际：`1754ms；两次样本 [2011, 1754]，load=11.8/8 核`
  - 说明：在新进程里量才是「冷启动」；进程内量会因为已积累的状态而虚高
