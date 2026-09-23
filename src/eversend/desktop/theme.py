"""Visual theme: colours, the Qt stylesheet and small painting helpers.

Kept in one place so the whole application has a single look, and so the
light/dark choice is made once from the platform palette rather than being
hard-coded per widget.
"""

from __future__ import annotations

import os
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
    if dark:
        # harness 深色：画布 #151517，层次依次 +2~4 个亮度点，描边是白色 12%
        bg = "#151517"
        surface = "#1b1b1e"
        surface_alt = "#232327"
        border = "#2f2f34"
        text = "#f9fafb"
        text_dim = "#adb2b8"
        hover = "#26262b"
        bubble_out = "#2e2e35"
        bubble_out_line = "#43434c"
        sel = "rgba(255, 255, 255, 0.08)"
        accent = ACCENT
        accent_dim = ACCENT_DIM
        accent_fg = "#0f1115"
    else:
        # harness 浅色：白底 + 近黑文字 + 黑色 10% 描边
        bg = "#ffffff"
        surface = "#ffffff"
        surface_alt = "#f5f6f7"
        border = "#e3e5e8"
        text = "#0f1115"
        text_dim = "#81858c"
        hover = "#f0f1f3"
        bubble_out = "#eceef1"
        bubble_out_line = "#dcdfe3"
        sel = "rgba(15, 17, 21, 0.06)"
        # 浅色下强调色反过来用近黑，否则白底 + 近白按钮 = 字看不见。
        accent = "#0f1115"
        accent_dim = "#2b2e33"
        accent_fg = "#f9fafb"

    check = _check_icon(accent_fg)
    chevron = _chevron_icon(text_dim, up=False)
    chevron_up = _chevron_icon(text_dim, up=True)
    chevron_down = chevron

    return f"""
    QWidget {{
        background: {bg};
        color: {text};
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
    QFrame#DropZone {{
        background: {surface_alt};
        border: 2px dashed {border};
        border-radius: 10px;
    }}
    QFrame#DropZone[active="true"] {{
        border: 2px dashed {accent};
        background: {hover};
    }}

    QLabel#Title {{ font-size: 17px; font-weight: 600; }}
    QLabel#Subtitle {{ color: {text_dim}; font-size: 12px; }}
    QLabel#Dim {{ color: {text_dim}; }}
    QLabel#Mono {{ font-family: monospace; }}

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
        background: {surface_alt};
        border: 1px solid {border};
        border-radius: 6px;
        height: 18px;
        text-align: center;
        font-size: 11px;
    }}
    QProgressBar::chunk {{ background: {accent}; border-radius: 5px; }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {border}; border-radius: 5px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {text_dim}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; }}
    QScrollBar::handle:horizontal {{ background: {border}; border-radius: 5px; min-width: 30px; }}

    QStatusBar {{ background: {bg}; border-top: 1px solid {border}; color: {text_dim}; }}
    QMenuBar {{ background: {bg}; }}
    QMenuBar::item:selected {{ background: {hover}; }}
    QMenu {{ background: {surface}; border: 1px solid {border}; padding: 4px; }}
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
    """


_PLATFORM_GLYPHS = {
    "linux": "🐧",
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
    "ACCENT",
    "ACCENT_DIM",
    "DANGER",
    "SUCCESS",
    "WARNING",
    "app_icon",
    "is_dark",
    "platform_glyph",
    "stylesheet",
]
