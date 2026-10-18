"""Runtime-painted monochrome icons.

The harness web client draws its sidebar with a small set of hairline line
icons; the desktop app used to spell everything out in emoji (「📱 手机连接」,
「😊」,「📎」), which on a dark canvas looks like a different product.  This
module paints the same kind of icon at runtime — no binary assets in the repo,
and the stroke colour follows the current palette, exactly like the app icon
already did (see :func:`eversend.desktop.theme.app_icon`).

Every icon is drawn inside a unit square and then scaled, so one definition
works at 14, 18 or 22 pixels.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

#: Painted icons are cached per (name, colour, size): repainting a 20-pixel
#: glyph on every stylesheet change and every row redraw is pure waste.
_CACHE: dict[tuple[str, str, int], QIcon] = {}

#: Oversampling factor.  Qt scales a 1x pixmap on a HiDPI screen and the
#: hairlines turn to mush, so draw at 2x and tell Qt the ratio.
_RATIO = 2


def icon(name: str, color: str, size: int = 18) -> QIcon:
    """Return ``name`` drawn in ``color`` at ``size`` pixels."""
    key = (name, color, size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    pixmap = QPixmap(size * _RATIO, size * _RATIO)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color))
    # 画的时候坐标系是"单位方块"（见 _draw 里的 0..1 坐标），所以线宽也要
    # 按单位给：1.5 / size 正好是"这个尺寸下的 1.5 像素"。
    pen.setWidthF(1.5 / max(1, size))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.scale(size * _RATIO, size * _RATIO)
    try:
        _draw(painter, name)
    finally:
        painter.end()
    pixmap.setDevicePixelRatio(_RATIO)
    result = QIcon(pixmap)
    _CACHE[key] = result
    return result


def _line(painter: QPainter, x1: float, y1: float, x2: float, y2: float) -> None:
    painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))


def _arc(painter: QPainter, rect: QRectF, start: float, sweep: float) -> None:
    path = QPainterPath()
    path.arcMoveTo(rect, start)
    path.arcTo(rect, start, sweep)
    painter.drawPath(path)


def _draw(painter: QPainter, name: str) -> None:
    if name == "plus":
        _line(painter, 0.5, 0.22, 0.5, 0.78)
        _line(painter, 0.22, 0.5, 0.78, 0.5)
    elif name == "refresh":
        # A nearly-closed circle with an arrow head: "look again".
        _arc(painter, QRectF(0.2, 0.2, 0.6, 0.6), 60, 300)
        _line(painter, 0.68, 0.16, 0.82, 0.3)
        _line(painter, 0.82, 0.3, 0.64, 0.36)
    elif name == "scan":
        # Radar: two arcs plus the sweep.
        _arc(painter, QRectF(0.16, 0.16, 0.68, 0.68), 0, 360)
        _arc(painter, QRectF(0.34, 0.34, 0.32, 0.32), 0, 360)
        _line(painter, 0.5, 0.5, 0.84, 0.5)
    elif name == "phone":
        painter.drawRoundedRect(QRectF(0.32, 0.14, 0.36, 0.72), 0.08, 0.08)
        _line(painter, 0.44, 0.76, 0.56, 0.76)
    elif name == "users":
        _arc(painter, QRectF(0.12, 0.22, 0.32, 0.32), 0, 360)
        _arc(painter, QRectF(0.56, 0.22, 0.32, 0.32), 0, 360)
        _arc(painter, QRectF(0.06, 0.56, 0.5, 0.4), 0, 180)
        _arc(painter, QRectF(0.44, 0.56, 0.5, 0.4), 0, 180)
    elif name == "chat":
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.14, 0.18, 0.72, 0.52), 0.12, 0.12)
        path.moveTo(0.36, 0.70)
        path.lineTo(0.34, 0.86)
        path.lineTo(0.52, 0.70)
        painter.drawPath(path)
    elif name == "transfer":
        _line(painter, 0.34, 0.2, 0.34, 0.8)
        _line(painter, 0.2, 0.66, 0.34, 0.8)
        _line(painter, 0.48, 0.8, 0.34, 0.8)
        _line(painter, 0.66, 0.2, 0.66, 0.8)
        _line(painter, 0.52, 0.34, 0.66, 0.2)
        _line(painter, 0.8, 0.34, 0.66, 0.2)
    elif name == "settings":
        # 三条滑杆（sliders）比齿轮在小尺寸下清楚得多，也是同一套线性图标
        # 语言里常见的"设置"画法。
        for y, knob in ((0.28, 0.62), (0.5, 0.38), (0.72, 0.56)):
            _line(painter, 0.16, y, 0.84, y)
            painter.setBrush(painter.pen().color())
            painter.drawEllipse(QPointF(knob, y), 0.09, 0.09)
            painter.setBrush(Qt.NoBrush)
    elif name == "file":
        path = QPainterPath()
        path.moveTo(0.26, 0.16)
        path.lineTo(0.58, 0.16)
        path.lineTo(0.76, 0.36)
        path.lineTo(0.76, 0.84)
        path.lineTo(0.26, 0.84)
        path.closeSubpath()
        path.moveTo(0.58, 0.16)
        path.lineTo(0.58, 0.36)
        path.lineTo(0.76, 0.36)
        painter.drawPath(path)
    elif name == "image":
        # 相框 + 一座小山 + 一个太阳：小尺寸下"这是图片"最认得出的一组符号
        painter.drawRoundedRect(QRectF(0.16, 0.22, 0.68, 0.56), 0.08, 0.08)
        _line(painter, 0.28, 0.66, 0.44, 0.48)
        _line(painter, 0.44, 0.48, 0.58, 0.62)
        _line(painter, 0.58, 0.62, 0.66, 0.54)
        painter.setBrush(painter.pen().color())
        painter.drawEllipse(QPointF(0.62, 0.38), 0.055, 0.055)
        painter.setBrush(Qt.NoBrush)
    elif name == "film":
        painter.drawRoundedRect(QRectF(0.16, 0.24, 0.68, 0.52), 0.08, 0.08)
        for x in (0.24, 0.32, 0.68, 0.76):
            _line(painter, x, 0.3, x, 0.38)
            _line(painter, x, 0.62, x, 0.7)
        _line(painter, 0.44, 0.42, 0.62, 0.5)
        _line(painter, 0.62, 0.5, 0.44, 0.58)
        _line(painter, 0.44, 0.58, 0.44, 0.42)
    elif name == "music":
        _line(painter, 0.62, 0.2, 0.62, 0.68)
        _line(painter, 0.62, 0.2, 0.82, 0.26)
        _line(painter, 0.82, 0.26, 0.82, 0.6)
        painter.setBrush(painter.pen().color())
        painter.drawEllipse(QPointF(0.52, 0.7), 0.1, 0.08)
        painter.drawEllipse(QPointF(0.72, 0.62), 0.1, 0.08)
        painter.setBrush(Qt.NoBrush)
    elif name == "code":
        _line(painter, 0.38, 0.3, 0.18, 0.5)
        _line(painter, 0.18, 0.5, 0.38, 0.7)
        _line(painter, 0.62, 0.3, 0.82, 0.5)
        _line(painter, 0.82, 0.5, 0.62, 0.7)
        _line(painter, 0.56, 0.22, 0.44, 0.78)
    elif name == "archive":
        painter.drawRoundedRect(QRectF(0.2, 0.18, 0.6, 0.64), 0.08, 0.08)
        for y in (0.26, 0.36, 0.46):
            _line(painter, 0.42, y, 0.58, y)
        _line(painter, 0.46, 0.56, 0.54, 0.56)
        _line(painter, 0.54, 0.56, 0.54, 0.7)
        _line(painter, 0.54, 0.7, 0.46, 0.7)
        _line(painter, 0.46, 0.7, 0.46, 0.56)
    elif name == "paperclip":
        path = QPainterPath()
        path.moveTo(0.3, 0.6)
        path.lineTo(0.6, 0.3)
        # 右上那个回头弯
        path.arcTo(QRectF(0.6, 0.2, 0.2, 0.2), 180, -180)
        path.lineTo(0.44, 0.66)
        # 左下那个回头弯
        path.arcTo(QRectF(0.26, 0.48, 0.18, 0.18), 0, -180)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "smile":
        _arc(painter, QRectF(0.16, 0.16, 0.68, 0.68), 0, 360)
        painter.setBrush(painter.pen().color())
        painter.drawEllipse(QPointF(0.38, 0.42), 0.035, 0.035)
        painter.drawEllipse(QPointF(0.62, 0.42), 0.035, 0.035)
        painter.setBrush(Qt.NoBrush)
        _arc(painter, QRectF(0.32, 0.36, 0.36, 0.34), 200, 140)
    elif name == "folder":
        path = QPainterPath()
        path.moveTo(0.14, 0.72)
        path.lineTo(0.14, 0.32)
        path.lineTo(0.4, 0.32)
        path.lineTo(0.47, 0.42)
        path.lineTo(0.86, 0.42)
        path.lineTo(0.86, 0.72)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "send":
        _line(painter, 0.5, 0.78, 0.5, 0.24)
        _line(painter, 0.3, 0.44, 0.5, 0.24)
        _line(painter, 0.7, 0.44, 0.5, 0.24)
    elif name == "chevron-down":
        _line(painter, 0.28, 0.4, 0.5, 0.62)
        _line(painter, 0.5, 0.62, 0.72, 0.4)
    elif name == "chevron-right":
        _line(painter, 0.4, 0.28, 0.62, 0.5)
        _line(painter, 0.62, 0.5, 0.4, 0.72)
    elif name == "back":
        _line(painter, 0.6, 0.24, 0.34, 0.5)
        _line(painter, 0.34, 0.5, 0.6, 0.76)
    elif name == "close":
        _line(painter, 0.28, 0.28, 0.72, 0.72)
        _line(painter, 0.72, 0.28, 0.28, 0.72)
    elif name == "search":
        _arc(painter, QRectF(0.2, 0.2, 0.46, 0.46), 0, 360)
        _line(painter, 0.62, 0.62, 0.82, 0.82)
    elif name == "bolt":
        path = QPainterPath()
        path.moveTo(0.56, 0.1)
        path.lineTo(0.28, 0.54)
        path.lineTo(0.46, 0.54)
        path.lineTo(0.4, 0.9)
        path.lineTo(0.72, 0.44)
        path.lineTo(0.52, 0.44)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "more":
        # ⋯ ：一个意图的第二个入口都收进这里之后，它出现的次数比任何图标都多，
        # 所以三个点要一样大、间距均等（点画成实心小圆，1.5px 描边下最清楚）。
        painter.setBrush(painter.pen().color())
        for x in (0.24, 0.5, 0.76):
            painter.drawEllipse(QPointF(x, 0.5), 0.055, 0.055)
        painter.setBrush(Qt.NoBrush)
    elif name == "clock":
        _arc(painter, QRectF(0.16, 0.16, 0.68, 0.68), 0, 360)
        _line(painter, 0.5, 0.3, 0.5, 0.52)
        _line(painter, 0.5, 0.52, 0.66, 0.62)
    elif name == "retry":
        # 和 refresh 同一族（一段开口圆 + 箭头），但缺口在下方，读作"再来一次"。
        _arc(painter, QRectF(0.2, 0.2, 0.6, 0.6), 150, 250)
        _line(painter, 0.18, 0.62, 0.34, 0.76)
        _line(painter, 0.34, 0.76, 0.44, 0.58)
    elif name == "alert":
        _line(painter, 0.5, 0.14, 0.9, 0.82)
        _line(painter, 0.9, 0.82, 0.1, 0.82)
        _line(painter, 0.1, 0.82, 0.5, 0.14)
        _line(painter, 0.5, 0.38, 0.5, 0.6)
        painter.setBrush(painter.pen().color())
        painter.drawEllipse(QPointF(0.5, 0.71), 0.045, 0.045)
        painter.setBrush(Qt.NoBrush)
    elif name == "inbox":
        # 空状态用的"托盘"：上沿开口 + 中间一个下凹的小格。
        path = QPainterPath()
        path.moveTo(0.14, 0.5)
        path.lineTo(0.3, 0.26)
        path.lineTo(0.7, 0.26)
        path.lineTo(0.86, 0.5)
        path.lineTo(0.86, 0.78)
        path.lineTo(0.14, 0.78)
        path.closeSubpath()
        painter.drawPath(path)
        _line(painter, 0.14, 0.5, 0.38, 0.5)
        _line(painter, 0.38, 0.5, 0.44, 0.62)
        _line(painter, 0.44, 0.62, 0.56, 0.62)
        _line(painter, 0.56, 0.62, 0.62, 0.5)
        _line(painter, 0.62, 0.5, 0.86, 0.5)
    elif name == "check":
        _line(painter, 0.2, 0.52, 0.42, 0.74)
        _line(painter, 0.42, 0.74, 0.8, 0.28)
    elif name == "info":
        _arc(painter, QRectF(0.16, 0.16, 0.68, 0.68), 0, 360)
        _line(painter, 0.5, 0.44, 0.5, 0.7)
        painter.setBrush(painter.pen().color())
        painter.drawEllipse(QPointF(0.5, 0.32), 0.045, 0.045)
        painter.setBrush(Qt.NoBrush)
    elif name == "copy":
        painter.drawRoundedRect(QRectF(0.16, 0.16, 0.48, 0.48), 0.07, 0.07)
        painter.drawRoundedRect(QRectF(0.36, 0.36, 0.48, 0.48), 0.07, 0.07)
    elif name == "trash":
        _line(painter, 0.16, 0.28, 0.84, 0.28)
        _line(painter, 0.4, 0.28, 0.42, 0.16)
        _line(painter, 0.42, 0.16, 0.58, 0.16)
        _line(painter, 0.58, 0.16, 0.6, 0.28)
        path = QPainterPath()
        path.moveTo(0.24, 0.28)
        path.lineTo(0.3, 0.86)
        path.lineTo(0.7, 0.86)
        path.lineTo(0.76, 0.28)
        painter.drawPath(path)
        _line(painter, 0.44, 0.42, 0.44, 0.74)
        _line(painter, 0.56, 0.42, 0.56, 0.74)
    elif name == "zoom-in":
        _arc(painter, QRectF(0.18, 0.18, 0.48, 0.48), 0, 360)
        _line(painter, 0.61, 0.61, 0.84, 0.84)
        _line(painter, 0.42, 0.3, 0.42, 0.54)
        _line(painter, 0.3, 0.42, 0.54, 0.42)
    elif name == "zoom-out":
        _arc(painter, QRectF(0.18, 0.18, 0.48, 0.48), 0, 360)
        _line(painter, 0.61, 0.61, 0.84, 0.84)
        _line(painter, 0.3, 0.42, 0.54, 0.42)
    elif name == "fit":
        # 四角向内的箭头 = 缩到窗口里
        _line(painter, 0.14, 0.14, 0.36, 0.36)
        _line(painter, 0.14, 0.14, 0.36, 0.14)
        _line(painter, 0.14, 0.14, 0.14, 0.36)
        _line(painter, 0.86, 0.86, 0.64, 0.64)
        _line(painter, 0.86, 0.86, 0.64, 0.86)
        _line(painter, 0.86, 0.86, 0.86, 0.64)
    elif name == "actual":
        # 1:1 —— 一个方框加一条基线，表示"原始大小"
        painter.drawRoundedRect(QRectF(0.18, 0.24, 0.64, 0.44), 0.07, 0.07)
        _line(painter, 0.32, 0.58, 0.32, 0.36)
        _line(painter, 0.5, 0.58, 0.5, 0.36)
        _line(painter, 0.68, 0.58, 0.68, 0.36)
    else:  # pragma: no cover - a typo should be visible, not silent
        raise KeyError(f"unknown icon: {name}")


def names() -> set[str]:
    """Every icon this module can paint.

    Callers that want to *offer* an icon only when it exists use this instead
    of catching ``KeyError``; the three clients share one set of names, so a
    missing name means a real gap in the set (see charter §2.3).
    """
    return set(NAMES)


#: 三端共用的图标名（宪章 §2.3）。``names()`` 之外再导出一个常量，方便别人
#: ``"clock" in icons.NAMES`` 这样判断，而不必构造一次集合。
NAMES = frozenset(
    {
        "plus", "refresh", "scan", "phone", "users", "chat", "transfer", "settings",
        "file", "image", "film", "music", "code", "archive", "paperclip", "smile",
        "folder", "send", "chevron-down", "chevron-right", "back", "close", "search",
        "bolt", "more", "clock", "retry", "alert", "inbox", "check", "info", "copy",
        "trash", "zoom-in", "zoom-out", "fit", "actual",
    }
)


__all__ = ["NAMES", "icon", "names"]
