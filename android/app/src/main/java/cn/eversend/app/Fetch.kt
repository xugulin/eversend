package cn.eversend.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Uri
import android.os.Build
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONObject

/**
 * 电脑"交给手机"的文件 → **真的下到手机上**。
 *
 * 为什么必须是这样一条路（真机实测，见 `docs/REAL_DEVICE_FINDINGS.md` §2.3）
 * ----------------------------------------------------------------------
 * 手机 App 是**纯 HTTP 客户端**：`/proc/net/tcp6` 里没有任何 LISTEN 端口，
 * 电脑推不过来。所以 PC → 手机 的唯一形态是：**电脑把文件发布出来**
 * （`/api/state` 的 `shares`），**手机主动来取**（`GET /api/share/<id>`）。
 * 用户报的"电脑说发出了、手机上一个文件都没有"，就是这条路没人走：
 * 以前 App 只把分享列成一行"待保存"，要点一下才下载，会话里也没有进度。
 *
 * 这个类负责把"待取"变成"已收到"：
 *
 * 1. **自动下载**：一发现电脑发布的新文件就排队下载（移动网络下默认不自动跑，
 *    免得拿用户的流量下一部 762 MB 的电影 —— 界面上写明原因，点一下就下）；
 * 2. **进度是真的**：字节流每写一块就报一次，界面显示百分比 + 已下载/总大小；
 * 3. **落地到系统「下载」**（MediaStore），完成后发一条**可点开**的通知；
 * 4. **失败有原因、可重试**，不静默。
 */
enum class FetchState { Queued, Downloading, Done, Failed }

/** 一件"电脑交给我"的文件在手机上的下载状态。 */
data class FetchTask(
    val shareId: String,
    val name: String,
    val size: Long,
    val got: Long = 0L,
    val state: FetchState = FetchState.Queued,
    val error: String = "",
    /** 落地之后的 content URI（MediaStore）；「打开」用的就是它。 */
    val uri: String = "",
    val startedAt: Long = 0L,
    val finishedAt: Long = 0L,
    /** 最近一次测到的速度（字节/秒），界面上写"剩余多久"。 */
    val speed: Double = 0.0,
) {
    val percent: Float get() = if (size > 0) (got.toFloat() / size).coerceIn(0f, 1f) else 0f

    val active: Boolean get() = state == FetchState.Queued || state == FetchState.Downloading

    /** 界面上那一行状态字（中文，带原因）。 */
    val label: String get() = when (state) {
        FetchState.Queued -> "排队中"
        FetchState.Downloading -> "下载中 ${(percent * 100).toInt()}%"
        FetchState.Done -> "已完成"
        FetchState.Failed -> "下载失败：${error.ifBlank { "原因未知" }}"
    }

    /** 进度那句话：百分比 + 已下载/总大小（§2.4 要求的样子）。 */
    val progressText: String
        get() = buildString {
            append("${(percent * 100).toInt()}%")
            append(" · ${fmtSize(got)} / ${fmtSize(size)}")
            if (state == FetchState.Downloading && speed > 0) {
                append(" · ${fmtSpeed(speed)}")
                val left = if (speed > 0) ((size - got) / speed) else 0.0
                if (left > 0) append(" · ${fmtEta(left)}")
            }
        }
}

/**
 * 下载队列（进程内单例）。
 *
 * 为什么要有自己的协程作用域：下载大文件时用户会切到别的 App 或转屏，
 * 挂在 `rememberCoroutineScope` 上的下载会随界面一起被取消 —— 762 MB 传到 90%
 * 断掉，用户看到的是"又失败了"。这里的 Scope 跟着进程走，前台服务（[LinkService]）
 * 只要在跑，熄屏也继续下。
 */
class FetchManager private constructor(private val appContext: Context) {

    companion object {
        private const val TAG = "EverSend"

        /** 「下载完成」通知的渠道：和常驻通知分开，用户可以单独关掉它。 */
        const val CHANNEL_FILES = "eversend-files"

        /** 下载断线后的最大尝试次数（续传，不从头再来）。 */
        private const val MAX_DOWNLOAD_ATTEMPTS = 6

        @Volatile
        private var shared: FetchManager? = null

        fun get(context: Context): FetchManager = shared ?: synchronized(this) {
            shared ?: FetchManager(context.applicationContext).also { shared = it }
        }
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    private val _tasks = MutableStateFlow<List<FetchTask>>(emptyList())

    /** 队列快照（界面直接读它）。 */
    val tasks: StateFlow<List<FetchTask>> = _tasks.asStateFlow()

    /** 正在下载/排队的 id，避免同一份文件被排两次。 */
    private val inFlight = java.util.Collections.synchronizedSet(mutableSetOf<String>())

    /** 用户点了「放弃」的 id：下载线程看到这个标记就中止。 */
    private val cancelled = java.util.Collections.synchronizedSet(mutableSetOf<String>())

    /** 自动下载总开关（设置页可关；默认开）。 */
    @Volatile
    var autoFetch: Boolean = true

    /** 陈旧 pending 行自愈清理的限频（每 60s 最多一次）。 */
    @Volatile
    private var lastPendingCleanupAt = 0L

    /**
     * 兜底清掉陈旧的 pending 行（>5 分钟，A6.3）。
     *
     * 只清"陈旧"的，所以不会误删并发下载中（新鲜）的那条；每次下载结束
     * （成功/失败）调一次，限频 60s —— App 不重启也能自愈。
     */
    private fun cleanupStalePending() {
        val now = System.currentTimeMillis()
        if (now - lastPendingCleanupAt < 60_000L) return
        lastPendingCleanupAt = now
        try {
            val removed = cleanupPendingDownloads(appContext)
            if (removed > 0) Log.d(TAG, "清理了 $removed 条陈旧 pending 残留")
        } catch (ignored: Exception) {
        }
        // 陈旧断点文件（>1 天）也清掉：失败保留了 .part 供续传，但用户可能永远不再点
        // 「重试」——不能让 cache 无限长胖。1 天是"这次会话/今天还会重试"的宽限。
        try {
            val cache = appContext.cacheDir
            cache?.listFiles()?.forEach { file ->
                if (file.name.endsWith(".part") &&
                    now - file.lastModified() > 24L * 60 * 60 * 1000
                ) {
                    if (file.delete()) Log.d(TAG, "清理陈旧断点文件: ${file.name}")
                }
            }
        } catch (ignored: Exception) {
        }
    }

    // ------------------------------------------------------------------ 入队

    /**
     * 电脑发布的文件 → 排队下载。返回这次**新排进去**的 id。
     *
     * `auto = false`（移动网络、或用户关了自动下载）时只登记不下载：
     * 界面上仍然能看到"电脑发来 N 个文件 · 点击下载"，点一下就开始。
     */
    fun offer(api: ApiClient, shares: List<JSONObject>, auto: Boolean = autoFetch): List<String> {
        val queued = mutableListOf<String>()
        val current = _tasks.value.associateBy { it.shareId }
        for (share in shares) {
            val id = share.optString("id")
            if (id.isBlank()) continue
            val name = share.optString("name").ifBlank { "文件" }
            val size = share.optLong("size")
            val downloaded = share.optBoolean("downloaded", false)
            val known = current[id]
            // PG-2/PG-3 取证：每次 offer 决策都要能回答"为什么没起任务"。
            Log.d(
                TAG,
                "offer id=$id name=$name size=$size downloaded=$downloaded " +
                    "known=${known?.state ?: "-"} auto=$auto tasks=${_tasks.value.size}",
            )
            if (downloaded) {
                // 电脑说这份已经被取走了：本机没有记录就补一条"已完成"，
                // 让会话/传输表能对上账（真实字节在电脑上，这里只是个历史）。
                if (known == null) {
                    publish(
                        FetchTask(
                            shareId = id,
                            name = name,
                            size = size,
                            got = size,
                            state = FetchState.Done,
                        ),
                    )
                }
                continue
            }
            if (known != null) {
                // 已经在下载 / 已经下好 / 失败等待重试 —— 都不重复排队。
                if (known.active || known.state == FetchState.Done) continue
                if (known.state == FetchState.Failed && !auto) continue
            } else if (!auto) {
                // 自动下载关着：先登记成"排队中"，界面上给入口。
                publish(FetchTask(shareId = id, name = name, size = size, state = FetchState.Queued))
                continue
            }
            start(api, FetchTask(shareId = id, name = name, size = size))
            queued += id
        }
        return queued
    }

    /** 手动「下载到手机」/「重试」。 */
    fun fetchNow(api: ApiClient, shareId: String, name: String, size: Long) {
        Log.d(TAG, "fetchNow id=$shareId name=$name size=$size")
        cancelled.remove(shareId)
        start(api, FetchTask(shareId = shareId, name = name, size = size))
    }

    /** 界面上的「放弃」：中止下载，留着记录但标失败（可以再点重试）。 */
    fun cancel(shareId: String) {
        cancelled.add(shareId)
        update(shareId) { it.copy(state = FetchState.Failed, error = "已取消") }
    }

    fun forget(shareId: String) {
        _tasks.value = _tasks.value.filterNot { it.shareId == shareId }
    }

    /** 现在是不是"不该自动下大文件"的网络（移动网络/计费网络）。 */
    fun meteredNetwork(): Boolean = try {
        val manager = appContext.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
        manager?.isActiveNetworkMetered ?: false
    } catch (problem: Exception) {
        false
    }

    // ------------------------------------------------------------------ 下载

    private fun start(api: ApiClient, task: FetchTask) {
        val added = inFlight.add(task.shareId)
        Log.d(TAG, "start id=${task.shareId} inFlightAdded=$added name=${task.name} size=${task.size}")
        if (!added) return
        cancelled.remove(task.shareId)
        publish(task.copy(state = FetchState.Downloading, startedAt = System.currentTimeMillis(), error = ""))
        Log.d(TAG, "start published id=${task.shareId} tasksNow=${_tasks.value.size}")
        scope.launch { run(api, task) }
    }

    /**
     * 下载一份电脑发布的文件。
     *
     * **断点续传**（P1-D）：先下到 App 缓存里的 `.part` 文件，收满再一次性发布到
     * 系统「下载」。原因：MediaStore 的 pending 输出流没法可靠地"从第 N 字节续写"，
     * 而真机切换前台/锁屏那一刻连接会被系统掐掉（A2：100MB 下到 73MiB 被 abort）。
     * 缓存文件可以自由 append，配合服务端的 Range(206) 从断点续；代价是多一次
     * 本地拷贝（局域网 24MB/s 下 100MB 只多花 <1s）。
     */
    private fun run(api: ApiClient, task: FetchTask) {
        var lastReport = 0L
        var lastBytes = 0L
        var lastAt = System.currentTimeMillis()
        var uri: Uri? = null
        val partial = java.io.File(
            appContext.cacheDir,
            "fetch-" + task.shareId.replace(Regex("[^A-Za-z0-9_.-]"), "_") + ".part",
        )
        try {
            partial.parentFile?.mkdirs()
            var attempts = 0
            while (true) {
                val already = if (partial.exists()) partial.length() else 0L
                if (task.size > 0 && already >= task.size) break
                attempts++
                lastBytes = already
                lastAt = System.currentTimeMillis()
                try {
                    java.io.FileOutputStream(partial, true).use { sink ->
                        api.download(
                            "/api/share/" + encodeUrl(task.shareId),
                            sink,
                            offset = already,
                        ) { sent ->
                            if (cancelled.contains(task.shareId)) throw java.io.IOException("已取消")
                            val now = System.currentTimeMillis()
                            if (now - lastReport >= 400 || (task.size > 0 && sent >= task.size)) {
                                lastReport = now
                                val elapsed = (now - lastAt).coerceAtLeast(1L)
                                val speed = (sent - lastBytes) * 1000.0 / elapsed
                                lastBytes = sent
                                lastAt = now
                                update(task.shareId) {
                                    it.copy(
                                        state = FetchState.Downloading,
                                        got = sent,
                                        speed = speed,
                                        error = "",
                                    )
                                }
                            }
                        }
                    }
                } catch (problem: Exception) {
                    if (cancelled.contains(task.shareId)) throw problem
                    val landedNow = if (partial.exists()) partial.length() else 0L
                    Log.d(
                        TAG,
                        "下载中断（第 $attempts 次尝试，已落 $landedNow/${task.size} 字节）: ${problem.message}",
                    )
                    if (attempts >= MAX_DOWNLOAD_ATTEMPTS) throw problem
                    update(task.shareId) {
                        it.copy(
                            state = FetchState.Downloading,
                            got = landedNow,
                            speed = 0.0,
                            error = "连接中断，正在从断点续传…（第 $attempts 次）",
                        )
                    }
                    Thread.sleep((500L * (1L shl (attempts - 1).coerceAtMost(4))).coerceAtMost(8_000L))
                    continue
                }
                break
            }
            val got = if (partial.exists()) partial.length() else 0L
            if (task.size > 0 && got != task.size) {
                throw java.io.IOException("只收到 $got / ${task.size} 字节")
            }
            // 收满了再发布到系统「下载」（期望字节数仍然要校验一遍）。
            uri = saveToDownloads(appContext, task.name, if (task.size > 0) task.size else -1L) { sink ->
                java.io.FileInputStream(partial).use { input ->
                    val buffer = ByteArray(256 * 1024)
                    while (true) {
                        val read = input.read(buffer)
                        if (read <= 0) break
                        sink.write(buffer, 0, read)
                    }
                }
            }
        } catch (problem: Exception) {
            // 失败时**保留 .part**（除非用户主动取消）：
            // ① 下一次自动 offer / 用户点「重试」时 run() 会从 .part 长度续传，不用从头再来；
            // ② .part 在 App cache 里，不会产生 A6.3 的 MediaStore pending 残留；
            // ③ 启动时清 >1 天的陈旧 .part（cleanupStalePending 里做）。
            Log.d(TAG, "下载失败 ${task.name}: ${problem.message}")
            inFlight.remove(task.shareId)
            val cancelledByUser = cancelled.contains(task.shareId)
            val kept = if (partial.exists()) partial.length() else 0L
            if (cancelledByUser) {
                try {
                    partial.delete()
                } catch (ignored: Exception) {
                }
            }
            val reason = if (cancelledByUser) {
                "已取消"
            } else {
                val base = reasonOf(problem)
                if (kept > 0L) {
                    "$base（已保留 ${fmtSize(kept)} 断点：网络恢复后点「重试」会从断点续传）"
                } else {
                    "$base（网络恢复后点「重试」）"
                }
            }
            update(task.shareId) { it.copy(state = FetchState.Failed, error = reason, got = kept) }
            cleanupStalePending()
            return
        }
        try {
            partial.delete()
        } catch (ignored: Exception) {
        }
        inFlight.remove(task.shareId)
        if (cancelled.contains(task.shareId)) {
            update(task.shareId) { it.copy(state = FetchState.Failed, error = "已取消") }
            return
        }
        if (uri == null) {
            update(task.shareId) {
                it.copy(state = FetchState.Failed, error = "写不进系统「下载」目录（空间不足，或只写了一部分）")
            }
            return
        }
        val landed = _tasks.value.firstOrNull { it.shareId == task.shareId }?.got ?: task.size
        publish(
            FetchTask(
                shareId = task.shareId,
                name = task.name,
                size = task.size,
                got = if (task.size > 0) task.size else if (landed > 0) landed else task.size,
                state = FetchState.Done,
                uri = uri.toString(),
                startedAt = task.startedAt,
                finishedAt = System.currentTimeMillis(),
            ),
        )
        notifyDone(task.name, uri)
        cleanupStalePending()
    }

    // ------------------------------------------------------------------ 状态

    /**
     * 落一条下载任务到 UI 状态（**insert-or-update**）。
     *
     * 以前这里实现成了 update-only（`_tasks.value.map { ... }`）：新 share 的任务
     * 根本不会进入列表 → `ui.fetches` 恒为空 → 真机表现为
     * 「传输中无百分比/速度、批量条目一直『正在排队』、完成后没有『已保存』」
     * （PG-1/PG-2/PG-3 的总根因，v9 诊断日志 `start published ... tasksNow=0` 实锤）。
     * 下载线程本身是对的（`start()` 直接 `run()`），坏的只是"状态没有登记"。
     */
    private fun publish(task: FetchTask) {
        val current = _tasks.value
        _tasks.value = if (current.any { it.shareId == task.shareId }) {
            current.map { if (it.shareId == task.shareId) task else it }
        } else {
            current + task
        }
    }

    private fun update(shareId: String, change: (FetchTask) -> FetchTask) {
        _tasks.value = _tasks.value.map { if (it.shareId == shareId) change(it) else it }
    }

    /** 完成的文件点一下就能打开（Content URI + 系统的查看器）。 */
    fun openTask(context: Context, task: FetchTask): Boolean {
        val raw = task.uri.takeIf { it.isNotBlank() } ?: return false
        return try {
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(Uri.parse(raw), mimeForName(task.name))
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            context.startActivity(intent)
            true
        } catch (problem: Exception) {
            Log.d(TAG, "打开失败: ${problem.message}")
            false
        }
    }

    /** 「下载完成」通知：可点开（§ 要求"完成后通知、可点击打开"）。 */
    private fun notifyDone(name: String, uri: Uri?) {
        try {
            val manager = appContext.getSystemService(NotificationManager::class.java) ?: return
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O &&
                manager.getNotificationChannel(CHANNEL_FILES) == null
            ) {
                manager.createNotificationChannel(
                    NotificationChannel(
                        CHANNEL_FILES,
                        "文件下载完成",
                        NotificationManager.IMPORTANCE_DEFAULT,
                    ).apply { description = "电脑发来的文件下载完成时提醒，点一下就能打开" },
                )
            }
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, mimeForName(name))
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            val pending = PendingIntent.getActivity(
                appContext,
                name.hashCode(),
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
            val notification = NotificationCompat.Builder(appContext, CHANNEL_FILES)
                .setContentTitle("已下载：$name")
                .setContentText("来自电脑 · 点一下打开")
                .setSmallIcon(R.drawable.ic_launcher_foreground)
                .setAutoCancel(true)
                .setContentIntent(pending)
                .build()
            manager.notify((System.currentTimeMillis() % 100000).toInt() + 3000, notification)
        } catch (problem: Throwable) {
            // 没有通知权限（用户拒了）也只是没提醒，文件已经在「下载」里了。
            Log.d(TAG, "下载完成通知发不出去: ${problem.message}")
        }
    }
}

/** 按文件名猜 MIME（通知里点开时要告诉系统用什么程序）。 */
fun mimeForName(name: String): String {
    val extension = name.substringAfterLast('.', "").lowercase()
    return android.webkit.MimeTypeMap.getSingleton().getMimeTypeFromExtension(extension)
        ?: "application/octet-stream"
}

/**
 * 把下载中的文件伪装成一条"接收中的传输"，喂给既有的进度 UI。
 *
 * 会话气泡与传输表早就会画"接收中 42% + 进度条"，靠的是 `/api/state` 的
 * `transfers`。手机这条下载路径在服务端**不是**一笔 transfer（它是 HTTP 拉取），
 * 所以这里按同一套字段造一份 —— 界面代码一行都不用改，三端的观感也就一致了。
 */
fun fetchTransfers(fetches: List<FetchTask>): List<JSONObject> = fetches.mapNotNull { task ->
    // 只有真的在下载的才算「接收中」：排队中的任务由气泡显示「排队中」，
    // 不能占「接收中 X%」那一行（否则排队和下载中在界面上无法区分）。
    if (task.state != FetchState.Downloading) return@mapNotNull null
    try {
        JSONObject()
            .put("transferId", "fetch-" + task.shareId)
            .put("direction", "receive")
            .put("percent", task.percent * 100.0)
            .put("done", task.got)
            .put("total", task.size)
            // 速度和 ETA 也给出来：气泡/卡片上的进度行要显示"与真值一致"的数字，
            // 不能只显示百分比（PG-2）。
            .put("speed", task.speed)
            .put(
                "eta",
                if (task.speed > 0 && task.size > task.got) (task.size - task.got) / task.speed else 0.0,
            )
            .put("items", org.json.JSONArray().put(JSONObject().put("name", task.name)))
    } catch (ignored: Exception) {
        null
    }
}
