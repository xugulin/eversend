# 企业级验收报告 · smoke-c1

生成时间：2026-09-25 18:48:22　·　HEAD `c3245e6`　·　变更文件 124 个（仓库处于写入中）

> **本次复跑时仓库处于写入中**：下面这些文件在跑之前刚被改过，某条 FAIL 可能是某个实现者的中间状态。

```
README.md
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
docs/screenshots/android-app-preview-text.png
```

| # | 检查项 | 结论 | 失败的子断言 |
|---|---|---|---|
| C1 | 控件去重（一个意图一个入口，三端一致） | ❌ FAIL | C1.7a 安卓主区不再有 [会话｜传输] 分段控件、C1.7d 安卓传输页顶部不是一串平级按钮（清空/刷新要进 ⋯） |

## 失败明细

### C1 控件去重（一个意图一个入口，三端一致）
- **C1.7a 安卓主区不再有 [会话｜传输] 分段控件**
  - 期望：`主区分段控件已删除（侧栏底部导航是唯一入口）`
  - 实际：`发现 listOf("chat" to "会话", "transfers" to "传输"),`
  - 说明：§3.1 与桌面端同一条处置
- **C1.7d 安卓传输页顶部不是一串平级按钮（清空/刷新要进 ⋯）**
  - 期望：`只剩「打开接收文件夹」一个平级动作`
  - 实际：`平级动作=['刷新', '打开接收文件夹', '清空会话', '清空记录']`
  - 说明：§3.1：传输页表头右侧三个平级按钮 → 主按钮 + ⋯
