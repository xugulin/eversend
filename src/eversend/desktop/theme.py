"""Visual theme: colours, the Qt stylesheet and small painting helpers.

Kept in one place so the whole application has a single look, and so the
light/dark choice is made once from the platform palette rather than being
hard-coded per widget.
"""

from __future__ import annotations

import os
import re
import tempfile

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

#: 设计语言取自 harness 的 Web 界面（--dsh-boot-* / --dsw-alias-*）：
#: 近黑画布 + 发丝级描边 + 近白文字，强调色是**单色**（不是另一个色相）。
#: 深色下「强调」是一块近白实心（配深色字），浅色下整块反过来 —— 这也是
#: harness 的做法：浅色的 brand 色就是近黑。
ACCENT = "#f9fafb"
ACCENT_DIM = "#cfd3d6"
SUCCESS = "#3fb950"
WARNING = "#d29922"
DANGER = "#f85149"
INFO = "#58a6ff"

# ---------------------------------------------------------------------------
# 设计 token —— 见 docs/ENTERPRISE_CHARTER.md §2
#
# 三个客户端（电脑端 / 网页版 / 安卓）共用同一组数字。**排版不要写裸数字**：
# 间距取 SPACE 里的一档、圆角取 RADIUS、字号取 TYPE、控件高度取 CONTROL。
# 目的很实际 —— 之前每个控件各自 padding 6px/7px/9px，凑出来的界面就是用户说的
# 「排版丑陋」。一档一档取就不会歪。
# ---------------------------------------------------------------------------

#: 间距刻度，全部 4 的倍数。
SPACE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 20, "2xl": 24, "3xl": 32}
#: 圆角：控件 / 卡片 / 弹层 / 药丸。
RADIUS = {"control": 6, "card": 10, "overlay": 14, "pill": 999}
#: 字号（px）与字重。
TYPE = {
    "badge": (11, 400),
    "caption": (12, 400),
    "body": (13, 400),
    "body-strong": (13, 600),
    "subtitle": (15, 600),
    "title": (17, 600),
    "display": (22, 700),
}
#: 控件高度。
CONTROL = {"sm": 26, "md": 32, "lg": 38}
#: 侧栏宽度（电脑端与网页版宽屏一致）。
SIDEBAR_WIDTH = 288
#: 表格行 / 列表行的行高。
ROW_HEIGHT = 44
#: 状态名 → 颜色。徽标、药丸、状态点按状态取色，三端同一套。
STATUS_COLORS = {
    "online": SUCCESS,
    "trusted": SUCCESS,
    "success": SUCCESS,
    "done": SUCCESS,
    "warn": WARNING,
    "warning": WARNING,
    "pending": WARNING,
    "danger": DANGER,
    "failed": DANGER,
    "error": DANGER,
    "info": INFO,
    # 传输状态：进行中用信息蓝（不是"成功"也不是"警告"），取消用警告黄
    # —— 用户主动取消不算故障，红色会让人以为出错了。
    "active": INFO,
    "running": INFO,
    "cancelled": WARNING,
    "canceled": WARNING,
    "offline": "#a1a1aa",
    "idle": "#a1a1aa",
}


def font(key: str) -> tuple[int, int]:
    """Look up a type-scale step as ``(px, weight)``."""
    return TYPE[key]


# ---------------------------------------------------------------------------
# 配色表 —— **样式表与运行时绘画的唯一来源**
#
# 以前颜色只活在 ``stylesheet()`` 的局部变量里，于是"用 QSS 画的"和"用 QPainter
# 画的"是两份值，迟早对不上（传输表的进度条、状态药丸描边、空状态图标都是画的）。
# 现在两边都从这里取：改一处，整个界面跟着变。
#
# 深色是默认。层次是 canvas < surface < raised < overlay —— 画布最暗、越靠前越亮，
# 卡片才有"浮起来"的感觉（旧版画布和卡片只差 6 个亮度点，看着是平的）。
# ---------------------------------------------------------------------------
_SCHEMES: dict[bool, dict[str, str]] = {
    True: {
        "canvas": "#0f0f11",
        "surface": "#151517",
        "raised": "#1b1b1f",
        "overlay": "#232329",
        "surface_alt": "#232329",
        "border": "#2a2a30",
        "border_strong": "#3a3a42",
        "text": "#f4f4f5",
        "text_muted": "#a1a1aa",
        "text_faint": "#71717a",
        "hover": "#232329",
        "sel": "rgba(255, 255, 255, 0.08)",
        "accent": ACCENT,
        "accent_dim": ACCENT_DIM,
        "accent_fg": "#0f1115",
        "bubble_out": "#2e2e35",
        "bubble_out_line": "#43434c",
        "seg_track": "#202024",
        "seg_active": "#3a3a42",
        "progress_track": "#242429",
        "progress_chunk": "#6b6b76",
    },
    False: {
        "canvas": "#ffffff",
        "surface": "#ffffff",
        "raised": "#f7f7f8",
        "overlay": "#ffffff",
        "surface_alt": "#f0f1f3",
        "border": "#e4e4e7",
        "border_strong": "#d4d4d8",
        "text": "#18181b",
        "text_muted": "#52525b",
        "text_faint": "#71717a",
        "hover": "#f0f1f3",
        "sel": "rgba(15, 17, 21, 0.06)",
        # 浅色下强调色反过来用近黑，否则白底 + 近白按钮 = 字看不见。
        "accent": "#18181b",
        "accent_dim": "#2b2e33",
        "accent_fg": "#ffffff",
        "bubble_out": "#eceef1",
        "bubble_out_line": "#dcdfe3",
        "seg_track": "#f0f1f3",
        "seg_active": "#ffffff",
        "progress_track": "#eceef1",
        "progress_chunk": "#9aa0a8",
    },
}


def colors(dark: bool | None = None) -> dict[str, str]:
    """The full semantic palette for a scheme (default: the active one).

    Use this for anything painted at runtime (``paintEvent``, delegates, freshly
    built icons) so it always matches the stylesheet.  Do **not** snapshot it at
    import time: the user can switch theme while the app is running.
    """
    return dict(_SCHEMES[is_dark() if dark is None else bool(dark)])


def color(name: str, dark: bool | None = None) -> str:
    """One semantic colour by name, e.g. ``theme.color("border")``."""
    table = _SCHEMES[is_dark() if dark is None else bool(dark)]
    try:
        return table[name]
    except KeyError as exc:  # pragma: no cover - programming error
        raise KeyError(
            f"未知的主题色 {name!r}；可用：{', '.join(sorted(table))}"
        ) from exc


# ---------------------------------------------------------------------------
# 字体
#
# Qt 的默认字体是「Sans Serif 9pt」这个抽象名 —— 在 Linux 上解析到哪个字族完全
# 看 fontconfig，中文可能落到点阵/衬线体；Windows 上会落到宋体；macOS 上是
# Helvetica。三个平台的界面因此长得不一样，中文尤其明显。
#
# 这里给一个**有序**字族表：拉丁优先用 UI 无衬线，中文按三个平台各自的系统字体
# 排下来，最后兜底。Qt 6 的 ``QFont.setFamilies`` 会按顺序回退，所以一个字族
# 覆盖不到的字（中英混排就是这样）会自动去下一个取。
# ---------------------------------------------------------------------------

#: UI 字体栈（按优先级）。前几个是拉丁字形，中间是三个平台各自的中文字体。
UI_FONT_STACK: tuple[str, ...] = (
    "Inter",
    "Segoe UI Variable Text",
    "Segoe UI",
    "PingFang SC",
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "Noto Sans SC",
    "WenQuanYi Micro Hei",
    "Noto Sans",
    "Helvetica Neue",
    "Arial",
    "sans-serif",
)

#: 等宽字体栈：预览里的文本、校验和、地址、端口都用它。
MONO_FONT_STACK: tuple[str, ...] = (
    "JetBrains Mono",
    "Cascadia Mono",
    "SF Mono",
    "Consolas",
    "Menlo",
    "DejaVu Sans Mono",
    "Noto Sans Mono",
    "Liberation Mono",
    "monospace",
)

# ---------------------------------------------------------------------------
# 字号档（宪章 §3.4 设置 › 外观）
#
# 只缩放**字号**，不缩放间距与控件高度 —— 那是另一件事（密度），混在一起调
# 会让 44px 的行高配上 20px 的字，反而更难看。三档的倍率取得很克制：
# 紧凑 0.9 用来在小屏笔记本上多塞几行，宽松 1.1 给视力吃力的用户。
# ---------------------------------------------------------------------------

#: 档名 → 字号倍率。界面上的下拉按这个顺序显示。
SCALE_PRESETS: dict[str, float] = {"compact": 0.9, "standard": 1.0, "relaxed": 1.1}

#: 档名 → 中文标签（设置页直接用，三端同一套措辞）。
SCALE_LABELS: dict[str, str] = {"compact": "紧凑", "standard": "标准", "relaxed": "宽松"}

_scale: float = 1.0
_scale_name: str = "standard"


def set_scale(name: str | float) -> str:
    """Set the font scale.  Accepts a preset name or a raw multiplier.

    Returns the preset name in force (``"custom"`` for a raw multiplier).
    Callers must re-apply :func:`stylesheet` / :func:`application_font`
    afterwards — the theme does not touch a live ``QApplication`` itself.
    """
    global _scale, _scale_name
    if isinstance(name, str):
        key = name.strip().lower()
        if key in SCALE_PRESETS:
            _scale, _scale_name = SCALE_PRESETS[key], key
            return _scale_name
        try:
            name = float(key)
        except ValueError:
            _scale, _scale_name = 1.0, "standard"
            return _scale_name
    factor = max(0.75, min(1.5, float(name)))
    _scale = factor
    _scale_name = next(
        (k for k, v in SCALE_PRESETS.items() if abs(v - factor) < 0.001), "custom"
    )
    return _scale_name


def current_scale() -> float:
    """The font-size multiplier in force."""
    return _scale


def current_scale_name() -> str:
    """The preset name in force (``"custom"`` when a raw multiplier was set)."""
    return _scale_name


def scaled(px: float) -> int:
    """Scale a font size in px, never below 9 (below that Chinese is unreadable)."""
    return max(9, round(px * _scale))

#: 正文字号（pt）。QSS 里写 px，Qt 按 DPI 换算；这里给 QFont 用 pt。
UI_POINT_SIZE = 10.0


def _pick(stack: tuple[str, ...]) -> list[str]:
    """Existing families first, then the rest of the stack.

    A missing family is harmless (Qt falls through), but putting the ones that
    *do* exist first makes the common case resolve in one lookup.
    """
    try:
        from PySide6.QtGui import QFontDatabase

        available = set(QFontDatabase.families())
    except Exception:  # pragma: no cover - no QGuiApplication yet
        return list(stack)
    return [n for n in stack if n in available] + [n for n in stack if n not in available]


def application_font(point_size: float | None = None) -> QFont:
    """The UI font for this platform, with Chinese fallbacks wired up.

    Call after ``QApplication`` exists (it reads the font database) and hand the
    result to ``QApplication.setFont``.  **Re-call it after**
    :func:`set_scale` — the scale is applied here, not inside the stylesheet
    alone (the stylesheet only covers widgets that set a font-size).
    """
    font = QFont()
    font.setFamilies(_pick(UI_FONT_STACK))
    base = UI_POINT_SIZE if point_size is None else point_size
    font.setPointSizeF(round(base * _scale, 2))
    font.setStyleStrategy(QFont.PreferAntialias)
    return font


def mono_font(point_size: float | None = None) -> QFont:
    """The monospace font, for checksums, addresses and the text preview."""
    font = QFont()
    font.setFamilies(_pick(MONO_FONT_STACK))
    base = UI_POINT_SIZE if point_size is None else point_size
    font.setPointSizeF(round(base * _scale, 2))
    return font


#: 样式表里 `font-size: 13px` 这种字面量。切换字号档时整体乘一遍。
_FONT_SIZE_RE = re.compile(r"font-size:\s*(\d+(?:\.\d+)?)px")


def _apply_font_scale(qss: str) -> str:
    """Multiply every ``font-size: Npx`` in the stylesheet by the current scale.

    Qt 的样式表没有全局缩放，而字号是**写死在每条规则里**的（``QLabel#Title`` 17px、
    ``QPushButton`` 13px …）。与其把每一处都改成 ``{scaled(17)}``（改一处漏一处，
    以后新加一条规则又忘了），不如在**最后统一过一遍正则** ——
    这样任何人往样式表里新加 ``font-size`` 都自动跟着档位走。

    只缩字号，**不缩间距、圆角、控件高度**：那是"密度"，和"字号"是两件事，
    混在一起调会得到"20px 的字配 44px 的行高"这种更难看的组合。
    """
    if abs(_scale - 1.0) < 0.001:
        return qss
    return _FONT_SIZE_RE.sub(lambda m: f"font-size: {scaled(float(m.group(1)))}px", qss)


def ui_font_families() -> str:
    """The UI stack as a QSS ``font-family`` value (quoted, comma separated)."""
    return ", ".join(f'"{name}"' for name in UI_FONT_STACK)


def mono_font_families() -> str:
    """The mono stack as a QSS ``font-family`` value."""
    return ", ".join(f'"{name}"' for name in MONO_FONT_STACK)


def is_dark() -> bool:
    """Dark by default, like the harness web UI.

    The palette below follows the DSH web client's tokens (``--dsh-boot-*`` /
    ``--dsw-alias-*``): a near-black canvas, hairline borders, near-white text
    and a **monochrome** accent — no second hue competing with the content.
    Set ``EVERSEND_THEME=light`` (or ``=dark``) to force one.
    """
    forced = os.environ.get("EVERSEND_THEME", "").strip().lower()
    if forced in ("dark", "light"):
        return forced == "dark"
    return True


# --------------------------------------------------------------------------
# 运行时画出来的小图标
#
# 复选框的勾、下拉箭头这些小东西，Qt 只有在你自己给 image: 的时候才画得
# 好看；不给的话样式表一接管，它们就退回样式引擎，深浅两套里总有一套难看。
# 与其往仓库里塞 png，不如按当前配色现画一张 —— 这个项目连应用图标都是
# 运行时画的（见 app_icon）。
# --------------------------------------------------------------------------

_assets_cache: dict[str, str] = {}


def _render_png(name: str, size: tuple[int, int], paint) -> str:
    """Paint one small PNG into a cache directory and return its path.

    Returns "" when there is no QGuiApplication yet (import time), which makes
    the stylesheet fall back to the style engine's own indicators.
    """
    key = f"{name}-{size[0]}x{size[1]}"
    cached = _assets_cache.get(key)
    if cached:
        return cached
    try:
        from PySide6.QtGui import QImage

        if QApplication.instance() is None:
            return ""
        image = QImage(size[0], size[1], QImage.Format_ARGB32)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, True)
        paint(painter, size)
        painter.end()
        folder = os.path.join(tempfile.gettempdir(), "eversend-theme")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, key + ".png")
        if not image.save(path):
            return ""
        _assets_cache[key] = path.replace(os.sep, "/")
        return _assets_cache[key]
    except Exception:  # pragma: no cover - painting is best effort
        return ""


def _check_icon(colour: str) -> str:
    def paint(painter, size) -> None:
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPen

        pen = QPen(QColor(colour))
        pen.setWidthF(1.8)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        w, h = size
        painter.drawPolyline(
            [
                QPointF(w * 0.20, h * 0.52),
                QPointF(w * 0.42, h * 0.74),
                QPointF(w * 0.80, h * 0.28),
            ]
        )

    return _render_png(f"check-{colour.lstrip('#')}", (12, 12), paint)


def _chevron_icon(colour: str, up: bool) -> str:
    def paint(painter, size) -> None:
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPen

        pen = QPen(QColor(colour))
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        w, h = size
        tips = (0.3, 0.7) if up else (0.7, 0.3)
        painter.drawPolyline(
            [
                QPointF(w * 0.15, h * tips[0]),
                QPointF(w * 0.5, h * tips[1]),
                QPointF(w * 0.85, h * tips[0]),
            ]
        )

    return _render_png(f"chevron-{'up' if up else 'down'}-{colour.lstrip('#')}", (10, 10), paint)


def stylesheet(dark: bool) -> str:
    """The application stylesheet for the given scheme."""
    # 颜色只有一份，在 _SCHEMES 里 —— 见 colors()。这里只做改名，不再有第二份字面量。
    c = _SCHEMES[bool(dark)]
    bg = c["canvas"]
    surface = c["surface"]
    raised = c["raised"]
    surface_alt = c["surface_alt"]
    border = c["border"]
    border_strong = c["border_strong"]
    text = c["text"]
    text_dim = c["text_muted"]
    hover = c["hover"]
    bubble_out = c["bubble_out"]
    bubble_out_line = c["bubble_out_line"]
    sel = c["sel"]
    accent = c["accent"]
    accent_dim = c["accent_dim"]
    accent_fg = c["accent_fg"]
    seg_track = c["seg_track"]
    seg_active = c["seg_active"]
    progress_track = c["progress_track"]
    progress_chunk = c["progress_chunk"]

    ui_fonts = ui_font_families()
    mono_fonts = mono_font_families()

    check = _check_icon(accent_fg)
    chevron = _chevron_icon(text_dim, up=False)
    chevron_up = _chevron_icon(text_dim, up=True)
    chevron_down = chevron

    qss = f"""
    QWidget {{
        background: {bg};
        color: {text};
        font-family: {ui_fonts};
        font-size: 13px;
    }}
    QMainWindow, QDialog {{ background: {bg}; }}

    /* QWidget 那条规则会把画布底色画到每个子控件上，于是卡片里的标签会自己
       刷出一块比卡片更暗的方块。文字控件一律透明，需要底色的（头像、徽标）
       在下面单独给。 */
    QLabel, QCheckBox, QRadioButton, QAbstractButton#Ghost {{ background: transparent; }}

    QFrame#Card {{
        background: {surface};
        border: 1px solid {border};
        border-radius: 10px;
    }}
    QLabel#Title {{ font-size: 17px; font-weight: 600; }}
    QLabel#Subtitle {{ color: {text_dim}; font-size: 12px; }}
    QLabel#Dim {{ color: {text_dim}; }}
    QLabel#Mono, QLabel#MonoValue {{ font-family: {mono_fonts}; }}

    QPushButton {{
        background: {surface_alt};
        border: 1px solid {border};
        border-radius: 7px;
        padding: 6px 14px;
        min-height: 20px;
    }}
    QPushButton:hover {{ background: {hover}; }}
    QPushButton:pressed {{ background: {border}; }}
    QPushButton:disabled {{ color: {text_dim}; background: {surface_alt}; }}
    /* 主按钮沿用 harness 的「近白实心块 + 深色字」，而不是另一个色相的蓝。 */
    QPushButton#Primary {{
        background: {accent};
        border: 1px solid {accent};
        color: {accent_fg};
        font-weight: 600;
    }}
    QPushButton#Primary:hover {{ background: {accent_dim}; border-color: {accent_dim}; }}
    QPushButton#Primary:disabled {{ background: {border}; border-color: {border}; color: {text_dim}; }}
    QPushButton#Danger {{ background: {DANGER}; border-color: {DANGER}; color: #ffffff; font-weight: 600; }}

    QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {{
        background: {surface};
        border: 1px solid {border};
        border-radius: 7px;
        padding: 6px 9px;
        selection-background-color: {accent};
        selection-color: {accent_fg};
    }}
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {accent_dim}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox::down-arrow {{ image: url("{chevron}"); width: 10px; height: 10px; }}
    QSpinBox::up-button, QSpinBox::down-button {{ width: 18px; background: transparent; border: none; }}
    QSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; }}
    QSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; }}
    QSpinBox::up-arrow {{ image: url("{chevron_up}"); width: 10px; height: 10px; }}
    QSpinBox::down-arrow {{ image: url("{chevron_down}"); width: 10px; height: 10px; }}
    QCheckBox::indicator {{
        width: 15px; height: 15px;
        border: 1px solid {border};
        border-radius: 4px;
        background: {surface_alt};
    }}
    QCheckBox::indicator:hover {{ border-color: {text_dim}; }}
    QCheckBox::indicator:checked {{
        background: {accent};
        border-color: {accent};
        image: url("{check}");
    }}
    QCheckBox::indicator:disabled {{ background: {bg}; border-color: {border}; }}
    QRadioButton::indicator {{ width: 14px; height: 14px; border: 1px solid {border}; border-radius: 7px; }}
    QRadioButton::indicator:checked {{ background: {accent}; border-color: {accent}; }}

    QTabWidget::pane {{ border: 1px solid {border}; border-radius: 10px; background: {surface}; top: -1px; }}
    QTabBar::tab {{
        background: transparent;
        padding: 8px 18px;
        margin-right: 3px;
        border: 1px solid transparent;
        border-top-left-radius: 8px;
        border-top-right-radius: 8px;
        color: {text_dim};
    }}
    QTabBar::tab:selected {{ background: {surface}; border-color: {border}; border-bottom-color: {surface}; color: {text}; font-weight: 600; }}
    QTabBar::tab:hover:!selected {{ color: {text}; }}

    /* One big card per device: a large glyph, a wrapping name, two detail
       lines and a status chip.  Telling two devices apart has to be possible
       at a glance -- picking the wrong one sends a file to the wrong person. */
    QListWidget#DeviceTable {{
        background: {surface};
        border: 1px solid {border};
        border-radius: 8px;
        padding: 6px;
        outline: none;
    }}
    QListWidget#DeviceTable::item {{
        border: 1px solid transparent;
        border-radius: 8px;
        margin: 2px 0;
    }}
    QListWidget#DeviceTable::item:hover {{ background: {hover}; }}
    QListWidget#DeviceTable::item:selected {{
        background: {sel};
        border-color: {accent_dim};
    }}
    /* 聊天气泡：自己发的靠右、用品牌色底；别人发的靠左、用中性底。
       objectName 早就在 chat_view 里设了（BubbleIn/BubbleOut），但样式表里
       一直没有对应的规则 —— 于是两边长得一模一样，用户根本分不清哪句是自己
       说的。（左右对齐本来就是对的，缺的是颜色。） */
    QFrame#BubbleOut {{
        background: {bubble_out};
        border: 1px solid {bubble_out_line};
        border-radius: 12px;
    }}
    QFrame#BubbleIn {{
        background: {surface_alt};
        border: 1px solid {border};
        border-radius: 12px;
    }}
    QLabel#BubbleMeta {{ color: {text_dim}; font-size: 11px; }}
    QLabel#BubbleState {{ color: {text_dim}; font-size: 11px; }}
    QLabel#BubbleImage {{ border-radius: 8px; }}

    /* 会话列表：头像块 + 两行文字 + 未读小红点（QQ 那种排布） */
    QWidget#ConvRow {{ background: transparent; }}
    QLabel#ConvAvatar {{
        background: {surface_alt};
        border: 1px solid {border};
        border-radius: 10px;
        font-size: 20px;
    }}
    QLabel#ConvName {{ font-size: 14px; font-weight: 600; color: {text}; }}
    QLabel#ConvPreview {{ color: {text_dim}; font-size: 12px; }}
    QLabel#ConvTime {{ color: {text_dim}; font-size: 11px; }}
    QLabel#ConvBadge {{
        background: {DANGER}; color: #ffffff; border-radius: 9px;
        font-size: 11px; font-weight: 600; min-width: 18px; max-height: 18px;
    }}
    QListWidget#ConvList {{ background: transparent; border: 0; }}
    QListWidget#ConvList::item {{ border-radius: 8px; }}
    QListWidget#ConvList::item:hover {{ background: {hover}; }}
    QListWidget#ConvList::item:selected {{ background: {sel}; }}

    QFrame#DeviceCard {{
        background: {surface_alt};
        border: 1px solid {border};
        border-radius: 10px;
    }}
    QLabel#DeviceGlyph {{ font-size: 34px; }}
    QLabel#DeviceName {{ font-size: 15px; font-weight: 600; color: {text}; }}
    QLabel#DeviceDetail {{ font-size: 12px; color: {text_dim}; }}
    QLabel#DeviceStatus {{
        font-size: 12px;
        padding: 2px 8px;
        border-radius: 9px;
        border: 1px solid {border};
        color: {text_dim};
    }}
    QLabel#DeviceStatus[state="online"] {{ color: {SUCCESS}; border-color: {SUCCESS}; }}
    QLabel#DeviceStatus[state="offline"] {{ color: {text_dim}; border-color: {border}; }}

    QTableWidget, QListWidget, QTreeWidget {{
        background: {surface};
        border: 1px solid {border};
        border-radius: 8px;
        gridline-color: {border};
        selection-background-color: {sel};
        selection-color: {text};
        outline: none;
    }}
    QHeaderView::section {{
        background: {surface_alt};
        border: none;
        border-bottom: 1px solid {border};
        padding: 7px 8px;
        font-weight: 600;
    }}
    QTableWidget::item {{ padding: 4px; }}

    QProgressBar {{
        background: {progress_track};
        border: 1px solid {border};
        border-radius: 6px;
        height: 18px;
        text-align: center;
        font-size: 11px;
        color: {text};
    }}
    QProgressBar::chunk {{ background: {progress_chunk}; border-radius: 5px; }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {border}; border-radius: 5px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {text_dim}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; }}
    QScrollBar::handle:horizontal {{ background: {border}; border-radius: 5px; min-width: 30px; }}

    QStatusBar {{ background: {bg}; border-top: 1px solid {border}; color: {text_dim}; }}
    QMenuBar {{ background: {bg}; }}
    QMenuBar::item:selected {{ background: {hover}; }}
    QMenu {{ background: {raised}; border: 1px solid {border_strong}; padding: 4px; }}
    QMenu::item:selected {{ background: {sel}; }}
    QSplitter::handle {{ background: {border}; }}
    QToolTip {{ background: {surface}; color: {text}; border: 1px solid {border}; padding: 4px; }}
    QCheckBox {{ spacing: 7px; }}
    QGroupBox {{
        border: 1px solid {border};
        border-radius: 9px;
        margin-top: 12px;
        padding-top: 10px;
        font-weight: 600;
    }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 5px; }}

    /* ==================================================================
       侧栏 + 主区：harness 的骨架
       ------------------------------------------------------------------
       左边一条固定宽度的栏（品牌在顶、操作在其次、设备列表在中间、
       传输/设置在底部），右边一大片主区（对方设备详情在顶、会话在中间、
       输入区在底）。两者之间是一条发丝线，不是卡片、不圆角。
       ================================================================== */
    QFrame#Sidebar {{
        background: {bg};
        border-right: 1px solid {border};
    }}
    QWidget#SidebarBody {{ background: transparent; }}
    QLabel#BrandName {{ font-size: 15px; font-weight: 600; }}
    QLabel#BrandSub {{ color: {text_dim}; font-size: 11px; }}
    QLabel#SectionTitle {{ color: {text_dim}; font-size: 12px; font-weight: 600; }}
    QLabel#CountBadge {{
        background: {surface_alt};
        border: 1px solid {border};
        border-radius: 9px;
        padding: 0 6px;
        color: {text_dim};
        font-size: 11px;
    }}
    /* 设备列表的分段标题（在线 / 离线）：整行可点，带 ▾/▸ 与数量 */
    QPushButton#SectionToggle {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: 7px;
        padding: 0;
        text-align: left;
    }}
    QPushButton#SectionToggle:hover {{ background: {hover}; }}
    QPushButton#SectionToggle:checked {{ background: transparent; }}

    /* 侧栏底部的导航项（传输 / 设置）：整行可点，选中时是 harness 那种浅色底 */
    QPushButton#NavItem {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: 8px;
        padding: 8px 10px;
        text-align: left;
        color: {text_dim};
    }}
    QPushButton#NavItem:hover {{ background: {hover}; color: {text}; }}
    QPushButton#NavItem[active="true"] {{
        background: {sel};
        color: {text};
        font-weight: 600;
        border-color: {border};
    }}
    QPushButton#NavBadge {{
        background: {accent};
        color: {accent_fg};
        border: none;
        border-radius: 9px;
        padding: 0 6px;
        font-size: 11px;
        font-weight: 600;
        min-width: 16px;
    }}
    /* 图标按钮：设备操作那一排，方形、无字、hover 才出现底色 */
    QPushButton#IconButton {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: 8px;
        padding: 5px;
        min-width: 30px;
        min-height: 26px;
    }}
    QPushButton#IconButton:hover {{ background: {hover}; border-color: {border}; }}
    QPushButton#IconButton:pressed {{ background: {border}; }}

    /* 主区顶部的对方设备详情 */
    QFrame#PeerHeader {{
        background: {bg};
        border-bottom: 1px solid {border};
    }}
    QLabel#PeerAvatar {{
        background: {surface_alt};
        border: 1px solid {border};
        border-radius: 12px;
        font-size: 22px;
    }}
    QLabel#PeerName {{ font-size: 16px; font-weight: 600; }}
    QLabel#PeerDetail {{ color: {text_dim}; font-size: 12px; }}
    QLabel#Chip {{
        border: 1px solid {border};
        border-radius: 9px;
        padding: 1px 8px;
        color: {text_dim};
        font-size: 11px;
    }}
    QLabel#Chip[state="online"] {{ color: {SUCCESS}; border-color: {SUCCESS}; }}
    QLabel#Chip[state="warn"] {{ color: {WARNING}; border-color: {WARNING}; }}
    QLabel#Chip[state="offline"] {{ color: {text_dim}; border-color: {border}; }}
    QLabel#Chip[state="trusted"] {{ color: {SUCCESS}; border-color: {SUCCESS}; }}

    /* 分段控件：[会话｜传输]、[接收｜发送] 就地切换 */
    QFrame#Segmented {{
        background: {seg_track};
        border: 1px solid {border};
        border-radius: 9px;
    }}
    QPushButton#Segment {{
        background: transparent;
        border: none;
        border-radius: 7px;
        padding: 5px 14px;
        color: {text_dim};
    }}
    QPushButton#Segment:hover {{ color: {text}; }}
    QPushButton#Segment:checked {{
        background: {seg_active};
        color: {text};
        font-weight: 600;
    }}

    /* 输入区：一块圆角卡片（harness 的输入框就是一块内嵌的圆角面板） */
    QFrame#Composer {{ background: {bg}; border-top: 1px solid {border}; }}
    QFrame#ComposerBox {{
        background: {surface};
        border: 1px solid {border};
        border-radius: 14px;
    }}
    QLineEdit#ComposerInput {{
        background: transparent;
        border: none;
        padding: 6px 2px;
    }}
    /* 表情面板：输入区里的一块内嵌面板（不是弹窗），宽度跟着输入区走 */
    QFrame#EmojiPad {{
        background: {surface};
        border: 1px solid {border};
        border-radius: 12px;
    }}
    QScrollArea#EmojiScroll {{ background: transparent; border: none; }}
    /* 尺寸写在这里而不是 setFixedSize：样式表里的 min-height 会盖掉控件的
       fixed size，之前写成 min-height: 0，按钮被压成 2 px 高、表情全看不见。 */
    QFrame#EmojiPad QPushButton {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: 8px;
        /* 表情要够大：17px 那版有人反馈看不清（见 UI_LAYOUT §3⑥）。 */
        font-size: 26px;
        padding: 0;
        min-width: 44px;
        max-width: 44px;
        min-height: 42px;
        max-height: 42px;
    }}
    QFrame#EmojiPad QPushButton:hover {{ background: {hover}; border-color: {border}; }}

    QFrame#AttachmentBar {{
        background: {surface_alt};
        border: 1px dashed {border};
        border-radius: 10px;
    }}
    QFrame#AttachmentBar[active="true"] {{
        border: 1px dashed {accent};
        background: {hover};
    }}
    QFrame#AttachChip {{
        background: {surface};
        border: 1px solid {border};
        border-radius: 8px;
    }}
    /* 待发文件：一块一块（上面缩略图、下面文件名），像相册那样排开 */
    QFrame#AttachTile {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: 10px;
    }}
    QFrame#AttachTile:hover {{ background: {hover}; border-color: {border}; }}
    QFrame#AttachThumb {{
        background: {surface};
        border: 1px solid {border};
        border-radius: 8px;
    }}
    QLabel#AttachPicture {{ background: transparent; }}
    QPushButton#TileRemove {{
        background: {surface_alt};
        border: 1px solid {border};
        border-radius: 9px;
        color: {text_dim};
        padding: 0;
        font-size: 10px;
        min-height: 0;
    }}
    QPushButton#TileRemove:hover {{ color: {DANGER}; border-color: {DANGER}; }}
    QScrollArea#AttachScroll {{ background: transparent; border: none; }}
    QLineEdit#PinEdit {{ max-width: 110px; }}

    /* 文件预览对话框（desktop/preview.py） */
    QLabel#PreviewCanvas {{
        background: {surface_alt};
        border: 1px solid {border};
        border-radius: 10px;
    }}
    QFrame#PreviewEmpty {{
        background: {surface_alt};
        border: 1px solid {border};
        border-radius: 10px;
    }}
    QPlainTextEdit#PreviewText {{
        background: {surface};
        border: 1px solid {border};
        border-radius: 10px;
        padding: 8px;
    }}
    QLabel#AttachName {{ font-size: 12px; }}
    QLabel#AttachSize {{ color: {text_dim}; font-size: 11px; }}
    QLabel#StageHint {{ color: {text_dim}; font-size: 12px; }}
    QFrame#AttachChip[selected="true"] {{ border-color: {accent_dim}; background: {hover}; }}
    QPushButton#ChipRemove {{
        background: transparent;
        border: none;
        color: {text_dim};
        padding: 0;
        min-height: 16px;
    }}
    QPushButton#ChipRemove:hover {{ color: {DANGER}; }}
    /* 表格里的小按钮（取消 / 打开所在位置）：别把行撑高 */
    QPushButton#CellAction {{ padding: 2px 8px; font-size: 12px; min-height: 16px; }}
    QTableWidget#TransferTable, QTableView#TransferTable, QTreeView#TransferTable {{
        border: 1px solid {border};
        border-radius: 10px;
        background: {surface};
    }}

    QFrame#SettingsHeader {{ background: {bg}; border-bottom: 1px solid {border}; }}
    QLabel#EmptyTitle {{ font-size: 15px; font-weight: 600; }}
    QLabel#EmptyHint {{ color: {text_dim}; font-size: 12px; }}

    /* ==================================================================
       企业级打磨层（docs/ENTERPRISE_CHARTER.md §2）
       上面的规则是原有外观，不要动；这一层补的是"缺状态"和"新组件"——
       没有 :focus 的输入框、没有 :disabled 的菜单、没有选中态的表格，
       用起来就是用户说的"粗糙"。
       ================================================================== */

    /* -- 焦点环：键盘用户必须看得出焦点在哪（可访问性基线） ---------- */
    QPushButton:focus, QToolButton:focus {{ outline: none; }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
        border: 1px solid {accent_dim};
    }}
    QListWidget:focus, QTableWidget:focus, QTreeWidget:focus {{ border-color: {accent_dim}; }}

    /* -- 次要按钮：和主按钮配对使用，不该长得像主按钮 ---------------- */
    QPushButton#Ghost {{
        background: transparent;
        border: 1px solid transparent;
        color: {text_dim};
        padding: 5px 10px;
    }}
    QPushButton#Ghost:hover {{ background: {hover}; color: {text}; }}
    QPushButton#Ghost:pressed {{ background: {border}; }}

    /* -- 药丸 / 徽标：状态、计数、标签共用一套 ----------------------- */
    QLabel#Pill {{
        border: 1px solid {border};
        border-radius: 999px;
        padding: 2px 10px;
        font-size: 11px;
    }}
    QLabel#Pill[state="online"], QLabel#Pill[state="success"], QLabel#Pill[state="done"] {{
        color: {SUCCESS}; border-color: {SUCCESS};
    }}
    QLabel#Pill[state="warn"], QLabel#Pill[state="pending"] {{
        color: {WARNING}; border-color: {WARNING};
    }}
    QLabel#Pill[state="danger"], QLabel#Pill[state="failed"] {{
        color: {DANGER}; border-color: {DANGER};
    }}
    QLabel#Pill[state="info"] {{ color: {INFO}; border-color: {INFO}; }}
    QLabel#Pill[state="offline"], QLabel#Pill[state="idle"] {{ color: {text_dim}; }}

    /* -- 筛选药丸（传输页 全部/进行中/已完成/失败）-------------------- */
    QPushButton#FilterChip {{
        background: transparent;
        border: 1px solid {border};
        border-radius: 999px;
        padding: 4px 14px;
        font-size: 12px;
        color: {text_dim};
    }}
    QPushButton#FilterChip:hover {{ background: {hover}; color: {text}; }}
    QPushButton#FilterChip:checked {{
        background: {accent};
        border-color: {accent};
        color: {accent_fg};
        font-weight: 600;
    }}

    /* -- 工具栏 / 页头：一个横条，右侧动作 --------------------------- */
    QFrame#Toolbar {{ background: transparent; border: none; }}
    QFrame#PageHeader {{ background: {bg}; border-bottom: 1px solid {border}; }}

    /* -- 表格：补齐选中态、隔行、整行选中 ---------------------------- */
    QTableWidget::item {{ padding: 4px 8px; border: none; }}
    QTableWidget::item:hover {{ background: {hover}; }}
    QTableWidget::item:selected {{ background: {sel}; color: {text}; }}
    QTableWidget {{ alternate-background-color: {surface}; }}
    QHeaderView::section:hover {{ color: {text}; }}
    QHeaderView::section:checked {{ color: {text}; }}

    /* -- 菜单 / 提示：补上禁用态与内边距，别让文字贴边 ---------------- */
    QMenu {{ padding: 6px; }}
    QMenu::item {{ padding: 6px 26px 6px 12px; border-radius: 6px; font-size: 13px; }}
    QMenu::item:disabled {{ color: {text_dim}; }}
    QMenu::separator {{ height: 1px; background: {border}; margin: 5px 8px; }}
    QToolTip {{
        padding: 6px 9px;
        border-radius: 6px;
        font-size: 12px;
    }}

    /* -- 弹层：对话框、悬浮卡片 -------------------------------------- */
    QDialog {{ background: {bg}; }}
    QDialogButtonBox QPushButton {{ min-width: 76px; min-height: 28px; }}
    QFrame#Overlay {{ background: {surface}; border: 1px solid {border}; border-radius: 14px; }}

    /* -- 进度条：进行中不留白，文字居中可读 -------------------------- */
    QProgressBar {{ text-align: center; font-size: 11px; }}

    /* -- 滚动区：内容透明，别在卡片里刷一层更暗的底 ------------------ */
    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}

    /* -- 分隔线 ------------------------------------------------------ */
    QFrame#HLine {{ background: {border}; max-height: 1px; border: none; }}
    QFrame#VLine {{ background: {border}; max-width: 1px; border: none; }}

    /* -- 普通 QToolButton（不是自绘的 IconButton）-------------------- */
    QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 3px; }}
    QToolButton:hover {{ background: {hover}; border-color: {border}; }}
    QToolButton:pressed {{ background: {border}; }}
    QToolButton:disabled {{ color: {text_dim}; }}
    """
    return _apply_font_scale(qss)


#: 正文色 / 次要文字色（深色一套、浅色一套）。样式表里的同名变量只活在
#: ``stylesheet()`` 的 f-string 里，而"运行时画出来的图标"（desktop/icons.py）
#: 需要一个随时可问的颜色，所以在这里再放一份。
#: 兼容旧接口：正文色现在也在 _SCHEMES 里。
_TEXT_COLORS = {True: _SCHEMES[True]["text"], False: _SCHEMES[False]["text"]}


def current_text_color() -> str:
    """The body-text colour of the active scheme."""
    return _TEXT_COLORS[is_dark()]


_PLATFORM_GLYPHS = {    "linux": "🐧",
    "windows": "🪟",
    "macos": "",
    "android": "🤖",
    "ios": "📱",
    # A phone that reached us through the browser UI.
    "browser": "📱",
}


def platform_glyph(platform: str) -> str:
    return _PLATFORM_GLYPHS.get(platform, "💻")


def app_icon(size: int = 128, dark: bool = True) -> QIcon:
    """Draw the application icon at runtime.

    Generating it avoids shipping a binary asset and, more usefully, lets the
    icon adapt to the colour scheme instead of looking wrong in one of them.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)

    # 深色下是「近白方块 + 深色箭头」，浅色下反过来 —— 跟 harness 的单色标识一致。
    plate = QColor(ACCENT if dark else "#0f1115")
    glyph = QColor("#0f1115" if dark else "#f9fafb")
    painter.setBrush(plate)
    painter.setPen(Qt.NoPen)
    radius = size * 0.24
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

    # Two arrows passing each other: "sent" and "received".
    painter.setPen(glyph)
    font = QFont()
    font.setPointSizeF(size * 0.44)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "⇅")
    painter.end()
    return QIcon(pixmap)


__all__ = [
    "SCALE_LABELS",
    "SCALE_PRESETS",
    "application_font",
    "current_scale",
    "current_scale_name",
    "mono_font",
    "mono_font_families",
    "scaled",
    "set_scale",
    "ui_font_families",
    "CONTROL",
    "INFO",
    "RADIUS",
    "ROW_HEIGHT",
    "SIDEBAR_WIDTH",
    "SPACE",
    "STATUS_COLORS",
    "TYPE",
    "color",
    "colors",
    "font",
    "space",
    "ACCENT",
    "ACCENT_DIM",
    "DANGER",
    "SUCCESS",
    "WARNING",
    "app_icon",
    "current_text_color",
    "is_dark",
    "platform_glyph",
    "stylesheet",
]
