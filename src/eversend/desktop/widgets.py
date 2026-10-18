"""Reusable widgets: 侧栏里的设备行、分段控件、待发文件、传输表格、二维码面板。

布局规格见 ``docs/UI_LAYOUT.md``：左侧栏一列设备、右侧主区一段会话；
「传输」页用一张表格显示传输，``[接收｜发送]`` 分段控件就地切换两张表。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from typing import Callable, Iterable

from PySide6.QtCore import QMimeData, QObject, QPoint, QRect, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QMessageBox,
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..core import qr
from ..core.model import DeviceInfo, Peer, human_bytes
from . import icons, preview, theme


def icon_button(name: str, tooltip: str, size: int = 18) -> QPushButton:
    """A square, icon-only button for the sidebar's action rows.

    Harness draws these as hairline glyphs that only show a background on
    hover; spelling them out in words (「扫描局域网」) took the whole width of
    the sidebar and pushed the device list down.
    """
    button = QPushButton()
    button.setObjectName("IconButton")
    button.setToolTip(tooltip)
    button.setCursor(Qt.PointingHandCursor)
    button.setProperty("iconName", name)
    button.setIcon(icons.icon(name, theme.current_text_color(), size))
    button.setIconSize(QSize(size, size))
    button.setFixedWidth(34)
    return button


def repaint_icons(widget: QWidget) -> None:
    """Redraw every ``IconButton`` under ``widget`` after a theme switch.

    A QIcon is a snapshot: the light palette needs near-black strokes where the
    dark one had near-white, and Qt will not re-render it for us.
    """
    color = theme.current_text_color()
    for button in widget.findChildren(QPushButton):
        if button.objectName() != "IconButton":
            continue
        name = str(button.property("iconName") or "")
        if name:
            size = button.iconSize().width() or 18
            button.setIcon(icons.icon(name, color, size))


class SegmentedControl(QFrame):
    """``[会话｜传输]`` —— 一小段整体，选中的那格亮起来。

    Used twice in the new layout (会话/传输 in the peer header, 接收/发送 in the
    transfer page).  Behaving like one control in both places keeps the two
    switches looking like the same idea.
    """

    changed = Signal(str)

    def __init__(
        self, options: Iterable[tuple[str, str]], current: str = "", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Segmented")
        row = QHBoxLayout(self)
        row.setContentsMargins(theme.SPACE["xs"], theme.SPACE["xs"], theme.SPACE["xs"], theme.SPACE["xs"])
        row.setSpacing(theme.SPACE["xs"])
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        for key, label in options:
            button = QPushButton(label)
            button.setObjectName("Segment")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            self._group.addButton(button)
            self._buttons[key] = button
            row.addWidget(button)
            button.clicked.connect(lambda _checked=False, k=key: self._on_click(k))
        if current:
            self.set_current(current)

    def _on_click(self, key: str) -> None:
        self.changed.emit(key)

    def current(self) -> str:
        for key, button in self._buttons.items():
            if button.isChecked():
                return key
        return ""

    def set_current(self, key: str, emit: bool = False) -> None:
        button = self._buttons.get(key)
        if button is None or button.isChecked():
            return
        button.setChecked(True)
        if emit:
            self.changed.emit(key)

    def button(self, key: str) -> QPushButton | None:
        return self._buttons.get(key)


#: 图形簇的最小单元：零宽连接符（👨‍👩‍👧）、变体选择符（✅）、肤色修饰符、
#: 区域指示符（🇨🇳 是两个码位）都不许被切开。
_GRAPHEME_RE = (
    r"(?:[\U0001F1E6-\U0001F1FF]{2}"
    r"|[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]"
    r"(?:\uFE0F|[\U0001F3FB-\U0001F3FF])?"
    r"(?:\u200D[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF](?:\uFE0F|[\U0001F3FB-\U0001F3FF])?)*"
    r"|.)"
)


def graphemes(text: str) -> list[str]:
    """把字符串切成"人眼看到的一个字"（emoji 组合不拆开）。"""
    import re

    return re.findall(_GRAPHEME_RE, str(text), flags=re.DOTALL)


def _minimum_units(text: str) -> list[str]:
    """省略时可以被切开的**最小单元**。

    「Chrome」这种连着写的词算一个单元 —— ``QFontMetrics.elidedText`` 会把它
    切成 ``Chro…``，用户看到的是半个词（"Android 上的 C…" 就是这么来的）；
    中日韩文字每个字都可以断，所以一个字一个单元。
    """
    units: list[str] = []
    buffer = ""
    for char in str(text):
        if char == " ":
            units.append(buffer + char if buffer else char)
            buffer = ""
            continue
        is_cjk = (
            "\u2e80" <= char <= "\u9fff"
            or "\uac00" <= char <= "\ud7af"
            or "\uff00" <= char <= "\uffef"
            or "\u3000" <= char <= "\u303f"
        )
        if is_cjk:
            if buffer:
                units.append(buffer)
                buffer = ""
            units.append(char)
            continue
        buffer += char
    if buffer:
        units.append(buffer)
    return units or ([str(text)] if text else [])


#: 省略号前面**不允许**留下一个悬空的标点/空格（"Android 上的 Chrome（…"）。
_DANGLING_CHARS = " \t（(【[「《〈｛{·、,，;；:：-—–~"


def elide_text(metrics, text: str, width: int, ellipsis: str = "…") -> str:
    """按"不切断一个词/一个字"省略到 ``width`` 像素以内。

    规则（宪章 C4；用户投诉过 ``Android 上的 C...``）：

    1. 宽度够就原样返回；
    2. 否则按最小单元累加 —— ``Android 上的 Chrome`` 在窄行里先变成
       ``Android 上的 Chrome…``，再窄就变成 ``Android 上的…``，
       **不会**出现 ``Android 上的 C…``；
    3. 省略号前面不留悬空标点（``（``、``·``、``、`` …）；
    4. 一个单元都放不下（名字本身就是一长串英文）时改成**中间省略**
       （``Supercalifr…istic``），保住头尾两个能认的片段。
    """
    text = str(text)
    if width <= 0 or not text:
        return text
    if metrics.horizontalAdvance(text) <= width:
        return text
    out = ""
    for unit in _minimum_units(text):
        if metrics.horizontalAdvance(out + unit + ellipsis) > width:
            break
        out += unit
    out = out.rstrip(_DANGLING_CHARS)
    if out:
        return out + ellipsis
    # 一个单元都放不下的时候：中间省略比"只剩开头两个字"更好认。
    middle = metrics.elidedText(text, Qt.ElideMiddle, width)
    if middle and middle != text:
        return middle
    pieces = graphemes(text)
    while pieces:
        candidate = "".join(pieces) + ellipsis
        if metrics.horizontalAdvance(candidate) <= width:
            return candidate
        pieces.pop()
    return ellipsis


class ElidedLabel(QLabel):
    """A label that shortens its text instead of widening the sidebar.

    Device names are user-chosen and can be arbitrarily long; the old device
    card wrapped them, which made one row twice as tall as its neighbours.

    省略按"最小单元"做（见 :func:`elide_text`）：绝不把 ``Chrome`` 切成
    ``C…``，也不把 emoji 组合切成半个。
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._full = text

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._full = str(text)
        super().setText(self._elide(self._full))

    def fullText(self) -> str:  # noqa: N802 - Qt naming
        return self._full

    def _elide(self, text: str) -> str:
        width = self.width()
        if width <= 0:
            return text
        return elide_text(self.fontMetrics(), text, width)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        super().setText(self._elide(self._full))


class StatusDot(QLabel):
    """设备行上的状态点：绿=在线，灰=离线。

    以前状态是行尾一块文字徽标（"在线 / 已信任 / 网页版 / 未连接"），它把名字
    挤到只剩不到 100px，用户看到的是 ``Android 上的 C...``。状态本来就是一个
    "点一下就能问出来"的事实，做成头像右下角的一个小圆点，名字就能拿到整行
    宽度（宪章 C4/C9）。

    ``text()`` 仍然是状态文案 —— 悬停提示和测试都按它读。
    """

    #: 状态名 → 语义色名（真正的色值在 ``theme.STATUS_COLORS`` 里，**画的时候
    #: 现取**：主题可以在运行时切换，import 时快照下来的颜色不会跟着变）。
    STATES = ("online", "offline", "warn")

    def __init__(self, text: str, state: str = "online", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("DeviceStatusDot")
        self._state = state if state in self.STATES else "online"
        self.setFixedSize(10, 10)
        self.setToolTip(text)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_state(self, state: str) -> None:
        self._state = state if state in self.STATES else "online"
        self.update()

    def _colour(self) -> QColor:
        table = getattr(theme, "STATUS_COLORS", {}) or {}
        fallback = theme.SUCCESS if self._state == "online" else theme.current_text_color()
        return QColor(table.get(self._state) or fallback)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        colour = self._colour()
        painter.setBrush(colour)
        # 深色画布上给一圈同色描边，浅色下这个点也不至于糊进背景。
        painter.setPen(QPen(colour.darker(140), 1))
        painter.drawEllipse(1, 1, self.width() - 2, self.height() - 2)
        painter.end()


class DeviceAvatar(QWidget):
    """设备头像：平台字形 + 右下角的状态点。

    状态点压着头像右下角（像聊天软件的在线点），这样名字那一行能独占整行
    宽度 —— 以前状态徽标站在名字右边，长名字被挤成 ``Android 上的 C...``。
    """

    SIZE = 34

    def __init__(
        self,
        glyph: str,
        status_text: str,
        state: str = "online",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.glyph = QLabel(glyph, self)
        self.glyph.setObjectName("DeviceGlyph")
        self.glyph.setAlignment(Qt.AlignCenter)
        self.glyph.setGeometry(0, 0, self.SIZE - 4, self.SIZE - 4)
        self.status = StatusDot(status_text, state, self)
        self.status.move(self.SIZE - self.status.width(), self.SIZE - self.status.height())


class DeviceCard(QFrame):
    """One device row in the sidebar: 头像（带状态点）+ 名字 + 一行细节。

    The device list lives in a 288 px column now, so the old wide card (52 px
    glyph, two detail lines, a chip) left nothing for the name.  Everything
    needed to tell two devices apart at a glance is still here -- the rest of
    the story is in the peer header on the right and in the tooltip.

    规格（宪章 C4/C9）：
    * 名字拿整行宽度（状态已经是头像上的点，不再跟名字抢地方），省略按最小单元
      —— 不切半个词、不留悬空标点；悬停能看到全名；
    * 副标题**只放一个事实**：在线只写地址（不带端口），离线只写
      ``平台 · 最后在线 X``（地址留在悬停提示与顶部详情里）；
    * 列表里真的出现同名设备时（``distinguish``）才把地址补上 —— 那种情况下
      "分得清谁是谁"比"只放一个事实"重要。
    """

    def __init__(
        self,
        peer: Peer,
        meta: dict | None = None,
        parent: QWidget | None = None,
        distinguish: bool = False,
    ) -> None:
        super().__init__(parent)
        self.peer = peer
        self.setObjectName("DeviceCard")
        info = peer.info
        online = bool(info.capabilities.get("online", True))
        is_web = bool(info.capabilities.get("web"))
        meta = meta or {}

        row = QHBoxLayout(self)
        row.setContentsMargins(
            theme.SPACE["sm"], theme.SPACE["xs"] + 2, theme.SPACE["sm"], theme.SPACE["xs"] + 2
        )
        row.setSpacing(theme.SPACE["sm"])

        status_text = _device_status_text(peer, is_web, online)
        self.avatar = DeviceAvatar(
            _device_glyph(peer, is_web, meta),
            status_text,
            "online" if online else ("warn" if peer.trusted else "offline"),
        )
        # 旧名字：行卡片的"徽标"就是头像上这个点，`text()` 给的是状态文案。
        self.status = self.avatar.status
        row.addWidget(self.avatar, 0, Qt.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(1)
        name = info.name or "（没有名字的设备）"
        if meta.get("pinned"):
            name = "📌 " + name
        self.name = ElidedLabel(name)
        self.name.setObjectName("DeviceName")
        # 名字被省略时，悬停必须能看到全名（宪章 C4）。
        self.name.setToolTip(name)
        text.addWidget(self.name)

        self.detail = ElidedLabel(_device_sidebar_line(peer, is_web, online, distinguish))
        self.detail.setObjectName("DeviceDetail")
        text.addWidget(self.detail)
        row.addLayout(text, 1)

        unread = int(meta.get("unread") or 0)
        if unread > 0:
            self.badge = QLabel("99+" if unread > 99 else str(unread))
            self.badge.setObjectName("ConvBadge")
            self.badge.setAlignment(Qt.AlignCenter)
            row.addWidget(self.badge, 0, Qt.AlignVCenter)

        tooltip = _device_tooltip(peer, is_web, online)
        if meta.get("muted"):
            tooltip += "\n🔕 消息免打扰"
        if unread:
            tooltip += f"\n{unread} 条未读"
        self.setToolTip(tooltip)


def _device_glyph(peer: Peer, is_web: bool, meta: dict | None = None) -> str:
    """设备头像上的字形。

    手机（安卓 App 与手机浏览器都是手机）统一用 📱 —— "手机 vs 电脑"是**形状**
    差异，一眼就分得开；App 与浏览器的差别写在副标题里（``安卓 App`` / ``网页版``）。
    电脑保持各自的平台字形。

    ``meta["kind"] == "phone" | "device"`` 是侧栏算好的权威判断（desktop-shell
    在 ``Sidebar._apply_search`` 里填），有就优先信它。
    """
    kind = str((meta or {}).get("kind") or "")
    if kind == "phone":
        return "📱"
    if kind == "device" and not is_web:
        return theme.platform_glyph(peer.info.platform)
    if is_web or str(peer.info.platform).lower() in ("android", "ios"):
        return "📱"
    return theme.platform_glyph(peer.info.platform)


def _device_platform_label(peer: Peer, is_web: bool) -> str:
    """这台设备"是什么"：平台名（网页客户端说清是 App 还是浏览器）。"""
    info = peer.info
    if is_web:
        return "安卓 App" if _is_app_client(peer) else "网页版"
    return {
        "linux": "Linux",
        "windows": "Windows",
        "macos": "macOS",
        "android": "安卓",
        "ios": "iOS",
        "browser": "网页版",
    }.get(str(info.platform).lower(), info.platform or "未知系统")


def _device_sidebar_line(peer: Peer, is_web: bool, online: bool, distinguish: bool = False) -> str:
    """The one line under the name: **一个事实**（宪章 C9）。

    * 在线：只写地址 —— **不带端口**（端口对用户没意义，同一台机器上几个端口
      只会更糊涂；端口和平台都在右边的 PeerHeader 里）；
    * 离线：只写 ``平台 · 最后在线 X``，不写地址（它是"它什么时候还在"，不是
      "它在哪"）；
    * ``distinguish``（列表里真的有同名设备）时才把地址补回来，否则两行看起来
      一模一样。核心的同名前缀消歧（``core.model.disambiguate_names``）已经在
      窗口那边套上，这里是最后一道保险。
    """
    platform = _device_platform_label(peer, is_web)
    address = peer.address or ""
    if online:
        if is_web:
            # 手机是纯 HTTP 客户端（docs/REAL_DEVICE_FINDINGS.md §2.3）：App 进程
            # 根本没有监听端口，电脑推不过去、也没法照地址连它 —— 那个 IP 对用户
            # 没有任何可操作性。写清"这是安卓 App / 这是网页版"才有用：它决定了
            # 接下来该怎么发文件（等它来取）。
            return platform
        if not address:
            return platform
        return f"{platform} · {address}" if distinguish else address
    ago = float((peer.info.capabilities or {}).get("secondsAgo") or 0)
    when = f"最后在线 {_ago_label(ago)}"
    if distinguish and address:
        return f"{platform} · {when} · {address}"
    return f"{platform} · {when}"


def _device_detail_line(info: DeviceInfo) -> str:
    """What the device *is*: kind, platform, version, and its short id."""
    kind = {"desktop": "电脑", "mobile": "手机", "server": "服务器"}.get(info.kind, info.kind or "设备")
    parts = [kind]
    if info.platform and info.platform != "browser":
        parts.append(info.platform)
    if info.version and info.version != "web":
        parts.append("v" + info.version)
    if info.device_id and not info.device_id.startswith("web:"):
        # Two devices can share a name; the id is what actually differs.  A
        # browser client has no such id (it is synthesised from address + UA),
        # so printing its first eight characters would be pure noise.
        parts.append("ID " + info.device_id[:8])
    return " · ".join(parts)


def _device_where_line(peer: Peer, is_web: bool, online: bool) -> str:
    """Where it is and how we know about it.

    只写"在哪 / 怎么发现的"，客户端类型只说一次 —— 以前网页客户端在这条里
    被写了两次（``… · 网页客户端（手机浏览器） · … · 网页版``），同一个事实
    出现两遍（宪章 C9）。
    """
    parts = [f"{peer.address}:{peer.port}" if peer.address else "地址未知"]
    if is_web:
        # 手机有两种客户端：安卓 App 和手机浏览器。以前一律写成"网页客户端
        # （手机浏览器）"，装了 App 的用户看到自己的手机被标成网页版。
        is_app = _is_app_client(peer)
        parts.append("安卓 App" if is_app else "网页客户端（手机浏览器）")
    else:
        parts.append(_source_label(peer.source))
    if peer.same_host:
        parts.append("就在本机")
    if not online:
        ago = float(peer.info.capabilities.get("secondsAgo") or 0)
        parts.append("最后在线 " + _ago_label(ago))
    elif peer.rtt_ms and not is_web:
        parts.append(f"{peer.rtt_ms:.0f} ms")
    return " · ".join(parts)


def _is_app_client(peer: Peer) -> bool:
    """这台"网页客户端"其实是安卓 App 吗（两者都走 HTTP）。"""
    caps = peer.info.capabilities or {}
    return str(caps.get("clientKind") or "") == "app" or str(peer.info.platform).lower() == "android"


def _source_label(source: str) -> str:
    return {
        "multicast": "组播发现",
        "broadcast": "广播发现",
        "mdns": "mDNS 发现",
        "scan": "扫描发现",
        "manual": "手动添加",
        "incoming": "它连过来的",
        "local": "本机",
        "web": "网页客户端",
        # 设备早就不在线了，但会话记录还在 —— 列表里留着它，历史才进得去。
        "history": "记录里的会话",
        # 以前见过（身份记在本机上），现在不在线。
        "offline": "以前见过",
    }.get(source or "", source or "未知来源")


def _device_status_text(peer: Peer, is_web: bool, online: bool) -> str:
    # 「离线」优先判定：以前协议设备只会在"刚被听见"的时候出现在列表里，
    # 所以这里没看 online 也一直是对的；现在「以前见过」的设备也会列出来，
    # 再不看这个标记，一台已经走掉的电脑会在行尾挂着绿色的「在线」。
    if not online:
        return "未连接" if is_web else "已离线"
    if is_web:
        return "在线"
    if peer.same_host:
        return "本机实例"
    if peer.trusted:
        return "已信任"
    return "在线"


def _ago_label(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    return f"{int(seconds // 3600)} 小时前"


def _device_tooltip(peer: Peer, is_web: bool, online: bool) -> str:
    info = peer.info
    lines = [info.name or "（没有名字的设备）", _device_detail_line(info), _device_where_line(peer, is_web, online)]
    if is_web:
        lines.append(
            "手机是通过浏览器连接的，所以它不在对端列表里。\n"
            + ("现在页面开着，可以直接发送。" if online else "现在不在线（熄屏或离开页面）——文件会留着等它回来。")
        )
    if peer.same_host:
        lines.append("这台设备就在本机上——通常是又开了一个韧传实例。")
    if peer.source == "offline":
        lines.append(
            "以前见过这台设备，本机记着它；现在它不在线，发不了文件。"
            + (f"\n上次看到的地址：{peer.address}" + (f":{peer.port}" if peer.port else "") if peer.address else "")
        )
    elif peer.source == "history":
        lines.append("只剩聊天记录了——这台设备现在不在线。")
    if peer.addresses:
        lines.append("它自报的地址：" + "、".join(peer.addresses[:4]))
    return "\n".join(lines)


class DeviceTable(QListWidget):
    """The list of known devices: one row each.

    Keeps the old API (``set_peers`` / ``peers`` / ``selected_peer``) so the
    window does not care how the list is drawn.
    """

    #: 右键一台设备：菜单由窗口弹（会话偏好 + 忘记设备）
    context_requested = Signal(object, object)
    #: 点一行 = 选中它 + 把主区切到和它的会话（这是新布局的主交互）
    peer_activated = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DeviceTable")
        # 多选：一次把文件发给多台设备（Ctrl/Shift 点选）。
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setUniformItemSizes(False)
        self.setWordWrap(False)
        self.setSpacing(theme.SPACE["xs"])
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._peers: list[Peer] = []
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self.itemClicked.connect(self._on_item_clicked)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        row = self.row(item)
        if 0 <= row < len(self._peers):
            self.peer_activated.emit(self._peers[row])

    def _on_context_menu(self, position) -> None:
        peer = self.selected_peer()
        if peer is None:
            return
        self.context_requested.emit(peer, self.mapToGlobal(position))

    @property
    def peers(self) -> list[Peer]:
        return list(self._peers)

    def selected_peer(self) -> Peer | None:
        """The one row the user is on (kept for the single-target paths).

        With a multi-selection this is the row under the cursor, which is what
        "open this device's page" and similar single-target actions want.
        """
        row = self.currentRow()
        if 0 <= row < len(self._peers):
            return self._peers[row]
        return None

    def selected_peers(self) -> list[Peer]:
        """Every selected device, in list order.

        Multi-select is what makes "send this to the desktop *and* the phone"
        one action instead of two; the table had single selection only, so the
        user had to repeat the whole flow per device.
        """
        rows = sorted(index.row() for index in self.selectedIndexes())
        if not rows:
            peer = self.selected_peer()
            return [peer] if peer is not None else []
        return [self._peers[row] for row in rows if 0 <= row < len(self._peers)]

    def select_first(self) -> None:
        if self._peers and self.currentRow() < 0:
            self.setCurrentRow(0)

    def select_row(self, row: int) -> None:
        if 0 <= row < len(self._peers):
            self.setCurrentRow(row)

    def set_peers(self, peers: list[Peer], meta: dict[str, dict] | None = None) -> None:
        """Fill the list.  ``meta`` maps ``peer.key`` to conversation state.

        A device row now also carries the chat state of the conversation with
        that device (未读 / 置顶 / 免打扰), because the sidebar's device list is
        the only list there is: selecting a device *is* opening the session.
        """
        current = self.selected_peer()
        selected_key = current.key if current is not None else None
        meta = meta or {}

        # 真正重名的设备（两台手机都叫「Android 上的 Chrome」）：名字分不出来，
        # 那一行的细节就把地址露出来，否则用户只能靠猜。
        name_counts: dict[str, int] = {}
        for peer in peers:
            name_counts[peer.info.name] = name_counts.get(peer.info.name, 0) + 1

        self.clear()
        self._peers = list(peers)
        for peer in self._peers:
            card = DeviceCard(peer, meta.get(peer.key), distinguish=name_counts.get(peer.info.name, 0) > 1)
            item = QListWidgetItem()
            # The card is decoration: clicks must reach the list item, or
            # selection and double-click stop working.
            card.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            item.setData(Qt.UserRole, peer.key)
            self.addItem(item)
            self.setItemWidget(item, card)

        if not self._peers:
            empty = QListWidgetItem("还没有发现设备。点上面的 ⟳ 刷新，或让对方发一次。")
            empty.setFlags(Qt.NoItemFlags)
            self.addItem(empty)

        self._fit_cards()

        if selected_key is not None:
            for row, peer in enumerate(self._peers):
                if peer.key == selected_key:
                    self.setCurrentRow(row)
                    return
        self.select_first()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._fit_cards()

    def _fit_cards(self) -> None:
        """Give every row the viewport's width so its text elides.

        Without this a row keeps the width its (long) text asks for: the list
        then scrolls sideways and the status chip disappears off the right
        edge -- which is exactly how the first version of this list looked.
        """
        width = max(200, self.viewport().width() - 4)
        for row in range(self.count()):
            item = self.item(row)
            if item is None:
                continue
            card = self.itemWidget(item)
            if card is None:
                continue
            card.setMinimumWidth(0)
            layout = card.layout()
            height = -1
            if layout is not None:
                layout.activate()
                height = layout.heightForWidth(width)
            if height <= 0:
                height = card.sizeHint().height()
            item.setSizeHint(QSize(width, max(height, 44)))


# --------------------------------------------------------------------------
# 说明：旧版这里还有一份 `TransferTable`（QTableWidget）与 `TransferRow`。
# 传输页现在由 `desktop/transfers_view.py` 自己的 `QTableView + QAbstractTableModel`
# 实现（同名的 `TransferTable` / `TransferRow`），这一份已经没有任何地方引用 ——
# 两份同名实现并存只会让下一个人改错文件，所以删掉。
# 文件动作矩阵（`file_actions_menu` 等）与传输表无关，继续留在这里给三处共用。
# --------------------------------------------------------------------------


def save_as(parent: QWidget, path: str, name: str = "") -> str:
    """另存为：把文件复制到用户挑的地方（原文件不动）。"""
    from PySide6.QtWidgets import QFileDialog

    if not path or not os.path.exists(path):
        return ""
    suggested = name or os.path.basename(path)
    target, _filter = QFileDialog.getSaveFileName(parent, "另存为", suggested, "所有文件 (*)")
    if not target:
        return ""
    try:
        if os.path.abspath(target) != os.path.abspath(path):
            if os.path.isdir(path):
                shutil.copytree(path, target, dirs_exist_ok=True)
            else:
                shutil.copy2(path, target)
    except OSError as exc:
        QMessageBox.warning(parent, "保存失败", str(exc))
        return ""
    return target


def open_path(path: str) -> None:
    """用系统默认程序打开这个文件；起不来要说出来（UI_LAYOUT §4.5）。"""
    if not path or not os.path.exists(path):
        notify(parent=None, message="这个文件不在这台机器上，打不开。")
        return
    from ..core.platform_open import open_path_report

    ok, message = open_path_report(path)
    if not ok:
        notify(parent=None, message=message or "打不开这个文件（系统没有可用的打开方式）。")


# --------------------------------------------------------------------------
# 文件动作矩阵（宪章 §4 · C6）
#
# 一个文件在界面上会出现在三个地方：会话气泡里的附件、输入区的待发方块、
# 传输表的历史行。三处必须给**同一套**动作 —— 以前气泡上只有「预览 / 打开」，
# 表格里只有「预览 / 打开所在位置」，想另存为得自己去文件管理器里翻。
#
# 矩阵只有这一份实现：:func:`file_actions_menu` 出菜单，:func:`file_action_buttons`
# 出「主按钮『预览』+ ⋯」那一对控件，:class:`FilePropertiesDialog` 出属性弹窗
# （SHA-256 在后台线程算）。
# --------------------------------------------------------------------------

#: 菜单里动作的顺序。前四项被测试钉住（``test_gui_actions.py`` 的
#: ``file_actions_menu(...).actions()[:4]``），不要改顺序、不要改字。
FILE_ACTION_LABELS: dict[str, str] = {
    "preview": "预览",
    "open": "打开",
    "reveal": "打开所在文件夹",
    "save_as": "另存为…",
    "copy_path": "复制路径",
    "copy_file": "复制文件",
    "delete": "删除本地文件",
    "resend": "重新发送",
    "properties": "属性",
}

#: 菜单里的分隔线位置（在这一项**之前**画一条线）。
FILE_ACTION_BREAKS = ("copy_path", "delete", "properties")

#: 悬停说明：菜单项只说动作名，能力边界写在 tooltip 里（不静默失败）。
FILE_ACTION_TIPS: dict[str, str] = {
    "preview": "在这里看一眼内容，不用先打开别的程序",
    "open": "用系统的默认程序打开这个文件",
    "reveal": "在文件管理器里定位这个文件",
    "save_as": "把这份文件另存到别处（原文件不动）",
    "copy_path": "把这个文件的完整路径放到剪贴板",
    "copy_file": "把文件本身放到剪贴板（图片/文本可以直接粘贴到聊天里）",
    "delete": "删掉本机这一份（会先问你一次；对方那边的副本不受影响）",
    "resend": "把这个文件重新发给对方",
    "properties": "名称、大小、类型、路径、修改时间、SHA-256",
}


def notify(parent: QWidget | None, message: str) -> None:
    """把一句话显示给用户：优先窗口底部状态栏，没有就一个浮动提示。

    动作失败必须有反馈（宪章 E3）。文件动作是在控件里触发的，控件拿不到
    状态栏时（比如被单独构造的对话框）退回 ``QToolTip`` —— 总比什么都不说好。
    """
    text = str(message or "").strip()
    if not text:
        return
    window = parent.window() if parent is not None else None
    label = getattr(window, "status_left", None)
    if label is not None and hasattr(label, "setText"):
        label.setText(text)
        return
    QToolTip.showText(QCursor.pos() if parent is None else parent.mapToGlobal(QPoint(0, 0)), text)


def clipboard():
    return QApplication.clipboard()


def copy_path_to_clipboard(path: str) -> bool:
    """复制路径（宪章 §4：电脑端 ✅）。"""
    if not path:
        return False
    clipboard().setText(str(path))
    return True


def copy_file_to_clipboard(path: str) -> tuple[bool, str]:
    """复制文件到剪贴板：文件本身 + （图片/文本时）可直接粘贴的内容。

    返回 ``(成功, 给用户看的一句话)``。只放一个路径字符串是不够的 ——
    粘到聊天软件里出来的是文字，不是文件。
    """
    if not path or not os.path.exists(path):
        return False, "这个文件不在这台机器上，复制不了。"
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(os.path.abspath(path))])
    what = "文件"
    if os.path.isfile(path):
        if preview.preview_kind(path) == "image":
            image = QImage(path)
            if not image.isNull():
                mime.setImageData(image)
                what = "图片"
        elif preview.preview_kind(path) == "text":
            text, _truncated = preview.read_text_preview(path)
            mime.setText(text)
            what = "文本"
    clipboard().setMimeData(mime)
    return True, f"已复制{what}：{os.path.basename(path)}"


def trash_available() -> bool:
    """这台机器上有"删除到回收站"的路子吗（决定确认框怎么写）。"""
    if sys.platform == "darwin":
        return True
    if os.name == "nt":
        return True
    try:
        import send2trash  # noqa: F401

        return True
    except Exception:
        return shutil.which("gio") is not None


def _trash_path(path: str) -> str:
    """把文件移到系统回收站；返回错误文案（空字符串=成功）。"""
    try:
        import send2trash

        send2trash.send2trash(path)
        return ""
    except ImportError:
        pass
    except Exception as exc:  # send2trash 自己的异常类型不稳定
        return str(exc)
    if sys.platform == "darwin":
        script = f'tell application "Finder" to delete POSIX file "{path}"'
        done = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=20
        )
        return "" if done.returncode == 0 else (done.stderr.strip() or "Finder 拒绝了这次删除")
    if os.name == "nt":
        return _trash_windows(path)
    gio = shutil.which("gio")
    if gio:
        done = subprocess.run([gio, "trash", path], capture_output=True, text=True, timeout=30)
        return "" if done.returncode == 0 else (done.stderr.strip() or "gio trash 执行失败")
    return "这台机器上没有可用的回收站命令"


def _trash_windows(path: str) -> str:  # pragma: no cover - Windows only
    """Windows 的回收站（``SHFileOperationW`` + ``FOF_ALLOWUNDO``）。"""
    try:
        import ctypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p),
                ("wFunc", ctypes.c_uint),
                ("pFrom", ctypes.c_wchar_p),
                ("pTo", ctypes.c_wchar_p),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", ctypes.c_bool),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_wchar_p),
            ]

        FO_DELETE = 3
        FOF_ALLOWUNDO = 0x40
        FOF_NOCONFIRMATION = 0x10
        FOF_SILENT = 0x4
        operation = SHFILEOPSTRUCTW()
        operation.wFunc = FO_DELETE
        operation.pFrom = os.path.abspath(path) + "\0\0"
        operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
        return "" if result == 0 else f"回收站拒绝了这次删除（代码 {result}）"
    except Exception as exc:
        return str(exc)


def delete_local_file(parent: QWidget | None, path: str) -> bool:
    """删除本地文件：先问一次，能进回收站就进回收站（宪章 §4）。

    删掉的只是**本机这一份**；对方的副本、聊天记录里的这条消息都不受影响 ——
    这句话写在确认框里，免得用户以为"删了就等于撤回"。
    """
    if not path or not os.path.exists(path):
        notify(parent, "这个文件已经不在这台机器上了。")
        return False
    label = "文件夹" if os.path.isdir(path) else "文件"
    trashed = trash_available()
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle("删除本地文件")
    box.setText(f"要删除这个{label}吗？")
    box.setInformativeText(
        f"{os.path.basename(path)}\n{path}\n\n"
        + (
            "会移到系统回收站，还能找回来。"
            if trashed
            else "这台机器上没有回收站命令，删除后无法恢复。"
        )
        + "\n对方那边的副本、以及这段会话里的记录都不受影响。"
    )
    confirm = box.addButton("删除", QMessageBox.AcceptRole)
    box.addButton("取消", QMessageBox.RejectRole)
    box.exec()
    if box.clickedButton() is not confirm:
        return False
    error = _trash_path(path) if trashed else ""
    if not trashed or error:
        if not trashed:
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
            except OSError as exc:
                QMessageBox.warning(parent, "删除失败", str(exc))
                return False
        else:
            QMessageBox.warning(parent, "删除失败", error or "回收站操作失败")
            return False
    notify(parent, f"已删除本机这一份：{os.path.basename(path)}")
    return True


def human_time(stamp: float) -> str:
    """``2025-09-25 18:38`` —— 属性弹窗里的修改时间。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(stamp)))
    except (OSError, ValueError, TypeError):
        return "—"


class _HashTask(QObject):
    """后台算 SHA-256：大文件也不能卡住界面（宪章 §4 / E5），而且随时能取消。"""

    progress = Signal(int)
    finished = Signal(str, str)  # digest, error

    CHUNK = 1024 * 1024

    def __init__(self, path: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.path = str(path)
        self._cancel = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._work, name="sha256", daemon=True).start()

    def cancel(self) -> None:
        """取消：下一个 1 MiB 分片处就停（不用等整块读完）。"""
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _work(self) -> None:
        digest = hashlib.sha256()
        try:
            total = max(1, os.path.getsize(self.path))
            read = 0
            with open(self.path, "rb") as handle:
                while True:
                    if self._cancel.is_set():
                        self.finished.emit("", "cancelled")
                        return
                    chunk = handle.read(self.CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
                    read += len(chunk)
                    self.progress.emit(min(100, int(100 * read / total)))
        except OSError as exc:
            self.finished.emit("", str(exc))
            return
        self.finished.emit(digest.hexdigest(), "")


def sha256_async(path: str, on_done: Callable[[str, str], None], parent: QObject | None = None):
    """异步算 SHA-256，``on_done(digest, error)`` 回到 GUI 线程。

    返回那个任务对象，调用方要留着它（不是 Qt 父子关系就没人持有，线程里的
    信号会连同对象一起被回收）。
    """
    task = _HashTask(path, parent)
    task.finished.connect(on_done)
    task.start()
    return task


def file_type_label(path: str) -> str:
    """中文的类型名（属性弹窗第一行的事实之一）。"""
    if os.path.isdir(str(path)):
        return "文件夹"
    extension = os.path.splitext(str(path))[1].lower()
    suffix = f"（{extension[1:].upper()}）" if extension else ""
    return {
        "image": "图片",
        "video": "视频",
        "audio": "音频",
        "text": "文本",
        "binary": "二进制文件",
        "folder": "文件夹",
    }.get(preview.preview_kind(path), "文件") + suffix


class FilePropertiesDialog(QDialog):
    """属性：名称 / 大小 / 类型 / 路径 / 修改时间 / SHA-256（宪章 §4）。

    SHA-256 必须**异步**算：一个 4 GB 的镜像同步算一遍要十几秒，界面会假死。
    所以这一行先写「计算中…」，算完再替换成摘要；旁边给「复制」。
    """

    def __init__(self, path: str, name: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path = str(path)
        self.name = name or os.path.basename(self.path) or self.path
        self._task = None
        self.setWindowTitle(f"属性 · {self.name}")
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(8)
        mark = QLabel()
        mark.setPixmap(
            icons.icon(preview.tile_icon_name(self.path), theme.current_text_color(), 20).pixmap(20, 20)
        )
        mark.setFixedSize(20, 20)
        head.addWidget(mark)
        title = QLabel(self.name)
        title.setObjectName("Title")
        title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        title.setWordWrap(True)
        head.addWidget(title, 1)
        root.addLayout(head)

        form = QGridLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        form.setColumnStretch(1, 1)
        self.value_labels: dict[str, QLabel] = {}
        rows = [
            ("名称", self.name),
            ("大小", self._size_text()),
            ("类型", file_type_label(self.path)),
            ("路径", self.path),
            ("修改时间", self._mtime_text()),
        ]
        for row, (key, value) in enumerate(rows):
            form.addWidget(self._field_label(key), row, 0, Qt.AlignTop)
            label = self._value_label(value, mono=key == "路径")
            self.value_labels[key] = label
            form.addWidget(label, row, 1)

        hash_row = len(rows)
        form.addWidget(self._field_label("SHA-256"), hash_row, 0, Qt.AlignTop)
        hash_box = QWidget()
        hash_layout = QHBoxLayout(hash_box)
        hash_layout.setContentsMargins(0, 0, 0, 0)
        hash_layout.setSpacing(8)
        self.hash_label = self._value_label("计算中…", mono=True)
        self.hash_label.setObjectName("Mono")
        hash_layout.addWidget(self.hash_label, 1)
        self.copy_hash_button = QPushButton("复制")
        self.copy_hash_button.setObjectName("Ghost")
        self.copy_hash_button.setEnabled(False)
        self.copy_hash_button.setToolTip("把 SHA-256 摘要放到剪贴板")
        self.copy_hash_button.clicked.connect(self._copy_hash)
        hash_layout.addWidget(self.copy_hash_button, 0, Qt.AlignTop)
        form.addWidget(hash_box, hash_row, 1)
        root.addLayout(form)

        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(theme.SPACE["sm"])
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        progress_row.addWidget(self.bar, 1)
        # 1 GB 的文件要算好几秒：给一个"不看了"的出口（宪章 E3：别让用户干等）。
        self.cancel_hash_button = QPushButton("取消计算")
        self.cancel_hash_button.setObjectName("Ghost")
        self.cancel_hash_button.setToolTip("停止计算 SHA-256（关掉这个窗口也会停）")
        self.cancel_hash_button.setVisible(False)
        self.cancel_hash_button.clicked.connect(self.cancel_hash)
        progress_row.addWidget(self.cancel_hash_button)
        root.addLayout(progress_row)

        hint = QLabel(
            "SHA-256 是这份文件内容的指纹：两台设备上的同一个文件，摘要应当完全一致。"
        )
        hint.setObjectName("Subtitle")
        hint.setWordWrap(True)
        root.addWidget(hint)

        buttons = QDialogButtonBox()
        reveal = buttons.addButton("打开所在文件夹", QDialogButtonBox.ActionRole)
        reveal.clicked.connect(lambda: _reveal_path(self.path))
        close = buttons.addButton("关闭", QDialogButtonBox.RejectRole)
        close.clicked.connect(self.reject)
        root.addWidget(buttons)

        if os.path.isfile(self.path):
            self._task = sha256_async(self.path, self._hash_done, self)
            self._task.progress.connect(self._hash_progress)
            self.cancel_hash_button.setVisible(True)
        else:
            self.hash_label.setText("文件夹没有内容摘要")
            self.bar.setVisible(False)
            self.cancel_hash_button.setVisible(False)

    # -- construction helpers ---------------------------------------------

    def _field_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Subtitle")
        label.setMinimumWidth(64)
        return label

    def _value_label(self, text: str, mono: bool = False) -> QLabel:
        label = QLabel(str(text))
        if mono:
            label.setObjectName("Mono")
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setWordWrap(True)
        return label

    def _size_text(self) -> str:
        try:
            if os.path.isdir(self.path):
                total = _tree_size(self.path)
                return human_bytes(total)
            size = os.path.getsize(self.path)
            return f"{human_bytes(size)}（{size:,} 字节）"
        except OSError:
            return "—"

    def _mtime_text(self) -> str:
        try:
            return human_time(os.path.getmtime(self.path))
        except OSError:
            return "—"

    # -- hash --------------------------------------------------------------

    def _hash_progress(self, percent: int) -> None:
        self.bar.setValue(int(percent))

    def cancel_hash(self) -> None:
        """用户不想算了：立刻停掉后台线程（文件夹/已算完时什么也不做）。"""
        task = self._task
        if task is not None:
            task.cancel()
        self.cancel_hash_button.setVisible(False)
        self.bar.setVisible(False)
        if "计算中" in self.hash_label.text():
            self.hash_label.setText("已取消计算（想算的话重新打开属性）")

    def _hash_done(self, digest: str, error: str) -> None:
        self.cancel_hash_button.setVisible(False)
        if error == "cancelled":
            if "计算中" in self.hash_label.text():
                self.hash_label.setText("已取消计算（想算的话重新打开属性）")
            self.bar.setVisible(False)
            return
        if error or not digest:
            self.hash_label.setText(f"算不出来：{error or '读取失败'}")
            self.bar.setVisible(False)
            return
        self.hash_label.setText(digest)
        self.copy_hash_button.setEnabled(True)
        self.bar.setValue(100)
        self.bar.setVisible(False)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """关窗就别再算了：大文件的摘要算完也没人看。"""
        if self._task is not None:
            self._task.cancel()
        super().closeEvent(event)

    def _copy_hash(self) -> None:
        text = self.hash_label.text()
        if text and "计算中" not in text:
            clipboard().setText(text)
            notify(self, "已复制 SHA-256")

    def hash_text(self) -> str:
        """给测试/脚本一个把手：当前的摘要文案（没算完时是「计算中…」）。"""
        return self.hash_label.text()


def _preview_path(path: str, name: str = "") -> None:
    """Open the preview dialog from a table cell.

    The table has no engine handle, so this uses the dialog's defaults: a
    preview only needs the file itself (``core.media`` finds FFmpeg on ``PATH``
    or in the app's data directory on its own).
    """
    if not path or not os.path.exists(path):
        notify(None, "这个文件不在这台机器上，没法预览。")
        return
    preview.FilePreviewDialog(path, name or os.path.basename(path)).exec()


def _reveal_path(path: str) -> None:
    from ..core.platform_open import reveal_in_file_manager

    if not reveal_in_file_manager(path):
        notify(None, "打不开文件管理器：找不到这个文件所在的文件夹。")


def file_actions_menu(
    parent: QWidget,
    path: str,
    name: str = "",
    resend: Callable[[str], None] | None = None,
) -> QMenu:
    """文件动作矩阵（宪章 §4）：气泡附件、待发方块、传输表历史行共用这一份。

    前四项是钉住的（预览 / 打开 / 打开所在文件夹 / 另存为…），之后依次是
    复制路径 / 复制文件 / 删除本地文件 / 重新发送 / 属性。文件不在这台机器上时
    这些动作全部禁用，并在菜单底部写明原因 —— 不做"点了没反应"。
    """
    label = name or os.path.basename(path or "")
    menu = QMenu(parent)
    exists = bool(path) and os.path.exists(path)

    handlers: dict[str, Callable[[], None]] = {
        "preview": lambda: _preview_path(path, label),
        "open": lambda: _open_report(parent, path),
        "reveal": lambda: _reveal_report(parent, path),
        "save_as": lambda: _save_as_report(parent, path, label),
        "copy_path": lambda: _copy_path_report(parent, path),
        "copy_file": lambda: _copy_file_report(parent, path),
        "delete": lambda: delete_local_file(parent, path),
        "properties": lambda: FilePropertiesDialog(path, label, parent).exec(),
    }
    if resend is not None:
        handlers["resend"] = lambda: resend(path)

    for key in FILE_ACTION_LABELS:
        if key in FILE_ACTION_BREAKS:
            menu.addSeparator()
        action = menu.addAction(FILE_ACTION_LABELS[key])
        tip = FILE_ACTION_TIPS.get(key, "")
        enabled = exists
        if key == "resend" and resend is None:
            enabled = False
            tip = "这条记录没有可用的接收方（比如对方已经不在设备列表里）"
        elif key == "resend":
            tip = FILE_ACTION_TIPS["resend"]
        action.setEnabled(enabled)
        if tip:
            action.setToolTip(tip)
        action.setStatusTip(tip)
        handler = handlers.get(key)
        if handler is not None:
            action.triggered.connect(lambda _checked=False, fn=handler: fn())
    if not exists:
        menu.addSeparator()
        note = menu.addAction("文件不在这台机器上")
        note.setEnabled(False)
        note.setToolTip("可能还没接收完，或者已经被移走了")
    return menu


def overflow_menu_button(
    parent: QWidget,
    path: str,
    name: str = "",
    resend: Callable[[str], None] | None = None,
    size: int = 28,
) -> QPushButton:
    """「⋯」溢出按钮：主按钮旁边那一个（宪章 §3.1）。

    视觉上小，命中区 28×28（点不着的小按钮等于没做）；有菜单的 ``QPushButton``
    默认会把文字换成一个小箭头 —— "⋯"就看不见了，所以这里把箭头去掉。
    """
    button = QPushButton("⋯", parent)
    button.setObjectName("CellAction")
    button.setMinimumSize(size, size)
    button.setFixedWidth(size + 2)
    button.setToolTip("更多文件操作（打开 / 在文件夹中显示 / 另存为 / 复制 / 删除 / 重发 / 属性）")
    button.setMenu(file_actions_menu(button, path, name, resend=resend))
    button.setStyleSheet("QPushButton::menu-indicator { image: none; width: 0px; }")
    return button


def file_action_buttons(
    parent: QWidget,
    path: str,
    name: str = "",
    resend: Callable[[str], None] | None = None,
    preview_callback: Callable[[str], None] | None = None,
) -> tuple[QWidget, QPushButton, QPushButton]:
    """「主按钮『预览』+ ⋯」那一对（宪章 §3.1）。

    一个意图一个入口：预览是主按钮，其它动作全在 ⋯ 里。返回
    ``(容器, 预览按钮, ⋯按钮)``，调用方按需要接自己的信号。
    """
    holder = QWidget(parent)
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(theme.SPACE["xs"])
    exists = bool(path) and os.path.exists(path)

    preview_button = QPushButton(FILE_ACTION_LABELS["preview"])
    preview_button.setObjectName("CellAction")
    preview_button.setToolTip(FILE_ACTION_TIPS["preview"])
    preview_button.setEnabled(exists)
    if preview_callback is not None:
        preview_button.clicked.connect(lambda _c=False: preview_callback(path))
    layout.addWidget(preview_button)

    overflow = overflow_menu_button(holder, path, name, resend=resend)
    layout.addWidget(overflow)
    layout.addStretch(1)
    return holder, preview_button, overflow


def _open_report(parent: QWidget, path: str) -> None:
    from ..core.platform_open import open_path_report

    ok, message = open_path_report(path)
    if not ok:
        notify(parent, message or "打不开这个文件")


def _reveal_report(parent: QWidget, path: str) -> None:
    from ..core.platform_open import reveal_in_file_manager

    if not reveal_in_file_manager(path):
        notify(parent, "打不开文件管理器：找不到这个文件所在的文件夹。")


def _save_as_report(parent: QWidget, path: str, name: str) -> None:
    target = save_as(parent, path, name)
    if target:
        notify(parent, f"已另存为 {target}")


def _copy_path_report(parent: QWidget, path: str) -> None:
    if copy_path_to_clipboard(path):
        notify(parent, f"已复制路径：{path}")
    else:
        notify(parent, "没有可复制的路径")


def _copy_file_report(parent: QWidget, path: str) -> None:
    _ok, message = copy_file_to_clipboard(path)
    notify(parent, message)


def _is_loopback_url(url: str) -> bool:
    """True for a URL only this machine can open."""
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return False
    return host in ("127.0.0.1", "::1", "localhost")

class QrDialog(QDialog):
    """Shows a QR code and the URL a phone should open.

    It also **watches for the phone to arrive**.  Without that, the dialog was
    a dead end: the phone is a browser client rather than a peer, so scanning
    the code produces no visible change anywhere in the desktop app, and the
    only sane conclusion for the user was "it did not find my phone".
    """

    def __init__(
        self,
        url: str,
        parent: QWidget | None = None,
        clients=None,
        alternatives: Iterable[str] = (),
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("手机扫码连接")
        self.setMinimumWidth(420)
        #: ``clients()`` of the running browser UI, or ``None`` when there is
        #: no browser UI to ask.
        self._clients = clients

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("用手机相机或浏览器扫这个码")
        title.setObjectName("Title")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        hint = QLabel("手机和电脑要在同一个局域网里。手机上不需要安装任何 App。")
        hint.setObjectName("Subtitle")
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        image = QLabel()
        image.setAlignment(Qt.AlignCenter)
        image.setPixmap(render_qr(url, 320))
        layout.addWidget(image)

        url_label = QLabel(url)
        url_label.setObjectName("Mono")
        url_label.setAlignment(Qt.AlignCenter)
        url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        font = QFont("monospace")
        font.setPointSize(13)
        url_label.setFont(font)
        layout.addWidget(url_label)

        note = QLabel("也可以直接在手机浏览器里输入上面这个地址。")
        note.setObjectName("Subtitle")
        note.setAlignment(Qt.AlignCenter)
        layout.addWidget(note)

        # Multi-homed machines (VPN, docker0, several Wi-Fi cards) are common
        # enough that the *right* address is not always the first one we found.
        # When the phone cannot open the first address, the only difference the
        # user sees is that nothing happened -- so offer the others right here.
        others = [u for u in alternatives if u and u != url]
        if others:
            alt = QLabel("连不上？换成这个地址试试：\n" + "\n".join(others[:3]))
            alt.setObjectName("Subtitle")
            alt.setAlignment(Qt.AlignCenter)
            alt.setWordWrap(True)
            alt.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(alt)

        if _is_loopback_url(url):
            warn = QLabel(
                "⚠️ 这个地址是 127.0.0.1，只有本机能打开。\n"
                "说明没有检测到局域网地址——请先连上 Wi-Fi 或网线，手机才连得过来。"
            )
            warn.setObjectName("Subtitle")
            warn.setAlignment(Qt.AlignCenter)
            warn.setWordWrap(True)
            layout.addWidget(warn)

        self.status = QLabel("")
        self.status.setObjectName("Subtitle")
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        layout.addWidget(close, alignment=Qt.AlignCenter)

        if self._clients is not None:
            self._watch = QTimer(self)
            self._watch.setInterval(1000)
            self._watch.timeout.connect(self._refresh_status)
            self._watch.start()
            self._refresh_status()
        else:
            # No browser UI to ask: saying so here is better than a dialog that
            # silently waits for something that cannot happen.
            self.status.setText("⚠️ 浏览器界面没有启动，手机现在连不上。请看主窗口的提示。")

    def _refresh_status(self) -> None:
        """Show whether the phone has actually opened the page."""
        try:
            clients = list(self._clients() or [])
        except Exception:
            clients = []
        from ..web.server import is_mobile_client

        phones = [c for c in clients if is_mobile_client(c)]
        if phones:
            who = "、".join(
                f"{c.get('address', '?')}（{c.get('label', '浏览器')}）" for c in phones[:3]
            )
            self.status.setText(f"✅ 已连上：{who}\n现在可以在手机上选文件发送，或下载电脑上的文件。")
        else:
            # Only phones count.  A local script polling the API is not "已连上",
            # and saying so once made this dialog congratulate itself while the
            # phone was still stuck on a welcome screen.
            self.status.setText("等待手机打开页面…（手机是浏览器客户端，连上后这里会显示）")


def render_qr(text: str, size: int = 320) -> QPixmap:
    """Render a QR code into a pixmap, drawn by our own encoder."""
    try:
        matrix, _version, _level, _mask = qr.encode(text)
    except qr.QrError:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        return pixmap

    modules = len(matrix)
    border = 4
    # Integer scale keeps every module exactly the same size, which is what
    # cameras need; a fractional scale produces uneven modules that fail to
    # decode at small sizes.
    scale = max(1, size // (modules + border * 2))
    dimension = (modules + border * 2) * scale

    pixmap = QPixmap(dimension, dimension)
    # 二维码必须是"深色模块 + 浅色底"这一种对比（扫码器认的是对比度，不是主题色），
    # 所以这两个颜色**故意**不走 theme。
    pixmap.fill(QColor("#ffffff"))
    painter = QPainter(pixmap)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#000000"))
    for row_index, row in enumerate(matrix):
        col = 0
        while col < modules:
            if not row[col]:
                col += 1
                continue
            start = col
            while col < modules and row[col]:
                col += 1
            painter.drawRect(
                (start + border) * scale,
                (row_index + border) * scale,
                (col - start) * scale,
                scale,
            )
    painter.end()
    return pixmap


# --------------------------------------------------------------------------
# 传输进度（气泡里的那一行）
#
# 真机 P0（docs/REAL_DEVICE_FINDINGS.md §2）暴露的问题：发大文件给手机时，
# 会话里那条记录**从头到尾没有任何进度**，用户看到的是"发出去了，然后什么都不知道"。
# 手机是纯 HTTP 客户端，电脑推不过去 —— 只能发布出去等它来取，进度也只有服务端
# 知道（`note_share_progress` → `shares()` 里的 `sentBytes`）。
#
# 所以气泡里那一行不能只是一句文字，要有**进度条 + 百分比**；文案由这里统一生成，
# 三处（气泡 / 传输表行 / 状态栏）说同一句话。
# --------------------------------------------------------------------------


def transfer_progress_text(
    percent: float,
    done: int = 0,
    total: int = 0,
    verb: str = "传输中",
    speed: str = "",
) -> str:
    """``⬆ 传输中 42% · 240 MB / 572 MB · 12 MB/s`` —— 进度行的统一文案。

    ``percent`` 为 ``None`` 时返回不确定态文案（大小还不知道，比如刚交给手机）。
    """
    if percent is None:
        return "已交给手机，等它下载"
    parts = [f"{verb} {max(0.0, min(100.0, float(percent))):.0f}%"]
    if total > 0:
        parts.append(f"{human_bytes(max(0, int(done)))} / {human_bytes(int(total))}")
    if speed:
        parts.append(speed)
    return " · ".join(parts)


def phone_download_text(percent: float | None, done: int = 0, total: int = 0) -> str:
    """「手机正在取」那条路的文案（真机验收口径 §2.4.3）。

    手机从电脑的网页服务端下载：进度 = 服务端已经发出去的字节数，
    所以动词是「手机正在取」而不是「传输中」。
    """
    if percent is None:
        return "已交给手机，等它下载"
    return transfer_progress_text(percent, done, total, verb="⬇ 手机正在取")


class TransferProgress(QLabel):
    """气泡里的进度行：**一行文字 + 一条细进度条**。

    兼容两套用法（chat_view 现在用的是第一套，一行代码就能升级到第二套）：

    * ``setText("⬆ 传输中 42% · 12 MB/s")`` —— 老用法：只写文字；文字里有百分比
      就顺手把进度条画出来，没有就画不确定态（来回走的短条，表示"在动"）。
    * ``set_progress(42.0, "⬇ 手机正在取 42% · 240 MB / 572 MB")`` —— 新接口：文字 + 条。
    * ``set_progress(None, "已交给手机，等它下载")`` —— 大小还未知：不确定态。

    进度条的颜色取**当前主题**（标签自己的文字色 + 同色低透明度做轨道），
    所以深色/浅色、字号档切换都不用改这里。
    """

    #: 进度条高度（3px：发丝级，不抢文字的注意力）。
    BAR_HEIGHT = 3

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("BubbleState")
        # 文字下面留出画条的地方（4 的倍数）。
        self.setContentsMargins(0, 0, 0, self.BAR_HEIGHT + 1)
        self.setTextFormat(Qt.PlainText)
        self._percent: float | None = None
        self._indeterminate = False
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._advance)
        if str(text or "").strip():
            self.setText(str(text))

    # -- 文字 / 百分比 -----------------------------------------------------

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        """写文字；能解析出百分比就画确定态，否则画不确定态（在动）。"""
        plain = str(text or "")
        super().setText(plain)
        found = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", plain)
        if found:
            self._percent = max(0.0, min(100.0, float(found.group(1))))
            self._indeterminate = False
        else:
            self._percent = None
            self._indeterminate = bool(plain.strip())
        self._sync_timer()
        self.update()

    def set_progress(self, percent: float | None, text: str | None = None) -> None:
        """新接口：``percent`` 给条，``text`` 给文字（``None`` = 保持原文字）。"""
        if percent is None:
            self._percent = None
            self._indeterminate = True
        else:
            self._percent = max(0.0, min(100.0, float(percent)))
            self._indeterminate = False
        if text is not None:
            super().setText(str(text))
            found = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", str(text))
            if found and percent is None:
                self._percent = max(0.0, min(100.0, float(found.group(1))))
                self._indeterminate = False
        self._sync_timer()
        self.update()

    def percent(self) -> float | None:
        return self._percent

    def is_indeterminate(self) -> bool:
        return self._indeterminate

    # -- 动画 --------------------------------------------------------------

    def _sync_timer(self) -> None:
        """只在"看得见 + 不确定态"时跑动画：别为一行提示常驻一个定时器。"""
        if self._indeterminate and self.isVisible():
            if not self._timer.isActive():
                self._timer.start()
        elif self._timer.isActive():
            self._timer.stop()

    def _advance(self) -> None:
        self._phase = (self._phase + 0.05) % 2.0
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().hideEvent(event)
        if self._timer.isActive():
            self._timer.stop()

    # -- 画 ----------------------------------------------------------------

    def _bar_rect(self) -> QRect | None:
        width = self.width()
        if width <= 4 or not str(self.text() or "").strip():
            return None
        top = self.height() - self.BAR_HEIGHT - 1
        if top <= 0:
            return None
        return QRect(0, top, width, self.BAR_HEIGHT)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().paintEvent(event)
        rect = self._bar_rect()
        if rect is None:
            return
        colour = self.palette().color(self.foregroundRole())
        if not colour.isValid():
            colour = QColor(theme.current_text_color())
        track = QColor(colour)
        track.setAlpha(48)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        radius = self.BAR_HEIGHT / 2
        painter.setBrush(track)
        painter.drawRoundedRect(rect, radius, radius)
        painter.setBrush(colour)
        if self._percent is not None:
            width = max(self.BAR_HEIGHT, int(rect.width() * self._percent / 100.0))
            filled = QRect(rect.x(), rect.y(), width, rect.height())
        else:
            # 不确定态：一条短的在轨道上往复（"还在动"，但不知道到了多少）。
            width = max(self.BAR_HEIGHT * 3, int(rect.width() * 0.28))
            travel = rect.width() - width
            position = self._phase if self._phase <= 1.0 else 2.0 - self._phase
            filled = QRect(int(rect.x() + travel * position), rect.y(), width, rect.height())
        painter.drawRoundedRect(filled, radius, radius)
        painter.end()


def badge_pixmap(name: str, color: str, size: int = 14) -> QPixmap:
    """角标位图：优先用 ``icons.py`` 那套线图标，缺了才自绘。

    待发送队列的「时钟」角标必须有 —— 一个空白角标等于没提示。``icons.py``
    现在已经有 ``clock``（见 ``icons.NAMES``），这里保留自绘兜底是为了不让
    图标库的一次改名把界面变成静默失败。
    """
    if name in getattr(icons, "NAMES", ()):
        return icons.icon(name, color, size).pixmap(size, size)
    if name == "clock":
        return _clock_pixmap(color, size)
    return QPixmap()


def _clock_pixmap(color: str, size: int) -> QPixmap:  # pragma: no cover - 兜底
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(size * 2 * 0.09)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    inset = size * 2 * 0.12
    painter.drawEllipse(inset, inset, size * 2 - 2 * inset, size * 2 - 2 * inset)
    centre = size
    painter.drawLine(centre, centre, centre, centre - size * 0.42)
    painter.drawLine(centre, centre, centre + size * 0.30, centre + size * 0.10)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return pixmap


class AttachmentTile(QFrame):
    """一个待发文件：上面是缩略图（或类型图标），下面是文件名。

    规格见 ``docs/UI_LAYOUT.md`` §3⑥：待发的文件不写成一行小字，而是像相册
    那样一块一块排开 —— 图片/视频直接给缩略图，其它给类型图标，名字压在下面。
    点一下就是预览（``desktop/preview.py``），右上角的 ✕ 是"不发送这个"，
    右键是**和气泡、传输表历史行完全相同**的那套文件动作（宪章 §4）。
    """

    preview_requested = Signal(str)
    remove_requested = Signal(str)
    #: 菜单里做过动作之后（比如"删除本地文件"）：待发那一行要重新算一遍。
    refresh_requested = Signal()

    #: 缩略图边长。够看清一张照片，又不至于让输入区吃掉会话的高度。
    THUMB = 64

    def __init__(self, path: str, data_dir: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AttachTile")
        self.path = path
        self.setFixedWidth(self.THUMB + 20)
        self.setCursor(Qt.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACE["xs"], theme.SPACE["xs"], theme.SPACE["xs"], theme.SPACE["xs"])
        layout.setSpacing(theme.SPACE["xs"])

        self.thumb = QFrame()
        self.thumb.setObjectName("AttachThumb")
        self.thumb.setFixedSize(self.THUMB, self.THUMB)
        thumb_layout = QVBoxLayout(self.thumb)
        thumb_layout.setContentsMargins(0, 0, 0, 0)
        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignCenter)
        pixmap = preview.tile_pixmap(path, self.THUMB, data_dir)
        if pixmap is not None:
            self.picture.setPixmap(pixmap)
            self.picture.setObjectName("AttachPicture")
        else:
            self.picture.setPixmap(
                icons.icon(
                    preview.tile_icon_name(path), theme.current_text_color(), 26
                ).pixmap(26, 26)
            )
        thumb_layout.addWidget(self.picture, 1)
        layout.addWidget(self.thumb, 0, Qt.AlignHCenter)

        # ✕ 浮在缩略图右上角（像附件角标那样），布局不叠控件，所以手动摆。
        self.remove_button = QPushButton("✕", self.thumb)
        self.remove_button.setObjectName("TileRemove")
        self.remove_button.setFixedSize(18, 18)
        self.remove_button.setToolTip("不发送这个")
        self.remove_button.setCursor(Qt.PointingHandCursor)
        self.remove_button.clicked.connect(lambda _c=False: self.remove_requested.emit(self.path))

        self.name = ElidedLabel(os.path.basename(path) or path)
        self.name.setObjectName("AttachName")
        self.name.setAlignment(Qt.AlignHCenter)
        self.name.setFixedWidth(self.THUMB + 16)
        layout.addWidget(self.name, 0, Qt.AlignHCenter)

        size_text = human_bytes(_tree_size(path))
        self.setToolTip(f"{path}\n{size_text}　（点击预览，右键更多操作）")

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)

    def _context_menu(self, point) -> None:
        menu = file_actions_menu(
            self,
            self.path,
            os.path.basename(self.path),
            resend=None,
        )
        resend_action = next(
            (action for action in menu.actions() if action.text() == FILE_ACTION_LABELS["resend"]),
            None,
        )
        if resend_action is not None:
            resend_action.setToolTip("这个文件还没发出去，等发送之后再重发")
        menu.exec(self.mapToGlobal(point))
        self.refresh_requested.emit()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self.remove_button.move(self.thumb.width() - self.remove_button.width() - 2, 2)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # 整块（缩略图和名字）都可以点：小图标上抠一个精确的点击区不好用。
        if event.button() == Qt.LeftButton:
            self.preview_requested.emit(self.path)
        super().mouseReleaseEvent(event)


class AttachmentBar(QFrame):
    """待发送的文件：一排缩略图 + 右侧统计，排在输入框**上面**那一行。

    旧版是一张"文件名 + 大小"的表格，后来变成一排 chip；现在按规格改成
    "图标 + 底部文字文件名"的方块，并且把"共 N 项，合计 X MB"挪到这一行 ——
    输入框那一行留到最后一行，只有输入框和按钮。

    对外仍然叫 ``paths`` / ``add_paths`` / ``clear_all`` / ``total_size``：
    窗口和测试都是按这几个名字驱动它的。
    """

    paths_dropped = Signal(list)
    changed = Signal()
    preview_requested = Signal(str)

    def __init__(self, data_dir: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AttachmentBar")
        self.setAcceptDrops(True)
        self.data_dir = data_dir
        self.paths: list[str] = []
        self.tiles: list[AttachmentTile] = []

        row = QHBoxLayout(self)
        row.setContentsMargins(theme.SPACE["md"], theme.SPACE["sm"], theme.SPACE["md"], theme.SPACE["sm"])
        row.setSpacing(theme.SPACE["md"])

        self.hint = QLabel("拖文件或文件夹到这里，或点下面的「传输文件」")
        self.hint.setObjectName("StageHint")
        row.addWidget(self.hint, 1)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFixedHeight(AttachmentTile.THUMB + 40)
        self.scroll.setObjectName("AttachScroll")
        self.host = QWidget()
        self.host_layout = QHBoxLayout(self.host)
        self.host_layout.setContentsMargins(0, 0, 0, 0)
        self.host_layout.setSpacing(theme.SPACE["sm"])
        self.host_layout.addStretch(1)
        self.scroll.setWidget(self.host)
        row.addWidget(self.scroll, 4)

        self.stats_label = QLabel("")
        self.stats_label.setObjectName("Subtitle")
        self.stats_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.stats_label, 0)

        self._refresh()

    # -- staging -----------------------------------------------------------

    def add_paths(self, paths: Iterable[str]) -> int:
        added = 0
        for path in paths:
            absolute = os.path.abspath(path)
            if absolute in self.paths or not os.path.exists(absolute):
                continue
            self.paths.append(absolute)
            added += 1
        if added:
            self.changed.emit()
        self._refresh()
        return added

    def remove_path(self, path: str) -> None:
        if path in self.paths:
            self.paths.remove(path)
            self.changed.emit()
            self._refresh()

    def clear_all(self) -> None:
        if not self.paths:
            return
        self.paths.clear()
        self.changed.emit()
        self._refresh()

    def total_size(self) -> int:
        return sum(_tree_size(path) for path in self.paths)

    def stats_text(self) -> str:
        if not self.paths:
            return ""
        return f"共 {len(self.paths)} 项 · {human_bytes(self.total_size())}"

    def refresh_stats(self) -> None:
        """窗口改了统计口径时调用（现在只有这一处口径，留着给外部一个把手）。"""
        self.stats_label.setText(self.stats_text())
        self.stats_label.setVisible(bool(self.paths))

    # -- drawing -----------------------------------------------------------

    def _refresh(self) -> None:
        # 已经不在磁盘上的（比如刚用「删除本地文件」删掉的那个）从待发里去掉：
        # 发不出去的东西挂在待发行上只会让人以为还能发。
        kept = [path for path in self.paths if os.path.exists(path)]
        if len(kept) != len(self.paths):
            self.paths = kept
            self.changed.emit()
        for tile in self.tiles:
            self.host_layout.removeWidget(tile)
            tile.hide()
            tile.setParent(None)
            tile.deleteLater()
        self.tiles = []

        for path in self.paths:
            tile = AttachmentTile(path, self.data_dir, self.host)
            tile.preview_requested.connect(self.preview_requested)
            tile.remove_requested.connect(self.remove_path)
            tile.refresh_requested.connect(self._refresh)
            self.tiles.append(tile)
            self.host_layout.insertWidget(self.host_layout.count() - 1, tile)

        has_files = bool(self.paths)
        self.hint.setVisible(not has_files)
        self.scroll.setVisible(has_files)
        self.stats_label.setText(self.stats_text())
        self.stats_label.setVisible(has_files)

    # -- drag & drop -------------------------------------------------------

    def _set_active(self, active: bool) -> None:
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_active(True)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._set_active(False)

    def dropEvent(self, event) -> None:  # noqa: N802
        self._set_active(False)
        paths: list[str] = []
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local and os.path.exists(local):
                paths.append(local)
        if paths:
            self.paths_dropped.emit(paths)
            event.acceptProposedAction()


def _tree_size(path: str) -> int:
    """Total size of a file or a directory tree (best effort)."""
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
        return total
    except OSError:
        return 0


class OfferDialog(QDialog):
    """Asks the user whether to accept an incoming transfer."""

    def __init__(self, event: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("收到文件传输请求")
        self.setMinimumWidth(460)
        self.accept_transfer = False
        self.trust_device = False

        peer = event.get("peer")
        name = getattr(peer, "name", "未知设备")
        total = int(event.get("total", 0))
        resume = int(event.get("resume_bytes", 0))
        files = event.get("files", []) or []
        authenticated = bool(event.get("authenticated", False))
        sas = str(event.get("sas", ""))

        layout = QVBoxLayout(self)
        layout.setSpacing(theme.SPACE["md"])

        title = QLabel(f"「{name}」想要发送 {len(files)} 个文件")
        title.setObjectName("Title")
        layout.addWidget(title)

        summary = QLabel(f"共 {human_bytes(total)}")
        summary.setObjectName("Subtitle")
        layout.addWidget(summary)

        if resume > 0:
            resumed = QLabel(
                f"其中 {human_bytes(resume)} 之前已经传过，本次会从断点继续。"
            )
            resumed.setWordWrap(True)
            resumed.setStyleSheet(f"color: {theme.SUCCESS};")
            layout.addWidget(resumed)

        if not authenticated:
            warning = QLabel(
                "⚠ 未能验证对方身份（缺少加密组件）。请确认你认识这台设备。"
            )
            warning.setWordWrap(True)
            warning.setStyleSheet(f"color: {theme.WARNING};")
            layout.addWidget(warning)
        elif sas:
            verify = QLabel(f"安全码：{sas}  （两台设备上显示的数字应一致）")
            verify.setObjectName("Subtitle")
            layout.addWidget(verify)

        table = QTableWidget(len(files), 2)
        table.setHorizontalHeaderLabels(("文件", "大小"))
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setShowGrid(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        for row, item in enumerate(files[:500]):
            table.setItem(row, 0, QTableWidgetItem(str(item.get("name", ""))))
            table.setItem(row, 1, QTableWidgetItem(human_bytes(int(item.get("size", 0)))))
        table.setMaximumHeight(240)
        layout.addWidget(table)

        self.trust_box = QCheckBox("信任这台设备，以后自动接收")
        layout.addWidget(self.trust_box)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        reject = QPushButton("拒绝")
        reject.clicked.connect(self._on_reject)
        buttons.addWidget(reject)
        accept = QPushButton("接收")
        accept.setObjectName("Primary")
        accept.setDefault(True)
        accept.clicked.connect(self._on_accept)
        buttons.addWidget(accept)
        layout.addLayout(buttons)

    def _on_accept(self) -> None:
        self.accept_transfer = True
        self.trust_device = self.trust_box.isChecked()
        self.accept()

    def _on_reject(self) -> None:
        self.accept_transfer = False
        self.reject()


__all__ = [
    "AttachmentBar",
    "AttachmentTile",
    "DeviceAvatar",
    "DeviceCard",
    "DeviceTable",
    "ElidedLabel",
    "FilePropertiesDialog",
    "OfferDialog",
    "QrDialog",
    "SegmentedControl",
    "StatusDot",
    "TransferProgress",
    "badge_pixmap",
    "copy_file_to_clipboard",
    "copy_path_to_clipboard",
    "delete_local_file",
    "elide_text",
    "file_action_buttons",
    "file_actions_menu",
    "file_type_label",
    "graphemes",
    "human_time",
    "icon_button",
    "notify",
    "render_qr",
    "repaint_icons",
    "save_as",
    "phone_download_text",
    "sha256_async",
    "transfer_progress_text",
    "trash_available",
]
