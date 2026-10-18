# 截图索引（QA 维护）

命名约定：**QA 拍的都带 `qa-` 前缀**，不覆盖实现方/其他测试已有的文件。
两套截图并存：

| 目录 | 用途 | 谁往里写 | 命名空间 |
|---|---|---|---|
| `docs/screenshots/` | **README 画廊**（三端都放，靠前缀区分） | 各端实现者 + QA 的 `qa-*` | 桌面 `NN-*.png`、网页 `web-*.png`、安卓 `android-app-*.png`、QA 补充 `qa-*` |
| `tests/screenshots/` | **测试产物**（不被 README 引用） | `tests/test_gui_render.py`、`tools/qa_screenshots.py` | `NN-*.png`、`qa-*.png`、`qa-*-before.png` |

> **目录语义（Lead 2026-09-25 核实，别再搞混）**：README 引用了 42 张 `docs/screenshots/*.png`，
> 而 `tests/screenshots/` 在 README 里**一次都没被引用**。桌面图出现在 `docs/screenshots/` 里
> **不是有人跑错目录**，而是画廊本来就在那儿 —— `EVERSEND_SHOT_DIR=docs/screenshots` 跑渲染测试
> 正是**重拍画廊的正式做法**。

### 交付前必做：重拍 README 画廊

```bash
EVERSEND_SHOT_DIR=docs/screenshots xvfb-run -a -s "-screen 0 1280x900x24" \
    .venv/bin/python tests/test_gui_render.py
```

**并且跑一次引用对照**：`tests/reports/readme_screenshots_audit.md`（README 引用 vs 磁盘实况）。
这份对照会把"引用了但不存在"（README 上是坏图）与"存在但没被引用"都列出来。

> 为什么另起一套：`test_gui_render.py` 拍的是"界面长什么样"，QA 这套拍的是
> **"验收要判定什么"**（例如故意塞两行同身份设备，看列表里是不是只有一行）。

---

## 桌面端 · `tests/screenshots/qa-*.png`

重拍：`xvfb-run -a -s "-screen 0 1280x900x24" .venv/bin/python tools/qa_screenshots.py`

| 文件 | 验收对应 | 能看出什么 |
|---|---|---|
| `qa-01-device-list.png` | C4 / C1 | 故意给两行同 `device_id` 的设备 → 列表里**只有一行**；侧栏操作行是「扫描 + 下拉箭头 / 手机 / 群聊」，**没有独立的「刷新」「手动添加 IP」**；底部导航是唯一的页面入口，主区**没有** `[会话｜传输]` 分段控件 |
| `qa-02-transfers.png` | C5 | 筛选药丸（全部/进行中/已完成/失败）+ 搜索框 + 可排序列头 + 空状态 + 右上角只有一个主按钮「打开接收文件夹」与 `⋯` |
| `qa-03-transfers-fail.png` | C5 | 失败行：红药丸「失败」+ 中文原因 + **操作列「重试」**（修复后） |
| `qa-03-transfers-fail-before.png` | C5 | **修复前对照（before）**：同一场景下操作列只有「移除」、没有「重试」—— `C5.5b` 当时报红的原始样子。**这张不代表现在的界面** |
| `qa-04-settings-diag.png` | E2 | 设置页分组卡片（含诊断信息） |
| `qa-05-preview-image.png` | C11 | 图片预览：标题=文件名、工具栏九个动作、**适应窗口不放大超过 100%** |
| `qa-06-preview-text.png` | C11 | 文本预览：等宽字体、只读、可选中 |
| `qa-07-preview-binary.png` | C11 | 二进制预览：类型图标 + 名字 + 大小 + 动作，**不装作能看** |
| `qa-08-chat-pending.png` | C14 | 会话里的待发消息（`state=queued`），对应"对面不在线 → 时钟角标 + 等待对方上线" |

**命名约定**：`-before` 后缀 = **修复前对照**，不代表当前界面。
`qa-03-transfers-fail-before.png` 是 `C5.5b` 修复前的样子；同名的 `qa-03-transfers-fail.png`
已按修复后的代码重拍（操作列 = 「重试」），`C5` 复跑 PASS 见 `tests/reports/recheck-c5.md`。
`tests/screenshots/` 里 `01-*.png` … `23-*.png` 那一套由 `tests/test_gui_render.py`（desktop-shell）维护，
全部拍于本轮改造之后（mtime 19:08/19:10），没有混入改造前的旧图；`qa-*` 是本文件维护的 QA 套。

## 网页版 · `docs/screenshots/qa-web-*.png`

重拍（需要两个后台进程）：

```bash
# 1) 起一个只为截图的实例（打印 WEB_URL）
EVERSEND_HOME=/tmp/qa-web-shots .venv/bin/python tools/qa_web_server.py --port 53919 &

# 2) 起一个私有 headless 浏览器并开放 CDP（面板浏览器不允许新建标签页）
/opt/brave.com/brave-origin-beta/brave --headless=new --no-sandbox --disable-gpu \
    --remote-debugging-port=9444 --user-data-dir=/tmp/qa-brave-cdp about:blank &

# 3) 截图
.venv/bin/python tools/cdp_shot.py --port 9444 --url http://127.0.0.1:53919/ \
    --width 1440 --height 900 --desktop --scale 1 --out docs/screenshots/qa-web-wide.png
.venv/bin/python tools/cdp_shot.py --port 9444 --url http://127.0.0.1:53919/ \
    --width 390 --height 844 --scale 2 --out docs/screenshots/qa-web-mobile.png
```

| 文件 | 验收对应 | 能看出什么 |
|---|---|---|
| `qa-web-wide.png` | C12 / C1 | 宽屏：侧栏固定 288px、主区只有一种内容、设备操作行只有「扫描 + ▾ / 连接手机 / 群聊」、底部导航 传输/设置、深色 token 与桌面端同值 |
| `qa-web-mobile.png` | C12 | 390×844：侧栏变抽屉（☰），主区只剩内容 + 输入区 |
| `qa-web-transfers.png` | C12 / C5 | 传输页 |
| `qa-web-settings.png` | C12 / E2 | 设置页 |

## 安卓端 · `docs/screenshots/android-*.png`

目录里已有一批 `android-app-*.png`（设备分段/一行一设备/文件动作/预览/表情/清空离线…），
由实现方在模拟器上拍摄，**QA 无法复现**（本会话没有可用的安卓模拟器）。

**证据等级说明（重要）**：这些截图是**实现方提供的证据**，不是 QA 独立复现的结论。
终局报告里凡涉及安卓的条目会写成"静态断言通过 + 实现方截图，**未由 QA 真机复现**"，
不会记成 QA 验证通过的绿。android-ui 重拍后请把新文件名与拍摄时间补到这张表里。

| 文件 | 对应 |
|---|---|
| `android-app-device-sections.png` / `-offline.png` | C4 在线/离线两段 |
| `android-app-one-device-one-row.png` | C4 同一身份一行 |
| `android-app-file-actions.png` / `android-app-transfer-row-actions.png` | C6 文件动作矩阵 |
| `android-app-one-row-per-file.png` | C8 一个文件一行 |
| `android-app-preview-*.png` | C11 预览三态 |
| `android-app-settings.png` | E2 设置页 |


---

## 两点环境事实（供终局报告引用）

1. **安卓 APK 确实编译出来了**（Lead 核实，19:09）：
   `android/app/build/outputs/apk/debug/app-arm64-v8a-debug.apk`（72 MB）、
   `app-x86_64-debug.apk`（80 MB）。"能编译"是硬证据；
   "在模拟器上跑起来是什么样"仍然是**未由 QA 复现**的那一半。
2. **README 画廊有 13 张坏图**（详见 `tests/reports/readme_screenshots_audit.md`）：
   README 引用 `web-composer-tiles.png` / `web-transfers.png` / `web-preview-image.png` 等 13 个名字，
   而 web-ui 这轮把产物改名成了 `web-01-session-wide.png` … `web-11-settings-light.png`，
   旧名字的文件**已经不在磁盘上** → README 上这 13 条 `![](...)` 全部打不开。
   另有 6 张桌面引用（`02-transfers`/`03-receive`/`10-offline-devices`/`12-emoji-in-bubble`/
   `13-one-row-per-file`/`14-transfer-record-in-chat`/`15-settings-maintenance`/`16-one-phone-one-row`）
   是**改造前遗留**，`test_gui_render.py` 已不再产出这些文件名。
