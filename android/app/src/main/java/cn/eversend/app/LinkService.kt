package cn.eversend.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import org.json.JSONObject

/**
 * 前台服务：这就是做原生 App 的理由。
 *
 * 手机浏览器页面在熄屏或切到后台时会被系统冻结，连接必然断；前台服务则可以在
 * 熄屏、锁屏、切后台时继续持有长连接（SSE），来了消息发通知。用户要的
 * "只有手动断开或彻底退出才断开"，在浏览器里做不到，在这里是默认行为。
 */
class LinkService : Service() {

    companion object {
        const val TAG = "EverSend"
        const val CHANNEL_ID = "eversend-link"
        const val NOTIFICATION_ID = 1001
        const val ACTION_START = "cn.eversend.app.START"
        const val ACTION_STOP = "cn.eversend.app.STOP"
        const val EXTRA_HOST = "host"

        /** 界面通过它拿到最新状态；服务只在进程内用，不做跨进程。 */
        @Volatile
        var lastState: String = "未启动"
            private set

        @Volatile
        var lastMessage: String = ""
            private set

        @Volatile
        var running: Boolean = false
            private set

        /** 心跳间隔：电脑端 15 秒没听到就算离线，所以 5 秒一次足够稳。 */
        const val HEARTBEAT_MS = 5000L

        /** 上一次成功收到电脑端回答的时间（毫秒）。界面用它说"多久没联系上"。 */
        @Volatile
        var lastSeenAt: Long = 0L
            private set

        /** 最近一次失败的原因，排查"为什么连不上"时比"未连接"有用得多。 */
        @Volatile
        var lastError: String = ""
            private set
    }

    private var streamUpNotified = false
    private var introduced = false

    private var worker: Thread? = null
    @Volatile private var stopRequested = false
    private var base: String = ""
    private var token: String = ""

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopRequested = true
                stopSelf()
                return START_NOT_STICKY
            }
            else -> {
                base = intent?.getStringExtra(EXTRA_HOST) ?: base
                if (base.isNotBlank()) start(base)
            }
        }
        return START_STICKY
    }

    private fun start(host: String) {
        startForegroundCompat()
        // 前台服务也把发现引擎拉起来：熄屏/切后台时电脑端仍然能"听到"这台手机
        // （每 15 秒一条公告），而手机这边也在继续听电脑的公告 —— 换 Wi-Fi、
        // 电脑换 IP 之后不用用户手动重连（C13 的"自动"要在这个场景下也成立）。
        try {
            LanDiscovery.get(this).start()
        } catch (problem: Exception) {
            Log.d(TAG, "后台发现引擎启动失败: ${problem.message}")
        }
        if (worker?.isAlive == true) return
        stopRequested = false
        running = true
        worker = Thread({ loop() }, "eversend-link").apply { isDaemon = true; start() }
    }

    /**
     * 保活循环：SSE 是主通道，**心跳兜底**是它没接上时的保险。
     *
     * 用户报"连上电脑后很快就掉线"，两个原因都在这里：
     * 1. 电脑端把"最近 15 秒内说过话"当作在线。只开一条 SSE 时，一旦这条流
     *    被路由器/省电策略掐掉，App 就不再发任何请求，电脑端 15 秒后就把这台
     *    手机标成"未连接"；
     * 2. SSE 断了以后原来的实现只是退避重连，中间没有任何请求 —— 界面就一直
     *    显示"重连中"。
     *
     * 现在：每隔 HEARTBEAT_MS 至少发一次 /api/state（在线状态就不会掉），
     * SSE 正常时它只是顺便的一次小请求；SSE 断了也照样维持在线并把事件流重连。
     */
    private fun loop() {
        var backoff = 1000L
        var lastBeat = 0L
        var streamUp = false
        while (!stopRequested) {
            val host = currentHost()
            try {
                if (base != host) {
                    // 地址变了（换了 Wi-Fi、用户重新连了别的电脑）：重新握手。
                    base = host
                    token = ""
                }
                if (base.isBlank()) {
                    lastState = "没填电脑地址"
                    Thread.sleep(2000)
                    continue
                }
                if (token.isBlank()) token = ApiClient.fetchToken(base)
                if (token.isBlank()) {
                    lastState = "连不上（拿不到令牌）"
                    Thread.sleep(3000)
                    continue
                }
                if (!introduced) {
                    // 自报身份：界面没打开过时（开机自启、只在后台跑），电脑端
                    // 也得知道这是哪台手机，否则列表里只有一串地址。
                    val store = AppState(this)
                    try {
                        ApiClient(base, token).postJson(
                            "/api/hello",
                            org.json.JSONObject()
                                .put("deviceId", store.deviceId)
                                .put("name", store.deviceName)
                                .put("kind", "android")
                                .put("version", "1.0.0"),
                        )
                        introduced = true
                        ApiClient.announce(store.deviceId, store.deviceName)
                    } catch (ignored: Exception) {
                    }
                }
                val now = System.currentTimeMillis()
                if (now - lastBeat > HEARTBEAT_MS) {
                    // 心跳：既维持"在线"，也是 SSE 挂掉时的替代通道。
                    val state = ApiClient(base, token).getJson("/api/state")
                    // 顺手把"电脑交给我的文件"收下来：熄屏/切后台时界面轮询停了，
                    // 这里是唯一还在跑的地方 —— 762 MB 的文件就该在口袋里下完。
                    offerShares(state)
                    lastBeat = now
                    lastSeenAt = now
                    if (!streamUp) {
                        lastState = "已连接"
                        updateNotification("已连接 · 熄屏也在")
                    }
                }
                streamUp = true
                if (!streamUpNotified) {
                    lastState = "已连接"
                    updateNotification("已连接 · 熄屏也在")
                    streamUpNotified = true
                }
                backoff = 1000L
                ApiClient(base, token).events(
                    onEvent = { event -> onEvent(event) },
                    isRunning = { !stopRequested },
                )
                streamUp = false
                streamUpNotified = false
                lastState = "重连中…"
            } catch (error: Exception) {
                streamUp = false
                streamUpNotified = false
                lastError = error.javaClass.simpleName + ": " + (error.message ?: "")
                lastState = "重连中…（${error.javaClass.simpleName}）"
                Log.d(TAG, "事件流断开: ${error.message}")
                // 退避期间也要维持在线：SSE 不通不代表这台手机不在。
                try {
                    val now = System.currentTimeMillis()
                    if (now - lastBeat > HEARTBEAT_MS) {
                        ApiClient(base, token).getJson("/api/state")
                        lastBeat = now
                        lastSeenAt = now
                        lastState = "已连接（事件流重连中）"
                    }
                } catch (ignored: Exception) {
                }
            }
            if (stopRequested) break
            updateNotification(lastState)
            try {
                Thread.sleep(minOf(backoff, 1000L))
            } catch (interrupted: InterruptedException) {
                break
            }
            backoff = (backoff * 2).coerceAtMost(15000L)
        }
        running = false
        lastState = "未连接"
        stopForegroundCompat()
    }

    /**
     * 后台收文件：电脑把文件发布在 `/api/state` 的 `shares` 里，手机来取
     * （真机实测：App 没有任何 LISTEN 端口，电脑推不过来，只能手机主动取）。
     *
     * 只做"排队 + 下载"，进度与落地都由 [FetchManager] 管；界面开着的时候两边
     * 读的是同一份队列，不会重复下载（它按 shareId 去重）。
     */
    private fun offerShares(state: JSONObject) {
        val shares = state.optJSONArray("shares") ?: return
        if (shares.length() == 0) return
        val pending = (0 until shares.length())
            .mapNotNull { shares.optJSONObject(it) }
            .filterNot { it.optBoolean("downloaded", false) }
        if (pending.isEmpty()) return
        val fetcher = FetchManager.get(this)
        if (!fetcher.autoFetch) return
        try {
            fetcher.offer(ApiClient(base, token), pending, auto = !fetcher.meteredNetwork())
        } catch (problem: Exception) {
            Log.d(TAG, "后台收文件失败: ${problem.message}")
        }
    }

    /** 服务里的地址以界面里存的那个为准（用户改地址、换网络都能跟上）。 */
    private fun currentHost(): String {
        val stored = AppState(this).host
        if (stored.isNotBlank()) {
            val normalized = if (stored.contains(":")) stored else "$stored:${ApiClient.DEFAULT_WEB_PORT}"
            return "http://$normalized/"
        }
        return base.ifBlank { "" }
    }

    private fun onEvent(event: JSONObject) {
        when (event.optString("kind")) {
            // 电脑一发布文件就推过来（web-ui 定的 SSE 契约）：**立刻开始收**，
            // 不必等下一次轮询。熄屏/切后台时，这条事件是唯一还能触发下载的路径。
            "share_added" -> {
                val name = event.optString("name")
                lastMessage = "电脑发来文件：$name"
                notify("📥 电脑发来文件", "$name · 正在接收")
                Thread({
                    try {
                        val api = ApiClient(base, token)
                        val payload = api.getJson("/api/shares")
                        val array = payload.optJSONArray("shares") ?: return@Thread
                        val pending = (0 until array.length())
                            .mapNotNull { array.optJSONObject(it) }
                            .filterNot { it.optBoolean("downloaded", false) }
                        if (pending.isNotEmpty()) {
                            val fetcher = FetchManager.get(this)
                            if (fetcher.autoFetch) {
                                fetcher.offer(api, pending, auto = !fetcher.meteredNetwork())
                            }
                        }
                    } catch (problem: Exception) {
                        Log.d(TAG, "share_added 处理失败: ${problem.message}")
                    }
                }, "eversend-fetch-share").apply { isDaemon = true }.start()
            }
            "share_taken" -> {
                lastMessage = "手机已取走：${event.optString("name")}"
            }
            "chat_message" -> {
                val message = event.optJSONObject("message") ?: return
                val who = message.optString("senderName", "对方")
                val preview = message.optString("text").ifBlank {
                    when (message.optString("kind")) {
                        "image" -> "[图片]"
                        "video" -> "[视频]"
                        "voice" -> "[语音]"
                        "file" -> "[文件]"
                        else -> "新消息"
                    }
                }
                lastMessage = "$who：$preview"
                notify("💬 $who", preview)
            }
            "offer_received" -> notify("📥 有人要发文件给你", "打开韧传确认接收")
            "transfer_finished" -> {
                if (event.optString("status") == "done") notify("✅ 传输完成", "")
            }
        }
    }

    private fun startForegroundCompat() {
        ensureChannel()
        val notification = buildNotification("正在连接电脑…")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            @Suppress("DEPRECATION")
            stopForeground(true)
        }
    }

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(NotificationManager::class.java)
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.channel_name),
            NotificationManager.IMPORTANCE_LOW,
        ).apply { description = getString(R.string.channel_desc) }
        manager.createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        ensureChannel()
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.notif_title))
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    private fun updateNotification(text: String) {
        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(NOTIFICATION_ID, buildNotification(text))
    }

    /** 聊天消息单独一条通知，方便在锁屏上直接看到内容。 */
    private fun notify(title: String, text: String) {
        ensureChannel()
        val manager = getSystemService(NotificationManager::class.java)
        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setAutoCancel(true)
            .build()
        manager.notify((System.currentTimeMillis() % 100000).toInt() + 2000, notification)
    }

    override fun onDestroy() {
        stopRequested = true
        running = false
        super.onDestroy()
    }

    fun startIntent(context: Context, host: String): Intent =
        Intent(context, LinkService::class.java).apply {
            action = ACTION_START
            putExtra(EXTRA_HOST, host)
        }
}
