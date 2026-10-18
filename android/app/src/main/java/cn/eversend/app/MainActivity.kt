package cn.eversend.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.MediaMetadataRetriever
import android.media.MediaPlayer
import android.media.MediaRecorder
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.OpenableColumns
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.OffsetMapping
import androidx.compose.ui.text.input.TransformedText
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 韧传 Android。
 *
 * 布局按 `docs/UI_LAYOUT.md` 的 harness 风格来（三个客户端同一份规格）：
 *
 * ```
 * ┌────────────── 侧栏（300dp，常驻或抽屉）──────────────┬───────── 主区 ─────────┐
 * │ ① ⚡ 韧传 EverSend · 本机名 · 地址                    │ ④ 对方设备详情 + [会话｜传输] │
 * │ ② ＋发送文件 / 刷新·扫描·手动·手机                    │ ⑤ 会话气泡 / 传输出口+表格    │
 * │ ③ 设备列表（在线 / 离线两段 + 群聊）                 │ ⑥ 输入区（只在会话页）        │
 * │ 底部 ▸传输（进行中徽标） ▸⚙设置                       │                              │
 * └─────────────────────────────────────────────────────┴──────────────────────────────┘
 * ```
 *
 * 窄屏（手机竖屏）放不下 300dp 侧栏，就用 `ModalNavigationDrawer` 收成抽屉，
 * 左上角 ☰ 打开；宽屏（≥ 600dp，平板/横屏）常驻侧栏。**两种形态用的是同一个
 * `AppSidebar`**，所以内容顺序天然一致（这是规格里的硬要求）。
 *
 * 「传输」有两个入口（顶部 `[会话｜传输]` 分段、侧栏底部「传输」），它们指向
 * 同一页、高亮同步；传输页里 `[接收｜发送]` 分段**就地**切换两张表格，不跳页。
 *
 * Emoji：界面所有文本都用 Compose 的 `Text`，走系统字体栈，彩色 emoji 由系统
 * 字体渲染（Noto Color Emoji），和微信、相册一个水平；输入既可以用系统键盘的
 * emoji 面板，也可以用内置面板（发出去的就是普通 Unicode 字符，电脑端同样能显示）。
 * 我们**不**自带字体、不用图片当表情——那样跨设备一定花。内置的那个 😊 面板是
 * 输入区里的**内嵌面板**，宽度和输入行对齐、紧贴待发方块那一行的上边（§3⑥）。
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = AppState(this)
        // CI 里用 intent extra 注入电脑地址，省去手工扫码/输入。
        intent?.getStringExtra("host")?.let { store.host = it }
        // 每个请求都会带上它：电脑端靠设备号认这台手机，不用猜来源地址。
        ApiClient.remember(store.deviceId, store.deviceName)
        setContent { EverSendTheme { Root(store) } }
    }
}

class AppState(context: android.content.Context) {
    private val prefs = context.getSharedPreferences("eversend", android.content.Context.MODE_PRIVATE)

    init {
        // 每个 HTTP 请求都要带上设备号（电脑端靠它认这台手机，而不是靠来源
        // 地址）。放在这里而不是 Activity 里：前台服务和测试走的也是这个类，
        // 只在 Activity 里记的话，服务发出的请求就没有身份了。
        //
        // **必须在这里就把 id 生成出来，不能等到有人读 deviceId**：以前这里
        // 记的是"prefs 里已有的值"，全新安装时是空字符串，于是保活服务发出的
        // 请求（包括它那条长连接的事件流）都不带设备号 —— 电脑端只好按来源地址
        // 另记一条"浏览器"式的记录，而真正的设备记录只有 /api/hello 正文里那
        // 一下，15 秒后就变成"未连接"。用户看到的是手机明明连着、电脑端说它离线
        // （仪器测试 keepAliveServiceStaysOnline 抓的就是这个）。
        ApiClient.remember(deviceId, deviceName)
    }

    /** 稳定设备 id：电脑端靠它认出"还是这台手机"，换 Wi-Fi 也不会变成新设备。 */
    val deviceId: String
        get() = prefs.getString("deviceId", null) ?: java.util.UUID.randomUUID().toString().also {
            prefs.edit().putString("deviceId", it).apply()
        }

    /** 手机型号当名字，用户一眼能认出是哪台。 */
    val deviceName: String
        get() = prefs.getString("deviceName", null) ?: (Build.MODEL ?: "安卓手机").also {
            prefs.edit().putString("deviceName", it).apply()
        }

    var host: String
        get() = prefs.getString("host", "") ?: ""
        set(value) {
            prefs.edit().putString("host", value).apply()
        }

    var keepAlive: Boolean
        get() = prefs.getBoolean("keepalive", true)
        set(value) {
            prefs.edit().putBoolean("keepalive", value).apply()
        }

    /**
     * 最近一次连上的客户端（进程内共享）。
     *
     * 设置页点「连接」之后，会话页应当直接用它，而不是自己再猜一遍地址 ——
     * 以前两边各连各的，就会出现"设置页说已连接、聊天页说连不上"。
     */
    var client: ApiClient? = null

    /** 广播循环只起一个（见 [announcePresence]）。 */
    var announcing: Boolean = false

    /** 这个地址是怎么来的（手输 / 搜索 / 记住的），主区顶部要写"发现方式"。 */
    var via: String = ""

    /**
     * 上一次尝试"记住的地址"的时刻。
     *
     * 电脑换了 IP 之后那个地址会一直连不上，而每次尝试最长要等十几秒（连接超时
     * 6 秒 + 读超时）—— 不节流的话，界面会一直在"正在连接"，自动发现的结果反而
     * 排不上队。节流之后：记住的地址试一次，不行就让位给刚发现的设备。
     */
    var lastTryAt: Long = 0L

    /**
     * 上次连上的那台电脑的稳定身份（identityKey）。
     *
     * 发现列表里可能有好几台电脑（办公室里不止一台），**不能因为"刚听到一台新的"
     * 就换一台连** —— 用户上一次用的是哪台，下次还应该是哪台。有了它，电脑换了 IP
     * 也能跟上（发现列表按身份匹配，地址用最新报出来的那个）。
     */
    var lastDesktopId: String
        get() = prefs.getString("lastDesktopId", "") ?: ""
        set(value) {
            prefs.edit().putString("lastDesktopId", value).apply()
        }
}

// ------------------------------------------------------------- 界面共享状态

/** 主区显示哪一页。`[会话｜传输]` 分段与侧栏底部两项都改它，所以高亮永远同步。 */
enum class MainPage { Chat, Transfers, Settings }

/** 传输页里的两张表：`[接收｜发送]` 就地切换，不跳页、不弹窗。 */
enum class TransferTab { Receive, Send }

/** 传输页的筛选药丸（宪章 §3.2）。 */
enum class TransferFilter(val label: String, val tag: String) {
    All("全部", "filter-all"),
    Running("进行中", "filter-running"),
    Done("已完成", "filter-done"),
    Failed("失败", "filter-failed"),
}

/**
 * 一条"还没投出去"的消息（等待重发）。
 *
 * 失败不再只写一行状态字：它进这个队列，界面上有一条带红点的「待重发」条目，
 * 带原因、带「重试」与「放弃」两个动作（宪章 §5.5：队列可见、可手动重试、可放弃）。
 */
data class Queued(
    val key: String,
    val conversationId: String,
    val text: String,
    val attempts: Int = 0,
    val error: String = "",
    val nextAt: Long = 0L,
    val createdAt: Long = System.currentTimeMillis(),
)

/** 最多自动重投几次（宪章 §5.5 上限 5 次）。 */
const val MAX_SEND_ATTEMPTS = 5

/** 指数退避 1s / 2s / 4s / 8s / 16s（宪章 §5.5）。 */
fun backoffMs(attempt: Int): Long = 1000L shl (attempt - 1).coerceIn(0, 4)

/** 第 [attempts] 次失败之后：记原因、排下一次重试时间。 */
fun Queued.afterFailure(reason: String, now: Long = System.currentTimeMillis()): Queued =
    copy(attempts = attempts + 1, error = reason, nextAt = now + backoffMs(attempts + 1))

/** 自动重投已经用完（要用户点一下「重试」才会再来 5 次）。 */
fun Queued.exhausted(): Boolean = attempts >= MAX_SEND_ATTEMPTS

/** 到点了、而且次数没用完 —— 可以自动重投。 */
fun Queued.ready(now: Long = System.currentTimeMillis()): Boolean = !exhausted() && now >= nextAt

/** 手动「重试」：次数清零，立刻可投。 */
fun Queued.retryNow(now: Long = System.currentTimeMillis()): Queued =
    copy(attempts = 0, error = "", nextAt = now)

/** 待发文件：系统文件选择器给的 content URI + 名字 + 大小。 */
data class Picked(val uri: Uri, val name: String, val size: Long)

/**
 * 界面共享状态（侧栏、主区、输入区读的是同一份）。
 *
 * 规格第 4 节：「选中设备」是全局状态，侧栏选中行、主区顶部信息、输入区目标
 * 三者永远一致。以前每个 tab 各自 `remember` 一份，切个页就丢，用户看到的是
 * 「选好的设备又没了」。
 */
class UiState {
    var devices by mutableStateOf<List<DeviceRow>>(emptyList())
    var selected by mutableStateOf<DeviceRow?>(null)
    var conversations by mutableStateOf<List<Conversation>>(emptyList())
    var open by mutableStateOf<Conversation?>(null)
    var messages by mutableStateOf<List<ChatMessage>>(emptyList())
    var transfers by mutableStateOf<List<JSONObject>>(emptyList())
    var uploads by mutableStateOf<List<JSONObject>>(emptyList())
    var recentUploads by mutableStateOf<List<JSONObject>>(emptyList())
    var offers by mutableStateOf<List<JSONObject>>(emptyList())
    var shares by mutableStateOf<List<JSONObject>>(emptyList())
    var computerFiles by mutableStateOf<List<JSONObject>>(emptyList())
    var receiveDir by mutableStateOf("")
    var page by mutableStateOf(MainPage.Chat)
    var tab by mutableStateOf(TransferTab.Receive)
    var pending by mutableStateOf<List<Picked>>(emptyList())
    var targets by mutableStateOf<Set<String>>(emptySet())
    var pin by mutableStateOf("")
    var pinNeeded by mutableStateOf(false)
    var status by mutableStateOf("")
    var error by mutableStateOf("")
    var busy by mutableStateOf(false)
    var client by mutableStateOf<ApiClient?>(null)
    var found by mutableStateOf<List<ApiClient.Found>>(emptyList())
    var scanning by mutableStateOf(false)
    var dialog by mutableStateOf("")
    var manualHost by mutableStateOf("")
    var pickingGroup by mutableStateOf(false)
    var members by mutableStateOf<List<DeviceRow>>(emptyList())
    var chosen by mutableStateOf<Set<String>>(emptySet())
    var groupTitle by mutableStateOf("")
    var hiddenUploads by mutableStateOf<Set<String>>(emptySet())
    var clearedMessages by mutableStateOf(false)
    var autoOpened by mutableStateOf(false)
    /**
     * `/api/chat` 说的"我是谁"（`web:<设备号>`）。
     *
     * 消息在电脑端库里存的 `direction` 是**电脑视角**的（手机发过去的是 "in"），
     * 所以手机不能拿它当自己的左右 —— 以前手机自己发的每一句都画成"收到"。
     * 现在按 `sender == selfId` 算，谁看就是谁的视角（和网页版 `selfId` 同一个）。
     */
    var selfId by mutableStateOf("")
    /**
     * 「传输文件」发出去的本机文件（文件身份 → content URI）。
     *
     * 会话里那条传输记录要能"预览/打开/另存为"：手机上还留着原件时就直接用它，
     * 不在手上（比如 App 重开过）就走电脑上那份（`/api/download?path=`）。
     */
    var localFiles by mutableStateOf<Map<String, Uri>>(emptyMap())
    /**
     * 常驻发现引擎报上来的设备（[LanDiscovery]）。
     *
     * 这是"零手动输入"的关键：只要同网段有电脑在跑韧传，≤5 秒它就会出现在这里，
     * 界面把它并进设备列表并自动连上去 —— 用户不需要点任何按钮，更不需要输 IP。
     */
    var discovered by mutableStateOf<List<DiscoveredPeer>>(emptyList())
    /** 发现引擎的诊断状态（设置页「诊断」卡片，对应 E2）。 */
    var discovery by mutableStateOf<DiscoveryStats?>(null)
    /** 传输页筛选药丸（宪章 §3.2：全部 / 进行中 / 已完成 / 失败）。 */
    var filter by mutableStateOf(TransferFilter.All)
    /**
     * 还没投出去的消息（宪章 §5.5 / C14）：失败必须看得见、能重试、能放弃。
     *
     * 以前 [Root] 里的 `sendText` 只是把失败写进一行状态文字，"发出去但对面没有"
     * 的情况用户根本看不出来 —— 这就是用户报的"发送消息还会失败不显示"。
     */
    var outbox by mutableStateOf<List<Queued>>(emptyList())
    /**
     * 电脑"交给手机"的那些文件在本机的下载状态（[FetchManager]）。
     *
     * 会话里那条「电脑发来 N 个文件 · 点击下载」、传输表里的进度行、以及气泡上的
     * 百分比，读的都是它 —— 一份状态，三处显示（宪章第 3 条"一行一事实"）。
     */
    var fetches by mutableStateOf<List<FetchTask>>(emptyList())
    /** 自动下载开关（设置页可关；默认开）。 */
    var autoFetch by mutableStateOf(true)

    /**
     * 前台保活服务当前用哪个地址（"" = 没起来）。
     *
     * 用它做幂等：只有地址变了才重新 startForegroundService —— 重复调用虽然
     * 幂等，但每 3 秒发一次 binder 调用没必要。
     */
    var linkHost by mutableStateOf("")
}

// ------------------------------------------------------------------ 根布局

@Composable
fun Root(store: AppState) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val ui = remember { UiState() }

    // 通知权限：前台服务靠它把「来消息了」摆到状态栏。放在根上申请，
    // 而不是等用户点进设置页。
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
        val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { }
        LaunchedEffect(Unit) { permission.launch(Manifest.permission.POST_NOTIFICATIONS) }
    }

    // 自动发现：**打开 App 就开始**，不点任何按钮。
    //
    // 这是"必须手输 IP"的直接修复：引擎常驻（组播 + 广播 + 单播 + 主动扫描四条
    // 通道），同网段的电脑 ≤5 秒就会出现在 peers 里，下面的连接循环会自动连上去。
    val discovery = remember { LanDiscovery.get(context) }
    LaunchedEffect(Unit) { discovery.start() }
    LaunchedEffect(discovery) {
        discovery.peers.collect { peers ->
            ui.discovered = peers
            Log.d("EverSend", "自动发现列表更新：${peers.size} 台（${peers.joinToString("、") { it.name + "@" + it.host }}）")
        }
    }
    LaunchedEffect(discovery) {
        discovery.stats.collect { stats -> ui.discovery = stats }
    }

    // 启动时清一次"上次失败留下的 pending 残留"：真机上抓到过 5 个 0 字节的
    // `.pending-*`（既不在「下载」里显示，又占着名字，用户会以为程序坏了）。
    LaunchedEffect(Unit) {
        val removed = withContext(Dispatchers.IO) { cleanupPendingDownloads(context) }
        if (removed > 0) Log.d("EverSend", "清理了 $removed 条下载残留")
    }

    // 电脑发布的文件 → 下载队列（进程内的单例，切页/转屏都不会把下载掐断）。
    val fetcher = remember { FetchManager.get(context) }
    LaunchedEffect(fetcher) {
        ui.autoFetch = fetcher.autoFetch
        fetcher.tasks.collect { tasks -> ui.fetches = tasks }
    }

    // 前台保活服务：**App 一启动就把服务拉起来**（keepAlive 默认开），而且
    // **不等 host 非空**：新装/换网/电脑还没出现时 host 可能是空的，而这正是最
    // 需要保活的时候（发现引擎要持续公告+组播监听；掉到 cached 会被系统掐网、
    // 销毁 socket）。地址为空时服务先跑发现，连上后这个循环会把地址补进去。
    //
    // 以前这段只在「设置」页的 LaunchedEffect 里 —— 用户不点进设置页，就从来
    // 没有前台服务；App 一旦掉到后台（切文件选择器、熄屏），系统把进程
    // importance 降成 CACHED(400)，紧接着：
    //   · `InetDiagMessage: Destroyed live tcp sockets for uids={10292}`
    //     （100MB 下载到 73MiB 被切走 → "Software caused connection abort"）；
    //   · 发现通道 udp sendto 全部 EPERM（日志刷屏）。
    // 真机（MI9/crDroid 16）实测：回到前台 importance=100 立刻恢复。
    LaunchedEffect(Unit) {
        while (true) {
            val host = normalizeHost(store.host)
            // "<未连接>" 也是合法的"已启动"状态：服务已经在跑，只差地址。
            val wantedKey = if (store.keepAlive) host.ifBlank { "<未连接>" } else ""
            if (wantedKey.isNotEmpty() && ui.linkHost != wantedKey) {
                startLinkService(context, store, true)
                ui.linkHost = wantedKey
                Log.d("EverSend", "保活服务启动（host=${host.ifBlank { "待发现" }}）")
            } else if (wantedKey.isEmpty() && ui.linkHost.isNotEmpty()) {
                startLinkService(context, store, false)
                ui.linkHost = ""
                Log.d("EverSend", "保活服务停止（keepAlive=false）")
            }
            delay(3000)
        }
    }

    // 待重发队列的泵：1s/2s/4s/8s/16s 退避、最多 5 次；到点自动再投一次。
    // 投不出去就一直留在界面上（带原因），绝不再"静默丢弃"。
    LaunchedEffect(Unit) {
        while (true) {
            delay(1000)
            val active = ui.client ?: continue
            val now = System.currentTimeMillis()
            for (item in ui.outbox.filter { it.ready(now) }) {
                val reason = withContext(Dispatchers.IO) { deliverMessage(active, item) }
                ui.outbox = ui.outbox.mapNotNull { queued ->
                    when {
                        queued.key != item.key -> queued
                        reason == null -> null                     // 投出去了：出队
                        else -> queued.afterFailure(reason)
                    }
                }
                if (reason == null) {
                    ui.status = "重发成功：${item.text.take(20)}"
                    refreshOnce(store, ui)
                } else if (ui.outbox.firstOrNull { it.key == item.key }?.exhausted() == true) {
                    ui.error = "消息发不出去：$reason（已重试 $MAX_SEND_ATTEMPTS 次，可点「重试」再来）"
                }
            }
        }
    }

    // 一条循环管全部：连上电脑 → 一次把 /api/state 该读的都读回来 → 喂给侧栏、
    // 会话、传输三块。以前每个 tab 各起一条轮询，切页就断、数字还互相打架。
    LaunchedEffect(Unit) {
        var fast = true
        while (true) {
            val active = ui.client ?: withContext(Dispatchers.IO) { connectSession(store, ui) }
            if (active != null) {
                val snap = withContext(Dispatchers.IO) {
                    try {
                        readSnapshot(active, store, ui.open?.id)
                    } catch (problem: Exception) {
                        null
                    }
                }
                if (snap != null) {
                    applySnapshot(ui, snap, store)
                    ui.error = ""
                    // 电脑发布的文件：**自动开始下载**（这就是"收不到文件"的修复）。
                    // 移动网络下不自动跑（762 MB 走流量会挨骂），只在界面上写明原因；
                    // 用户点一下「全部下载」照样下。
                    val pending = snap.shares.filterNot { it.optBoolean("downloaded", false) }
                    if (pending.isNotEmpty()) {
                        val auto = ui.autoFetch && !fetcher.meteredNetwork()
                        fetcher.autoFetch = ui.autoFetch
                        withContext(Dispatchers.IO) { fetcher.offer(active, pending, auto) }
                    }
                }
            }
            // 头几次快一点：刚打开 App 时要马上看到和电脑的会话，稳定后 3 秒一轮。
            delay(if (fast) 400 else 3000)
            if (ui.open != null) fast = false
        }
    }

    // 选文件（多选）：侧栏「＋ 发送文件」和输入区「📎 传输文件」都走这里。
    val pickFiles = rememberLauncherForActivityResult(
        ActivityResultContracts.GetMultipleContents()
    ) { uris: List<Uri> ->
        if (uris.isEmpty()) return@rememberLauncherForActivityResult
        val added = uris.map { uri ->
            val info = queryFile(context, uri)
            Picked(uri, info.first, info.second)
        }
        ui.pending = (ui.pending + added).distinctBy { it.uri.toString() }
        // 记住"这份文件在手机上的原件在哪"：会话里那条传输记录要能预览/另存为，
        // 手机上还留着就用原件（比再从电脑取一遍快，也离线可用）。
        ui.localFiles = ui.localFiles + added.associate { rowIdentity(it.name, it.size) to it.uri }
        if (ui.targets.isEmpty()) {
            ui.selected?.let { ui.targets = setOf(it.id) } ?: run {
                ui.devices.firstOrNull()?.let { ui.targets = setOf(it.id) }
            }
        }
        ui.page = MainPage.Chat
        ui.status = "已选 ${ui.pending.size} 个文件，点「发送」发给 " +
            (ui.devices.filter { ui.targets.contains(it.id) }.joinToString("、") { it.name }
                .ifBlank { "还没选设备" })
    }

    // 附件（图片/视频/文件）走会话：单张选，和以前一样。
    val pickAttachment = rememberLauncherForActivityResult(
        ActivityResultContracts.GetContent()
    ) { uri: Uri? ->
        val active = ui.client
        if (uri != null && active != null) {
            scope.launch {
                uploadUri(context, active, uri) { scope.launch { refreshOnce(store, ui) } }
            }
        }
    }

    val refresh: () -> Unit = { scope.launch { refreshOnce(store, ui) } }

    val sendPending: () -> Unit = {
        val files = ui.pending
        val chosen = ui.devices.filter { ui.targets.contains(it.id) }
        when {
            files.isEmpty() -> ui.status = "先点「📎 传输文件」选要发的文件"
            chosen.isEmpty() -> ui.status = "先选发给哪台设备（点设备栏里的设备）"
            ui.client == null -> ui.status = "还没连上电脑，先连上再发"
            else -> {
                ui.busy = true
                ui.status = "正在发送 ${files.size} 个文件给 ${chosen.size} 台设备…"
                scope.launch {
                    var done = 0
                    var delivered = 0
                    val problems = mutableListOf<String>()
                    val notSent = mutableListOf<Picked>()
                    files.forEach { picked ->
                        var thisFileOk = false
                        chosen.forEach { device ->
                            // 电脑端重启会换防跨站令牌，手里的旧令牌就 403。换一张再试一次 ——
                            // 不然用户看到的就是"发不出去"，其实只差一张令牌（真机实测到过）。
                            suspend fun deliver(): Boolean {
                                val api = ui.client
                                    ?: throw IllegalStateException("还没连上电脑")
                                return try {
                                    withContext(Dispatchers.IO) {
                                        uploadToDevice(
                                            context, api, picked.uri,
                                            Pair(picked.name, picked.size), device, store, ui.pin,
                                        )
                                    }
                                    true
                                } catch (problem: Exception) {
                                    val why = problem.message ?: problem.javaClass.simpleName
                                    android.util.Log.w("EverSend", "发送失败（准备换令牌）: $why")
                                    // **refreshToken() 会发 HTTP 请求，必须在 IO 线程**。
                                    // 以前这里裸调（deliver() 跑在主调度器上），真机日志：
                                    //   "refreshToken 失败: NetworkOnMainThreadException: null"
                                    // —— 所以"令牌过期→换一张→自动重发"这条恢复路径从来没生效过，
                                    // 用户看到的就是发不出去（A7 真机用例 FAIL 的直接原因）。
                                    // 令牌过期的表现**不只一种**：电脑端重启后地址不变，
                                    // 服务端 CSRF 门禁在**读 body 之前**就 403 并关连接——
                                    // 客户端正在流式写 body，看到的是 `Broken pipe` /
                                    // `Connection reset`，而不是含「令牌」的错误 JSON
                                    // （真机 P0：6 次重试全是 Broken pipe，自动恢复没触发）。
                                    val whyLower = why.lowercase()
                                    val tokenish = why.contains("令牌") ||
                                        whyLower.contains("broken pipe") ||
                                        whyLower.contains("connection reset") ||
                                        whyLower.contains("socketexception") ||
                                        whyLower.contains("unexpected end of stream")
                                    val refreshed = if (tokenish) {
                                        withContext(Dispatchers.IO) {
                                            try {
                                                api.refreshToken()
                                            } catch (failure: Throwable) {
                                                android.util.Log.w(
                                                    "EverSend",
                                                    "换令牌失败: ${failure.javaClass.simpleName}: ${failure.message}",
                                                )
                                                false
                                            }
                                        }
                                    } else {
                                        false
                                    }
                                    if (refreshed) {
                                        // 换到新令牌后原样重试一次；还失败就抛给外层，
                                        // 外层会写出"回设备列表重新点一下"这类可执行的话。
                                        withContext(Dispatchers.IO) {
                                            uploadToDevice(
                                                context, api, picked.uri,
                                                Pair(picked.name, picked.size), device, store, ui.pin,
                                            )
                                        }
                                        true
                                    } else {
                                        throw problem
                                    }
                                }
                            }
                            try {
                                deliver()
                                delivered++
                                thisFileOk = true
                            } catch (problem: Exception) {
                                val why = problem.message ?: problem.javaClass.simpleName
                                problems += "${device.name}：$why"
                                // 令牌类的失败给一句**能照做**的下一步：电脑端重启会换
                                // 防跨站令牌，这时候"刷新一次连接"就好了（宪章 E3：
                                // 失败必须带可执行的下一步，不能只说"失败了"）。
                                ui.status = if (why.contains("令牌")) {
                                    "发给 ${device.name} 失败：电脑端重启过，令牌已过期 —— " +
                                        "回设备列表点一下「${device.name}」重新连接，再发一次"
                                } else {
                                    "发给 ${device.name} 失败：$why"
                                }
                            }
                        }
                        if (!thisFileOk) notSent += picked
                        done++
                        if (problems.isEmpty()) ui.status = "已处理 $done/${files.size} 个文件"
                    }
                    ui.busy = false
                    // 结果如实说：以前这里**无条件**写「已发给 N 台设备」，
                    // 把上面刚写进去的失败原因盖掉了 —— 手机说发出去了、电脑上什么都没有。
                    // 没发出去的文件留在待发区，用户可以再点一次发送。
                    ui.pending = notSent
                    ui.status = when {
                        problems.isEmpty() ->
                            "已发给 ${chosen.size} 台设备（${chosen.joinToString("、") { it.name }}）"
                        delivered == 0 ->
                            "发送失败：${problems.first()}" +
                                if (problems.size > 1) "（等 ${problems.size} 个问题）" else ""
                        else ->
                            "部分成功：成功 $delivered 次、失败 ${problems.size} 次" +
                                "（${problems.first()}）—— 没发出去的文件还在待发区"
                    }
                    refreshOnce(store, ui)
                }
            }
        }
    }

    val sendText: (String) -> Unit = { text ->
        val active = ui.client
        val item = Queued(
            key = "m-${System.currentTimeMillis()}-${text.hashCode()}",
            conversationId = ui.open?.id.orEmpty(),
            text = text,
        )
        if (active == null) {
            // 还没连上也要"发得出去"：先入队，连上之后由队列的泵自动投。
            ui.outbox = ui.outbox + item.afterFailure("还没连上电脑")
            ui.error = "还没连上电脑：这条消息已放进「待重发」，连上后自动投递"
        } else {
            scope.launch {
                val reason = withContext(Dispatchers.IO) { deliverMessage(active, item) }
                if (reason == null) {
                    refreshOnce(store, ui)
                } else {
                    // 失败必须看得见：进队列 + 一行红字（带原因和下一步）。
                    ui.outbox = ui.outbox + item.afterFailure(reason)
                    ui.error = "消息没发出去：$reason（已放进「待重发」，会自动重试）"
                }
            }
        }
    }

    // 侧栏（两形态共用同一份内容）—— 顺序即 UI_LAYOUT.md 的顺序：
    // 品牌行 → 设备操作 → 设备列表 →（群聊）→ 底部「传输」「设置」。
    // 抽屉形态下，从抽屉里选完东西就把抽屉收起来（用户已经选好了，
    // 抽屉继续挡着会话区反而是干扰）。宽屏常驻侧栏传进来的 close 是空的。
    val sidebar: @Composable (() -> Unit) -> Unit = { close ->
        AppSidebar(
            store = store,
            ui = ui,
            onRefresh = refresh,
            onScan = {
                // 搜索结果（"试过哪几条路"、"搜到几台"）写在设置页上，所以
                // 点「扫描」把主区切到设置页再看：不这么写的话，抽屉一关，
                // 用户根本看不到搜到什么。
                ui.page = MainPage.Settings
                close()
                scope.launch { scanLan(context, store, ui) }
            },
            onManual = { ui.manualHost = store.host; ui.dialog = "manual" },
            onPhone = { ui.dialog = "phone" },
            onSelect = { device ->
                ui.selected = device
                ui.autoOpened = false
                ui.page = MainPage.Chat
                // 点一行 = 选中它，「会话」页立刻切成和它的对话（规格第 2 节）。
                ui.open = conversationFor(device, ui.conversations, ui.devices)
                close()
                refresh()
            },
            onOpenGroup = { group ->
                ui.open = group
                ui.autoOpened = false
                ui.page = MainPage.Chat
                close()
                refresh()
            },
            onTrust = { device, trusted ->
                scope.launch { setTrust(store, ui, device, trusted) }
            },
            onNav = { page ->
                ui.page = page
                close()
            },
            onNewGroup = {
                close()
                scope.launch { startGroupPick(store, ui) }
            },
        )
    }

    BoxWithConstraints(Modifier.fillMaxSize().background(Es.Bg)) {
        // 宽屏 600dp 起：平板、横屏、折叠屏展开态。再窄就放不下 300dp 侧栏 + 会话区。
        val wide = maxWidth >= 600.dp
        if (wide) {
            Row(Modifier.fillMaxSize()) {
                Box(Modifier.width(300.dp).fillMaxHeight().background(Es.Surface)) { sidebar({}) }
                // 发丝级竖线，不是卡片、不圆角（规格第 1 节）。
                Box(Modifier.width(1.dp).fillMaxHeight().background(Es.Border))
                Box(Modifier.weight(1f).fillMaxHeight()) {
                    MainArea(
                        store, ui,
                        onOpenDrawer = null,
                        onSendText = sendText,
                        onPickFiles = { pickFiles.launch("*/*") },
                        onAttach = { pickAttachment.launch("*/*") },
                        onSendFiles = sendPending,
                        onRefresh = refresh,
                    )
                }
            }
        } else {
            val drawer = rememberDrawerState(DrawerValue.Closed)
            // 抽屉开着时按返回键先关抽屉，而不是把整个 App 退出（安卓 14 以下
            // Material3 的抽屉不接管返回键，用户会以为"点了返回就退出"）。
            if (drawer.isOpen) {
                BackHandler { scope.launch { drawer.close() } }
            }
            ModalNavigationDrawer(
                drawerState = drawer,
                drawerContent = {
                    ModalDrawerSheet(
                        modifier = Modifier.width(300.dp),
                        drawerContainerColor = Es.Surface,
                        drawerContentColor = Es.Text,
                    ) { sidebar { scope.launch { drawer.close() } } }
                },
            ) {
                MainArea(
                    store, ui,
                    onOpenDrawer = { scope.launch { drawer.open() } },
                    onSendText = sendText,
                    onPickFiles = { pickFiles.launch("*/*") },
                    onAttach = { pickAttachment.launch("*/*") },
                    onSendFiles = sendPending,
                    onRefresh = refresh,
                )
            }
        }
    }

    // 手动添加电脑：直接填地址，不用等搜索。
    if (ui.dialog == "manual") {
        InfoDialog(
            title = "手动添加电脑",
            onClose = { ui.dialog = "" },
        ) {
            Text("把电脑窗口上「手机访问」那一行的地址填进来。", fontSize = 12.sp, color = Es.Muted)
            OutlinedTextField(
                value = ui.manualHost,
                onValueChange = { ui.manualHost = it },
                singleLine = true,
                placeholder = { Text("例如 192.168.1.5:52119") },
                modifier = Modifier.fillMaxWidth().testTag("manual-host"),
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(
                    onClick = {
                        store.host = ui.manualHost
                        store.via = "手动"
                        ui.dialog = ""
                        ui.status = "正在连接 ${ui.manualHost}…"
                        scope.launch {
                            ui.client = null
                            store.client = null
                            refreshOnce(store, ui)
                        }
                    },
                    modifier = Modifier.testTag("btn-manual-connect"),
                ) { Text("连接") }
                OutlinedButton(onClick = { ui.dialog = "" }) { Text("取消") }
            }
        }
    }

    // 手机连接：电脑端窗口上的二维码是给**浏览器**扫的，App 不需要扫码 ——
    // 这里说清这台手机的身份和已经连上的地址，省得用户在两边找同一个数字。
    if (ui.dialog == "phone") {
        InfoDialog(title = "手机连接", onClose = { ui.dialog = "" }) {
            Text("这台手机", fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
            Text("${store.deviceName} · ${store.deviceId.take(8)}", fontSize = 12.sp, color = Es.Muted)
            Text("本机地址 ${localIpv4().ifBlank { "无网络" }}", fontSize = 12.sp, color = Es.Muted)
            Spacer(Modifier.height(6.dp))
            Text("电脑", fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
            Text(
                store.host.ifBlank { "还没连上：去「设置」填地址，或点「扫描」" },
                fontSize = 12.sp,
                color = Es.Muted,
            )
            Text(
                "电脑端窗口上那个二维码是给手机浏览器扫的（扫开就是网页版）。" +
                    "App 不用扫码：地址填一次就记住了，下次打开自动连。",
                fontSize = 11.sp,
                color = Es.Dim,
            )
        }
    }
}

/** 主区：顶部对方设备详情、中间会话/传输、底部输入区（三段）。 */
@Composable
private fun MainArea(
    store: AppState,
    ui: UiState,
    onOpenDrawer: (() -> Unit)?,
    onSendText: (String) -> Unit,
    onPickFiles: () -> Unit,
    onAttach: () -> Unit,
    onSendFiles: () -> Unit,
    onRefresh: () -> Unit,
) {
    Column(Modifier.fillMaxSize()) {
        PeerHeader(store, ui, onOpenDrawer, onRefresh)
        Box(Modifier.fillMaxWidth().height(1.dp).background(Es.Border))
        Box(Modifier.weight(1f).fillMaxWidth()) {
            when (ui.page) {
                MainPage.Chat -> ChatPane(store, ui, onSendText, onPickFiles, onAttach, onSendFiles)
                MainPage.Transfers -> TransfersPane(store, ui, onRefresh)
                MainPage.Settings -> SettingsPane(store, ui, onRefresh)
            }
        }
    }
}

// ------------------------------------------------------------------ 侧栏

/**
 * 左侧设备栏：顶部设备操作、中部设备列表（含群聊）、底部「传输」+「设置」。
 *
 * 常驻（宽屏）与抽屉（窄屏）用的是**这一个** composable，所以两种形态的内容
 * 顺序完全一致 —— 规格里最硬的一条。
 */
@Composable
private fun AppSidebar(
    store: AppState,
    ui: UiState,
    onRefresh: () -> Unit,
    onScan: () -> Unit,
    onManual: () -> Unit,
    onPhone: () -> Unit,
    onSelect: (DeviceRow) -> Unit,
    onOpenGroup: (Conversation) -> Unit,
    onTrust: (DeviceRow, Boolean) -> Unit,
    onNav: (MainPage) -> Unit,
    onNewGroup: () -> Unit,
) {
    val groups = ui.conversations.filter { it.id.startsWith("g:") }
    val running = ui.transfers.size

    // 设备列表 = 电脑端报的设备 ∪ 自动发现引擎听到的设备（按 identityKey 去重，
    // 一台一行）。少了后半截，还没连上电脑时列表就是空的 —— 用户看到的是
    // "搜不到电脑"，只能自己去输 IP，那正是要修掉的行为。
    val devices = mergeDiscovered(ui.devices, ui.discovered)
    // ③ 设备列表分「在线」「离线」两段（规格第 2 节）：在线默认展开、离线默认折叠。
    val online = devices.filter { it.online }
    val offline = devices.filterNot { it.online }
    var onlineOpen by remember { mutableStateOf(true) }
    var offlineOpen by remember { mutableStateOf(false) }
    // 选中的设备落在折叠着的那一段时，自动把那一段展开。
    // 只跟着**选中项**跑（不把 ui.devices 当键：它每轮轮询都是新对象，那样会
    // 每 2 秒把用户刚收起来的那一段又弹开）。
    LaunchedEffect(ui.selected?.id) {
        val id = ui.selected?.id ?: return@LaunchedEffect
        val device = devices.firstOrNull { it.id == id } ?: return@LaunchedEffect
        if (device.online) onlineOpen = true else offlineOpen = true
    }

    Column(Modifier.fillMaxHeight().fillMaxWidth().background(Es.Surface)) {
        // ① 品牌行：本机名 + 本机地址（让别人能照着手输）。
        Row(
            Modifier.fillMaxWidth().padding(start = 14.dp, end = 14.dp, top = 10.dp, bottom = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text("⚡", fontSize = 20.sp)
            Spacer(Modifier.width(6.dp))
            Text("韧传", fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
            Spacer(Modifier.width(6.dp))
            Text("EverSend", fontSize = 11.sp, color = Es.Dim)
        }
        Column(Modifier.fillMaxWidth().padding(horizontal = 14.dp)) {
            Text(
                "${store.deviceName} · ${localIpv4().ifBlank { "无网络" }}",
                fontSize = 11.sp,
                color = Es.Muted,
                modifier = Modifier.testTag("sidebar-self"),
            )
            Text(
                if (ui.client != null && store.host.isNotBlank()) {
                    "电脑：${ui.selected?.name ?: "已连上"} · ${store.host}"
                } else {
                    // 没连上时也要说清"正在自动找"：用户看到"正在搜索"就不会去手输 IP。
                    val found = ui.discovered.count { it.connectable }
                    if (found > 0) {
                        "已自动发现 $found 台电脑，正在连…"
                    } else {
                        "正在自动搜索同网段的电脑…（不用输地址，点「扫描」可催一次）"
                    }
                },
                fontSize = 11.sp,
                color = if (ui.client != null) Es.Ok else Es.Dim,
                modifier = Modifier.testTag("sidebar-link"),
            )
            // 自动发现的设备**必须被看见**：设备段最多显示 6 行且内部滚动，网络里
            // 设备一多，刚出现的那台就可能落在折叠区外 —— 那和"没搜到"对用户是
            // 同一件事（C13：两台客户端 ≤5 秒互相出现，不是"5 秒后躺在某处"）。
            // 所以这里把最近听到的几台直接写在侧栏顶部，一眼就能看到。
            val fresh = ui.discovered.filterNot { it.stale }
            if (fresh.isNotEmpty()) {
                Text(
                    "自动发现 ${fresh.size} 台：" +
                        fresh.take(3).joinToString("、") { it.name } +
                        (if (fresh.size > 3) " 等" else ""),
                    fontSize = 11.sp,
                    color = Es.Muted,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.testTag("sidebar-discovered"),
                )
            }
        }

        Spacer(Modifier.height(6.dp))

        // ② 设备操作：一行图标按钮。
        // 这里曾经有个整宽的「＋ 发送文件」和输入区的「📎 传输文件」重复，已去掉：
        // 文件是"发给当前会话"的，入口就留在输入区那一行。
        //
        // 「⟳ 刷新」和「◎ 扫描」也合并成了一个「扫描」（宪章 §3.1）：两个按钮做的是
        // 同一件事——让列表里的设备重新跟现实对一遍。搜索本来就是自动的，这个按钮
        // 只是"催一次"；「手动填地址」降级进它右边的 ▾ 里（留一条后路，不占正位）。
        Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp)) {
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(4.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                SidebarAction(
                    icon = "◎",
                    label = if (ui.scanning) "扫描中…" else "扫描",
                    tag = "btn-scan-lan",
                    modifier = Modifier.weight(1f),
                    onClick = onScan,
                )
                var more by remember { mutableStateOf(false) }
                Box {
                    Text(
                        "▾",
                        fontSize = 11.sp,
                        color = Es.Muted,
                        modifier = Modifier
                            .clickable { more = true }
                            .padding(horizontal = 4.dp, vertical = 6.dp)
                            .testTag("btn-scan-more"),
                    )
                    DropdownMenu(expanded = more, onDismissRequest = { more = false }) {
                        DropdownMenuItem(
                            text = { Text("手动输入地址…", fontSize = 13.sp) },
                            onClick = {
                                more = false
                                onManual()
                            },
                        )
                        DropdownMenuItem(
                            text = { Text("连接手机（二维码）…", fontSize = 13.sp) },
                            onClick = {
                                more = false
                                onPhone()
                            },
                        )
                        DropdownMenuItem(
                            text = { Text("刷新设备状态", fontSize = 13.sp) },
                            onClick = {
                                more = false
                                onRefresh()
                            },
                        )
                    }
                }
                SidebarAction("📱", "手机", "btn-phone-link", Modifier.weight(1f), onPhone)
            }
        }
        if (ui.scanning) {
            Text("正在搜索电脑…", fontSize = 11.sp, color = Es.Muted, modifier = Modifier.padding(horizontal = 14.dp, vertical = 4.dp))
        }

        Spacer(Modifier.height(6.dp))
        HorizontalDivider(color = Es.Border)
        Spacer(Modifier.height(6.dp))

        // ③ 设备列表：小节标题 + 数量；下面按在线状态分两段（各自可展开折叠），
        //    每行 = 头像 / 名字 / 状态 / 副标题。
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 14.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            // 小节标题按 UI_LAYOUT.md 第 2 节：写「设备列表」（不是「设备」），
            // 这也是三端统一的用户可见文案。
            Text("设备列表", fontWeight = FontWeight.SemiBold, fontSize = 13.sp)
            Spacer(Modifier.width(6.dp))
            Text(
                "${devices.size}",
                fontSize = 11.sp,
                color = Es.Muted,
                modifier = Modifier.testTag("device-count"),
            )
            Spacer(Modifier.weight(1f))
            Text(
                if (devices.any { it.online }) "有设备在线" else "都在离线",
                fontSize = 11.sp,
                color = Es.Dim,
            )
        }
        // 设备区（在线段 / 离线段 / 群聊段 / 新建群聊）：**只有这一块吃弹性高度**，
        // 条目一律从顶部往下排。于是：
        //   · 折叠的段 = 只有标题行那点高度，下一段（离线 / 群聊）紧跟着上来 ——
        //     段与段之间没有任何 Spacer(weight)，中间不会夹出一大块空白；
        //   · 展开的段按内容定高，封顶 DEVICE_SECTION_MAX（6 行），再多在段内自己滚；
        //   · 这一块吃不完的剩余高度落在**最后**（「＋新建群聊」之下、底部导航之上），
        //     底部「传输 / 设置」永远贴底。
        LazyColumn(Modifier.weight(1f).fillMaxWidth()) {
            // 在线（默认展开）：现在就能收发的设备。
            item(key = "section-online") {
                DeviceSection(
                    title = "在线",
                    count = online.size,
                    expanded = onlineOpen,
                    testTag = "section-online",
                    toggleTag = "section-toggle-在线",
                    // 一段为空时标题仍然在（数量 0），展开后给一句空状态文案。
                    emptyText = "正在自动搜索同网段的电脑：组播 + 广播 + 主动扫描三条通道" +
                        "同时跑，打开 App 就在搜（一般 5 秒内出现）。一直搜不到时点上面的" +
                        "「扫描」再催一次，或看「设置 › 诊断」里各通道的结果。",
                    devices = online,
                    selectedId = ui.selected?.id,
                    onToggle = { onlineOpen = !onlineOpen },
                    onSelect = onSelect,
                    onTrust = onTrust,
                )
            }
            // 离线（默认折叠）：上次见过、现在不在线的设备 —— 点开还进得去历史会话。
            item(key = "section-offline") {
                DeviceSection(
                    title = "离线",
                    count = offline.size,
                    expanded = offlineOpen,
                    testTag = "section-offline",
                    toggleTag = "section-toggle-离线",
                    emptyText = "没有离线的设备：以前连过的设备会留在这段里。",
                    devices = offline,
                    selectedId = ui.selected?.id,
                    onToggle = { offlineOpen = !offlineOpen },
                    onSelect = onSelect,
                    onTrust = onTrust,
                )
            }
            // 群聊不是设备：接在两段设备**下面**，用一条细分隔线分开（规格第 2 节）。
            if (groups.isNotEmpty()) {
                item {
                    Column(Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 6.dp)) {
                        HorizontalDivider(color = Es.Border)
                        Spacer(Modifier.height(6.dp))
                        Text("群聊", fontSize = 11.sp, color = Es.Dim)
                    }
                }
                items(groups, key = { it.id }) { group ->
                    GroupListItem(
                        group = group,
                        selected = ui.open?.id == group.id,
                        onClick = { onOpenGroup(group) },
                    )
                }
            }
            item {
                TextButton(
                    onClick = onNewGroup,
                    modifier = Modifier.padding(horizontal = 6.dp).testTag("btn-new-group"),
                ) { Text("＋ 新建群聊", fontSize = 12.sp) }
            }
        }

        HorizontalDivider(color = Es.Border)
        // 底部栏固定在侧栏底部：列表再长也压不下去（规格第 2 节）。
        Column(Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
            SidebarNavItem(
                icon = "▸",
                label = "传输",
                tag = "nav-transfers",
                selected = ui.page == MainPage.Transfers,
                badge = running,
                onClick = { onNav(MainPage.Transfers) },
            )
            SidebarNavItem(
                icon = "⚙",
                label = "设置",
                tag = "tab-settings",
                selected = ui.page == MainPage.Settings,
                badge = 0,
                onClick = { onNav(MainPage.Settings) },
            )
        }
    }
}

@Composable
private fun SidebarAction(
    icon: String,
    label: String,
    tag: String,
    modifier: Modifier = Modifier,
    onClick: () -> Unit,
) {
    TextButton(
        onClick = onClick,
        modifier = modifier.height(30.dp).testTag(tag),
        contentPadding = PaddingValues(horizontal = 2.dp, vertical = 0.dp),
    ) {
        Text("$icon $label", fontSize = 11.sp, maxLines = 1)
    }
}

@Composable
private fun SidebarNavItem(
    icon: String,
    label: String,
    tag: String,
    selected: Boolean,
    badge: Int,
    onClick: () -> Unit,
) {
    Row(
        Modifier
            .fillMaxWidth()
            .clickable { onClick() }
            .background(if (selected) Es.SurfaceAlt else Color.Transparent)
            .padding(horizontal = 14.dp, vertical = 8.dp)
            .testTag(tag),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(icon, fontSize = 15.sp)
        Spacer(Modifier.width(8.dp))
        Text(label, fontSize = 14.sp, fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal)
        Spacer(Modifier.weight(1f))
        if (badge > 0) {
            // 进行中的数量徽标：传完自动消失（数量来自 /api/state 的 transfers）。
            Box(
                Modifier
                    .background(Es.Accent, CircleShape)
                    .padding(horizontal = 6.dp, vertical = 1.dp),
            ) { Text("$badge", fontSize = 10.sp, color = Es.AccentText) }
        }
    }
}

/**
 * 设备列表里一段（「在线」/「离线」）的标题行（规格第 2 节）：左边 ▾/▸ 指示，
 * 中间标题，右边数量徽标；整行可点 = 这一段整体展开/折叠。
 *
 * 空段也一样显示标题（数量写 0），展开后再给一句空状态文案 —— 标题消失的话
 * 用户会以为"这段功能没了"。
 */
@Composable
private fun DeviceSectionHeader(
    title: String,
    count: Int,
    expanded: Boolean,
    tag: String,
    onToggle: () -> Unit,
) {
    Row(
        Modifier
            .fillMaxWidth()
            .clickable { onToggle() }
            .padding(start = 12.dp, end = 14.dp, top = 6.dp, bottom = 4.dp)
            .testTag(tag),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(if (expanded) "▾" else "▸", fontSize = 10.sp, color = Es.Dim)
        Spacer(Modifier.width(6.dp))
        Text(title, fontSize = 12.sp, color = Es.Muted, fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.weight(1f))
        Box(
            Modifier
                .background(Es.SurfaceAlt, CircleShape)
                .padding(horizontal = 6.dp, vertical = 1.dp),
        ) {
            Text("$count", fontSize = 10.sp, color = Es.Muted, modifier = Modifier.testTag("section-count-$title"))
        }
    }
}

/**
 * 展开的设备段最多这么高：6 行 × 64dp（真机一行量出来是 176px @440dpi = 64dp）。
 *
 * 再多就只在**这一段内部**滚动 —— 否则二十来台设备能把「离线」整段顶出屏幕，
 * 用户还得先把上面那一段滑到底才看得见下面。封顶之后，一段最多占 6 行的高度。
 */
private val DEVICE_SECTION_MAX = 384.dp

/**
 * 设备列表里**一整段**（「在线」/「离线」）：标题行 + （展开了才有的）设备行。
 *
 * 折叠时整段就只有标题行的高度，一个像素的弹性高度都不占 —— 下一段（离线，
 * 或没有离线设备时的群聊）立刻贴上来，不会中间空出一大块。展开时按内容定高，
 * 上限 [DEVICE_SECTION_MAX]，超过就在这一段里自己滚，不影响别的段的位置。
 */
@Composable
private fun DeviceSection(
    title: String,
    count: Int,
    expanded: Boolean,
    testTag: String,
    toggleTag: String,
    emptyText: String,
    devices: List<DeviceRow>,
    selectedId: String?,
    onToggle: () -> Unit,
    onSelect: (DeviceRow) -> Unit,
    onTrust: (DeviceRow, Boolean) -> Unit,
) {
    Column(Modifier.fillMaxWidth().testTag(testTag)) {
        DeviceSectionHeader(
            title = title,
            count = count,
            expanded = expanded,
            tag = toggleTag,
            onToggle = onToggle,
        )
        if (expanded) {
            if (devices.isEmpty()) {
                // 空段只加一句话，标题与数量（0）仍然在。
                SectionEmptyText(emptyText)
            } else {
                Column(
                    Modifier
                        .fillMaxWidth()
                        .heightIn(max = DEVICE_SECTION_MAX)
                        .verticalScroll(rememberScrollState()),
                ) {
                    devices.forEach { device ->
                        DeviceListItem(
                            device = device,
                            selected = selectedId == device.id,
                            onClick = { onSelect(device) },
                            onTrust = { trusted -> onTrust(device, trusted) },
                        )
                    }
                }
            }
        }
    }
}

/** 某一段展开后为空时的一句话（标题与数量徽标仍然在）。 */
@Composable
private fun SectionEmptyText(text: String) {
    Text(
        text,
        fontSize = 11.sp,
        color = Es.Dim,
        modifier = Modifier.padding(start = 24.dp, end = 14.dp, bottom = 6.dp),
    )
}

/** 设备行：头像 / 名字 / 状态徽标 / 副标题（地址 · 端口 · 发现方式）。 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun DeviceListItem(
    device: DeviceRow,
    selected: Boolean,
    onClick: () -> Unit,
    onTrust: (Boolean) -> Unit,
) {
    var menu by remember { mutableStateOf(false) }
    val context = LocalContext.current
    val clipboard = LocalClipboardManager.current

    Box {
        Row(
            Modifier
                .fillMaxWidth()
                .background(if (selected) Es.SurfaceAlt else Color.Transparent)
                .combinedClickable(onClick = onClick, onLongClick = { menu = true })
                .padding(horizontal = 14.dp, vertical = 8.dp)
                .testTag("device-${device.name}"),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(
                Modifier
                    .size(30.dp)
                    .background(Es.SurfaceAlt, CircleShape),
                contentAlignment = Alignment.Center,
            ) { Text(avatarOf(device), fontSize = 15.sp) }
            Spacer(Modifier.width(10.dp))
            Column(Modifier.weight(1f)) {
                Text(
                    device.name.ifBlank { device.id.take(8) },
                    fontSize = 14.sp,
                    fontWeight = FontWeight.SemiBold,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                Text(
                    "${device.address.ifBlank { device.kind }} · ${sourceLabel(device.source)}",
                    fontSize = 11.sp,
                    color = Es.Dim,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            Text(
                statusText(device),
                fontSize = 10.sp,
                color = if (device.online) Es.Ok else Es.Dim,
                modifier = Modifier.testTag("status-${device.name}"),
            )
        }
        // 长按出菜单（规格第 2 节：右键/长按出「忘记这台设备」等菜单）：
        // 信任开关与复制地址是这台手机上真的做得到的两件事。
        //
        // 第一项是**完整名字**（宪章 C4：名字按字素省略 + 长按显示全名）。手机上
        // 没有"悬停"，长按就是悬停的等价物；这一项同时把全名复制到剪贴板，长名字
        // 也能整条拿走。
        DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
            DropdownMenuItem(
                text = {
                    Column {
                        Text("完整名字", fontSize = 10.sp, color = Es.Dim)
                        Text(
                            device.name.ifBlank { device.id },
                            fontSize = 13.sp,
                            maxLines = 3,
                            overflow = TextOverflow.Ellipsis,
                            modifier = Modifier.testTag("device-full-name"),
                        )
                    }
                },
                onClick = {
                    menu = false
                    clipboard.setText(AnnotatedString(device.name.ifBlank { device.id }))
                    android.widget.Toast.makeText(context, "已复制名字", android.widget.Toast.LENGTH_SHORT).show()
                },
            )
            DropdownMenuItem(
                text = { Text(if (device.trusted) "取消信任这台设备" else "信任这台设备") },
                onClick = {
                    menu = false
                    onTrust(!device.trusted)
                },
            )
            DropdownMenuItem(
                text = { Text("复制地址") },
                onClick = {
                    menu = false
                    clipboard.setText(AnnotatedString(device.address))
                    android.widget.Toast.makeText(context, "已复制 ${device.address}", android.widget.Toast.LENGTH_SHORT).show()
                },
            )
        }
    }
}

@Composable
private fun GroupListItem(group: Conversation, selected: Boolean, onClick: () -> Unit) {
    Row(
        Modifier
            .fillMaxWidth()
            .background(if (selected) Es.SurfaceAlt else Color.Transparent)
            .clickable { onClick() }
            .padding(horizontal = 14.dp, vertical = 8.dp)
            .testTag("group-${group.id}"),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            Modifier.size(30.dp).background(Es.SurfaceAlt, CircleShape),
            contentAlignment = Alignment.Center,
        ) { Text("👥", fontSize = 15.sp) }
        Spacer(Modifier.width(10.dp))
        Column(Modifier.weight(1f)) {
            Text(
                group.displayTitle.ifBlank { group.title.ifBlank { "群聊" } },
                fontSize = 14.sp,
                fontWeight = FontWeight.SemiBold,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Text(
                group.lastText.ifBlank { "群聊 · ${group.members.size} 人" },
                fontSize = 11.sp,
                color = Es.Dim,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
        if (group.unread > 0) {
            Box(Modifier.background(Es.Accent, CircleShape).padding(horizontal = 6.dp, vertical = 1.dp)) {
                Text("${group.unread}", fontSize = 10.sp, color = Es.AccentText)
            }
        }
    }
}

// ------------------------------------------------------------- 主区：顶部

/**
 * ④ 会话顶部：对方设备详细信息 + `[会话｜传输]` 分段 + 操作按钮。
 *
 * 规格要求这里必须有：名字、平台、设备 id 前 8 位、地址 + 端口、发现方式、
 * 在线状态、是否已信任、是否需要 PIN。没有选中设备时给占位文案，但分段控件
 * 与按钮仍在 —— 保证「传输」页永远进得去。
 */
@Composable
private fun PeerHeader(
    store: AppState,
    ui: UiState,
    onOpenDrawer: (() -> Unit)?,
    onRefresh: () -> Unit,
) {
    val device = ui.selected
    val group = ui.open?.takeIf { it.id.startsWith("g:") }
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    val title = when {
        device != null -> device.name.ifBlank { device.id.take(8) }
        group != null -> "👥 " + group.displayTitle.ifBlank { group.title.ifBlank { "群聊" } }
        else -> "从左边选一台设备"
    }
    val detail = when {
        // 只留"用户看得懂、且有用"的三件事：是什么系统、在哪儿、怎么发现的。
        // 设备号前 8 位（e4233004）对用户没有任何意义，却把这一行挤到截断 ——
        // 真机上「发现方式：组播」正好被省略号吃掉，用户看不到它是怎么连上的。
        device != null -> listOf(
            device.platform.ifBlank { device.kind },
            device.address.ifBlank { "地址未知" },
            "发现方式：" + sourceLabel(device.source),
        ).joinToString(" · ")
        group != null -> "群聊 · ${group.members.size} 人 · ${group.members.joinToString("、") { it.take(8) }}"
        else -> "选中一台设备后，这里显示它的平台、设备号、地址与在线状态"
    }

    Column(Modifier.fillMaxWidth().background(Es.Surface).padding(horizontal = 12.dp, vertical = 8.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            if (onOpenDrawer != null) {
                // 窄屏：左上角 ☰ 打开设备抽屉（规格第 1 节）。
                IconButton(onClick = onOpenDrawer, modifier = Modifier.testTag("btn-drawer")) {
                    Icon(Icons.Filled.Menu, "设备栏")
                }
            }
            Box(
                Modifier.size(38.dp).background(Es.SurfaceAlt, CircleShape),
                contentAlignment = Alignment.Center,
            ) { Text(avatarOf(device), fontSize = 18.sp) }
            Spacer(Modifier.width(10.dp))
            Column(Modifier.weight(1f)) {
                Text(
                    title,
                    fontSize = 16.sp,
                    fontWeight = FontWeight.SemiBold,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.testTag("peer-name"),
                )
                Text(
                    detail,
                    fontSize = 10.sp,
                    color = Es.Dim,
                    // 允许折两行：1080 宽的机器上「系统 · 地址 · 发现方式」一行放不下，
                    // 真机实测尾部被省略号吃掉（用户看不到"怎么发现的"）。两行够用，
                    // 再多就该换行到详情区了。
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.testTag("peer-detail"),
                )
                if (device != null || group != null) {
                    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        Badge(
                            text = if (device == null) "群聊" else if (device.online) "在线" else offlineText(device),
                            color = if (device == null || device.online) Es.Ok else Es.Dim,
                        )
                        if (device != null) {
                            Badge(if (device.trusted) "已信任" else "未信任", if (device.trusted) Es.Ok else Es.Dim)
                            if (ui.pinNeeded) Badge("需要 PIN", Es.Warn)
                        }
                    }
                }
            }
            // `[会话｜传输]` 分段控件在这里被删掉了（宪章 §3.1 / C1）。
            //
            // 它和侧栏底部的「传输」是**同一个意图的两个入口**，而规格写得很明确：
            // 保留侧栏底部导航为唯一入口。选中设备 = 进入该设备的会话；传输页里给
            // 「← 会话」回来。桌面端和网页版都已经这么做了，安卓是最后一处。
        }
        Row(
            Modifier.fillMaxWidth().padding(top = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            // 这一行只留**一个**主按钮（§3.1）：其余（刷新 / 清空记录 / 清空会话）
            // 全部收进右边的 ⋯ 溢出菜单 —— 以前四个平级按钮并排，用户得先读懂
            // 哪个是"安全"的、哪个会删东西。
            TextButton(onClick = { openDownloadsFolder(context) }) {
                Text("打开接收文件夹", fontSize = 11.sp, modifier = Modifier.testTag("btn-open-folder"))
            }
            var more by remember { mutableStateOf(false) }
            Box {
                Text(
                    "⋯",
                    fontSize = 15.sp,
                    color = Es.Muted,
                    modifier = Modifier
                        .clickable { more = true }
                        .padding(horizontal = 8.dp, vertical = 4.dp)
                        .testTag("btn-header-more"),
                )
                DropdownMenu(expanded = more, onDismissRequest = { more = false }) {
                    DropdownMenuItem(
                        text = { Text("刷新", fontSize = 13.sp) },
                        onClick = {
                            more = false
                            onRefresh()
                        },
                    )
                    if (ui.page == MainPage.Transfers) {
                        DropdownMenuItem(
                            text = { Text("清空记录（只清这张表）", fontSize = 13.sp) },
                            onClick = {
                                more = false
                                ui.hiddenUploads =
                                    (ui.uploads + ui.recentUploads).map { it.optString("id") }.toSet()
                            },
                            modifier = Modifier.testTag("btn-clear-records"),
                        )
                    } else {
                        DropdownMenuItem(
                            text = { Text("清空会话（只清这一段显示）", fontSize = 13.sp) },
                            onClick = {
                                more = false
                                ui.clearedMessages = true
                            },
                            modifier = Modifier.testTag("btn-clear-chat"),
                        )
                    }
                }
            }
            Spacer(Modifier.weight(1f))
            if (ui.status.isNotBlank()) {
                Text(
                    ui.status,
                    fontSize = 10.sp,
                    color = Es.Muted,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.testTag("header-status"),
                )
            }
        }
        if (ui.error.isNotBlank()) {
            Text(ui.error, fontSize = 10.sp, color = Es.Danger, maxLines = 2, modifier = Modifier.testTag("root-error"))
        }
    }
}

@Composable
private fun Badge(text: String, color: Color) {
    Box(
        Modifier
            .background(Es.SurfaceAlt, RoundedCornerShape(6.dp))
            .padding(horizontal = 5.dp, vertical = 1.dp),
    ) { Text(text, fontSize = 9.sp, color = color) }
}

/** 分段控件：`[会话｜传输]`、`[接收｜发送]` 都用它，当前项高亮。 */
@Composable
private fun SegmentControl(
    options: List<Pair<String, String>>,
    current: String,
    tagOf: (String) -> String,
    onPick: (String) -> Unit,
) {
    Row(
        Modifier
            .background(Es.SurfaceAlt, RoundedCornerShape(8.dp))
            .padding(2.dp),
        horizontalArrangement = Arrangement.spacedBy(2.dp),
    ) {
        options.forEach { (key, label) ->
            val active = key == current
            Box(
                Modifier
                    .background(if (active) Es.Accent else Color.Transparent, RoundedCornerShape(6.dp))
                    .clickable { onPick(key) }
                    .padding(horizontal = 10.dp, vertical = 5.dp)
                    .testTag(tagOf(key)),
            ) {
                Text(
                    label,
                    fontSize = 12.sp,
                    color = if (active) Es.AccentText else Es.Muted,
                    fontWeight = if (active) FontWeight.SemiBold else FontWeight.Normal,
                )
            }
        }
    }
}

// ------------------------------------------------------------- 主区：会话

@Composable
private fun ChatPane(
    store: AppState,
    ui: UiState,
    onSendText: (String) -> Unit,
    onPickFiles: () -> Unit,
    onAttach: () -> Unit,
    onSendFiles: () -> Unit,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val listState = rememberLazyListState()
    // 下载队列（进程内单例）：会话里那条汇总与每行的进度都读它。
    val fetcher = remember { FetchManager.get(context) }
    val current = ui.open

    // 会话页：没有打开任何会话时显示会话列表（和电脑的那个会话就在这里）。
    if (ui.pickingGroup) {
        GroupPicker(store, ui)
        return
    }
    if (current == null) {
        Column(Modifier.fillMaxSize().padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("会话", fontWeight = FontWeight.SemiBold, fontSize = 16.sp, modifier = Modifier.testTag("chat-title"))
                Spacer(Modifier.width(8.dp))
                Text("${ui.conversations.size} 个", fontSize = 11.sp, color = Es.Muted)
            }
            if (ui.pending.isNotEmpty()) {
                PendingBar(ui, onSendFiles)
            }
            if (ui.conversations.isEmpty()) {
                Text(
                    "还没有会话。点下面的「和电脑聊天」打一声招呼就会建好，" +
                        "然后就能发文字、表情、图片、视频、文件、语音。",
                    fontSize = 12.sp,
                    color = Es.Muted,
                    modifier = Modifier.padding(vertical = 8.dp),
                )
            }
            LazyColumn(Modifier.weight(1f)) {
                items(ui.conversations, key = { it.id }) { conversation ->
                    val isGroup = conversation.id.startsWith("g:")
                    Card(
                        Modifier
                            .fillMaxWidth()
                            .padding(vertical = 4.dp)
                            .clickable {
                                ui.open = conversation
                                ui.autoOpened = false
                                scope.launch { refreshOnce(store, ui) }
                            },
                        colors = CardDefaults.cardColors(containerColor = Es.SurfaceAlt),
                    ) {
                        Column(Modifier.padding(12.dp)) {
                            Text(
                                (if (isGroup) "👥 " else "") +
                                    readableTitle(conversation.displayTitle.ifBlank { conversation.title }, ui.members),
                                fontWeight = FontWeight.SemiBold,
                                fontSize = 14.sp,
                            )
                            Text(conversation.lastText, fontSize = 11.sp, color = Es.Muted, maxLines = 1, overflow = TextOverflow.Ellipsis)
                        }
                    }
                }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(
                    onClick = {
                        scope.launch {
                            val active = ui.client ?: withContext(Dispatchers.IO) { connectSession(store, ui) }
                            if (active != null) {
                                withContext(Dispatchers.IO) {
                                    try {
                                        active.postJson("/api/chat/send", JSONObject().put("text", "你好 👋"))
                                    } catch (problem: Exception) {
                                        ui.status = "发送失败：${problem.message ?: problem.javaClass.simpleName}"
                                    }
                                }
                                refreshOnce(store, ui)
                            }
                        }
                    },
                    modifier = Modifier.weight(1f).testTag("btn-new-chat"),
                ) { Text("和电脑聊天") }

                OutlinedButton(
                    onClick = { scope.launch { startGroupPick(store, ui) } },
                    modifier = Modifier.weight(1f).testTag("btn-new-group-2"),
                ) { Text("建群聊") }
            }
        }
        return
    }

    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                (if (current.id.startsWith("g:")) "👥 " else "") +
                    readableTitle(current.displayTitle.ifBlank { current.title }, ui.members),
                fontWeight = FontWeight.SemiBold,
                fontSize = 15.sp,
                modifier = Modifier.testTag("chat-title"),
            )
            Spacer(Modifier.width(8.dp))
            // 规范显示名称：谁在群里、各自是什么系统、什么版本、什么 IP。
            Text(
                chatMembersLabel(ui.members, current, store),
                fontSize = 10.sp,
                color = Es.Dim,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f).testTag("chat-members"),
            )
            TextButton(
                onClick = {
                    ui.open = null
                    ui.autoOpened = false
                },
            ) { Text("← 会话", fontSize = 11.sp) }
        }
        HorizontalDivider(color = Es.Border)
        if (ui.clearedMessages) {
            Column(
                Modifier.weight(1f).fillMaxWidth().padding(12.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.Center,
            ) {
                Text("会话已从手机上清空显示", fontSize = 13.sp)
                Text(
                    "记录还在电脑上（手机不删电脑上的聊天）。点「重新载入」再拉回来。",
                    fontSize = 11.sp,
                    color = Es.Muted,
                )
                TextButton(onClick = { ui.clearedMessages = false }) { Text("重新载入") }
            }
        } else {
            // 正常顺序（老的在上、新的在下）：真机测试按"最后一个「保存」"点，
            // 顺序反过来会让它点到最老的那条气泡 —— 顺序别动。
            //
            // 列表里除了消息，还有"传输文件"那条记录（见 [conversationEntries]）：
            // 用户在会话里就能看到"我给谁发过什么、传到哪了"，和传输页是同一份账。
            val entries = conversationEntries(ui, store)
            // 电脑刚发来的、还没取走的文件：会话里给**一条明确的记录**
            // （"电脑发来 N 个文件 · 点击下载"），而不是什么都不显示。
            val pendingShares = ui.shares.filterNot { it.optBoolean("downloaded", false) }
            LazyColumn(Modifier.weight(1f).fillMaxWidth().padding(horizontal = 12.dp), state = listState) {
                if (pendingShares.isNotEmpty()) {
                    item(key = "shares-summary") {
                        SharesSummaryCard(
                            ui = ui,
                            metered = fetcher.meteredNetwork(),
                            onFetchAll = {
                                val active = ui.client ?: return@SharesSummaryCard
                                pendingShares.forEach { share ->
                                    val id = share.optString("id")
                                    if (id.isBlank()) return@forEach
                                    fetcher.fetchNow(
                                        active, id,
                                        share.optString("name").ifBlank { "文件" },
                                        share.optLong("size"),
                                    )
                                }
                                ui.status = "正在接收电脑发来的 ${pendingShares.size} 个文件…"
                            },
                            onRetry = { task ->
                                ui.client?.let { active ->
                                    fetcher.fetchNow(active, task.shareId, task.name, task.size)
                                }
                            },
                        )
                    }
                }
                items(entries, key = { it.key }) { entry ->
                    Bubble(
                        entry.message,
                        ui.client,
                        onError = { ui.status = it },
                        // 下载中的那些文件也当成"接收中的传输"喂进去：气泡上的
                        // 「接收中 42% + 进度条」于是对手机自己的下载同样成立。
                        transfers = ui.transfers + fetchTransfers(ui.fetches),
                        // 本机下载的真实进度/速度/完成态（电脑发给手机的文件只有这里有）。
                        fetches = ui.fetches,
                        target = entry.target,
                        actions = !entry.transferNote,
                        onStatus = { ui.status = it },
                        onResend = { file ->
                            resendTarget(
                                scope, context, ui.client, file, resendDeviceOf(ui), store, ui.pin,
                                onStatus = { ui.status = it },
                                onDone = { scope.launch { refreshOnce(store, ui) } },
                            )
                        },
                        onChanged = { scope.launch { refreshOnce(store, ui) } },
                    )
                }
            }
            LaunchedEffect(entries.size) {
                if (entries.isNotEmpty()) listState.animateScrollToItem(entries.size - 1)
            }
        }
        // 待重发队列：发不出去的消息在这里**看得见**（红点 + 原因 + 重试/放弃），
        // 而不是只闪一行状态字（宪章 §5.5 / C14："发送消息失败不显示"就是这么修的）。
        if (ui.outbox.isNotEmpty()) {
            PendingMessages(
                items = ui.outbox,
                onRetry = { item ->
                    ui.outbox = ui.outbox.map { if (it.key == item.key) it.retryNow() else it }
                    ui.error = ""
                    ui.status = "正在重发：${item.text.take(20)}"
                },
                onDrop = { item ->
                    ui.outbox = ui.outbox.filterNot { it.key == item.key }
                    ui.status = "已放弃这条消息（不会重发）"
                    ui.error = ""
                },
            )
        }
        // ⑥ 输入区：只在会话页出现（规格第 3 节）。
        Composer(
            context = context,
            client = ui.client,
            pending = ui.pending,
            // 目标设备也包含"刚自动发现、还没连上"的那些：点它就能发。
            targets = mergeDiscovered(ui.devices, ui.discovered),
            chosen = ui.targets,
            pin = ui.pin,
            pinNeeded = ui.pinNeeded,
            status = ui.status,
            onPinChange = { ui.pin = it },
            onToggleTarget = { device ->
                ui.targets = if (ui.targets.contains(device.id)) ui.targets - device.id else ui.targets + device.id
            },
            onRemovePending = { picked -> ui.pending = ui.pending.filterNot { it.uri == picked.uri } },
            onPickFiles = onPickFiles,
            onAttach = onAttach,
            onSendFiles = onSendFiles,
            onSend = onSendText,
            onUploaded = { scope.launch { refreshOnce(store, ui) } },
            onError = { ui.status = it },
        )
    }
}

/** 新建群聊：先选成员（电脑 + 已配对的手机），再建一个 g: 会话。 */
@Composable
private fun GroupPicker(store: AppState, ui: UiState) {
    val scope = rememberCoroutineScope()
    Column(Modifier.fillMaxSize().padding(12.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = { ui.pickingGroup = false }) { Icon(Icons.Filled.ArrowBack, "返回") }
            Text("新建群聊", fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
        }
        OutlinedTextField(
            value = ui.groupTitle,
            onValueChange = { ui.groupTitle = it },
            singleLine = true,
            placeholder = { Text("群名称（可留空）") },
            modifier = Modifier.fillMaxWidth().testTag("group-title"),
        )
        Text("选成员（可以多选）", fontSize = 12.sp, color = Es.Muted)
        LazyColumn(Modifier.weight(1f)) {
            items(ui.members, key = { it.id }) { member ->
                val picked = ui.chosen.contains(member.id)
                Card(
                    Modifier
                        .fillMaxWidth()
                        .padding(vertical = 4.dp)
                        .clickable {
                            ui.chosen = if (picked) ui.chosen - member.id else ui.chosen + member.id
                        }
                        .testTag("member-${member.name}"),
                    colors = CardDefaults.cardColors(containerColor = Es.SurfaceAlt),
                ) {
                    Column(Modifier.padding(12.dp)) {
                        Text((if (picked) "☑ " else "☐ ") + member.name, fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
                        Text("${member.kind} · ${statusText(member)}", fontSize = 11.sp, color = Es.Muted)
                        if (member.facts.isNotBlank()) Text(member.facts, fontSize = 10.sp, color = Es.Dim)
                    }
                }
            }
        }
        Button(
            enabled = ui.chosen.isNotEmpty(),
            modifier = Modifier.fillMaxWidth().testTag("btn-create-group"),
            onClick = {
                val active = ui.client ?: return@Button
                val picked = ui.members.filter { ui.chosen.contains(it.id) }
                scope.launch {
                    withContext(Dispatchers.IO) {
                        try {
                            // 群 id 用 g: 开头，和电脑端、网页版一致；成员是
                            // 各自在电脑端登记的身份（电脑是设备号，手机是 web:<key>）。
                            val payload = JSONObject()
                                .put("conv", "g:" + java.util.UUID.randomUUID().toString().replace("-", "").take(16))
                                .put("title", ui.groupTitle.ifBlank { "群聊" })
                                .put("members", org.json.JSONArray().apply { picked.forEach { put(it.id) } })
                                .put("kind", "text")
                                .put("text", "群聊建好了 👋")
                            val response = active.postJson("/api/chat/send", payload)
                            val created = response.optString("conversationId").ifBlank { payload.optString("conv") }
                            val state = active.getJson("/api/state")
                            val list = state.optJSONObject("chat")?.optJSONArray("conversations")
                            val conversations = (0 until (list?.length() ?: 0))
                                .mapNotNull { list?.optJSONObject(it)?.toConversation() }
                            withContext(Dispatchers.Main) {
                                ui.pickingGroup = false
                                ui.chosen = emptySet()
                                ui.groupTitle = ""
                                ui.conversations = conversations
                                ui.open = conversations.firstOrNull { it.id == created }
                                ui.autoOpened = false
                            }
                        } catch (problem: Exception) {
                            ui.status = "建群失败：${problem.message ?: problem.javaClass.simpleName}"
                        }
                    }
                }
            },
        ) { Text("创建群聊（已选 ${ui.chosen.size}）") }
    }
}

// ------------------------------------------------------------- 主区：传输

/**
 * ⑤ 传输页：顶部 `[接收｜发送]` 分段，下面一张表格；两张表**共用同一块区域**，
 * 点分段就地切换（不跳页、不弹窗）。页头右侧是「打开接收文件夹」「清空记录」。
 */
@Composable
private fun TransfersPane(store: AppState, ui: UiState, onRefresh: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val receive = ui.tab == TransferTab.Receive
    // §6：接收表历史行上的「预览」也开同一个弹层（只对"文件还在电脑上、路也还在"
    // 的行给入口：电脑交给手机的、以及电脑接收目录里的文件）。
    var previewItem by remember { mutableStateOf<PreviewItem?>(null) }

    // 一个文件一行：同一份文件的多条记录在这里归并（隐藏掉被顶掉的那些）。
    val rows = if (receive) {
        collapseTableRows(receiveRows(context, store, ui, scope, onRefresh) { previewItem = it }.toMutableList())
    } else {
        collapseTableRows(sendRows(store, ui, scope, onRefresh).toMutableList())
    }
    // 收起来的那一行仍然算在列表里（下标不动、key 不变），只是不画；筛选也只看画出来的。
    val visible = rows.filterNot { it.hidden }
    val shown = filterRows(visible, ui.filter)

    // 空状态里的主按钮要能"去发送一个文件"：这里自带一个选择器（选完落到待发方块里，
    // 和输入区那条路是同一份状态，不会出现"两个待发列表"）。
    val picker = rememberLauncherForActivityResult(
        ActivityResultContracts.GetMultipleContents()
    ) { uris: List<Uri> ->
        if (uris.isEmpty()) return@rememberLauncherForActivityResult
        val added = uris.map { uri ->
            val info = queryFile(context, uri)
            Picked(uri, info.first, info.second)
        }
        ui.pending = (ui.pending + added).distinctBy { it.uri.toString() }
        ui.localFiles = ui.localFiles + added.associate { rowIdentity(it.name, it.size) to it.uri }
        if (ui.targets.isEmpty()) {
            ui.selected?.let { ui.targets = setOf(it.id) } ?: run {
                ui.devices.firstOrNull()?.let { ui.targets = setOf(it.id) }
            }
        }
        ui.status = "已选 ${ui.pending.size} 个文件，回到「会话」点「发送」"
    }

    Column(Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                // 传输页的返回入口（宪章 §3.1）：主区顶部的 `[会话｜传输]` 分段删掉
                // 之后，"点设备名回到会话"这条路由要留下来 —— 窄屏上抽屉是关着的，
                // 没有这个按钮用户会以为"进了传输页就回不去了"。
                TextButton(
                    onClick = { ui.page = MainPage.Chat },
                    modifier = Modifier.testTag("btn-back-chat-from-transfers"),
                ) { Text("← 会话", fontSize = 11.sp) }
                Spacer(Modifier.width(4.dp))
                SegmentControl(
                    options = listOf("receive" to "接收", "send" to "发送"),
                    current = if (receive) "receive" else "send",
                    tagOf = { if (it == "receive") "tab-receive" else "tab-send" },
                    onPick = { key -> ui.tab = if (key == "receive") TransferTab.Receive else TransferTab.Send },
                )
                Spacer(Modifier.width(8.dp))
                Text(
                    ui.selected?.name ?: "所有设备",
                    fontSize = 10.sp,
                    color = Es.Dim,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            // 筛选药丸（宪章 §3.2）：全部 / 进行中 / 已完成 / 失败。数字是"这一档有几行"，
            // 所以在界面上不用去数表格。空档也要显示（写 0），否则用户以为功能没了。
            Row(
                Modifier.fillMaxWidth().padding(top = 6.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                TransferFilter.entries.forEach { option ->
                    val count = visible.count { matchesFilter(it, option) }
                    val active = ui.filter == option
                    Text(
                        "${option.label} $count",
                        fontSize = 11.sp,
                        color = if (active) Es.AccentText else Es.Muted,
                        modifier = Modifier
                            .background(if (active) Es.Accent else Es.SurfaceAlt, RoundedCornerShape(999.dp))
                            .clickable {
                                ui.filter = option
                                // 切筛选时顺手把状态行清掉：那句话说的是上一次动作，
                                // 留着会让人以为筛选失败了。
                                ui.status = ""
                            }
                            .padding(horizontal = 10.dp, vertical = 4.dp)
                            .testTag(option.tag),
                    )
                }
            }
            Text(
                if (receive) {
                    "手机收到的东西都存进系统「下载」；电脑的接收目录：${ui.receiveDir.ifBlank { "—" }}"
                } else {
                    "发给谁就点设备栏里的那台设备；可以多台一起发（输入区上面能勾）"
                },
                fontSize = 10.sp,
                color = Es.Dim,
                modifier = Modifier.padding(top = 4.dp),
            )
        }
        HorizontalDivider(color = Es.Border)

        // 表头固定，行可滚动。
        TransferTableHeader(receive)
        HorizontalDivider(color = Es.Border)

        if (shown.isEmpty()) {
            // 空状态（宪章 §3.2）：居中图标 + 一句话 + 主按钮。以前只有一行灰字，
            // "筛出来是空的"和"真的没传过"看起来一模一样。
            Column(
                Modifier.weight(1f).fillMaxWidth().padding(24.dp).testTag("table-empty"),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.Center,
            ) {
                Text(if (receive) "📥" else "📤", fontSize = 34.sp)
                Spacer(Modifier.height(8.dp))
                Text(
                    if (visible.isEmpty()) {
                        if (receive) "还没有收到文件" else "还没有发送过文件"
                    } else {
                        "「${ui.filter.label}」里没有记录"
                    },
                    fontSize = 13.sp,
                )
                Text(
                    if (visible.isEmpty()) {
                        if (receive) "别人发给你、或电脑上收到的文件会出现在这里" else "点下面的按钮选一个文件发给电脑"
                    } else {
                        "还有 ${visible.size} 条记录在别的筛选项里"
                    },
                    fontSize = 11.sp,
                    color = Es.Muted,
                    modifier = Modifier.padding(top = 2.dp),
                )
                Spacer(Modifier.height(10.dp))
                if (visible.isEmpty()) {
                    Button(
                        onClick = { if (receive) onRefresh() else picker.launch("*/*") },
                        modifier = Modifier.testTag("empty-primary"),
                    ) { Text(if (receive) "刷新一下" else "去发送一个文件") }
                } else {
                    OutlinedButton(
                        onClick = { ui.filter = TransferFilter.All },
                        modifier = Modifier.testTag("empty-primary"),
                    ) { Text("显示全部") }
                }
            }
        }
        LazyColumn(Modifier.weight(1f).fillMaxWidth()) {
            items(shown, key = { it.key }) { row ->
                TransferTableRow(
                    row = row,
                    receive = receive,
                    client = ui.client,
                    onPreviewItem = { previewItem = it },
                    onResend = { file ->
                        resendTarget(
                            scope, context, ui.client, file, resendDeviceOf(ui), store, ui.pin,
                            onStatus = { ui.status = it },
                            onDone = onRefresh,
                        )
                    },
                    onChanged = onRefresh,
                )
            }
        }
        if (ui.status.isNotBlank()) {
            HorizontalDivider(color = Es.Border)
            Text(
                ui.status,
                fontSize = 11.sp,
                color = Es.Muted,
                modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp)
                    .testTag(if (receive) "receive-status" else "send-status"),
            )
        }
    }
    previewItem?.let { item ->
        FilePreviewDialog(item = item, client = ui.client, onClose = { previewItem = null })
    }
}

/** 一行传输记录（两张表共用；哪几列有值由 [receive] 决定）。 */
data class TableRow(
    val key: String,
    val name: String,
    val peer: String,
    val size: Long,
    val time: String,
    val status: String,
    val progress: Float? = null,
    val extra: String = "",
    val action: String = "",
    val onAction: (() -> Unit)? = null,
    val second: String = "",
    val onSecond: (() -> Unit)? = null,
    /** §6：这一行能不能"先看一眼"（拿得到字节的行才有）。 */
    val onPreview: (() -> Unit)? = null,
    /**
     * 这一行代表哪一份文件（同名同大小 = 同一份）。空串 = 不参与合并
     * （比如"待确认"的 offer：它是一次还没开始的传输，得留给用户按接收/拒绝）。
     */
    val identity: String = "",
    /**
     * 这一行"落地"到什么程度，合并同名的行时大的留下：
     * 2 = 已完成（文件已经在了）、1 = 等着动作（待保存/待确认）、0 = 正在传。
     */
    val rank: Int = 0,
    /** 同一 rank 下谁留下（大的留下）：手机自己那条 > 电脑接收目录那条 > 正在传。 */
    val priority: Int = 0,
    /** 长按出来的四个动作围着这份文件转（预览/打开/打开所在文件夹/另存为）。 */
    val target: FileTarget? = null,
) {
    /**
     * 被同一份文件的另一行顶掉了 —— **收起来，不删除**。
     *
     * 和电脑端 `TransferTableRow.hide()` 是同一条规矩：删除会让下面所有行的下标
     * 上移，正在跑的传输是按行持有句柄的，一次"去重"就能让它开始更新别人的行。
     * 这里行仍然在列表里（[TransferTablePane] 渲染成一个零高度的占位），只是看不见。
     */
    var hidden: Boolean = false
}

/** 表里一行的文件身份：同名同大小算同一份（大小不知道时只按名字）。 */
fun rowIdentity(name: String, size: Long): String {
    val clean = name.trim()
    if (clean.isEmpty()) return ""
    return clean + "\u0000" + if (size > 0) size.toString() else "*"
}

/**
 * 同一份文件在表里只占一行（电脑端同一套规则的手机版）。
 *
 * 电脑端那边是：「文件一落地，"正在传"那一行就收起来，只留"已完成"那一行
 * （带完整动作）」。这里按"文件身份"归并，规则是：
 *
 * * 电脑接收目录里的「已完成」盖过同一份文件的「接收中」（文件都到了，别再显示在传）
 *   和还没取走的「待保存」；
 * * 已经取走的那条（「已完成（已取走）」）盖过接收目录那条（它带着"取走"这件事）；
 * * 发出去的：这条上传彻底落地（已完成/已交接/失败）之后，同一次传输的「发送中」
 *   那一行收起来。**按传输号配对**（不是按文件名），所以同一个文件发第二次时，
 *   新那条"发送中"不会被上一次的"已完成"顶掉。
 *
 * 被顶掉的那一行如果是"电脑交给手机的"（它的动作要告诉电脑端"这份取走了"），
 * 动作会挪到留下来的那一行上 —— 一行一个文件，但动作不能丢。
 *
 * 只隐藏、不删除，理由见 [TableRow.hidden]。
 */
fun collapseTableRows(rows: MutableList<TableRow>): List<TableRow> {
    val winner = HashMap<String, Int>()
    rows.forEachIndexed { index, row ->
        if (row.identity.isBlank()) return@forEachIndexed
        val current = winner[row.identity] ?: run { winner[row.identity] = index; return@forEachIndexed }
        val better = rows[current]
        if (row.rank > better.rank || (row.rank == better.rank && row.priority >= better.priority)) {
            winner[row.identity] = index
        }
    }
    rows.forEachIndexed { index, row ->
        if (row.identity.isBlank()) return@forEachIndexed
        if (winner[row.identity] != index) row.hidden = true
    }
    // 收起来的那一行如果带着"手机自己那条"的动作（取走时要告诉电脑端），
    // 就把它挪到留下来的那一行上 —— 一个文件一行，但动作不能丢。
    rows.forEachIndexed { index, row ->
        if (!row.hidden || row.onAction == null) return@forEachIndexed
        val kept = winner[row.identity] ?: return@forEachIndexed
        val visible = rows[kept]
        if (row.priority > visible.priority) {
            rows[kept] = visible.copy(
                action = row.action,
                onAction = row.onAction,
                target = visible.target ?: row.target,
            )
        }
    }
    return rows
}

/**
 * 传输页筛选（宪章 §3.2）：这一行属不属于这一档。
 *
 * 判定只看行上**已经有**的字段（进度/状态文案），不重新猜业务状态 —— 猜的话
 * 就会出现"表格写已完成、筛选说进行中"这种自相矛盾。
 */
fun matchesFilter(row: TableRow, filter: TransferFilter): Boolean = when (filter) {
    TransferFilter.All -> true
    TransferFilter.Running ->
        row.progress != null || RUNNING_STATES.any { row.status.contains(it) }
    TransferFilter.Done ->
        row.progress == null && DONE_STATES.any { row.status.contains(it) }
    TransferFilter.Failed -> FAILED_STATES.any { row.status.contains(it) }
}

private val RUNNING_STATES = listOf("接收中", "发送中", "排队中", "待确认", "传输中", "进行中")
private val DONE_STATES = listOf("已完成", "已交接", "已发送", "已保存", "下载完成")
private val FAILED_STATES = listOf("失败", "错误", "取消", "被拒")

/** 按筛选药丸过一遍（顺序不变）。 */
fun filterRows(rows: List<TableRow>, filter: TransferFilter): List<TableRow> =
    rows.filter { matchesFilter(it, filter) }

private val NAME_W = 2.2f
private val PEER_W = 1.3f
private val SIZE_W = 1.0f
private val TIME_W = 1.3f
private val STATE_W = 1.4f
private val ACT_W = 1.3f

@Composable
private fun TransferTableHeader(receive: Boolean) {
    Row(
        Modifier
            .fillMaxWidth()
            .background(Es.SurfaceAlt)
            .padding(horizontal = 10.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        HeaderCell("文件", NAME_W)
        HeaderCell(if (receive) "来自" else "发给", PEER_W)
        HeaderCell("大小", SIZE_W)
        HeaderCell(if (receive) "时间" else "进度", if (receive) TIME_W else TIME_W + STATE_W)
        if (receive) HeaderCell("状态", STATE_W)
        HeaderCell(if (receive) "操作" else "速度 · 剩余", if (receive) ACT_W else ACT_W + STATE_W)
    }
}

@Composable
private fun RowScope.HeaderCell(text: String, weight: Float) {
    Text(text, fontSize = 10.sp, color = Es.Dim, modifier = Modifier.weight(weight))
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun TransferTableRow(
    row: TableRow,
    receive: Boolean,
    client: ApiClient?,
    onPreviewItem: (PreviewItem) -> Unit,
    onResend: ((FileTarget) -> Unit)? = null,
    onChanged: () -> Unit = {},
) {
    // 安卓没有"右键"：长按这一行弹出和会话文件卡片同一套动作菜单。
    var menu by remember(row.key) { mutableStateOf(false) }
    Row(
        Modifier
            .fillMaxWidth()
            .combinedClickable(
                onClick = { row.onPreview?.invoke() },
                onLongClick = { if (row.target != null) menu = true },
            )
            .padding(horizontal = 10.dp, vertical = 7.dp)
            .testTag("transfer-${row.key}"),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (menu && row.target != null) {
            FileActionMenu(
                target = row.target,
                client = client,
                onDismiss = { menu = false },
                onPreview = onPreviewItem,
                onStatus = { },
                onResend = onResend,
                onChanged = onChanged,
            )
        }
        Text(
            row.name,
            fontSize = 11.sp,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(NAME_W).padding(end = 4.dp),
        )
        Text(
            row.peer,
            fontSize = 10.sp,
            color = Es.Muted,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(PEER_W).padding(end = 4.dp),
        )
        Text(
            fmtSize(row.size).ifBlank { "—" },
            fontSize = 10.sp,
            color = Es.Muted,
            maxLines = 1,
            modifier = Modifier.weight(SIZE_W).padding(end = 4.dp),
        )
        if (receive) {
            Text(
                row.time,
                fontSize = 10.sp,
                color = Es.Dim,
                maxLines = 1,
                modifier = Modifier.weight(TIME_W).padding(end = 4.dp),
            )
            Text(
                row.status,
                fontSize = 10.sp,
                color = statusColor(row.status),
                maxLines = 2,
                modifier = Modifier.weight(STATE_W).padding(end = 4.dp),
            )
            Column(Modifier.weight(ACT_W).padding(end = 4.dp)) {
                // §6：历史行上的「预览」——先在这里看一眼，再决定要不要存下来。
                if (row.onPreview != null) {
                    Text(
                        "预览",
                        fontSize = 10.sp,
                        color = Es.Accent,
                        maxLines = 1,
                        modifier = Modifier
                            .clickable { row.onPreview?.invoke() }
                            .padding(vertical = 1.dp)
                            .testTag("row-preview-${row.key}"),
                    )
                }
                if (row.action.isNotBlank()) {
                    Text(
                        row.action,
                        fontSize = 10.sp,
                        color = Es.Accent,
                        maxLines = 1,
                        modifier = Modifier
                            .clickable(enabled = row.onAction != null) { row.onAction?.invoke() }
                            .padding(vertical = 1.dp)
                            .testTag("row-action-${row.key}"),
                    )
                }
                if (row.second.isNotBlank()) {
                    Text(
                        row.second,
                        fontSize = 10.sp,
                        color = Es.Danger,
                        maxLines = 1,
                        modifier = Modifier
                            .clickable(enabled = row.onSecond != null) { row.onSecond?.invoke() }
                            .padding(vertical = 1.dp),
                    )
                }
            }
        } else {
            Column(Modifier.weight(TIME_W + STATE_W).padding(end = 4.dp)) {
                Text(
                    row.status,
                    fontSize = 10.sp,
                    color = statusColor(row.status),
                    maxLines = 1,
                )
                val fraction = row.progress
                if (fraction != null) {
                    LinearProgressIndicator(
                        progress = { fraction.coerceIn(0f, 1f) },
                        modifier = Modifier.fillMaxWidth().height(4.dp).padding(top = 2.dp),
                    )
                }
            }
            Column(Modifier.weight(ACT_W + STATE_W)) {
                Text(
                    row.extra.ifBlank { "—" },
                    fontSize = 10.sp,
                    color = Es.Dim,
                    maxLines = 1,
                )
                if (row.action.isNotBlank()) {
                    Text(
                        row.action,
                        fontSize = 10.sp,
                        color = Es.Accent,
                        maxLines = 1,
                        modifier = Modifier
                            .clickable(enabled = row.onAction != null) { row.onAction?.invoke() }
                            .padding(top = 2.dp)
                            .testTag("row-action-${row.key}"),
                    )
                }
            }
        }
    }
    HorizontalDivider(color = Es.Border)
}

/**
 * 被同一份文件的另一行顶掉的那一行：**位置留着，一个字都不画**。
 *
 * 用零高度的 Box（而不是 `return`）是为了让它仍然占着 LazyColumn 里的一项：
 * 下标不移动，key 也还在 —— 和电脑端 `setRowHidden(true)` 是同一件事。
 */
@Composable
private fun HiddenTableRow(row: TableRow) {
    Box(Modifier.fillMaxWidth().height(0.dp).testTag("row-hidden-${row.key}"))
}

private fun statusColor(status: String): Color = when {
    status.contains("失败") || status.contains("拒绝") -> Es.Danger
    status.contains("待确认") || status.contains("待保存") -> Es.Warn
    status.contains("完成") || status.contains("已取走") -> Es.Ok
    else -> Es.Muted
}

/** 接收表：待确认的 offer + 正在接收 + 电脑交给手机的 + 电脑接收目录里的文件。 */
@Composable
private fun receiveRows(
    context: android.content.Context,
    store: AppState,
    ui: UiState,
    scope: kotlinx.coroutines.CoroutineScope,
    onRefresh: () -> Unit,
    onPreview: (PreviewItem) -> Unit,
): List<TableRow> {
    val rows = mutableListOf<TableRow>()

    ui.offers.forEach { offer ->
        val requestId = offer.optString("request_id")
        val peerName = offer.optJSONObject("peer")?.optString("name").orEmpty().ifBlank { "对方" }
        // offer 里带的是逐条文件（files: name/size），按名字写出来比"3 个文件"
        // 有用得多 —— 用户要确认的正是"这是哪个文件"。
        val files = offer.optJSONArray("files")
        val names = (0 until (files?.length() ?: 0))
            .mapNotNull { files?.optJSONObject(it)?.optString("name") }
        val total = if (files == null) {
            offer.optLong("total")
        } else {
            (0 until files.length()).sumOf { files.optJSONObject(it)?.optLong("size") ?: 0L }
        }
        rows.add(
            TableRow(
                key = "offer-$requestId",
                name = when {
                    names.isEmpty() -> "${offer.optInt("files")} 个文件"
                    names.size == 1 -> names[0]
                    else -> "${names[0]} 等 ${names.size} 个文件"
                },
                peer = peerName,
                size = total,
                time = clock(offer.optDouble("ts", 0.0)).ifBlank { "刚刚" },
                status = "待确认",
                action = "接收",
                onAction = { respondOffer(store, ui, scope, requestId, true, onRefresh) },
                second = "拒绝",
                onSecond = { respondOffer(store, ui, scope, requestId, false, onRefresh) },
                // 故意**不参与合并**：这是一次还没开始的传输，得留给用户按接收/拒绝。
                // 它和"已经落地的那一份"不是同一件事（同名也只是恰好同名）。
                identity = "",
            ),
        )
    }

    ui.transfers.filter { it.optString("direction") == "receive" }.forEach { transfer ->
        val id = transfer.optString("transferId")
        val percent = transfer.optDouble("percent", 0.0).toFloat()
        rows.add(
            TableRow(
                key = "recv-$id",
                name = transferNames(transfer),
                peer = transfer.optJSONObject("peer")?.optString("name").orEmpty().ifBlank { "对方" },
                size = transfer.optLong("total"),
                time = clock(transfer.optDouble("started", 0.0)).ifBlank { "—" },
                status = "接收中 ${percent.toInt()}%",
                progress = percent / 100f,
                extra = "",
                action = "取消",
                onAction = {
                    scope.launch {
                        withContext(Dispatchers.IO) {
                            try {
                                ui.client?.postJson("/api/cancel", JSONObject().put("transferId", id))
                            } catch (ignored: Exception) {
                            }
                        }
                        onRefresh()
                    }
                },
                // 正在传：文件还没落地，字节取不到，所以这一行不给"预览/打开/另存为"
                // —— 落地那一行（电脑接收目录里的"已完成"）才给全套动作。
                identity = rowIdentity(transferNames(transfer), transfer.optLong("total")),
                rank = 0,
                priority = 0,
            ),
        )
    }

    val fetcher = FetchManager.get(context)
    ui.shares.forEach { share ->
        val shareId = share.optString("id")
        val name = share.optString("name", "文件")
        val size = share.optLong("size")
        val done = share.optBoolean("downloaded", false)
        // 这一行的状态**就是本机下载队列的状态**（不是猜的）：
        // 排队中 → 下载中 42% → 已完成；失败给中文原因，行内直接「重试」。
        val fetch = ui.fetches.firstOrNull { it.shareId == shareId }
        val finished = fetch?.state == FetchState.Done || (fetch == null && done)
        rows.add(
            TableRow(
                key = "share-$shareId",
                name = name,
                peer = "电脑",
                size = size,
                time = clock(share.optDouble("added", 0.0)).ifBlank { "—" },
                status = when {
                    fetch == null -> if (done) "已在其它客户端取走" else "待下载"
                    fetch.state == FetchState.Done -> "已保存到手机"
                    else -> fetch.label
                },
                progress = if (fetch?.state == FetchState.Downloading) fetch.percent else null,
                // 进度那句话：百分比 + 已下载/总大小 + 速度 + 剩余（§2.4 的口径）。
                extra = if (fetch != null && fetch.active) fetch.progressText else "",
                action = when {
                    fetch?.state == FetchState.Downloading || fetch?.state == FetchState.Queued -> "取消"
                    fetch?.state == FetchState.Failed -> "重试"
                    fetch?.state == FetchState.Done -> "打开"
                    finished -> "再存一次"
                    else -> "下载到手机"
                },
                onAction = {
                    when {
                        fetch?.state == FetchState.Downloading || fetch?.state == FetchState.Queued ->
                            fetcher.cancel(shareId)
                        fetch?.state == FetchState.Done ->
                            if (!fetcher.openTask(context, fetch)) {
                                ui.status = "系统里没有能打开 ${fetch.name} 的程序"
                            }
                        else -> ui.client?.let { active -> fetcher.fetchNow(active, shareId, name, size) }
                    }
                },
                onPreview = { onPreview(previewOfShare(ui.client, share)) },
                // "电脑交给手机的"是手机自己的那条下载入口：它从「待下载」走到
                // 「已完成（已取走）」，同一份文件的其它行（接收目录里那条）让它。
                identity = rowIdentity(name, size),
                rank = if (finished) 2 else 1,
                priority = 2,
                target = FileTarget(
                    key = "share-$shareId",
                    name = name,
                    size = size,
                    remotePath = "/api/share/" + encodeUrl(shareId),
                ),
            ),
        )
    }

    ui.computerFiles.forEach { entry ->
        val path = entry.optString("path")
        val name = entry.optString("name").ifBlank { path.substringAfterLast('/') }
        rows.add(
            TableRow(
                key = "file-$path",
                name = name,
                peer = "电脑 · 接收目录",
                size = entry.optLong("size"),
                time = clock(entry.optDouble("mtime", 0.0)).ifBlank { "—" },
                status = "已完成",
                action = "保存到手机",
                onAction = { downloadComputerFile(context, store, ui, scope, path, name, onRefresh) },
                onPreview = { onPreview(previewOfComputerFile(ui.client, entry)) },
                // 文件已经在电脑上了 = 落地：同一份文件的"正在传/接收中"那一行收起来。
                identity = rowIdentity(name, entry.optLong("size")),
                rank = 2,
                priority = 1,
                target = FileTarget(
                    key = "file-$path",
                    name = name,
                    size = entry.optLong("size"),
                    remotePath = "/api/download?path=" + encodeUrl(path),
                ),
            ),
        )
    }
    return rows
}

/** 发送表：正在跑的发送（带进度、速度、剩余时间）+ 上传记录。 */
@Composable
private fun sendRows(
    store: AppState,
    ui: UiState,
    scope: kotlinx.coroutines.CoroutineScope,
    onRefresh: () -> Unit,
): List<TableRow> {
    val rows = mutableListOf<TableRow>()

    ui.transfers.filter { it.optString("direction") == "send" }.forEach { transfer ->
        val id = transfer.optString("transferId")
        val percent = transfer.optDouble("percent", 0.0).toFloat()
        rows.add(
            TableRow(
                key = "send-$id",
                name = transferNames(transfer),
                peer = transfer.optJSONObject("peer")?.optString("name").orEmpty().ifBlank { "对方" },
                size = transfer.optLong("total"),
                time = "",
                status = "发送中 ${percent.toInt()}%",
                progress = percent / 100f,
                extra = fmtSpeed(transfer.optDouble("speed", 0.0)) + " · " + fmtEta(transfer.optDouble("eta", 0.0)),
                action = "取消",
                onAction = {
                    scope.launch {
                        withContext(Dispatchers.IO) {
                            try {
                                ui.client?.postJson("/api/cancel", JSONObject().put("transferId", id))
                            } catch (ignored: Exception) {
                            }
                        }
                        onRefresh()
                    }
                },
                // 和这次传输的上传记录共用一个身份（按传输号配对，不按文件名）：
                // 传输还在跑时这条留下（带实时进度/速度），落地之后让给"已完成"那条。
                identity = "xfer:$id",
                rank = 0,
                priority = 1,
            ),
        )
    }

    (ui.uploads + ui.recentUploads).forEach { upload ->
        val id = upload.optString("id")
        if (ui.hiddenUploads.contains(id)) return@forEach
        val name = upload.optString("name")
        val size = upload.optLong("size")
        val transferId = upload.optString("transferId")
        val status = uploadStatus(upload)
        val done = status == "已完成" || status == "已交接"
        val failed = status.startsWith("失败")
        rows.add(
            TableRow(
                key = "upload-$id",
                name = name,
                peer = upload.optString("deviceName").ifBlank { "电脑" },
                size = size,
                time = "",
                status = status,
                progress = if (done) 1f else null,
                extra = upload.optString("error").ifBlank { "—" },
                identity = if (transferId.isNotBlank()) "xfer:$transferId" else rowIdentity(name, size),
                // 已经落地（已完成/已交接/失败）就算"这一份文件在这里了"：
                // 同一次传输里那条"发送中"于是收起来 —— 只留一行，带完整动作。
                rank = if (done || failed) 2 else 0,
                priority = 0,
                target = fileTargetForUpload(ui, name, size),
            ),
        )
    }
    return rows
}

private fun transferItemNames(transfer: JSONObject): List<String> {
    val items = transfer.optJSONArray("items") ?: return emptyList()
    return (0 until items.length()).mapNotNull { items.optJSONObject(it)?.optString("name") }
}

private fun transferNames(transfer: JSONObject): String {
    val names = transferItemNames(transfer)
    return when {
        names.isEmpty() -> "文件"
        names.size == 1 -> names[0]
        else -> "${names[0]} 等 ${names.size} 个文件"
    }
}

private fun uploadStatus(upload: JSONObject): String {
    val status = upload.optString("status")
    val error = upload.optString("error")
    return when {
        status == "done" -> "已完成"
        status == "handed-off" -> "已交接"
        status == "failed" && error.contains("PIN", ignoreCase = true) -> "失败：需要 PIN"
        status == "failed" -> "失败"
        status == "queued" -> "排队中"
        status == "sending" -> "发送中"
        else -> status.ifBlank { "—" }
    }
}

@Composable
private fun PendingBar(ui: UiState, onSendFiles: () -> Unit) {
    Card(
        Modifier.fillMaxWidth().padding(vertical = 6.dp),
        colors = CardDefaults.cardColors(containerColor = Es.SurfaceAlt),
    ) {
        Column(Modifier.padding(10.dp)) {
            Text("已选 ${ui.pending.size} 个文件：${ui.pending.joinToString("、") { it.name }}", fontSize = 11.sp, maxLines = 2, overflow = TextOverflow.Ellipsis)
            Button(
                onClick = onSendFiles,
                modifier = Modifier.padding(top = 6.dp).testTag("btn-send-pending"),
            ) { Text("发送给 ${ui.devices.filter { ui.targets.contains(it.id) }.joinToString("、") { it.name }.ifBlank { "设备" }}") }
        }
    }
}

/**
 * 「待重发」队列（宪章 §5.5 / C14）：发不出去的消息一条一行地摆在这里。
 *
 * 每一行说清三件事：**内容**（截断）、**为什么没发出去**、**下一步**（「重试」立刻
 * 再来一次并清零计数；「放弃」明确丢掉，不再重发）。自动重投由 [Root] 的队列泵做
 * （1s/2s/4s/8s/16s，最多 5 次），这里只负责"让人看得见、能插手"。
 */
@Composable
private fun PendingMessages(
    items: List<Queued>,
    onRetry: (Queued) -> Unit,
    onDrop: (Queued) -> Unit,
) {
    Card(
        Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 4.dp)
            .testTag("pending-messages"),
        colors = CardDefaults.cardColors(containerColor = Es.SurfaceAlt),
    ) {
        Column(Modifier.padding(10.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text(
                "待重发 ${items.size} 条（红色标记 = 还没投出去）",
                fontSize = 12.sp,
                fontWeight = FontWeight.SemiBold,
                color = Es.Danger,
            )
            items.forEach { item ->
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("🔴", fontSize = 11.sp)
                    Spacer(Modifier.width(6.dp))
                    Column(Modifier.weight(1f)) {
                        Text(
                            item.text.take(40).ifBlank { "（空消息）" },
                            fontSize = 12.sp,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                        Text(
                            if (item.exhausted()) {
                                "已重试 ${item.attempts} 次仍失败：${item.error.ifBlank { "原因未知" }}"
                            } else {
                                "第 ${item.attempts} 次失败：${item.error.ifBlank { "原因未知" }}（到点自动重试）"
                            },
                            fontSize = 10.sp,
                            color = Es.Danger,
                            maxLines = 2,
                        )
                    }
                    Text(
                        "重试",
                        fontSize = 11.sp,
                        color = Es.Accent,
                        modifier = Modifier
                            .clickable { onRetry(item) }
                            .padding(horizontal = 6.dp, vertical = 2.dp)
                            .testTag("retry-${item.key}"),
                    )
                    Text(
                        "放弃",
                        fontSize = 11.sp,
                        color = Es.Muted,
                        modifier = Modifier
                            .clickable { onDrop(item) }
                            .padding(horizontal = 6.dp, vertical = 2.dp)
                            .testTag("drop-${item.key}"),
                    )
                }
            }
        }
    }
}

/**
 * 会话里那条「电脑发来 N 个文件 · 点击下载」（task-9 第 3 条）。
 *
 * 以前电脑发了文件，手机这边**什么都不显示** —— 用户以为"没发过来"。现在会话
 * 顶部固定有这一条：说清几件、多大、下到哪一步，并且**点一下就能下**
 * （自动下载被关掉、或在移动网络下暂停时，它是唯一的手动入口）。
 */
@Composable
private fun SharesSummaryCard(
    ui: UiState,
    metered: Boolean,
    onFetchAll: () -> Unit,
    onRetry: (FetchTask) -> Unit,
) {
    val pending = ui.shares.filterNot { it.optBoolean("downloaded", false) }
    if (pending.isEmpty()) return
    val tasks = pending.mapNotNull { share ->
        ui.fetches.firstOrNull { it.shareId == share.optString("id") }
    }
    val totalSize = pending.sumOf { it.optLong("size") }
    // "在跑"不能只看"已匹配到的 tasks"：真机探针抓到过下载到 41.8% 时这张卡片
    // 还写「正在排队」—— 那一刻 tasks 匹配为空（分享 id 与本地 fetch 记录短暂对不上），
    // 但全局确实有下载在跑。所以两个来源都看（PG-3）。
    val activeFetches = ui.fetches.filter { it.active }
    val running = tasks.any { it.active } || activeFetches.isNotEmpty()
    val runningTask = tasks.firstOrNull { it.active } ?: activeFetches.firstOrNull()
    val got = if (runningTask != null) runningTask.got else tasks.sumOf { it.got }
    val failed = tasks.filter { it.state == FetchState.Failed && it.error != "已取消" }
    val done = tasks.count { it.state == FetchState.Done }

    Card(
        Modifier.fillMaxWidth().padding(vertical = 4.dp).testTag("shares-summary"),
        colors = CardDefaults.cardColors(containerColor = Es.SurfaceAlt),
    ) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(
                if (running) "📥 正在接收电脑发来的文件…" else "📥 电脑发来 ${pending.size} 个文件 · 点击下载",
                fontWeight = FontWeight.SemiBold,
                fontSize = 13.sp,
            )
            Text(
                when {
                    // 进度那句话里带百分比 / 已收/总大小 / 速度 / ETA（PG-2/PG-3 的判据）：
                    // 数据来源是本地 FetchTask（每 400ms 更新），不是服务端 transfers
                    // —— 手机自己下载的字节只有本机知道。
                    running -> {
                        val task = runningTask
                        if (task != null) "正在接收 · ${task.progressText}"
                        else "正在接收 $done/${pending.size} · ${fmtSize(got)} / ${fmtSize(totalSize)}"
                    }
                    done == pending.size -> "全部接收完成（在系统「下载」里）"
                    failed.isNotEmpty() -> "${failed.size} 个下载失败，下面可以重试"
                    metered -> "当前是移动网络，自动下载已暂停（省流量）——点右边按钮用流量下"
                    else -> "共 ${fmtSize(totalSize)}，正在排队"
                },
                fontSize = 11.sp,
                color = if (failed.isEmpty()) Es.Muted else Es.Danger,
            )
            if (tasks.isNotEmpty() && totalSize > 0) {
                LinearProgressIndicator(
                    progress = { (got.toFloat() / totalSize).coerceIn(0f, 1f) },
                    modifier = Modifier.fillMaxWidth().height(4.dp),
                )
            }
            // 逐个文件一行：名字 + 进度那句话；失败的给「重试」。
            tasks.forEach { task ->
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        task.name,
                        fontSize = 11.sp,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f),
                    )
                    Text(
                        if (task.state == FetchState.Failed) task.error.ifBlank { "失败" } else task.progressText,
                        fontSize = 10.sp,
                        color = when (task.state) {
                            FetchState.Failed -> Es.Danger
                            FetchState.Done -> Es.Ok
                            else -> Es.Muted
                        },
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f),
                    )
                    if (task.state == FetchState.Failed) {
                        Text(
                            "重试",
                            fontSize = 11.sp,
                            color = Es.Accent,
                            modifier = Modifier
                                .clickable { onRetry(task) }
                                .padding(horizontal = 6.dp, vertical = 2.dp)
                                .testTag("fetch-retry-" + task.shareId),
                        )
                    }
                }
            }
            Button(
                onClick = onFetchAll,
                modifier = Modifier.testTag("btn-fetch-all"),
            ) {
                Text(
                    if (failed.isNotEmpty()) "重试全部（${failed.size}）"
                    else if (running) "继续下载（${pending.size} 个）"
                    else "全部下载（${fmtSize(totalSize)}）",
                )
            }
        }
    }
}

// ------------------------------------------------------------- 主区：设置

/**
 * 设置页：**不占侧栏中间的位置**，点侧栏底部「设置」把主区换成它（侧栏还在），
 * 顶部给一条回会话的路（规格第 4 节）。
 */
@Composable
private fun SettingsPane(store: AppState, ui: UiState, onRefresh: () -> Unit) {
    val context = LocalContext.current
    var host by remember { mutableStateOf(store.host) }
    var status by remember { mutableStateOf("") }
    var keepAlive by remember { mutableStateOf(store.keepAlive) }
    // 「清空离线设备」的确认框开关：清空是"点一下就没了"的动作，必须先问一句。
    var askClearOffline by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    // 「离线」那一栏（侧栏里"以前见过、现在不在线"的那些）：按钮能不能点、
    // 确认框里报几台、点完之后还剩几台，读的都是这一份。
    val offlineRows = ui.devices.filterNot { it.online }
    // 其中**能被这个按钮清掉**的那一份：电脑端 `/api/devices/forget-offline`
    // 清的是"以前见过"的设备名单；剩下的行是连过的手机 / 网页版
    // （`knownClients`，id 前缀 `web:`），它们属于配对关系，清空不动它们。
    // 两拨人分开数，确认框才敢报准数 —— 否则"说清 17 台、清完还剩 6 台"。
    val clearableRows = offlineRows.filterNot { it.id.startsWith("web:") }

    /**
     * 清空离线设备：只清电脑上那份"以前见过"的名单，在线设备、聊天记录、
     * 文件和配对关系都不动（服务端 `/api/devices/forget-offline` 的语义）。
     *
     * 走 `ApiClient.postJson`（令牌、设备号头都在里面），不自己拼 HTTP；
     * 失败如实写在状态行和错误行上 —— 静默失败会让用户以为已经清干净了。
     */
    fun clearOffline() {
        status = "正在清空离线设备…"
        scope.launch {
            val active = ui.client ?: withContext(Dispatchers.IO) { connectSession(store, ui) }
            if (active == null) {
                val text = "清空离线设备失败：还没连上电脑"
                status = text
                ui.error = text
                return@launch
            }
            val (forgotten, problem) = withContext(Dispatchers.IO) {
                try {
                    val response = active.postJson("/api/devices/forget-offline", JSONObject())
                    if (response.optBoolean("ok", false)) {
                        response.optInt("forgotten", 0) to ""
                    } else {
                        -1 to response.optString("error").ifBlank { "电脑端拒绝了这次清空" }
                    }
                } catch (failure: Exception) {
                    -1 to (failure.message ?: failure.javaClass.simpleName)
                }
            }
            if (problem.isNotEmpty()) {
                val text = "清空离线设备失败：$problem"
                status = text
                ui.error = text
            } else {
                val text = "清空了 $forgotten 台离线设备（聊天记录保留）"
                status = text
                ui.status = text
                ui.error = ""
                // 再刷一遍设备列表（侧栏「离线」那段立刻跟着变）。它自己读不到
                // 状态时会把话写进 ui.error，所以上一步只是"先清掉旧错误"。
                refreshOnce(store, ui)
            }
        }
    }

    // 打开设置页就把保活服务拉起来（如果该开而没开）。
    // 以前只有"手动切换那个勾选框"才会启动服务：带着默认勾选启动 App 时
    // 服务从来没起来过，界面就一直写着"状态：未连接"——用户的截图正是如此。
    LaunchedEffect(keepAlive, store.host) {
        if (keepAlive && normalizeHost(store.host).isNotBlank()) {
            startLinkService(context, store, true)
        }
    }

    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            TextButton(onClick = { ui.page = MainPage.Chat }, modifier = Modifier.testTag("btn-back-chat")) {
                Text("← 会话")
            }
            Text("设置", fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
        }

        Text("电脑地址", fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
        OutlinedTextField(
            value = host,
            onValueChange = { host = it },
            singleLine = true,
            placeholder = { Text("例如 192.168.1.5:52119") },
            modifier = Modifier
                .fillMaxWidth()
                .testTag("host-field"),
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(
                onClick = {
                    status = "正在连接…"
                    scope.launch {
                        val (api, cleaned, message) = withContext(Dispatchers.IO) {
                            connectToDesktop(host)
                        }
                        if (cleaned.isNotEmpty()) {
                            host = cleaned
                            store.host = cleaned
                            store.via = "手动"
                        }
                        store.client = api
                        ui.client = api
                        if (api != null) {
                            announcePresence(store, api)
                            status = message
                        } else {
                            status = message
                        }
                        onRefresh()
                    }
                },
                modifier = Modifier.testTag("btn-connect"),
            ) { Text("连接") }

            OutlinedButton(
                onClick = {
                    ui.scanning = true
                    status = "正在搜索电脑…"
                    scope.launch {
                        val message = scanLan(context, store, ui)
                        status = message
                        onRefresh()
                    }
                },
                modifier = Modifier.testTag("btn-scan"),
            ) { Text(if (ui.scanning) "搜索中…" else "搜索电脑") }
        }
        if (status.isNotBlank()) {
            Text(status, fontSize = 13.sp, modifier = Modifier.testTag("status-text"))
        }

        // 自动发现到的电脑：点一行就连它。
        //
        // 这里以前显示的是"点一次搜索才有的列表"（`ui.found`）—— 现在列表是常驻的
        // （引擎一直在听/扫），所以这里显示的就是**实时**的发现结果，还带上来源。
        val peers = ui.discovered
        if (peers.isNotEmpty()) {
            Text("自动发现的设备（${peers.size}）", fontWeight = FontWeight.SemiBold, fontSize = 13.sp)
            peers.forEach { peer ->
                Card(
                    Modifier.fillMaxWidth().clickable {
                        host = "${peer.host}:${peer.webPort}"
                        store.host = host
                        store.via = peer.source
                        status = "已选择 ${peer.name}（来源：${peer.source}）"
                    },
                    colors = CardDefaults.cardColors(containerColor = Es.SurfaceAlt),
                ) {
                    Column(Modifier.padding(12.dp)) {
                        Text(
                            peer.name,
                            fontWeight = FontWeight.SemiBold,
                            fontSize = 14.sp,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                        val age = peer.ageMs / 1000
                        Text(
                            "${peer.host}:${peer.webPort} · 来源：${peer.source} · " +
                                (if (age < 2) "刚刚听到" else "${age} 秒前听到") +
                                (if (peer.note.isBlank()) "" else " · ${peer.note}"),
                            fontSize = 12.sp,
                            color = Es.Muted,
                        )
                    }
                }
            }
        }

        HorizontalDivider(color = Es.Border)

        // 诊断卡片（宪章 E2）：本机地址、监听端口、三条通道各自发了多少/收了多少、
        // 最后一次错误。出问题用户截一张图就够，不用去猜"到底哪一步没成"。
        Text("诊断", fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
        Card(
            Modifier.fillMaxWidth().testTag("diagnostics"),
            colors = CardDefaults.cardColors(containerColor = Es.SurfaceAlt),
        ) {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text("本机地址：${localIpv4().ifBlank { "无网络" }}", fontSize = 12.sp)
                Text("发现端口：UDP ${ui.discovery?.port ?: LanDiscovery.PORT}（组播/广播）", fontSize = 12.sp)
                Text("监听状态：${if (ui.discovery?.listening == true) "正在收听" else "未启动"}", fontSize = 12.sp)
                val channels = ui.discovery?.channels.orEmpty()
                if (channels.isEmpty()) {
                    Text("发现通道：还在启动…", fontSize = 12.sp, color = Es.Muted)
                } else {
                    channels.forEach { channel ->
                        Text(
                            "${channel.label}：发出 ${channel.sent} 包 / 收到 ${channel.heard} 条" +
                                (if (channel.lastError.isBlank()) "" else " · ${channel.lastError}"),
                            fontSize = 11.sp,
                            color = if (channel.lastError.isBlank()) Es.Muted else Es.Warn,
                        )
                    }
                }
                Text("主动扫描轮数：${ui.discovery?.rounds ?: 0}", fontSize = 11.sp, color = Es.Muted)
                val lastError = ui.discovery?.lastError.orEmpty()
                Text(
                    if (lastError.isBlank()) "最近错误：无" else "最近错误：$lastError",
                    fontSize = 11.sp,
                    color = if (lastError.isBlank()) Es.Muted else Es.Danger,
                    modifier = Modifier.testTag("diagnostics-error"),
                )
                Text(
                    "看不到对方时逐条查：① 手机和电脑在同一个 Wi-Fi；② 电脑上的韧传开着；" +
                        "③ 电脑防火墙允许 52119/TCP 与 52118/UDP；④ 公司网络可能开了客户端隔离。",
                    fontSize = 11.sp,
                    color = Es.Dim,
                )
            }
        }

        HorizontalDivider(color = Es.Border)
        // 自动下载电脑发来的文件（默认开）。移动网络下不自动跑 —— 一条 762 MB 的
        // 文件走流量会挨骂，界面上写明原因，点一下照样下。
        val fetcher = FetchManager.get(context)
        var autoFetch by remember { mutableStateOf(fetcher.autoFetch) }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Checkbox(
                checked = autoFetch,
                onCheckedChange = {
                    autoFetch = it
                    fetcher.autoFetch = it
                    ui.autoFetch = it
                    status = if (it) "已打开自动下载：电脑一发文件就开始收" else "已关闭自动下载：文件会列出来，点「全部下载」才收"
                },
                modifier = Modifier.testTag("autofetch-box"),
            )
            Column {
                Text("自动下载电脑发来的文件（推荐）", fontSize = 14.sp)
                Text(
                    "电脑一发文件就自动下到系统「下载」目录；移动网络下不自动跑（省流量）。" +
                        "下载完成的文件会发一条通知，点一下就能打开。",
                    fontSize = 11.sp,
                    color = Es.Muted,
                )
            }
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Checkbox(
                checked = keepAlive,
                onCheckedChange = {
                    keepAlive = it
                    store.keepAlive = it
                    startLinkService(context, store, it)
                },
                modifier = Modifier.testTag("keepalive-box"),
            )
            Column {
                Text("保持后台连接（推荐）", fontSize = 14.sp)
                Text(
                    "用前台服务常驻连接：熄屏、锁屏、切到别的 App 都能继续收消息和文件。" +
                        "这正是原生 App 存在的理由——浏览器页面做不到。",
                    fontSize = 11.sp,
                    color = Es.Muted,
                )
            }
        }
        val seenAgo = if (LinkService.lastSeenAt > 0) {
            ((System.currentTimeMillis() - LinkService.lastSeenAt) / 1000).coerceAtLeast(0)
        } else {
            -1L
        }
        Text(
            "状态：${LinkService.lastState}" +
                (if (seenAgo >= 0) "　·　最后联系 ${seenAgo} 秒前" else ""),
            fontSize = 12.sp,
            modifier = Modifier.testTag("link-state"),
        )
        if (LinkService.lastError.isNotBlank()) {
            Text("最近一次失败：${LinkService.lastError}", fontSize = 11.sp, color = Es.Danger)
        }
        if (LinkService.lastMessage.isNotBlank()) {
            Text("最近消息：${LinkService.lastMessage}", fontSize = 12.sp)
        }

        HorizontalDivider(color = Es.Border)
        Text("外观", fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
        Text(
            "深色是默认主题，和电脑端、网页版一致（三个客户端同一套配色 token）。",
            fontSize = 11.sp,
            color = Es.Muted,
        )

        HorizontalDivider(color = Es.Border)
        // 设备列表的维护动作：和「接收目录」「清空会话」同一类的一键维护
        // （电脑端设备栏上的「清空离线设备…」是同一件事，文案对齐）。
        Text("设备列表", fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
        // 每一句都按**实际会清掉几台**来说：清空只针对"以前见过"的那份名单，
        // 连过的手机 / 网页版（配对关系）不在里面，得让用户在点之前就知道。
        Text(
            when {
                clearableRows.isNotEmpty() ->
                    "「离线」那一栏现在有 ${offlineRows.size} 台：${clearableRows.size} 台是「以前见过、现在不在线」的设备" +
                        (if (offlineRows.size > clearableRows.size)
                            "，另外 ${offlineRows.size - clearableRows.size} 台是连过的手机 / 网页版（要删会话才会消失）。"
                        else "。") +
                        "清空只去掉前面那份名单：在线设备、聊天记录、文件和配对关系都不动。"
                offlineRows.isNotEmpty() ->
                    "「离线」那一栏现在有 ${offlineRows.size} 台，都是连过的手机 / 网页版：" +
                        "没有「以前见过」的设备可清（删掉对应的会话它们才会消失）。"
                ui.client == null ->
                    "「离线」那一栏是空的：还没连上电脑，连上之后以前见过的设备会列在这里。"
                else ->
                    "没有离线的设备：「离线」那一栏现在是空的。"
            },
            fontSize = 11.sp,
            color = Es.Muted,
            modifier = Modifier.testTag("offline-summary"),
        )
        OutlinedButton(
            onClick = { askClearOffline = true },
            enabled = clearableRows.isNotEmpty(),
            modifier = Modifier.testTag("btn-clear-offline"),
        ) { Text("清空离线设备") }
        if (clearableRows.isEmpty()) {
            // 没得清就别让人点了个寂寞：按钮禁用 + 一行说清为什么。
            Text(
                if (offlineRows.isEmpty()) "没有离线的设备" else "没有「以前见过」的离线设备",
                fontSize = 11.sp,
                color = Es.Dim,
                modifier = Modifier.testTag("offline-empty"),
            )
        }

        HorizontalDivider(color = Es.Border)
        Text("连接管理", fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
        OutlinedButton(
            onClick = {
                scope.launch {
                    val active = ui.client
                    ui.client = null
                    store.client = null
                    ui.selected = null
                    ui.open = null
                    ui.messages = emptyList()
                    ui.devices = emptyList()
                    // 下次连上时又要自动选「连着的这台电脑」。
                    ui.autoOpened = false
                    startLinkService(context, store, false)
                    if (active != null) {
                        withContext(Dispatchers.IO) {
                            try {
                                active.postJson("/api/leave", JSONObject())
                            } catch (ignored: Exception) {
                            }
                        }
                    }
                    status = "已断开：电脑上不再把这台手机算作在线"
                }
            },
            modifier = Modifier.testTag("btn-disconnect"),
        ) { Text("断开与电脑的连接") }

        HorizontalDivider(color = Es.Border)
        Text("关于", fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
        Text(
            "韧传 EverSend 安卓 App ${BuildConfig.VERSION_NAME}：局域网内直接收发，" +
                "不经过任何服务器。聊天记录、接收的文件都在电脑上，手机只是它的一个客户端。",
            fontSize = 11.sp,
            color = Es.Muted,
        )
        Text("本机：${store.deviceName} · ${store.deviceId.take(8)}", fontSize = 11.sp, color = Es.Dim)
    }

    // 「清空离线设备」的确认框：说清清的是哪一份名单、什么留着。
    if (askClearOffline) {
        ConfirmDialog(
            title = "清空离线设备列表",
            confirmLabel = "清空",
            confirmTag = "btn-clear-offline-confirm",
            cancelTag = "btn-clear-offline-cancel",
            onConfirm = {
                askClearOffline = false
                clearOffline()
            },
            onCancel = { askClearOffline = false },
        ) {
            Text(
                "把「离线」那一栏里「以前见过、现在不在线」的 ${clearableRows.size} 台设备从列表里去掉？",
                fontSize = 13.sp,
            )
            if (offlineRows.size > clearableRows.size) {
                Text(
                    "· 另外 ${offlineRows.size - clearableRows.size} 台是连过的手机 / 网页版，" +
                        "属于配对关系、不在这次清空的范围里（要删会话才会消失）。",
                    fontSize = 11.sp,
                    color = Es.Muted,
                )
            }
            Text(
                "· 只清这份离线名单：在线设备不受影响（它们下次被发现又会回来）。",
                fontSize = 11.sp,
                color = Es.Muted,
            )
            Text(
                "· 聊天记录、文件和配对关系都保留（对方的手机下次连上还在）。",
                fontSize = 11.sp,
                color = Es.Muted,
            )
        }
    }
}

// ------------------------------------------------------------- 输入区

/**
 * ⑥ 输入区（只在会话页出现）。
 *
 * 分两行，**输入行永远是最后一行、贴着底**（规格 §3⑥）：
 *
 * 1. 待发区（上面那一行）：方块（缩略图/类型图标 + 底部文件名 + 角上 ✕）横着排，
 *    这一行的**最右端**是统计「共 N 项 · X MB」；
 * 2. 输入行（最下面那一行）：`📎 传输文件`、附件、`😊`、`🎤`、输入框（占满剩余
 *    宽度）、PIN（窄）、`发送` —— PIN 在这一行里，不再单独占一行；
 * 3. 表情面板（点 😊 展开/收起）：**内嵌面板**，不是弹窗 —— 宽度和输入行一致，
 *    位置紧贴待发区那一行的上边，顺序固定成
 *    `[表情面板] → [待发文件 + 统计] → [输入行]`；点一个表情插进输入框、面板不关。
 *
 * 回车 = 发送；点方块 = 预览（§6）。
 */
@Composable
fun Composer(
    context: android.content.Context,
    client: ApiClient?,
    pending: List<Picked>,
    targets: List<DeviceRow>,
    chosen: Set<String>,
    pin: String,
    pinNeeded: Boolean,
    status: String,
    onPinChange: (String) -> Unit,
    onToggleTarget: (DeviceRow) -> Unit,
    onRemovePending: (Picked) -> Unit,
    onPickFiles: () -> Unit,
    onAttach: () -> Unit,
    onSendFiles: () -> Unit,
    onSend: (String) -> Unit,
    onUploaded: () -> Unit,
    onError: (String) -> Unit,
) {
    var draft by remember { mutableStateOf("") }
    var showEmoji by remember { mutableStateOf(false) }
    var recorder by remember { mutableStateOf<MediaRecorder?>(null) }
    var voiceFile by remember { mutableStateOf<File?>(null) }
    var recording by remember { mutableStateOf(false) }
    var voiceStatus by remember { mutableStateOf("") }
    var startedAt by remember { mutableStateOf(0L) }
    var previewItem by remember { mutableStateOf<PreviewItem?>(null) }
    val scope = rememberCoroutineScope()

    // 状态提示放在输入区**最上面**：输入行必须是最后一行（规格 §3⑥），
    // 以前它写在最底下，于是"输入框下面还有两行"，贴着底的反而是小字。
    if (status.isNotBlank()) {
        Text(
            status,
            fontSize = 10.sp,
            color = Es.Muted,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 2.dp),
        )
    }
    // 群发目标：默认是侧栏里选中的那台，可以多勾几台一起发。
    if (pending.isNotEmpty()) {
        // 待发区在上面（方块 + 统计），这一行只是"发给谁"。
        LazyRow(
            Modifier.fillMaxWidth().padding(horizontal = 12.dp),
            horizontalArrangement = Arrangement.spacedBy(6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            item { Text("发给：", fontSize = 11.sp, color = Es.Muted) }
            item {
                Text(
                    "已选 ${chosen.size} 台",
                    fontSize = 10.sp,
                    color = Es.Dim,
                    modifier = Modifier.padding(vertical = 4.dp).testTag("target-count"),
                )
            }
            items(targets, key = { it.id }) { device ->
                val on = chosen.contains(device.id)
                Text(
                    (if (on) "☑ " else "☐ ") + device.name,
                    fontSize = 11.sp,
                    color = if (on) Es.Accent else Es.Muted,
                    modifier = Modifier
                        .clickable { onToggleTarget(device) }
                        .padding(vertical = 4.dp)
                        .testTag("target-${device.name}"),
                )
            }
        }
    }
    // 录音提示单独一行，状态清楚：录音中 → 发送中 → 清空。
    if (voiceStatus.isNotBlank()) {
        Text(
            voiceStatus,
            fontSize = 11.sp,
            color = Es.Muted,
            modifier = Modifier.padding(horizontal = 12.dp).testTag("voice-status"),
        )
    }

    // 🎤 录音：按一次开始，再按一次结束并发送。抽成局部函数，输入行才读得清。
    fun toggleVoice() {
        val active = client ?: return
        if (!recording) {
            val granted = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) ==
                PackageManager.PERMISSION_GRANTED
            if (!granted) {
                onError("需要麦克风权限才能发语音")
                return
            }
            try {
                val file = File(context.cacheDir, "voice-${System.currentTimeMillis()}.m4a")
                val fresh = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    MediaRecorder(context)
                } else {
                    @Suppress("DEPRECATION") MediaRecorder()
                }
                fresh.apply {
                    setAudioSource(MediaRecorder.AudioSource.MIC)
                    setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                    setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
                    setOutputFile(file.absolutePath)
                    prepare()
                    start()
                }
                recorder = fresh
                voiceFile = file      // 记住到底是哪个文件，不靠"最新那个"
                startedAt = System.currentTimeMillis()
                recording = true
                voiceStatus = "正在录音…再按一次 ⏹ 结束并发送"
            } catch (problem: Exception) {
                recorder = null
                recording = false
                voiceStatus = ""
                onError("录音失败：${problem.message ?: problem.javaClass.simpleName}")
            }
        } else {
            val current = recorder
            recorder = null
            recording = false
            try {
                current?.stop()
            } catch (ignored: Exception) {
            }
            try {
                current?.release()
            } catch (ignored: Exception) {
            }
            val seconds = (System.currentTimeMillis() - startedAt) / 1000.0
            val file = voiceFile
            voiceFile = null
            if (file == null || !file.exists() || file.length() == 0L || seconds < 0.6) {
                if (file != null) file.delete()
                voiceStatus = "太短了，没发出去"
            } else {
                voiceStatus = "正在发送语音…（${"%.1f".format(seconds)} 秒）"
                scope.launch {
                    try {
                        withContext(Dispatchers.IO) {
                            uploadFile(active, file, "voice", (seconds * 1000).toInt(), onUploaded)
                        }
                        voiceStatus = "语音已发送 ✅"
                    } catch (problem: Exception) {
                        voiceStatus = ""
                        onError("语音发送失败：${problem.message ?: problem.javaClass.simpleName}")
                    }
                    file.delete()
                }
            }
        }
    }

    // ①-0 表情面板（规格 §3⑥ 第 3 条）：**不是弹窗**，是输入区里的一块内嵌面板。
    //      顺序固定成 `[表情面板] → [待发文件 + 统计] → [输入行]`，所以它排在
    //      PendingStage **正上方**一行：宽度对齐输入行（同一个 6dp 内缩），
    //      高度有上限（面板内部自己滚），不会把上面的会话区挤没。
    if (showEmoji) {
        EmojiPad(client = client, onPick = { draft += it })
    }

    // ① 待发区（输入框**上面**那一行）：一块一块的方块 —— 上面缩略图/类型图标、
    //    下面文件名、角上 ✕，点方块 = 预览（规格 §3⑥ + §6）；这一行最右端是统计。
    PendingStage(
        pending = pending,
        onRemove = onRemovePending,
        onPreview = { previewItem = it },
    )

    // ② 输入行：**输入区的最后一行**，贴着底。📎传输文件 / 附件 / 😊 / 🎤 /
    //    输入框（占满剩余宽度）/ PIN / 发送 都在这**一行**里（PIN 不再单独占一行）。
    //
    //    窄屏上要做的两件事：📎 的文字标签收成图标、PIN **按需出现** —— 手机竖屏
    //    一行要塞 7 个控件，PIN 常显会把输入框挤到 100dp 左右，打字都伸不开。
    BoxWithConstraints(Modifier.fillMaxWidth()) {
        val composerWidth = maxWidth
        // 手机竖屏（≥380dp）保留「传输文件」这四个字；再窄就只留 📎 图标，
        // 省下的宽度给输入框。
        val showPickLabel = composerWidth >= 380.dp
        // PIN 就在这一行里，但**只在用得上时占位置**：对方要 PIN（电脑端打回上传
        // 时点亮 ui.pinNeeded，状态行会写「对方要 PIN，右边填一下」）或者用户已经
        // 填过内容。所有宽度都这样，桌面上那个"常显"是桌面端自己的事。
        val showPinField = pinNeeded || pin.isNotBlank()
        // 输入框实际能拿到的宽度：这一行的固定控件都扣掉。占位文字按剩下多少地方
        // 选长短（写全了在窄屏会被裁成「说点什」，看着像坏了）。
        val reserved = (if (showPickLabel) 74.dp else 34.dp) + 34.dp * 3 + 4.dp + 49.dp +
            (if (showPinField) 68.dp else 0.dp)
        val inputWidth = composerWidth - 12.dp - reserved
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 6.dp, vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            if (showPickLabel) {
                TextButton(
                    onClick = onPickFiles,
                    modifier = Modifier.height(40.dp).testTag("btn-pick"),
                    contentPadding = PaddingValues(horizontal = 4.dp, vertical = 0.dp),
                ) { Text("📎 传输文件", fontSize = 11.sp, maxLines = 1) }
            } else {
                ComposerAction("btn-pick", onPickFiles) { Text("📎", fontSize = 17.sp) }
            }
            // 会话附件（图片/视频/文件长在气泡里）走单张选择：和「传输文件」是
            // 两条路——一条是"把文件发给这台设备"，一条是"在会话里说一张图"。
            ComposerAction("btn-attach", onAttach) {
                Icon(Icons.Filled.AttachFile, "附件", modifier = Modifier.size(19.dp))
            }
            ComposerAction("btn-emoji", { showEmoji = !showEmoji }) { Text("😊", fontSize = 22.sp) }
            ComposerAction("btn-voice", { toggleVoice() }) {
                Text(if (recording) "⏹" else "🎤", fontSize = 17.sp)
            }
            OutlinedTextField(
                value = draft,
                onValueChange = { draft = it },
                modifier = Modifier.weight(1f).testTag("chat-input"),
                // 输入框窄的时候（小屏手机 + 几个按钮挤在一行），占位文字会折成
                // 两行「说点什 / 么…」，看着像坏掉了。单行显示，长文本自己横滚。
                placeholder = {
                    Text(if (inputWidth >= 130.dp) "说点什么…" else "说点…", maxLines = 1)
                },
                // 输入框窄的时候（小屏手机 + 几个按钮挤在一行），占位文字会折成
                // 两行「说点什 / 么…」，看着像坏掉了。单行显示，长文本自己横滚。
                singleLine = true,
                maxLines = 1,
                // 面板里选中/打进来的 emoji，在输入框里就已经是最终那个大小。
                visualTransformation = EmojiSizeTransform,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                keyboardActions = KeyboardActions(onSend = {
                    if (draft.isNotBlank()) {
                        onSend(draft.trim())
                        draft = ""
                    }
                }),
            )
            if (showPinField) {
                Spacer(Modifier.width(4.dp))
                OutlinedTextField(
                    value = pin,
                    onValueChange = onPinChange,
                    singleLine = true,
                    placeholder = { Text("PIN", fontSize = 9.sp, maxLines = 1) },
                    modifier = Modifier.width(if (composerWidth >= 520.dp) 88.dp else 64.dp).testTag("pin-field"),
                    textStyle = androidx.compose.ui.text.TextStyle(
                        fontSize = 11.sp,
                        color = if (pinNeeded) Es.Warn else Es.Text,
                    ),
                )
            }
            Spacer(Modifier.width(4.dp))
            Button(
                onClick = {
                    if (draft.isNotBlank()) {
                        onSend(draft.trim())
                        draft = ""
                    }
                    if (pending.isNotEmpty()) onSendFiles()
                },
                enabled = draft.isNotBlank() || pending.isNotEmpty(),
                modifier = Modifier.testTag("btn-send"),
                contentPadding = PaddingValues(horizontal = 10.dp, vertical = 6.dp),
            ) { Text("发送", fontSize = 13.sp) }
        }
    }

    // 预览弹层（§6）：待发方块、气泡上的「预览」、接收表历史行的「预览」都开它。
    previewItem?.let { item ->
        FilePreviewDialog(item = item, client = client, onClose = { previewItem = null })
    }
}

/**
 * 输入行里的一个紧凑图标按钮。
 *
 * Material 的 `IconButton` 固定 48dp 见方，手机竖屏一行要放四个（📎/附件/😊/🎤）
 * 再加输入框、PIN、发送 —— 用 48dp 的话输入框会被挤没。这里 34dp 宽、40dp 高，
 * 仍然在触摸容差的边缘（左右各有相邻控件分摊），实测点得准。
 */
@Composable
private fun ComposerAction(tag: String, onClick: () -> Unit, content: @Composable () -> Unit) {
    Box(
        Modifier
            .width(34.dp)
            .height(40.dp)
            .clickable { onClick() }
            .testTag(tag),
        contentAlignment = Alignment.Center,
    ) { content() }
}

/**
 * ⑥ 待发区那一行：一排方块 + **最右端**的统计。
 *
 * 规格 §3⑥：选好的文件不是一行小字，而是"图标/缩略图 + 底部文件名"的方块，
 * 图片给真缩略图、视频给封面帧、其余给类型图标；方块多了横向滚动（`LazyRow`，
 * 不换行、不把上面的会话区挤扁）。统计「共 N 项 · X MB」就在这一行的右端，
 * 而这一行整体位于输入框**上面**（输入框那一行是输入区的最后一行）。
 */
@Composable
private fun PendingStage(
    pending: List<Picked>,
    onRemove: (Picked) -> Unit,
    onPreview: (PreviewItem) -> Unit,
) {
    Row(
        Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (pending.isEmpty()) {
            // 没有文件时这一行只剩一句提示（规格 §3⑥）。
            Text(
                "点「📎 传输文件」选要发的文件：方块排在这里，点方块可以先看一眼。",
                fontSize = 10.sp,
                color = Es.Muted,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
            return@Row
        }
        LazyRow(
            Modifier.weight(1f),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            items(pending, key = { it.uri.toString() }) { picked ->
                PendingTile(
                    picked = picked,
                    onRemove = { onRemove(picked) },
                    onPreview = { onPreview(previewOfPicked(picked)) },
                )
            }
        }
        Spacer(Modifier.width(8.dp))
        // 统计就在这一行的最右端（同时也是输入框上面那一行）。
        Box(Modifier.testTag("stage-stats")) {
            Text(
                "共 ${pending.size} 项 · " + fmtSize(pending.sumOf { it.size }).ifBlank { "—" },
                fontSize = 10.sp,
                color = Es.Dim,
                maxLines = 1,
                // 上一轮的标记叫 pending-summary（同一块统计文字，没换地方），
                // 这里两层都留着，免得以前按它找的脚本落空。
                modifier = Modifier.testTag("pending-summary"),
            )
        }
    }
}

/** 方块缩略图的像素边长：够看清一张照片，又不至于让输入区吃掉会话的高度。 */
private const val TILE_THUMB_PX = 160

/**
 * 一个待发文件（规格 §3⑥）：上面缩略图（图片/视频）或类型图标，下面文件名，
 * 右上角一个 ✕。点整块 = 预览（§6），✕ = 不发送这个。
 */
@Composable
private fun PendingTile(
    picked: Picked,
    onRemove: () -> Unit,
    onPreview: () -> Unit,
) {
    val context = LocalContext.current
    var kind by remember(picked.uri) { mutableStateOf("") }
    var thumb by remember(picked.uri) { mutableStateOf<Bitmap?>(null) }

    // 缩略图和"这到底是什么类型"都在 IO 线程上算：判定顺序照 §6（扩展名优先，
    // 其次闻内容），所以图片/视频即使没有后缀也能给出真缩略图。
    LaunchedEffect(picked.uri) {
        val (detected, picture) = withContext(Dispatchers.IO) {
            val mime = try {
                context.contentResolver.getType(picked.uri).orEmpty()
            } catch (ignored: Exception) {
                ""
            }
            val resolved = previewKindOf(picked.name, readLocalHead(context, picked.uri, SNIFF_BYTES), mime)
            resolved to thumbnailFor(context, picked.uri, resolved, TILE_THUMB_PX)
        }
        kind = detected
        thumb = picture
    }

    Column(
        Modifier
            .width(78.dp)
            .clickable { onPreview() }
            .padding(2.dp)
            .testTag("tile-${picked.name}"),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Box(Modifier.size(64.dp)) {
            Box(
                Modifier
                    .fillMaxSize()
                    .clip(RoundedCornerShape(8.dp))
                    .background(Es.SurfaceAlt)
                    .border(1.dp, Es.Border, RoundedCornerShape(8.dp)),
                contentAlignment = Alignment.Center,
            ) {
                val picture = thumb
                if (picture != null) {
                    Image(
                        bitmap = picture.asImageBitmap(),
                        contentDescription = picked.name,
                        contentScale = ContentScale.Crop,
                        modifier = Modifier.fillMaxSize(),
                    )
                } else {
                    Text(tileGlyph(picked.name, kind), fontSize = 26.sp)
                }
            }
            // ✕ 浮在右上角（角标那样）。布局里不叠控件，用 offset 摆。
            Box(
                Modifier
                    .align(Alignment.TopEnd)
                    .offset(x = 5.dp, y = (-5).dp)
                    .size(20.dp)
                    .background(Es.Bg, CircleShape)
                    .clickable { onRemove() }
                    .testTag("tile-remove"),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    "✕",
                    fontSize = 11.sp,
                    color = Es.Danger,
                    // 上一轮的「不发送这个」标记；元素还在，只是从 chip 变成了方块。
                    modifier = Modifier.testTag("chip-remove"),
                )
            }
        }
        Spacer(Modifier.height(3.dp))
        Text(
            picked.name,
            fontSize = 10.sp,
            color = Es.Muted,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            textAlign = TextAlign.Center,
            modifier = Modifier.fillMaxWidth(),
        )
    }
}

// ------------------------------------------------------------- 消息气泡

/**
 * 一条消息。四种附件都要能真的用起来，而不是只显示个文件名：
 *
 * * 图片：气泡里直接显示缩略图，点开全屏看大图；
 * * 视频：气泡里是播放卡片，点开在应用内播放（VideoView 走媒体接口的 URL）；
 * * 语音：气泡里能直接播放/停止（MediaPlayer 同上）；
 * * 文件：一键「保存到手机」（写进系统「下载」目录，SAF/MediaStore 都走通）。
 *
 * 文字除了长按选择，还给了显式的「复制」按钮 —— 长按能选、但用户不一定知道。
 *
 * 文件卡片还多两件事：**长按出四个动作**（预览/打开/打开所在文件夹/另存为，
 * 电脑端那是右键菜单），以及**下面那行进度**：这份文件正在传的时候，进度就长在
 * 这条消息下面（和电脑端 `_refresh_media_progress` 同一套，看的也是同一份
 * `engine.active_transfers()`）。
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun Bubble(
    message: ChatMessage,
    client: ApiClient?,
    onError: (String) -> Unit = {},
    /** 正在跑的传输（`/api/state` 的 transfers）：进度长在这条消息下面。 */
    transfers: List<JSONObject> = emptyList(),
    /**
     * 本机下载队列（[FetchManager]）。电脑"交给手机"的文件走的是手机自己拉取，
     * 在服务端**没有**对应 transfer —— 进度/速度/完成状态只能从这里拿（PG-1/PG-2）。
     */
    fetches: List<FetchTask> = emptyList(),
    /** 「传输文件」那条记录带过来的那份文件；普通消息按消息 id 取。 */
    target: FileTarget? = null,
    /** 附件气泡上要不要画「预览/保存」那行按钮（传输记录那条不用，长按即可）。 */
    actions: Boolean = true,
    onStatus: (String) -> Unit = {},
    /** 长按菜单里的「重新发送」：发给谁由调用方决定（那里知道当前会话的对端）。 */
    onResend: ((FileTarget) -> Unit)? = null,
    /** 删除本地副本之后刷新一下。 */
    onChanged: () -> Unit = {},
) {
    val outgoing = message.outgoing
    val context = LocalContext.current
    val clipboard = LocalClipboardManager.current
    val scope = rememberCoroutineScope()
    var preview by remember { mutableStateOf(false) }
    var menu by remember { mutableStateOf(false) }
    // 长按菜单点「预览」时用的那份（可能来自远端、也可能就是手机上那份原件）。
    var previewItem by remember { mutableStateOf<PreviewItem?>(null) }
    var playing by remember { mutableStateOf(false) }
    var player by remember { mutableStateOf<VlcHolder.Player?>(null) }
    var saving by remember { mutableStateOf(false) }
    val hasMedia = message.kind != "text" && message.kind != "system"
    // 这份文件在哪儿：普通附件按消息 id 取，传输记录那份由调用方给（可能是本机原件）。
    val file = target ?: fileTargetOf(message)
    // 这份文件在本机的下载状态：按"分享条目 id"或"文件名"对上 FetchTask。
    // 电脑发给手机的文件在服务端不是 transfer，进度/速度/ETA/完成态都只能从这拿。
    val shareId = (target?.key.orEmpty()).removePrefix("share-")
    val fetch = fetches.firstOrNull {
        (shareId.isNotBlank() && it.shareId == shareId) ||
            (message.mediaName.isNotBlank() && it.name == message.mediaName)
    }
    val fetchRunning = fetch?.active == true
    val fetchDone = fetch?.state == FetchState.Done

    DisposableEffect(message.id) {
        onDispose {
            // 只停自己的播放，共享的 LibVLC 实例留着给下一条消息用。
            try {
                player?.stop()
            } catch (ignored: Exception) {
            }
            player = null
        }
    }

    // §6：气泡上点「预览」、点图片缩略图、点视频卡片，都开同一个预览弹层。
    if (preview) {
        FilePreviewDialog(
            item = if (file != null) previewOfTarget(client, file) else previewOfMessage(client, message),
            client = client,
            onClose = { preview = false },
        )
    }
    // 长按：和传输表里的行同一个菜单（安卓没有右键）。
    if (menu) {
        FileActionMenu(
            target = file,
            client = client,
            onDismiss = { menu = false },
            onPreview = { item -> previewItem = item },
            onStatus = onStatus,
            onResend = onResend,
            onChanged = onChanged,
        )
    }
    previewItem?.let { item ->
        FilePreviewDialog(item = item, client = client, onClose = { previewItem = null })
    }

    Row(
        Modifier.fillMaxWidth().padding(vertical = 2.dp),
        horizontalArrangement = if (outgoing) Arrangement.End else Arrangement.Start,
    ) {
        Column(
            Modifier
                .widthIn(max = 300.dp)
                .background(
                    if (outgoing) Es.BubbleOut else Es.BubbleIn,
                    RoundedCornerShape(12.dp),
                )
                .padding(10.dp),
        ) {
            Text("${message.senderName.ifBlank { "对方" }} · ${clock(message.ts)}", fontSize = 10.sp, color = Es.Dim)
            when (message.kind) {
                "text", "system" -> SelectionContainer {
                    // emoji 单独放大到面板里那个字号（同一个 😂 在两边一样大）。
                    Text(emojiSized(message.text), fontSize = 15.sp)
                }
                "image" -> ImagePreview(message, client, onError, onLongClick = { if (file != null) menu = true }) { preview = true }
                "video" -> MediaCard(
                    icon = "🎬",
                    title = message.mediaName.ifBlank { "视频" },
                    detail = fmtSize(message.mediaSize) + " · 点击播放",
                    tag = "video-card",
                    onLongClick = { if (file != null) menu = true },
                ) { preview = true }
                "voice" -> MediaCard(
                    icon = if (playing) "⏹" else "🎤",
                    title = "语音 " + (message.durationMs / 1000) + " 秒",
                    detail = if (playing) "点击停止" else "点击播放（内置解码器）",
                    tag = "voice-card",
                    onLongClick = { if (file != null) menu = true },
                ) {
                    val active = client
                    if (active == null) {
                        onError("还没有连上电脑")
                    } else if (playing) {
                        playing = false
                        player?.stop()
                    } else {
                        // libVLC：自带 opus/webm/m4a 解码。网页录的是 webm/opus，
                        // App 录的是 m4a —— 系统解码器不保证两样都能放。
                        val vlc = VlcHolder.player(context) { problem ->
                            playing = false
                            onError("播放失败：$problem")
                        }
                        if (vlc == null) {
                            onError("内置播放器初始化失败")
                        } else {
                            try {
                                player = vlc
                                playing = true
                                vlc.play(active.mediaUrl(message.id)) { playing = false }
                            } catch (problem: Exception) {
                                playing = false
                                onError("语音播放失败：${problem.message}")
                            }
                        }
                    }
                }
                else -> MediaCard(
                    icon = if (fetchDone) "✅" else "📄",
                    title = message.mediaName.ifBlank { "文件" },
                    // 详情不能是死文案：下载中显示 百分比+已收/总+速度+ETA（PG-2），
                    // 保存完写"已保存到手机"而不是继续骗用户点保存（PG-1）。
                    detail = when {
                        fetchRunning && fetch != null -> fetch.progressText
                        fetchDone -> fmtSize(message.mediaSize) + " · 已保存到手机"
                        message.state == "failed" -> "接收失败"
                        else -> fmtSize(message.mediaSize) + " · 点击保存到手机"
                    },
                    tag = "file-card",
                    onLongClick = { if (file != null) menu = true },
                ) {
                    val active = client
                    if (active == null) {
                        onError("还没有连上电脑")
                    } else if (!saving) {
                        saving = true
                        scope.launch {
                            val name = message.mediaName.ifBlank { "eversend-${message.id}" }
                            // 走**解析后的**入口：有分享条目就取分享条目（电脑发布给
                            // 手机的正是它），没有才退回聊天附件那条路。
                            val remote = file?.remotePath.orEmpty()
                            val expected = file?.size ?: message.mediaSize
                            val saved = withContext(Dispatchers.IO) {
                                saveToDownloads(context, name, expected) { sink ->
                                    if (remote.isNotBlank()) active.download(remote, sink)
                                    else active.saveMedia(message.id, sink)
                                }
                            }
                            saving = false
                            if (saved == null) {
                                // 说清"为什么"（404 与磁盘满的处理方式完全不同），
                                // 不留一个 0 字节文件让用户以为已经收到了。
                                onError("保存失败：${saveFailureReason(active, remote, message)}")
                            }
                            else android.widget.Toast
                                .makeText(context, "已保存到「下载」：$name", android.widget.Toast.LENGTH_LONG)
                                .show()
                        }
                    }
                }
            }

            // 可复制的文字，以及附件的说明文字
            val caption = message.text
            if (hasMedia && caption.isNotBlank()) {
                SelectionContainer { Text(emojiSized(caption), fontSize = 14.sp) }
            }
            // 有文字就给「复制」，有附件就给「保存」——图片/视频本身没有文字，
            // 但「保存到手机」正是它最需要的动作（第一版把整行放在"有文字"的
            // 条件里，图片气泡上于是既没有复制也没有保存）。
            val copyable = message.kind == "text" || message.kind == "system" || caption.isNotBlank()
            if (actions && (copyable || hasMedia)) {
                Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    if (copyable) {
                        Text(
                            "复制",
                            fontSize = 12.sp,
                            color = Es.Accent,
                            modifier = Modifier
                                .combinedClickable(
                                    onClick = {
                                        clipboard.setText(
                                            AnnotatedString(
                                                if (message.kind == "text" || message.kind == "system") {
                                                    message.text
                                                } else {
                                                    caption
                                                },
                                            ),
                                        )
                                        android.widget.Toast
                                            .makeText(context, "已复制", android.widget.Toast.LENGTH_SHORT)
                                            .show()
                                    },
                                    onLongClick = {},
                                )
                                .padding(vertical = 2.dp, horizontal = 2.dp)
                                .testTag("btn-copy"),
                        )
                    }
                    if (hasMedia) {
                        // §6：气泡上的「预览」——图片看大图、视频看封面帧并播放、
                        // 文本看开头一段、其它二进制如实说"看不了"，并给保存/打开。
                        Text(
                            "预览",
                            fontSize = 12.sp,
                            color = Es.Accent,
                            modifier = Modifier
                                .clickable { preview = true }
                                .padding(vertical = 2.dp, horizontal = 2.dp)
                                .testTag("preview-open"),
                        )
                        Text(
                            if (fetchDone) "已保存" else "保存",
                            fontSize = 12.sp,
                            color = if (fetchDone) Es.Ok else Es.Accent,
                            modifier = Modifier
                                // 已保存就不给"保存"入口（PG-1：完成 3.7s 后仍写「点击保存到手机」
                                // 是误导，用户会以为没存下来）。
                                .clickable(enabled = !fetchDone) {
                                    val active = client
                                    if (active == null) {
                                        onError("还没有连上电脑")
                                    } else {
                                        scope.launch {
                                            val name = message.mediaName.ifBlank { "eversend-${message.id}" }
                                            val saved = withContext(Dispatchers.IO) {
                                                saveToDownloads(context, name) { sink ->
                                                    active.saveMedia(message.id, sink)
                                                }
                                            }
                                            if (saved == null) onError("保存失败（存储权限或空间不足）")
                                            else android.widget.Toast
                                                .makeText(
                                                    context,
                                                    "已保存到「下载」：$name",
                                                    android.widget.Toast.LENGTH_LONG,
                                                )
                                                .show()
                                        }
                                    }
                                }
                                .padding(vertical = 2.dp, horizontal = 2.dp)
                                .testTag("btn-save"),
                        )
                    }
                }
            }
            if (outgoing && message.state == "failed") {
                Text("发送失败（对方不在线）", fontSize = 11.sp, color = Es.Danger)
            }
            // 传输的状态/进度就长在这条消息下面（和传输表里那一行是同一份记录）。
            if (hasMedia) {
                val live = liveTransferOf(transfers, message.mediaName, outgoing)
                if (live == null && fetchRunning && fetch != null) {
                    // 电脑发给手机的文件：本机下载进度（百分比+已收/总+速度+ETA）。
                    Text(
                        fetch.progressText,
                        fontSize = 10.sp,
                        color = Es.Accent,
                        modifier = Modifier.testTag("transfer-live"),
                    )
                    LinearProgressIndicator(
                        progress = { fetch.percent.coerceIn(0f, 1f) },
                        modifier = Modifier.fillMaxWidth().height(4.dp).padding(top = 2.dp),
                    )
                } else if (live != null) {
                    Text(
                        live.label,
                        fontSize = 10.sp,
                        color = Es.Accent,
                        modifier = Modifier.testTag("transfer-live"),
                    )
                    live.percent?.let { fraction ->
                        LinearProgressIndicator(
                            progress = { fraction.coerceIn(0f, 1f) },
                            modifier = Modifier.fillMaxWidth().height(4.dp).padding(top = 2.dp),
                        )
                    }
                } else {
                    // 本机已下载完成就写「已保存到手机」——不能只依赖服务端消息状态
                    // （PG-1：完成 3.7s 后仍显示「接收中…」）。
                    val label = if (fetchDone) "已保存到手机" else messageStateLabel(message)
                    if (label.isNotBlank()) {
                        Text(
                            label,
                            fontSize = 10.sp,
                            color = if (message.state == "failed") Es.Danger else Es.Muted,
                            modifier = Modifier.testTag("transfer-state"),
                        )
                    }
                }
            }
        }
    }
}

/** 一条消息对应哪份文件（附件按消息 id 取字节：就是图片预览/保存走的那条路）。 */
fun fileTargetOf(message: ChatMessage): FileTarget? {
    if (message.kind == "text" || message.kind == "system") return null
    return FileTarget(
        key = "msg-${message.id}",
        name = message.mediaName.ifBlank { "eversend-${message.id}" },
        size = message.mediaSize,
        remotePath = "/api/chat/media/" + encodeUrl(message.id),
        kindHint = message.kind,
    )
}

/**
 * 这份文件是不是正在传：在跑的传输里按文件名找它。
 *
 * 「传输」页和会话看的是**同一份事实**（`/api/state` 的 transfers），所以在会话里
 * 发文件时进度就长在那条消息下面，不用切页签去猜 —— 电脑端 `_refresh_media_progress`
 * 做的是同一件事。
 */
fun liveTransferOf(transfers: List<JSONObject>, name: String, outgoing: Boolean): TransferLive? {
    if (name.isBlank()) return null
    val transfer = transfers.firstOrNull { transferItemNames(it).contains(name) } ?: return null
    val percent = transfer.optDouble("percent", 0.0).toFloat()
    return TransferLive(
        label = (if (outgoing) "发送中 " else "接收中 ") + percent.toInt() + "%",
        percent = percent / 100f,
    )
}

/** 会话里那条记录底下的一行进度/状态。 */
data class TransferLive(val label: String, val percent: Float?)

/**
 * 记录自己的状态（没有在跑的传输时显示）：
 * 发送中 → 已发送 / 失败；收到的就是已完成。
 */
/**
 * 一条气泡右下角那行状态字。
 *
 * 措辞按**事实**分档（qa-verify 在真机上抓到过"界面写已收到、/sdcard/Download 里
 * 什么都没有"）：消息送达是一回事，文件真的落到手机上完全是另一回事。
 *   · `sent`     —— 消息投递成功（"已收到"），**不代表文件在手机上**；
 *   · `downloaded` —— 文件真的写进系统「下载」且字节数相符，才敢说"已保存到手机"；
 *   · `taken`    —— 电脑说这份被取走了，但本机没有下载记录（多半是浏览器取的）。
 */
fun messageStateLabel(message: ChatMessage): String = when (message.state) {
    "sending" -> if (message.outgoing) "发送中…" else "接收中…"
    "queued" -> "排队中…"
    "ready" -> if (message.outgoing) "待发送" else "待下载"
    "downloaded" -> "已保存到手机"
    "taken" -> "已在其它客户端取走"
    "failed" -> if (message.outgoing) "发送失败" else "接收失败"
    "sent" -> if (message.outgoing) "已发送" else "已收到"
    "received", "done" -> "已完成"
    else -> ""
}

/**
 * 会话列表里的一项：一条真消息，或者「传输文件」那条记录。
 *
 * 电脑端的规矩是「走传输文件这条路的收发，也要在会话里留一条记录（方向/文件名/
 * 大小/状态：发送中 → 已发送 / 失败），进度长在那条消息下面」。手机这边照做，
 * 但**不另记一本账**：那条记录就是传输表里的那一行（`uploads`/`recentUploads`/
 * `shares`），所以会话里写的状态和传输页里写的永远是同一个。
 *
 * 聊天附件（气泡）本来就是一条消息，电脑端也已经把它记进会话了 —— 靠"同名同
 * 方向、5 分钟内"去重，不会记两遍（和电脑端 `_find_transfer_message` 同一套）。
 */
data class ChatEntry(
    val message: ChatMessage,
    val target: FileTarget?,
    /** 这条是"传输文件"留下的记录（不画「预览/保存」那行按钮，长按出菜单）。 */
    val transferNote: Boolean,
) {
    val key: String get() = message.id.ifBlank { "ts-" + message.ts }
}

fun conversationEntries(ui: UiState, store: AppState): List<ChatEntry> {
    val conversation = ui.open
    // 文件消息的"从哪儿取字节"：电脑把文件**交给手机**时它是分享条目
    // （`/api/share/<id>`），只有聊天附件才是 `/api/chat/media/<id>`。
    // 真机上抓到过：那条消息的 `media_rel` 是空串 → `/api/chat/media/<id>` 404
    // 「没有这条附件」，用户看到"保存失败"，而分享表里明明有那份文件。
    // 所以：文件名能对上分享条目，就用分享条目这条路。
    fun shareTargetOf(message: ChatMessage): FileTarget? {
        if (message.kind == "text" || message.kind == "system") return null
        val name = message.mediaName
        if (name.isBlank()) return null
        val share = ui.shares.firstOrNull { it.optString("name") == name }
        if (share == null) return null
        return FileTarget(
            key = "share-" + share.optString("id"),
            name = name,
            size = share.optLong("size"),
            remotePath = "/api/share/" + encodeUrl(share.optString("id")),
            kindHint = message.kind,
        )
    }
    val entries = ui.messages.map { ChatEntry(it, shareTargetOf(it), false) }.toMutableList()
    if (conversation == null) return entries
    // 电脑端已经把它记成消息了？那就用那条（有真附件、有真 id）。
    fun recorded(name: String, outgoing: Boolean, when_: Double): Boolean = ui.messages.any { message ->
        message.mediaName == name && message.outgoing == outgoing &&
            (when_ <= 0 || kotlin.math.abs(message.ts - when_) < 300)
    }

    // ① 手机用「传输文件」发出去的：记录按传输号/文件名和上传记录对上。
    (ui.uploads + ui.recentUploads).distinctBy { it.optString("id") }.forEach { upload ->
        val name = upload.optString("name")
        if (name.isBlank() || !uploadInConversation(ui, conversation, upload)) return@forEach
        val ts = upload.optDouble("finished", 0.0).takeIf { it > 0.0 } ?: upload.optDouble("started", 0.0)
        if (recorded(name, true, ts)) return@forEach
        val status = uploadStatus(upload)
        entries.add(
            ChatEntry(
                message = noteMessage(
                    id = "note-out-" + upload.optString("id"),
                    conversation = conversation,
                    senderName = store.deviceName,
                    name = name,
                    size = upload.optLong("size"),
                    ts = ts,
                    outgoing = true,
                    state = when {
                        status.startsWith("失败") -> "failed"
                        status == "已完成" || status == "已交接" -> "sent"
                        status == "排队中" -> "queued"
                        else -> "sending"
                    },
                ),
                target = fileTargetForUpload(ui, name, upload.optLong("size")),
                transferNote = true,
            ),
        )
    }

    // ② 电脑交给手机的（收到的）：这是手机这一侧真实的"接收"那条路。
    if (shareInConversation(ui, conversation)) {
        ui.shares.forEach { share ->
            val name = share.optString("name", "文件")
            if (name.isBlank()) return@forEach
            val ts = share.optDouble("added", 0.0)
            if (recorded(name, false, ts)) return@forEach
            val done = share.optBoolean("downloaded", false)
            // 这条记录的状态 = 本机下载队列的真实状态：
            //   排队中 → 待保存（ready）· 下载中 → 接收中（sending，气泡上带百分比）
            //   下好了 → 已完成（done）  · 失败 → 失败（failed，行内给"重试"）
            val fetch = ui.fetches.firstOrNull { it.shareId == share.optString("id") }
            // **文案不许超过事实**（E3 的反面就是"反馈错了"）：
            //   · 只有本机真的把字节写进 MediaStore 并校验过（fetch.Done）才说"已保存到手机"；
            //   · 电脑说被取走、但本机没有下载记录（可能是浏览器取的）→ 只说"已在其它客户端取走"；
            //   · 还没下 → "待下载"；下着呢 → "接收中 42%"；失败 → "接收失败：原因"。
            val state = when {
                fetch?.state == FetchState.Done -> "downloaded"
                fetch?.state == FetchState.Failed -> "failed"
                fetch?.state == FetchState.Downloading -> "sending"
                fetch?.state == FetchState.Queued -> "ready"
                done -> "taken"
                else -> "ready"
            }
            entries.add(
                ChatEntry(
                    message = noteMessage(
                        id = "note-in-" + share.optString("id"),
                        conversation = conversation,
                        senderName = ui.devices.firstOrNull { it.kind == "电脑" }?.name.orEmpty().ifBlank { "电脑" },
                        name = name,
                        size = share.optLong("size"),
                        ts = ts,
                        outgoing = false,
                        state = state,
                    ),
                    target = FileTarget(
                        key = "share-" + share.optString("id"),
                        name = name,
                        size = share.optLong("size"),
                        remotePath = "/api/share/" + encodeUrl(share.optString("id")),
                    ),
                    transferNote = true,
                ),
            )
        }
    }
    return entries.sortedBy { it.message.ts }
}

/** 「传输文件」那条记录本身：一条没有文字的附件消息（字节在别处，见 [ChatEntry.target]）。 */
private fun noteMessage(
    id: String,
    conversation: Conversation,
    senderName: String,
    name: String,
    size: Long,
    ts: Double,
    outgoing: Boolean,
    state: String,
): ChatMessage = ChatMessage(
    id = id,
    conversation = conversation.id,
    senderName = senderName,
    kind = "file",
    text = "",
    mediaName = name,
    mediaSize = size,
    durationMs = 0,
    ts = ts,
    outgoing = outgoing,
    state = state,
)

/** 这条上传记录是不是这次会话里的（发给这台设备/这个群）。 */
private fun uploadInConversation(ui: UiState, conversation: Conversation, upload: JSONObject): Boolean {
    val deviceId = upload.optString("deviceId")
    if (deviceId.isNotBlank() && conversation.members.contains(deviceId)) return true
    val wanted = listOfNotNull(
        ui.devices.firstOrNull { it.id == deviceId },
        ui.devices.firstOrNull { aliasOf(it.id) == aliasOf(deviceId) },
        ui.devices.firstOrNull { it.name == upload.optString("deviceName") && it.name.isNotBlank() },
    )
    return wanted.any { conversationFor(it, ui.conversations, ui.devices)?.id == conversation.id }
}

/** 「电脑交给手机的」这条属于和电脑的那个会话。 */
private fun shareInConversation(ui: UiState, conversation: Conversation): Boolean {
    val desktop = ui.devices.firstOrNull { it.kind == "电脑" } ?: return false
    return conversation.members.contains(desktop.id) || conversation.id.contains(desktop.id)
}

/** 设备号的长短写法不一定一样（`web:android:<id>` / `web:<id>`），按最后一段认。 */
fun aliasOf(id: String): String = id.substringAfterLast(':').trim()

/** 图片气泡：真图缩略图，点开全屏；长按出四个动作。 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun ImagePreview(
    message: ChatMessage,
    client: ApiClient?,
    onError: (String) -> Unit,
    onLongClick: () -> Unit = {},
    onOpen: () -> Unit,
) {
    var bitmap by remember(message.id) { mutableStateOf<android.graphics.Bitmap?>(null) }
    var failed by remember(message.id) { mutableStateOf(false) }
    // 关键字里带上 client：它一开始可能是 null（会话页刚进来还没连上），
    // 只按 message.id 做 key 的话这个协程会直接返回、再也不会重跑 —— 气泡就
    // 永远停在"载入中…"（真机测试里抓到过这一幕）。
    LaunchedEffect(message.id, client) {
        val active = client ?: return@LaunchedEffect
        withContext(Dispatchers.IO) {
            try {
                val bytes = active.mediaBytes(message.id)
                bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                if (bitmap == null) failed = true
            } catch (problem: Exception) {
                failed = true
                onError("图片取不回来：${problem.message}")
            }
        }
    }
    val image = bitmap
    when {
        image != null -> Image(
            bitmap = image.asImageBitmap(),
            contentDescription = message.mediaName,
            contentScale = ContentScale.Fit,
            modifier = Modifier
                .heightIn(max = 260.dp)
                .combinedClickable(onClick = { onOpen() }, onLongClick = { onLongClick() })
                .testTag("image-preview"),
        )
        failed -> Text("🖼 ${message.mediaName}（预览失败，可「保存」后在本机看）", fontSize = 14.sp)
        else -> Text("🖼 ${message.mediaName} 载入中…", fontSize = 14.sp)
    }
}

/** 视频/语音/文件的卡片：图标 + 标题 + 一行说明。长按出四个动作。 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun MediaCard(
    icon: String,
    title: String,
    detail: String,
    tag: String,
    onLongClick: () -> Unit = {},
    onClick: () -> Unit,
) {
    Row(
        Modifier
            .fillMaxWidth()
            .combinedClickable(onClick = { onClick() }, onLongClick = { onLongClick() })
            .padding(vertical = 6.dp, horizontal = 4.dp)
            .testTag(tag),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text(icon, fontSize = 26.sp)
        Column {
            Text(title, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
            Text(detail, fontSize = 12.sp, color = Es.Muted)
        }
    }
}

// ------------------------------------------------------------- 文件预览（§6）

/** 图片扩展名。判定顺序与清单照 `docs/UI_LAYOUT.md` §6 和电脑端 `desktop/preview.py`。 */
private val IMAGE_EXTENSIONS = setOf(
    "png", "jpg", "jpeg", "gif", "bmp", "webp", "ico", "tif", "tiff", "svg", "avif", "heic", "heif",
)
private val VIDEO_EXTENSIONS = setOf(
    "mp4", "mkv", "mov", "avi", "webm", "m4v", "flv", "wmv", "mpg", "mpeg", "ts", "3gp",
)
private val AUDIO_EXTENSIONS = setOf(
    "mp3", "m4a", "aac", "wav", "flac", "ogg", "opus", "oga", "wma", "amr", "aiff", "weba",
)
private val TEXT_EXTENSIONS = setOf(
    "txt", "md", "markdown", "rst", "log", "json", "yaml", "yml", "toml", "ini", "cfg", "conf",
    "csv", "tsv", "xml", "html", "htm", "css", "js", "mjs", "cjs", "ts", "tsx", "jsx", "py", "rb",
    "go", "rs", "java", "kt", "kts", "c", "h", "cpp", "hpp", "cc", "sh", "bash", "zsh", "fish",
    "ps1", "bat", "cmd", "sql", "properties", "gradle", "lua", "php", "pl", "swift", "m", "mm",
    "cs", "scala", "clj", "vim", "tex", "srt", "vtt", "env", "gitignore", "dockerfile",
)
private val ARCHIVE_EXTENSIONS = setOf("zip", "tar", "gz", "bz2", "xz", "7z", "rar")

/** 文本预览最多读这么多（§6：前 256 KB）。 */
private const val TEXT_PREVIEW_BYTES = 256 * 1024

/** 判断"是不是文本"时闻一下的字节数。 */
private const val SNIFF_BYTES = 4096

/** 图片这种"要看整份"的预览上限：再大就该走「保存到手机」了。 */
private const val MEDIA_PREVIEW_BYTES = 24L * 1024 * 1024

/**
 * 「复制」到剪贴板时最多读这么多字节。
 *
 * 剪贴板是给"粘到别处"用的，文本超过 1 MB 粘过去只会把目标应用卡住；图片走的是
 * content URI（不读整份），所以这个上限实际只约束文本。
 */
private const val COPY_PREVIEW_BYTES = 1024L * 1024

/**
 * 要预览的一份文件：名字、大小，以及**从哪儿取字节**。
 *
 * 字节只从已有接口来，没有为预览新增任何后端接口：
 * 待发方块是本机 content URI（选择器给的）；聊天附件按消息 id 取
 * （`/api/chat/media/<id>` —— 就是气泡里图片预览和「保存」走的那条路）；
 * 接收表的两行分别是 `/api/share/<id>` 与 `/api/download?path=`（「保存到手机」用的
 * 也是这两条）。
 */
data class PreviewItem(
    /** 换一份文件就重新取值：界面里的 remember 用它当 key。 */
    val key: String,
    val name: String,
    val size: Long,
    val localUri: Uri? = null,
    /** 远端文件：交给 `client.download()` 的路径。 */
    val remotePath: String = "",
    /** 视频/音频的播放地址（GET 那条路不需要令牌，可以直接交给内置播放器）。 */
    val playUrl: String = "",
    val durationMs: Int = 0,
    /** 服务端已经知道的类型（聊天消息的 kind），扩展名认不出来时兜底。 */
    val kindHint: String = "",
    /** 顶上那行事实里额外写一句（比如"来自会话"）。 */
    val facts: String = "",
)

/** 待发方块 → 预览：字节来自本机 content URI，不依赖电脑。 */
fun previewOfPicked(picked: Picked): PreviewItem = PreviewItem(
    key = "picked-" + picked.uri,
    name = picked.name,
    size = picked.size,
    localUri = picked.uri,
)

/** 聊天附件 → 预览：按消息 id 取字节（和气泡里图片预览是同一条路）。 */
fun previewOfMessage(client: ApiClient?, message: ChatMessage): PreviewItem {
    val name = message.mediaName.ifBlank { "eversend-${message.id}" }
    return PreviewItem(
        key = "msg-${message.id}",
        name = name,
        size = message.mediaSize,
        remotePath = "/api/chat/media/" + encodeUrl(message.id),
        playUrl = client?.mediaUrl(message.id).orEmpty(),
        durationMs = message.durationMs,
        kindHint = message.kind,
    )
}

/** 接收表「电脑交给手机的」那一行 → 预览（字节走「保存到手机」那条路）。 */
fun previewOfShare(client: ApiClient?, share: JSONObject): PreviewItem {
    val id = share.optString("id")
    val path = "/api/share/" + encodeUrl(id)
    return PreviewItem(
        key = "share-$id",
        name = share.optString("name", "文件"),
        size = share.optLong("size"),
        remotePath = path,
        playUrl = client?.base?.trimEnd('/')?.plus(path).orEmpty(),
    )
}

/** 接收表「电脑接收目录」那一行 → 预览（字节走 `/api/download?path=`）。 */
fun previewOfComputerFile(client: ApiClient?, entry: JSONObject): PreviewItem {
    val path = entry.optString("path")
    val name = entry.optString("name").ifBlank { path.substringAfterLast('/') }
    val fetch = "/api/download?path=" + encodeUrl(path)
    return PreviewItem(
        key = "file-$path",
        name = name,
        size = entry.optLong("size"),
        remotePath = fetch,
        playUrl = client?.base?.trimEnd('/')?.plus(fetch).orEmpty(),
    )
}

fun extensionOf(name: String): String = name.substringAfterLast('.', "").lowercase()

// -------------------------------------------- 文件动作（安卓版的"右键"：长按）

/**
 * 会话里的文件卡片、传输表里的行，围着的是**同一份文件**：四个动作都靠它。
 *
 * 字节从哪儿来：手机本机还留着的那份（自己刚用「传输文件」发出去的）直接用它的
 * content URI；在电脑上的那份走 `client.download(remotePath)` —— 就是「保存到
 * 手机」那条路，这几个动作没有新增任何后端接口。
 */
data class FileTarget(
    val key: String,
    val name: String,
    val size: Long = 0,
    /** 手机本机就有的那一份（自己发出去的文件）。 */
    val localUri: Uri? = null,
    /** 在电脑上的那一份：交给 `client.download()` 的路径。 */
    val remotePath: String = "",
    /** 服务端已经知道的类型（聊天消息的 kind），预览时兜底。 */
    val kindHint: String = "",
)

/**
 * 文件动作（宪章 §4 的"安卓"那一列），顺序与电脑端右键菜单一致。
 *
 * | 动作 | 安卓 |
 * |---|---|
 * | 预览 / 播放 | ✅ 内置弹层 |
 * | 打开 | ✅ 系统 Intent |
 * | 打开所在文件夹 | ✅ 存到系统「下载」再打开它 |
 * | 另存为… | ✅ 存到系统「下载」 |
 * | 复制路径 | ❌ 安卓没有可复制的路径（见 [PropertiesDialog] 里那句说明） |
 * | 复制文件 | ✅ 文本进剪贴板、图片进剪贴板 |
 * | 删除本地文件 | ✅ 确认框 + 删本地副本 |
 * | 重新发送 | ✅ 再投一次给当前选中的设备 |
 * | 属性 | ✅ 名字/大小/类型/时间/SHA-256（无路径） |
 */
enum class FileAction(val label: String) {
    Preview("预览"),
    Open("打开"),
    Reveal("打开所在文件夹"),
    SaveAs("另存为…"),
    Copy("复制"),
    Delete("删除本地文件…"),
    Resend("重新发送"),
    Properties("属性"),
}

/** 文件 → 预览弹层要的那份描述（预览/播放复用会话里同一套）。 */
fun previewOfTarget(client: ApiClient?, target: FileTarget): PreviewItem {
    val url = if (target.remotePath.isBlank()) {
        ""
    } else {
        client?.base?.trimEnd('/')?.plus(target.remotePath).orEmpty()
    }
    return PreviewItem(
        key = "target-${target.key}",
        name = target.name,
        size = target.size,
        localUri = target.localUri,
        remotePath = target.remotePath,
        playUrl = url,
        kindHint = target.kindHint,
    )
}

/** 「传输文件」发出去的那份文件：手机上还留着原件就用原件，否则取电脑上那一份。 */
fun fileTargetForUpload(ui: UiState, name: String, size: Long): FileTarget = FileTarget(
    key = "upload-$name",
    name = name,
    size = size,
    localUri = ui.localFiles[rowIdentity(name, size)],
    remotePath = "/api/download?path=" + encodeUrl(name),
)

/**
 * 把一份文件落到系统「下载」目录（MediaStore，和「保存到手机」同一条路）。
 * 返回那个 content URI —— 「打开」要的正是它。
 */
fun saveTargetToDownloads(
    context: android.content.Context,
    client: ApiClient?,
    target: FileTarget,
): Uri? {
    val name = target.name.ifBlank { "eversend-file" }
    val local = target.localUri
    return try {
        when {
            local != null -> saveToDownloads(context, name) { sink ->
                val input = context.contentResolver.openInputStream(local)
                    ?: throw java.io.IOException("读不到本机文件")
                input.use { it.copyTo(sink) }
            }
            target.remotePath.isNotBlank() && client != null -> saveToDownloads(context, name) { sink ->
                client.download(target.remotePath, sink)
            }
            else -> null
        }
    } catch (problem: Exception) {
        Log.d("EverSend", "保存失败: ${problem.message}")
        null
    }
}

/** 「打开」的三种结果：打开了 / 存下来了但系统里没有能打开它的程序 / 文件还取不到。 */
enum class OpenResult { Opened, NoViewer, NoFile }

/**
 * 「打开」：本机就有的直接交给系统程序；在电脑上的先落到「下载」再打开。
 *
 * 老系统（安卓 9 及以下）落盘给的是 `file://` URI，系统会拒绝把它交给别的程序
 * —— 那时候如实说"文件已经在「下载」里"，不硬来。
 */
fun openTarget(context: android.content.Context, client: ApiClient?, target: FileTarget): OpenResult {
    val uri: Uri = target.localUri ?: saveTargetToDownloads(context, client, target) ?: return OpenResult.NoFile
    return try {
        val intent = Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, mimeOf(context, target.name, uri).ifBlank { "*/*" })
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_GRANT_READ_URI_PERMISSION)
        context.startActivity(intent)
        OpenResult.Opened
    } catch (problem: Exception) {
        Log.d("EverSend", "打开失败: ${problem.message}")
        OpenResult.NoViewer
    }
}

/**
 * 长按菜单里的动作。
 *
 * 安卓没有"右键"，也**没有"打开所在文件夹"这个概念**：手机上收到的文件都落在
 * 系统「下载」里，没有"某个文件的所在目录"这种入口。所以这个动作用最接近的等价
 * 做法 —— **存到系统「下载」，然后打开那个目录**（和电脑端「打开所在位置」
 * 对用户是同一件事：文件在哪、我现在就带你去）。
 *
 * 「删除本地文件 / 重新发送 / 属性」需要界面状态（确认框、目标设备、计算摘要），
 * 由 [FileActionMenu] 自己处理，不在这里。
 */
fun handleFileAction(
    scope: kotlinx.coroutines.CoroutineScope,
    context: android.content.Context,
    client: ApiClient?,
    target: FileTarget,
    action: FileAction,
    onPreview: (PreviewItem) -> Unit,
    onStatus: (String) -> Unit,
) {
    val name = target.name.ifBlank { "文件" }
    when (action) {
        FileAction.Preview -> onPreview(previewOfTarget(client, target))
        FileAction.Open -> scope.launch {
            onStatus("正在打开 $name…")
            when (withContext(Dispatchers.IO) { openTarget(context, client, target) }) {
                OpenResult.Opened -> onStatus("已打开 $name")
                OpenResult.NoViewer -> {
                    // 文件已经落到「下载」了，只是这台机器上没有能打开它的程序。
                    onStatus("已存到系统「下载」：$name（系统里没有能打开它的程序）")
                    toast(context, "已存到「下载」：$name（没有能打开它的程序）")
                }
                OpenResult.NoFile -> {
                    onStatus("打不开 $name：手机上还没有它，先「另存为」到「下载」")
                    toast(context, "打不开：先「另存为」落到「下载」")
                }
            }
        }
        FileAction.Reveal -> scope.launch {
            onStatus("正在存到「下载」：$name…")
            val saved = withContext(Dispatchers.IO) { saveTargetToDownloads(context, client, target) }
            if (saved == null) {
                onStatus("保存失败（存储权限或空间不足）：$name")
                toast(context, "保存失败（存储权限或空间不足）")
            } else {
                onStatus("已保存到系统「下载」：$name")
                toast(context, "已保存到「下载」：$name")
                openDownloadsFolder(context)
            }
        }
        FileAction.SaveAs -> scope.launch {
            onStatus("正在另存 $name…")
            val saved = withContext(Dispatchers.IO) { saveTargetToDownloads(context, client, target) }
            if (saved == null) {
                onStatus("另存失败（存储权限或空间不足）：$name")
                toast(context, "另存失败（存储权限或空间不足）")
            } else {
                onStatus("已另存到系统「下载」：$name")
                toast(context, "已另存到「下载」：$name")
            }
        }
        FileAction.Copy -> scope.launch {
            onStatus("正在复制 $name…")
            val message = withContext(Dispatchers.IO) { copyTargetToClipboard(context, client, target) }
            onStatus(message)
            toast(context, message)
        }
        // 这三个由 FileActionMenu 就地处理（需要确认框 / 目标设备 / 计算摘要）。
        FileAction.Delete, FileAction.Resend, FileAction.Properties -> Unit
    }
}

/**
 * 「复制」（宪章 §4：安卓上"复制文件到剪贴板"是文本 / 图片）。
 *
 * 文本和图片各走各的路：
 * - 文本：读出来（本地原件或从电脑取）直接进剪贴板，任何能收文本的地方都能粘；
 * - 图片：把**内容 URI** 放进剪贴板（`ClipData.newUri`）—— 微信、聊天框这些
 *   收图片的地方都认它，而且不用把整张图复制一份进内存。
 * 其它类型如实说"这条路不支持"，不装作复制成功了。
 */
fun copyTargetToClipboard(
    context: android.content.Context,
    client: ApiClient?,
    target: FileTarget,
): String {
    val name = target.name.ifBlank { "文件" }
    val kind = previewKindOf(name, hint = target.kindHint)
    val bytes = try {
        readPreviewBytes(context, client, previewOfTarget(client, target), COPY_PREVIEW_BYTES).first
    } catch (problem: Exception) {
        null
    }
    return when {
        kind == "text" && bytes != null -> {
            val text = previewTextOf(bytes)
            if (text.isBlank()) {
                "复制失败：这份文件是空的"
            } else {
                setClipboardText(context, text)
                "已复制文本（${text.length} 字）到剪贴板"
            }
        }
        kind == "image" -> {
            val uri = target.localUri ?: cacheCopy(context, client, target)
            if (uri == null) {
                "复制失败：拿不到这张图（先「另存为」再复制）"
            } else {
                val clipboard = context.getSystemService(android.content.Context.CLIPBOARD_SERVICE)
                    as? android.content.ClipboardManager
                if (clipboard == null) {
                    "复制失败：系统剪贴板不可用"
                } else {
                    clipboard.setPrimaryClip(android.content.ClipData.newUri(context.contentResolver, name, uri))
                    "已复制图片到剪贴板"
                }
            }
        }
        else -> "只有文本和图片能复制到剪贴板（$name 是 ${previewTypeLabel(kind)}）"
    }
}

private fun setClipboardText(context: android.content.Context, text: String) {
    val clipboard = context.getSystemService(android.content.Context.CLIPBOARD_SERVICE)
        as? android.content.ClipboardManager ?: return
    clipboard.setPrimaryClip(android.content.ClipData.newPlainText("韧传", text))
}

/** 把电脑上那份落到 App 的缓存目录，返回可分享的 content URI（图片复制用）。 */
fun cacheCopy(context: android.content.Context, client: ApiClient?, target: FileTarget): Uri? {
    if (client == null || target.remotePath.isBlank()) return null
    return try {
        val directory = File(context.cacheDir, "clipboard").apply { mkdirs() }
        val file = File(directory, target.name.ifBlank { "eversend-copy" })
        file.outputStream().use { sink -> client.download(target.remotePath, sink) }
        androidx.core.content.FileProvider.getUriForFile(context, "${context.packageName}.files", file)
    } catch (problem: Exception) {
        Log.d("EverSend", "复制到缓存失败: ${problem.message}")
        null
    }
}

/**
 * 删除**手机上**的那一份（宪章 §4：安卓 ✅ 带确认框）。
 *
 * 有三种情况，各说各的话，不含糊：
 * 1. 本机有原件（content URI）→ 交给 ContentResolver 删（系统相册/文件提供者会真删）；
 * 2. 只落在 App 缓存里 → 删缓存文件；
 * 3. 手机上本来就没有 → 明说"没有本地副本可删"，而不是假装删掉了。
 * 电脑上那份**不动**（手机不删电脑上的文件，这条界线一直没变）。
 */
fun deleteLocalCopy(context: android.content.Context, target: FileTarget): String {
    val name = target.name.ifBlank { "文件" }
    try {
        if (target.localUri != null) {
            val removed = context.contentResolver.delete(target.localUri, null, null)
            return if (removed > 0) {
                "已删除手机上的那份：$name"
            } else {
                // 有些提供者不返回行数，删成功也报 0；再查一次它还在不在。
                val still = try {
                    context.contentResolver.openInputStream(target.localUri)?.close()
                    true
                } catch (ignored: Exception) {
                    false
                }
                if (still) "删除失败：系统不让删这份文件（$name）" else "已删除手机上的那份：$name"
            }
        }
        val cached = File(File(context.cacheDir, "clipboard"), name)
        if (cached.exists()) {
            return if (cached.delete()) "已删除缓存里的那份：$name" else "删除失败（$name）"
        }
        return "手机上本来就没有这份文件的副本，没什么可删（电脑上那份不动）"
    } catch (problem: Exception) {
        return "删除失败：${problem.message ?: problem.javaClass.simpleName}"
    }
}

/** 属性面板要的那几行（宪章 §4：名字/大小/类型/时间/SHA-256；安卓无路径）。 */
data class FileFacts(
    val name: String,
    val size: Long,
    val type: String,
    val where: String,
    val modified: String,
    val sha256: String,
    val note: String,
)

/** 算属性（IO 线程调用）：能读到字节就算真的 SHA-256，读不到就如实写"没读到"。 */
fun factsOfTarget(
    context: android.content.Context,
    client: ApiClient?,
    target: FileTarget,
): FileFacts {
    val name = target.name.ifBlank { "文件" }
    val kind = previewKindOf(name, hint = target.kindHint)
    var size = target.size
    var modified = "—"
    var sha = "（拿不到内容，算不出摘要）"
    val where = when {
        target.localUri != null -> "手机上（应用选择的文件）"
        target.remotePath.isNotBlank() -> "电脑上（${target.remotePath}）"
        else -> "只有一条记录，字节已不在手上"
    }
    try {
        val digest = java.security.MessageDigest.getInstance("SHA-256")
        var total = 0L
        val stream = when {
            target.localUri != null -> context.contentResolver.openInputStream(target.localUri)
            target.remotePath.isNotBlank() && client != null -> {
                val buffer = java.io.ByteArrayOutputStream()
                client.download(target.remotePath, buffer)
                java.io.ByteArrayInputStream(buffer.toByteArray())
            }
            else -> null
        }
        stream?.use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val read = input.read(buffer)
                if (read <= 0) break
                digest.update(buffer, 0, read)
                total += read
            }
        }
        if (total > 0) {
            size = total
            sha = digest.digest().joinToString("") { "%02x".format(it) }
        }
    } catch (problem: Exception) {
        sha = "算不出摘要：${problem.message ?: problem.javaClass.simpleName}"
    }
    if (target.localUri != null) {
        modified = try {
            context.contentResolver.query(target.localUri, null, null, null, null)?.use { cursor ->
                if (cursor.moveToFirst()) {
                    val index = cursor.getColumnIndex(
                        android.provider.DocumentsContract.Document.COLUMN_LAST_MODIFIED
                    )
                    if (index >= 0) {
                        val stamp = cursor.getLong(index)
                        if (stamp > 0) SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.getDefault())
                            .format(Date(stamp)) else "—"
                    } else "—"
                } else "—"
            } ?: "—"
        } catch (ignored: Exception) {
            "—"
        }
    }
    return FileFacts(
        name = name,
        size = size,
        type = previewTypeLabel(kind) + "（${extensionOf(name).ifBlank { "无扩展名" }}）",
        where = where,
        modified = modified,
        sha256 = sha,
        // 「复制路径」在安卓上是明确不支持的：文件都在系统「下载」里，没有可复制的
        // 完整路径（宪章 §4 那个 ❌ 就要这么写出来，而不是菜单里悄悄少一项）。
        note = "复制路径：安卓不提供完整文件路径，所以没有这一项；要给别人就用「另存为…」或「复制」。",
    )
}

/** 属性弹层（名字/大小/类型/时间/SHA-256）。 */
@Composable
private fun PropertiesDialog(
    target: FileTarget,
    client: ApiClient?,
    onClose: () -> Unit,
) {
    val context = LocalContext.current
    var facts by remember(target.key) { mutableStateOf<FileFacts?>(null) }
    LaunchedEffect(target.key) {
        facts = withContext(Dispatchers.IO) { factsOfTarget(context, client, target) }
    }
    InfoDialog(title = "属性", onClose = onClose) {
        val current = facts
        if (current == null) {
            Text("正在计算（大文件要算 SHA-256，稍等）…", fontSize = 12.sp, color = Es.Muted)
        } else {
            FactRow("名字", current.name)
            FactRow("大小", fmtSize(current.size))
            FactRow("类型", current.type)
            FactRow("位置", current.where)
            FactRow("时间", current.modified)
            FactRow("SHA-256", current.sha256)
            Text(current.note, fontSize = 11.sp, color = Es.Dim)
        }
    }
}

@Composable
private fun FactRow(label: String, value: String) {
    Column {
        Text(label, fontSize = 11.sp, color = Es.Dim)
        SelectionContainer {
            Text(value, fontSize = 12.sp)
        }
    }
}

/**
 * 长按弹出来的动作菜单：预览 / 打开 / 打开所在文件夹 / 另存为 / 复制 / 删除 /
 * 重新发送 / 属性 —— 宪章 §4 安卓那一列的全部动作，一处实现。
 *
 * 会话里的文件卡片和传输表里的行用的是**这一个**菜单，所以两处的动作永远一致
 * （电脑端那边靠 `file_actions_menu()` 保证同一件事）。
 */
@Composable
fun FileActionMenu(
    target: FileTarget?,
    client: ApiClient?,
    onDismiss: () -> Unit,
    onPreview: (PreviewItem) -> Unit,
    onStatus: (String) -> Unit,
    /** 「重新发送」要发给谁由调用方决定（那里才知道当前选中/会话里的设备）。 */
    onResend: ((FileTarget) -> Unit)? = null,
    /** 本地副本被删掉之后刷新列表。 */
    onChanged: () -> Unit = {},
) {
    if (target == null) return
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var askDelete by remember(target.key) { mutableStateOf(false) }
    var showFacts by remember(target.key) { mutableStateOf(false) }

    // 菜单与两个弹层互斥：点了「删除」就把菜单收掉换成确认框（菜单盖着确认框
    // 会让人以为点错了）。
    if (!askDelete && !showFacts) {
        DropdownMenu(
            expanded = true,
            onDismissRequest = onDismiss,
            modifier = Modifier.testTag("file-action-menu"),
        ) {
            FileAction.entries.forEach { action ->
                DropdownMenuItem(
                    text = {
                        Text(
                            action.label,
                            fontSize = 13.sp,
                            modifier = Modifier.testTag("file-action-${action.name.lowercase()}"),
                        )
                    },
                    onClick = {
                        when (action) {
                            FileAction.Delete -> askDelete = true
                            FileAction.Properties -> showFacts = true
                            FileAction.Resend -> {
                                onDismiss()
                                if (onResend == null) {
                                    onStatus("重新发送：先在设备列表里选一台设备")
                                } else {
                                    onResend(target)
                                }
                            }
                            else -> {
                                onDismiss()
                                handleFileAction(scope, context, client, target, action, onPreview, onStatus)
                            }
                        }
                    },
                )
            }
        }
    }
    if (askDelete) {
        ConfirmDialog(
            title = "删除手机上的这份文件？",
            confirmLabel = "删除",
            confirmTag = "confirm-delete",
            cancelTag = "cancel-delete",
            onConfirm = {
                askDelete = false
                scope.launch {
                    val message = withContext(Dispatchers.IO) { deleteLocalCopy(context, target) }
                    onStatus(message)
                    toast(context, message)
                    onChanged()
                    onDismiss()
                }
            },
            onCancel = { askDelete = false },
        ) {
            Text(target.name.ifBlank { "文件" }, fontSize = 13.sp)
            Text(
                "只删手机上的那一份（系统「下载」或应用缓存）；电脑上那份不动。",
                fontSize = 11.sp,
                color = Es.Muted,
            )
        }
    }
    if (showFacts) {
        PropertiesDialog(target = target, client = client, onClose = {
            showFacts = false
            onDismiss()
        })
    }
}

/**
 * 「重新发送」：把这份文件再投一次给 [device]（宪章 §4）。
 *
 * 手机上还留着原件就用原件；只剩电脑上那份时先从电脑取回缓存再发（这条路上
 * 数据要先下后上，所以状态行里写明"正在取回"）—— 用户点的是"重发"，就该真的重发。
 */
fun resendTarget(
    scope: kotlinx.coroutines.CoroutineScope,
    context: android.content.Context,
    client: ApiClient?,
    target: FileTarget,
    device: DeviceRow?,
    store: AppState,
    pin: String,
    onStatus: (String) -> Unit,
    onDone: () -> Unit,
) {
    val name = target.name.ifBlank { "文件" }
    val active = client
    when {
        active == null -> onStatus("重新发送失败：还没连上电脑")
        device == null -> onStatus("重新发送：先在设备列表里选一台设备")
        else -> scope.launch {
            onStatus("正在重新发送 $name 给 ${device.name}…")
            val result = withContext(Dispatchers.IO) {
                try {
                    val uri = target.localUri ?: cacheCopy(context, active, target)
                    if (uri == null) {
                        "重新发送失败：拿不到这份文件（先「另存为」到「下载」）"
                    } else {
                        uploadToDevice(context, active, uri, Pair(name, target.size), device, store, pin)
                        "已重新发送 $name 给 ${device.name}"
                    }
                } catch (problem: Exception) {
                    "重新发送失败：${problem.message ?: problem.javaClass.simpleName}"
                }
            }
            onStatus(result)
            toast(context, result)
            onDone()
        }
    }
}


/**
 * §6 的判定顺序：**扩展名优先，再闻内容** —— 前 4 KB 里有 NUL 字节，或者命中
 * zip/gzip/PNG/JPEG/PDF/ELF/SQLite/rar/7z 这些魔数，就按二进制处理；其余尝试按
 * UTF-8 解码，解得开就当文本（`README`、日志、配置这些没有后缀的文件也能看）。
 *
 * `hint` 是电脑端在收下附件时定的类型（聊天消息的 `kind`）：语音消息在手机上是
 * `.webm`/`.m4a` 录的，`.webm` 按扩展名会被当成视频 —— 服务端既然已经知道那是
 * 一段语音，就听服务端的。
 */
fun previewKindOf(name: String, head: ByteArray? = null, mime: String = "", hint: String = ""): String {
    when (hint) {
        "voice", "audio" -> return "audio"
        "image" -> return "image"
        "video" -> return "video"
    }
    val extension = extensionOf(name)
    if (extension in IMAGE_EXTENSIONS) return "image"
    if (extension in VIDEO_EXTENSIONS) return "video"
    if (extension in AUDIO_EXTENSIONS) return "audio"
    if (extension in TEXT_EXTENSIONS) return "text"
    if (extension == "pdf") return "binary"   // 没有 PDF 渲染器，别装作能看
    if (head != null && head.isNotEmpty()) {
        if (head.any { it == 0.toByte() } || binaryMagic(head)) return "binary"
        return if (decodesAsUtf8(head)) "text" else "binary"
    }
    // 闻不到内容（文件是空的、或者读不出来）时退一步看系统报的类型。
    val lower = mime.lowercase()
    return when {
        lower.startsWith("image/") -> "image"
        lower.startsWith("video/") -> "video"
        lower.startsWith("audio/") -> "audio"
        lower.startsWith("text/") || lower.contains("json") || lower.contains("xml") -> "text"
        else -> "binary"
    }
}

/** 前 4 KB 里的已知魔数（和电脑端 `_is_probably_binary_magic` 同一份清单）。 */
private fun binaryMagic(head: ByteArray): Boolean {
    val magics = listOf(
        byteArrayOf(0x50, 0x4B, 0x03, 0x04),                       // zip / docx / apk
        byteArrayOf(0x1F, 0x8B.toByte()),                         // gzip
        byteArrayOf(0x89.toByte(), 0x50, 0x4E, 0x47),              // PNG
        byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0xFF.toByte()),  // JPEG
        byteArrayOf(0x47, 0x49, 0x46, 0x38),                       // GIF8
        byteArrayOf(0x25, 0x50, 0x44, 0x46),                       // %PDF
        byteArrayOf(0x7F, 0x45, 0x4C, 0x46),                       // ELF
        "SQLite format 3".toByteArray(Charsets.US_ASCII),
        byteArrayOf(0x52, 0x61, 0x72, 0x21, 0x1A),                 // Rar!
        byteArrayOf(0x37, 0x7A, 0xBC.toByte(), 0xAF.toByte()),     // 7z
    )
    return magics.any { magic -> head.size >= magic.size && magic.indices.all { head[it] == magic[it] } }
}

/**
 * 能不能当 UTF-8 文本读。
 *
 * 尾巴上可能正好把一个多字节字符切两半（我们只读了前 4 KB），那不是"二进制" ——
 * 去掉不完整的尾巴再判一次。这和电脑端"严格按 UTF-8 解码"是同一个判断，只是
 * 少了那个把文本误判成二进制的边界情况。
 */
private fun decodesAsUtf8(bytes: ByteArray): Boolean {
    for (trim in 0..3) {
        val size = bytes.size - trim
        if (size <= 0) break
        try {
            Charsets.UTF_8.newDecoder()
                .onMalformedInput(java.nio.charset.CodingErrorAction.REPORT)
                .onUnmappableCharacter(java.nio.charset.CodingErrorAction.REPORT)
                .decode(java.nio.ByteBuffer.wrap(bytes, 0, size))
            return true
        } catch (ignored: java.nio.charset.CharacterCodingException) {
        }
    }
    return false
}

/** 文本预览的正文：先按 UTF-8 读，不成就退回 GB18030（和电脑端一致）。 */
private fun previewTextOf(bytes: ByteArray): String {
    for (encoding in listOf(Charsets.UTF_8, java.nio.charset.Charset.forName("GB18030"))) {
        try {
            return encoding.newDecoder()
                .onMalformedInput(java.nio.charset.CodingErrorAction.REPORT)
                .onUnmappableCharacter(java.nio.charset.CodingErrorAction.REPORT)
                .decode(java.nio.ByteBuffer.wrap(bytes))
                .toString()
        } catch (ignored: Exception) {
        }
    }
    return String(bytes, Charsets.UTF_8)
}

/** 没有缩略图时的类型图标：文本 / 压缩包 / 普通文件（规格 §3⑥）。 */
fun tileGlyph(name: String, kind: String = ""): String {
    val resolved = kind.ifBlank { previewKindOf(name, null) }
    return when (resolved) {
        "image" -> "🖼"
        "video" -> "🎬"
        "audio" -> "🎵"
        "text" -> "📄"
        else -> if (extensionOf(name) in ARCHIVE_EXTENSIONS) "🗜" else "📦"
    }
}

/** 取字节时"够了就停"的信号：不是错误，是自己不想再读了。 */
private class PreviewBytesReached : java.io.IOException("预览只读开头一段")

/**
 * 一个最多收 [limit] 字节的输出流。
 *
 * 远端的字节只能靠 `ApiClient.download(path, sink)` 拿（这是既有接口），它会把
 * 整份文件灌进来；文本预览只要前 256 KB，所以读到上限就把流掐掉（连接会在
 * `download` 的 finally 里关掉）。
 */
private class CappedSink(private val limit: Long) : java.io.OutputStream() {
    private val buffer = java.io.ByteArrayOutputStream()

    var truncated = false
        private set

    fun bytes(): ByteArray = buffer.toByteArray()

    private fun overflow(): Nothing {
        truncated = true
        throw PreviewBytesReached()
    }

    override fun write(one: Int) {
        if (buffer.size() >= limit) overflow()
        buffer.write(one)
    }

    override fun write(source: ByteArray, offset: Int, length: Int) {
        val room = limit - buffer.size()
        if (room <= 0) overflow()
        if (length > room) {
            buffer.write(source, offset, room.toInt())
            overflow()
        }
        buffer.write(source, offset, length)
    }
}

/** 预览取字节：本机读 content URI，远端走已有下载接口；最多读 [limit] 个字节。 */
private fun readPreviewBytes(
    context: android.content.Context,
    client: ApiClient?,
    item: PreviewItem,
    limit: Long,
): Pair<ByteArray, Boolean> {
    val local = item.localUri
    if (local != null) {
        val stream = context.contentResolver.openInputStream(local) ?: return ByteArray(0) to false
        stream.use {
            val sink = CappedSink(limit)
            try {
                it.copyTo(sink, 64 * 1024)
            } catch (ignored: PreviewBytesReached) {
            }
            return sink.bytes() to sink.truncated
        }
    }
    if (item.remotePath.isBlank()) return ByteArray(0) to false
    val api = client ?: return ByteArray(0) to false
    val sink = CappedSink(limit)
    return try {
        api.download(item.remotePath, sink)
        sink.bytes() to false
    } catch (ignored: PreviewBytesReached) {
        sink.bytes() to true
    }
}

/** 闻内容：本机文件读前 [limit] 个字节（读不出来就返回 null，交给扩展名/MIME 判）。 */
private fun readLocalHead(context: android.content.Context, uri: Uri, limit: Int): ByteArray? = try {
    context.contentResolver.openInputStream(uri)?.use { stream ->
        val buffer = ByteArray(limit)
        var filled = 0
        while (filled < limit) {
            val read = stream.read(buffer, filled, limit - filled)
            if (read <= 0) break
            filled += read
        }
        buffer.copyOf(filled)
    }
} catch (ignored: Exception) {
    null
}

/**
 * 方块上的缩略图：图片给真缩略图、视频给封面帧；拿不到就返回 null（界面画类型图标）。
 *
 * 先走系统相册那条路（`ContentResolver.loadThumbnail`）：图片和视频它都能给一帧，
 * 而且不用把整张原图读进内存。不成就退回解码原图 / `MediaMetadataRetriever`。
 */
private fun thumbnailFor(
    context: android.content.Context,
    uri: Uri,
    kind: String,
    sizePx: Int,
): Bitmap? {
    if (kind != "image" && kind != "video") return null
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
        try {
            context.contentResolver.loadThumbnail(uri, android.util.Size(sizePx, sizePx), null)?.let { return it }
        } catch (ignored: Exception) {
        }
    }
    if (kind == "image") {
        return try {
            context.contentResolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it) }
        } catch (ignored: Exception) {
            null
        }
    }
    return videoFrameOf(context, uri.toString(), "")
}

/** 取视频的封面帧（本机给 content URI，远端给 http 地址）。 */
private fun videoFrameOf(context: android.content.Context, source: String, token: String): Bitmap? {
    if (source.isBlank()) return null
    val retriever = MediaMetadataRetriever()
    return try {
        if (source.startsWith("http", ignoreCase = true)) {
            val headers = mutableMapOf("User-Agent" to ApiClient.userAgent())
            if (token.isNotBlank()) headers["X-EverSend-Token"] = token
            retriever.setDataSource(source, headers)
        } else {
            retriever.setDataSource(context, Uri.parse(source))
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            retriever.getScaledFrameAtTime(0, MediaMetadataRetriever.OPTION_CLOSEST_SYNC, 640, 360)
        } else {
            @Suppress("DEPRECATION")
            retriever.getFrameAtTime(0, MediaMetadataRetriever.OPTION_CLOSEST_SYNC)
        }
    } catch (ignored: Exception) {
        null
    } finally {
        try {
            retriever.release()
        } catch (ignored: Exception) {
        }
    }
}

/** 时长的秒数（封面帧那条路顺手问一下，拿不到就返回 0）。 */
private fun durationSecondsOf(context: android.content.Context, source: String, token: String): Int {
    if (source.isBlank()) return 0
    val retriever = MediaMetadataRetriever()
    return try {
        if (source.startsWith("http", ignoreCase = true)) {
            val headers = mutableMapOf("User-Agent" to ApiClient.userAgent())
            if (token.isNotBlank()) headers["X-EverSend-Token"] = token
            retriever.setDataSource(source, headers)
        } else {
            retriever.setDataSource(context, Uri.parse(source))
        }
        (retriever.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)?.toLongOrNull() ?: 0L)
            .div(1000).toInt()
    } catch (ignored: Exception) {
        0
    } finally {
        try {
            retriever.release()
        } catch (ignored: Exception) {
        }
    }
}

/** 播放地址：优先用消息附件那条免令牌的 URL，其次本机 content URI，最后拼远端路径。 */
private fun playbackUrl(item: PreviewItem, client: ApiClient?): String {
    if (item.playUrl.isNotBlank()) return item.playUrl
    item.localUri?.let { return it.toString() }
    if (client != null && item.remotePath.isNotBlank()) return client.base.trimEnd('/') + item.remotePath
    return ""
}

/**
 * 交给内置播放器的地址（和 [playbackUrl] 的差别只有本机文件那一条）。
 *
 * 选择器给的是 DocumentsProvider 的 `content://…/document/video%3A123`，libVLC 的
 * content 访问模块打不开它（真机上实测："VLC is unable to open the MRL"），而系统
 * 解码器（拿封面帧用的 MediaMetadataRetriever）认得。所以本地文件先落一份到缓存目录，
 * 用 `file://` 播；返回的第二个值是那份缓存文件，弹层关掉时删掉。
 */
private fun playableUrl(
    context: android.content.Context,
    item: PreviewItem,
    client: ApiClient?,
): Pair<String, File?> {
    val local = item.localUri ?: return playbackUrl(item, client) to null
    // 特别大的文件不往缓存里搬：为了"看一眼"把手机塞满不划算，宁可让它播不了。
    if (item.size > 2L * 1024 * 1024 * 1024) return local.toString() to null
    val safe = item.name.ifBlank { "media" }.replace('/', '_').replace('\\', '_')
    val target = File(context.cacheDir, "preview-$safe")
    return try {
        context.contentResolver.openInputStream(local)?.use { input ->
            target.outputStream().use { output -> input.copyTo(output, 64 * 1024) }
        }
        Uri.fromFile(target).toString() to target
    } catch (problem: Exception) {
        // 落盘失败（空间不够等）也别装作没事：退回原地址，让播放器自己报错。
        local.toString() to null
    }
}

private fun toast(context: android.content.Context, message: String) {
    android.widget.Toast.makeText(context, message, android.widget.Toast.LENGTH_LONG).show()
}

/**
 * §6 的预览弹层：能看的直接看，不能看的说清楚。
 *
 * | 类型 | 给什么 |
 * |---|---|
 * | 图片 | 铺满弹层，点一下在「适应窗口 / 原始大小」之间切换，可保存到手机 |
 * | 视频 | 封面帧 + 「▶ 播放」（内置 libVLC）+ 时长 |
 * | 音频/语音 | 「▶ 播放」 + 时长 |
 * | 文本/代码 | 前 256 KB 等宽只读、可选中；超出就在末尾说明"只显示开头一段" |
 * | 其它二进制 | 不装作能看：类型图标 + 名字 + 大小 + 「保存到手机 / 用系统程序打开」 |
 *
 * 三个入口共用它：待发方块、气泡上的「预览」、接收表历史行的「预览」。
 */
@Composable
fun FilePreviewDialog(item: PreviewItem, client: ApiClient?, onClose: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    // 判定顺序照 §6：扩展名优先，其次闻内容（先取前 4 KB）。
    var kind by remember(item.key) { mutableStateOf("") }
    var videoLayout by remember(item.key) { mutableStateOf<org.videolan.libvlc.util.VLCVideoLayout?>(null) }
    var playing by remember(item.key) { mutableStateOf(false) }
    var player by remember(item.key) { mutableStateOf<VlcHolder.Player?>(null) }
    // 交给播放器的地址（本地文件要先落一份到缓存，见 playableUrl），
    // 以及那份缓存文件 —— 关掉弹层就删掉，别攒在手机上。
    var playUrl by remember(item.key) { mutableStateOf("") }
    var temporary by remember(item.key) { mutableStateOf<File?>(null) }

    LaunchedEffect(item.key, client) {
        val head = withContext(Dispatchers.IO) {
            try {
                readPreviewBytes(context, client, item, SNIFF_BYTES.toLong()).first
            } catch (problem: Exception) {
                ByteArray(0)
            }
        }
        kind = previewKindOf(item.name, head, hint = item.kindHint)
    }

    fun stopPlayback() {
        try {
            player?.stop()
        } catch (ignored: Exception) {
        }
        player = null
        playing = false
        videoLayout?.let { VlcHolder.detach(it) }
    }

    fun close() {
        stopPlayback()
        try {
            temporary?.delete()
        } catch (ignored: Exception) {
        }
        temporary = null
        onClose()
    }

    Dialog(
        onDismissRequest = { close() },
        // 默认的弹窗宽度只有 280dp 上下，图片/文本预览要的是"铺满"。
        properties = DialogProperties(usePlatformDefaultWidth = false),
    ) {
        Box(Modifier.fillMaxSize().padding(10.dp)) {
            Card(Modifier.fillMaxSize()) {
                Column(
                    Modifier.fillMaxSize().padding(12.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    // 标题行：类型图标 + 名字 + 事实（类型 · 大小 · 时长 · 出处）。
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(tileGlyph(item.name, kind), fontSize = 18.sp)
                        Spacer(Modifier.width(8.dp))
                        Column(Modifier.weight(1f)) {
                            Text(
                                item.name,
                                fontWeight = FontWeight.SemiBold,
                                fontSize = 15.sp,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                                modifier = Modifier.testTag("viewer-title"),
                            )
                            Text(
                                previewFacts(item, kind),
                                fontSize = 10.sp,
                                color = Es.Dim,
                                maxLines = 2,
                                overflow = TextOverflow.Ellipsis,
                            )
                        }
                    }

                    Box(Modifier.weight(1f).fillMaxWidth()) {
                        when (kind) {
                            "" -> Text("正在看这个文件是什么…", fontSize = 12.sp, color = Es.Muted)
                            "image" -> PreviewImageBody(item = item, client = client)
                            "video", "audio" -> PreviewMediaBody(
                                item = item,
                                client = client,
                                video = kind == "video",
                                playing = playing,
                                playUrl = playUrl,
                                onLayout = { videoLayout = it },
                            )
                            "text" -> PreviewTextBody(item = item, client = client)
                            else -> PreviewBinaryBody(item = item)
                        }
                    }

                    // 按钮：能力不同的地方如实写（内置播放器 / 交给系统程序）。
                    Row(
                        Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        if (kind == "video" || kind == "audio") {
                            OutlinedButton(
                                onClick = {
                                    if (playing) {
                                        stopPlayback()
                                    } else {
                                        // 本地文件要先落一份到缓存再用播放器打开
                                        // （见 playableUrl），这一步是 IO。
                                        scope.launch {
                                            val prepared = withContext(Dispatchers.IO) {
                                                playableUrl(context, item, client)
                                            }
                                            val url = prepared.first
                                            if (url.isBlank()) {
                                                toast(context, "还没有连上电脑")
                                                return@launch
                                            }
                                            temporary = prepared.second
                                            playUrl = url
                                            if (kind == "audio") {
                                                // libVLC：自带 opus/webm/m4a 解码，网页录的
                                                // webm/opus 与手机录的 m4a 都能放。
                                                val vlc = VlcHolder.player(context) { problem ->
                                                    playing = false
                                                    toast(context, "播放失败：$problem")
                                                }
                                                if (vlc == null) {
                                                    toast(context, "内置播放器初始化失败")
                                                } else {
                                                    player = vlc
                                                    playing = true
                                                    vlc.play(url) { playing = false }
                                                }
                                            } else {
                                                // 视频在下面的 AndroidView 里起播。
                                                playing = true
                                            }
                                        }
                                    }
                                },
                                modifier = Modifier.testTag("preview-play"),
                            ) { Text(if (playing) "⏹ 停止" else "▶ 播放") }
                        }
                        if (kind != "video" && kind != "audio") {
                            OutlinedButton(
                                onClick = {
                                    scope.launch { toast(context, savePreview(context, client, item)) }
                                },
                                modifier = Modifier.testTag("preview-save"),
                            ) { Text("保存到手机") }
                        }
                        OutlinedButton(
                            onClick = {
                                scope.launch { toast(context, openWithSystem(context, client, item)) }
                            },
                            modifier = Modifier.testTag("preview-open-system"),
                        ) { Text("用系统程序打开") }
                        Button(
                            onClick = { close() },
                            modifier = Modifier.testTag("preview-close"),
                        ) { Text("关闭") }
                    }
                }
            }
        }
    }
}

/** 弹层顶上那行事实：类型 · 大小 · 时长 · 出处。 */
private fun previewFacts(item: PreviewItem, kind: String): String {
    val parts = mutableListOf(previewTypeLabel(kind))
    fmtSize(item.size).takeIf { it.isNotBlank() }?.let { parts.add(it) }
    if (item.facts.isNotBlank()) parts.add(item.facts)
    if (item.localUri != null) parts.add("待发送") else if (item.remotePath.isNotBlank()) parts.add("来自电脑")
    return parts.joinToString("  ·  ")
}

private fun previewTypeLabel(kind: String): String = when (kind) {
    "image" -> "图片"
    "video" -> "视频"
    "audio" -> "音频"
    "text" -> "文本"
    else -> "二进制文件"
}

/** 图片：铺满弹层，点一下在「适应窗口 / 原始大小」之间切换（和电脑端一个手感）。 */
@Composable
private fun PreviewImageBody(item: PreviewItem, client: ApiClient?) {
    val context = LocalContext.current
    val density = LocalDensity.current.density
    var bitmap by remember(item.key) { mutableStateOf<Bitmap?>(null) }
    var failed by remember(item.key) { mutableStateOf(false) }
    var original by remember(item.key) { mutableStateOf(false) }

    LaunchedEffect(item.key, client) {
        val loaded = withContext(Dispatchers.IO) {
            try {
                val (bytes, _) = readPreviewBytes(context, client, item, MEDIA_PREVIEW_BYTES)
                BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
            } catch (problem: Exception) {
                null
            }
        }
        bitmap = loaded
        if (loaded == null) failed = true
    }

    val image = bitmap
    when {
        image != null -> Column(Modifier.fillMaxSize()) {
            Box(
                Modifier
                    .weight(1f)
                    .fillMaxWidth()
                    .background(Es.Bg, RoundedCornerShape(8.dp))
                    .clickable { original = !original },
                contentAlignment = Alignment.Center,
            ) {
                if (original) {
                    // 原始大小：1 个图片像素对 1 个屏幕像素，超出的部分自己滚。
                    Box(
                        Modifier
                            .fillMaxSize()
                            .verticalScroll(rememberScrollState())
                            .horizontalScroll(rememberScrollState()),
                    ) {
                        Image(
                            bitmap = image.asImageBitmap(),
                            contentDescription = item.name,
                            contentScale = ContentScale.None,
                            modifier = Modifier
                                .size((image.width / density).dp, (image.height / density).dp)
                                .testTag("viewer-image"),
                        )
                    }
                } else {
                    Image(
                        bitmap = image.asImageBitmap(),
                        contentDescription = item.name,
                        contentScale = ContentScale.Fit,
                        modifier = Modifier.fillMaxSize().testTag("viewer-image"),
                    )
                }
            }
            Text(
                "点一下在「适应窗口 / 原始大小」之间切换（当前：${if (original) "原始大小" else "适应窗口"}）",
                fontSize = 10.sp,
                color = Es.Dim,
            )
        }
        failed -> Text(
            "这个图片打不开（文件可能坏了，或者格式系统不认识）。点「保存到手机」留一份再看。",
            fontSize = 12.sp,
            color = Es.Muted,
        )
        else -> Text("载入中…", fontSize = 12.sp, color = Es.Muted)
    }
}

/** 视频：封面帧 + 「▶ 播放」；音频/语音：「▶ 播放」 + 时长（§6）。 */
@Composable
private fun PreviewMediaBody(
    item: PreviewItem,
    client: ApiClient?,
    video: Boolean,
    playing: Boolean,
    playUrl: String,
    onLayout: (org.videolan.libvlc.util.VLCVideoLayout) -> Unit,
) {
    val context = LocalContext.current
    // 封面帧/时长走系统解码器：它认 content URI，所以这里用原始地址。
    val url = playbackUrl(item, client)
    var poster by remember(item.key) { mutableStateOf<Bitmap?>(null) }
    var seconds by remember(item.key) { mutableStateOf(item.durationMs / 1000) }

    LaunchedEffect(item.key, client) {
        val loaded = withContext(Dispatchers.IO) {
            var frame: Bitmap? = null
            var length = item.durationMs / 1000
            if (video && url.isNotBlank()) {
                val token = client?.token.orEmpty()
                frame = videoFrameOf(context, url, token)
                if (length <= 0) length = durationSecondsOf(context, url, token)
            }
            Pair(frame, length)
        }
        poster = loaded.first
        seconds = loaded.second
    }

    Column(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Box(
            Modifier
                .weight(1f)
                .fillMaxWidth()
                .clip(RoundedCornerShape(8.dp))
                .background(Es.Bg),
            contentAlignment = Alignment.Center,
        ) {
            if (video && playing) {
                // 视频用 libVLC 渲染：MKV/HEVC/VP9 这些安卓不保证能解的格式，
                // 靠它自带的 FFmpeg 软解，不看设备的脸色。
                AndroidView(
                    factory = { ctx ->
                        org.videolan.libvlc.util.VLCVideoLayout(ctx).also { layout ->
                            onLayout(layout)
                            VlcHolder.attach(context, layout, playUrl) { problem ->
                                toast(context, "播放失败：$problem")
                            }
                        }
                    },
                    modifier = Modifier.fillMaxSize().testTag("viewer-video"),
                    update = { },
                )
            } else if (video) {
                val frame = poster
                if (frame != null) {
                    Image(
                        bitmap = frame.asImageBitmap(),
                        contentDescription = item.name,
                        contentScale = ContentScale.Fit,
                        modifier = Modifier.fillMaxSize().testTag("viewer-poster"),
                    )
                } else {
                    Text("🎬", fontSize = 40.sp)
                }
            } else {
                Text("🎤", fontSize = 44.sp)
            }
        }
        Text(
            buildString {
                append(if (video) "视频" else "音频 / 语音")
                if (seconds > 0) append(" · ${seconds} 秒")
                append(
                    if (video) {
                        " · 点「▶ 播放」用内置播放器播放（libVLC，自带 FFmpeg 解码）"
                    } else {
                        " · 点「▶ 播放」用内置播放器播放"
                    },
                )
            },
            fontSize = 10.sp,
            color = Es.Muted,
        )
    }
}

/** 文本 / 代码：前 256 KB 等宽只读、可选中；超出就在末尾说明只显示开头一段（§6）。 */
@Composable
private fun PreviewTextBody(item: PreviewItem, client: ApiClient?) {
    val context = LocalContext.current
    var text by remember(item.key) { mutableStateOf<String?>(null) }
    var truncated by remember(item.key) { mutableStateOf(false) }
    var failed by remember(item.key) { mutableStateOf(false) }

    LaunchedEffect(item.key, client) {
        val loaded = withContext(Dispatchers.IO) {
            try {
                val (bytes, more) = readPreviewBytes(context, client, item, TEXT_PREVIEW_BYTES.toLong())
                Pair(previewTextOf(bytes), more)
            } catch (problem: Exception) {
                null
            }
        }
        if (loaded == null) {
            failed = true
        } else {
            text = loaded.first
            truncated = loaded.second
        }
    }

    val body = text
    Column(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Box(
            Modifier
                .weight(1f)
                .fillMaxWidth()
                .background(Es.Bg, RoundedCornerShape(8.dp))
                .padding(8.dp),
        ) {
            when {
                body != null -> {
                    // 逐行丢进 LazyColumn：256 KB 一次性交给 Text 的话，光排版就把
                    // 主线程卡住好几秒（模拟器上实测掉了 321 帧）。懒加载只排可见的
                    // 那几十行，可滚动的内容仍然是整份。
                    val lines = remember(body) { body.replace("\r\n", "\n").split('\n') }
                    val widest = remember(lines) { lines.maxOfOrNull { it.length } ?: 0 }
                    // 等宽 11sp 的字符宽约 6.6dp；超长行（压缩过的 js）给个上限，
                    // 免得算出一个天文数字的滚动宽度。
                    val contentWidth = ((widest.coerceAtMost(2000) * 6.6f) + 20f).dp
                    Box(Modifier.fillMaxSize().horizontalScroll(rememberScrollState())) {
                        SelectionContainer {
                            LazyColumn(
                                Modifier
                                    .width(contentWidth)
                                    .fillMaxHeight()
                                    .testTag("preview-text"),
                            ) {
                                items(lines.size) { index ->
                                    Text(
                                        lines[index],
                                        fontFamily = FontFamily.Monospace,
                                        fontSize = 11.sp,
                                        lineHeight = 15.sp,
                                        maxLines = 1,
                                        softWrap = false,
                                    )
                                }
                            }
                        }
                    }
                }
                failed -> Text("这个文件读不出来（可能已经不在了）。", fontSize = 12.sp, color = Es.Muted)
                else -> Text("载入中…", fontSize = 12.sp, color = Es.Muted)
            }
        }
        if (truncated) {
            Text(
                "…（文件比 256 KB 大，这里只显示开头一段；要看全文请点「保存到手机」或「用系统程序打开」）",
                fontSize = 10.sp,
                color = Es.Warn,
                modifier = Modifier.testTag("preview-truncated"),
            )
        }
    }
}

/** 其它二进制：**不装作能看** —— 类型图标 + 名字 + 大小 + 两个出口（§6）。 */
@Composable
private fun PreviewBinaryBody(item: PreviewItem) {
    Column(
        Modifier.fillMaxSize(),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text(tileGlyph(item.name), fontSize = 44.sp)
        Text(
            "这类文件没有可预览的内容",
            fontSize = 15.sp,
            fontWeight = FontWeight.SemiBold,
            modifier = Modifier.testTag("preview-binary"),
        )
        Text(
            "${item.name}${fmtSize(item.size).takeIf { it.isNotBlank() }?.let { "（$it）" }.orEmpty()}" +
                " 是二进制文件，界面里看不出内容。\n" +
                "点「用系统程序打开」交给本机对应的程序，或者「保存到手机」留一份。",
            fontSize = 11.sp,
            color = Es.Muted,
            textAlign = TextAlign.Center,
            modifier = Modifier.padding(horizontal = 15.dp),
        )
    }
}

/**
 * 「保存到手机」：写进系统「下载」目录。
 *
 * 本机文件直接拷，远端走已有下载接口（和气泡里的「保存」是同一条路，
 * 没有为预览新增接口）。
 */
private suspend fun savePreview(
    context: android.content.Context,
    client: ApiClient?,
    item: PreviewItem,
): String = withContext(Dispatchers.IO) {
    val name = item.name.ifBlank { "eversend-preview" }
    val saved = try {
        saveToDownloads(context, name) { sink ->
            val local = item.localUri
            if (local != null) {
                context.contentResolver.openInputStream(local)?.use { it.copyTo(sink, 64 * 1024) }
            } else {
                val api = client ?: throw IllegalStateException("还没连上电脑")
                api.download(item.remotePath, sink)
            }
        }
    } catch (problem: Exception) {
        null
    }
    if (saved == null) "保存失败（存储权限或空间不足）" else "已保存到「下载」：$name"
}

/**
 * 「用系统程序打开」。
 *
 * 本机文件把 content URI 直接交给系统；远端文件先落到「下载」，再用那个 URI 打开
 * （只有落了盘，别的程序才有一个读得到的地址）。打不开就说清文件在哪，不硬撑。
 */
private suspend fun openWithSystem(
    context: android.content.Context,
    client: ApiClient?,
    item: PreviewItem,
): String {
    val local = item.localUri
    if (local != null) {
        return try {
            context.startActivity(
                Intent(Intent.ACTION_VIEW)
                    .setDataAndType(local, mimeOf(context, item.name, local))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_GRANT_READ_URI_PERMISSION),
            )
            "已交给系统程序打开"
        } catch (problem: Exception) {
            "这台手机上还没有能打开它的程序，先「保存到手机」留一份吧"
        }
    }
    val name = item.name.ifBlank { "eversend-preview" }
    val saved = withContext(Dispatchers.IO) {
        try {
            saveToDownloads(context, name) { sink ->
                val api = client ?: throw IllegalStateException("还没连上电脑")
                api.download(item.remotePath, sink)
            }
        } catch (problem: Exception) {
            null
        }
    } ?: return "保存失败（存储权限或空间不足）"
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
        // 老系统上 file:// 的地址不能跨程序给（FileUriExposedException），
        // 说清文件在哪比崩一下好。
        return "已保存到「下载」：$name"
    }
    return try {
        context.startActivity(
            Intent(Intent.ACTION_VIEW)
                .setDataAndType(saved, mimeOf(context, item.name, saved))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_GRANT_READ_URI_PERMISSION),
        )
        "已保存到「下载」并交给系统程序：$name"
    } catch (problem: Exception) {
        "已保存到「下载」：$name"
    }
}

/** 交给系统程序时用的 MIME：先问系统，再按扩展名猜，最后兜底成二进制流。 */
private fun mimeOf(context: android.content.Context, name: String, uri: Uri): String {
    val fromResolver = try {
        context.contentResolver.getType(uri)
    } catch (ignored: Exception) {
        null
    }
    if (!fromResolver.isNullOrBlank()) return fromResolver
    return android.webkit.MimeTypeMap.getSingleton().getMimeTypeFromExtension(extensionOf(name))
        ?: "application/octet-stream"
}

/** 一个简单的信息弹窗（手动添加电脑 / 手机连接）。 */
@Composable
private fun InfoDialog(title: String, onClose: () -> Unit, body: @Composable ColumnScope.() -> Unit) {
    Dialog(onDismissRequest = onClose) {
        Card(Modifier.fillMaxWidth().padding(8.dp)) {
            Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(title, fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
                body()
                TextButton(onClick = onClose, modifier = Modifier.testTag("dialog-close")) {
                    Text("知道了")
                }
            }
        }
    }
}

/**
 * 确认弹窗：一个「确认」、一个「取消」。
 *
 * 为什么不复用 [InfoDialog]：它的收尾按钮叫「知道了」（纯告知），而这个框要用户
 * **做决定** —— 「清空离线设备」点了就生效，让人以为点「知道了」也会清、或者以为
 * 点了「知道了」就等于清过了，两种误会都很难查。
 */
@Composable
private fun ConfirmDialog(
    title: String,
    confirmLabel: String,
    confirmTag: String,
    cancelTag: String,
    onConfirm: () -> Unit,
    onCancel: () -> Unit,
    body: @Composable ColumnScope.() -> Unit,
) {
    Dialog(onDismissRequest = onCancel) {
        Card(Modifier.fillMaxWidth().padding(8.dp)) {
            Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(title, fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
                body()
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = onConfirm, modifier = Modifier.testTag(confirmTag)) {
                        Text(confirmLabel)
                    }
                    OutlinedButton(onClick = onCancel, modifier = Modifier.testTag(cancelTag)) {
                        Text("取消")
                    }
                }
            }
        }
    }
}

/**
 * 表情面板：整份表情表（三端共用，来自电脑端的 `/api/emoji`），分组、**可上下滑动**。
 *
 * 以前是写死在代码里的 48 个，用户说太少。现在拉电脑端那同一份表（服务端从
 * `core/emoji.py` 生成，桌面端也用它），拉不到就退回内置的一小组，离线也不至于
 * 没有表情可用。发出去的就是普通 Unicode 字符，跨设备显示一定一致。
 *
 * 它是输入区里的一块**内嵌面板**（不是弹窗，规格 §3⑥ 第 3 条）：宽度和输入行
 * 一致 —— 外面那层 `6.dp` 内缩和输入行用的是同一组数值，所以两边的左右边缘
 * 正好对齐；高度有上限，超出在面板里滚动。
 */
@Composable
fun EmojiPad(client: ApiClient?, onPick: (String) -> Unit) {
    var groups by remember { mutableStateOf<List<Pair<String, List<String>>>>(emptyList()) }
    var loading by remember { mutableStateOf(true) }

    LaunchedEffect(Unit) {
        val loaded = withContext(Dispatchers.IO) {
            try {
                val active = client ?: return@withContext emptyList<Pair<String, List<String>>>()
                val payload = active.getJson("/api/emoji")
                val array = payload.optJSONArray("groups")
                (0 until (array?.length() ?: 0)).mapNotNull { index ->
                    val group = array?.optJSONObject(index) ?: return@mapNotNull null
                    val title = group.optString("title")
                    val emoji = group.optJSONArray("emoji") ?: return@mapNotNull null
                    val list = (0 until emoji.length()).map { emoji.optString(it) }
                    if (title.isBlank() || list.isEmpty()) null else title to list
                }
            } catch (ignored: Exception) {
                emptyList()
            }
        }
        groups = loaded.ifEmpty { BUILTIN_EMOJI }
        loading = false
    }

    // 宽度对齐「输入框那一行」：同一个 6dp 左右内缩（输入行是
    // padding(horizontal = 6.dp, vertical = 4.dp)）。两边用同一个常数，
    // 改宽度时两处一起改，面板就不会比输入行宽出去一块。
    Box(
        Modifier
            .fillMaxWidth()
            .padding(start = 6.dp, end = 6.dp, top = 4.dp)
            .testTag("emoji-inline"),
    ) {
        if (loading) {
            Text("表情加载中…", fontSize = 12.sp, color = Es.Muted, modifier = Modifier.padding(8.dp))
            return@Box
        }
        // 一格放多大：用户嫌小（"眼睛不好的人看不清"），所以格子**最少 48dp**、
        // 表情字号 26sp（原来是 9 列硬铺 380dp ≈ 42dp 一格 + 22sp 字）。
        // 一行放几个由可用宽度算出来 —— 窄屏自然变成 6~8 个，不硬塞 9 列。
        BoxWithConstraints(Modifier.fillMaxWidth()) {
            val columns = emojiColumns(maxWidth)
            // 整个面板自己滚：往下滑就是更多表情（用户要的"上下滑动加载更多"）。
            // 高度上限放大到 240dp（格子变大之后 200dp 只看得到两行半），
            // 再展开也不会把上面的会话区挤没。
            LazyColumn(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(max = EMOJI_PAD_MAX)
                    .clip(RoundedCornerShape(12.dp))
                    .background(Es.SurfaceAlt)
                    .border(1.dp, Es.Border, RoundedCornerShape(12.dp))
                    .testTag("emoji-pad"),
            ) {
                groups.forEach { (title, emoji) ->
                    item(key = "title-$title") {
                        Text(
                            "$title（${emoji.size}）",
                            fontSize = 11.sp,
                            color = Es.Muted,
                            modifier = Modifier.padding(start = 8.dp, top = 8.dp, bottom = 4.dp),
                        )
                    }
                    items(emoji.chunked(columns)) { row ->
                        Row(Modifier.fillMaxWidth()) {
                            // 一行里每个表情等分宽度：整片网格正好铺满面板宽度
                            // （也就等于输入行宽度），窄屏上不会挤出边界；
                            // 每格自身高 ≥48dp，格子里一个 EMOJI_FONT_SIZE 的表情居中。
                            row.forEach { emoji ->
                                Box(
                                    Modifier
                                        .weight(1f)
                                        .heightIn(min = EMOJI_CELL_MIN)
                                        .clickable { onPick(emoji) }
                                        .testTag("emoji-$emoji"),
                                    contentAlignment = Alignment.Center,
                                ) {
                                    Text(emoji, fontSize = EMOJI_FONT_SIZE, maxLines = 1)
                                }
                            }
                            // 最后一行不满时补空位，表情仍然按列对齐。
                            repeat(columns - row.size) { Spacer(Modifier.weight(1f)) }
                        }
                    }
                }
            }
        }
    }
}

/** 表情格子最小边长：48dp（安卓可点区域的下限，用户要的"看得清"）。 */
private val EMOJI_CELL_MIN = 48.dp

/**
 * **emoji 的唯一字号**：表情面板的格子、输入框里已经打进去的、消息气泡里的，
 * 全都用这一个值 —— 用户的要求是"选择框和消息里显示大小要一致，而且再大一点"。
 *
 * 30sp 是比正文（15sp）大一倍的位置：同一个 😂 在面板里、在输入框里、发出去以后
 * 在气泡里占的宽度一模一样。改这里三处一起变。
 */
private val EMOJI_FONT_SIZE = 30.sp

/** 表情面板的高度上限：240dp，超出在面板里滚（格子变大后 200dp 只看得见两行）。 */
private val EMOJI_PAD_MAX = 240.dp

/** 这个码位是不是 emoji（够用的近似集合：常见表情/手势/国旗/符号都覆盖到）。 */
private fun isEmojiCodePoint(code: Int): Boolean = when (code) {
    in 0x1F000..0x1FAFF -> true      // 笑脸、手势、动物、食物、交通、旗帜…
    in 0x1F1E6..0x1F1FF -> true      // 区域指示符（国旗就是两个拼起来的）
    in 0x2600..0x27BF -> true        // ☀ ✅ ✂ ❤ ✌ ⚠ …
    in 0x2B00..0x2BFF -> true        // ⬛ ⭐ ⬅ …
    0x203C, 0x2049, 0x2122, 0x2139, 0x2194, 0x2195, 0x2196, 0x2197, 0x2198, 0x2199,
    0x21A9, 0x21AA, 0x231A, 0x231B, 0x2328, 0x23CF, 0x23E9, 0x23EA, 0x23EB, 0x23EC,
    0x23ED, 0x23EE, 0x23EF, 0x23F0, 0x23F1, 0x23F2, 0x23F3, 0x23F8, 0x23F9, 0x23FA,
    0x24C2, 0x25AA, 0x25AB, 0x25B6, 0x25C0, 0x25FB, 0x25FC, 0x25FD, 0x25FE,
    0x2934, 0x2935, 0x2B05, 0x2B06, 0x2B07, 0x3030, 0x303D, 0x3297, 0x3299,
    -> true
    else -> false
}

/** 把 emoji 连成一串的字符：ZWJ（👨👩👧）、变体选择符（️）、肤色、键帽。 */
private fun isEmojiJoiner(code: Int): Boolean =
    code == 0x200D || code in 0xFE00..0xFE0F || code in 0x1F3FB..0x1F3FF || code == 0x20E3

/**
 * 把文字里的 emoji 放大到 [EMOJI_FONT_SIZE]，其余字符原样。
 *
 * 表情面板里那个 26sp（现在 30sp）的 😂，跟着文字发出去以后会被正文的 15sp 拖着
 * 变小 —— 用户看到的就是"选的时候挺大，发出去变小了"。这里按码位把 emoji 段挑出来
 * 单独给字号（连着的 emoji、ZWJ 组合、肤色修饰都算一段，不会被拆开）。
 *
 * 只加样式、不改字符，所以下标和原文一一对应，光标/选择区不受影响。
 */
fun emojiSized(text: String): AnnotatedString {
    if (text.isEmpty()) return AnnotatedString("")
    val builder = AnnotatedString.Builder(text)
    var index = 0
    while (index < text.length) {
        val code = text.codePointAt(index)
        if (!isEmojiCodePoint(code)) {
            index += Character.charCount(code)
            continue
        }
        var end = index + Character.charCount(code)
        while (end < text.length) {
            val next = text.codePointAt(end)
            if (isEmojiCodePoint(next) || isEmojiJoiner(next)) {
                end += Character.charCount(next)
            } else {
                break
            }
        }
        builder.addStyle(SpanStyle(fontSize = EMOJI_FONT_SIZE), index, end)
        index = end
    }
    return builder.toAnnotatedString()
}

/**
 * 输入框里也按同一号字显示 emoji。
 *
 * 只是"看得见的样式"，值还是那个普通 String（[OffsetMapping.Identity]：字符一个
 * 没多一个没少，光标和选择区的位置不变），所以发出去的内容不受影响。
 */
private object EmojiSizeTransform : VisualTransformation {
    override fun filter(text: AnnotatedString): TransformedText =
        TransformedText(emojiSized(text.text), OffsetMapping.Identity)
}

/**
 * 一行放几个表情：按可用宽度除以 [EMOJI_CELL_MIN]（48dp）算，所以**每格一定
 * ≥48dp**。380dp 的窄屏得到 7 列（约 52dp 一格），平板宽起来自然多放几个，
 * 上限 12 列（再多一格就比 48dp 大太多了，眼睛要来回扫）。
 */
private fun emojiColumns(width: Dp): Int {
    val fits = (width / EMOJI_CELL_MIN).toInt()
    return fits.coerceIn(4, 12)
}

/** 拉不到电脑端的表情表时用这一小组（保证离线也能发表情）。 */
val BUILTIN_EMOJI: List<Pair<String, List<String>>> = listOf(
    "常用" to listOf(
        "😀", "😂", "🥹", "😊", "😍", "😘", "🤔", "😴",
        "😎", "🤩", "😭", "😅", "🙃", "😇", "🥳", "🤝",
        "👍", "👎", "👌", "🙏", "👏", "💪", "🤙", "✌️",
        "❤️", "💔", "🔥", "✨", "🎉", "🎁", "⭐", "💡",
        "✅", "❌", "⚠️", "❓", "❗", "📎", "📷", "🎬",
        "🎵", "🎤", "💻", "📱", "🖥️", "📁", "📄", "🗑️",
    ),
)

// ------------------------------------------------------------- 数据与动作

/** 一次轮询读回来的全部东西：IO 线程读，主线程喂给界面。 */
class Snapshot(
    val devices: List<DeviceRow>,
    val conversations: List<Conversation>,
    val conversationId: String,
    val messages: List<ChatMessage>,
    val transfers: List<JSONObject>,
    val uploads: List<JSONObject>,
    val recentUploads: List<JSONObject>,
    val offers: List<JSONObject>,
    val shares: List<JSONObject>,
    val computerFiles: List<JSONObject>,
    val receiveDir: String,
    /** `/api/chat` 里那句"我是谁"：消息方向按它算（见 [messageFrom]）。 */
    val selfId: String = "",
)

/**
 * 消息里存的 `senderName` 是**发信设备自称的名字**（手机上就是 V2408A）。
 *
 * 历史消息里存的是"安卓 App/网页版"这种**客户端类型** —— 那不是名字。显示时兜底
 * 成设备名（电脑端 `_sender_label` 是同一套规则：先在设备表里找这台设备，找不到
 * 才退回原来那个词）。
 */
private val GENERIC_SENDER_NAMES = setOf(
    "安卓 App", "网页版", "手机", "未知客户端", "浏览器", "命令行/脚本", "未知设备",
)

fun senderLabel(sender: String, storedName: String, devices: List<DeviceRow>, store: AppState): String {
    val stored = storedName.trim()
    if (stored.isNotEmpty() && stored !in GENERIC_SENDER_NAMES) return stored
    // 自己发的那条：署名就是这台手机的名字。
    if (sender.isNotBlank() && aliasOf(sender) == aliasOf(store.deviceId)) return store.deviceName
    // 设备列表里能对上号的：用那台设备自称的名字（V2408A），而不是"安卓 App"。
    val row = devices.firstOrNull { it.id == sender }
        ?: devices.firstOrNull { aliasOf(it.id) == aliasOf(sender) }
    if (row != null && row.name.isNotBlank() && row.name !in GENERIC_SENDER_NAMES) return row.name
    return stored.ifBlank { "对方" }
}

/**
 * 这条消息是不是"我发的"。
 *
 * 库里存的 `direction` 是**电脑视角**的（手机发过去的记成 "in"），所以谁在看就
 * 按 `sender == selfId` 算 —— 不然手机自己发的每一句都会被画成"收到"。
 */
fun outgoingOf(message: JSONObject, selfId: String): Boolean {
    val sender = message.optString("sender")
    if (selfId.isNotBlank() && sender.isNotBlank()) return sender == selfId
    return message.optString("direction") == "out"
}

/**
 * 一条消息 → 界面用的视图模型。
 *
 * 署名兜底和方向都定在这里（不是改数据库、也不是让服务端重写历史），所以老会话里
 * 那些"安卓 App"现在显示的是设备名。
 */
fun messageFrom(
    obj: JSONObject,
    selfId: String,
    devices: List<DeviceRow>,
    store: AppState,
): ChatMessage = ChatMessage(
    id = obj.optString("id"),
    conversation = obj.optString("conv"),
    senderName = senderLabel(obj.optString("sender"), obj.optString("senderName"), devices, store),
    kind = obj.optString("kind", "text"),
    text = obj.optString("text"),
    mediaName = obj.optString("mediaName"),
    mediaSize = obj.optLong("mediaSize", 0),
    durationMs = obj.optInt("durationMs", 0),
    ts = obj.optDouble("ts", 0.0),
    outgoing = outgoingOf(obj, selfId),
    state = obj.optString("state", "sent"),
)

/** IO 线程：把侧栏、会话、传输三块要的东西一次读回来。 */
fun readSnapshot(api: ApiClient, store: AppState, conversationId: String?): Snapshot {
    val state = api.getJson("/api/state")
    val devices = loadDevicesFrom(state, store)
    val list = state.optJSONObject("chat")?.optJSONArray("conversations")
    val conversations = (0 until (list?.length() ?: 0)).mapNotNull { list?.optJSONObject(it)?.toConversation() }
    val convId = conversationId ?: conversations.firstOrNull()?.id.orEmpty()
    var selfId = ""
    val messages = if (convId.isBlank()) {
        emptyList()
    } else {
        val payload = api.getJson("/api/chat?conv=" + encodeUrl(convId))
        selfId = payload.optString("selfId")
        val array = payload.optJSONArray("messages")
        (0 until (array?.length() ?: 0)).mapNotNull { index ->
            array?.optJSONObject(index)?.let { messageFrom(it, selfId, devices, store) }
        }
    }
    fun objects(name: String): List<JSONObject> {
        val array = state.optJSONArray(name) ?: return emptyList()
        return (0 until array.length()).mapNotNull { array.optJSONObject(it) }
    }
    val computerFiles = try {
        val payload = api.getJson("/api/files")
        val array = payload.optJSONArray("files") ?: org.json.JSONArray()
        (0 until array.length()).mapNotNull { array.optJSONObject(it) }
    } catch (ignored: Exception) {
        emptyList()
    }
    return Snapshot(
        devices = devices,
        conversations = conversations,
        conversationId = convId,
        messages = messages,
        transfers = objects("transfers"),
        uploads = objects("uploads"),
        recentUploads = objects("recentUploads"),
        offers = objects("offers"),
        shares = objects("shares"),
        computerFiles = computerFiles,
        receiveDir = state.optString("receiveDir"),
    )
}

/** 主线程：把读回来的东西落到界面上（选中行、会话、传输一致）。 */
fun applySnapshot(ui: UiState, snap: Snapshot, store: AppState) {
    // 同名设备在这里就消歧好（`RealProbe` / `RealProbe #2`）：侧栏、输入区的目标
    // 选择、会话标题读的都是这一份，名字必须处处一致（C4 / E1）。
    ui.devices = disambiguateNames(snap.devices)
    ui.conversations = snap.conversations
    ui.transfers = snap.transfers
    ui.uploads = snap.uploads
    ui.recentUploads = snap.recentUploads
    ui.offers = snap.offers
    ui.shares = snap.shares
    ui.computerFiles = snap.computerFiles
    ui.receiveDir = snap.receiveDir
    ui.selfId = snap.selfId
    // 有一条上传因为 PIN 被拒，就点亮「需要 PIN」徽标和输入区的 PIN 框。
    //
    // PIN 框平时不占输入行的位置（手机上那一行要塞 7 个控件），所以第一次被打回时
    // 必须**说一句**：状态行写「对方要 PIN，右边填一下」，PIN 框同时出现在输入行
    // 右端 —— 否则用户只看到"发送失败"，不知道该往哪儿填。
    if ((snap.uploads + snap.recentUploads).any {
            it.optString("status") == "failed" && it.optString("error").contains("PIN", ignoreCase = true)
        }
    ) {
        if (!ui.pinNeeded) ui.status = "对方要 PIN，右边填一下"
        ui.pinNeeded = true
    }

    // 选中行永远指向设备列表里的那一条：在线状态、地址都跟着刷新。
    ui.selected = ui.selected?.let { chosen -> snap.devices.firstOrNull { it.id == chosen.id } }

    // 第一次连上：默认选中"连着的这台电脑"——打开 App 就是和电脑的会话，
    // 这本来就是它存在的理由。用户点别的设备就按用户选的。
    if (ui.selected == null && !ui.autoOpened) {
        val computer = snap.devices.firstOrNull { it.kind == "电脑" }
        if (computer != null) {
            ui.selected = computer
            ui.autoOpened = true
        }
    }

    if (ui.autoOpened) {
        conversationFor(ui.selected, snap.conversations, snap.devices)?.let { found ->
            ui.open = found
            ui.autoOpened = false
        }
    } else if (ui.open != null) {
        ui.open = snap.conversations.firstOrNull { it.id == ui.open?.id } ?: ui.open
    }
    // 消息只在"读的就是当前打开的那个会话"时才采用，否则会串会话。
    if (ui.open?.id == snap.conversationId) {
        ui.messages = snap.messages
    }
}

/** 选中的设备对应哪个会话：按**成员写法**匹配（aliases / web: / android: / 旧身份键），
 *  名字只在认不出写法时兜底。
 *
 *  以前第一判据是 `members.contains(device.id)`，而设备行的 id 被写成
 *  `"web:" + key`（key 本身已带前缀）→ `web:web:<uuid>`，永远匹配不上，于是整体
 *  退化成按**显示名**匹配（还用 startsWith）：两台同名设备点哪一行都开同一段会话，
 *  名字对不上就打开一段空会话。审计员在真实数据上确证过这条。
 */
fun conversationFor(
    device: DeviceRow?,
    conversations: List<Conversation>,
    devices: List<DeviceRow> = emptyList(),
): Conversation? {
    if (device == null) return null
    // 身份写法要**取并集**：服务端给的 aliases 里可能只有归并掉的旧键，而设备自己的
    // id 变体（web:x / android:x / 裸 x）在 identityAliases 里 —— 只看其中一个，
    // 会话成员写的是裸设备号时就匹配不上（真机上标题会退化成"与 e4233004… 的对话"）。
    val wanted = identityAliases(device.id) + device.aliases
    conversations.firstOrNull { conversation ->
        conversation.members.any { member ->
            member in wanted || identityAliases(member).any { it in wanted }
        }
    }?.let { return it }
    // 名字兜底：只在写法完全认不出来、**而且列表里没有第二台同名设备**时用。
    // 两台同名设备（不同设备号）都认不出成员写法时，按名字挑一定会挑错 ——
    // 宁可不开这一段，也不能点哪一行都进别人的会话（审计 #5 的原症状）。
    val name = device.name.trim()
    if (name.isEmpty()) return null
    if (devices.any { it.id != device.id && it.name.trim() == name }) return null
    // 要求**完全同名**（不再 startsWith —— "安卓"会匹配上"安卓手机"，那是另一台设备）。
    return conversations.firstOrNull {
        it.displayTitle.isNotBlank() && it.displayTitle.substringBefore("（").trim() == name
    }
}

/**
 * 连上电脑：**先用在自动发现里刚听到的那台**，再退回"记住的地址"。IO 线程调用。
 *
 * 这一条的改动就是"必须手输 IP"的修复。以前这里只有一句话：拿 `store.host`
 * 去连 —— 全新安装时它是空的，电脑换了 IP 时它是废的，于是 App 打开就是
 * "连不上"，用户只能自己到设置页把 IP 敲进去。现在：
 *
 * 1. [LanDiscovery] 常驻在后台听/扫（组播 + 广播 + 单播 + 主动扫描），
 *    同网段的电脑 ≤5 秒就会出现在 `ui.discovered` 里；
 * 2. 这里优先连它（用它的地址和网页端口），并把这台是怎么发现的写进
 *    `store.via`，主区顶部就会显示「发现方式：组播 / 广播 / 主动扫描」；
 * 3. 都失败时给出的也不再是干巴巴的"连不上"，而是**为什么**（哪几条通道试过、
 *    各自的最后错误），对应宪章 C13 的"自动发现失败时界面明说为什么"。
 */
fun connectSession(store: AppState, ui: UiState): ApiClient? {
    store.client?.let { return it }
    val remembered = normalizeHost(store.host)
    val connectable = ui.discovered.filter { it.connectable }
    val lastId = store.lastDesktopId
    val now = System.currentTimeMillis()

    // 候选按"最可能是用户想连的那台"排序，一个一个试：
    //
    // ① 记住的地址 —— 上次用的就是它，能连上就别换（15 秒节流：地址失效时不能让
    //    它每轮都白等十几秒，把自动发现的结果挡在后面）；
    // ② 上次那台电脑又出现在发现列表里 —— 用它的**最新地址**（电脑换了 IP 也跟得上）；
    // ③ 自动发现里最新的一台 —— 全新安装（什么都没记住）走的就是这一条，
    //    这也是"必须手输 IP"被修掉的地方：不输任何东西，≤5 秒就能连上。
    val candidates = LinkedHashMap<String, String>()
    if (remembered.isNotBlank() && now - store.lastTryAt > 15_000) {
        candidates["http://$remembered/"] = store.via.ifBlank { "记住的地址" }
    }
    if (lastId.isNotBlank()) {
        connectable.firstOrNull { it.identityKey == lastId }?.let { candidates[it.base] = it.source }
    }
    autoConnectTarget(connectable)?.let { candidates.putIfAbsent(it.base, it.source) }

    if (candidates.isEmpty()) {
        if (ui.error.isBlank()) ui.error = discoveryHint(ui)
        return null
    }
    var lastError = ""
    for ((base, via) in candidates) {
        val plain = base.removePrefix("http://").removeSuffix("/")
        if (plain == remembered) store.lastTryAt = now
        val (api, cleaned, message) = kotlinx.coroutines.runBlocking { connectToDesktop(base) }
        if (api != null) {
            store.host = cleaned
            store.via = via
            store.client = api
            ui.client = api
            ui.error = ""
            rememberDesktopIdentity(store, api)
            announcePresence(store, api)
            return api
        }
        lastError = message
    }
    ui.error = lastError.ifBlank { discoveryHint(ui) }
    return null
}

/**
 * 记下"这台电脑是谁"（`/api/state` 的 `device.id`，也就是 identityKey）。
 *
 * 只在刚连上时记一次：下次打开 App，发现列表里如果出现了同一台电脑，就连它
 * （用最新地址），而不是随便挑一台刚听到的 —— 办公室里往往不止一台电脑。
 */
fun rememberDesktopIdentity(store: AppState, api: ApiClient) {
    try {
        val id = api.getJson("/api/state").optJSONObject("device")?.optString("id").orEmpty()
        if (id.isNotBlank()) store.lastDesktopId = id
    } catch (ignored: Exception) {
    }
}

/** 自动发现失败时给用户看的一句话：试过哪几条通道、各听到多少、下一步做什么。 */
fun discoveryHint(ui: UiState): String {
    val stats = ui.discovery
    val channels = stats?.channels.orEmpty()
        .filter { it.sent > 0 || it.heard > 0 }
        .joinToString(" · ") { "${it.label} 发${it.sent}/收${it.heard}" }
    val seen = ui.discovered.size
    val head = if (seen == 0) {
        "正在自动搜索同网段的电脑（组播 + 广播 + 主动扫描，打开就在跑）"
    } else {
        "已经发现 $seen 台，但都连不上（网页端口没开？）"
    }
    return buildString {
        append(head)
        if (channels.isNotBlank()) append("；已试通道：$channels")
        append("。检查：① 手机和电脑在同一个 Wi-Fi；② 电脑上的韧传开着；")
        append("③ 电脑防火墙允许 52119/TCP 与 52118/UDP。")
    }
}

/**
 * 投一条消息：成功返回 null，失败返回**给用户看的中文原因**（C14）。
 *
 * 电脑端明确说"没投出去"（`ok=false`）时也算失败 —— 以前只看 HTTP 状态码，
 * "发出去了但对面没有"就这么发生。
 */
fun deliverMessage(api: ApiClient, item: Queued): String? = try {
    val payload = JSONObject().put("text", item.text)
    if (item.conversationId.isNotBlank()) payload.put("conv", item.conversationId)
    val response = api.postJson("/api/chat/send", payload)
    if (response.optBoolean("ok", true)) {
        null
    } else {
        response.optString("error").ifBlank { "电脑端没有确认收到" }
    }
} catch (problem: Exception) {
    reasonOf(problem)
}

/** 异常 → 中文原因（宪章 §5.6 的原因码要求；比 `SocketTimeoutException` 有用得多）。 */
fun reasonOf(problem: Throwable): String = when (problem) {
    is java.net.SocketTimeoutException -> "超时（对方可能不在线或被防火墙挡住）"
    is java.net.ConnectException -> "连接被拒（对方不在线，或端口没开）"
    is java.net.UnknownHostException -> "找不到这个地址"
    is java.net.NoRouteToHostException -> "没有到对方的路由（不同网段？）"
    // 404 是真机上抓到过的一种："电脑说发了文件，但按这个消息 id 取不到附件"
    // —— 原因是那条记录没带 media_rel。这时要说清"电脑没有提供下载入口"，
    // 而不是含混的"存储权限或空间不足"。
    is java.io.FileNotFoundException -> "这台电脑没有提供下载入口（附件不存在，HTTP 404）"
    is java.io.IOException -> problem.message ?: "读写出错"
    else -> (problem.message ?: problem.javaClass.simpleName)
}

/**
 * 「保存到手机」失败时给用户的那句话：**先问他"你走的哪个入口"**。
 *
 * 404 与"磁盘满"完全是两回事，处理方式也不同：前者要电脑端把附件补齐
 * （或改用分享条目），后者要清空间。真机上这两种都被笼统写成"保存失败
 * （存储权限或空间不足）"，用户照着清空间当然没用。
 */
fun saveFailureReason(active: ApiClient, remote: String, message: ChatMessage): String {
    val path = remote.ifBlank { "/api/chat/media/" + encodeUrl(message.id) }
    return try {
        // 轻量探一下：请求同一个入口，读到第一块就停（不会把整个文件拉下来），
        // 目的只是把服务端给的原因（404 / 500 / 超时）翻译成中文。
        val probe = object : java.io.OutputStream() {
            override fun write(b: Int) = throw StopProbe()
            override fun write(b: ByteArray, off: Int, len: Int) = throw StopProbe()
        }
        try {
            active.download(path, probe)
        } catch (stop: StopProbe) {
            return "入口 $path 其实是通的，请重试"
        }
        "入口 $path 其实是通的，请重试"
    } catch (problem: Exception) {
        // 服务端自己写的那句（404 的 hint）最准确，优先用它。
        active.failureText(path).ifBlank { reasonOf(problem) }
    }
}

/** 探针读完第一块就收手（见 [saveFailureReason]）。 */
private class StopProbe : java.io.IOException("探针已够")


/** 立刻刷新一轮（按钮点完不想等下一个 3 秒）。 */
suspend fun refreshOnce(store: AppState, ui: UiState) {
    val active = ui.client ?: withContext(Dispatchers.IO) { connectSession(store, ui) } ?: return
    val snap = withContext(Dispatchers.IO) {
        try {
            readSnapshot(active, store, ui.open?.id)
        } catch (problem: Exception) {
            ui.error = "读不到电脑上的状态：${problem.message ?: problem.javaClass.simpleName}"
            null
        }
    }
    if (snap != null) applySnapshot(ui, snap, store)
}

/**
 * 「扫描」按钮：催一次自动发现，然后等结果（最多 5 秒）。
 *
 * 发现本身是**常驻**的（[LanDiscovery] 在 App 打开时就开始跑），所以这个按钮
 * 不是"必经步骤"，只是"我怀疑它在但没出现，再催一次"的加速手段 —— 这正是宪章
 * C13 的要求（扫描按钮是加速而不是必需）。找不到时不返回干巴巴的"没搜到"，
 * 而是把每条通道发了几包、收了几包、最后一次错误都写出来（E2）。
 */
suspend fun scanLan(context: android.content.Context, store: AppState, ui: UiState): String {
    ui.scanning = true
    val discovery = LanDiscovery.get(context)
    discovery.scanNow("手动")
    val deadline = System.currentTimeMillis() + 5000
    var peers = emptyList<DiscoveredPeer>()
    while (System.currentTimeMillis() < deadline) {
        peers = ui.discovered.filter { it.connectable }
        if (peers.isNotEmpty()) break
        delay(250)
    }
    ui.scanning = false
    if (peers.isEmpty()) {
        val stats = ui.discovery
        val tried = stats?.channels.orEmpty()
            .filter { it.sent > 0 || it.heard > 0 }
            .joinToString(" · ") { "${it.label} 发${it.sent}/收${it.heard}" }
            .ifBlank { "（还没有统计，引擎刚起来）" }
        val error = stats?.lastError.orEmpty().takeIf { it.isNotBlank() }?.let { "最近错误：$it。" }.orEmpty()
        return "没搜到电脑。已试通道：$tried。${error}逐条查：① 手机和电脑在同一个 Wi-Fi 或热点里；" +
            "② 电脑上的韧传开着（手机浏览器能打开它显示的地址就说明通了）；" +
            "③ 电脑防火墙允许 52119/TCP 与 52118/UDP（公司网络常会拦）。" +
            "还不行就点「扫描 ▾ › 手动输入地址」照着电脑上显示的地址填。"
    }
    val first = autoConnectTarget(peers) ?: peers.first()
    store.host = "${first.host}:${first.webPort}"
    store.via = first.source
    ui.client = null
    store.client = null
    ui.status = "搜到 ${peers.size} 台：${peers.joinToString("、") { it.name }}（来源：${first.source}）"
    refreshOnce(store, ui)
    return "搜到 ${peers.size} 台：" + peers.joinToString("、") { "${it.name}(${it.source})" } +
        "（已连上「${first.name}」）"
}

/** 信任/取消信任一台设备（长按设备行出来的菜单）。 */
suspend fun setTrust(store: AppState, ui: UiState, device: DeviceRow, trusted: Boolean) {
    val api = ui.client ?: return
    withContext(Dispatchers.IO) {
        try {
            api.postJson("/api/trust", JSONObject().put("deviceId", device.id).put("trusted", trusted))
        } catch (problem: Exception) {
            ui.status = "设置信任失败：${problem.message ?: problem.javaClass.simpleName}"
        }
    }
    refreshOnce(store, ui)
}

/** 接受/拒绝一次待确认的传输。 */
fun respondOffer(
    store: AppState,
    ui: UiState,
    scope: kotlinx.coroutines.CoroutineScope,
    requestId: String,
    accept: Boolean,
    onRefresh: () -> Unit,
) {
    scope.launch {
        withContext(Dispatchers.IO) {
            try {
                ui.client?.postJson(
                    "/api/offer/respond",
                    JSONObject().put("requestId", requestId).put("accept", accept),
                )
            } catch (problem: Exception) {
                ui.status = (if (accept) "接收失败：" else "拒绝失败：") + problem.message
            }
        }
        ui.status = if (accept) "已答应接收，文件会自己传过来" else "已拒绝这次传输"
        onRefresh()
        refreshOnce(store, ui)
    }
}

/** 电脑交给手机的文件：保存到系统「下载」。 */
fun saveShare(
    context: android.content.Context,
    store: AppState,
    ui: UiState,
    scope: kotlinx.coroutines.CoroutineScope,
    shareId: String,
    name: String,
    onRefresh: () -> Unit,
) {
    scope.launch {
        ui.status = "正在下载 $name…"
        val api = ui.client
        val saved = withContext(Dispatchers.IO) {
            if (api == null) {
                null
            } else {
                try {
                    // 期望字节数 = 电脑报的大小：只写了一部分不算成功（真机上
                    // "界面说已收到、磁盘上没有"就是这么来的）。
                    val expected = ui.shares
                        .firstOrNull { it.optString("id") == shareId }
                        ?.optLong("size") ?: -1L
                    saveToDownloads(context, name, expected) { sink ->
                        api.download("/api/share/" + encodeUrl(shareId), sink)
                    }
                } catch (problem: Exception) {
                    ui.status = "下载失败：${problem.message}"
                    null
                }
            }
        }
        if (saved != null) {
            ui.status = "已保存到「下载」：$name"
            withContext(Dispatchers.IO) {
                try {
                    // 告诉电脑"这份取走了"：它那边会标成已下载。
                    api?.getJson("/api/share/" + encodeUrl(shareId))
                } catch (ignored: Exception) {
                }
            }
        }
        onRefresh()
    }
}

/** 电脑接收目录里的文件：保存到手机「下载」。 */
fun downloadComputerFile(
    context: android.content.Context,
    store: AppState,
    ui: UiState,
    scope: kotlinx.coroutines.CoroutineScope,
    path: String,
    name: String,
    onRefresh: () -> Unit,
) {
    scope.launch {
        ui.status = "正在下载 $name…"
        val api = ui.client
        val saved = withContext(Dispatchers.IO) {
            if (api == null) {
                null
            } else {
                try {
                    saveToDownloads(context, name) { sink ->
                        api.download("/api/download?path=" + encodeUrl(path), sink)
                    }
                } catch (problem: Exception) {
                    ui.status = "下载失败：${problem.message ?: problem.javaClass.simpleName}"
                    null
                }
            }
        }
        if (saved != null) ui.status = "已下载到「下载」目录：$name"
        onRefresh()
    }
}

/** 开始建群：先选成员（电脑 + 已配对的手机）。 */
suspend fun startGroupPick(store: AppState, ui: UiState) {
    val active = ui.client ?: withContext(Dispatchers.IO) { connectSession(store, ui) } ?: return
    ui.members = withContext(Dispatchers.IO) { loadMembers(active, store) }
    ui.chosen = emptySet()
    ui.pickingGroup = true
}

/** 打开系统「下载」目录（手机上收到的文件都落在那里）。 */
fun openDownloadsFolder(context: android.content.Context) {
    try {
        val intent = Intent(android.app.DownloadManager.ACTION_VIEW_DOWNLOADS)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(intent)
    } catch (problem: Exception) {
        // 有的 ROM 没有这个界面：至少说清文件到底在哪。
        android.widget.Toast
            .makeText(context, "收到的文件都保存在系统「下载」目录里", android.widget.Toast.LENGTH_LONG)
            .show()
    }
}

/** 本机 Wi-Fi 地址（侧栏品牌行第二行要写"本机名 · 地址"）。 */
fun localIpv4(): String {
    try {
        val interfaces = java.net.NetworkInterface.getNetworkInterfaces() ?: return ""
        for (nic in java.util.Collections.list(interfaces)) {
            if (!nic.isUp || nic.isLoopback) continue
            for (address in nic.interfaceAddresses) {
                val candidate = address.address
                if (candidate is java.net.Inet4Address && !candidate.isLoopbackAddress &&
                    !candidate.isLinkLocalAddress
                ) {
                    return candidate.hostAddress.orEmpty()
                }
            }
        }
    } catch (ignored: Exception) {
    }
    return ""
}

/**
 * 发现方式的中文说法（主区顶部与设备行副标题都要写清"这台是怎么发现的"）。
 *
 * 入参既可能是服务端给的英文（`multicast` / `broadcast` / `manual`），也可能是
 * [DiscoveryChannel] 已经算好的中文标签 —— 两个都认，三端措辞保持一致。
 */
fun sourceLabel(source: String): String = when (source.lowercase()) {
    "multicast", "mdns", "nsd", "组播", "mdns 查询" -> "组播"
    "broadcast", "广播" -> "广播"
    "unicast", "incoming", "单播应答", "直连" -> "单播应答"
    "tcp", "scan", "subnet", "主动扫描" -> "主动扫描"
    "manual", "typed", "手动" -> "手动"
    "网页客户端", "web", "browser" -> "网页客户端"
    "" -> "手动"
    else -> source
}

fun avatarOf(device: DeviceRow?): String = when {
    device == null -> "💬"
    device.kind.contains("安卓") || device.kind.contains("手机") -> "📱"
    device.kind.contains("网页") -> "🌐"
    else -> "💻"
}

fun offlineText(device: DeviceRow): String = "已离线 " + statusText(device).removePrefix("未连接 · 最后在线 ")

fun fmtSpeed(bytesPerSecond: Double): String = when {
    bytesPerSecond <= 0 -> "—"
    else -> fmtSize(bytesPerSecond.toLong()) + "/s"
}

fun fmtEta(seconds: Double): String = when {
    seconds <= 0 -> "剩余 —"
    seconds < 60 -> "剩余 ${seconds.toInt()} 秒"
    seconds < 3600 -> "剩余 ${(seconds / 60).toInt()} 分"
    else -> "剩余 ${(seconds / 3600).toInt()} 小时"
}

/** 启动/停止前台保活服务。幂等：重复调用只是把地址刷新一下。 */
fun startLinkService(context: android.content.Context, store: AppState, enabled: Boolean) {
    if (!enabled) {
        context.stopService(Intent(context, LinkService::class.java))
        return
    }
    val host = normalizeHost(store.host)
    // **host 可以为空**：新装/换网/电脑还没出现时先启动服务跑发现与组播监听，
    // 连上电脑之后调用方会再调一次把地址补进来。以前这里空 host 直接 return，
    // 于是"没连上过电脑"的设备永远没有前台服务，一掉后台就被系统掐网。
    val intent = LinkService().startIntent(context, if (host.isBlank()) "" else "http://$host/")
    try {
        ContextCompat.startForegroundService(context, intent)
    } catch (problem: Exception) {
        Log.d("EverSend", "保活服务启动失败: ${problem.message}")
    }
}

/**
 * 把用户填的东西变成 `host:port`。
 *
 * 空输入返回**空串**：以前会拼成 `:52119`，于是界面显示
 * "连不上：Invalid host: http://:52119/" —— 用户看不出是自己没填地址。
 */
fun normalizeHost(raw: String): String {
    var text = raw.trim().removePrefix("http://").removePrefix("https://").trimEnd('/').trim()
    if (text.isEmpty() || text == ":") return ""
    if (!text.contains(":")) text = "$text:${ApiClient.DEFAULT_WEB_PORT}"
    return text
}

/**
 * 试着连一台电脑：先按用户填的端口，不行再试网页端口。
 *
 * 电脑端窗口顶上显示的是**传输端口**（52117），而手机要连的是**网页端口**
 * （52119）—— 用户照着顶上那个地址填进来，昨天就是这么失败的（
 * "unexpected end of stream"，因为 HTTP 打到了二进制传输端口上）。
 * 所以这里替他把两种都试一遍，并说清楚用的是哪个。
 */
/**
 * 连上之后要做的两件事：报上自己的身份（电脑端就会显示成「我的手机（安卓 App）」），
 * 以及每 5 秒广播一次自己的存在（电脑端不必等我们先访问它就能看到这台手机）。
 *
 * 用 [AppState.announcing] 挡住重复启动：设置页连一次、会话页再连一次时，
 * 以前会起两个广播循环。
 */
fun announcePresence(store: AppState, api: ApiClient) {
    if (store.announcing) return
    store.announcing = true
    CoroutineScope(Dispatchers.IO).launch {
        try {
            api.postJson(
                "/api/hello",
                JSONObject()
                    .put("deviceId", store.deviceId)
                    .put("name", store.deviceName)
                    .put("kind", "android")
                    .put("version", "1.0.0"),
            )
        } catch (ignored: Exception) {
        }
        while (true) {
            try {
                ApiClient.announce(store.deviceId, store.deviceName)
            } catch (ignored: Exception) {
            }
            delay(5000)
        }
    }
}

suspend fun connectToDesktop(
    typed: String,
): Triple<ApiClient?, String, String> {
    val host = normalizeHost(typed)
    if (host.isEmpty()) {
        return Triple(null, "", "先填电脑地址（电脑窗口上「手机访问」那一行），或者点「扫描」")
    }
    val address = host.substringBeforeLast(":")
    val port = host.substringAfterLast(":").toIntOrNull() ?: ApiClient.DEFAULT_WEB_PORT
    val candidates = LinkedHashSet<Int>()
    candidates.add(port)
    candidates.add(ApiClient.DEFAULT_WEB_PORT)
    candidates.add(ApiClient.DEFAULT_TLS_PORT)
    var lastError = ""
    for (candidate in candidates) {
        val base = "http://$address:$candidate/"
        try {
            val token = ApiClient.fetchToken(base)
            if (token.isBlank()) {
                lastError = "$address:$candidate 上没有韧传的网页界面"
                continue
            }
            val api = ApiClient(base, token)
            val state = api.getJson("/api/state")
            val name = state.optJSONObject("device")?.optString("name").orEmpty()
            val note = if (candidate != port) {
                "（$port 不是网页端口，已自动改用 $candidate）"
            } else {
                ""
            }
            return Triple(api, "$address:$candidate", "已连接：${name.ifBlank { "$address:$candidate" }}$note")
        } catch (problem: Exception) {
            lastError = "${problem.javaClass.simpleName}: ${problem.message}"
        }
    }
    return Triple(null, host, "连不上 $address：$lastError")
}

suspend fun uploadFile(
    client: ApiClient,
    file: File,
    kind: String,
    durationMs: Int,
    done: () -> Unit,
) {
    withContext(Dispatchers.IO) {
        try {
            val query = "/api/chat/upload?name=${encodeUrl(file.name)}&kind=$kind&duration=$durationMs"
            file.inputStream().use { stream ->
                client.upload(query, stream, file.length(), "audio/mp4")
            }
        } catch (ignored: Exception) {
            Log.d("EverSend", "语音上传失败: ${ignored.message}")
        }
    }
    done()
}

suspend fun uploadUri(
    context: android.content.Context,
    client: ApiClient,
    uri: Uri,
    done: () -> Unit,
) {
    withContext(Dispatchers.IO) {
        try {
            val resolver = context.contentResolver
            var name = "attachment"
            var size = 0L
            resolver.query(uri, null, null, null, null)?.use { cursor ->
                if (cursor.moveToFirst()) {
                    val nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    val sizeIndex = cursor.getColumnIndex(OpenableColumns.SIZE)
                    if (nameIndex >= 0) name = cursor.getString(nameIndex) ?: name
                    if (sizeIndex >= 0) size = cursor.getLong(sizeIndex)
                }
            }
            val kind = guessKind(name, resolver.getType(uri) ?: "")
            val query = "/api/chat/upload?name=${encodeUrl(name)}&kind=$kind"
            resolver.openInputStream(uri)?.use { stream ->
                client.upload(query, stream, size, resolver.getType(uri) ?: "application/octet-stream")
            }
        } catch (problem: Exception) {
            Log.d("EverSend", "附件上传失败: ${problem.message}")
        }
    }
    done()
}

fun guessKind(name: String, mime: String): String {
    val lower = name.lowercase()
    return when {
        mime.startsWith("image/") || lower.matches(Regex(".*\\.(png|jpe?g|gif|webp|heic|heif|bmp)$")) -> "image"
        mime.startsWith("video/") || lower.matches(Regex(".*\\.(mp4|mov|mkv|webm|3gp|avi)$")) -> "video"
        mime.startsWith("audio/") || lower.matches(Regex(".*\\.(m4a|aac|opus|ogg|mp3|wav|amr|weba)$")) -> "voice"
        else -> "file"
    }
}

fun encodeUrl(value: String): String = java.net.URLEncoder.encode(value, "UTF-8")

fun clock(seconds: Double): String {
    if (seconds <= 0) return ""
    return SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date((seconds * 1000).toLong()))
}

/** 人类可读的大小，气泡里那行说明用。 */
fun fmtSize(bytes: Long): String {
    if (bytes <= 0) return ""
    // 1024 进制 + IEC 单位标签（KiB/MiB/GiB）：原来标成 KB/MB/GB，512 MiB 会显示成
    // "512.0 MB"，按 SI 差 -4.6%。三端（桌面 model.py / 网页 app.js / 手机）统一为
    // 1024 进制 + IEC 标签（PG-6，与 gui-desktop 对齐）。
    val units = listOf("B", "KiB", "MiB", "GiB", "TiB")
    var value = bytes.toDouble()
    var unit = 0
    while (value >= 1024 && unit < units.size - 1) {
        value /= 1024
        unit++
    }
    return if (unit == 0) "${bytes} B" else String.format(Locale.US, "%.1f %s", value, units[unit])
}

/**
 * 把一份文件写进系统「下载」（MediaStore）。**成功 = 真的落盘 + 字节数相符。**
 *
 * 真机上发现过一个"界面说已收到、磁盘上什么都没有"的坑，机制在这里：
 *
 * 1. `IS_PENDING=1` 插进 MediaStore 之后，如果 `sink` 中途抛异常，控制流直接跳到
 *    外层 catch —— **那条 pending 行永远不会被清 0**，于是「下载」里既看不见它
 *    （用户看到的只有 `ls` 都列不出来的 0 字节 `.pending-*` 残留），App 的界面却
 *    记着"已收到"。这就是"界面说谎"的完整机制。
 * 2. 以前不校验写进去多少字节：写了 0 字节也算成功。
 *
 * 现在：**任何失败都必须把那条 pending 行删掉**（失败回滚），并且按
 * [expectedBytes] 校验实际写入量；对不上就算失败，返回 null，让界面如实说
 * "未保存 · 可重试"，而不是"已收到"。
 *
 * @param expectedBytes 期望字节数（>0 时校验；-1 = 不校验，比如"打开"路径的临时落盘）
 */
fun saveToDownloads(
    context: android.content.Context,
    name: String,
    expectedBytes: Long = -1L,
    sink: (java.io.OutputStream) -> Unit,
): Uri? {
    val safeName = name.ifBlank { "eversend-file" }
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
        // 旧系统：直接写公共目录，同样校验字节数（少写一个字节也不算成功）。
        return try {
            val directory = File(
                Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS),
                "",
            )
            directory.mkdirs()
            val file = File(directory, safeName)
            var written = 0L
            file.outputStream().use { out ->
                val counting = CountingOutputStream(out)
                sink(counting)
                written = counting.count
            }
            if (expectedBytes >= 0 && written != expectedBytes) {
                file.delete()
                Log.d("EverSend", "保存失败：只写了 $written / $expectedBytes 字节")
                null
            } else {
                Uri.fromFile(file)
            }
        } catch (problem: Exception) {
            Log.d("EverSend", "保存失败: ${problem.message}")
            null
        }
    }

    val resolver = context.contentResolver
    val values = android.content.ContentValues().apply {
        put(android.provider.MediaStore.Downloads.DISPLAY_NAME, safeName)
        put(android.provider.MediaStore.Downloads.MIME_TYPE, "application/octet-stream")
        put(android.provider.MediaStore.Downloads.IS_PENDING, 1)
    }
    val uri = try {
        resolver.insert(android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
    } catch (problem: Exception) {
        Log.d("EverSend", "在「下载」里建条目失败: ${problem.message}")
        null
    } ?: return null

    var written = 0L
    return try {
        val out = resolver.openOutputStream(uri) ?: throw java.io.IOException("打不开输出流")
        out.use { raw ->
            val counting = CountingOutputStream(raw)
            sink(counting)
            counting.flush()
            written = counting.count
        }
        if (expectedBytes >= 0 && written != expectedBytes) {
            throw java.io.IOException("只写了 $written / $expectedBytes 字节")
        }
        // 收尾：清掉 pending，文件才在「下载」里出现。
        resolver.update(
            uri,
            android.content.ContentValues().apply {
                put(android.provider.MediaStore.Downloads.IS_PENDING, 0)
            },
            null,
            null,
        )
        uri
    } catch (problem: Exception) {
        // **失败必须把那条 pending 行删掉**：否则它在「下载」里既看不见又占着名字，
        // 还会以 0 字节 `.pending-*` 的形式留在磁盘上（真机上抓到过 5 个）。
        try {
            resolver.delete(uri, null, null)
        } catch (ignored: Exception) {
        }
        Log.d("EverSend", "保存到「下载」失败（已回滚）: ${problem.message}")
        null
    }
}

/**
 * 清掉以前失败留下的 pending 残留（`/sdcard/Download/.pending-*`）。
 *
 * 只能通过 MediaStore 删自己那一条（Android 10+ 的分区存储下 App 看不到也删不了
 * 别人的文件），这正好也只会清掉"本 App 建了但从没写完"的那些。
 */
fun cleanupPendingDownloads(context: android.content.Context): Int {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return 0
    val resolver = context.contentResolver
    var removed = 0
    // 只清"陈旧"的 pending 行（>5 分钟）：正在下载的那条 pending 行是新鲜的，
    // 不能被并发清理误删（A6.3 真机残留的根因就是活跑期间没人清）。
    val cutoff = System.currentTimeMillis() / 1000 - 300
    try {
        resolver.query(
            android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI,
            arrayOf(android.provider.MediaStore.Downloads._ID),
            android.provider.MediaStore.Downloads.IS_PENDING + " = 1" +
                " AND " + android.provider.MediaStore.Downloads.DATE_ADDED + " < " + cutoff,
            null,
            null,
        )?.use { cursor ->
            while (cursor.moveToNext()) {
                val id = cursor.getLong(0)
                val row = android.content.ContentUris.withAppendedId(
                    android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                    id,
                )
                try {
                    if (resolver.delete(row, null, null) > 0) removed++
                } catch (ignored: Exception) {
                }
            }
        }
    } catch (problem: Exception) {
        Log.d("EverSend", "清理 pending 残留失败: ${problem.message}")
    }
    // MediaStore 行删掉后磁盘文件通常也没了；但真机抓到过**没有对应行**的 0 字节
    // `.pending-*` 文件（进程在 insert 与写盘之间被杀）——按文件名再扫一遍兜底。
    try {
        val dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
        dir?.listFiles()?.forEach { file ->
            if (file.name.startsWith(".pending-") &&
                System.currentTimeMillis() - file.lastModified() > 5 * 60_000L
            ) {
                if (file.delete()) removed++
            }
        }
    } catch (problem: Exception) {
        Log.d("EverSend", "清理 pending 文件失败: ${problem.message}")
    }
    return removed
}

/** 数一数到底写出了多少字节（"写了 0 字节也算成功"就是这么堵住的）。 */
private class CountingOutputStream(private val delegate: java.io.OutputStream) :
    java.io.OutputStream() {
    var count: Long = 0L
        private set

    override fun write(b: Int) {
        delegate.write(b)
        count++
    }

    override fun write(b: ByteArray, off: Int, len: Int) {
        delegate.write(b, off, len)
        count += len
    }

    override fun flush() = delegate.flush()

    override fun close() = delegate.close()
}

/**
 * 把一个文件发给一台设备（多选时对每台各来一遍）。
 *
 * deviceId 就是"发给谁"：电脑会被电脑端转发过去；手机（web: 开头）会被
 * 电脑端交给那台手机的页面去下载 —— 手机之间没法直连，这是 App 作为
 * "电脑的客户端"唯一能走通的路。`pin` 是对方设了 PIN 时的口令（可空）。
 */
private const val MAX_UPLOAD_ATTEMPTS = 6

/**
 * "上传 0 字节就失败"且异常长这样 → 很可能是令牌过期。
 *
 * 电脑端重启后地址不变、令牌已换：服务端 CSRF 门禁在**读 body 之前**就 403
 * 并关闭连接；客户端这时正流式写 body，只能收到 `Broken pipe` / `Connection
 * reset` / `unexpected end of stream`，错误文案里根本没有"令牌"两个字。
 * （真机 P0：电脑重启后手机发 25MB 文件 6 次重试全 Broken pipe。）
 */
private fun looksLikeTokenFailure(problem: Exception): Boolean {
    val text = ((problem.message ?: "") + " " + problem.javaClass.simpleName).lowercase()
    return text.contains("令牌") ||
        text.contains("broken pipe") ||
        text.contains("connection reset") ||
        text.contains("unexpected end of stream") ||
        text.contains("socketexception") ||
        problem is java.net.SocketException
}

/**
 * 把一份文件发给一台设备。
 *
 * 优先走 **断点续传**（服务端 `caps.uploadResume=true` 时；接口见
 * `tests/reports/opt-1017/http-upload-resume-api.md`）：
 *   · 客户端生成 32 位 hex `uploadId`，同一份文件的重试链复用它；
 *   · `have` 永远以服务端回报的 offset 为准（202/409/status），不要信本地计数；
 *   · 每段用 setFixedLengthStreamingMode，流式发送，不整份读进内存。
 * 旧服务端（没有 caps.uploadResume）退回原来的"一次性 POST"，行为完全不变。
 */
suspend fun uploadToDevice(
    context: android.content.Context,
    api: ApiClient,
    uri: Uri,
    info: Pair<String, Long>,
    device: DeviceRow,
    store: AppState,
    pin: String = "",
) {
    val name = info.first
    val size = info.second
    val baseQuery = "/api/upload?name=${encodeUrl(name)}" +
        "&deviceId=${encodeUrl(device.id)}" +
        if (pin.isBlank()) "" else "&pin=${encodeUrl(pin)}"
    val resolver = context.contentResolver

    // **上传链开始前先换一张令牌**（真机 P0）：电脑端每次重启都换令牌、地址不变；
    // 拿旧令牌上传时服务端在**读 body 之前**就 403+关连接，客户端流式写 body 只能
    // 看到 "Broken pipe" —— 错误文案里没有"令牌"，自动恢复链判断不到。
    // 这里先无条件换一次（一次 GET，几十毫秒），彻底绕开这个盲区。
    try {
        withContext(Dispatchers.IO) { api.refreshToken() }
    } catch (ignored: Exception) {
    }

    // 先问服务端能力：旧服务端没有 caps 就退回一次性上传（不然旧服务端会把
    // 带 offset 的 body 当成整个文件，落一个不完整文件 —— 兼容矩阵里写明了）。
    val caps = try {
        withContext(Dispatchers.IO) { api.getJson("/api/state").optJSONObject("caps") }
    } catch (problem: Exception) {
        null
    }
    val canResume = caps?.optBoolean("uploadResume", false) == true && size > 0
    if (!canResume) {
        // 读不到就**抛**，不许静默跳过：以前这里是 ``openInputStream(uri)?.use { … }``，
        // 系统没给读权限（URI 过期、被回收）时它什么都不做也不报错 —— 循环照常往下走，
        // 最后界面上写「已发给 N 台设备」，而电脑那边一个字节都没收到（真机实测到过）。
        val stream = resolver.openInputStream(uri)
            ?: throw IllegalStateException("读不到这个文件（系统没给读权限或它已被移走），请重新选一次")
        stream.use { api.upload(baseQuery, it, size, "application/octet-stream") }
        return
    }
    val maxBytes = caps?.optLong("uploadMaxBytes", 0L) ?: 0L
    if (maxBytes > 0 && size > maxBytes) {
        throw IllegalStateException("文件太大：服务端最多接 ${maxBytes / (1024 * 1024)} MiB")
    }

    val uploadId = java.util.UUID.randomUUID().toString().replace("-", "")
    var have = 0L
    var attempt = 0
    var lastProblem: Exception? = null
    while (have < size && attempt < MAX_UPLOAD_ATTEMPTS) {
        attempt++
        try {
            val result = withContext(Dispatchers.IO) {
                val stream = resolver.openInputStream(uri)
                    ?: throw IllegalStateException(
                        "读不到这个文件（系统没给读权限或它已被移走），请重新选一次",
                    )
                stream.use { src ->
                    api.upload(
                        path = "$baseQuery&uploadId=$uploadId&offset=$have",
                        stream = src,
                        length = size,
                        mime = "application/octet-stream",
                        uploadId = uploadId,
                        offset = have,
                        total = size,
                    )
                }
            }
            if (result.optBoolean("complete", false)) return
            // 202：服务端收了一部分；409：它有的比我们记的多。都跟它走。
            val serverOffset = result.optLong("offset", -1L)
            if (serverOffset > have) have = serverOffset
            lastProblem = null
        } catch (problem: Exception) {
            lastProblem = problem
            android.util.Log.w(
                "EverSend",
                "上传中断（第 $attempt 次，已确认 $have/$size 字节）: ${problem.message}",
            )
            // 断线后先问服务端到底收了多少 —— 不要信本地计数（socket 缓冲可能没到）。
            val serverOffset = try {
                withContext(Dispatchers.IO) { api.uploadStatus(uploadId) }
            } catch (ignored: Exception) {
                -1L
            }
            if (serverOffset >= 0L) have = serverOffset
            // 0 字节就失败、且是"对端提前关连接"类错误 → 很可能是令牌过期
            // （服务端读 body 前就 403 关连接，客户端只能看到 Broken pipe）。
            // 换一张令牌再试，下一轮 attempt 就会带上新令牌。
            if (have == 0L && attempt <= 2 && looksLikeTokenFailure(problem)) {
                try {
                    withContext(Dispatchers.IO) { api.refreshToken() }
                    android.util.Log.w("EverSend", "上传 0 字节失败疑似令牌过期，已换令牌后重试")
                } catch (ignored: Exception) {
                }
            }
            if (have >= size) return
            delay(500L shl (attempt - 1).coerceAtMost(4))
        }
    }
    throw (lastProblem ?: IllegalStateException("上传没传完（服务端已有 $have / $size 字节），请重试"))
}

/** 设备行：电脑（协议对端）与手机（网页/App 客户端）都在这里。
 *
 *  ``identity`` 是这台设备的**稳定身份**（服务端的 ``identityKey``）：有设备号就是
 *  ``web:<设备号>`` / ``android:<设备号>``（跟地址无关），确实没有设备号的浏览器
 *  才退回它自己的键。同一台设备的多条记录（换过网络的旧记录、App 与它自己的
 *  浏览器）identity 相同 —— ``dedupeDevices`` 靠它把列表并成一行。
 *
 *  ``aliases`` 是这台设备在**会话成员**里的各种写法（web:x / android:x / 裸 x /
 *  老式 ``地址|UA摘要``）：会话按写法匹配，不再按显示名匹配。
 */
data class DeviceRow(
    val id: String,
    val name: String,
    val kind: String,          // 电脑 / 安卓 App / 网页版
    val address: String,
    val online: Boolean = true,
    val secondsAgo: Double = 0.0,
    val facts: String = "",    // 系统 · 韧传 版本 · IP
    val platform: String = "", // Windows / Linux / android / browser
    val source: String = "",   // 组播 / 广播 / 手动 / 网页客户端
    val trusted: Boolean = false,
    val identity: String = "",
    val aliases: Set<String> = emptySet(),
    val version: String = "",
)

private fun platformName(platform: String): String = when (platform.lowercase()) {
    "android" -> "安卓"
    "browser" -> "网页版"
    "windows" -> "Windows"
    "linux" -> "Linux"
    "darwin", "macos" -> "macOS"
    else -> platform
}

private fun factsOf(platform: String, version: String, address: String, webPort: Int = 0): String {
    val facts = mutableListOf<String>()
    if (platform.isNotBlank()) facts.add(platformName(platform))
    if (version.isNotBlank()) facts.add("韧传 $version")
    if (address.isNotBlank()) {
        facts.add(if (webPort > 0) "$address:$webPort" else address)
    }
    return facts.joinToString(" · ")
}

/** 一个身份值的所有写法：``web:x`` / ``android:x`` / 裸 ``x`` 都算同一台。
 *
 *  这是服务端 ``core.chat.client_aliases`` 的等价物：会话里存的成员可能是当时
 *  那一种写法，只按一种查就会查不到（"手机上看不到消息"就是这么来的）。
 */
fun identityAliases(vararg values: String?): Set<String> {
    val out = LinkedHashSet<String>()
    for (value in values) {
        val text = value?.trim().orEmpty()
        if (text.isEmpty()) continue
        out.add(text)
        val bare = when {
            text.startsWith("web:") -> text.removePrefix("web:")
            text.startsWith("android:") -> text.removePrefix("android:")
            else -> text
        }
        if (bare.isNotEmpty()) {
            out.add(bare)
            out.add("web:$bare")
            out.add("android:$bare")
        }
    }
    return out
}

/** 一条客户端记录在会话里的所有写法（含服务端归并时记下的旧身份 ``aliases``）。 */
fun clientAliases(client: JSONObject): Set<String> {
    val values = mutableListOf<String?>(
        client.optString("key"),
        client.optString("deviceId"),
        client.optString("identityKey"),
    )
    val bucket = client.optJSONArray("aliases")
    for (index in 0 until (bucket?.length() ?: 0)) {
        values.add(bucket?.optString(index))
    }
    return identityAliases(*values.toTypedArray())
}

/** 一组记录（同一台设备）→ 一行：地址取最新、online 取任一条在线、名字取非空。
 *
 *  规则与服务端/网页版/桌面端一致；能收发的**协议对端优先**当这一行的身份
 *  （手机行走的是网页通道，选中它也发不出文件协议那一套）。
 */
private fun mergeDeviceGroup(group: List<DeviceRow>): DeviceRow {
    if (group.size == 1) return group[0]
    val newest = group.sortedBy { it.secondsAgo }
    val base = newest.firstOrNull { it.kind == "电脑" } ?: newest.first()
    val live = newest.firstOrNull { it.online }
    val fresh = live ?: newest.first()
    val name = newest.firstOrNull { it.name.isNotBlank() }?.name ?: base.name
    val address = fresh.address.ifBlank { base.address }
    val merged = base.copy(
        name = name,
        address = address,
        online = group.any { it.online },
        secondsAgo = if (live != null) live.secondsAgo else group.minOf { it.secondsAgo },
        trusted = group.any { it.trusted },
        aliases = group.flatMap { it.aliases }.toSet(),
    )
    // 合并之后地址可能是"更新那条"的，facts 里那句地址要跟着换，别写着旧 IP。
    if (base.kind != "电脑") {
        return merged.copy(facts = factsOf(merged.platform, merged.version, address))
    }
    return merged
}

/** 同一台设备只留一行：写法有交集的记录连成一组（并查集），再并成一行。 */
fun dedupeDevices(rows: List<DeviceRow>): List<DeviceRow> {
    if (rows.size < 2) return rows
    val parent = IntArray(rows.size) { it }
    fun find(start: Int): Int {
        var root = start
        while (parent[root] != root) root = parent[root]
        var walk = start
        while (parent[walk] != root) {
            val next = parent[walk]
            parent[walk] = root
            walk = next
        }
        return root
    }
    fun union(a: Int, b: Int) {
        val left = find(a)
        val right = find(b)
        if (left != right) parent[right] = left
    }
    val owner = HashMap<String, Int>()
    rows.forEachIndexed { index, row ->
        val names = row.aliases.ifEmpty { setOf(row.id) }
        for (name in names) {
            val seen = owner[name]
            if (seen == null) owner[name] = index else union(seen, index)
        }
    }
    val groups = LinkedHashMap<Int, MutableList<DeviceRow>>()
    rows.forEachIndexed { index, row ->
        groups.getOrPut(find(index)) { mutableListOf() }.add(row)
    }
    return groups.values.map { mergeDeviceGroup(it) }
}

fun loadDevices(api: ApiClient, store: AppState): List<DeviceRow> =
    loadDevicesFrom(api.getJson("/api/state"), store)

fun loadDevicesFrom(state: JSONObject, store: AppState): List<DeviceRow> {
    val rows = mutableListOf<DeviceRow>()
    val via = store.via

    // 这台手机连着的**就是**一台电脑 —— 它是发送的首要目标。
    //
    // 以前这里只看 devices 数组并且跳过 isSelf，于是"自己连的那台电脑"被当成
    // 本机过滤掉了：App 的设备列表里只有别的手机，用户根本选不到电脑，"传文件"
    // 自然无从谈起。App 的"本机"是这台手机（按 store.deviceId 判断），不是它连
    // 的那台电脑。
    val self = state.optJSONObject("device")
    val selfId = self?.optString("id").orEmpty()
    if (selfId.isNotEmpty()) {
        val webPort = self?.optInt("webPort", 0) ?: 0
        val addresses = self?.optJSONArray("addresses")
        val selfAddress = if ((addresses?.length() ?: 0) > 0) addresses?.optString(0).orEmpty() else ""
        rows.add(
            DeviceRow(
                id = selfId,
                name = self?.optString("name").orEmpty().ifBlank { "电脑" },
                kind = "电脑",
                address = if (webPort > 0) "$selfAddress:$webPort" else selfAddress,
                online = true,
                facts = factsOf(
                    self?.optString("platform").orEmpty(),
                    self?.optString("version").orEmpty(),
                    selfAddress,
                    webPort,
                ),
                platform = self?.optString("platform").orEmpty(),
                source = via.ifBlank { "手动" },
                trusted = true,
                identity = selfId,
                aliases = identityAliases(selfId),
                version = self?.optString("version").orEmpty(),
            )
        )
    }

    val devices = state.optJSONArray("devices")
    for (index in 0 until (devices?.length() ?: 0)) {
        val device = devices.optJSONObject(index) ?: continue
        if (device.optString("id") == selfId) continue        // 上面已经加过了
        if (device.optBoolean("isSelf")) continue             // 电脑端眼里的"本机"
        val address = device.optString("address")
        val webPort = device.optInt("webPort", 0)
        // 服务端给协议对端也带 identityKey（T1 之后）—— 没有就退回它自己的设备号。
        val identity = device.optString("identityKey").ifBlank { device.optString("id") }
        rows.add(
            DeviceRow(
                id = device.optString("id"),
                name = device.optString("name"),
                kind = "电脑",
                address = "$address:${device.optInt("port")}",
                online = device.optBoolean("online", true),
                secondsAgo = device.optDouble("secondsAgo", 0.0),
                facts = factsOf(
                    device.optString("platform"),
                    device.optString("version"),
                    address,
                    webPort,
                ),
                platform = device.optString("platform"),
                source = device.optString("source").ifBlank { "组播" },
                trusted = device.optBoolean("trusted", false),
                identity = identity,
                aliases = identityAliases(device.optString("id"), identity, device.optString("deviceId")),
                version = device.optString("version"),
            )
        )
    }

    val clients = state.optJSONArray("knownClients")
    for (index in 0 until (clients?.length() ?: 0)) {
        val client = clients.optJSONObject(index) ?: continue
        val deviceId = client.optString("deviceId")
        if (deviceId == store.deviceId) continue          // 就是本机
        val isApp = client.optString("clientKind") == "app"
        val key = client.optString("key")
        // 稳定身份：服务端的 identityKey 最可信；没有就按设备号造一个稳定的；
        // 都没有（老式浏览器）才退回它自己的地址键。
        //
        // 以前这里是 `"web:" + key`，而 key 本身已经带 `web:` / `android:` 前缀，
        // 于是 id 变成 `web:web:<uuid>` —— 和会话成员里任何一种写法都对不上，
        // conversationFor 只能退化成按**显示名**匹配：两台同名设备点哪一行都开
        // 同一段会话，名字对不上则打开空会话（审计员确证的 bug）。
        val identity = client.optString("identityKey").ifBlank {
            when {
                deviceId.isNotBlank() -> "web:$deviceId"
                key.isNotBlank() -> key
                else -> ""
            }
        }
        rows.add(
            DeviceRow(
                id = identity.ifBlank { key },
                name = client.optString("label", if (isApp) "安卓 App" else "手机"),
                kind = if (isApp) "安卓 App" else "网页版",
                address = client.optString("address"),
                online = client.optBoolean("online", false),
                secondsAgo = client.optDouble("secondsAgo", 0.0),
                facts = factsOf(
                    if (isApp) "android" else "browser",
                    client.optString("version"),
                    client.optString("address"),
                ),
                platform = if (isApp) "android" else "browser",
                source = "网页客户端",
                trusted = true,
                identity = identity,
                aliases = clientAliases(client),
                version = client.optString("version"),
            )
        )
    }
    // 服务端已经保证一台设备一条记录；这里按稳定身份再兜一层，别让同一台手机
    // 在侧栏里出现两行同名同机的条目（计数也就跟着对了）。
    return dedupeDevices(rows)
}

/**
 * 把"自动发现引擎听到的设备"并进设备列表（宪章 C4：一台设备只有一行）。
 *
 * 去重键是**稳定身份** [DiscoveredPeer.identityKey]（公告里的 `id`，服务端叫
 * identityKey），不是显示名、也不是地址：同一台电脑既被组播听到、又被主动扫描
 * 命中时，id 相同 ⇒ 只留一行；换网段导致地址变了也还是同一行（只更新地址）。
 *
 * 已经有一行（`/api/state` 报出来的那台）就地更新它的「发现方式」；没见过的那台
 * 才新增一行 —— 新增的正是"能直接连、但电脑端还没报出来"的电脑，也是自动连接
 * 的候选。
 */
fun mergeDiscovered(rows: List<DeviceRow>, peers: List<DiscoveredPeer>): List<DeviceRow> {
    if (peers.isEmpty()) return rows
    val out = rows.toMutableList()
    for (peer in peers) {
        val index = out.indexOfFirst { row -> matchesPeer(row, peer) }
        if (index >= 0) {
            val row = out[index]
            out[index] = row.copy(
                source = peer.source,
                // 刚听到它 = 它现在在线（不要因为 /api/state 那份还在写"未连接"就显示离线）。
                online = row.online || !peer.stale,
                address = if (peer.connectable) "${peer.host}:${peer.webPort}" else row.address,
                identity = row.identity.ifBlank { peer.identityKey },
                aliases = row.aliases + identityAliases(peer.identityKey),
            )
        } else {
            val isAndroid = peer.platform.equals("android", ignoreCase = true) ||
                peer.kind.equals("mobile", ignoreCase = true)
            out.add(
                DeviceRow(
                    id = peer.identityKey,
                    name = peer.name.ifBlank { peer.host },
                    kind = if (isAndroid) "安卓 App" else "电脑",
                    address = if (peer.connectable) "${peer.host}:${peer.webPort}" else peer.host,
                    online = !peer.stale,
                    secondsAgo = peer.ageMs / 1000.0,
                    facts = factsOf(peer.platform, peer.version, peer.host, peer.webPort) +
                        (if (peer.note.isBlank()) "" else " · ${peer.note}"),
                    platform = peer.platform,
                    source = peer.source,
                    identity = peer.identityKey,
                    aliases = identityAliases(peer.identityKey),
                    version = peer.version,
                )
            )
        }
    }
    // 排序：**本机刚刚听到的设备排最前面**。
    //
    // 这一条不是装饰，是"≤5 秒出现在列表里"能不能被用户看见的问题。用电脑端报的
    // `secondsAgo` 排序是不够的：它对自己刚发现的设备一律写 0.0，于是办公室里
    // 十几台设备全都"0 秒前"，而本机刚听到的那台反而排到它们后面 —— 一屏只显示
    // 6 行，用户看到的仍然是"什么都没搜到"（仪器测试就是这么逮住它的）。
    // 所以这里用**本机自己的 listening 时刻**（`DiscoveredPeer.ageMs`）当主键：
    // 刚听到的排第一，越久越靠后，没听到过的（纯服务端来源）排最后。
    val localAges = HashMap<String, Long>()
    for (peer in peers) {
        if (peer.stale) continue
        for (alias in identityAliases(peer.identityKey)) {
            val current = localAges[alias]
            if (current == null || peer.ageMs < current) localAges[alias] = peer.ageMs
        }
    }
    return disambiguateNames(dedupeDevices(out))
        .map { row ->
            row to (identityAliases(row.identity) + row.aliases).mapNotNull { localAges[it] }.minOrNull()
        }
        .sortedWith(
            compareBy<Pair<DeviceRow, Long?>> { it.second == null }
                .thenBy { it.second ?: Long.MAX_VALUE }
                .thenBy { if (it.first.online) 0 else 1 }
                .thenBy { it.first.secondsAgo }
                .thenBy { it.first.name },
        )
        .map { it.first }
}

/** 没有名字的设备统一叫这个（和 Python 侧 `model.UNKNOWN_DEVICE_LABEL` 同一句话）。 */
const val UNKNOWN_DEVICE_LABEL = "未知设备"

/**
 * 同名设备消歧（C4）。
 *
 * 去重是按**稳定身份**做的（一台设备一行），但"同一个显示名"完全可能属于不同的
 * 身份：同一台机器上跑了两个实例（deviceId 各不相同）就会出现三行 `RealProbe` ——
 * 用户在列表里根本分不清哪行是哪台。规则与 Python 侧 `core/model.py` 的
 * `disambiguateNames` 一致：
 *
 * 1. 名字本来就不同的设备**原样返回**，不加后缀；
 * 2. 同名的：最"该被指代"的那台保留原名，排序键 =（在线优先 → 最近出现优先 →
 *    身份字典序，保证结果与遍历顺序无关），其余依次 `${name} #2`、`${name} #3`；
 * 3. 显示名永远非空：没有名字时用 [UNKNOWN_DEVICE_LABEL]。
 *
 * 注意：这里**只改名、不合并行** —— 不同 deviceId 就是不同设备，合并会把两台
 * 真设备并成一台（records-data 在真实数据上验证过这条边界）。
 */
fun disambiguateNames(rows: List<DeviceRow>): List<DeviceRow> {
    if (rows.isEmpty()) return rows
    val groups = LinkedHashMap<String, MutableList<Int>>()
    rows.forEachIndexed { index, row ->
        val name = row.name.trim().ifBlank { UNKNOWN_DEVICE_LABEL }
        groups.getOrPut(name) { mutableListOf() }.add(index)
    }
    val out = rows.toMutableList()
    for ((name, indexes) in groups) {
        if (indexes.size < 2) {
            // 名字唯一：只补一个非空显示名，别的不动。
            val only = indexes.first()
            if (out[only].name.isBlank()) out[only] = out[only].copy(name = name)
            continue
        }
        indexes.sortedWith(
            compareBy<Int> { if (out[it].online) 0 else 1 }
                .thenBy { out[it].secondsAgo }
                .thenBy { out[it].id },
        ).forEachIndexed { position, index ->
            val row = out[index]
            out[index] = row.copy(name = if (position == 0) name else "$name #${position + 1}")
        }
    }
    return out
}

/**
 * 这一行是不是就是发现到的那台：**只按稳定身份**（identityKey 与它的各种写法）对。
 *
 * 这里以前还有一条"地址相同就算同一台"的兜底，被仪器测试逮住了：测试注入的那台
 * 假装在 `10.42.42.42`，而电脑端报回来的设备表里正好也有一台地址是 `10.42.42.42`
 * 的设备 —— 两条不同的身份被并成了一行，新出现的那台于是"消失在列表里"。
 * 地址会撞（NAT 后面两台设备同址、测试夹具随手写的地址、换网段后的残留），
 * 而宪章第 6 条铁律写得清楚：**去重键是稳定身份，不是显示名、不是地址**。
 */
fun matchesPeer(row: DeviceRow, peer: DiscoveredPeer): Boolean {
    val keys = identityAliases(peer.identityKey)
    if (row.identity.isNotBlank() && identityAliases(row.identity).any { it in keys }) return true
    return row.aliases.any { it in keys }
}

/**
 * 自动连接选哪台：只挑能连的（有网页端口），优先真正的电脑，其次最新的。
 *
 * 安卓端只能走 HTTP（网页接口），所以"电脑没开 52119"的那台连不上 —— 它仍然
 * 显示在列表里，但不会被选成自动连接的目标。
 */
fun autoConnectTarget(peers: List<DiscoveredPeer>): DiscoveredPeer? =
    peers.asSequence()
        .filter { it.connectable }
        .sortedWith(
            compareBy<DiscoveredPeer> { it.platform.equals("android", ignoreCase = true) }
                .thenBy { if (it.stale) 1 else 0 }
                .thenByDescending { it.seenAt },
        )
        .firstOrNull()

/**
 * 「重新发送」发给谁：优先当前选中的那台设备，其次是列表里第一台在线的。
 *
 * 手机上的"重发"是"再投给一台设备"，所以必须有明确的收件人 —— 没有就如实告诉
 * 用户"先选一台设备"，而不是发到一个猜出来的地方。
 */
fun resendDeviceOf(ui: UiState): DeviceRow? =
    ui.selected
        ?: mergeDiscovered(ui.devices, ui.discovered).firstOrNull { it.online }
        ?: mergeDiscovered(ui.devices, ui.discovered).firstOrNull()

/** 这个名字像不像内部 id（32~64 位 hex）：设备列表没认出名字时的回退值。 */
fun looksLikeInternalId(text: String): Boolean {
    val trimmed = text.trim()
    return trimmed.length >= 12 && trimmed.all { it.isDigit() || it in 'a'..'f' || it in 'A'..'F' }
}

/** 设备显示名：名字为空/长得像内部 id 时，别把设备号摊给用户看（PG-5）。 */
fun humanDeviceName(name: String, id: String = ""): String {
    val trimmed = name.trim()
    if (trimmed.isNotBlank() && !looksLikeInternalId(trimmed)) return trimmed
    val seed = trimmed.ifBlank { id.trim() }
    return if (seed.isBlank()) "一台设备" else "设备 ${seed.take(6)}…"
}

/**
 * 会话标题可读化：服务端给的 displayTitle 里可能夹着内部设备号
 * （真机截图「QA-真机验收 与 00254c4971c16f15c4bcd7982900f3cb…」是 PG-5）。
 * 能对上设备就换成名字，对不上就写"一台设备"，绝不把内部 id 摊在标题上。
 */
fun readableTitle(raw: String, members: List<DeviceRow>, fallback: String = "会话"): String {
    val text = raw.trim()
    if (text.isEmpty()) return fallback
    var out = text
    for (match in Regex("[0-9a-fA-F]{16,64}").findAll(text)) {
        val id = match.value
        val name = members.firstOrNull { member ->
            member.id == id || member.id.endsWith(id) || member.id == "web:$id" ||
                member.id == "android:$id" || member.aliases.any { it == id || it.endsWith(id) }
        }?.name
        val shown = if (name.isNullOrBlank()) "一台设备" else humanDeviceName(name, id)
        out = out.replace(id, shown)
    }
    return out.replace(Regex("\\s+"), " ").trim().ifBlank { fallback }
}

/**
 * 会话页副标题：成员用统一格式写清楚 —— 名字（系统 · 韧传 版本 · IP）。
 *
 * 用户要的"聊天应该规范显示名称"就是这一行：一眼看出这句话是谁发的、
 * 对方是什么机器、什么版本、局域网地址是多少。
 */
fun chatMembersLabel(members: List<DeviceRow>, conversation: Conversation, store: AppState): String {
    val mine = "我（${store.deviceName}）"
    val others = mutableListOf<String>()
    val ids = conversation.members
    for (member in members) {
        // 成员对比的是**写法**（web:x / android:x / 旧身份键），不是只比一个 id：
        // 会话里存的是当时那种写法，严格比 id 会让成员漏掉（那台设备就不再出现在
        // "与 … 的对话"里）。
        val aliases = identityAliases(member.id) + member.aliases
        val matched = ids.any { it in aliases || identityAliases(it).any { alias -> alias in aliases } }
        if (ids.isNotEmpty() && !matched) continue
        val facts = member.facts.ifBlank { member.kind }
        others.add("${humanDeviceName(member.name, member.id)}（$facts）")
    }
    // 一对一的标题里**不能有"我"**：真机上显示成「与 我（MI 9） 的对话」，
    // 用户看到的是"跟自己的对话"—— 标题说的是对面是谁。
    return if (conversation.id.startsWith("g:")) {
        "群成员：" + (listOf(mine) + others).joinToString("、")
    } else if (others.isNotEmpty()) {
        "与 " + others.joinToString("、") + " 的对话"
    } else {
        // 一个都没对上（对方换了身份、或它还没出现在设备列表里）：如实写原始写法，
        // 别拿"我"顶上，也别显示成空标题。
        val raw = ids.firstOrNull { it != store.deviceId } ?: ids.firstOrNull() ?: ""
        if (raw.isBlank()) "这段会话的对方已经不在列表里了"
        else "与 ${humanDeviceName("", raw)} 的对话（这台设备现在不在列表里）"
    }
}

/** 群聊可选成员：电脑（协议对端）和已配对的手机（网页版 / 安卓 App）。 */
fun loadMembers(api: ApiClient, store: AppState): List<DeviceRow> =
    loadDevices(api, store).filter { it.id != store.deviceId }

/** 「已连接 / 未连接 · 最后在线 X」——手机端也要一眼看出谁在线。 */
fun statusText(device: DeviceRow): String {
    if (device.online) return "已连接"
    val ago = device.secondsAgo
    val when_ = when {
        ago < 60 -> "${ago.toInt()} 秒前"
        ago < 3600 -> "${(ago / 60).toInt()} 分钟前"
        else -> "${(ago / 3600).toInt()} 小时前"
    }
    return "未连接 · 最后在线 $when_"
}

/** 从 content URI 读文件名与大小。 */
fun queryFile(context: android.content.Context, uri: Uri): Pair<String, Long> {
    var name = "file"
    var size = 0L
    context.contentResolver.query(uri, null, null, null, null)?.use { cursor ->
        if (cursor.moveToFirst()) {
            val nameIndex = cursor.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
            val sizeIndex = cursor.getColumnIndex(android.provider.OpenableColumns.SIZE)
            if (nameIndex >= 0) name = cursor.getString(nameIndex) ?: name
            if (sizeIndex >= 0) size = cursor.getLong(sizeIndex)
        }
    }
    return Pair(name, size)
}
