# 企业级验收报告 · audit-1004

生成时间：2026-10-04 03:52:03　·　HEAD `c3245e6`　·　变更文件 172 个（仓库处于写入中）　·　load average `[10.06, 6.48, 5.19]` / 8 核

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
| C11 | 预览：类型矩阵、工具栏、图片适应窗口不放大、文本等宽、二进制不装 | ❌ FAIL | — |
| C12 | 网页版：同一套 IA 与 token、无三层嵌套、两次点击可达 | ✅ PASS | — |
| C13 | 自动发现：≤5s 零手动、扫描兜底、跨网段覆盖、bye 即时离线 | ✅ PASS | — |
| C14 | 消息必达：ACK、待重发队列、重连补投、失败原因码 | ❌ FAIL | C14.3c 队列在补投成功后清空、C14.3d 有 chat_delivered 事件 |
| E4 | 无回归：宪章 §8 的现有测试继续全绿 | ❌ FAIL | E4 compileall 退出码 0（0.0s） |
| E5 | 性能：窗口冷启动 ≤1.5s、1000 条传输记录滚动流畅 | ❌ FAIL | E5.1 冷启动（新进程） |
| E6 | 交付质量：README 里每一条截图引用都能打开 | ✅ PASS | — |

## 失败明细

### C11 预览：类型矩阵、工具栏、图片适应窗口不放大、文本等宽、二进制不装
```
Traceback (most recent call last):
  File "/home/xgl/我的项目/自制软件/韧传/eversend/tests/test_enterprise_acceptance.py", line 2519, in run
    fn(recorder)
    ~~^^^^^^^^^^
  File "/home/xgl/我的项目/自制软件/韧传/eversend/tests/test_enterprise_acceptance.py", line 1643, in c11
    from PySide6.QtTest import QTest
ModuleNotFoundError: No module named 'PySide6.QtTest'
```

### C14 消息必达：ACK、待重发队列、重连补投、失败原因码
- **C14.3c 队列在补投成功后清空**
  - 期望：`队列里不再有它`
  - 实际：`[{'messageId': '0af3985dd46849d28a80', 'conv': 'd:ba35f86dc84b62d09bbd538b76488956|f13596ae6248171353f37c99f8547cd4', 'peer': 'ba35f86dc84b62d09bbd538b76488956', 'peerName': 'QA-B', 'state': 'sent', 'attempts': 1, 'maxAt`
- **C14.3d 有 chat_delivered 事件**
  - 期望：`chat_delivered 事件`
  - 实际：`['chat_sent', 'announced', 'chat_failed', 'announced', 'chat_queued', 'chat_sent', 'chat_sent', 'device_updated']`

### E4 无回归：宪章 §8 的现有测试继续全绿
```
Traceback (most recent call last):
  File "/home/xgl/我的项目/自制软件/韧传/eversend/tests/test_enterprise_acceptance.py", line 2519, in run
    fn(recorder)
    ~~^^^^^^^^^^
  File "/home/xgl/我的项目/自制软件/韧传/eversend/tests/test_enterprise_acceptance.py", line 2293, in e04
    proc = subprocess.run(command, cwd=str(REPO), capture_output=True, text=True,
                          timeout=timeout_s, env=env)
  File "/home/xgl/我的项目/自制软件/韧传/EverSend/runtime/lib/python3.14/subprocess.py", line 555, in run
    with Popen(*popenargs, **kwargs) as process:
         ~~~~~^^^^^^^^^^^^^^^^^^^^^^
  File "/home/xgl/我的项目/自制软件/韧传/EverSend/runtime/lib/python3.14/subprocess.py", line 1039, in __init__
    self._execute_child(args, executable, preexec_fn, close_fds,
    ~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                        pass_fds, cwd, env,
                        ^^^^^^^^^^^^^^^^^^^
    ...<5 lines>...
                        gid, gids, uid, umask,
                        ^^^^^^^^^^^^^^^^^^^^^^
                        start_new_session, process_group)
                        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/xgl/我的项目/自制软件/韧传/EverSend/runtime/lib/python3.14/subprocess.py", line 1990, in _execute_child
    raise child_exception_type(errno_num, err_msg, err_filename)
FileNotFoundError: [Errno 2] 没有那个文件或目录: '.venv/bin/python'
```
- **E4 compileall 退出码 0（0.0s）**
  - 期望：`exit 0`
  - 实际：`exit=127；末尾输出：bash: 行 1: .venv/bin/python: 没有那个文件或目录`
  - 说明：宪章 E4 + §8 验收命令

### E5 性能：窗口冷启动 ≤1.5s、1000 条传输记录滚动流畅
- **E5.1 冷启动（新进程）**
  - 期望：`通过`
  - 实际：`tools/qa_cold_start.py 两次都没量到`
