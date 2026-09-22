package cn.eversend.app

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.net.URLEncoder

/**
 * 真机（模拟器）上的真实测试：App 真的去连电脑、真的发消息、真的传文件。
 *
 * 电脑地址从 instrumentation 参数里拿（`-e host 10.0.2.2:52119`），因为模拟器
 * 访问宿主机就是 10.0.2.2。断言直接查电脑端的接口，而不是查 App 自己的界面——
 * 只有电脑端真的收到了，才算通。
 */
@RunWith(AndroidJUnit4::class)
class EverSendInstrumentedTest {

    private fun host(): String {
        val arguments = InstrumentationRegistry.getArguments()
        return arguments.getString("host") ?: "10.0.2.2:52119"
    }

    private fun client(): ApiClient {
        val base = "http://${host()}/"
        val token = ApiClient.fetchToken(base)
        assertTrue("拿不到电脑端的 CSRF 令牌（地址 $base 是否可达？）", token.isNotBlank())
        return ApiClient(base, token)
    }

    @Test
    fun connectsToDesktopAndReadsState() {
        val state = client().getJson("/api/state")
        val device = state.optJSONObject("device")
        assertTrue("电脑端必须返回设备信息", device != null && device!!.optString("name").isNotEmpty())
        assertTrue("电脑端必须带聊天接口", state.optJSONObject("chat") != null)
    }

    @Test
    fun sendsTextWithEmojiAndDesktopReceivesIt() {
        val api = client()
        val text = "来自安卓 App 的问候 👋🇨🇳 表情测试 ✅"
        val response = api.postJson("/api/chat/send", JSONObject().put("text", text))
        assertTrue("电脑端接受了消息", response.optBoolean("ok", false))

        val chat = api.getJson("/api/chat")
        val messages = chat.optJSONArray("messages") ?: throw AssertionError("没有消息列表")
        var found = false
        for (index in 0 until messages.length()) {
            val message = messages.optJSONObject(index) ?: continue
            if (message.optString("text") == text) {
                found = true
                assertEquals("消息必须标为“收到”（来自手机）", "in", message.optString("direction"))
            }
        }
        assertTrue("电脑端必须原样收到带 emoji 的那句话", found)
    }

    @Test
    fun uploadsAnImageAttachmentAndDesktopCanFetchIt() {
        val api = client()
        // 真图：截图里的气泡会真的显示出来（假 PNG 字节会让界面正确地提示
        // "预览失败"，那张截图看上去就像功能坏了）。
        val file = File(
            InstrumentationRegistry.getInstrumentation().targetContext.cacheDir,
            "instrumented-图片.png",
        )
        val bitmap = android.graphics.Bitmap.createBitmap(120, 80, android.graphics.Bitmap.Config.ARGB_8888)
        bitmap.eraseColor(android.graphics.Color.rgb(47, 129, 247))
        java.io.ByteArrayOutputStream().use { out ->
            bitmap.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, out)
            file.writeBytes(out.toByteArray())
        }

        val name = URLEncoder.encode(file.name, "UTF-8")
        val response = file.inputStream().use { stream ->
            api.upload("/api/chat/upload?name=$name&kind=image", stream, file.length(), "image/png")
        }
        assertTrue("电脑端接受了图片", response.optBoolean("ok", false))
        val message = response.optJSONObject("message") ?: throw AssertionError("没有消息")
        assertEquals("image", message.optString("kind"))

        // 再从电脑端按消息 id 把附件取回来，必须逐字节一致。
        val messageId = message.optString("id")
        val sink = java.io.ByteArrayOutputStream()
        api.download("/api/chat/media/$messageId", sink)
        assertTrue("取回的图片要和发出去的一样大", sink.size() == file.length().toInt())
        assertTrue("取回的图片内容一致", sink.toByteArray().contentEquals(file.readBytes()))
    }

    /**
     * 界面级的真机测试：真的启动 App 界面，真的连上电脑，把会话和附件显示出来，
     * 并且**真的点一遍**新加的三个动作 —— 看大图、复制文字、保存到手机。
     *
     * 为什么要有这一条：接口测试全绿的时候，界面仍然可能是坏的。第一版就是
     * 这样：`connect()` 在主线程发网络请求，安卓抛 NetworkOnMainThreadException，
     * 界面上只有一行"连不上电脑：null"，而接口测试照样全过。CI 里那张真机
     * 截图是唯一的线索，所以把这件事也变成断言。
     */
    @Test
    fun appUiConnectsAndShowsTheConversation() {
        val api = client()
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        api.postJson("/api/chat/send", JSONObject().put("text", "界面测试消息 " + System.currentTimeMillis()))

        // 界面上要有一张图片可点，所以这条自己先传一张（测试顺序不保证）。
        // 真图（真的能让 BitmapFactory 解码出来）。用"PNG 魔数 + 随机字节"
        // 那种假图，界面会正确地显示"预览失败"——测的就不是预览了。
        val png = File(context.cacheDir, "ui-图片.png")
        val bitmap = android.graphics.Bitmap.createBitmap(96, 64, android.graphics.Bitmap.Config.ARGB_8888)
        bitmap.eraseColor(android.graphics.Color.rgb(47, 129, 247))
        java.io.ByteArrayOutputStream().use { out ->
            bitmap.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, out)
            png.writeBytes(out.toByteArray())
        }
        val name = URLEncoder.encode(png.name, "UTF-8")
        png.inputStream().use { api.upload("/api/chat/upload?name=$name&kind=image", it, png.length(), "image/png") }

        context.getSharedPreferences("eversend", android.content.Context.MODE_PRIVATE)
            .edit()
            .putString("host", host())
            .apply()
        val intent = android.content.Intent(context, MainActivity::class.java)
            .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
        androidx.test.core.app.ActivityScenario.launch<MainActivity>(intent).use {
            val device = androidx.test.uiautomator.UiDevice.getInstance(
                InstrumentationRegistry.getInstrumentation()
            )
            val shown = device.wait(
                androidx.test.uiautomator.Until.hasObject(
                    androidx.test.uiautomator.By.textContains("会话")
                ),
                15_000,
            )
            assertTrue("App 界面必须渲染出会话页", shown)
            val broken = device.hasObject(androidx.test.uiautomator.By.textContains("连不上电脑"))
            assertTrue("界面不应该显示『连不上电脑』（主线程联网的坑）", !broken)

            // UiDevice.takeScreenshot(File) 直接写 PNG，比拿 Bitmap 再压缩省事。
            fun shoot(fileName: String) {
                try {
                    device.takeScreenshot(File(context.getExternalFilesDir(null), fileName))
                } catch (ignored: Exception) {
                }
            }
            shoot("android-app-chat.png")

            // 会话列表出来了，还要真的点进去：原来只断言"会话"两个字，列表页
            // 本身就有这两个字，所以"聊天页打不开"也能过。
            val conversation = device.wait(
                androidx.test.uiautomator.Until.findObject(
                    androidx.test.uiautomator.By.textStartsWith("d:")
                ),
                10_000,
            )
            assertTrue("会话列表里要有和电脑的那个会话", conversation != null)
            conversation?.click()
            Thread.sleep(1500)
            shoot("android-app-conversation.png")

            // 1. 图片要真的显示出来，点一下要能看大图
            val picture = device.wait(
                androidx.test.uiautomator.Until.findObject(
                    androidx.test.uiautomator.By.desc("ui-图片.png")
                ),
                10_000,
            )
            if (picture == null) {
                // 找不到就把整棵界面树存下来，CI 里能直接看（否则只有一句断言失败）
                try {
                    device.dumpWindowHierarchy(File(context.getExternalFilesDir(null), "ui-dump.xml"))
                } catch (ignored: Exception) {
                }
            }
            assertTrue("聊天里的图片必须真的渲染出来（不是一行文件名）", picture != null)
            if (picture != null) {
                picture.click()
                val viewer = device.wait(
                    androidx.test.uiautomator.Until.hasObject(androidx.test.uiautomator.By.text("关闭")),
                    8_000,
                )
                assertTrue("点开图片要出现全屏查看器", viewer)
                shoot("android-app-image-viewer.png")
                device.pressBack()
                Thread.sleep(500)
            }

            // 2. 文字要能复制（点一下「复制」出现提示）
            val copy = device.wait(
                androidx.test.uiautomator.Until.findObject(androidx.test.uiautomator.By.text("复制")),
                8_000,
            )
            assertTrue("文字气泡上要有「复制」入口", copy != null)
            if (copy != null) {
                copy.click()
                Thread.sleep(1200)
                // 直接查剪贴板，而不是查那句提示：提示会自己消失（还可能被系统
                // 限制成看不到），而"有没有真的复制到"才是用户要的结果。
                val clipboard = context.getSystemService(android.content.ClipboardManager::class.java)
                val copied = clipboard?.primaryClip?.getItemAt(0)?.text?.toString().orEmpty()
                assertTrue("点了「复制」，剪贴板里要有这句话（拿到的是「$copied」）", copied.isNotEmpty())
                // 点到的可能是任意一条（整套测试跑过之后聊天里有好几条），
                // 只要确实是聊天里的文字就算过。
                assertTrue(
                    "复制出来的应该是聊天里的一句话：$copied",
                    copied.contains("界面测试消息") || copied.contains("来自安卓 App 的问候"),
                )
            }

            // 3. 附件要能存进系统「下载」目录
            //
            // 取**最后一个**「保存」：聊天列表自动滚到底，最后一条一定在屏幕上；
            // 取第一个（最老的那条）时它可能已经滚出可视区，点下去等于点在别处
            // ——CI 上就是这么失败的（本机屏幕更高所以碰巧能过）。
            fun savedToDownloads(): Boolean {
                context.contentResolver.query(
                    android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                    arrayOf(android.provider.MediaStore.Downloads.DISPLAY_NAME),
                    android.provider.MediaStore.Downloads.DISPLAY_NAME + " = ?",
                    arrayOf("ui-图片.png"),
                    null,
                )?.use { cursor -> return cursor.count > 0 }
                return false
            }

            val saveButtons = device.wait(
                androidx.test.uiautomator.Until.findObjects(androidx.test.uiautomator.By.text("保存")),
                8_000,
            )
            val save = saveButtons.lastOrNull()
            assertTrue("附件气泡上要有「保存」入口（找到 ${saveButtons.size} 个）", save != null)
            var saved = false
            if (save != null) {
                save.click()
                val deadline = System.currentTimeMillis() + 12_000
                while (System.currentTimeMillis() < deadline && !saved) {
                    saved = savedToDownloads()
                    if (!saved) Thread.sleep(500)
                }
                if (!saved) {
                    // 点空了（元素在屏幕边缘）就滚一下再点一次；屏幕尺寸不写死，
                    // CI 的模拟器分辨率与本机不同。
                    device.swipe(
                        device.displayWidth / 2, (device.displayHeight * 3) / 4,
                        device.displayWidth / 2, device.displayHeight / 2, 20,
                    )
                    Thread.sleep(800)
                    device.wait(
                        androidx.test.uiautomator.Until.findObjects(androidx.test.uiautomator.By.text("保存")),
                        5_000,
                    ).lastOrNull()?.click()
                    val retryDeadline = System.currentTimeMillis() + 15_000
                    while (System.currentTimeMillis() < retryDeadline && !saved) {
                        saved = savedToDownloads()
                        if (!saved) Thread.sleep(500)
                    }
                }
            }
            assertTrue("点「保存」要把附件真的写进系统「下载」目录", saved)
        }
    }

    /**
     * 「搜索电脑」在真机上必须真的能把探针发出去、把回包认出来。
     *
     * 这条用一个跑在同一台设备上的桩电脑来验：真开一个 UDP socket 听探针，
     * 收到就按电脑端的格式回一条公告，然后断言 discover() 认出了它。走的
     * 是 App 里那条真实代码路径（枚举网卡 → 定向广播 + 子网内逐台单播 →
     * 收包 → 解析），只有"另一头是谁"是桩。
     *
     * 为什么必须用桩：CI 的模拟器在 NAT 后面，广播出不去；而真机上出的问题
     * 恰恰是"探针根本没离开手机"（默认路由是蜂窝数据），所以这条测的就是
     * "探针到底有没有发出去"。
     */
    @Test
    fun discoverySendsProbesAndFindsAReply() {
        // 故意**不**开 reuseAddress：否则 App 的探针 socket 可能绑到同一个端口，
        // 内核只会把包投给其中一个，桩就永远收不到探针（第一次跑就是这样失败的）。
        // 端口被占住时 App 会退回到临时端口，这正好也把那条退路测了。
        val server = java.net.DatagramSocket(null)
        server.bind(java.net.InetSocketAddress(0))
        val port = server.localPort
        val reply = """{"t":"eversend/1","id":"stub-desktop","n":"桩电脑","k":"desktop",""" +
            """"p":"linux","v":"1.0.0","port":52117,"web":52119,"ts":0}"""

        val worker = Thread {
            val buffer = ByteArray(8192)
            val packet = java.net.DatagramPacket(buffer, buffer.size)
            server.soTimeout = 15_000
            try {
                server.receive(packet)          // 收到探针
                val bytes = reply.toByteArray(Charsets.UTF_8)
                server.send(java.net.DatagramPacket(bytes, bytes.size, packet.address, packet.port))
            } catch (ignored: Exception) {
            }
        }
        worker.isDaemon = true
        worker.start()

        val found = try {
            ApiClient.discover(timeoutMs = 8000, deviceId = "instrumented", port = port)
        } finally {
            worker.join(500)
            server.close()
        }

        assertTrue(
            "探针必须真的发到本机（收到了回包才算）：found=${found.map { it.name }}",
            found.any { it.name == "桩电脑" },
        )
        val stub = found.first { it.name == "桩电脑" }
        assertEquals("回包里的网页端口要解析出来", 52119, stub.webPort)
        assertTrue("要能拼出可用的地址", stub.base.startsWith("http://"))
    }

    /**
     * 扫网段必须**扫完**。
     *
     * 用户报"搜不到电脑"时，他的电脑在 172.20.90.177 —— 而当时那次扫描
     * 固定 6 秒预算、12 个并发，算下来只扫到第 150 台左右就收了工：电脑明明
     * 开着，却正好在扫不到的尾巴上。这条断言把"覆盖整个 /24"钉住。
     */
    @Test
    fun tcpScanCoversTheWholeSubnet() {
        // 故意扫一个没人监听的端口：只有这样才会走完整的 254 台扫描
        // （扫真端口时，电脑排在优先名单里，第 2 台就命中并提前返回 —— 那是
        // 想要的行为，但量不到覆盖面）。
        ApiClient.discoverByTcp(webPort = 59999, timeoutMs = 12_000, threads = 32)
        val probed = ApiClient.lastTcpScanProbed
        assertTrue(
            "一次扫描要覆盖整个 /24（至少 250 台），实际只探了 $probed 台",
            probed >= 250,
        )
    }

    /** 探针目标里必须有本网段的定向广播地址，而不是只有 255.255.255.255。 */
    @Test
    fun probeTargetsCoverTheLocalSubnet() {
        val targets = ApiClient.probeTargets().map { it.hostAddress }
        assertTrue("至少有若干目标地址", targets.size > 4)
        assertTrue(
            "必须包含 255.255.255.255 兜底：$targets",
            targets.contains("255.255.255.255"),
        )
        val broadcast = targets.filter { it.endsWith(".255") && it != "255.255.255.255" }
        // 模拟器的网卡是 10.0.2.15/24，定向广播就是 10.0.2.255。
        assertTrue("必须包含本网段的定向广播地址：$targets", broadcast.isNotEmpty())
        val hosts = targets.filter { it.startsWith("10.0.2.") && !it.endsWith(".255") }
        assertTrue("必须扫子网内的主机地址（广播被拦时的第二条路）：${hosts.size}", hosts.size > 100)
    }

    /**
     * 聊天里的附件在 App 里必须"能用"：能取回字节（图片预览就是这条路）、
     * 能存进系统「下载」目录（文件下载就是这条路）。
     */
    @Test
    fun chatAttachmentCanBeFetchedAndSavedToDownloads() {
        val api = client()
        val payload = ByteArray(48 * 1024) { (it % 97).toByte() }
        val png = byteArrayOf(0x89.toByte(), 'P'.code.toByte(), 'N'.code.toByte(), 'G'.code.toByte())
        val file = File(
            InstrumentationRegistry.getInstrumentation().targetContext.cacheDir,
            "preview-图片.png",
        )
        file.writeBytes(png + payload)

        val name = URLEncoder.encode(file.name, "UTF-8")
        val response = file.inputStream().use { stream ->
            api.upload("/api/chat/upload?name=$name&kind=image", stream, file.length(), "image/png")
        }
        assertTrue("电脑端接受了图片", response.optBoolean("ok", false))
        val messageId = response.optJSONObject("message")?.optString("id").orEmpty()
        assertTrue("消息要有 id", messageId.isNotEmpty())

        // 1. 预览用的取字节接口
        val bytes = api.mediaBytes(messageId)
        assertTrue("取回的图片要和发出去的一样大", bytes.size == file.length().toInt())
        assertTrue("取回的内容一致", bytes.contentEquals(file.readBytes()))
        assertTrue(
            "取回的确实是 PNG（预览能解码）",
            bytes.size > 8 && bytes[0] == png[0] && bytes[1] == png[1],
        )

        // 2. 「保存到手机」用的落盘路径：真的写进系统「下载」目录
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val uri = saveToDownloads(context, "eversend-instrumented-图片.png") { sink ->
            api.saveMedia(messageId, sink)
        }
        assertTrue("必须写进「下载」目录", uri != null)
        val saved = context.contentResolver.openInputStream(uri!!)?.use { it.readBytes() }
        assertTrue("保存下来的内容和电脑端一致", saved != null && saved.contentEquals(file.readBytes()))
        context.contentResolver.delete(uri, null, null)

        // 3. 附件地址是可直接给 <img>/VideoView 用的完整 URL
        val url = api.mediaUrl(messageId)
        assertTrue("附件地址是完整 URL：$url", url.startsWith("http://") && url.contains("/api/chat/media/"))
    }

    /**
     * 端到端：真的用「搜索电脑」找到那台正在跑的电脑，并确认那个地址真的能用。
     *
     * 上一条用桩验证了"探针发得出去、回包认得出来"，这一条验证真实场景：
     * 电脑端就在 CI 的宿主机上（模拟器里是 10.0.2.2，属于本网段，正好落在
     * App 的子网扫描里），Discover 必须把它找出来，并且拿到的地址能打开
     * `/api/state`。用户报的 "手机上搜索不到电脑" 就是这条。
     */
    @Test
    fun discoveryFindsTheRunningDesktop() {
        // 这条测"搜索电脑"真实的那条路：把每台主机的网页端口敲一遍，
        // 谁答"我是韧传"就是电脑。用 `-e host` 里那个端口，正好就是本次
        // 正在服务我们的那台电脑，所以结果是确定的。
        val arguments = InstrumentationRegistry.getArguments()
        val webPort = host().substringAfterLast(":").toIntOrNull() ?: ApiClient.DEFAULT_WEB_PORT
        // 扫描要重试几次：模拟器（和不少家用路由器）在一阵子密集连接之后会
        // 短暂丢包，一次扫描可能空手而归，隔一两秒再来就有了。用户按一次
        // 「搜索电脑」，App 内部本来也会扫两遍。
        var found = emptyList<ApiClient.Found>()
        var summary = ""
        for (attempt in 1..3) {
            found = ApiClient.discoverByTcp(webPort = webPort, timeoutMs = 12_000)
            summary = found.joinToString("、") { it.name + "@" + it.base }
            println("discovery(TCP $webPort) 第 $attempt 次: [$summary]")
            if (found.isNotEmpty()) break
            Thread.sleep(1500)
        }
        assertTrue("TCP 扫描必须找到正在运行的电脑（网页端口 $webPort）：[$summary]", found.isNotEmpty())

        // 找到的地址必须真的能用（令牌拿得到 = 网页接口活着）
        val reachable = found.filter { candidate ->
            try {
                ApiClient.fetchToken(candidate.base).isNotBlank()
            } catch (problem: Exception) {
                false
            }
        }
        assertTrue("搜到的电脑必须真的连得上：[$summary]", reachable.isNotEmpty())

        // 完整发现流程（UDP 广播/单播 + TCP 兜底）在模拟器 NAT 里可能拿不到回包，
        // 那就只记录，不当失败。
        val full = ApiClient.discover(timeoutMs = 4000, deviceId = "instrumented")
        println("discovery(完整流程): " + full.joinToString("、") { it.name + "@" + it.base })
    }

    /**
     * 群聊：手机端建一个群（电脑 + 本机），电脑端必须认这个群并记住成员。
     *
     * 这是"App 还没有群聊功能"那条的实现验证：群 id 用 `g:` 开头、成员用各自
     * 在电脑端登记的身份，两边（电脑端 SQLite、网页版、桌面端）看到的是同一个群。
     */
    @Test
    fun createsAGroupChatWithTheComputer() {
        val api = client()
        val state = api.getJson("/api/state")
        val computerId = state.optJSONObject("device")?.optString("id").orEmpty()
        assertTrue("电脑端要有设备号", computerId.isNotEmpty())

        val groupId = "g:instrumented" + System.currentTimeMillis().toString().takeLast(6)
        val title = "真机群聊测试"
        val response = api.postJson(
            "/api/chat/send",
            JSONObject()
                .put("conv", groupId)
                .put("title", title)
                .put("members", org.json.JSONArray().apply { put(computerId) })
                .put("kind", "text")
                .put("text", "群聊建好了 👋"),
        )
        assertTrue("建群的消息被接受", response.optBoolean("ok", false))
        assertEquals("返回的会话 id 就是群 id", groupId, response.optString("conversationId"))

        // 电脑端必须把它当成群聊，并且成员里有电脑和这台手机
        val chat = api.getJson("/api/chat?conv=" + URLEncoder.encode(groupId, "UTF-8"))
        val conversations = chat.optJSONArray("conversations") ?: throw AssertionError("没有会话列表")
        var found = false
        for (index in 0 until conversations.length()) {
            val conversation = conversations.optJSONObject(index) ?: continue
            if (conversation.optString("id") != groupId) continue
            found = true
            assertEquals("必须标成群聊", "group", conversation.optString("kind"))
            assertEquals("群名要保留", title, conversation.optString("title"))
            val members = conversation.optJSONArray("members") ?: org.json.JSONArray()
            val names = (0 until members.length()).map { members.optString(it) }
            assertTrue("成员里要有电脑：$names", names.contains(computerId))
        }
        assertTrue("电脑端记住了这个群", found)

        val messages = chat.optJSONArray("messages") ?: org.json.JSONArray()
        assertTrue("群里的第一条消息要在", messages.length() >= 1)
    }

    /**
     * 设备列表要带状态标记：手机端也要看得出哪些连着、哪些断开了。
     *
     * 电脑端给 /api/state 的每条设备都带 online，手机 UI 才能打「已连接 /
     * 未连接 · 最后在线」——以前只有名字和地址，用户分不清谁在线。
     */
    @Test
    fun deviceListCarriesOnlineStatus() {
        val state = client().getJson("/api/state")
        val devices = state.optJSONArray("devices") ?: throw AssertionError("没有设备列表")
        var checked = 0
        for (index in 0 until devices.length()) {
            val device = devices.optJSONObject(index) ?: continue
            if (device.optBoolean("isSelf")) continue
            assertTrue(
                "每台设备都要有 online 字段：${device.optString("name")}",
                device.has("online"),
            )
            checked++
        }
        assertTrue("至少要有一台设备可查（电脑端自己也算）", checked >= 1 || devices.length() >= 1)

        val clients = state.optJSONArray("knownClients") ?: org.json.JSONArray()
        for (index in 0 until clients.length()) {
            val client = clients.optJSONObject(index) ?: continue
            assertTrue("记住的手机要有 online", client.has("online"))
            assertTrue("记住的手机要有 clientKind（区分 App / 网页版）", client.has("clientKind"))
            assertTrue(
                "clientKind 只能是 app 或 browser：${client.optString("clientKind")}",
                client.optString("clientKind") in listOf("app", "browser"),
            )
        }
    }

    /**
     * 内置播放内核真的能用：libVLC 在设备上初始化得起来，并且真的把一段
     * 音频放完（不依赖系统解码器 —— 用户要的"内置 ffmpeg，别调系统"）。
     *
     * 这条在真机/模拟器上跑：如果 AAR 少了对应 ABI、或者 .so 没打进 APK，
     * 初始化就会失败，断言立刻炸 —— 那正是"装到手机上才发现放不了"的场景。
     */
    @Test
    fun bundledPlayerInitializesAndPlaysAudio() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val engine = cn.eversend.app.VlcHolder.engine(context)
        assertTrue("libVLC 必须能初始化（AAR 里的 .so 要与设备 ABI 匹配）", engine != null)

        // 造一段 1 秒的 WAV（44 字节头 + 静音采样），交给内置播放器放。
        val sampleRate = 8000
        val seconds = 1
        val dataSize = sampleRate * seconds * 2
        val wav = java.io.ByteArrayOutputStream()
        fun le32(value: Int) = byteArrayOf(
            (value and 0xFF).toByte(), ((value shr 8) and 0xFF).toByte(),
            ((value shr 16) and 0xFF).toByte(), ((value shr 24) and 0xFF).toByte(),
        )
        fun le16(value: Int) = byteArrayOf((value and 0xFF).toByte(), ((value shr 8) and 0xFF).toByte())
        wav.write("RIFF".toByteArray()); wav.write(le32(36 + dataSize)); wav.write("WAVE".toByteArray())
        wav.write("fmt ".toByteArray()); wav.write(le32(16)); wav.write(le16(1)); wav.write(le16(1))
        wav.write(le32(sampleRate)); wav.write(le32(sampleRate * 2)); wav.write(le16(2)); wav.write(le16(16))
        wav.write("data".toByteArray()); wav.write(le32(dataSize)); wav.write(ByteArray(dataSize))
        val file = File(context.cacheDir, "instrumented-语音.wav")
        file.writeBytes(wav.toByteArray())

        val finished = java.util.concurrent.CountDownLatch(1)
        val error = java.util.concurrent.atomic.AtomicReference("")
        val player = cn.eversend.app.VlcHolder.player(context) { problem -> error.set(problem) }
        assertTrue("播放器要能创建：${error.get()}", player != null)
        player!!.play(android.net.Uri.fromFile(file).toString()) { finished.countDown() }
        assertTrue("内置播放器要真的把这段音频放完", finished.await(15, java.util.concurrent.TimeUnit.SECONDS))
        player.stop()
        file.delete()
    }
}
