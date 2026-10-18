package cn.eversend.app

import android.content.Context
import android.net.wifi.WifiManager
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.MulticastSocket
import java.net.NetworkInterface
import java.net.SocketTimeoutException
import java.util.Collections
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * 常驻的局域网发现引擎（对应宪章 C13 / §5）。
 *
 * 为什么必须常驻，而不是"点一下搜一次"
 * ------------------------------------
 * 以前安卓端只有 [ApiClient.discover] 这个**一次性阻塞**函数，而且没有任何地方在
 * 启动时调用它：App 打开后只会去连"上次记住的地址"，地址是空的（全新安装）或者
 * 电脑换了 IP，界面就一直写着"连不上"，用户只能自己去设置页手输 IP。这就是
 * "安卓版不能自动扫描连上，还需要手动输 IP"的第一层原因。
 *
 * 第二层原因是"听不到"：安卓的 Wi-Fi 固件在省电状态下会把**目的地址不是本机的
 * 组播/广播帧直接丢掉**，只有拿到 `WifiManager.MulticastLock` 才会放行。没有这个
 * 锁，电脑端每 30 秒一次的公告根本进不来，mDNS 也收不到 —— 只能靠 TCP 主动扫描
 * 兜底，而那条路（254 台 × 超时）本来就要好几秒。
 *
 * 所以这里做三件事：
 *
 * 1. **启动即公告**：首包 ≤300ms，之后 1s / 2s / 5s 退避（宪章 §5.1），稳态 15 秒；
 *    顺便让电脑端立刻看到这台手机（不用等它自己扫）。
 * 2. **常驻收听**：三条 UDP 通道各占一个 socket（见下），随时接住电脑端的公告
 *    和它对探针的单播应答，不需要用户点任何东西。
 * 3. **主动扫描**：UDP 全被拦时按 TCP 把本网段敲一遍（节流到不刷爆交换机），
 *    启动时也会跑一次，所以"≤5 秒出现在列表里"在最坏的网络下也成立。
 *
 * 三条通道各自一个 socket、各自一个端口，这样"这台设备是从哪条通道发现的"是
 * **真的**，而不是猜的：回包只会回到发探针的那个 socket 上。
 *
 * | 通道 | socket | 发包目标 |
 * |---|---|---|
 * | 组播 | 52118，加入 239.255.83.68 | 组播组（每个网卡各发一次） |
 * | 广播 | 临时端口 | 255.255.255.255 + 每个网卡自己的定向广播地址 |
 * | 单播 | 临时端口 | 本网段每一台主机（/24 逐台） |
 * | 主动扫描 | TCP | 每台主机的网页端口（`/api/state`）→ 通道记作「主动扫描」 |
 * | mDNS | 系统 NsdManager | 查询 `_eversend._tcp`（安卓 14 起才拿得到主机地址） |
 *
 * 线程模型：每条通道一个接收线程 + 一个公告线程 + 一个扫描线程，全是守护线程；
 * 状态通过 [peers] / [stats] 两个 StateFlow 交给界面，跨线程安全。
 */
class LanDiscovery private constructor(private val appContext: Context) {

    companion object {
        private const val TAG = "EverSend"

        /** 和电脑端约定的发现端口。 */
        const val PORT = ApiClient.DISCOVERY_PORT

        /** 电脑端的组播组（`core/constants.py` 的 MULTICAST_GROUP_V4）。 */
        const val GROUP = ApiClient.MULTICAST_GROUP

        /**
         * 公告节奏：首包 ≤300ms，之后 1s / 2s / 5s（宪章 §5.1），稳态 15 秒。
         * 值是"从启动算起的绝对时刻"，不是间隔。
         */
        private val ANNOUNCE_AT_MS = longArrayOf(0L, 250L, 1000L, 2500L)

        /** 稳态：每 15 秒再公告一次。 */
        private const val STEADY_MS = 15_000L

        /** 超过这么久没再听到，就算"这台现在听不到了"（电脑端自己是 30 秒一次）。 */
        const val STALE_MS = 45_000L

        /** 十分钟没再听到就丢掉，列表不会无限长。 */
        private const val FORGET_MS = 600_000L

        /** 主动扫描的节流：两次之间至少隔这么久。 */
        private const val SCAN_MIN_GAP_MS = 6_000L

        /** 没有发现任何设备时的主动扫描周期起点（有设备了就不需要一直扫）。 */
        private const val SCAN_PERIOD_MS = 20_000L

        /** 一直找不到设备时扫描周期逐轮翻倍，最大到这里（5 分钟）。 */
        private const val SCAN_MAX_PERIOD_MS = 300_000L

        /** 单播全网兜底扫（254 包）的最小间隔：广播/组播都被拦时才用。 */
        private const val UNICAST_SWEEP_MS = 60_000L

        /** 发送失败日志限流：同一通道两次日志至少隔这么久（EPERM 时别刷爆 logcat）。 */
        private const val SEND_ERROR_LOG_GAP_MS = 5_000L

        @Volatile
        private var shared: LanDiscovery? = null

        /** 进程内单例：界面和前台服务共用同一份发现结果。 */
        fun get(context: Context): LanDiscovery = shared ?: synchronized(this) {
            shared ?: LanDiscovery(context.applicationContext).also { shared = it }
        }
    }

    // ------------------------------------------------------------------ 对外状态

    private val _peers = MutableStateFlow<List<DiscoveredPeer>>(emptyList())

    /** 当前听/扫到的设备（按 identityKey 去重，一台一行）。 */
    val peers: StateFlow<List<DiscoveredPeer>> = _peers.asStateFlow()

    private val _stats = MutableStateFlow(DiscoveryStats())

    /** 诊断用：每条通道各成功/失败多少次、最后一次错误（对应 E2）。 */
    val stats: StateFlow<DiscoveryStats> = _stats.asStateFlow()

    // ------------------------------------------------------------------ 内部状态

    private val running = AtomicBoolean(false)
    private val started = AtomicBoolean(false)
    private val scanning = AtomicBoolean(false)

    private val found = LinkedHashMap<String, DiscoveredPeer>()
    private val lock = Any()

    private val sent = HashMap<DiscoveryChannel, Int>()
    private val heard = HashMap<DiscoveryChannel, Int>()
    private val lastError = HashMap<DiscoveryChannel, String>()

    @Volatile private var lastScanAt = 0L
    @Volatile private var scanRounds = 0
    @Volatile private var startedAt = 0L
    @Volatile private var fatal: String = ""

    private var multicastLock: WifiManager.MulticastLock? = null
    private var multicastSocket: MulticastSocket? = null
    private var broadcastSocket: MulticastSocket? = null
    private var unicastSocket: MulticastSocket? = null
    private var announcer: Thread? = null
    private var scanner: Thread? = null

    // ---- 降噪/退避（真机 EPERM 事件后的加固，见 android-audit.md P1-C）----

    /** 上一次单播全网兜底扫的时刻：平时不扫，最多每分钟一次。 */
    @Volatile private var lastUnicastSweepAt = 0L

    /** 连续多少轮公告没听到任何可连设备（≥2 才动用单播兜底扫）。 */
    @Volatile private var announcePassesWithoutPeer = 0

    /** 本轮公告里发送失败的包数（日志里给出汇总数，而不是每包一条）。 */
    @Volatile private var sendFailuresThisPass = 0

    /** 发送失败日志限流用：每通道上一次打日志的时刻。 */
    private val lastSendLogAt = HashMap<DiscoveryChannel, Long>()

    // ------------------------------------------------------------------ 启动 / 停止

    /**
     * 幂等：重复调用不会起第二套线程。
     *
     * **所有 socket 操作都放到工作线程里做**，因为调用方是界面（`LaunchedEffect`
     * 跑在主线程上）：安卓对主线程上的 socket 收发作硬拦截，抛
     * `NetworkOnMainThreadException`。第一版就是在主线程上建的 socket，日志里
     * 每一条公告都写着 `NetworkOnMainThreadException` —— 三条 UDP 通道一条都没
     * 发出去，只剩 TCP 主动扫描在兜底。这和"第一版 connect() 在主线程联网、
     * 界面上只显示一行连不上"是同一个坑，所以这里干脆连绑定都不在主线程做。
     */
    fun start() {
        if (!started.compareAndSet(false, true)) return
        running.set(true)
        startedAt = System.currentTimeMillis()
        spawn("eversend-discover-start") {
            // 组播/广播收包的总开关（少了它，安卓的 Wi-Fi 省电逻辑会把帧丢掉）。
            multicastLock = acquireMulticastLock()
            multicastSocket = openSocket(PORT, joinGroup = true, channel = DiscoveryChannel.Multicast)
            broadcastSocket = openSocket(0, joinGroup = false, channel = DiscoveryChannel.Broadcast)
            unicastSocket = openSocket(0, joinGroup = false, channel = DiscoveryChannel.Unicast)
            Log.d(
                TAG,
                "发现引擎启动：组播口=${multicastSocket?.localPort} 广播口=${broadcastSocket?.localPort} " +
                    "单播口=${unicastSocket?.localPort} 组播锁=${multicastLock != null} " +
                    "网卡=${interfaces().joinToString(",") { "${it.name}(${ipv4Of(it).firstOrNull()?.address?.hostAddress ?: "?"})" }}",
            )
            // 全部网卡的原始状态：排查"为什么没发现"时先看这一行。
            Log.d(TAG, "网卡全景：${describeInterfaces()}")
            multicastSocket?.let { socket ->
                spawn("eversend-discover-multicast") { receiveLoop(socket, DiscoveryChannel.Multicast) }
            }
            broadcastSocket?.let { socket ->
                spawn("eversend-discover-broadcast") { receiveLoop(socket, DiscoveryChannel.Broadcast) }
            }
            unicastSocket?.let { socket ->
                spawn("eversend-discover-unicast") { receiveLoop(socket, DiscoveryChannel.Unicast) }
            }
            announcer = spawn("eversend-discover-announce") { announceLoop() }
            spawn("eversend-discover-scanloop") { scanLoop() }
            publish()
            // 启动就公告一次 + 扫一次：UDP 全被网络拦掉时，主动扫描是"≤5 秒出现"的保险。
            // 但单播兜底扫（254 包/网卡）不要一启动就触发：从"现在"开始算冷却，
            // 只有 60 秒后仍然没有可连设备才用（平时靠组播 + 定向广播 + TCP 扫描）。
            lastUnicastSweepAt = System.currentTimeMillis()
            announceOnce("启动")
            scanNow("启动", announce = false)
        }
    }

    fun stop() {
        running.set(false)
        started.set(false)
        for (socket in listOf(multicastSocket, broadcastSocket, unicastSocket)) {
            try {
                socket?.close()
            } catch (ignored: Exception) {
            }
        }
        multicastSocket = null
        broadcastSocket = null
        unicastSocket = null
        try {
            multicastLock?.release()
        } catch (ignored: Exception) {
        }
        multicastLock = null
        publish()
    }

    /**
     * 手动「扫描」：催一次公告，并把主动扫描提前（有节流）。可以从界面线程调用：
     * 发包与扫描都在自己的工作线程里（见 [start] 那段说明）。
     */
    fun scanNow(reason: String = "手动", announce: Boolean = true) {
        if (announce) spawn("eversend-discover-announce-once") { announceOnce(reason) }
        val now = System.currentTimeMillis()
        val gap = now - lastScanAt
        if (gap < SCAN_MIN_GAP_MS) {
            record(DiscoveryChannel.Scan, "扫描太频繁（${gap / 1000} 秒前刚扫过），已改用即时公告")
            return
        }
        if (!scanning.compareAndSet(false, true)) return
        lastScanAt = now
        scanner = spawn("eversend-discover-scan") {
            try {
                activeScan()
            } finally {
                scanning.set(false)
                publish()
            }
        }
    }

    // ------------------------------------------------------------------ 收包

    private fun openSocket(
        port: Int,
        joinGroup: Boolean,
        channel: DiscoveryChannel,
    ): MulticastSocket? {
        // 端口被占（比如上一个实例没退干净）时退到临时端口：回包是发到**来源端口**的，
        // 临时端口一样收得到，只有"顺便听广播"这一件事会退化成靠公告线程催。
        for (attempt in 0..1) {
            val wanted = if (attempt == 0) port else 0
            try {
                val socket = MulticastSocket(null)
                socket.reuseAddress = true
                socket.bind(InetSocketAddress(wanted))
                socket.broadcast = true
                socket.soTimeout = 500
                if (joinGroup) {
                    var joined = 0
                    for (nic in interfaces()) {
                        try {
                            socket.joinGroup(InetSocketAddress(InetAddress.getByName(GROUP), port), nic)
                            joined++
                        } catch (problem: Exception) {
                            record(channel, "网卡 ${nic.name} 加入组播组失败：${problem.javaClass.simpleName}")
                        }
                    }
                    if (joined == 0) record(channel, "没有网卡能加入组播组 $GROUP（网络可能禁了组播）")
                }
                return socket
            } catch (problem: Exception) {
                record(channel, "绑定端口 $wanted 失败：${problem.message}")
                if (wanted == 0) break
            }
        }
        return null
    }

    private fun receiveLoop(socket: MulticastSocket, channel: DiscoveryChannel) {
        val buffer = ByteArray(8192)
        while (running.get()) {
            val packet = DatagramPacket(buffer, buffer.size)
            try {
                socket.receive(packet)
            } catch (timeout: SocketTimeoutException) {
                continue
            } catch (problem: Exception) {
                if (running.get()) record(channel, "收包线程结束：${problem.message}")
                return
            }
            val text = String(packet.data, 0, packet.length, Charsets.UTF_8)
            val json = try {
                JSONObject(text)
            } catch (ignored: Exception) {
                continue
            }
            handleAnnouncement(json, packet.address?.hostAddress.orEmpty(), channel)
        }
    }

    /**
     * 收到一条公告 → 记成一台设备。
     *
     * 这里**不回答**（和电脑端同一个设计）：发现是无状态的，丢一个包只是晚一点知道，
     * 不会把对端搞成半连接状态。
     */
    private fun handleAnnouncement(json: JSONObject, host: String, channel: DiscoveryChannel) {
        if (json.optString("t") != "eversend/1") return
        val id = json.optString("id")
        if (id.isBlank()) return
        // 自己发的公告会从组播/广播回环回来（尤其模拟器上）。
        if (id == ApiClient.identity) return
        if (id == "probe" && json.optString("k") == "mobile") return
        val web = json.optInt("web", 0)
        val tcp = json.optInt("port", 0)
        val kind = json.optString("k")
        // 另一台手机/网页版：它自己没有可连的 HTTP 端口（手机之间都是经电脑中转），
        // 记进来会变成一行点不动的设备 —— 交给电脑端的设备列表去显示。
        if (web <= 0 && tcp <= 0 && (kind == "mobile" || kind == "")) return
        if (host.isBlank()) return
        // 收到什么就记一句：排查"界面上没有这台设备"时，第一个要回答的问题就是
        // "公告到底有没有进来"（E2 的可观测性从发现这一层就要有）。
        Log.d(TAG, "听到公告：id=$id name=${json.optString("n")} web=$web 来自 $host（${channel.label}）")
        upsert(
            DiscoveredPeer(
                identityKey = id,
                name = json.optString("n").ifBlank { host },
                host = host,
                webPort = web,
                tcpPort = tcp,
                kind = kind,
                platform = json.optString("p"),
                version = json.optString("v"),
                channels = setOf(channel.label),
                seenAt = System.currentTimeMillis(),
                note = if (web <= 0) "电脑没开手机网页端口，暂时连不上" else "",
            ),
            channel,
        )
    }

    /** 同一台设备（identityKey 相同）永远只有一条：新地址覆盖旧的，来源并集。 */
    private fun upsert(peer: DiscoveredPeer, channel: DiscoveryChannel) {
        synchronized(lock) {
            val existing = found[peer.identityKey]
            found[peer.identityKey] = if (existing == null) {
                peer
            } else {
                existing.copy(
                    name = peer.name.ifBlank { existing.name },
                    host = peer.host.ifBlank { existing.host },
                    webPort = if (peer.webPort > 0) peer.webPort else existing.webPort,
                    tcpPort = if (peer.tcpPort > 0) peer.tcpPort else existing.tcpPort,
                    kind = peer.kind.ifBlank { existing.kind },
                    platform = peer.platform.ifBlank { existing.platform },
                    version = peer.version.ifBlank { existing.version },
                    channels = existing.channels + peer.channels,
                    seenAt = peer.seenAt,
                    note = peer.note.ifBlank { existing.note },
                )
            }
        }
        heard[channel] = (heard[channel] ?: 0) + 1
        publish()
    }

    /** 主动扫描命中：身份能从 `/api/state` 里拿到（`device.id`）就用它，拿不到用地址。 */
    private fun upsertFromScan(candidate: ApiClient.Found) {
        val identity = candidate.identityKey.ifBlank { "${candidate.host}:${candidate.webPort}" }
        upsert(
            DiscoveredPeer(
                identityKey = identity,
                name = candidate.name.ifBlank { candidate.host },
                host = candidate.host,
                webPort = candidate.webPort,
                platform = candidate.platform,
                version = candidate.version,
                kind = "desktop",
                channels = setOf(DiscoveryChannel.Scan.label),
                seenAt = System.currentTimeMillis(),
            ),
            DiscoveryChannel.Scan,
        )
    }

    // ------------------------------------------------------------------ 发包

    /**
     * 公告一轮：组播 + 广播（全局与定向）+ 本网段逐台单播。
     *
     * 三条通道各自从自己的 socket 出去，所以电脑端的回包落在哪条通道上是确定的
     * —— 界面上的「发现来源」因此是真的。
     */
    fun announceOnce(reason: String = "") {
        val payload = announcement().toByteArray(Charsets.UTF_8)
        val group = try {
            InetAddress.getByName(GROUP)
        } catch (ignored: Exception) {
            null
        }
        val globalBroadcast = try {
            InetAddress.getByName("255.255.255.255")
        } catch (ignored: Exception) {
            null
        }
        for (nic in interfaces()) {
            multicastSocket?.let { socket ->
                if (group != null) send(socket, payload, group, PORT, nic, DiscoveryChannel.Multicast)
            }
            broadcastSocket?.let { socket ->
                if (globalBroadcast != null) {
                    send(socket, payload, globalBroadcast, PORT, nic, DiscoveryChannel.Broadcast)
                }
                directedBroadcast(nic)?.let { directed ->
                    send(socket, payload, directed, PORT, nic, DiscoveryChannel.Broadcast)
                }
            }
        }
        // 单播全网（254 包/网卡）**不再每轮都发**。
        // 原实现是"广播被 ROM 拦掉时的第二条路"，但代价太大：真机实测每轮公告
        // 给 /24 每台主机各发一个 UDP，ARP 表被 FAILED 条目塞满（ip neigh 里
        // 上百条 FAILED），一旦 App 掉到 cached 状态系统还会让每个包都返 EPERM
        // —— 一轮 256 条日志、256 次 UI 状态刷新，logcat 直接刷爆。
        // 现在：只有连续 2 轮一个能连的设备都没听到（说明组播+广播确实没兜住）
        // 才扫一次，且最多每 60 秒一次；平时靠组播 + 定向广播 + TCP 主动扫描
        // （scanLoop 已带指数退避）就够。
        val hasPeer = _peers.value.any { it.connectable }
        if (hasPeer) announcePassesWithoutPeer = 0 else announcePassesWithoutPeer++
        sendFailuresThisPass = 0
        val sweepSocket = unicastSocket
        val now = System.currentTimeMillis()
        if (sweepSocket != null && !hasPeer &&
            announcePassesWithoutPeer >= 2 && now - lastUnicastSweepAt >= UNICAST_SWEEP_MS
        ) {
            lastUnicastSweepAt = now
            var count = 0
            for (nic in interfaces()) {
                for (host in subnetHosts(nic)) {
                    send(sweepSocket, payload, host, PORT, nic, DiscoveryChannel.Unicast)
                    count++
                }
            }
            Log.d(TAG, "单播兜底扫一轮：$count 个地址")
        }
        if (reason.isNotBlank()) Log.d(TAG, "公告一轮（$reason）")
    }

    private fun send(
        socket: MulticastSocket,
        payload: ByteArray,
        target: InetAddress,
        port: Int,
        nic: NetworkInterface,
        channel: DiscoveryChannel,
    ) {
        // 出接口只在**组播**时才需要显式指定（组播没有"默认路由"这回事，不指定
        // 就不知道该从哪张网卡出去，很多 ROM 会直接丢掉）。广播与单播交给路由表
        // 决定 —— 见过的真实故障是：给一个没有 IPv4 的网卡（模拟器的 dummy0、
        // 手机上的 rmnet 残留）设了出接口之后，连本来好好的单播都发不出去了。
        if (channel == DiscoveryChannel.Multicast) {
            try {
                socket.networkInterface = nic
            } catch (problem: Exception) {
                record(channel, "指定网卡 ${nic.name} 失败：${problem.javaClass.simpleName}")
            }
        }
        try {
            socket.send(DatagramPacket(payload, payload.size, target, port))
            sent[channel] = (sent[channel] ?: 0) + 1
        } catch (problem: Exception) {
            sendFailuresThisPass++
            throttledSendError(channel, problem, target)
        }
    }

    /**
     * 发送失败：限流 + 聚合。
     *
     * 以前每个包失败都 `record()` 一次 —— log 一行 + StateFlow publish + 覆盖
     * UI 上的错误。真机 cached 状态下发 256 个包全 EPERM 时，就是每秒几百行
     * logcat + 几百次 UI 重组，真正有用的信息全被埋掉。
     * 现在：同一通道每 5 秒最多记一条，内容是本轮累计失败数 + 一个样例。
     */
    private fun throttledSendError(
        channel: DiscoveryChannel,
        problem: Exception,
        target: InetAddress,
    ) {
        val now = System.currentTimeMillis()
        val last = lastSendLogAt[channel] ?: 0L
        if (now - last < SEND_ERROR_LOG_GAP_MS) return
        lastSendLogAt[channel] = now
        val summary = "发送失败（本轮已 ${sendFailuresThisPass} 个包）：" +
            "${problem.javaClass.simpleName}: ${problem.message}（例：$target）"
        lastError[channel] = summary
        Log.d(TAG, "发现通道 ${channel.label}：$summary")
        publish()
    }

    /** 手机自己的公告：`kind=mobile`、没有 TCP/网页端口（它只通过电脑中转）。 */
    private fun announcement(): String {
        val id = ApiClient.identity ?: "android-unknown"
        val name = ApiClient.name ?: "安卓手机"
        val stamp = System.currentTimeMillis() / 1000
        return """{"t":"eversend/1","id":"$id","n":"${ApiClient.escapeJson(name)}",""" +
            """"k":"mobile","p":"android","v":"1.0.0","port":0,"web":0,"ts":$stamp}"""
    }

    private fun announceLoop() {
        val began = System.currentTimeMillis()
        for (moment in ANNOUNCE_AT_MS) {
            val wait = moment - (System.currentTimeMillis() - began)
            if (wait > 0 && !sleep(wait)) return
            if (!running.get()) return
            announceOnce(if (moment == 0L) "首包" else "退避")
        }
        while (running.get()) {
            if (!sleep(STEADY_MS)) return
            if (!running.get()) return
            announceOnce("稳态")
            publish()
        }
    }

    // ------------------------------------------------------------------ 主动扫描

    /**
     * TCP 主动扫描：把本网段每台主机的网页端口敲一遍，谁答"我是韧传"就是电脑。
     *
     * 用 [ApiClient.discoverByTcp]（已有的、经过测试的实现：整段覆盖 + 优先名单 +
     * 多端口重试），这里只负责节流、把结果并进发现列表、以及在没有设备时周期重扫。
     */
    private fun activeScan() {
        scanRounds++
        val ports = LinkedHashSet<Int>()
        ports.add(ApiClient.DEFAULT_WEB_PORT)
        rememberedPort()?.let { ports.add(it) }
        val hints = rememberedHosts()
        var hits = 0
        for (port in ports) {
            if (!running.get()) return
            val foundHere = try {
                ApiClient.discoverByTcp(webPort = port, timeoutMs = 9_000, threads = 32, hints = hints)
            } catch (problem: Exception) {
                record(DiscoveryChannel.Scan, "扫描失败：${problem.message}")
                emptyList()
            }
            for (candidate in foundHere) {
                upsertFromScan(candidate)
                hits++
            }
            if (foundHere.isNotEmpty()) break
        }
        Log.d(
            TAG,
            "主动扫描第 $scanRounds 轮结束：探了 ${ApiClient.lastTcpScanProbed} 台，命中 $hits 台（端口 $ports）",
        )
        if (hits == 0) {
            record(DiscoveryChannel.Scan, "扫了 ${ApiClient.lastTcpScanProbed} 台主机，没有应答（防火墙 / 不同网段？）")
        }
    }

    private fun scanLoop() {
        var period = SCAN_PERIOD_MS
        while (running.get()) {
            if (!sleep(period)) return
            if (!running.get()) return
            // 已经有能连的设备就不再反复扫网段：没必要刷交换机。
            if (_peers.value.any { it.connectable }) {
                period = SCAN_PERIOD_MS
                continue
            }
            // 一直找不到设备时逐轮拉长周期（20s→40→80→160→300s 封顶）。
            // 以前是固定 20 秒一次全 /24 TCP 扫描（254 条连接）：电脑没开机、
            // 熄屏、网络被限的时候等于一直在戳网络 —— 费电、扰民、还制造
            // 大量 EPERM 日志。现在退避后包数下降一个数量级。
            period = (period * 2).coerceAtMost(SCAN_MAX_PERIOD_MS)
            scanNow("定期")
        }
    }

    // ------------------------------------------------------------------ 其它

    private fun publish() {
        val now = System.currentTimeMillis()
        val snapshot: List<DiscoveredPeer>
        synchronized(lock) {
            val iterator = found.entries.iterator()
            while (iterator.hasNext()) {
                if (now - iterator.next().value.seenAt > FORGET_MS) iterator.remove()
            }
            snapshot = found.values.sortedWith(
                compareByDescending<DiscoveredPeer> { it.connectable }
                    .thenByDescending { it.seenAt },
            )
        }
        _peers.value = snapshot
        _stats.value = DiscoveryStats(
            listening = running.get(),
            port = multicastSocket?.localPort ?: PORT,
            localAddresses = localAddresses(),
            channels = DiscoveryChannel.entries
                .filter { it != DiscoveryChannel.Manual }
                .map { channel ->
                    ChannelStat(
                        label = channel.label,
                        sent = sent[channel] ?: 0,
                        heard = heard[channel] ?: 0,
                        lastError = lastError[channel].orEmpty(),
                    )
                },
            rounds = scanRounds,
            lastError = fatal,
            startedAt = startedAt,
        )
    }

    private fun record(channel: DiscoveryChannel, message: String) {
        lastError[channel] = message
        fatal = "${channel.label}：$message"
        Log.d(TAG, "发现通道 ${channel.label}：$message")
        publish()
    }

    private fun acquireMulticastLock(): WifiManager.MulticastLock? = try {
        val wifi = appContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
        wifi?.createMulticastLock("eversend-discovery")?.apply {
            setReferenceCounted(false)
            acquire()
        }
    } catch (problem: Throwable) {
        // 没有这个锁也能跑（只是收不到组播/广播），不该让整个发现挂掉。
        Log.d(TAG, "拿不到组播锁：${problem.message}")
        null
    }

    /**
     * 能用的网卡 —— **以系统当前连接的网络为准，再并上枚举出来的所有候选**。
     *
     * 这里踩过一个真机上的大坑（vivo V2408A / Android 16，双 Wi-Fi + 蜂窝）：
     * 光靠 `NetworkInterface.getNetworkInterfaces()` 枚举，拿到的是
     * `wlan1`（172.20.110.78，一块**有 IP 但一个字节流量都没有**的僵尸网卡）
     * 加 `rmnet_data3`（蜂窝），**真正在用的 `wlan0`（172.20.90.82）反而不在列表里**。
     * 后果是：
     *   · 组播/广播只从 wlan1 和蜂窝发出去 → 同一个 Wi-Fi 上的电脑**一个包都收不到**；
     *   · 主动扫描按网卡逐个扫网段 → 扫的是 172.20.110.0/24 与 10.19.5.x，
     *     压根没扫电脑所在的 172.20.90.0/24（真机日志就是"探了 508 台，命中 0 台"）。
     * 用户看到的就是"手机永远找不到电脑，只能手输 IP"。
     *
     * 所以现在两条腿走路：
     * 1. **`ConnectivityManager`**：问系统"你现在走的是哪块网卡"（`LinkProperties`），
     *    那是路由表真正在用的那块，最权威；
     * 2. **枚举兜底**：把 `getNetworkInterfaces()` 里其它可用的也并进来 ——
     *    多网卡时另一块网卡上也可能有电脑，不能只顾一块。
     */
    private fun interfaces(): List<NetworkInterface> {
        val picked = LinkedHashMap<String, NetworkInterface>()
        for (nic in activeNetworkInterfaces()) picked[nic.name] = nic
        for (nic in enumeratedInterfaces()) picked.putIfAbsent(nic.name, nic)
        // 真的一块都没挑出来时退回最宽松的候选：宁可发出去没人听，也好过一条通道都不跑。
        if (picked.isEmpty()) {
            for (nic in enumeratedInterfaces(relaxed = true)) picked[nic.name] = nic
        }
        return picked.values.toList()
    }

    /** 系统认为"当前在用"的网卡（Wi-Fi + 蜂窝同时在线时会有多块）。 */
    private fun activeNetworkInterfaces(): List<NetworkInterface> {
        val out = LinkedHashMap<String, NetworkInterface>()
        try {
            val cm = appContext.getSystemService(Context.CONNECTIVITY_SERVICE)
                as? android.net.ConnectivityManager ?: return emptyList()
            for (network in cm.allNetworks) {
                val props = try {
                    cm.getLinkProperties(network)
                } catch (ignored: Exception) {
                    null
                } ?: continue
                val name = props.interfaceName ?: continue
                val nic = try {
                    NetworkInterface.getByName(name)
                } catch (ignored: Exception) {
                    null
                } ?: continue
                if (nic.isLoopback) continue
                // 只认有 IPv4 的：我们的公告是 v4，纯 v6 的链路发不出去。
                if (ipv4Of(nic).none { info -> !info.address.isLinkLocalAddress }) continue
                out[name] = nic
            }
        } catch (problem: Exception) {
            Log.d(TAG, "读系统当前网络失败：${problem.javaClass.simpleName} ${problem.message}")
        }
        return out.values.toList()
    }

    private fun enumeratedInterfaces(relaxed: Boolean = false): List<NetworkInterface> = try {
        Collections.list(NetworkInterface.getNetworkInterfaces())
            .filter { it.isUp && !it.isLoopback && (relaxed || !it.isVirtual) }
            .filter { ipv4Of(it).any { info -> !info.address.isLinkLocalAddress } }
    } catch (ignored: Exception) {
        emptyList()
    }

    /**
     * 每块网卡的原始状态，进诊断日志。
     *
     * 真机排查时最需要的就是这一行：哪块在用、哪块被丢了，以及**为什么**被丢。
     * （这次就是因为 `interfaces()` 把 `wlan0` 悄悄丢了，日志里只剩 `网卡=wlan1,rmnet_data3`，
     * 看了半天才反应过来真正在用的那块不在列表里。）
     */
    private fun describeInterfaces(): String = try {
        val used = interfaces().map { it.name }.toSet()
        Collections.list(NetworkInterface.getNetworkInterfaces()).joinToString(" | ") { nic ->
            val v4 = ipv4Of(nic).joinToString(",") { it.address.hostAddress ?: "?" }
            "${nic.name}(up=${nic.isUp},virtual=${nic.isVirtual},v4=${v4.ifEmpty { "无" }},${if (nic.name in used) "用" else "弃"})"
        }
    } catch (ignored: Exception) {
        "(枚举失败)"
    }

    private fun ipv4Of(nic: NetworkInterface): List<java.net.InterfaceAddress> =
        nic.interfaceAddresses.filter { it.address is Inet4Address && !it.address.isLoopbackAddress }

    private fun directedBroadcast(nic: NetworkInterface): InetAddress? =
        ipv4Of(nic).firstOrNull { it.broadcast != null }?.broadcast

    /** 本网段（最多 /24）的每一台主机地址：广播被拦时靠它把对端叫醒。 */
    private fun subnetHosts(nic: NetworkInterface): List<InetAddress> {
        val out = ArrayList<InetAddress>(254)
        for (info in ipv4Of(nic)) {
            if (info.address.isLinkLocalAddress) continue
            val base = info.address.address
            val prefix = info.networkPrefixLength.toInt().coerceAtLeast(24)
            if (prefix > 24) continue // 点对点链路没必要逐台敲
            for (last in 1..254) {
                try {
                    out.add(InetAddress.getByAddress(byteArrayOf(base[0], base[1], base[2], last.toByte())))
                } catch (ignored: Exception) {
                }
            }
        }
        return out
    }

    private fun localAddresses(): List<String> {
        val out = mutableListOf<String>()
        for (nic in interfaces()) {
            for (info in ipv4Of(nic)) {
                info.address.hostAddress?.let { out.add(it) }
            }
        }
        return out
    }

    /** 上次连过的电脑：扫描优先名单 + 端口候选（电脑换了端口时唯一还能自己找回来的路）。 */
    private fun rememberedHosts(): List<String> {
        val host = AppState(appContext).host
        if (host.isBlank()) return emptyList()
        return listOf(host)
    }

    private fun rememberedPort(): Int? =
        AppState(appContext).host.substringAfterLast(":", "").toIntOrNull()

    private fun spawn(name: String, body: () -> Unit): Thread =
        Thread(body, name).apply {
            isDaemon = true
            start()
        }

    private fun sleep(ms: Long): Boolean = try {
        Thread.sleep(ms)
        true
    } catch (interrupted: InterruptedException) {
        false
    }

    // ------------------------------------------------------------------ 供测试用

    /** 每条通道实际绑到的端口（仪器测试直接把假公告打进这些端口，验证收包与归类）。 */
    fun debugPort(channel: DiscoveryChannel): Int = when (channel) {
        DiscoveryChannel.Multicast -> multicastSocket?.localPort ?: 0
        DiscoveryChannel.Broadcast -> broadcastSocket?.localPort ?: 0
        DiscoveryChannel.Unicast -> unicastSocket?.localPort ?: 0
        else -> 0
    }

    /** 只给测试用：把一条公告当成本地收到的包处理（不经过网络）。 */
    fun debugDeliver(json: String, host: String, channel: DiscoveryChannel) {
        try {
            handleAnnouncement(JSONObject(json), host, channel)
        } catch (ignored: Exception) {
        }
    }

    /** 只给测试用：丢掉一条设备记录（真机上引擎是常驻的，测试造的假设备要能清掉）。 */
    fun debugForget(identityKey: String) {
        synchronized(lock) { found.remove(identityKey) }
        publish()
    }
}

/** 发现通道：界面上显示的"发现来源"就是这里的 [label]。 */
enum class DiscoveryChannel(val label: String) {
    Multicast("组播"),
    Broadcast("广播"),
    Unicast("单播应答"),
    Scan("主动扫描"),
    Mdns("mDNS"),
    Manual("手动"),
}

/** 发现到的一台设备（还没连上，只是听到了它）。 */
data class DiscoveredPeer(
    /** 稳定身份：公告里的 `id`（服务端叫 identityKey），去重就用它。 */
    val identityKey: String,
    val name: String,
    val host: String,
    val webPort: Int,
    val tcpPort: Int = 0,
    val kind: String = "",
    val platform: String = "",
    val version: String = "",
    /** 这台设备被哪几条通道听到过（界面显示第一条）。 */
    val channels: Set<String> = emptySet(),
    val seenAt: Long = System.currentTimeMillis(),
    /** 连不上时的一句话原因（比如"电脑没开手机网页端口"）。 */
    val note: String = "",
) {
    /** 手机上只能走 HTTP（网页接口），所以没有网页端口的电脑连不上。 */
    val connectable: Boolean get() = webPort > 0 && host.isNotBlank()

    val base: String get() = "http://$host:$webPort/"

    /** 发现来源：取最近一次听到的那条通道；界面禁止这里为空。 */
    val source: String get() = channels.firstOrNull().orEmpty().ifBlank { DiscoveryChannel.Manual.label }

    val ageMs: Long get() = System.currentTimeMillis() - seenAt

    val stale: Boolean get() = ageMs > LanDiscovery.STALE_MS
}

/** 一条通道的统计（设置页诊断卡片用）。 */
data class ChannelStat(
    val label: String,
    val sent: Int = 0,
    val heard: Int = 0,
    val lastError: String = "",
)

/** 发现引擎的整体状态。 */
data class DiscoveryStats(
    val listening: Boolean = false,
    val port: Int = 0,
    val localAddresses: List<String> = emptyList(),
    val channels: List<ChannelStat> = emptyList(),
    val rounds: Int = 0,
    val lastError: String = "",
    val startedAt: Long = 0L,
)
