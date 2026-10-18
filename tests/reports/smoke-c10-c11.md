# 企业级验收报告 · smoke-c10-c11

生成时间：2026-09-25 19:00:02　·　HEAD `c3245e6`　·　变更文件 136 个（仓库处于写入中）

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
docs/screenshots/android-app-preview-image.png
```

| # | 检查项 | 结论 | 失败的子断言 |
|---|---|---|---|
| C10 | 全中文：Qt 系统对话框不再出现英文文案 | ✅ PASS | — |
| C11 | 预览：类型矩阵、工具栏、图片适应窗口不放大、文本等宽、二进制不装 | ❌ FAIL | C11.8c →（右方向键）真的切到下一张 |

## 失败明细

### C11 预览：类型矩阵、工具栏、图片适应窗口不放大、文本等宽、二进制不装
- **C11.8c →（右方向键）真的切到下一张**
  - 期望：`从 /home/xgl/python/跨平台文件互传/eversend/.testscratch/qa-acc-_dvyz30t/c11-small.png 切走`
  - 实际：`/home/xgl/python/跨平台文件互传/eversend/.testscratch/qa-acc-_dvyz30t/c11-small.png`
