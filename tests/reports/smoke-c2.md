# 企业级验收报告 · smoke-c2

生成时间：2026-09-25 19:05:56　·　HEAD `c3245e6`　·　变更文件 151 个（仓库处于写入中）　·　load average `[8.37, 8.05, 7.72]` / 8 核

> **时序断言的环境说明**：C13/C14 里有若干带时间预算的断言（≤5s 自动发现、≤1s bye 转离线、补投 ≤20s）。当 load average 接近或超过核数时，这些断言可能**假红**，判断时请对照上面的负载数字。

> **本次复跑时仓库处于写入中**：下面这些文件在跑之前刚被改过，某条 FAIL 可能是某个实现者的中间状态。

```
README.md
android/app/src/androidTest/java/cn/eversend/app/DiscoveryAndActionsTest.kt
android/app/src/androidTest/java/cn/eversend/app/EverSendInstrumentedTest.kt
android/app/src/main/AndroidManifest.xml
android/app/src/main/java/cn/eversend/app/ApiClient.kt
android/app/src/main/java/cn/eversend/app/Discovery.kt
android/app/src/main/java/cn/eversend/app/LinkService.kt
android/app/src/main/java/cn/eversend/app/MainActivity.kt
docs/ENTERPRISE_CHARTER.md
docs/PROTOCOL.md
docs/UI_LAYOUT.md
docs/screenshots/01-send.png
docs/screenshots/01-session.png
docs/screenshots/02-transfers.png
docs/screenshots/03-receive.png
docs/screenshots/04-settings.png
docs/screenshots/06-chat.png
docs/screenshots/07-preview.png
docs/screenshots/08-preview-image.png
docs/screenshots/09-emoji-and-sections.png
docs/screenshots/10-offline-devices.png
docs/screenshots/11-sections-collapsed.png
docs/screenshots/12-emoji-in-bubble.png
docs/screenshots/13-one-row-per-file.png
docs/screenshots/14-transfer-record-in-chat.png
docs/screenshots/15-settings-maintenance.png
docs/screenshots/16-one-phone-one-row.png
docs/screenshots/android-app-chat.png
docs/screenshots/android-app-clear-offline-empty.png
docs/screenshots/android-app-clear-offline.png
docs/screenshots/android-app-composer-tiles.png
docs/screenshots/android-app-conversation.png
docs/screenshots/android-app-device-sections-offline.png
docs/screenshots/android-app-device-sections.png
docs/screenshots/android-app-emoji-message.png
docs/screenshots/android-app-emoji-pad.png
docs/screenshots/android-app-file-actions.png
docs/screenshots/android-app-image-viewer.png
docs/screenshots/android-app-one-device-one-row.png
docs/screenshots/android-app-one-row-per-file.png
```

| # | 检查项 | 结论 | 失败的子断言 |
|---|---|---|---|
| C2 | 排版：间距 4 的倍数、字号取类型刻度、圆角来自 token | ❌ FAIL | C2.1a 运行期布局 margin 是 4 的倍数（检查 92 个布局，违规 3 处）、C2.1b 运行期布局 spacing 是 4 的倍数（检查 92 个布局，违规 1 处）、C2.1c 源码里的间距字面量都在刻度上（检查 137 个字面量，违规 12 处） |

## 失败明细

### C2 排版：间距 4 的倍数、字号取类型刻度、圆角来自 token
- **C2.1a 运行期布局 margin 是 4 的倍数（检查 92 个布局，违规 3 处）**
  - 期望：`落在 4 的倍数（0/1 发丝线合规；Qt 样式默认 [6, 9, 11] 不算应用选择）`
  - 实际：`Top8：['SegmentedControl#Segmented margins=(2, 2, 2, 2)', 'QWidget#- margins=(16, 14, 16, 14)', 'QWidget#- margins=(2, 2, 2, 2)']`
- **C2.1b 运行期布局 spacing 是 4 的倍数（检查 92 个布局，违规 1 处）**
  - 期望：`落在 4 的倍数（Qt 样式默认 [6, 9, 11] 不算应用选择）`
  - 实际：`Top8：['SegmentedControl#Segmented spacing=2']`
- **C2.1c 源码里的间距字面量都在刻度上（检查 137 个字面量，违规 12 处）**
  - 期望：`4 的倍数（0/1 合规）`
  - 实际：`Top10：['chat_view.py:114 setSpacing(6) → [6]', 'chat_view.py:122 setContentsMargins(2, 2, 2, 2) → [2, 2, 2, 2]', 'chat_view.py:150 setSpacing(2) → [2]', 'chat_view.py:346 setContentsMargins(16, 14, 16, 14) → [14, 14]', 'chat_view.py:921 setSpacing(6) → [6]', 'sidebar.py:161 setSpacing(theme.SPACE["sm"] - 2) → [2]', 'sidebar.py:387 setSpacing(theme.SPACE["sm"] - 2) → [2]', 'widgets.py:108 setContentsMargins(2, 2, 2, 2) → [2, 2, 2, 2]', 'widgets.py:109 setSpacing(2) → [2]', 'widgets.py:581 setSpacing(2) → [2]']`
