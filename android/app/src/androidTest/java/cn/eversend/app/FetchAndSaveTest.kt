package cn.eversend.app

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayOutputStream

/**
 * 「电脑发来的文件真的落到手机上」这条路的仪器测试（task-9）。
 *
 * 这些断言是**能不能说谎**的判据（宪章 E3 的反面是"反馈错了"，比没反馈更糟）：
 *
 * 1. [saveToDownloads] 少写字节必须**失败**，并且**不许在「下载」里留下 pending 残留**
 *    —— 真机上抓到过 8 个 0 字节 `.pending-*`，而界面写着「已收到」；
 * 2. 只有真的落盘（且字节数相符）才允许出现「已保存到手机」这句话；
 * 3. 下载中的进度要能喂给既有的"接收中 X%"UI（三端同一套观感）。
 */
@RunWith(AndroidJUnit4::class)
class FetchAndSaveTest {

    private val context: Context
        get() = InstrumentationRegistry.getInstrumentation().targetContext

    /** 数一数「下载」里还有多少条 pending 行（只看得见本 App 自己的）。 */
    private fun pendingRows(): Int {
        if (android.os.Build.VERSION.SDK_INT < android.os.Build.VERSION_CODES.Q) return 0
        var count = 0
        try {
            context.contentResolver.query(
                android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                arrayOf(android.provider.MediaStore.Downloads._ID),
                android.provider.MediaStore.Downloads.IS_PENDING + " = 1",
                null,
                null,
            )?.use { cursor -> count = cursor.count }
        } catch (ignored: Exception) {
        }
        return count
    }

    /**
     * **少写字节 = 失败**（这是真机 bug 的核心）。
     *
     * 期望 100 字节、只写 10 字节：必须返回 null，而且不能在「下载」里留下一条
     * pending 行（旧实现正是因为异常路径不清 IS_PENDING，才留下 0 字节垃圾 + 界面说谎）。
     */
    @Test
    fun shortWriteIsRejectedAndRolledBack() {
        val before = pendingRows()
        val name = "eversend-short-${System.currentTimeMillis()}.bin"
        val uri = saveToDownloads(context, name, expectedBytes = 100L) { out ->
            out.write(ByteArray(10))
        }
        assertNull("只写了 10/100 字节：必须算失败（旧实现会返回一个看起来成功的 URI）", uri)
        assertEquals("失败必须回滚那条 pending 行，不留 0 字节残留", before, pendingRows())
    }

    /** 写够了就真的落盘，而且能原样读回来（"已保存到手机"这句话的判据）。 */
    @Test
    fun completeWriteLandsReadBackAndClearsPending() {
        val name = "eversend-full-${System.currentTimeMillis()}.bin"
        val payload = ByteArray(64 * 1024) { (it % 251).toByte() }
        val uri = saveToDownloads(context, name, expectedBytes = payload.size.toLong()) { out ->
            out.write(payload)
        }
        assertNotNull("写满了就必须成功（否则用户永远收不到文件）", uri)
        val back = context.contentResolver.openInputStream(uri!!)?.use { it.readBytes() }
        assertTrue("读回来必须和写进去的一模一样", back != null && back.contentEquals(payload))
        context.contentResolver.delete(uri, null, null)
    }

    /** 下载中的进度文案：百分比 + 已下载/总大小（§2.4 要求的样子）。 */
    @Test
    fun fetchProgressTextSaysPercentAndBytes() {
        val task = FetchTask(
            shareId = "s1",
            name = "进度测试.bin",
            size = 200L * 1024 * 1024,
            got = 100L * 1024 * 1024,
            state = FetchState.Downloading,
            speed = 10.0 * 1024 * 1024,
        )
        assertTrue("要有百分比：${task.progressText}", task.progressText.contains("50%"))
        assertTrue("要有已下载/总大小：${task.progressText}", task.progressText.contains("/"))
        assertEquals("下载中", task.label.take(3))
    }

    /** 状态文案不许超过事实：失败写原因，完成才写已完成。 */
    @Test
    fun fetchLabelsAreHonest() {
        val failed = FetchTask("s", "a.bin", 10, state = FetchState.Failed, error = "连接被拒")
        assertTrue("失败要带中文原因：${failed.label}", failed.label.contains("连接被拒"))
        val done = FetchTask("s", "a.bin", 10, got = 10, state = FetchState.Done)
        assertEquals("已完成", done.label)
        val queued = FetchTask("s", "a.bin", 10)
        assertEquals("排队中", queued.label)
    }

    /** 下载中的文件要能喂给气泡的"接收中 X%"（伪装成一条 receive 传输）。 */
    @Test
    fun activeFetchFeedsTheReceiveProgressUi() {
        val active = FetchTask("s1", "进度测试.bin", 100, got = 25, state = FetchState.Downloading)
        val idle = FetchTask("s2", "还没下.bin", 100, state = FetchState.Queued)
        val rows = fetchTransfers(listOf(active, idle))
        assertEquals("只有正在下载的才占「接收中」那一行", 1, rows.size)
        assertEquals("receive", rows[0].optString("direction"))
        assertEquals("25%", rows[0].optDouble("percent", 0.0).toInt().toString() + "%")
        val items = rows[0].optJSONArray("items")
        val names = (0 until (items?.length() ?: 0)).mapNotNull { items?.optJSONObject(it)?.optString("name") }
        assertTrue("名字要能对得上气泡：$names", names.contains("进度测试.bin"))
    }

    /** 完成后的字节数要如实记（不要写 0）：界面上的"已下载 X/总 Y"靠它。 */
    @Test
    fun finishedFetchKeepsTheRealByteCount() {
        val buffer = ByteArrayOutputStream()
        buffer.write(ByteArray(1234))
        val task = FetchTask("s", "a.bin", 4096, got = 1234, state = FetchState.Done)
        assertEquals(1234L, task.got)
        assertTrue(task.percent > 0.3f && task.percent < 0.31f)
    }
}
