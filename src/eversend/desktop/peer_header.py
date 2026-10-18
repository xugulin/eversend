"""主区顶部那一条：正在和谁说话。

``docs/UI_LAYOUT.md`` §3④ 要求这里写清对方**是什么**（类型 · 平台 · 版本 · 设备号
前 8 位）、**在哪**（地址:端口 · 发现方式 · 延迟 / 最后在线）、**什么状态**
（在线 / 已离线 / 已信任 / 需要 PIN）。

宪章 §3.1 这一轮删掉的东西：右边那个 `[会话｜传输]` 分段控件。页面入口现在只有
侧栏底部导航一个（铁律 1：一个意图一个入口）。剩下的动作按 §3.1 的表格收进 ``⋯``：
会话页给「清空会话…」，传输页的那些动作在传输页自己的工具条上（那里才有
「打开接收文件夹」主按钮），这里就不重复摆一份。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.model import Peer
from . import icons, theme
from .widgets import icon_button

PLATFORM_NAMES = {
    "linux": "Linux",
    "windows": "Windows",
    "darwin": "macOS",
    "macos": "macOS",
    "android": "安卓",
    "ios": "iOS",
    "browser": "网页版",
}

KIND_NAMES = {"desktop": "电脑", "mobile": "手机", "server": "服务器"}


def _chip(text: str, state: str = "") -> QLabel:
    label = QLabel(text)
    label.setObjectName("Pill")
    if state:
        label.setProperty("state", state)
    label.setAlignment(Qt.AlignCenter)
    return label


class PeerHeader(QFrame):
    """对方设备的详细信息 + 会话动作（``⋯``）。"""

    #: 兼容 ``SessionView``：本版没有分段控件了，这个信号不再发出，但保留着
    #: 不会让老接线炸掉（页面切换由侧栏底部导航发起，见 ``MainWindow.show_page``）。
    view_changed = Signal(str)
    #: ``⋯`` 里的「清空会话…」（只在会话页出现）。
    clear_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PeerHeader")
        self._peer: Peer | None = None
        self._view = "chat"

        root = QHBoxLayout(self)
        root.setContentsMargins(theme.SPACE["lg"], theme.SPACE["md"], theme.SPACE["lg"], theme.SPACE["md"])
        root.setSpacing(theme.SPACE["md"])

        self.avatar = QLabel("💬")
        self.avatar.setObjectName("PeerAvatar")
        self.avatar.setFixedSize(44, 44)
        self.avatar.setAlignment(Qt.AlignCenter)
        root.addWidget(self.avatar, 0, Qt.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(theme.SPACE["xs"])

        name_row = QHBoxLayout()
        name_row.setSpacing(theme.SPACE["sm"])
        self.name_label = QLabel("从左边选一台设备")
        self.name_label.setObjectName("PeerName")
        name_row.addWidget(self.name_label)

        self.chips: list[QLabel] = []
        self.chip_row = name_row
        name_row.addStretch(1)
        text.addLayout(name_row)

        self.detail_label = QLabel("点一下设备列表里的任意一台，就能和它聊天、传文件。")
        self.detail_label.setObjectName("PeerDetail")
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text.addWidget(self.detail_label)

        # 地址/来源那一行：**同一件事只写一遍**（宪章 C9）。
        self.where_label = QLabel("")
        self.where_label.setObjectName("PeerDetail")
        self.where_label.setWordWrap(True)
        self.where_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text.addWidget(self.where_label)

        # 活动行（宪章 E3：任何时刻说清"现在在干什么"）。默认不占位 ——
        # 没有事情在发生时它整行隐藏，页头不会挂着一句过期的话。
        self.activity_row = QWidget()
        activity = QHBoxLayout(self.activity_row)
        activity.setContentsMargins(0, 0, 0, 0)
        activity.setSpacing(theme.SPACE["xs"])
        self.activity_dot = QLabel()
        self.activity_dot.setFixedSize(8, 8)
        activity.addWidget(self.activity_dot, 0, Qt.AlignVCenter)
        self.activity_label = QLabel("")
        self.activity_label.setObjectName("PeerDetail")
        self.activity_label.setWordWrap(True)
        activity.addWidget(self.activity_label, 1)
        self.activity_row.setVisible(False)
        text.addWidget(self.activity_row)
        root.addLayout(text, 1)

        right = QHBoxLayout()
        right.setSpacing(theme.SPACE["sm"])
        right.addStretch(1)
        self.more_button = icon_button("more", "更多：清空这个会话")
        self.menu = QMenu(self.more_button)
        self.clear_action = self.menu.addAction("清空会话…")
        self.clear_action.setToolTip("把这个会话里的消息从本机删掉（对方那边的记录不受影响）")
        self.clear_action.triggered.connect(self.clear_clicked)
        self.more_button.setMenu(self.menu)
        right.addWidget(self.more_button, 0, Qt.AlignTop)
        root.addLayout(right)

        self.set_view("chat")

    # -- state -------------------------------------------------------------

    def set_view(self, key: str) -> None:
        """会话页 / 传输页。``⋯`` 的内容跟着换（动作不重复摆两处）。"""
        self._view = "transfers" if key == "transfers" else "chat"
        # 传输页的动作（打开接收文件夹 / 更改接收位置 / 清空记录）在传输页自己的
        # 工具条上；会话页这边只留「清空会话…」，所以传输页时整颗 ⋯ 收起来。
        self.more_button.setVisible(self._view == "chat")

    def view(self) -> str:
        return self._view

    def _set_chips(self, chips: list[tuple[str, str]]) -> None:
        for chip in self.chips:
            self.chip_row.removeWidget(chip)
            chip.hide()
            chip.setParent(None)
            chip.deleteLater()
        self.chips = []
        # 插在名字后面、弹簧前面（弹簧永远是最后一个）。
        for index, (text, state) in enumerate(chips):
            chip = _chip(text, state)
            self.chips.append(chip)
            self.chip_row.insertWidget(min(index + 1, self.chip_row.count() - 1), chip)

    def set_peer(self, peer: Peer | None) -> None:
        if peer is not getattr(self, "_peer", None):
            # 换了对象：上一台设备的"正在传…"不能留在页头上（过期文案比空白更糟）。
            self.clear_activity()
        self._peer = peer
        if peer is None:
            self.avatar.setText("💬")
            self.name_label.setText("从左边选一台设备")
            self.detail_label.setText("点一下设备列表里的任意一台，就能和它聊天、传文件。")
            self.where_label.setText("")
            self.name_label.setToolTip("")
            self._set_chips([])
            return

        info = peer.info
        caps = info.capabilities or {}
        online = bool(caps.get("online", True))
        is_web = bool(caps.get("web"))
        is_app = str(caps.get("clientKind") or "") == "app" or info.platform == "android"

        self.avatar.setText(theme.platform_glyph(info.platform))
        name = info.name or "（没有名字的设备）"
        self.name_label.setText(name)
        self.name_label.setToolTip(name)

        # 第一行：它**是什么**（类型 · 平台 · 版本 · 设备号）。名字已经在标题上，
        # 不再重复；地址也不在这一行（下面是它唯一出现的地方）。
        parts = [KIND_NAMES.get(info.kind, info.kind or "设备")]
        if is_web:
            parts.append("安卓 App" if is_app else "网页版")
        else:
            platform = PLATFORM_NAMES.get(str(info.platform).lower(), info.platform)
            if platform:
                parts.append(platform)
        if info.version and info.version not in ("web", "app"):
            parts.append("韧传 " + info.version)
        if info.device_id and not info.device_id.startswith("web:"):
            parts.append("ID " + info.device_id[:8])
        self.detail_label.setText(" · ".join(parts))

        # 第二行：它**在哪**、**怎么发现的**、多久没见了。每个事实只出现一次。
        where: list[str] = []
        if peer.address:
            where.append(f"{peer.address}:{peer.port}" if peer.port else peer.address)
        else:
            where.append("地址未知")
        where.append(
            {
                "multicast": "组播发现",
                "broadcast": "广播发现",
                "mdns": "mDNS 发现",
                "scan": "扫描发现",
                "manual": "手动添加",
                "incoming": "它连过来的",
                "local": "本机",
                # ``source == "web"`` 只说明"它是连过来的 HTTP 客户端"（手机 App 或
                # 浏览器），**不等于扫码**：真机实测里手机是组播自动发现自己连上来
                # 的，这里却写着「扫码连接的手机」—— 用户看到的就是一句错话。
                "web": "它连过来的",
                "history": "只剩记录（现在不在线）",
                "offline": "以前见过（现在不在线）",
            }.get(peer.source, peer.source or "未知来源")
        )
        if peer.same_host:
            where.append("就在本机")
        if not online:
            where.append("最后在线 " + _ago(float(caps.get("secondsAgo") or 0)))
        elif peer.rtt_ms and not is_web:
            where.append(f"{peer.rtt_ms:.0f} ms")
        self.where_label.setText(" · ".join(where))

        chips: list[tuple[str, str]] = []
        if peer.source in ("history", "offline") or not online:
            chips.append(("已离线", "offline"))
        elif peer.same_host:
            chips.append(("本机实例", "warn"))
        else:
            chips.append(("在线", "success"))
        # 设备类别：手机（App / 浏览器）与"被发现的一台电脑"是两种东西 ——
        # 前者是 HTTP 客户端、要靠它自己来取文件，后者能直接被推。
        # 这个芯片说的是"它是什么设备"，详情行说的才是"哪个客户端"，不重复。
        if is_web:
            chips.append(("手机", "info"))
        if peer.trusted:
            chips.append(("已信任", "trusted"))
        if str(caps.get("pin") or "").strip() or caps.get("needPin"):
            chips.append(("需要 PIN", "warn"))
        self._set_chips(chips)

        self.setToolTip(
            "\n".join(
                part
                for part in (name, self.detail_label.text(), self.where_label.text())
                if part
            )
        )

    # -- 活动行：现在在干什么 -------------------------------------------------

    def set_activity(self, text: str, state: str = "active") -> None:
        """页头写一句"现在在干什么"（传进度、等手机取、失败原因…）。

        ``state`` 只用来定颜色：``active`` 蓝、``done`` 绿、``failed`` 红、
        ``warn`` 黄、空串灰。文案为空 = 隐藏这一行（页头不留过期的话）。
        """
        text = str(text or "").strip()
        if not text:
            self.clear_activity()
            return
        self.activity_label.setText(text)
        colour = theme.STATUS_COLORS.get(
            {"active": "info", "done": "success", "failed": "danger", "warn": "warning"}.get(
                state, state
            ),
            theme.STATUS_COLORS["info"],
        )
        self.activity_dot.setStyleSheet(
            f"background: {colour}; border-radius: 4px;"
        )
        self.activity_row.setVisible(True)

    def clear_activity(self) -> None:
        self.activity_label.setText("")
        self.activity_row.setVisible(False)

    def activity_text(self) -> str:
        return self.activity_label.text()

    def set_conversation(
        self,
        title: str,
        subtitle: str,
        avatar: str = "👥",
        pill: str = "群聊",
        pill_state: str = "info",
    ) -> None:
        """Show a group chat (or any conversation that is not a device)."""
        self._peer = None
        self.avatar.setText(avatar)
        self.name_label.setText(title)
        self.name_label.setToolTip(title)
        self.detail_label.setText(subtitle)
        self.where_label.setText("")
        self._set_chips([(pill, pill_state)] if pill else [])

    #: 旧名字：以前顶部那个 `[会话｜传输]` 分段控件从这里取（宪章 §3.1 已删）。
    @property
    def segmented(self):
        raise AttributeError(
            "PeerHeader.segmented 已按宪章 §3.1 删除：页面入口只有侧栏底部导航；"
            "请用 MainWindow.show_page('session', view=...) 切换。"
        )


def _ago(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    return f"{int(seconds // 3600)} 小时前"


__all__ = ["PeerHeader"]
