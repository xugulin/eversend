"""The fixed left sidebar: 品牌在顶、设备操作在顶、设备列表在中、传输/设置在底。

Layout follows ``docs/UI_LAYOUT.md`` §1–§2 and the charter's §3.1 table.  The
device list *is* the conversation list here: clicking a device opens the session
with it on the right.

这一轮按宪章 §3.1 删掉/合并的重复入口：

* 「⟳ 刷新」 + 「◎ 扫描局域网」 → **一个「扫描」**：先催一次公告，没人应答就
  自己升级成主动扫描（见 :meth:`Sidebar.scan_clicked` 的接线）；
* 「＋ 手动添加 IP」 → 降级进「扫描」右边那个小箭头的下拉里；
* 「📱 手机连接」（以及下拉里那份二维码） → **一个「连接手机」**，点击直接出二维码；
* 唯一的页面入口：底部「传输 / 设置」。主区顶部那个 `[会话｜传输]` 分段控件删掉了。
"""

from __future__ import annotations

from typing import Any, Iterable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.model import Peer
from . import icons, theme
from .widgets import DeviceTable, ElidedLabel, icon_button

#: 侧栏宽度（宪章 §2.2：电脑端与网页版宽屏都是 288 px）。
SIDEBAR_WIDTH = theme.SIDEBAR_WIDTH

#: 设备超过这个数量才出现搜索框（宪章 §3.1 ③）。
SEARCH_THRESHOLD = 6


def identity_of(peer: Peer) -> str:
    """这台设备的**稳定身份**（宪章铁律 6：去重键不是显示名、不是地址）。

    协议设备用 ``deviceId``；网页/手机客户端在窗口那侧已经被规范化成
    ``web:<设备号>``；两样都没有时才退回 ``Peer.key``（地址:端口）。
    """
    return str(peer.info.device_id or "").strip() or str(peer.key or "")


def peer_is_online(peer: Peer) -> bool:
    """``device_is_online`` 的同义函数（模块内读起来顺一点）。"""
    return device_is_online(peer)


def dedupe_peers(peers: Iterable[Peer]) -> list[Peer]:
    """一台物理设备只留一行。

    宪章 C4/C9：同一台设备在列表里**永远只有一行**。同一个身份出现两次时，
    留下"现在能用"的那一条（在线的胜过离线的、离线的胜过只剩聊天记录的），
    并列时保留先出现的那条 —— 顺序不能因为去重而乱跳。
    """
    order: list[str] = []
    best: dict[str, Peer] = {}
    for peer in peers:
        key = identity_of(peer)
        current = best.get(key)
        if current is None:
            best[key] = peer
            order.append(key)
            continue
        if peer_rank(peer) > peer_rank(current):
            best[key] = peer
    return [best[key] for key in order]


def is_phone(peer: Peer) -> bool:
    """这台设备是不是"手机"（安卓 App 或手机浏览器）。

    两者都走 HTTP：本机**推不过去**（App 进程没有 LISTEN 端口），只能把文件发布
    出去等它来取 —— 所以设备行上必须一眼分得出来，见
    ``docs/REAL_DEVICE_FINDINGS.md`` §2.3。
    """
    caps = peer.info.capabilities or {}
    if caps.get("web") or peer.source == "web":
        return True
    cap_kind = str(caps.get("clientKind") or "")
    if cap_kind in ("app", "browser"):
        return True
    return str(peer.info.platform or "").lower() in ("android", "ios")


def peer_search_keywords(peer: Peer) -> str:
    """搜索框里能匹配到的词（让"手机 / 网页版 / 安卓"都能搜出来）。"""
    caps = peer.info.capabilities or {}
    words = [str(peer.info.name or ""), str(peer.address or ""), identity_of(peer)]
    if is_phone(peer):
        words.append("手机")
        words.append("安卓 App" if str(caps.get("clientKind") or "") == "app" else "网页版")
    else:
        words.append(PLATFORM_WORDS.get(str(peer.info.platform or "").lower(), ""))
        words.append(SOURCE_WORDS.get(str(peer.source or ""), ""))
    return " ".join(word for word in words if word).casefold()


#: 搜索用（也是设备行副标题该写的那几种"是什么"）。
PLATFORM_WORDS = {
    "linux": "Linux",
    "windows": "Windows",
    "macos": "macOS",
    "darwin": "macOS",
    "android": "安卓",
    "ios": "iOS",
    "browser": "网页版",
}

SOURCE_WORDS = {
    "multicast": "组播发现",
    "broadcast": "广播发现",
    "mdns": "mDNS 发现",
    "scan": "扫描发现",
    "manual": "手动添加",
    "incoming": "它连过来的",
    "local": "本机",
    "history": "记录里的会话",
    "offline": "以前见过",
}


def peer_rank(peer: Peer) -> int:
    """排序权重：能直接收发的排前面。"""
    if peer.source == "history":
        return 0
    if peer.source == "offline":
        return 1
    return 3 if device_is_online(peer) else 2


class _CopyableLabel(QLabel):
    """本机地址：点一下就复制（宪章 §3.1 ①：本机地址（可点复制））。"""

    clicked = Signal()

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点一下复制本机地址")

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class NavItem(QPushButton):
    """A sidebar-bottom navigation row: icon + label (+ an optional count)."""

    def __init__(self, icon_name: str, label: str, parent: QWidget | None = None) -> None:
        super().__init__(f"  {label}", parent)
        self.setObjectName("NavItem")
        self.setCursor(Qt.PointingHandCursor)
        self.setCheckable(True)
        self.setProperty("iconName", icon_name)
        self.setIcon(icons.icon(icon_name, theme.current_text_color(), 17))
        self._label = label

    def set_count(self, count: int) -> None:
        """Show how many transfers are running, the way a chat app shows unread."""
        self.setText(f"  {self._label}" + (f"   （{count}）" if count else ""))


class DeviceSection(QWidget):
    """设备列表的一段：标题可点，整段展开或折叠。

    以前所有设备挤在一个列表里，一台两小时前见过的手机和正在用的电脑长得
    一样，唯一的区别是右边那个小徽标。现在按用户要求分成「在线」「离线」两段，
    标题上带数量，离线那一段默认收起 —— 列表一眼就只剩现在能用的设备。
    """

    peer_activated = Signal(object)
    context_requested = Signal(object, object)
    toggled = Signal(bool)
    #: 右键标题：目前只用在「离线」那一段（清空列表）。
    title_context = Signal(str)

    #: 一段最多占多高（再多就在表格里自己滚）。取"够看几台设备"的值，
    #: 因为折叠的那一段必须能紧贴上一行 —— 以前它按弹性空间撑开，收起之后
    #: 还占着几百像素，下面那一段被推到很远的地方。侧栏会按窗口高度调这个上限
    #: （见 ``Sidebar._apply_section_caps``）。
    MAX_HEIGHT = 320

    def __init__(self, title: str, expanded: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = title
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE["xs"])

        self.header = QPushButton()
        self.header.setObjectName("SectionToggle")
        self.header.setCursor(Qt.PointingHandCursor)
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setToolTip(f"展开或收起「{title}」")
        self.header.clicked.connect(self._on_toggle)

        header_row = QHBoxLayout(self.header)
        header_row.setContentsMargins(theme.SPACE["sm"], theme.SPACE["xs"], theme.SPACE["sm"], theme.SPACE["xs"])
        header_row.setSpacing(theme.SPACE["xs"])
        self.chevron = QLabel()
        self.chevron.setFixedSize(12, 12)
        header_row.addWidget(self.chevron)
        self.label = QLabel(title)
        self.label.setObjectName("SectionTitle")
        header_row.addWidget(self.label)
        self.count = QLabel("0")
        self.count.setObjectName("CountBadge")
        header_row.addWidget(self.count)
        header_row.addStretch(1)
        self.header.setContextMenuPolicy(Qt.CustomContextMenu)
        self.header.customContextMenuRequested.connect(self._on_title_menu)
        layout.addWidget(self.header)

        self.empty = QLabel("")
        self.empty.setObjectName("EmptyHint")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)

        self.table = DeviceTable()
        self.table.peer_activated.connect(self.peer_activated)
        self.table.context_requested.connect(self.context_requested)
        self.table.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        layout.addWidget(self.table, 0)
        # 段自己按内容占位（不抢弹性空间，多余的高度落到下面去，见 Sidebar）。
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

        self._sync()

    # -- 状态 --------------------------------------------------------------

    def is_expanded(self) -> bool:
        return self.header.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        if self.header.isChecked() != expanded:
            self.header.setChecked(expanded)
        self._sync()
        self.toggled.emit(self.is_expanded())

    def _on_title_menu(self, point) -> None:
        menu = QMenu(self)
        action = menu.addAction("展开" if not self.is_expanded() else "收起")
        action.triggered.connect(lambda: self.set_expanded(not self.is_expanded()))
        if self.title == "离线":
            menu.addSeparator()
            clear = menu.addAction("清空离线设备列表…")
            clear.triggered.connect(lambda: self.title_context.emit("clear-offline"))
        menu.exec(self.header.mapToGlobal(point))

    def _on_toggle(self) -> None:
        self._sync()
        self.toggled.emit(self.is_expanded())

    def set_max_height(self, cap: int) -> None:
        if cap != self.MAX_HEIGHT:
            self.MAX_HEIGHT = max(theme.SPACE["3xl"] * 3, int(cap))
            self._sync()

    def content_height(self) -> int:
        """表格要按内容算高度：空表不该是一大块空白，两行也不该占满侧栏。"""
        table = self.table
        if table.count() == 0:
            return 0
        rows = 0
        for index in range(table.count()):
            item = table.item(index)
            widget = table.itemWidget(item) if item is not None else None
            # 行高要问行里的控件：QListWidget 自己的 sizeHintForRow 不认识
            # setItemWidget 放进去的卡片，会回一个默认值（192px），一台设备
            # 也能撑出一大块空白来。
            if widget is not None:
                rows += max(widget.sizeHint().height(), widget.minimumSizeHint().height())
            else:
                rows += max(1, table.sizeHintForRow(index))
        spacing = table.spacing() * max(0, table.count() - 1)
        return rows + spacing + 2 * table.frameWidth()

    def _sync(self) -> None:
        expanded = self.header.isChecked()
        self.chevron.setPixmap(
            icons.icon(
                "chevron-down" if expanded else "chevron-right",
                theme.current_text_color(),
                12,
            ).pixmap(12, 12)
        )
        self.table.setVisible(expanded)
        self.empty.setVisible(expanded and self.table.count() == 0)
        if not expanded:
            # 收起时把选中项清掉：隐藏的选中项既看不见、又会被"发给几台"数进去。
            self.table.clearSelection()
        if expanded:
            # 定高（= 内容高度，封顶 MAX_HEIGHT，再多就在表格里自己滚）。
            # 不要再单独 setMaximumHeight：那会把刚定好的高度又放开，
            # 表格于是回到 sizeHint（192）而不是按内容算出来的高度。
            height = min(self.content_height(), self.MAX_HEIGHT)
            self.table.setFixedHeight(max(height, theme.ROW_HEIGHT - theme.SPACE["xs"]))
        else:
            self.table.setFixedHeight(0)
        self.updateGeometry()

    # -- 内容 --------------------------------------------------------------

    def set_peers(
        self, peers: list[Peer], meta: dict[str, dict] | None = None, phones: int = 0
    ) -> None:
        self.table.set_peers(peers, meta)
        # 数量徽标里把手机单列出来：手机上要"等它来取"，跟电脑不是一回事，
        # 用户扫一眼这段就知道有没有手机在里面。
        self.count.setText(f"{len(peers)}" + (f" · 手机 {phones}" if phones else ""))
        self.count.setToolTip(
            f"{self.title}：{len(peers)} 台" + (f"，其中手机 {phones} 台" if phones else "")
        )
        self.count.setVisible(True)
        if not peers:
            self.empty.setText(
                "现在没有在线的设备 —— 让对方打开韧传，或点「扫描」。"
                if self.title == "在线"
                else "没有离线的设备。"
            )
        elif self.title == "在线" and phones:
            self.empty.setText("")
        self._sync()

    @property
    def peers(self) -> list[Peer]:
        return list(self.table.peers)


class Sidebar(QFrame):
    """The whole left column."""

    #: 设备操作。扫描 = 催公告 + 主动扫描（宪章 §3.1：两个按钮合并成一个）。
    scan_clicked = Signal()
    manual_clicked = Signal()
    qr_clicked = Signal()
    new_group_clicked = Signal()
    #: 选中
    peer_activated = Signal(object)
    context_requested = Signal(object, object)
    group_activated = Signal(str)
    #: 段标题右键触发的动作（"clear-offline" 等）。
    section_action = Signal(str)
    #: 本机地址被点了一下（窗口负责把它复制进剪贴板并给一句反馈）。
    address_clicked = Signal(str)
    #: 底部导航
    transfers_clicked = Signal()
    settings_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(SIDEBAR_WIDTH)
        self._peers: list[Peer] = []
        self._meta: dict[str, dict] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(theme.SPACE["md"], theme.SPACE["md"], theme.SPACE["md"], theme.SPACE["md"])
        root.setSpacing(theme.SPACE["sm"])

        # ---- ① 品牌 + 本机信息 ------------------------------------------
        brand = QHBoxLayout()
        brand.setSpacing(theme.SPACE["sm"])
        mark = QLabel()
        mark.setPixmap(theme.app_icon(26, theme.is_dark()).pixmap(26, 26))
        mark.setFixedSize(26, 26)
        brand.addWidget(mark)
        name = QLabel("韧传")
        name.setObjectName("BrandName")
        brand.addWidget(name)
        sub = QLabel("EverSend")
        sub.setObjectName("BrandSub")
        brand.addWidget(sub)
        brand.addStretch(1)
        root.addLayout(brand)

        self.device_name_label = QLabel("本机")
        self.device_name_label.setObjectName("DeviceName")
        root.addWidget(self.device_name_label)

        info_row = QHBoxLayout()
        info_row.setSpacing(theme.SPACE["sm"])
        self.device_address_label = _CopyableLabel("正在检测网络…")
        self.device_address_label.setObjectName("Subtitle")
        self.device_address_label.setWordWrap(True)
        self.device_address_label.clicked.connect(self._copy_address)
        info_row.addWidget(self.device_address_label, 1)
        self.security_label = QLabel("")
        self.security_label.setObjectName("Subtitle")
        info_row.addWidget(self.security_label, 0, Qt.AlignRight | Qt.AlignVCenter)
        root.addLayout(info_row)

        # ---- ② 设备操作：一行图标按钮 ------------------------------------
        # 宪章 §3.1：刷新 + 扫描 = 一个「扫描」；手动输入地址降级到扫描右边的小箭头里；
        # 手机连接（连同下拉里那份二维码）= 一个「连接手机」，点了直接出二维码。
        actions = QHBoxLayout()
        actions.setSpacing(theme.SPACE["xs"])
        self.scan_button = icon_button(
            "scan", "扫描局域网：先催一次公告，没人应答就主动探测整个网段"
        )
        self.scan_button.clicked.connect(self.scan_clicked)
        actions.addWidget(self.scan_button)

        self.scan_menu_button = QPushButton()
        self.scan_menu_button.setObjectName("IconButton")
        self.scan_menu_button.setCursor(Qt.PointingHandCursor)
        self.scan_menu_button.setFixedSize(16, theme.CONTROL["md"])
        self.scan_menu_button.setIcon(icons.icon("chevron-down", theme.current_text_color(), 12))
        self.scan_menu_button.setToolTip("扫描选项：找不到对方时手动输入地址")
        scan_menu = QMenu(self.scan_menu_button)
        manual_action = scan_menu.addAction("手动输入地址…")
        manual_action.triggered.connect(self.manual_clicked)
        self.scan_menu_button.setMenu(scan_menu)
        actions.addWidget(self.scan_menu_button)

        actions.addSpacing(theme.SPACE["xs"])
        self.phone_button = icon_button("phone", "连接手机：扫码即用，手机不用装 App")
        self.phone_button.clicked.connect(self.qr_clicked)
        actions.addWidget(self.phone_button)

        self.group_button = icon_button("users", "新建群聊")
        self.group_button.clicked.connect(self.new_group_clicked)
        actions.addWidget(self.group_button)
        actions.addStretch(1)
        root.addLayout(actions)

        # 旧名字：以前那一排里有两个按钮叫 refresh_button / qr_button，测试和窗口
        # 都按名字取。刷新已经并进扫描，那个名字留着会让人以为还有第二个入口 ——
        # 所以 refresh_button 不再存在，扫描就是扫描。
        self.qr_button = self.phone_button

        # ---- ③ 设备列表：在线 / 离线两段，各自可折叠 ----------------------
        heading = QHBoxLayout()
        heading.setSpacing(theme.SPACE["xs"])
        self.section_title = QLabel("设备列表")
        self.section_title.setObjectName("SectionTitle")
        heading.addWidget(self.section_title)
        self.device_count = QLabel("0")
        self.device_count.setObjectName("CountBadge")
        heading.addWidget(self.device_count)
        heading.addStretch(1)
        root.addLayout(heading)

        # 设备多到一眼扫不完时才出现搜索框（宪章 §3.1 ③：>6 台才出现）。
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("Search")
        self.search_edit.setPlaceholderText("搜索设备")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(lambda _text: self._apply_search())
        self.search_edit.setVisible(False)
        root.addWidget(self.search_edit)

        self.online_section = DeviceSection("在线", expanded=True)
        self.online_section.peer_activated.connect(self.peer_activated)
        self.online_section.context_requested.connect(self.context_requested)
        self.online_section.toggled.connect(lambda _open: self._apply_section_caps())
        root.addWidget(self.online_section, 0)

        self.offline_section = DeviceSection("离线", expanded=False)
        self.offline_section.peer_activated.connect(self.peer_activated)
        self.offline_section.context_requested.connect(self.context_requested)
        self.offline_section.toggled.connect(lambda _open: self._apply_section_caps())
        self.offline_section.title_context.connect(self.section_action)
        root.addWidget(self.offline_section, 0)
        # 设备两段都按内容定高、都不吃弹性：多出来的高度落在这里，
        # 于是"收起的那一段紧贴「设备列表」下边"，群聊和底部导航仍旧贴底。
        root.addStretch(1)

        #: 旧名字：在线那一张表（窗口和测试都按这个名字取设备）
        self.device_table = self.online_section.table
        self.offline_table = self.offline_section.table

        # 群聊接在设备下面（群聊不是设备，所以用一条细分隔线分开）
        self.group_header = QWidget()
        group_row = QHBoxLayout(self.group_header)
        group_row.setContentsMargins(0, theme.SPACE["xs"], 0, 0)
        self.group_title = QLabel("群聊")
        self.group_title.setObjectName("SectionTitle")
        group_row.addWidget(self.group_title)
        group_row.addStretch(1)
        root.addWidget(self.group_header)

        self.group_list = QListWidget()
        self.group_list.setObjectName("ConvList")
        self.group_list.setMaximumHeight(150)
        self.group_list.itemClicked.connect(self._on_group_clicked)
        root.addWidget(self.group_list)
        self.group_header.setVisible(False)
        self.group_list.setVisible(False)

        root.addStretch(0)

        # ---- 底部：传输 / 设置（唯一的页面入口）--------------------------
        self.transfers_nav = NavItem("transfer", "传输")
        self.transfers_nav.clicked.connect(self.transfers_clicked)
        root.addWidget(self.transfers_nav)

        self.settings_nav = NavItem("settings", "设置")
        self.settings_nav.clicked.connect(self.settings_clicked)
        root.addWidget(self.settings_nav)

        # 两段的高度上限按窗口高度算（要等两段都建好）。
        self._apply_section_caps()

    # -- filling -----------------------------------------------------------

    def set_self_info(self, name: str, addresses: str) -> None:
        self.device_name_label.setText(name)
        self.device_address_label.setText(addresses)
        self.device_address_label.setToolTip(f"点一下复制本机地址\n{addresses}")

    def _copy_address(self) -> None:
        text = self.device_address_label.text()
        clipboard = QApplication.clipboard()
        if clipboard is not None and text:
            clipboard.setText(text.split("  ·  ")[0].strip())
        self.address_clicked.emit(text)

    def set_security(self, text: str, color: str = "") -> None:
        """一行小字说明这次传输加不加密（以前在顶部卡片里）。"""
        self.security_label.setText(text)
        self.security_label.setStyleSheet(f"color: {color};" if color else "")

    #: 两段的表格最少留多高（两三行），再小就没法用了。
    MIN_SECTION_ROOM = 140

    def _apply_section_caps(self) -> None:
        """按窗口高度给两段分上限：加起来不会把群聊和底部导航挤出窗口。

        "除了设备两段之外还占多少"是**量出来的**（布局的 sizeHint 减去两段的
        sizeHint），不是写死的常数：品牌区、操作行、群聊有没有内容、底部导航
        都会变，猜一个数总会在某个窗口尺寸上出问题（猜小了表格被裁掉最后一行，
        猜大了「传输 / 设置」被挤出窗口）。
        """
        layout = self.layout()
        if layout is None:
            return
        sections = self.online_section.sizeHint().height() + self.offline_section.sizeHint().height()
        chrome = max(0, layout.sizeHint().height() - sections)
        online_open = self.online_section.is_expanded()
        offline_open = self.offline_section.is_expanded()
        # 展开着的那一段除了表格还有一行标题和间距，也要算进去（少算 26px
        # 就会正好把表格最后一行裁掉）。
        overhead = sum(
            section.header.sizeHint().height() + theme.SPACE["xs"]
            for section, is_open in (
                (self.online_section, online_open),
                (self.offline_section, offline_open),
            )
            if is_open
        )
        # 再留一点余量给布局间距，免得正好卡在边界上。
        room = max(self.MIN_SECTION_ROOM, self.height() - chrome - theme.SPACE["md"] - overhead)
        if online_open and offline_open:
            online_cap = int(room * 0.58)
            offline_cap = room - online_cap
        else:
            online_cap = offline_cap = room
        self.online_section.set_max_height(online_cap)
        self.offline_section.set_max_height(offline_cap)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._apply_section_caps()

    # -- 设备列表 -----------------------------------------------------------

    def set_devices(self, peers: list[Peer], meta: dict[str, dict] | None = None) -> None:
        """Fill both sections; 在线 is what you can talk to right now.

        **一台物理设备一行**（宪章 C4）：按 ``deviceId`` 去重之后才分段、才计数。
        以前同一台手机的两条注册表记录会并排出现两次，用户截图上就是"同一个
        设备重复显示"。
        """
        self._peers = dedupe_peers(peers)
        self._meta = meta or {}
        self.search_edit.setVisible(len(self._peers) > SEARCH_THRESHOLD)
        self._apply_search()

    def _apply_search(self) -> None:
        query = self.search_edit.text().strip().casefold()
        peers = self._peers
        if query:
            peers = [peer for peer in peers if query in peer_search_keywords(peer)]
        online = [peer for peer in peers if device_is_online(peer)]
        offline = [peer for peer in peers if not device_is_online(peer)]
        # 在线段：手机（HTTP 客户端，要等它来取）排在前面 —— 刚连上的手机就是用户
        # 此刻要找的那一行；稳定排序，同一类里保持原来的顺序。
        online.sort(key=lambda peer: 0 if is_phone(peer) else 1)
        meta = dict(self._meta)
        for peer in peers:
            entry = dict(meta.get(peer.key) or {})
            entry["kind"] = "phone" if is_phone(peer) else "device"
            meta[peer.key] = entry
        self.online_section.set_peers(
            online, meta, phones=len([peer for peer in online if is_phone(peer)])
        )
        self.offline_section.set_peers(offline, meta)
        self.device_count.setText(str(len(self._peers)))
        # 选中的那台在离线那一段里 → 自动把它展开，别让选中行躲在折起来的地方。
        current = self.device_table.currentRow() >= 0 or self.offline_table.currentRow() >= 0
        if not current and offline:
            self.offline_section.set_expanded(False)
        self._apply_section_caps()

    def shown_peers(self) -> list[Peer]:
        """现在列表里真正显示着的设备（搜索之后）。"""
        return self.online_section.peers + self.offline_section.peers

    def set_groups(self, groups: list[dict[str, Any]], titles: dict[str, str]) -> None:
        """Fill the 群聊 section (it disappears when there are no groups)."""
        self.group_list.clear()
        for conversation in groups:
            conv_id = str(conversation.get("id") or "")
            title = titles.get(conv_id) or conv_id
            row = ElidedLabel(title)
            row.setObjectName("ConvName")
            item = QListWidgetItem()
            item.setData(Qt.UserRole, conv_id)
            item.setSizeHint(row.sizeHint())
            item.setToolTip(title)
            unread = int(conversation.get("unread") or 0)
            if unread:
                item.setText(f"{title}   （{unread} 条未读）")
            self.group_list.addItem(item)
            self.group_list.setItemWidget(item, row)
        has_groups = self.group_list.count() > 0
        self.group_header.setVisible(has_groups)
        self.group_list.setVisible(has_groups)

    def select_group(self, conv_id: str) -> None:
        for row in range(self.group_list.count()):
            if self.group_list.item(row).data(Qt.UserRole) == conv_id:
                self.group_list.setCurrentRow(row)
                return

    def _on_group_clicked(self, item: QListWidgetItem) -> None:
        self.group_activated.emit(str(item.data(Qt.UserRole) or ""))

    def set_active_page(self, page: str) -> None:
        """Highlight 传输 / 设置 in the bottom navigation."""
        self.transfers_nav.setChecked(page == "transfers")
        self.settings_nav.setChecked(page == "settings")
        for nav in (self.transfers_nav, self.settings_nav):
            nav.style().unpolish(nav)
            nav.style().polish(nav)

    def set_transfer_count(self, count: int) -> None:
        self.transfers_nav.set_count(max(0, int(count)))


def device_is_online(peer: Peer) -> bool:
    """这台设备现在能不能直接收发。

    三种情况算在线：协议设备（刚被发现/刚说过话）、正连着的手机（``online``）、
    本机实例。会话记录里的"历史设备"（``source == "history"``）永远是离线 ——
    它本来就是因为再也见不到才被合成的。
    """
    if peer.source == "history":
        return False
    return bool((peer.info.capabilities or {}).get("online", True))


__all__ = [
    "SEARCH_THRESHOLD",
    "SIDEBAR_WIDTH",
    "DeviceSection",
    "NavItem",
    "Sidebar",
    "dedupe_peers",
    "device_is_online",
    "identity_of",
    "is_phone",
    "peer_search_keywords",
]
