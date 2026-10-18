# 企业级验收报告 · opt-final2

生成时间：2026-10-18 02:32:19　·　HEAD `0346e2a`　·　变更文件 26 个（仓库处于写入中）　·　load average `[0.89, 0.82, 0.89]` / 16 核

> **冻结树规则**：`final` 必须对着**冻结的树**跑 —— 开跑前确认没有文件在mtime 上晚于开跑时刻（报告头会记 HEAD + 变更清单 + 各文件 mtime）。对着「别人还在写」的树跑出来的结论只能叫候选，不能当交付证据。

> **时序断言的环境说明**：C13/C14 里有若干带时间预算的断言（≤5s 自动发现、≤1s bye 转离线、补投 ≤20s）。当 load average 接近或超过核数时，这些断言可能**假红**，判断时请对照上面的负载数字。

> **本次复跑时仓库处于写入中**：下面这些文件在跑之前刚被改过，某条 FAIL 可能是某个实现者的中间状态。

```
src/eversend/web/server.py
tests/reports/opt-1017/FINAL_REPORT.md
tests/reports/opt-1017/acceptance-logs/concurrency.log
tests/reports/opt-1017/acceptance-logs/gui-actions.log
tests/reports/opt-1017/acceptance-logs/http-resume.log
tests/reports/opt-1017/acceptance-logs/loopback.log
tests/reports/opt-1017/acceptance-logs/resilience.log
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
tests/test_http_resume.py
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
| C11 | 预览：类型矩阵、工具栏、图片适应窗口不放大、文本等宽、二进制不装 | ❌ FAIL | C11.8g 原始大小模式下「原始大小」药丸亮着、C11.8c →（右方向键）真的切到下一张、C11.8d ←（左方向键）真的切回上一张 |
| C12 | 网页版：同一套 IA 与 token、无三层嵌套、两次点击可达 | ✅ PASS | — |
| C13 | 自动发现：≤5s 零手动、扫描兜底、跨网段覆盖、bye 即时离线 | ✅ PASS | — |
| C14 | 消息必达：ACK、待重发队列、重连补投、失败原因码 | ✅ PASS | — |
| E4 | 无回归：宪章 §8 的现有测试继续全绿 | ❌ FAIL | E4 test_gui_actions 退出码 0（696.5s） |
| E5 | 性能：窗口冷启动 ≤1.5s、1000 条传输记录滚动流畅 | ✅ PASS | — |
| E6 | 交付质量：README 里每一条截图引用都能打开 | ✅ PASS | — |

## 失败明细

### C11 预览：类型矩阵、工具栏、图片适应窗口不放大、文本等宽、二进制不装
- **C11.8g 原始大小模式下「原始大小」药丸亮着**
  - 期望：`actual=checked, fit=unchecked`
  - 实际：`actual=False fit=True`
- **C11.8c →（右方向键）真的切到下一张**
  - 期望：`从 /home/xgl/我的项目/自制软件/韧传/eversend/.testscratch/qa-acc-twc7_l0w/c11-small.png 切走`
  - 实际：`/home/xgl/我的项目/自制软件/韧传/eversend/.testscratch/qa-acc-twc7_l0w/c11-small.png`
- **C11.8d ←（左方向键）真的切回上一张**
  - 期望：`回到 /home/xgl/我的项目/自制软件/韧传/eversend/.testscratch/qa-acc-twc7_l0w/c11-small.png`
  - 实际：`/home/xgl/我的项目/自制软件/韧传/eversend/.testscratch/qa-acc-twc7_l0w/c11-big.png`

### E4 无回归：宪章 §8 的现有测试继续全绿
- **E4 test_gui_actions 退出码 0（696.5s）**
  - 期望：`exit 0`
  - 实际：`exit=-15；末尾输出：[32mPASS[0m 两台各有一张传输卡片 /  / [6] 侧栏设备列表：名字 + 未读 + 置顶 + 群聊`
  - 说明：宪章 E4 + §8 验收命令
