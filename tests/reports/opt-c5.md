# 企业级验收报告 · opt-c5

生成时间：2026-10-18 00:16:13　·　HEAD `e18c079`　·　变更文件 65 个（仓库处于写入中）　·　load average `[1.78, 2.74, 7.78]` / 16 核

> **冻结树规则**：`final` 必须对着**冻结的树**跑 —— 开跑前确认没有文件在mtime 上晚于开跑时刻（报告头会记 HEAD + 变更清单 + 各文件 mtime）。对着「别人还在写」的树跑出来的结论只能叫候选，不能当交付证据。

> **时序断言的环境说明**：C13/C14 里有若干带时间预算的断言（≤5s 自动发现、≤1s bye 转离线、补投 ≤20s）。当 load average 接近或超过核数时，这些断言可能**假红**，判断时请对照上面的负载数字。

> **本次复跑时仓库处于写入中**：下面这些文件在跑之前刚被改过，某条 FAIL 可能是某个实现者的中间状态。

```
android/app/src/main/AndroidManifest.xml
android/app/src/main/java/cn/eversend/app/ApiClient.kt
android/app/src/main/java/cn/eversend/app/Discovery.kt
android/app/src/main/java/cn/eversend/app/Fetch.kt
android/app/src/main/java/cn/eversend/app/LinkService.kt
android/app/src/main/java/cn/eversend/app/MainActivity.kt
docs/ENTERPRISE_CHARTER.md
docs/PROTOCOL.md
docs/UI_LAYOUT.md
src/eversend/core/chunkstore.py
src/eversend/core/connection.py
src/eversend/core/crypto.py
src/eversend/core/model.py
src/eversend/core/sockutil.py
src/eversend/core/transfer.py
src/eversend/desktop/main_window.py
src/eversend/desktop/transfers_view.py
src/eversend/web/assets/app.js
src/eversend/web/server.py
tests/reports/opt-1017/
tests/reports/opt-final.json
tests/reports/opt-final.md
tests/reports/real_device_1017-2223.json
tests/reports/real_device_1017-2223.md
tests/reports/real_device_mi9-clean1.json
tests/reports/real_device_mi9-clean1.md
tests/reports/real_device_mi9-clean2.json
tests/reports/real_device_mi9-clean2.md
tests/reports/real_device_mi9-v2-a7rerun.json
tests/reports/real_device_mi9-v2-a7rerun.md
tests/reports/real_device_mi9-v2.json
tests/reports/real_device_mi9-v2.md
tests/reports/real_device_opt-a1-rerun.json
tests/reports/real_device_opt-a1-rerun.md
tests/reports/real_device_opt-final.json
tests/reports/real_device_opt-final.md
tests/reports/real_device_probe-before-oldapk.json
tests/reports/real_device_probe-before-oldapk.md
tests/screenshots/01-session.png
tests/screenshots/02-transfers.png
```

| # | 检查项 | 结论 | 失败的子断言 |
|---|---|---|---|
| C5 | 传输页：筛选、列头、排序、空状态、失败原因与重试 | ✅ PASS | — |

## 失败明细
