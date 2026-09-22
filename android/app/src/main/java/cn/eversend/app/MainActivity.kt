package cn.eversend.app

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
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
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
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
                    icon = { Icon(Icons.Filled.Settings, null) },
                    label = { Text("设置") },
                    modifier = Modifier.testTag("tab-settings"),
                )
            }
        }
    ) { padding ->
        Box(Modifier.padding(padding)) {
            if (tab == 0) ChatScreen(store) else SettingsScreen(store)
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
                    val cleaned = normalizeHost(host)
                    host = cleaned
                    store.host = cleaned
                    status = "正在连接 $cleaned …"
                    scope.launch {
                        status = withContext(Dispatchers.IO) {
                            try {
                                val token = ApiClient.fetchToken("http://$cleaned/")
                                if (token.isBlank()) "连不上：地址对，但拿不到令牌"
                                else {
                                    val state = ApiClient("http://$cleaned/", token).getJson("/api/state")
                                    "已连接：${state.optJSONObject("device")?.optString("name") ?: cleaned}"
                                }
                            } catch (error: Exception) {
                                "连不上：${error.message}"
                            }
                        }
                    }
                },
                modifier = Modifier.testTag("btn-connect"),
            ) { Text("连接") }

            OutlinedButton(
                onClick = {
                    scanning = true
                    scope.launch {
                        discovered = withContext(Dispatchers.IO) { ApiClient.discover() }
                        scanning = false
                        if (discovered.isEmpty()) status = "没有搜到电脑（可手输地址，或检查是否同一 Wi-Fi）"
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

fun normalizeHost(raw: String): String {
    var text = raw.trim().removePrefix("http://").removePrefix("https://").trimEnd('/')
    if (!text.contains(":")) text = "$text:52119"
    return text
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
        val host = normalizeHost(store.host)
        if (host.isBlank()) {
            error = "先在「设置」里填电脑地址"
            return null
        }
        return try {
            val base = "http://$host/"
            val token = ApiClient.fetchToken(base)
            if (token.isBlank()) {
                error = "连不上电脑（$host）：拿不到令牌"
                return null
            }
            ApiClient(base, token).also {
                client = it
                error = ""
            }
        } catch (problem: Exception) {
            error = "连不上电脑（$host）：" +
                (problem.message ?: problem.javaClass.simpleName)
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
                items(messages) { message -> Bubble(message) }
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

@Composable
fun Bubble(message: ChatMessage) {
    val outgoing = message.outgoing
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
                "text", "system" -> Text(message.text, fontSize = 15.sp)
                "image" -> Text("🖼 ${message.mediaName}", fontSize = 15.sp)
                "video" -> Text("🎬 ${message.mediaName}", fontSize = 15.sp)
                "voice" -> Text(
                    "🎤 语音 ${message.durationMs / 1000} 秒",
                    fontSize = 15.sp,
                )
                else -> Text("📄 ${message.mediaName}", fontSize = 15.sp)
            }
            if (outgoing && message.state == "failed") {
                Text("发送失败（对方不在线）", fontSize = 11.sp, color = Color(0xFFB42318))
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
