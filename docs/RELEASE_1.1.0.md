# 韧传 EverSend v1.1.0 发布说明

> 发布日期：2026-10-19　·　基线：v1.0.0（2026-09-22）
> 主题：**传输稳定性 / 速度 / GUI 显示准确性** 的系统性优化（opt-1017），全程 MI 9 真机 + Linux 真机真系统验证。

## 一句话

大文件传输「断了能续传」从桌面端延伸到手机端和跨进程重启；传输内核每字节 CPU 成本下降约 10%；
手机端下载进度/完成状态不再「永远接收中」，桌面与网页接收表的进度、速度、剩余时间也和真实传输一致。

## 稳定性

- **手机 ↔ 电脑大文件断点续传（新）**：`/api/upload` 支持 `uploadId`/`offset`/`X-EverSend-Total`，
  返回 202/409/status 三段语义；收满才落盘，偏移不符回 409 并按服务端 offset 重发；完成幂等重放不重复落盘。
  完成快照**落盘持久化**：电脑端进程重启后重放最后一段仍回原响应，不会多出一个 `(1)` 文件。
- **安卓 P0 修复**：令牌刷新移出主线程；每次（重）连接与上传前主动刷新；`Broken pipe` 等 0 字节失败
  也会换令牌重试；前台服务启动即保活（`specialUse` + WifiLock/WakeLock + 真实心跳）。
  真机 60s 熄屏实验：`sendto EPERM` 从 **256 条/轮 → 0**，电脑端全程看到手机在线。
- **下载续传**：手机端 Range 续传 + `.part` 缓存，失败保留断点供重试；重试 6 次、退避至 8s。
- **分享表持久化**：电脑端重启后手机仍能按原分享 id 续传（不再 404）。
- 真机全量回归 **A0/A1/A2/A6/A7/A8 多轮 6/6 PASS**（含 100 MiB 双向 sha256、中文+emoji、界面反作弊）。

## 速度

- 传输内核每字节 CPU 成本下降：A/B（同机同脚本、5 轮中位）**CPU s/MiB 平均 −9.7%，吞吐 +4.5%**。
- AEAD 加解密去掉一次全量内存拷贝（4 MiB seal 4 MiB 1.45→3.12 GiB/s）。
- `fsync/fdatasync` 移出文件锁（慢盘注入实验 p99 **11.8s → 0.70ms**）；加密会话接收端不再重复 crc32；
  每块 O(n) 扫描改 dict 索引（**80.8ms → 0.13ms**）；socket 缓冲区按内核真实值自检。
- 跨架构兼容：HELLO 增加可选 `cipher` 协商（旧对端忽略未知字段、不升协议版本），
  消除 x86 ↔ 32 位 ARM 之间 100% 校验失败的隐患。
- 说明：手机↔电脑的 WiFi 链路吞吐受 WiFi/落盘限制（≈23 MiB/s），本版**不声称提升该链路吞吐**，
  它的收益在稳定性（续传/保活/令牌自愈）。

## GUI 显示准确性

- **桌面 / 网页**：接收完成不再出现重复两行；接收表新增「进度 / 速度 / 剩余」列；
  100% 后显示「校验中…」而不是继续"传输中"；单位统一 KiB/MiB/GiB；页头设备名不再出现"未知设备 #hash"；
  修复每次接收抛 `UnboundLocalError` 的问题；接收记录跨重启恢复。
- **手机 App**：下载中显示「接收中 42% + 进度条 + 速度 + 剩余」；批量卡片显示「正在接收…」；
  完成后「✅ 已保存到手机」、按钮变「已保存」；会话标题不再泄露内部设备 id。
  根因是下载队列 `publish()` 被写成 update-only（新任务从未进入 UI 状态列表），已修复。
- **Linux 桌面启动项（新）**：`python3 tools/install_desktop_entry.py` 一键安装 `.desktop` + 多尺寸图标
  + 桌面快捷方式（图标由程序自身绘制代码渲染，不含二进制资源；数据目录固定到 `~/.local/share/eversend`）。

## 兼容与升级

- EVSD 桌面协议：`cipher` 为可选字段，**与 v1.0.0 双向兼容**。
- 手机 HTTP 上传：旧客户端不带 `uploadId` 自动走原有一次性路径，行为不变；
  新客户端先查 `/api/state.caps.uploadResume`，旧服务端自动退回一次性上传。
- 桌面数据目录无需迁移：`chat.db` 新增 `share_id` 列在启动时自动补齐，历史记录保留。
- Android：`versionCode=2` / `versionName=1.1.0`；同签名覆盖安装**保留数据**。

## 已知限制

- macOS / Windows 界面的真机观感仍未验证（CI 覆盖内核与互传）。
- 手机端「批量正在接收」状态本轮以真机截图验证；同名文件反复重发时历史卡片的极端串台已从
  "写时保存 shareId"层面根除，但旧历史消息（无 shareId）仍走兼容匹配。
- `netem` 高 RTT 下的窗口自适应尚未做实测驱动优化；桌面部分进阶状态（状态栏"正在接收"、
  引擎显式 `verifying` 阶段）与网页实页/DPI/浅色截图未做。
- Android 仍为 **debug 签名**，正式分发请用自己的密钥重新签名（见 README「安卓 App」一节）。

## 验证与证据

全量报告在仓库 `tests/reports/opt-1017/`：

- 总报告：`FINAL_REPORT.md`（含根因、修复、逐项复验、性能 A/B、验收汇总）
- 性能 A/B：`bench-before-after.md` / `bench-baseline.md`
- 真机：`real_device_v11-final.md`、`real_device_v11-chatlink.md`、`real_device_a8-recheck.md`
- 手机 GUI 探针：`gui-probe-after-v11b.md`、`pg3-batch/v11f-1.png`（PC 真值时间线 × 手机截图）
- 全量验收：`acceptance-final2.md` / `acceptance-final3.md` / `acceptance-final4.md` + 复验报告

## 构建 / 获取

- 绿色便携包（Linux/Windows）构建方式见 `docs/PACKAGING.md`：
  `python3 tools/build_green.py --platform linux --out tools/.build`。
- Android APK：`cd android && ./gradlew assembleDebug`（CI 也会在每次 push 到 main 时构建并归档产物）。
