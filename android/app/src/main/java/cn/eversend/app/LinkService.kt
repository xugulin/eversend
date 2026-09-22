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
        var lastState: String = "未连接"
            private set

        @Volatile
        var lastMessage: String = ""
            private set

        @Volatile
        var running: Boolean = false
            private set
    }

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
        if (worker?.isAlive == true) return
        stopRequested = false
        running = true
        worker = Thread({ loop() }, "eversend-link").apply { isDaemon = true; start() }
    }

    private fun loop() {
        var backoff = 1000L
        while (!stopRequested) {
            try {
                if (token.isBlank()) token = ApiClient.fetchToken(base)
                lastState = "已连接"
                updateNotification("已连接 · 熄屏也在")
                backoff = 1000L
                ApiClient(base, token).events(
                    onEvent = { event -> onEvent(event) },
                    isRunning = { !stopRequested },
                )
                lastState = "重连中…"
            } catch (error: Exception) {
                lastState = "重连中…（${error.javaClass.simpleName}）"
                Log.d(TAG, "事件流断开: ${error.message}")
            }
            if (stopRequested) break
            updateNotification(lastState)
            try {
                Thread.sleep(backoff)
            } catch (interrupted: InterruptedException) {
                break
            }
            backoff = (backoff * 2).coerceAtMost(15000L)
        }
        running = false
        lastState = "未连接"
        stopForegroundCompat()
    }

    private fun onEvent(event: JSONObject) {
        when (event.optString("kind")) {
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
