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
}
