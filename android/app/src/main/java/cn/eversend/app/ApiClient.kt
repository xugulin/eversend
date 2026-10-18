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
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.NetworkInterface
import java.net.SocketTimeoutException
import java.net.URL
import java.net.URLEncoder
import java.util.Collections

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
class ApiClient(val base: String, token: String) {

    /**
     * 防跨站令牌。**电脑端每次重启都会换一张**，而 App 手里的 client 是连接时建的 ——
     * 电脑重启后旧令牌就 403（"缺少或错误的防跨站令牌"），用户看到的是"发不出去"。
     * 所以它得是 var，并且提供 [refreshToken] 让调用方换一张再试。
     */
    @Volatile
    var token: String = token
        private set

    /** 重新从网页里取一张令牌；成功返回 true（失败保持旧值，让调用方照旧报错）。 */
    fun refreshToken(): Boolean {
        val result = try {
            val fresh = fetchToken(base)
            if (fresh.isBlank()) {
                Log.w("EverSend", "refreshToken: 页面里没取到令牌 base=$base")
                false
            } else {
                token = fresh
                Log.w("EverSend", "refreshToken: 换到新令牌 ${fresh.take(8)} base=$base")
                true
            }
        } catch (problem: Exception) {
            Log.w("EverSend", "refreshToken 失败: ${problem.javaClass.simpleName}: ${problem.message}")
            false
        }
        return result
    }


    private fun open(path: String): HttpURLConnection {
        val url = URL(base.trimEnd('/') + path)
        val conn = url.openConnection() as HttpURLConnection
        conn.connectTimeout = 8000
        conn.readTimeout = 30000
        // **禁用缓存**：Android 的 HttpResponseCache 会把网页/接口的响应缓存下来。
        // 电脑端重启换的是"防跨站令牌"，缓存里那份旧 HTML 会把旧令牌再喂回来 ——
        // 于是"刷新令牌再重试"看起来执行了、却还是 403（真机实测到过）。
        conn.useCaches = false
        conn.setRequestProperty("Cache-Control", "no-cache")
        conn.setRequestProperty("Pragma", "no-cache")
        conn.setRequestProperty("Accept", "application/json")
        conn.setRequestProperty("X-EverSend-Token", token)
        conn.setRequestProperty("User-Agent", userAgent())
        // 每个请求都报上设备号：电脑端靠它认这台手机，而不是靠来源地址。
        // 手机换 Wi-Fi、走热点、被 NAT 改写地址时，来源地址是会变的 —— 地址一
        // 变电脑端就认不出这条记录，于是"刚连上就掉线"。
        identity?.let { id ->
            conn.setRequestProperty("X-EverSend-Device", id)
            name?.let { conn.setRequestProperty("X-EverSend-Name", it) }
        }
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

    /**
     * 上传附件：字节从 content URI 直接流进 socket，不整份读进内存。
     *
     * 支持断点续传（服务端 v1，见 `tests/reports/opt-1017/http-upload-resume-api.md`）：
     * 传了 [uploadId] 就走续传协议（带 `X-EverSend-Total`，body 从 [offset] 起）；
     * 不传就是原来的旧一次性上传（行为完全不变）。
     *
     * **[length] 始终是文件总字节**，body 长度 = length - offset。
     * 返回服务端 JSON；202/409 也正常返回（调用方按 `offset` 续），其它非 2xx 抛异常。
     * [onProgress] 的参数是"整份文件已发出多少字节"（= offset + 本次已发）。
     */
    fun upload(
        path: String,
        stream: InputStream,
        length: Long,
        mime: String,
        uploadId: String = "",
        offset: Long = 0L,
        total: Long = -1L,
        onProgress: (Long) -> Unit = {},
    ): JSONObject {
        val conn = open(path)
        conn.requestMethod = "POST"
        conn.doOutput = true
        val bodyLength = (length - offset).coerceAtLeast(0L)
        conn.setFixedLengthStreamingMode(bodyLength)
        conn.setRequestProperty("Content-Type", mime.ifBlank { "application/octet-stream" })
        if (uploadId.isNotBlank()) {
            // 续传协议要求带总大小：服务端靠它判断"收满没有"以及 409 的偏移。
            conn.setRequestProperty("X-EverSend-Total", (if (total > 0) total else length).toString())
        }
        try {
            if (offset > 0L) skipFully(stream, offset)
            conn.outputStream.use { out ->
                val buffer = ByteArray(64 * 1024)
                var sent = offset
                while (true) {
                    val read = stream.read(buffer)
                    if (read <= 0) break
                    out.write(buffer, 0, read)
                    sent += read
                    onProgress(sent)
                }
            }
            val code = conn.responseCode
            val body = if (code in 200..299 || code == 409) conn.inputStream else conn.errorStream
            val text = body?.reader(Charsets.UTF_8)?.readText().orEmpty()
            val json = if (text.isBlank()) JSONObject() else JSONObject(text)
            // 202 = 收了一部分（`complete=false` + `offset`）；409 = 偏移不匹配
            // （同样带 `offset`）。这两种都交给调用方按服务端给的 offset 续传。
            if (code == 202 || code == 409) {
                val serverOffset = json.optLong("offset", -1L)
                if (serverOffset > 0L) {
                    Log.d(TAG, "上传未完成/偏移不符（HTTP $code）：服务端已有 $serverOffset 字节")
                }
                return json
            }
            // **非 2xx 必须抛**：以前这里不检查状态码 —— 服务端回 400/404/500 时它照样
            // 读 errorStream、解析出 JSON 返回，调用方以为成功、界面写「已发给 N 台
            // 设备」，而电脑那边一个字节都没收到（真机上可复现）。
            // 原因用服务端给的中文 error 字段，用户能直接看懂。
            if (code !in 200..299) {
                val why = json.optString("error").ifBlank { "HTTP $code" }
                throw IllegalStateException(why)
            }
            return json
        } finally {
            conn.disconnect()
        }
    }

    /** 断点续传前问服务端这个 uploadId 已有多少字节；失败返回 -1（调用方保留本地值）。 */
    fun uploadStatus(uploadId: String): Long {
        val conn = open("/api/upload/status?uploadId=" + java.net.URLEncoder.encode(uploadId, "UTF-8"))
        try {
            val text = conn.inputStream.reader(Charsets.UTF_8).readText()
            if (text.isBlank()) return -1L
            val json = JSONObject(text)
            return if (json.optBoolean("ok", false)) json.optLong("offset", -1L) else -1L
        } catch (problem: Exception) {
            return -1L
        } finally {
            conn.disconnect()
        }
    }

    private fun skipFully(stream: InputStream, count: Long) {
        var left = count
        while (left > 0) {
            val skipped = stream.skip(left)
            if (skipped > 0) {
                left -= skipped
                continue
            }
            // skip() 返回 0 不一定是 EOF（网络/ContentProvider 流很常见）：读一个字节探一下。
            if (stream.read() < 0) throw java.io.IOException("文件比预期短：想从第 $count 字节续传，但它没这么长")
            left -= 1
        }
    }

    /**
     * 下载到 [sink]。[offset] > 0 时用 HTTP Range 续传
     * （电脑端 `/api/share`、`/api/chat/media`、`/api/download` 都支持单段 Range 并回 206）。
     *
     * 返回"这条文件一共取到多少字节"（续传时 = offset + 本次收到）。
     * 读超时/连接被系统掐断会抛 IOException，由调用方按 [offset] 续传重试。
     */
    fun download(
        path: String,
        sink: java.io.OutputStream,
        offset: Long = 0L,
        onProgress: (Long) -> Unit = {},
    ): Long {
        val conn = open(path)
        try {
            if (offset > 0L) conn.setRequestProperty("Range", "bytes=$offset-")
            val code = conn.responseCode
            if (code !in 200..299) {
                val why = try {
                    conn.errorStream?.reader(Charsets.UTF_8)?.readText().orEmpty()
                } catch (ignored: Exception) {
                    ""
                }
                throw java.io.IOException("HTTP $code${if (why.isBlank()) "" else "：$why"}")
            }
            if (offset > 0L && code != 206) {
                // 服务端没理 Range（或文件变了）：不能再往半截文件后面追加，
                // 抛一个能识别的错误让调用方从头下。
                throw java.io.IOException("服务端不支持续传（期望 206，实际 $code）")
            }
            var total = offset
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

    /** 聊天里某个附件（图片/视频/语音/文件）的下载地址。
     *
     *  直接给出完整 URL 是有意的：图片用 BitmapFactory、视频用 VideoView、
     *  语音用 MediaPlayer，它们都只认 URL，塞不进自定义请求头。这个接口本来
     *  也不需要令牌 —— 它就是手机页面里 <img>/<video> 取图的那条路。
     */
    fun mediaUrl(messageId: String): String =
        base.trimEnd('/') + "/api/chat/media/" + java.net.URLEncoder.encode(messageId, "UTF-8")

    /**
     * 把聊天附件读成字节（图片预览用；图片通常几百 KB）。
     *
     * 读超时特意设短（10 秒）：预览卡住时宁可显示"预览失败，可保存后看"，
     * 也不要让气泡永远停在"载入中…"—— 用户看到的是"图片打不开"。
     */
    fun mediaBytes(messageId: String, limit: Long = 24L * 1024 * 1024): ByteArray {
        val conn = open("/api/chat/media/" + java.net.URLEncoder.encode(messageId, "UTF-8"))
        try {
            conn.readTimeout = 10_000
            val declared = conn.contentLengthLong
            if (declared > limit) throw IllegalStateException("图片太大（${declared / 1024} KB），请用「保存到手机」")
            return conn.inputStream.use { it.readBytes() }
        } finally {
            conn.disconnect()
        }
    }

    /** 把聊天附件存到给定的输出流（保存到「下载」目录用）。 */
    fun saveMedia(messageId: String, sink: java.io.OutputStream): Long =
        download("/api/chat/media/" + java.net.URLEncoder.encode(messageId, "UTF-8"), sink)

    /**
     * 服务端拒绝这件事时给的那句话（404 的 `hint` / `error`）。
     *
     * web-ui 把 404 的 body 写成了"可以执行的信息"（比如"这条消息没有可下载的附件，
     * 让电脑重新发一次，或者在「传输 → 接收」里找它的分享条目"）—— 界面直接显示它，
     * 比 App 自己猜一句"保存失败"有用得多（宪章 E3：失败要带可执行的下一步）。
     */
    fun failureText(path: String): String {
        return try {
            val conn = open(path)
            try {
                val stream = if (conn.responseCode in 200..299) conn.inputStream else conn.errorStream
                val text = stream?.reader(Charsets.UTF_8)?.readText().orEmpty()
                val json = try {
                    JSONObject(text)
                } catch (ignored: Exception) {
                    JSONObject()
                }
                json.optString("hint").ifBlank { json.optString("error") }
            } finally {
                conn.disconnect()
            }
        } catch (problem: Exception) {
            ""
        }
    }

    /** 事件流：电脑端一有消息就推过来，App 不用轮询。 */
    fun events(onEvent: (JSONObject) -> Unit, isRunning: () -> Boolean) {
        val conn = open("/api/events")
        conn.setRequestProperty("Accept", "text/event-stream")
        // 电脑端每 SSE_HEARTBEAT=15 秒发一条 ": ping" 注释保活，所以 90 秒一行都
        // 没收到 = 这条 TCP 已经半死（切 Wi-Fi、路由器掉 NAT 表但没回 RST）。
        // 以前 readTimeout=0（永不超时）：readLine() 永远阻塞，LinkService 的重连
        // 和心跳根本轮不到执行 —— 界面一直显示"已连接"，事件永远不来。
        conn.readTimeout = 90_000
        try {
            val reader = BufferedReader(InputStreamReader(conn.inputStream, Charsets.UTF_8))
            val data = StringBuilder()
            var idleTimeouts = 0
            while (isRunning()) {
                val line = try {
                    val next = reader.readLine()
                    idleTimeouts = 0
                    next
                } catch (timeout: SocketTimeoutException) {
                    // 连续两次（180 秒）没心跳：认定连接已死，抛出去让上层重连。
                    if (++idleTimeouts >= 2) {
                        throw java.io.IOException("事件流 180 秒没有任何数据，重建连接")
                    }
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
                // 取令牌这一页**绝对不能走缓存**，否则拿到的是重启前那份 HTML 里的旧令牌。
                useCaches = false
                setRequestProperty("Cache-Control", "no-cache")
                setRequestProperty("Pragma", "no-cache")
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

        /** 这台手机的稳定设备号与名字（界面启动时填一次）。 */
        @Volatile
        var identity: String? = null

        @Volatile
        var name: String? = null

        fun remember(deviceId: String, deviceName: String) {
            identity = deviceId
            name = deviceName
        }

        /** 广播公告用的端口，和电脑端约定一致。 */
        const val DISCOVERY_PORT = 52118

        /** 电脑端用的组播地址；写错成别的组就永远收不到电脑的定时公告。 */
        const val MULTICAST_GROUP = "239.255.83.68"

        /** 电脑端网页界面的默认端口，TCP 兜底扫描用它。 */
        const val DEFAULT_WEB_PORT = 52119

        /**
         * 上一次 TCP 扫描实际探了多少台主机。
         *
         * 这个数字是给测试看的，也是给"搜索不到电脑"这类问题定位用的：
         * 一个 /24 网段有 254 台，扫到一半就被时间预算截断的话，电脑正好在
         * 尾巴上（比如 .177）就永远搜不到 —— 用户的机器就是这么漏掉的。
         */
        @Volatile
        var lastTcpScanProbed: Int = 0
            private set

        /** 上一次搜索各条路的结果，用来告诉用户"到底试过什么"。 */
        @Volatile
        var lastNsdFound: Int = -1
            private set

        @Volatile
        var lastProbeTargets: Int = 0
            private set

        /** HTTPS 那份页面的默认端口（手机录音要用安全上下文）。 */
        const val DEFAULT_TLS_PORT = 52120

        /** mDNS 服务类型，和电脑端 constants.py 里的 MDNS_SERVICE_TYPE 一致。 */
        const val MDNS_SERVICE_TYPE = "_eversend._tcp"

        /**
         * 在局域网里找电脑：**先把整个子网戳一遍，同时听回包**。
         *
         * 第一版只往 255.255.255.255 发一条探针，在真手机上找不到电脑。原因
         * 不在电脑端，而在手机的路由表：手机是热点（AP）时它的默认网络是移动
         * 数据，发往 255.255.255.255 的包按路由表会走**蜂窝**那一侧，或者被
         * ROM 直接丢掉，根本到不了热点子网。组播同理：没指定出口网卡时也不
         * 知道从哪出去。
         *
         * 所以现在按网卡逐个来：
         *
         * 1. 枚举所有 IPv4 网卡，往各自的**定向广播地址**（10.229.70.255 这种）
         *    各发一条——它只会从该网卡出去，路由表没有歧义；
         * 2. 再往该子网里**每一个主机地址**各发一条单播探针（最多 254 个，都是
         *    几十字节的小包）。广播被 ROM 拦掉时，这一条仍然能把电脑叫醒；
         * 3. 255.255.255.255 和组播照旧发一份，作为兜底。
         *
         * 电脑端收到探针会**立刻单播回一条**公告，回包发到本 socket 的源端口，
         * 所以绑定 52118 只是"顺便听听定时广播"，绑不上也不影响发现。
         */
        fun discover(
            timeoutMs: Int = 5000,
            deviceId: String = "probe",
            port: Int = DISCOVERY_PORT,
            webPort: Int = DEFAULT_WEB_PORT,
            context: android.content.Context? = null,
            hint: String = "",
        ): List<Found> {
            // 路数一：安卓自带的 mDNS。电脑端收到查询会立刻回一条，最快也最省，
            // 而且不需要广播权限。找不到再往下走。
            if (context != null) {
                val byMdns = discoverByNsd(context, timeoutMs = 3500)
                lastNsdFound = byMdns.size
                if (byMdns.isNotEmpty()) {
                    Log.d(TAG, "搜索结束（mDNS）：" + byMdns.joinToString { it.name + "@" + it.base })
                    return byMdns
                }
            }
            val found = LinkedHashMap<String, Found>()
            // 绑端口失败必须换一个**新的** socket 再绑：Java 的
            // DatagramSocket.bind() 一旦抛异常，这个 socket 就已经被关掉了，
            // 在它身上再 bind 只会得到 "Socket closed"（真机测试抓到的）。
            var socket = DatagramSocket(null)
            var bound = false
            try {
                socket.reuseAddress = true
                socket.bind(InetSocketAddress(port))
                bound = true
            } catch (ignored: Exception) {
                try {
                    socket.close()
                } catch (ignoredToo: Exception) {
                }
                // 端口被别的进程占着也没关系：回包是发到来源端口的，
                // 临时端口一样收得到。
                socket = DatagramSocket(null)
                socket.bind(InetSocketAddress(0))
            }
            socket.broadcast = true
            socket.soTimeout = 400
            val deadline = System.currentTimeMillis() + timeoutMs
            try {
                val probe = """{"t":"eversend/1","id":"$deviceId","n":"安卓 App","k":"mobile",""" +
                    """"p":"android","v":"1.0.0","port":0,"web":0,"ts":0}"""
                val payload = probe.toByteArray(Charsets.UTF_8)
                val targets = probeTargets()
                lastProbeTargets = targets.size
                Log.d(TAG, "搜索电脑：向 ${targets.size} 个地址发探针")
                var lastProbe = 0L
                while (System.currentTimeMillis() < deadline) {
                    if (System.currentTimeMillis() - lastProbe > 1000) {
                        lastProbe = System.currentTimeMillis()
                        for (target in targets) {
                            try {
                                socket.send(DatagramPacket(payload, payload.size, target, port))
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
                        // 自己发的探针会被本机回环收回来，跳过。
                        if (json.optString("n") == "安卓 App" && json.optInt("web", 0) <= 0) continue
                        val web = json.optInt("web", 0)
                        if (web <= 0) continue
                        val host = packet.address.hostAddress ?: continue
                        found["$host:$web"] = Found(
                            name = json.optString("n", host),
                            host = host,
                            webPort = web,
                            platform = json.optString("p", ""),
                            identityKey = json.optString("id", ""),
                            version = json.optString("v", ""),
                        )
                        if (found.size >= 8) break
                    } catch (ignored: SocketTimeoutException) {
                    }
                }
            } finally {
                socket.close()
            }
            if (!bound) Log.d(TAG, "端口被占用，只靠探针的回包发现")
            if (found.isEmpty()) {
                // UDP 一无所获时再敲一遍网页端口。真机上这一步很值：
                // 不少 ROM（vivo/小米的省电策略）会悄悄丢掉 UDP 广播，
                // 而"能打开网页"是用户心里"通了"的标准，TCP 走得通就一定能连。
                Log.d(TAG, "UDP 没找到电脑，改用 TCP 扫网页端口 $webPort")
                // TCP 扫描给足时间：/24 网段 254 台，每台 400ms 超时，32 个并发
                // 大约 3.5 秒扫完。以前固定 6 秒、并发 12，算下来只能扫到第 150
                // 台左右 —— 用户的电脑在 .177，正好在扫不到的尾巴上。
                // 端口不写死：默认端口扫不到，就把"上次连过的那个端口"也扫一遍
                // （电脑端换了端口时，这是唯一还能自己找回来的路）。
                val ports = LinkedHashSet<Int>()
                ports.add(webPort)
                hint.substringAfterLast(":").toIntOrNull()?.let { ports.add(it) }
                val hintHosts = if (hint.isBlank()) emptyList() else listOf(hint)
                for (attempt in 1..2) {
                    for (candidatePort in ports) {
                        val foundHere = discoverByTcp(
                            candidatePort,
                            timeoutMs = 8000,
                            threads = 32,
                            hints = hintHosts,
                        )
                        for (candidate in foundHere) {
                            found[candidate.host + ":" + candidate.webPort] = candidate
                        }
                        if (found.isNotEmpty()) break
                    }
                    if (found.isNotEmpty()) break
                    Log.d(TAG, "第 $attempt 次 TCP 扫描没有结果，再扫一遍")
                }
            }
            Log.d(TAG, "搜索结束：找到 ${found.size} 台电脑 " + found.values.joinToString { it.name + "@" + it.base })
            return found.values.toList()
        }

        /**
         * TCP 兜底：逐台敲网页端口，看谁答"我是韧传"。
         *
         * 为什么需要它：UDP 广播在很多真机上不可靠（省电策略、路由器隔离、
         * 默认路由跑到蜂窝数据），而网页端口是 TCP，只要能打开网页就一定能连上。
         * 254 个地址、32 个并发、每个 300ms 超时，最坏两秒左右。
         */
        fun discoverByTcp(
            webPort: Int = DEFAULT_WEB_PORT,
            timeoutMs: Int = 5000,
            threads: Int = 12,
            hints: List<String> = emptyList(),
        ): List<Found> {
            val found = java.util.Collections.synchronizedMap(LinkedHashMap<String, Found>())
            // 大网段（/16 的办公室网、校园网）不能逐台扫 —— 几万个地址扫不完也
            // 不礼貌。退一步扫"我们自己所在的 /24"，以及上次连过的电脑所在的
            // /24（跨网段的情况靠 mDNS 与 UDP 公告，那两条路都带着端口）。
            val chunks = mutableListOf<Pair<ByteArray, Int>>()
            for (subnet in subnets()) {
                val base = subnet.address.address
                val prefix = if (subnet.prefix >= 24) subnet.prefix else 24
                chunks.add(base to prefix)
            }
            for (hint in hints) {
                val host = hint.substringBefore(":")
                val parts = host.split(".")
                if (parts.size == 4 && parts.all { it.toIntOrNull() != null }) {
                    chunks.add(
                        byteArrayOf(
                            parts[0].toInt().toByte(),
                            parts[1].toInt().toByte(),
                            parts[2].toInt().toByte(),
                            0,
                        ) to 24,
                    )
                }
            }
            val candidates = chunks.flatMap { (base, _) ->
                (1..254).mapNotNull { last ->
                    try {
                        InetAddress.getByAddress(byteArrayOf(base[0], base[1], base[2], last.toByte()))
                    } catch (ignored: Exception) {
                        null
                    }
                }
            }.distinctBy { it.hostAddress }
            // 扫了哪些网段必须留痕：真机上"扫了 508 台命中 0 台"时，
            // 第一件要知道的事就是"这 508 台里有没有电脑所在的那个网段"。
            Log.d(
                TAG,
                "TCP 扫描：${chunks.size} 个网段 " +
                    chunks.joinToString(",") { (base, prefix) ->
                        "${base[0].toInt() and 0xFF}.${base[1].toInt() and 0xFF}.${base[2].toInt() and 0xFF}.0/$prefix"
                    } + "，共 ${candidates.size} 个地址，端口 $webPort",
            )
            if (candidates.isEmpty()) return emptyList()

            // 先按"最可能是电脑"的顺序串行试几个：上次连过的那台（hints）、
            // 网关常见的 .1/.2/.254。真实网络里电脑通常就在这几个里，串行几条
            // 连接既快又不会引起丢包 —— 一次铺开几百条连接，普通路由器（和
            // 模拟器的 NAT）会开始丢，反而连活着的那台都连不上。
            val preferred = LinkedHashSet<String>()
            preferred.addAll(hints.filter { it.isNotBlank() }.map { it.substringBefore(":") })
            val prefixes = candidates.mapNotNull { it.hostAddress?.substringBeforeLast(".") }.distinct()
            prefixes.forEach { prefix ->
                // 网关、常见的静态地址、DHCP 池的尾巴都排在前面。
                preferred.add("$prefix.1")
                preferred.add("$prefix.2")
                preferred.add("$prefix.254")
                for (last in 150..253) preferred.add("$prefix.$last")
                for (last in 100..149) preferred.add("$prefix.$last")
            }
            val addresses = candidates.mapNotNull { it.hostAddress }.toSet()
            val probed = java.util.concurrent.atomic.AtomicInteger(0)
            // 串行先探过的地址记下来：下面的并发池必须跳过它们，否则同一台会被探两遍，
            // 日志就会出现"探了 266/254 台"这种自相矛盾的数字（真机日志实测到过）。
            val earlyProbed = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()
            for (host in preferred.take(12)) {
                if (host !in addresses) continue
                earlyProbed.add(host)
                probed.incrementAndGet()
                val match = probeWeb(host, webPort, connectTimeoutMs = 500)
                if (match != null) {
                    found["$host:$webPort"] = match
                    // 早退也要记账：测试用这个数字确认"整个网段都扫过了"，
                    // 漏记就会得出"探了 0 台"这种自相矛盾的结论。
                    lastTcpScanProbed = probed.get()
                    Log.d(TAG, "TCP 优先探测命中：$host:$webPort（已探 ${probed.get()} 台）")
                    return found.values.toList()
                }
            }
            Log.d(TAG, "TCP 扫描：${candidates.size} 个地址，端口 $webPort")

            // 并发不能太高：这台模拟器的 NAT（以及不少家用路由器）在几十个
            // 同时发起、又大多连不上的连接面前会开始丢包，结果连"本来活着"的
            // 那台电脑都超时了（真机测试里第一次扫描就是这样，隔两秒再扫就
            // 找到了）。12 个并发足够快，也不至于把链路压垮。
            val pool = java.util.concurrent.Executors.newFixedThreadPool(threads)
            try {
                for (address in candidates) {
                    pool.execute {
                        if (found.size >= 4) return@execute
                        val host = address.hostAddress ?: return@execute
                        // 优先名单里串行探过的跳过：别让同一台主机被探两遍（266/254 的根因）。
                        if (host in earlyProbed) return@execute
                        probed.incrementAndGet()
                        val match = probeWeb(host, webPort)
                        if (match != null) found["$host:$webPort"] = match
                    }
                }
                pool.shutdown()
                pool.awaitTermination(timeoutMs.toLong(), java.util.concurrent.TimeUnit.MILLISECONDS)
            } catch (ignored: Exception) {
            } finally {
                pool.shutdownNow()
            }
            lastTcpScanProbed = probed.get()
            Log.d(TAG, "TCP 扫描结束：探了 ${probed.get()}/${candidates.size} 台，找到 ${found.size} 台（端口 $webPort）")
            return found.values.toList()
        }

        /** 问一台主机"你是不是韧传"，是就返回它的名字。 */
        private fun probeWeb(host: String, webPort: Int, connectTimeoutMs: Int = 400): Found? {
            var socket: java.net.Socket? = null
            try {
                socket = java.net.Socket()
                socket.connect(InetSocketAddress(host, webPort), connectTimeoutMs)
                socket.soTimeout = 900
                // Connection: close 让服务端答完就关；body 仍按 Content-Length 读，
                // 不靠"读到 EOF"——靠 EOF 会在 keep-alive 上一直等到超时，然后
                // 连已经收到的内容一起丢掉（第一次跑就是这样，52119 明明活着
                // 却被判成"没有电脑"）。
                val request = "GET /api/state HTTP/1.1\r\nHost: $host:$webPort\r\n" +
                    "Accept: application/json\r\nConnection: close\r\n" +
                    "User-Agent: ${userAgent()}\r\n\r\n"
                socket.getOutputStream().write(request.toByteArray(Charsets.UTF_8))
                socket.getOutputStream().flush()

                val input = socket.getInputStream()
                val head = StringBuilder()
                while (!head.endsWith("\r\n\r\n") && head.length < 8192) {
                    val next = input.read()
                    if (next < 0) break
                    head.append(next.toChar())
                }
                if (!head.startsWith("HTTP/") || !head.contains(" 200")) return null
                val length = Regex("(?i)content-length:\\s*(\\d+)")
                    .find(head)?.groupValues?.get(1)?.toIntOrNull() ?: 0
                if (length <= 0 || length > (1 shl 20)) return null
                val body = ByteArray(length)
                var filled = 0
                while (filled < length) {
                    val read = input.read(body, filled, length - filled)
                    if (read <= 0) break
                    filled += read
                }
                val text = String(body, 0, filled, Charsets.UTF_8)
                if (!text.contains("nameCn")) return null
                val payload = JSONObject(text)
                val device = payload.optJSONObject("device") ?: return null
                val name = device.optString("name").ifBlank { host }
                return Found(
                    name = name,
                    host = host,
                    webPort = webPort,
                    platform = device.optString("platform", ""),
                    // 稳定身份（服务端的 identityKey）：主动扫描和组播/广播听到的是
                    // 同一台电脑时，界面才能把它合成一行（宪章 C4）。
                    identityKey = device.optString("id", ""),
                    version = device.optString("version", ""),
                )
            } catch (ignored: Exception) {
                return null
            } finally {
                try {
                    socket?.close()
                } catch (ignored: Exception) {
                }
            }
        }

        /**
         * 用安卓自带的 mDNS（NsdManager）找电脑。
         *
         * 这是最"正规"的一条路：电脑端注册了 `_eversend._tcp`，收到查询会
         * **立刻**回一条（PTR/SRV/TXT/A 四段齐全），NsdManager 解析出来就是
         * 主机地址和网页端口。不需要广播、不需要扫网段，几百毫秒就出结果 ——
         * 前提是网络没有禁掉 5353/UDP 组播（不少公司网会禁，所以它只是三条
         * 路里的一条，不是唯一）。
         */
        fun discoverByNsd(context: android.content.Context, timeoutMs: Int = 4000): List<Found> {
            val found = java.util.Collections.synchronizedList(mutableListOf<Found>())
            val manager = context.getSystemService(android.net.nsd.NsdManager::class.java)
                ?: return emptyList()
            val done = java.util.concurrent.CountDownLatch(1)
            val listener = object : android.net.nsd.NsdManager.DiscoveryListener {
                override fun onStartDiscoveryFailed(serviceType: String?, errorCode: Int) {
                    Log.d(TAG, "mDNS 启动失败：$errorCode")
                    done.countDown()
                }

                override fun onStopDiscoveryFailed(serviceType: String?, errorCode: Int) {
                    done.countDown()
                }

                override fun onDiscoveryStarted(serviceType: String?) {}

                override fun onDiscoveryStopped(serviceType: String?) {
                    done.countDown()
                }

                override fun onServiceLost(service: android.net.nsd.NsdServiceInfo?) {}

                override fun onServiceFound(service: android.net.nsd.NsdServiceInfo) {
                    try {
                        manager.resolveService(
                            service,
                            object : android.net.nsd.NsdManager.ResolveListener {
                                override fun onResolveFailed(
                                    info: android.net.nsd.NsdServiceInfo?,
                                    errorCode: Int,
                                ) {
                                    Log.d(TAG, "mDNS 解析失败：$errorCode")
                                }

                                override fun onServiceResolved(info: android.net.nsd.NsdServiceInfo) {
                                    // 主机地址要 API 34（安卓 14）才有；更早的系统
                                    // 上取不到就交给别的发现方式，不在这里硬撑。
                                    val host = if (Build.VERSION.SDK_INT >= 34) {
                                        info.host?.hostAddress
                                    } else {
                                        null
                                    } ?: return
                                    val attributes = info.attributes ?: emptyMap()
                                    val web = attributes["web"]
                                        ?.toString(Charsets.UTF_8)?.toIntOrNull()
                                        ?: DEFAULT_WEB_PORT
                                    val name = attributes["n"]?.toString(Charsets.UTF_8)
                                        ?.takeIf { it.isNotBlank() }
                                        ?: info.serviceName
                                    val platform = attributes["p"]?.toString(Charsets.UTF_8).orEmpty()
                                    val identity = attributes["id"]?.toString(Charsets.UTF_8).orEmpty()
                                    val version = attributes["v"]?.toString(Charsets.UTF_8).orEmpty()
                                    found.add(
                                        Found(
                                            name = name,
                                            host = host,
                                            webPort = web,
                                            platform = platform,
                                            identityKey = identity,
                                            version = version,
                                        )
                                    )
                                    Log.d(TAG, "mDNS 找到电脑：$name@http://$host:$web/")
                                }
                            },
                        )
                    } catch (ignored: Exception) {
                    }
                }
            }
            try {
                manager.discoverServices(
                    MDNS_SERVICE_TYPE,
                    android.net.nsd.NsdManager.PROTOCOL_DNS_SD,
                    listener,
                )
                done.await(timeoutMs.toLong(), java.util.concurrent.TimeUnit.MILLISECONDS)
            } catch (problem: Exception) {
                Log.d(TAG, "mDNS 发现不可用：${problem.message}")
            } finally {
                try {
                    manager.stopServiceDiscovery(listener)
                } catch (ignored: Exception) {
                }
            }
            // resolveService 是异步回调，停掉发现之后再给它一点时间落地。
            Thread.sleep(400)
            return found.toList()
        }

        /** 一个网卡的 IPv4 信息。 */
        private data class Subnet(val address: InetAddress, val broadcast: InetAddress?, val prefix: Int)

        /**
         * 读出所有可用网卡的 IPv4 地址与掩码；失败就当作没有。
         *
         * **枚举结果要和系统当前网络求并集**：真机（vivo V2408A / Android 16，双 Wi-Fi）
         * 上光靠 `getNetworkInterfaces()` 会漏掉真正在用的那块网卡，于是扫描只覆盖
         * 了别的网段，电脑所在的 172.20.90.0/24 一个地址都没探 —— 真机日志就是
         * "探了 508 台，命中 0 台"，用户看到的是"手机找不到电脑，只能手输 IP"。
         */
        private fun subnets(): List<Subnet> {
            val result = mutableListOf<Subnet>()
            val seen = HashSet<String>()
            fun addOnce(address: InetAddress, broadcast: InetAddress?, prefix: Int) {
                val key = address.hostAddress ?: return
                if (!seen.add(key)) return
                result.add(Subnet(address, broadcast, prefix))
            }
            try {
                val interfaces = NetworkInterface.getNetworkInterfaces() ?: return result
                for (nic in Collections.list(interfaces)) {
                    if (!nic.isUp || nic.isLoopback) continue
                    for (info in nic.interfaceAddresses) {
                        val address = info.address
                        if (address !is Inet4Address) continue
                        if (address.isLoopbackAddress || address.isLinkLocalAddress) continue
                        addOnce(address, info.broadcast, info.networkPrefixLength.toInt())
                    }
                }
            } catch (ignored: Exception) {
            }
            // 系统当前网络优先（路由表真正在用的那些），并集兜底。
            for (subnet in subnetsOfActiveNetwork()) addOnce(subnet.address, subnet.broadcast, subnet.prefix)
            return result
        }

        /** 系统当前网络（`ConnectivityManager`）的 IPv4 网段。 */
        private fun subnetsOfActiveNetwork(): List<Subnet> {
            val out = mutableListOf<Subnet>()
            try {
                val context = EverSendApp.instance
                val cm = context.getSystemService(android.content.Context.CONNECTIVITY_SERVICE)
                    as? android.net.ConnectivityManager ?: return out
                for (network in cm.allNetworks) {
                    val props = cm.getLinkProperties(network) ?: continue
                    for (link in props.linkAddresses) {
                        val address = link.address
                        if (address !is Inet4Address) continue
                        if (address.isLoopbackAddress || address.isLinkLocalAddress) continue
                        out.add(Subnet(address, null, link.prefixLength))
                    }
                }
            } catch (ignored: Exception) {
            }
            return out
        }

        /**
         * 探针要发去的地址：定向广播 + 子网内每一台主机 + 全局广播 + 组播。
         *
         * 子网扫描限制在 /24（最多 254 个主机地址）以内：更大的网段逐台扫没有
         * 意义（几千个包换几秒延迟），而家里的热点、路由器都是 /24。
         */
        fun probeTargets(): List<InetAddress> {
            val targets = LinkedHashSet<InetAddress>()
            for (subnet in subnets()) {
                subnet.broadcast?.let { targets.add(it) }
                // /24 及更小的网段全扫；更大的（/16 的办公室网、网卡的 /8）
                // 也扫"自己所在的那个 /24" —— 否则大网段上一个目标地址都没有，
                // 探针只剩下广播，遇到拦广播的网络就彻底找不到了。
                run {
                    val base = subnet.address.address
                    // 本机地址也在扫描范围里：这一遍本来就是"子网内每一台"，
                    // 而且它让"同一台机器上的电脑端"（测试桩、模拟器里的宿主机）
                    // 也能被发现 —— 少了它，探针就只往外发，本机收不到。
                    for (last in 1..254) {
                        val candidate = byteArrayOf(base[0], base[1], base[2], last.toByte())
                        try {
                            targets.add(InetAddress.getByAddress(candidate))
                        } catch (ignored: Exception) {
                        }
                    }
                }
            }
            try {
                targets.add(InetAddress.getByName("255.255.255.255"))
            } catch (ignored: Exception) {
            }
            try {
                targets.add(InetAddress.getByName(MULTICAST_GROUP))
            } catch (ignored: Exception) {
            }
            return targets.toList()
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
    data class Found(
        val name: String,
        val host: String,
        val webPort: Int,
        val platform: String,
        /** 服务端的稳定身份（`/api/state` 的 `device.id` / mDNS TXT 的 `id`）。 */
        val identityKey: String = "",
        val version: String = "",
    ) {
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
    /**
     * 电脑为这条文件消息发布的分享条目 id（服务端 `/api/chat` 的 `shareId`）。
     *
     * 手机点「保存」时要走 FetchManager 下载队列（进度/速度/ETA/已保存态），
     * 而队列按分享 id 记账；消息没带这个字段时只能退回旧的直连下载旁路，
     * 于是气泡永远显示「接收中… / 点击保存到手机」（真机 PG-1/PG-2 根因）。
     */
    val mediaShareId: String = "",
)

data class Conversation(
    val id: String,
    val title: String,
    /// 服务端算好的显示名（对方设备名 / 群名），列表和标题栏都优先用它。
    val displayTitle: String,
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
    mediaShareId = optString("shareId"),
)

fun JSONObject.toConversation(): Conversation = Conversation(
    id = optString("id"),
    title = optString("title"),
    displayTitle = optString("displayTitle"),
    kind = optString("kind", "direct"),
    lastText = optString("lastText"),
    unread = optInt("unread", 0),
    members = optJSONArray("members")?.let { array: JSONArray ->
        (0 until array.length()).map { array.optString(it) }
    } ?: emptyList(),
)

fun encode(value: String): String = URLEncoder.encode(value, "UTF-8")
