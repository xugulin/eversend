package cn.eversend.app

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress

/**
 * 自动发现 + 文件动作矩阵 + 待重发队列的仪器测试（对应宪章 C13 / C4 / C6 / §4 / §5.5）。
 *
 * 为什么这些必须在真设备上跑：[LanDiscovery] 的收包路径依赖真实的 UDP socket、
 * 组播锁与端口绑定，这些在 JVM 单元测试里全是假的。这里的每一条要么真的开一个
 * socket 把公告打进去（收包 → 解析 → 归类 → 去重整条链），要么真的把界面拉起来
 * 看列表里有没有那台设备。
 *
 * 纯函数那几条（去重、筛选、队列）也放在这里：它们是上面那些行为的判定依据，
 * 和"界面到底怎么画"放在同一个文件里更不容易走散。
 */
@RunWith(AndroidJUnit4::class)
class DiscoveryAndActionsTest {

    /**
     * 通知权限先给掉。
     *
     * 不给的话，App 一启动就弹系统权限框，整个屏幕被系统窗口盖住 —— uiautomator
     * 只看得到那个弹窗（"Allow 韧传 to send you notifications?"），界面级的断言
     * 会以"界面没渲染出来"这种看不出原因的方式失败（第一次跑就是这样）。
     */
    @get:org.junit.Rule
    val notificationPermission: androidx.test.rule.GrantPermissionRule =
        androidx.test.rule.GrantPermissionRule.grant(android.Manifest.permission.POST_NOTIFICATIONS)

    private val context: Context
        get() = InstrumentationRegistry.getInstrumentation().targetContext

    // ---------------------------------------------------------------- 工具

    /** 等某条通道绑上端口（引擎启动是异步的）。 */
    private fun waitPort(discovery: LanDiscovery, channel: DiscoveryChannel, timeoutMs: Long = 4000): Int {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val port = discovery.debugPort(channel)
            if (port > 0) return port
            Thread.sleep(100)
        }
        return 0
    }

    /** 把一条公告真的打到本机某个端口上（走完整的内核 → socket → 解析那条路）。 */
    private fun sendAnnouncement(port: Int, json: String) {
        DatagramSocket().use { sender ->
            val bytes = json.toByteArray(Charsets.UTF_8)
            sender.send(DatagramPacket(bytes, bytes.size, InetAddress.getByName("127.0.0.1"), port))
        }
    }

    private fun announcement(id: String, name: String, web: Int = 0, tcp: Int = 52117): String =
        """{"t":"eversend/1","id":"$id","n":"$name","k":"desktop","p":"linux","v":"1.0.0",""" +
            """"port":$tcp,"web":$web,"ts":0}"""

    private fun waitPeer(discovery: LanDiscovery, id: String, timeoutMs: Long = 5000): DiscoveredPeer? {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            discovery.peers.value.firstOrNull { it.identityKey == id }?.let { return it }
            Thread.sleep(100)
        }
        return null
    }

    // ---------------------------------------------------------------- 自动发现

    /**
     * **C13 的核心断言**：一条公告打进来，≤5 秒它必须以"组播"来源出现在发现列表里。
     *
     * 走的是真实的 socket（不是直接调内部函数）：绑定端口 → 内核投递 → 解析 JSON →
     * 按通道归类 → 发布到 StateFlow。这几步任何一步坏了，用户看到的就是"搜不到电脑"。
     */
    @Test
    fun autoDiscoveryHearsAPeerWithinFiveSeconds() {
        val discovery = LanDiscovery.get(context)
        discovery.start()
        val port = waitPort(discovery, DiscoveryChannel.Multicast)
        assertTrue(
            "组播通道必须绑上一个端口（拿不到端口就永远听不到公告）",
            port > 0,
        )

        val id = "auto-test-${System.currentTimeMillis()}"
        val began = System.currentTimeMillis()
        var peer: DiscoveredPeer? = null
        try {
            while (System.currentTimeMillis() - began < 5000 && peer == null) {
                sendAnnouncement(port, announcement(id, "自测电脑"))
                Thread.sleep(200)
                peer = discovery.peers.value.firstOrNull { it.identityKey == id }
            }
        } finally {
            // 别把这条假设备留给后面的用例（真机上引擎是常驻的）。
            discovery.debugForget(id)
        }
        val elapsed = System.currentTimeMillis() - began
        assertNotNull("5 秒内必须听到这条公告（端口 $port）", peer)
        assertTrue("必须在 5 秒内（实际 ${elapsed}ms）", elapsed <= 5000)
        assertEquals("名字要原样带回来", "自测电脑", peer!!.name)
        assertEquals("发现来源必须是组播", DiscoveryChannel.Multicast.label, peer.source)
    }

    /** 广播通道同样要能收到；**同一个 identityKey 无论从几条通道听到，都只有一行**。 */
    @Test
    fun broadcastIsHeardAndOneDeviceStaysOneRow() {
        val discovery = LanDiscovery.get(context)
        discovery.start()
        val port = waitPort(discovery, DiscoveryChannel.Broadcast)
        assertTrue("广播通道必须绑上端口", port > 0)

        val id = "dedupe-test-${System.currentTimeMillis()}"
        sendAnnouncement(port, announcement(id, "同名电脑"))
        val heard = waitPeer(discovery, id)
        assertNotNull("广播通道也要能听到公告（广播是组播被 IGMP 剪掉时的主力）", heard)
        assertEquals(DiscoveryChannel.Broadcast.label, heard!!.source)

        // 同一条身份再从"单播应答"听到一次，地址不同 —— 不能变成第二行。
        discovery.debugDeliver(announcement(id, "同名电脑"), "10.1.2.3", DiscoveryChannel.Unicast)
        val rows = discovery.peers.value.filter { it.identityKey == id }
        discovery.debugForget(id)
        assertEquals("同一个 identityKey 只能有一行（C4）", 1, rows.size)
        assertEquals("地址要跟着最近一次听到的走", "10.1.2.3", rows[0].host)
        assertTrue(
            "来源是并集：广播 + 单播应答",
            rows[0].channels.contains(DiscoveryChannel.Broadcast.label) &&
                rows[0].channels.contains(DiscoveryChannel.Unicast.label),
        )
    }

    /** 没有网页端口的电脑：仍然显示（用户要能看见"它在那儿"），但不能自动连。 */
    @Test
    fun peerWithoutWebPortIsShownButNotConnectable() {
        val discovery = LanDiscovery.get(context)
        discovery.start()
        val id = "no-web-${System.currentTimeMillis()}"
        discovery.debugDeliver(announcement(id, "没开网页的电脑", web = 0), "10.0.0.9", DiscoveryChannel.Unicast)
        val peer = discovery.peers.value.firstOrNull { it.identityKey == id }
        discovery.debugForget(id)
        assertNotNull("没有网页端口的电脑也要显示出来（否则用户只会看到'什么都没有'）", peer)
        assertFalse("手机只能走 HTTP，所以它不能被选成自动连接目标", peer!!.connectable)
        assertTrue("要写清为什么连不上：${peer.note}", peer.note.contains("网页"))
    }

    // ---------------------------------------------------------------- 去重 / 来源

    /** 电脑端报的设备 + 自动发现的同一台 = 一行，并且带上"怎么发现的"。 */
    @Test
    fun deviceListKeepsOneRowPerIdentityKey() {
        val row = DeviceRow(
            id = "web:dev-1",
            name = "韧传电脑",
            kind = "电脑",
            address = "192.168.1.5:52119",
            identity = "dev-1",
            aliases = identityAliases("dev-1"),
        )
        val peer = DiscoveredPeer(
            identityKey = "dev-1",
            name = "韧传电脑",
            host = "192.168.1.9",
            webPort = 52119,
            platform = "linux",
            channels = setOf(DiscoveryChannel.Multicast.label),
        )
        val merged = mergeDiscovered(listOf(row), listOf(peer))
        assertEquals("同一台设备只能有一行（C4）", 1, merged.size)
        assertEquals("地址跟着最近一次听到的走", "192.168.1.9:52119", merged[0].address)
        assertEquals("发现来源要写在行上", "组播", merged[0].source)
    }

    /** 发现到但电脑端还没报出来的那台：作为新行加进列表（否则"发现了却看不见"）。 */
    @Test
    fun discoveredPeerBecomesItsOwnRow() {
        val peer = DiscoveredPeer(
            identityKey = "brand-new",
            name = "另一台电脑",
            host = "10.0.0.7",
            webPort = 52119,
            platform = "windows",
            channels = setOf(DiscoveryChannel.Scan.label),
        )
        val merged = mergeDiscovered(emptyList(), listOf(peer))
        assertEquals(1, merged.size)
        assertEquals("主动扫描", merged[0].source)
        assertTrue("要能直接连（有地址和网页端口）", merged[0].online)
    }

    /** 自动连接优先选"能连的电脑"，陈旧的和安卓端排在后面。 */    @Test
    fun autoConnectTargetPrefersAReachableDesktop() {
        val now = System.currentTimeMillis()
        val android = DiscoveredPeer("a", "手机", "10.0.0.2", 52119, platform = "android", seenAt = now)
        val stale = DiscoveredPeer("b", "老电脑", "10.0.0.3", 52119, platform = "linux", seenAt = now - 600_000)
        val fresh = DiscoveredPeer("c", "新电脑", "10.0.0.4", 52119, platform = "linux", seenAt = now)
        val dead = DiscoveredPeer("d", "没网页口的", "10.0.0.5", 0, platform = "linux", seenAt = now)
        assertEquals("优先新鲜的电脑", "c", autoConnectTarget(listOf(android, stale, dead, fresh))?.identityKey)
        assertEquals("没有能连的就返回 null（不硬连）", null, autoConnectTarget(listOf(dead)))
        assertEquals("安卓端排最后（它不是能收发文件的那台）", "a", autoConnectTarget(listOf(android))?.identityKey)
    }

    /**
     * 同名不同身份（records-data 在真实数据上看到的形态）：**不合并行**，但显示名要能分清。
     *
     * 规则与 Python 侧 `core/model.py` 的 `disambiguateNames` 一致：在线优先、最近出现
     * 优先、身份字典序兜底；其余依次加 `#2`、`#3`；名字唯一的原样不动。
     */
    @Test
    fun sameNameDifferentIdentityGetsNumberedSuffixes() {
        val rows = listOf(
            DeviceRow(id = "a", name = "RealProbe", kind = "电脑", address = "10.0.0.1:52119", online = false, secondsAgo = 5.0),
            DeviceRow(id = "b", name = "RealProbe", kind = "电脑", address = "10.0.0.2:52119", online = true, secondsAgo = 30.0),
            DeviceRow(id = "c", name = "RealProbe", kind = "电脑", address = "10.0.0.3:52119", online = true, secondsAgo = 9.0),
            DeviceRow(id = "d", name = "别的电脑", kind = "电脑", address = "10.0.0.4:52119"),
            DeviceRow(id = "e", name = "", kind = "电脑", address = "10.0.0.5:52119"),
        )
        val named = disambiguateNames(rows).associateBy { it.id }
        assertEquals("不同的身份一台都不能合并", 5, named.size)
        assertEquals("在线且最近出现的保留原名", "RealProbe", named["c"]!!.name)
        assertEquals("RealProbe #2", named["b"]!!.name)
        assertEquals("RealProbe #3", named["a"]!!.name)
        assertEquals("名字本来就唯一的原样不动", "别的电脑", named["d"]!!.name)
        assertEquals("没有名字的用统一文案", UNKNOWN_DEVICE_LABEL, named["e"]!!.name)

        // 结果必须与遍历顺序无关（否则每轮轮询名字都在跳）。
        val shuffled = disambiguateNames(rows.reversed()).associateBy { it.id }
        assertEquals(named["c"]!!.name, shuffled["c"]!!.name)
        assertEquals(named["b"]!!.name, shuffled["b"]!!.name)
        assertEquals(named["a"]!!.name, shuffled["a"]!!.name)
    }

    /**
     * 同一个地址上的两台不同设备：**两台，两行**（铁律 6）。
     *
     * 这条是回归测试：注入的那台假装在 10.42.42.42，而电脑端设备表里正好也有一台
     * 地址相同的设备，旧实现按地址兜底把它们并成了一行 —— 新发现的那台就这么
     * "消失在列表里"。地址会撞（NAT 后面两台设备同址、换网段后的残留），
     * 所以去重只能看稳定身份。
     */
    @Test
    fun sameAddressDifferentIdentityStaysTwoRows() {
        val row = DeviceRow(
            id = "web:dev-a",
            name = "A 电脑",
            kind = "电脑",
            address = "10.42.42.42:52119",
            identity = "dev-a",
            aliases = identityAliases("dev-a"),
        )
        val peer = DiscoveredPeer(
            identityKey = "dev-b",
            name = "B 电脑",
            host = "10.42.42.42",
            webPort = 52119,
            platform = "linux",
            channels = setOf(DiscoveryChannel.Multicast.label),
        )
        val merged = mergeDiscovered(listOf(row), listOf(peer))
        assertEquals("地址相同、身份不同 ⇒ 两台设备必须两行", 2, merged.size)
        assertTrue("新发现的那台不能被吃掉：${merged.map { it.name }}", merged.any { it.name == "B 电脑" })
        assertTrue("原来那台也要在", merged.any { it.name == "A 电脑" })
    }

    /** 三端同一套措辞：发现方式的中文说法。 */    @Test
    fun discoverySourceLabelsAreChinese() {
        assertEquals("组播", sourceLabel("multicast"))
        assertEquals("广播", sourceLabel("broadcast"))
        assertEquals("单播应答", sourceLabel("unicast"))
        assertEquals("主动扫描", sourceLabel("scan"))
        assertEquals("组播", sourceLabel(DiscoveryChannel.Multicast.label))
        assertEquals("主动扫描", sourceLabel(DiscoveryChannel.Scan.label))
    }

    // ---------------------------------------------------------------- 文件动作矩阵

    /** 宪章 §4 的安卓那一列，菜单里一个不能少（C6）。 */
    @Test
    fun fileActionMatrixMatchesTheCharter() {
        val labels = FileAction.entries.map { it.label }
        listOf("预览", "打开", "打开所在文件夹", "另存为", "复制", "删除本地文件", "重新发送", "属性")
            .forEach { wanted ->
                assertTrue("菜单里必须有「$wanted」，现在是 $labels", labels.any { it.startsWith(wanted) })
            }
        assertTrue(
            "「复制路径」在安卓上不支持：不能出现在菜单里（用属性弹层里那句话说明），现在是 $labels",
            labels.none { it.contains("复制路径") },
        )
        assertEquals("一个动作只能有一个入口（C1）", labels.size, labels.toSet().size)
    }

    // ---------------------------------------------------------------- 待重发队列

    /** C14：发失败的消息必须留在队列里、带原因、带退避、能重试、能放弃。 */
    @Test
    fun failedMessageStaysVisibleInTheQueue() {
        val item = Queued(key = "m1", conversationId = "d:x", text = "你好")
        val failed = item.afterFailure("超时（对方可能不在线或被防火墙挡住）", now = 1_000L)
        assertEquals(1, failed.attempts)
        assertTrue("失败原因要留在条目上（界面上就是那行红字）", failed.error.contains("超时"))
        assertEquals("第一次失败等 1 秒再投", 2_000L, failed.nextAt)
        assertEquals(
            "退避必须是 1/2/4/8/16 秒",
            listOf(1000L, 2000L, 4000L, 8000L, 16000L),
            (1..5).map { backoffMs(it) },
        )

        var queued = item
        repeat(5) { queued = queued.afterFailure("还是不行", now = 10_000L) }
        assertTrue("5 次之后不再自动重投", queued.exhausted())
        assertFalse("用完了就不能自动投", queued.ready(now = 999_999L))

        val retried = queued.retryNow(now = 20_000L)
        assertFalse("手动重试要把次数清零", retried.exhausted())
        assertTrue("手动重试要立刻可投", retried.ready(now = 20_000L))
        assertEquals("手动重试要清掉旧原因", "", retried.error)
    }

    // ---------------------------------------------------------------- 传输页筛选

    /** §3.2 的四个筛选药丸：判定只看行上已有的字段。 */
    @Test
    fun transferFilterPillsSplitTheTable() {
        val rows = listOf(
            TableRow(key = "run", name = "a.bin", peer = "电脑", size = 10, time = "10:00", status = "接收中 42%", progress = 0.42f),
            TableRow(key = "done", name = "b.bin", peer = "电脑", size = 10, time = "10:01", status = "已完成"),
            TableRow(key = "fail", name = "c.bin", peer = "电脑", size = 10, time = "10:02", status = "失败"),
        )
        assertEquals(3, filterRows(rows, TransferFilter.All).size)
        assertEquals(listOf("run"), filterRows(rows, TransferFilter.Running).map { it.key })
        assertEquals(listOf("done"), filterRows(rows, TransferFilter.Done).map { it.key })
        assertEquals(listOf("fail"), filterRows(rows, TransferFilter.Failed).map { it.key })
        assertEquals(
            "四个药丸的文案按宪章 §3.2",
            listOf("全部", "进行中", "已完成", "失败"),
            TransferFilter.entries.map { it.label },
        )
    }

    // ---------------------------------------------------------------- 界面级

    /**
     * 界面级：**不输任何地址**，自动发现的设备要出现在设备列表里，并写明发现来源。
     *
     * 这条正是用户报的那句话（"还得手动输 IP"）的反面：先把记住的地址清掉（模拟
     * 全新安装），拉起界面，然后把一条公告投进引擎 —— 列表里必须出现那台设备。
     */
    @Test
    fun sidebarShowsAutoDiscoveredDeviceWithoutTypingAnAddress() {
        context.getSharedPreferences("eversend", Context.MODE_PRIVATE)
            .edit()
            .remove("host")
            .apply()
        val discovery = LanDiscovery.get(context)
        discovery.start()
        val id = "ui-peer-${System.currentTimeMillis()}"

        androidx.test.core.app.ActivityScenario.launch(MainActivity::class.java).use {
            val device = androidx.test.uiautomator.UiDevice.getInstance(
                InstrumentationRegistry.getInstrumentation()
            )
            assertTrue(
                "界面必须渲染出来（侧栏在窄屏是抽屉，所以先看会话页）",
                device.wait(
                    androidx.test.uiautomator.Until.hasObject(
                        androidx.test.uiautomator.By.textContains("会话")
                    ),
                    60_000,
                ),
            )
            // 窄屏：打开设备抽屉（☰ 的内容描述是「设备栏」）。
            //
            // 每一步都等：界面刚起来时 ☰ 还没进无障碍树，直接 click 会点空 ——
            // 那样后面的断言全都"看起来像功能坏了"，其实只是没打开抽屉。
            val drawerButton = device.wait(
                androidx.test.uiautomator.Until.findObject(androidx.test.uiautomator.By.desc("设备栏")),
                30_000,
            )
            assertNotNull("窄屏左上角必须有打开设备栏的按钮", drawerButton)
            drawerButton!!.click()
            assertTrue(
                "点 ☰ 要能打开设备抽屉",
                device.wait(
                    androidx.test.uiautomator.Until.hasObject(
                        androidx.test.uiautomator.By.textContains("设备列表")
                    ),
                    15_000,
                ),
            )
            // 侧栏必须能看出"正在自动找"**或者**"已经自动连上了" —— 两种都对：
            // 跑测试的这台机器上如果真有一台电脑在跑韧传，App 会在几秒内直接连上
            // （那正是要的行为）；一台都没有时才显示"正在自动搜索"。
            Thread.sleep(500)
            val autoSearching = device.hasObject(
                androidx.test.uiautomator.By.textContains("自动搜索同网段的电脑")
            )
            val autoConnected = device.hasObject(androidx.test.uiautomator.By.textContains("电脑："))
            assertTrue(
                "侧栏必须显示自动发现的状态（搜索中或已自动连上），而不是让人去输 IP",
                autoSearching || autoConnected,
            )

            // 这台机器上同时跑着别的实例（队友的测试），在线设备会把"在线"那一段
            // 6 行的可视区占满，而且它们每 15 秒被重新听到一次、永远比注入的这条
            // 新鲜 —— 断言会以一个看起来像功能坏了的方式失败。
            //
            // 所以先把引擎停掉（不再发包/收包，就不会有新结果进来），并把别的发现
            // 结果清掉，只留下这一条被测的公告：这样"它出现在列表里"是确定的。
            discovery.stop()
            discovery.peers.value
                .map { it.identityKey }
                .filter { it != id }
                .forEach { discovery.debugForget(it) }

            discovery.debugDeliver(
                announcement(id, "局域网里的电脑", web = 52119),
                "10.42.42.42",
                DiscoveryChannel.Multicast,
            )
            val injected = discovery.peers.value.firstOrNull { it.identityKey == id }
            assertNotNull("引擎必须先把这条公告记下来", injected)
            // 断言的是侧栏顶部那行「自动发现 N 台：…」而不是设备段里的一行：
            // 设备段最多显示 6 行且内部滚动，在一条真实网络里（这台机器上同时跑着
            // 十几个实例）新出现的那台可能落在折叠区外 —— 那是"看不见"，不是"没发现"。
            // 顶部那行永远可见，所以它是"自动发现的设备确实进了界面"的确定性证据。
            val shown = device.wait(
                androidx.test.uiautomator.Until.hasObject(
                    androidx.test.uiautomator.By.textContains("局域网里的电脑")
                ),
                15_000,
            )
            if (!shown) {
                // 失败时把现场留下：引擎里有没有这条、界面上到底画了什么。
                val dump = File(context.getExternalFilesDir(null), "sidebar-dump.xml")
                try {
                    device.dumpWindowHierarchy(dump)
                } catch (ignored: Exception) {
                }
                throw AssertionError(
                    "自动发现的设备必须出现在界面上（零手动输入）；" +
                        "引擎里有：${injected!!.identityKey}@${injected.host} 来源 ${injected.source}；" +
                        "界面 dump：$dump",
                )
            }
            // 顶部那行先出现（只含名字），设备行的「发现来源」要等下一次重组才画；
            // 慢一点的模拟器上紧跟其后断言会假失败（CI 实测），所以等一会儿。
            assertTrue(
                "每一行都要写清「发现来源」",
                device.wait(
                    androidx.test.uiautomator.Until.hasObject(
                        androidx.test.uiautomator.By.textContains("组播")
                    ),
                    10_000,
                ),
            )
            try {
                device.takeScreenshot(File(context.getExternalFilesDir(null), "android-app-auto-discovery.png"))
            } catch (ignored: Exception) {
            }
            discovery.debugForget(id)
        }
    }
}
