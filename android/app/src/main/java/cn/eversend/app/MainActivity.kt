package cn.eversend.app

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.media.MediaPlayer
import android.media.MediaRecorder
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.OpenableColumns
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
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
 * Emoji：界面所有文本都用 Compose 的 `Text`，走系统字体栈，彩色 emoji 由系统
 * 字体渲染（Noto Color Emoji），和微信、相册一个水平；输入既可以用系统键盘的
 * emoji 面板，也可以用内置面板（发出去的就是普通 Unicode 字符，电脑端同样能显示）。
 * 我们**不**自带字体、不用图片当表情——那样跨设备一定花。
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = AppState(this)
        // CI 里用 intent extra 注入电脑地址，省去手工扫码/输入。
        intent?.getStringExtra("host")?.let { store.host = it }
        setContent { EverSendTheme { Root(store) } }
    }
}

class AppState(context: android.content.Context) {
    private val prefs = context.getSharedPreferences("eversend", android.content.Context.MODE_PRIVATE)

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
     * 设置页点「连接」之后，聊天页应当直接用它，而不是自己再猜一遍地址 ——
     * 以前两边各连各的，就会出现"设置页说已连接、聊天页说连不上"。
     */
    var client: ApiClient? = null

    /** 广播循环只起一个（见 [announcePresence]）。 */
    var announcing: Boolean = false
}

@Composable
fun EverSendTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = lightColorScheme(primary = Color(0xFF2F81F7))) { content() }
}

@Composable
fun Root(store: AppState) {
    var tab by remember { mutableStateOf(0) }
    Scaffold(
        bottomBar = {
            NavigationBar {
                NavigationBarItem(
                    selected = tab == 0,
                    onClick = { tab = 0 },
                    icon = { Icon(Icons.Filled.Chat, null) },
                    label = { Text("聊天") },
                    modifier = Modifier.testTag("tab-chat"),
                )
                NavigationBarItem(
                    selected = tab == 1,
                    onClick = { tab = 1 },
                    icon = { Icon(Icons.Filled.Upload, null) },
                    label = { Text("发送") },
                    modifier = Modifier.testTag("tab-send"),
                )
                NavigationBarItem(
                    selected = tab == 2,
                    onClick = { tab = 2 },
                    icon = { Icon(Icons.Filled.Download, null) },
                    label = { Text("接收") },
                    modifier = Modifier.testTag("tab-receive"),
                )
                NavigationBarItem(
                    selected = tab == 3,
                    onClick = { tab = 3 },
                    icon = { Icon(Icons.Filled.SwapVert, null) },
                    label = { Text("传输") },
                    modifier = Modifier.testTag("tab-transfers"),
                )
                NavigationBarItem(
                    selected = tab == 4,
                    onClick = { tab = 4 },
                    icon = { Icon(Icons.Filled.Settings, null) },
                    label = { Text("设置") },
                    modifier = Modifier.testTag("tab-settings"),
                )
            }
        }
    ) { padding ->
        Box(Modifier.padding(padding)) {
            when (tab) {
                0 -> ChatScreen(store)
                1 -> SendScreen(store)
                2 -> ReceiveScreen(store)
                3 -> TransfersScreen(store)
                else -> SettingsScreen(store)
            }
        }
    }
}

// ------------------------------------------------------------------ 设置

@Composable
fun SettingsScreen(store: AppState) {
    val context = LocalContext.current
    var host by remember { mutableStateOf(store.host) }
    var status by remember { mutableStateOf("") }
    var discovered by remember { mutableStateOf<List<ApiClient.Found>>(emptyList()) }
    var scanning by remember { mutableStateOf(false) }
    var keepAlive by remember { mutableStateOf(store.keepAlive) }
    val scope = rememberCoroutineScope()

    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("电脑地址", fontWeight = FontWeight.SemiBold)
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
                        }
                        store.client = api
                        if (api != null) announcePresence(store, api)
                        status = message
                    }
                },
                modifier = Modifier.testTag("btn-connect"),
            ) { Text("连接") }

            OutlinedButton(
                onClick = {
                    scanning = true
                    status = "正在搜索电脑…"
                    scope.launch {
                        // 先试记着的那台（通常是上次用的），它在线就秒连；
                        // 不行再走完整发现：UDP 探针 → 子网单播 → TCP 扫网页端口。
                        val remembered = normalizeHost(store.host)
                        val quick = withContext(Dispatchers.IO) {
                            if (remembered.isBlank()) {
                                emptyList()
                            } else {
                                try {
                                    val token = ApiClient.fetchToken("http://$remembered/")
                                    if (token.isBlank()) {
                                        emptyList()
                                    } else {
                                        val state = ApiClient("http://$remembered/", token).getJson("/api/state")
                                        val device = state.optJSONObject("device")
                                        val port = remembered.substringAfterLast(":", ApiClient.DEFAULT_WEB_PORT.toString())
                                            .toIntOrNull() ?: ApiClient.DEFAULT_WEB_PORT
                                        listOf(
                                            ApiClient.Found(
                                                name = device?.optString("name").orEmpty().ifBlank { remembered },
                                                host = remembered.substringBeforeLast(":"),
                                                webPort = port,
                                                platform = device?.optString("platform").orEmpty(),
                                            ),
                                        )
                                    }
                                } catch (ignored: Exception) {
                                    emptyList()
                                }
                            }
                        }
                        if (quick.isNotEmpty()) {
                            discovered = quick
                            scanning = false
                            host = "${quick[0].host}:${quick[0].webPort}"
                            status = "上次那台「${quick[0].name}」还在，已经填好，点「连接」即可"
                            return@launch
                        }
                        discovered = withContext(Dispatchers.IO) {
                            ApiClient.discover(context = context)
                        }
                        scanning = false
                        if (discovered.isEmpty()) {
                            status = "没搜到电脑。逐条查：① 手机和电脑在同一个 Wi-Fi 或热点里；" +
                                "② 电脑上的韧传开着（电脑上写着「手机访问」的那个地址，手机浏览器能打开就说明通了）；" +
                                "③ 电脑的防火墙允许 52119/TCP 与 52118/UDP（公司网络常会拦）。" +
                                "也可以把电脑上显示的地址手输到上面。"
                        } else {
                            val first = discovered.first()
                            host = "${first.host}:${first.webPort}"
                            store.host = host
                            status = "搜到 ${discovered.size} 台：" +
                                discovered.joinToString("、") { it.name } +
                                "（已填好第一台，点「连接」）"
                        }
                    }
                },
                modifier = Modifier.testTag("btn-scan"),
            ) { Text(if (scanning) "搜索中…" else "搜索电脑") }
        }
        if (status.isNotBlank()) {
            Text(status, fontSize = 14.sp, modifier = Modifier.testTag("status-text"))
        }
        discovered.forEach { found ->
            Card(Modifier.fillMaxWidth().clickable {
                host = "${found.host}:${found.webPort}"
                store.host = host
                status = "已选择 ${found.name}（${found.platform}）"
            }) {
                Column(Modifier.padding(12.dp)) {
                    Text(found.name, fontWeight = FontWeight.SemiBold)
                    Text("${found.host}:${found.webPort}", fontSize = 13.sp)
                }
            }
        }

        HorizontalDivider()
        Row(verticalAlignment = Alignment.CenterVertically) {
            Checkbox(
                checked = keepAlive,
                onCheckedChange = {
                    keepAlive = it
                    store.keepAlive = it
                    val serviceIntent = LinkService().startIntent(context, "http://${store.host}/")
                    if (it) ContextCompat.startForegroundService(context, serviceIntent)
                    else context.stopService(Intent(context, LinkService::class.java))
                },
                modifier = Modifier.testTag("keepalive-box"),
            )
            Column {
                Text("保持后台连接（推荐）")
                Text(
                    "用前台服务常驻连接：熄屏、锁屏、切到别的 App 都能继续收消息和文件。" +
                        "这正是原生 App 存在的理由——浏览器页面做不到。",
                    fontSize = 12.sp,
                )
            }
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            val launcher = rememberLauncherForActivityResult(
                ActivityResultContracts.RequestPermission()
            ) { }
            LaunchedEffect(Unit) {
                launcher.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
        }
        Text("状态：${LinkService.lastState}", fontSize = 13.sp, modifier = Modifier.testTag("link-state"))
        if (LinkService.lastMessage.isNotBlank()) {
            Text("最近消息：${LinkService.lastMessage}", fontSize = 13.sp)
        }
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
 * 用 [AppState.announcing] 挡住重复启动：设置页连一次、聊天页再连一次时，
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
        return Triple(null, "", "先填电脑地址（电脑窗口上「手机访问」那一行），或者点「搜索电脑」")
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

// ------------------------------------------------------------------ 聊天

@Composable
fun ChatScreen(store: AppState) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var client by remember { mutableStateOf<ApiClient?>(null) }
    var conversations by remember { mutableStateOf<List<Conversation>>(emptyList()) }
    var open by remember { mutableStateOf<Conversation?>(null) }
    var messages by remember { mutableStateOf<List<ChatMessage>>(emptyList()) }
    var error by remember { mutableStateOf("") }
    val listState = rememberLazyListState()

    fun reload(active: ApiClient) {
        scope.launch {
            withContext(Dispatchers.IO) {
                try {
                    val state = active.getJson("/api/state")
                    val list = state.optJSONObject("chat")?.optJSONArray("conversations")
                    val loaded = (0 until (list?.length() ?: 0)).mapNotNull { index ->
                        list?.optJSONObject(index)?.toConversation()
                    }
                    conversations = loaded
                    val current = open
                    if (current != null) {
                        val payload = active.getJson("/api/chat?conv=" + encodeUrl(current.id))
                        val array = payload.optJSONArray("messages")
                        messages = (0 until (array?.length() ?: 0)).mapNotNull { index ->
                            array?.optJSONObject(index)?.toMessage()
                        }
                    }
                } catch (problem: Exception) {
                    error = "读取失败：${problem.message}"
                }
            }
        }
    }

    // 注意：这个函数会发网络请求，必须从 IO 线程调用。第一版在主线程直接调，
    // 结果安卓抛 NetworkOnMainThreadException（message 是 null，界面上只显示
    // "连不上电脑：null"）—— 是 CI 里那张真机截图把它暴露出来的。
    fun connect(): ApiClient? {
        // 设置页刚连上的话直接用那个客户端：两边各自猜地址、各自连接，就会出现
        // "设置页显示已连接、聊天页说连不上"这种自相矛盾的界面。
        store.client?.let { return it }
        return try {
            val (api, cleaned, message) = kotlinx.coroutines.runBlocking { connectToDesktop(store.host) }
            if (api == null) {
                error = message
                return null
            }
            store.host = cleaned
            store.client = api
            announcePresence(store, api)
            error = ""
            client = api
            api
        } catch (problem: Exception) {
            error = "连不上电脑：" + (problem.message ?: problem.javaClass.simpleName)
            null
        }
    }

    LaunchedEffect(Unit) {
        val active = withContext(Dispatchers.IO) { connect() }
        if (active != null) reload(active)
        while (true) {
            delay(3000)
            client?.let { reload(it) }
        }
    }

    Column(Modifier.fillMaxSize().padding(12.dp)) {
        if (error.isNotBlank()) {
            Text(error, color = MaterialTheme.colorScheme.error, fontSize = 13.sp)
        }
        val current = open
        if (current == null) {
            Text("会话", fontWeight = FontWeight.SemiBold, fontSize = 18.sp)
            if (conversations.isEmpty()) {
                Text(
                    "还没有会话。点下面的按钮和电脑打一声招呼就会建好。",
                    fontSize = 13.sp,
                    modifier = Modifier.padding(vertical = 8.dp),
                )
            }
            LazyColumn(Modifier.weight(1f)) {
                items(conversations) { conversation ->
                    Card(
                        Modifier
                            .fillMaxWidth()
                            .padding(vertical = 4.dp)
                            .clickable {
                                open = conversation
                                client?.let { reload(it) }
                            }
                    ) {
                        Column(Modifier.padding(12.dp)) {
                            Text(
                                conversation.title.ifBlank { conversation.id.take(12) },
                                fontWeight = FontWeight.SemiBold,
                            )
                            Text(conversation.lastText, fontSize = 12.sp)
                        }
                    }
                }
            }
            Button(
                onClick = {
                    scope.launch {
                        val active = client ?: withContext(Dispatchers.IO) { connect() }
                        if (active != null) {
                            withContext(Dispatchers.IO) {
                                try {
                                    active.postJson("/api/chat/send", JSONObject().put("text", "你好 👋"))
                                } catch (problem: Exception) {
                                    error = "发送失败：" +
                                        (problem.message ?: problem.javaClass.simpleName)
                                }
                            }
                            reload(active)
                        }
                    }
                },
                modifier = Modifier.fillMaxWidth().testTag("btn-new-chat"),
            ) { Text("和电脑开始聊天") }
        } else {
            Row(verticalAlignment = Alignment.CenterVertically) {
                IconButton(onClick = { open = null }) { Icon(Icons.Filled.ArrowBack, "返回") }
                Text(current.title.ifBlank { "会话" }, fontWeight = FontWeight.SemiBold, fontSize = 18.sp)
            }
            LazyColumn(Modifier.weight(1f), state = listState) {
                items(messages) { message ->
                    Bubble(message, client, onError = { error = it })
                }
            }
            LaunchedEffect(messages.size) {
                if (messages.isNotEmpty()) listState.animateScrollToItem(messages.size - 1)
            }
            Composer(
                context = context,
                client = client,
                conversation = current,
                onSend = { text ->
                    val active = client ?: return@Composer
                    scope.launch {
                        withContext(Dispatchers.IO) {
                            try {
                                active.postJson(
                                    "/api/chat/send",
                                    JSONObject().put("conv", current.id).put("text", text),
                                )
                            } catch (problem: Exception) {
                                error = "发送失败：${problem.message}"
                            }
                        }
                        reload(active)
                    }
                },
                onUploaded = { client?.let { reload(it) } },
                onError = { error = it },
            )
        }
    }
}

/** 输入区：文字、表情面板、附件、语音。 */
@Composable
fun Composer(
    context: android.content.Context,
    client: ApiClient?,
    conversation: Conversation,
    onSend: (String) -> Unit,
    onUploaded: () -> Unit,
    onError: (String) -> Unit,
) {
    var draft by remember { mutableStateOf("") }
    var showEmoji by remember { mutableStateOf(false) }
    var recorder by remember { mutableStateOf<MediaRecorder?>(null) }
    var startedAt by remember { mutableStateOf(0L) }
    val scope = rememberCoroutineScope()

    val pickMedia = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri: Uri? ->
        val active = client
        if (uri != null && active != null) {
            scope.launch { uploadUri(context, active, uri, onUploaded) }
        }
    }

    if (showEmoji) {
        EmojiPad(onPick = { draft += it })
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
        IconButton(onClick = { showEmoji = !showEmoji }, modifier = Modifier.testTag("btn-emoji")) {
            Text("😊", fontSize = 22.sp)
        }
        IconButton(
            onClick = {
                val active = client ?: return@IconButton
                val granted = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) ==
                    PackageManager.PERMISSION_GRANTED
                if (!granted) {
                    onError("需要麦克风权限才能发语音")
                    return@IconButton
                }
                val current = recorder
                if (current == null) {
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
                    startedAt = System.currentTimeMillis()
                    onError("正在录音…再按一次结束并发送")
                } else {
                    try {
                        current.stop()
                    } catch (ignored: Exception) {
                    }
                    current.release()
                    recorder = null
                    val seconds = (System.currentTimeMillis() - startedAt) / 1000.0
                    val file = context.cacheDir.listFiles()
                        ?.filter { it.name.startsWith("voice-") }
                        ?.maxByOrNull { it.lastModified() }
                    if (file != null && seconds >= 0.6) {
                        scope.launch { uploadFile(active, file, "voice", (seconds * 1000).toInt(), onUploaded) }
                    } else {
                        onError("太短了，没发出去")
                    }
                }
            },
            modifier = Modifier.testTag("btn-voice"),
        ) { Text(if (recorder == null) "🎤" else "⏹", fontSize = 20.sp) }

        IconButton(onClick = { pickMedia.launch("*/*") }, modifier = Modifier.testTag("btn-attach")) {
            Icon(Icons.Filled.AttachFile, "附件")
        }

        OutlinedTextField(
            value = draft,
            onValueChange = { draft = it },
            modifier = Modifier.weight(1f).testTag("chat-input"),
            placeholder = { Text("说点什么…") },
            maxLines = 3,
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
            keyboardActions = KeyboardActions(onSend = {
                if (draft.isNotBlank()) {
                    onSend(draft.trim())
                    draft = ""
                }
            }),
        )
        IconButton(
            onClick = {
                if (draft.isNotBlank()) {
                    onSend(draft.trim())
                    draft = ""
                }
            },
            modifier = Modifier.testTag("btn-send"),
        ) { Icon(Icons.Filled.Send, "发送") }
    }
}

/**
 * 一条消息。四种附件都要能真的用起来，而不是只显示个文件名：
 *
 * * 图片：气泡里直接显示缩略图，点开全屏看大图；
 * * 视频：气泡里是播放卡片，点开在应用内播放（VideoView 走媒体接口的 URL）；
 * * 语音：气泡里能直接播放/停止（MediaPlayer 同上）；
 * * 文件：一键「保存到手机」（写进系统「下载」目录，SAF/MediaStore 都走通）。
 *
 * 文字除了长按选择，还给了显式的「复制」按钮 —— 长按能选、但用户不一定知道。
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun Bubble(message: ChatMessage, client: ApiClient?, onError: (String) -> Unit = {}) {
    val outgoing = message.outgoing
    val context = LocalContext.current
    val clipboard = LocalClipboardManager.current
    val scope = rememberCoroutineScope()
    var preview by remember { mutableStateOf(false) }
    var playing by remember { mutableStateOf(false) }
    var player by remember { mutableStateOf<MediaPlayer?>(null) }
    var saving by remember { mutableStateOf(false) }
    val hasMedia = message.kind != "text" && message.kind != "system"

    DisposableEffect(message.id) {
        onDispose {
            try {
                player?.stop()
            } catch (ignored: Exception) {
            }
            try {
                player?.release()
            } catch (ignored: Exception) {
            }
            player = null
        }
    }

    if (preview) {
        MediaViewer(message = message, client = client, onClose = { preview = false })
    }

    Row(
        Modifier.fillMaxWidth(),
        horizontalArrangement = if (outgoing) Arrangement.End else Arrangement.Start,
    ) {
        Column(
            Modifier
                .widthIn(max = 300.dp)
                .background(
                    if (outgoing) Color(0xFFDCEBFF) else Color(0xFFF0F2F5),
                    RoundedCornerShape(12.dp),
                )
                .padding(10.dp),
        ) {
            Text("${message.senderName.ifBlank { "对方" }} · ${clock(message.ts)}", fontSize = 11.sp)
            when (message.kind) {
                "text", "system" -> SelectionContainer { Text(message.text, fontSize = 15.sp) }
                "image" -> ImagePreview(message, client, onError) { preview = true }
                "video" -> MediaCard(
                    icon = "🎬",
                    title = message.mediaName.ifBlank { "视频" },
                    detail = fmtSize(message.mediaSize) + " · 点击播放",
                    tag = "video-card",
                ) { preview = true }
                "voice" -> MediaCard(
                    icon = if (playing) "⏹" else "🎤",
                    title = "语音 " + (message.durationMs / 1000) + " 秒",
                    detail = if (playing) "点击停止" else "点击播放",
                    tag = "voice-card",
                ) {
                    val active = client
                    if (active == null) {
                        onError("还没有连上电脑")
                    } else if (playing) {
                        try {
                            player?.stop()
                        } catch (ignored: Exception) {
                        }
                        playing = false
                    } else {
                        try {
                            val media = MediaPlayer()
                            media.setDataSource(active.mediaUrl(message.id))
                            media.setOnPreparedListener {
                                it.start()
                                playing = true
                            }
                            media.setOnCompletionListener { playing = false }
                            media.setOnErrorListener { _, _, _ ->
                                playing = false
                                onError("这段语音播不了")
                                true
                            }
                            media.prepareAsync()
                            player = media
                        } catch (problem: Exception) {
                            onError("语音播放失败：${problem.message}")
                        }
                    }
                }
                else -> MediaCard(
                    icon = "📄",
                    title = message.mediaName.ifBlank { "文件" },
                    detail = fmtSize(message.mediaSize) + " · 点击保存到手机",
                    tag = "file-card",
                ) {
                    val active = client
                    if (active == null) {
                        onError("还没有连上电脑")
                    } else if (!saving) {
                        saving = true
                        scope.launch {
                            val name = message.mediaName.ifBlank { "eversend-${message.id}" }
                            val saved = withContext(Dispatchers.IO) {
                                saveToDownloads(context, name) { sink ->
                                    active.saveMedia(message.id, sink)
                                }
                            }
                            saving = false
                            if (saved == null) onError("保存失败（存储权限或空间不足）")
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
                SelectionContainer { Text(caption, fontSize = 14.sp) }
            }
            // 有文字就给「复制」，有附件就给「保存」——图片/视频本身没有文字，
            // 但「保存到手机」正是它最需要的动作（第一版把整行放在"有文字"的
            // 条件里，图片气泡上于是既没有复制也没有保存）。
            val copyable = message.kind == "text" || message.kind == "system" || caption.isNotBlank()
            if (copyable || hasMedia) {
                Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    if (copyable) {
                        Text(
                            "复制",
                            fontSize = 12.sp,
                            color = Color(0xFF2F81F7),
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
                        Text(
                            "保存",
                            fontSize = 12.sp,
                            color = Color(0xFF2F81F7),
                            modifier = Modifier
                                .clickable {
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
                Text("发送失败（对方不在线）", fontSize = 11.sp, color = Color(0xFFB42318))
            }
        }
    }
}

/** 图片气泡：真图缩略图，点开全屏。 */
@Composable
fun ImagePreview(message: ChatMessage, client: ApiClient?, onError: (String) -> Unit, onOpen: () -> Unit) {
    var bitmap by remember(message.id) { mutableStateOf<android.graphics.Bitmap?>(null) }
    var failed by remember(message.id) { mutableStateOf(false) }
    LaunchedEffect(message.id) {
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
                .clickable { onOpen() }
                .testTag("image-preview"),
        )
        failed -> Text("🖼 ${message.mediaName}（预览失败，可「保存」后在本机看）", fontSize = 14.sp)
        else -> Text("🖼 ${message.mediaName} 载入中…", fontSize = 14.sp)
    }
}

/** 视频/语音/文件的卡片：图标 + 标题 + 一行说明。 */
@Composable
fun MediaCard(icon: String, title: String, detail: String, tag: String, onClick: () -> Unit) {
    Row(
        Modifier
            .fillMaxWidth()
            .clickable { onClick() }
            .padding(vertical = 6.dp, horizontal = 4.dp)
            .testTag(tag),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text(icon, fontSize = 26.sp)
        Column {
            Text(title, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
            Text(detail, fontSize = 12.sp, color = Color(0xFF5B6470))
        }
    }
}

/**
 * 全屏看图片 / 播视频。
 *
 * 视频用系统自带的 VideoView：它认 URL、走 HTTP Range，和手机页面里的
 * <video> 是同一条路。不需要 ExoPlayer，也就不需要多一个第三方依赖。
 */
@Composable
fun MediaViewer(message: ChatMessage, client: ApiClient?, onClose: () -> Unit) {
    val context = LocalContext.current
    Dialog(onDismissRequest = { onClose() }) {
        Card(Modifier.fillMaxWidth().padding(8.dp)) {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(
                    message.mediaName.ifBlank { "预览" },
                    fontWeight = FontWeight.SemiBold,
                    modifier = Modifier.testTag("viewer-title"),
                )
                when (message.kind) {
                    "image" -> {
                        var bitmap by remember(message.id) { mutableStateOf<android.graphics.Bitmap?>(null) }
                        LaunchedEffect(message.id) {
                            val active = client ?: return@LaunchedEffect
                            withContext(Dispatchers.IO) {
                                try {
                                    val bytes = active.mediaBytes(message.id)
                                    bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                                } catch (ignored: Exception) {
                                }
                            }
                        }
                        val image = bitmap
                        if (image != null) {
                            Image(
                                bitmap = image.asImageBitmap(),
                                contentDescription = message.mediaName,
                                contentScale = ContentScale.Fit,
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .heightIn(max = 420.dp)
                                    .testTag("viewer-image"),
                            )
                        } else {
                            Text("载入中…")
                        }
                    }
                    else -> {
                        val url = client?.mediaUrl(message.id).orEmpty()
                        AndroidView(
                            factory = { ctx ->
                                android.widget.VideoView(ctx).apply {
                                    setVideoURI(Uri.parse(url))
                                    setOnPreparedListener { it.isLooping = false; start() }
                                    setMediaController(android.widget.MediaController(ctx).also { it.setAnchorView(this) })
                                }
                            },
                            modifier = Modifier
                                .fillMaxWidth()
                                .height(240.dp)
                                .testTag("viewer-video"),
                        )
                        Text("视频走局域网直连播放，拖动进度条即可跳转。", fontSize = 12.sp, color = Color(0xFF5B6470))
                    }
                }
                Button(onClick = { onClose() }, modifier = Modifier.testTag("viewer-close")) { Text("关闭") }
            }
        }
    }
}

/** 内置 emoji 面板：发出去的就是普通 Unicode 字符，跨设备一定能显示。 */
@Composable
fun EmojiPad(onPick: (String) -> Unit) {
    val emojis = listOf(
        "😀", "😂", "🥹", "😊", "😍", "😘", "🤔", "😴",
        "😎", "🤩", "😭", "😅", "🙃", "😇", "🥳", "🤝",
        "👍", "👎", "👌", "🙏", "👏", "💪", "🤙", "✌️",
        "❤️", "💔", "🔥", "✨", "🎉", "🎁", "⭐", "💡",
        "✅", "❌", "⚠️", "❓", "❗", "📎", "📷", "🎬",
        "🎵", "🎤", "💻", "📱", "📁", "📄", "🗑️", "🚀",
    )
    Column(Modifier.fillMaxWidth().padding(4.dp)) {
        emojis.chunked(8).forEach { row ->
            Row(Modifier.fillMaxWidth()) {
                row.forEach { emoji ->
                    Text(
                        emoji,
                        fontSize = 24.sp,
                        modifier = Modifier
                            .padding(4.dp)
                            .clickable { onPick(emoji) }
                            .testTag("emoji-$emoji"),
                    )
                }
            }
        }
    }
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

/** 保存下载的附件到公共下载目录，并返回可分享的 URI。 */
/** 人类可读的大小，气泡里那行说明用。 */
fun fmtSize(bytes: Long): String {
    if (bytes <= 0) return ""
    val units = listOf("B", "KB", "MB", "GB")
    var value = bytes.toDouble()
    var unit = 0
    while (value >= 1024 && unit < units.size - 1) {
        value /= 1024
        unit++
    }
    return if (unit == 0) "${bytes} B" else String.format(Locale.US, "%.1f %s", value, units[unit])
}

fun saveToDownloads(context: android.content.Context, name: String, sink: (java.io.OutputStream) -> Unit): Uri? {
    return try {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val values = android.content.ContentValues().apply {
                put(android.provider.MediaStore.Downloads.DISPLAY_NAME, name)
                put(android.provider.MediaStore.Downloads.MIME_TYPE, "application/octet-stream")
                put(android.provider.MediaStore.Downloads.IS_PENDING, 1)
            }
            val resolver = context.contentResolver
            val uri = resolver.insert(android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            if (uri != null) {
                resolver.openOutputStream(uri)?.use(sink)
                values.clear()
                values.put(android.provider.MediaStore.Downloads.IS_PENDING, 0)
                resolver.update(uri, values, null, null)
            }
            uri
        } else {
            val directory = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), "")
            directory.mkdirs()
            val file = File(directory, name)
            file.outputStream().use(sink)
            Uri.fromFile(file)
        }
    } catch (problem: Exception) {
        Log.d("EverSend", "保存失败: ${problem.message}")
        null
    }
}

// ------------------------------------------------------------------ 发送

/**
 * 发送页：选设备 → 选文件 → 发送。
 *
 * 走的是电脑端已有的 `/api/upload`：字节流上去，电脑再按设备的身份转发或落地。
 * 选择设备时同时列出「协议对端（电脑）」和「网页/App 客户端（手机）」，
 * 后者会在电脑端变成一次交接。
 */
@Composable
fun SendScreen(store: AppState) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var devices by remember { mutableStateOf<List<DeviceRow>>(emptyList()) }
    var picked by remember { mutableStateOf<List<Uri>>(emptyList()) }
    var target by remember { mutableStateOf<DeviceRow?>(null) }
    var progress by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var status by remember { mutableStateOf("") }

    val pick = rememberLauncherForActivityResult(ActivityResultContracts.GetMultipleContents()) { uris ->
        if (uris.isNotEmpty()) picked = uris
    }

    LaunchedEffect(Unit) {
        while (true) {
            withContext(Dispatchers.IO) {
                try {
                    val base = "http://${normalizeHost(store.host)}/"
                    val token = ApiClient.fetchToken(base)
                    if (token.isNotBlank()) {
                        val api = ApiClient(base, token)
                        devices = loadDevices(api, store)
                    }
                } catch (problem: Exception) {
                    status = "连不上电脑：${problem.message ?: problem.javaClass.simpleName}"
                }
            }
            delay(4000)
        }
    }

    Column(Modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text("选择接收设备", fontWeight = FontWeight.SemiBold, fontSize = 18.sp)
        if (devices.isEmpty()) Text("还没有发现设备。确认手机和电脑在同一 Wi-Fi。", fontSize = 13.sp)
        LazyColumn(Modifier.weight(1f)) {
            items(devices) { device ->
                Card(
                    Modifier
                        .fillMaxWidth()
                        .padding(vertical = 4.dp)
                        .clickable { target = device }
                ) {
                    Column(Modifier.padding(12.dp)) {
                        Text(
                            device.name + if (device.id == target?.id) "  ✅" else "",
                            fontWeight = FontWeight.SemiBold,
                        )
                        Text("${device.kind} · ${device.address}", fontSize = 12.sp)
                    }
                }
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = { pick.launch("*/*") }, modifier = Modifier.testTag("btn-pick")) {
                Text(if (picked.isEmpty()) "选择文件" else "已选 ${picked.size} 个")
            }
            Button(
                enabled = !busy && picked.isNotEmpty() && target != null,
                onClick = {
                    val device = target ?: return@Button
                    busy = true
                    status = ""
                    scope.launch {
                        withContext(Dispatchers.IO) {
                            try {
                                val base = "http://${normalizeHost(store.host)}/"
                                val api = ApiClient(base, ApiClient.fetchToken(base))
                                picked.forEach { uri ->
                                    val info = queryFile(context, uri)
                                    val query = "/api/upload?name=${encodeUrl(info.first)}" +
                                        "&deviceId=${encodeUrl(device.id)}"
                                    context.contentResolver.openInputStream(uri)?.use { stream ->
                                        api.upload(query, stream, info.second, "application/octet-stream") {
                                            progress = "已发送 $it 字节"
                                        }
                                    }
                                }
                                status = "已交给电脑端，正在传输"
                            } catch (problem: Exception) {
                                status = "发送失败：${problem.message ?: problem.javaClass.simpleName}"
                            }
                        }
                        busy = false
                        picked = emptyList()
                    }
                },
                modifier = Modifier.testTag("btn-send-files"),
            ) { Text(if (busy) "发送中…" else "发送") }
        }
        if (progress.isNotBlank()) Text(progress, fontSize = 12.sp)
        if (status.isNotBlank()) Text(status, fontSize = 13.sp)
    }
}

// ------------------------------------------------------------------ 接收

/** 接收页：待确认的传输 + 电脑上的文件（可直接下载到手机）。 */
@Composable
fun ReceiveScreen(store: AppState) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var offers by remember { mutableStateOf<List<JSONObject>>(emptyList()) }
    var files by remember { mutableStateOf<List<Pair<String, Long>>>(emptyList()) }
    var status by remember { mutableStateOf("") }

    LaunchedEffect(Unit) {
        while (true) {
            withContext(Dispatchers.IO) {
                try {
                    val base = "http://${normalizeHost(store.host)}/"
                    val api = ApiClient(base, ApiClient.fetchToken(base))
                    val state = api.getJson("/api/state")
                    val list = state.optJSONArray("offers")
                    offers = (0 until (list?.length() ?: 0)).mapNotNull { list?.optJSONObject(it) }
                    val payload = api.getJson("/api/files")
                    val array = payload.optJSONArray("files")
                    files = (0 until (array?.length() ?: 0)).mapNotNull { index ->
                        val entry = array?.optJSONObject(index) ?: return@mapNotNull null
                        Pair(entry.optString("path"), entry.optLong("size", 0))
                    }
                } catch (problem: Exception) {
                    status = "连不上电脑"
                }
            }
            delay(3000)
        }
    }

    Column(Modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text("待确认", fontWeight = FontWeight.SemiBold, fontSize = 18.sp)
        if (offers.isEmpty()) Text("没有等待确认的传输。", fontSize = 13.sp)
        offers.forEach { offer ->
            val requestId = offer.optString("request_id")
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp)) {
                    Text(offer.optJSONObject("peer")?.optString("name") ?: "对方", fontWeight = FontWeight.SemiBold)
                    Text("${offer.optInt("files")} 个文件 · ${offer.optLong("total")} 字节", fontSize = 12.sp)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(onClick = {
                            scope.launch {
                                withContext(Dispatchers.IO) {
                                    try {
                                        val base = "http://${normalizeHost(store.host)}/"
                                        val api = ApiClient(base, ApiClient.fetchToken(base))
                                        api.postJson(
                                            "/api/offer/respond",
                                            JSONObject().put("requestId", requestId).put("accept", true),
                                        )
                                    } catch (problem: Exception) {
                                        status = "接受失败"
                                    }
                                }
                            }
                        }) { Text("接收") }
                        OutlinedButton(onClick = {
                            scope.launch {
                                withContext(Dispatchers.IO) {
                                    try {
                                        val base = "http://${normalizeHost(store.host)}/"
                                        val api = ApiClient(base, ApiClient.fetchToken(base))
                                        api.postJson(
                                            "/api/offer/respond",
                                            JSONObject().put("requestId", requestId).put("accept", false),
                                        )
                                    } catch (ignored: Exception) {
                                    }
                                }
                            }
                        }) { Text("拒绝") }
                    }
                }
            }
        }
        Text("电脑上的文件", fontWeight = FontWeight.SemiBold, fontSize = 18.sp)
        if (files.isEmpty()) Text("电脑上还没有文件。", fontSize = 13.sp)
        LazyColumn(Modifier.weight(1f)) {
            items(files) { entry ->
                Card(
                    Modifier
                        .fillMaxWidth()
                        .padding(vertical = 4.dp)
                        .clickable {
                            scope.launch {
                                withContext(Dispatchers.IO) {
                                    try {
                                        val base = "http://${normalizeHost(store.host)}/"
                                        val api = ApiClient(base, ApiClient.fetchToken(base))
                                        val name = entry.first.substringAfterLast('/')
                                        val saved = saveToDownloads(context, name) { sink ->
                                            api.download(
                                                "/api/download?path=" + encodeUrl(entry.first),
                                                sink,
                                            )
                                        }
                                        status = if (saved != null) "已下载到「下载」目录：$name" else "下载失败"
                                    } catch (problem: Exception) {
                                        status = "下载失败：${problem.message ?: problem.javaClass.simpleName}"
                                    }
                                }
                            }
                        }
                ) {
                    Column(Modifier.padding(12.dp)) {
                        Text(entry.first.substringAfterLast('/'), fontWeight = FontWeight.SemiBold)
                        Text("${entry.second} 字节 · 点一下下载", fontSize = 12.sp)
                    }
                }
            }
        }
        if (status.isNotBlank()) Text(status, fontSize = 13.sp, modifier = Modifier.testTag("receive-status"))
    }
}

// ------------------------------------------------------------------ 传输

/** 传输页：正在跑的传输与最近的上传。 */
@Composable
fun TransfersScreen(store: AppState) {
    var transfers by remember { mutableStateOf<List<JSONObject>>(emptyList()) }
    var uploads by remember { mutableStateOf<List<JSONObject>>(emptyList()) }
    var status by remember { mutableStateOf("") }

    LaunchedEffect(Unit) {
        while (true) {
            withContext(Dispatchers.IO) {
                try {
                    val base = "http://${normalizeHost(store.host)}/"
                    val api = ApiClient(base, ApiClient.fetchToken(base))
                    val state = api.getJson("/api/state")
                    val list = state.optJSONArray("transfers")
                    transfers = (0 until (list?.length() ?: 0)).mapNotNull { list?.optJSONObject(it) }
                    val recent = state.optJSONArray("recentUploads")
                    uploads = (0 until (recent?.length() ?: 0)).mapNotNull { recent?.optJSONObject(it) }
                    status = ""
                } catch (problem: Exception) {
                    status = "连不上电脑"
                }
            }
            delay(2000)
        }
    }

    Column(Modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text("正在传输", fontWeight = FontWeight.SemiBold, fontSize = 18.sp)
        if (transfers.isEmpty()) Text("当前没有传输。", fontSize = 13.sp)
        transfers.forEach { transfer ->
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp)) {
                    val direction = if (transfer.optString("direction") == "send") "发送到" else "接收自"
                    Text(
                        "$direction ${transfer.optJSONObject("peer")?.optString("name") ?: "对方"}",
                        fontWeight = FontWeight.SemiBold,
                    )
                    val done = transfer.optLong("doneBytes")
                    val total = transfer.optLong("totalBytes").coerceAtLeast(1)
                    LinearProgressIndicator(
                        progress = { (done.toFloat() / total).coerceIn(0f, 1f) },
                        modifier = Modifier.fillMaxWidth().padding(vertical = 6.dp),
                    )
                    Text("$done / $total 字节", fontSize = 12.sp)
                }
            }
        }
        Text("最近的上传", fontWeight = FontWeight.SemiBold, fontSize = 18.sp)
        if (uploads.isEmpty()) Text("还没有上传记录。", fontSize = 13.sp)
        LazyColumn {
            items(uploads) { upload ->
                Card(Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
                    Column(Modifier.padding(12.dp)) {
                        Text(upload.optString("name"), fontWeight = FontWeight.SemiBold)
                        Text(
                            upload.optString("status") + " · " + upload.optLong("size") + " 字节",
                            fontSize = 12.sp,
                        )
                    }
                }
            }
        }
        if (status.isNotBlank()) Text(status, fontSize = 13.sp)
    }
}

/** 设备行：电脑（协议对端）与手机（网页/App 客户端）都在这里。 */
data class DeviceRow(val id: String, val name: String, val kind: String, val address: String)

fun loadDevices(api: ApiClient, store: AppState): List<DeviceRow> {
    val rows = mutableListOf<DeviceRow>()
    val state = api.getJson("/api/state")
    val devices = state.optJSONArray("devices")
    for (index in 0 until (devices?.length() ?: 0)) {
        val device = devices.optJSONObject(index) ?: continue
        if (device.optBoolean("isSelf")) continue
        rows.add(
            DeviceRow(
                id = device.optString("id"),
                name = device.optString("name"),
                kind = "电脑",
                address = "${device.optString("address")}:${device.optInt("port")}",
            )
        )
    }
    val clients = state.optJSONArray("knownClients")
    for (index in 0 until (clients?.length() ?: 0)) {
        val client = clients.optJSONObject(index) ?: continue
        val deviceId = client.optString("deviceId")
        if (deviceId == store.deviceId) continue          // 就是本机
        rows.add(
            DeviceRow(
                id = "web:" + client.optString("key"),
                name = client.optString("label", "手机"),
                kind = "手机",
                address = client.optString("address"),
            )
        )
    }
    return rows
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
