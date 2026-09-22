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

        /**
         * 在局域网里找电脑：电脑端每隔一会儿往 52118 广播一条 JSON 公告。
         * 找不到也没关系，用户还可以手输地址。
         */
        fun discover(timeoutMs: Int = 2500): List<Found> {
            val found = LinkedHashMap<String, Found>()
            val socket = DatagramSocket()
            socket.broadcast = true
            socket.soTimeout = 500
            val deadline = System.currentTimeMillis() + timeoutMs
            try {
                val payload = """{"t":"eversend/1","id":"probe","n":"android","port":0,"web":0,"ts":0}"""
                    .toByteArray(Charsets.UTF_8)
                val broadcast = InetAddress.getByName("255.255.255.255")
                while (System.currentTimeMillis() < deadline) {
                    try {
                        socket.send(DatagramPacket(payload, payload.size, broadcast, 52118))
                    } catch (ignored: Exception) {
                    }
                    val buffer = ByteArray(4096)
                    try {
                        while (true) {
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
                            val key = "$host:$web"
                            found[key] = Found(
                                name = json.optString("n", host),
                                host = host,
                                webPort = web,
                                platform = json.optString("p", ""),
                            )
                        }
                    } catch (ignored: SocketTimeoutException) {
                    }
                }
            } finally {
                socket.close()
            }
            return found.values.toList()
        }
    }

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
