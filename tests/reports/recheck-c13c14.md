# 企业级验收报告 · recheck-c13c14

生成时间：2026-09-25 19:47:33　·　HEAD `c3245e6`　·　变更文件 156 个（仓库处于写入中）　·　load average `[3.99, 5.36, 8.8]` / 8 核

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
docs/screenshots/05-qr.png
docs/screenshots/06-chat.png
docs/screenshots/07-preview.png
docs/screenshots/08-preview-image.png
docs/screenshots/09-emoji-and-sections.png
docs/screenshots/10-offline-devices.png
docs/screenshots/11-sections-collapsed.png
docs/screenshots/12-emoji-in-bubble.png
docs/screenshots/12-transfers-failed.png
docs/screenshots/13-one-row-per-file.png
docs/screenshots/13-settings-top.png
docs/screenshots/14-transfer-record-in-chat.png
docs/screenshots/15-settings-maintenance.png
docs/screenshots/16-one-phone-one-row.png
docs/screenshots/android-app-auto-discovery-drawer.png
docs/screenshots/android-app-auto-discovery.png
docs/screenshots/android-app-chat.png
docs/screenshots/android-app-clear-offline-empty.png
docs/screenshots/android-app-clear-offline.png
docs/screenshots/android-app-composer-tiles.png
docs/screenshots/android-app-conversation.png
docs/screenshots/android-app-device-sections-offline.png
docs/screenshots/android-app-device-sections.png
docs/screenshots/android-app-emoji-message.png
```

| # | 检查项 | 结论 | 失败的子断言 |
|---|---|---|---|
| C13 | 自动发现：≤5s 零手动、扫描兜底、跨网段覆盖、bye 即时离线 | ✅ PASS | — |
| C14 | 消息必达：ACK、待重发队列、重连补投、失败原因码 | ❌ FAIL | C14.3a 重连后「必达-2-离线」最终到达对端（自动补投） |

## 失败明细

### C14 消息必达：ACK、待重发队列、重连补投、失败原因码
- **C14.3a 重连后「必达-2-离线」最终到达对端（自动补投）**
  - 期望：`20s 内 B 的存储里出现这条消息`
  - 实际：`['必达-1']`
  - 说明：§5.5：无 ACK 进重发队列（指数退避），重连后自动补投；高负载下这条可能假红
