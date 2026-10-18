# 企业级验收报告 · smoke-e6

生成时间：2026-09-25 19:13:29　·　HEAD `c3245e6`　·　变更文件 143 个（仓库处于写入中）　·　load average `[4.04, 7.29, 7.81]` / 8 核

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
| E6 | 交付质量：README 里每一条截图引用都能打开 | ❌ FAIL | E6.1 README 引用的 42 张截图都在磁盘上、E6.3 README 里所有本地图片路径都能解析 |

## 失败明细

### E6 交付质量：README 里每一条截图引用都能打开
- **E6.1 README 引用的 42 张截图都在磁盘上**
  - 期望：`0 张缺失`
  - 实际：`缺失 13 张：['web-composer-tiles.png', 'web-emoji-pad-390.png', 'web-emoji-pad.png', 'web-file-context-menu.png', 'web-offline-clear.png', 'web-one-device-one-row.png', 'web-phone-390.png', 'web-preview-image.png', 'web-preview-text.png', 'web-sections-collapsed.png', 'web-sender-name.png', 'web-sidebar-session.png', 'web-transfers.png']`
  - 说明：交付前必查；完整对照见 tests/reports/readme_screenshots_audit.md
- **E6.3 README 里所有本地图片路径都能解析**
  - 期望：`[]`
  - 实际：`['docs/screenshots/web-sidebar-session.png', 'docs/screenshots/web-transfers.png', 'docs/screenshots/web-phone-390.png', 'docs/screenshots/web-composer-tiles.png', 'docs/screenshots/web-preview-text.png', 'docs/screenshots/web-preview-image.png', 'docs/screenshots/web-sections-collapsed.png', 'docs/screenshots/web-emoji-pad.png', 'docs/screenshots/web-emoji-pad-390.png', 'docs/screenshots/web-file-context-menu.png', 'docs/screenshots/web-sender-name.png', 'docs/screenshots/web-offline-clear.png', 'docs/screenshots/web-one-device-one-row.png']`
