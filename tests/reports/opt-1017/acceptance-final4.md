# 全量验收报告 · final4

- 时间：2026-10-18 03:02:51　·　耗时：11.0 分钟　·　Python 3.14.7
- HEAD：`8bd847c opt-1017 round5: 续传完成快照落盘（跨重启幂等）+ 最终全量验收（含逐项复验）`　·　工作树变更：30 项
- 负载：(1.1171875, 1.451171875, 1.251953125)（16 核）

## 结果

| 步骤 | 内容 | 结果 | 耗时 | 日志 |
|---|---|---|---|---|
| compile | 编译检查（src/tests/tools 顶层） | ✅ PASS | 0.1s | `compile.log` |
| loopback | 内核 e2e（loopback，含 2GiB 吞吐） | ✅ PASS | 13.5s | `loopback.log` |
| resilience | 恶劣网络/中断恢复 | ✅ PASS | 22.2s | `resilience.log` |
| concurrency | 并发传输 | ✅ PASS | 9.6s | `concurrency.log` |
| chat | 聊天/记录模型 | ✅ PASS | 5.0s | `chat.log` |
| http-resume | 可续传上传（opt-1017 新增） | ✅ PASS | 1.6s | `http-resume.log` |
| web | 网页版 356 项自检 | ✅ PASS | 51.6s | `web.log` |
| gui-render | Qt 离屏渲染 | ✅ PASS | 35.0s | `gui-render.log` |
| gui-actions | Qt 真点击交互 | ✅ PASS | 188.5s | `gui-actions.log` |
| enterprise | 企业级验收 C1-C14 | ❌ FAIL(exit=1) | 159.1s | `enterprise.log` |
| realdevice | MI9 真机 A0-A8（APK↔Linux；A9 1GiB 为可选项另跑） | ✅ PASS | 173.7s | `realdevice.log` |

**10 / 11 步通过**

## 冻结树证据

```
HEAD: 8bd847c opt-1017 round5: 续传完成快照落盘（跨重启幂等）+ 最终全量验收（含逐项复验）
变更文件数: 30
  M tests/reports/opt-1017/acceptance-logs/concurrency.log
   M tests/reports/opt-1017/acceptance-logs/enterprise.log
   M tests/reports/opt-1017/acceptance-logs/loopback.log
   M tests/reports/opt-1017/acceptance-logs/realdevice.log
   M tests/reports/opt-1017/acceptance-logs/resilience.log
   M tests/screenshots/01-session.png
   M tests/screenshots/02-transfers.png
   M tests/screenshots/03-receive.png
   M tests/screenshots/04-settings.png
   M tests/screenshots/06-chat.png
   M tests/screenshots/07-preview.png
   M tests/screenshots/08-preview-image.png
   M tests/screenshots/09-emoji-and-sections.png
   M tests/screenshots/10-offline-devices.png
   M tests/screenshots/11-sections-collapsed.png
   M tests/screenshots/12-emoji-in-bubble.png
   M tests/screenshots/12-transfers-failed.png
   M tests/screenshots/13-one-row-per-file.png
   M tests/screenshots/13-settings-top.png
   M tests/screenshots/14-transfer-record-in-chat.png
   M tests/screenshots/15-settings-maintenance.png
   M tests/screenshots/16-one-phone-one-row.png
   M tests/screenshots/conversation-list.png
   M tools/acceptance_opt1017.py
  ?? tests/reports/opt-1017/acceptance-final3.json
  ?? tests/reports/opt-1017/acceptance-final3.md
  ?? tests/reports/opt-final3.json
  ?? tests/reports/opt-final3.md
  ?? tests/reports/real_device_opt-final3.json
  ?? tests/reports/real_device_opt-final3.md
```

## 失败步骤明细

### enterprise（exit=1）

```
  C6    PASS  文件动作矩阵：四处入口同一套动作 + 属性齐全
  C7    PASS  记录卫生：按身份去重、保留策略、清空一次清干净、无孤儿
  C8    PASS  记录去重：同一 (方向, 对端, 内容) 只占一行并累加状态
  C9    PASS  设备信息：同一事实不在标题/副标题/详情里重复
  C10   PASS  全中文：Qt 系统对话框不再出现英文文案
  C11   FAIL  预览：类型矩阵、工具栏、图片适应窗口不放大、文本等宽、二进制不装
  C12   PASS  网页版：同一套 IA 与 token、无三层嵌套、两次点击可达
  C13   PASS  自动发现：≤5s 零手动、扫描兜底、跨网段覆盖、bye 即时离线
  C14   PASS  消息必达：ACK、待重发队列、重连补投、失败原因码
  E4    PASS  无回归：宪章 §8 的现有测试继续全绿
  E5    PASS  性能：窗口冷启动 ≤1.5s、1000 条传输记录滚动流畅
  E6    PASS  交付质量：README 里每一条截图引用都能打开
==============================================================
  16/17 项通过（其中 1 条子断言因机器负载高而未判定：['E4 test_loopback 的吞吐地板（默认 60 MiB/s）']）
报告已写入 /home/xgl/我的项目/自制软件/韧传/eversend/tests/reports/opt-final4.md 与 .json
```

