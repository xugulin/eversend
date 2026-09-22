package cn.eversend.app

import android.content.Context
import android.os.Build
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStream
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.SocketTimeoutException
import java.net.URL
import java.net.URLEncoder

/**
 * 与电脑端通信的客户端。
 *
 * 为什么走 HTTP 而不是自己实现二进制协议
 * --------------------------------------
 * 电脑端已经有一套经过测试的 HTTP 接口（`/api/state`、`/api/chat/…`、`/api/upload`、
 * `/api/chat/media/…`、SSE 事件流），网页版就是用它工作的。原生 App 复用同一套接口，
 * 得到的是同一份正确性；而浏览器的那些限制（熄屏冻结、后台挂起、麦克风要 HTTPS）
 * **不是 HTTP 带来的，是浏览器带来的** —— 换成原生代码 + 前台服务就都没了。
 *
 * 这也意味着：手机与手机之间的直连（二进制协议那条路）暂时不走这里，手机都是通过
 * 电脑中转，和网页版一致。这一点在 README 里写明了。
 */
class ApiClient(val base: String, val token: String) {

    private fun open(path: String): HttpURLConnection {
        val url = URL(base.trimEnd('/') + path)
        val conn = url.openConnection() as HttpURLConnection
        conn.connectTimeout = 8000
        conn.readTimeout = 30000
        conn.setRequestProperty("Accept", "application/json")
        conn.setRequestProperty("X-EverSend-Token", token)
        conn.setRequestProperty("User-Agent", userAgent())
        return conn
    }

    fun getJson(path: String): JSONObject {
        val conn = open(path)
        try {
            val text = conn.inputStream.reader(Charsets.UTF_8).readText()
            return if (text.isBlank()) JSONObject() else JSONObject(text)
        } finally {
            conn.disconnect()
        }
    }

    fun postJson(path: String, body: JSONObject): JSONObject {
        val conn = open(path)
        conn.requestMethod = "POST"
        conn.doOutput = true
        conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
        try {
            conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            val stream = if (conn.responseCode in 200..299) conn.inputStream else conn.errorStream
            val text = stream?.reader(Charsets.UTF_8)?.readText().orEmpty()
            return if (text.isBlank()) JSONObject() else JSONObject(text)
        } finally {
            conn.disconnect()
        }
    }

    /** 上传附件：字节从 content URI 直接流进 socket，不整份读进内存。 */
    fun upload(
        path: String,
        stream: InputStream,
        length: Long,
        mime: String,
        onProgress: (Long) -> Unit = {},
    ): JSONObject {
        val conn = open(path)
        conn.requestMethod = "POST"
        conn.doOutput = true
        conn.setFixedLengthStreamingMode(length)
        conn.setRequestProperty("Content-Type", mime.ifBlank { "application/octet-stream" })
        try {
            conn.outputStream.use { out ->
                val buffer = ByteArray(64 * 1024)
                var sent = 0L
                while (true) {
                    val read = stream.read(buffer)
                    if (read <= 0) break
                    out.write(buffer, 0, read)
                    sent += read
                    onProgress(sent)
                }
            }
            val body = if (conn.responseCode in 200..299) conn.inputStream else conn.errorStream
            val text = body?.reader(Charsets.UTF_8)?.readText().orEmpty()
            return if (text.isBlank()) JSONObject() else JSONObject(text)
        } finally {
            conn.disconnect()
        }
    }

    fun download(path: String, sink: java.io.OutputStream, onProgress: (Long) -> Unit = {}): Long {
        val conn = open(path)
        try {
            var total = 0L
            conn.inputStream.use { input ->
                val buffer = ByteArray(64 * 1024)
                while (true) {
                    val read = input.read(buffer)
                    if (read <= 0) break
                    sink.write(buffer, 0, read)
                    total += read
                    onProgress(total)
                }
            }
            return total
        } finally {
            conn.disconnect()
        }
    }

    /** 事件流：电脑端一有消息就推过来，App 不用轮询。 */
    fun events(onEvent: (JSONObject) -> Unit, isRunning: () -> Boolean) {
        val conn = open("/api/events")
        conn.setRequestProperty("Accept", "text/event-stream")
        conn.readTimeout = 0
        try {
            val reader = BufferedReader(InputStreamReader(conn.inputStream, Charsets.UTF_8))
            val data = StringBuilder()
            while (isRunning()) {
                val line = try {
                    reader.readLine()
                } catch (timeout: SocketTimeoutException) {
                    continue
                } ?: break
                if (line.isEmpty()) {
                    if (data.isNotEmpty()) {
                        try {
                            onEvent(JSONObject(data.toString()))
                        } catch (ignored: Exception) {
                            Log.d(TAG, "跳过无法解析的事件")
                        }
                        data.setLength(0)
                    }
                } else if (line.startsWith("data:")) {
                    data.append(line.removePrefix("data:").trim())
                }
            }
        } finally {
            conn.disconnect()
        }
    }

    companion object {
        const val TAG = "EverSend"

        fun userAgent(): String = "EverSend-Android/1.0 (Android ${Build.VERSION.RELEASE})"

        /** 取网页里的 CSRF 令牌：接口要求每个 POST 带上它。 */
        fun fetchToken(base: String): String {
            val conn = (URL(base.trimEnd('/') + "/").openConnection() as HttpURLConnection).apply {
                connectTimeout = 6000
                readTimeout = 15000
                setRequestProperty("User-Agent", userAgent())
            }
            try {
                val html = conn.inputStream.reader(Charsets.UTF_8).readText()
                val match = Regex("name=\"eversend-token\"\\s+content=\"([^\"]+)\"").find(html)
                return match?.groupValues?.get(1).orEmpty()
            } finally {
                conn.disconnect()
            }
        }

        /** 广播公告用的端口，和电脑端约定一致。 */
        const val DISCOVERY_PORT = 52118

        /** 电脑端用的组播地址；写错成别的组就永远收不到电脑的定时公告。 */
        const val MULTICAST_GROUP = "239.255.83.68"

        /**
         * 在局域网里找电脑：**先绑住 52118 收公告，再发一条自己的探针**。
         *
         * 第一版只发探针、不等回包，永远找不到电脑——两个原因：电脑端的广播
         * 间隔是 30 秒（等不到），而且它原本不回探针。现在电脑端收到公告会立刻
         * 单播回一条（回包发到来源端口，所以任何端口的 socket 都收得到）。
         *
         * 绑定 52118 是第二条路：即使回包被路由器丢掉，也能听到电脑的定时广播。
         */
        fun discover(timeoutMs: Int = 4000, deviceId: String = "probe"): List<Found> {
            val found = LinkedHashMap<String, Found>()
            val socket = DatagramSocket(null)
            var bound = false
            try {
                socket.reuseAddress = true
                socket.bind(InetSocketAddress(DISCOVERY_PORT))
                bound = true
            } catch (ignored: Exception) {
                // 端口被别的进程占着也没关系，还能靠探针的回包。
                socket.bind(InetSocketAddress(0))
            }
            socket.broadcast = true
            socket.soTimeout = 500
            val deadline = System.currentTimeMillis() + timeoutMs
            try {
                val probe = """{"t":"eversend/1","id":"$deviceId","n":"安卓 App","k":"mobile",""" +
                    """"p":"android","v":"1.0.0","port":0,"web":0,"ts":0}"""
                val payload = probe.toByteArray(Charsets.UTF_8)
                val targets = mutableListOf(InetAddress.getByName("255.255.255.255"))
                try {
                    targets.add(InetAddress.getByName(MULTICAST_GROUP))
                } catch (ignored: Exception) {
                }
                var lastProbe = 0L
                while (System.currentTimeMillis() < deadline) {
                    if (System.currentTimeMillis() - lastProbe > 1000) {
                        lastProbe = System.currentTimeMillis()
                        for (target in targets) {
                            try {
                                socket.send(DatagramPacket(payload, payload.size, target, DISCOVERY_PORT))
                            } catch (ignored: Exception) {
                            }
                        }
                    }
                    val buffer = ByteArray(8192)
                    try {
                        val packet = DatagramPacket(buffer, buffer.size)
                        socket.receive(packet)
                        val text = String(packet.data, 0, packet.length, Charsets.UTF_8)
                        val json = try {
                            JSONObject(text)
                        } catch (ignored: Exception) {
                            continue
                        }
                        if (json.optString("t") != "eversend/1") continue
                        val web = json.optInt("web", 0)
                        if (web <= 0) continue
                        val host = packet.address.hostAddress ?: continue
                        found["$host:$web"] = Found(
                            name = json.optString("n", host),
                            host = host,
                            webPort = web,
                            platform = json.optString("p", ""),
                        )
                        if (found.size >= 8) break
                    } catch (ignored: SocketTimeoutException) {
                    }
                }
            } finally {
                socket.close()
            }
            if (!bound) Log.d(TAG, "52118 被占用，只靠探针回包发现")
            return found.values.toList()
        }

        /** 定期广播自己的存在，电脑端就能在设备列表里看到这台手机。 */
        fun announce(deviceId: String, name: String, version: String = "1.0.0") {
            val socket = DatagramSocket()
            socket.broadcast = true
            try {
                val payload = (
                    "{\"t\":\"eversend/1\",\"id\":\"" + deviceId +
                        "\",\"n\":\"" + escapeJson(name) +
                        "\",\"k\":\"mobile\",\"p\":\"android\",\"v\":\"" + version +
                        "\",\"port\":0,\"web\":0,\"ts\":" + (System.currentTimeMillis() / 1000) + "}"
                    ).toByteArray(Charsets.UTF_8)
                for (target in listOf("255.255.255.255", MULTICAST_GROUP)) {
                    try {
                        socket.send(
                            DatagramPacket(
                                payload, payload.size,
                                InetAddress.getByName(target), DISCOVERY_PORT,
                            )
                        )
                    } catch (ignored: Exception) {
                    }
                }
            } catch (ignored: Exception) {
            } finally {
                socket.close()
            }
        }

        /** 最小的 JSON 字符串转义：设备名是用户自己起的，可能带引号或反斜杠。 */
        fun escapeJson(value: String): String {
            val builder = StringBuilder()
            for (ch in value) {
                when (ch) {
                    '"' -> builder.append("\\\"")
                    '\\' -> builder.append("\\\\")
                    '\n' -> builder.append("\\n")
                    '\r' -> builder.append("\\r")
                    '\t' -> builder.append("\\t")
                    else -> if (ch < ' ') builder.append("?") else builder.append(ch)
                }
            }
            return builder.toString()
        }

    }

    /** 发现到的一台电脑。放在 companion 外面，类型名才是 ``ApiClient.Found``。 */
    data class Found(val name: String, val host: String, val webPort: Int, val platform: String) {
        val base: String get() = "http://$host:$webPort/"
    }
}

/** 消息/会话的轻量视图模型，只保留界面用得上的字段。 */
data class ChatMessage(
    val id: String,
    val conversation: String,
    val senderName: String,
    val kind: String,
    val text: String,
    val mediaName: String,
    val mediaSize: Long,
    val durationMs: Int,
    val ts: Double,
    val outgoing: Boolean,
    val state: String,
)

data class Conversation(
    val id: String,
    val title: String,
    val kind: String,
    val lastText: String,
    val unread: Int,
    val members: List<String>,
)

fun JSONObject.toMessage(): ChatMessage = ChatMessage(
    id = optString("id"),
    conversation = optString("conv"),
    senderName = optString("senderName"),
    kind = optString("kind", "text"),
    text = optString("text"),
    mediaName = optString("mediaName"),
    mediaSize = optLong("mediaSize", 0),
    durationMs = optInt("durationMs", 0),
    ts = optDouble("ts", 0.0),
    outgoing = optString("direction") == "out",
    state = optString("state", "sent"),
)

fun JSONObject.toConversation(): Conversation = Conversation(
    id = optString("id"),
    title = optString("title"),
    kind = optString("kind", "direct"),
    lastText = optString("lastText"),
    unread = optInt("unread", 0),
    members = optJSONArray("members")?.let { array: JSONArray ->
        (0 until array.length()).map { array.optString(it) }
    } ?: emptyList(),
)

fun encode(value: String): String = URLEncoder.encode(value, "UTF-8")
