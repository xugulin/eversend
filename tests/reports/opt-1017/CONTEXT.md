# 韧传 EverSend · 2026-10-17 优化工程 · 共享上下文（Lead 维护，所有专家先读这份）

## 0. 目标（用户原话）

> 这是一个 win/linux/安卓设置之间互传文件的项目，现在要求再次学习共享基座的知识，要求多专家联合，
> 从最底层开始彻底优化这个项目传输的稳定性和速度以及 GUI 界面显示的准确度。
> 现在电脑上已经连接了安卓手机，目前先进行安卓手机 apk 版和 linux 之间的真机真系统测试。

判定标准：**真实真机、可复现测量、前后对比、不回归**。每项优化要有「改前数字 / 改后数字 / 复现命令」。

## 1. 仓库与基线状态

- 仓库：`/home/xgl/我的项目/自制软件/韧传/eversend`
- git：分支 `main`，HEAD = `e18c079`（本次优化基线快照，内容 = 2026-10-04「企业级改造完成」状态）。
  备份标记分支：`opt-1017/baseline`。**禁止** commit / checkout / reset / 改分支。
- 上一轮全量验收：`tests/reports/audit-1004-final.md` → 17/17 PASS（2026-10-04）。
- 工作树由所有专家共享；改动即时可见。**只改自己任务 write_scopes 内的文件**。

## 2. 运行环境

- 解释器：系统 `python3`（3.14.7），PySide6 6.11.2、cryptography 50.0.2 已装。
  从 `eversend/` 目录下 `PYTHONPATH=src python3 ...` 直接跑源码，**不需要 .venv**（.venv 已不存在）。
- 便携版在 `../EverSend`（内嵌 runtime + `app/` 下一份源码副本）。开发期**不要改 `app/` 里的副本**，
  它只用于打包/绿色包验证；改源码后如需重打绿色包再走 `tools/build_green.py`。
- 图形：`Xvfb :99`（1680x1050，已有 openbox）。**必须 `DISPLAY=:99 QT_QPA_PLATFORM=xcb`** ——
  当前 shell 环境里 `QT_QPA_PLATFORM=wayland`，不覆盖会让 Qt 窗口不出现。
- 默认端口：TCP 52117（传输）、UDP 52118（发现）、TCP 52119（Web/App API）、UDP 5353（mDNS）。
  默认端口已被 Lead 的基线实例占用。**自起实例一律用独立端口 + 独立数据目录**，例如：

  ```bash
  mkdir -p .testscratch/<你的名字>-home
  EVERSEND_HOME=$PWD/.testscratch/<你的名字>-home PYTHONPATH=src DISPLAY=:99 QT_QPA_PLATFORM=xcb \
    python3 -m eversend --cli serve --port 53117 --web --web-port 53119 ...
  ```

  （GUI 的端口用环境变量/设置文件；引擎配置见 `core/engine.py: EngineConfig`。）
- 测试临时目录：`eversend/.testscratch/`（大盘，不是 /tmp tmpfs）；可用 `EVERSEND_TEST_TMP` 覆盖。
- 不要 kill 不是自己启动的进程；用 `ss -ltnp` 先看端口占用。

## 3. 真机（这是本阶段重点）

- adb serial：`2430d3c`；手机 **MI 9（cepheus）**，Android 16 / API 36，arm64-v8a，屏幕 1080x2340。
- 手机 WiFi `192.168.110.158/24`；电脑 WiFi `192.168.110.41/24`。手机→电脑 ping 通，
  RTT 会在 17~100ms 抖动（WiFi 省电），做速度测量时要记录这个背景条件。
- App 包名 `cn.eversend.app`；当前装的是 2026-09-26 构建的 v1.0.0 arm64 APK（Lead 已装）。
  已实测：App 打开后通过组播自动发现电脑（`xgl@192.168.110.41`，在线/已信任）。
- **ADB 写操作（input tap / swipe / am start / force-stop / install）默认归 Lead**；
  专家可用只读命令：`adb logcat`、`adb shell dumpsys`、`adb exec-out screencap`、`adb shell getprop`。
  需要驱动 UI 时先给 Lead 发消息约定时间窗。
- 手机已设常亮（`svc power stayon true` + `stay_on_while_plugged_in=7`）。
  截图全黑 = 屏幕灭了：`adb shell input keyevent 224` 再截。
- Android 构建：本机**没有** JDK / Android SDK / gradle（`~/android-dev` 为空）。
  CI 配置见 `.github/workflows/android-app.yml`（JDK 17 + gradle 8.7 + compileSdk 34，minSdk 24）。
  `dl.google.com` 与 maven 直连可用（实测 200，~0.3–1.2s）。谁负责安卓构建见任务说明。

## 4. 代码地图（先读再动手）

| 路径 | 作用 | 关键点 |
|---|---|---|
| `src/eversend/core/` | 自研 EVSD 二进制协议传输内核 | 桌面↔桌面走这条；`protocol.md` 权威描述；分块 1–16 MiB、拉取式、流水线 window 2–8、AEAD ChaCha20-Poly1305 |
| `src/eversend/web/server.py` | ~4000 行 HTTP API + 浏览器界面 + 上传/下载 | **手机 App↔电脑走这条**；`/api/upload` 原始 body 流式落 spool、`/api/download` 支持 Range、SSE 事件；APK 速度/稳定性/显示准确性关键 |
| `src/eversend/desktop/` | Qt 桌面 GUI | 进度%、速度、ETA、传输记录、设备列表显示准确性 |
| `src/eversend/web/assets/` | 浏览器 UI（app.js/app.css/index.html） | 三端同一套 IA |
| `android/app/src/main/java/cn/eversend/app/` | Kotlin Compose App | `ApiClient.kt`（HttpURLConnection + 发现）、`Discovery.kt`、`Fetch.kt`、`LinkService.kt`、`MainActivity.kt` |
| `src/eversend/desktop/theme.py` | 设计 token 唯一来源 | |
| `tests/` | 测试套件 | `test_loopback.py`(内核 e2e)、`test_resilience.py`(恶劣网络)、`test_concurrency.py`、`test_enterprise_acceptance.py`(C1–C14 验收)、`test_gui_*`、`test_real_device.py`(真机 harness，上一台 vivo 用) |
| `tools/` | 构建/QA 工具 | `build_green.py`、`qa_*`、`ci_android_*` |

必备文档：`docs/ARCHITECTURE.md`（分层/不变式/线程模型）、`docs/PROTOCOL.md`（线协议）、
`docs/ENTERPRISE_CHARTER.md`（验收口径）、`docs/UI_LAYOUT.md`（三端布局）、
`docs/REAL_DEVICE_FINDINGS.md`（上一台 vivo 真机的发现，MI9 需重新验证）。

## 5. 并行纪律

1. **阶段 1 = 审计（默认只读）**：不改产品代码，产出报告到 `tests/reports/opt-1017/<任务名>.md`。
2. 每条结论必须有可复现命令 + 证据（输出/截图/数字）；没测过写「未测」，不许猜。
3. 发现别人范围内的 bug：写进自己报告 + 消息通知 Lead，不越界改。
4. 需要改产品代码时等 Lead 建「阶段 2」任务（带 write_scopes 与验收命令），再动手。
5. 完成闭环：报告落盘 → `team_task_update` complete（3–5 行结论）→ 给 Lead `send_message`（简短）。
6. 任何长时间测量（>2 分钟）放后台 job 或 nohup，别堵住 step。

## 6. 共享基座

- 动手前 `jizuo_exp_find` 查历史经验；做完Lead 统一 `jizuo_exp_record`（领域=`软件`）。
- 已知有用的基座经验方向：真机 ADB 驱动、无 WM 的 Xvfb 键盘焦点、HTTP chunked、缓存整份覆盖、
  判据仪器正例自检、find 权限噪声等。查了再动手，别重复踩坑。

## 7. 本阶段第一批任务（见共享任务板）

| 任务 | 负责人 | 产出 |
|---|---|---|
| Lead：MI9 真机 APK↔Linux 端到端基线 | Lead | 速度/稳定性/显示准确性基线数字 + 问题清单 |
| transport：传输内核底层审计 | transport-core | 协议/加密/IO/并发瓶颈与收益评估 |
| android：安卓端审计 + 构建工具链 | android-device | 可复现构建、安卓侧问题清单 |
| gui：三端显示准确性审计 | gui-desktop | 显示值与真值不符的逐条证据 |
| perf：可复现性能基准 | perf-bench | 基准脚本 + 改前数字 |

（本文件由 Lead 维护，更新以 Lead 消息为准。）
