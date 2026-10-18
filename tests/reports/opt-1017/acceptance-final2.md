# 全量验收报告 · final2

- 时间：2026-10-18 02:27:16　·　耗时：21.4 分钟　·　Python 3.14.7
- HEAD：`0346e2a opt-1017 round4: 关联 shareId 写时保存（同名串台根除）+ PG-3 真机截图证据`　·　工作树变更：2 项
- 负载：(0.36181640625, 0.52978515625, 0.84375)（16 核）

## 结果

| 步骤 | 内容 | 结果 | 耗时 | 日志 |
|---|---|---|---|---|
| compile | 编译检查（src/tests/tools 顶层） | ✅ PASS | 0.1s | `compile.log` |
| loopback | 内核 e2e（loopback，含 2GiB 吞吐） | ✅ PASS | 13.6s | `loopback.log` |
| resilience | 恶劣网络/中断恢复 | ✅ PASS | 22.0s | `resilience.log` |
| concurrency | 并发传输 | ✅ PASS | 9.6s | `concurrency.log` |
| chat | 聊天/记录模型 | ✅ PASS | 5.1s | `chat.log` |
| http-resume | 可续传上传（opt-1017 新增） | ✅ PASS | 1.6s | `http-resume.log` |
| web | 网页版 356 项自检 | ✅ PASS | 51.6s | `web.log` |
| gui-render | Qt 离屏渲染 | ✅ PASS | 9.2s | `gui-render.log` |
| gui-actions | Qt 真点击交互 | ✅ PASS | 190.9s | `gui-actions.log` |
| enterprise | 企业级验收 C1-C14 | ❌ FAIL(exit=1) | 809.7s | `enterprise.log` |
| realdevice | MI9 真机 A0-A9（APK↔Linux） | ❌ FAIL(exit=1) | 169.8s | `realdevice.log` |

**9 / 11 步通过**

## 冻结树证据

```
HEAD: 0346e2a opt-1017 round4: 关联 shareId 写时保存（同名串台根除）+ PG-3 真机截图证据
变更文件数: 2
  M src/eversend/web/server.py
   M tests/test_http_resume.py
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
  E4    FAIL  无回归：宪章 §8 的现有测试继续全绿
  E5    PASS  性能：窗口冷启动 ≤1.5s、1000 条传输记录滚动流畅
  E6    PASS  交付质量：README 里每一条截图引用都能打开
==============================================================
  15/17 项通过（其中 1 条子断言因机器负载高而未判定：['E4 test_loopback 的吞吐地板（默认 60 MiB/s）']）
报告已写入 /home/xgl/我的项目/自制软件/韧传/eversend/tests/reports/opt-final2.md 与 .json
```

### realdevice（exit=1）

```
           期望: 手机界面含 'QA 真机消息 🚀 中文+emoji 62867'
           实际: 手机界面片段=['💻', 'QA-真机验收', 'linux · 192.168.110.41:52119 · 发现方式：组播', '在线', '已信任', '打开接收文件夹', '⋯', '已发给 1 台设备（QA-真机验收）', 'QA-真机验收', '与 一台设备 的对话（这台设备现在不在列表里）', '← 会话', '预览']
==============================================================
  A0   PASS      现场：真机在线、三个端口空闲、测试目录就绪
  A1   PASS      电脑发小文件（<1 MB）→ 手机磁盘 + sha256
  A2   PASS      电脑发中等文件（约 100 MB）→ 手机磁盘 + sha256
  A9   UNDECIDED 电脑发大文件（约 1 GB，可选）→ 手机磁盘 + sha256
  A6   PASS      反作弊：界面说「已收到」时手机磁盘必须真的有（否则判红）
  A7   PASS      手机发文件给电脑 → 电脑接收目录 + sha256
  A8   FAIL      双向中文 + emoji 消息，两端都能看到
==============================================================
  5/7 项通过；1 项失败；1 项未判定（前提缺失，不记绿）
报告已写入 /home/xgl/我的项目/自制软件/韧传/eversend/tests/reports/real_device_opt-final2.md 与 .json
192.168.110.158 - Request timed out: TimeoutError('timed out')
192.168.110.158 - Request timed out: TimeoutError('timed out')
```

