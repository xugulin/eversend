"""Visual theme: colours, the Qt stylesheet and small painting helpers.

Kept in one place so the whole application has a single look, and so the
light/dark choice is made once from the platform palette rather than being
hard-coded per widget.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

#: Brand accent.  A calm blue that reads well on both light and dark surfaces.
ACCENT = "#2f81f7"
ACCENT_DIM = "#1f6feb"
SUCCESS = "#3fb950"
WARNING = "#d29922"
DANGER = "#f85149"


def is_dark() -> bool:
    """Whether the platform is using a dark colour scheme."""
    app = QApplication.instance()
    if app is None:
        return True
    window = app.palette().window().color()
    # Perceived luminance; the 128 midpoint matches what most desktops use.
    luminance = 0.299 * window.red() + 0.587 * window.green() + 0.114 * window.blue()
    return luminance < 128


def stylesheet(dark: bool) -> str:
    """The application stylesheet for the given scheme."""
    if dark:
        bg = "#0d1117"
        surface = "#161b22"
        surface_alt = "#1c2129"
        border = "#30363d"
        text = "#e6edf3"
        text_dim = "#8b949e"
        hover = "#21262d"
    else:
        bg = "#f6f8fa"
        surface = "#ffffff"
        surface_alt = "#f0f3f6"
        border = "#d0d7de"
        text = "#1f2328"
        text_dim = "#59636e"
        hover = "#eaeef2"

    return f"""
    QWidget {{
        background: {bg};
        color: {text};
        font-size: 13px;
    }}
    QMainWindow, QDialog {{ background: {bg}; }}

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
        border: 2px dashed {ACCENT};
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
    QPushButton#Primary {{
        background: {ACCENT};
        border: 1px solid {ACCENT_DIM};
        color: #ffffff;
        font-weight: 600;
    }}
    QPushButton#Primary:hover {{ background: {ACCENT_DIM}; }}
    QPushButton#Primary:disabled {{ background: {border}; border-color: {border}; color: {text_dim}; }}
    QPushButton#Danger {{ background: {DANGER}; border-color: {DANGER}; color: #ffffff; font-weight: 600; }}

    QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {{
        background: {surface};
        border: 1px solid {border};
        border-radius: 7px;
        padding: 6px 9px;
        selection-background-color: {ACCENT};
    }}
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {ACCENT}; }}
    QComboBox::drop-down {{ border: none; width: 20px; }}

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
    QListWidget#DeviceTable::item:selected {{
        background: {ACCENT};
        border-color: {ACCENT};
    }}
    QFrame#DeviceCard {{
        background: {surface_alt};
        border: 1px solid {border};
        border-radius: 8px;
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
    QLabel#DeviceStatus[state="offline"] {{ color: {WARNING}; border-color: {WARNING}; }}

    QTableWidget, QListWidget, QTreeWidget {{
        background: {surface};
        border: 1px solid {border};
        border-radius: 8px;
        gridline-color: {border};
        selection-background-color: {ACCENT};
        selection-color: #ffffff;
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
    QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {border}; border-radius: 5px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {text_dim}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; }}
    QScrollBar::handle:horizontal {{ background: {border}; border-radius: 5px; min-width: 30px; }}

    QStatusBar {{ background: {surface}; border-top: 1px solid {border}; color: {text_dim}; }}
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

    painter.setBrush(QColor(ACCENT if dark else ACCENT_DIM))
    painter.setPen(Qt.NoPen)
    radius = size * 0.22
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

    # Two arrows passing each other: "sent" and "received".
    pen_width = max(2.0, size * 0.075)
    painter.setPen(QColor("#ffffff"))
    font = QFont()
    font.setPointSizeF(size * 0.44)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "⇅")
    _ = pen_width
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
