package cn.eversend.app

import android.app.Application
import android.util.Log
import androidx.emoji2.text.EmojiCompat
import androidx.emoji2.text.DefaultEmojiCompatConfig

/**
 * 应用入口：把 EmojiCompat 初始化好。
 *
 * Android 11 以下的系统字体不带彩色 emoji（显示成黑白方块），EmojiCompat 用
 * 可下载/内置的字体补上。Android 11+ 本身就是彩色 emoji，这里几乎是空操作，
 * 但初始化一次没有代价，老设备也能正确显示。
 */
class EverSendApp : Application() {
    override fun onCreate() {
        super.onCreate()
        try {
            // 有可用的字体配置就初始化；没有（比如没有 Google Play 服务又没内置
            // 字体）就跳过，系统自己的 emoji 字体照样能用。
            DefaultEmojiCompatConfig.create(this)?.let { config -> EmojiCompat.init(config) }
        } catch (problem: Exception) {
            Log.d("EverSend", "EmojiCompat 初始化跳过: ${problem.message}")
        }
    }
}
