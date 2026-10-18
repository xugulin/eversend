# README 截图引用 vs 磁盘实况（交付前必查）

更新：2026-09-25 20:07:22

- `README.md` 引用 `docs/screenshots/*.png` 共 **40** 张
- 磁盘上实际有 **58** 张
- **引用了但文件不存在：0 张**（✅ 全部可解析）
- 存在但未被 README 引用：18 张

> QA 断言：`tests/test_enterprise_acceptance.py --only E6`。
> 「README 图片引用必须全部可解析」已写进宪章 §8 / E6。

## ① 引用了但磁盘上没有

**无。** 上一版这里是 13 张 `web-*.png` 坏图（web-ui 改名后 README 没跟），Lead 已全部修好。

## ② 磁盘上有但 README 没引用

| 文件 | 类别 | mtime |
|---|---|---|
| `07-preview.png` | 桌面 | 09-25 19:28 |
| `08-preview-image.png` | 桌面 | 09-25 19:28 |
| `12-transfers-failed.png` | 桌面 | 09-25 19:28 |
| `13-settings-top.png` | 桌面 | 09-25 19:28 |
| `android-app-auto-discovery-drawer.png` | 安卓 | 09-25 19:27 |
| `android-app-auto-discovery.png` | 安卓 | 09-25 19:27 |
| `android-app-clear-offline-empty.png` | 安卓 | 09-24 23:49 |
| `android-app-device-sections-offline.png` | 安卓 | 09-24 19:08 |
| `android-app-device-sections.png` | 安卓 | 09-24 19:06 |
| `android-app-emoji-message.png` | 安卓 | 09-24 20:29 |
| `android-app-preview-video.png` | 安卓 | 09-24 17:30 |
| `android-app-settings.png` | 安卓 | 09-24 16:16 |
| `android-app-transfer-row-actions.png` | 安卓 | 09-24 21:58 |
| `android-app-transfers-send.png` | 安卓 | 09-24 16:16 |
| `qa-web-mobile.png` | 网页版 | 09-25 19:10 |
| `qa-web-settings.png` | 网页版 | 09-25 19:10 |
| `qa-web-transfers.png` | 网页版 | 09-25 19:10 |
| `qa-web-wide.png` | 网页版 | 09-25 19:10 |

## ③ README 引用的桌面画廊实况

| 引用 | 磁盘 | mtime |
|---|---|---|
| `01-session.png` | ✅ | 09-25 19:27 |
| `02-transfers.png` | ✅ | 09-25 19:27 |
| `03-receive.png` | ✅ | 09-25 19:27 |
| `04-settings.png` | ✅ | 09-25 19:27 |
| `05-qr.png` | ✅ | 09-25 19:28 |
| `06-chat.png` | ✅ | 09-25 19:27 |
| `09-emoji-and-sections.png` | ✅ | 09-25 19:27 |
| `10-offline-devices.png` | ✅ | 09-25 19:27 |
| `11-sections-collapsed.png` | ✅ | 09-25 19:27 |
| `12-emoji-in-bubble.png` | ✅ | 09-25 19:27 |
| `13-one-row-per-file.png` | ✅ | 09-25 19:27 |
| `14-transfer-record-in-chat.png` | ✅ | 09-25 19:27 |
| `15-settings-maintenance.png` | ✅ | 09-25 19:28 |
| `16-one-phone-one-row.png` | ✅ | 09-25 19:28 |
