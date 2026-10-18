# 企业级验收报告 · c11-recheck

生成时间：2026-10-18 03:14:18　·　HEAD `8bd847c`　·　变更文件 37 个（仓库处于写入中）　·　load average `[2.65, 2.29, 1.77]` / 16 核

> **冻结树规则**：`final` 必须对着**冻结的树**跑 —— 开跑前确认没有文件在mtime 上晚于开跑时刻（报告头会记 HEAD + 变更清单 + 各文件 mtime）。对着「别人还在写」的树跑出来的结论只能叫候选，不能当交付证据。

> **时序断言的环境说明**：C13/C14 里有若干带时间预算的断言（≤5s 自动发现、≤1s bye 转离线、补投 ≤20s）。当 load average 接近或超过核数时，这些断言可能**假红**，判断时请对照上面的负载数字。

> **本次复跑时仓库处于写入中**：下面这些文件在跑之前刚被改过，某条 FAIL 可能是某个实现者的中间状态。

```
tests/reports/opt-1017/acceptance-final3.json
tests/reports/opt-1017/acceptance-final3.md
tests/reports/opt-1017/acceptance-final4.json
tests/reports/opt-1017/acceptance-final4.md
tests/reports/opt-1017/acceptance-logs/concurrency.log
tests/reports/opt-1017/acceptance-logs/enterprise.log
tests/reports/opt-1017/acceptance-logs/loopback.log
tests/reports/opt-1017/acceptance-logs/realdevice.log
tests/reports/opt-1017/acceptance-logs/resilience.log
tests/reports/opt-final3.json
tests/reports/opt-final3.md
tests/reports/opt-final4.json
tests/reports/opt-final4.md
tests/reports/real_device_opt-final3.json
tests/reports/real_device_opt-final3.md
tests/reports/real_device_opt-final4.json
tests/reports/real_device_opt-final4.md
tests/screenshots/01-session.png
tests/screenshots/02-transfers.png
tests/screenshots/03-receive.png
tests/screenshots/04-settings.png
tests/screenshots/06-chat.png
tests/screenshots/07-preview.png
tests/screenshots/08-preview-image.png
tests/screenshots/09-emoji-and-sections.png
tests/screenshots/10-offline-devices.png
tests/screenshots/11-sections-collapsed.png
tests/screenshots/12-emoji-in-bubble.png
tests/screenshots/12-transfers-failed.png
tests/screenshots/13-one-row-per-file.png
tests/screenshots/13-settings-top.png
tests/screenshots/14-transfer-record-in-chat.png
tests/screenshots/15-settings-maintenance.png
tests/screenshots/16-one-phone-one-row.png
tests/screenshots/conversation-list.png
tests/test_enterprise_acceptance.py
tools/acceptance_opt1017.py
```

| # | 检查项 | 结论 | 失败的子断言 |
|---|---|---|---|
| C11 | 预览：类型矩阵、工具栏、图片适应窗口不放大、文本等宽、二进制不装 | ✅ PASS | — |

## 失败明细
