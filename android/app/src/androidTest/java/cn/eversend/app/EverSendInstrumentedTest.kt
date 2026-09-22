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
        val payload = ByteArray(64 * 1024) { (it % 251).toByte() }
        val file = File(
            InstrumentationRegistry.getInstrumentation().targetContext.cacheDir,
            "instrumented-图片.png",
        )
        file.writeBytes(byteArrayOf(0x89.toByte(), 'P'.code.toByte(), 'N'.code.toByte(), 'G'.code.toByte()) + payload)

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
     * 界面级的真机测试：真的启动 App 界面，真的连上电脑，真的把会话显示出来。
     *
     * 为什么要有这一条：三个接口测试全绿的时候，App 界面仍然可能是坏的 —— 第一版
     * 就是这样：`connect()` 在主线程发网络请求，安卓抛 NetworkOnMainThreadException，
     * 界面上只有一行"连不上电脑：null"，而接口测试照样全过。CI 里那张真机截图是
     * 唯一的线索，所以把这件事也变成断言。
     */
    @Test
    fun appUiConnectsAndShowsTheConversation() {
        val api = client()
        api.postJson("/api/chat/send", JSONObject().put("text", "界面测试消息 " + System.currentTimeMillis()))

        val context = InstrumentationRegistry.getInstrumentation().targetContext
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
        }
    }
}
