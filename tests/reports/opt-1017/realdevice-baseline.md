# 真机基线报告 · MI 9 × Linux · APK↔电脑（opt-1017 阶段 1）

生成：2026-10-17 22:35　·　仓库基线 `e18c079`（本次优化快照）　·　Lead 亲测
判定立场：**界面说什么不算证据**，只有手机磁盘/电脑磁盘 sha256、服务端字节计数、系统 API 真值算。

## 0. 现场

| 项 | 值 |
|---|---|
| 手机 | MI 9（cepheus），Android 16 / API 36，arm64-v8a，1080x2340 |
| 手机网络 | WiFi `192.168.110.158/24`；`svc power stayon true` + `stay_on_while_plugged_in=7` |
| 电脑 | Linux 7.2.8-1-cachyos x86_64，WiFi `192.168.110.41/24`，TCP 52117 / UDP 52118 / TCP 52119 |
| APK | 2026-09-26 构建的 v1.0.0（arm64），`adb install -r` 装入；包名 `cn.eversend.app` |
| 电脑端 | 源码直跑 `PYTHONPATH=src python3 -m eversend`（Xvfb :99, QT_QPA_PLATFORM=xcb），真 Qt 窗口 + 真 Web API |
| 真机驱动 | `tests/test_real_device.py`（已适配本机 adb：`EVERSEND_ADB`/shutil.which，见 commit 变更 4 个文件） |
| 背景负载 | 1 分钟 load ≈ 4.6–8.9（4 名专家正在同机跑 benchmark）——**速度数字受此影响，偏保守** |

复现命令：

```bash
cd eversend
EVERSEND_ADB_SERIAL=2430d3c EVERSEND_REAL_WORK=/home/xgl/qa-real \
  DISPLAY=:99 QT_QPA_PLATFORM=xcb python3 tests/test_real_device.py --only A1,A2,A6 --label mi9-clean1
EVERSEND_ADB_SERIAL=2430d3c EVERSEND_REAL_WORK=/home/xgl/qa-real \
  DISPLAY=:99 QT_QPA_PLATFORM=xcb python3 tests/test_real_device.py --only A7,A8 --label mi9-clean2
```

## 1. 真机用例结果（改前基线）

| 用例 | 内容 | 结果 | 证据 |
|---|---|---|---|
| A0 | 现场：真机在线、端口独占、目录就绪 | ✅ PASS | `real_device_1017-2223.md` |
| A1 | 电脑→手机 512 KiB，手机磁盘 sha256 相等 | ✅ PASS | `real_device_mi9-clean1.md` |
| A2 | 电脑→手机 100 MiB，手机磁盘 sha256 相等 | ✅ PASS | 同上 |
| A6 | 反作弊：界面说「已收到」时手机磁盘必须真有且非 0 字节 | ✅ PASS | 同上 |
| A7 | 手机→电脑 370 KiB PNG（真 DocumentsUI 选文件→发送→电脑磁盘 sha256） | ❌ **FAIL** | `real_device_mi9-clean2.md` |
| A8 | 双向中文 + emoji 消息 | ✅ PASS | 同上 |

**A7 失败原因（P0 稳定性 bug，已定位到行）**：手机端「发送」POST `/api/upload` 时用的是旧 CSRF 令牌（电脑端每次重启换令牌），
`MainActivity.kt:504-530` 的自动换令牌路径把 `api.refreshToken()` 放在**主线程**跑 →
`NetworkOnMainThreadException` → 自动恢复永远不生效。真机日志（22:28:12）：

```
W EverSend: 发送失败（准备换令牌）: 缺少或错误的防跨站令牌，请刷新页面
W EverSend: refreshToken 失败: NetworkOnMainThreadException: null
（电脑接收目录为空 → A7.g FAIL）
```

对比：A7 的 UI 驱动链路（点「📎传输文件」→ DocumentsUI → 图片分类 → 选中 → 待发区 → 勾设备 → 发送）**全部成功**，
唯独最后的 HTTP 上传因令牌 403 失败。A8 聊天能过说明令牌在其它异步路径可被刷新，但「发文件」这条路的自动恢复是坏的。

## 2. 速度基线（电脑→手机，A2 100 MiB）

测法：独立采样器每 0.5s 读电脑端 `/api/state` 的 share `sentBytes`（服务端真实发出的字节），
与手机侧最终 sha256 对照；采样原始数据 `/home/xgl/qa-real/a2-speed-sample.log`。

| 时刻(epoch) | sentBytes | 说明 |
|---|---|---|
| 1792247234.9–236.5 | 0 | 手机点「保存」前（约 2.6 s 启动延迟：MediaStore 建 pending 文件 + 建连） |
| 1792247237.07 | 786 432 | 首字节 |
| 1792247237.62 | 14 680 064 | |
| 1792247238.18 | 27 525 120 | |
| 1792247239.24 | 55 836 672 | |
| 1792247240.32 | 84 148 224 | |
| 1792247241.42 | 104 857 600 | 完成（手机磁盘 sha256 一致） |

- **有效传输窗口 4.34 s，平均 ≈ 23.0 MiB/s**（≈ 24.1 MB/s）；稳态分档 **23.8–26.9 MiB/s**，末段收尾 ~10 MiB/s。
- 手机点「保存」→ 首字节 **≈ 2.6 s**（可优化，见 §4）。完成 → 界面状态更新见 §3 的 bug。
- 负载 8.9 下测得，空闲时应更高；阶段 3 复测需在安静环境跑 3 次取中位数。

**手机→电脑速度：基线缺失**——A7 被 P0 令牌 bug 阻断，无法形成有效速度基线（阶段 2 修复后用同法采样上传 spool 增长补测）。

## 3. GUI 显示准确性真值对照（手机端，截图 `qa-real/shots/`）

| # | 现象 | 真值 | 判定 |
|---|---|---|---|
| G1 | A2 完成（手机磁盘已有 100 MiB 且 sha256 相等）后，文件卡片仍显示「接收中…」、操作仍写「点击保存到手机」 | `download=True`、sentBytes=104857600 | ❌ 状态残留/说谎（`/home/xgl/qa-real/shots/A2-phone.png`） |
| G2 | 大小写「512.0 KB」「100.0 MB」，实际 524288/104857600 字节（KiB/MiB 口径被标成 KB/MB） | 524288 / 104857600 | ⚠️ 单位口径不准确，三端需统一 |
| G3 | 会话标题行「QA-真机验收 与 00254c4971c16f15c4bcd7982900f3cb…」把内部设备号摊在标题，右侧另有一个「← 会话」 | 应由设备名/角色构成，不露内部 id | ⚠️ 疑似信息架构/截断问题 |
| G4 | 首屏提示长文本右边缘被裁：「…③ 电脑防火墙允许 52119/」 | 应完整可读/自动换行 | ❌ 文本截断（`/tmp/phone-7.png`） |
| G5 | 电脑端传输结束截图中底部状态「手机已经取走「A2-中文件-104857600.bin」。」正确，但**传输中的进度/速度/ETA 中间态未捕获** | 待阶段 2 用 512 MiB 慢速传输连续采样 | ⚠️ 未测 |

## 4. 稳定性问题清单（本阶段实测）

| # | 严重度 | 现象 | 证据 | 归属 |
|---|---|---|---|---|
| S1 | **P0** | PC 重启后手机「发文件」必失败（CSRF 令牌过期 + 刷新在主线程崩） | 22:28:12 logcat；`MainActivity.kt:504-530` | android |
| S2 | **P0** | App 的 LinkService 前台服务根本没起来：`dumpsys activity services cn.eversend.app` 为空；熄屏后系统对 App 发 UDP 全部 EPERM（DreamManager/InetDiag 日志同刻）→ 熄屏=断发现/断流 | android-device 归档 logcat + dumpsys | android |
| S3 | P1 | 发现通道每轮公告对 /24 发 254 个 UDP + 每 20s 全 /24 TCP 扫描；EPERM 时每包一条日志并刷新 UI → 日志洪泛、耗电、失败可见 | logcat 2777 条 EPERM；`Discovery.kt` | android |
| S4 | P1 | 100 MiB 下载进行中，手机被切到另一个前台界面（files picker）后下载在 73 MiB 处 `Software caused connection abort` 并回滚（一次污染实验，但场景真实：传输中切 App） | 22:25:27 logcat；A2 首次运行记录 | android/transport（待受控复现） |
| S5 | P1 | 手机主动 TCP 扫描一次「扫了 266 台主机，没有应答」而电脑在线（组播能发现）；扫描目标/线程/超时需查 | 22:19:44 logcat | android |
| S6 | P2 | 接收目录重名时落地为 `A1-…(1).bin` 属正常防覆盖，但手机端卡片仍指向原文件名，三端记录一致性需复核 | A1/A2 目录清单 | gui/web |

## 5. 本阶段结论

1. **电脑→手机链路可用且速度健康**：100 MiB 4.34 s（≈23 MiB/s，带高负载）且 sha256 一致、记录状态机正确（取走前 sending、取走后 sent）。
2. **手机→电脑链路功能上可达，但被 P0 令牌 bug 阻断**——这是阶段 2 第一优先级（修复成本极小，收益是"每次电脑重启后手机发文件必失败"变可用）。
3. **熄屏/后台保活是系统级稳定性根因**：前台服务没起来，导致 MIUI/Doze 掐网。修复后需用受控熄屏实验验证（60 s 灭屏→亮屏恢复→传输继续）。
4. 显示准确性已有 4 条确定问题（G1/G2/G4 为硬伤），阶段 2 与 gui-desktop 的桌面/网页审计合并修。
5. 速度基线只在"高负载"下测得，阶段 3 需在安静环境复测 3 次；手机→电脑速度待 S1 修复后补。

## 6. 复现与证据索引

- 报告：`tests/reports/real_device_1017-2223.md`、`tests/reports/real_device_mi9-clean1.md`、`tests/reports/real_device_mi9-clean2.md`（+ 同名 .json）
- 截图：`/home/xgl/qa-real/shots/{A1,A2}-phone.png`、`{A1,A2}-desktop-progress.png`
- 速度采样：`/home/xgl/qa-real/a2-speed-sample.log`
- 手机日志：`adb logcat -d -s EverSend:V`（本次会话 2 万行已由 android-device 归档分析）
- 测试文件：`/home/xgl/qa-real/files/`；手机 `/sdcard/Download/`（A1/A2 文件已留作证据）
