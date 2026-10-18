# 全量验收报告 · final3

- 时间：2026-10-18 02:52:24　·　耗时：10.1 分钟　·　Python 3.14.7
- HEAD：`8bd847c opt-1017 round5: 续传完成快照落盘（跨重启幂等）+ 最终全量验收（含逐项复验）`　·　工作树变更：0 项
- 负载：(1.2607421875, 1.03955078125, 1.1591796875)（16 核）

## 结果

| 步骤 | 内容 | 结果 | 耗时 | 日志 |
|---|---|---|---|---|
| compile | 编译检查（src/tests/tools 顶层） | ✅ PASS | 0.1s | `compile.log` |
| loopback | 内核 e2e（loopback，含 2GiB 吞吐） | ✅ PASS | 13.7s | `loopback.log` |
| resilience | 恶劣网络/中断恢复 | ✅ PASS | 22.3s | `resilience.log` |
| concurrency | 并发传输 | ✅ PASS | 20.8s | `concurrency.log` |
| chat | 聊天/记录模型 | ✅ PASS | 4.9s | `chat.log` |
| http-resume | 可续传上传（opt-1017 新增） | ✅ PASS | 1.6s | `http-resume.log` |
| web | 网页版 356 项自检 | ✅ PASS | 51.7s | `web.log` |
| gui-render | Qt 离屏渲染 | ✅ PASS | 9.3s | `gui-render.log` |
| gui-actions | Qt 真点击交互 | ✅ PASS | 189.7s | `gui-actions.log` |
| enterprise | 企业级验收 C1-C14 | ✅ PASS | 163.9s | `enterprise.log` |
| realdevice | MI9 真机 A0-A9（APK↔Linux） | ❌ FAIL(exit=1) | 128.7s | `realdevice.log` |

**10 / 11 步通过**

## 冻结树证据

```
HEAD: 8bd847c opt-1017 round5: 续传完成快照落盘（跨重启幂等）+ 最终全量验收（含逐项复验）
变更文件数: 0
```

## 失败步骤明细

### realdevice（exit=1）

```
=== A7 手机发文件给电脑 → 电脑接收目录 + sha256 ===
--- A7: PASS ---
=== A8 双向中文 + emoji 消息，两端都能看到 ===
--- A8: PASS ---
==============================================================
  A0   PASS      现场：真机在线、三个端口空闲、测试目录就绪
  A1   PASS      电脑发小文件（<1 MB）→ 手机磁盘 + sha256
  A2   PASS      电脑发中等文件（约 100 MB）→ 手机磁盘 + sha256
  A9   UNDECIDED 电脑发大文件（约 1 GB，可选）→ 手机磁盘 + sha256
  A6   PASS      反作弊：界面说「已收到」时手机磁盘必须真的有（否则判红）
  A7   PASS      手机发文件给电脑 → 电脑接收目录 + sha256
  A8   PASS      双向中文 + emoji 消息，两端都能看到
==============================================================
  6/7 项通过；1 项未判定（前提缺失，不记绿）
报告已写入 /home/xgl/我的项目/自制软件/韧传/eversend/tests/reports/real_device_opt-final3.md 与 .json
```

