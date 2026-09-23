package cn.eversend.app

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/**
 * 界面配色：跟电脑端、网页版用的是同一套设计 token（harness 的 Web 界面）。
 *
 * 近黑画布 + 发丝级描边 + 近白文字，强调色是**单色**（近白实心块配深色字），
 * 不再有第二个色相抢视觉。深色是默认主题。
 *
 * 三个平台的颜色写在三个地方（theme.py / app.css / 这里），改一处就要三处一起改，
 * 所以值的来源都标了同一个名字：Es。
 */
object Es {
    /** 画布。 */
    val Bg = Color(0xFF151517)

    /** 卡片。 */
    val Surface = Color(0xFF1B1B1E)

    /** 卡片里的次级面（输入框、进度条槽、对方气泡）。 */
    val SurfaceAlt = Color(0xFF232327)

    /** 发丝级描边（harness 那边是白色 12%，这里是等价的实色）。 */
    val Border = Color(0xFF2F2F34)

    /** 正文、标题。 */
    val Text = Color(0xFFF9FAFB)

    /** 次要文字（说明、时间）。 */
    val Muted = Color(0xFFADB2B8)

    /** 三级文字（脚注、ID）。 */
    val Dim = Color(0xFF8B8F96)

    /** 单色强调：按钮底色、进度条、链接。 */
    val Accent = Color(0xFFF9FAFB)

    /** 强调色上的字。 */
    val AccentText = Color(0xFF0F1115)

    val Danger = Color(0xFFFF7B72)
    val Ok = Color(0xFF3FB950)
    val Warn = Color(0xFFD29922)

    /** 别人发的消息：压暗。 */
    val BubbleIn = SurfaceAlt

    /** 自己发的消息：提亮一点，一眼能分清哪句是自己说的。 */
    val BubbleOut = Color(0xFF2E2E35)
}

@Composable
fun EverSendTheme(content: @Composable () -> Unit) {
    // Material 组件（导航栏、输入框、按钮、Tab）自己会去取这些槽位，
    // 不填的话它们会按浅色主题画，深色下就是一块块白斑。
    val scheme = darkColorScheme(
        primary = Es.Accent,
        onPrimary = Es.AccentText,
        primaryContainer = Es.SurfaceAlt,
        onPrimaryContainer = Es.Text,
        secondary = Es.Muted,
        onSecondary = Es.AccentText,
        background = Es.Bg,
        onBackground = Es.Text,
        surface = Es.Surface,
        onSurface = Es.Text,
        surfaceVariant = Es.SurfaceAlt,
        onSurfaceVariant = Es.Muted,
        // Material3 1.3 的 Card / 菜单取的是 surfaceContainer* 这一组槽位，
        // 不填的话它们会回到基线深色（带一点紫），跟 harness 的灰不对味。
        surfaceDim = Es.Bg,
        surfaceBright = Color(0xFF35353B),
        surfaceContainerLowest = Color(0xFF101012),
        surfaceContainerLow = Es.Surface,
        surfaceContainer = Color(0xFF1F1F23),
        surfaceContainerHigh = Es.SurfaceAlt,
        surfaceContainerHighest = Color(0xFF2A2A30),
        inverseSurface = Es.Text,
        inverseOnSurface = Es.Bg,
        secondaryContainer = Es.SurfaceAlt,
        onSecondaryContainer = Es.Text,
        tertiary = Es.Muted,
        onTertiary = Es.AccentText,
        outline = Es.Border,
        outlineVariant = Es.Border,
        error = Es.Danger,
        onError = Es.AccentText,
    )
    MaterialTheme(colorScheme = scheme, content = content)
}
