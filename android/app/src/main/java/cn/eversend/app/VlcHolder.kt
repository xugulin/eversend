package cn.eversend.app

import android.content.Context
import android.net.Uri
import android.util.Log
import org.videolan.libvlc.LibVLC
import org.videolan.libvlc.Media
import org.videolan.libvlc.MediaPlayer
import org.videolan.libvlc.util.VLCVideoLayout

/**
 * 内置播放内核（libVLC）—— 一个进程里只需要一个实例。
 *
 * 为什么不用系统的 `MediaPlayer` / `VideoView`：安卓并不保证能解 MKV、HEVC、
 * VP9 这类格式，而 EverSend 收到的媒体来自各种设备与浏览器 —— 网页录的是
 * webm/opus、App 录的是 m4a、用户发的可能是手机拍的 HEVC。系统解不了就只会
 * 报一句"播放失败"，用户看到的是"这软件不能放视频"。libVLC 自带 FFmpeg
 * （LGPL，动态链接），软解这些格式，不依赖设备能力。
 *
 * 一个 `LibVLC` 实例可以带多个 `MediaPlayer`，但每个播放器都要 `release()`，
 * 所以这里只共享最外层的引擎，播放器由调用方（Composable）持有并在离开时停掉。
 */
object VlcHolder {

    private const val TAG = "EverSend"

    @Volatile
    private var engine: LibVLC? = null

    /** 共享的 libVLC 引擎；初始化失败返回 null（界面要能解释原因）。 */
    fun engine(context: Context): LibVLC? {
        engine?.let { return it }
        synchronized(this) {
            engine?.let { return it }
            return try {
                val options = arrayListOf(
                    // 局域网直连，缓冲小一点更跟手；解码交给 FFmpeg。
                    "--network-caching=800",
                    "--no-drop-late-frames",
                    "--no-skip-frames",
                )
                LibVLC(context.applicationContext, options).also { engine = it }
            } catch (problem: Throwable) {
                Log.d(TAG, "libVLC 初始化失败: ${problem.message}")
                null
            }
        }
    }

    /** 一段音频（语音消息）的播放器，随时可以 stop 再来一条。 */
    class Player internal constructor(private val player: MediaPlayer, private val vlc: LibVLC) {
        private var current: Media? = null

        fun play(url: String, onFinished: () -> Unit) {
            stop()
            val media = Media(vlc, Uri.parse(url))
            // 硬件解码优先，失败自动回落到软件解码（libVLC 自己会试）。
            media.setHWDecoderEnabled(true, false)
            current = media
            player.media = media
            media.release()
            player.setEventListener { event ->
                if (event.type == MediaPlayer.Event.EndReached ||
                    event.type == MediaPlayer.Event.EncounteredError
                ) {
                    onFinished()
                }
            }
            player.play()
        }

        fun stop() {
            try {
                player.stop()
            } catch (ignored: Throwable) {
            }
            current = null
        }
    }

    fun player(context: Context, onError: (String) -> Unit): Player? {
        val vlc = engine(context) ?: run {
            onError("内置播放器初始化失败")
            return null
        }
        return try {
            Player(MediaPlayer(vlc), vlc)
        } catch (problem: Throwable) {
            onError(problem.message ?: problem.javaClass.simpleName)
            null
        }
    }

    /**
     * 视频：把播放器挂到 `VLCVideoLayout` 上。
     *
     * `attachViews(layout, null, false, false)` 的意思是"不要内置的播放控件、
     * 不要自动缩放窗口"——进度条与全屏由我们自己的界面控制。
     */
    fun attach(context: Context, layout: VLCVideoLayout, url: String, onError: (String) -> Unit) {
        val vlc = engine(context) ?: run {
            onError("内置播放器初始化失败")
            return
        }
        try {
            val player = MediaPlayer(vlc)
            player.attachViews(layout, null, false, false)
            val media = Media(vlc, Uri.parse(url))
            media.setHWDecoderEnabled(true, false)
            player.media = media
            media.release()
            player.setEventListener { event ->
                if (event.type == MediaPlayer.Event.EncounteredError) {
                    onError("这个文件内置解码器也打不开")
                }
            }
            player.play()
            layout.tag = player
        } catch (problem: Throwable) {
            onError(problem.message ?: problem.javaClass.simpleName)
        }
    }

    /** 视频对话框关掉时要把播放器停掉，否则声音会继续放。 */
    fun detach(layout: VLCVideoLayout) {
        val player = layout.tag as? MediaPlayer ?: return
        try {
            player.stop()
            player.detachViews()
            player.release()
        } catch (ignored: Throwable) {
        }
        layout.tag = null
    }
}
