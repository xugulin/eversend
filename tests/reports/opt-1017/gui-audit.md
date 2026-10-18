# 三端 GUI 显示准确性审计（桌面 / 网页 / 手机）

- 任务：task-3（阶段 1 只读审计，未改产品代码）
- 负责人：gui-desktop
- 基线：`e18c079`（2026-10-04 企业级改造完成状态）
- 日期：2026-10-17
- 证据目录：`tests/reports/opt-1017/shots/`；原始真值序列与采集脚本：`eversend/.testscratch/gui-desktop/`
- 环境说明：审计时同机有多个专家任务并行，`load average` 8~12；**本报告里的速度/耗时数字只用于「显示值 vs 真值」对照，不作为性能结论**。

## 0. 方法与真值源（可复现）

| 角色 | 做法 |
|---|---|
| 被审桌面端 A | `EVERSEND_HOME=eversend/.testscratch/gui-desktop/home`，`launch_gui.py` 把 `EngineConfig` 覆盖为 tcp 53117 / web 53119 / discovery 53118、`auto_accept_all=True`（避开 Lead 的 52117/52119）；显示用私有 `Xvfb :98`（1680x1050 + openbox），避免与 Lead 的 `:99` 抢窗口/抢焦点 |
| 发送端 B（真值来源那台「另一台电脑」） | `EVERSEND_HOME=.../sender-home python3 -m eversend --cli send --port 53127 --name B-true --to 127.0.0.1:53117 --wait 2 --no-broadcast --no-mdns <file>`，走真实 EVSD 协议（AEAD + 流水线），不是 mock |
| 后台真值 | 每 150ms 抓 `http://127.0.0.1:53119/api/state`：`transfers[*].{percent,bytes,total,speed,eta,status,elapsed}`（`transfer_dict`, `web/server.py:1875+`）；`/api/events` SSE 抓事件时序 |
| 界面真值 | 同一循环里 `DISPLAY=:98 import -window <win>` 截桌面窗口 → 与同一时刻的 `/api/state` 对齐 |
| 驱动 | `xdotool --window <win> mousemove/click`（:98 上没有别的窗口，不会误触别人的 GUI） |

采集脚本：`eversend/.testscratch/gui-desktop/capture.py`（同时落 `truth/series-*.jsonl` 与 `shots/*.png`，每帧带 api_ms/shot_ms）。

复现命令（核心一条 512MiB 真实接收）：

```bash
cd /home/xgl/我的项目/自制软件/韧传/eversend
BASE=$PWD/.testscratch/gui-desktop
# 1) 起 GUI（需 DISPLAY=:98 的 Xvfb+openbox，见 CONTEXT.md；端口 53117/53119）
EVERSEND_HOME=$BASE/home PYTHONPATH=src DISPLAY=:98 QT_QPA_PLATFORM=xcb \
  nohup python3 $BASE/launch_gui.py > $BASE/gui.log 2>&1 &
# 2) 点开「传输」页（让接收表可见）
DISPLAY=:98 xdotool mousemove --window $(DISPLAY=:98 xdotool search --name '韧传 EverSend'|tail -1) 75 679 click 1
# 3) 一边采帧一边真传 512MiB
(DISPLAY=:98 python3 -u $BASE/capture.py $BASE/truth/series.jsonl 30 0.15 >$BASE/truth/cap.log 2>&1 &)
EVERSEND_HOME=$BASE/sender-home PYTHONPATH=src python3 -m eversend --cli send \
  --port 53127 --name B-true --to 127.0.0.1:53117 --wait 2 --no-broadcast --no-mdns $BASE/files/f512m.bin
```

## 1. 结论摘要

| ID | 优先级 | 一行为结论 |
|---|---|---|
| G-01 | P0 | 每次「接收完成」在传输页产生**两行重复**（「完成/移除」+「已完成/预览」），且其中一行大小显示 **0 B**（真值 4 MiB）。根因：收尾行隐藏条件用了 `"recv"`，实际 direction 是 `"receive"`，恒不成立 |
| G-02 | P0 | 桌面**接收**表没有「进度/速度/剩余」列（RECV_COLUMNS 6 列），传输中只显示「传输中」；同一传输在 `/api/state` 有 percent=62.5、speed=320MB/s，网页端/手机端有百分比 → 三端不一致、桌面接收无进度反馈 |
| G-03 | P1 | 字节收满 100% 后界面仍显示「传输中」**7.6s**（校验/收尾阶段），期间速度显示会持续衰减（327→55MB/s）为误导值；无「校验中/收尾中」状态。（与手机 A2「已保存仍接收中」同类） |
| G-04 | P1 | 单位口径：`human_bytes` 用 1024 进制却标 `KB/MB/GB`。512MiB 显示「512.00 MB」（真值为 512 MiB=536.87 MB，差 -4.6%）；4MiB 显示「4.00 MB」（真值 4.19 MB）。网页 `app.js:69-74` 同口径；三端同口径但标签错 |
| G-05 | P1 | 同一对端在会话页头显示「未知设备 #457b1f5c」，而消息卡片/设备列表显示「B-true」；`/api/state` 真值 name=B-true |
| G-06 | P1 | 快速完成的接收行「大小」显示 0 B（真值 4 MiB）：短传输在首次进度轮询前完成，行对象没拿到 size，收尾也不补 |
| G-07 | P2 | 传输进行中，底部状态栏仍停在上一次的「传输完成：512.00 MB」；没有「正在接收 X / 512.00 MB」全局提示（传输页未打开时用户完全看不到正在进行） |
| G-08 | P2 | 状态词不统一：live 行「完成」/历史行「已完成」/筛选药丸「已完成」；同一事件两套词 |
| G-09 | P2 | 进度小数位三端不一致：桌面 peer 行 `progress_text()` 用 1 位小数（transfers_view.py:263），网页 `toFixed(0)`（app.js:1315/3164），手机端未核对（归 android-device） |
| G-10 | P0 | **每收一个文件，桌面 slot 抛 `UnboundLocalError`**：`main_window.py:3150 _announce_received_file` 中 `other_member` 在「会话已存在」分支未赋值就被引用；Qt 打印 traceback 并吞掉 → 会话文件卡片/会话头名称永远补不上（与 G-05「未知设备 #hash」直接相关） |
| G-11 | P1 | 接收「大小」列显示的是**已接收字节**而不是文件大小：接收开始阶段显示「0 B」→「60.00 MB」→「64.00 MB」（真值文件大小恒为 64.00 MB），列语义错误（`TransferRow.update_progress` receive 分支 transfers_view.py:1459-1462） |
| G-12 | P1 | 接收历史不跨重启：窗口重建（同引擎）后接收表 0 行；代码中接收行只由运行时事件创建（`add_history_row` 仅 main_window.py:2941 调用），没有启动加载路径 → 重启后「传输记录」为空（聊天记录仍在） |

## 2. 逐条证据

### G-01（P0）接收完成 → 同一文件两行，一行 0 B

**真值/现象**：两次接收（f4m.bin 4194304 B、f512m.bin 536870912 B）完成后，「传输 → 接收」表出现 **4 行**：

| 文件 | 来自 | 大小 | 时间 | 状态 | 操作 |
|---|---|---|---|---|---|
| f4m.bin | B-true | **0 B** | 22:26 | 完成 | 移除 |
| f4m.bin | B-true | 4.00 MB | 22:26 | 已完成 | 预览 ⋯ |
| f512m.bin | B-true | 512.00 MB | 22:30 | 完成 | 移除 |
| f512m.bin | B-true | 512.00 MB | 22:30 | 已完成 | 预览 ⋯ |

证据截图：`shots/desktop-transfers-duplicate-rows.png`（`/api/state` 真值此时 `transfers=[]`，磁盘上只有 2 个文件，`ls received/` 2 项，可复核不是传了两次）。

**复现**：按 §0 起 A，B 发送任意文件到 A，完成后点左侧「传输」。
**根因（已定位）**：
- 收尾时打算「历史行接管、隐藏 live 行」的代码：`src/eversend/desktop/main_window.py:2532`
  ```python
  if str(getattr(row, "direction", "")) == "recv" and key in self._history_transfers:
      row.hide()
  ```
- 但 live 行是用引擎的 `transfer.direction` 建的：`main_window.py:2735` → `self._add_transfer_row(transfer.transfer_id, label, transfer.direction)`，引擎接收方向是 **`"receive"`**（`core/engine.py` `_execute_receive` 里 `ActiveTransfer(direction="receive")`）→ 条件恒为假，live 行永不隐藏。
- 历史行状态词固定写「已完成」：`transfers_view.py:959`；live 行收尾写着「完成」：`main_window.py:886`。
- 0 B 行：短传输 live 行在第一次进度轮询时创建、尚未拿到 size（`main_window.py:2735` 建行时不传 size），收尾事件也不回填。

**修法建议**：① 方向判断统一（`in ("recv","receive")` 或把引擎方向规范成 recv/send 一套）；② 收尾时用 `_record_key` 找历史行并合并，而不是依赖到达顺序；③ 收尾事件到达时把 `bytes/total` 回填到行；④ 状态词统一为「已完成」。
**验收断言**：见 §5 A1–A3。

### G-02（P0）桌面接收端没有进度/速度/ETA

**真值/现象**：512MiB 接收进行中（真值 `percent=62.5, bytes=335544320, speed=318MB/s, eta=0.63s`，`/api/state` 由同一进程提供），桌面传输页「接收」表只有 6 列 `文件/来自/大小/时间/状态/操作`，行内仅徽标「传输中」；无百分比、无速度、无剩余时间。
证据截图：`shots/desktop-recv-active-row.png`（同时刻真值见 `.testscratch/gui-desktop/truth/series-512m2.jsonl` frame 20）；`shots/desktop-chat-during-recv.png`（会话页在传输中也没有任何进度卡片）。
**代码事实**：`transfers_view.py:56` `SEND_COLUMNS = ("文件","发给","大小","进度","速度","剩余","状态","操作")`；`:57` `RECV_COLUMNS = ("文件","来自","大小","时间","状态","操作")`；`TransferItem.progress_text/speed_text/eta_text`（`:249-273`）只在发送表被渲染。
**影响**：手机→电脑、电脑↔电脑的「接收」是最常用方向，用户看不到还要多久；网页端同一传输却有进度条，三端 IA 不一致（宪章要求三端同一套 IA）。
**修法建议**：给接收表补「进度/速度/剩余」三列（`progress_text()` 已实现，可直接复用；列宽按窄窗退化），或至少把进度条画进「状态」列并在行内显示 `xx% · 12.3 MB/s · 剩余 8s`；窄窗口下可只留百分比。
**验收断言**：见 §5 A4。

### G-03（P1）100% 后仍「传输中」7.6s，速度显示衰减误导

**真值序列**（`.testscratch/gui-desktop/truth/series-512m2.jsonl`，512MiB 接收）：

| 帧/相对时刻 | `/api/state` 真值 | 界面显示 |
|---|---|---|
| frame 20 / 3.32s | pct=62.5, speed=318.3MB/s, eta=0.63s, status=active | 行徽标「传输中」 |
| frame 21 / 4.04s | pct=100.0, bytes=536870912, speed=318.8MB/s, eta=0 | 「传输中」 |
| frame 44 / 6.92s | pct=100, speed=113.4MB/s | 「传输中」 |
| frame 77 / 11.87s | pct=100, speed=55.4MB/s, 仍 active | 「传输中」 |
| frame 90 / 13.82s | transfers=[]（结束） | 行变「完成」+ 历史行「已完成」 |

即：字节满了之后还有 **7.6s** 停留在 active 状态，界面一直「传输中」，而 `instant_speed` 从 318 衰减到 55（因为窗口内没有新字节）。同源现象在真机上已由 Lead 记录（`/home/xgl/qa-real/shots/A2-phone.png`：下载完成、sha256 一致，卡片仍「接收中…」）。
**根因方向**：接收完成后引擎还要做收尾（写入落盘/校验摘要），但 `ActiveTransfer.stats` 状态没有「verifying/finalizing」阶段，`transfer_dict` 只有 active/failed 两值（`web/server.py:1875+`），UI 无法区分「传字节」与「校验」。具体耗时点建议 transport-core 复核（本机 load 高，7.6s 不能直接当基准）。
**修法建议**：`TransferStats`/`ActiveTransfer` 增加 `phase`（`transferring|verifying|finalizing`）；bytes==total 后 UI 显示「校验中…」、速度显示「—」不再取衰减值、ETA 显示「—」；网页/手机同步。验收断言见 §5 A5。

### G-04（P1）单位：1024 进制算、KB/MB 标签（三端同口径但标签错）

**代码**：`src/eversend/core/model.py:655-667` `human_bytes()` 每级 `/1024.0`，但 `units = ("B","KB","MB","GB","TB","PB")`；`human_speed`（`:668`）直接拼接。网页同一套算法（`web/assets/app.js:67-74`，`size /= 1024` + `units` 是 KB/MB/GB）。手机端 Lead 实测「512.0 KB / 100.0 MB」对应 524288/104857600 B，同为 1024 进制 → 三端统一，但都把 KiB/MiB 标成 KB/MB。
**真值 vs 显示**（截图 `desktop-transfers-duplicate-rows.png`、CLI 同屏输出）：

| 文件真实字节 | SI 十进制 | IEC | 三端显示 | 误差（按 SI） |
|---|---|---|---|---|
| 4 194 304 | 4.19 MB | 4.00 MiB | 4.00 MB | -0.19 MB（-4.6%） |
| 536 870 912 | 536.87 MB | 512.00 MiB | 512.00 MB | -24.87 MB（-4.6%） |
| 1 048 576（手机证据） | 1.05 MB | 1.00 MiB | 1000.0 KB | -4.6% |

**建议**：二选一并全端统一——(a) 保留 1024 进制但单位改 `KiB/MiB/GiB`；(b) 改 10 进制（`/1000`）标签 KB/MB/GB。考虑到设备厂商/SI 习惯，推荐 (a)，并加一份 `tests/test_display_units.py` 用上表字节数断言字符串。验收断言见 §5 A6。

### G-05（P1）会话页头「未知设备 #hash」 vs 卡片「B-true」

证据：`shots/desktop-chat-during-recv.png`（传输刚完成时）：页头「未知设备 #457b1f5c」+「这台设备现在不在设备列表里；会话记录还在…」；同一屏消息卡片署名「B-true」；左侧设备列表（离线）显示「B-true」。`/api/state` 里该 peer `name="B-true"`。
**影响**：用户会以为收到了陌生设备的文件（信任判断被影响）。这是「同一个真值三处不同显示」的典型。
**修法方向**：会话头取 `conversation.title` 或最后一条消息的 `sender_name` 兜底；仅当全都没有时才显示短 hash，且 hash 应用可读缩写（如 `#457b`）而不是 32 位全 hex 前缀。定位建议从 `chat_view.py`/`session_view.py` 的 header 更新函数入手（阶段 2 用 `grep -n "未知设备"`）。
**验收断言**：见 §5 A7。

### G-06（P1）快速接收行大小显示 0 B

见 G-01 的 0 B 行：4 MiB 文件、真值 4194304 B，live 行「大小」列显示 `0 B`。512MiB 行因为传了 7s 被轮询回填所以显示正确 → 说明回填依赖轮询频率，短传输必错。
**修法**：建行时用 offer 事件里的 `files[].size` 回填（offer 已带 size），或收尾/进度事件里补 `size_bytes`。
**验收断言**：见 §5 A3。

### G-07（P2）传输中底部状态栏仍是上一笔的「传输完成」

证据：`shots/desktop-recv-active-row.png` 底部状态栏「传输完成: 512.00 MB」，而当时正在接收第二笔 512MiB（真值 active）。状态栏没有「正在接收」提示。
**影响**：误以为已经结束；最小改动是 active_transfers 非空时状态栏显示 `正在接收 2/1 个文件 · 320 MB/s`（300ms 刷新，避免每帧重排）。

### G-08/G-09（P2）状态词与小数位不统一

- 「完成」（live 行，main_window.py:886）vs「已完成」（历史行 transfers_view.py:959）vs 药丸「已完成」（transfers_view.py:60）。
- 进度小数：桌面 peer 行 `f"{...:.1f}%"`（transfers_view.py:260-263）vs 网页 `toFixed(0)`（app.js:1315）→ 同一传输两端显示 `62.5%` 与 `63%`。
**建议**：文案/格式统一走一个 display 词典（阶段 2 可落到 `core/model.py` 或 `desktop/i18n.py`），并加跨端 fixture 测试。

## 3. 三端一致性对照表

| 项目 | 桌面（接收表） | 网页（app.js） | 手机（Lead 证据） | 结论 |
|---|---|---|---|---|
| 接收有百分比进度条 | **无** | 有（`nodes.percentNode` toFixed(0)） | 有 | 不一致（G-02） |
| 接收速度 | **无** | 有（transfer.speed 渲染） | 有 | 不一致（G-02） |
| 接收 ETA | **无** | 有（transfer.eta） | 未核对 | 不一致（G-02） |
| 完成状态词 | 完成 / 已完成 两套 | 待核对（同一 /api/state 但文案来自 app.js） | 「已收到」/卡「接收中」残留 | 不一致（G-08） |
| 字节单位 | 1024 进制标 KB/MB | 1024 进制标 KB/MB | 1024 进制标 KB/MB | 同口径、标签错（G-04） |
| 进度小数位 | 1 位 | 0 位 | 未核对 | 不一致（G-09） |
| 会话里设备名 | 未知设备 #hash（页头） | 未核对 | 标题含设备号（Lead 证据） | 不一致（G-05） |

> 网页端结论来自 `web/assets/app.js:69-74、1315、3163-3164` 的渲染代码与 `/api/state` 同源确认；浏览器实页面渲染待补（本机无独立 chromium 可执行，Brave 面板未开 CDP；可用 `tools/cdp_shot.py` 若 Lead 的浏览器带 `--remote-debugging-port`）。

## 4. 检查矩阵进度

| 项目 | 状态 | 说明 |
|---|---|---|
| 0 B / 1 KiB / 4 MiB / 512 MiB 传输 | 512MiB×3、4MiB 已实测；0B/1KiB 待补 | 见 §0 |
| 超大文件（稀疏 64GiB 只测计算） | 未测 | 待补：`human_bytes` 在 >TB 前不会错，但 4GiB+ 进度百分比/精度待验 |
| 空状态 | 已截图（`desktop-empty-dark.png`） | 「还没有发现设备」文案正常；无传输时接收表空态待补 |
| 失败状态（校验失败/取消/磁盘满） | 未测 | 待补：断连中断 + 取消；验证失败原因文案与重试入口 |
| 断线/重启恢复 | 未测 | 待补：接收中 kill 对端/重启 A，检查记录是否残留「传输中」 |
| DPI 1.0/1.25/1.5/2.0 | 未测 | 待补：`QT_SCALE_FACTOR=1.25/1.5/2` 起 GUI 截图，检查截断 |
| 深色/浅色主题 | 深色已测；浅色待补 | 待补：`EVERSEND_THEME=light` |
| 窄窗/最小尺寸 | 未测 | 待补：wmctrl 缩到 900x600，检查表头/状态栏截断 |
| 1000 条传输记录滚动 | 未测 | 待补：构造 1000 行（`add_history_row` 批量）观察刷新/滚动耗时 |
| 既有 GUI 测试基线 | 未跑 | 待补：`python3 tests/test_gui_render.py`、`python3 tests/test_gui_actions.py`（用自建 home/端口，见 §6） |

## 5. 显示准确性验收断言建议（可直接落成测试）

| 编号 | 断言 |
|---|---|
| A1 | 一次接收完成后，接收表记录数 == 1（不是 2），行状态 == 「已完成」 |
| A2 | 接收完成后 live 行不残留：不存在「完成」与「已完成」并存的同名同来源两行 |
| A3 | 接收 4 MiB 文件完成后，行「大小」字符串解析 == 4.00 MiB（按 §5 单位口径），不是 0 B |
| A4 | 接收进行中，接收表的可见文本包含「%」与「/s」与「剩余」（或 API-visible row text），且百分比随 `/api/state` 的 percent 在刷新周期内单调不减 |
| A5 | bytes 达到 total 后到 transfer_finished 之间，UI 状态必须是「校验中/收尾中」之一，且速度显示不是衰减中的旧值（显示「—」或冻结在最后一帧） |
| A6 | `human_bytes(4*1024**2) == "4.00 MiB"`、`human_bytes(512*1024**2) == "512.00 MiB"`、`human_speed(125*1024**2)=="125.00 MiB/s"`（或按 10 进制口径另一套常量），三端同一 fixture |
| A7 | 同一 peer 的会话页头名称 == 设备列表名称 == 消息卡片署名（用 /api/state 的 name 当 oracle） |
| A8 | 传输进行中，桌面底部状态栏出现「正在接收/正在发送」，结束后出现「传输完成：<正确大小>」 |
| A9 | 跨端一致性：对同一 `/api/state` 快照，桌面/网页对同一 transfer 的 percent 文本差 <= 1 个百分位；单位与小数位由同一 fixture 断言 |
| A10 | 失败状态：注入校验失败后，行状态中文文案来自 `error_text`，不出现英文技术串（宪章 E3） |

## 6. 阶段 2 修复范围建议（等 Lead 建任务）

1. `src/eversend/desktop/main_window.py`（G-01/G-05/G-06/G-07）：方向规范、收尾合并、size 回填、状态栏；
2. `src/eversend/desktop/transfers_view.py`（G-02/G-08/G-09）：接收表列/进度渲染、状态词统一；
3. `src/eversend/core/model.py` + `web/assets/app.js` + 安卓 Kotlin（G-04）：单位口径统一（跨专家，需 Lead 协调）；
4. `src/eversend/core/transfer.py`/`engine.py`（G-03）：收尾阶段 phase 状态（transport-core 配合）；
5. 新测试 `tests/test_display_accuracy.py`（A1–A10）。

## 7. 附录：关键真值数据位置

- 512MiB 接收真值序列：`eversend/.testscratch/gui-desktop/truth/series-512m2.jsonl`（212 帧，含 shot 路径与每帧 `/api/state`）
- 首次 512MiB：`truth/series-512m.jsonl`（187 帧）；4MiB：`truth/series-4m.jsonl`
- SSE 事件：`truth/events-512m3.jsonl`（hello/announced/peer_connected/transfer_accepted/transfer_started/file_started）
- 发送端 CLI 输出：`truth/sender-512m3.log`

## 6b. 追加证据：进程内精确文本探针（offscreen，不读像素）

第一阶段后半程新增了第二种「界面真值」采集法，避免截图 OCR 误差：

- 脚本：`eversend/.testscratch/gui-desktop/desktop_probe.py`（offscreen Qt，不依赖 X server）
- 做法：真 `Engine`（接收机）+ 真 `Engine`（发送机）在同一进程内做**真实 EVSD 传输**；
  每 100ms 同时取 (a) 引擎真值 `active_transfers()` 的 percent/bytes/total/speed/eta；
  (b) 接收表模型每行的**精确显示字符串** `TransferModel.column_text(item, col)` ——
  这就是 delegate 实际绘制的文本，等于用户看到的文字。
- 产物：`.testscratch/gui-desktop/probe/transfer-64mb.json`（含每帧真值+显示文本+截图）、
  `probe/{edge,restart,fail,bulk,layout}.log`、`probe/*.png`（offscreen `window.grab()`）。

### G-10（P0）每次接收文件，Qt slot 抛 UnboundLocalError（会话卡片/名称补不上）

**证据**（`probe/edge.log`，真实 0B 传输完成后）：

```
Traceback (most recent call last):
  File "src/eversend/desktop/main_window.py", line 906, in _on_file_done
  File "src/eversend/desktop/main_window.py", line 2950, in _add_history
  File "src/eversend/desktop/main_window.py", line 3150, in _announce_received_file
UnboundLocalError: cannot access local variable 'other_member' where it is not associated with a value
```

**根因**：`_announce_received_file`（main_window.py:3123-3162）里 `other_member` 只在
`if not conv_id:` 分支内赋值；当 `_conversation_for_peer(found)` 直接返回了已有会话 id 时，
`other_member` 未定义 → 3150 行 `if not other_member:` 抛异常。PySide6 会打印 traceback
并**吞掉** slot 异常，所以用户看不到报错，只表现为：会话里没有这条文件卡片、会话头名称
一直是「未知设备 #hash」（= G-05 的直接根因之一）。
**修法**（1 行）：函数开头 `other_member = ""`，并让 `if not other_member:` 的兜底逻辑保留。
**验收**：接收文件后 chat DB 中应有该文件的 `mediaName` 消息，且控制台无 traceback（A11）。

### G-11（P1）接收「大小」列显示「已收字节」而非文件大小

**证据**（`probe/transfer.log`，真实 64 MiB 接收；真值文件大小恒为 67108864 B=64.00 MiB）：

| 时刻 | 真值 percent | 接收表「大小」列显示 | 应为 |
|---|---|---|---|
| 0.21s | 17.19% | `0 B` | 64.00 MiB |
| 1.20s | 96.88% | `60.00 MB` | 64.00 MiB |
| 1.58s | 100% | `64.00 MB` | 64.00 MiB |

**根因**：`TransferRow.update_progress`（transfers_view.py:1453-1466）对非 send 方向写
`item.size_text = human_bytes(done)`（已收字节），而「大小」列语义是文件大小；只有
send 方向写了 `done / total`。修法：接收方向 `size_text = human_bytes(total)`，
进度/速度走新加的独立列（G-B）。
**验收**：接收过程中「大小」列文本不随时间变化且等于总大小（A12）。

### G-12（P1）接收传输记录不跨重启

**证据**：`probe/restart.log`（同一引擎、销毁并重建 MainWindow，即真实「重启后重新加载界面」路径）：

```
before restart rows: 2      ← f4m 重复行 bug 也复现在此
after restart rows: 0
```

代码侧：接收行只有 `add_history_row` 一个创建入口（唯一调用 main_window.py:2941，
由 `file_done` 事件触发），启动路径没有从磁盘/聊天库恢复接收行的逻辑
（`grep recv_table src/eversend/desktop/main_window.py` 只有事件/渲染相关行）。
→ 重启后「传输 → 接收」为空，只有会话里的文件卡片（chat DB 持久化）还在。
**影响**：用户重启后无法从传输页找回刚才收到的文件；与「聊天记录持久化」不一致。
**修法建议**：启动时用 `WebUI.list_files`/`receive_dir` 扫描 + chat DB 的 `mediaName`
重建最近 N 行（或落一份 `received.jsonl` 记录），复用 `add_history_row`。
**验收**：接收 1 个文件 → 完全退出进程 → 重启 → 接收表仍有 1 行且可预览（A13）。

### 其余探针结果（矩阵补充）

| 项目 | 结果 | 证据 |
|---|---|---|
| 0 B / 1 KiB 接收 | 均成功；0B 显示「0 B」正确；1KiB 显示「1.00 KB」（= G-04 单位）；**两者都各出现两行**（G-01） | `probe/edge.log`、`shots/desktop-edge-0b-1k.png` |
| 失败态显示 | 注入 checksum_failed 后：行「失败」+「重试」，状态栏「传输失败：校验失败，文件可能在路上损坏了（checksum_failed）」= 正确（中文原因、无英文技术串） | `probe/fail.log`、`shots/desktop-fail-state.png` |
| 1000 条记录 | 插入 1000 行耗时 0.519s；20×50ms pump 1.048s（无卡顿）；滚动条往返 0.408s；可见 1000 行 | `probe/bulk.log`、`shots/desktop-bulk-1000.png` |
| 窄窗 900x600 | QLabel 文本无截断（clipped labels=0）；表格列宽自适应 | `probe/layout.log`、`shots/desktop-narrow-900x600.png` |
| 深/浅色主题 | 深色已测；浅色待补 | — |
| DPI 1.25/1.5/2.0 | 未测（offscreen 可用 `QT_SCALE_FACTOR` 跑同一探针） | 待补 |
| 既有测试基线 | `tests/test_gui_render.py`：**PASS（RENDER_EXIT=0，10 张截图）**；`tests/test_gui_actions.py`：待跑完记录 | `.testscratch/gui-desktop/test-logs/test_gui_render.log` |

## 6c. 阶段 2 修复判据（改前 / 改后）

| 编号 | 改前（实测） | 改后判据（必须可自动化） |
|---|---|---|
| G-A 重复行 | 每次接收 = 2 行（`完成/移除` + `已完成/预览`），其中 live 行常显示 0 B | 接收 1 个文件后接收表行数 == 1；该行状态「已完成」、操作含「预览」；`_transfers` 中 live 行已 hidden |
| G-B 接收进度 | 接收表 6 列，无百分比/速度/ETA；进行中只有「传输中」 | RECV_COLUMNS == (文件,来自,大小,进度,速度,剩余,状态,操作)；接收中「进度」文本含 % 且与真值 percent 误差 ≤1（同帧对齐）；「速度」在 >0 时显示 `x.xx MiB/s`；100% 后「剩余」为空 |
| G-C 100% 卡状态 | 100% 后仍「传输中」1.5~7.6s（64MiB:3.5s；512MiB:7.6s） | bytes==total 且仍 active 时状态显示「校验中…」，速度列显示「—」；事件 `transfer_finished` 后状态为「已完成」 |
| G-D 单位 | 512MiB→「512.00 MB」；1KiB→「1.00 KB」 | `human_bytes(536870912)=="512.00 MiB"`，`human_bytes(1024)=="1.00 KiB"`，`human_bytes(0)=="0 B"`；三端同 fixture（网页 app.js 同步） |
| G-E/G-10 会话名 | 页头「未知设备 #457b1f5c」，且每次接收抛 UnboundLocalError | 接收后控制台无 traceback；页头名称 == /api/state 中 peer.name（真实传输断言） |
| G-11 大小列 | 接收中大小列 0 B→60MB→64MB 变化 | 接收过程中「大小」列 == 文件总大小且不变化 |
| G-12 重启记录 | 重启后接收表 0 行 | 接收 1 文件→重启→接收表 1 行 |
| 回归 | — | `tests/test_gui_render.py` 与 `tests/test_gui_actions.py` 保持 PASS；新 `tests/test_display_accuracy.py` 覆盖上表 |

---

（第二版：2026-10-17 23:25，追加进程内精确文本探针结果 G-10/G-11/G-12、修复判据 §6c。§4 中 DPI/浅色/actions 基线仍在补。）
